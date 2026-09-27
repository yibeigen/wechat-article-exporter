"""
微信读书 (weread.qq.com) 登录态管理模块
=========================================
用途：为「微信读书通道」抓取公众号历史文章提供登录凭证。

- 凭证来源：Playwright 弹窗扫码登录（与知乎扫码登录同一模式）
- 关键 Cookie：wr_vid / wr_skey / wr_rt
- 存储位置：data/weread_session.json
- 失效特征：接口返回 errCode -2010 / -2012 / -2041，或 HTTP 401/403
"""
import json
import time
import threading
from typing import Dict, Any

import httpx

from app.config import BASE_DIR, DEFAULT_HEADERS

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = DATA_DIR / "weread_session.json"

WEREAD_HOST = "https://weread.qq.com"

# 已知的登录失效错误码（社区实测汇总）
AUTH_ERR_CODES = (-2010, -2012, -2041)

# ==============================================================================
# 1. Cookie 存储与加载
# ==============================================================================

def get_saved_weread_cookies() -> Dict[str, str]:
    """读取本地已保存的微信读书 Session Cookies"""
    if not SESSION_FILE.exists():
        return {}
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data.get("cookies", {}) if "cookies" in data else data
    except Exception as e:
        print(f"读取微信读书 Session 异常: {e}")
    return {}


def save_weread_cookies(cookies: Dict[str, str]):
    """持久化保存微信读书 Cookies"""
    payload = {
        "cookies": cookies,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"微信读书 Cookies 已持久化至 {SESSION_FILE}")
    except Exception as e:
        print(f"保存微信读书 Cookies 异常: {e}")


def build_weread_cookie_str(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def build_weread_headers(cookies: Dict[str, str]) -> Dict[str, str]:
    """构造微信读书 Web API 请求头"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://weread.qq.com/",
        "Cookie": build_weread_cookie_str(cookies),
    }


def is_auth_error(data: Any) -> bool:
    """判断接口响应是否为登录失效"""
    if not isinstance(data, dict):
        return False
    err_code = data.get("errCode") or data.get("errcode") or 0
    try:
        err_code = int(err_code)
    except (TypeError, ValueError):
        err_code = 0
    return err_code in AUTH_ERR_CODES

# ==============================================================================
# 2. 校验登录态
# ==============================================================================

async def check_weread_auth_status() -> Dict[str, Any]:
    """检查当前保存的微信读书 Cookie 是否有效（走轻量书架接口探测）"""
    cookies = get_saved_weread_cookies()
    if not cookies or "wr_skey" not in cookies:
        return {
            "is_logged_in": False,
            "has_cookie": bool(cookies),
            "message": "未检测到有效的微信读书登录凭证 (wr_skey)"
        }

    headers = build_weread_headers(cookies)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            resp = await client.get(
                f"{WEREAD_HOST}/web/shelf/sync",
                params={"userVid": "", "synckey": 0, "lectureSynckey": 0},
            )
            if resp.status_code in (401, 403):
                return {
                    "is_logged_in": False,
                    "has_cookie": True,
                    "message": "微信读书 Cookie 已过期 (HTTP 401/403)，请重新扫码登录"
                }
            data = resp.json()
            if is_auth_error(data):
                return {
                    "is_logged_in": False,
                    "has_cookie": True,
                    "message": f"微信读书 Cookie 已过期 (errCode {data.get('errCode') or data.get('errcode')})，请重新扫码登录"
                }
            book_count = len(data.get("books", [])) if isinstance(data, dict) else 0
            return {
                "is_logged_in": True,
                "has_cookie": True,
                "book_count": book_count,
                "message": f"微信读书已连接（书架 {book_count} 本）"
            }
    except Exception as e:
        return {
            "is_logged_in": False,
            "has_cookie": True,
            "message": f"校验微信读书登录态失败: {str(e)}"
        }

# ==============================================================================
# 3. 扫码登录 (Playwright 弹窗一次性登录，与知乎登录同一模式)
# ==============================================================================

_LOGIN_STATE: Dict[str, Any] = {"running": False, "result": None}


def _launch_weread_qr_login_sync() -> Dict[str, Any]:
    """同步弹出浏览器窗口供用户扫码登录微信读书（专属工作线程中运行）"""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            launch_args = [
                "--window-size=920,720",
                "--window-position=200,100",
                "--disable-blink-features=AutomationControlled",
            ]
            try:
                browser = p.chromium.launch(channel="msedge", headless=False, args=launch_args)
            except Exception:
                browser = p.chromium.launch(headless=False, args=launch_args)

            context = browser.new_context(
                user_agent=DEFAULT_HEADERS["User-Agent"],
                viewport={"width": 880, "height": 680},
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )

            page.goto(WEREAD_HOST, wait_until="domcontentloaded", timeout=45000)
            try:
                page.bring_to_front()
            except Exception:
                pass

            # 页面上若存在登录按钮则自动点开二维码面板
            try:
                page.click("text=登录", timeout=5000)
            except Exception:
                pass

            # 轮询等待登录成功（最多等待 180 秒）
            logged_in = False
            for _ in range(180):
                time.sleep(1)
                try:
                    cookies_list = context.cookies()
                except Exception:
                    # 用户可能手动关闭了窗口
                    break

                cookie_dict = {
                    c["name"]: c["value"]
                    for c in cookies_list
                    if "weread.qq.com" in c.get("domain", "")
                }
                if "wr_skey" in cookie_dict and "wr_vid" in cookie_dict:
                    save_weread_cookies(cookie_dict)
                    logged_in = True
                    break

            try:
                browser.close()
            except Exception:
                pass

            if logged_in:
                return {"success": True, "message": "微信读书扫码登录成功，凭证已保存！"}
            return {"success": False, "message": "登录超时或窗口被关闭，请重试"}

    except Exception as e:
        return {"success": False, "message": f"启动微信读书扫码登录失败: {str(e)}"}


def launch_weread_qr_login() -> Dict[str, Any]:
    """在专属工作线程中启动扫码登录（立即返回，不阻塞 FastAPI 事件循环）"""
    if _LOGIN_STATE["running"]:
        return {"started": False, "message": "扫码登录窗口已在运行中，请直接扫码"}

    def _worker():
        _LOGIN_STATE["running"] = True
        _LOGIN_STATE["result"] = None
        try:
            _LOGIN_STATE["result"] = _launch_weread_qr_login_sync()
        finally:
            _LOGIN_STATE["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "message": "扫码登录窗口已弹出，请使用微信扫码登录微信读书"}


def get_weread_login_progress() -> Dict[str, Any]:
    """查询扫码登录进度"""
    if _LOGIN_STATE["running"]:
        return {"running": True, "message": "等待扫码中..."}
    result = _LOGIN_STATE.get("result")
    if result:
        return {"running": False, **result}
    return {"running": False, "success": False, "message": "尚未发起扫码登录"}
