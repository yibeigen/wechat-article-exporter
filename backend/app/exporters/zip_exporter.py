import zipfile
import re
import html
import asyncio
import hashlib
import mimetypes
import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
import httpx
from bs4 import BeautifulSoup
from app.exporters.base import BaseExporter
from app.models import ArticleItem
from app.config import BRAND_OFFICIAL_ACCOUNT, BRAND_FOOTER_NOTE, BRAND_DISCLAIMER
from app.exporters.pdf_exporter import find_system_browser
from app.core.image_helper import download_image_bytes, compress_image_bytes

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
        elif "jianshu" in u:
            headers["Referer"] = "https://www.jianshu.com/"
        return headers

    async def _download_images_task(
        self,
        articles: List[ArticleItem],
        temp_dir: Path,
        progress_callback: Optional[Any] = None,
        image_mode: str = "compressed"
    ) -> Dict[str, Dict[str, Any]]:
        """并发下载所有文章内的配图，并按用户选择的模式归档 (三选一)。

        模式说明：
        - "compressed" (推荐)：只存储极速轻量压缩图 (WebP/优化 JPEG，体积减小 85%+，秒开省内存)；
        - "original"：只存储高清原图 (体积大、加载慢，符合用户对"高清原图"耗时的预期)；
        - "none"：不下载任何配图，返回空映射，正文保留在线 CDN 链接。

        每张图只下载一次 (复用全局 LRU 字节缓存，同任务多格式不会重复拉取)，写盘也只写一份。
        """
        url_to_filename_map: Dict[str, Dict[str, Any]] = {}

        # "不配图"模式：直接返回空映射，全程零图片网络请求
        if image_mode == "none":
            return url_to_filename_map

        compressed_dir = temp_dir / "compressed"
        original_dir = temp_dir / "original"
        compressed_dir.mkdir(parents=True, exist_ok=True)
        original_dir.mkdir(parents=True, exist_ok=True)

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
            return url_to_filename_map

        total_imgs = len(all_img_urls)
        done_count = 0
        count_lock = asyncio.Lock()

        # 进度文案随模式变化，让用户清楚当前在做什么
        if image_mode == "original":
            mode_hint = "高清原图"
        else:
            mode_hint = "轻量压缩图"

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False, trust_env=False) as client:
            semaphore = asyncio.Semaphore(10) # 限制最大 10 并发，保护网络稳定

            async def fetch_one(img_url: str, idx: int):
                nonlocal done_count
                async with semaphore:
                    try:
                        res = await download_image_bytes(client, img_url)
                        if res:
                            img_bytes, mime = res
                            ext = mimetypes.guess_extension(mime) or ".png"
                            if ext in [".jpe", ".jpeg"]:
                                ext = ".jpg"
                            if not ext.startswith("."):
                                ext = f".{ext}"

                            url_hash = hashlib.md5(img_url.encode("utf-8")).hexdigest()[:8]

                            if image_mode == "original":
                                # 高清原图模式：原样保存，展示与备份同一份
                                raw_filename = f"img_{idx:05d}_{url_hash}_raw{ext}"
                                await asyncio.to_thread((original_dir / raw_filename).write_bytes, img_bytes)
                                url_to_filename_map[img_url] = {
                                    "thumb": f"images/original/{raw_filename}",
                                    "thumb_name": raw_filename,
                                    "original": "",
                                    "original_name": "",
                                    "is_compressed": False
                                }
                            else:
                                # 压缩图模式 (推荐)：只生成轻量 WebP/优化 JPEG，杜绝原图体积翻倍
                                comp_bytes, comp_ext, comp_mime = compress_image_bytes(img_bytes, max_width=1200, quality=75)
                                thumb_filename = f"img_{idx:05d}_{url_hash}{comp_ext}"
                                await asyncio.to_thread((compressed_dir / thumb_filename).write_bytes, comp_bytes)
                                url_to_filename_map[img_url] = {
                                    "thumb": f"images/{thumb_filename}",
                                    "thumb_name": thumb_filename,
                                    "original": "",
                                    "original_name": "",
                                    "is_compressed": True
                                }
                    except Exception:
                        pass # 下载失败则保持原链接
                    finally:
                        async with count_lock:
                            done_count += 1
                            current = done_count
                        # 每 40 张或最后一张广播一次实时进度
                        if progress_callback and (current % 40 == 0 or current == total_imgs):
                            try:
                                await progress_callback(f"正在智能归档文章配图 ({mode_hint}, {current}/{total_imgs} 张)...")
                            except Exception:
                                pass

            tasks = [fetch_one(u, i) for i, u in enumerate(all_img_urls, 1)]
            await asyncio.gather(*tasks, return_exceptions=True)

        return url_to_filename_map

    def _generate_single_html(self, art: ArticleItem, idx: int, now_str: str, html_content: str) -> str:
        """生成单篇高颜值独立 HTML 文章"""
        safe_title = html.escape(art.title)
        safe_author = html.escape(art.author or self.author_name)
        safe_platform = html.escape(art.platform or self.platform)
        safe_time = html.escape(art.publish_time or "未知")
        safe_url = html.escape(art.url)

        # 标签横向展示
        tags_html = ""
        if art.tags:
            tag_spans = "".join([f'<span class="badge" style="background:#e0f2fe; color:#0284c7; margin-right:6px; font-weight:500;">#{html.escape(t.strip().lstrip("#"))}</span>' for t in art.tags if t.strip()])
            tags_html = f'<div style="margin-bottom: 12px; display:flex; flex-wrap:wrap; gap:6px;">{tag_spans}</div>'

        # 剔除正文头部重复的标题
        if html_content:
            soup = BeautifulSoup(html_content, "lxml")
            for top_h in soup.find_all(["h1", "h2", "h3"]):
                if top_h.text.strip().lower() == art.title.strip().lower():
                    top_h.decompose()
                    break
            html_content = soup.body.decode_contents() if soup.body else str(soup)

        return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="referrer" content="no-referrer">
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
        .markdown-body figure, .markdown-body .image-package {{ margin: 16px auto; text-align: center; max-width: 100%; }}
        .markdown-body figcaption, .markdown-body .image-caption {{ font-size: 0.85rem; color: var(--text-muted); margin-top: 6px; text-align: center; }}
        .markdown-body .image-container {{ max-width: 100% !important; max-height: none !important; height: auto !important; }}
        .markdown-body .image-container-fill {{ display: none !important; }}
        .markdown-body .image-view {{ position: static !important; width: 100% !important; height: auto !important; }}
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
        {tags_html}
        <div class="meta-bar">
            <span>📰 <strong>原文：</strong>{safe_platform} · {safe_author} · {safe_time}</span>
            {f'<span>🔗 <strong>原文链接：</strong><a href="{safe_url}" target="_blank" rel="noopener">{safe_url}</a></span>' if safe_url else ''}
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

        function toggleImageQuality(img) {{
            if (!img) return;
            const isOrig = img.getAttribute('data-is-original') === 'true';
            const origSrc = img.getAttribute('data-original');
            const thumbSrc = img.getAttribute('data-thumb');
            const figure = img.closest('figure');
            const badge = figure ? figure.querySelector('.img-badge-status') : null;
            const btn = figure ? figure.querySelector('.toggle-img-btn') : null;

            if (!isOrig && origSrc) {{
                img.src = origSrc;
                img.setAttribute('data-is-original', 'true');
                if (badge) {{
                    badge.textContent = '🔍 高清原图';
                    badge.style.background = 'rgba(234, 88, 12, 0.92)';
                }}
                if (btn) {{
                    btn.innerHTML = '⚡ 还原为轻量压缩图 <span style="font-size:11px;color:#0284c7;opacity:0.9;">(推荐·更省内存)</span>';
                    btn.style.color = '#0284c7';
                }}
            }} else if (thumbSrc) {{
                img.src = thumbSrc;
                img.setAttribute('data-is-original', 'false');
                if (badge) {{
                    badge.textContent = '⚡ 轻量压缩图';
                    badge.style.background = 'rgba(2, 132, 199, 0.92)';
                }}
                if (btn) {{
                    btn.innerHTML = '🔍 查看高清原图 <span style="font-size:11px;color:#dc2626;opacity:0.9;">(⚠️加载慢·耗内存)</span>';
                    btn.style.color = '#ea580c';
                }}
            }}
        }}
        window.toggleImageQuality = toggleImageQuality;
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

        # 标签横向展示
        tags_html = ""
        if art.tags:
            tag_spans = "".join([f'<span class="badge" style="background:#e0f2fe; color:#0284c7; margin-right:6px; font-weight:500;">#{html.escape(t.strip().lstrip("#"))}</span>' for t in art.tags if t.strip()])
            tags_html = f'<div style="margin-top:4px; margin-bottom: 8px; display:flex; flex-wrap:wrap; gap:6px;">{tag_spans}</div>'

        # 剔除正文头部重复的标题
        if html_body:
            soup = BeautifulSoup(html_body, "lxml")
            for top_h in soup.find_all(["h1", "h2", "h3"]):
                if top_h.text.strip().lower() == art.title.strip().lower():
                    top_h.decompose()
                    break
            html_body = soup.body.decode_contents() if soup.body else str(soup)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="referrer" content="no-referrer">
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
        .markdown-body figure, .markdown-body .image-package {{
            margin: 14px auto;
            text-align: center;
            max-width: 100%;
        }}
        .markdown-body figcaption, .markdown-body .image-caption {{
            font-size: 8.5pt;
            color: #64748b;
            margin-top: 4px;
            text-align: center;
        }}
        .markdown-body .image-container {{
            max-width: 100% !important;
            max-height: none !important;
            height: auto !important;
        }}
        .markdown-body .image-container-fill {{
            display: none !important;
        }}
        .markdown-body .image-view {{
            position: static !important;
            width: 100% !important;
            height: auto !important;
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
        {tags_html}
        <div class="meta">
            <span>📰 <strong>原文：</strong>{safe_platform} · {safe_author} · {safe_time}</span>
            {f'<span>🔗 <strong>原文链接：</strong><a href="{safe_url}" style="color:#0284c7;">{safe_url}</a></span>' if safe_url else ''}
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

    def _generate_single_docx(self, art: ArticleItem, embed_images: bool = True, img_bytes_map: Optional[Dict[str, bytes]] = None) -> bytes:
        """生成单篇独立 Word 文档二进制流 (所见即所得、内嵌所选模式配图与富文本排版)

        img_bytes_map: url -> 图片字节。由调用方传入 (本地已下载文件读盘而来，零额外网络请求)。
        """
        import io
        from docx import Document
        from app.exporters.docx_exporter import append_article_content_to_docx

        doc = Document()
        append_article_content_to_docx(doc, art, self.author_name, self.platform, embed_images=embed_images, img_bytes_map=img_bytes_map)
        bio = io.BytesIO()
        doc.save(bio)
        return bio.getvalue()

    async def export(
        self,
        articles: List[ArticleItem],
        filename_prefix: str,
        generated_files: Optional[Dict[str, Any]] = None,
        image_mode: str = "compressed",
        progress_callback: Optional[Any] = None
    ) -> Path:
        """generated_files 的值支持 Path 或 List[Path] (PDF 多卷拆分时传入多份文件)

        image_mode: "compressed" 压缩图(推荐) / "original" 高清原图 / "none" 不配图。
        """
        zip_output_file = self.output_dir / f"{filename_prefix}_知识归档包.zip"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        generated_files = generated_files or {}

        safe_author = re.sub(r'[\\/:*?"<>|]', '_', self.author_name).strip() or "博主"

        import tempfile, shutil
        temp_dir = Path(tempfile.mkdtemp(prefix="bd_imgs_"))

        # 1. 按用户选择的配图模式开始并发下载归档 (none 模式直接跳过，正文保留在线链接)
        url_to_filename = {}
        if image_mode != "none":
            if progress_callback:
                await progress_callback("正在扫描并下载文章配图离线归档...")
            url_to_filename = await self._download_images_task(articles, temp_dir, progress_callback, image_mode)
            if progress_callback:
                await progress_callback(f"配图离线下载完成 (共 {len(url_to_filename)} 张)，正在写入压缩包...")

        if progress_callback:
            await progress_callback("正在初始化 ZIP 归档包结构并写入全量合并总文档...")

        with zipfile.ZipFile(str(zip_output_file), "w", zipfile.ZIP_DEFLATED) as zf:
            # ---------- 提速辅助：分块并发生成单篇独立文章 ----------
            # 原实现是上千篇文章在主线程串行循环 + 同步写入 ZIP，事件循环被长时间阻塞，
            # 期间所有进度广播都发不出去，前端日志就会长时间空白（观感像卡死）。
            # 现在把每篇文章的生成扔进线程池并发执行，每块生成完上报一次进度，
            # 主线程只负责把已生成的字节串行写回 ZIP (zipfile 非线程安全，必须主线程写)。
            async def _build_single_articles(
                total: int,
                worker,
                label: str,
                chunk_size: int = 100,
                n_workers: int = 8,
                comp_type: int = zipfile.ZIP_DEFLATED
            ):
                for start in range(0, total, chunk_size):
                    end = min(start + chunk_size, total)
                    if progress_callback:
                        try:
                            await progress_callback(f"正在生成{label} ({start}/{total})...")
                        except Exception:
                            pass
                    sem = asyncio.Semaphore(n_workers)
                    items = [None] * (end - start)

                    async def _one(pos: int, idx: int):
                        async with sem:
                            items[pos] = await asyncio.to_thread(worker, idx)

                    await asyncio.gather(*(_one(p, i) for p, i in enumerate(range(start, end))))
                    for arcname, data in items:
                        if data:
                            zf.writestr(arcname, data, compress_type=comp_type)
                if progress_callback:
                    try:
                        await progress_callback(f"正在生成{label} ({total}/{total})...")
                    except Exception:
                        pass

            # 2. 如果下载了图片，从临时目录流式写入 images/ 文件夹 (仅包含所选模式的图片)
            if url_to_filename:
                for item in url_to_filename.values():
                    if isinstance(item, dict):
                        # 展示用图片 (压缩模式=压缩图；原图模式=原图)，只写这一份
                        sub_dir = "compressed" if item.get("is_compressed") else "original"
                        t_path = temp_dir / sub_dir / item["thumb_name"]
                        if t_path.exists():
                            # 配图已是 WebP/JPEG 等压缩编码，DEFLATE 二次压缩几乎无收益，改用 STORED 免去纯 CPU 开销
                            zf.write(str(t_path), arcname=item["thumb"], compress_type=zipfile.ZIP_STORED)
                        # 兼容兜底：若仍有额外原图字段（旧数据），才额外写入
                        if item.get("original") and item.get("original_name"):
                            o_path = temp_dir / "original" / item["original_name"]
                            if o_path.exists():
                                zf.write(str(o_path), arcname=item["original"], compress_type=zipfile.ZIP_STORED)
                    else:
                        img_path = temp_dir / item
                        if img_path.exists():
                            zf.write(str(img_path), arcname=f"images/{item}", compress_type=zipfile.ZIP_STORED)

            # 3. 写入用户勾选生成的各个格式合并单文件 (值可为单文件或多卷文件列表)
            format_names = {
                "md": f"合并总文档/【合并合集】{safe_author}_{self.platform}_文章合集.md",
                "html": f"合并总文档/【合并合集】{safe_author}_{self.platform}_离线网页电子书.html",
                "original_html": f"合并总文档/【网站原版】{safe_author}_{self.platform}_电脑端经典原版.html",
                "pdf": f"合并总文档/【合并合集】{safe_author}_{self.platform}_排版打印.pdf",
                "docx": f"合并总文档/【合并合集】{safe_author}_{self.platform}_Word文档.docx",
                "txt": f"合并总文档/【合并合集】{safe_author}_{self.platform}_纯文本语料.txt"
            }

            for fmt_key, file_value in generated_files.items():
                file_list = file_value if isinstance(file_value, list) else [file_value]
                for f_idx, file_path in enumerate(file_list, 1):
                    if not file_path or not file_path.exists():
                        continue
                    arcname = format_names.get(fmt_key, f"合并总文档/{file_path.name}")
                    # 多卷文件：在文件名中体现卷号
                    if len(file_list) > 1:
                        stem = arcname.rsplit(".", 1)
                        arcname = f"{stem[0]}_第{f_idx}卷.{stem[1]}" if len(stem) == 2 else f"{arcname}_第{f_idx}卷"
                    # 如果由于降级实际为 html 文件，保留其正确扩展名
                    if fmt_key == "pdf" and file_path.suffix == ".html":
                        arcname = arcname.rsplit(".", 1)[0] + ".html"
                    # PDF/DOCX 内部已是压缩二进制，STORED 免二次压缩；纯文本类仍用 DEFLATE 压缩
                    comp_type = zipfile.ZIP_STORED if fmt_key in ("pdf", "docx") else zipfile.ZIP_DEFLATED
                    zf.write(str(file_path), arcname=arcname, compress_type=comp_type)

            # 4. 生成并写入单篇独立文章 (根据用户选定格式精准提供相应独立文件)
            # A. Markdown 独立篇章 (始终默认提供便携式 Markdown，严格对齐用户标准格式：标题 -> 标签 -> 原文 -> 原文链接 -> 正文)
            # 由原来的主线程串行循环改为分块线程池并发生成，块间上报进度 (见 _build_single_articles)
            def _md_single_worker(idx: int):
                art = articles[idx - 1]
                clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                single_md_filename = f"单篇独立文章_Markdown/{idx:02d}_{clean_title[:45]}.md"

                tags_str = "   ".join([f"#{t.strip().lstrip('#')}" for t in art.tags if t.strip()]) if art.tags else ""
                tag_line = f"**标签**：{tags_str}\n\n" if tags_str else ""
                url_line = f"**原文链接**：{art.url}\n\n" if art.url else ""
                clean_header = (
                    f"# {art.title}\n\n"
                    f"{tag_line}"
                    f"**原文**：{art.platform or self.platform}  ·  {art.author or self.author_name}  ·  {art.publish_time or '未知时间'}\n\n"
                    f"{url_line}"
                    f"---\n\n"
                )

                md_body = art.content_markdown or ""
                # 若已本地化配图，将 Markdown 中的在线图片链接替换为相对路径（按所选模式展示，不再同时引用原图）
                if url_to_filename:
                    # 压缩模式追加一句轻量提示；原图模式提示来源
                    mode_note = (
                        "> ⚡ *已加载轻量压缩图（极速秒开·内存极低）*\n\n"
                        if image_mode == "compressed"
                        else "> 🖼️ *已加载高清原图（体积较大）*\n\n"
                    )
                    for online_url, item in url_to_filename.items():
                        if isinstance(item, dict):
                            thumb_path = item["thumb"]
                            md_card = f"\n\n![文章配图](../{thumb_path})\n{mode_note}"
                            md_body = re.sub(r'!\[.*?\]\(' + re.escape(online_url) + r'\)', md_card, md_body)
                            md_body = md_body.replace(online_url, f"../{thumb_path}")
                        else:
                            md_body = md_body.replace(online_url, f"../images/{item}")

                # 剔除正文头部重复的标题行
                md_body_lines = md_body.split("\n")
                b_idx = 0
                while b_idx < len(md_body_lines):
                    l_str = md_body_lines[b_idx].strip()
                    if not l_str:
                        b_idx += 1
                        continue
                    l_no_hash = re.sub(r'^#+\s*', '', l_str).strip()
                    if l_no_hash.lower() == art.title.strip().lower() or l_str.startswith(art.title):
                        b_idx += 1
                        continue
                    break
                md_body_clean = "\n".join(md_body_lines[b_idx:]).strip()

                single_md_content = clean_header + md_body_clean + f"\n\n> *{BRAND_FOOTER_NOTE}*\n"
                return (single_md_filename, single_md_content.encode("utf-8"))

            await _build_single_articles(len(articles), _md_single_worker, "单篇独立 Markdown")

            # B. HTML 独立篇章（如果勾选了 HTML，生成高颜值独立单篇 HTML 文件）
            if "html" in generated_files:
                # 由原来的主线程串行循环改为分块线程池并发生成，块间上报进度 (见 _build_single_articles)
                def _html_single_worker(idx: int):
                    art = articles[idx - 1]
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                    single_html_filename = f"单篇独立文章_HTML/{idx:02d}_{clean_title[:45]}.html"

                    html_body = art.content_html or ""
                    # 若已本地化配图，将 HTML 中的图片链接替换为所选模式的单图卡片 (三选一模式下不再做双图切换)
                    if url_to_filename:
                        # 压缩模式提示秒开省内存；原图模式提示体积大
                        badge_text = "⚡ 轻量压缩图" if image_mode == "compressed" else "🖼️ 高清原图"
                        badge_color = "#0284c7" if image_mode == "compressed" else "#ea580c"
                        for online_url, item in url_to_filename.items():
                            if isinstance(item, dict):
                                thumb_path = item["thumb"]
                                html_card = (
                                    f'<figure class="article-image-card" style="margin: 24px auto; text-align: center; max-width: 100%;">'
                                    f'  <div style="position: relative; display: inline-block; max-width: 100%;">'
                                    f'    <img src="../{thumb_path}" '
                                    f'         alt="文章配图" '
                                    f'         class="article-img-responsive" '
                                    f'         loading="lazy" '
                                    f'         style="display: block; max-width: 100%; height: auto; margin: 0 auto; border-radius: 8px; box-shadow: 0 4px 14px rgba(0,0,0,0.08);" />'
                                    f'    <span class="img-badge-status" style="position: absolute; top: 10px; right: 10px; background: {badge_color}; color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 12px; backdrop-filter: blur(4px); box-shadow: 0 2px 6px rgba(0,0,0,0.15); pointer-events: none;">{badge_text}</span>'
                                    f'  </div>'
                                    f'</figure>'
                                )
                                img_pattern = re.compile(r'<img\b[^>]*?(?:src|data-src|data-original-src)=["\']' + re.escape(online_url) + r'["\'][^>]*>', re.IGNORECASE)
                                if img_pattern.search(html_body):
                                    html_body = img_pattern.sub(html_card, html_body)
                                else:
                                    html_body = html_body.replace(online_url, f"../{thumb_path}")
                            else:
                                html_body = html_body.replace(online_url, f"../images/{item}")

                    single_html_content = self._generate_single_html(art, idx, now_str, html_body)
                    return (single_html_filename, single_html_content.encode("utf-8"))

                await _build_single_articles(len(articles), _html_single_worker, "单篇独立 HTML")

            # C. PDF 独立篇章（仅当勾选了 PDF 且文章总数 <= 20 篇时才生成单篇独立 PDF）
            # 对于大规模批量文章（如数十到数千篇），单篇独立调用数千次无头浏览器将造成数小时的严重卡死与内存耗尽。
            # 全量博文的高清排版打印文件已在【合并总文档】中完整提供。
            if "pdf" in generated_files:
                if len(articles) <= 20:
                    if progress_callback:
                        await progress_callback(f"正在快速批量渲染单篇独立 PDF (共 {len(articles)} 篇)...")
                    pdf_tasks = []
                    for idx, art in enumerate(articles, 1):
                        clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                        single_pdf_arcname = f"单篇独立文章_PDF/{idx:02d}_{clean_title[:45]}.pdf"
                        
                        html_body = art.content_html or ""
                        single_pdf_html = self._generate_single_pdf_html(art, idx, now_str, html_body)
                        pdf_tasks.append((single_pdf_arcname, single_pdf_html))

                    def _render_single_pdfs_batch_sync(tasks: List[Tuple[str, str]]) -> Dict[str, bytes]:
                        res = {}
                        # 1. 优先使用系统 Edge / Chrome 无头打印
                        browser_path = find_system_browser()
                        if browser_path:
                            import tempfile, subprocess
                            with tempfile.TemporaryDirectory() as temp_dir:
                                temp_dir_p = Path(temp_dir)
                                for t_idx, (arcname, html_text) in enumerate(tasks):
                                    try:
                                        temp_html = temp_dir_p / f"temp_{t_idx}.html"
                                        temp_pdf = temp_dir_p / f"temp_{t_idx}.pdf"
                                        temp_html.write_text(html_text, encoding="utf-8")
                                        cmd = [
                                            browser_path,
                                            "--headless",
                                            "--disable-gpu",
                                            "--no-pdf-header-footer",
                                            f"--print-to-pdf={str(temp_pdf.resolve())}",
                                            str(temp_html.resolve())
                                        ]
                                        subprocess.run(cmd, capture_output=True, timeout=25)
                                        if temp_pdf.exists() and temp_pdf.stat().st_size > 1000:
                                            res[arcname] = temp_pdf.read_bytes()
                                    except Exception:
                                        pass
                            if res:
                                return res

                        # 2. 次选 Playwright 同步渲染
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
                        # PDF 内部已是压缩二进制，STORED 免二次压缩
                        zf.writestr(arcname, pdf_data, compress_type=zipfile.ZIP_STORED)
                else:
                    if progress_callback:
                        await progress_callback(f"检测到文章篇数较多 ({len(articles)} 篇)，已在【合并总文档】中提供全量高清排版文件，自动免去数千次浏览器启停...")

            # D. Word 独立篇章（如果勾选了 docx）
            if "docx" in generated_files:
                # 从已下载的本地图片文件构建 字节映射 (直接读盘复用，零额外网络请求；未下载则为空)
                docx_img_map: Dict[str, bytes] = {}
                if url_to_filename:
                    for online_url, item in url_to_filename.items():
                        if isinstance(item, dict):
                            sub_dir = "compressed" if item.get("is_compressed") else "original"
                            p = temp_dir / sub_dir / item["thumb_name"]
                            if p.exists():
                                try:
                                    docx_img_map[online_url] = p.read_bytes()
                                except Exception:
                                    pass
                # 由原来的主线程串行循环改为分块线程池并发生成，块间上报进度 (见 _build_single_articles)
                def _docx_single_worker(idx: int):
                    art = articles[idx - 1]
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                    single_docx_filename = f"单篇独立文章_Word/{idx:02d}_{clean_title[:45]}.docx"
                    try:
                        single_docx_bytes = self._generate_single_docx(art, embed_images=image_mode != "none", img_bytes_map=docx_img_map)
                        return (single_docx_filename, single_docx_bytes)
                    except Exception:
                        return (single_docx_filename, b"")

                # DOCX 内部已是压缩 XML 二进制，写入 ZIP 用 STORED 免二次压缩
                await _build_single_articles(len(articles), _docx_single_worker, "单篇独立 Word", comp_type=zipfile.ZIP_STORED)

            # E. TXT 纯文本独立篇章（如果勾选了 TXT）
            if "txt" in generated_files:
                # 由原来的主线程串行循环改为分块线程池并发生成，块间上报进度 (见 _build_single_articles)
                def _txt_single_worker(idx: int):
                    art = articles[idx - 1]
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', art.title).strip() or f"文章_{idx}"
                    single_txt_filename = f"单篇独立文章_TXT/{idx:02d}_{clean_title[:45]}.txt"
                    txt_body = f"标题：{art.title}\n作者：{art.author or self.author_name}\n发布时间：{art.publish_time}\n原文链接：{art.url}\n\n" + (art.content_markdown or "") + f"\n\n[{BRAND_FOOTER_NOTE}]\n"
                    return (single_txt_filename, txt_body.encode("utf-8"))

                await _build_single_articles(len(articles), _txt_single_worker, "单篇独立 TXT")

            # 5. 写入 00_目录与索引清单.md
            if self.platform in ["微信公众号", "wechat"] or "公众号" in self.platform:
                catalog_title = f"【{self.author_name}公众号合集】文章归档索引清单" if "公众号" not in self.author_name else f"【{self.author_name}文章合集】文章归档索引清单"
            else:
                catalog_title = f"【{self.author_name}】文章归档索引清单"

            # 图片模式描述随用户三选一变化
            if image_mode == "none":
                img_mode_desc = "🌐 在线 CDN 链接 (未下载配图)"
            elif image_mode == "original":
                img_mode_desc = "🖼️ 高清原图本地化存储 (images/ 目录 · 体积较大加载慢)"
            else:
                img_mode_desc = "⚡ 极速轻量压缩图本地化存储 (images/ 目录 · 体积减小 85%+ 秒开省内存)"

            manifest_lines = [
                f"# 📚 {catalog_title}",
                f"",
                f"> **排版整理**：微信公众号【{BRAND_OFFICIAL_ACCOUNT}】  ",
                f"> **来源平台**：{self.platform}  ",
                f"> **文章总数**：{len(articles)} 篇  ",
                f"> **打包时间**：{now_str}  ",
                f"> **免责声明**：{BRAND_DISCLAIMER}  ",
                f"> **图片模式**：{img_mode_desc}  ",
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

        # 图片临时目录清理 (无论成功与否都尽力释放磁盘)
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

        return zip_output_file
