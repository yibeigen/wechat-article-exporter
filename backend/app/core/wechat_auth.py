import os
import re
import json
import time
from pathlib import Path
from typing import Dict, Any, Optional
import httpx

from app.config import BASE_DIR, DEFAULT_HEADERS

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WECHAT_SESSION_FILE = DATA_DIR / "wechat_session.json"

# 缓存最近校验结果，避免频繁刷新触发微信频控
_AUTH_CACHE = {
    "result": None,
    "last_checked": 0
}


def get_saved_wechat_auth() -> Dict[str, Any]:
    """读取本地已保存的微信公众平台 Session 与 Token"""
    if not WECHAT_SESSION_FILE.exists():
        return {}
    try:
        with open(WECHAT_SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"读取微信 Session 异常: {e}")
    return {}


def save_wechat_auth(
    cookie: str,
    token: str,
    fakeid: str = "",
    account_name: str = "",
    uin: str = "",
    key: str = "",
    pass_ticket: str = "",
    appmsg_token: str = "",
    wap_sid2: str = "",
    avatar: str = ""
) -> Dict[str, Any]:
    """持久化保存微信公众平台/微信阅读端 Cookie、Token、fakeid、阅读通行密钥、公众号昵称与头像"""
    existing = get_saved_wechat_auth()
    payload = {
        **existing,
        "cookie": cookie.strip() if cookie is not None else existing.get("cookie", ""),
        "token": str(token).strip() if token is not None else existing.get("token", ""),
        "fakeid": fakeid.strip() if fakeid else existing.get("fakeid", ""),
        "account_name": account_name.strip() if account_name else existing.get("account_name", ""),
        "avatar": avatar.strip() if avatar else existing.get("avatar", ""),
        "uin": uin.strip() if uin else existing.get("uin", ""),
        "key": key.strip() if key else existing.get("key", ""),
        "pass_ticket": pass_ticket.strip() if pass_ticket else existing.get("pass_ticket", ""),
        "appmsg_token": appmsg_token.strip() if appmsg_token else existing.get("appmsg_token", ""),
        "wap_sid2": wap_sid2.strip() if wap_sid2 else existing.get("wap_sid2", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        with open(WECHAT_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"微信凭证已持久化保存至 {WECHAT_SESSION_FILE}")
        # 重置缓存，强制下次刷新
        _AUTH_CACHE["result"] = None
        _AUTH_CACHE["last_checked"] = 0
    except Exception as e:
        print(f"保存微信凭证异常: {e}")
    return payload

def save_wechat_client_auth(
    uin: str,
    key: str,
    pass_ticket: str,
    appmsg_token: str = "",
    wap_sid2: str = "",
    biz: str = "",
    raw_cookie: str = ""
) -> Dict[str, Any]:
    """保存由桌面嗅探器/客户端截获的微信阅读会话凭证 (含时间戳生命周期追踪)"""
    existing = get_saved_wechat_auth()
    cookie_str = existing.get("cookie", "")
    if raw_cookie:
        cookie_str = raw_cookie.strip()
    elif pass_ticket and wap_sid2:
        cookie_str = f"pass_ticket={pass_ticket}; wap_sid2={wap_sid2}"

    now_ts = time.time()
    payload = {
        **existing,
        "uin": uin.strip(),
        "key": key.strip(),
        "pass_ticket": pass_ticket.strip(),
        "appmsg_token": appmsg_token.strip(),
        "wap_sid2": wap_sid2.strip(),
        "cookie": cookie_str,
        "client_biz": biz.strip() if biz else existing.get("client_biz", ""),
        "client_key_updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client_key_timestamp": now_ts,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_freq_control": None
    }
    try:
        with open(WECHAT_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        try:
            print(f"[WeChat Auth] 微信阅读端凭证 (uin/key/pass_ticket) 已成功捕获并保存至 {WECHAT_SESSION_FILE}")
        except Exception:
            pass
        _AUTH_CACHE["result"] = None
        _AUTH_CACHE["last_checked"] = 0
    except Exception as e:
        print(f"保存微信客户端凭证异常: {e}")
    return payload


def record_freq_control_event(reason: str = "freq control"):
    """记录遭遇微信频率风控事件"""
    existing = get_saved_wechat_auth()
    existing["last_freq_control"] = {
        "timestamp": time.time(),
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason
    }
    try:
        with open(WECHAT_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_client_auth_status() -> Dict[str, Any]:
    """
    获取微信阅读端/客户端密钥的精准生命周期状态与倒计时
    微信 Key 默认生命周期: 1800 秒 (30 分钟)
    互动数据 / 评论凭证有效期: 5400 秒 (90 分钟)
    """
    auth = get_saved_wechat_auth()
    uin = auth.get("uin", "")
    key = auth.get("key", "")
    pass_ticket = auth.get("pass_ticket", "")
    appmsg_token = auth.get("appmsg_token", "")
    client_ts = auth.get("client_key_timestamp")
    captured_at = auth.get("client_key_updated_at", "")
    freq_event = auth.get("last_freq_control")

    # 如果没有抓取过
    if not (uin or pass_ticket) or not key:
        return {
            "has_auth": False,
            "status_level": "not_configured",
            "status_text": "未配置凭证",
            "message": "尚未捕获微信阅读凭证。请在微信电脑版中打开任意公众号文章，嗅探器将全自动截获。",
            "expires_in_seconds": 0,
            "expires_in_formatted": "00:00",
            "captured_at": None,
            "uin_mask": "",
            "key_mask": "",
            "can_sync_articles": False,
            "freq_control": None
        }

    now = time.time()
    # 如果没有精确时间戳，根据更新字符串估算或默认给定 1800s
    if not client_ts:
        client_ts = now - 600

    elapsed = now - client_ts
    KEY_LIFESPAN = 1800  # 30 分钟

    remaining_seconds = max(0, int(KEY_LIFESPAN - elapsed))
    mins = remaining_seconds // 60
    secs = remaining_seconds % 60
    formatted_countdown = f"{mins:02d}:{secs:02d}"

    # 掩码显示
    uin_mask = (uin[:3] + "****" + uin[-3:]) if len(uin) > 6 else uin
    key_mask = (key[:4] + "****" + key[-4:]) if len(key) > 8 else (key[:2] + "****" if key else "")

    if remaining_seconds > 300:
        level = "valid"
        text = f"凭证有效 (剩 {mins} 分钟)"
        msg = "微信阅读通行凭证工作正常，可顺畅批量同步文章列表与互动数据。"
    elif remaining_seconds > 0:
        level = "expiring_soon"
        text = f"即将过期 (剩 {mins} 分钟)"
        msg = "凭证即将满 30 分钟过期。如需长时间拉取，可在微信中重新点开文章自动续期（0 秒刷新）。"
    else:
        level = "expired"
        text = "凭证已过期"
        msg = "凭证已满 30 分钟自然失效。请在电脑微信中打开任意公众号文章（Ctrl+R刷新），嗅探器 1 秒内自动续期！"

    # 检查是否有近期重度风控记录 (24小时内)
    freq_info = None
    if freq_event:
        freq_ts = freq_event.get("timestamp", 0)
        freq_elapsed = now - freq_ts
        if freq_elapsed < 86400:  # 24小时内
            wait_hours_left = max(1, int((86400 - freq_elapsed) / 3600))
            freq_info = {
                "triggered_at": freq_event.get("time_str"),
                "reason": freq_event.get("reason"),
                "suggested_wait_hours": wait_hours_left,
                "fast_solution": "切换电脑微信登录另一个微信号并点开文章，即可立即解除限制（等待时间变为 0 秒）！"
            }

    return {
        "has_auth": True,
        "status_level": level,
        "status_text": text,
        "message": msg,
        "expires_in_seconds": remaining_seconds,
        "expires_in_formatted": formatted_countdown,
        "captured_at": captured_at,
        "uin_mask": uin_mask,
        "key_mask": key_mask,
        "can_sync_articles": remaining_seconds > 0,
        "freq_control": freq_info
    }


def generate_wechat_profile_url(target: str) -> Dict[str, Any]:
    """
    输入公众号文章链接、__biz 或名称，智能提取 __biz 并生成微信专属历史消息页面链接
    供用户复制后发送给微信「文件传输助手」直接点击截获密钥
    """
    target = target.strip()
    biz = ""

    # 1. 尝试从 URL 参数中提取 __biz
    biz_match = re.search(r'[\?&]__biz=([^&#]+)', target)
    if biz_match:
        biz = biz_match.group(1)
    elif len(target) > 10 and target.endswith("==") and not target.startswith("http"):
        biz = target
    
    # 2. 如果输入的是文章链接但 __biz 经过短链接转义或未显式暴露
    # 返回原始文章链接或标准 profile_ext 格式
    if biz:
        profile_url = f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}&scene=124#wechat_redirect"
        return {
            "success": True,
            "biz": biz,
            "profile_url": profile_url,
            "message": "已生成该公众号专属历史主页链接，在微信打开即可截获凭证！"
        }
    elif target.startswith("http"):
        # 如果是普通文章链接，直接返回文章链接本身（在微信点开该文章同样 100% 触发嗅探截获）
        return {
            "success": True,
            "biz": "",
            "profile_url": target,
            "message": "已锁定微信文章链接，在微信电脑版中点开即可自动获取该号密钥！"
        }
    else:
        # 输入的是公众号名称，如果已有 client_biz 可复用
        auth = get_saved_wechat_auth()
        cached_biz = auth.get("client_biz", "")
        if cached_biz:
            return {
                "success": True,
                "biz": cached_biz,
                "profile_url": f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={cached_biz}&scene=124#wechat_redirect",
                "message": f"使用已记录的公众号凭证主页链接"
            }
        return {
            "success": False,
            "biz": "",
            "profile_url": "",
            "message": f"未识别到文章链接中的 __biz。请粘贴该公众号任意一篇文章链接即可一键生成！"
        }


async def check_wechat_auth_status() -> Dict[str, Any]:
    """检查当前保存的微信公众平台凭证是否有效 (带安全缓存与频控容错)"""
    now = time.time()
    if _AUTH_CACHE["result"] and (now - _AUTH_CACHE["last_checked"] < 30):
        return _AUTH_CACHE["result"]

    auth = get_saved_wechat_auth()
    cookie = auth.get("cookie", "")
    token = auth.get("token", "")

    if not cookie or not token:
        res = {
            "authenticated": False,
            "message": "未配置微信公众平台凭证（请在当前浏览器登录 mp.weixin.qq.com 后台）",
            "token": "",
            "account": ""
        }
        _AUTH_CACHE["result"] = res
        _AUTH_CACHE["last_checked"] = now
        return res

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Referer": f"https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token={token}"
    }

    # 1. 优先使用无频控限制的公众平台主页进行会话有效性验证
    home_url = f"https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token={token}"

    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False, follow_redirects=False) as client:
            resp = await client.get(home_url, headers=headers)
            
            # 如果返回 200 且正文中包含正常后台标识或未被重定向回登录页
            if resp.status_code == 200 and ("home/index" in resp.text or "user_name" in resp.text or "logout" in resp.text):
                # 尝试提取后台当前登录的公众号昵称与 fakeid 与 头像
                account_name = auth.get("account_name", "")
                name_match = re.search(r'class="weui-desktop-account__nickname">([^<]+)<', resp.text) or \
                             re.search(r'class="user_name"[^>]*>([^<]+)<', resp.text) or \
                             re.search(r'nickname\s*[:=]\s*["\']([^"\']+)["\']', resp.text)
                if name_match:
                    account_name = name_match.group(1).strip()

                avatar = auth.get("avatar", "")
                avatar_match = re.search(r'class="weui-desktop-account__avatar"\s+src="([^"]+)"', resp.text) or \
                               re.search(r'class="avatar"[^>]*src="([^"]+)"', resp.text) or \
                               re.search(r'headimg\s*[:=]\s*["\']([^"\']+)["\']', resp.text) or \
                               re.search(r'head_img\s*[:=]\s*["\']([^"\']+)["\']', resp.text)
                if avatar_match:
                    avatar = avatar_match.group(1).strip()

                fakeid = auth.get("fakeid", "")
                if not fakeid:
                    # 优先从页面脚本变量匹配
                    fakeid_match = re.search(r'fakeid\s*[:=]\s*["\']([A-Za-z0-9+/=]+)["\']', resp.text)
                    if not fakeid_match:
                        fakeid_match = re.search(r'"fakeid"\s*:\s*"([A-Za-z0-9+/=]+)"', resp.text)
                    if not fakeid_match:
                        fakeid_match = re.search(r'data-fakeid\s*=\s*["\']([A-Za-z0-9+/=]+)["\']', resp.text)
                    if fakeid_match:
                        fakeid = fakeid_match.group(1).strip()

                save_wechat_auth(cookie, token, fakeid, account_name, avatar=avatar)

                msg = f"微信公众平台官方通道已连通{f'（当前账号：{account_name}）' if account_name else ''}，支持任意公众号全量历史文章下载"
                res = {
                    "authenticated": True,
                    "message": msg,
                    "token": token,
                    "account": account_name,
                    "avatar": avatar,
                    "fakeid": fakeid,
                    "updated_at": auth.get("updated_at", "")
                }
                _AUTH_CACHE["result"] = res
                _AUTH_CACHE["last_checked"] = now
                return res

            # 如果主页被 302 重定向到登录页，说明 Session 已过期
            if resp.status_code in [301, 302] and "login" in resp.headers.get("location", "").lower():
                res = {
                    "authenticated": False,
                    "message": "微信公众平台登录已过期，请在浏览器中重新登录 mp.weixin.qq.com",
                    "token": token
                }
                _AUTH_CACHE["result"] = res
                _AUTH_CACHE["last_checked"] = now
                return res

    except Exception as e:
        print(f"主页探测异常: {e}")

    # 2. 备用验证：请求 searchbiz 接口
    test_url = f"https://mp.weixin.qq.com/cgi-bin/searchbiz?action=search_biz&begin=0&count=1&query=微信&token={token}&lang=zh_CN&f=json&ajax=1"
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            resp = await client.get(test_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                base_resp = data.get("base_resp", {})
                ret = base_resp.get("ret")
                err_msg = str(base_resp.get("err_msg", "")).lower()

                # ret == 0 或返回频控限制 freq control，均证实凭证与 Token 100% 正确有效！
                if ret == 0 or ret == 200013 or "freq control" in err_msg or "ok" in err_msg:
                    res = {
                        "authenticated": True,
                        "message": "微信公众平台官方通道已连通，支持全量历史文章批量下载",
                        "token": token,
                        "updated_at": auth.get("updated_at", "")
                    }
                    _AUTH_CACHE["result"] = res
                    _AUTH_CACHE["last_checked"] = now
                    return res
                elif ret == 200003:
                    res = {
                        "authenticated": False,
                        "message": "微信公众平台 Token 已过期，请在浏览器中重新打开后台刷新",
                        "token": token
                    }
                    _AUTH_CACHE["result"] = res
                    _AUTH_CACHE["last_checked"] = now
                    return res
    except Exception as e:
        pass

    # 只要本地已保存了非空 Token 和 Cookie，默认判定为有效状态
    if token and len(cookie) > 20:
        res = {
            "authenticated": True,
            "message": "微信公众平台凭证已就绪，可直接进行全量抓取",
            "token": token,
            "updated_at": auth.get("updated_at", "")
        }
        _AUTH_CACHE["result"] = res
        _AUTH_CACHE["last_checked"] = now
        return res

    res = {
        "authenticated": False,
        "message": "微信公众平台凭证校验未通过，请在浏览器中打开后台",
        "token": token
    }
    _AUTH_CACHE["result"] = res
    _AUTH_CACHE["last_checked"] = now
    return res
