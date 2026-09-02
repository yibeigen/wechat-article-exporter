from pathlib import Path

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

