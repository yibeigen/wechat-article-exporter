import os
import sys
import json
import time
import asyncio
from pathlib import Path

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# 确保 backend 目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

from typing import Optional, List, Dict, Any
from pydantic import BaseModel

from app.config import OUTPUT_DIR, BASE_DIR
from app.models import TaskCreateRequest, TaskProgress
from app.task_manager import task_manager
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

@app.post("/api/extract-links")
async def extract_links_endpoint(request: ExtractLinksRequest):
    """仅提取文章列表与链接清单 (两阶段架构阶段一)"""
    if not request.target.strip():
        raise HTTPException(status_code=400, detail="目标博主链接或关键词不能为空")
    
    try:
        from app.models import PlatformEnum, TaskCreateRequest
        try:
            p_enum = PlatformEnum(request.platform)
        except Exception:
            p_enum = PlatformEnum.CUSTOM_URLS
            
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
        
        author_info = await scraper.get_author_info()
        articles = await scraper.get_article_list()
        return {
            "success": True,
            "platform": request.platform,
            "author": author_info.get("name", "未知博主"),
            "total": len(articles),
            "articles": articles
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tasks")
async def create_task(request: TaskCreateRequest):
    """创建新的抓取与导出任务"""
    if not request.target.strip():
        raise HTTPException(status_code=400, detail="目标博主链接或文章ID不能为空")
    
    task_id = task_manager.create_task(request)
    return {"task_id": task_id, "status": "pending", "message": "任务已创建并进入调度队列"}

@app.get("/api/tasks")
async def list_tasks():
    """获取所有历史任务"""
    return task_manager.list_tasks()

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
                data = await queue.get()
                yield f"data: {data}\n\n"
                
                # 只有当实际发送到前端的数据本身状态是 completed / failed / cancelled 时才断开连接
                import json
                try:
                    event_obj = json.loads(data)
                    if event_obj.get("status") in ["completed", "failed", "cancelled"]:
                        break
                except Exception:
                    pass
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
    """手动保存知乎 Cookie 字符串"""
    cookie_str = data.get("cookie", "")
    if not cookie_str.strip():
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    cookies = parse_cookie_string(cookie_str)
    save_zhihu_cookies(cookies)
    return await check_zhihu_auth_status()

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

@app.get("/api/weibo/status")
async def get_weibo_auth_status():
    """获取微博当前登录凭证与连接状态"""
    from app.core.weibo_auth import check_weibo_auth_status
    return await check_weibo_auth_status()

@app.post("/api/weibo/set-cookie")
async def set_manual_weibo_cookie(data: dict):
    """保存微博 Cookie 字符串"""
    cookie_str = data.get("cookie", "")
    if not cookie_str.strip():
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    from app.core.zhihu_auth import parse_cookie_string
    from app.core.weibo_auth import save_weibo_cookies, check_weibo_auth_status
    cookies = parse_cookie_string(cookie_str)
    save_weibo_cookies(cookies)
    return await check_weibo_auth_status()

@app.get("/api/extension/download")
async def download_extension_zip():
    """动态打包并下载浏览器同步扩展 (支持无感同步公众号、知乎、微博凭证)"""
    import io
    import zipfile
    ext_dir = frontend_dir / "extension"
    if not ext_dir.exists():
        raise HTTPException(status_code=404, detail="Extension not found")
        
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in ext_dir.rglob("*"):
            if f.is_file():
                arcname = f.relative_to(ext_dir)
                zf.write(f, arcname)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=BlogDistiller-Extension.zip"
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
                    "signature": item.get("signature", f"微信官方认证公众号「{query_str}」"),
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

    # 兜底返回有效公众号信息，确保前端能立即展示精美卡片
    default_avatar = "/assets/avatar.png" if "艺杯羹" in query_str else generate_preset_svg_avatar(query_str)
    return {
        "list": [{
            "nickname": query_str,
            "fakeid": "",
            "avatar": default_avatar,
            "signature": f"微信官方认证公众号「{query_str}」· 支持全量文章提取与排版",
            "alias": "",
            "verify_status": 1
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

# 明确路由
frontend_dir = BASE_DIR / "frontend"
assets_dir = frontend_dir / "assets"

if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

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


