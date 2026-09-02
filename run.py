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

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[os.path.join(BASE_DIR, "backend"), os.path.join(BASE_DIR, "frontend")],
        loop="asyncio"
    )
