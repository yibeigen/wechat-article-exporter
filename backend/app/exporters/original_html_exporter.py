# -*- coding: utf-8 -*-
"""
平台网站原版 HTML 离线导出器 (Original / Classic Platform HTML Exporter)

核心特性：
1. 平台策略化分发：针对不同平台定制 1:1 还原各平台电脑端官网真实排版、组件与视觉要素。
2. 率先完整接入【新浪博客】：
   - 1:1 复刻 Theme 30_1 蓝白 PC 经典双栏布局；
   - 标志性折角小图标、宋体粗标题、博主卡片组件；
   - 左侧栏内置毫秒级离线即时搜索（拼音/汉字高亮模糊搜索、无刷新换篇、键盘方向键左右翻页）；
   - 100% 独立单文件 All-in-One 离线保存（背景素材与正文高清插图全量 Base64 嵌入）。
3. 弹性降级机制：针对尚未单独上线原版模板的平台，平滑降级至高保真离线版本，保障任务永不中断。
"""

import os
import re
import json
import base64
from pathlib import Path
from typing import List, Dict, Any, Optional

from jinja2 import Template

from app.exporters.base import BaseExporter
from app.models import ArticleItem
from app.core.image_helper import embed_articles_images_as_base64


CURRENT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = CURRENT_DIR.parent.parent
ROOT_DIR = BACKEND_DIR.parent


class OriginalHTMLExporter(BaseExporter):
    """平台网站原版 HTML 离线导出器"""

    async def export(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        # 1. 确保所有文章中的图片均已转换为 Base64 内嵌，保证 100% 单文件永久离线
        await embed_articles_images_as_base64(articles)

        # 2. 识别当前平台类型
        platform_name = self.get_effective_platform_name(articles)

        # 3. 平台策略路由分发
        if platform_name == "新浪博客" or "sina" in str(self.platform).lower() or any(
            (a.platform and "sina" in a.platform.lower()) or ("blog.sina.com.cn" in a.url) for a in articles
        ):
            return await self._export_sina_classic(articles, filename_prefix)

        # 新增：简书 PC 原版 1:1 排版路由
        if platform_name == "简书" or "jianshu" in str(self.platform).lower() or any(
            (a.platform and "jianshu" in a.platform.lower()) or ("jianshu.com" in a.url) for a in articles
        ):
            return await self._export_jianshu_classic(articles, filename_prefix)

        # 新增：知乎 PC 原版 1:1 排版路由（复刻专栏文章 + 问题回答双版式）
        if platform_name == "知乎" or "zhihu" in str(self.platform).lower() or any(
            (a.platform and "zhihu" in a.platform.lower()) or ("zhihu.com" in a.url) for a in articles
        ):
            return await self._export_zhihu_classic(articles, filename_prefix)

        # 4. 弹性降级策略 (若其他平台原版模板尚未独立编写，平滑采用高质量离线版本生成原版)
        return await self._export_fallback_classic(articles, filename_prefix, platform_name)

    async def _export_sina_classic(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        """渲染新浪博客 PC 电脑端 1:1 原版经典排版"""
        output_file = self.output_dir / f"{filename_prefix}_原版.html"

        # 提取分类或专栏名称
        category_name = None
        for a in articles:
            if a.category and a.category.strip() and a.category.strip() not in ["新浪博文", "全部博文", "未分类"]:
                category_name = a.category.strip()
                break

        title = f"{self.author_name} 的新浪博客"

        # 构建给前端模版使用的结构化文章列表
        articles_data = []
        for idx, art in enumerate(articles, 1):
            cat = art.category
            if not cat or cat in ["新浪博文", "全部博文", "未分类"]:
                if art.tags and len(art.tags) > 0 and art.tags[0] != "新浪博客":
                    cat = art.tags[0]
                else:
                    cat = "其它"

            articles_data.append({
                "id": art.id or str(idx),
                "title": art.title,
                "author": art.author or self.author_name,
                "publish_time": art.publish_time,
                "url": art.url,
                "category": cat,
                "tags": art.tags or ["新浪博客"],
                "content_html": art.content_html
            })

        # 加载 Base64 经典素材 (优先从 backend/app/exporters/templates/sina_classic/assets 读取)
        asset_dirs = [
            CURRENT_DIR / "templates" / "sina_classic" / "assets",
            ROOT_DIR / "tools" / "sina_classic_exporter" / "assets"
        ]

        def load_asset_b64(fname: str, mime: str = "image/png") -> str:
            for adir in asset_dirs:
                fpath = adir / fname
                if fpath.exists():
                    try:
                        return f"data:{mime};base64," + base64.b64encode(fpath.read_bytes()).decode("ascii")
                    except Exception:
                        pass
            return ""

        banner_b64 = load_asset_b64("sinablogb.jpg", "image/jpeg")
        navbg_b64 = load_asset_b64("blognavbg.png", "image/png")
        modelhead_b64 = load_asset_b64("modelhead.png", "image/png")
        modelbody_b64 = load_asset_b64("modelbody.png", "image/png")
        modelfoot_b64 = load_asset_b64("modelfoot.png", "image/png")
        dot_b64 = load_asset_b64("SG_dot.gif", "image/gif")
        linedot_b64 = load_asset_b64("SG_linedot.gif", "image/gif")
        newsp_b64 = load_asset_b64("sg_newsp.png", "image/png")

        # 默认博主头像 (SVG Base64)
        default_avatar_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 24 24" fill="#a0aec0"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>'
        author_avatar = "data:image/svg+xml;base64," + base64.b64encode(default_avatar_svg.encode("utf-8")).decode("ascii")

        # 查找模版文件
        tmpl_paths = [
            CURRENT_DIR / "templates" / "sina_classic" / "sina_classic.html",
            ROOT_DIR / "tools" / "sina_classic_exporter" / "templates" / "sina_classic.html"
        ]
        template_str = ""
        for tp in tmpl_paths:
            if tp.exists():
                template_str = tp.read_text(encoding="utf-8")
                break

        if not template_str:
            raise FileNotFoundError("未找到新浪博客经典排版模板 sina_classic.html")

        template = Template(template_str)
        rendered_html = template.render(
            title=title,
            author_name=self.author_name,
            author_avatar=author_avatar,
            category_name=category_name,
            total_articles=len(articles_data),
            banner_b64=banner_b64,
            navbg_b64=navbg_b64,
            modelhead_b64=modelhead_b64,
            modelbody_b64=modelbody_b64,
            modelfoot_b64=modelfoot_b64,
            dot_b64=dot_b64,
            linedot_b64=linedot_b64,
            newsp_b64=newsp_b64,
            articles_json=json.dumps(articles_data, ensure_ascii=False)
        )

        output_file.write_text(rendered_html, encoding="utf-8")
        return output_file

    async def _export_jianshu_classic(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        """渲染简书 PC 电脑端 1:1 原版经典排版"""
        output_file = self.output_dir / f"{filename_prefix}_原版.html"

        # 提取分类/文集名称（优先从文章 category 字段取）
        category_name = None
        for a in articles:
            if a.category and a.category.strip() and a.category.strip() not in ["简书博文", "未分类", "全部博文"]:
                category_name = a.category.strip()
                break

        # 标题：如果是专题/文集就显示专题名，否则显示“作者 的简书主页”
        title = category_name or f"{self.author_name} 的简书主页"

        # 默认作者头像（SVG Base64）
        default_avatar_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 24 24" fill="#a0aec0"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>'
        default_avatar_b64 = "data:image/svg+xml;base64," + base64.b64encode(default_avatar_svg.encode("utf-8")).decode("ascii")

        # 异步下载图片并转为 Base64，保证离线单文件也能显示头像
        async def url_to_base64(url: str) -> str:
            if not url or url.startswith("data:"):
                return url or default_avatar_b64
            if url.startswith("//"):
                url = "https:" + url
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    resp = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://www.jianshu.com/"
                    })
                    if resp.status_code == 200:
                        mime = resp.headers.get("content-type", "image/png").split(";")[0]
                        if mime == "application/octet-stream":
                            mime = "image/png"
                        return f"data:{mime};base64," + base64.b64encode(resp.content).decode("ascii")
            except Exception:
                pass
            return default_avatar_b64

        # 从文章中提取真实作者头像（JianshuScraper 已在 scrape_article_detail 中写入）
        author_avatar_url = ""
        for a in articles:
            if a.author_avatar and a.author_avatar.strip():
                author_avatar_url = a.author_avatar.strip()
                break
        author_avatar = await url_to_base64(author_avatar_url)

        # 作者简介：目前抓取器未统一返回 bio，先留空
        author_bio = ""

        # 清理正文 HTML：移除可能导致文字与图片重叠的绝对定位、浮动、负边距等样式
        def sanitize_jianshu_html(html: str) -> str:
            """对简书正文再做一次安全清洗，防止原站残留样式导致离线排版错乱"""
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            for tag in soup.find_all(True):
                # 1. 清理危险内联样式：绝对定位、固定定位、浮动、负边距、z-index、transform
                if tag.get("style"):
                    style = tag["style"]
                    # 直接移除定位、浮动、层叠相关样式
                    style = re.sub(r'(position\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(float\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(z-index\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(transform\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(margin-top\s*:\s*-[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(margin-left\s*:\s*-[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(top\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(left\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(right\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(bottom\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = style.strip().rstrip(';').strip()
                    if style:
                        tag["style"] = style
                    else:
                        del tag["style"]
                # 2. 移除危险 class
                dangerous_classes = ["image-container-fill", "image-container", "image-view", "image-package"]
                cls = tag.get("class", [])
                if isinstance(cls, str):
                    cls = cls.split()
                new_cls = [c for c in cls if c not in dangerous_classes]
                if new_cls:
                    tag["class"] = new_cls
                else:
                    if "class" in tag.attrs:
                        del tag["class"]
            # 3. 确保图片独立成块，不被文字环绕
            for img in soup.find_all("img"):
                img["style"] = "display:block;max-width:100%;margin:20px auto;border-radius:4px;"
            result = str(soup)
            # 去除 lxml 自动生成的 html/body 外壳，只保留内部片段
            if soup.body:
                result = soup.body.decode_contents()
            return result

        # 构建给前端模板使用的结构化文章列表
        articles_data = []
        for idx, art in enumerate(articles, 1):
            cleaned_html = sanitize_jianshu_html(art.content_html or "")
            art_avatar = await url_to_base64(art.author_avatar or author_avatar_url)
            articles_data.append({
                "id": art.id or str(idx),
                "title": art.title,
                "author": art.author or self.author_name,
                "author_avatar": art_avatar,
                "publish_time": art.publish_time,
                "url": art.url,
                "content_html": cleaned_html,
                "like_count": art.like_count or 0,
                "read_num": art.read_num or 0,
                "comment_count": art.comment_count or 0
            })

        # 加载简书模板
        tmpl_path = CURRENT_DIR / "templates" / "jianshu_classic" / "jianshu_classic.html"
        if not tmpl_path.exists():
            # 模板不存在时降级到普通 HTML，避免任务失败
            return await self._export_fallback_classic(articles, filename_prefix, "简书")

        template_str = tmpl_path.read_text(encoding="utf-8")
        template = Template(template_str)
        rendered_html = template.render(
            title=title,
            author_name=self.author_name,
            author_avatar=author_avatar,
            author_bio=author_bio,
            category_name=category_name,
            total_articles=len(articles_data),
            articles_json=json.dumps(articles_data, ensure_ascii=False)
        )

        output_file.write_text(rendered_html, encoding="utf-8")
        return output_file

    async def _export_zhihu_classic(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        """渲染知乎 PC 电脑端 1:1 原版经典排版（同时支持专栏文章与问题回答双版式）"""
        output_file = self.output_dir / f"{filename_prefix}_原版.html"

        # 默认作者头像（SVG Base64）
        default_avatar_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 24 24" fill="#a0aec0"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>'
        default_avatar_b64 = "data:image/svg+xml;base64," + base64.b64encode(default_avatar_svg.encode("utf-8")).decode("ascii")

        # 异步下载图片并转为 Base64，保证离线单文件也能显示头像
        async def url_to_base64(url: str) -> str:
            if not url or url.startswith("data:"):
                return url or default_avatar_b64
            if url.startswith("//"):
                url = "https:" + url
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    resp = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://www.zhihu.com/"
                    })
                    if resp.status_code == 200:
                        mime = resp.headers.get("content-type", "image/png").split(";")[0]
                        if mime == "application/octet-stream":
                            mime = "image/png"
                        return f"data:{mime};base64," + base64.b64encode(resp.content).decode("ascii")
            except Exception:
                pass
            return default_avatar_b64

        # 从文章中提取真实作者头像
        author_avatar_url = ""
        for a in articles:
            if a.author_avatar and a.author_avatar.strip():
                author_avatar_url = a.author_avatar.strip()
                break
        author_avatar = await url_to_base64(author_avatar_url)

        # 知乎内容统计：「文章」为全量集合（含被收录进专栏的），专栏只是归类标签
        article_count = sum(1 for a in articles if a.get_content_type() in ("article", "column_article"))
        answer_count = sum(1 for a in articles if a.get_content_type() == "answer")
        # 专栏按「个数」统计：从文章的 column_title 字段（多专栏用 、 分隔）去重
        column_count = len({c.strip() for a in articles for c in (a.column_title or "").split("、") if c.strip()})

        # 清理正文 HTML：知乎原站可能残留一些导致离线排版错乱的样式，这里做安全清洗
        def sanitize_zhihu_html(html: str, is_answer: bool = False) -> str:
            """对知乎正文做安全清洗，移除危险样式，保证离线单文件展示整洁"""
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            for tag in soup.find_all(True):
                # 移除定位、浮动等危险内联样式
                if tag.get("style"):
                    style = tag["style"]
                    style = re.sub(r'(position\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(float\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(z-index\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(transform\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(margin-[^;]+\s*:\s*-[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(top\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(left\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(right\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = re.sub(r'(bottom\s*:\s*[^;]+;?)', '', style, flags=re.I)
                    style = style.strip().rstrip(';').strip()
                    if style:
                        tag["style"] = style
                    else:
                        del tag["style"]
                # 针对回答页去掉可能干扰排版的 class
                if is_answer:
                    dangerous_classes = ["image-container-fill", "image-container", "image-view", "RichContent-collapsed"]
                    cls = tag.get("class", [])
                    if isinstance(cls, str):
                        cls = cls.split()
                    new_cls = [c for c in cls if c not in dangerous_classes]
                    if new_cls:
                        tag["class"] = new_cls
                    else:
                        if "class" in tag.attrs:
                            del tag["class"]
            # 图片统一显示为块级，居中，自适应
            for img in soup.find_all("img"):
                img["style"] = "display:block;max-width:100%;margin:20px auto;border-radius:4px;"
            result = str(soup)
            if soup.body:
                result = soup.body.decode_contents()
            return result

        # 构建给前端模板使用的结构化文章列表
        articles_data = []
        for idx, art in enumerate(articles, 1):
            ctype = art.get_content_type()
            if ctype == "column_article":
                # 旧版缓存把专栏文章当作独立类型；新模型里「文章」为全量，
                # 专栏只是归类标签（看 column_title 字段），这里做兼容归一
                ctype = "article"
            cleaned_html = sanitize_zhihu_html(art.content_html or "", is_answer=(ctype == "answer"))
            art_avatar = await url_to_base64(art.author_avatar or author_avatar_url)
            # 去掉标题中的【文章】【专栏文章】【回答】前缀，便于模板按类型干净展示
            display_title = art.title
            if display_title.startswith("【专栏文章】"):
                display_title = display_title[len("【专栏文章】"):]
            elif display_title.startswith("【文章】"):
                display_title = display_title[len("【文章】"):]
            elif display_title.startswith("【专栏】"):
                # 兼容历史缓存/旧数据仍使用【专栏】前缀的情况
                display_title = display_title[len("【专栏】"):]
            elif display_title.startswith("【回答】"):
                display_title = display_title[len("【回答】"):]
            articles_data.append({
                "id": art.id or str(idx),
                "title": display_title,
                "raw_title": art.title,
                "author": art.author or self.author_name,
                "author_avatar": art_avatar,
                "publish_time": art.publish_time,
                "url": art.url,
                "content_html": cleaned_html,
                "content_type": ctype,
                "column_title": art.column_title or "",
                "like_count": art.like_count or 0,
                "comment_count": art.comment_count or 0,
                "summary": art.summary or ""
            })

        # 加载知乎 1:1 模板
        tmpl_path = CURRENT_DIR / "templates" / "zhihu_classic" / "zhihu_classic.html"
        if not tmpl_path.exists():
            # 模板不存在时降级到普通 HTML，避免任务失败
            return await self._export_fallback_classic(articles, filename_prefix, "知乎")

        template_str = tmpl_path.read_text(encoding="utf-8")
        template = Template(template_str)
        rendered_html = template.render(
            title=f"{self.author_name} 的知乎",
            author_name=self.author_name,
            author_avatar=author_avatar,
            total_articles=len(articles_data),
            article_count=article_count,
            column_count=column_count,
            answer_count=answer_count,
            articles_json=json.dumps(articles_data, ensure_ascii=False)
        )

        output_file.write_text(rendered_html, encoding="utf-8")
        return output_file

    async def _export_fallback_classic(self, articles: List[ArticleItem], filename_prefix: str, platform_name: str) -> Path:
        """针对尚未单独上线原版模板的平台，平滑降级至高保真离线版本"""
        from app.exporters.html_exporter import HTMLExporter
        fallback_exporter = HTMLExporter(self.author_name, self.platform, self.output_dir)
        return await fallback_exporter.export(articles, f"{filename_prefix}_原版")
