import re
import time
import asyncio
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
import httpx

from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content
from app.config import DEFAULT_HEADERS

BOT_USER_AGENT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def _launch_browser(p):
    """优先调用 Edge 浏览器内核，其次 Chrome，最后 Chromium，配备完整反反爬参数"""
    launch_args = [
        '--disable-blink-features=AutomationControlled',
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-infobars',
        '--window-size=1920,1080',
        '--disable-dev-shm-usage'
    ]
    for channel in ["msedge", "chrome", None]:
        try:
            if channel:
                return p.chromium.launch(channel=channel, headless=True, args=launch_args)
            else:
                return p.chromium.launch(headless=True, args=launch_args)
        except Exception:
            continue
    return p.chromium.launch(headless=True, args=launch_args)


def _fetch_51cto_via_bot_sync(user_id: str, max_articles: Optional[int] = None) -> Dict[str, Any]:
    """
    通道 1: 通过搜索引擎安全通道穿透 EdgeOne WAF 防护，
    零无头浏览器开销、秒级分页拉取 51CTO 博主全量博文目录与信息。
    """
    author_name = user_id
    articles = []
    seen_urls = set()
    article_id_pattern = re.compile(rf"/{user_id}/(\d+)")

    with httpx.Client(timeout=10.0, verify=False, follow_redirects=True) as client:
        headers = {
            "User-Agent": BOT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://blog.51cto.com/"
        }

        for page in range(1, 50):
            url = f"https://blog.51cto.com/{user_id}/p{page}" if page > 1 else f"https://blog.51cto.com/{user_id}"
            try:
                resp = client.get(url, headers=headers)
                if resp.status_code != 200 or len(resp.text) < 1000:
                    break

                soup = BeautifulSoup(resp.text, "lxml")

                # 提取博主昵称 (仅在第1页)
                if page == 1:
                    name_tag = soup.select_one(".name, .username, h1, .avatar-name, .base-info .name, .user-info .name, .nickname")
                    if name_tag and name_tag.text.strip():
                        raw_name = name_tag.text.strip()
                        author_name = raw_name.removesuffix("的博客").strip()
                    elif soup.title and soup.title.string:
                        m_title = re.search(r"^(.*?)的博客", soup.title.string)
                        if m_title:
                            author_name = m_title.group(1).strip()

                page_new_count = 0
                # 匹配文章标题标签
                for a in soup.select("a.title, .item a.title, h3.title a, .graphic h3 a, h3 a, h2 a, h4 a, .artical-title a, .article-item a, a"):
                    href = a.get("href", "").strip()
                    m = article_id_pattern.search(href)
                    if m:
                        art_id = m.group(1)
                        full_url = f"https://blog.51cto.com/{user_id}/{art_id}"
                        if full_url in seen_urls:
                            continue

                        title = a.text.strip()
                        if not title or len(title) > 120 or "\n" in title:
                            title_tag = a.select_one(".title, h3, h2, h4")
                            if title_tag:
                                title = title_tag.text.strip()
                            elif "\n" in title:
                                title = title.split("\n")[0].strip()

                        # 过滤非文章标题
                        if title and "篇" not in title and not title.startswith("202") and title not in ["动态", "专栏", "资源", "问答", "博文", "粉丝", "关注"]:
                            seen_urls.add(full_url)
                            articles.append({
                                "id": full_url,
                                "url": full_url,
                                "title": title
                            })
                            page_new_count += 1
                            if max_articles and len(articles) >= max_articles:
                                break

                if page_new_count == 0:
                    break

                if max_articles and len(articles) >= max_articles:
                    articles = articles[:max_articles]
                    break

            except Exception as e:
                print(f"51CTO 搜索引擎通道第 {page} 页抓取异常: {e}")
                break

    return {
        "author_name": author_name,
        "user_id": user_id,
        "articles": articles,
        "cookies": {}
    }


def _scrape_51cto_author_and_list_sync(target: str, max_articles: Optional[int] = None) -> Dict[str, Any]:
    """
    多通道 51CTO 抓取引擎：
    1. 优先通过搜索引擎安全直连通道穿透 WAF，极速拉取；
    2. 兜底通过 Playwright 无头浏览器渲染突破。
    """
    # 提取 user_id
    match = re.search(r"blog\.51cto\.com/([a-zA-Z0-9\-_]+)", target)
    if match:
        user_id = match.group(1)
    else:
        user_id = target.strip("/ ")

    art_match = re.search(r"blog\.51cto\.com/([a-zA-Z0-9\-_]+)/(\d+)", target)
    if art_match:
        user_id = art_match.group(1)

    # 优先尝试通道 1
    bot_res = _fetch_51cto_via_bot_sync(user_id, max_articles)
    if bot_res.get("articles") and len(bot_res["articles"]) > 0:
        return bot_res

    # 通道 2: Playwright 兜底
    from playwright.sync_api import sync_playwright
    author_name = user_id
    articles = []
    cookies_dict = {}

    try:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
                timezone_id='Asia/Shanghai'
            )
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            """)

            home_url = f"https://blog.51cto.com/{user_id}"
            
            try:
                page.goto(home_url, wait_until="commit", timeout=30000)
                
                for _ in range(10):
                    time.sleep(1)
                    try:
                        t = page.title()
                        if t and ("51CTO" in t or "博客" in t):
                            break
                    except Exception:
                        pass

                html = page.content()
                soup = BeautifulSoup(html, "lxml")

                name_tag = soup.select_one(".name, .username, h1, .avatar-name, .base-info .name, .user-info .name")
                if name_tag and name_tag.text.strip():
                    raw_name = name_tag.text.strip()
                    author_name = raw_name.removesuffix("的博客").strip()

                for c in context.cookies():
                    cookies_dict[c["name"]] = c["value"]

                article_id_pattern = re.compile(rf"/{user_id}/(\d+)")
                seen_urls = set()

                for a in soup.select("a.title, .item a.title, h3.title a, .graphic h3 a, h3 a, h2 a, h4 a, .artical-title a, .article-item a, a"):
                    href = a.get("href", "").strip()
                    m = article_id_pattern.search(href)
                    if m:
                        art_id = m.group(1)
                        full_url = f"https://blog.51cto.com/{user_id}/{art_id}"
                        if full_url in seen_urls:
                            continue
                        title = a.text.strip()
                        if not title or len(title) > 120 or "\n" in title:
                            title_tag = a.select_one(".title, h3, h2, h4")
                            if title_tag:
                                title = title_tag.text.strip()
                            elif "\n" in title:
                                title = title.split("\n")[0].strip()
                        if title and "篇" not in title and not title.startswith("202") and title not in ["动态", "专栏", "资源", "问答"]:
                            seen_urls.add(full_url)
                            articles.append({
                                "id": full_url,
                                "url": full_url,
                                "title": title
                            })

            except Exception as e:
                print(f"51CTO Playwright 页面抓取过程异常: {e}")
            finally:
                browser.close()
    except Exception as e:
        print(f"51CTO 启动 Playwright 失败: {e}")

    return {
        "author_name": author_name,
        "user_id": user_id,
        "articles": articles,
        "cookies": cookies_dict
    }


def _scrape_51cto_detail_playwright_sync(url: str, cookies: Dict[str, str]) -> Dict[str, Any]:
    """使用 Playwright 兜底抓取 51CTO 单篇文章详情"""
    from playwright.sync_api import sync_playwright

    title = ""
    publish_time = ""
    raw_html = ""

    try:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0'
            )
            if cookies:
                cookie_objs = [{"name": k, "value": v, "domain": ".51cto.com", "path": "/"} for k, v in cookies.items()]
                context.add_cookies(cookie_objs)

            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.5)
                soup = BeautifulSoup(page.content(), "lxml")

                h1 = soup.select_one(".main-title, .article-title, h1.title, h1")
                if h1 and h1.text.strip():
                    title = h1.text.strip()
                elif soup.title and soup.title.string:
                    clean_t = re.split(r'[-_]', soup.title.string)[0].strip()
                    if clean_t:
                        title = clean_t

                time_tag = soup.select_one("time, .mess-line1, .msg-left, .time, .pub-time, .date, .artical-time")
                if time_tag:
                    t_match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", time_tag.text)
                    if t_match:
                        publish_time = t_match.group(0)

                content_tag = soup.select_one(".article-content-wrap, .main-content, #content, .article-content")
                raw_html = str(content_tag) if content_tag else page.content()
            except Exception as e:
                print(f"Playwright 单篇详情抓取异常: {e}")
            finally:
                browser.close()
    except Exception as e:
        print(f"Playwright 单篇启动异常: {e}")

    return {
        "title": title,
        "publish_time": publish_time,
        "raw_html": raw_html
    }


class CTO51Scraper(BaseScraper):
    """51CTO 博客文章抓取器 (多通道秒级突破 EdgeOne WAF 防护)"""

    def __init__(self, target: str, enable_noise_filter: bool = True, max_articles: Optional[int] = None, remove_image_watermark: bool = True):
        super().__init__(target, enable_noise_filter=enable_noise_filter, max_articles=max_articles, remove_image_watermark=remove_image_watermark)
        self.cookies_cache: Dict[str, str] = {}
        self.cached_author_data: Optional[Dict[str, Any]] = None

    def _extract_user_id(self) -> str:
        match = re.search(r"blog\.51cto\.com/([a-zA-Z0-9\-_]+)", self.target)
        if match:
            return match.group(1)
        return self.target.strip("/ ")

    async def _ensure_author_and_list(self):
        """确保在需要时一次性获取博主信息与文章列表"""
        if self.cached_author_data is None:
            res = await asyncio.to_thread(_scrape_51cto_author_and_list_sync, self.target, self.max_articles)
            self.cached_author_data = res
            self.cookies_cache = res.get("cookies", {})

    async def get_author_info(self) -> Dict[str, Any]:
        await self._ensure_author_and_list()
        user_id = self.cached_author_data.get("user_id", self._extract_user_id())
        author_name = self.cached_author_data.get("author_name", user_id)
        return {"name": author_name, "user_id": user_id, "platform": "51CTO"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        m_art = re.search(r"blog\.51cto\.com/[a-zA-Z0-9\-_]+/(\d+)", self.target)
        if m_art:
            return [{"id": self.target, "url": self.target, "title": "51CTO单篇文章"}]

        await self._ensure_author_and_list()
        articles = self.cached_author_data.get("articles", [])
        
        if self.max_articles and len(articles) > self.max_articles:
            articles = articles[:self.max_articles]
            
        if progress_callback:
            progress_callback(f"已发现 51CTO 文章 {len(articles)} 篇...", len(articles), 0)
            
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta["url"]
        title = article_meta.get("title", "").strip() or "无标题"
        publish_time = ""
        author = self.cached_author_data.get("author_name", "") if self.cached_author_data else ""
        if not author:
            author = self._extract_user_id()

        # 方案 1：使用搜索引擎 Bot 安全通道直接读取
        headers = {
            "User-Agent": BOT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"https://blog.51cto.com/{self._extract_user_id()}"
        }
        try:
            resp = await self.client.get(url, headers=headers, timeout=12.0)
            if resp.status_code == 200 and "EO-Bot-Js-Token" not in resp.text and "<title>屏蔽" not in resp.text and "<title>被屏蔽" not in resp.text:
                soup = BeautifulSoup(resp.text, "lxml")

                h1 = soup.select_one(".main-title, .article-title, h1.title, h1")
                if h1 and h1.text.strip():
                    title = h1.text.strip()
                elif (not title or title == "无标题") and soup.title and soup.title.string:
                    clean_t = re.split(r'[-_]', soup.title.string)[0].strip()
                    if clean_t:
                        title = clean_t

                time_tag = soup.select_one("time, .mess-line1, .msg-left, .time, .pub-time, .date, .artical-time")
                if time_tag:
                    t_match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", time_tag.text)
                    if t_match:
                        publish_time = t_match.group(0)

                content_tag = soup.select_one(".article-content-wrap, .main-content, #content, .article-content, #container, .detail-content, article")
                if content_tag:
                    raw_html = str(content_tag)
                    cleaned_html, md_content, images = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)
                    return ArticleItem(
                        id=url,
                        title=title,
                        author=author,
                        publish_time=publish_time,
                        url=url,
                        platform="51CTO",
                        content_html=cleaned_html,
                        content_markdown=md_content,
                        images=images
                    )
        except Exception as e:
            print(f"51CTO Bot 通道抓取详情失败，尝试 Playwright 兜底: {e}")

        # 方案 2：Playwright 无头浏览器动态渲染兜底
        try:
            detail_res = await asyncio.to_thread(_scrape_51cto_detail_playwright_sync, url, self.cookies_cache)
            if detail_res.get("title") and (not title or title == "无标题"):
                title = detail_res["title"]
            if detail_res.get("publish_time"):
                publish_time = detail_res["publish_time"]
            raw_html = detail_res.get("raw_html", "")

            if raw_html:
                cleaned_html, md_content, images = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)
                return ArticleItem(
                    id=url,
                    title=title,
                    author=author,
                    publish_time=publish_time,
                    url=url,
                    platform="51CTO",
                    content_html=cleaned_html,
                    content_markdown=md_content,
                    images=images
                )
        except Exception as e:
            print(f"51CTO Playwright 抓取单篇详情最终失败: {e}")

        return ArticleItem(
            id=url,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="51CTO",
            content_html="<p>抓取失败</p>",
            content_markdown="抓取失败",
            images=[]
        )
