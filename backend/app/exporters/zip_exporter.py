import zipfile
import re
import html
import asyncio
import hashlib
import mimetypes
import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import httpx
from app.exporters.base import BaseExporter
from app.models import ArticleItem
from app.config import BRAND_OFFICIAL_ACCOUNT, BRAND_FOOTER_NOTE, BRAND_DISCLAIMER

class ZipExporter(BaseExporter):
    """ZIP 全量打包导出器 (支持单文件合集 + 分篇独立文章 + 图片本地化离线下载 + 目录索引清单)"""

    def _get_platform_referer(self, url: str) -> Dict[str, str]:
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

    async def _download_images_task(
        self,
        articles: List[ArticleItem]
    ) -> Tuple[Dict[str, bytes], Dict[str, str]]:
        """并发下载所有文章内的图片，返回 (图片二进制字典, URL->本地相对路径映射字典)"""
        image_bytes_map: Dict[str, bytes] = {}
        url_to_filename_map: Dict[str, str] = {}

        # 收集所有独立图片 URL
        all_img_urls = set()
        for art in articles:
            for img_url in (art.images or []):
                if img_url and img_url.startswith("http"):
                    all_img_urls.add(img_url)
            # 从 markdown 中正则提取额外图片链接
            if art.content_markdown:
                found = re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', art.content_markdown)
                for f_url in found:
                    all_img_urls.add(f_url)

        if not all_img_urls:
            return image_bytes_map, url_to_filename_map

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            semaphore = asyncio.Semaphore(10) # 限制最大 10 并发，保护网络稳定

            async def fetch_one(img_url: str, idx: int):
                async with semaphore:
                    try:
                        headers = self._get_platform_referer(img_url)
                        resp = await client.get(img_url, headers=headers)
                        if resp.status_code == 200 and resp.content:
                            # 识别文件后缀
                            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                            ext = mimetypes.guess_extension(content_type) or ".png"
                            if ext in [".jpe", ".jpeg"]:
                                ext = ".jpg"
                            if not ext.startswith("."):
                                ext = f".{ext}"
                            
                            # 生成干净的文件名
                            url_hash = hashlib.md5(img_url.encode("utf-8")).hexdigest()[:8]
                            clean_filename = f"img_{idx:03d}_{url_hash}{ext}"
                            
                            image_bytes_map[clean_filename] = resp.content
                            url_to_filename_map[img_url] = clean_filename
                    except Exception:
                        pass # 下载失败则保持原链接

            tasks = [fetch_one(u, i) for i, u in enumerate(all_img_urls, 1)]
            await asyncio.gather(*tasks, return_exceptions=True)

        return image_bytes_map, url_to_filename_map

    def _generate_single_html(self, art: ArticleItem, idx: int, now_str: str, html_content: str) -> str:
        """生成单篇高颜值独立 HTML 文章"""
        safe_title = html.escape(art.title)
        safe_author = html.escape(art.author or self.author_name)
        safe_platform = html.escape(art.platform or self.platform)
        safe_time = html.escape(art.publish_time or "未知")
        safe_url = html.escape(art.url)

        return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_title} - {safe_author}</title>
    <style>
        :root[data-theme="light"] {{
            --bg-base: #f8fafc;
            --bg-surface: #ffffff;
            --text-primary: #0f172a;
            --text-secondary: #475569;
            --text-muted: #94a3b8;
            --border-color: #e2e8f0;
            --accent-color: #0284c7;
            --code-bg: #f1f5f9;
        }}
        :root[data-theme="dark"] {{
            --bg-base: #0b0f19;
            --bg-surface: #182234;
            --text-primary: #f8fafc;
            --text-secondary: #cbd5e1;
            --text-muted: #94a3b8;
            --border-color: rgba(255, 255, 255, 0.08);
            --accent-color: #38bdf8;
            --code-bg: #0b1120;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
            background-color: var(--bg-base);
            color: var(--text-primary);
            line-height: 1.75;
            padding: 30px 16px;
            transition: background-color 0.2s, color 0.2s;
        }}
        .article-container {{
            max-width: 840px;
            margin: 0 auto;
            background: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 40px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.04);
            transition: background-color 0.2s, border-color 0.2s;
        }}
        .top-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
        }}
        .badge {{
            display: inline-block;
            background: rgba(2, 132, 199, 0.1);
            color: var(--accent-color);
            padding: 3px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 700;
        }}
        .theme-btn {{
            background: var(--bg-base);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.76rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s;
        }}
        .theme-btn:hover {{ border-color: var(--accent-color); color: var(--accent-color); }}
        h1.title {{
            font-size: 1.8rem;
            font-weight: 800;
            line-height: 1.35;
            margin-bottom: 16px;
            color: var(--text-primary);
        }}
        .meta-bar {{
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            padding-bottom: 18px;
            margin-bottom: 24px;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.85rem;
            color: var(--text-secondary);
        }}
        .meta-bar a {{
            color: var(--accent-color);
            text-decoration: none;
        }}
        .meta-bar a:hover {{ text-decoration: underline; }}
        .markdown-body p {{ margin-bottom: 16px; font-size: 1.02rem; }}
        .markdown-body h2 {{ font-size: 1.35rem; margin: 28px 0 14px; border-bottom: 1px solid var(--border-color); padding-bottom: 6px; }}
        .markdown-body h3 {{ font-size: 1.15rem; margin: 20px 0 10px; }}
        .markdown-body code {{
            background: var(--code-bg);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: Consolas, monospace;
            font-size: 0.9em;
            color: #e11d48;
        }}
        .markdown-body pre {{
            background: var(--code-bg);
            border: 1px solid var(--border-color);
            padding: 16px 20px;
            border-radius: 8px;
            overflow-x: auto;
            margin-bottom: 18px;
        }}
        .markdown-body pre code {{ background: none; padding: 0; color: var(--text-primary); border: none; }}
        .markdown-body img {{ max-width: 100%; height: auto; border-radius: 8px; margin: 16px auto; display: block; }}
        .markdown-body blockquote {{
            border-left: 4px solid var(--accent-color);
            padding: 8px 16px;
            background: rgba(2, 132, 199, 0.05);
            color: var(--text-secondary);
            margin-bottom: 16px;
            border-radius: 0 6px 6px 0;
        }}
        .footer-note {{
            margin-top: 40px;
            padding-top: 16px;
            border-top: 1px solid var(--border-color);
            font-size: 0.78rem;
            color: var(--text-muted);
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="article-container">
        <div class="top-bar">
            <div class="badge">第 {idx} 篇 · {safe_platform}</div>
            <button class="theme-btn" onclick="toggleTheme()"><span id="themeText">🌙 暗黑</span></button>
        </div>
        <h1 class="title">{safe_title}</h1>
        <div class="meta-bar">
            <span>👤 作者：{safe_author}</span>
            <span>📅 时间：{safe_time}</span>
            {f'<span>🔗 <a href="{safe_url}" target="_blank" rel="noopener">查看原文</a></span>' if safe_url else ''}
        </div>
        <div class="markdown-body">
            {html_content}
        </div>
        <div class="footer-note">
            由 BlogDistiller 离线备份与归档 · 打包时间 {now_str}
        </div>
    </div>
    <script>
        function initTheme() {{
            const saved = localStorage.getItem('bd_single_theme') || 'light';
            document.documentElement.setAttribute('data-theme', saved);
            document.getElementById('themeText').innerText = saved === 'dark' ? '☀️ 浅色' : '🌙 暗黑';
        }}
        function toggleTheme() {{
            const cur = document.documentElement.getAttribute('data-theme') || 'light';
            const next = cur === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('bd_single_theme', next);
            document.getElementById('themeText').innerText = next === 'dark' ? '☀️ 浅色' : '🌙 暗黑';
        }}
        initTheme();
    </script>
</body>
</html>
"""

    def _generate_single_pdf_html(self, art: ArticleItem, idx: int, now_str: str, html_body: str) -> str:
        """生成适合单篇独立 PDF 打印的高清排版 HTML"""
        safe_title = html.escape(art.title)
        safe_author = html.escape(art.author or self.author_name)
        safe_platform = html.escape(art.platform or self.platform)
        safe_time = html.escape(art.publish_time or "未知")
        safe_url = html.escape(art.url)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{safe_title}</title>
    <style>
        @page {{
            size: A4;
            margin: 20mm 15mm 20mm 15mm;
            @bottom-right {{ content: counter(page); }}
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
            color: #1e293b;
            background: #ffffff;
            font-size: 11pt;
            line-height: 1.7;
            padding: 10px 20px;
        }}
        .header {{
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 14px;
            margin-bottom: 20px;
        }}
        .badge {{
            display: inline-block;
            font-size: 8.5pt;
            color: #0284c7;
            background: #e0f2fe;
            padding: 2px 8px;
            border-radius: 4px;
            margin-bottom: 8px;
            font-weight: 600;
        }}
        h1 {{ font-size: 19pt; color: #0f172a; line-height: 1.35; margin-bottom: 8px; }}
        .meta {{ font-size: 9pt; color: #64748b; display: flex; gap: 16px; flex-wrap: wrap; }}
        .markdown-body p {{ margin-bottom: 12px; text-align: justify; }}
        .markdown-body h2 {{ font-size: 14pt; margin-top: 18px; margin-bottom: 8px; color: #1e293b; }}
        .markdown-body h3 {{ font-size: 12pt; margin-top: 14px; margin-bottom: 6px; color: #334155; }}
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
            display: block;
            margin: 12px auto;
            border-radius: 4px;
        }}
        .markdown-body blockquote {{
            border-left: 3px solid #0284c7;
            padding: 6px 12px;
            background: #f8fafc;
            color: #475569;
            margin-bottom: 12px;
        }}
        .footer {{
            margin-top: 40px;
            padding-top: 12px;
            border-top: 1px dashed #cbd5e1;
            font-size: 8.5pt;
            color: #94a3b8;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="badge">第 {idx} 篇 · {safe_platform}</div>
        <h1>{safe_title}</h1>
        <div class="meta">
            <span>👤 作者：{safe_author}</span>
            <span>📅 时间：{safe_time}</span>
            {f'<span>🔗 原文：{safe_url}</span>' if safe_url else ''}
        </div>
    </div>
    <div class="markdown-body">
        {html_body}
    </div>
    <div class="footer">
        {html.escape(BRAND_FOOTER_NOTE)} · 打包时间 {now_str}
    </div>
</body>
</html>
"""

    def _generate_single_docx(self, art: ArticleItem) -> bytes:
        """生成单篇独立 Word 文档二进制流 (所见即所得、内嵌高清配图与富文本排版)"""
        import io
        from docx import Document
        from app.exporters.docx_exporter import append_article_content_to_docx
        
        doc = Document()
        append_article_content_to_docx(doc, art, self.author_name, self.platform, embed_images=True)
        bio = io.BytesIO()
        doc.save(bio)
        return bio.getvalue()

    async def export(
        self,
        articles: List[ArticleItem],
        filename_prefix: str,
        generated_files: Optional[Dict[str, Path]] = None,
        download_images: bool = False
    ) -> Path:
        zip_output_file = self.output_dir / f"{filename_prefix}_知识归档包.zip"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        generated_files = generated_files or {}

        safe_author = re.sub(r'[\\/:*?"<>|]', '_', self.author_name).strip() or "博主"
        
        # 1. 如果用户勾选了“下载配图 (本地化离线归档)”，开始并发下载并生成替换表
        image_bytes_map = {}
        url_to_filename = {}
        if download_images:
            image_bytes_map, url_to_filename = await self._download_images_task(articles)

        with zipfile.ZipFile(str(zip_output_file), "w", zipfile.ZIP_DEFLATED) as zf:
            # 2. 如果下载了图片，写入 images/ 文件夹
            if image_bytes_map:
                for img_name, img_data in image_bytes_map.items():
                    zf.writestr(f"images/{img_name}", img_data)

            # 3. 写入用户勾选生成的各个格式合并单文件
            format_names = {
                "md": f"合并总文档/【合并合集】{safe_author}_{self.platform}_文章合集.md",
                "html": f"合并总文档/【合并合集】{safe_author}_{self.platform}_离线网页电子书.html",
                "pdf": f"合并总文档/【合并合集】{safe_author}_{self.platform}_排版打印.pdf",
                "docx": f"合并总文档/【合并合集】{safe_author}_{self.platform}_Word文档.docx",
                "txt": f"合并总文档/【合并合集】{safe_author}_{self.platform}_纯文本语料.txt"
            }

            for fmt_key, file_path in generated_files.items():
                if file_path and file_path.exists():
                    arcname = format_names.get(fmt_key, f"合并总文档/{file_path.name}")
                    # 如果由于降级实际为 html 文件，保留其正确扩展名
                    if fmt_key == "pdf" and file_path.suffix == ".html":
                        arcname = f"合并总文档/【合并合集】{safe_author}_{self.platform}_排版打印.html"
                    zf.write(str(file_path), arcname=arcname)

            # 4. 生成并写入单篇独立文章 (根据用户选定格式精准提供相应独立文件)
            # A. Markdown 独立篇章 (始终默认提供便携式 Markdown)
            for idx, art in enumerate(articles, 1):
                clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                single_md_filename = f"单篇独立文章_Markdown/{idx:02d}_{clean_title[:45]}.md"

                yaml_header = (
                    f"---\n"
                    f"title: \"{art.title}\"\n"
                    f"author: \"{art.author or self.author_name}\"\n"
                    f"platform: \"{art.platform or self.platform}\"\n"
                    f"publish_time: \"{art.publish_time}\"\n"
                    f"url: \"{art.url}\"\n"
                    f"curator: \"微信公众号【{BRAND_OFFICIAL_ACCOUNT}】\"\n"
                    f"archived_at: \"{now_str}\"\n"
                    f"---\n\n"
                )
                
                md_body = art.content_markdown or ""
                # 若开启了图片本地化，将 Markdown 中的在线图片链接替换为相对路径 ../images/xxx
                if url_to_filename:
                    for online_url, local_img_name in url_to_filename.items():
                        md_body = md_body.replace(online_url, f"../images/{local_img_name}")

                single_md_content = yaml_header + f"# {art.title}\n\n" + md_body + f"\n\n> *{BRAND_FOOTER_NOTE}*\n"
                zf.writestr(single_md_filename, single_md_content.encode("utf-8"))

            # B. HTML 独立篇章（如果勾选了 HTML，生成高颜值独立单篇 HTML 文件）
            if "html" in generated_files:
                for idx, art in enumerate(articles, 1):
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                    single_html_filename = f"单篇独立文章_HTML/{idx:02d}_{clean_title[:45]}.html"
                    
                    html_body = art.content_html or ""
                    # 若开启了图片本地化，将 HTML 中的图片链接替换为相对路径 ../images/xxx
                    if url_to_filename:
                        for online_url, local_img_name in url_to_filename.items():
                            html_body = html_body.replace(online_url, f"../images/{local_img_name}")

                    single_html_content = self._generate_single_html(art, idx, now_str, html_body)
                    zf.writestr(single_html_filename, single_html_content.encode("utf-8"))

            # C. PDF 独立篇章（如果勾选了 PDF，批量快速渲染生成单篇独立 PDF）
            if "pdf" in generated_files:
                pdf_tasks = []
                for idx, art in enumerate(articles, 1):
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                    single_pdf_arcname = f"单篇独立文章_PDF/{idx:02d}_{clean_title[:45]}.pdf"
                    
                    html_body = art.content_html or ""
                    single_pdf_html = self._generate_single_pdf_html(art, idx, now_str, html_body)
                    pdf_tasks.append((single_pdf_arcname, single_pdf_html))

                def _render_single_pdfs_batch_sync(tasks: List[Tuple[str, str]]) -> Dict[str, bytes]:
                    res = {}
                    try:
                        from playwright.sync_api import sync_playwright
                        with sync_playwright() as p:
                            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
                            page = browser.new_page()
                            for arcname, html_text in tasks:
                                try:
                                    page.set_content(html_text, wait_until="domcontentloaded", timeout=12000)
                                    pdf_bytes = page.pdf(
                                        format="A4",
                                        print_background=True,
                                        margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
                                    )
                                    res[arcname] = pdf_bytes
                                except Exception:
                                    pass
                            browser.close()
                    except Exception:
                        pass
                    return res

                rendered_pdfs = await asyncio.to_thread(_render_single_pdfs_batch_sync, pdf_tasks)
                for arcname, pdf_data in rendered_pdfs.items():
                    zf.writestr(arcname, pdf_data)

            # D. Word 独立篇章（如果勾选了 docx）
            if "docx" in generated_files:
                for idx, art in enumerate(articles, 1):
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                    single_docx_filename = f"单篇独立文章_Word/{idx:02d}_{clean_title[:45]}.docx"
                    try:
                        single_docx_bytes = self._generate_single_docx(art)
                        zf.writestr(single_docx_filename, single_docx_bytes)
                    except Exception:
                        pass

            # E. TXT 纯文本独立篇章（如果勾选了 TXT）
            if "txt" in generated_files:
                for idx, art in enumerate(articles, 1):
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                    single_txt_filename = f"单篇独立文章_TXT/{idx:02d}_{clean_title[:45]}.txt"
                    txt_body = f"标题：{art.title}\n作者：{art.author or self.author_name}\n发布时间：{art.publish_time}\n原文链接：{art.url}\n\n" + (art.content_markdown or "") + f"\n\n[{BRAND_FOOTER_NOTE}]\n"
                    zf.writestr(single_txt_filename, txt_body.encode("utf-8"))

            # 5. 写入 00_目录与索引清单.md
            if self.platform in ["微信公众号", "wechat"] or "公众号" in self.platform:
                catalog_title = f"【{self.author_name}公众号合集】文章归档索引清单" if "公众号" not in self.author_name else f"【{self.author_name}文章合集】文章归档索引清单"
            else:
                catalog_title = f"【{self.author_name}】文章归档索引清单"

            manifest_lines = [
                f"# 📚 {catalog_title}",
                f"",
                f"> **排版整理**：微信公众号【{BRAND_OFFICIAL_ACCOUNT}】  ",
                f"> **来源平台**：{self.platform}  ",
                f"> **文章总数**：{len(articles)} 篇  ",
                f"> **打包时间**：{now_str}  ",
                f"> **免责声明**：{BRAND_DISCLAIMER}  ",
                f"> **图片模式**：{'📸 本地化离线存储 (已保存至 images/ 目录)' if url_to_filename else '🌐 在线 CDN 链接'}  ",
                f"",
                f"---",
                f"",
                f"## 📑 文章全量目录",
                f""
            ]

            for idx, art in enumerate(articles, 1):
                manifest_lines.append(f"{idx}. **{art.title}**")
                if art.publish_time:
                    manifest_lines.append(f"   - 发布时间: `{art.publish_time}`")
                if art.url:
                    manifest_lines.append(f"   - 原文链接: [{art.url}]({art.url})")

            manifest_content = "\n".join(manifest_lines)
            zf.writestr("00_目录与索引清单.md", manifest_content.encode("utf-8"))

        return zip_output_file
