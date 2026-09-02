import re
import html
import base64
import mimetypes
import asyncio
from pathlib import Path
from typing import List, Dict
import datetime
from bs4 import BeautifulSoup
import httpx

from app.exporters.base import BaseExporter
from app.models import ArticleItem
from app.config import BRAND_OFFICIAL_ACCOUNT, BRAND_FOOTER_NOTE, BRAND_DISCLAIMER


def _get_platform_referer(url: str) -> Dict[str, str]:
    """获取各平台防盗链 Referer 请求头"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }
    u = url.lower()
    if "csdn" in u:
        headers["Referer"] = "https://blog.csdn.net/"
    elif "qpic.cn" in u or "weixin" in u:
        headers["Referer"] = "https://mp.weixin.qq.com/"
    elif "zhihu" in u or "zhimg" in u:
        headers["Referer"] = "https://www.zhihu.com/"
    elif "sinaimg" in u or "weibo" in u or "sina" in u:
        headers["Referer"] = "https://weibo.com/"
    elif "juejin" in u:
        headers["Referer"] = "https://juejin.cn/"
    elif "cnblogs" in u:
        headers["Referer"] = "https://www.cnblogs.com/"
    elif "51cto" in u:
        headers["Referer"] = "https://blog.51cto.com/"
    return headers


class PDFExporter(BaseExporter):
    """PDF 单文件排版导出器 (内嵌高清离线 Base64 图片，100% 免疫防盗链与脱机渲染)"""

    async def _embed_images_as_base64(self, articles: List[ArticleItem]) -> Dict[str, str]:
        """并发抓取所有文章的插图并转换为 Base64 Data URI"""
        img_urls = set()
        for art in articles:
            if art.images:
                for u in art.images:
                    if u and u.startswith("http"):
                        img_urls.add(u)
            if art.content_html:
                soup = BeautifulSoup(art.content_html, "lxml")
                for img in soup.find_all("img"):
                    src = img.get("src") or img.get("data-src")
                    if src and src.startswith("http"):
                        img_urls.add(src)

        url_to_base64 = {}
        if not img_urls:
            return url_to_base64

        sem = asyncio.Semaphore(10)

        async def fetch_one(client: httpx.AsyncClient, u: str):
            async with sem:
                headers = _get_platform_referer(u)
                try:
                    resp = await client.get(u, headers=headers, timeout=12.0, follow_redirects=True)
                    if resp.status_code == 200 and len(resp.content) > 100:
                        content_type = resp.headers.get("content-type", "")
                        mime = content_type.split(";")[0].strip() if content_type else "image/png"
                        if not mime.startswith("image/"):
                            mime = "image/png"
                        b64_str = base64.b64encode(resp.content).decode("utf-8")
                        url_to_base64[u] = f"data:{mime};base64,{b64_str}"
                except Exception as e:
                    print(f"PDF 下载图片转 Base64 失败 [{u}]: {e}")

        async with httpx.AsyncClient(verify=False) as client:
            tasks = [fetch_one(client, u) for u in img_urls]
            await asyncio.gather(*tasks, return_exceptions=True)

        return url_to_base64

    async def export(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        output_file = self.output_dir / f"{filename_prefix}.pdf"
        fallback_html_file = self.output_dir / f"{filename_prefix}_for_pdf.html"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 预先将所有外部图片转换为 Base64，确保在 PDF 引擎中无损离线渲染
        base64_map = await self._embed_images_as_base64(articles)

        # 2. 生成适合 PDF 打印的高清优雅排版 HTML
        article_htmls = []
        for idx, art in enumerate(articles, 1):
            safe_title = html.escape(art.title)
            safe_author = html.escape(art.author or self.author_name)
            safe_time = html.escape(art.publish_time)

            # 替换正文中的图片 src 为 Base64
            content_html = art.content_html
            if base64_map and content_html:
                soup = BeautifulSoup(content_html, "lxml")
                for img in soup.find_all("img"):
                    src = img.get("src")
                    if src and src in base64_map:
                        img["src"] = base64_map[src]
                    elif img.get("data-src") and img.get("data-src") in base64_map:
                        img["src"] = base64_map[img.get("data-src")]
                content_html = soup.body.decode_contents() if soup.body else str(soup)

            article_htmls.append(f"""
                <div class="pdf-chapter">
                    <div class="chapter-header">
                        <div class="badge">第 {idx} 篇 · {art.platform or self.platform}</div>
                        <h1 class="chapter-title">{safe_title}</h1>
                        <div class="chapter-meta">
                            <span>作者：{safe_author}</span>
                            <span>发布时间：{safe_time or '未知'}</span>
                        </div>
                    </div>
                    <div class="chapter-body markdown-body">
                        {content_html}
                    </div>
                    <div class="pdf-article-footer">
                        <span>{html.escape(BRAND_FOOTER_NOTE)}</span>
                    </div>
                </div>
            """)

        # 构建符合平台特点的清晰大标题与副标题
        if self.platform in ["微信公众号", "wechat"] or "公众号" in self.platform:
            if "公众号" in self.author_name:
                cover_title = f"【{html.escape(self.author_name)}文章合集】"
            else:
                cover_title = f"【{html.escape(self.author_name)}公众号合集】"
            cover_sub = f"微信公众号文章全量归档 · 共 {len(articles)} 篇"
        else:
            cover_title = f"【{html.escape(self.author_name)}】"
            cover_sub = f"全量文章知识合集 · {html.escape(self.platform)}"

        pdf_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="referrer" content="no-referrer">
    <title>{cover_title}</title>
    <style>
        @page {{
            size: A4;
            margin: 20mm 15mm 20mm 15mm;
            @bottom-left {{
                content: "📖 微信公众号【{html.escape(BRAND_OFFICIAL_ACCOUNT)}】整理排版 · 仅供个人学习交流";
                font-size: 8pt;
                color: #94a3b8;
            }}
            @bottom-right {{
                content: counter(page);
                font-size: 8pt;
                color: #94a3b8;
            }}
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
            color: #1e293b;
            background: #ffffff;
            font-size: 11pt;
            line-height: 1.7;
            padding: 20px;
        }}
        .pdf-cover {{
            min-height: 80vh;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
            page-break-after: always;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 40px;
        }}
        .pdf-cover h1 {{
            font-size: 26pt;
            color: #0f172a;
            margin-bottom: 16px;
        }}
        .pdf-cover .subtitle {{
            font-size: 13pt;
            color: #64748b;
            margin-bottom: 32px;
        }}
        .pdf-cover .meta {{
            font-size: 10pt;
            color: #64748b;
            border-top: 1px solid #e2e8f0;
            padding-top: 16px;
            width: 85%;
            margin: 0 auto;
        }}
        .pdf-cover .disclaimer {{
            margin-top: 16px;
            font-size: 8.5pt;
            color: #475569;
            text-align: left;
            line-height: 1.6;
            background: #f8fafc;
            border-left: 3px solid #0284c7;
            padding: 10px 14px;
            border-radius: 4px;
        }}
        .pdf-chapter {{
            page-break-after: always;
            padding-top: 20px;
            margin-bottom: 30px;
        }}
        .chapter-header {{
            margin-bottom: 20px;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 12px;
        }}
        .badge {{
            display: inline-block;
            font-size: 8pt;
            color: #0284c7;
            background: #e0f2fe;
            padding: 2px 8px;
            border-radius: 4px;
            margin-bottom: 8px;
            font-weight: 600;
        }}
        .chapter-title {{
            font-size: 18pt;
            color: #0f172a;
            line-height: 1.3;
            margin-bottom: 8px;
        }}
        .chapter-meta {{
            font-size: 9pt;
            color: #64748b;
            display: flex;
            gap: 16px;
        }}
        .markdown-body p {{ margin-bottom: 12px; text-align: justify; }}
        .markdown-body h1, .markdown-body h2 {{ font-size: 14pt; margin-top: 16px; margin-bottom: 8px; color: #1e293b; }}
        .markdown-body h3 {{ font-size: 12pt; margin-top: 12px; margin-bottom: 6px; color: #334155; }}
        .markdown-body code {{
            background: #f1f5f9;
            padding: 2px 4px;
            border-radius: 3px;
            font-family: Consolas, monospace;
            font-size: 9.5pt;
            color: #e11d48;
        }}
        .markdown-body pre {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 12px;
            font-family: Consolas, monospace;
            font-size: 9pt;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        .markdown-body img {{
            max-width: 90%;
            height: auto;
            display: block;
            margin: 14px auto;
            border-radius: 6px;
        }}
        .markdown-body img.url-icon, 
        .markdown-body img[src*="sinaimg.cn/upload"], 
        .markdown-body img[src*="h5.sinaimg.cn"],
        .markdown-body img[src*="timeline_card"] {{
            display: none !important;
        }}
        .markdown-body img.emoji, .markdown-body img.face {{
            display: inline-block !important;
            width: 16px !important;
            height: 16px !important;
            vertical-align: middle !important;
            margin: 0 2px !important;
        }}
        .markdown-body blockquote {{
            border-left: 3px solid #0284c7;
            padding: 6px 12px;
            background: #f8fafc;
            color: #475569;
            margin-bottom: 12px;
        }}
        .pdf-article-footer {{
            margin-top: 24px;
            padding-top: 10px;
            border-top: 1px dashed #cbd5e1;
            font-size: 8.5pt;
            color: #94a3b8;
            text-align: center;
            font-style: italic;
        }}
    </style>
</head>
<body>
    <div class="pdf-cover">
        <h1>{cover_title}</h1>
        <div class="subtitle">{cover_sub}</div>
        <div class="meta">
            <p><strong>排版整理：</strong>微信公众号【{html.escape(BRAND_OFFICIAL_ACCOUNT)}】</p>
            <p><strong>文章总数：</strong>{len(articles)} 篇 · <strong>导出时间：</strong>{now_str}</p>
            <div class="disclaimer">
                {html.escape(BRAND_DISCLAIMER)}
            </div>
        </div>
    </div>
    {''.join(article_htmls)}
</body>
</html>
"""
        # 保存离线排版 HTML
        fallback_html_file.write_text(pdf_html, encoding="utf-8")

        # 使用同步 Playwright 渲染 PDF
        def _render_pdf_sync(html_text: str, target_pdf: Path) -> bool:
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-blink-features=AutomationControlled"
                        ]
                    )
                    page = browser.new_page()
                    page.set_content(html_text, wait_until="load", timeout=35000)
                    page.pdf(
                        path=str(target_pdf),
                        format="A4",
                        print_background=True,
                        margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
                    )
                    browser.close()
                return True
            except Exception as e:
                print(f"PDF 渲染生成失败: {e}")
                return False

        success = await asyncio.to_thread(_render_pdf_sync, pdf_html, output_file)
        if success and output_file.exists() and output_file.stat().st_size > 1000:
            return output_file

        return fallback_html_file
