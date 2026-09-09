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

        # 4. 弹性降级策略 (若其他平台原版模板尚未独立编写，平滑采用高质量离线排版生成原版)
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

    async def _export_fallback_classic(self, articles: List[ArticleItem], filename_prefix: str, platform_name: str) -> Path:
        """针对尚未单独上线原版模板的平台，平滑降级至高保真离线版本"""
        from app.exporters.html_exporter import HTMLExporter
        fallback_exporter = HTMLExporter(self.author_name, self.platform, self.output_dir)
        return await fallback_exporter.export(articles, f"{filename_prefix}_原版")
