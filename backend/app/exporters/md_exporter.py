import re
from pathlib import Path
from typing import List
import datetime
from app.exporters.base import BaseExporter
from app.models import ArticleItem
from app.config import BRAND_OFFICIAL_ACCOUNT, BRAND_FOOTER_NOTE, BRAND_DISCLAIMER

class MarkdownExporter(BaseExporter):
    """Markdown 单文件合并导出器"""

    async def export(self, articles: List[ArticleItem], filename_prefix: str) -> Path:
        output_file = self.output_dir / f"{filename_prefix}.md"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        md_lines = []
        # 1. 封面与文档头
        if self.platform in ["微信公众号", "wechat"] or "公众号" in self.platform:
            doc_title = f"【{self.author_name}公众号合集】" if "公众号" not in self.author_name else f"【{self.author_name}文章合集】"
        else:
            doc_title = f"【{self.author_name}】全量文章知识合集"

        md_lines.append(f"# {doc_title}\n")
        md_lines.append(f"> **博主/来源**：{self.author_name} ({self.platform})  \n")
        md_lines.append(f"> **排版整理**：微信公众号【{BRAND_OFFICIAL_ACCOUNT}】  \n")
        md_lines.append(f"> **导出时间**：{now_str}  \n")
        md_lines.append(f"> **文章总数**：共计 {len(articles)} 篇  \n")
        md_lines.append(f"> **免责声明**：{BRAND_DISCLAIMER}\n")
        md_lines.append("\n---\n")
        
        # 2. 全局目录 (Table of Contents)
        md_lines.append("## 📚 目录导航 (Table of Contents)\n")
        for idx, art in enumerate(articles, 1):
            clean_title = art.title.replace("[", "\\[").replace("]", "\\]")
            # 生成锚点
            anchor = re.sub(r"[^\w\u4e00-\u9fa5\-]+", "-", f"{idx}-{art.title}").strip("-").lower()
            date_info = f" `({art.publish_time})`" if art.publish_time else ""
            md_lines.append(f"- [{idx}. {clean_title}](#{anchor}){date_info}")
        md_lines.append("\n---\n")
        
        # 3. 逐篇文章内容拼装
        for idx, art in enumerate(articles, 1):
            anchor = re.sub(r"[^\w\u4e00-\u9fa5\-]+", "-", f"{idx}-{art.title}").strip("-").lower()
            md_lines.append(f'<a id="{anchor}"></a>\n')
            md_lines.append(f"## {idx}. {art.title}\n")
            
            # YAML Frontmatter 元数据块 (方便 RAG 和知识库切分)
            md_lines.append("```yaml")
            md_lines.append(f"title: {art.title}")
            md_lines.append(f"author: {art.author or self.author_name}")
            md_lines.append(f"date: {art.publish_time}")
            md_lines.append(f"platform: {art.platform or self.platform}")
            md_lines.append(f"url: {art.url}")
            md_lines.append(f"curator: 微信公众号【{BRAND_OFFICIAL_ACCOUNT}】")
            md_lines.append("```\n")
            
            # 文章正文
            md_lines.append(art.content_markdown)

            # 互动数据与精选留言
            stats = []
            if art.read_num:
                stats.append(f"阅读 {art.read_num if art.read_num < 10000 else f'{art.read_num/10000:.1f}万'.replace('.0万', '万')}")
            if art.like_count:
                stats.append(f"点赞 {art.like_count if art.like_count < 10000 else f'{art.like_count/10000:.1f}万'.replace('.0万', '万')}")
            if art.old_like_count:
                stats.append(f"在看 {art.old_like_count if art.old_like_count < 10000 else f'{art.old_like_count/10000:.1f}万'.replace('.0万', '万')}")
            if art.share_count:
                stats.append(f"分享 {art.share_count if art.share_count < 10000 else f'{art.share_count/10000:.1f}万'.replace('.0万', '万')}")
            if art.comment_count:
                stats.append(f"留言 {art.comment_count}")

            if stats or art.comments:
                md_lines.append("\n\n---")
                md_lines.append("### 💬 互动数据与精选留言\n")
                if stats:
                    md_lines.append(f"> **📊 数据统计**：{' · '.join(stats)}\n")
                if art.comments:
                    md_lines.append("> **📝 精选留言**：\n")
                    for c in art.comments:
                        nick = c.get("nick_name", "微信用户")
                        time_str = f" `({c.get('create_time')})`" if c.get("create_time") else ""
                        like_str = f" 👍 {c.get('like_num')}" if c.get("like_num") else ""
                        ip_str = f" [{c.get('ip_region')}]" if c.get("ip_region") else ""
                        content = c.get("content", "").replace("\n", " ")
                        md_lines.append(f"> - **{nick}**{ip_str}{time_str}{like_str}：{content}")
                        for r in c.get("replies", []):
                            r_nick = r.get("nick_name", "作者")
                            r_content = r.get("content", "").replace("\n", " ")
                            md_lines.append(f">   - ↳ **{r_nick}回复**：{r_content}")
                    md_lines.append("")

            # 页脚水印与免责提示
            md_lines.append(f"\n\n> *{BRAND_FOOTER_NOTE}*")
            md_lines.append("\n\n---\n")
            
        final_content = "\n".join(md_lines)
        output_file.write_text(final_content, encoding="utf-8")
        return output_file
