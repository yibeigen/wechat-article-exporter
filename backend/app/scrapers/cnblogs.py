import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content

class CNBlogsScraper(BaseScraper):
    """博客园文章抓取器 (支持随笔 / 文章 / 合集 / 标签 / 分类全量深度递归抓取)"""
    
    def _extract_blog_id(self) -> str:
        # 1. 匹配个人中心链接 home.cnblogs.com/u/ybgly
        m_home = re.search(r"home\.cnblogs\.com/u/([a-zA-Z0-9\-_]+)", self.target)
        if m_home:
            return m_home.group(1)
        
        # 2. 匹配博客主页或单篇文章 cnblogs.com/ybgly/articles/123 或 cnblogs.com/ybgly/p/123
        m_blog = re.search(r"cnblogs\.com/([a-zA-Z0-9\-_]+)", self.target)
        if m_blog:
            val = m_blog.group(1)
            if val == "u":
                m_sub = re.search(r"cnblogs\.com/u/([a-zA-Z0-9\-_]+)", self.target)
                if m_sub:
                    return m_sub.group(1)
            return val

        return self.target.strip("/ ")

    def _is_single_article_url(self) -> bool:
        """判断输入的是否为单篇文章链接 (包含 /articles/、/p/、/archive/ 等)"""
        return bool(re.search(r"cnblogs\.com/[a-zA-Z0-9\-_]+/(?:articles|p|archive|diary)/([a-zA-Z0-9\-_]+)", self.target))

    async def get_author_info(self) -> Dict[str, Any]:
        blog_id = self._extract_blog_id()
        home_url = f"https://www.cnblogs.com/{blog_id}/"
        author_name = blog_id
        try:
            resp = await self.client.get(home_url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                title_tag = soup.select_one("#Header1_HeaderTitle, #blogTitle h1, .headermaintitle, #lnkBlogLogo, .header-title")
                if title_tag and title_tag.text.strip():
                    author_name = title_tag.text.strip()
        except Exception:
            pass
        return {"name": author_name, "blog_id": blog_id, "platform": "博客园"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        blog_id = self._extract_blog_id()
        articles = []
        seen_urls = set()
        is_single = self._is_single_article_url()

        # ==========================================================
        # 1. 单篇文章链接直接返回单篇
        # ==========================================================
        if is_single:
            clean_url = self.target.split("?")[0].strip()
            title = "博客园文章"
            try:
                resp = await self.client.get(clean_url)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    title_tag = soup.select_one("#cb_post_title_url, .postTitle2, .postTitle, .article-title, h1.postTitle")
                    if title_tag and title_tag.text.strip() and title_tag.text.strip() != blog_id:
                        title = title_tag.text.strip()
                    elif soup.title and soup.title.text.strip():
                        title = soup.title.text.split(" - ")[0].strip()
            except Exception:
                pass
            return [{"id": clean_url, "url": clean_url, "title": title}]

        # ==========================================================
        # 2. 如果输入的是单篇文章，先将本篇保底加入
        # ==========================================================
        if is_single:
            clean_url = self.target.split("?")[0].strip()
            title = "博客园文章"
            try:
                resp = await self.client.get(clean_url)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    title_tag = soup.select_one("#cb_post_title_url, .postTitle2, .postTitle, .article-title, h1.postTitle")
                    if title_tag and title_tag.text.strip() and title_tag.text.strip() != blog_id:
                        title = title_tag.text.strip()
                    elif soup.title and soup.title.text.strip():
                        title = soup.title.text.split(" - ")[0].strip()
            except Exception:
                pass
            seen_urls.add(clean_url)
            articles.append({"id": clean_url, "url": clean_url, "title": title})

        # ==========================================================
        # 3. 通道 A: 遍历博客主页默认“随笔”分页 (default.html 及 /p/)
        # ==========================================================
        for base_pat in [f"https://www.cnblogs.com/{blog_id}/default.html?page=", f"https://www.cnblogs.com/{blog_id}/p/?page="]:
            page = 1
            while True:
                page_url = f"{base_pat}{page}" if page > 1 else (f"https://www.cnblogs.com/{blog_id}/" if "default.html" in base_pat else f"https://www.cnblogs.com/{blog_id}/p/")
                try:
                    resp = await self.client.get(page_url)
                    if resp.status_code != 200:
                        break
                    
                    soup = BeautifulSoup(resp.text, "lxml")
                    posts = soup.select(
                        ".postTitle, .entrylistPosttitle, .dayTitle + .postTitle, .article-title, "
                        "a.postTitle2, a.entrylistItemTitle, .post-list-item-title, .vertical-article-item-title"
                    )
                    if not posts:
                        posts = soup.select("a[href*='/p/'], a[href*='/articles/']")
                    
                    if not posts:
                        break
                    
                    new_in_page = 0
                    for post in posts:
                        a_tag = post if post.name == "a" else post.find("a")
                        if not a_tag or not a_tag.get("href"):
                            continue
                        
                        url = a_tag["href"].split("?")[0].strip()
                        title = a_tag.text.strip()
                        
                        if "#" in url or not title or "评论" in title or "编辑" in title:
                            continue
                        
                        clean_url_lower = url.lower()
                        if (
                            f"/{blog_id.lower()}/p/" not in clean_url_lower
                            and f"/{blog_id.lower()}/articles/" not in clean_url_lower
                            and f"/{blog_id.lower()}/archive/" not in clean_url_lower
                        ):
                            continue
                        
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                            
                        articles.append({"id": url, "url": url, "title": title})
                        new_in_page += 1
                        
                        if self.max_articles and len(articles) >= self.max_articles:
                            return articles
                            
                    if new_in_page == 0:
                        break
                        
                    if progress_callback:
                        progress_callback(f"已发现博客园博文 {len(articles)} 篇 (第 {page} 页)...", len(articles), 0)
                    
                    next_page = soup.select(".pager, #nav_next_page, .topicListFooter, .pager a")
                    if not next_page or "下一页" not in str(next_page):
                        if page > 1:
                            break
                            
                    page += 1
                    if page > 50:
                        break
                except Exception:
                    break

        # ==========================================================
        # 4. 通道 B: 深度嗅探合集 API (collectionPosts?collectionId=...) 与侧边栏全部分类/合集
        # ==========================================================
        try:
            side_sources = []
            # 1. 尝试 ajax/sidebar-lists
            resp_lists = await self.client.get(f"https://www.cnblogs.com/{blog_id}/ajax/sidebar-lists")
            if resp_lists.status_code == 200:
                try:
                    data = resp_lists.json()
                    side_sources.append(data.get("sideColumn", ""))
                except Exception:
                    side_sources.append(resp_lists.text)

            # 2. 尝试 ajax/sidecolumn.aspx
            resp_side = await self.client.get(
                f"https://www.cnblogs.com/{blog_id}/ajax/sidecolumn.aspx",
                headers={"Referer": f"https://www.cnblogs.com/{blog_id}/"}
            )
            if resp_side.status_code == 200:
                side_sources.append(resp_side.text)

            sub_pages = []
            collection_ids = set()

            for source_html in side_sources:
                if not source_html:
                    continue
                soup_side = BeautifulSoup(source_html, "lxml")
                for a in soup_side.find_all("a"):
                    href = a.get("href", "")
                    if "/collections/" in href:
                        m_cid = re.search(r"/collections/(\d+)", href)
                        if m_cid:
                            collection_ids.add(m_cid.group(1))
                    if any(k in href for k in ("/collections/", "/category/", "/tag/", "/articles/")):
                        if href not in sub_pages:
                            sub_pages.append(href)

            # 针对合集调用官方 API 极速提取合集内文章
            for cid in collection_ids:
                try:
                    api_url = f"https://www.cnblogs.com/{blog_id}/ajax/collectionPosts?collectionId={cid}"
                    r_coll = await self.client.get(api_url)
                    if r_coll.status_code == 200:
                        posts_data = r_coll.json()
                        if isinstance(posts_data, list):
                            for p_item in posts_data:
                                p_url = p_item.get("url", "").split("?")[0].strip()
                                p_title = p_item.get("title", "").strip()
                                if p_url and p_url not in seen_urls:
                                    seen_urls.add(p_url)
                                    articles.append({"id": p_url, "url": p_url, "title": p_title or "博客园文章"})
                                    if progress_callback:
                                        progress_callback(f"已从合集提取文章 {len(articles)} 篇...", len(articles), 0)
                                    if self.max_articles and len(articles) >= self.max_articles:
                                        return articles
                except Exception:
                    pass

            # 遍历分类、标签与合集页面
            for sub_url in sub_pages:
                try:
                    for p in range(1, 10):
                        page_sub_url = f"{sub_url}?page={p}" if p > 1 else sub_url
                        resp_sub = await self.client.get(page_sub_url)
                        if resp_sub.status_code != 200:
                            break
                        soup_sub = BeautifulSoup(resp_sub.text, "lxml")
                        new_in_sub = 0
                        for a in soup_sub.find_all("a"):
                            href = a.get("href", "").split("?")[0].strip()
                            title = a.text.strip()
                            if (
                                f"/{blog_id.lower()}/" in href.lower()
                                and any(k in href for k in ("/articles/", "/p/", "/archive/"))
                                and len(title) > 2
                                and not title.isdigit()
                                and "评论" not in title
                                and "编辑" not in title
                                and "阅读全文" not in title
                            ):
                                if href not in seen_urls:
                                    seen_urls.add(href)
                                    articles.append({"id": href, "url": href, "title": title})
                                    new_in_sub += 1
                                    if progress_callback:
                                        progress_callback(f"已发现博客园分类/标签文章 {len(articles)} 篇...", len(articles), 0)
                                    if self.max_articles and len(articles) >= self.max_articles:
                                        return articles
                        if new_in_sub == 0 or "下一页" not in resp_sub.text:
                            break
                except Exception:
                    pass
        except Exception:
            pass

        # ==========================================================
        # 5. 通道 C: 深度日期归档档案扫描 (获取未加入合集与标签的独立长文)
        # ==========================================================
        try:
            import datetime
            now_year = datetime.datetime.now().year
            # 扫描近 4 年内的所有月份归档
            for y in range(now_year, now_year - 4, -1):
                for m in range(12, 0, -1):
                    archive_url = f"https://www.cnblogs.com/{blog_id}/archives/{y}/{m:02d}.html"
                    try:
                        resp_arch = await self.client.get(archive_url)
                        if resp_arch.status_code == 200 and len(resp_arch.text) > 2000 and "404" not in resp_arch.text:
                            soup_arch = BeautifulSoup(resp_arch.text, "lxml")
                            for a in soup_arch.select(".postTitle a, .postTitle2, a[href*='/articles/'], a[href*='/p/']"):
                                href = a.get("href", "").split("?")[0].strip()
                                title = a.text.strip()
                                if (
                                    f"/{blog_id.lower()}/" in href.lower()
                                    and any(k in href for k in ("/articles/", "/p/", "/archive/"))
                                    and len(title) > 2
                                    and not title.isdigit()
                                    and "评论" not in title
                                    and "编辑" not in title
                                    and "阅读全文" not in title
                                ):
                                    if href not in seen_urls:
                                        seen_urls.add(href)
                                        articles.append({"id": href, "url": href, "title": title})
                                        if progress_callback:
                                            progress_callback(f"已发现博客园归档文章 {len(articles)} 篇...", len(articles), 0)
                                        if self.max_articles and len(articles) >= self.max_articles:
                                            return articles
                    except Exception:
                        pass
        except Exception:
            pass

        # ==========================================================
        # 6. 通道 D: 用户总标签页遍历 (https://www.cnblogs.com/{blog_id}/tag/)
        # ==========================================================
        try:
            resp_tag_page = await self.client.get(f"https://www.cnblogs.com/{blog_id}/tag/")
            if resp_tag_page.status_code == 200:
                soup_tags = BeautifulSoup(resp_tag_page.text, "lxml")
                tag_links = [
                    a["href"] for a in soup_tags.find_all("a")
                    if a.get("href") and f"/{blog_id}/tag/" in a["href"] and a["href"] != f"https://www.cnblogs.com/{blog_id}/tag/"
                ]
                for t_url in tag_links:
                    try:
                        resp_t = await self.client.get(t_url)
                        if resp_t.status_code == 200:
                            soup_t = BeautifulSoup(resp_t.text, "lxml")
                            for a in soup_t.find_all("a"):
                                href = a.get("href", "").split("?")[0].strip()
                                title = a.text.strip()
                                if (
                                    f"/{blog_id.lower()}/" in href.lower()
                                    and any(k in href for k in ("/articles/", "/p/", "/archive/"))
                                    and len(title) > 2
                                    and not title.isdigit()
                                    and "评论" not in title
                                    and "编辑" not in title
                                    and "阅读全文" not in title
                                ):
                                    if href not in seen_urls:
                                        seen_urls.add(href)
                                        articles.append({"id": href, "url": href, "title": title})
                                        if progress_callback:
                                            progress_callback(f"已发现博客园标签文章 {len(articles)} 篇...", len(articles), 0)
                                        if self.max_articles and len(articles) >= self.max_articles:
                                            return articles
                    except Exception:
                        pass
        except Exception:
            pass

        # ==========================================================
        # 6. 通道 D: Atom RSS 官方订阅流兜底
        # ==========================================================
        if len(articles) <= 1:
            try:
                rss_url = f"https://www.cnblogs.com/{blog_id}/rss"
                rss_resp = await self.client.get(rss_url)
                if rss_resp.status_code == 200 and rss_resp.content:
                    root = ET.fromstring(rss_resp.content.strip())
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    entries = root.findall("atom:entry", ns)
                    for entry in entries:
                        title_el = entry.find("atom:title", ns)
                        link_el = entry.find("atom:link", ns)
                        if link_el is not None and link_el.get("href"):
                            url = link_el.get("href").split("?")[0].strip()
                            title = title_el.text.strip() if title_el is not None and title_el.text else "博客园文章"
                            if f"/{blog_id.lower()}/" in url.lower() and url not in seen_urls:
                                seen_urls.add(url)
                                articles.append({"id": url, "url": url, "title": title})
                                if self.max_articles and len(articles) >= self.max_articles:
                                    break
            except Exception:
                pass
                
        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta["url"]
        title = article_meta.get("title", "无标题")
        publish_time = ""
        author = self._extract_blog_id()
        
        try:
            resp = await self.client.get(url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                
                # 标题精准定位 (多套主题 + Title 兜底)
                title_tag = soup.select_one("#cb_post_title_url, .postTitle2, .postTitle, .article-title, h1.postTitle, #topics h1")
                if title_tag and title_tag.text.strip() and title_tag.text.strip() != author:
                    title = title_tag.text.strip()
                elif soup.title and soup.title.text.strip():
                    extracted_title = soup.title.text.split(" - ")[0].strip()
                    if extracted_title and extracted_title != author:
                        title = extracted_title
                    
                # 作者
                author_tag = soup.select_one("#Header1_HeaderTitle, #blogTitle h1, .author, .postDesc a")
                if author_tag and author_tag.text.strip():
                    author = author_tag.text.strip()

                # 发布时间
                date_tag = soup.select_one("#post-date, .postDesc, .post-desc, .date, time")
                if date_tag:
                    time_match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", date_tag.text)
                    if time_match:
                        publish_time = time_match.group(0)
                        
                # 正文区域
                content_tag = soup.select_one("#cnblogs_post_body, .blogpost-body, .article-content, #BlogPostDescription")
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
                    platform="博客园",
                    content_html=cleaned_html,
                    content_markdown=md_content,
                    images=images
                )
        except Exception:
            pass
            
        return ArticleItem(
            id=url,
            title=title,
            author=author,
            publish_time=publish_time,
            url=url,
            platform="博客园",
            content_html="<p>内容获取异常，请稍后重试</p>",
            content_markdown="内容获取异常，请稍后重试",
            images=[]
        )
