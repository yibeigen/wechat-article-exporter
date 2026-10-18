import sqlite3
import json
import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from app.config import BASE_DIR
from app.models import ArticleItem

CACHE_DIR = BASE_DIR / "data"
# mkdir 也可能因权限不足抛异常（如软件装在 Program Files 而以普通用户运行），
# 必须与 init_db 一样做容错，否则 import 阶段进程直接崩死（用户报障：管理员权限才能打开）
try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    print(f"⚠️ 缓存目录创建失败（磁盘只读或权限不足）：{type(e).__name__}: {e}")
DB_PATH = CACHE_DIR / "article_cache.db"

# 缓存可用性开关：初始化失败（磁盘只读/权限不足）时降级为无缓存模式。
# 降级只损失「断点续爬/重复抓取秒过」能力，任务创建、抓取、导出全链路照常工作，
# 避免过去"数据库打不开 => 整个后端 import 崩溃 => 软件完全无法使用"的硬伤。
CACHE_AVAILABLE = True

def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化缓存数据库表结构（模块导入时执行一次）。

    关键容错：此前这里任何异常（磁盘只读、权限不足、杀毒拦截）都会让
    import 直接崩死 => 整个后端服务无法启动 => 用户软件完全打不开。
    现在失败只降级为无缓存模式并打印醒目警告，进程照常存活。
    """
    global CACHE_AVAILABLE
    try:
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
                    content_type TEXT,
                    column_title TEXT,
                    cached_at TEXT
                )
            """)
            # 兼容旧缓存表：如不存在 content_type / column_title 列则动态添加（避免清空已有缓存）
            for col_name in ["content_type", "column_title"]:
                try:
                    conn.execute(f"ALTER TABLE article_cache ADD COLUMN {col_name} TEXT")
                except Exception:
                    pass
            conn.commit()
    except Exception as e:
        # 权限不足/磁盘只读：降级运行，而不是让整个后端进程崩溃
        CACHE_AVAILABLE = False
        print(f"⚠️ 本地缓存初始化失败（磁盘只读或权限不足），已降级为无缓存模式：{type(e).__name__}: {e}")
        print("⚠️ 影响：任务仍可正常抓取导出，但断点续爬与重复抓取加速不可用。")
        print("⚠️ 建议：将软件安装到非系统盘（如 D:\\），或右键\"以管理员身份运行\"。")

init_db()

def get_cached_article(url: str) -> Optional[ArticleItem]:
    """根据 URL 查询本地持久化缓存中的文章详情"""
    if not url or not CACHE_AVAILABLE:
        return None
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM article_cache WHERE url = ?", (url.strip(),))
            row = cur.fetchone()
            if row:
                images = json.loads(row["images_json"]) if row["images_json"] else []
                tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                category = tags[0] if (tags and len(tags) > 0) else None
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
                    tags=tags,
                    category=category,
                    content_type=row["content_type"] or None,
                    column_title=row["column_title"] or None
                )
    except Exception as e:
        print(f"读取缓存异常: {e}")
    return None

def save_cached_article(article: ArticleItem):
    """将抓取并清洗后的文章保存到本地持久化缓存中"""
    if not article or not article.url or not CACHE_AVAILABLE:
        return
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        images_json = json.dumps(article.images, ensure_ascii=False)
        tags_json = json.dumps(article.tags, ensure_ascii=False)
        
        with _get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO article_cache (
                    url, platform, article_id, title, author, publish_time,
                    summary, content_html, content_markdown, images_json, tags_json, content_type, column_title, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                article.content_type,
                article.column_title,
                now_str
            ))
            conn.commit()
    except Exception as e:
        print(f"写入缓存异常: {e}")

def delete_cached_article(url: str):
    """从本地持久化缓存中删除指定文章"""
    if not url or not CACHE_AVAILABLE:
        return
    try:
        with _get_conn() as conn:
            conn.execute("DELETE FROM article_cache WHERE url = ?", (url.strip(),))
            conn.commit()
    except Exception as e:
        print(f"删除缓存异常: {e}")

def clear_all_cache() -> int:
    """清空全部本地持久化缓存"""
    if not CACHE_AVAILABLE:
        return 0
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

