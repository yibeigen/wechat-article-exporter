# -*- coding: utf-8 -*-
"""百家号抓取器。

- 列表：浏览器逐页顶层导航官方 JSONP feed（主页自身 XHR 发出的 feed 被百度风控回空壳），
        含文章 URL / 标题 / 精确时间，无需逆向私有接口；另保留纯 HTTP feed 兜底。
- 详情：baijiahao.baidu.com/s?id=xxx 先用搜索引擎 UA 穿透（与 CSDN 同一套路），
        被百度 TLS 指纹风控拦成空壳时用常驻浏览器顶层导航兜底，
        解析 window.jsonData 里的结构化正文 sections 与作者/时间信息。
- 支持输入：作者主页 (/u?app_id=xxx)、author.baidu.com 主页、任意百家号文章链接。
"""
import re
import json
import time
import datetime
from typing import List, Dict, Any, Optional, Callable

from app.models import ArticleItem
from app.scrapers.base import BaseScraper
from app.cleaners.html_cleaner import clean_html_content

SPIDER_UA = "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"
GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _parse_jsonp(text: str) -> Optional[Dict]:
    m = re.search(r"^[^(]*\(([\s\S]*)\)\s*;?\s*$", (text or "").strip())
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _parse_json_after(text: str, marker: str) -> Optional[Dict]:
    """从 HTML 里截取 marker 后面第一个完整 JSON 对象（花括号配对，兼容内嵌字符串）"""
    i = text.find(marker)
    if i < 0:
        return None
    start = text.find("{", i)
    if start < 0:
        return None
    depth, in_str, esc, end = 0, False, False, -1
    for p in range(start, len(text)):
        ch = text[p]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = p + 1
                    break
    if end < 0:
        return None
    try:
        return json.loads(text[start:end])
    except Exception:
        return None


def _epoch_to_str(val) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(float(val))).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _collect_feed(obj: Optional[Dict], articles: List[Dict[str, str]], seen: set,
                  max_articles: Optional[int]) -> int:
    """把官方 feed 的 JSONP 对象折进文章清单，返回新增篇数"""
    if not obj:
        return 0
    data = obj.get("data") or {}
    added = 0
    for it in (data.get("list") or []):
        item = it.get("itemData") or {}
        url = (item.get("url") or "").strip()
        if "/s?id=" not in url:
            continue
        url = url.split("?")[0] + "?id=" + url.split("?id=")[-1].split("&")[0]
        if url in seen:
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        seen.add(url)
        art_id = (item.get("shoubai_c_articleid") or url.split("=")[-1]).strip()
        ts = item.get("publish_at") or it.get("dynamic_ctime") or ""
        articles.append({
            "id": art_id,
            "url": url,
            "title": title,
            "publish_time": _epoch_to_str(ts),
            "content_html": "",
            "excerpt": "",
            "like_count": 0,
        })
        added += 1
        if max_articles and len(articles) >= max_articles:
            break
    return added


def _scrape_author_feed_http(app_id: str, max_articles: Optional[int],
                             progress_callback: Optional[Callable[[str, int, int], None]]) -> List[Dict[str, str]]:
    """不依赖浏览器的 feed 通道：作者页外壳下发 Hmery-Time cookie 与 staticMap.version，
    带上二者即可直接请求官方 JSONP 翻页接口。Win7 内置 Chromium 109 跑不动主页现代 JS 时靠它兜底。"""
    import httpx

    articles: List[Dict[str, str]] = []
    seen = set()
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=30,
                      headers={"User-Agent": DESKTOP_UA}) as client:
        try:
            shell = client.get(f"https://baijiahao.baidu.com/u?app_id={app_id}")
        except Exception:
            return []
        m_uk = re.search(r'"uk"\s*:\s*"([A-Za-z0-9_-]{6,})"', shell.text)
        m_ver = re.search(r'"staticMap"\s*:\s*\{\s*"version"\s*:\s*"(\d+)"', shell.text)
        token = client.cookies.get("Hmery-Time") or ""
        if not m_uk or not token:
            return []
        params = {
            "tab": "main", "num": "10", "uk": m_uk.group(1), "source": "pc",
            "type": "newhome", "action": "dynamic", "format": "jsonp",
            "otherext": "h5_" + (m_ver.group(1) if m_ver else ""),
            "Tenger-Mhor": token, "callback": "__jsonp0%d" % int(time.time() * 1000),
        }
        for _ in range(200):
            if max_articles and len(articles) >= max_articles:
                break
            try:
                resp = client.get("https://mbd.baidu.com/webpage", params=params,
                                  headers={"Referer": f"https://baijiahao.baidu.com/u?app_id={app_id}"})
                obj = _parse_jsonp(resp.text)
            except Exception:
                break
            data = (obj or {}).get("data") or {}
            items = data.get("list") or []
            _collect_feed(obj, articles, seen, max_articles)
            if progress_callback:
                try:
                    progress_callback(f"已发现百家号文章 {len(articles)} 篇...", len(articles), 0)
                except Exception:
                    pass
            cursor = items[-1].get("dynamic_ctime_raw") if items else None
            if not data.get("hasMore") or not cursor:
                break
            params["ctime"] = str(cursor)
            time.sleep(0.6)
    return articles


def _scrape_author_feed_sync(app_id: str, max_articles: Optional[int],
                             progress_callback: Optional[Callable[[str, int, int], None]]) -> List[Dict[str, str]]:
    """用浏览器逐页导航官方 JSONP feed 取文章清单。
    主页自身 XHR 发出的 feed 会被百度风控回 foe 空壳（list=null），而顶层导航到 feed URL 能拿全量数据，
    因此不做响应拦截/滚动，直接构造 feed URL 逐页 goto 翻页。
    注意：本函数会创建 Playwright 浏览器，必须从独立线程调用。"""
    import httpx
    from urllib.parse import urlencode
    from playwright.sync_api import sync_playwright

    articles: List[Dict[str, str]] = []
    seen = set()
    # 作者页外壳下发 uk / staticMap.version / Hmery-Time cookie，用于构造 feed 参数
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=30,
                      headers={"User-Agent": DESKTOP_UA}) as client:
        try:
            shell = client.get(f"https://baijiahao.baidu.com/u?app_id={app_id}")
        except Exception:
            return []
        m_uk = re.search(r'"uk"\s*:\s*"([A-Za-z0-9_-]{6,})"', shell.text)
        m_ver = re.search(r'"staticMap"\s*:\s*\{\s*"version"\s*:\s*"(\d+)"', shell.text)
        token = client.cookies.get("Hmery-Time") or ""
        if not m_uk or not token:
            return []
        params = {
            "tab": "main", "num": "10", "uk": m_uk.group(1), "source": "pc",
            "type": "newhome", "action": "dynamic", "format": "jsonp",
            "otherext": "h5_" + (m_ver.group(1) if m_ver else ""),
            "Tenger-Mhor": token, "callback": "__jsonp0%d" % int(time.time() * 1000),
        }
    feed_base = "https://mbd.baidu.com/webpage?" + urlencode(params)

    with sync_playwright() as pw:
        # --disable-quic/--disable-http2：Win7 内置 Chromium 109 在 QEMU NAT 下 QUIC(UDP) 不通会挂起，
        # 关掉后走 HTTP/1.1，feed 导航才能正常返回
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True,
                                         args=['--disable-blink-features=AutomationControlled',
                                               '--disable-quic', '--disable-http2'])
        except Exception:
            browser = pw.chromium.launch(headless=True,
                                         args=['--disable-blink-features=AutomationControlled',
                                               '--disable-quic', '--disable-http2'])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=DESKTOP_UA,
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        # 先打开主页建立会话 cookie，再逐页导航 feed
        try:
            page.goto(f"https://baijiahao.baidu.com/u?app_id={app_id}",
                      wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        cursor = None
        for _ in range(200):
            if max_articles and len(articles) >= max_articles:
                break
            url = feed_base + (("&ctime=" + str(cursor)) if cursor else "")
            try:
                page.goto(url, wait_until="commit", timeout=45000)
                time.sleep(1.0)
                body = page.evaluate("document.body ? document.body.innerText : ''")
            except Exception:
                break
            obj = _parse_jsonp(body)
            _collect_feed(obj, articles, seen, max_articles)
            data = (obj or {}).get("data") or {}
            items = data.get("list") or []
            if progress_callback:
                try:
                    progress_callback(f"已发现百家号文章 {len(articles)} 篇...", len(articles), 0)
                except Exception:
                    pass
            cursor = items[-1].get("dynamic_ctime_raw") if items else None
            if not data.get("hasMore") or not cursor:
                break
            time.sleep(0.5)
        try:
            browser.close()
        except Exception:
            pass
    return articles


class BaijiahaoScraper(BaseScraper):
    """百家号博主全量文章抓取器"""

    def __init__(self, target: str, enable_noise_filter: bool = True,
                 max_articles: Optional[int] = None, remove_image_watermark: bool = True):
        super().__init__(target, enable_noise_filter=enable_noise_filter,
                         max_articles=max_articles, remove_image_watermark=remove_image_watermark)
        self.app_id = self._extract_app_id(self.target)
        self._article_id = self._extract_article_id(self.target)
        self._author_cache: Dict[str, Any] = {}
        self._pw = None
        self._browser = None
        self._browser_ctx = None

    @staticmethod
    def _extract_app_id(target: str) -> Optional[str]:
        m = re.search(r"app_id=(\d{6,})", target, re.I)
        return m.group(1) if m else None

    @staticmethod
    def _extract_article_id(target: str) -> Optional[str]:
        m = re.search(r"baijiahao\.baidu\.com/s\?id=(\d{10,})", target, re.I)
        if not m:
            m = re.search(r"/s\?id=(\d{10,})", target)
        return m.group(1) if m else None

    async def _fetch_article_page(self, article_id: str) -> str:
        """搜索引擎 UA 穿透百度安全验证；百度短时风控会间歇拦截，多 UA 重试兜底。
        Win7 实测 httpx 被百度按 TLS 指纹拦成空壳（三种 UA 均拿不到 window.jsonData），
        此时用真实浏览器顶层导航文章页兜底。"""
        import asyncio
        last_html = ""
        for attempt, ua in enumerate((SPIDER_UA, GOOGLEBOT_UA, DESKTOP_UA)):
            try:
                resp = await self.client.get(
                    f"https://baijiahao.baidu.com/s?id={article_id}",
                    headers={"User-Agent": ua, "Referer": "https://www.baidu.com/"},
                )
                last_html = resp.text
                if "window.jsonData" in last_html:
                    return last_html
            except Exception:
                pass
            await asyncio.sleep(1.2 * (attempt + 1))
        try:
            html = await self._fetch_article_html_browser(article_id)
            if "window.jsonData" in html:
                return html
            if html:
                last_html = html
        except Exception:
            pass
        return last_html

    async def _ensure_browser(self):
        """懒启动一个常驻浏览器供正文导航复用（async API 跑在事件循环上，无 sync 的线程亲和问题）。
        Win7 内置 Chromium 109 关 QUIC/HTTP2：QEMU NAT 下 QUIC(UDP) 不通会挂起。"""
        if self._browser_ctx is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        args = ['--disable-blink-features=AutomationControlled', '--disable-quic', '--disable-http2']
        try:
            self._browser = await self._pw.chromium.launch(channel="msedge", headless=True, args=args)
        except Exception:
            self._browser = await self._pw.chromium.launch(headless=True, args=args)
        self._browser_ctx = await self._browser.new_context(
            viewport={"width": 1440, "height": 900}, user_agent=DESKTOP_UA)
        await self._browser_ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    async def _fetch_article_html_browser(self, article_id: str) -> str:
        await self._ensure_browser()
        page = await self._browser_ctx.new_page()
        try:
            await page.goto(f"https://baijiahao.baidu.com/s?id={article_id}",
                            wait_until="commit", timeout=45000)
            try:
                await page.wait_for_function("window.jsonData !== undefined", timeout=20000)
            except Exception:
                await page.wait_for_timeout(1500)
            return await page.content()
        except Exception:
            return ""
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _close_browser(self):
        for obj in (self._browser_ctx, self._browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._browser_ctx = self._browser = self._pw = None

    async def close(self):
        await self._close_browser()
        await super().close()

    def _article_json(self, html: str) -> Optional[Dict]:
        return _parse_json_after(html, "window.jsonData")

    async def get_author_info(self) -> Dict[str, Any]:
        if self._author_cache:
            return self._author_cache
        # 单篇文章链接：从文章页 jsonData 里拿到作者 app_id / uk / 昵称
        if not self.app_id and self._article_id:
            html = await self._fetch_article_page(self._article_id)
            d = self._article_json(html) or {}
            bs = d.get("bsData") or {}
            try:
                info = bs["superlanding"][0]["itemData"]["infoBaiJiaHao"]
            except Exception:
                info = {}
            self.app_id = str(info.get("link") or info.get("third_id") or "") or None
            self._author_cache = {
                "name": info.get("name") or "百家号作者",
                "avatar": (info.get("thumbnail") or {}).get("src") if isinstance(info.get("thumbnail"), dict) else "",
                "bio": info.get("sign") or "",
                "target_id": self.app_id or self._article_id or "",
            }
            return self._author_cache
        if not self.app_id:
            raise ValueError("无法识别百家号作者，请粘贴作者主页或文章链接")
        # 作者主页外壳 HTML 的 <title> 就是作者昵称（百度风控间歇拦截，多 UA 重试）
        name = ""
        for ua in (DESKTOP_UA, SPIDER_UA):
            try:
                resp = await self.client.get(
                    f"https://baijiahao.baidu.com/u?app_id={self.app_id}",
                    headers={"User-Agent": ua},
                )
                tm = re.search(r"<title>(.*?)</title>", resp.text, re.S)
                if tm and tm.group(1).strip():
                    name = tm.group(1).strip()
                    break
            except Exception:
                continue
        self._author_cache = {"name": name or "百家号作者", "avatar": "", "bio": "", "target_id": self.app_id}
        return self._author_cache

    async def get_article_list(
        self, progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> List[Dict[str, str]]:
        if not self.app_id:
            await self.get_author_info()
        if not self.app_id:
            raise ValueError("无法识别百家号作者")
        # 直接访问单篇文章且解析不出作者时，至少返回该篇，不让用户一无所获
        import asyncio
        try:
            articles = await asyncio.to_thread(
                _scrape_author_feed_sync, self.app_id, self.max_articles, progress_callback)
        except Exception:
            articles = []
        # 浏览器通道空手（Win7 内置 Chromium 109 跑不动主页现代 JS）时走纯 HTTP feed 兜底
        if not articles:
            articles = await asyncio.to_thread(
                _scrape_author_feed_http, self.app_id, self.max_articles, progress_callback)
        # 作者页外壳被风控拿不到昵称时，用首篇文章 jsonData 里的作者名兜底
        if articles and self._author_cache.get("name") in (None, "", "百家号作者"):
            try:
                first_id = re.search(r"id=(\d{10,})", articles[0].get("url") or "")
                if first_id:
                    d = self._article_json(await self._fetch_article_page(first_id.group(1))) or {}
                    info = d.get("bsData", {}).get("superlanding", [{}])[0].get("itemData", {}).get("infoBaiJiaHao") or {}
                    if info.get("name"):
                        self._author_cache["name"] = info["name"]
            except Exception:
                pass
        if not articles and self._article_id:
            html = await self._fetch_article_page(self._article_id)
            d = self._article_json(html) or {}
            bs = d.get("bsData") or {}
            title = bs.get("title") or ""
            articles = [{
                "id": self._article_id,
                "url": f"https://baijiahao.baidu.com/s?id={self._article_id}",
                "title": title or "百家号文章",
                "publish_time": bs.get("datetime") or "",
                "content_html": "",
                "excerpt": "",
                "like_count": 0,
            }]
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta.get("url") or ""
        article_id = (re.search(r"id=(\d{10,})", url) or [None, ""])[1] if url else ""
        if not article_id:
            article_id = self._article_id or ""
        title = article_meta.get("title") or "百家号文章"
        author_info = await self.get_author_info()
        author = author_info.get("name") or "百家号作者"
        publish_time = article_meta.get("publish_time") or ""

        if not article_id:
            return ArticleItem(id=url, title=title, author=author, url=url, platform="百家号",
                               content_markdown="无法识别文章ID", is_failed=True, error_reason="无法识别文章ID")

        html = await self._fetch_article_page(article_id)
        d = self._article_json(html)
        if not d:
            reason = "页面被百度安全验证拦截或结构变更"
            return ArticleItem(id=url, title=title, author=author, publish_time=publish_time, url=url,
                               platform="百家号", content_markdown=f"抓取失败: {reason}",
                               is_failed=True, error_reason=reason,
                               summary=article_meta.get("excerpt", ""))

        bs = d.get("bsData") or {}
        try:
            item_data = bs["superlanding"][0]["itemData"]
        except Exception:
            item_data = {}

        title = (bs.get("title") or item_data.get("header") or title).strip()
        publish_time = (bs.get("datetime") or publish_time).strip()
        info = item_data.get("infoBaiJiaHao") or {}
        author = (info.get("name") or author).strip()

        parts: List[str] = []
        for sec in item_data.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            sec_type = (sec.get("type") or "").lower()
            data_html = (sec.get("data_html") or "").strip()
            if data_html:
                parts.append(data_html)
            elif sec_type in ("image", "img"):
                # 百家号图片节用 link 字段（不是 url/src）
                src = (sec.get("link") or sec.get("url") or sec.get("src") or "").strip()
                if src:
                    parts.append(f'<p><img src="{src}" /></p>')
                caption = (sec.get("caption") or "").strip()
                if caption:
                    parts.append(f"<p>{caption}</p>")
            elif sec.get("content"):
                parts.append(f"<p>{sec['content']}</p>")
        raw_html = "\n".join(parts)
        if not raw_html.strip():
            reason = "未解析到正文内容"
            return ArticleItem(id=url, title=title, author=author, publish_time=publish_time, url=url,
                               platform="百家号", content_markdown=f"抓取失败: {reason}",
                               is_failed=True, error_reason=reason,
                               summary=article_meta.get("excerpt", ""))

        cleaned_html, md_content, images = clean_html_content(
            raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)

        return ArticleItem(
            id=url,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="百家号",
            summary=article_meta.get("excerpt", ""),
            content_html=cleaned_html,
            content_markdown=md_content,
            images=images,
            like_count=item_data.get("like") if isinstance(item_data.get("like"), int) else 0,
        )