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
        try:
            resp = await self.client.get(api_url)
            if resp.status_code == 432:
                raise RuntimeError(f"微博官方对博主【微博用户_{uid}】主页开启了登录访问保护 (HTTP 432)。请在浏览器登录 weibo.com，使用插件一键同步 Cookie 或填入微博 Cookie 凭证！")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") == -100:
                    raise RuntimeError(f"微博提示需要登录才能访问博主【微博用户_{uid}】主页。请使用插件同步微博 Cookie 凭证！")
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
        except RuntimeError:
            raise
        except Exception:
            pass
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
        """把「主页标称 N 条 / 实收 M 条」的缺口和成因摊开说，避免用户以为静默漏抓。"""
        if not self.declared_count or actual >= self.declared_count:
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
                        raise RuntimeError("微博第 1 页即被风控拦下，请重新同步微博 Cookie 或稍后再试。")
                    stop_reason = f"第 {page} 页起被微博风控连续拦截"
                    break
                if data.get("ok") == -100:
                    raise RuntimeError(f"微博提示需要登录才能访问博主主页，请使用插件同步微博 Cookie 凭证！")
                if data.get("ok") != 1:
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

                    mblog_id = str(mblog.get("id", ""))
                    if mblog_id in seen_ids:
                        continue
                    seen_ids.add(mblog_id)
                    new_on_page += 1
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
                    page_info = mblog.get("page_info", {})
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
                        retweet_user = retweeted_status.get("user", {}).get("screen_name", "原博主")
                        retweet_page_title = retweeted_status.get("page_info", {}).get("page_title")
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
                    
                    if self.max_articles and len(articles) >= self.max_articles:
                        return articles
                        
                if new_on_page == 0:
                    stop_reason = f"第 {page} 页整页均为重复卡片，判定时间线到底"
                    reached_bottom = True
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

        self._report_count_gap(len(articles), stop_reason, reached_bottom)
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        mblog = article_meta.get("mblog", {})
        url = article_meta["url"]
        title = article_meta.get("title", "无标题")
        publish_time = article_meta.get("publish_time", "")
        author = mblog.get("user", {}).get("screen_name", "微博用户")
        
        # 1. 解析当前博主发表的正文
        is_long = mblog.get("isLongText", False)
        raw_html = mblog.get("text", "")
        
        if is_long:
            # 抓取展开全文 API
            mblog_id = str(mblog.get("id", ""))
            long_url = f"https://m.weibo.cn/statuses/extend?id={mblog_id}"
            try:
                resp = await self.client.get(long_url)
                if resp.status_code == 200:
                    long_data = resp.json()
                    if long_data.get("ok") == 1:
                        raw_html = long_data.get("data", {}).get("longTextContent", raw_html)
            except Exception:
                pass
                
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
            retweet_raw_html = retweeted_status.get("text", "")
            retweet_is_long = retweeted_status.get("isLongText", False)
            
            if retweet_is_long:
                retweet_id = str(retweeted_status.get("id", ""))
                long_url = f"https://m.weibo.cn/statuses/extend?id={retweet_id}"
                try:
                    resp = await self.client.get(long_url)
                    if resp.status_code == 200:
                        long_data = resp.json()
                        if long_data.get("ok") == 1:
                            retweet_raw_html = long_data.get("data", {}).get("longTextContent", retweet_raw_html)
                except Exception:
                    pass

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
