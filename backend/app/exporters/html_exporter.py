import html
from pathlib import Path
from typing import List
import datetime
from app.exporters.base import BaseExporter
from app.models import ArticleItem
from app.config import BRAND_OFFICIAL_ACCOUNT, BRAND_FOOTER_NOTE, BRAND_DISCLAIMER

class HTMLExporter(BaseExporter):
    """HTML 响应式离线电子书导出器 (默认优雅浅色阅读排版，支持一键切换暗黑模式)"""

    async def export(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        output_file = self.output_dir / f"{filename_prefix}.html"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 构建目录项与文章正文
        toc_items = []
        article_sections = []

        for idx, art in enumerate(articles, 1):
            safe_title = html.escape(art.title)
            safe_author = html.escape(art.author or self.author_name)
            safe_time = html.escape(art.publish_time)
            safe_url = html.escape(art.url)
            art_id = f"art-{idx}"

            toc_items.append(f"""
                <a href="#{art_id}" class="toc-link" onclick="highlightToc(this)">
                    <span class="toc-num">{idx:02d}</span>
                    <span class="toc-text">{safe_title}</span>
                </a>
            """)

            article_sections.append(f"""
                <article id="{art_id}" class="article-card">
                    <header class="article-header">
                        <div class="article-meta-badge">{html.escape(art.platform or self.platform)} · 第 {idx} 篇</div>
                        <h2 class="article-title">{safe_title}</h2>
                        <div class="article-meta">
                            <span>👤 {safe_author}</span>
                            <span>🕒 {safe_time or '未知时间'}</span>
                            {f'<span>🔗 <a href="{safe_url}" target="_blank" rel="noopener">查看原文</a></span>' if safe_url else ''}
                        </div>
                    </header>
                    <div class="article-content markdown-body">
                        {art.content_html}
                    </div>
                    <div class="article-footer-watermark">
                        <span>{html.escape(BRAND_FOOTER_NOTE)}</span>
                    </div>
                </article>
            """)

        # 构建符合平台特点的清晰大标题与副标题
        if self.platform in ["微信公众号", "wechat"] or "公众号" in self.platform:
            if "公众号" in self.author_name:
                cover_title = f"【{html.escape(self.author_name)}文章合集】"
            else:
                cover_title = f"【{html.escape(self.author_name)}公众号合集】"
            cover_sub = f"共 {len(articles)} 篇文章 · 微信公众号"
        else:
            cover_title = f"【{html.escape(self.author_name)}】文章知识合集"
            cover_sub = f"共 {len(articles)} 篇文章 · {html.escape(self.platform)}"

        html_template = f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="referrer" content="no-referrer">
    <title>{cover_title} - BlogDistiller 离线电子书</title>
    <style>
        /* ==============================================
           自适应双主题系统 (默认浅色阅读排版)
           ============================================== */
        :root[data-theme="light"] {{
            --bg-base: #f8fafc;
            --sidebar-bg: #f1f5f9;
            --card-bg: #ffffff;
            --card-border: #e2e8f0;
            --text-main: #0f172a;
            --text-secondary: #334155;
            --text-muted: #64748b;
            --accent: #0284c7;
            --accent-subtle: rgba(2, 132, 199, 0.08);
            --code-bg: #f1f5f9;
            --code-border: #e2e8f0;
            --shadow-card: 0 4px 16px rgba(0, 0, 0, 0.04), 0 1px 3px rgba(0, 0, 0, 0.03);
            --cover-gradient: linear-gradient(145deg, #ffffff, #f8fafc);
        }}

        :root[data-theme="dark"] {{
            --bg-base: #0b0f19;
            --sidebar-bg: #111827;
            --card-bg: #182234;
            --card-border: rgba(255, 255, 255, 0.08);
            --text-main: #f8fafc;
            --text-secondary: #cbd5e1;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --accent-subtle: rgba(56, 189, 248, 0.15);
            --code-bg: #0b1120;
            --code-border: #1e293b;
            --shadow-card: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
            --cover-gradient: linear-gradient(145deg, #182234, #111827);
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
            background-color: var(--bg-base);
            color: var(--text-main);
            display: flex;
            height: 100vh;
            overflow: hidden;
            line-height: 1.75;
            transition: background-color 0.2s, color 0.2s;
        }}

        /* 侧边栏 */
        #sidebar {{
            width: 330px;
            background: var(--sidebar-bg);
            border-right: 1px solid var(--card-border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            z-index: 10;
            transition: background-color 0.2s, border-color 0.2s;
        }}

        .sidebar-header {{
            padding: 18px 20px;
            border-bottom: 1px solid var(--card-border);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .author-title {{
            font-size: 1.05rem;
            font-weight: 800;
            color: var(--text-main);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .author-sub {{
            font-size: 0.78rem;
            color: var(--text-muted);
            margin-top: 2px;
        }}

        .theme-toggle-btn {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            color: var(--text-main);
            padding: 6px 10px;
            border-radius: 6px;
            font-size: 0.76rem;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 4px;
            transition: all 0.15s;
            flex-shrink: 0;
        }}
        .theme-toggle-btn:hover {{
            border-color: var(--accent);
            color: var(--accent);
        }}

        .search-box {{
            padding: 12px 18px;
            border-bottom: 1px solid var(--card-border);
        }}
        .search-box input {{
            width: 100%;
            padding: 8px 12px;
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 8px;
            color: var(--text-main);
            font-size: 0.84rem;
            outline: none;
            transition: border-color 0.15s;
        }}
        .search-box input:focus {{
            border-color: var(--accent);
        }}

        .toc-list {{
            flex: 1;
            overflow-y: auto;
            padding: 10px;
        }}
        .toc-link {{
            display: flex;
            align-items: center;
            padding: 8px 12px;
            color: var(--text-secondary);
            text-decoration: none;
            font-size: 0.85rem;
            border-radius: 6px;
            margin-bottom: 3px;
            transition: all 0.15s;
        }}
        .toc-link:hover, .toc-link.active {{
            background: var(--accent-subtle);
            color: var(--accent);
            font-weight: 600;
        }}
        .toc-num {{
            font-size: 0.75rem;
            font-family: Consolas, monospace;
            opacity: 0.6;
            margin-right: 8px;
            min-width: 22px;
        }}
        .toc-text {{
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        /* 主阅读内容区域 */
        #main {{
            flex: 1;
            overflow-y: auto;
            padding: 40px 48px;
            scroll-behavior: smooth;
        }}
        .main-container {{
            max-width: 860px;
            margin: 0 auto;
        }}

        .cover-card {{
            background: var(--cover-gradient);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 36px 40px;
            margin-bottom: 36px;
            box-shadow: var(--shadow-card);
        }}
        .cover-card h1 {{
            font-size: 1.85rem;
            font-weight: 800;
            margin-bottom: 12px;
            color: var(--text-main);
        }}

        .article-card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 36px 40px;
            margin-bottom: 36px;
            box-shadow: var(--shadow-card);
            transition: background-color 0.2s, border-color 0.2s;
        }}
        .article-meta-badge {{
            display: inline-block;
            font-size: 0.76rem;
            font-weight: 700;
            padding: 3px 10px;
            background: var(--accent-subtle);
            color: var(--accent);
            border-radius: 6px;
            margin-bottom: 12px;
        }}
        .article-title {{
            font-size: 1.55rem;
            font-weight: 800;
            line-height: 1.35;
            margin-bottom: 12px;
            color: var(--text-main);
        }}
        .article-meta {{
            display: flex;
            gap: 16px;
            font-size: 0.84rem;
            color: var(--text-muted);
            padding-bottom: 16px;
            border-bottom: 1px solid var(--card-border);
            margin-bottom: 24px;
            flex-wrap: wrap;
        }}
        .article-meta a {{
            color: var(--accent);
            text-decoration: none;
        }}
        .article-meta a:hover {{
            text-decoration: underline;
        }}

        /* Markdown 正文排版 */
        .markdown-body {{
            font-size: 1.02rem;
            line-height: 1.8;
            color: var(--text-secondary);
        }}
        .markdown-body p {{ margin-bottom: 16px; }}
        .markdown-body h1, .markdown-body h2, .markdown-body h3 {{
            color: var(--text-main);
            font-weight: 700;
            margin-top: 28px;
            margin-bottom: 14px;
        }}
        .markdown-body h2 {{
            font-size: 1.35rem;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 6px;
        }}
        .markdown-body h3 {{ font-size: 1.15rem; }}
        .markdown-body code {{
            background: var(--code-bg);
            border: 1px solid var(--code-border);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: Consolas, monospace;
            font-size: 0.9em;
            color: #e11d48;
        }}
        .markdown-body pre {{
            background: var(--code-bg);
            border: 1px solid var(--code-border);
            padding: 16px 20px;
            border-radius: 8px;
            overflow-x: auto;
            margin-bottom: 18px;
        }}
        .markdown-body pre code {{
            background: none;
            border: none;
            padding: 0;
            color: var(--text-main);
        }}
        .markdown-body img {{
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            margin: 16px auto;
            display: block;
            box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        }}
        .markdown-body blockquote {{
            border-left: 4px solid var(--accent);
            padding: 8px 16px;
            background: var(--accent-subtle);
            border-radius: 0 8px 8px 0;
            color: var(--text-secondary);
            margin-bottom: 16px;
        }}
        .article-footer-watermark {{
            margin-top: 24px;
            padding-top: 12px;
            border-top: 1px dashed var(--card-border);
            font-size: 0.85rem;
            color: var(--text-muted);
            text-align: center;
            font-style: italic;
        }}
        .disclaimer-badge {{
            margin-top: 12px;
            padding: 10px 14px;
            background: var(--accent-subtle);
            border-left: 3px solid var(--accent);
            border-radius: 4px;
            font-size: 0.82rem;
            color: var(--text-secondary);
            line-height: 1.5;
            text-align: left;
        }}
        .sidebar-brand-tag {{
            font-size: 0.78rem;
            color: var(--accent);
            background: var(--accent-subtle);
            padding: 2px 6px;
            border-radius: 4px;
            display: inline-block;
            margin-top: 4px;
            font-weight: 500;
        }}

        @media (max-width: 768px) {{
            body {{ flex-direction: column; }}
            #sidebar {{ width: 100%; height: 240px; }}
            #main {{ padding: 20px; }}
        }}
    </style>
</head>
<body>
    <div id="sidebar">
        <div class="sidebar-header">
            <div>
                <div class="author-title">{html.escape(self.author_name)}</div>
                <div class="author-sub">{cover_sub}</div>
                <div class="sidebar-brand-tag">📖 公众号：{html.escape(BRAND_OFFICIAL_ACCOUNT)}</div>
            </div>
            <button class="theme-toggle-btn" onclick="toggleTheme()">
                <span id="themeIcon">🌙 暗黑</span>
            </button>
        </div>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="搜索目录..." oninput="filterToc()">
        </div>
        <div class="toc-list" id="tocList">
            {''.join(toc_items)}
        </div>
    </div>
    <div id="main">
        <div class="main-container">
            <div class="cover-card">
                <h1>{cover_title}</h1>
                <p style="color: var(--text-muted); margin-bottom: 6px; font-size: 0.9rem;">
                    排版整理：微信公众号【{html.escape(BRAND_OFFICIAL_ACCOUNT)}】 · 导出时间：{now_str} · 总篇数：{len(articles)} 篇
                </p>
                <div class="disclaimer-badge">
                    {html.escape(BRAND_DISCLAIMER)}
                </div>
            </div>
            {''.join(article_sections)}
        </div>
    </div>
    <script>
        function initTheme() {{
            const saved = localStorage.getItem('bd_doc_theme') || 'light';
            document.documentElement.setAttribute('data-theme', saved);
            updateThemeBtn(saved);
        }}

        function toggleTheme() {{
            const current = document.documentElement.getAttribute('data-theme') || 'light';
            const next = current === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('bd_doc_theme', next);
            updateThemeBtn(next);
        }}

        function updateThemeBtn(theme) {{
            const btnText = document.getElementById('themeIcon');
            if (btnText) {{
                btnText.innerText = theme === 'dark' ? '☀️ 浅色' : '🌙 暗黑';
            }}
        }}

        function filterToc() {{
            const val = document.getElementById('searchInput').value.toLowerCase();
            const links = document.querySelectorAll('.toc-link');
            links.forEach(link => {{
                const text = link.querySelector('.toc-text').innerText.toLowerCase();
                link.style.display = text.includes(val) ? 'flex' : 'none';
            }});
        }}

        function highlightToc(el) {{
            document.querySelectorAll('.toc-link').forEach(l => l.classList.remove('active'));
            el.classList.add('active');
        }}

        initTheme();
    </script>
</body>
</html>
"""
        output_file.write_text(html_template, encoding="utf-8")
        return output_file
