import re
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

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        if "weibo.com/detail/" in self.target or "m.weibo.cn/status/" in self.target or "/status/" in self.target or re.search(r"weibo\.com/\d+/[a-zA-Z0-9]+", self.target):
            m_single = re.search(r"weibo\.com/(?:detail/|\d+/)?([a-zA-Z0-9]+)", self.target)
            bid = m_single.group(1) if m_single else self.target
            return [{"id": bid, "url": self.target, "title": "微博博文", "publish_time": ""}]

        uid = self._extract_uid()
        container_id, _ = await self._get_container_id(uid)
        articles = []
        page = 1
        
        while True:
            api_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid={container_id}&page={page}"
            try:
                resp = await self.client.get(api_url)
                if resp.status_code == 432:
                    raise RuntimeError(f"微博官方对博主【微博用户_{uid}】开启了登录访问保护 (HTTP 432)。请在浏览器登录 weibo.com 并使用插件同步登录凭证！")
                if resp.status_code != 200:
                    break
                data = resp.json()
                if data.get("ok") == -100:
                    raise RuntimeError(f"微博提示需要登录才能访问博主主页，请使用插件同步微博 Cookie 凭证！")
                if data.get("ok") != 1:
                    break
                    
                cards = data.get("data", {}).get("cards", [])
                if not cards:
                    break
                    
                for card in cards:
                    mblog = card.get("mblog")
                    if not mblog:
                        continue
                        
                    mblog_id = str(mblog.get("id", ""))
                    bid = mblog.get("bid", mblog_id)
                    raw_text = mblog.get("raw_text") or mblog.get("text", "")
                    created_at = mblog.get("created_at", "")
                    retweeted_status = mblog.get("retweeted_status")
                    
                    # 优先提取长文/头条标题，或截取博文内容作为标题
                    page_info = mblog.get("page_info", {})
                    title = page_info.get("page_title")
                    if not title:
                        clean_preview = re.sub(r"<[^>]+>", "", raw_text).strip()
                        # 如果是转发微博，提取更具辨识度的标题（原博作者及原博内容摘要）
                        if retweeted_status and isinstance(retweeted_status, dict):
                            retweet_user = retweeted_status.get("user", {}).get("screen_name", "原博主")
                            retweet_page_title = retweeted_status.get("page_info", {}).get("page_title")
                            retweet_text = retweeted_status.get("raw_text") or retweeted_status.get("text", "")
                            retweet_clean = re.sub(r"<[^>]+>", "", retweet_text).strip()
                            
                            if retweet_page_title:
                                title = f"转发@{retweet_user}: {retweet_page_title}"
                            elif clean_preview and clean_preview != "转发微博":
                                title = f"{clean_preview} // 转发@{retweet_user}: {retweet_clean[:22]}"
                            else:
                                title = f"转发@{retweet_user}: {retweet_clean[:25]}"
                        else:
                            title = (clean_preview[:35] + "...") if len(clean_preview) > 35 else (clean_preview or f"微博_{bid}")
                        
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
                        
                if progress_callback:
                    progress_callback(f"已发现微博内容 {len(articles)} 条 (第 {page} 页)...", len(articles), 0)
                    
                page += 1
                if page > 50:
                    break
            except Exception:
                break
                
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
