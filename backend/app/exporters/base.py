from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any
from app.models import ArticleItem

class BaseExporter(ABC):
    def __init__(self, author_name: str, platform: str, output_dir: Path):
        self.author_name = author_name or "未知博主"
        self.platform = platform or "多平台"
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def get_platform_display_name(platform_str: str) -> str:
        """将内部平台标识转换为标准的中文友好展示名"""
        mapping = {
            "wechat": "微信公众号",
            "sina_blog": "新浪博客",
            "jianshu": "简书",
            "csdn": "CSDN 博客",
            "zhihu": "知乎",
            "weibo": "新浪微博",
            "juejin": "稀土掘金",
            "cnblogs": "博客园",
            "51cto": "51CTO",
            "custom_urls": "网页合集"
        }
        if not platform_str:
            return "网页文章"
        p = str(platform_str).strip()
        return mapping.get(p.lower(), p)

    def get_effective_platform_name(self, articles: List[ArticleItem]) -> str:
        """从文章列表或自身 platform 中智能推导统一友好的展示平台名 (避免泄露内部代号)"""
        if articles:
            from collections import Counter
            art_platforms = [a.platform for a in articles if a.platform]
            if art_platforms:
                most_common = Counter(art_platforms).most_common(1)[0][0]
                if most_common and most_common not in ["custom_urls", "多平台", "自定义", "未知"]:
                    return self.get_platform_display_name(most_common)
        return self.get_platform_display_name(self.platform)

    @abstractmethod
    async def export(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        """
        将所有抓取到的文章合并导出为单一文件。
        返回导出文件的 Path 绝对路径。
        """
        pass

