import re
import datetime
import asyncio
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content
from app.cleaners.noise_filter import filter_noise_text

def _scrape_juejin_playwright_sync(url: str) -> Dict[str, str]:
    """通过同步 Playwright 无头浏览器渲染掘金文章（线程隔离终极兜底）"""
    res = {"title": "", "author": "", "publish_time": "", "html": ""}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)

            title = page.evaluate("() => document.querySelector('.article-title, h1.title, h1')?.innerText || ''")
            author = page.evaluate("() => document.querySelector('.author-name, .username, .name')?.innerText || ''")
            time_str = page.evaluate("() => document.querySelector('.time, time, .meta-box')?.innerText || ''")
            html_content = page.evaluate("() => document.querySelector('.article-content, .markdown-body, article')?.innerHTML || ''")

            res["title"] = (title or "").strip()
            res["author"] = (author or "").strip()
            res["publish_time"] = (time_str or "").strip()
            res["html"] = (html_content or "").strip()
            browser.close()
    except Exception:
        pass
    return res

class JuejinScraper(BaseScraper):
    """掘金文章抓取器 (API / HTML / 无头浏览器三通道高容灾架构)"""
    
    def __init__(
        self,
        target: str,
        enable_noise_filter: bool = True,
        max_articles: Optional[int] = None,
        remove_image_watermark: bool = True
    ):
        super().__init__(target, enable_noise_filter=enable_noise_filter, max_articles=max_articles, remove_image_watermark=remove_image_watermark)
        self.resolved_user_id = None
        self.resolved_author_name = None

    async def _resolve_from_post_url(self, post_url: str) -> Optional[str]:
        """从单篇掘金文章页面逆向解析博主 user_id"""
        try:
            resp = await self.client.get(post_url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                # 寻找作者链接 /user/885523901319033
                author_link = soup.select_one("a[href*='/user/']")
                if author_link and author_link.get("href"):
                    m = re.search(r"/user/(\d+)", author_link["href"])
                    if m:
                        self.resolved_user_id = m.group(1)
                
                author_tag = soup.select_one(".author-name, .username, .name")
                if author_tag:
                    self.resolved_author_name = author_tag.text.strip()
                    
                # 从 script 中匹配 user_id
                if not self.resolved_user_id:
                    m = re.search(r'\"user_id\":\"(\d+)\"', resp.text)
                    if m:
                        self.resolved_user_id = m.group(1)
        except Exception:
            pass
        return self.resolved_user_id

    async def _extract_user_id(self) -> str:
        if self.resolved_user_id:
            return self.resolved_user_id
            
        # 1. 匹配标准用户主页 juejin.cn/user/123456
        match = re.search(r"juejin\.cn/user/(\d+)", self.target)
        if match:
            self.resolved_user_id = match.group(1)
            return self.resolved_user_id

        # 2. 如果输入的是单篇文章链接 juejin.cn/post/123456
        if "juejin.cn/post/" in self.target:
            uid = await self._resolve_from_post_url(self.target)
            if uid:
                return uid

        # 3. 纯数字 ID
        match = re.search(r"(\d{10,})", self.target)
        if match:
            self.resolved_user_id = match.group(1)
            return self.resolved_user_id

        return self.target.strip()

    async def get_author_info(self) -> Dict[str, Any]:
        user_id = await self._extract_user_id()
        api_url = f"https://api.juejin.cn/user_api/v1/user/get?user_id={user_id}"
        author_name = self.resolved_author_name or f"掘金用户_{user_id}"
        avatar = ""
        bio = ""
        try:
            resp = await self.client.get(api_url)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("err_no") == 0 and "data" in data:
                    user_data = data["data"]
                    author_name = user_data.get("user_name", author_name)
                    avatar = user_data.get("avatar_large", "")
                    bio = user_data.get("description", "")
        except Exception:
            pass
        return {"name": author_name, "avatar": avatar, "bio": bio, "user_id": user_id, "platform": "掘金"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        user_id = await self._extract_user_id()
        api_url = "https://api.juejin.cn/content_api/v1/article/query_list"
        articles = []
        cursor = "0"
        
        # 如果 target 是单篇文章，直接作为单篇列表返回
        if "juejin.cn/post/" in self.target:
            m = re.search(r"juejin\.cn/post/(\d+)", self.target)
            art_id = m.group(1) if m else self.target.strip()
            return [{"id": art_id, "url": self.target, "title": "掘金文章", "publish_time": ""}]

        while True:
            payload = {
                "user_id": user_id,
                "sort_type": 2, # 2=最新发布
                "cursor": cursor
            }
            try:
                resp = await self.client.post(api_url, json=payload)
                if resp.status_code != 200:
                    break
                data = resp.json()
                if data.get("err_no") != 0 or not data.get("data"):
                    break
                    
                items = data["data"]
                for item in items:
                    article_info = item.get("article_info", {})
                    article_id = article_info.get("article_id")
                    title = article_info.get("title", "")
                    ctime = article_info.get("ctime", "")
                    
                    if not article_id:
                        continue
                        
                    url = f"https://juejin.cn/post/{article_id}"
                    
                    # 格式化发布时间
                    publish_time = ""
                    if ctime:
                        try:
                            publish_time = datetime.datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass
                            
                    articles.append({
                        "id": article_id,
                        "url": url,
                        "title": title,
                        "publish_time": publish_time,
                        "brief_content": article_info.get("brief_content", "")
                    })
                    
                    if self.max_articles and len(articles) >= self.max_articles:
                        return articles
                        
                if progress_callback:
                    progress_callback(f"已发现掘金文章 {len(articles)} 篇...", len(articles), 0)
                    
                if not data.get("has_more", False):
                    break
                cursor = str(data.get("cursor", ""))
                if not cursor or cursor == "0":
                    break
            except Exception:
                break
                
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        article_id = article_meta["id"]
        url = article_meta.get("url") or f"https://juejin.cn/post/{article_id}"
        title = article_meta.get("title", "掘金文章")
        publish_time = article_meta.get("publish_time", "")
        author = ""
        
        # ==========================================================
        # 通道 1: 尝试官方开放 API 接口
        # ==========================================================
        detail_api = "https://api.juejin.cn/content_api/v1/article/detail"
        try:
            resp = await self.client.post(detail_api, json={"article_id": article_id})
            if resp.status_code == 200:
                data = resp.json()
                if data.get("err_no") == 0 and "data" in data and data["data"]:
                    article_data = data["data"]
                    article_info = article_data.get("article_info", {})
                    author_user_info = article_data.get("author_user_info", {})
                    
                    title = article_info.get("title", title)
                    author = author_user_info.get("user_name", "")
                    raw_mark_content = article_info.get("mark_content", "")
                    raw_html_content = article_info.get("content", "")
                    
                    if raw_mark_content:
                        md_content = raw_mark_content
                        if self.enable_noise_filter:
                            md_content = filter_noise_text(md_content)
                        images = re.findall(r"!\[.*?\]\((https?://[^\s\)]+)\)", md_content)
                        cleaned_html = f"<div class='markdown-body'>{raw_html_content}</div>"
                    else:
                        cleaned_html, md_content, images = clean_html_content(
                            raw_html_content,
                            self.enable_noise_filter,
                            remove_watermark=self.remove_image_watermark
                        )
                        
                    if len(md_content.strip()) > 30:
                        return ArticleItem(
                            id=article_id,
                            title=title,
                            author=author,
                            publish_time=publish_time,
                            url=url,
                            platform="掘金",
                            summary=article_info.get("brief_content", ""),
                            content_html=cleaned_html,
                            content_markdown=md_content,
                            images=images
                        )
        except Exception:
            pass

        # ==========================================================
        # 通道 2: 直接请求网页端 HTML 并解析 DOM 与 SSR 数据（极速、免风控）
        # ==========================================================
        try:
            resp = await self.client.get(url)
            if resp.status_code == 200 and resp.text:
                soup = BeautifulSoup(resp.text, "lxml")
                
                # 提取文章正文区域
                content_tag = soup.select_one(".article-content, .markdown-body, article")
                if content_tag:
                    raw_html = str(content_tag)
                    
                    # 提取标题
                    title_tag = soup.select_one(".article-title, h1.title, h1")
                    if title_tag and title_tag.text.strip():
                        title = title_tag.text.strip()
                        
                    # 提取作者
                    author_tag = soup.select_one(".author-name, .username, .name")
                    if author_tag and author_tag.text.strip():
                        author = author_tag.text.strip()
                        
                    # 提取发布时间
                    time_tag = soup.select_one(".time, time, .meta-box")
                    if time_tag and time_tag.text.strip():
                        t_match = re.search(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:\s+\d{1,2}:\d{1,2}(?::\d{1,2})?)?", time_tag.text)
                        if t_match:
                            publish_time = t_match.group(0)

                    cleaned_html, md_content, images = clean_html_content(
                        raw_html,
                        self.enable_noise_filter,
                        remove_watermark=self.remove_image_watermark
                    )

                    if len(md_content.strip()) > 30:
                        return ArticleItem(
                            id=article_id,
                            title=title,
                            author=author,
                            publish_time=publish_time,
                            url=url,
                            platform="掘金",
                            summary="",
                            content_html=cleaned_html,
                            content_markdown=md_content,
                            images=images
                        )
        except Exception:
            pass

        # ==========================================================
        # 通道 3: Playwright 无头浏览器动态渲染兜底
        # ==========================================================
        try:
            extracted = await asyncio.to_thread(_scrape_juejin_playwright_sync, url)
            if extracted.get("html") and len(extracted["html"].strip()) > 30:
                if extracted.get("title"):
                    title = extracted["title"]
                if extracted.get("author"):
                    author = extracted["author"]
                if extracted.get("publish_time"):
                    publish_time = extracted["publish_time"]

                cleaned_html, md_content, images = clean_html_content(
                    extracted["html"],
                    self.enable_noise_filter,
                    remove_watermark=self.remove_image_watermark
                )

                return ArticleItem(
                    id=article_id,
                    title=title,
                    author=author,
                    publish_time=publish_time,
                    url=url,
                    platform="掘金",
                    summary="",
                    content_html=cleaned_html,
                    content_markdown=md_content,
                    images=images
                )
        except Exception:
            pass

        return ArticleItem(
            id=article_id,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="掘金",
            content_html="<p>内容获取异常，请稍后重试</p>",
            content_markdown="内容获取异常，请稍后重试",
            images=[]
        )
