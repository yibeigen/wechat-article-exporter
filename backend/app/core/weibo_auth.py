import json
import sys
import httpx
from pathlib import Path
from typing import Dict, Any
from app.config import BASE_DIR

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WEIBO_AUTH_FILE = DATA_DIR / "weibo_session.json"

def get_saved_weibo_cookies() -> Dict[str, str]:
    """读取本地持久化保存的微博 Cookies"""
    if WEIBO_AUTH_FILE.exists():
        try:
            with open(WEIBO_AUTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def parse_weibo_cookie_string(cookie_str: str) -> Dict[str, str]:
    """将字符串格式的微博 Cookie 转换为字典"""
    cookie_dict = {}
    for item in cookie_str.strip().split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            cookie_dict[k.strip()] = v.strip()
    return cookie_dict

def save_weibo_cookies(cookies: Dict[str, str]) -> None:
    """持久化保存微博 Cookies"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(WEIBO_AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)

def get_weibo_cookie_string() -> str:
    """获取拼接好的 Cookie 字符串"""
    cookies = get_saved_weibo_cookies()
    if not cookies:
        return ""
    return "; ".join([f"{k}={v}" for k, v in cookies.items()])

async def check_weibo_auth_status() -> Dict[str, Any]:
    """检测当前保存的微博 Cookie 是否有效"""
    cookies = get_saved_weibo_cookies()
    cookie_str = get_weibo_cookie_string()
    
    if not cookies or "SUB" not in cookies:
        return {
            "authenticated": False,
            "message": "未配置微博登录凭证 (SUB)，请使用插件自动同步或在浏览器登录后配置",
            "username": None
        }
        
    # 探测接口选型说明（2026-09-29 实测结论）：
    # 旧方案 weibo.com/ajax/profile/info 已不可用——微博对该接口做了非浏览器
    # TLS 指纹拦截，httpx 无论带什么请求头都固定返回 400，导致旧代码长期
    # 走“兜底假已连接”、用户看到假昵称“微博用户”。
    # 新方案改用 m.weibo.cn/api/config：与实际抓取走完全相同的通道，
    # 实测带 Cookie 返回 login=true、不带 Cookie 返回 login=false，
    # 能精确区分“登录态有效 / 已失效”，状态显示与真实抓取能力永远一致。
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Cookie": cookie_str,
        "Referer": "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://m.weibo.cn/api/config", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", {}) or {}
                if data.get("login"):
                    # login=true → 登录态确凿有效
                    # 顺手用返回的 uid 再查一次昵称，让界面显示真实账号名
                    # （昵称查询失败不影响“已连接”的判定）
                    username = "已登录用户"
                    uid = str(data.get("uid") or "")
                    if uid:
                        try:
                            r2 = await client.get(
                                f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}",
                                headers=headers
                            )
                            user_info = r2.json().get("data", {}).get("userInfo", {}) or {}
                            username = user_info.get("screen_name") or username
                        except Exception:
                            pass
                    return {
                        "authenticated": True,
                        "message": "微博登录态有效",
                        "username": username
                    }
                # login=false：Cookie 已被微博判定为未登录（过期/被踢）
                return {
                    "authenticated": False,
                    "message": "微博登录态已失效，请点击【一键同步本机浏览器】或【官方扫码登录】重新连接",
                    "username": None
                }
            # 非 200：探测通道异常，如实报告，绝不假装已连接
            return {
                "authenticated": False,
                "message": f"微博状态探测异常 (HTTP {resp.status_code})，请点击刷新按钮重新检测",
                "username": None
            }
    except Exception:
        # 网络异常导致探测失败：如实报告“未验证”，避免界面与抓取结果互相矛盾
        return {
            "authenticated": False,
            "message": "微博状态探测失败（网络异常），请点击刷新按钮重新检测",
            "username": None
        }

# ==============================================================================
# 同步本机 Edge/Chrome 浏览器的微博 Cookie
# ==============================================================================

async def sync_local_weibo_cookies() -> Dict[str, Any]:
    """尝试从本机 Microsoft Edge 或 Google Chrome 中一键读取已登录的微博 Cookie"""
    # 仅 Windows 支持：Cookie 解密依赖 Windows 独有的 DPAPI（与知乎同步同理）
    if sys.platform != "win32":
        return {
            "success": False,
            "message": "Mac 版暂不支持「一键同步本机浏览器」（Cookie 由 macOS Keychain 加密，无法直接读取）。\n\n请改用【📱 官方扫码登录】或【📋 手动 Cookie】连接微博。"
        }
    import os
    import shutil
    import sqlite3
    import base64
    from app.core.zhihu_auth import _decrypt_dpapi, _decrypt_v10

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    browser_dirs = [
        ("Edge", Path(local_app_data) / "Microsoft" / "Edge" / "User Data"),
        ("Chrome", Path(local_app_data) / "Google" / "Chrome" / "User Data"),
    ]
    
    extracted_cookies = {}
    found_browser = ""
    db_locked = False  # 标记是否因浏览器正在运行导致 Cookie 数据库被锁（用于最后给出可操作的提示）

    for browser_name, user_data_dir in browser_dirs:
        local_state_path = user_data_dir / "Local State"
        cookie_db_path = user_data_dir / "Default" / "Network" / "Cookies"
        
        if not local_state_path.exists() or not cookie_db_path.exists():
            continue
            
        try:
            with open(local_state_path, "r", encoding="utf-8") as f:
                local_state = json.load(f)
            encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
            master_key = _decrypt_dpapi(encrypted_key[5:])
            
            temp_db = DATA_DIR / f"temp_weibo_{browser_name.lower()}_cookies.db"
            try:
                shutil.copy2(cookie_db_path, temp_db)
            except Exception:
                # 新版 Edge/Chrome 运行时会独占锁定 Cookies 数据库（官方防窃取机制），
                # 浏览器开着时复制必然失败。这里记下原因，最后给用户可操作的提示，
                # 而不是弹一个让人摸不着头脑的笼统报错
                db_locked = True
                
            if not temp_db.exists():
                continue
                
            conn = sqlite3.connect(str(temp_db))
            cursor = conn.cursor()
            cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%weibo.com%' OR host_key LIKE '%weibo.cn%'")
            
            cookies = {}
            for host, name, encrypted_value in cursor.fetchall():
                try:
                    if encrypted_value[:3] in [b'v10', b'v11', b'v20']:
                        val = _decrypt_v10(master_key, encrypted_value)
                        cookies[name] = val
                    else:
                        val = _decrypt_dpapi(encrypted_value).decode('utf-8', errors='ignore')
                        cookies[name] = val
                except Exception:
                    pass
                    
            conn.close()
            if temp_db.exists():
                try:
                    temp_db.unlink()
                except Exception:
                    pass
                    
            if cookies and "SUB" in cookies:
                extracted_cookies = cookies
                found_browser = browser_name
                break
            elif cookies and not extracted_cookies:
                extracted_cookies = cookies
                found_browser = browser_name
        except Exception as e:
            print(f"尝试读取 {browser_name} 微博 Cookie 异常: {e}")
            
    if extracted_cookies and "SUB" in extracted_cookies:
        save_weibo_cookies(extracted_cookies)
        status = await check_weibo_auth_status()
        return {
            "success": True,
            "browser": found_browser,
            "message": f"成功从本机 {found_browser} 同步微博登录凭证！",
            "user_info": status
        }
    elif extracted_cookies:
        save_weibo_cookies(extracted_cookies)
        return {
            "success": True,
            "browser": found_browser,
            "message": f"从本机 {found_browser} 读取到了基础游客 Cookie（未检测到已登录的 SUB 凭证，建议扫码登录）",
            "user_info": {"authenticated": False}
        }
    else:
        # 失败提示要分清两种原因：数据库被浏览器锁住 vs 浏览器里根本没登录微博
        if db_locked:
            return {
                "success": False,
                "message": "Edge / Chrome 正在运行，新版浏览器会锁死 Cookie 数据库导致无法读取。\n\n"
                           "解决办法（二选一）：\n"
                           "1. 完全退出所有浏览器窗口（含托盘后台进程）后，再点【一键同步本机浏览器】；\n"
                           "2. 直接点【官方扫码登录】扫码授权，无需关闭浏览器。"
            }
        return {
            "success": False,
            "message": "未能直接从本机 Edge / Chrome 读取到微博登录态。\n建议点击【官方扫码登录】扫码授权，或在已登录微博的浏览器按 F12 复制 Cookie 手动填入。"
        }

def _launch_weibo_qr_login_sync() -> Dict[str, Any]:
    """同步弹出浏览器窗口供用户扫码登录微博"""
    import time
    from playwright.sync_api import sync_playwright
    
    try:
        with sync_playwright() as p:
            launch_args = [
                '--window-size=920,720',
                '--window-position=200,100',
                '--disable-blink-features=AutomationControlled'
            ]
            try:
                browser = p.chromium.launch(channel="msedge", headless=False, args=launch_args)
            except Exception:
                browser = p.chromium.launch(headless=False, args=launch_args)
                
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
                viewport={'width': 880, 'height': 680}
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
            
            page.goto("https://weibo.com/login.php", wait_until="domcontentloaded", timeout=45000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            
            logged_in = False
            for _ in range(120):
                time.sleep(1)
                try:
                    cookies_list = context.cookies()
                except Exception:
                    break
                    
                cookie_dict = {c["name"]: c["value"] for c in cookies_list if ".weibo.com" in c.get("domain", "") or ".weibo.cn" in c.get("domain", "")}
                if "SUB" in cookie_dict:
                    save_weibo_cookies(cookie_dict)
                    logged_in = True
                    break
            
            try:
                browser.close()
            except Exception:
                pass
                
            if logged_in:
                return {
                    "success": True,
                    "message": "微博扫码登录成功，凭证已保存！"
                }
            else:
                return {
                    "success": False,
                    "message": "登录超时或用户取消了登录窗口"
                }
    except Exception as e:
        return {
            "success": False,
            "message": f"启动微博扫码登录失败: {str(e)}"
        }

async def launch_weibo_qr_login() -> Dict[str, Any]:
    """弹出浏览器窗口供用户扫码登录微博，登录后自动保存 Session 并关闭窗口"""
    import asyncio
    res = await asyncio.to_thread(_launch_weibo_qr_login_sync)
    if res.get("success"):
        status = await check_weibo_auth_status()
        res["user_info"] = status
    return res

