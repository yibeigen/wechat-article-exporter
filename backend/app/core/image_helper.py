import re
import base64
import asyncio
from urllib.parse import quote
from typing import List, Dict, Optional, Tuple, Set
from bs4 import BeautifulSoup
import httpx

from app.models import ArticleItem

# 全局内存缓存，同一导出批次或重复导出的图片无需二次网络请求
# 使用 OrderedDict 实现 LRU 上限，防止数万张图片 (原 bytes + Base64 约膨胀 1.33 倍) 无限驻留内存导致 OOM
from collections import OrderedDict

_MAX_IMAGE_CACHE_ENTRIES = 600
_GLOBAL_IMAGE_BASE64_CACHE: "OrderedDict[str, str]" = OrderedDict()
_GLOBAL_IMAGE_BYTES_CACHE: "OrderedDict[str, Tuple[bytes, str]]" = OrderedDict()


def _lru_put(cache: OrderedDict, key: str, value):
    """写入 LRU 缓存，超出上限时淘汰最久未使用的条目"""
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > _MAX_IMAGE_CACHE_ENTRIES:
        cache.popitem(last=False)


def _lru_get(cache: OrderedDict, key: str):
    """读取 LRU 缓存并刷新热度"""
    if key not in cache:
        return None
    cache.move_to_end(key)
    return cache[key]


def clear_image_caches():
    """导出任务结束后清空全局图片缓存，释放大对象内存"""
    _GLOBAL_IMAGE_BASE64_CACHE.clear()
    _GLOBAL_IMAGE_BYTES_CACHE.clear()


def get_platform_referer_headers(url: str, is_proxy: bool = False) -> Dict[str, str]:
    """获取针对特定图片 URL 或代理通道的防盗链请求头"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if is_proxy:
        # 请求搜狗等中转 CDN 代理时，必须使用对应站点的合法 Referer，严禁透传第三方站点的 Referer，否则搜狗会报 403
        headers["Referer"] = "https://pic.sogou.com/"
        return headers

    u = url.lower()
    if "csdn" in u:
        headers["Referer"] = "https://blog.csdn.net/"
    elif "qpic.cn" in u or "weixin" in u:
        headers["Referer"] = "https://mp.weixin.qq.com/"
    elif "zhihu" in u or "zhimg" in u:
        headers["Referer"] = "https://www.zhihu.com/"
    elif "sinaimg" in u or "weibo" in u or "sina.com" in u:
        headers["Referer"] = "https://weibo.com/"
    elif "juejin" in u or "byteimg" in u:
        headers["Referer"] = "https://juejin.cn/"
    elif "cnblogs" in u:
        headers["Referer"] = "https://www.cnblogs.com/"
    elif "51cto" in u:
        headers["Referer"] = "https://blog.51cto.com/"
    elif "jianshu" in u:
        headers["Referer"] = "https://www.jianshu.com/"
    return headers


def detect_image_mime(data: bytes, fallback: str = "image/png") -> str:
    """根据二进制魔数精准探测真实 MIME 类型"""
    if not data or len(data) < 12:
        return fallback
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[8:16]:
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.strip().startswith(b"<?xml") or data.strip().startswith(b"<svg"):
        return "image/svg+xml"
    if fallback and fallback.startswith("image/"):
        return fallback.split(";")[0].strip()
    return "image/png"


async def download_image_bytes(client: httpx.AsyncClient, img_url: str) -> Optional[Tuple[bytes, str]]:
    """高可用多通道图片下载器 (支持原站直连、无防盗链重试、搜狗代理 CDN 多节点穿透)"""
    if not img_url or not img_url.startswith("http"):
        return None

    cached = _lru_get(_GLOBAL_IMAGE_BYTES_CACHE, img_url)
    if cached:
        return cached

    # 构建候选拉取通道列表: (url, is_proxy)
    channels: List[Tuple[str, bool]] = []
    is_jianshu = "upload-images.jianshu.io" in img_url or "jianshu.io" in img_url

    if is_jianshu:
        # 本地住宅宽带直连简书 CDN 通常秒开，必须先直连；搜狗代理通道仅作兜底
        # （搜狗中转是当年为云服务器海外 IP 被 Cloudflare 拦截而设计，本地版再走它反而每张图白白多等 5~10 秒）
        channels.append((img_url, False))
        channels.append((f"https://img01.sogoucdn.com/net/a/04/link?appid=100520029&url={img_url}", True))
        channels.append((f"https://img02.sogoucdn.com/net/a/04/link?appid=100520031&url={img_url}", True))
    else:
        # 其他平台优先直连，搜狗代理作为兜底
        channels.append((img_url, False))
        channels.append((f"https://img01.sogoucdn.com/net/a/04/link?appid=100520029&url={img_url}", True))

    for target_url, is_proxy in channels:
        headers = get_platform_referer_headers(target_url if not is_proxy else img_url, is_proxy=is_proxy)
        try:
            resp = await client.get(target_url, headers=headers, timeout=12.0, follow_redirects=True)
            if resp.status_code == 200 and len(resp.content) > 100:
                # 排除返回 HTML 403/404 错误页的情况
                content_prefix = resp.content[:64].lower()
                if b"<html" in content_prefix or b"<!doctype html" in content_prefix:
                    continue
                mime = detect_image_mime(resp.content, resp.headers.get("content-type", "image/png"))
                _GLOBAL_IMAGE_BYTES_CACHE[img_url] = (resp.content, mime)
                return (resp.content, mime)
        except Exception:
            continue

    # 若上述通道全部受挫，尝试完全不带 Referer 的极简直连
    try:
        resp = await client.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8.0, follow_redirects=True)
        if resp.status_code == 200 and len(resp.content) > 100:
            content_prefix = resp.content[:64].lower()
            if b"<html" not in content_prefix and b"<!doctype html" not in content_prefix:
                mime = detect_image_mime(resp.content, resp.headers.get("content-type", "image/png"))
                _GLOBAL_IMAGE_BYTES_CACHE[img_url] = (resp.content, mime)
                return (resp.content, mime)
    except Exception:
        pass

    return None


def compress_image_bytes(raw_bytes: bytes, max_width: int = 1200, quality: int = 75) -> Tuple[bytes, str, str]:
    """使用 Pillow 将图片二进制流等比缩放并压缩为极致轻量的现代 WebP 或优化 JPEG。
    
    返回: (compressed_bytes, ext, mime_type)
    - 内存与体积极小：通常为每张大图减少 80%~95% 体积 (如 2MB 压至 30~50KB)；
    - 等比限制最大宽度至 max_width (默认 1200px)，保证高清视网膜清晰度的同时杜绝巨幅大图撑爆内存；
    - 动图 (GIF) 自动保留原样帧；若 Pillow 异常或压缩后未减小，自动平滑回退至原图。
    """
    if not raw_bytes or len(raw_bytes) < 100:
        return raw_bytes, ".png", "image/png"
    
    try:
        import io
        from PIL import Image
        
        with Image.open(io.BytesIO(raw_bytes)) as im:
            if getattr(im, "is_animated", False):
                return raw_bytes, ".gif", "image/gif"
                
            orig_w, orig_h = im.size
            target_im = im
            
            # 1. 超过最大宽度时等比高质量缩放 (LANCZOS)
            if orig_w > max_width:
                new_h = max(1, int(orig_h * (max_width / orig_w)))
                target_im = target_im.resize((max_width, new_h), Image.Resampling.LANCZOS)
            
            # 2. 导出为现代高效 WebP 格式
            out_bio = io.BytesIO()
            if target_im.mode in ("RGBA", "LA") or (target_im.mode == "P" and "transparency" in target_im.info):
                target_im.save(out_bio, format="WEBP", quality=quality, method=4)
            else:
                if target_im.mode != "RGB":
                    target_im = target_im.convert("RGB")
                target_im.save(out_bio, format="WEBP", quality=quality, method=4)
                
            comp_bytes = out_bio.getvalue()
            if len(comp_bytes) > 0 and len(comp_bytes) < len(raw_bytes):
                return comp_bytes, ".webp", "image/webp"
    except Exception:
        pass
        
    return raw_bytes, ".jpg", "image/jpeg"


async def fetch_images_base64_map(img_urls: Set[str], max_concurrency: int = 15) -> Dict[str, str]:
    """并发抓取一组图片 URL 并转换为极致轻量的 Base64 Data URI (默认经高效压缩，防止内存爆炸)"""
    url_to_base64: Dict[str, str] = {}
    pending_urls = set()

    for u in img_urls:
        if not u or not u.startswith("http"):
            continue
        cached_b64 = _lru_get(_GLOBAL_IMAGE_BASE64_CACHE, u)
        if cached_b64:
            url_to_base64[u] = cached_b64
        else:
            pending_urls.add(u)

    if not pending_urls:
        return url_to_base64

    sem = asyncio.Semaphore(max_concurrency)

    async def _worker(client: httpx.AsyncClient, u: str):
        async with sem:
            res = await download_image_bytes(client, u)
            if res:
                data_bytes, mime = res
                # 先进行极速轻量压缩，将数 MB 压至几十 KB，彻底解除 Base64 内存暴增风险
                comp_bytes, comp_ext, comp_mime = compress_image_bytes(data_bytes, max_width=1200, quality=75)
                b64_str = base64.b64encode(comp_bytes).decode("utf-8")
                b64_uri = f"data:{comp_mime};base64,{b64_str}"
                _lru_put(_GLOBAL_IMAGE_BASE64_CACHE, u, b64_uri)
                url_to_base64[u] = b64_uri

    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        tasks = [_worker(client, u) for u in pending_urls]
        await asyncio.gather(*tasks, return_exceptions=True)

    return url_to_base64


def apply_base64_to_html(content_html: str, base64_map: Dict[str, str]) -> str:
    """将 HTML 正文中所有 <img> 标签的远程图片链接替换为 Base64 Data URI"""
    if not content_html or not base64_map:
        return content_html

    soup = BeautifulSoup(content_html, "lxml")
    imgs = soup.find_all("img")
    if not imgs:
        return content_html

    for img in imgs:
        src = img.get("src") or ""
        data_src = img.get("data-src") or ""
        data_orig = img.get("data-original-src") or ""
        data_actual = img.get("data-actualsrc") or ""

        # 查找匹配的 Base64 映射
        matched_b64 = None
        for candidate in [src, data_src, data_orig, data_actual]:
            if candidate and candidate in base64_map:
                matched_b64 = base64_map[candidate]
                break

        if matched_b64:
            img["src"] = matched_b64
            # 清除干扰惰性加载的属性，强制立即可见
            for lazy_attr in ["data-src", "data-original-src", "data-actualsrc", "data-original"]:
                if img.has_attr(lazy_attr):
                    del img[lazy_attr]
            img["loading"] = "eager"

    return soup.body.decode_contents() if soup.body else str(soup)


async def embed_articles_images_as_base64(articles: List[ArticleItem]) -> Dict[str, str]:
    """一键为文章列表中的所有文章抓取图片并就地内联替换为 Base64 (彻底摆脱网络外链，实现 100% 离线备份)"""
    all_img_urls: Set[str] = set()

    for art in articles:
        if art.images:
            for u in art.images:
                if u and u.startswith("http"):
                    all_img_urls.add(u)
        if art.content_html:
            soup = BeautifulSoup(art.content_html, "lxml")
            for img in soup.find_all("img"):
                for attr in ["src", "data-src", "data-original-src", "data-actualsrc"]:
                    val = img.get(attr)
                    if val and val.startswith("http"):
                        all_img_urls.add(val)

    if not all_img_urls:
        return {}

    base64_map = await fetch_images_base64_map(all_img_urls)

    # 就地替换文章 content_html 中的所有远程外链为 Base64 Data URI
    if base64_map:
        for art in articles:
            if art.content_html:
                art.content_html = apply_base64_to_html(art.content_html, base64_map)

    return base64_map


def collect_articles_image_urls(articles: List[ArticleItem]) -> Set[str]:
    """收集文章列表中的全部远程图片 URL (从 art.images 与正文 HTML 双来源，天然去重)"""
    all_img_urls: Set[str] = set()
    for art in articles:
        for u in (art.images or []):
            if u and u.startswith("http"):
                all_img_urls.add(u)
        if art.content_html:
            soup = BeautifulSoup(art.content_html, "lxml")
            for img in soup.find_all("img"):
                for attr in ["src", "data-src", "data-original-src", "data-actualsrc"]:
                    val = img.get(attr)
                    if val and val.startswith("http"):
                        all_img_urls.add(val)
        # Markdown 正文里也可能直接带图片链接，一并收集保证 DOCX/Word 不遗漏
        # (兼容带 "title" 的图片语法，如微信残留的 ![](url "null")，防止收集端漏抓)
        if art.content_markdown:
            for m in re.findall(r'!\[[^\]]*\]\(\s*(https?://[^\s\)"]+)', art.content_markdown):
                if m.startswith("http"):
                    all_img_urls.add(m)
    return all_img_urls


async def fetch_images_bytes_map(img_urls: Set[str], image_mode: str = "compressed") -> Dict[str, bytes]:
    """并发预取一组图片的原始字节，供 python-docx 等纯同步渲染场景使用。

    - image_mode == "compressed": 返回经轻量压缩的字节 (WebP/优化 JPEG)，体积小省内存；
    - image_mode == "original": 返回高清原图字节；
    - 复用全局 LRU 字节缓存 (download_image_bytes)，同任务内多格式导出不会重复下载同一张图。
    """
    result: Dict[str, bytes] = {}
    pending = [u for u in img_urls if u and u.startswith("http")]
    if not pending:
        return result

    sem = asyncio.Semaphore(15)

    async def _worker(client: httpx.AsyncClient, u: str):
        async with sem:
            res = await download_image_bytes(client, u)
            if not res:
                return
            data_bytes, _mime = res
            if image_mode == "compressed":
                comp_bytes, _ext, _mime2 = compress_image_bytes(data_bytes, max_width=1200, quality=75)
                result[u] = comp_bytes
            else:
                result[u] = data_bytes

    async with httpx.AsyncClient(verify=False, trust_env=False) as client:
        await asyncio.gather(*[_worker(client, u) for u in pending], return_exceptions=True)

    return result
