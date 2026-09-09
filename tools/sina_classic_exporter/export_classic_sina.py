#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新浪博客电脑端原版经典排版离线导出工具 (Sina Blog Classic Desktop Exporter)

功能特性：
1. 1:1 复刻新浪博客 PC 电脑端经典双栏版面（蓝头宋体排版、折角标题小标、经典字号行距）。
2. 内置纯前端毫秒级离线即时搜索框（支持实时搜索与高亮，支持键盘左右键快速切篇）。
3. 严格按照专栏名称精准命名（如《西游正解.html》）。
4. 纯单文件（All-in-One）永久离线保存，自动并发下载图片并转换为 Base64 内嵌，断网永不裂图。
"""

import os
import sys
import re
import json
import base64
import asyncio
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional

# 确保能正常加载 backend/app 模块
CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from jinja2 import Template
from bs4 import BeautifulSoup

from backend.app.scrapers.sina_blog import SinaBlogScraper
from backend.app.models import ArticleItem

# 图片缓存字典避免重复抓取相同的图片
IMAGE_BASE64_CACHE: Dict[str, str] = {}

async def download_image_as_base64(client: httpx.AsyncClient, img_url: str) -> Optional[str]:
    """下载图片并转为 Base64 格式数据"""
    if not img_url or img_url.startswith("data:"):
        return img_url
    if img_url in IMAGE_BASE64_CACHE:
        return IMAGE_BASE64_CACHE[img_url]

    clean_url = img_url.strip()
    if clean_url.startswith("//"):
        clean_url = "https:" + clean_url
    elif not clean_url.startswith("http"):
        return img_url

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://blog.sina.com.cn/"
        }
        resp = await client.get(clean_url, headers=headers, timeout=12.0)
        if resp.status_code == 200 and resp.content:
            content_type = resp.headers.get("content-type", "").lower()
            if "jpeg" in content_type or "jpg" in content_type or clean_url.endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"
            elif "png" in content_type or clean_url.endswith(".png"):
                mime = "image/png"
            elif "gif" in content_type or clean_url.endswith(".gif"):
                mime = "image/gif"
            elif "webp" in content_type or clean_url.endswith(".webp"):
                mime = "image/webp"
            else:
                mime = "image/jpeg"

            b64_data = base64.b64encode(resp.content).decode("ascii")
            data_uri = f"data:{mime};base64,{b64_data}"
            IMAGE_BASE64_CACHE[img_url] = data_uri
            return data_uri
    except Exception:
        pass

    return img_url

async def inline_html_images(html_content: str, client: httpx.AsyncClient) -> str:
    """将正文中的 <img> 转换为内嵌 Base64"""
    if not html_content or "<img" not in html_content:
        return html_content

    soup = BeautifulSoup(html_content, "lxml")
    imgs = soup.find_all("img")
    if not imgs:
        return html_content

    tasks = []
    img_nodes = []
    for img in imgs:
        src = img.get("src") or img.get("real_src")
        if src and not src.startswith("data:"):
            img_nodes.append(img)
            tasks.append(download_image_as_base64(client, src))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for img_node, res in zip(img_nodes, results):
            if isinstance(res, str) and res.startswith("data:"):
                img_node["src"] = res
                if img_node.get("real_src"):
                    del img_node["real_src"]

    body_tag = soup.body
    return "".join(str(c) for c in body_tag.contents) if body_tag else str(soup)

def sanitize_filename(name: str) -> str:
    """过滤文件名非法字符"""
    clean = re.sub(r'[\/\\:\*\?"<>\|]', '_', name).strip()
    return clean or "新浪博客合集"

async def export_classic_sina(
    target_url: str,
    output_dir: Optional[str] = None,
    max_articles: Optional[int] = None,
    inline_images: bool = True,
    force_refresh: bool = False
) -> str:
    """主导出流程"""
    out_dir = Path(output_dir) if output_dir else (ROOT_DIR / "downloads")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("======================================================================")
    print("  🚀 新浪博客电脑端原版经典排版离线导出工具 (Sina Classic Exporter)")
    print("======================================================================")
    print(f"📌 目标链接: {target_url}")

    scraper = SinaBlogScraper(target_url, max_articles=max_articles)
    
    # 1. 获取博主与专栏元信息
    print("🔍 正在检索博主与专栏元数据...")
    author_info = await scraper.get_author_info()
    author_name = author_info.get("name") or "新浪博主"
    author_avatar = author_info.get("avatar") or ""

    # 2. 深度检索博文清单
    print("📑 正在遍历分页，获取全量博文清单...")
    articles_meta = await scraper.get_article_list(
        progress_callback=lambda msg, c, t: print(f"  ➜ {msg}", flush=True)
    )

    total_count = len(articles_meta)
    print(f"✅ 博文检索完成！共获取到有效博文: {total_count} 篇")
    if scraper.category_name:
        print(f"🏷️ 专栏分类: 【{scraper.category_name}】 (标称 {scraper.declared_count} 篇)")

    # 确定输出文件名：严格按照专栏名称（如《西游正解.html》）命名
    if scraper.category_name:
        base_name = sanitize_filename(scraper.category_name)
    else:
        base_name = sanitize_filename(f"{author_name}_全部博文")

    output_html_file = out_dir / f"{base_name}.html"

    # 3. 逐篇抓取详情并转换内联图片 (支持本地缓存加速)
    cache_file = CURRENT_DIR / f"cached_articles_{base_name}.json"
    articles_data = []

    if not force_refresh and cache_file.exists():
        print(f"📦 发现本地已有该专栏的文章缓存文件: {cache_file.name}")
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                articles_data = json.load(f)
            print(f"⚡ 成功快速加载 {len(articles_data)} 篇已归档文章数据！")
        except Exception as e:
            print(f"⚠️ 读取缓存异常: {e}，将重新在线抓取")
            articles_data = []

    if not articles_data:
        print(f"\n📥 正在下载 {total_count} 篇博文正文与图文内容 (支持 100% 离线 Base64)...")
        async with httpx.AsyncClient(timeout=15.0) as img_client:
            for idx, meta in enumerate(articles_meta, 1):
                title = meta.get("title", f"第{idx}篇")
                print(f"  [{idx}/{total_count}] 抓取详情: {title[:35]}...", flush=True)

                try:
                    item: ArticleItem = await scraper.scrape_article_detail(meta)
                    content_html = item.content_html

                    if inline_images:
                        content_html = await inline_html_images(content_html, img_client)

                    articles_data.append({
                        "id": item.id,
                        "title": item.title,
                        "author": item.author or author_name,
                        "publish_time": item.publish_time,
                        "url": item.url,
                        "category": scraper.category_name or "西游正解",
                        "tags": item.tags or ["新浪博客"],
                        "content_html": content_html
                    })
                except Exception as e:
                    print(f"    ⚠️ 抓取单篇异常跳过: {e}")
                    articles_data.append({
                        "id": meta.get("id", str(idx)),
                        "title": title,
                        "author": author_name,
                        "publish_time": meta.get("publish_time", ""),
                        "url": meta.get("url", ""),
                        "category": scraper.category_name or "西游正解",
                        "tags": ["新浪博客"],
                        "content_html": "<p>博文暂无内容或已被设为私密</p>"
                    })

        # 写入本地缓存以备后续极速二次排版使用
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(articles_data, f, ensure_ascii=False, indent=2)
            print(f"💾 文章数据已缓存至 {cache_file.name}")
        except Exception:
            pass

    # 如果博主头像是外链，也内联成 Base64
    if inline_images and author_avatar and author_avatar.startswith("http"):
        async with httpx.AsyncClient(timeout=10.0) as avatar_client:
            author_avatar = await download_image_as_base64(avatar_client, author_avatar) or author_avatar

    # 4. 读取新浪经典 Theme 30_1 真实素材并转 Base64 内嵌
    assets_dir = CURRENT_DIR / "assets"
    def load_asset_b64(fname, mime="image/png"):
        fpath = assets_dir / fname
        if fpath.exists():
            return f"data:{mime};base64," + base64.b64encode(fpath.read_bytes()).decode('ascii')
        return ""

    banner_b64 = load_asset_b64("sinablogb.jpg", "image/jpeg")
    navbg_b64 = load_asset_b64("blognavbg.png", "image/png")
    modelhead_b64 = load_asset_b64("modelhead.png", "image/png")
    modelbody_b64 = load_asset_b64("modelbody.png", "image/png")
    modelfoot_b64 = load_asset_b64("modelfoot.png", "image/png")
    dot_b64 = load_asset_b64("SG_dot.gif", "image/gif")
    linedot_b64 = load_asset_b64("SG_linedot.gif", "image/gif")
    newsp_b64 = load_asset_b64("sg_newsp.png", "image/png")

    # 5. 渲染新浪电脑端原版经典模板
    print("\n🎨 正在渲染新浪经典原版 PC 样式与离线即时搜索模块...")
    template_path = CURRENT_DIR / "templates" / "sina_classic.html"
    with open(template_path, "r", encoding="utf-8") as f:
        template_str = f.read()

    template = Template(template_str)
    rendered_html = template.render(
        title=scraper.category_name or f"{author_name} 的新浪博客",
        author_name=author_name,
        author_avatar=author_avatar,
        category_name=scraper.category_name,
        total_articles=len(articles_data),
        banner_b64=banner_b64,
        navbg_b64=navbg_b64,
        modelhead_b64=modelhead_b64,
        modelbody_b64=modelbody_b64,
        modelfoot_b64=modelfoot_b64,
        dot_b64=dot_b64,
        linedot_b64=linedot_b64,
        newsp_b64=newsp_b64,
        articles_json=json.dumps(articles_data, ensure_ascii=False)
    )

    with open(output_html_file, "w", encoding="utf-8") as f:
        f.write(rendered_html)

    file_size_mb = output_html_file.stat().st_size / (1024 * 1024)
    print("\n" + "=" * 70)
    print("🎉 导出成功！新浪博客原版版面离线合集已生成！")
    print(f"📁 输出文件: {output_html_file}")
    print(f"📊 文件大小: {file_size_mb:.2f} MB")
    print(f"📖 包含文章: {len(articles_data)} 篇")
    print("✨ 特性说明: 100% 还原电脑端双栏原版排版 · 内置左侧即时搜索框 · 纯离线单文件")
    print("=" * 70 + "\n")

    return str(output_html_file)

def main():
    parser = argparse.ArgumentParser(description="新浪博客电脑端原版经典排版离线导出工具")
    parser.add_argument("--url", "-u", required=True, help="新浪博客主页链接、分类专栏链接或博文目录链接")
    parser.add_argument("--output-dir", "-o", default=None, help="输出文件目录 (默认保存在项目的 downloads/ 目录)")
    parser.add_argument("--max-articles", "-m", type=int, default=None, help="最大导出篇数限制 (默认全部导出)")
    parser.add_argument("--no-inline-images", action="store_true", help="不内联 Base64 图片 (降低文件体积，但离线看需联网)")
    parser.add_argument("--force-refresh", "-f", action="store_true", help="强制在线抓取，忽略本地缓存")

    args = parser.parse_args()

    asyncio.run(export_classic_sina(
        target_url=args.url,
        output_dir=args.output_dir,
        max_articles=args.max_articles,
        inline_images=not args.no_inline_images,
        force_refresh=args.force_refresh
    ))

if __name__ == "__main__":
    main()
