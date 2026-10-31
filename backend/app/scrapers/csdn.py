import re
import asyncio
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content

# CSDN 全站挂了「加速乐」JS 质询：普通浏览器 UA 的裸请求一律返回 HTTP 521 挑战页，
# 拿不到目录也拿不到正文。与 51CTO 的破局思路一致（见 cto51.py），主流搜索引擎爬虫
# 走的是 SEO 特权放行通道，可直接 200 返回完整 HTML/JSON，因此这里优先用爬虫 UA，
# 普通浏览器 UA 只作兜底。
BOT_USER_AGENT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class CSDNScraper(BaseScraper):
    """CSDN 博客文章抓取器 (具备 WAF 防护突破与 API/HTML 双通道容灾)"""

    last_error: Optional[str] = None

    def _extract_username(self) -> str:
        # 支持多种 CSDN URL 格式:
        # https://blog.csdn.net/qq_46987323?type=blog
        # https://blog.csdn.net/qq_46987323
        # qq_46987323
        target = self.target.split("?")[0].split("#")[0].strip("/ ")
        match = re.search(r"blog\.csdn\.net/([a-zA-Z0-9\-_]+)", target)
        if match:
            return match.group(1)
        match = re.search(r"csdn\.net/([a-zA-Z0-9\-_]+)", target)
        if match:
            return match.group(1)
        if "/" in target:
            parts = [p for p in target.split("/") if p]
            return parts[-1]
        return target

    def _get_headers(self, referer: Optional[str] = None, as_bot: bool = True) -> Dict[str, str]:
        username = self._extract_username()
        ref = referer or f"https://blog.csdn.net/{username}?type=blog"
        return {
            "User-Agent": BOT_USER_AGENT if as_bot else BROWSER_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": ref,
            "Origin": "https://blog.csdn.net"
        }

    async def _get(self, url: str, referer: Optional[str] = None, timeout: float = 12.0):
        """特权爬虫 UA 与普通浏览器 UA 各试两次；成功返回响应，失败返回 None 并记下真实原因。"""
        self.last_error = None
        for as_bot in (True, False):
            for _ in range(2):
                try:
                    resp = await self.client.get(url, headers=self._get_headers(referer, as_bot), timeout=timeout)
                except Exception as e:
                    self.last_error = f"网络异常 {e.__class__.__name__}"
                    await asyncio.sleep(0.4)
                    continue
                if resp.status_code == 200:
                    return resp
                self.last_error = f"HTTP {resp.status_code} (CSDN 反爬质询拦截)" if resp.status_code == 521 \
                    else f"HTTP {resp.status_code}"
        return None

    async def get_author_info(self) -> Dict[str, Any]:
        username = self._extract_username()
        author_name = username
        try:
            url = f"https://blog.csdn.net/{username}?type=blog"
            resp = await self._get(url, referer=url)
            if resp is not None:
                soup = BeautifulSoup(resp.text, "lxml")
                name_tag = soup.select_one(".user-profile-name, .user-name, #uid, .profile-intro-name, .user-profile-head-info-name")
                if name_tag and name_tag.text.strip():
                    author_name = name_tag.text.strip()
                else:
                    # 爬虫通道返回的是服务端 HTML，昵称落在 <title>艺杯羹-CSDN博客</title>
                    title_tag = soup.select_one("title")
                    if title_tag and title_tag.text.strip():
                        name = re.sub(r"-CSDN(\博客|问答)?\s*$", "", title_tag.text.strip()).strip()
                        if name:
                            author_name = name
        except Exception:
            pass
        return {"name": author_name, "username": username, "platform": "CSDN"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        # 如果输入的是单篇文章链接，直接返回单篇，不再全量遍历博主历史
        if "/article/details/" in self.target:
            return [{
                "id": self.target,
                "url": self.target,
                "title": "CSDN单篇文章"
            }]

        username = self._extract_username()
        articles = []
        seen_urls = set()
        total_expected = 0

        # 通道 1: 官方 API 接口
        page = 1
        while page <= 100:
            api_url = f"https://blog.csdn.net/community/home-api/v1/get-business-list?page={page}&size=20&businessType=blog&username={username}"
            resp = await self._get(api_url)
            if resp is None:
                break
            try:
                data = resp.json()
            except Exception:
                self.last_error = "接口返回非 JSON (疑似被拦截页替换)"
                break
            if data.get("code") != 200:
                self.last_error = f"接口返回 code={data.get('code')}"
                break
            d = data.get("data") or {}
            items = d.get("list") or []
            total_expected = d.get("total", 0) or total_expected
            self.declared_count = total_expected or None
            if not items:
                break

            for item in items:
                url = (item.get("url") or item.get("articleDetailUrl") or "").split("?")[0]
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                articles.append({
                    "id": str(item.get("articleId", "")) or url,
                    "url": url,
                    "title": (item.get("title") or "").strip(),
                    "publish_time": item.get("postTime", "") or item.get("createTime", ""),
                    "description": item.get("description", "")
                })
                if self.max_articles and len(articles) >= self.max_articles:
                    return articles

            if progress_callback:
                progress_callback(f"已获取 CSDN 文章 {len(articles)}/{total_expected or len(articles)} 篇 (第 {page} 页)...", len(articles), total_expected or 0)

            if total_expected > 0 and len(articles) >= total_expected:
                break
            page += 1
            await asyncio.sleep(0.15)

        # 通道 2: HTML 页面解析兜底 (如果 API 仅获取到部分或被阻断)
        if not articles or (total_expected > 0 and len(articles) < total_expected):
            for page in range(1, 100):
                list_url = f"https://blog.csdn.net/{username}/article/list/{page}"
                resp = await self._get(list_url, referer=list_url)
                if resp is None:
                    break
                soup = BeautifulSoup(resp.text, "lxml")
                card_items = soup.select(".blog-list-box, .article-item-box, .mainContent article, .article-list-item")
                if not card_items:
                    break

                for card in card_items:
                    link = card.select_one("a[href*='/article/details/']")
                    if not link:
                        continue
                    url = link.get("href", "").split("?")[0]
                    title = link.text.strip()
                    if not title:
                        title_el = card.select_one("h4, .blog-list-box-top, .title")
                        title = title_el.text.strip() if title_el else "无标题"

                    time_el = card.select_one(".date, .time, .view-time")
                    publish_time = time_el.text.strip() if time_el else ""

                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        articles.append({
                            "id": url,
                            "url": url,
                            "title": title,
                            "publish_time": publish_time,
                            "description": ""
                        })

                    if self.max_articles and len(articles) >= self.max_articles:
                        return articles

                if progress_callback:
                    progress_callback(f"已获取 CSDN 文章 {len(articles)} 篇 (HTML 兜底第 {page} 页)...", len(articles), total_expected or 0)

        # 0 结果与截断都必须给出真实原因，否则前端只能显示与 CSDN 无关的兜底提示
        if not articles:
            self.explanation = (
                f"原因：未能读取该博主的博文目录（{self.last_error or 'CSDN 未返回数据'}）。"
                "CSDN 对无 Cookie 的批量抓取有风控质询，请稍后重试；若长期失败，可改用「自定义链接」逐篇导入。"
            )
        elif total_expected > 0 and len(articles) < total_expected:
            self.list_truncated = True
            self.explanation = f"仅取到 {len(articles)}/{total_expected} 篇，其余被 CSDN 分页/风控截断。"
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta["url"]
        title = article_meta.get("title", "无标题")
        publish_time = article_meta.get("publish_time", "")
        author = self._extract_username()
        error_msg = None
        resp_text = article_meta.get("raw_html")

        try:
            if not resp_text:
                resp = await self._get(url, referer=url, timeout=15.0)
                if resp is None:
                    error_msg = f"页面获取失败：{self.last_error or '未知原因'}"
                else:
                    resp_text = resp.text

            if resp_text:
                soup = BeautifulSoup(resp_text, "lxml")

                title_tag = soup.select_one("#articleContentId, .title-article, h1")
                if title_tag:
                    title = title_tag.text.strip()

                time_tag = soup.select_one(".time, .article-time, .time-box, .article-info-box .time, .blog-post-time")
                if time_tag:
                    time_match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", time_tag.text)
                    if time_match:
                        publish_time = time_match.group(0)

                content_tag = soup.select_one("#content_views, .article_content, #article_content")
                if content_tag and content_tag.text.strip():
                    raw_html = str(content_tag)
                    cleaned_html, md_content, images = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)

                    return ArticleItem(
                        id=url,
                        title=title,
                        author=author,
                        publish_time=publish_time,
                        url=url,
                        platform="CSDN",
                        summary=article_meta.get("description", ""),
                        content_html=cleaned_html,
                        content_markdown=md_content,
                        images=images,
                        is_failed=False,
                        error_reason=None
                    )
                else:
                    error_msg = "页面已加载但未找到有效正文 (可能为博主付费专栏、需登录可见或已被作者隐藏)"
        except Exception as e:
            error_msg = f"网络请求异常: {str(e)}"

        return ArticleItem(
            id=url,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="CSDN",
            content_html=f"<p>【抓取失败】：{error_msg or '未知异常'}</p>",
            content_markdown=f"【抓取失败】：{error_msg or '未知异常'}",
            images=[],
            is_failed=True,
            error_reason=error_msg or "无法获取正文"
        )
