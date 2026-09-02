from abc import ABC, abstractmethod
from typing import List, Callable, Optional, Dict, Any
import httpx
from app.config import DEFAULT_HEADERS, DEFAULT_TIMEOUT
from app.models import ArticleItem

class BaseScraper(ABC):
    def __init__(
        self,
        target: str,
        enable_noise_filter: bool = True,
        max_articles: Optional[int] = None,
        remove_image_watermark: bool = True
    ):
        self.target = target.strip()
        self.enable_noise_filter = enable_noise_filter
        self.max_articles = max_articles
        self.remove_image_watermark = remove_image_watermark
        self.client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            verify=False
        )

    async def close(self):
        await self.client.aclose()

    @abstractmethod
    async def get_author_info(self) -> Dict[str, Any]:
        """获取博主基本信息，返回 {'name': str, 'avatar': str, 'bio': str}"""
        pass

    @abstractmethod
    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        """获取博主的所有文章元数据列表，返回 [{'url': str, 'title': str, 'id': str, ...}]"""
        pass

    @abstractmethod
    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        """根据文章元数据抓取并解析单篇文章详情"""
        pass
