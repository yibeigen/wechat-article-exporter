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

    @abstractmethod
    async def export(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        """
        将所有抓取到的文章合并导出为单一文件。
        返回导出文件的 Path 绝对路径。
        """
        pass
