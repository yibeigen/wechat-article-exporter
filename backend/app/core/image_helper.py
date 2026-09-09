import re
import base64
import asyncio
from urllib.parse import quote
from typing import List, Dict, Optional, Tuple, Set
from bs4 import BeautifulSoup
import httpx

from app.models import ArticleItem

# 全局内存缓存，同一导出批次或重复导出的图片无需二次网络请求
_GLOBAL_IMAGE_BASE64_CACHE: Dict[str, str] = {}
_GLOBAL_IMAGE_BYTES_CACHE: Dict[str, Tuple[bytes, str]] = {}


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

    if img_url in _GLOBAL_IMAGE_BYTES_CACHE:
        return _GLOBAL_IMAGE_BYTES_CACHE[img_url]

    # 构建候选拉取通道列表: (url, is_proxy)
    channels: List[Tuple[str, bool]] = []
    is_jianshu = "upload-images.jianshu.io" in img_url or "jianshu.io" in img_url

    if is_jianshu:
        # 简书 CDN 在海外服务器直连被 Cloudflare 拦截 403，优先使用搜狗代理通道
        channels.append((f"https://img01.sogoucdn.com/net/a/04/link?appid=100520029&url={img_url}", True))
        channels.append((f"https://img02.sogoucdn.com/net/a/04/link?appid=100520031&url={img_url}", True))
        channels.append((img_url, False))
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


async def fetch_images_base64_map(img_urls: Set[str], max_concurrency: int = 15) -> Dict[str, str]:
    """并发抓取一组图片 URL 并转换为 Base64 Data URI"""
    url_to_base64: Dict[str, str] = {}
    pending_urls = set()

    for u in img_urls:
        if not u or not u.startswith("http"):
            continue
        if u in _GLOBAL_IMAGE_BASE64_CACHE:
            url_to_base64[u] = _GLOBAL_IMAGE_BASE64_CACHE[u]
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
                b64_str = base64.b64encode(data_bytes).decode("utf-8")
                b64_uri = f"data:{mime};base64,{b64_str}"
                _GLOBAL_IMAGE_BASE64_CACHE[u] = b64_uri
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
