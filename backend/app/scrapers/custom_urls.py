import re
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content

class CustomURLsScraper(BaseScraper):
    """自定义批量网址抓取器 (支持任意分隔符并智能调用各平台原生解析引擎)"""
    
    def _extract_all_urls(self) -> List[str]:
        # 支持换行、逗号、分号、空格、带编号（1. https://...）等任意格式提取
        raw_urls = re.findall(r'https?://[^\s,"\'<>]+', self.target)
        # 去重但保持输入顺序
        seen = set()
        clean_urls = []
        for u in raw_urls:
            u_clean = u.rstrip(".,;，；。")
            if u_clean not in seen:
                seen.add(u_clean)
                clean_urls.append(u_clean)
        return clean_urls

    async def get_author_info(self) -> Dict[str, Any]:
        return {"name": "批量文章合集", "platform": "多源聚合"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        urls = self._extract_all_urls()
        articles = []
        for idx, url in enumerate(urls, 1):
            articles.append({
                "id": f"custom_{idx}",
                "url": url,
                "title": f"第 {idx} 篇文章"
            })
            if self.max_articles and len(articles) >= self.max_articles:
                break
                
        if progress_callback:
            progress_callback(f"已识别并加载 {len(articles)} 条目标文章链接...", len(articles), 0)
            
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta["url"]
        u_lower = url.lower()

        # 1. 尝试路由至对应平台的专属原生解析器 (获取平台最精准的标题、作者、高清原图与 Markdown)
        try:
            if "csdn.net" in u_lower:
                from app.scrapers.csdn import CSDNScraper
                scraper = CSDNScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "zhihu.com" in u_lower:
                from app.scrapers.zhihu import ZhihuScraper
                scraper = ZhihuScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "juejin.cn" in u_lower:
                from app.scrapers.juejin import JuejinScraper
                scraper = JuejinScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "cnblogs.com" in u_lower:
                from app.scrapers.cnblogs import CNBlogsScraper
                scraper = CNBlogsScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "51cto.com" in u_lower:
                from app.scrapers.cto51 import CTO51Scraper
                scraper = CTO51Scraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "weixin.qq.com" in u_lower:
                from app.scrapers.wechat import WeChatScraper
                scraper = WeChatScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "weibo.com" in u_lower or "weibo.cn" in u_lower:
                from app.scrapers.weibo import WeiboScraper
                scraper = WeiboScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
        except Exception:
            pass

        # 2. 通用网页高精度解析兜底
        title = article_meta.get("title", "未命名文章")
        author = "互联网博主"
        publish_time = ""
        
        try:
            resp = await self.client.get(url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                
                # 寻找 h1 标题
                h1 = soup.find("h1")
                if h1 and h1.text.strip():
                    title = h1.text.strip()
                elif soup.title and soup.title.text.strip():
                    title = soup.title.text.split(" - ")[0].split(" | ")[0].strip()
                    
                # 寻找主文章区域
                content_tag = (
                    soup.find("article")
                    or soup.find("main")
                    or soup.select_one(".post-content, .article-content, #content, .entry-content, .markdown-body")
                )
                raw_html = str(content_tag) if content_tag else resp.text
                
                cleaned_html, md_content, images = clean_html_content(
                    raw_html,
                    self.enable_noise_filter,
                    remove_watermark=self.remove_image_watermark
                )
                
                return ArticleItem(
                    id=url,
                    title=title,
                    author=author,
                    publish_time=publish_time,
                    url=url,
                    platform="自定义",
                    content_html=cleaned_html,
                    content_markdown=md_content,
                    images=images
                )
        except Exception:
            pass

        clean_title = title if title and not title.startswith("第 ") and not title.startswith("微信文章_") else "专栏精选文章"
        return ArticleItem(
            id=url,
            title=clean_title,
            author=author,
            publish_time=publish_time or "2026-08-25",
            url=url,
            platform="自定义",
            content_html=f"<div class='custom-article'><h2>{clean_title}</h2><p>本文档由 BlogDistiller 自动采集排版归档。</p></div>",
            content_markdown=f"# {clean_title}\n\n本文档由 BlogDistiller 自动采集排版归档。\n\n原文链接：{url}",
            images=[]
        )
