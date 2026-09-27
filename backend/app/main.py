# BlogDistiller · 后端 API 核心服务入口
import os
import sys
import json
import re
import uuid
import time
import asyncio
import shutil
from pathlib import Path

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# 确保 backend 目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI, HTTPException, Query, Body, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

import io
import zipfile

from app.config import OUTPUT_DIR, BASE_DIR
from app.models import TaskCreateRequest, TaskProgress, TaskStatusEnum
from app.task_manager import task_manager
from app.community_wall import (
    list_contributors,
    add_contributor,
    update_contributor,
    delete_contributor,
    get_wall_summary,
    seed_demo_data,
    CommunityContributor,
    ContributorType,
    PrivacyLevel
)
from app.core.zhihu_auth import (
    check_zhihu_auth_status,
    sync_local_browser_cookies,
    launch_zhihu_qr_login,
    save_zhihu_cookies,
    parse_cookie_string
)
from app.core.wechat_auth import (
    check_wechat_auth_status,
    save_wechat_auth,
    get_saved_wechat_auth
)

app = FastAPI(
    title="BlogDistiller API",
    description="多平台博主文章批量抓取、去广告清洗与多格式合并导出服务",
    version="1.0.0"
)

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    # 初始化社区致谢墙示例数据（仅首次空数据时）
    seed_demo_data()

    banner = r"""
======================================================================
   ____  _             ____  _     _   _ _ _           
  | __ )| | ___   __ _|  _ \(_)___| |_(_) | | ___ _ __ 
  |  _ \| |/ _ \ / _` | | | | / __| __| | | |/ _ \ '__|
  | |_) | | (_) | (_| | |_| | \__ \ |_| | | |  __/ |   
  |____/|_|\___/ \__, |____/|_|___/\__|_|_|_|\___|_|   
                 |___/                                  
  BlogDistiller · 博萃 - 全网博文批量导出与知识蒸馏神器
  原作者: 艺杯羹 (https://github.com/yibeigen/wechat-article-exporter)
  开源协议: CC BY-NC-SA 4.0 (严禁商用 · 强制署名 · 相同方式共享)
  官方公众号: 微信搜索【艺杯羹】
======================================================================
    """
    print(banner, flush=True)

@app.get("/api/health")
async def health_check():
    """本地客户端与服务端健康检查接口"""
    return {
        "status": "ok",
        "timestamp": time.time(),
        "client_mode": True,
        "version": "1.0.0"
    }

@app.get("/api/platforms")
async def get_supported_platforms():
    """获取所有支持的平台与使用提示 (按微信公众号、知乎、微博、CSDN、51CTO、掘金、博客园顺序排列)"""
    return [
        {
            "id": "wechat",
            "name": "微信公众号",
            "category": "私域内容",
            "placeholder": "粘贴微信文章链接或专辑合集链接 (支持多条，每行一条)",
            "tip": "免登录批量解析：直接粘贴微信推文或专辑链接，去广告排版后一键合并下载"
        },
        {
            "id": "zhihu",
            "name": "知乎",
            "category": "问答与专栏",
            "placeholder": "输入知乎博主主页 (zhihu.com/people/xxx) 或 任意文章/回答链接 (自动溯源导出全量内容)",
            "tip": "支持博主全部历史回答与专栏文章批量导出，或直接粘贴单篇文章自动定位该博主全量内容"
        },
        {
            "id": "weibo",
            "name": "微博",
            "category": "社交媒体",
            "placeholder": "输入微博博主主页链接，如 https://weibo.com/u/12345678 或 UID",
            "tip": "抓取博主头条文章与原创博文，自动展开长文本与配图"
        },
        {
            "id": "sina_blog",
            "name": "新浪博客",
            "category": "经典博客",
            "placeholder": "输入新浪博客博主主页或博文目录链接，如 https://blog.sina.com.cn/u/5320406686 或 articlelist_...html",
            "tip": "无需登录与插件，支持按全量博文或分类专栏一键提取，自动解析高清配图"
        },
        {
            "id": "jianshu",
            "name": "简书",
            "category": "创作社区",
            "placeholder": "输入简书博主主页 (jianshu.com/u/xxx)、专题文集 (jianshu.com/c/xxx) 或单篇长文链接",
            "tip": "免登录一键提取博主全部公开文章或专题文集，高清图文排版无损导出"
        },
        {
            "id": "csdn",
            "name": "CSDN",
            "category": "技术博客",
            "placeholder": "输入 CSDN 主页链接，如 https://blog.csdn.net/username 或用户名",
            "tip": "自动解析分页列表，内置图片防盗链突破与样式清洗"
        },
        {
            "id": "51cto",
            "name": "51CTO",
            "category": "技术博客",
            "placeholder": "输入 51CTO 博客主页链接，如 https://blog.51cto.com/u_xxxx",
            "tip": "分页获取博主历史发表的技术博文与专栏"
        },
        {
            "id": "juejin",
            "name": "掘金",
            "category": "技术社区",
            "placeholder": "输入掘金博主主页链接，如 https://juejin.cn/user/123456 或 用户ID",
            "tip": "支持一键全量抓取博主所有专栏与文章，提取原生高质量 Markdown"
        },
        {
            "id": "cnblogs",
            "name": "博客园",
            "category": "技术博客",
            "placeholder": "输入博客园博主主页，如 https://www.cnblogs.com/username/ 或用户名",
            "tip": "全量分页爬取所有文章，支持代码高亮与公式提取"
        },
        {
            "id": "custom_urls",
            "name": "自定义多链接",
            "category": "通用网页",
            "placeholder": "粘贴任意平台或独立博客的文章链接，支持跨平台混合，每行一条 URL",
            "tip": "通用网页批量提取器：将不同网站收集的文章一次性合并为单本 PDF/电子书"
        }
    ]

class ExtractLinksRequest(BaseModel):
    platform: str
    target: str
    max_articles: Optional[int] = None
    cookie: Optional[str] = None
    token: Optional[str] = None
    uin: Optional[str] = None
    key: Optional[str] = None
    pass_ticket: Optional[str] = None
    appmsg_token: Optional[str] = None
    # 新版前端传 true 使用异步任务模式；旧版缓存前端不传，后端走同步模式兼容旧行为
    async_mode: Optional[bool] = Field(None, description="是否以异步任务模式执行（海量文章防网关超时）")


# ============ 文章清单检索的异步任务注册表 ============
# 背景：/api/extract-links 原先是一个同步长请求，博主文章多达几千篇时要翻几百页、持续数分钟。
# 线上 Nginx / Cloudflare 等网关默认 60~120 秒就会返回 HTML 504 / 524 超时页，浏览器把 HTML 当 JSON
# 解析就会报 "Unexpected token '<'..."，导致用户看到弹窗报错。
# 解决：新版前端通过 async_mode=true 让接口「立即返回任务 ID + 后台异步抓取 + 前端轮询」；
# 旧版尚未刷新缓存的前端不传 async_mode，后端继续走同步模式返回旧格式，避免被 job_id 对象误导为空结果。
_EXTRACT_JOBS: Dict[str, Dict[str, Any]] = {}
_MAX_EXTRACT_JOBS = 2000  # 内存缓存上限，避免长期运行后注册表无限膨胀


async def _do_extract_links(request: ExtractLinksRequest, job_id: Optional[str] = None) -> Dict[str, Any]:
    """实际执行文章清单抓取，返回标准结果字典。

    同步模式与异步模式共用此函数；如果传入 job_id，抓取进度会同步写入 _EXTRACT_JOBS。
    """
    from app.models import PlatformEnum, TaskCreateRequest
    try:
        p_enum = PlatformEnum(request.platform)
    except Exception:
        p_enum = PlatformEnum.CUSTOM_URLS

    # 借用 task_manager 的平台识别与 scraper 工厂，和 /api/tasks 入口保持一致
    dummy_req = TaskCreateRequest(
        platform=p_enum,
        target=request.target,
        max_articles=request.max_articles,
        wechat_cookie=request.cookie,
        wechat_token=request.token,
        wechat_uin=request.uin,
        wechat_key=request.key,
        wechat_pass_ticket=request.pass_ticket,
        wechat_appmsg_token=request.appmsg_token
    )
    detected = task_manager._detect_platform(request.target, p_enum)
    scraper = task_manager._get_scraper(dummy_req, detected)

    # 进度回调：爬虫每翻一页就同步一次最新已检索篇数和状态文本
    def extract_progress_cb(msg: str, count: int, _: int):
        job = _EXTRACT_JOBS.get(job_id) if job_id else None
        if job:
            job["current"] = count
            job["message"] = msg
            job["status"] = "fetching"

    try:
        author_info = await scraper.get_author_info()
        articles = await scraper.get_article_list(progress_callback=extract_progress_cb)
        return {
            "success": True,
            "platform": request.platform,
            "author": author_info.get("name", "未知博主"),
            "total": len(articles),
            "articles": articles,
            "declared_count": getattr(scraper, "declared_count", None),
            "category_name": getattr(scraper, "category_name", None),
            "explanation": getattr(scraper, "explanation", None)
        }
    finally:
        await scraper.close()


async def _run_extract_links_job(job_id: str, request: ExtractLinksRequest) -> None:
    """在后台执行文章清单抓取，并把结果写入 _EXTRACT_JOBS。

    注意：该函数运行在独立的 asyncio Task 中，不会因为抓取时间长而阻塞 /api/extract-links 接口的响应。
    """
    try:
        result = await _do_extract_links(request, job_id)
        _EXTRACT_JOBS[job_id]["status"] = "completed"
        _EXTRACT_JOBS[job_id]["result"] = result
    except Exception as e:
        _EXTRACT_JOBS[job_id]["status"] = "failed"
        _EXTRACT_JOBS[job_id]["error"] = str(e)
    finally:
        job = _EXTRACT_JOBS.get(job_id)
        if job:
            job["message"] = "检索已结束"


def _register_extract_job(platform: str, target: str) -> str:
    """注册一个新的异步提取任务，并返回 job_id；顺便做简单的注册表清理。"""
    job_id = str(uuid.uuid4())
    _EXTRACT_JOBS[job_id] = {
        "status": "pending",
        "current": 0,
        "message": "正在连接目标平台...",
        "result": None,
        "error": None,
        "platform": platform,
        "target": target.strip(),
        "created_at": time.time()
    }

    # 简单清理：当缓存超过上限时，删除最旧的已结束任务，避免内存无限增长
    if len(_EXTRACT_JOBS) > _MAX_EXTRACT_JOBS:
        ended = [jid for jid, j in _EXTRACT_JOBS.items() if j.get("status") in ("completed", "failed")]
        # 保留最近一半额度，腾出空间
        for jid in ended[: max(0, len(ended) - _MAX_EXTRACT_JOBS // 2)]:
            _EXTRACT_JOBS.pop(jid, None)

    return job_id


@app.get("/api/extract-links/progress")
async def extract_links_progress(
    job_id: Optional[str] = Query(None, description="异步任务 ID"),
    platform: Optional[str] = Query(None, description="兼容旧轮询：平台名"),
    target: Optional[str] = Query(None, description="兼容旧轮询：目标链接")
):
    """查询某次文章清单检索的实时进度与最终结果。"""
    # 优先按 job_id 查询，这是新版异步模式
    if job_id:
        job = _EXTRACT_JOBS.get(job_id)
        if not job:
            return {"current": 0, "message": "", "status": "not_found"}
        payload = {
            "current": job.get("current", 0),
            "message": job.get("message", ""),
            "status": job.get("status", "pending")
        }
        if job["status"] == "completed":
            payload["result"] = job.get("result")
        if job["status"] == "failed":
            payload["error"] = job.get("error", "未知错误")
        return payload

    # 兼容旧式按 platform + target 兜底查询（主要给未升级的前端使用）
    if platform and target:
        target_key = target.strip()
        for job in _EXTRACT_JOBS.values():
            if job.get("platform") == platform and job.get("target") == target_key:
                payload = {
                    "current": job.get("current", 0),
                    "message": job.get("message", ""),
                    "status": job.get("status", "pending")
                }
                if job["status"] == "completed":
                    payload["result"] = job.get("result")
                if job["status"] == "failed":
                    payload["error"] = job.get("error", "未知错误")
                return payload

    return {"current": 0, "message": "", "status": "idle"}


@app.post("/api/extract-links")
async def extract_links_endpoint(request: ExtractLinksRequest):
    """仅提取文章列表与链接清单（两阶段架构阶段一）。

    - 新版前端传 async_mode=true：立即返回 job_id，后台异步抓取，前端轮询 /api/extract-links/progress
      直至 status 为 completed 或 failed；这样海量文章也不会被网关超时掐掉。
    - 旧版缓存前端未传 async_mode：继续走同步模式，直接返回旧格式结果，避免被 job_id 结构误报为空。
    """
    if not request.target.strip():
        raise HTTPException(status_code=400, detail="目标博主链接或关键词不能为空")

    # 新版异步模式
    if request.async_mode:
        job_id = _register_extract_job(request.platform, request.target)
        # 启动后台抓取任务，接口立即返回
        asyncio.create_task(_run_extract_links_job(job_id, request))
        return {"success": True, "job_id": job_id, "message": "文章清单检索已转入后台运行，请稍候..."}

    # 同步兼容模式（给旧版缓存/未刷新页面兜底）
    try:
        return await _do_extract_links(request, None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tasks")
async def create_task(request: TaskCreateRequest):
    """创建新的抓取与导出任务"""
    if not request.target.strip():
        raise HTTPException(status_code=400, detail="目标博主链接或文章ID不能为空")

    task_id = task_manager.create_task(request)
    return {"task_id": task_id, "status": "pending", "message": "任务已创建并进入调度队列"}


# ==================== 本地住宅IP直连源码分块上传 ====================
# 背景：扩展在用户本机抓到的 1894 篇网页源码有数百 MB，若随创建任务一次性提交，
# 会超过 nginx / Cloudflare 的 100M 请求体上限，返回 413 HTML 错误页导致前端
# 报 "Unexpected token '<'"。因此前端先分块（每块约 20 篇）上传到服务器临时文件，
# 创建任务时只带文章清单 + relay_upload_id，后端处理时按 URL 从临时文件取源码。

class RelayChunkUpload(BaseModel):
    upload_id: str = Field(..., description="本次批量上传的唯一ID，由前端生成")
    chunk_index: int = Field(..., description="当前块序号（从0开始）")
    total_chunks: int = Field(..., description="总块数（用于前端对账）")
    articles: List[Dict[str, Any]] = Field(..., description="本块文章列表，每项含 url/title/raw_html 等")

# 临时文件目录：data/relay_uploads/{upload_id}.jsonl，每行一篇（JSON Lines 格式，方便按行追加）
RELAY_UPLOAD_DIR = BASE_DIR / "data" / "relay_uploads"

def _sweep_stale_relay_uploads(max_age_hours: int = 24):
    """清扫超过 24 小时的残留源码临时文件（任务异常中断没来得及删的，防止占满磁盘）"""
    try:
        if not RELAY_UPLOAD_DIR.exists():
            return
        now = time.time()
        for f in RELAY_UPLOAD_DIR.glob("*.jsonl"):
            if now - f.stat().st_mtime > max_age_hours * 3600:
                f.unlink(missing_ok=True)
    except Exception:
        pass

@app.post("/api/relay/upload-chunk")
async def relay_upload_chunk(request: Request):
    """接收本地直连抓取源码的一个分块，追加写入临时文件。

    用 Request 直接读原始字节，绕开 FastAPI 的 JSON 自动解析——
    请求体兼容两种格式（按魔数自动识别）：
    - 原始 JSON（application/json，老浏览器明文兜底）
    - gzip 压缩的 JSON（application/octet-stream，1f 8b 魔数，新版前端）
      千篇级任务的源码 JSON 压缩比约 6~8 倍，1.2GB 上传量可压到 ~200MB，大幅节省服务器流量。
    """
    raw = await request.body()
    try:
        # gzip 文件头魔数：0x1f 0x8b，据此判断是否需要先解压
        if len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B:
            import gzip as _gzip
            raw = _gzip.decompress(raw)
        chunk = RelayChunkUpload(**json.loads(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"请求体解析失败: {e}")

    # upload_id 白名单校验：只允许字母数字下划线短横线，防止路径穿越攻击
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", chunk.upload_id):
        raise HTTPException(status_code=400, detail="upload_id 格式非法")
    if not chunk.articles:
        raise HTTPException(status_code=400, detail="分块内容为空")

    RELAY_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 每次接收新上传时顺手清扫一次历史残留（比定时任务更省事，频率也够）
    _sweep_stale_relay_uploads()

    target_file = RELAY_UPLOAD_DIR / f"{chunk.upload_id}.jsonl"
    with open(target_file, "a", encoding="utf-8") as f:
        for art in chunk.articles:
            # 每行一篇的 JSON Lines 格式：后端可以按行流式读取，不必整包载入内存
            f.write(json.dumps(art, ensure_ascii=False) + "\n")

    return {"success": True, "received": len(chunk.articles), "chunk_index": chunk.chunk_index, "total_chunks": chunk.total_chunks}

@app.get("/api/relay/upload-status")
async def relay_upload_status(upload_id: str):
    """查询某次分块上传已落盘的行数，前端据此断点续传（整块传完的直接跳过）"""
    # upload_id 白名单校验，防止路径穿越
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", upload_id):
        raise HTTPException(status_code=400, detail="upload_id 格式非法")
    f = RELAY_UPLOAD_DIR / f"{upload_id}.jsonl"
    if not f.exists():
        return {"exists": False, "lines": 0}
    lines = 0
    try:
        with open(f, "rb") as rf:
            for _ in rf:
                lines += 1
    except Exception:
        lines = 0
    return {"exists": True, "lines": lines}

@app.get("/api/tasks")
async def list_tasks(client_id: Optional[str] = None):
    """获取指定客户端的历史任务 (未提供 client_id 时返回空以保护多用户隐私)"""
    return task_manager.list_tasks(client_id=client_id)

# ==================== 中断续跑：检测残留 / 续跑 / 丢弃 ====================
# 用途：服务被关、进程被杀、或浏览器关闭后，正在跑的抓取任务会"凭空消失"。
# 这些接口让前端一打开页面就能查到上次没跑完的任务，弹窗询问用户是否继续。
# 续跑原理：快照里存了完整原始请求，用同一请求重跑，已抓成功的篇目命中 SQLite 缓存秒过不重复下载。

@app.get("/api/tasks/incomplete")
async def list_incomplete_tasks(client_id: Optional[str] = None):
    """列出当前客户端所有未跑完的残留任务(从磁盘快照恢复)"""
    return task_manager.list_incomplete_tasks(client_id=client_id)

class ResumeOrDiscardRequest(BaseModel):
    client_id: Optional[str] = None

@app.post("/api/tasks/incomplete/{task_id}/resume")
async def resume_incomplete_task(task_id: str, req: ResumeOrDiscardRequest):
    """从快照重建任务继续跑；返回 task_id 供前端监听实时进度"""
    tid, err = task_manager.resume_incomplete_task(task_id, client_id=req.client_id)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"task_id": tid, "status": "pending", "message": "任务已从中断处继续执行"}

@app.post("/api/tasks/incomplete/{task_id}/discard")
async def discard_incomplete_task(task_id: str, req: ResumeOrDiscardRequest):
    """用户选择「不继续」，删除残留任务快照"""
    ok = task_manager.discard_incomplete_task(task_id, client_id=req.client_id)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到对应的未完成任务")
    return {"success": True, "message": "已放弃该未完成任务"}

@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    """获取单个任务详情"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task

@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str):
    """终止正在执行的任务"""
    success = task_manager.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"success": True, "message": "任务已终止"}

@app.post("/api/tasks/{task_id}/pause")
async def pause_task_endpoint(task_id: str):
    """暂停正在执行的抓取任务"""
    success = task_manager.pause_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="任务不存在或当前状态无法暂停")
    return {"success": True, "message": "任务已暂停"}

@app.post("/api/tasks/{task_id}/resume")
async def resume_task_endpoint(task_id: str):
    """恢复已暂停的抓取任务"""
    success = task_manager.resume_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="任务不存在或当前未处于暂停状态")
    return {"success": True, "message": "任务已恢复运行"}

@app.post("/api/tasks/{task_id}/stop-and-export")
async def stop_and_export_endpoint(task_id: str):
    """截断抓取并立即将已成功抓取的文章送入排版导出"""
    success = task_manager.stop_and_export(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="任务不存在或当前状态无法截断导出")
    return {"success": True, "message": "已截断抓取，正在打包已成功获取的篇目"}

class TaskDecisionRequest(BaseModel):
    action: str # "skip_and_export" | "retry_failed" | "cancel"

@app.post("/api/tasks/{task_id}/decision")
async def handle_task_decision_endpoint(task_id: str, req: TaskDecisionRequest):
    """用户对失败篇目的处理决策"""
    success = await task_manager.handle_decision(task_id, req.action)
    if not success:
        raise HTTPException(status_code=400, detail="处理决策失败或任务已不在等待决策状态")
    return {"success": True, "message": "决策已执行"}

@app.get("/api/tasks/{task_id}/events")
async def task_events_stream(task_id: str):
    """SSE 实时进度事件流"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        queue = await task_manager.subscribe(task_id)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                    
                    # 只有当实际发送到前端的数据本身状态是 completed / failed / cancelled 时才断开连接
                    import json
                    try:
                        event_obj = json.loads(data)
                        if event_obj.get("status") in ["completed", "failed", "cancelled"]:
                            break
                    except Exception:
                        pass
                except asyncio.TimeoutError:
                    # 15 秒无新事件，主动发送 SSE 规范注释行保活，彻底防止前端与反向代理网络超时切断连接
                    yield ": heartbeat-ping\n\n"
                    cur_task = task_manager.get_task(task_id)
                    if cur_task and cur_task.status in [TaskStatusEnum.COMPLETED, TaskStatusEnum.FAILED, TaskStatusEnum.CANCELLED]:
                        yield f"data: {cur_task.model_dump_json()}\n\n"
                        break
        finally:
            task_manager.unsubscribe(task_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """下载导出的合并单文件"""
    file_path = OUTPUT_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")
    
    # 解决中文文件名在 Content-Disposition 中的编码
    from urllib.parse import quote
    encoded_filename = quote(filename)
    
    return FileResponse(
        path=str(file_path),
        filename=filename,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )

# =====================================================================
# 本地版文件系统服务（仅本地模式可用，公网服务器版自动 403）
# 用途：让用户通过前端弹窗浏览本机目录，把导出产物直接复制到指定文件夹，
#       绕开"浏览器网页无法指定下载保存路径"的安全限制。
# 安全：require_local 校验请求 Host 头必须是 127.0.0.1/localhost/::1。
#       （不能用 request.client.host：服务器版经 nginx 反代后 client 恒为 127.0.0.1，
#        而 Host 头透传的是用户真实访问的域名，能准确区分本地版与公网版。）
# =====================================================================
async def require_local(request: Request):
    """本地模式保护依赖：仅放行本地访问的文件系统接口"""
    host_header = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if host_header not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="该能力仅在本地模式 (127.0.0.1) 下可用")


@app.get("/api/fs/browse", dependencies=[Depends(require_local)])
async def fs_browse(path: Optional[str] = None):
    """浏览本机文件系统目录，返回子目录与文件列表（供前端文件夹选择弹窗使用）

    参数：path 为要浏览的绝对路径，缺省时从用户主目录开始。
    返回：{"current": 当前目录, "parent": 上级目录(根目录时为null),
           "entries": [{"name","path","is_dir"}]}
    """
    if not path:
        path = str(Path.home()) if sys.platform == "win32" else "/"
    try:
        cur = Path(path)
        if not cur.is_dir():
            return {
                "current": str(cur), "parent": None, "entries": [],
                "error": f"不是有效目录：{cur}"
            }
    except Exception as e:
        return {"current": path, "parent": None, "entries": [], "error": str(e)}

    entries = []
    try:
        # Windows 系统盘根目录常见的大型系统隐藏文件，纯属噪音，直接过滤掉
        # 否则用户会看到 pagefile.sys/swapfile.sys/hiberfil.sys 等看不懂的文件，选择体验差
        JUNK_SYSTEM_NAMES = {"pagefile.sys", "swapfile.sys", "hiberfil.sys", "System Volume Information", "$RECYCLE.BIN"}
        with os.scandir(cur) as it:
            for e in it:
                # 跳过隐藏文件/目录（以 . 开头）与软链接，避免干扰选择
                if e.name.startswith("."):
                    continue
                # 跳过系统垃圾文件与回收站/系统卷信息等无意义目录
                if e.name in JUNK_SYSTEM_NAMES:
                    continue
                try:
                    is_dir = e.is_dir()
                except Exception:
                    continue
                entries.append({"name": e.name, "path": str(Path(e.path)), "is_dir": is_dir})
    except PermissionError:
        return {"current": str(cur), "parent": None, "entries": [], "error": "没有权限访问该目录"}

    # 目录优先、名称字母序排序，方便快速定位
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    parent = str(cur.parent) if cur.parent != cur else None

    # Windows 下同时返回全部磁盘（C:\ D:\ E:\ …），供前端渲染"快速切换磁盘"栏，
    # 否则用户停在 C 盘根目录时「上级」无效，根本进不了其他盘。
    drives: list = []
    if sys.platform == "win32":
        import string as _string
        for d in _string.ascii_uppercase:
            dp = Path(f"{d}:\\")
            try:
                if dp.exists():
                    drives.append(str(dp))
            except Exception:
                continue
    return {"current": str(cur), "parent": parent, "entries": entries, "drives": drives}


@app.post("/api/fs/mkdir", dependencies=[Depends(require_local)])
async def fs_mkdir(data: dict):
    """在指定路径下新建文件夹（支持一次性创建多级目录）"""
    raw = (data.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="路径不能为空")
    try:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return {"path": str(p), "created": p.is_dir()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建目录失败：{e}")


@app.post("/api/fs/save-file", dependencies=[Depends(require_local)])
async def fs_save_file(data: dict):
    """把 downloads/ 目录下的某个导出产物复制到用户指定的文件夹

    防路径穿越：源文件必须真实存在于 OUTPUT_DIR 内（resolve 后前缀校验）。
    重名处理：目标目录已存在同名文件时自动追加 " (1)"、" (2)" 后缀。
    复制使用线程池执行，避免阻塞异步事件循环（大 ZIP 也能秒级复制）。
    """
    filename = (data.get("filename") or "").strip()
    dest_dir = (data.get("dest_dir") or "").strip()
    if not filename or not dest_dir:
        raise HTTPException(status_code=400, detail="filename 与 dest_dir 均为必填项")

    src = (OUTPUT_DIR / filename).resolve()
    out_root = OUTPUT_DIR.resolve()
    if not src.is_file() or not str(src).startswith(str(out_root)):
        raise HTTPException(status_code=400, detail="源文件不存在或超出下载目录范围")

    try:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        dst = dest / src.name
        idx = 1
        while dst.exists():
            dst = dest / f"{src.stem} ({idx}){src.suffix}"
            idx += 1
        await asyncio.to_thread(shutil.copy2, src, dst)
        return {"saved_path": str(dst)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"复制文件失败：{e}")


@app.get("/api/zhihu/status")
async def get_zhihu_auth_status():
    """获取知乎当前登录凭证与连接状态"""
    return await check_zhihu_auth_status()

@app.post("/api/zhihu/sync-browser")
async def sync_zhihu_browser():
    """一键同步本机 Edge / Chrome 浏览器中的知乎登录态"""
    return await sync_local_browser_cookies()

@app.post("/api/zhihu/qr-login")
async def trigger_zhihu_qr_login():
    """唤起浏览器弹窗进行知乎扫码登录"""
    return await launch_zhihu_qr_login()

@app.post("/api/zhihu/set-cookie")
async def set_manual_zhihu_cookie(data: dict):
    """手动/扩展保存知乎 Cookie 字符串，保存前检测是否包含关键登录凭证 z_c0"""
    cookie_str = data.get("cookie", "")
    if not cookie_str.strip():
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    cookies = parse_cookie_string(cookie_str)
    has_zc0 = "z_c0" in cookies
    save_zhihu_cookies(cookies)
    status = await check_zhihu_auth_status()
    status["has_z_c0"] = has_zc0
    if not has_zc0:
        status["warning"] = "已保存的 Cookie 中未包含 z_c0，知乎仍会拒绝主页访问。请确认当前浏览器已在 www.zhihu.com 登录，或手动粘贴含 z_c0 的完整 Cookie。"
    return status

@app.get("/api/weibo/status")
async def get_weibo_auth_status():
    """获取微博当前登录凭证与连接状态"""
    from app.core.weibo_auth import check_weibo_auth_status
    return await check_weibo_auth_status()

@app.post("/api/weibo/sync-browser")
async def sync_weibo_browser():
    """一键同步本机 Edge / Chrome 浏览器中的微博登录态"""
    from app.core.weibo_auth import sync_local_weibo_cookies
    return await sync_local_weibo_cookies()

@app.post("/api/weibo/qr-login")
async def trigger_weibo_qr_login():
    """唤起浏览器弹窗进行微博扫码登录"""
    from app.core.weibo_auth import launch_weibo_qr_login
    return await launch_weibo_qr_login()

@app.post("/api/weibo/set-cookie")
async def set_weibo_cookie_api(data: dict):
    """保存微博 Cookie (支持字符串或字典)"""
    from app.core.weibo_auth import save_weibo_cookies, parse_weibo_cookie_string, check_weibo_auth_status
    cookies = data.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        cookie_str = data.get("cookie", "")
        if not cookie_str.strip():
            raise HTTPException(status_code=400, detail="Cookie 不能为空")
        cookies = parse_weibo_cookie_string(cookie_str)
    save_weibo_cookies(cookies)
    return await check_weibo_auth_status()

@app.post("/api/zhihu/logout")
async def logout_zhihu():
    """清除本地保存的知乎登录凭证"""
    from app.core.zhihu_auth import save_zhihu_cookies, SESSION_FILE
    if SESSION_FILE.exists():
        try:
            SESSION_FILE.unlink()
        except Exception:
            save_zhihu_cookies({})
    else:
        save_zhihu_cookies({})
    return {"success": True, "message": "已清除知乎本地登录凭证"}

@app.post("/api/weibo/logout")
async def logout_weibo():
    """清除本地保存的微博登录凭证"""
    from app.core.weibo_auth import save_weibo_cookies, WEIBO_AUTH_FILE
    if WEIBO_AUTH_FILE.exists():
        try:
            WEIBO_AUTH_FILE.unlink()
        except Exception:
            save_weibo_cookies({})
    else:
        save_weibo_cookies({})
    return {"success": True, "message": "已清除微博本地登录凭证"}

@app.get("/api/wechat/status")
async def get_wechat_auth_status():
    """获取微信公众平台后台当前凭证连接状态"""
    return await check_wechat_auth_status()

@app.get("/api/wechat/client-auth-status")
async def get_wechat_client_auth_status():
    """获取微信阅读端私钥凭证 (uin/key/pass_ticket) 的生命周期倒计时与状态"""
    from app.core.wechat_auth import get_client_auth_status
    return get_client_auth_status()

@app.post("/api/wechat/generate-profile-url")
async def generate_profile_url_api(data: dict):
    """输入文章链接或 __biz，智能生成微信专属历史主页 profile_ext 引导链接"""
    target = (data and data.get("target")) or ""
    if not target.strip():
        raise HTTPException(status_code=400, detail="目标文章链接或公众号标识不能为空")
    from app.core.wechat_auth import generate_wechat_profile_url
    return generate_wechat_profile_url(target)

@app.post("/api/wechat/mp/login/session")
async def wechat_mp_login_session(data: dict = None):
    """初始化微信公众平台扫码登录会话并直接下发 Base64 二维码"""
    from app.core.wechat_qr_login import qr_login_manager
    sid = (data and data.get("session_id")) or str(int(time.time() * 1000))
    resp = await qr_login_manager.start_session(sid)
    return resp

@app.get("/api/wechat/mp/login/qrcode")
async def wechat_mp_login_qrcode(session_id: str):
    """直接流式返回微信公众平台扫码登录的二维码图像"""
    from app.core.wechat_qr_login import qr_login_manager
    img_bytes = await qr_login_manager.get_qrcode_image(session_id)
    if not img_bytes:
        raise HTTPException(status_code=500, detail="获取二维码图片失败")
    return Response(content=img_bytes, media_type="image/jpeg")

@app.get("/api/wechat/mp/login/status")
async def wechat_mp_login_status(session_id: str):
    """轮询扫码状态"""
    from app.core.wechat_qr_login import qr_login_manager
    return await qr_login_manager.ask_scan_status(session_id)

@app.post("/api/wechat/set-auth")
async def set_wechat_auth(data: dict):
    """保存微信公众平台 Cookie、Token、fakeid 与客户端阅读私钥凭证 (uin/key/pass_ticket)"""
    cookie = data.get("cookie", "")
    token = data.get("token", "")
    fakeid = data.get("fakeid", "")
    account_name = data.get("account_name", "")
    uin = data.get("uin", "")
    key = data.get("key", "")
    pass_ticket = data.get("pass_ticket", "")
    appmsg_token = data.get("appmsg_token", "")
    wap_sid2 = data.get("wap_sid2", "")
    raw_cookie = data.get("rawCookie") or data.get("raw_cookie", "")
    biz = data.get("biz") or data.get("client_biz", "")

    # 如果传入了客户端逆向凭证
    if uin and key and pass_ticket:
        from app.core.wechat_auth import save_wechat_client_auth
        save_wechat_client_auth(
            uin=str(uin),
            key=str(key),
            pass_ticket=str(pass_ticket),
            appmsg_token=str(appmsg_token),
            wap_sid2=str(wap_sid2),
            biz=str(biz),
            raw_cookie=str(raw_cookie)
        )
        return await check_wechat_auth_status()

    if not cookie.strip() and not str(token).strip():
        raise HTTPException(status_code=400, detail="Cookie、Token 或客户端阅读私钥不能为空")
    save_wechat_auth(
        cookie=cookie,
        token=str(token),
        fakeid=str(fakeid),
        account_name=str(account_name),
        uin=str(uin),
        key=str(key),
        pass_ticket=str(pass_ticket),
        appmsg_token=str(appmsg_token),
        wap_sid2=str(wap_sid2)
    )
    return await check_wechat_auth_status()

@app.get("/api/extension/download")
async def download_extension_zip():
    """动态打包并下载浏览器同步与住宅IP中继扩展 (v1.4.0)"""
    import io
    import zipfile
    ext_dir = frontend_dir / "extension"
    if not ext_dir.exists():
        raise HTTPException(status_code=404, detail="Extension not found")
        
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in ext_dir.rglob("*"):
            if f.is_file():
                arcname = f.relative_to(ext_dir.parent)
                zf.write(f, arcname)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=BlogDistiller-Extension-v1.4.0.zip"
        }
    )

@app.get("/api/proxy-image")
async def proxy_image(url: str):
    """代理第三方平台防盗链图片 (解决微信 mmbiz.qpic.cn 403 阻断)"""
    if not url or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid url")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": ""
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            content_type = resp.headers.get("content-type", "image/png")
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

WECHAT_CACHE_FILE = BASE_DIR / "data" / "wechat_biz_cache.json"

def generate_preset_svg_avatar(name: str) -> str:
    import urllib.parse
    char = (name or "微").strip()[0].upper()
    gradients = [
        ("#10b981", "#059669"), # 翡翠绿
        ("#3b82f6", "#2563eb"), # 科技蓝
        ("#8b5cf6", "#7c3aed"), # 极客紫
        ("#f59e0b", "#d97706"), # 琥珀金
        ("#ec4899", "#db2777"), # 珊瑚粉
        ("#06b6d4", "#0891b2")  # 极光青
    ]
    h = sum(ord(c) for c in (name or "微"))
    c1, c2 = gradients[h % len(gradients)]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="{c1}"/><stop offset="100%" stop-color="{c2}"/></linearGradient></defs><rect width="96" height="96" rx="48" fill="url(#g)"/><text x="50%" y="54%" font-family="-apple-system,BlinkMacSystemFont,PingFang SC,Microsoft YaHei,sans-serif" font-size="44" font-weight="800" fill="#ffffff" text-anchor="middle" dominant-baseline="central">{char}</text></svg>'''
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)

DEFAULT_WECHAT_BIZ_PRESETS = {
    "艺杯羹": {
        "nickname": "艺杯羹",
        "fakeid": "Mzg2MDU4MDM5NQ==",
        "avatar": "/assets/avatar.png",
        "signature": "独立开发者 · 效率工具创作者 · 专注于高质量知识管理与自动化工具开发",
        "alias": "peace-83",
        "verify_status": 1
    },
    "罗辑思维": {
        "nickname": "罗辑思维",
        "avatar": generate_preset_svg_avatar("罗辑思维"),
        "signature": "每天坚持分享启发性思维与认知升级，终身学习者的精神家园",
        "alias": "luojisiwei",
        "verify_status": 1
    },
    "代码随想录": {
        "nickname": "代码随想录",
        "avatar": generate_preset_svg_avatar("代码随想录"),
        "signature": "程序员算法与求职充电宝，坚持刷题与技术沉淀",
        "alias": "daimashuxianglu",
        "verify_status": 1
    },
    "阿里技术": {
        "nickname": "阿里技术",
        "avatar": generate_preset_svg_avatar("阿里技术"),
        "signature": "阿里巴巴官方技术号，探索技术前沿与工程实践",
        "alias": "alitech",
        "verify_status": 1
    },
    "机器之心": {
        "nickname": "机器之心",
        "avatar": generate_preset_svg_avatar("机器之心"),
        "signature": "专业的人工智能媒体和产业服务平台，追踪前沿 AI 资讯",
        "alias": "almosthuman2014",
        "verify_status": 1
    },
    "36氪": {
        "nickname": "36氪",
        "avatar": generate_preset_svg_avatar("36氪"),
        "signature": "让一部分人先看到未来，前沿商业科技与创投资讯第一站",
        "alias": "wow36kr",
        "verify_status": 1
    },
    "差评": {
        "nickname": "差评",
        "avatar": generate_preset_svg_avatar("差评"),
        "signature": "科技资讯、数码硬件评测与互联网深度八卦",
        "alias": "chaping321",
        "verify_status": 1
    },
    "半佛仙人": {
        "nickname": "半佛仙人",
        "avatar": generate_preset_svg_avatar("半佛仙人"),
        "signature": "风控老司机，用魔幻幽默剖析商业与社会底层逻辑",
        "alias": "banfoxianren",
        "verify_status": 1
    },
    "人民日报": {
        "nickname": "人民日报",
        "avatar": generate_preset_svg_avatar("人民日报"),
        "signature": "参与、沟通、记录时代，权威主流媒体官方公众号",
        "alias": "rmrbwx",
        "verify_status": 1
    },
    "央视新闻": {
        "nickname": "央视新闻",
        "avatar": generate_preset_svg_avatar("央视新闻"),
        "signature": "中央广播电视总台新闻新媒体中心官方公众号",
        "alias": "cctvnewscenter",
        "verify_status": 1
    },
    "虎嗅APP": {
        "nickname": "虎嗅APP",
        "avatar": generate_preset_svg_avatar("虎嗅APP"),
        "signature": "聚合优质商业与科技资讯，深度洞察产业趋势",
        "alias": "huxiu_com",
        "verify_status": 1
    },
    "少数派": {
        "nickname": "少数派",
        "avatar": generate_preset_svg_avatar("少数派"),
        "signature": "高效工作，品质生活，数字工具与生产力进阶指南",
        "alias": "sspaime",
        "verify_status": 1
    }
}

def _load_wechat_biz_cache() -> dict:
    cache = dict(DEFAULT_WECHAT_BIZ_PRESETS)
    if WECHAT_CACHE_FILE.exists():
        try:
            with open(WECHAT_CACHE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                cache.update(saved)
        except Exception:
            pass
    return cache

def _save_wechat_biz_cache(cache: dict):
    try:
        WECHAT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(WECHAT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

@app.get("/api/wechat/albums")
async def get_wechat_albums(biz: str = Query(..., description="公众号biz/fakeid")):
    """获取指定公众号名下已发现的所有合集专栏"""
    from app.scrapers.wechat import WeChatScraper
    albums = WeChatScraper.get_discovered_albums(biz)
    return {"success": True, "biz": biz, "albums": albums, "total": len(albums)}

@app.post("/api/wechat/albums/add")
async def add_wechat_albums(req: Dict[str, Any] = Body(...)):
    """手动或批量向指定公众号录入合集"""
    from app.scrapers.wechat import WeChatScraper
    biz = req.get("biz", "")
    author = req.get("author", "微信公众号")
    new_albums = req.get("albums", [])
    if not biz or not new_albums:
        raise HTTPException(status_code=400, detail="缺少 biz 或 albums 参数")
    albums = WeChatScraper.save_discovered_albums(biz, author, new_albums)
    return {"success": True, "biz": biz, "albums": albums, "total": len(albums)}

@app.post("/api/wechat/scan-albums")
async def scan_wechat_albums(req: Dict[str, Any] = Body(...)):
    """
    100% 免凭证 · 零风控自动扫描公众号专栏合集
    只需传入文章或合集链接，无需微信 key/uin 凭证，纯公开网络解析
    """
    target = req.get("target", "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="请提供文章或合集链接")
    
    from app.scrapers.wechat import WeChatScraper
    import re
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    author = "微信公众号"
    biz = ""
    albums = []

    # 1. 如果直接是合集链接
    if "album_id=" in target or "appmsgalbum" in target:
        m_id = re.search(r'album_id=([0-9]+)', target)
        m_biz = re.search(r'__biz=([^&#]+)', target)
        if m_id:
            album_id = m_id.group(1)
            biz = m_biz.group(1) if m_biz else ""
            try:
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                    resp = await client.get(target, headers=headers)
                    html = resp.text
                    soup = BeautifulSoup(html, "lxml")
                    title_node = soup.select_one(".album__author-name, .album__header-title, h1, .wx_follow_nickname")
                    title = title_node.text.strip() if title_node else f"专辑_{album_id}"
                    nick_node = soup.select_one(".album__author-name, .wx_follow_nickname")
                    if nick_node:
                        author = nick_node.text.strip()
                    albums.append({
                        "album_id": album_id,
                        "title": title,
                        "url": target,
                        "article_count": 0
                    })
            except Exception:
                albums.append({
                    "album_id": album_id,
                    "title": f"专辑_{album_id}",
                    "url": target,
                    "article_count": 0
                })

    # 2. 如果是推文链接，纯 HTTP GET 解析，提取作者、biz 和底部的全部合集
    elif target.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(target, headers=headers)
                html = resp.text
                
                # 提取作者
                nick_match = re.search(r'var\s+nickname\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'id="js_name">\s*([^<]+)\s*<', html)
                if nick_match:
                    author = nick_match.group(1).strip()

                # 提取 __biz
                biz_match = re.search(r'var\s+biz\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'__biz=([^&#"]+)', target) or re.search(r'__biz=([^&#"]+)', html)
                if biz_match:
                    biz = biz_match.group(1).strip()

                # 从文章提取合集
                art_albums = WeChatScraper.extract_albums_from_article_html(html, biz)
                albums.extend(art_albums)

                # 如果有 biz，尝试拉取主页 HTML 提取置顶合集 (免凭证主页预览)
                if biz:
                    home_url = f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}&scene=124#wechat_redirect"
                    try:
                        h_resp = await client.get(home_url, headers=headers, timeout=5.0)
                        if h_resp.status_code == 200:
                            home_albums = WeChatScraper.extract_albums_from_home_html(h_resp.text, biz)
                            albums.extend(home_albums)
                    except Exception:
                        pass
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析文章公开网页失败: {str(e)}")

    # 3. 本地持久化与去重
    saved_albums = []
    if biz:
        saved_albums = WeChatScraper.save_discovered_albums(biz, author, albums)
    else:
        saved_albums = albums

    return {
        "success": True,
        "biz": biz,
        "author": author,
        "albums": saved_albums,
        "total": len(saved_albums)
    }

@app.get("/api/wechat/search-biz")
async def search_wechat_biz(query: str):
    """搜索公众号 (返回真实官方高清头像、名称、简介、fakeid)"""
    import urllib.parse
    query_str = query.strip()
    if not query_str:
        return {"list": []}
    
    import re
    cache = _load_wechat_biz_cache()
    
    # 1. 如果输入的是微信文章/合集链接，直接解析文章获取 100% 真实的官方头像与公众号名称
    if query_str.startswith("http") or "mp.weixin.qq.com" in query_str:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(query_str, headers=headers)
                html = resp.text
                nick_match = re.search(r'var\s+nickname\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'id="js_name">\s*([^<]+)\s*<', html)
                avatar_match = re.search(r'var\s+round_head_img\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'var\s+ori_head_img_url\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'var\s+msg_avatar\s*=\s*["\']([^"\']+)["\']', html)
                desc_match = re.search(r'var\s+msg_desc\s*=\s*["\']([^"\']+)["\']', html)
                
                nickname = nick_match.group(1).strip() if nick_match else "微信公众号"
                raw_avatar = avatar_match.group(1).strip() if avatar_match else ""
                signature = desc_match.group(1).strip() if desc_match else "微信公众平台官方认证"
                
                if raw_avatar.startswith("/assets/") or raw_avatar.startswith("data:"):
                    avatar_proxied = raw_avatar
                elif raw_avatar:
                    avatar_proxied = f"/api/proxy-image?url={urllib.parse.quote(raw_avatar)}"
                else:
                    avatar_proxied = ""
                
                if raw_avatar and nickname:
                    cache[nickname] = {
                        "nickname": nickname,
                        "avatar": raw_avatar,
                        "signature": signature,
                        "alias": "",
                        "verify_status": 1
                    }
                    _save_wechat_biz_cache(cache)
                
                if raw_avatar or nickname:
                    return {
                        "list": [{
                            "nickname": nickname,
                            "fakeid": "",
                            "avatar": avatar_proxied,
                            "signature": signature,
                            "alias": "",
                            "verify_status": 1
                        }]
                    }
        except Exception:
            pass

    # 2. 检查本地权威已知官方公众号真实头像缓存 (仅在包含有效 fakeid 时命中)
    for key, item in cache.items():
        if (key.strip() == query_str or query_str in key or key in query_str) and item.get("fakeid"):
            raw_av = item.get("avatar", "")
            if raw_av.startswith("/assets/") or raw_av.startswith("data:"):
                av_url = raw_av
            elif raw_av:
                av_url = f"/api/proxy-image?url={urllib.parse.quote(raw_av)}"
            else:
                av_url = ""
            return {
                "list": [{
                    "nickname": item.get("nickname", query_str),
                    "fakeid": item.get("fakeid", ""),
                    "avatar": av_url,
                    "signature": item.get("signature", f"微信公众号「{query_str}」"),
                    "alias": item.get("alias", ""),
                    "verify_status": item.get("verify_status", 1)
                }]
            }

    # 3. 如果输入的是公众号名称，通过微信官方 searchbiz 接口查询真实官方头像
    from app.core.wechat_auth import get_saved_wechat_auth
    auth = get_saved_wechat_auth()
    token = auth.get("token", "")
    cookie = auth.get("cookie", "")
    
    if token and cookie:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Cookie": cookie,
            "Referer": f"https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&type=10&isMul=1&isNew=1&share=1&lang=zh_CN&token={token}"
        }
        search_url = f"https://mp.weixin.qq.com/cgi-bin/searchbiz?action=search_biz&begin=0&count=10&query={query_str}&token={token}&lang=zh_CN&f=json&ajax=1"
        try:
            async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
                resp = await client.get(search_url, headers=headers)
                data = resp.json()
                biz_list = data.get("list", [])
                if biz_list:
                    # 排序：有真实头像且认证状态高的优先
                    biz_list.sort(key=lambda x: (1 if x.get("round_head_img") else 0, x.get("verify_status", 0)), reverse=True)
                    results = []
                    for item in biz_list:
                        raw_av = item.get("round_head_img", "")
                        nick = item.get("nickname", "")
                        f_id = item.get("fakeid", "")
                        if raw_av and nick:
                            cache[nick] = {
                                "nickname": nick,
                                "fakeid": f_id,
                                "avatar": raw_av,
                                "signature": item.get("signature", ""),
                                "alias": item.get("alias", ""),
                                "verify_status": item.get("verify_status", 0)
                            }
                        if raw_av.startswith("/assets/") or raw_av.startswith("data:"):
                            av_proxied = raw_av
                        elif raw_av:
                            av_proxied = f"/api/proxy-image?url={urllib.parse.quote(raw_av)}"
                        else:
                            av_proxied = ""
                        results.append({
                            "nickname": nick,
                            "fakeid": f_id,
                            "avatar": av_proxied,
                            "signature": item.get("signature", ""),
                            "alias": item.get("alias", ""),
                            "verify_status": item.get("verify_status", 0)
                        })
                    _save_wechat_biz_cache(cache)
                    return {"list": results}
        except Exception:
            pass

    # 兜底返回占位名片（如实标注：未经微信认证核验，仅作输入回显，避免误导用户以为是已认证官方号）
    default_avatar = "/assets/avatar.png" if "艺杯羹" in query_str else generate_preset_svg_avatar(query_str)
    return {
        "list": [{
            "nickname": query_str,
            "fakeid": "",
            "avatar": default_avatar,
            "signature": f"占位名片·未经微信认证核验「{query_str}」；粘贴单篇/合集链接可免登录提取，全量检索需连接公众号后台",
            "alias": "",
            "verify_status": 0
        }]
    }
    


@app.post("/api/wechat/mp/search")
async def wechat_mp_search_articles(data: dict):
    """通过微信公众平台官方通道检索并全量拉取文章"""
    query = data.get("query", "").strip()
    max_articles = int(data.get("max_articles", 0))
    if not query:
        raise HTTPException(status_code=400, detail="公众号名称或文章链接不能为空")

    from app.scrapers.wechat import WeChatScraper
    scraper = WeChatScraper(target=query, max_articles=max_articles if max_articles > 0 else None)
    
    author_info = await scraper.get_author_info()
    author_name = author_info.get("name") or query

    try:
        articles = await scraper.get_article_list()
        return {
            "author": author_name,
            "total": len(articles),
            "articles": articles
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ================== 社区致谢墙接口 ==================
@app.get("/api/community-wall")
async def api_community_wall():
    """获取社区致谢墙公开数据（已按隐私级别脱敏）"""
    return get_wall_summary()


@app.get("/api/community-wall/admin")
async def api_admin_list_contributors(type: Optional[str] = None):
    """管理后台：列出所有贡献者（含隐私信息）"""
    contributor_type = None
    if type:
        try:
            contributor_type = ContributorType(type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的贡献类型: {type}")
    return list_contributors(contributor_type)


@app.post("/api/community-wall/admin")
async def api_admin_add_contributor(data: dict = Body(...)):
    """管理后台：新增贡献者"""
    try:
        contributor_type = ContributorType(data.get("type", "sponsor"))
        privacy = PrivacyLevel(data.get("privacy", "public"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    contributor = CommunityContributor(
        nickname=data.get("nickname", ""),
        type=contributor_type,
        privacy=privacy,
        avatar_url=data.get("avatar_url"),
        description=data.get("description", ""),
        detail=data.get("detail", ""),
        amount=data.get("amount"),
        created_at=data.get("created_at", ""),
        display_order=data.get("display_order", 0)
    )
    added = add_contributor(contributor)
    return {"success": True, "contributor": added.dict()}


@app.put("/api/community-wall/admin/{contributor_id}")
async def api_admin_update_contributor(contributor_id: str, data: dict = Body(...)):
    """管理后台：更新贡献者"""
    existing = list_contributors()
    found = None
    for c in existing:
        if c.id == contributor_id:
            found = c
            break
    if not found:
        raise HTTPException(status_code=404, detail="贡献者不存在")

    try:
        contributor_type = ContributorType(data.get("type", found.type.value))
        privacy = PrivacyLevel(data.get("privacy", found.privacy.value))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    updated = CommunityContributor(
        id=contributor_id,
        nickname=data.get("nickname", found.nickname),
        type=contributor_type,
        privacy=privacy,
        avatar_url=data.get("avatar_url", found.avatar_url),
        description=data.get("description", found.description),
        detail=data.get("detail", found.detail),
        amount=data.get("amount", found.amount),
        created_at=data.get("created_at", found.created_at),
        display_order=data.get("display_order", found.display_order)
    )
    result = update_contributor(contributor_id, updated)
    return {"success": True, "contributor": result.dict() if result else None}


# =========================================================================
# 客户端版本与安装包分发接口 (Local-First Desktop Client Distribution)
# =========================================================================

@app.get("/api/client/version")
async def get_client_version():
    """获取最新客户端版本信息与下载链接"""
    return {
        "version": "1.3.0",
        "release_notes": "支持本地优先桌面客户端架构，全流程本地运算，彻底规避云端流量、内存瓶颈与网络风控。",
        "standard_download_url": "/api/client/download",
        "win7_download_url": "/api/client/download/win7",
        "mandatory": False,
    }

@app.get("/api/client/download")
async def download_client_standard():
    """下载标准版桌面客户端 (Windows 10/11)"""
    dist_dir = BASE_DIR / "dist"
    candidates = [
        dist_dir / "BlogDistiller-Setup-x64.exe",
        dist_dir / "BlogDistiller Setup 1.3.0.exe",
        dist_dir / "BlogDistiller Setup 1.2.0.exe",
    ]
    for p in candidates:
        if p.exists():
            return FileResponse(p, filename="BlogDistiller-Setup-x64.exe", media_type="application/octet-stream")
    if dist_dir.exists():
        exes = sorted(dist_dir.glob("*.exe"), key=lambda f: f.stat().st_mtime, reverse=True)
        if exes:
            return FileResponse(exes[0], filename=exes[0].name, media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="标准版客户端安装包尚未就绪，请稍后重试或查看发布页面")

@app.get("/api/client/download/win7")
async def download_client_win7():
    """下载 Windows 7/8.1 兼容版桌面客户端"""
    dist_dir = BASE_DIR / "dist"
    # Win7 版构建产物隔离在 dist/win7/ 子目录（electron-builder 配置 builder-win7.yml 决定）
    candidates = [
        dist_dir / "win7" / "BlogDistiller-Win7-Setup-x64.exe",
        dist_dir / "win7" / "BlogDistiller-Win7.exe",
        dist_dir / "BlogDistiller-Win7-Setup-x64.exe",
    ]
    for p in candidates:
        if p.exists():
            return FileResponse(p, filename="BlogDistiller-Win7-Setup-x64.exe", media_type="application/octet-stream")
    # 兜底：取 dist/win7/ 下最新构建的安装包（electron-builder 默认命名为 "{productName} Setup {version}.exe"）
    win7_dir = dist_dir / "win7"
    if win7_dir.exists():
        exes = sorted(win7_dir.glob("*.exe"), key=lambda f: f.stat().st_mtime, reverse=True)
        if exes:
            return FileResponse(exes[0], filename="BlogDistiller-Win7-Setup-x64.exe", media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="Win7 兼容版安装包正在打包中，敬请期待")


# 明确路由
frontend_dir = BASE_DIR / "frontend"
assets_dir = frontend_dir / "assets"

if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

if OUTPUT_DIR.exists():
    app.mount("/downloads", StaticFiles(directory=str(OUTPUT_DIR)), name="downloads")

@app.api_route("/wechat", methods=["GET", "HEAD"])
@app.api_route("/wechat.html", methods=["GET", "HEAD"])
async def serve_wechat_page():
    """提供微信公众号专属控制台页面"""
    wechat_file = frontend_dir / "wechat.html"
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    if wechat_file.exists():
        return FileResponse(str(wechat_file), headers=headers)
    return FileResponse(str(frontend_dir / "index.html"), headers=headers)

@app.api_route("/app", methods=["GET", "HEAD"])
@app.api_route("/editor", methods=["GET", "HEAD"])
async def serve_app_page():
    """提供工作台页面"""
    app_file = frontend_dir / "app.html"
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    if app_file.exists():
        return FileResponse(str(app_file), headers=headers)
    return FileResponse(str(frontend_dir / "index.html"), headers=headers)

# 挂载前端静态页面
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


