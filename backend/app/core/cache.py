import sqlite3
import json
import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from app.config import BASE_DIR
from app.models import ArticleItem

CACHE_DIR = BASE_DIR / "data"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CACHE_DIR / "article_cache.db"

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS article_cache (
                url TEXT PRIMARY KEY,
                platform TEXT,
                article_id TEXT,
                title TEXT,
                author TEXT,
                publish_time TEXT,
                summary TEXT,
                content_html TEXT,
                content_markdown TEXT,
                images_json TEXT,
                tags_json TEXT,
                cached_at TEXT
            )
        """)
        conn.commit()

init_db()

def get_cached_article(url: str) -> Optional[ArticleItem]:
    """根据 URL 查询本地持久化缓存中的文章详情"""
    if not url:
        return None
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM article_cache WHERE url = ?", (url.strip(),))
            row = cur.fetchone()
            if row:
                images = json.loads(row["images_json"]) if row["images_json"] else []
                tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                return ArticleItem(
                    id=row["article_id"] or row["url"],
                    title=row["title"] or "",
                    author=row["author"] or "",
                    publish_time=row["publish_time"] or "",
                    url=row["url"],
                    platform=row["platform"] or "",
                    summary=row["summary"] or "",
                    content_html=row["content_html"] or "",
                    content_markdown=row["content_markdown"] or "",
                    images=images,
                    tags=tags
                )
    except Exception as e:
        print(f"读取缓存异常: {e}")
    return None

def save_cached_article(article: ArticleItem):
    """将抓取并清洗后的文章保存到本地持久化缓存中"""
    if not article or not article.url:
        return
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        images_json = json.dumps(article.images, ensure_ascii=False)
        tags_json = json.dumps(article.tags, ensure_ascii=False)
        
        with _get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO article_cache (
                    url, platform, article_id, title, author, publish_time,
                    summary, content_html, content_markdown, images_json, tags_json, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article.url.strip(),
                article.platform,
                article.id,
                article.title,
                article.author,
                article.publish_time,
                article.summary,
                article.content_html,
                article.content_markdown,
                images_json,
                tags_json,
                now_str
            ))
            conn.commit()
    except Exception as e:
        print(f"写入缓存异常: {e}")

def delete_cached_article(url: str):
    """从本地持久化缓存中删除指定文章"""
    if not url:
        return
    try:
        with _get_conn() as conn:
            conn.execute("DELETE FROM article_cache WHERE url = ?", (url.strip(),))
            conn.commit()
    except Exception as e:
        print(f"删除缓存异常: {e}")

def clear_all_cache() -> int:
    """清空全部本地持久化缓存"""
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM article_cache")
            conn.commit()
            return cur.rowcount
    except Exception as e:
        print(f"清空缓存异常: {e}")
        return 0

def get_cache_stats() -> Dict[str, Any]:
    """获取当前缓存的文章总数与平台分布"""
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as total FROM article_cache")
            total = cur.fetchone()["total"]
            
            cur.execute("SELECT platform, COUNT(*) as cnt FROM article_cache GROUP BY platform")
            by_platform = {row["platform"]: row["cnt"] for row in cur.fetchall()}
            return {"total_cached_articles": total, "by_platform": by_platform}
    except Exception:
        return {"total_cached_articles": 0, "by_platform": {}}

