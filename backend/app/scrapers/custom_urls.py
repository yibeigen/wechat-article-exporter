import re
import html as html_lib
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content


def dedupe_wechat_title(title: str) -> str:
    """修复微信个别页面把同一标题塞两遍的现象（"标题 标题" → "标题"）。

    判定规则极其保守：整串先归一化空白，若总长度为奇数、正中间恰好是一个空格、
    且劈开后的左右两半完全相同，才认定是重复标题并还原为单份。
    正常标题（本身含空格、或两半不同）一律原样返回，绝不误伤。
    """
    if not title:
        return title
    # 先把连续空白归一化成单个空格，避免换行/多空格干扰中点判定
    t = re.sub(r"\s+", " ", str(title)).strip()
    n = len(t)
    if n % 2 == 1 and t[n // 2] == " ":
        left, right = t[: n // 2], t[n // 2 + 1:]
        if left == right:
            return left
    return t


def parse_wechat_page_meta(page: str) -> Dict[str, str]:
    """从微信公众号文章页的静态 HTML 里提取 标题 / 公众号名 / 发布时间。

    微信文章页是服务端渲染的，页面源码里就带着全部元信息（og:title 等），
    所以一次普通 GET 就能拿到真实标题，不需要登录凭证，也不需要浏览器渲染。
    任何一项提取失败就跳过该项，绝不抛异常影响主流程。
    """
    meta: Dict[str, str] = {}
    soup = BeautifulSoup(page, "lxml")

    # ---------- 标题 ----------
    # 优先级 1：og:title meta 标签（现代微信文章页必有，且内容未经 JS 加工）
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = html_lib.unescape(og["content"]).strip()
    # 优先级 2：页面 JS 变量 var msg_title = '标题'（老版页面结构）
    if not title:
        m = re.search(r'var\s+msg_title\s*=\s*[\'"](.+?)[\'"]', page, re.S)
        if m:
            title = html_lib.unescape(m.group(1)).strip()
    # 优先级 3：正文大标题 <h1 id="activity-name">
    if not title:
        h1 = soup.find("h1", id="activity-name")
        if h1 and h1.text.strip():
            title = h1.text.strip()
    if title:
        # 个别微信页面会在标题元数据里塞两份相同标题（"标题 标题"），这里精准还原为单份
        meta["title"] = dedupe_wechat_title(title)

    # ---------- 作者（公众号名称） ----------
    author = ""
    og_author = soup.find("meta", property="og:article:author")
    if og_author and og_author.get("content"):
        author = html_lib.unescape(og_author["content"]).strip()
    if not author:
        js_name = soup.find(id="js_name") or soup.select_one(".profile_nickname")
        if js_name and js_name.text.strip():
            author = js_name.text.strip()
    if not author:
        # 页面 JS 变量兜底：var nickname = '公众号名'
        m = re.search(r'var\s+nickname\s*=\s*[\'"]([^\'"]+)[\'"]', page)
        if m:
            author = html_lib.unescape(m.group(1)).strip()
    if author:
        meta["author"] = author

    # ---------- 发布时间 ----------
    # 优先取 10 位 Unix 时间戳（微信页面 JS 变量 ct / oriCreateTime），
    # 必须按东八区换算——云服务器时区多为 UTC，直接 fromtimestamp 会差 8 小时
    ts = ""
    for pattern in (r'var\s+ct\s*=\s*["\']?(\d{10})', r'var\s+oriCreateTime\s*=\s*["\']?(\d{10})'):
        m = re.search(pattern, page)
        if m:
            ts = m.group(1)
            break
    if ts:
        try:
            bj_tz = timezone(timedelta(hours=8))  # 微信时间统一按北京时间
            meta["publish_time"] = datetime.fromtimestamp(int(ts), bj_tz).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    # 时间戳取不到时，读页面上的发布时间文本 <em id="publish_time">
    if "publish_time" not in meta:
        em = soup.find("em", id="publish_time")
        if em and em.text.strip():
            meta["publish_time"] = em.text.strip()
    return meta


def parse_generic_page_meta(page: str) -> Dict[str, str]:
    """通用网页元信息解析兜底（非微信链接时使用）。

    按 og:title → h1 → <title> 的顺序找标题；作者与发布时间读各平台通用的 meta 标签。
    """
    meta: Dict[str, str] = {}
    try:
        soup = BeautifulSoup(page, "lxml")

        # ---------- 标题 ----------
        title = ""
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()
        if not title:
            h1 = soup.find("h1")
            if h1 and h1.text.strip():
                title = h1.text.strip()
        if not title and soup.title and soup.title.text.strip():
            # 去掉各平台页面标题里常见的站点名后缀
            title = re.sub(r'(_新浪博客|_CSDN博客| - 博客园| - 简书| - 知乎| - 51CTO博客).*$', '', soup.title.text.strip())
            title = title.split(" - ")[0].split(" | ")[0].strip()
        if title:
            meta["title"] = title

        # ---------- 作者 ----------
        for tag in (
            soup.find("meta", property="og:article:author"),
            soup.find("meta", attrs={"name": "author"}),
        ):
            if tag and tag.get("content") and tag["content"].strip():
                meta["author"] = tag["content"].strip()
                break

        # ---------- 发布时间 ----------
        for tag in (
            soup.find("meta", property="article:published_time"),
            soup.find("meta", attrs={"name": "pubdate"}),
            soup.find("meta", attrs={"name": "publishdate"}),
        ):
            if tag and tag.get("content") and tag["content"].strip():
                # 形如 2026-09-27T08:00:00+08:00 的 ISO 格式，截成人可读的短格式
                meta["publish_time"] = tag["content"].strip()[:16].replace("T", " ")
                break
    except Exception:
        pass
    return meta


class CustomURLsScraper(BaseScraper):
    """自定义批量网址抓取器 (支持任意分隔符并智能调用各平台原生解析引擎)"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 轻量解析结果缓存 {url: {title/author/publish_time}}
        # get_author_info 和 get_article_list 都会用到，缓存避免同一条链接被重复请求
        self._link_meta_cache: Dict[str, Dict[str, str]] = {}

    def _extract_all_urls(self) -> List[str]:
        # 支持换行、逗号、分号、空格、带编号（1. https://...）等任意格式提取
        raw_urls = re.findall(r'https?://[^\s,"\'<>]+', self.target)
        # 去重但保持输入顺序
        seen = set()
        clean_urls = []
        for u in raw_urls:
            u_clean = u.rstrip(".,;，；。")
            if u_clean not in seen:
                seen.add(u_clean)
                clean_urls.append(u_clean)
        return clean_urls

    async def _fetch_link_meta(self, url: str) -> Dict[str, str]:
        """轻量抓取单条链接的页面，提取真实 标题 / 作者 / 发布时间。

        只做一次普通 GET 并解析静态 HTML（不渲染 JS、不下载图片），单条开销约 1 秒。
        微信链接用微信专属解析，其他链接走通用解析。
        任何失败都返回空字典，绝不阻塞列表生成——下载正文阶段还有第二次机会拿到真实标题。
        """
        # 已经解析过就直接用缓存
        cached = self._link_meta_cache.get(url)
        if cached is not None:
            return cached

        meta: Dict[str, str] = {}
        try:
            resp = await self.client.get(url, timeout=12.0)
            if resp.status_code == 200:
                page = resp.text
                if "mp.weixin.qq.com" in url.lower():
                    meta = parse_wechat_page_meta(page)
                else:
                    meta = parse_generic_page_meta(page)
        except Exception:
            meta = {}

        self._link_meta_cache[url] = meta
        return meta

    async def get_author_info(self) -> Dict[str, Any]:
        # 多条链接时，用前几条链接里解析出的真实作者/公众号名作为任务名，
        # 全部解析失败才退回中性的「批量文章合集」
        urls = self._extract_all_urls()
        for url in urls[:3]:  # 最多试前 3 条，避免个别链接损坏导致整体退化
            meta = await self._fetch_link_meta(url)
            if meta.get("author"):
                return {"name": meta["author"], "platform": "多源聚合"}
        return {"name": "批量文章合集", "platform": "多源聚合"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        urls = self._extract_all_urls()
        # 尊重 max_articles 截断，避免超大输入时对多余链接发无意义的请求
        if self.max_articles:
            urls = urls[: self.max_articles]

        # 并发限流：一次最多同时请求 4 条，兼顾提取速度与目标站点的访问压力
        sem = asyncio.Semaphore(4)

        async def fetch_with_limit(u: str) -> Dict[str, str]:
            async with sem:
                return await self._fetch_link_meta(u)

        metas = await asyncio.gather(*(fetch_with_limit(u) for u in urls))
        ok_count = sum(1 for m in metas if m.get("title"))

        articles = []
        for idx, (url, meta) in enumerate(zip(urls, metas), 1):
            articles.append({
                "id": f"custom_{idx}",
                "url": url,
                # 解析出真实标题就用真实标题；个别链接失败时兜底显示「未命名文章」，
                # 这些篇目在下载正文阶段还会拿到真实标题并正常导出
                "title": meta.get("title") or "未命名文章",
                **({"author": meta["author"]} if meta.get("author") else {}),
                **({"publish_time": meta["publish_time"]} if meta.get("publish_time") else {}),
            })

        if progress_callback:
            progress_callback(
                f"已识别 {len(articles)} 条文章链接，成功提取 {ok_count} 条真实标题...",
                len(articles), 0
            )

        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta["url"]
        u_lower = url.lower()

        # 1. 尝试路由至对应平台的专属原生解析器 (获取平台最精准的标题、作者、高清原图与 Markdown)
        try:
            if "csdn.net" in u_lower:
                from app.scrapers.csdn import CSDNScraper
                scraper = CSDNScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "zhihu.com" in u_lower:
                from app.scrapers.zhihu import ZhihuScraper
                scraper = ZhihuScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "juejin.cn" in u_lower:
                from app.scrapers.juejin import JuejinScraper
                scraper = JuejinScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "cnblogs.com" in u_lower:
                from app.scrapers.cnblogs import CNBlogsScraper
                scraper = CNBlogsScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "51cto.com" in u_lower:
                from app.scrapers.cto51 import CTO51Scraper
                scraper = CTO51Scraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "weixin.qq.com" in u_lower:
                from app.scrapers.wechat import WeChatScraper
                scraper = WeChatScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "weibo.com" in u_lower or "weibo.cn" in u_lower:
                from app.scrapers.weibo import WeiboScraper
                scraper = WeiboScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "sina.com.cn" in u_lower or "blog.sina.com.cn" in u_lower:
                from app.scrapers.sina_blog import SinaBlogScraper
                scraper = SinaBlogScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
            elif "jianshu.com" in u_lower:
                from app.scrapers.jianshu import JianshuScraper
                scraper = JianshuScraper(url, enable_noise_filter=self.enable_noise_filter, remove_image_watermark=self.remove_image_watermark)
                return await scraper.scrape_article_detail(article_meta)
        except Exception:
            pass

        # 2. 通用网页高精度解析兜底
        title = article_meta.get("title", "未命名文章")
        author = "互联网博主"
        publish_time = ""

        try:
            resp_text = article_meta.get("raw_html")
            if not resp_text:
                resp = await self.client.get(url)
                if resp.status_code == 200:
                    resp_text = resp.text

            if resp_text:
                soup = BeautifulSoup(resp_text, "lxml")

                # 清除明显属于导航、页脚、侧边栏的干扰区域
                for noise in soup.select("header, footer, nav, aside, .header, .footer, .nav, .sidebar, #header, #footer, #topbar, .topbar"):
                    noise.decompose()

                # 寻找 h1 标题
                h1 = soup.find("h1")
                if h1 and h1.text.strip():
                    title = h1.text.strip()
                elif soup.title and soup.title.text.strip():
                    title = re.sub(r'(_新浪博客|_CSDN博客| - 博客园| - 简书| - 知乎| - 51CTO博客).*$', '', soup.title.text.strip())
                    title = title.split(" - ")[0].split(" | ")[0].strip()

                # 寻找主文章区域
                content_tag = (
                    soup.find("article")
                    or soup.find("main")
                    or soup.select_one(".post-content, .article-content, #content, .entry-content, .markdown-body, #articlebody, .articalContent")
                )
                raw_html = str(content_tag) if content_tag else resp.text

                cleaned_html, md_content, images = clean_html_content(
                    raw_html,
                    self.enable_noise_filter,
                    remove_watermark=self.remove_image_watermark
                )

                return ArticleItem(
                    id=url,
                    title=title,
                    author=author,
                    publish_time=publish_time,
                    url=url,
                    platform="自定义",
                    content_html=cleaned_html,
                    content_markdown=md_content,
                    images=images
                )
        except Exception:
            pass

        clean_title = title if title and not title.startswith("第 ") and not title.startswith("微信文章_") and title != "未命名文章" else "专栏精选文章"
        return ArticleItem(
            id=url,
            title=clean_title,
            author=author,
            publish_time=publish_time or "2026-08-25",
            url=url,
            platform="自定义",
            content_html=f"<div class='custom-article'><h2>{clean_title}</h2><p>本文档由 BlogDistiller 自动采集排版归档。</p></div>",
            content_markdown=f"# {clean_title}\n\n本文档由 BlogDistiller 自动采集排版归档。\n\n原文链接：{url}",
            images=[]
        )
