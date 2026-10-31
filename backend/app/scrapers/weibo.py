import re
import asyncio
import random
import datetime
from typing import List, Dict, Any, Optional, Callable, Tuple
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content

class WeiboScraper(BaseScraper):
    """微博博主博文/长文章抓取器"""
    
    def __init__(self, target: str, enable_noise_filter: bool = True, max_articles: Optional[int] = None, remove_image_watermark: bool = True, cookie: Optional[str] = None):
        super().__init__(target, enable_noise_filter, max_articles, remove_image_watermark)
        self.cookie = cookie or ""
        if not self.cookie:
            from app.core.weibo_auth import get_weibo_cookie_string
            self.cookie = get_weibo_cookie_string()
            
        if self.cookie:
            self.client.headers.update({
                "Cookie": self.cookie
            })
    
    def _extract_uid(self) -> str:
        match = re.search(r"weibo\.com/u/(\d+)", self.target)
        if match:
            return match.group(1)
        match = re.search(r"m\.weibo\.cn/u/(\d+)", self.target)
        if match:
            return match.group(1)
        match = re.search(r"(\d{8,})", self.target)
        if match:
            return match.group(1)
        return self.target.strip("/ ")

    async def _get_container_id(self, uid: str) -> Tuple[str, Dict[str, Any]]:
        api_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}"
        author_info = {"name": f"微博用户_{uid}", "avatar": "", "bio": "", "platform": "微博"}
        container_id = f"107603{uid}"
        # profile 调用复用带退避重试的翻页通道：瞬时风控若让这次调用失败，
        # 标称数会丢失，前端「标称/实收」缺口提示与截断警告就全部失效
        data = await self._fetch_container_page(api_url, 1)
        if data is not None:
            if data.get("ok") == -100:
                raise RuntimeError(f"微博要求登录后才能访问博主【微博用户_{uid}】主页。{await self._blocked_reason()}")
            if data.get("ok") == 1:
                user_info = data.get("data", {}).get("userInfo", {})
                author_info["name"] = user_info.get("screen_name", author_info["name"])
                author_info["avatar"] = user_info.get("avatar_hd", "")
                author_info["bio"] = user_info.get("description", "")
                # 主页标称微博数：前端检索清单据此显示「标称 X / 实得 Y」缺口
                self.declared_count = user_info.get("statuses_count") or None

                tabs = data.get("data", {}).get("tabsInfo", {}).get("tabs", [])
                for tab in tabs:
                    if tab.get("tab_type") == "weibo":
                        container_id = tab.get("containerid", container_id)
                        break
        return container_id, author_info

    async def get_author_info(self) -> Dict[str, Any]:
        uid = self._extract_uid()
        _, author_info = await self._get_container_id(uid)
        return author_info

    async def _fetch_container_page(self, api_url: str, page: int) -> Optional[Dict[str, Any]]:
        """翻一页微博时间线，返回解析后的 JSON；被风控连续拦死时返回 None。

        微博深翻页会随机打回 HTTP 432 或一段风控 HTML 而不是 JSON。这不代表
        时间线到底，旧代码却把它当成「翻完了」静默收口，导致 6000+ 条的博主
        只返回几十页。这里退避重试，重试仍失败才交给调用方判定。
        """
        for attempt in range(4):
            try:
                resp = await self.client.get(api_url)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        data = None
                    if isinstance(data, dict) and "ok" in data:
                        return data
            except Exception:
                pass
            if attempt < 3:
                await asyncio.sleep(8 * (attempt + 1) + random.uniform(0, 4))
        return None

    def _report_count_gap(self, actual: int, stop_reason: str, reached_bottom: bool):
        """把「主页标称 N 条 / 实收 M 条」的缺口和成因摊开说，避免用户以为静默漏抓。

        就算没拿到标称数（profile 调用被风控降级），只要不是干净翻到底，
        也必须标记 list_truncated 并给出截断警告，绝不允许静默少抓。
        """
        if not reached_bottom:
            self.list_truncated = True
        if not self.declared_count or actual >= self.declared_count:
            if self.list_truncated:
                self.explanation = (
                    f"本次翻页在「{stop_reason}」处提前停止，已收录 <b>{actual}</b> 条，"
                    f"清单可能不含全部历史微博。<br>"
                    f"这通常是微博对当前账号/IP 的翻页风控，稍后重试或重新同步微博 Cookie 可继续向后翻取更早的微博。"
                )
            return
        diff = self.declared_count - actual
        if reached_bottom:
            self.explanation = (
                f"博主主页标称 <b>{self.declared_count}</b> 条微博，实际检索到 <b>{actual}</b> 条（{stop_reason}）。<br>"
                f"差额 <b>{diff}</b> 条通常为博主多年间自行删除、设为仅自己可见，或微博对公开时间线的"
                f"展示深度限制所致（公网已无对应访问页面），并非系统漏抓。"
            )
        else:
            self.explanation = (
                f"博主主页标称 <b>{self.declared_count}</b> 条微博，本次检索到 <b>{actual}</b> 条，"
                f"差额 <b>{diff}</b> 条本次未翻完。<br>"
                f"中断原因：{stop_reason}。这通常是微博对当前账号/IP 的翻页风控，"
                f"稍后重试或重新同步微博 Cookie 可继续向后翻取更早的微博。"
            )

    async def _blocked_reason(self) -> str:
        """时间线被拦时，单独用 api/config 探测登录态，把两种完全不同的成因分开报。

        「本地没同步到有效 SUB」和「登录态好好的但 IP/账号被风控」在时间线接口上
        长得一模一样（都是 432/拒答），只报「风控拦下」会让用户去盲试换网络。
        """
        from app.core.weibo_auth import check_weibo_auth_status
        try:
            st = await check_weibo_auth_status()
        except Exception:
            return "登录态探测失败，请重启客户端后重试。"
        if st.get("authenticated"):
            return (f"本机登录态有效（账号【{st.get('username')}】），微博仍拒绝返回时间线，"
                    "属于当前 IP 或该账号被微博限流。建议换网络（如手机热点）或稍后再试。")
        return (f"本机没有可用的微博登录态：{st.get('message')}"
                "。注意：在自己浏览器里登录微博 ≠ 客户端拿到了凭证，抓取只认「设置」里同步/扫码保存的那份。")

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        if "weibo.com/detail/" in self.target or "m.weibo.cn/status/" in self.target or "/status/" in self.target or re.search(r"weibo\.com/\d+/[a-zA-Z0-9]+", self.target):
            m_single = re.search(r"weibo\.com/(?:detail/|\d+/)?([a-zA-Z0-9]+)", self.target)
            bid = m_single.group(1) if m_single else self.target
            return [{"id": bid, "url": self.target, "title": "微博博文", "publish_time": ""}]

        uid = self._extract_uid()
        container_id, _ = await self._get_container_id(uid)
        articles = []
        seen_ids = set()
        page = 1
        # m 接口每页仅 10~20 条，大博主需数百页；真正的收口交给下面
        # 「无更多卡片 / 整页重复」两条判定，此上限只用于兜底防死循环
        max_page = 700
        stop_reason = f"已达最大翻页数上限 {max_page} 页"
        reached_bottom = False

        while page <= max_page:
            api_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid={container_id}&page={page}"
            try:
                data = await self._fetch_container_page(api_url, page)
                if data is None:
                    if page == 1:
                        raise RuntimeError(f"微博第 1 页即被拦下（接口返回 432 风控页或空响应）。{await self._blocked_reason()}")
                    stop_reason = f"第 {page} 页起被微博风控连续拦截"
                    break
                if data.get("ok") == -100:
                    raise RuntimeError(f"微博要求登录后才能访问该博主主页。{await self._blocked_reason()}")
                if data.get("ok") != 1:
                    # 深翻页的 ok=0 多为瞬时限流，退避重试同一页再决定是否收口
                    recovered = False
                    for attempt in range(2):
                        await asyncio.sleep(5 * (attempt + 1) + random.uniform(0, 3))
                        retry_data = await self._fetch_container_page(api_url, page)
                        if retry_data and retry_data.get("ok") == 1:
                            data = retry_data
                            recovered = True
                            break
                    if not recovered:
                        stop_reason = f"第 {page} 页接口拒答 (ok={data.get('ok')})"
                        break

                cards = data.get("data", {}).get("cards", [])
                if not cards:
                    stop_reason = f"第 {page} 页已无更多卡片，时间线到底"
                    reached_bottom = True
                    break

                new_on_page = 0
                for card in cards:
                    mblog = card.get("mblog")
                    if not mblog:
                        continue

                    # 单卡解析异常只跳过该卡：深翻页时常出现 page_info 为 null 等
                    # 畸形卡片，绝不能让一张坏卡掐断整条时间线
                    try:
                        new_on_page += self._parse_timeline_card(
                            card, mblog, seen_ids, articles, uid
                        )
                    except Exception:
                        continue

                    if self.max_articles and len(articles) >= self.max_articles:
                        return articles
                        
                if new_on_page == 0:
                    # 整页重复 ≠ 时间线到底：真到底的最后一页返回空 cards。
                    # 深翻页被风控降级时微博会反复回填同页内容，按「未翻完」处理
                    stop_reason = f"第 {page} 页整页均为重复卡片，疑似翻页被风控降级"
                    break

                if progress_callback:
                    progress_callback(f"已发现微博内容 {len(articles)} 条 (第 {page} 页)...", len(articles), self.declared_count or 0)

                page += 1
                if page <= max_page:
                    await asyncio.sleep(0.6 + random.uniform(0, 0.9))
            except RuntimeError:
                raise
            except Exception as e:
                stop_reason = f"第 {page} 页请求异常 ({type(e).__name__})"
                break

        # m 端 page 翻页存在深度硬限（实测约 270+ 页即 ok=0，重试也穿不过），
        # 未验证到时间线底部时切换 weibo.com 桌面版 ajax 通道补抓更早的微博
        if not reached_bottom:
            stop_reason, reached_bottom = await self._fill_older_via_desktop(
                uid, seen_ids, articles, stop_reason, progress_callback
            )
            if self.max_articles and len(articles) >= self.max_articles:
                return articles

        self._report_count_gap(len(articles), stop_reason, reached_bottom)
        return articles

    async def _fetch_desktop_page(self, api_url: str, uid: str) -> Optional[Dict[str, Any]]:
        """请求 weibo.com 桌面版 ajax 接口，返回 JSON；连续失败返回 None。"""
        headers = {
            "Referer": f"https://weibo.com/u/{uid}/home",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }
        for attempt in range(3):
            try:
                resp = await self.client.get(api_url, headers=headers)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        data = None
                    if isinstance(data, dict) and "ok" in data:
                        return data
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1) + random.uniform(0, 3))
        return None

    def _normalize_desktop_mblog(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """把 weibo.com ajax 博文对象归一成 m.weibo.cn 卡片结构，复用同一套解析。"""
        pics = []
        for p in (item.get("pic_infos") or {}).values():
            u = (p.get("largest") or p.get("large") or p.get("original") or {}).get("url")
            if u:
                pics.append({"large": {"url": u}})
        text_raw = item.get("text_raw") or item.get("text") or ""
        mb = {
            "id": str(item.get("id", "")),
            "bid": item.get("mblogid") or str(item.get("id", "")),
            "raw_text": text_raw,
            "text": item.get("text") or text_raw,
            "created_at": item.get("created_at", ""),
            "isLongText": item.get("isLongText", False),
            "page_info": item.get("page_info") or {},
            "pics": pics,
            "user": item.get("user") or {},
        }
        ret = item.get("retweeted_status")
        if ret and isinstance(ret, dict):
            mb["retweeted_status"] = self._normalize_desktop_mblog(ret)
        return mb

    async def _fill_older_via_desktop(self, uid: str, seen_ids: set, articles: List[Dict[str, str]],
                                      m_stop_reason: str,
                                      progress_callback: Optional[Callable[[str, int, int], None]]) -> Tuple[str, bool]:
        """用 weibo.com 桌面版 ajax 时间线（/ajax/statuses/mymblog，每页 20 条）
        从新到旧补抓，按 ID 去重后自动接续 m 端未翻到的更早微博。
        返回 (最终中断原因, 是否已验证到底)。"""
        max_page = 600
        dup_run = 0
        for page in range(1, max_page + 1):
            api_url = f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0"
            data = await self._fetch_desktop_page(api_url, uid)
            if data is None:
                return f"m端{m_stop_reason}；切换桌面通道补抓时第 {page} 页被拦截", False
            if data.get("ok") != 1:
                return (f"m端{m_stop_reason}；切换桌面通道补抓至第 {page} 页仍被接口拒答 "
                        f"(ok={data.get('ok')})", False)
            if not self.declared_count:
                self.declared_count = (data.get("data") or {}).get("total") or None
            items = (data.get("data") or {}).get("list") or []
            if not items:
                return f"桌面通道第 {page} 页已无更多内容，补抓到底", True

            new_on_page = 0
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                try:
                    mb = self._normalize_desktop_mblog(item)
                    new_on_page += self._parse_timeline_card({"mblog": mb}, mb, seen_ids, articles, uid)
                except Exception:
                    continue
                if self.max_articles and len(articles) >= self.max_articles:
                    return "已达设定的篇数上限", True

            if new_on_page == 0:
                # 与 m 端已覆盖区间重叠属正常，继续向后翻；连续多页零新增才是回环
                dup_run += 1
                if dup_run >= 50:
                    return f"桌面通道第 {page} 页前后连续多页均为重复内容，疑似翻页回环", False
            else:
                dup_run = 0

            if progress_callback:
                progress_callback(f"已发现微博内容 {len(articles)} 条 (m端+桌面通道补抓第 {page} 页)...",
                                  len(articles), self.declared_count or 0)
            await asyncio.sleep(0.6 + random.uniform(0, 0.9))
        return f"桌面通道已达最大翻页数上限 {max_page} 页", False

    def _parse_timeline_card(self, card: Dict[str, Any], mblog: Dict[str, Any],
                             seen_ids: set, articles: List[Dict[str, str]], uid: str) -> int:
        """解析单张时间线卡片，新收录返回 1、重复返回 0。

        m.weibo.cn 深翻页常出现 page_info/user 为 null 的畸形字段，
        统一用 `or {}` 兜底，避免 AttributeError。
        """
        mblog_id = str(mblog.get("id", ""))
        if mblog_id in seen_ids:
            return 0
        seen_ids.add(mblog_id)
        bid = mblog.get("bid", mblog_id)
        raw_text = mblog.get("raw_text") or mblog.get("text", "")
        created_at = mblog.get("created_at", "")
        retweeted_status = mblog.get("retweeted_status")

        # ---------- 标题提取策略（2026-09-29 与用户对齐）----------
        # 微博分两类：头条文章有真标题；普通博文没有标题。
        # 旧逻辑无条件优先取 page_info.page_title（卡片标题），
        # 会产出“【超话】随笔”“xx的微博视频”这类噪音标题。
        # 新策略：
        #   1) 头条文章卡（type 含 article）→ 用作者起的真标题
        #   2) 普通博文 → 用正文第一行（即“取第一句”，最能代表内容）
        #   3) 正文为空（纯视频/纯图/超话打卡）→ 才退回卡片标题兜底
        page_info = mblog.get("page_info") or {}
        page_title = str(page_info.get("page_title") or "")
        page_type = str(page_info.get("type") or "")

        # <br> 先转成真实换行再剥其余标签，否则多行正文会被拼成一行，
        # “第一句”就变成了“第一段全文”
        clean_preview = re.sub(r"<br\s*/?>", "\n", raw_text)
        clean_preview = re.sub(r"<[^>]+>", "", clean_preview).strip()
        # 取正文第一个非空行作为“第一句”（微博正文常用换行分段）
        first_line = ""
        for _ln in clean_preview.splitlines():
            if _ln.strip():
                first_line = _ln.strip()
                break

        if retweeted_status and isinstance(retweeted_status, dict):
            # 转发微博：保留“原博作者+摘要”的高辨识度格式
            retweet_user = (retweeted_status.get("user") or {}).get("screen_name", "原博主")
            retweet_page_title = (retweeted_status.get("page_info") or {}).get("page_title")
            retweet_text = retweeted_status.get("raw_text") or retweeted_status.get("text", "")
            retweet_clean = re.sub(r"<[^>]+>", "", retweet_text).strip()

            if retweet_page_title:
                title = f"转发@{retweet_user}: {retweet_page_title}"
            elif first_line and first_line != "转发微博":
                title = f"{first_line} // 转发@{retweet_user}: {retweet_clean[:22]}"
            else:
                title = f"转发@{retweet_user}: {retweet_clean[:25]}"
        elif "article" in page_type and page_title:
            # 头条文章：page_title 是作者自己起的标题，优先使用
            title = page_title
        elif first_line:
            # 普通博文：正文第一行（第一句）做标题
            title = first_line
        elif page_title:
            # 无正文的博文（纯视频/纯图/超话签到）：退回卡片标题
            title = page_title
        else:
            title = f"微博_{bid}"

        if len(title) > 40:
            title = title[:38] + "..."

        url = f"https://weibo.com/{uid}/{bid}"

        articles.append({
            "id": bid,
            "url": url,
            "title": title,
            "publish_time": created_at,
            "mblog": mblog
        })
        return 1

    # m 端 extend 接口有请求头指纹校验：桌面 UA 或缺 Referer/X-Requested-With
    # 时不返回 JSON，而是固定弹「安全验证」HTML 页（2026-10-20 本机实测）。
    _M_EXTEND_HEADERS = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Referer": "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    _DESKTOP_LONGTEXT_HEADERS = {
        "Referer": "https://weibo.com/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    # 链接文字只含「全文/展开」及省略号、零宽字符才算截断锚点，
    # 正文里正常链接标题恰好带“全文”二字的不算
    _TRUNCATION_ANCHOR = re.compile(
        r"<a[^>]*>[\s.。…·\u200b]*(?:全文|展开)[\s.。…\u200b]*</a>[\s\u200b\u200c\u200d\ufeff]*$"
    )

    @classmethod
    def _has_truncation_anchor(cls, html: str) -> bool:
        """识别列表接口截断文本尾部的「...全文 / ...展开」锚点（部分长微博 isLongText 标志会漏标）。"""
        return bool(cls._TRUNCATION_ANCHOR.search(html or ""))

    async def _fetch_long_text(self, mblog: Dict[str, Any]) -> Optional[str]:
        """长微博展开全文：先走 m 端 extend（内容最全、含视频卡文案），失败退 weibo.com 桌面版 longtext。
        两条通道都被拦截时返回 None，由调用方做显式降级与告警，绝不静默用截断文本冒充全文。"""
        mblog_id = str(mblog.get("id", ""))
        bid = mblog.get("bid") or mblog_id
        if not mblog_id:
            return None

        # 1) m 端 extend：必须带手机 UA + m 站 Referer + XHR，否则命中安全验证页
        for attempt in range(2):
            try:
                resp = await self.client.get(
                    f"https://m.weibo.cn/statuses/extend?id={mblog_id}",
                    headers=self._M_EXTEND_HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok") == 1:
                        content = (data.get("data") or {}).get("longTextContent")
                        if content:
                            return content
            except Exception:
                pass
            if attempt < 1:
                await asyncio.sleep(2 + random.uniform(0, 2))

        # 2) 桌面版 longtext：与时间线补抓同源的 weibo.com 通道
        for attempt in range(2):
            try:
                resp = await self.client.get(
                    f"https://weibo.com/ajax/statuses/longtext?id={bid}",
                    headers=self._DESKTOP_LONGTEXT_HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = (data.get("data") or {}).get("longTextContent")
                    if content:
                        return content
            except Exception:
                pass
            if attempt < 1:
                await asyncio.sleep(2 + random.uniform(0, 2))
        return None

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        mblog = article_meta.get("mblog", {})
        url = article_meta["url"]
        title = article_meta.get("title", "无标题")
        publish_time = article_meta.get("publish_time", "")
        author = mblog.get("user", {}).get("screen_name", "微博用户")

        # 1. 解析当前博主发表的正文
        raw_html = mblog.get("text", "") or mblog.get("raw_text", "")
        is_long = bool(mblog.get("isLongText", False)) or self._has_truncation_anchor(raw_html)

        truncation_warned = False
        if is_long:
            full_html = await self._fetch_long_text(mblog)
            if full_html:
                raw_html = full_html
            else:
                # 两条展开全文通道均被拦截：剥掉截断锚点 + 显式警告，绝不静默冒充全文
                raw_html = self._TRUNCATION_ANCHOR.sub("", raw_html).strip()
                truncation_warned = True
                
        # 提取当前微博自带图片
        images = []
        for pic in mblog.get("pics", []):
            large = pic.get("large", {}).get("url") or pic.get("url")
            if large:
                images.append(large)

        # 提取当前微博的 page_info (如视频封面、头条文章封面等)
        page_info = mblog.get("page_info")
        if page_info and isinstance(page_info, dict) and not images:
            page_pic = page_info.get("page_pic", {}).get("url") if isinstance(page_info.get("page_pic"), dict) else page_info.get("page_pic")
            if page_pic and page_pic not in images:
                images.append(page_pic)
                
        cleaned_html, md_content, _ = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)

        if truncation_warned:
            warn_md = ("\n\n> ⚠️ 本条为长微博，两条「展开全文」接口均被微博拦截，"
                       "本次仅保存到可见部分的摘要。稍后重新抓取本条可获取全文。")
            md_content += warn_md
            cleaned_html += ('<p style="color:#b45309; background:#fffbeb; border:1px solid #fcd34d; '
                             'border-radius:6px; padding:8px 12px; font-size:13px;">⚠️ 本条为长微博，'
                             '两条「展开全文」接口均被微博拦截，本次仅保存到可见部分的摘要。</p>')

        # 拼装当前微博图片到 Markdown 与 HTML 中
        if images:
            img_md = "\n\n" + "\n\n".join([f"![]({img})" for img in images])
            md_content += img_md
            gallery_html = '<div class="weibo-gallery" style="margin-top: 16px;">' + "".join([f'<p><img src="{img}" class="weibo-photo" style="max-width: 100%; border-radius: 8px; margin: 12px auto; display: block;" /></p>' for img in images]) + '</div>'
            cleaned_html += gallery_html

        # 2. 如果是转发微博，深度提取被转发的原博内容 (retweeted_status)
        retweeted_status = mblog.get("retweeted_status")
        if retweeted_status and isinstance(retweeted_status, dict):
            retweet_user = retweeted_status.get("user", {}).get("screen_name", "原博主")
            retweet_raw_html = retweeted_status.get("text", "") or retweeted_status.get("raw_text", "")
            retweet_is_long = (bool(retweeted_status.get("isLongText", False))
                               or self._has_truncation_anchor(retweet_raw_html))

            retweet_truncation_warned = False
            if retweet_is_long:
                retweet_full = await self._fetch_long_text(retweeted_status)
                if retweet_full:
                    retweet_raw_html = retweet_full
                else:
                    retweet_raw_html = self._TRUNCATION_ANCHOR.sub("", retweet_raw_html).strip()
                    retweet_truncation_warned = True

            retweet_images = []
            for pic in retweeted_status.get("pics", []):
                large = pic.get("large", {}).get("url") or pic.get("url")
                if large:
                    retweet_images.append(large)
                    if large not in images:
                        images.append(large)

            # 提取原博的多媒体/视频卡片 (page_info)
            rt_page_info = retweeted_status.get("page_info")
            media_card_md = ""
            media_card_html = ""
            if rt_page_info and isinstance(rt_page_info, dict):
                rt_card_title = rt_page_info.get("page_title") or rt_page_info.get("content2") or ""
                rt_card_url = rt_page_info.get("page_url") or rt_page_info.get("short_url") or ""
                rt_card_pic = rt_page_info.get("page_pic", {}).get("url") if isinstance(rt_page_info.get("page_pic"), dict) else rt_page_info.get("page_pic")
                if rt_card_pic and rt_card_pic not in retweet_images:
                    retweet_images.append(rt_card_pic)
                    if rt_card_pic not in images:
                        images.append(rt_card_pic)
                
                if rt_card_title and rt_card_title not in retweet_raw_html:
                    if rt_card_url:
                        media_card_md = f"\n\n▶️ [{rt_card_title}]({rt_card_url})"
                        media_card_html = f'<p style="margin-top: 8px;">▶️ <a href="{rt_card_url}" target="_blank" rel="noopener" style="color: #0284c7; text-decoration: none; font-weight: 500;">{rt_card_title}</a></p>'
                    else:
                        media_card_md = f"\n\n▶️ {rt_card_title}"
                        media_card_html = f'<p style="margin-top: 8px; color: #475569;">▶️ {rt_card_title}</p>'

            retweet_cleaned_html, retweet_md, _ = clean_html_content(retweet_raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)

            if retweet_truncation_warned:
                retweet_md += ("\n\n> ⚠️ 被转发的原博为长微博，「展开全文」接口被微博拦截，"
                               "本次仅保存到可见部分的摘要。")
                retweet_cleaned_html += ('<p style="color:#b45309; font-size:13px;">⚠️ 原博长微博展开全文被拦截，'
                                         '仅保存到可见部分摘要。</p>')

            if media_card_md:
                retweet_md += media_card_md
            if media_card_html:
                retweet_cleaned_html += media_card_html

            if retweet_images:
                retweet_img_md = "\n\n" + "\n\n".join([f"![]({img})" for img in retweet_images])
                retweet_md += retweet_img_md
                retweet_gallery_html = '<div class="weibo-gallery" style="margin-top: 12px;">' + "".join([f'<p><img src="{img}" class="weibo-photo" style="max-width: 100%; border-radius: 8px; margin: 10px auto; display: block;" /></p>' for img in retweet_images]) + '</div>'
                retweet_cleaned_html += retweet_gallery_html

            # 拼装 Markdown 引用块
            quoted_md_lines = "\n".join([f"> {line}" for line in retweet_md.split("\n")])
            retweet_block_md = f"\n\n> **@{retweet_user}**：\n>\n{quoted_md_lines}"
            md_content = (md_content.strip() + "\n" + retweet_block_md).strip()

            # 拼装 HTML 引用卡片
            retweet_block_html = f'''
<div class="weibo-retweet-card" style="background:#f8f9fa; border-left: 4px solid #fa8c16; padding: 14px 18px; margin: 16px 0; border-radius: 6px; color: #333;">
  <div style="font-weight: 600; color: #fa8c16; margin-bottom: 8px;">@{retweet_user}</div>
  <div class="retweet-content" style="line-height: 1.6;">{retweet_cleaned_html}</div>
</div>
'''
            cleaned_html = (cleaned_html + "\n" + retweet_block_html).strip()
            
        return ArticleItem(
            id=article_meta.get("id", url),
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="微博",
            content_html=cleaned_html,
            content_markdown=md_content,
            images=images
        )
