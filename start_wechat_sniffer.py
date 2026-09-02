#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 BlogDistiller 微信桌面客户端阅读凭证嗅探助手 (Capture Runner)
基于「公号三刀」逆向成果开发，实现 0 门槛自动截获微信电脑版阅读通行密钥 (uin/key/pass_ticket)。
"""

import os
import sys
import time
import subprocess
from pathlib import Path

# 确保 backend 路径可用
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from app.core.wechat_sniffer import WeChatSnifferProxy, DEFAULT_PROXY_PORT

def enable_windows_proxy(port: int = DEFAULT_PROXY_PORT):
    """设置 Windows 当前用户系统 HTTP 代理为 127.0.0.1:port"""
    if sys.platform != "win32":
        return
    try:
        cmd1 = f'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyEnable /t REG_DWORD /d 1 /f'
        cmd2 = f'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyServer /t REG_SZ /d "127.0.0.1:{port}" /f'
        subprocess.run(cmd1, shell=True, capture_output=True)
        subprocess.run(cmd2, shell=True, capture_output=True)
        print(f"⚙️ 已自动配置 Windows 系统代理 ➔ 127.0.0.1:{port}")
    except Exception as e:
        print(f"配置系统代理异常: {e}")

def disable_windows_proxy():
    """还原 Windows 系统代理"""
    if sys.platform != "win32":
        return
    try:
        cmd = 'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f'
        subprocess.run(cmd, shell=True, capture_output=True)
        print("🛑 已自动关闭 Windows 系统代理")
    except Exception as e:
        pass

def main():
    print("="*65)
    print("  🚀 BlogDistiller 微信阅读通行私钥自动嗅探助手")
    print("  （基于公号三刀逆向链路：CA证书注入 + 本地代理拦截 + 实时同步）")
    print("="*65)
    
    proxy = WeChatSnifferProxy(port=DEFAULT_PROXY_PORT, sync_remote_url="https://doc.305758.xyz/api/wechat/set-auth")
    
    # 自动开启系统代理
    enable_windows_proxy(DEFAULT_PROXY_PORT)
    
    try:
        proxy.start(daemon=False)
    except KeyboardInterrupt:
        print("\n正在停止嗅探代理...")
    finally:
        disable_windows_proxy()
        proxy.stop()
        print("👋 嗅探器已退出。")

if __name__ == "__main__":
    main()
