import re
import json
import asyncio
import datetime
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup

from app.config import DEFAULT_HEADERS
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content


class JianshuScraper(BaseScraper):
    """简书（jianshu.com）专栏与博文抓取器

    支持模式：
    1. 博主个人主页：https://www.jianshu.com/u/{slug} 或 /users/{slug}/timeline 或纯 slug
    2. 专题与文集：https://www.jianshu.com/c/{slug} 或 /nb/{slug}
    3. 单篇文章：https://www.jianshu.com/p/{slug}
    """

    MOBILE_HEADERS = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.jianshu.com/",
    }

    DESKTOP_AJAX_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.jianshu.com/",
    }

    def __init__(
        self,
        target: str,
        enable_noise_filter: bool = True,
        max_articles: Optional[int] = None,
        remove_image_watermark: bool = True
    ):
        super().__init__(
            target,
            enable_noise_filter=enable_noise_filter,
            max_articles=max_articles,
            remove_image_watermark=remove_image_watermark
        )
        self.target_type = "user"  # "user", "collection", "single"
        self.target_slug = ""
        self.author_name = "简书作者"
        self.author_avatar = ""
        self.author_bio = ""
        self._parse_target()

    def _parse_target(self):
        """分析并规范化输入目标类型与 Slug"""
        t = self.target.strip()

        # 1. 单篇文章链接: https://www.jianshu.com/p/89140be1185c
        p_match = re.search(r"jianshu\.com/p/([a-f0-9]+)", t, re.IGNORECASE)
        if not p_match:
            p_match = re.search(r"^/p/([a-f0-9]+)", t, re.IGNORECASE)
        if p_match:
            self.target_type = "single"
            self.target_slug = p_match.group(1)
            return

        # 2. 专题链接: https://www.jianshu.com/c/1694de00d239 或 /c/V2CqjW
        c_match = re.search(r"jianshu\.com/c/([a-zA-Z0-9]+)", t)
        if not c_match:
            c_match = re.search(r"^/c/([a-zA-Z0-9]+)", t)
        if c_match:
            self.target_type = "collection"
            self.target_slug = c_match.group(1)
            return

        # 3. 文集链接: https://www.jianshu.com/nb/53410766
        nb_match = re.search(r"jianshu\.com/nb/(\d+)", t)
        if nb_match:
            self.target_type = "collection"
            self.target_slug = nb_match.group(1)
            return

        # 4. 用户主页: https://www.jianshu.com/u/5bb0bdbbee02 或 /users/5bb0bdbbee02/timeline
        u_match = re.search(r"jianshu\.com/u/([a-f0-9]+)", t, re.IGNORECASE)
        if not u_match:
            u_match = re.search(r"jianshu\.com/users/([a-f0-9]+)", t, re.IGNORECASE)
        if not u_match:
            u_match = re.search(r"^([a-f0-9]{12})$", t, re.IGNORECASE)
        if u_match:
            self.target_type = "user"
            self.target_slug = u_match.group(1)
            return

        # 默认回退：按用户 Slug
        self.target_type = "user"
        self.target_slug = t.split("?")[0].split("/")[-1]

    async def get_author_info(self) -> Dict[str, Any]:
        """获取博主基本信息或专题信息"""
        if self.target_type == "single":
            try:
                article_url = f"https://www.jianshu.com/p/{self.target_slug}"
                resp = await self.client.get(article_url, headers=self.MOBILE_HEADERS)
                if resp.status_code == 200:
                    info = self._extract_state_info(resp.text)
                    if info:
                        self.author_name = info.get("user", {}).get("nickname") or "简书作者"
                        self.author_avatar = info.get("user", {}).get("avatar") or ""
                        self.author_bio = info.get("description") or ""
                        return {
                            "name": self.author_name,
                            "avatar": self.author_avatar,
                            "bio": self.author_bio
                        }
            except Exception:
                pass
            return {"name": self.author_name, "avatar": self.author_avatar, "bio": self.author_bio}

        if self.target_type == "collection":
            # 专题或文集
            url = f"https://www.jianshu.com/c/{self.target_slug}"
            try:
                resp = await self.client.get(url, headers=self.DESKTOP_AJAX_HEADERS)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    title_elem = soup.select_one(".main-top .title .name") or soup.find("h1") or soup.find("title")
                    avatar_elem = soup.select_one(".avatar-collection img") or soup.find("img")
                    desc_elem = soup.select_one(".main-top .info, .summary, .description")

                    if title_elem:
                        self.author_name = title_elem.text.strip().replace(" - 专题 - 简书", "").replace(" - 简书", "")
                    if avatar_elem and avatar_elem.get("src"):
                        src = avatar_elem["src"]
                        self.author_avatar = "https:" + src if src.startswith("//") else src
                    if desc_elem:
                        self.author_bio = desc_elem.text.strip().replace("\n", " ")

                    self.category_name = self.author_name
                    return {
                        "name": self.author_name,
                        "avatar": self.author_avatar,
                        "bio": self.author_bio
                    }
            except Exception:
                pass
            return {"name": "简书专题", "avatar": "", "bio": ""}

        # 用户主页：优先通过移动端 SSR 提取最详尽的结构化作者档案
        user_url = f"https://www.jianshu.com/u/{self.target_slug}"
        try:
            resp = await self.client.get(user_url, headers=self.MOBILE_HEADERS)
            if resp.status_code == 200:
                user_state = self._extract_user_state_info(resp.text)
                if user_state:
                    self.author_name = user_state.get("nickname") or self.author_name
                    self.author_avatar = user_state.get("avatar") or self.author_avatar
                    self.author_bio = user_state.get("intro") or ""
                    if user_state.get("total_wordage"):
                        self.explanation = f"总字数: {user_state.get('total_wordage')} · 获赞: {user_state.get('total_likes_count', 0)}"
                    return {
                        "name": self.author_name,
                        "avatar": self.author_avatar,
                        "bio": self.author_bio
                    }
        except Exception:
            pass

        # 备用方案：通过 /users/{slug}/timeline 提取
        timeline_url = f"https://www.jianshu.com/users/{self.target_slug}/timeline"
        try:
            resp = await self.client.get(timeline_url, headers=self.DESKTOP_AJAX_HEADERS)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                name_elem = soup.select_one(".main-top .name, a.name, h1")
                avatar_elem = soup.select_one(".main-top .avatar img, a.avatar img")
                bio_elem = soup.select_one(".main-top .description .js-intro, .main-top .info")

                if name_elem:
                    self.author_name = name_elem.text.strip()
                if avatar_elem and avatar_elem.get("src"):
                    src = avatar_elem["src"]
                    self.author_avatar = "https:" + src if src.startswith("//") else src
                if bio_elem:
                    self.author_bio = bio_elem.text.strip().replace("\n", " ")

                return {
                    "name": self.author_name,
                    "avatar": self.author_avatar,
                    "bio": self.author_bio
                }
        except Exception:
            pass

        return {"name": self.author_name, "avatar": self.author_avatar, "bio": self.author_bio}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        """获取文章元数据列表"""
        articles: List[Dict[str, str]] = []
        seen_ids = set()

        if self.target_type == "single":
            single_url = f"https://www.jianshu.com/p/{self.target_slug}"
            return [{
                "id": self.target_slug,
                "url": single_url,
                "title": "简书文章",
                "publish_time": ""
            }]

        if self.target_type == "collection":
            # 专题/文集分页逻辑：?order_by=added_at&page=N
            page = 1
            max_page = 500
            empty_streak = 0
            base_url = f"https://www.jianshu.com/c/{self.target_slug}"

            while page <= max_page:
                page_url = f"{base_url}?order_by=added_at&page={page}"
                if progress_callback:
                    progress_callback(f"正在扫描专题第 {page} 页博文...", len(articles), len(articles))

                try:
                    resp = await self.client.get(page_url, headers=self.DESKTOP_AJAX_HEADERS)
                    if resp.status_code != 200 or (page > 1 and "/page=1" in str(resp.url)):
                        break

                    soup = BeautifulSoup(resp.text, "lxml")
                    items = soup.select("ul.note-list > li") or soup.select("li.have-img, li")
                    new_count = 0

                    for it in items:
                        t = it.select_one("a.title")
                        if not t or not t.get("href"):
                            continue

                        m = re.search(r"/p/([a-f0-9]+)", t["href"])
                        if not m:
                            continue

                        pid = m.group(1)
                        if pid in seen_ids:
                            continue

                        seen_ids.add(pid)
                        new_count += 1
                        time_span = it.select_one("span.time, time")
                        publish_time = time_span.get("data-shared-at") or time_span.text if time_span else ""

                        articles.append({
                            "id": pid,
                            "url": f"https://www.jianshu.com/p/{pid}",
                            "title": t.text.strip(),
                            "publish_time": self._normalize_datetime(publish_time)
                        })

                        if self.max_articles and len(articles) >= self.max_articles:
                            break

                    if self.max_articles and len(articles) >= self.max_articles:
                        break

                    if new_count == 0:
                        empty_streak += 1
                        if empty_streak >= 1:
                            break
                    else:
                        empty_streak = 0

                    page += 1
                    await asyncio.sleep(0.3)
                except Exception:
                    break

            if progress_callback:
                progress_callback(f"简书专题扫描完成，共获取 {len(articles)} 篇文章", len(articles), len(articles))
            return articles

        # 用户主页博文检索：使用高容灾的 timeline + max_id 瀑布流机制
        max_id: Optional[int] = None
        step = 0
        max_steps = 1000

        while step < max_steps:
            step += 1
            timeline_url = f"https://www.jianshu.com/users/{self.target_slug}/timeline"
            if max_id:
                timeline_url += f"?max_id={max_id}"

            if progress_callback:
                progress_callback(f"正在扫描博主时间线第 {step} 批文章...", len(articles), self.declared_count or len(articles))

            try:
                resp = await self.client.get(timeline_url, headers=self.DESKTOP_AJAX_HEADERS)
                if resp.status_code != 200:
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                items = soup.find_all("li")
                new_articles = 0
                min_feed_id: Optional[int] = None

                for it in items:
                    # 提取 feed-xxx 唯一流 ID 供下一次翻页使用
                    feed_attr = it.get("id", "")
                    if feed_attr and feed_attr.startswith("feed-"):
                        try:
                            fid = int(feed_attr.split("-")[1])
                            if min_feed_id is None or fid < min_feed_id:
                                min_feed_id = fid
                        except Exception:
                            pass

                    t = it.find("a", class_="title")
                    if not t or not t.get("href"):
                        continue

                    m = re.search(r"/p/([a-f0-9]+)", t["href"])
                    if not m:
                        continue

                    pid = m.group(1)
                    if pid in seen_ids:
                        continue

                    seen_ids.add(pid)
                    new_articles += 1

                    time_span = it.find("span", class_="time")
                    time_val = time_span.get("data-shared-at") or time_span.text if time_span else ""

                    articles.append({
                        "id": pid,
                        "url": f"https://www.jianshu.com/p/{pid}",
                        "title": t.text.strip(),
                        "publish_time": self._normalize_datetime(time_val)
                    })

                    if self.max_articles and len(articles) >= self.max_articles:
                        break

                if self.max_articles and len(articles) >= self.max_articles:
                    break

                # 终止条件：没有产生新的 feed ID 或没有新文章且已到底
                if not min_feed_id or (new_articles == 0 and min_feed_id == max_id):
                    break

                max_id = min_feed_id - 1
                await asyncio.sleep(0.3)
            except Exception:
                break

        if progress_callback:
            progress_callback(f"博主文章扫描完毕，共获取 {len(articles)} 篇文章", len(articles), len(articles))

        return articles

    def _extract_user_state_info(self, html_text: str) -> Optional[Dict[str, Any]]:
        """从用户主页的 window.__INITIAL_STATE__ 中提取作者信息"""
        try:
            match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html_text)
            if match:
                start_idx = match.end()
                decoder = json.JSONDecoder()
                data, _ = decoder.raw_decode(html_text[start_idx:].strip())
                if isinstance(data, dict):
                    user_info = data.get("users", {}).get("info")
                    if isinstance(user_info, dict) and user_info.get("nickname"):
                        return user_info
        except Exception:
            pass
        return None

    def _extract_state_info(self, html_text: str) -> Optional[Dict[str, Any]]:
        """从简书移动端 Nuxt.js SSR 的 window.__INITIAL_STATE__ 中提取结构化文章数据"""
        try:
            match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html_text)
            if match:
                start_idx = match.end()
                decoder = json.JSONDecoder()
                data, _ = decoder.raw_decode(html_text[start_idx:].strip())
                if isinstance(data, dict):
                    info = data.get("note", {}).get("info")
                    if isinstance(info, dict) and info.get("public_title"):
                        return info
                    pc_data = data.get("props", {}).get("initialState", {}).get("note", {}).get("data")
                    if isinstance(pc_data, dict) and pc_data.get("public_title"):
                        return pc_data
        except Exception:
            pass
        return None

    def _extract_next_data_info(self, html_text: str) -> Optional[Dict[str, Any]]:
        """从 PC 端 __NEXT_DATA__ 中提取数据"""
        try:
            match = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                note_data = (
                    data.get("props", {}).get("initialState", {}).get("note", {}).get("data")
                    or data.get("props", {}).get("pageProps", {}).get("note", {}).get("data")
                )
                if isinstance(note_data, dict):
                    return note_data
        except Exception:
            pass
        return None

    def _normalize_datetime(self, time_str: str) -> str:
        """标准化时间字符串为 YYYY-MM-DD HH:MM:SS"""
        if not time_str:
            return ""
        s = str(time_str).strip()
        if "T" in s:
            try:
                dt = datetime.datetime.fromisoformat(s)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                clean_s = s.replace("T", " ").split(".")[0].split("+")[0]
                return clean_s
        if s.isdigit() and len(s) == 10:
            try:
                dt = datetime.datetime.fromtimestamp(int(s))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return s

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        """根据文章元数据抓取并解析单篇文章详情"""
        url = article_meta["url"]
        post_id = article_meta.get("id") or url.split("/")[-1]
        title = article_meta.get("title", "未命名文章")
        publish_time = article_meta.get("publish_time", "")
        author = self.author_name or "简书作者"
        raw_html = ""
        read_num = 0
        like_count = 0
        comment_count = 0

        # 优先通道：使用 Mobile User-Agent 请求，直取 Nuxt SSR 注入的纯净结构化数据
        try:
            resp = await self.client.get(url, headers=self.MOBILE_HEADERS)
            if resp.status_code == 200:
                info = self._extract_state_info(resp.text)
                if info:
                    if info.get("public_title"):
                        title = info["public_title"].strip()
                    if info.get("user", {}).get("nickname"):
                        author = info["user"]["nickname"].strip()
                    if info.get("first_shared_at"):
                        publish_time = self._normalize_datetime(info["first_shared_at"])

                    raw_html = info.get("free_content") or ""
                    read_num = info.get("views_count") or 0
                    like_count = info.get("likes_count") or 0
                    comment_count = info.get("public_comment_count") or info.get("comments_count") or 0

                    # 付费文章友好提示
                    if info.get("paid_type") and info.get("paid_type") != "free":
                        raw_html += '<p><em>（注：本文包含简书付费/连载内容，以上为公开试读部分）</em></p>'
        except Exception:
            pass

        # 备用通道 1：若移动端未取到正文，使用 Desktop User-Agent 请求
        if not raw_html or len(raw_html.strip()) < 50:
            try:
                desktop_resp = await self.client.get(url, headers=DEFAULT_HEADERS)
                if desktop_resp.status_code == 200:
                    next_info = self._extract_next_data_info(desktop_resp.text)
                    if next_info and next_info.get("free_content"):
                        raw_html = next_info["free_content"]
                        if next_info.get("public_title"):
                            title = next_info["public_title"].strip()
                        if next_info.get("user", {}).get("nickname"):
                            author = next_info["user"]["nickname"].strip()
                        if next_info.get("first_shared_at"):
                            publish_time = self._normalize_datetime(next_info["first_shared_at"])

                    # 备用通道 2：PC DOM 解析
                    if not raw_html:
                        soup = BeautifulSoup(desktop_resp.text, "lxml")
                        h1 = soup.find("h1")
                        if h1 and h1.text.strip():
                            title = h1.text.strip()

                        article_tag = soup.find("article") or soup.select_one(".article-content, .show-content")
                        if article_tag:
                            raw_html = str(article_tag)
            except Exception:
                pass

        # 兜底文章时间
        if not publish_time:
            publish_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 经由通用清洗管道过滤广告、修复 lazyload 图片、溯源去水印原图并转换 Markdown
        cleaned_html, markdown_text, images = clean_html_content(
            raw_html,
            enable_noise_filter=self.enable_noise_filter,
            remove_watermark=self.remove_image_watermark
        )

        return ArticleItem(
            id=post_id,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="jianshu",
            summary=markdown_text[:200].replace("\n", " ").strip() if markdown_text else "",
            content_html=cleaned_html,
            content_markdown=markdown_text,
            images=images,
            category=self.category_name or "简书博文",
            read_num=read_num,
            like_count=like_count,
            comment_count=comment_count
        )
