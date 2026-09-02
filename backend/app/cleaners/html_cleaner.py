import re
from typing import Tuple, List
from bs4 import BeautifulSoup
import markdownify
from app.cleaners.noise_filter import filter_noise_text

def clean_html_content(raw_html: str, enable_noise_filter: bool = True, remove_watermark: bool = True) -> Tuple[str, str, List[str]]:
    """
    清洗 HTML 内容，提取图片列表，并转换为标准 Markdown。
    :param raw_html: 原始 HTML 片段
    :param enable_noise_filter: 是否开启智能文本去噪（过滤营销引流语）
    :param remove_watermark: 是否智能去除图片水印与溯源高清原图（默认 True）
    返回: (cleaned_html, markdown_text, image_urls)
    """
    if not raw_html:
        return "", "", []

    soup = BeautifulSoup(raw_html, "lxml")

    # 1. 移除无用标签（脚本、样式、广告、iframe、隐藏元素等）
    for tag in soup(["script", "style", "iframe", "noscript", "svg", "button", "input", "form"]):
        tag.decompose()

    # 移除常见的广告和分享容器选择器 (对齐公号三刀逆向去噪规则库)
    ad_selectors = [
        ".article-banner", ".advertisement", ".ad-container", ".reward-box",
        ".share-box", ".like-box", ".qr-code", ".wx-qrcode", ".copyright-box",
        "#blog_post_info_block", ".blog_post_info_block", ".postDesc",
        ".csdn-side-toolbar", ".recommend-box", ".comment-box", ".url-icon",
        ".qr_code_pc_outer", "#js_pc_qr_code", "#js_bottom_share_area",
        ".reward_area", ".rich_media_area_extra", "#js_sponsor_ad_area",
        ".rich_media_tool", "#js_toobar3", ".share_dialog", "#js_profile_qrcode",
        ".like_comment_wording", "#js_like_btn", "#js_view_source"
    ]
    for selector in ad_selectors:
        for tag in soup.select(selector):
            tag.decompose()

    # 移除微小的装饰性标签图标 (如微博超话钻石、话题角标等)
    for img in soup.find_all("img"):
        src = (img.get("src", "") or img.get("data-src", "") or "").lower()
        img_class = " ".join(img.get("class", [])) if isinstance(img.get("class"), list) else (img.get("class") or "")
        if "url-icon" in img_class or "h5.sinaimg.cn" in src or "timeline_card" in src or "small_super" in src or "sinaimg.cn/upload" in src:
            img.decompose()

    # 2. 修复懒加载图片与智能水印溯源
    image_urls = []
    for img in soup.find_all("img"):
        if remove_watermark:
            # 智能去水印模式：优先提取平台无水印原始高清母图 (data-original / data-actualsrc 等)
            src = (
                img.get("data-original")
                or img.get("data-original-src")
                or img.get("data-actualsrc")
                or img.get("data-src")
                or img.get("src")
                or ""
            ).strip()
        else:
            # 保留水印模式：优先提取网页当前渲染的实际图片 (带水印参数)
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-actualsrc")
                or img.get("data-original-src")
                or img.get("data-original")
                or ""
            ).strip()

        if src:
            # 补全相对协议
            if src.startswith("//"):
                src = "https:" + src

            if remove_watermark:
                # 智能剥离阿里云 OSS / CSDN / 腾讯云等图片水印参数
                src = re.sub(r'[\?&]x-oss-process=image/watermark[^&#]*', '', src)
                src = re.sub(r'[\?&]watermark[^&#]*', '', src)
                src = re.sub(r'[\?&]imageMogr2/watermark[^&#]*', '', src)
                src = re.sub(r'[\?&]source=[^&#]*', '', src)
                # 知乎缩略图/压缩图路径还原为原图 (例如 /80/v2-xxx -> /v2-xxx)
                src = re.sub(r'(zhimg\.com)/[0-9a-zA-Z_-]+/(v2-[0-9a-fA-F]+)', r'\1/\2', src)
                src = re.sub(r'[\?&]$', '', src)

            img["src"] = src
            image_urls.append(src)
            # 移除可能会影响渲染的冗余属性
            for attr in list(img.attrs):
                if attr not in ["src", "alt", "title", "width", "height"]:
                    del img[attr]
        else:
            img.decompose()

    # 3. 移除任何隐藏内容的内联样式 (如微信反爬设置的 visibility: hidden; opacity: 0 等)
    for tag in soup.find_all(True):
        if tag.get("style"):
            style = tag["style"]
            new_style = re.sub(r'visibility\s*:\s*hidden\s*;?', '', style, flags=re.I)
            new_style = re.sub(r'opacity\s*:\s*0\s*;?', '', new_style, flags=re.I)
            if not new_style.strip():
                del tag["style"]
            else:
                tag["style"] = new_style.strip()

    # 4. 提取清洗后的 HTML (仅提取 body 内部片段，避免生成外层多余的 html/body 标签破坏整体 DOM 结构)
    if soup.body:
        cleaned_html = soup.body.decode_contents().strip()
    else:
        cleaned_html = str(soup).strip()

    # 4. 转为 Markdown
    md_content = markdownify.markdownify(
        cleaned_html,
        heading_style="ATX",
        code_language="python",
        strip=['a'] if False else []
    )

    # 5. 格式规整
    md_content = re.sub(r"\n{3,}", "\n\n", md_content).strip()

    # 6. 如果开启了智能去噪，进一步清洗 Markdown 文本
    if enable_noise_filter:
        md_content = filter_noise_text(md_content)

    return cleaned_html, md_content, image_urls
