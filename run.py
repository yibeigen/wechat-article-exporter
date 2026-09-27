# BlogDistiller · 本地服务启动入口
import sys
import os
import asyncio

# 在 Windows 平台下，必须在创建任何 EventLoop 或启动 uvicorn 前设置 ProactorEventLoopPolicy，
# 以保证 Playwright 子进程、多线程调用与信号通信完全正常
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# 确保项目根目录和 backend 目录在 sys.path 中
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BlogDistiller 本地服务启动器")
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--no-reload", action="store_true", default=False, help="生产/客户端模式下禁用代码热重载")
    args = parser.parse_args()

    reload_flag = not args.no_reload

    uvicorn.run(
        "backend.app.main:app",
        host=args.host,
        port=args.port,
        reload=reload_flag,
        reload_dirs=[os.path.join(BASE_DIR, "backend"), os.path.join(BASE_DIR, "frontend")] if reload_flag else None,
        loop="asyncio"
    )

