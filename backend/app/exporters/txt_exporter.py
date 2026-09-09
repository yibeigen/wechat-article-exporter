import re
from pathlib import Path
from typing import List
import datetime
from app.exporters.base import BaseExporter
from app.models import ArticleItem
from app.config import BRAND_OFFICIAL_ACCOUNT, BRAND_FOOTER_NOTE, BRAND_DISCLAIMER

class TxtExporter(BaseExporter):
    """TXT 纯净文本语料导出器"""

    async def export(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        output_file = self.output_dir / f"{filename_prefix}.txt"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        txt_lines = []
        if self.platform in ["微信公众号", "wechat"] or "公众号" in self.platform:
            doc_title = f"【{self.author_name}公众号合集】" if "公众号" not in self.author_name else f"【{self.author_name}文章合集】"
        else:
            doc_title = f"【{self.author_name}】全量文章语料合集"

        txt_lines.append(doc_title)
        txt_lines.append(f"排版整理：微信公众号【{BRAND_OFFICIAL_ACCOUNT}】")
        txt_lines.append(f"导出时间：{now_str}")
        txt_lines.append(f"来源平台：{self.platform}")
        txt_lines.append(f"文章总数：{len(articles)} 篇")
        txt_lines.append(f"免责声明：{BRAND_DISCLAIMER}")
        txt_lines.append("=" * 50 + "\n")

        # 目录
        txt_lines.append("【目录】")
        for idx, art in enumerate(articles, 1):
            txt_lines.append(f"{idx}. {art.title} ({art.publish_time})")
        txt_lines.append("\n" + "=" * 50 + "\n")

        # 正文
        for idx, art in enumerate(articles, 1):
            txt_lines.append(f"第 {idx} 篇：{art.title}")
            if art.tags:
                tags_str = "   ".join([f"#{t.strip().lstrip('#')}" for t in art.tags if t.strip()])
                txt_lines.append(f"标签：{tags_str}")
            txt_lines.append(f"原文：{art.platform or self.platform}  ·  {art.author or self.author_name}  ·  {art.publish_time or '未知时间'}")
            if art.url:
                txt_lines.append(f"原文链接：{art.url}")
            txt_lines.append("-" * 40)
            
            # 净化 Markdown 语法为纯文本
            plain_text = art.content_markdown or ""
            # 剔除正文头部重复的标题
            p_lines = plain_text.split("\n")
            p_idx = 0
            while p_idx < len(p_lines):
                line_str = p_lines[p_idx].strip()
                if not line_str:
                    p_idx += 1
                    continue
                no_hash = re.sub(r'^#+\s*', '', line_str).strip()
                if no_hash.lower() == art.title.strip().lower() or line_str.startswith(art.title):
                    p_idx += 1
                    continue
                break
            clean_body_txt = "\n".join(p_lines[p_idx:]).strip()

            clean_body_txt = re.sub(r"!\[.*?\]\(.*?\)", "", clean_body_txt) # 移除图片
            clean_body_txt = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", clean_body_txt) # 还原超链接为文字
            clean_body_txt = re.sub(r"#{1,6}\s*", "", clean_body_txt) # 移除标题 #
            clean_body_txt = re.sub(r"[`*_~]", "", clean_body_txt) # 移除强调符
            clean_body_txt = re.sub(r"\n{3,}", "\n\n", clean_body_txt).strip()
            
            txt_lines.append(clean_body_txt)
            txt_lines.append(f"\n\n[{BRAND_FOOTER_NOTE}]")
            txt_lines.append("\n" + "=" * 50 + "\n")

        output_file.write_text("\n".join(txt_lines), encoding="utf-8")
        return output_file
