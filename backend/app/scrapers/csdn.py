import re
import asyncio
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content

class CSDNScraper(BaseScraper):
    """CSDN 博客文章抓取器 (具备 WAF 防护突破与 API/HTML 双通道容灾)"""
    
    def _extract_username(self) -> str:
        # 支持多种 CSDN URL 格式:
        # https://blog.csdn.net/qq_46987323?type=blog
        # https://blog.csdn.net/qq_46987323
        # qq_46987323
        target = self.target.split("?")[0].split("#")[0].strip("/ ")
        match = re.search(r"blog\.csdn\.net/([a-zA-Z0-9\-_]+)", target)
        if match:
            return match.group(1)
        match = re.search(r"csdn\.net/([a-zA-Z0-9\-_]+)", target)
        if match:
            return match.group(1)
        if "/" in target:
            parts = [p for p in target.split("/") if p]
            return parts[-1]
        return target

    def _get_headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        username = self._extract_username()
        ref = referer or f"https://blog.csdn.net/{username}?type=blog"
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": ref,
            "Origin": "https://blog.csdn.net"
        }

    async def get_author_info(self) -> Dict[str, Any]:
        username = self._extract_username()
        author_name = username
        try:
            url = f"https://blog.csdn.net/{username}?type=blog"
            resp = await self.client.get(url, headers=self._get_headers(url))
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                name_tag = soup.select_one(".user-profile-name, .user-name, #uid, .profile-intro-name, .user-profile-head-info-name")
                if name_tag and name_tag.text.strip():
                    author_name = name_tag.text.strip()
        except Exception:
            pass
        return {"name": author_name, "username": username, "platform": "CSDN"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        # 如果输入的是单篇文章链接，直接返回单篇，不再全量遍历博主历史
        if "/article/details/" in self.target:
            return [{
                "id": self.target,
                "url": self.target,
                "title": "CSDN单篇文章"
            }]

        username = self._extract_username()
        articles = []
        page = 1
        seen_urls = set()
        total_expected = 0
        
        # 通道 1: 官方 API 接口
        try:
            while True:
                api_url = f"https://blog.csdn.net/community/home-api/v1/get-business-list?page={page}&size=20&businessType=blog&username={username}"
                
                # 带 3 次重试与连接断开自动恢复
                resp = None
                for attempt in range(3):
                    try:
                        resp = await self.client.get(api_url, headers=self._get_headers(), timeout=10.0)
                        if resp.status_code == 200:
                            break
                    except Exception:
                        await asyncio.sleep(0.4)
                
                if not resp or resp.status_code != 200:
                    break
                    
                data = resp.json()
                if data.get("code") != 200 or not data.get("data"):
                    break
                    
                d = data.get("data", {})
                items = d.get("list", [])
                total_expected = d.get("total", 0) or total_expected
                if not items:
                    break
                    
                for item in items:
                    url = item.get("url") or item.get("articleDetailUrl")
                    title = (item.get("title") or "").strip()
                    article_id = str(item.get("articleId", ""))
                    publish_time = item.get("postTime", "") or item.get("createTime", "")
                    
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    
                    articles.append({
                        "id": article_id or url,
                        "url": url,
                        "title": title,
                        "publish_time": publish_time,
                        "description": item.get("description", "")
                    })
                    
                    if self.max_articles and len(articles) >= self.max_articles:
                        return articles
                        
                if progress_callback:
                    progress_callback(f"已获取 CSDN 文章 {len(articles)}/{total_expected or len(articles)} 篇 (第 {page} 页)...", len(articles), total_expected or 0)
                    
                if total_expected > 0 and len(articles) >= total_expected:
                    break
                if len(items) == 0:
                    break
                    
                page += 1
                if page > 100:
                    break
                await asyncio.sleep(0.15)
        except Exception:
            pass
            
        # 通道 2: HTML 页面解析兜底 (如果 API 仅获取到部分或被阻断)
        if not articles or (total_expected > 0 and len(articles) < total_expected):
            try:
                for page in range(1, 100):
                    url = f"https://blog.csdn.net/{username}/article/list/{page}"
                    resp = await self.client.get(url, headers=self._get_headers(url), timeout=10.0)
                    if resp.status_code != 200:
                        break
                        
                    soup = BeautifulSoup(resp.text, "lxml")
                    card_items = soup.select(".blog-list-box, .article-item-box, .mainContent article, .article-list-item")
                    if not card_items:
                        break
                        
                    for card in card_items:
                        link = card.select_one("a[href*='/article/details/']")
                        if not link:
                            continue
                        url = link.get("href", "").split("?")[0]
                        title = link.text.strip()
                        if not title:
                            title_el = card.select_one("h4, .blog-list-box-top, .title")
                            title = title_el.text.strip() if title_el else "无标题"
                            
                        time_el = card.select_one(".date, .time, .view-time")
                        publish_time = time_el.text.strip() if time_el else ""
                        
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            articles.append({
                                "id": url,
                                "url": url,
                                "title": title,
                                "publish_time": publish_time,
                                "description": ""
                            })
                            
                        if self.max_articles and len(articles) >= self.max_articles:
                            return articles
            except Exception:
                pass
                
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta["url"]
        title = article_meta.get("title", "无标题")
        publish_time = article_meta.get("publish_time", "")
        author = self._extract_username()
        error_msg = None
        
        try:
            headers = self._get_headers(referer=url)
            resp = await self.client.get(url, headers=headers, timeout=15.0)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                
                title_tag = soup.select_one("#articleContentId, .title-article, h1")
                if title_tag:
                    title = title_tag.text.strip()
                    
                time_tag = soup.select_one(".time, .article-time, .time-box, .article-info-box .time, .blog-post-time")
                if time_tag:
                    time_match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", time_tag.text)
                    if time_match:
                        publish_time = time_match.group(0)
                        
                content_tag = soup.select_one("#content_views, .article_content, #article_content")
                if content_tag and content_tag.text.strip():
                    raw_html = str(content_tag)
                    cleaned_html, md_content, images = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)
                    
                    return ArticleItem(
                        id=url,
                        title=title,
                        author=author,
                        publish_time=publish_time,
                        url=url,
                        platform="CSDN",
                        summary=article_meta.get("description", ""),
                        content_html=cleaned_html,
                        content_markdown=md_content,
                        images=images,
                        is_failed=False,
                        error_reason=None
                    )
                else:
                    error_msg = "页面已加载但未找到有效正文 (可能为博主付费专栏、需登录可见或已被作者隐藏)"
            elif resp.status_code == 404:
                error_msg = "HTTP 404 文章已被作者删除或链接无效"
            elif resp.status_code == 403:
                error_msg = "HTTP 403 访问被 CSDN 防护拦截"
            else:
                error_msg = f"CSDN 返回 HTTP {resp.status_code}"
        except Exception as e:
            error_msg = f"网络请求异常: {str(e)}"
            
        return ArticleItem(
            id=url,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="CSDN",
            content_html=f"<p>【抓取失败】：{error_msg or '未知异常'}</p>",
            content_markdown=f"【抓取失败】：{error_msg or '未知异常'}",
            images=[],
            is_failed=True,
            error_reason=error_msg or "无法获取正文"
        )
