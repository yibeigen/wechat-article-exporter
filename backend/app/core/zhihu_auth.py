import os
import json
import base64
import sqlite3
import shutil
import asyncio
import ctypes
import ctypes.wintypes
from pathlib import Path
from typing import Dict, Any, Optional, List
import httpx

from app.config import BASE_DIR, DEFAULT_HEADERS

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = DATA_DIR / "zhihu_session.json"

# ==============================================================================
# 1. Cookie 存储与加载
# ==============================================================================

def get_saved_zhihu_cookies() -> Dict[str, str]:
    """读取本地已保存的知乎 Session Cookies"""
    if not SESSION_FILE.exists():
        return {}
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"读取知乎 Session 异常: {e}")
    return {}

def save_zhihu_cookies(cookies: Dict[str, str]):
    """持久化保存知乎 Cookies"""
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"知乎 Cookies 已成功持久化至 {SESSION_FILE}")
    except Exception as e:
        print(f"保存知乎 Cookies 异常: {e}")

def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """将字符串格式的 Cookie 转换为字典"""
    cookie_dict = {}
    for item in cookie_str.strip().split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            cookie_dict[k.strip()] = v.strip()
    return cookie_dict

# ==============================================================================
# 2. 校验知乎登录态
# ==============================================================================

async def check_zhihu_auth_status() -> Dict[str, Any]:
    """检查当前保存的知乎 Cookie 是否有效"""
    cookies = get_saved_zhihu_cookies()
    if not cookies or "z_c0" not in cookies:
        return {
            "is_logged_in": False,
            "has_cookie": bool(cookies),
            "message": "未检测到有效的知乎登录凭证 (z_c0)"
        }
    
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.zhihu.com/",
        "x-requested-with": "fetch"
    }
    
    try:
        async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=8.0) as client:
            resp = await client.get("https://www.zhihu.com/api/v4/me")
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "is_logged_in": True,
                    "has_cookie": True,
                    "user_name": data.get("name", "知乎已认证用户"),
                    "url_token": data.get("url_token", ""),
                    "avatar_url": data.get("avatar_url", ""),
                    "headline": data.get("headline", ""),
                    "message": f"知乎已连接：{data.get('name')}"
                }
            else:
                return {
                    "is_logged_in": False,
                    "has_cookie": True,
                    "message": "已保存的 Cookie 已过期，请重新同步或扫码登录"
                }
    except Exception as e:
        return {
            "is_logged_in": False,
            "has_cookie": True,
            "message": f"校验知乎登录态失败: {str(e)}"
        }

# ==============================================================================
# 3. 扫码登录 (Playwright 弹窗一次性登录)
# ==============================================================================

def _launch_zhihu_qr_login_sync() -> Dict[str, Any]:
    """同步弹出浏览器窗口供用户扫码登录（运行在专属工作线程中，彻底避免 Windows 异步子进程冲突）"""
    import time
    from playwright.sync_api import sync_playwright
    
    try:
        with sync_playwright() as p:
            # 优先使用系统安装的 Edge，其次 Chromium
            launch_args = [
                '--window-size=920,720',
                '--window-position=200,100',
                '--disable-blink-features=AutomationControlled'
            ]
            try:
                browser = p.chromium.launch(
                    channel="msedge",
                    headless=False,
                    args=launch_args
                )
            except Exception:
                browser = p.chromium.launch(
                    headless=False,
                    args=launch_args
                )
                
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
                viewport={'width': 880, 'height': 680}
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
            
            page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded", timeout=45000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            
            # 轮询等待登录成功（最多等待 120 秒）
            logged_in = False
            for _ in range(120):
                time.sleep(1)
                try:
                    cookies_list = context.cookies()
                except Exception:
                    # 用户可能手动关闭了窗口
                    break
                    
                cookie_dict = {c["name"]: c["value"] for c in cookies_list if ".zhihu.com" in c.get("domain", "")}
                if "z_c0" in cookie_dict:
                    save_zhihu_cookies(cookie_dict)
                    logged_in = True
                    break
                
                try:
                    # 若 URL 已经跳出了 signin 页面
                    if "signin" not in page.url and page.url != "about:blank":
                        save_zhihu_cookies(cookie_dict)
                        logged_in = True
                        break
                except Exception:
                    break
            
            try:
                browser.close()
            except Exception:
                pass
                
            if logged_in:
                return {
                    "success": True,
                    "message": "知乎扫码登录成功，凭证已保存！"
                }
            else:
                return {
                    "success": False,
                    "message": "登录超时或用户取消了登录窗口"
                }
    except Exception as e:
        return {
            "success": False,
            "message": f"启动扫码登录失败: {str(e)}"
        }

async def launch_zhihu_qr_login() -> Dict[str, Any]:
    """弹出浏览器窗口供用户扫码登录，登录后自动保存 Session 并关闭窗口"""
    res = await asyncio.to_thread(_launch_zhihu_qr_login_sync)
    if res.get("success"):
        status = await check_zhihu_auth_status()
        res["user_info"] = status
    return res

# ==============================================================================
# 4. 同步本机 Edge/Chrome 浏览器的知乎 Cookie
# ==============================================================================

def _decrypt_dpapi(encrypted_bytes: bytes) -> bytes:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', ctypes.wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]
    pDataIn = DATA_BLOB(len(encrypted_bytes), ctypes.cast(encrypted_bytes, ctypes.POINTER(ctypes.c_char)))
    pDataOut = DATA_BLOB()
    flags = 0x01
    res = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(pDataIn), None, None, None, None, flags, ctypes.byref(pDataOut))
    if not res:
        raise ctypes.WinError()
    decrypted_bytes = ctypes.string_at(pDataOut.pbData, pDataOut.cbData)
    ctypes.windll.kernel32.LocalFree(pDataOut.pbData)
    return decrypted_bytes

def _decrypt_v10(master_key: bytes, encrypted_value: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = encrypted_value[3:15]
    ciphertext = encrypted_value[15:]
    aesgcm = AESGCM(master_key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode('utf-8', errors='ignore')

async def sync_local_browser_cookies() -> Dict[str, Any]:
    """尝试从本机 Microsoft Edge 或 Google Chrome 中一键读取已登录的知乎 Cookie"""
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
            
            temp_db = DATA_DIR / f"temp_{browser_name.lower()}_cookies.db"
            try:
                shutil.copy2(cookie_db_path, temp_db)
            except Exception:
                # 若被占用，尝试通过 powershell 复制或忽略
                pass
                
            if not temp_db.exists():
                continue
                
            conn = sqlite3.connect(str(temp_db))
            cursor = conn.cursor()
            cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%zhihu.com%'")
            
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
                temp_db.unlink()
                
            if cookies and "z_c0" in cookies:
                extracted_cookies = cookies
                found_browser = browser_name
                break
            elif cookies and not extracted_cookies:
                extracted_cookies = cookies
                found_browser = browser_name
        except Exception as e:
            print(f"尝试读取 {browser_name} Cookie 异常: {e}")
            
    if extracted_cookies and "z_c0" in extracted_cookies:
        save_zhihu_cookies(extracted_cookies)
        status = await check_zhihu_auth_status()
        return {
            "success": True,
            "browser": found_browser,
            "message": f"成功从本机 {found_browser} 同步知乎登录凭证！",
            "user_info": status
        }
    elif extracted_cookies:
        save_zhihu_cookies(extracted_cookies)
        return {
            "success": True,
            "browser": found_browser,
            "message": f"从本机 {found_browser} 读取到了基础游客 Cookie（未检测到已登录账号，建议扫码登录）",
            "user_info": {"is_logged_in": False, "has_cookie": True}
        }
    else:
        return {
            "success": False,
            "message": "提示：因当前 Edge / Chrome 浏览器正在运行并锁定了本地 Cookie 数据库，外部无法直接读取。\n\n【推荐方案】：\n1. 请直接点击旁边的【扫码登录 (永久免登)】，桌面将弹出窗口，手机知乎 App 扫码一次即可永久保存！\n2. 或者在已登录知乎的标签页按 F12 输入 document.cookie 复制并点击【手动 Cookie】粘贴保存。"
        }
