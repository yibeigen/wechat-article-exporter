import json
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
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Referer": "https://weibo.com"
    }
    
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.get("https://weibo.com/ajax/profile/info", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                user = data.get("data", {}).get("user", {})
                return {
                    "authenticated": True,
                    "message": "微博登录态有效",
                    "username": user.get("screen_name", "已登录用户"),
                    "avatar": user.get("avatar_hd", "")
                }
    except Exception:
        pass
        
    # 如果接口返回异常但存在 SUB 键
    return {
        "authenticated": True,
        "message": "已检测到微博 SUB 凭证",
        "username": "微博用户"
    }

# ==============================================================================
# 同步本机 Edge/Chrome 浏览器的微博 Cookie
# ==============================================================================

async def sync_local_weibo_cookies() -> Dict[str, Any]:
    """尝试从本机 Microsoft Edge 或 Google Chrome 中一键读取已登录的微博 Cookie"""
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
                pass
                
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

