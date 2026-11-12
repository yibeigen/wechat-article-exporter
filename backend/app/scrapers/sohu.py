# -*- coding: utf-8 -*-
"""搜狐号抓取器。

- 列表：m.sohu.com/media/{mkey} 移动端作者页（SSR 首页 20 篇）
        + odin blockdata 翻页接口（asId = v1+时间戳+HMAC_SHA1("t"+时间戳, 固定密钥)）
- 详情：www.sohu.com/a/{id}_{mkey}，正文 #mp-editor，发布时间 #news-time[data-val]（毫秒时间戳）
- 支持输入：mp.sohu.com/profile?xpt=... / m.sohu.com/media/{mkey} / www.sohu.com/a/{id}_{mkey}
"""
import re
import json
import time
import uuid
import hmac
import hashlib
import datetime
from typing import List, Dict, Any, Optional, Callable

from app.models import ArticleItem
from app.scrapers.base import BaseScraper
from app.cleaners.html_cleaner import clean_html_content

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# 作者页翻页鉴权密钥（来自官方前端 createAuthToken，公开可见）
SOHU_AUTH_KEY = "439642a904ef43d092d45509cdc4391c"
BLOCKDATA_URL = "https://odin.sohu.com/odin/api/blockdata"


def _auth_token() -> str:
    ts = str(int(time.time() * 1000))
    sig = hmac.new(SOHU_AUTH_KEY.encode(), ("t" + ts).encode(), hashlib.sha1).hexdigest()
    return "v1" + ts + sig


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://www.sohu.com" + u
    return u


def _ms_to_str(val) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(float(val)) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


class SohuScraper(BaseScraper):
    """搜狐号博主全量文章抓取器"""

    def __init__(self, target: str, enable_noise_filter: bool = True,
                 max_articles: Optional[int] = None, remove_image_watermark: bool = True):
        super().__init__(target, enable_noise_filter=enable_noise_filter,
                         max_articles=max_articles, remove_image_watermark=remove_image_watermark)
        self.mkey = self._extract_mkey(self.target)
        self.xpt = self._extract_xpt(self.target)
        self._author_cache: Dict[str, Any] = {}
        self._page_meta: Optional[Dict[str, Any]] = None  # 从移动作者页解析出的翻页参数
        self._first_page_items: Optional[List[Dict]] = None

    # ---------- 目标解析 ----------
    @staticmethod
    def _extract_mkey(target: str) -> Optional[str]:
        m = re.search(r"sohu\.com/a/\d+_(\d+)", target)
        if m:
            return m.group(1)
        m = re.search(r"m\.sohu\.com/media/(\d+)", target)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _extract_xpt(target: str) -> Optional[str]:
        m = re.search(r"[?&]xpt=([A-Za-z0-9=_\-]+)", target)
        return m.group(1) if m else None

    async def _fetch_text(self, url: str, mobile: bool = False) -> str:
        resp = await self.client.get(url, headers={"User-Agent": MOBILE_UA if mobile else DESKTOP_UA})
        return resp.text

    async def _resolve_mkey(self) -> str:
        """仅有 xpt 时，从作者页 SSR 数据里解析 mkey（作者唯一数字ID）"""
        if self.mkey:
            return self.mkey
        if not self.xpt:
            raise ValueError("无法识别搜狐号目标，请粘贴作者主页或文章链接")
        html = await self._fetch_text(f"https://mp.sohu.com/profile?xpt={self.xpt}", mobile=False)
        m = re.search(r'"mkeyConst_mkey"\s*:\s*"?(\d+)', html) \
            or re.search(r'"mkey"\s*:\s*\{\s*"mkey"\s*:\s*"(\d+)"', html)
        if not m:
            raise ValueError("无法从搜狐作者页解析出作者ID，请改用 m.sohu.com/media/{ID} 链接")
        self.mkey = m.group(1)
        return self.mkey

    # ---------- 列表 ----------
    async def _load_media_page(self) -> List[Dict]:
        """解析移动作者页：作者信息 + 首页 20 篇 + 翻页所需参数（全部从 SSR 数据动态提取）"""
        if self._page_meta is not None:
            return self._first_page_items or []
        mkey = await self._resolve_mkey()
        html = await self._fetch_text(f"https://m.sohu.com/media/{mkey}", mobile=True)

        block = re.search(r"window\.blockRenderData\s*=\s*(\{[\s\S]*?\})\s*;?\s*</script>", html)
        if not block:
            raise ValueError("搜狐作者页数据解析失败（网页结构可能已变更）")
        render = json.loads(block.group(1))

        feed_comp = None
        for key, val in render.items():
            if key.startswith("FeedSlideloadAuthor") and isinstance(val, dict):
                feed_comp = val
                break
        if not feed_comp:
            raise ValueError("搜狐作者页未找到文章列表组件")

        param = feed_comp.get("param") or {}
        data2 = param.get("data2") or {}
        req_param = data2.get("reqParam") or {}
        content = req_param.get("content") or {}

        # 页面级 contentData（第二个赋值才是完整对象）：翻页请求的 mainContent
        content_data = {}
        for m in re.finditer(r"window\.contentData\s*=\s*(\{[\s\S]*?\})\s*</script>", html):
            try:
                obj = json.loads(m.group(1))
                if len(obj) > len(content_data):
                    content_data = obj
            except Exception:
                pass

        # 作者名：页面标题「XXX的个人主页」
        title_m = re.search(r"<title>(.*?)</title>", html, re.S)
        page_title = title_m.group(1).strip() if title_m else ""
        author_name = re.sub(r"的个人主页.*$", "", page_title).strip() or mkey
        account = content_data.get("account") or {}

        self._author_cache = {
            "name": author_name,
            "avatar": _norm_url(account.get("avatar") or ""),
            "bio": "",
            "target_id": mkey,
        }
        self._page_meta = {
            "tpl_key": req_param.get("tplCompKey") or "",
            "product_id": str(content.get("productId") or "325"),
            "spm": content.get("spm") or "",
            "pro": content.get("pro") or "0,1",
            "main_content": {
                "productType": str(content_data.get("businessType") or "13"),
                "productId": str(content_data.get("id") or "324"),
                "secureScore": str(content_data.get("contentSecureScore") or content_data.get("secureScore") or "100"),
                "categoryId": str(content_data.get("categoryId") or "47"),
                "adTags": content_data.get("adTags"),
                "authorId": account.get("id"),
            },
        }
        self._first_page_items = data2.get("list") or []
        return self._first_page_items

    async def _fetch_list_page(self, page: int) -> List[Dict]:
        meta = self._page_meta or {}
        ts = int(time.time() * 1000)
        body = {
            "pvId": f"{ts}_sohu",
            "pageId": f"{ts}_{ts}odi_{uuid.uuid4().hex[:3]}",
            "mainContent": meta.get("main_content") or {},
            "resourceList": [{
                "tplCompKey": meta.get("tpl_key") or "",
                "isServerRender": True,
                "isSingleAd": False,
                "configSource": "mp",
                "content": {
                    "productId": meta.get("product_id") or "325",
                    "productType": "13",
                    "size": 20,
                    "pro": meta.get("pro") or "0,1",
                    "feedType": "XTOPIC_SYNTHETICAL",
                    "view": "operateFeedMode",
                    "innerTag": "work",
                    "spm": meta.get("spm") or "",
                    "page": page,
                    "requestId": f"{ts}{uuid.uuid4().hex[:7]}",
                },
                "adInfo": {},
                "context": {"mkey": self.mkey},
            }],
            "asId": _auth_token(),
        }
        resp = await self.client.post(
            BLOCKDATA_URL, json=body,
            headers={"User-Agent": MOBILE_UA, "Referer": "https://m.sohu.com/", "Origin": "https://m.sohu.com"},
        )
        try:
            data = (resp.json().get("data") or {})
        except Exception:
            return []
        payload = data.get(meta.get("tpl_key") or "") or {}
        return payload.get("list") or []

    @staticmethod
    def _list_item_time(item: Dict) -> str:
        """列表项只带相对时间（如「7小时前」「11月8日」），详情阶段会用精确时间覆盖"""
        for extra in item.get("extraInfoList") or []:
            if extra.get("image") == "time" and extra.get("text"):
                return str(extra["text"]).strip()
        return ""

    async def get_author_info(self) -> Dict[str, Any]:
        if self._author_cache:
            return self._author_cache
        await self._load_media_page()
        return self._author_cache

    async def get_article_list(
        self, progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> List[Dict[str, str]]:
        items = list(await self._load_media_page())
        articles: List[Dict[str, str]] = []
        seen = set()

        def _append(raw_items: List[Dict]) -> int:
            added = 0
            for it in raw_items:
                art_id = str(it.get("id") or "")
                # 统一用桌面站文章地址：移动站详情页没有 #mp-editor 正文容器
                if art_id.isdigit():
                    url = f"https://www.sohu.com/a/{art_id}_{self.mkey}"
                else:
                    url = _norm_url(it.get("url") or "").split("?")[0]
                if not url or url in seen:
                    continue
                seen.add(url)
                articles.append({
                    "id": art_id or url,
                    "url": url,
                    "title": (it.get("title") or "").strip() or f"搜狐文章_{art_id}",
                    "publish_time": self._list_item_time(it),
                    "content_html": "",
                    "excerpt": (it.get("brief") or "").strip(),
                    "like_count": 0,
                })
                added += 1
                if self.max_articles and len(articles) >= self.max_articles:
                    break
            return added

        _append(items)
        if progress_callback:
            progress_callback(f"已发现搜狐号文章 {len(articles)} 篇...", len(articles), 0)

        page = 2
        empty_rounds = 0
        while not (self.max_articles and len(articles) >= self.max_articles):
            page_items = await self._fetch_list_page(page)
            if not page_items:
                empty_rounds += 1
                if empty_rounds >= 2:
                    break
                page += 1
                continue
            empty_rounds = 0
            added = _append(page_items)
            if progress_callback:
                progress_callback(f"已发现搜狐号文章 {len(articles)} 篇...", len(articles), 0)
            # 官方前端规则：本页不足 10 条视为到底
            if len(page_items) < 10 or added == 0:
                break
            page += 1
            if page > 500:  # 防御性上限
                break
            await __import__("asyncio").sleep(0.4)

        return articles

    # ---------- 详情 ----------
    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = _norm_url(article_meta.get("url") or "")
        # 移动站文章页没有 #mp-editor 正文容器，统一改写为桌面站地址
        if "://m.sohu.com/a/" in url:
            url = url.replace("://m.sohu.com/a/", "://www.sohu.com/a/")
        url = url.split("?")[0]
        title = article_meta.get("title") or "搜狐文章"
        author_info = await self.get_author_info()
        author = author_info.get("name") or "搜狐号作者"
        publish_time = article_meta.get("publish_time") or ""

        try:
            html = await self._fetch_text(url, mobile=False)
        except Exception as e:
            return ArticleItem(
                id=url, title=title, author=author, url=url, platform="搜狐号",
                content_markdown=f"抓取失败: {e}", is_failed=True, error_reason=str(e),
            )

        h1 = re.search(r'<h1[^>]*class="[^"]*screen-reader-only[^"]*"[^>]*>([\s\S]*?)</h1>', html)
        if h1:
            title = re.sub(r"<[^>]+>", "", h1.group(1)).strip() or title
        else:
            tm = re.search(r"<title>(.*?)</title>", html, re.S)
            if tm:
                title = re.sub(r"_[^_]{0,30}$", "", tm.group(1).strip()) or title

        t_el = re.search(r'id="news-time"[^>]*data-val="(\d+)"[^>]*>([^<]*)<', html)
        if t_el:
            publish_time = _ms_to_str(t_el.group(1)) or t_el.group(2).strip() or publish_time

        author_el = re.search(r'mp\.sohu\.com/profile\?xpt=[A-Za-z0-9=_\-]+"[^>]*aria-label="([^"]+)"', html)
        if author_el:
            author = author_el.group(1).strip() or author

        body_m = re.search(r'id="mp-editor"[^>]*>([\s\S]*?)</article>', html)
        raw_html = body_m.group(1) if body_m else ""
        if not raw_html:
            # 搜狐号里混着纯视频简报页（只有播放器没有正文），不是抓取失败，说清楚免得用户反复重试
            fail_msg = ("该条是搜狐视频简报，只有视频没有正文，无法导出为文档"
                        if re.search(r'id="mainVideoContent"|id="videoPlayer"', html)
                        else "内容提取失败（页面无正文容器，可能是图集/视频页或网页结构已变更）")
            return ArticleItem(
                id=url, title=title, author=author, publish_time=publish_time, url=url,
                platform="搜狐号", content_markdown=fail_msg, is_failed=True, error_reason=fail_msg,
                summary=article_meta.get("excerpt", ""),
            )

        cleaned_html, md_content, images = clean_html_content(
            raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)

        return ArticleItem(
            id=url,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="搜狐号",
            summary=article_meta.get("excerpt", ""),
            content_html=cleaned_html,
            content_markdown=md_content,
            images=images,
        )