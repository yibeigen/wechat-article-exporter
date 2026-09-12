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
            # 步骤 1: 全量抓取博主「文章」Tab（模拟真人浏览，滚动加载）
            # 说明：知乎已收紧 members 接口的签名校验（自行拼 API 的 fetch
            # 会返回 403），因此改为驱动页面 Tab 滚动加载，数据由知乎自己
            # 签好名的请求返回，稳定且不易触发风控
            # ==========================================================

            # 通用 JS：从当前页面的卡片 DOM 中解析出文章/回答条目
            extract_js = '''() => {
                const out = {};
                document.querySelectorAll('.List-item, .Card').forEach(card => {
                    const a = card.querySelector('a[href*="/p/"], a[href*="/answer/"]');
                    if (!a) return;
                    const href = a.href || '';
                    const t = (a.innerText || '').trim();
                    if (!t) return;
                    let type = null;
                    if (/zhihu\\.com\\/p\\/\\d+/.test(href)) type = 'article';
                    else if (/question\\/\\d+\\/answer\\/\\d+/.test(href)) type = 'answer';
                    if (!type) return;
                    const timeEl = card.querySelector('.ContentItem-time');
                    out[href] = { url: href, title: t, type: type, time: timeEl ? (timeEl.innerText || '').trim() : '' };
                });
                return Object.values(out);
            }'''

            def _extract_time(raw_text):
                """从「发布于 2026-09-08 22:45:49」这类文本里提取时间字符串"""
                m = re.search(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", raw_text or "")
                return m.group(0).replace("T", " ") if m else ""

            def _norm_article_url(u):
                """统一文章 URL 主机名：www.zhihu.com/p/x 与 zhuanlan.zhihu.com/p/x 视为同一篇"""
                return re.sub(r"^https?://(?:www\.)?zhihu\.com/p/", "https://zhuanlan.zhihu.com/p/", u or "")

            def _scroll_collect(expected_type):
                """在当前 Tab 页反复滚动直到连续 3 轮无新内容，返回该类型的条目列表"""
                stable_rounds = 0
                last_count = 0
                items = []
                type_label = "文章" if expected_type == "article" else "回答"
                while stable_rounds < 3:
                    if max_articles and len(all_articles) >= max_articles:
                        break
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1.0)
                    raw = page.evaluate(extract_js) or []
                    items = [r for r in raw if r.get("type") == expected_type]
                    if progress_callback:
                        progress_callback(f"正在滚动加载博主【{author_name}】的{type_label} (已发现 {len(items)} 条)...", len(all_articles), 0)
                    if len(items) == last_count:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                        last_count = len(items)
                return items

            # 打开「文章」Tab 并滚动采集
            try:
                page.goto(f"https://www.zhihu.com/people/{url_token}/posts", wait_until="domcontentloaded", timeout=25000)
                time.sleep(2.0)
            except Exception:
                pass
            for r in _scroll_collect("article"):
                u = r.get("url") or ""
                if u in seen_urls:
                    continue
                if max_articles and len(all_articles) >= max_articles:
                    break
                seen_urls.add(u)
                art_id = u.rstrip("/").split("/")[-1]
                all_articles.append({
                    "id": art_id or u,
                    "url": u,
                    "title": "【文章】" + (r.get("title") or f"知乎文章_{art_id}"),
                    "publish_time": _extract_time(r.get("time")),
                    "content_html": "",
                    "excerpt": "",
                    "content_type": "article",  # article 普通知乎文章 / 后续步骤会升级为专栏文章
                    "column_title": None
                })

            # ==========================================================
            # 步骤 2: 全量抓取博主历史回答（同样滚动「回答」Tab 采集）
            # ==========================================================
            try:
                page.goto(f"https://www.zhihu.com/people/{url_token}/answers", wait_until="domcontentloaded", timeout=25000)
                time.sleep(2.0)
            except Exception:
                pass
            for r in _scroll_collect("answer"):
                u = r.get("url") or ""
                if u in seen_urls:
                    continue
                if max_articles and len(all_articles) >= max_articles:
                    break
                seen_urls.add(u)
                ans_id = u.rstrip("/").split("/")[-1]
                all_articles.append({
                    "id": ans_id or u,
                    "url": u,
                    "title": "【回答】" + (r.get("title") or f"回答_{ans_id}"),
                    "publish_time": _extract_time(r.get("time")),
                    "content_html": "",
                    "excerpt": "",
                    "content_type": "answer"  # 知乎问题回答
                })

            # ==========================================================
            # 步骤 3: 最后访问专栏页，建立「文章 URL -> 专栏名」映射并打标
            # （专栏列表接口不受签名保护，随时可访问；放在最后顺路完成）
            # ==========================================================
            try:
                page.goto(f"https://www.zhihu.com/people/{url_token}/columns", wait_until="domcontentloaded", timeout=25000)
                time.sleep(1.5)
                # 轻微滚动一次，触发可能的懒加载内容
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.8)

                # 提取专栏 ID：新版链接为 www.zhihu.com/column/c_xxx，旧版为 zhuanlan.zhihu.com/{slug}
                col_links = page.evaluate('''() => {
                    const out = [];
                    document.querySelectorAll('a[href*="/column/"], a[href*="zhuanlan.zhihu.com"]').forEach(a => {
                        const href = a.href || '';
                        let m = href.match(/zhihu\\.com\\/column\\/([A-Za-z0-9_-]+)\\/?$/);
                        if (!m) m = href.match(/zhuanlan\\.zhihu\\.com\\/([A-Za-z0-9_-]+)\\/?$/);
                        if (m) out.push({ id: m[1], text: (a.innerText || '').trim().slice(0, 60) });
                    });
                    return out;
                }''')

                # 按 ID 去重
                slugs = {}
                for lk in col_links or []:
                    slugs.setdefault(lk.get("id"), lk.get("text") or "")

                if not slugs:
                    # 专栏页可能被登录墙拦截（未配置 Cookie 或 Cookie 失效）
                    wall = page.evaluate("document.body.innerText.includes('请登录后查看')")
                    print(f"[知乎] 未能从专栏页解析到专栏（登录墙: {wall}）。如需专栏分组，请在设置中配置有效知乎 Cookie")

                print(f"[知乎] 从专栏页解析到 {len(slugs)} 个专栏: {list(slugs.keys())}")

                # 专栏归属映射：key=文章 URL, value=该文章所属的专栏名列表
                # 注意：「文章」是全量集合，专栏只是文章的归类标签（一篇文章可属于多个专栏）
                column_map: Dict[str, List[str]] = {}
                for slug in slugs:
                    if not slug:
                        continue
                    # 获取专栏名称（失败则退回链接文本或 slug）
                    col_title = slugs.get(slug) or slug
                    try:
                        col_meta = page.evaluate(f'''async () => {{
                            try {{
                                const resp = await fetch('/api/v4/columns/{slug}');
                                if (!resp.ok) return null;
                                return await resp.json();
                            }} catch(e) {{
                                return null;
                            }}
                        }}''')
                        if col_meta and col_meta.get("title"):
                            col_title = col_meta.get("title")
                    except Exception:
                        pass

                    # 分页拉取该专栏下的全部文章 URL（/articles 失败时退回 /items）
                    coff = 0
                    while True:
                        col_articles = page.evaluate(f'''async () => {{
                            try {{
                                const resp = await fetch('/api/v4/columns/{slug}/articles?limit=20&offset={coff}');
                                if (!resp.ok) return null;
                                return await resp.json();
                            }} catch(e) {{
                                return null;
                            }}
                        }}''')
                        if not col_articles:
                            col_articles = page.evaluate(f'''async () => {{
                                try {{
                                    const resp = await fetch('/api/v4/columns/{slug}/items?limit=20&offset={coff}');
                                    if (!resp.ok) return null;
                                    return await resp.json();
                                }} catch(e) {{
                                    return null;
                                }}
                            }}''')
                        col_items = (col_articles or {}).get("data", [])
                        if not col_items:
                            break
                        for a in col_items:
                            au = a.get("url") or f"https://zhuanlan.zhihu.com/p/{a.get('id')}"
                            column_map.setdefault(_norm_article_url(au), []).append(col_title)
                        col_paging = (col_articles or {}).get("paging", {})
                        if col_paging.get("is_end", True):
                            break
                        coff += 20
                        time.sleep(0.3)  # 轻微延迟，避免触发反爬

                    # 兜底：专栏接口可能漏掉个别文章（实测会少 1 篇），
                    # 再滚动专栏页面本身，从 DOM 里补充文章链接求并集
                    try:
                        page.goto(f"https://www.zhihu.com/column/{slug}", wait_until="domcontentloaded", timeout=25000)
                        time.sleep(1.5)
                        stable_rounds = 0
                        last_count = 0
                        while stable_rounds < 3:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            time.sleep(0.8)
                            p_links = page.evaluate('''() => {
                                const out = new Set();
                                document.querySelectorAll('a[href*="/p/"]').forEach(a => {
                                    const href = a.href || '';
                                    if (/zhihu\\.com\\/p\\/\\d+/.test(href)) out.add(href);
                                });
                                return Array.from(out);
                            }''') or []
                            if len(p_links) == last_count:
                                stable_rounds += 1
                            else:
                                stable_rounds = 0
                                last_count = len(p_links)
                            if last_count >= 500:  # 防御性上限，避免异常页面死循环
                                break
                        for href in p_links:
                            # 新版专栏页在 www.zhihu.com 域下，/p/ 链接可能是 www 前缀，统一归一化
                            column_map.setdefault(_norm_article_url(href), []).append(col_title)
                        print(f"[知乎] 专栏【{col_title}】页面 DOM 补充后累计映射 {sum(1 for v in column_map.values() if col_title in v)} 篇")
                    except Exception as _col_e:
                        print(f"[知乎] 专栏【{col_title}】页面 DOM 兜底解析失败: {_col_e}")

                # 统一打标：专栏只是文章的归类标签——文章的 content_type 保持
                # 「article」不变，仅把所属专栏名写入 column_title 字段
                marked = 0
                for art in all_articles:
                    col_hits = column_map.get(_norm_article_url(art.get("url") or ""))
                    if col_hits and art.get("content_type") == "article":
                        # 去重后拼接（一篇文章可能同时被收入多个专栏）
                        art["column_title"] = "、".join(dict.fromkeys(col_hits))
                        marked += 1
                print(f"[知乎] 专栏映射完成，共为 {marked} 篇文章标注专栏归属")
            except Exception as e:
                print(f"枚举知乎专栏异常（不影响文章抓取）: {e}")

            browser.close()
    except Exception as e:
        print(f"Playwright 同步抓取异常: {e}")

    # 如果仍未抓到任何列表内容，返回单篇内容兜底
    if not all_articles and is_direct_article:
        # 根据回退链接判断是专栏文章还是回答
        fallback_type = "answer" if re.search(r"zhihu\.com/question/\d+/answer/\d+", target_fallback_url) else "article"
        all_articles.append({
            "id": target_fallback_url,
            "url": target_fallback_url,
            "title": f"知乎博主文章",
            "publish_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content_type": fallback_type
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
                        "title": "【专栏文章】" + (title or f"知乎文章_{article_id}"),
                        "publish_time": publish_time,
                        "content_html": item.get("content", ""),
                        "excerpt": item.get("excerpt", ""),
                        "content_type": "column_article",  # 专栏文章
                        "column_title": column_meta.get("title") if column_meta else None
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
        # 继承列表阶段已识别的内容类型（专栏文章/普通知乎文章/回答）
        content_type = article_meta.get("content_type") or ("answer" if re.search(r"zhihu\.com/question/\d+/answer/\d+", url) else "article")
        column_title = article_meta.get("column_title")

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
                images=images,
                content_type=content_type,
                column_title=column_title
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
            images=[],
            content_type=content_type,
            column_title=column_title
        )
