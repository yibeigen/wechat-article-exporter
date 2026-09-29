import asyncio
import json
import math
import uuid
import datetime
import re
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from app.config import OUTPUT_DIR, BASE_DIR
from app.models import (
    TaskCreateRequest, TaskProgress, TaskStatusEnum, PlatformEnum, ExportFormatEnum, ArticleItem
)
from app.scrapers.base import BaseScraper
from app.scrapers.custom_urls import dedupe_wechat_title
from app.scrapers.cnblogs import CNBlogsScraper
from app.scrapers.juejin import JuejinScraper
from app.scrapers.csdn import CSDNScraper
from app.scrapers.cto51 import CTO51Scraper
from app.scrapers.zhihu import ZhihuScraper
from app.scrapers.weibo import WeiboScraper
from app.scrapers.wechat import WeChatScraper
from app.scrapers.sina_blog import SinaBlogScraper
from app.scrapers.jianshu import JianshuScraper
from app.scrapers.custom_urls import CustomURLsScraper

from app.exporters.md_exporter import MarkdownExporter
from app.exporters.html_exporter import HTMLExporter
from app.exporters.txt_exporter import TxtExporter
from app.exporters.docx_exporter import DocxExporter
from app.exporters.pdf_exporter import PDFExporter
from app.exporters.original_html_exporter import OriginalHTMLExporter
from app.exporters.zip_exporter import ZipExporter
from app.cleaners.html_cleaner import clean_html_content
from app.core.cache import get_cached_article, save_cached_article
from app.core.image_helper import clear_image_caches

class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, TaskProgress] = {}
        self.subscribers: Dict[str, List[asyncio.Queue]] = {}
        self.decision_events: Dict[str, asyncio.Event] = {}
        self.pause_events: Dict[str, asyncio.Event] = {}
        self.user_decisions: Dict[str, str] = {}

        # ===== 中断续跑快照存储 =====
        # 任务状态本来是纯内存的(self.tasks)，一旦服务/进程被杀、重启就全部丢失，
        # 正在运行的抓取任务"凭空消失"。这里把每个任务的关键信息(原始请求+进度摘要)
        # 额外固化为磁盘 JSON 快照，重启后凭快照就能检测到"上次没跑完的任务"并询问用户是否续跑。
        # 续跑的核心机制：已抓成功的文章早已写入 SQLite 缓存(见 app/core/cache.py)，
        # 用同一请求重新启动任务时，爬虫会命中缓存"秒过"，不重复下载，只继续抓没抓到的。
        self._snap_dir = BASE_DIR / "data" / "resume"   # 快照存放目录
        self._serialized_requests: Dict[str, Dict] = {}  # task_id -> 序列化后的原始请求(供快照复用，避免每篇文章都重新序列化)

    # ---------- 续跑快照：写盘 / 删盘 辅助函数 ----------
    def _snapshot_path(self, task_id: str) -> Path:
        """快照正式文件路径 (存在即代表任务尚未跑完/被中断)"""
        return self._snap_dir / f"{task_id}.json"

    def _snapshot_tmp_path(self, task_id: str) -> Path:
        """快照临时文件路径 (先写临时文件再原子改名，避免写一半读到损坏内容)"""
        return self._snap_dir / f"{task_id}.tmp"

    def _save_snapshot(self, task: TaskProgress):
        """把任务的续跑快照写入磁盘(原子替换)。

        快照里存的是「原始请求 + 当前进度摘要」，服务重启后调用 resume 时，
        用这份快照重建同一个任务继续跑。未到终态的快照会一直保留，
        一旦任务正常结束(完成/失败/取消)就在 finally 里删除，不再当"残留任务"。
        """
        req = self._serialized_requests.get(task.task_id)
        if req is None:
            # 没有存过原始请求(正常不会发生)，说明无法续跑，干脆不写快照
            return
        snap = {
            "task_id": task.task_id,
            "client_id": task.client_id,
            "platform": task.platform,
            "target": task.target,
            "author": task.author_name,
            "total_articles": task.total_articles,
            "current_article_index": task.current_article_index,
            "message": task.message,
            "phase": task.status.value,  # 记录跑到哪个阶段，便于前端展示
            "request": req,              # 重建任务所需的全部原始参数
            "created_at": task.created_at,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            self._snap_dir.mkdir(parents=True, exist_ok=True)
            # 惯例：先写 .tmp 再原子替换，防止崩溃时留下半个 JSON
            tmp = self._snapshot_tmp_path(task.task_id)
            final = self._snapshot_path(task.task_id)
            tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
            tmp.replace(final)
        except Exception as e:
            print(f"⚠️ 写入续跑快照失败: {e}")

    def _remove_snapshot(self, task_id: str):
        """删除续跑快照(任务到达终态，或用户选择"放弃续跑"时调用)。"""
        try:
            for p in (self._snapshot_path(task_id), self._snapshot_tmp_path(task_id)):
                if p.exists():
                    p.unlink()
        except Exception as e:
            print(f"⚠️ 删除续跑快照失败: {e}")

    def _read_snapshot(self, task_id: str) -> Optional[Dict]:
        """读取单个快照；文件不存在或损坏时返回 None。"""
        p = self._snapshot_path(task_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def get_task(self, task_id: str) -> Optional[TaskProgress]:
        return self.tasks.get(task_id)

    def list_tasks(self, client_id: Optional[str] = None) -> List[TaskProgress]:
        if not client_id or not client_id.strip():
            # 未提供 client_id 时不公开暴露全局所有人的任务，防止公网隐私泄露
            return []
        matching = [t for t in self.tasks.values() if t.client_id == client_id.strip()]
        return sorted(matching, key=lambda t: t.created_at, reverse=True)

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
        task.is_paused = False
        task.status = TaskStatusEnum.CANCELLED
        task.message = "🛑 任务已由用户手动终止"
        if task_id in self.decision_events:
            self.decision_events[task_id].set()
        if task_id in self.pause_events:
            self.pause_events[task_id].set()
        asyncio.create_task(self._broadcast(task_id))
        return True

    def pause_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.is_cancelled:
            return False
        if task.status not in [TaskStatusEnum.SCRAPING_ARTICLES, TaskStatusEnum.FETCHING_LIST]:
            return False
        task.is_paused = True
        task.status = TaskStatusEnum.PAUSED
        task.message = f"⏸️ 任务已暂停 (当前第 {task.current_article_index}/{task.total_articles} 篇)，点击「继续运行」可随时恢复"
        if task_id in self.pause_events:
            self.pause_events[task_id].clear()
        asyncio.create_task(self._broadcast(task_id))
        return True

    def resume_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or not task.is_paused:
            return False
        task.is_paused = False
        task.status = TaskStatusEnum.SCRAPING_ARTICLES
        task.message = f"▶️ 任务已恢复运行，继续推进抓取 ({task.current_article_index}/{task.total_articles})..."
        if task_id in self.pause_events:
            self.pause_events[task_id].set()
        asyncio.create_task(self._broadcast(task_id))
        return True

    def stop_and_export(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.is_cancelled or task.status in [TaskStatusEnum.COMPLETED, TaskStatusEnum.FAILED, TaskStatusEnum.EXPORTING]:
            return False
        task.is_interrupted_to_export = True
        task.is_paused = False
        task.message = "⚡ 用户指令提前截断，正在整理已抓取的博文并直接开始合并排版导出..."
        if task_id in self.pause_events:
            self.pause_events[task_id].set()
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
        """新发起一个抓取导出任务（生成新 task_id 后交给 _launch 公共启动逻辑）。"""
        task_id = str(uuid.uuid4())[:8]
        return self._launch(task_id, request)

    def _launch(self, task_id: str, request: TaskCreateRequest) -> str:
        """任务公共启动逻辑：建进度、写快照、拉起后台跑。新建任务与中断续跑共用。"""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        detected_platform = self._detect_platform(request.target, request.platform)
        # 若前端传入了已选文章元数据，说明用户从「检索清单弹窗」勾选了具体篇目，
        # 此时必须沿用用户选定的平台（如知乎），避免 URL 多条被误判为 custom_urls 丢失分类信息
        if request.articles_meta and len(request.articles_meta) > 0:
            detected_platform = request.platform
        progress = TaskProgress(
            task_id=task_id,
            client_id=request.client_id,
            platform=detected_platform.value,
            target=request.target,
            status=TaskStatusEnum.PENDING,
            created_at=now_str,
            message="任务已创建，准备抓取...",
            scrape_source=request.scrape_source or "server"
        )
        self.tasks[task_id] = progress

        # 序列化并缓存原始请求，供快照写盘与中断续跑重建使用 (mode="json" 让枚举转成字符串)
        self._serialized_requests[task_id] = request.model_dump(mode="json")
        # 写一份初始快照：此刻快照存在 = 该任务尚未完成，重启后可作为"残留任务"提示续跑
        self._save_snapshot(progress)

        # 启动异步后台任务
        asyncio.create_task(self._run_task(task_id, request))
        return task_id

    # =========================================================
    # 中断续跑：检测残留 / 续跑 / 丢弃
    # =========================================================
    def list_incomplete_tasks(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出当前客户端所有"未跑完"的残留任务(从磁盘快照恢复，非内存)。

        这些任务要么是服务重启/进程被杀后丢失的，要么是浏览器关闭后服务端还在跑、
        但用户主动想从中断处继续的。返回给前端，用于启动时弹"是否继续?"确认框。
        """
        results: List[Dict[str, Any]] = []
        if not self._snap_dir.exists():
            return results
        # 终态枚举：到达这些状态的任务它自己的 finally 会删快照，这里再防御一次
        terminal = (TaskStatusEnum.COMPLETED, TaskStatusEnum.FAILED, TaskStatusEnum.CANCELLED)
        for p in self._snap_dir.glob("*.json"):
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                # 快照损坏无法解析，直接删掉，别让脏文件反复弹窗
                self._remove_snapshot(p.stem)
                continue
            task_id = snap.get("task_id")
            if not task_id:
                self._remove_snapshot(p.stem)
                continue
            # 权限隔离：只能看到自己客户端的残留任务，防止公网多用户串台
            if client_id and snap.get("client_id") != client_id.strip():
                continue
            # 该任务还在内存里正常运行(未到终态) => 不是残留，不打扰
            if task_id in self.tasks and self.tasks[task_id].status not in terminal:
                continue
            # 若任务其实已结束但快照占着没删掉(异常边角)，顺手清掉
            if task_id in self.tasks and self.tasks[task_id].status in terminal:
                self._remove_snapshot(task_id)
                continue
            results.append({
                "task_id": task_id,
                "platform": snap.get("platform"),
                "target": snap.get("target"),
                "author": snap.get("author"),
                "total_articles": snap.get("total_articles", 0),
                "current_article_index": snap.get("current_article_index", 0),
                "message": snap.get("message"),
                "phase": snap.get("phase"),
                "created_at": snap.get("created_at"),
            })
        # 按创建时间倒序，最新中断的排最前
        results.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return results

    def resume_incomplete_task(self, task_id: str, client_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """从磁盘快照重建任务并续跑。返回 (task_id, error)；error 不为空表示失败。

        续跑原理：快照里保存了完整原始请求，直接用同一请求重新启动任务。
        已抓成功的文章已写入 SQLite 缓存，重新跑时会命中缓存"秒过"，不重复下载，
        只有真正还没抓到(或上次失败的)篇目才会重新访问网络。
        """
        p = self._snapshot_path(task_id)
        if not p.exists():
            return None, "未找到对应的未完成任务"
        snap = self._read_snapshot(task_id)
        if not snap:
            return None, "续跑快照已损坏，无法恢复"
        # 权限校验：只能续跑自己客户端的任务
        if client_id and snap.get("client_id") != client_id.strip():
            return None, "无权限续跑该任务"
        req_data = snap.get("request")
        if not req_data:
            return None, "快照缺少原始请求，无法续跑"
        # 冗余保护：如果该任务当前已在内存中正常运行，拒绝重复创建(避免双跑)
        if task_id in self.tasks and self.tasks[task_id].status not in (
            TaskStatusEnum.COMPLETED, TaskStatusEnum.FAILED, TaskStatusEnum.CANCELLED):
            return task_id, None
        try:
            request = TaskCreateRequest.model_validate(req_data)
            # 续跑必须命中缓存，否则之前已抓好的文章会被重复下载，断点续跑就失去意义了
            request.use_cache = True
        except Exception as e:
            return None, f"原始请求重建失败: {e}"
        # 复用同一个 task_id 继续跑，让前端看到连贯的进度
        self._launch(task_id, request)
        return task_id, None

    def discard_incomplete_task(self, task_id: str, client_id: Optional[str] = None) -> bool:
        """用户选择"不再继续"，删除残留任务的快照，并在内存任务还在跑时顺手取消。"""
        if client_id:
            snap = self._read_snapshot(task_id)
            if snap and snap.get("client_id") != client_id.strip():
                return False
        self._remove_snapshot(task_id)
        # 若该任务内存里其实还在跑(例如浏览器刷新看到的残留)，一并取消，避免后台白耗资源
        if task_id in self.tasks and not self.tasks[task_id].is_cancelled:
            self.cancel_task(task_id)
        return True

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
        elif "blog.sina.com.cn" in t:
            return PlatformEnum.SINA_BLOG
        elif "jianshu.com" in t:
            return PlatformEnum.JIANSHU
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
        elif platform == PlatformEnum.SINA_BLOG:
            return SinaBlogScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
        elif platform == PlatformEnum.JIANSHU:
            return JianshuScraper(request.target, request.enable_noise_filter, request.max_articles, remove_image_watermark=request.remove_image_watermark)
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

    async def _export_batch(
        self,
        task: TaskProgress,
        request: TaskCreateRequest,
        articles: List[ArticleItem],
        filename_prefix: str,
        author_name: str,
        platform_str: str,
        formats: List[ExportFormatEnum],
        progress_base: float,
        progress_span: float,
        batch_tag: str = ""
    ) -> Tuple[Optional[Dict[str, str]], List[str]]:
        """导出一批文章的指定格式并打 ZIP 归档包。

        返回 (export_files, export_errors)；若任务被取消则返回 (None, [])。
        PDF 多卷拆分产物会一并纳入 generated_paths 写入 ZIP。
        """
        task_id = task.task_id
        export_files: Dict[str, str] = {}
        generated_paths: Dict[str, Any] = {}
        export_errors: List[str] = []

        total_formats = len(formats)
        for idx, fmt in enumerate(formats, 1):
            if task.is_cancelled:
                return None, export_errors

            task.progress_percent = round(progress_base + (idx / (total_formats + 1)) * progress_span, 1)
            task.message = f"{batch_tag}正在生成 {fmt.value.upper()} 格式文档 ({idx}/{total_formats})..."
            await self._broadcast(task_id)

            try:
                if fmt == ExportFormatEnum.MARKDOWN:
                    exporter = MarkdownExporter(author_name, platform_str, OUTPUT_DIR)
                    out_path = await exporter.export(articles, filename_prefix)
                    export_files["md"] = f"/api/download/{out_path.name}"
                    generated_paths["md"] = out_path

                elif fmt == ExportFormatEnum.HTML:
                    exporter = HTMLExporter(author_name, platform_str, OUTPUT_DIR)
                    out_path = await exporter.export(articles, filename_prefix, download_images=request.image_mode != "none")
                    export_files["html"] = f"/api/download/{out_path.name}"
                    generated_paths["html"] = out_path

                elif fmt == ExportFormatEnum.ORIGINAL_HTML:
                    exporter = OriginalHTMLExporter(author_name, platform_str, OUTPUT_DIR)
                    out_path = await exporter.export(articles, filename_prefix, download_images=request.image_mode != "none")
                    export_files["original_html"] = f"/api/download/{out_path.name}"
                    generated_paths["original_html"] = out_path

                elif fmt == ExportFormatEnum.TXT:
                    exporter = TxtExporter(author_name, platform_str, OUTPUT_DIR)
                    out_path = await exporter.export(articles, filename_prefix)
                    export_files["txt"] = f"/api/download/{out_path.name}"
                    generated_paths["txt"] = out_path

                elif fmt == ExportFormatEnum.WORD:
                    exporter = DocxExporter(author_name, platform_str, OUTPUT_DIR)
                    out_path = await exporter.export(articles, filename_prefix, image_mode=request.image_mode)
                    export_files["docx"] = f"/api/download/{out_path.name}"
                    generated_paths["docx"] = out_path

                elif fmt == ExportFormatEnum.PDF:
                    exporter = PDFExporter(author_name, platform_str, OUTPUT_DIR)
                    out_path = await exporter.export(articles, filename_prefix, download_images=request.image_mode != "none")
                    export_files["pdf"] = f"/api/download/{out_path.name}"
                    # 多卷拆分：后续卷一并写入 ZIP 的合并总文档目录
                    extra_vols = list(getattr(exporter, "extra_outputs", []) or [])
                    generated_paths["pdf"] = [out_path] + extra_vols if extra_vols else out_path
            except Exception as export_err:
                err_msg = f"导出格式 {fmt.value} 失败: {export_err}"
                print(err_msg)
                export_errors.append(err_msg)

        if task.is_cancelled:
            return None, export_errors

        # 打包本批 ZIP 归档包
        try:
            task.message = f"{batch_tag}正在打包 ZIP 归档包 (含合并文档 + 分篇独立文章与目录清单)..."
            task.progress_percent = round(progress_base + progress_span * 0.96, 1)
            await self._broadcast(task_id)

            async def _zip_progress_cb(msg: str):
                task.message = f"{batch_tag}{msg}"
                await self._broadcast(task_id)

            zip_exporter = ZipExporter(author_name, platform_str, OUTPUT_DIR)
            zip_path = await zip_exporter.export(
                articles,
                filename_prefix,
                generated_paths,
                image_mode=request.image_mode,
                progress_callback=_zip_progress_cb
            )
            export_files["zip"] = f"/api/download/{zip_path.name}"
        except Exception as zip_err:
            err_msg = f"生成 ZIP 压缩包失败: {zip_err}"
            print(err_msg)
            export_errors.append(err_msg)

        return export_files, export_errors

    async def _run_task(self, task_id: str, request: TaskCreateRequest):
        task = self.tasks[task_id]
        detected_platform = self._detect_platform(request.target, request.platform)
        task.platform = detected_platform.value
        scraper = self._get_scraper(request, detected_platform)

        pause_event = asyncio.Event()
        pause_event.set()
        self.pause_events[task_id] = pause_event

        # ===== 本地住宅IP直连源码（分块上传）索引 =====
        # 前端把扩展抓到的源码按块上传到 data/relay_uploads/{upload_id}.jsonl（每行一篇），
        # 创建任务时 articles_meta 不带源码。这里建立 URL→字节偏移 的轻量索引，
        # 处理到某篇时再按偏移精确读取该行，全程不必把数百 MB 源码载入内存。
        relay_file_path = None
        relay_line_index = {}
        if getattr(request, "relay_upload_id", None):
            # upload_id 白名单校验，防止路径穿越
            if re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", request.relay_upload_id):
                relay_file_path = BASE_DIR / "data" / "relay_uploads" / f"{request.relay_upload_id}.jsonl"
                if relay_file_path.exists():
                    try:
                        with open(relay_file_path, "rb") as rf:
                            offset = 0
                            for line in rf:
                                try:
                                    u = json.loads(line).get("url")
                                    # 同 URL 以首次出现为准（网络重试可能造成极少量重复行，无害）
                                    if u and u not in relay_line_index:
                                        relay_line_index[u] = offset
                                except Exception:
                                    pass
                                offset += len(line)
                    except Exception:
                        relay_line_index = {}
                else:
                    # 承认现实：临时文件丢了（如服务器重启清理），后续全部按失败归档，不用云端IP补抓
                    relay_file_path = None

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

            # ===== 两阶段导出（弹窗勾选后直接导出）=====+
            # 当请求里携带了已选文章元数据时，跳过「重新检索文章列表」步骤，
            # 直接用前端回传的元数据作为 article_list，确保 content_type / column_title
            # 等分类信息不因为 URL 列表被识别为 custom_urls 而丢失。
            if request.articles_meta and len(request.articles_meta) > 0:
                article_list = request.articles_meta
                author_name = request.author_name_override or article_list[0].get("author") or "目标博主"
                task.author_name = author_name
                task.message = f"已接收用户勾选的 {len(article_list)} 篇文章，开始抓取正文..."
                await self._broadcast(task_id)
            else:
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
                # 统一出口清洗：微信个别通道（合集页 DOM 等）会输出「标题 标题」双份文本，
                # 在进入抓取/导出流程前统一去重（判定规则保守，正常标题零误伤）
                for _meta in article_list:
                    if _meta.get("title"):
                        _meta["title"] = dedupe_wechat_title(_meta["title"])
            
            if hasattr(scraper, "declared_count") and scraper.declared_count is not None:
                task.declared_count = scraper.declared_count
            if hasattr(scraper, "category_name") and scraper.category_name:
                task.category_name = scraper.category_name
            if hasattr(scraper, "explanation") and scraper.explanation:
                task.explanation = scraper.explanation

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
            # 抓到列表后落一次快照，便于中断恢复时展示"共几篇、已到第几篇"
            self._save_snapshot(task)

            # 3. 批量抓取单篇正文 (支持断点续爬与本地缓存)
            scraped_articles: List[ArticleItem] = []
            for idx, meta in enumerate(article_list, 1):
                if task.is_cancelled:
                    task.status = TaskStatusEnum.CANCELLED
                    await self._broadcast(task_id)
                    await scraper.close()
                    return

                # 检查是否要求立即截断并导出已抓取内容
                if task.is_interrupted_to_export:
                    task.message = f"⚡ 用户指令截断抓取，共成功获取 {len([a for a in scraped_articles if not a.is_failed])} 篇，正在立即启动排版导出..."
                    await self._broadcast(task_id)
                    break

                # 检查是否处于暂停状态，若暂停则挂起等待恢复或终止
                if task.is_paused:
                    await pause_event.wait()
                    if task.is_cancelled:
                        task.status = TaskStatusEnum.CANCELLED
                        await self._broadcast(task_id)
                        await scraper.close()
                        return
                    if task.is_interrupted_to_export:
                        task.message = f"⚡ 用户指令截断抓取，共成功获取 {len([a for a in scraped_articles if not a.is_failed])} 篇，正在立即启动排版导出..."
                        await self._broadcast(task_id)
                        break

                task.current_article_index = idx
                task.current_article_title = meta.get("title", f"第 {idx} 篇")
                task.progress_percent = round((idx / len(article_list)) * 75.0, 1) # 抓取占 75%

                article_url = meta.get("url", "")

                # 若该篇源码在分块上传的临时文件里（创建任务时未内联携带），按 URL 精确取回
                if relay_file_path is not None and not meta.get("raw_html"):
                    off = relay_line_index.get(article_url)
                    if off is not None:
                        try:
                            with open(relay_file_path, "rb") as rf:
                                rf.seek(off)
                                relay_item = json.loads(rf.readline())
                                for k in ("raw_html", "content_html", "status_code", "error_reason"):
                                    if relay_item.get(k) and not meta.get(k):
                                        meta[k] = relay_item[k]
                                # 标记来源：该篇确实经过本地直连通道（哪怕是失败的）
                                meta["_from_relay"] = True
                        except Exception:
                            pass

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

                # 本地直连中继判定：有源码且扩展侧未标记失败
                # （扩展对 403/超时会标 is_failed=True，此时即使带回了错误页 HTML 也不能当正文用）
                is_client_relay = bool((meta.get("raw_html") or meta.get("content_html")) and not meta.get("is_failed"))

                if is_client_relay:
                    task.message = f"[本地住宅IP中继] ({idx}/{len(article_list)}): {task.current_article_title[:22]}..."
                    await self._broadcast(task_id)
                    article_item = None
                    try:
                        article_item = await scraper.scrape_article_detail(meta)
                    except Exception:
                        article_item = None

                    if not article_item or article_item.is_failed:
                        raw_c = meta.get("content_html") or meta.get("raw_html") or ""
                        cleaned_html, md_content, images = clean_html_content(
                            raw_c,
                            enable_noise_filter=request.enable_noise_filter,
                            remove_watermark=request.remove_image_watermark
                        )
                        article_item = ArticleItem(
                            id=meta.get("id") or article_url,
                            title=meta.get("title", task.current_article_title),
                            author=meta.get("author", task.author_name),
                            publish_time=meta.get("publish_time", ""),
                            url=article_url,
                            platform=task.platform,
                            summary=md_content[:200].replace("\n", " ").strip() if md_content else "",
                            content_html=cleaned_html,
                            content_markdown=md_content,
                            images=images,
                            category=meta.get("column_title") or meta.get("category") or task.category_name,
                            content_type=meta.get("content_type", "article"),
                            is_failed=False
                        )

                    if request.use_cache and article_item.content_html and not article_item.is_failed:
                        save_cached_article(article_item)
                elif is_valid_cache:
                    task.message = f"[缓存命中] ({idx}/{len(article_list)}): {task.current_article_title[:22]}..."
                    await self._broadcast(task_id)
                    article_item = cached_item
                elif meta.get("_from_relay"):
                    # 本地直连抓取过但没拿到源码（失败项）：直接按失败归档。
                    # 千万不能用云端服务器 IP 去补抓知乎——机房 IP 已被风控，一旦触发整批雪崩
                    article_item = ArticleItem(
                        id=meta.get("id") or article_url,
                        title=meta.get("title", task.current_article_title),
                        author=meta.get("author", task.author_name),
                        publish_time=meta.get("publish_time", ""),
                        url=article_url,
                        platform=task.platform,
                        summary="",
                        content_html="",
                        content_markdown="",
                        images=[],
                        category=meta.get("column_title") or meta.get("category") or task.category_name,
                        content_type=meta.get("content_type", "article"),
                        is_failed=True
                    )
                else:
                    task.message = f"正在抓取 ({idx}/{len(article_list)}): {task.current_article_title[:22]}..."
                    await self._broadcast(task_id)
                    try:
                        article_item = await scraper.scrape_article_detail(meta)
                    except Exception as art_err:
                        # 单篇异常安全网：任何一篇抓取抛出意外异常（如底层超时、解析崩溃），
                        # 都只把这一篇归档为失败并继续下一篇，绝不能让整批任务陪葬。
                        # 注意 asyncio.CancelledError 继承自 BaseException，不会被这里吞掉，取消/暂停仍正常工作。
                        print(f"[单篇异常] ({idx}/{len(article_list)}) {article_url} "
                              f"抓取异常已归档为失败并继续: {type(art_err).__name__}: {art_err}")
                        article_item = ArticleItem(
                            id=meta.get("id") or article_url,
                            title=meta.get("title", task.current_article_title),
                            author=meta.get("author", task.author_name),
                            publish_time=meta.get("publish_time", ""),
                            url=article_url,
                            platform=task.platform,
                            summary="",
                            content_html="",
                            content_markdown="",
                            images=[],
                            category=meta.get("column_title") or meta.get("category") or task.category_name,
                            content_type=meta.get("content_type", "article"),
                            is_failed=True
                        )
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

                # ===== 两阶段导出：用前端传回的分类元数据覆盖缓存/抓取结果 =====
                # 缓存里的 ArticleItem 可能在之前导出时没有 column_title/content_type，
                # 本次弹窗已重新识别好分类，必须用它覆盖，否则导出模板里专栏还是会显示为空。
                if request.articles_meta and (idx - 1) < len(request.articles_meta):
                    selected_meta = request.articles_meta[idx - 1]
                    if selected_meta.get("content_type"):
                        article_item.content_type = selected_meta["content_type"]
                    if selected_meta.get("column_title"):
                        article_item.column_title = selected_meta["column_title"]
                    # 顺手把更新后的正确分类写回缓存，下次直接导出也能保持专栏信息
                    if request.use_cache and is_valid_cache:
                        save_cached_article(article_item)

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
                # 每抓 20 篇落一次快照：既不过度刷盘，又能保证中断时进度损失有界(最多回退 20 篇)
                if idx % 20 == 0:
                    self._save_snapshot(task)

            await scraper.close()

            if task.is_cancelled:
                task.status = TaskStatusEnum.CANCELLED
                await self._broadcast(task_id)
                return

            # 如果用户截断导出，更新篇数统计
            if task.is_interrupted_to_export:
                task.total_articles = len(scraped_articles)

            # 4. 统计与失败篇目交互校验
            success_items = [a for a in scraped_articles if not a.is_failed and "抓取失败" not in a.content_markdown]
            failed_items = [a for a in scraped_articles if a.is_failed or "抓取失败" in a.content_markdown]

            task.success_articles = [
                {"title": a.title, "url": a.url, "publish_time": a.publish_time} for a in success_items
            ]
            task.failed_articles = [
                {"title": a.title, "url": a.url, "error_reason": a.error_reason or "未能解析出有效正文"} for a in failed_items
            ]

            # 若用户提前要求截断导出，直接取全部成功文章进行排版，不弹窗打扰
            if task.is_interrupted_to_export and len(success_items) > 0:
                scraped_articles = success_items
            # 如果存在失败篇目且有部分成功篇目，进入确认等待状态
            elif len(failed_items) > 0 and len(success_items) > 0:
                task.status = TaskStatusEnum.WAITING_CONFIRMATION
                task.progress_percent = 78.0
                task.message = f"抓取阶段完成：{len(success_items)} 篇成功，{len(failed_items)} 篇失败。等待用户确认..."
                await self._broadcast(task_id)

                self.decision_events[task_id] = asyncio.Event()
                # 等待用户决策可能很久，先落一次快照，便于此时进程若被杀也能快速续跑
                self._save_snapshot(task)
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

            # 5. 智能提炼合集作者、平台名称与分类专栏名
            # 过滤掉无效作者名，保留真实博主名（如“欧洋”）
            valid_authors = [a.author for a in scraped_articles if a.author and a.author not in ["互联网博主", "微信公众号", "未知", "", "未知博主", "Blogger"]]
            # 过滤掉无效平台名，保留真实平台名（如“新浪博客”）
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
                elif top_platform in ["新浪博客", "sina_blog"]:
                    platform_str = "新浪博客"
                elif top_platform in ["简书", "jianshu"]:
                    platform_str = "简书"
                    task.platform = "简书"

            # 如果当前是自定义 URL 模式，scraper 本身没有 category_name，
            # 则从每篇文章的 category 字段里统计最常见的真实分类/专栏名（如“西游正解”）
            if not task.category_name:
                valid_categories = [
                    a.category for a in scraped_articles
                    if a.category and a.category.strip()
                    and a.category.strip() not in ["新浪博文", "全部博文", "未分类", "互联网博文"]
                ]
                if valid_categories:
                    task.category_name = max(set(valid_categories), key=valid_categories.count)

            # 6. 开始合并与排版导出
            task.status = TaskStatusEnum.EXPORTING
            task.message = f"文章抓取完毕 (共 {len(scraped_articles)} 篇有效文章)，正在排版合并并导出目标格式..."
            task.progress_percent = 82.0
            await self._broadcast(task_id)

            safe_author = re.sub(r'[\/:*?"<>|]', '_', author_name).strip() or "Blogger"
            safe_platform = re.sub(r'[\/:*?"<>|]', '_', platform_str).strip() or "platform"
            # 如果抓取的是某个分类/专栏（如新浪博客的“西游正解”），把分类名也放进文件名前缀里
            # 最终文件名形如：西游正解_欧洋_新浪博客_原版.html
            # 但简书通常是抓作者全部文章，不需要分类名，直接用“作者_简书”格式。
            safe_category = re.sub(r'[\/:*?"<>|]', '_', task.category_name or "").strip()
            if safe_category and safe_category not in ["单篇博文", "未分类"] and safe_platform != "简书":
                filename_prefix = f"{safe_category}_{safe_author}_{safe_platform}"
            else:
                filename_prefix = f"{safe_author}_{safe_platform}"

            # ===== 自动分批导出编排 =====
            # 大规模导出 (如九千篇) 一次性渲染合并 PDF/Word 会把内存与浏览器压垮 (用户反馈的「卡在 96%」)。
            # 策略：未显式指定 batch_size 且总数超过 800 篇时，自动按 500 篇/批拆分；
            # 多批模式下，md/txt/html/original_html 等轻量格式仍提供全量合并单文件，
            # PDF/Word 等重型格式随每批独立 ZIP 交付，永不一次性渲染数千篇。
            total_selected = len(scraped_articles)
            batch_size = request.batch_size
            auto_batched = False
            if not batch_size and total_selected > 800:
                batch_size = 500
                auto_batched = True
            if batch_size:
                try:
                    batch_size = max(50, min(2000, int(batch_size)))
                except (TypeError, ValueError):
                    batch_size = 500
            num_batches = math.ceil(total_selected / batch_size) if (batch_size and total_selected > batch_size) else 1

            export_files: Dict[str, str] = {}
            export_errors: List[str] = []

            if num_batches <= 1:
                # ---------- 单批：保持原有导出行为 ----------
                result_files, result_errors = await self._export_batch(
                    task, request, scraped_articles, filename_prefix, author_name, platform_str,
                    request.export_formats, 82.0, 17.0
                )
                if result_files is None:
                    task.status = TaskStatusEnum.CANCELLED
                    await self._broadcast(task_id)
                    return
                export_files.update(result_files)
                export_errors.extend(result_errors)

                # 若全部导出格式均失败，任务应标记为失败，不要让前端显示「已完成但无产物」
                if not export_files and export_errors:
                    raise RuntimeError("; ".join(export_errors))
            else:
                # ---------- 多批：轻量合并格式全量一份 + 每批独立 ZIP ----------
                if auto_batched:
                    task.message = f"📦 检测到 {total_selected} 篇大规模导出，已自动启用分批备份 (每批 {batch_size} 篇 · 共 {num_batches} 批)，告别内存爆炸与末尾卡死..."
                else:
                    task.message = f"📦 已启用分批备份 (每批 {batch_size} 篇 · 共 {num_batches} 批)，正在逐批导出..."
                task.progress_percent = 80.0
                await self._broadcast(task_id)

                # 1. 轻量合并格式 (md/txt/html/original_html) 全量合并单文件，进度 80 -> 86
                light_format_set = {ExportFormatEnum.MARKDOWN, ExportFormatEnum.TXT, ExportFormatEnum.HTML, ExportFormatEnum.ORIGINAL_HTML}
                light_formats = [f for f in request.export_formats if f in light_format_set]
                if light_formats:
                    light_total = len(light_formats)
                    for l_idx, fmt in enumerate(light_formats, 1):
                        if task.is_cancelled:
                            task.status = TaskStatusEnum.CANCELLED
                            await self._broadcast(task_id)
                            return
                        task.progress_percent = round(80.0 + (l_idx / (light_total + 1)) * 6.0, 1)
                        task.message = f"正在生成全量合并 {fmt.value.upper()} 文档 ({l_idx}/{light_total})..."
                        await self._broadcast(task_id)
                        try:
                            if fmt == ExportFormatEnum.MARKDOWN:
                                exporter = MarkdownExporter(author_name, platform_str, OUTPUT_DIR)
                                out_path = await exporter.export(scraped_articles, filename_prefix)
                                export_files["md"] = f"/api/download/{out_path.name}"
                            elif fmt == ExportFormatEnum.TXT:
                                exporter = TxtExporter(author_name, platform_str, OUTPUT_DIR)
                                out_path = await exporter.export(scraped_articles, filename_prefix)
                                export_files["txt"] = f"/api/download/{out_path.name}"
                            elif fmt == ExportFormatEnum.HTML:
                                exporter = HTMLExporter(author_name, platform_str, OUTPUT_DIR)
                                out_path = await exporter.export(scraped_articles, filename_prefix, download_images=request.image_mode != "none")
                                export_files["html"] = f"/api/download/{out_path.name}"
                            elif fmt == ExportFormatEnum.ORIGINAL_HTML:
                                exporter = OriginalHTMLExporter(author_name, platform_str, OUTPUT_DIR)
                                out_path = await exporter.export(scraped_articles, filename_prefix, download_images=request.image_mode != "none")
                                export_files["original_html"] = f"/api/download/{out_path.name}"
                        except Exception as light_err:
                            err_msg = f"全量合并格式 {fmt.value} 导出失败: {light_err}"
                            print(err_msg)
                            export_errors.append(err_msg)
                    # 轻量格式生成完毕，释放图片缓存
                    clear_image_caches()

                # 2. 逐批导出全部勾选格式并打独立 ZIP，进度 86 -> 99
                batch_span = 13.0 / num_batches
                for b_idx in range(num_batches):
                    if task.is_cancelled:
                        task.status = TaskStatusEnum.CANCELLED
                        await self._broadcast(task_id)
                        return
                    b_start = b_idx * batch_size
                    b_articles = scraped_articles[b_start:b_start + batch_size]
                    b_prefix = f"{filename_prefix}_第{b_idx + 1}批_{b_start + 1}-{b_start + len(b_articles)}篇"
                    b_tag = f"[第 {b_idx + 1}/{num_batches} 批] "

                    result_files, result_errors = await self._export_batch(
                        task, request, b_articles, b_prefix, author_name, platform_str,
                        request.export_formats, 86.0 + b_idx * batch_span, batch_span, batch_tag=b_tag
                    )
                    if result_files is None:
                        task.status = TaskStatusEnum.CANCELLED
                        await self._broadcast(task_id)
                        return

                    if "zip" in result_files:
                        export_files[f"zip_batch_{b_idx + 1}"] = result_files["zip"]
                    export_errors.extend(result_errors)
                    # 每批之间释放全局图片缓存，避免跨批内存累积
                    clear_image_caches()

                # 全部批次均失败才算任务失败
                has_any_zip = any(k.startswith("zip_batch_") for k in export_files)
                if not has_any_zip and not export_files and export_errors:
                    raise RuntimeError("; ".join(export_errors))

            # 导出阶段收尾：清空全局图片缓存，释放大对象内存
            clear_image_caches()

            # 8. 完成
            task.status = TaskStatusEnum.COMPLETED
            task.progress_percent = 100.0
            task.export_files = export_files
            task.completed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if num_batches > 1:
                task.message = f"🎉 分批备份全部完成！共 {len(scraped_articles)} 篇有效文章，已拆分为 {num_batches} 个 ZIP 归档包，可逐批下载。"
            else:
                task.message = f"🎉 抓取与导出全部完成！共合并 {len(scraped_articles)} 篇有效文章。"
            await self._broadcast(task_id)

        except Exception as e:
            task.status = TaskStatusEnum.FAILED
            # 关键修复：超时/内存不足这类异常的 str(e) 是空字符串，
            # 之前只显示 str(e) 会变成"执行过程中发生错误: "（前端兜底显示"未知错误"），
            # 完全无法定位原因。现在把异常类型名也带上，并在控制台打印完整堆栈供排查。
            task.error_message = f"{type(e).__name__}: {e}".strip()
            task.message = f"❌ 执行过程中发生错误: {task.error_message}"
            print(f"[任务失败] task={task_id} 异常类型={type(e).__name__} 详情={e}")
            import traceback
            traceback.print_exc()
            await self._broadcast(task_id)
            try:
                await scraper.close()
            except Exception:
                pass
        finally:
            self.pause_events.pop(task_id, None)
            self.decision_events.pop(task_id, None)
            # 任务到达终态后，删除本地直连源码临时文件（数百 MB，不能留着占满磁盘）。
            # 暂停/等待确认不算终态：文件保留，恢复执行时还要按 URL 读源码
            try:
                terminal = (TaskStatusEnum.COMPLETED, TaskStatusEnum.FAILED, TaskStatusEnum.CANCELLED)
                if relay_file_path is not None and relay_file_path.exists() and task.status in terminal:
                    relay_file_path.unlink()
            except Exception:
                pass
            # 任务到达终态后，同时删除续跑快照：不再把它当作"未完成的残留任务"去打扰用户续跑
            if task.status in (TaskStatusEnum.COMPLETED, TaskStatusEnum.FAILED, TaskStatusEnum.CANCELLED):
                self._remove_snapshot(task_id)
            # 清理该任务的请求序列化缓存，防止长驻进程内存累积
            self._serialized_requests.pop(task_id, None)

task_manager = TaskManager()
