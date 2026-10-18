# -*- coding: utf-8 -*-
"""
简书原版 HTML 本地预览测试脚本

用法:
    py -3 test_jianshu_preview.py <简书文章URL或用户主页URL>

示例:
    py -3 test_jianshu_preview.py https://www.jianshu.com/p/9400cd94bcb6
    py -3 test_jianshu_preview.py https://www.jianshu.com/u/5bb0bdbbee02

脚本会:
1. 抓取指定文章/用户文章列表
2. 调用 OriginalHTMLExporter 生成单文件原版 HTML
3. 将结果保存到 ./jianshu_preview_test.html
4. 打印 ArticleItem 关键字段，方便调试头像、正文等问题
"""

import asyncio
import json
import sys
from pathlib import Path

# 把 backend 加入 Python 路径
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from app.scrapers.jianshu import JianshuScraper
from app.exporters.original_html_exporter import OriginalHTMLExporter


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "https://www.jianshu.com/p/9400cd94bcb6"
    print(f"[测试目标] {target}")

    scraper = JianshuScraper(target)

    # 获取作者信息（调试用）
    author_info = await scraper.get_author_info()
    print(f"[作者信息] {json.dumps(author_info, ensure_ascii=False, indent=2)}")
    print(f"[self.author_avatar] {scraper.author_avatar}")

    # 获取文章列表
    article_metas = await scraper.get_article_list()
    print(f"[文章数量] {len(article_metas)}")

    if not article_metas:
        print("未获取到文章，测试结束。")
        return

    # 只取前 3 篇做预览（太多图片会很大）
    preview_metas = article_metas[:3]

    # 抓取详情
    articles = []
    for meta in preview_metas:
        print(f"[抓取] {meta.get('title')} - {meta.get('url')}")
        art = await scraper.scrape_article_detail(meta)
        print(f"  -> title={art.title}")
        print(f"  -> author={art.author}")
        print(f"  -> author_avatar={art.author_avatar}")
        print(f"  -> publish_time={art.publish_time}")
        print(f"  -> like_count={art.like_count}, read_num={art.read_num}, comment_count={art.comment_count}")
        print(f"  -> content_html length={len(art.content_html)}")
        print(f"  -> images count={len(art.images)}")
        articles.append(art)

    # 生成原版 HTML
        output_dir = Path(__file__).resolve().parent / "test_output"
        output_dir.mkdir(exist_ok=True)
        exporter = OriginalHTMLExporter(
            author_name=scraper.author_name or articles[0].author or "简书作者",
            platform="jianshu",
            output_dir=output_dir
        )
        print("[开始生成原版 HTML...]")
        output_path = await exporter.export(articles, "简书预览测试")

        # 复制一份到项目根目录方便用户打开
        preview_path = Path(__file__).resolve().parent / "jianshu_preview_test.html"
        preview_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")

        print(f"\n[生成成功] {output_path}")
        print(f"[预览文件] {preview_path}")
        print("请用浏览器打开上面的预览文件查看效果。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"[错误] {e}")
        traceback.print_exc()
