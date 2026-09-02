import asyncio
import uuid
import datetime
import re
from typing import Dict, List, Optional
from app.config import OUTPUT_DIR
from app.models import (
    TaskCreateRequest, TaskProgress, TaskStatusEnum, PlatformEnum, ExportFormatEnum, ArticleItem
)
from app.scrapers.base import BaseScraper
from app.scrapers.cnblogs import CNBlogsScraper
from app.scrapers.juejin import JuejinScraper
from app.scrapers.csdn import CSDNScraper
from app.scrapers.cto51 import CTO51Scraper
from app.scrapers.zhihu import ZhihuScraper
from app.scrapers.weibo import WeiboScraper
from app.scrapers.wechat import WeChatScraper
from app.scrapers.custom_urls import CustomURLsScraper

from app.exporters.md_exporter import MarkdownExporter
from app.exporters.html_exporter import HTMLExporter
from app.exporters.txt_exporter import TxtExporter
from app.exporters.docx_exporter import DocxExporter
from app.exporters.pdf_exporter import PDFExporter
from app.exporters.zip_exporter import ZipExporter
from app.core.cache import get_cached_article, save_cached_article

class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, TaskProgress] = {}
        self.subscribers: Dict[str, List[asyncio.Queue]] = {}
        self.decision_events: Dict[str, asyncio.Event] = {}
        self.user_decisions: Dict[str, str] = {}

    def get_task(self, task_id: str) -> Optional[TaskProgress]:
        return self.tasks.get(task_id)

    def list_tasks(self) -> List[TaskProgress]:
        return sorted(list(self.tasks.values()), key=lambda t: t.created_at, reverse=True)

    async def subscribe(self, task_id: str) -> asyncio.Queue:
        if task_id not in self.subscribers:
            self.subscribers[task_id] = []
        queue = asyncio.Queue()
        self.subscribers[task_id].append(queue)
        # 立即发送当前状态
        if task_id in self.tasks:
            await queue.put(self.tasks[task_id].model_dump_json())
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue):
        if task_id in self.subscribers and queue in self.subscribers[task_id]:
            self.subscribers[task_id].remove(queue)

    async def _broadcast(self, task_id: str):
        if task_id in self.tasks and task_id in self.subscribers:
            data = self.tasks[task_id].model_dump_json()
            for q in list(self.subscribers[task_id]):
                try:
                    await q.put(data)
                except Exception:
                    pass

    def cancel_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        task.is_cancelled = True
        task.status = TaskStatusEnum.CANCELLED
        task.message = "🛑 任务已由用户手动终止"
        if task_id in self.decision_events:
            self.decision_events[task_id].set()
        asyncio.create_task(self._broadcast(task_id))
        return True

    async def handle_decision(self, task_id: str, action: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        self.user_decisions[task_id] = action
        if task_id in self.decision_events:
            self.decision_events[task_id].set()
            return True
        return False

    def create_task(self, request: TaskCreateRequest) -> str:
        task_id = str(uuid.uuid4())[:8]
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        detected_platform = self._detect_platform(request.target, request.platform)
        progress = TaskProgress(
            task_id=task_id,
            platform=detected_platform.value,
            target=request.target,
            status=TaskStatusEnum.PENDING,
            created_at=now_str,
            message="任务已创建，准备抓取..."
        )
        self.tasks[task_id] = progress
        
        # 启动异步后台任务
        asyncio.create_task(self._run_task(task_id, request))
        return task_id

    def _detect_platform(self, target: str, fallback: PlatformEnum) -> PlatformEnum:
        """根据 URL 规则智能识别平台 (若包含多条 URL 则自动转为自定义聚合模式)"""
        urls = re.findall(r'https?://[^\s,"\'<>]+', target)
        if len(urls) > 1:
            return PlatformEnum.CUSTOM_URLS

        t = target.lower()
        if "csdn.net" in t:
            return PlatformEnum.CSDN
        elif "juejin.cn" in t:
            return PlatformEnum.JUEJIN
        elif "zhihu.com" in t:
            return PlatformEnum.ZHIHU
        elif "cnblogs.com" in t:
            return PlatformEnum.CNBLOGS
        elif "51cto.com" in t:
            return PlatformEnum.CTO51
        elif "weibo.com" in t or "weibo.cn" in t:
            return PlatformEnum.WEIBO
        elif "weixin.qq.com" in t:
            return PlatformEnum.WECHAT
        return fallback

    def _get_scraper(self, request: TaskCreateRequest, detected_platform: PlatformEnum) -> BaseScraper:
        platform = detected_platform
        if platform == PlatformEnum.CNBLOGS:
            return CNBlogsScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        elif platform == PlatformEnum.JUEJIN:
            return JuejinScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        elif platform == PlatformEnum.CSDN:
            return CSDNScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        elif platform == PlatformEnum.CTO51:
            return CTO51Scraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        elif platform == PlatformEnum.ZHIHU:
            return ZhihuScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        elif platform == PlatformEnum.WEIBO:
            return WeiboScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        elif platform == PlatformEnum.WECHAT:
            return WeChatScraper(
                request.target,
                request.enable_noise_filter,
                request.max_articles,
                cookie=request.wechat_cookie,
                token=request.wechat_token,
                remove_image_watermark=request.remove_image_watermark,
                uin=request.wechat_uin,
                key=request.wechat_key,
                pass_ticket=request.wechat_pass_ticket,
                appmsg_token=request.wechat_appmsg_token,
                include_comments=request.include_comments
            )
        elif platform == PlatformEnum.CUSTOM_URLS:
            return CustomURLsScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        else:
            return CustomURLsScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)

    async def _run_task(self, task_id: str, request: TaskCreateRequest):
        task = self.tasks[task_id]
        detected_platform = self._detect_platform(request.target, request.platform)
        task.platform = detected_platform.value
        scraper = self._get_scraper(request, detected_platform)
        
        try:
            if task.is_cancelled:
                task.status = TaskStatusEnum.CANCELLED
                await self._broadcast(task_id)
                await scraper.close()
                return

            # 1. 获取作者基本信息
            task.status = TaskStatusEnum.FETCHING_LIST
            task.message = "正在连接目标平台并获取博主信息..."
            await self._broadcast(task_id)
            
            author_info = await scraper.get_author_info()
            author_name = request.author_name_override or author_info.get("name") or "目标博主"
            task.author_name = author_name
            
            if task.is_cancelled:
                task.status = TaskStatusEnum.CANCELLED
                await self._broadcast(task_id)
                await scraper.close()
                return

            # 2. 遍历获取文章列表
            task.message = f"正在检索博主 [{author_name}] 的文章列表..."
            await self._broadcast(task_id)
            
            def list_progress_cb(msg: str, count: int, _: int):
                if task.is_cancelled:
                    return
                task.message = msg
                task.total_articles = count
                asyncio.create_task(self._broadcast(task_id))

            article_list = await scraper.get_article_list(list_progress_cb)
            
            if task.is_cancelled:
                task.status = TaskStatusEnum.CANCELLED
                await self._broadcast(task_id)
                await scraper.close()
                return

            if not article_list:
                task.status = TaskStatusEnum.FAILED
                if author_name and author_name != "未知博主":
                    task.message = f"已成功连接博主【{author_name}】，但该博主主页尚未公开发布任何文章。"
                    task.error_message = f"博主【{author_name}】暂无公开博文"
                else:
                    task.message = "未找到任何可抓取的文章，请检查链接或输入的目标ID。"
                    task.error_message = "文章列表为空"
                await self._broadcast(task_id)
                await scraper.close()
                return

            total_discovered = len(article_list)
            # 切片处理指定范围 (例如第 10 篇 到 第 30 篇)
            start_idx = max(1, request.start_index or 1)
            end_idx = min(total_discovered, request.end_index) if request.end_index else total_discovered
            if request.max_articles:
                end_idx = min(end_idx, start_idx + request.max_articles - 1)
            
            if start_idx > total_discovered:
                start_idx = total_discovered
            if start_idx > end_idx:
                end_idx = start_idx

            article_list = article_list[start_idx - 1 : end_idx]

            task.total_articles = len(article_list)
            task.status = TaskStatusEnum.SCRAPING_ARTICLES
            task.message = f"共选定 {len(article_list)} 篇目标文章 (全量检索到 {total_discovered} 篇)，开始抓取..."
            await self._broadcast(task_id)

            # 3. 批量抓取单篇正文 (支持断点续爬与本地缓存)
            scraped_articles: List[ArticleItem] = []
            for idx, meta in enumerate(article_list, 1):
                if task.is_cancelled:
                    task.status = TaskStatusEnum.CANCELLED
                    await self._broadcast(task_id)
                    await scraper.close()
                    return

                task.current_article_index = idx
                task.current_article_title = meta.get("title", f"第 {idx} 篇")
                task.progress_percent = round((idx / len(article_list)) * 75.0, 1) # 抓取占 75%

                article_url = meta.get("url", "")
                cached_item = get_cached_article(article_url) if request.use_cache else None

                # 仅当缓存内容完整、无破损乱码且非失败/残缺提示时才命中缓存
                is_stale_cache = (
                    cached_item is not None
                    and (
                        "\\x0a" in cached_item.content_markdown
                        or "\\x26" in cached_item.content_markdown
                        or "visibility: hidden" in cached_item.content_html
                        or "\n" in cached_item.author
                        or (cached_item.platform in ["weibo", "微博"] and cached_item.content_markdown.strip() == "转发微博" and not cached_item.images)
                        or cached_item.is_failed
                    )
                )
                is_valid_cache = (
                    cached_item is not None
                    and not is_stale_cache
                    and (bool(cached_item.content_markdown.strip()) or bool(cached_item.images))
                    and "抓取失败" not in cached_item.content_markdown
                    and "内容获取异常" not in cached_item.content_markdown
                )

                if is_valid_cache:
                    task.message = f"[缓存命中] ({idx}/{len(article_list)}): {task.current_article_title[:22]}..."
                    await self._broadcast(task_id)
                    article_item = cached_item
                else:
                    task.message = f"正在抓取 ({idx}/{len(article_list)}): {task.current_article_title[:22]}..."
                    await self._broadcast(task_id)
                    article_item = await scraper.scrape_article_detail(meta)
                    if (
                        request.use_cache
                        and article_item.content_html
                        and not article_item.is_failed
                        and "抓取失败" not in article_item.content_markdown
                        and "内容获取异常" not in article_item.content_markdown
                        and (bool(article_item.content_markdown.strip()) or bool(article_item.images))
                    ):
                        save_cached_article(article_item)
                    # 轻量延时防限频
                    await asyncio.sleep(0.2)

                # 如果是公众号文章，智能在标题前附带公众号名称
                if (article_item.platform in ["微信公众号", "wechat"] or "weixin.qq.com" in article_item.url):
                    art_auth = article_item.author.strip()
                    if art_auth and art_auth not in ["微信公众号", "未知", ""]:
                        if not article_item.title.startswith(f"【{art_auth}】") and not article_item.title.startswith(f"[{art_auth}]"):
                            article_item.title = f"【{art_auth}】{article_item.title}"

                scraped_articles.append(article_item)

                is_success = bool(
                    not article_item.is_failed
                    and (bool(article_item.content_markdown.strip()) or bool(article_item.images))
                    and "抓取失败" not in article_item.content_markdown
                    and "内容获取异常" not in article_item.content_markdown
                )

                task.articles_meta.append({
                    "id": article_item.id,
                    "index": idx,
                    "title": article_item.title or task.current_article_title,
                    "url": article_item.url or article_url,
                    "status": "success" if is_success else "failed",
                    "error_reason": article_item.error_reason if not is_success else None,
                    "is_cached": is_valid_cache,
                    "publish_time": article_item.publish_time,
                    "images_count": len(article_item.images),
                    "words_count": len(article_item.content_markdown.strip())
                })
                await self._broadcast(task_id)

            await scraper.close()

            if task.is_cancelled:
                task.status = TaskStatusEnum.CANCELLED
                await self._broadcast(task_id)
                return

            # 4. 统计与失败篇目交互校验
            success_items = [a for a in scraped_articles if not a.is_failed and "抓取失败" not in a.content_markdown]
            failed_items = [a for a in scraped_articles if a.is_failed or "抓取失败" in a.content_markdown]

            task.success_articles = [
                {"title": a.title, "url": a.url, "publish_time": a.publish_time} for a in success_items
            ]
            task.failed_articles = [
                {"title": a.title, "url": a.url, "error_reason": a.error_reason or "未能解析出有效正文"} for a in failed_items
            ]

            # 如果存在失败篇目且有部分成功篇目，进入确认等待状态
            if len(failed_items) > 0 and len(success_items) > 0:
                task.status = TaskStatusEnum.WAITING_CONFIRMATION
                task.progress_percent = 78.0
                task.message = f"抓取阶段完成：{len(success_items)} 篇成功，{len(failed_items)} 篇失败。等待用户确认..."
                await self._broadcast(task_id)

                self.decision_events[task_id] = asyncio.Event()
                await self.decision_events[task_id].wait()

                if task.is_cancelled:
                    task.status = TaskStatusEnum.CANCELLED
                    task.message = "🛑 任务已由用户手动终止"
                    await self._broadcast(task_id)
                    return

                decision = self.user_decisions.get(task_id, "skip_and_export")
                if decision == "cancel":
                    task.status = TaskStatusEnum.CANCELLED
                    task.message = "🛑 用户取消了本次任务"
                    await self._broadcast(task_id)
                    return
                elif decision == "retry_failed":
                    # 重试失败篇目
                    task.status = TaskStatusEnum.SCRAPING_ARTICLES
                    task.message = f"正在重新尝试抓取 {len(failed_items)} 篇失败文章..."
                    await self._broadcast(task_id)

                    retried_success = []
                    for f_item in failed_items:
                        try:
                            # 重新抓取
                            retry_scraper = self._get_scraper(request, detected_platform)
                            r_item = await retry_scraper.scrape_article_detail({"url": f_item.url, "title": f_item.title})
                            await retry_scraper.close()
                            if not r_item.is_failed and "抓取失败" not in r_item.content_markdown:
                                retried_success.append(r_item)
                        except Exception:
                            pass

                    success_items.extend(retried_success)
                    scraped_articles = success_items
                else: # skip_and_export
                    scraped_articles = success_items
            elif len(success_items) == 0:
                task.status = TaskStatusEnum.FAILED
                err_text = failed_items[0].error_reason if failed_items else "未能获取到任何文章正文"
                task.error_message = err_text
                task.message = f"❌ 全部文章均未能成功获取正文: {err_text}"
                await self._broadcast(task_id)
                return
            else:
                scraped_articles = success_items

            if not scraped_articles:
                task.status = TaskStatusEnum.FAILED
                task.message = "❌ 没有可供导出的成功文章"
                await self._broadcast(task_id)
                return

            # 5. 智能提炼合集作者与平台名称
            valid_authors = [a.author for a in scraped_articles if a.author and a.author not in ["互联网博主", "微信公众号", "未知", "", "未知博主", "Blogger"]]
            valid_platforms = [a.platform for a in scraped_articles if a.platform and a.platform not in ["多平台", "多源聚合", "自定义", "", "custom_urls"]]
            
            if not request.author_name_override:
                if valid_authors:
                    top_author = max(set(valid_authors), key=valid_authors.count)
                    if len(set(valid_authors)) == 1:
                        author_name = top_author
                    else:
                        author_name = f"{top_author}等"
                    task.author_name = author_name

            platform_str = task.platform
            if valid_platforms:
                top_platform = max(set(valid_platforms), key=valid_platforms.count)
                if top_platform in ["微信公众号", "wechat"]:
                    platform_str = "微信公众号"
                    task.platform = "微信公众号"
                elif top_platform in ["CSDN", "csdn"]:
                    platform_str = "CSDN"
                elif top_platform in ["知乎", "zhihu"]:
                    platform_str = "知乎"
                elif top_platform in ["掘金", "juejin"]:
                    platform_str = "掘金"
                elif top_platform in ["博客园", "cnblogs"]:
                    platform_str = "博客园"
                elif top_platform in ["51CTO", "51cto"]:
                    platform_str = "51CTO"
                elif top_platform in ["微博", "weibo"]:
                    platform_str = "微博"

            # 6. 开始合并与排版导出
            task.status = TaskStatusEnum.EXPORTING
            task.message = f"文章抓取完毕 (共 {len(scraped_articles)} 篇有效文章)，正在排版合并并导出目标格式..."
            task.progress_percent = 82.0
            await self._broadcast(task_id)

            safe_author = re.sub(r'[\\/:*?"<>|]', '_', author_name).strip() or "Blogger"
            safe_platform = re.sub(r'[\\/:*?"<>|]', '_', platform_str).strip() or "platform"
            filename_prefix = f"{safe_author}_{safe_platform}_{task_id}"

            export_files = {}
            generated_paths = {}

            total_formats = len(request.export_formats)
            for idx, fmt in enumerate(request.export_formats, 1):
                if task.is_cancelled:
                    task.status = TaskStatusEnum.CANCELLED
                    await self._broadcast(task_id)
                    return

                task.progress_percent = round(82.0 + (idx / total_formats) * 14.0, 1)
                task.message = f"正在生成 {fmt.value.upper()} 格式文档 ({idx}/{total_formats})..."
                await self._broadcast(task_id)

                try:
                    if fmt == ExportFormatEnum.MARKDOWN:
                        exporter = MarkdownExporter(author_name, platform_str, OUTPUT_DIR)
                        out_path = await exporter.export(scraped_articles, filename_prefix)
                        export_files["md"] = f"/api/download/{out_path.name}"
                        generated_paths["md"] = out_path

                    elif fmt == ExportFormatEnum.HTML:
                        exporter = HTMLExporter(author_name, platform_str, OUTPUT_DIR)
                        out_path = await exporter.export(scraped_articles, filename_prefix)
                        export_files["html"] = f"/api/download/{out_path.name}"
                        generated_paths["html"] = out_path

                    elif fmt == ExportFormatEnum.TXT:
                        exporter = TxtExporter(author_name, platform_str, OUTPUT_DIR)
                        out_path = await exporter.export(scraped_articles, filename_prefix)
                        export_files["txt"] = f"/api/download/{out_path.name}"
                        generated_paths["txt"] = out_path

                    elif fmt == ExportFormatEnum.WORD:
                        exporter = DocxExporter(author_name, platform_str, OUTPUT_DIR)
                        out_path = await exporter.export(scraped_articles, filename_prefix)
                        export_files["docx"] = f"/api/download/{out_path.name}"
                        generated_paths["docx"] = out_path

                    elif fmt == ExportFormatEnum.PDF:
                        exporter = PDFExporter(author_name, platform_str, OUTPUT_DIR)
                        out_path = await exporter.export(scraped_articles, filename_prefix)
                        export_files["pdf"] = f"/api/download/{out_path.name}"
                        generated_paths["pdf"] = out_path
                except Exception as export_err:
                    print(f"导出格式 {fmt.value} 失败: {export_err}")

            # 7. 自动生成全量 ZIP 归档包
            try:
                task.message = "正在打包全量 ZIP 归档包 (含合并文档 + 分篇独立文章与目录清单)..."
                task.progress_percent = 98.0
                await self._broadcast(task_id)

                zip_exporter = ZipExporter(author_name, platform_str, OUTPUT_DIR)
                zip_path = await zip_exporter.export(
                    scraped_articles,
                    filename_prefix,
                    generated_paths,
                    download_images=request.download_images
                )
                export_files["zip"] = f"/api/download/{zip_path.name}"
            except Exception as zip_err:
                print(f"生成 ZIP 压缩包失败: {zip_err}")

            # 8. 完成
            task.status = TaskStatusEnum.COMPLETED
            task.progress_percent = 100.0
            task.export_files = export_files
            task.completed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            task.message = f"🎉 抓取与导出全部完成！共合并 {len(scraped_articles)} 篇有效文章。"
            await self._broadcast(task_id)

        except Exception as e:
            task.status = TaskStatusEnum.FAILED
            task.error_message = str(e)
            task.message = f"❌ 执行过程中发生错误: {str(e)}"
            await self._broadcast(task_id)
            try:
                await scraper.close()
            except Exception:
                pass

task_manager = TaskManager()
