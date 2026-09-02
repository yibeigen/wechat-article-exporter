import time
import httpx
import base64
import urllib.parse
import re
from typing import Dict, Any
from app.core.wechat_auth import save_wechat_auth

class WeChatQRLoginManager:
    """微信公众平台原生扫码登录管理器 (原子化一键生成 Base64 二维码)"""
    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def _get_or_create_client(self, session_id: str) -> httpx.AsyncClient:
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "client": httpx.AsyncClient(
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Referer": "https://mp.weixin.qq.com/",
                        "Origin": "https://mp.weixin.qq.com",
                        "Accept": "application/json, text/plain, */*"
                    },
                    timeout=15.0,
                    follow_redirects=True
                ),
                "created_at": time.time(),
                "uuid": ""
            }
        return self.sessions[session_id]["client"]

    async def start_session(self, session_id: str) -> Dict[str, Any]:
        """初始化登录会话并直接获取 Base64 二维码图片"""
        client = self._get_or_create_client(session_id)
        
        startlogin_url = "https://mp.weixin.qq.com/cgi-bin/bizlogin?action=startlogin"
        payload = {
            "userlang": "zh_CN",
            "redirect_url": "",
            "login_type": "3",
            "sessionid": session_id,
            "token": "",
            "lang": "zh_CN",
            "f": "json",
            "ajax": "1"
        }
        
        resp = await client.post(
            startlogin_url,
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            }
        )
        data = resp.json()
        uuid_val = data.get("uuid", "")
        self.sessions[session_id]["uuid"] = uuid_val

        # 紧接着直接获取二维码图片
        random_ts = int(time.time() * 1000)
        qr_url = f"https://mp.weixin.qq.com/cgi-bin/scanloginqrcode?action=getqrcode&random={random_ts}"
        qr_resp = await client.get(
            qr_url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            }
        )
        img_bytes = qr_resp.content
        b64 = base64.b64encode(img_bytes).decode("utf-8") if img_bytes else ""

        return {
            "session_id": session_id,
            "uuid": uuid_val,
            "qrcode_base64": f"data:image/jpeg;base64,{b64}" if b64 else ""
        }

    async def ask_scan_status(self, session_id: str) -> Dict[str, Any]:
        """轮询二维码扫描与授权状态"""
        if session_id not in self.sessions:
            return {"status": -1, "message": "会话不存在或已过期"}

        client = self.sessions[session_id]["client"]
        random_ts = int(time.time() * 1000)
        ask_url = f"https://mp.weixin.qq.com/cgi-bin/scanloginqrcode?action=ask&token=&lang=zh_CN&f=json&ajax=1&random={random_ts}"
        
        resp = await client.get(ask_url)
        data = resp.json()
        status = data.get("status")
        user_category = data.get("user_category")

        if status == 1:
            # 手机已确认，调用 bizlogin 完成登录交换 Token 与 Cookie
            login_url = "https://mp.weixin.qq.com/cgi-bin/bizlogin?action=login"
            payload = {
                "userlang": "zh_CN",
                "redirect_url": "",
                "cookie_forbidden": "0",
                "cookie_cleaned": "0",
                "plugin_used": "0",
                "login_type": "3",
                "token": "",
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1"
            }
            login_resp = await client.post(
                login_url,
                data=payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
                }
            )
            login_data = login_resp.json()
            redirect_url = login_data.get("redirect_url") or ""

            token = ""
            if "token=" in redirect_url:
                parsed = urllib.parse.urlparse(redirect_url)
                params = urllib.parse.parse_qs(parsed.query)
                token = params.get("token", [""])[0]

            # 汇总 Cookie
            cookie_dict = {}
            for k, v in client.cookies.items():
                cookie_dict[k] = v
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])

            if token:
                account_name = "我的微信公众号"
                avatar_url = ""
                try:
                    home_url = f"https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token={token}"
                    home_resp = await client.get(home_url)
                    text = home_resp.text
                    name_m = re.search(r'class="weui-desktop-account__nickname">([^<]+)<', text) or \
                             re.search(r'class="user_name"[^>]*>([^<]+)<', text) or \
                             re.search(r'nickname\s*[:=]\s*["\']([^"\']+)["\']', text)
                    if name_m:
                        account_name = name_m.group(1).strip()

                    avatar_m = re.search(r'class="weui-desktop-account__avatar"\s+src="([^"]+)"', text) or \
                               re.search(r'class="avatar"[^>]*src="([^"]+)"', text) or \
                               re.search(r'headimg\s*[:=]\s*["\']([^"\']+)["\']', text) or \
                               re.search(r'head_img\s*[:=]\s*["\']([^"\']+)["\']', text)
                    if avatar_m:
                        avatar_url = avatar_m.group(1).strip()
                except Exception:
                    pass

                # 保存凭证
                save_wechat_auth(
                    cookie=cookie_str,
                    token=token,
                    account_name=account_name,
                    avatar=avatar_url
                )

                return {
                    "status": 1,
                    "token": token,
                    "account_name": account_name,
                    "avatar": avatar_url,
                    "message": "登录成功！官方通道已打通"
                }

        return {
            "status": status,
            "user_category": user_category,
            "message": "已扫码，请在手机微信上点击确认" if status == 4 else ("二维码已过期" if status == 2 else "等待扫码")
        }

qr_login_manager = WeChatQRLoginManager()
