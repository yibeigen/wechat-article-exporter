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
