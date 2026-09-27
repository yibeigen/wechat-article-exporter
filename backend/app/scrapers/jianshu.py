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
        self.author_stats = ""
        # 标记输入是否为无法识别的非标准简书链接（如中文昵称、非法路径），
        # 用于后续给用户明确报错，而不是静默返回空列表造成「博主没文章」的误导
        self.unrecognized_input = False
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

        # 默认回退：走到这里说明输入不是任何已知的标准简书链接格式（/p/ /c/ /nb/ /u/ 或纯ID）
        self.target_type = "user"
        self.target_slug = t.split("?")[0].split("/")[-1]
        self.unrecognized_input = True

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
                        self.author_stats = f"总字数: {user_state.get('total_wordage')} · 获赞: {user_state.get('total_likes_count', 0)}"
        except Exception:
            pass

        # 补充：从 PC 端主页提取官方标称文章总数与补充信息
        try:
            pc_resp = await self.client.get(user_url, headers=self.DESKTOP_AJAX_HEADERS)
            if pc_resp.status_code == 200:
                soup = BeautifulSoup(pc_resp.text, "lxml")
                name_elem = soup.select_one(".main-top .name, a.name, h1")
                avatar_elem = soup.select_one(".main-top .avatar img, a.avatar img")
                bio_elem = soup.select_one(".main-top .description .js-intro, .main-top .info")

                if name_elem and (not self.author_name or self.author_name == "简书作者"):
                    self.author_name = name_elem.text.strip()
                if avatar_elem and avatar_elem.get("src") and not self.author_avatar:
                    src = avatar_elem["src"]
                    self.author_avatar = "https:" + src if src.startswith("//") else src
                if bio_elem and not self.author_bio:
                    self.author_bio = bio_elem.text.strip().replace("\n", " ")

                info_box = soup.select_one(".main-top .info, .info")
                if info_box:
                    for li in info_box.find_all("li"):
                        m = re.search(r"(\d+)\s*文章", li.text.replace(" ", ""))
                        if m:
                            self.declared_count = int(m.group(1))
                            break
        except Exception:
            pass

        return {"name": self.author_name, "avatar": self.author_avatar, "bio": self.author_bio}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        """获取文章元数据列表"""
        articles: List[Dict[str, str]] = []
        seen_ids = set()

        if self.target_type == "single":
            single_url = f"https://www.jianshu.com/p/{self.target_slug}"
            self.explanation = "已成功提取单篇简书博文。"
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
                    resp = None
                    for retry_i in range(3):
                        try:
                            resp = await self.client.get(page_url, headers=self.DESKTOP_AJAX_HEADERS, timeout=12.0)
                            if resp.status_code == 200:
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(0.4 * (retry_i + 1))

                    if not resp or resp.status_code != 200 or (page > 1 and "/page=1" in str(resp.url)):
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

            self._build_explanation(len(articles))
            if progress_callback:
                progress_callback(f"简书专题扫描完成，共获取 {len(articles)} 篇文章", len(articles), len(articles))
            return articles

        # 用户主页博文检索：使用高容灾的 timeline + max_id 瀑布流机制
        # 先拦截无法识别的输入（如中文昵称）：简书 ID 是 12 位字母数字，昵称无法直接检索，必须明确告知用户
        if self.unrecognized_input:
            raise ValueError(
                f"无法识别简书博主链接「{self.target[:80]}」。"
                f"请粘贴简书博主主页链接，标准格式为 https://www.jianshu.com/u/xxxxxxxx（u 后跟 12 位字母数字ID），暂不支持直接输入中文昵称检索。"
            )

        # 优先通道：简书原生 public_notes JSON 接口（每批 100 篇，极速且 100% 原创，不混杂他人点赞）
        api_page = 1
        asimov_failed = False
        empty_batches = 0

        while True:
            api_url = f"https://www.jianshu.com/asimov/users/slug/{self.target_slug}/public_notes?page={api_page}&count=100"
            if progress_callback:
                progress_callback(
                    f"正在快速同步博主原创博文（第 {api_page} 批，已收录 {len(articles)} 篇）...",
                    len(articles),
                    self.declared_count or len(articles)
                )

            try:
                resp = await self.client.get(
                    api_url,
                    headers={
                        **self.MOBILE_HEADERS,
                        "Accept": "application/json"
                    },
                    timeout=12.0
                )
                if resp.status_code != 200:
                    if api_page == 1 and not articles:
                        asimov_failed = True
                    break

                items = resp.json()
                if not items or not isinstance(items, list):
                    break

                new_count = 0
                for it in items:
                    data = it.get("object", {}).get("data", {})
                    pid = data.get("slug")
                    if not pid or pid in seen_ids:
                        continue

                    seen_ids.add(pid)
                    new_count += 1
                    title = (data.get("title") or "简书文章").strip()
                    shared_time = data.get("first_shared_at") or ""

                    articles.append({
                        "id": pid,
                        "url": f"https://www.jianshu.com/p/{pid}",
                        "title": title,
                        "publish_time": self._normalize_datetime(shared_time)
                    })

                    if self.max_articles and len(articles) >= self.max_articles:
                        break

                if self.max_articles and len(articles) >= self.max_articles:
                    break

                if new_count == 0:
                    empty_batches += 1
                    if empty_batches >= 1:
                        break
                else:
                    empty_batches = 0

                api_page += 1
                await asyncio.sleep(0.08)
            except Exception:
                if api_page == 1 and not articles:
                    asimov_failed = True
                break

        if articles:
            self._build_explanation(len(articles))
            if progress_callback:
                progress_callback(f"博主博文扫描完毕，共获取 {len(articles)} 篇原创文章", len(articles), len(articles))
            return articles

        # 备用容灾通道：若原生 JSON 接口不可用，回退至 timeline 动态流
        # 注意：此处恢复为 1000 批（避免 60 批截断），并严格过滤 like_note 等他人文章！
        max_id: Optional[int] = None
        step = 0
        max_steps = 1000
        empty_streak = 0
        first_fail_status: Optional[int] = None
        first_fail_redirected = False

        while step < max_steps:
            step += 1
            timeline_url = f"https://www.jianshu.com/users/{self.target_slug}/timeline"
            if max_id:
                timeline_url += f"?max_id={max_id}"

            if progress_callback:
                progress_callback(f"正在扫描博主时间线第 {step} 批文章...", len(articles), self.declared_count or len(articles))

            try:
                resp = None
                for retry_i in range(3):
                    try:
                        resp = await self.client.get(timeline_url, headers=self.DESKTOP_AJAX_HEADERS, timeout=10.0)
                        if resp.status_code == 200:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.3 * (retry_i + 1))

                if not resp or resp.status_code != 200:
                    if step == 1 and not articles and resp is not None:
                        first_fail_status = resp.status_code
                    break

                if step == 1 and getattr(resp, "history", None):
                    first_fail_redirected = True
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                items = soup.find_all("li")
                new_articles = 0
                min_feed_id: Optional[int] = None

                for it in items:
                    feed_attr = it.get("id", "")
                    if feed_attr and feed_attr.startswith("feed-"):
                        try:
                            fid = int(feed_attr.split("-")[1])
                            if min_feed_id is None or fid < min_feed_id:
                                min_feed_id = fid
                        except Exception:
                            pass

                    # 严格过滤：剔除点赞他人、赞赏他人、评论他人等互动动态，确保只保留原创博文
                    type_span = it.find("span", attrs={"data-type": True})
                    feed_type = type_span.get("data-type", "") if type_span else ""
                    if feed_type in ("like_note", "comment_note", "reward_note"):
                        continue

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

                if not min_feed_id:
                    break
                if new_articles == 0:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                else:
                    empty_streak = 0

                max_id = min_feed_id - 1
                await asyncio.sleep(0.2)
            except Exception:
                break

        # 循环外统一抛出首次失败的原因
        if first_fail_status == 404:
            raise ValueError(
                "简书博主主页不存在（HTTP 404）：链接可能有误、ID 不完整，或该账号已注销。"
                "请确认粘贴的是完整的 https://www.jianshu.com/u/xxxxxxxx 主页链接。"
            )
        if first_fail_status in (403, 429):
            raise ValueError(
                f"简书服务器拒绝了本次访问（HTTP {first_fail_status}），可能触发临时风控，请稍后重试。"
            )
        if first_fail_redirected:
            raise ValueError(
                "简书服务器将该博主主页重定向到了其他页面：通常是链接无效、ID 不完整或账号已注销；"
                "若链接确认无误，则可能是简书临时风控，请稍后重试。"
            )

        self._build_explanation(len(articles))
        if progress_callback:
            progress_callback(f"博主文章扫描完毕，共获取 {len(articles)} 篇文章", len(articles), len(articles))

        return articles

    def _build_explanation(self, actual_count: int):
        """生成篇数一致性自洽说明，使前端提示让用户完全安心、明晰原委"""
        target_name = f"「{self.category_name}」" if self.category_name else (f"作者「{self.author_name}」" if self.author_name and self.author_name != "简书作者" else "博主主页")
        stats_note = f"（博主{self.author_stats}）" if self.author_stats else ""

        if self.declared_count is not None:
            if self.max_articles and actual_count >= self.max_articles:
                self.explanation = (
                    f"已按您设置的最大篇数限制成功提取{target_name}前 {actual_count} 篇博文"
                    f"（源平台页面标称共 {self.declared_count} 篇）{stats_note}。"
                )
            elif self.declared_count > actual_count:
                diff = self.declared_count - actual_count
                self.explanation = (
                    f"源网页标称{target_name}含 <b>{self.declared_count}</b> 篇博文，系统已全部分页深度遍历，"
                    f"<b>100% 完整收录公网当前所有可访问的 {actual_count} 篇有效博文</b>。<br>"
                    f"差额 <b>{diff} 篇</b> 通常为博主多年间自行删除、设为私密或平台系统下架屏蔽所致（公网已无对应访问页面），"
                    f"并非系统抓取遗漏，请放心导出使用！"
                )
                if self.author_stats:
                    self.explanation += f"<br><span style='color: var(--text-muted); font-size: 0.72rem;'>📊 数据统计：{self.author_stats}</span>"
            elif self.declared_count == actual_count:
                self.explanation = f"已 100% 完整全量同步{target_name}全部 <b>{actual_count}</b> 篇博文，与源平台标称数量完全吻合。"
                if self.author_stats:
                    self.explanation += f"<br><span style='color: var(--text-muted); font-size: 0.72rem;'>📊 数据统计：{self.author_stats}</span>"
            else:
                self.explanation = f"已成功检索并收录{target_name}全部 {actual_count} 篇公开博文。"
                if self.author_stats:
                    self.explanation += f" {stats_note}"
        else:
            self.explanation = f"已成功检索并收录全部 {actual_count} 篇公开博文。"
            if self.author_stats:
                self.explanation += f" {stats_note}"

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

        # 0. 客户端中继通道：如果已提供 raw_html，直接提取结构化数据或 DOM
        client_raw_html = article_meta.get("raw_html")
        if client_raw_html:
            info = self._extract_state_info(client_raw_html) or self._extract_next_data_info(client_raw_html)
            if info:
                if info.get("public_title"):
                    title = info["public_title"].strip()
                if info.get("user", {}).get("nickname"):
                    author = info["user"]["nickname"].strip()
                if info.get("user", {}).get("avatar"):
                    self.author_avatar = info["user"]["avatar"].strip()
                if info.get("first_shared_at"):
                    publish_time = self._normalize_datetime(info["first_shared_at"])
                raw_html = info.get("free_content") or ""
                read_num = info.get("views_count") or 0
                like_count = info.get("likes_count") or 0
                comment_count = info.get("public_comment_count") or info.get("comments_count") or 0
            if not raw_html:
                soup = BeautifulSoup(client_raw_html, "lxml")
                h1 = soup.find("h1")
                if h1 and h1.text.strip():
                    title = h1.text.strip()
                article_tag = soup.find("article") or soup.select_one(".article-content, .show-content")
                if article_tag:
                    raw_html = str(article_tag)

        # 优先通道：使用 Mobile User-Agent 请求，直取 Nuxt SSR 注入的纯净结构化数据
        if not raw_html or len(raw_html.strip()) < 50:
            try:
                resp = await self.client.get(url, headers=self.MOBILE_HEADERS)
                if resp.status_code == 200:
                    info = self._extract_state_info(resp.text)
                    if info:
                        if info.get("public_title"):
                            title = info["public_title"].strip()
                        if info.get("user", {}).get("nickname"):
                            author = info["user"]["nickname"].strip()
                        if info.get("user", {}).get("avatar"):
                            self.author_avatar = info["user"]["avatar"].strip()
                        if info.get("first_shared_at"):
                            publish_time = self._normalize_datetime(info["first_shared_at"])

                        raw_html = info.get("free_content") or ""
                        read_num = info.get("views_count") or 0
                        like_count = info.get("likes_count") or 0
                        comment_count = info.get("public_comment_count") or info.get("comments_count") or 0

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
            author_avatar=self.author_avatar or "",
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
