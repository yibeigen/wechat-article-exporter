import re
import json
import asyncio
import datetime
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
import httpx

from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content
from app.config import DEFAULT_HEADERS
from app.core.zhihu_auth import get_saved_zhihu_cookies

class ZhihuScraper(BaseScraper):
    """知乎全维度博主内容抓取器 (支持专栏/个人主页/单篇内容溯源全量抓取)"""
    
    def __init__(self, target: str, enable_noise_filter: bool = True, max_articles: Optional[int] = None):
        super().__init__(target, enable_noise_filter=enable_noise_filter, max_articles=max_articles)
        self.cookies = get_saved_zhihu_cookies()
        self.author_info_cache = {}

    def _is_direct_article_url(self) -> bool:
        return bool(re.search(r"zhuanlan\.zhihu\.com/p/\d+|zhihu\.com/question/\d+/answer/\d+|zhihu\.com/p/\d+", self.target))

    def _is_column_url(self) -> bool:
        return bool(re.search(r"zhuanlan\.zhihu\.com/(?:c_|column/)|zhihu\.com/column/", self.target))

    def _is_people_url(self) -> bool:
        return bool(re.search(r"zhihu\.com/people/([a-zA-Z0-9\-_]+)", self.target))

    def _extract_id(self) -> str:
        # 专栏链接: zhuanlan.zhihu.com/c_xxxx 或 zhihu.com/column/xxxx
        match = re.search(r"(?:zhuanlan\.zhihu\.com/(?:c_|column/)|zhihu\.com/column/)([a-zA-Z0-9\-_]+)", self.target)
        if match:
            return match.group(1)
        # 用户主页链接: zhihu.com/people/xxxx
        match = re.search(r"zhihu\.com/people/([a-zA-Z0-9\-_]+)", self.target)
        if match:
            return match.group(1)
        # 单篇文章链接
        match = re.search(r"(?:zhuanlan\.zhihu\.com/p/|zhihu\.com/p/)(\d+)", self.target)
        if match:
            return match.group(1)
        # 单个回答链接
        match = re.search(r"zhihu\.com/question/\d+/answer/(\d+)", self.target)
        if match:
            return match.group(1)
            
        return self.target.strip("/ ")

def _resolve_author_sync(url: str, cookies: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """在工作线程中同步解析单篇文章作者"""
    import time
    from playwright.sync_api import sync_playwright
    
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="msedge", headless=True, args=['--disable-blink-features=AutomationControlled'])
            except Exception:
                browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
                
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0'
            )
            
            if cookies:
                cookie_objs = [{"name": k, "value": v, "domain": ".zhihu.com", "path": "/"} for k, v in cookies.items()]
                context.add_cookies(cookie_objs)
            else:
                context.add_cookies([
                    {"name": "d_c0", "value": "AGCYyO_uBxqPTv1-XpL4_4h3f8s9a0b1c2d=", "domain": ".zhihu.com", "path": "/"},
                    {"name": "_zap", "value": "81f18579-9943-4dc7-a7eb-079ce2fbe0ab", "domain": ".zhihu.com", "path": "/"}
                ])
                
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
            
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.5)
            
            author_data = page.evaluate('''() => {
                let name = '';
                let urlToken = '';
                
                // 1. 从文章/回答数据中提取精准作者 (防止误匹配当前登录用户)
                const initialData = document.querySelector('#js-initialData');
                if (initialData) {
                    try {
                        const state = JSON.parse(initialData.innerText);
                        const articles = state.initialState?.entities?.articles || {};
                        const keys = Object.keys(articles);
                        if (keys.length > 0) {
                            const art = articles[keys[0]];
                            if (art && art.author) {
                                name = art.author.name || '';
                                urlToken = art.author.urlToken || '';
                            }
                        }
                        
                        if (!urlToken) {
                            const answers = state.initialState?.entities?.answers || {};
                            const ansKeys = Object.keys(answers);
                            if (ansKeys.length > 0) {
                                const ans = answers[ansKeys[0]];
                                if (ans && ans.author) {
                                    name = ans.author.name || '';
                                    urlToken = ans.author.urlToken || '';
                                }
                            }
                        }
                    } catch(e) {}
                }
                
                // 2. 查找 DOM 作者链接
                if (!urlToken) {
                    const authorLink = document.querySelector('.AuthorInfo-name a, a.UserLink-link, .Post-Author a, a[href*="/people/"]');
                    if (authorLink) {
                        name = authorLink.innerText.trim();
                        const href = authorLink.href;
                        const m = href.match(/zhihu\\.com\\/people\\/([^\\/\\?#]+)/);
                        if (m) urlToken = m[1];
                    }
                }
                
                if (!name) {
                    const fallbackNameEl = document.querySelector('.AuthorInfo-name, .UserLink, .Post-Author, [itemprop="name"]');
                    if (fallbackNameEl) name = fallbackNameEl.innerText.trim();
                }
                if (!name && urlToken) name = urlToken;
                
                return urlToken ? { name, url_token: urlToken } : null;
            }''')
            
            browser.close()
            return author_data
    except Exception as e:
        print(f"溯源博主信息异常: {e}")
        return None

def _scrape_user_all_content_sync(url_token: str, author_name: str, cookies: Optional[Dict[str, str]], max_articles: Optional[int], target_fallback_url: str, is_direct_article: bool, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
    """在工作线程中同步抓取博主全部回答与专栏文章"""
    import time
    from playwright.sync_api import sync_playwright
    
    all_articles: List[Dict[str, str]] = []
    seen_urls = set()

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="msedge", headless=True, args=['--disable-blink-features=AutomationControlled'])
            except Exception:
                browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])

            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
                viewport={'width': 1280, 'height': 900}
            )

            if cookies:
                cookie_objs = [{"name": k, "value": v, "domain": ".zhihu.com", "path": "/"} for k, v in cookies.items()]
                context.add_cookies(cookie_objs)
            else:
                context.add_cookies([
                    {"name": "d_c0", "value": "AGCYyO_uBxqPTv1-XpL4_4h3f8s9a0b1c2d=", "domain": ".zhihu.com", "path": "/"},
                    {"name": "_zap", "value": "81f18579-9943-4dc7-a7eb-079ce2fbe0ab", "domain": ".zhihu.com", "path": "/"}
                ])

            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

            # 先加载博主主页，激活浏览器会话与安全签名
            page.goto(f"https://www.zhihu.com/people/{url_token}", wait_until="domcontentloaded", timeout=25000)
            time.sleep(1.5)

            # ==========================================================
            # 步骤 1: 全量抓取博主专栏文章 (Articles)
            # ==========================================================
            offset = 0
            limit = 20
            while True:
                if max_articles and len(all_articles) >= max_articles:
                    break
                    
                if progress_callback:
                    progress_callback(f"正在抓取博主【{author_name}】的专栏文章 (已发现 {len(all_articles)} 篇)...", len(all_articles), 0)

                articles_data = page.evaluate(f'''async () => {{
                    try {{
                        const resp = await fetch('/api/v4/members/{url_token}/articles?include=data[*].content,voteup_count,created&offset={offset}&limit={limit}&sort_by=created');
                        return await resp.json();
                    }} catch(e) {{
                        return null;
                    }}
                }}''')

                if not articles_data or not articles_data.get("data"):
                    break

                items = articles_data.get("data", [])
                if not items:
                    break

                for it in items:
                    art_id = str(it.get("id", ""))
                    u = it.get("url") or f"https://zhuanlan.zhihu.com/p/{art_id}"
                    if u not in seen_urls:
                        seen_urls.add(u)
                        created_time = it.get("created", 0) or it.get("updated", 0)
                        pub_str = ""
                        if created_time:
                            try:
                                pub_str = datetime.datetime.fromtimestamp(int(created_time)).strftime("%Y-%m-%d %H:%M:%S")
                            except Exception:
                                pass
                        all_articles.append({
                            "id": art_id or u,
                            "url": u,
                            "title": "【专栏】" + it.get("title", f"专栏文章_{art_id}"),
                            "publish_time": pub_str,
                            "content_html": it.get("content", ""),
                            "excerpt": it.get("excerpt", "")
                        })
                        if max_articles and len(all_articles) >= max_articles:
                            break

                if max_articles and len(all_articles) >= max_articles:
                    break

                paging = articles_data.get("paging", {})
                if paging.get("is_end", True):
                    break
                offset += limit
                if offset > 2000:
                    break

            # ==========================================================
            # 步骤 2: 全量抓取博主历史回答 (Answers)
            # ==========================================================
            offset = 0
            while True:
                if max_articles and len(all_articles) >= max_articles:
                    break

                if progress_callback:
                    progress_callback(f"正在抓取博主【{author_name}】的历史回答 (已发现 {len(all_articles)} 篇)...", len(all_articles), 0)

                answers_data = page.evaluate(f'''async () => {{
                    try {{
                        const resp = await fetch('/api/v4/members/{url_token}/answers?include=data[*].content,voteup_count,created_time&offset={offset}&limit={limit}&sort_by=created');
                        return await resp.json();
                    }} catch(e) {{
                        return null;
                    }}
                }}''')

                if not answers_data or not answers_data.get("data"):
                    break

                items = answers_data.get("data", [])
                if not items:
                    break

                for it in items:
                    ans_id = str(it.get("id", ""))
                    q_id = str(it.get("question", {}).get("id", ""))
                    u = f"https://www.zhihu.com/question/{q_id}/answer/{ans_id}" if q_id else f"https://www.zhihu.com/answer/{ans_id}"
                    if u not in seen_urls:
                        seen_urls.add(u)
                        q_title = it.get("question", {}).get("title", f"回答_{ans_id}")
                        created_time = it.get("created_time", 0) or it.get("updated_time", 0)
                        pub_str = ""
                        if created_time:
                            try:
                                pub_str = datetime.datetime.fromtimestamp(int(created_time)).strftime("%Y-%m-%d %H:%M:%S")
                            except Exception:
                                pass
                        all_articles.append({
                            "id": ans_id or u,
                            "url": u,
                            "title": "【回答】" + q_title,
                            "publish_time": pub_str,
                            "content_html": it.get("content", ""),
                            "excerpt": it.get("excerpt", "")
                        })
                        if max_articles and len(all_articles) >= max_articles:
                            break

                if max_articles and len(all_articles) >= max_articles:
                    break

                paging = answers_data.get("paging", {})
                if paging.get("is_end", True):
                    break
                offset += limit
                if offset > 2000:
                    break

            browser.close()
    except Exception as e:
        print(f"Playwright 同步抓取异常: {e}")

    # 如果仍未抓到任何列表内容，返回单篇内容兜底
    if not all_articles and is_direct_article:
        all_articles.append({
            "id": target_fallback_url,
            "url": target_fallback_url,
            "title": f"知乎博主文章",
            "publish_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    return all_articles

def _scrape_detail_sync(url: str, cookies: Optional[Dict[str, str]]) -> Dict[str, str]:
    """在工作线程中同步获取单篇文章正文与标题"""
    import time
    from playwright.sync_api import sync_playwright
    
    res = {"title": "", "html": ""}
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="msedge", headless=True, args=['--disable-blink-features=AutomationControlled'])
            except Exception:
                browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0'
            )
            if cookies:
                cookie_objs = [{"name": k, "value": v, "domain": ".zhihu.com", "path": "/"} for k, v in cookies.items()]
                context.add_cookies(cookie_objs)
            else:
                context.add_cookies([
                    {"name": "d_c0", "value": "AGCYyO_uBxqPTv1-XpL4_4h3f8s9a0b1c2d=", "domain": ".zhihu.com", "path": "/"}
                ])
            
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.5)
            
            extracted = page.evaluate('''() => {
                const titleEl = document.querySelector('h1.Post-Title, .QuestionHeader-title, h1');
                const richEl = document.querySelector('.Post-RichText, .RichContent-inner, .RichText, .css-79elbk');
                return {
                    title: titleEl ? titleEl.innerText.trim() : '',
                    html: richEl ? richEl.innerHTML : ''
                };
            }''')
            
            if extracted:
                res["title"] = extracted.get("title", "")
                res["html"] = extracted.get("html", "")
                
            browser.close()
    except Exception as e:
        print(f"详情同步抓取异常: {e}")
        
    return res

class ZhihuScraper(BaseScraper):
    """知乎全维度博主内容抓取器 (支持专栏/个人主页/单篇内容溯源全量抓取)"""
    
    def __init__(self, target: str, enable_noise_filter: bool = True, max_articles: Optional[int] = None, remove_image_watermark: bool = True):
        super().__init__(target, enable_noise_filter=enable_noise_filter, max_articles=max_articles, remove_image_watermark=remove_image_watermark)
        self.cookies = get_saved_zhihu_cookies()
        self.author_info_cache = {}

    def _is_direct_article_url(self) -> bool:
        return bool(re.search(r"zhuanlan\.zhihu\.com/p/\d+|zhihu\.com/question/\d+/answer/\d+|zhihu\.com/p/\d+", self.target))

    def _is_column_url(self) -> bool:
        return bool(re.search(r"zhuanlan\.zhihu\.com/(?:c_|column/)|zhihu\.com/column/", self.target))

    def _is_people_url(self) -> bool:
        return bool(re.search(r"zhihu\.com/people/([a-zA-Z0-9\-_]+)", self.target))

    def _extract_id(self) -> str:
        match = re.search(r"(?:zhuanlan\.zhihu\.com/(?:c_|column/)|zhihu\.com/column/)([a-zA-Z0-9\-_]+)", self.target)
        if match:
            return match.group(1)
        match = re.search(r"zhihu\.com/people/([a-zA-Z0-9\-_]+)", self.target)
        if match:
            return match.group(1)
        match = re.search(r"(?:zhuanlan\.zhihu\.com/p/|zhihu\.com/p/)(\d+)", self.target)
        if match:
            return match.group(1)
        match = re.search(r"zhihu\.com/question/\d+/answer/(\d+)", self.target)
        if match:
            return match.group(1)
            
        return self.target.strip("/ ")

    async def _resolve_author_from_single_content(self, url: str) -> Optional[Dict[str, str]]:
        """从单篇知乎文章或回答中，逆向溯源提取文章作者的 url_token 和昵称（线程隔离，防 Windows 异步冲突）"""
        return await asyncio.to_thread(_resolve_author_sync, url, self.cookies)

    async def get_author_info(self) -> Dict[str, Any]:
        """获取目标博主或专栏的元数据信息"""
        if self.author_info_cache:
            return self.author_info_cache

        # 1. 如果是专栏
        if self._is_column_url():
            col_id = self._extract_id()
            author_name = f"知乎专栏_{col_id}"
            try:
                api_url = f"https://www.zhihu.com/api/v4/columns/{col_id}"
                resp = await self.client.get(api_url, headers=DEFAULT_HEADERS, cookies=self.cookies)
                if resp.status_code == 200:
                    data = resp.json()
                    author_name = data.get("title") or (data.get("author", {}).get("name", col_id))
            except Exception:
                pass
            res = {"name": author_name, "target_id": col_id, "platform": "知乎"}
            self.author_info_cache = res
            return res

        # 2. 如果是单篇或单回答链接，自动逆向溯源博主
        if self._is_direct_article_url():
            author_data = await self._resolve_author_from_single_content(self.target)
            if author_data:
                res = {
                    "name": author_data.get("name", author_data["url_token"]),
                    "target_id": author_data["url_token"],
                    "platform": "知乎",
                    "sourced_from_article": True
                }
                self.author_info_cache = res
                return res

        # 3. 如果是个人主页或 url_token
        target_id = self._extract_id()
        author_name = target_id
        try:
            api_url = f"https://www.zhihu.com/api/v4/members/{target_id}"
            resp = await self.client.get(api_url, headers=DEFAULT_HEADERS, cookies=self.cookies)
            if resp.status_code == 200:
                data = resp.json()
                author_name = data.get("name", target_id)
        except Exception:
            pass

        res = {"name": author_name, "target_id": target_id, "platform": "知乎"}
        self.author_info_cache = res
        return res

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        """全量获取博主所有历史回答与专栏文章"""
        
        # 1. 如果用户明确指定了仅抓取专栏
        if self._is_column_url():
            return await self._get_column_articles(progress_callback)

        # 2. 确定博主 url_token
        author_info = await self.get_author_info()
        url_token = author_info.get("target_id", self._extract_id())
        author_name = author_info.get("name", url_token)

        if progress_callback:
            progress_callback(f"已锁定博主【{author_name}】(@{url_token})，正在遍历其全量回答与专栏文章...", 0, 0)

        # 3. 通过工作线程中的同步 Playwright 自动化获取博主全部回答和文章
        return await asyncio.to_thread(
            _scrape_user_all_content_sync,
            url_token,
            author_name,
            self.cookies,
            self.max_articles,
            self.target,
            self._is_direct_article_url(),
            progress_callback
        )

    async def _get_column_articles(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        """抓取专栏文章列表"""
        target_id = self._extract_id()
        articles = []
        offset = 0
        limit = 20

        while True:
            api_url = f"https://www.zhihu.com/api/v4/columns/{target_id}/items?limit={limit}&offset={offset}"
            try:
                resp = await self.client.get(api_url, headers=DEFAULT_HEADERS, cookies=self.cookies)
                if resp.status_code != 200:
                    break
                data = resp.json()
                items = data.get("data", [])
                if not items:
                    break
                    
                for item in items:
                    article_id = str(item.get("id", ""))
                    title = item.get("title", "")
                    url = item.get("url", "") or f"https://zhuanlan.zhihu.com/p/{article_id}"
                            
                    created_time = item.get("created", 0) or item.get("created_time", 0)
                    publish_time = ""
                    if created_time:
                        try:
                            publish_time = datetime.datetime.fromtimestamp(int(created_time)).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass
                            
                    articles.append({
                        "id": article_id or url,
                        "url": url,
                        "title": title or f"知乎专栏文章_{article_id}",
                        "publish_time": publish_time,
                        "content_html": item.get("content", ""),
                        "excerpt": item.get("excerpt", "")
                    })
                    
                    if self.max_articles and len(articles) >= self.max_articles:
                        return articles
                        
                if progress_callback:
                    progress_callback(f"已发现知乎专栏文章 {len(articles)} 篇...", len(articles), 0)
                    
                paging = data.get("paging", {})
                if paging.get("is_end", True):
                    break
                offset += limit
                if offset > 2000:
                    break
            except Exception:
                break
                
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        """获取文章/回答的完整清洗正文"""
        url = article_meta["url"]
        title = article_meta.get("title", "知乎文章")
        publish_time = article_meta.get("publish_time", "")
        author_info = await self.get_author_info()
        author = author_info.get("name", "知乎博主")
        raw_html = article_meta.get("content_html", "")

        # 如果列表未直接提供正文，使用 Playwright 同步引擎抓取详情
        if not raw_html or len(raw_html.strip()) < 10:
            extracted = await asyncio.to_thread(_scrape_detail_sync, url, self.cookies)
            if extracted.get("title"):
                title = extracted["title"]
            if extracted.get("html"):
                raw_html = extracted["html"]

        if raw_html:
            cleaned_html, md_content, images = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)
            return ArticleItem(
                id=url,
                title=title,
                author=author,
                publish_time=publish_time,
                url=url,
                platform="知乎",
                summary=article_meta.get("excerpt", ""),
                content_html=cleaned_html,
                content_markdown=md_content,
                images=images
            )

        return ArticleItem(
            id=url,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="知乎",
            content_html="<p>内容提取失败或需要登录知乎账号</p>",
            content_markdown="内容提取失败或需要登录知乎账号",
            images=[]
        )
