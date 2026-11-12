import asyncio
import functools
from pathlib import Path

# Win7 随包内核是 Python 3.8，而 asyncio.to_thread 是 3.9 才加入的 API。
# 不补这个垫片，PDF/DOCX/ZIP 导出与知乎、掘金、51CTO 抓取会在真 Win7 上
# 统一抛 "module 'asyncio' has no attribute 'to_thread'"（Win11 开发机永远复现不了）。
if not hasattr(asyncio, "to_thread"):
    async def _to_thread(func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

    asyncio.to_thread = _to_thread

# 根路径与存储目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = BASE_DIR / "downloads"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 抓取默认配置
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

DEFAULT_TIMEOUT = 15.0

# 品牌与公众号水印配置 (闲鱼/自媒体引流与免责声明)
BRAND_OFFICIAL_ACCOUNT = "艺杯羹"
BRAND_FOOTER_NOTE = "📖 本文档由【微信公众号：艺杯羹】整理排版 · 仅供个人离线学习与学术交流"
BRAND_DISCLAIMER = "【免责声明】本文档内容均摘取自公开网络免费内容，排版整理：【微信公众号：艺杯羹】。仅供个人离线学习、学术研究与知识归档使用，严禁用于任何商业营利用途。原文知识产权归原作者及原发布平台所有。"

