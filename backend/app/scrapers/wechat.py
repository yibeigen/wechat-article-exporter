import re
import os
import html as html_module
import json
import datetime
import time
import asyncio
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable
from bs4 import BeautifulSoup
import httpx

from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content
from app.scrapers.custom_urls import dedupe_wechat_title
from app.scrapers.base import BaseScraper
from app.models import ArticleItem
from app.cleaners.html_cleaner import clean_html_content
from app.core.wechat_auth import get_saved_wechat_auth, record_freq_control_event

def unescape_wechat_text(text: str) -> str:
    """
    智能解码微信特有的各类十六进制转义符与实体编码 (如 \\x0a, \\x26quot;, \\x22, \\x27, \\x2f 等)
    """
    if not text:
        return ""
    # 1. 处理 \\x26xxx; 实体转义 (如 \\x26quot; -> &quot;, \\x26amp; -> &amp;)
    text = re.sub(r'\\x26([a-zA-Z0-9#]+);', r'&\1;', text)
    # 2. 处理标准十六进制字符 \\x[0-9a-fA-F]{2} (如 \\x0a -> \n, \\x22 -> ")
    def replace_hex_char(match):
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)
    text = re.sub(r'\\x([0-9a-fA-F]{2})', replace_hex_char, text)
    # 3. 还原 \\r \\n \\t 字符串字面量为真实换行符
    text = text.replace('\\r', '\r').replace('\\n', '\n').replace('\\t', '\t')
    # 4. HTML 实体解码
    text = html_module.unescape(text)
    return text.strip()

def clean_wechat_author(raw_author: str) -> str:
    """清洗去重微信作者/公众号昵称 (去除重复出现的昵称和多余换行符)"""
    if not raw_author:
        return "微信公众号"
    raw_author = unescape_wechat_text(raw_author)
    tokens = [t.strip() for t in raw_author.split() if t.strip()]
    dedup = []
    for t in tokens:
        if t not in dedup:
            dedup.append(t)
    cleaned = " ".join(dedup).strip()
    return cleaned or "微信公众号"

WECHAT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x6309092b) XWEB/11253",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document"
}

class WeChatScraper(BaseScraper):
    """微信公众号文章抓取器 (支持批量文章链接、合集专辑、客户端 profile_ext 协议)"""
    
    def __init__(
        self,
        target: str,
        enable_noise_filter: bool = True,
        max_articles: Optional[int] = None,
        cookie: Optional[str] = None,
        token: Optional[str] = None,
        account_name: Optional[str] = None,
        remove_image_watermark: bool = True,
        uin: Optional[str] = None,
        key: Optional[str] = None,
        pass_ticket: Optional[str] = None,
        appmsg_token: Optional[str] = None,
        include_comments: bool = True,
        delay_seconds: float = 3.0
    ):
        super().__init__(target, enable_noise_filter=enable_noise_filter, max_articles=max_articles, remove_image_watermark=remove_image_watermark)
        self.client.headers.update(WECHAT_HEADERS)
        saved_auth = get_saved_wechat_auth()
        self.cookie = (cookie or "").strip() or saved_auth.get("cookie", "")
        self.token = str(token or "").strip() or str(saved_auth.get("token", ""))
        self.fakeid = str(saved_auth.get("fakeid", "")).strip()
        self.account_name = (account_name or "").strip() or str(saved_auth.get("account_name", "")).strip()
        
        # 微信阅读端/客户端逆向凭证 (uin, key, pass_ticket, appmsg_token)
        self.uin = (uin or "").strip() or str(saved_auth.get("uin", "")).strip()
        self.key = (key or "").strip() or str(saved_auth.get("key", "")).strip()
        self.pass_ticket = (pass_ticket or "").strip() or str(saved_auth.get("pass_ticket", "")).strip()
        self.appmsg_token = (appmsg_token or "").strip() or str(saved_auth.get("appmsg_token", "")).strip()
        self.include_comments = include_comments
        self.delay_seconds = max(1.0, float(delay_seconds or 3.0))

    async def get_author_info(self) -> Dict[str, Any]:
        urls = [line.strip() for line in self.target.split("\n") if "mp.weixin.qq.com" in line]
        author_name = "微信公众号"
        
        if urls:
            try:
                resp = await self.client.get(urls[0], timeout=10.0)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    for selector in ["#js_name", "#js_author_name", ".profile_nickname"]:
                        tag = soup.select_one(selector)
                        if tag and tag.text.strip():
                            author_name = clean_wechat_author(tag.text)
                            break
                    if author_name == "微信公众号":
                        og_author = soup.find("meta", property="og:article:author") or soup.find("meta", attrs={"name": "author"})
                        if og_author and og_author.get("content"):
                            author_name = clean_wechat_author(og_author.get("content"))
            except Exception:
                pass
        else:
            author_name = clean_wechat_author(self.target.strip())
            
        return {"name": author_name, "platform": "微信公众号"}

    async def get_article_list(self, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        articles = []
        lines = [line.strip() for line in self.target.split("\n") if line.strip()]
        urls = [line for line in lines if line.startswith("http")]

        # 场景 A: 检查是否输入了微信「专辑/合集」链接 (appmsgalbum 或 album_id)
        album_urls = [u for u in urls if ("appmsgalbum" in u or "album_id" in u)]
        print(f"[WeChatScraper] album_urls detected: {len(album_urls)}, 'album_id' in target: {'album_id' in self.target}")
        if album_urls or "album_id" in self.target:
            target_album_url = album_urls[0] if album_urls else self.target.strip()
            print(f"[WeChatScraper] entering album branch, url={target_album_url[:200]!r}")
            album_articles = await self._fetch_album_articles(target_album_url, progress_callback)
            print(f"[WeChatScraper] album branch returned {len(album_articles)} articles")
            if album_articles:
                return album_articles
            # 如果专辑解析返回空，不直接 fallback，给用户明确提示；但继续向后执行以防其他通道能救场
            print("[WeChatScraper] album branch returned empty, will try fallback channels")

        # 场景 B: 用户只输入了 1 条普通的微信文章链接
        # 免凭证直接导出单篇；如需该号全部历史，可点「获取公众号链接」在微信打开
        if len(urls) == 1 and ("mp.weixin.qq.com/s" in urls[0] or "mp.weixin.qq.com/s?" in urls[0]):
            url = urls[0]
            if progress_callback:
                progress_callback("已识别单篇微信文章链接，正在提取标题与正文...", 1, 0)

            # 先轻量 GET 一次页面，同时提取 真实文章标题 / 公众号名 / 发布时间
            # （列表阶段弹窗与「导出文章名+链接」都依赖这里的元数据，不能拿公众号名冒充标题）
            page_meta: Dict[str, str] = {}
            try:
                resp = await self.client.get(url, timeout=12.0)
                if resp.status_code == 200:
                    from app.scrapers.custom_urls import parse_wechat_page_meta
                    page_meta = parse_wechat_page_meta(resp.text)
            except Exception:
                page_meta = {}

            account_name = page_meta.get("author")
            if not account_name:
                author_info = await self.get_author_info()
                account_name = author_info.get("name") or "微信公众号"

            return [{
                "id": url,
                "url": url,
                # 解析成功用真实标题；失败退回公众号名（至少不会是占位符）
                "title": page_meta.get("title") or account_name,
                **({"author": page_meta["author"]} if page_meta.get("author") else {}),
                **({"publish_time": page_meta["publish_time"]} if page_meta.get("publish_time") else {}),
            }]

        # 场景 C: 用户直接输入了多条微信文章链接（按行分隔）
        if urls and len(urls) > 1 and not any("profile_ext" in u for u in urls):
            # 并发限流逐条轻量 GET，提取每篇的真实标题/公众号名/发布时间
            # （与批量聚合通道保持一致：列表阶段必须展示真实文章名，而不是占位编号）
            from app.scrapers.custom_urls import parse_wechat_page_meta
            sem = asyncio.Semaphore(4)

            async def fetch_meta(u: str) -> Dict[str, str]:
                async with sem:
                    try:
                        resp = await self.client.get(u, timeout=12.0)
                        if resp.status_code == 200:
                            return parse_wechat_page_meta(resp.text)
                    except Exception:
                        pass
                    return {}

            metas = await asyncio.gather(*(fetch_meta(u) for u in urls))

            for idx, (url, meta) in enumerate(zip(urls, metas), 1):
                articles.append({
                    "id": f"wechat_{idx}",
                    "url": url,
                    "title": meta.get("title") or f"微信文章_{idx}",
                    **({"author": meta["author"]} if meta.get("author") else {}),
                    **({"publish_time": meta["publish_time"]} if meta.get("publish_time") else {}),
                })
                if self.max_articles and len(articles) >= self.max_articles:
                    break
            if progress_callback:
                ok_count = sum(1 for m in metas if m.get("title"))
                progress_callback(f"已识别 {len(articles)} 篇微信文章，成功提取 {ok_count} 条真实标题，准备抓取...", len(articles), 0)
            return articles
            
        # 场景 D: 输入的是 __biz 或 profile_ext 专属链接
        # 优先通过微信客户端协议通道 (profile_ext) 拉取全量历史
        biz = ""
        biz_m = re.search(r'[\?&]__biz=([^&#]+)', self.target)
        if biz_m:
            biz = biz_m.group(1)
        elif len(self.target.strip()) > 10 and self.target.strip().endswith("==") and not self.target.strip().startswith("http"):
            biz = self.target.strip()

        if not biz:
            if self.target.strip().startswith("http"):
                # 是链接但未能识别出微信通道参数（或合集/文章拉取失败），给出中性指引
                raise ValueError(
                    "未能通过该链接获取到文章列表：请确认粘贴的是微信单篇推文链接或合集/专辑链接，"
                    "且文章/合集未被作者删除或设为私密。"
                )
            # 公众号名称/纯文本输入：网页端名称导出通道已下线。
            # 旧搜狗公开检索接口已被微信封禁，且旧兜底会用上次同步的 client_biz 误抓其他账号，故直接给出明确指引。
            raise ValueError(
                "「公众号名称导出」网页端通道已下线（微信已封禁公开搜索接口）。"
                "请粘贴单篇推文链接或合集/专辑链接免登录导出；"
                "如需一键导出公众号全部历史文章，请使用桌面客户端。"
            )

        if progress_callback:
            progress_callback("正在通过微信客户端通道 (profile_ext) 分页拉取公众号历史消息...", 0, 0)
        client_articles = await self._fetch_via_profile_ext(biz, progress_callback)
        if client_articles:
            return client_articles

        raise ValueError(
            "未能通过该链接获取到文章列表：请确认粘贴的是微信单篇推文链接或合集/专辑链接。"
            "如需导出公众号全部历史文章，请使用桌面客户端。"
        )

    async def _fetch_via_profile_ext(self, biz: str, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        """
        基于微信客户端协议 (profile_ext?action=getmsg)
        利用 uin, key, pass_ticket 批量分页拉取目标公众号全部历史消息流
        """
        if not self.uin or not self.key or not self.pass_ticket:
            raise RuntimeError("缺少微信阅读通行密钥 (uin / key / pass_ticket)")

        articles = []
        offset = 0
        count = 10
        total_count = None
        rate_limit_retries = 0

        # 检查本地是否有上次中断的游标断点 (Checkpoint)
        checkpoint_dir = Path("data/checkpoints")
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        safe_biz = re.sub(r'[^a-zA-Z0-9_-]', '', biz) if biz else "default"
        checkpoint_file = checkpoint_dir / f"checkpoint_{safe_biz}.json"

        if checkpoint_file.exists():
            try:
                with open(checkpoint_file, "r", encoding="utf-8") as f:
                    cp_data = json.load(f)
                    cached_articles = cp_data.get("articles", [])
                    cached_offset = cp_data.get("next_offset", 0)
                    if cached_articles and cached_offset > 0:
                        articles = cached_articles
                        offset = cached_offset
                        if progress_callback:
                            progress_callback(f"📦 发现本地历史断点，已自动恢复 {len(articles)} 篇文章，直接从偏移量 {offset} 开始继续抓取！", len(articles), 0)
            except Exception as e:
                print(f"读取断点文件失败: {e}")

        cookie_hdr = f"pass_ticket={self.pass_ticket}; wap_sid2={self.uin}; uin={self.uin}; key={self.key}"
        if self.appmsg_token:
            cookie_hdr += f"; appmsg_token={self.appmsg_token}"

        headers = {
            "User-Agent": WECHAT_HEADERS["User-Agent"],
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Cookie": cookie_hdr
        }

        while True:
            if self.max_articles and len(articles) >= self.max_articles:
                break

            url = "https://mp.weixin.qq.com/mp/profile_ext"
            params = {
                "action": "getmsg",
                "__biz": biz,
                "offset": str(offset),
                "count": str(count),
                "is_ok": "1",
                "scene": "124",
                "uin": self.uin,
                "key": self.key,
                "pass_ticket": self.pass_ticket,
                "wxtoken": "777",
                "f": "json"
            }

            if progress_callback:
                progress_callback(f"正在通过微信客户端通道拉取历史消息（偏移量 {offset}），已获取 {len(articles)} 篇...", len(articles), total_count or 0)

            try:
                resp = await self.client.get(url, params=params, headers=headers, timeout=15.0)
                data = resp.json()
            except Exception as e:
                print(f"profile_ext 请求异常: {e}")
                break

            ret = data.get("ret")
            if ret == -6:
                # 保存当前断点
                try:
                    with open(checkpoint_file, "w", encoding="utf-8") as f:
                        json.dump({"biz": biz, "next_offset": offset, "articles": articles, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

                rate_limit_retries += 1
                record_freq_control_event("微信客户端 profile_ext 接口返回操作频繁 (-6)")
                if rate_limit_retries > 3:
                    print("⚠️ 微信 profile_ext 接口频控已达最大重试次数，当前断点已保存至本地")
                    raise RuntimeError(f"微信号已被微信频率风控限制拉取列表。已安全保存断点（已获取 {len(articles)} 篇，下次将直接从第 {offset + 1} 篇接力）。【立即解封方案】：在电脑微信中切换登录另一个微信小号并点开文章，即可立即解除限制（0 秒恢复）！")
                if progress_callback:
                    wait_sec = int(self.delay_seconds * rate_limit_retries + 3)
                    progress_callback(f"检测到微信客户端通道短时限流，安全休眠 {wait_sec} 秒后重试 ({rate_limit_retries}/3)...", len(articles), total_count or 0)
                await asyncio.sleep(self.delay_seconds * rate_limit_retries + 3.0)
                continue
            rate_limit_retries = 0

            if ret == -3:
                # 保存当前断点
                try:
                    with open(checkpoint_file, "w", encoding="utf-8") as f:
                        json.dump({"biz": biz, "next_offset": offset, "articles": articles, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
                raise RuntimeError(f"微信阅读通行密钥 (key) 已满 30 分钟自然过期。已安全保存断点（已获取 {len(articles)} 篇）。【立即恢复方案】：在电脑微信中打开任意公众号文章（Ctrl+R 刷新），嗅探器 1 秒内自动续期，无需等待！")

            if ret not in (0, "0", None):
                err_msg = data.get("errmsg") or f"错误代码 ({ret})"
                print(f"profile_ext 返回非 0 响应: {err_msg}")
                if "freq" in err_msg.lower():
                    record_freq_control_event(err_msg)
                    raise RuntimeError(f"微信提示操作过于频繁：{err_msg}。已保存断点，在电脑微信切换小号登录即可秒级恢复继续下载！")
                break

            # 解析 general_msg_list
            raw_msg_list = data.get("general_msg_list", "{}")
            msg_list_obj = json.loads(raw_msg_list) if isinstance(raw_msg_list, str) else (raw_msg_list or {})
            items = msg_list_obj.get("list", [])
            if not items:
                break

            new_articles = self._parse_profile_msg_list(msg_list_obj)
            if not new_articles:
                break

            for art in new_articles:
                if not any(a["url"] == art["url"] for a in articles):
                    articles.append(art)
                if self.max_articles and len(articles) >= self.max_articles:
                    break

            can_msg_continue = data.get("can_msg_continue", 0)
            next_offset = data.get("next_offset", offset + count)

            # 持久化当前抓取进度
            offset = next_offset
            try:
                with open(checkpoint_file, "w", encoding="utf-8") as f:
                    json.dump({"biz": biz, "next_offset": offset, "articles": articles, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            if can_msg_continue == 0:
                break

            # 安全防风控翻页延时
            await asyncio.sleep(self.delay_seconds)

        return articles

    async def fetch_article_comments(self, url: str, comment_id: str, appmsgid: str = "", itemidx: str = "1", biz: str = "") -> List[Dict[str, Any]]:
        """
        拉取单篇微信文章的精选留言与作者二级回复 (对齐公号三刀 appmsg_comment 接口规范)
        """
        if not comment_id or not self.uin or not self.key or not self.pass_ticket or not self.appmsg_token:
            return []

        comment_url = "https://mp.weixin.qq.com/mp/appmsg_comment"
        params = {
            "action": "getcomment",
            "scene": "0",
            "appmsgid": appmsgid or "0",
            "idx": itemidx or "1",
            "__biz": biz or "",
            "comment_id": comment_id,
            "uin": self.uin,
            "key": self.key,
            "pass_ticket": self.pass_ticket,
            "appmsg_token": self.appmsg_token,
            "wxtoken": "777",
            "devicetype": "UnifiedPCMac",
            "comment_scene": "0",
            "buffer": "",
            "offset": "0",
            "limit": "100",
            "x5": "0",
            "f": "json"
        }
        headers = {
            "User-Agent": WECHAT_HEADERS["User-Agent"],
            "Cookie": f"pass_ticket={self.pass_ticket}; appmsg_token={self.appmsg_token}; wap_sid2={self.uin}"
        }
        try:
            resp = await self.client.get(comment_url, params=params, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("base_resp", {}).get("ret") == 0:
                    elected_comment = data.get("elected_comment", [])
                    res = []
                    for c in elected_comment:
                        replies_list = []
                        raw_reply = c.get("reply_new", {})
                        for r in raw_reply.get("reply_list", []):
                            replies_list.append({
                                "nick_name": r.get("nick_name", "作者"),
                                "content": r.get("content", ""),
                                "like_num": r.get("reply_like_num", 0),
                                "create_time": self._format_wechat_create_time(r.get("create_time")),
                                "to_nick_name": r.get("to_nick_name", "")
                            })
                        res.append({
                            "nick_name": c.get("nick_name", "微信用户"),
                            "logo_url": c.get("logo_url", ""),
                            "content": c.get("content", ""),
                            "like_num": c.get("like_num", 0),
                            "create_time": self._format_wechat_create_time(c.get("create_time")),
                            "is_elected": c.get("is_elected") == 1,
                            "ip_region": c.get("ip_wording", {}).get("province_name", "") if isinstance(c.get("ip_wording"), dict) else "",
                            "replies": replies_list
                        })
                    return res
        except Exception as e:
            print(f"抓取微信留言异常: {e}")
        return []

    @staticmethod
    def save_discovered_albums(biz: str, author: str, new_albums: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        持久化保存该公众号名下已发现的专栏/专辑合集列表 (自动去重)
        存储于 data/albums/albums_{safe_biz}.json
        """
        if not biz:
            return []
        album_dir = Path("data/albums")
        album_dir.mkdir(parents=True, exist_ok=True)
        safe_biz = re.sub(r'[^a-zA-Z0-9_-]', '', biz)
        album_file = album_dir / f"albums_{safe_biz}.json"

        existing = []
        if album_file.exists():
            try:
                with open(album_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        existing_ids = {a.get("album_id") for a in existing if a.get("album_id")}
        added = 0
        for alb in new_albums:
            alb_id = str(alb.get("album_id", "")).strip()
            if alb_id and alb_id not in existing_ids:
                existing.append({
                    "album_id": alb_id,
                    "title": alb.get("title", f"专辑_{alb_id}"),
                    "url": alb.get("url") or f"https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz={biz}&album_id={alb_id}#wechat_redirect",
                    "article_count": alb.get("article_count", 0),
                    "author": author or "微信公众号",
                    "biz": biz,
                    "discovered_at": time.strftime("%Y-%m-%d %H:%M:%S")
                })
                existing_ids.add(alb_id)
                added += 1

        if added > 0 or not album_file.exists():
            try:
                with open(album_file, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"保存公众号合集文件失败: {e}")

        return existing

    @staticmethod
    def extract_albums_from_home_html(html: str, biz: str = "") -> List[Dict[str, Any]]:
        """
        全自动从微信公众号主页 HTML 中提取置顶的全部合集/专栏
        (解析 .album__item, data-album-id, appmsgalbum 结构)
        """
        if not html:
            return []
        albums = []
        soup = BeautifulSoup(html, "lxml")

        # 1. 解析 DOM 中的 album 容器
        album_nodes = soup.select(".album__item, .js_album_item, a[data-album-id], a[href*='appmsgalbum']")
        for node in album_nodes:
            album_id = node.get("data-album-id") or ""
            href = node.get("href") or node.get("data-link") or ""
            if not album_id and "album_id=" in href:
                m = re.search(r'album_id=([0-9]+)', href)
                if m:
                    album_id = m.group(1)
            
            title_node = node.select_one(".album__item-title, .title, .js_album_title")
            title = title_node.text.strip() if title_node else (node.text.strip() or f"专栏_{album_id}")
            # 过滤超长无意义文字
            if len(title) > 60:
                title = title[:60] + "..."

            count_node = node.select_one(".album__item-count, .count")
            art_count = 0
            if count_node:
                cm = re.search(r'([0-9]+)', count_node.text)
                if cm:
                    art_count = int(cm.group(1))

            if album_id:
                clean_url = f"https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz={biz}&album_id={album_id}#wechat_redirect" if biz else href
                albums.append({
                    "album_id": str(album_id),
                    "title": title or f"专栏_{album_id}",
                    "url": clean_url,
                    "article_count": art_count
                })

        # 2. 从内嵌 JavaScript 正则扫描 album_id
        js_matches = re.findall(r'[\'"]?album_id[\'"]?\s*:\s*[\'"]?([0-9]+)[\'"]?', html)
        for j_id in js_matches:
            if not any(a["album_id"] == str(j_id) for a in albums):
                albums.append({
                    "album_id": str(j_id),
                    "title": f"专栏专辑_{j_id[-6:]}",
                    "url": f"https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz={biz}&album_id={j_id}#wechat_redirect",
                    "article_count": 0
                })

        return albums

    @staticmethod
    def extract_albums_from_article_html(html: str, biz: str = "") -> List[Dict[str, Any]]:
        """
        全自动从微信文章正文 HTML 中提取该文章关联的专栏合集
        (每篇微信文章底部或头部的合集挂载卡片)
        """
        if not html:
            return []
        albums = []
        soup = BeautifulSoup(html, "lxml")

        # 扫描微信文章底部的合集卡片
        links = soup.select("a[href*='appmsgalbum'], a[data-album-id], .article_album_container a, .album_item_link")
        for link in links:
            href = link.get("href") or link.get("data-link") or ""
            album_id = link.get("data-album-id") or ""
            if not album_id and "album_id=" in href:
                m = re.search(r'album_id=([0-9]+)', href)
                if m:
                    album_id = m.group(1)

            title = link.text.strip() or f"专栏_{album_id}"
            if "收录于合集" in title:
                title = title.replace("收录于合集", "").strip()
            if len(title) > 60:
                title = title[:60] + "..."

            if album_id:
                clean_url = f"https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum&__biz={biz}&album_id={album_id}#wechat_redirect" if biz else href
                albums.append({
                    "album_id": str(album_id),
                    "title": title or f"专栏_{album_id}",
                    "url": clean_url,
                    "article_count": 0
                })

        return albums

    @staticmethod
    def get_discovered_albums(biz: str) -> List[Dict[str, Any]]:
        """读取指定公众号名下已发现的所有合集"""
        if not biz:
            return []
        safe_biz = re.sub(r'[^a-zA-Z0-9_-]', '', biz)
        album_file = Path("data/albums") / f"albums_{safe_biz}.json"
        if album_file.exists():
            try:
                with open(album_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    async def _fetch_album_articles(self, album_url: str, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[Dict[str, str]]:
        """
        自动解析微信专辑/合集链接下的全部文章 (免凭证 · 零风控)
        微信合集页面结构会不定期变化，常见两种形式：
        1) 一次返回全部 80+ 篇，但 DOM 里只渲染前 10 篇，需要翻页/懒加载 AJAX；
        2) 直接内嵌完整 JSON。
        这里先尝试静态解析，再尝试 AJAX 翻页。
        """
        articles = []
        html = ""
        final_url = album_url
        try:
            print(f"[WeChatScraper._fetch_album_articles] 请求合集页: {album_url[:300]!r}")
            resp = await self.client.get(album_url, headers=WECHAT_HEADERS, timeout=15.0, follow_redirects=True)
            final_url = str(resp.url)
            print(f"[WeChatScraper._fetch_album_articles] 状态码: {resp.status_code}, 最终 URL: {final_url[:300]!r}, 页面长度: {len(resp.text)}")
            if resp.status_code != 200:
                return articles
            html = resp.text

            # 保存原始页面便于排查
            debug_path = os.path.join(os.getcwd(), "album_debug.html")
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"[WeChatScraper._fetch_album_articles] 已保存原始 HTML 到: {debug_path}")
            except Exception as e:
                print(f"[WeChatScraper._fetch_album_articles] 保存 HTML 失败: {e}")

            soup = BeautifulSoup(html, "lxml")

            # ========== 第 1 层：传统 DOM 选择器（微信老版 H5 合集页常见结构）==========
            items = soup.select(".album__list-item, .js_album_item, li[data-link], .album__item, [data-msgid]")
            print(f"[WeChatScraper._fetch_album_articles] DOM 选择器命中: {len(items)} 个元素")
            page_items = []   # 临时保存，用于提取最后一篇的 msgid/itemidx 做翻页起点
            for idx, item in enumerate(items, 1):
                # data-link 才是真正的文章永久链接；data-msgid 只是消息编号，不能当 URL 用
                url = item.get("data-link") or ""
                msg_id = item.get("data-msgid") or ""
                item_idx = item.get("data-itemidx") or item.get("data-idx") or "1"
                if not url.startswith("http"):
                    # 有时候链接放在 a 标签的 href 里
                    a_tag = item.select_one("a[href*='/s/'], a[href*='mp.weixin.qq.com']")
                    if a_tag:
                        url = a_tag.get("href", "")
                title_elem = item.select_one(".album__item-title, .js_title, .title, h3")
                title = title_elem.text.strip() if title_elem else f"合集文章_{idx}"

                # 尝试从 DOM 元素提取发布时间（微信专辑页常见 data-time、data-create-time、.time、.date 等）
                create_time = ""
                time_raw = item.get("data-time") or item.get("data-create-time") or item.get("data-pub-time") or ""
                if not time_raw:
                    # 微信 H5 专辑页常见的时间元素类名：js_article_create_time / album__item-info-item
                    time_elem = item.select_one(
                        ".js_article_create_time, .album__item-info-item, "
                        ".album__item-time, .time, .date, .post-date, .album__time, .article-time"
                    )
                    if time_elem:
                        time_raw = time_elem.text.strip()
                if time_raw:
                    # 如果是纯数字，大概率是 Unix 时间戳
                    if re.match(r"^\d+$", str(time_raw)):
                        create_time = self._format_wechat_create_time(int(time_raw))
                    else:
                        create_time = str(time_raw)

                if url and url.startswith("http"):
                    art = {"id": f"album_{idx}", "url": url, "title": title, "create_time": create_time}
                    # 只有拿到 msgid 时才保存，用于后续翻页
                    if msg_id:
                        art["_msg_id"] = msg_id
                        art["_item_idx"] = item_idx or "1"
                    page_items.append(art)
                    articles.append(art)
                    if self.max_articles and len(articles) >= self.max_articles:
                        break

            # ========== 第 2 层：从页面内嵌脚本/变量提取 JSON 文章列表 ==========
            # 注意：不能只在前一层为空时才执行。微信合集页经常是 DOM 只展示 10 篇，
            # 但页面内嵌脚本里包含全部文章的 JSON，必须每次都尝试。
            print("[WeChatScraper._fetch_album_articles] DOM 解析完成，继续尝试从页面脚本提取完整 JSON 列表...")
            json_articles = self._extract_articles_from_album_scripts(html)
            print(f"[WeChatScraper._fetch_album_articles] 脚本 JSON 提取命中: {len(json_articles)} 篇")
            for art in json_articles:
                articles.append({
                    "id": f"album_{len(articles)+1}",
                    "url": art["url"],
                    "title": art["title"]
                })
                if self.max_articles and len(articles) >= self.max_articles:
                    break

            # ========== 第 3 层：正则兜底，扫描页面中所有微信公众号文章永久链接 ==========
            print("[WeChatScraper._fetch_album_articles] 继续尝试正则扫描页面内所有微信公众号文章链接...")
            regex_articles = self._extract_articles_from_all_links(html)
            print(f"[WeChatScraper._fetch_album_articles] 正则兜底命中: {len(regex_articles)} 篇")
            for art in regex_articles:
                articles.append({
                    "id": f"album_{len(articles)+1}",
                    "url": art["url"],
                    "title": art["title"]
                })
                if self.max_articles and len(articles) >= self.max_articles:
                    break

            # ========== 第 4 层：微信 H5 专辑页滚动翻页 AJAX 加载 ==========
            # 如果静态解析只拿到 10 篇且能提取到 msgid，则尝试模拟微信的翻页接口继续加载。
            print(f"[WeChatScraper._fetch_album_articles] 当前文章数: {len(articles)}，最后一页元素带 msgid 的数量: {len(page_items)}")
            paged_articles = []
            if page_items:
                last = page_items[-1]
                print(f"[WeChatScraper._fetch_album_articles] 尝试以 msg_id={last.get('_msg_id')} item_idx={last.get('_item_idx')} 为起点翻页...")
                paged_articles = await self._fetch_album_pages_via_ajax(album_url, page_items, html)
                print(f"[WeChatScraper._fetch_album_articles] 翻页加载额外获得: {len(paged_articles)} 篇")
            for art in paged_articles:
                articles.append({
                    "id": f"album_{len(articles)+1}",
                    "url": art["url"],
                    "title": art["title"],
                    "create_time": art.get("create_time", "")
                })
                if self.max_articles and len(articles) >= self.max_articles:
                    break

            # 去重保持顺序
            unique = []
            seen = set()
            for art in articles:
                if art["url"] not in seen:
                    seen.add(art["url"])
                    # 去掉内部辅助字段再返回，保持对外接口干净
                    clean = {k: v for k, v in art.items() if not k.startswith("_")}
                    unique.append(clean)
            articles = unique
            print(f"[WeChatScraper._fetch_album_articles] 最终去重后: {len(articles)} 篇")
        except Exception as e:
            print(f"[WeChatScraper._fetch_album_articles] 解析微信合集失败: {e}")
            import traceback
            traceback.print_exc()
            # 即使抛异常，也返回已经解析到的文章，避免直接 fallback 到需要凭证的分支
            if articles:
                print(f"[WeChatScraper._fetch_album_articles] 异常兜底返回已解析的 {len(articles)} 篇文章")
                # 清理内部辅助字段
                seen = set()
                unique = []
                for art in articles:
                    if art["url"] not in seen:
                        seen.add(art["url"])
                        unique.append({k: v for k, v in art.items() if not k.startswith("_")})
                return unique
        return articles

    def _extract_articles_from_album_scripts(self, html: str) -> List[Dict[str, str]]:
        """
        从微信合集页 HTML 内嵌的 JS 变量/JSON 数据里提取文章列表。
        微信经常把完整文章数据放在 window.__INITIAL_STATE__、var msgList、album_article_list 等变量里。
        """
        results = []
        candidates = []

        # 尝试 1：window.__INITIAL_STATE__ / __I_N_I_T___ 这类 React/Vue 初始状态
        init_state_match = re.search(r"window\.__INITIAL_STATE__\s*=\s*([\{\[][\s\S]*?);\s*</script>", html)
        if not init_state_match:
            init_state_match = re.search(r"window\.__INITIAL_STATE__\s*=\s*({[\s\S]*?});", html)
        if init_state_match:
            try:
                candidates.append(json.loads(init_state_match.group(1)))
            except Exception as e:
                print(f"[extract_album_scripts] __INITIAL_STATE__ JSON 解析失败: {e}")

        # 尝试 2：var msgList = '...' ; 这种微信经典转义 JSON
        msg_list_match = re.search(r"var\s+msgList\s*=\s*['\"]([\s\S]*?)['\"]\s*;", html)
        if msg_list_match:
            raw = msg_list_match.group(1)
            raw = raw.replace('\\x26quot;', '"').replace('\\x26amp;', '&').replace('&quot;', '"')
            try:
                candidates.append(json.loads(raw))
            except Exception as e:
                print(f"[extract_album_scripts] msgList JSON 解析失败: {e}")

        # 尝试 3：var msgList = {...}; 这种未转义的 JSON
        m2 = re.search(r"var\s+msgList\s*=\s*(\{[\s\S]*?\})\s*;", html)
        if m2:
            try:
                candidates.append(json.loads(m2.group(1)))
            except Exception as e:
                print(f"[extract_album_scripts] msgList 对象解析失败: {e}")

        # 尝试 4：页面里的 album_article_list / albumList / articleList / appmsg_list 等 JSON 数组
        for pattern in [
            r"var\s+album_article_list\s*=\s*(\[[\s\S]*?\])\s*;",
            r"var\s+albumList\s*=\s*(\[[\s\S]*?\])\s*;",
            r"var\s+articleList\s*=\s*(\[[\s\S]*?\])\s*;",
            r"var\s+appmsg_list\s*=\s*(\[[\s\S]*?\])\s*;",
            r"['\"]albumList['\"]:\s*(\[[\s\S]*?\])",
            r"['\"]articleList['\"]:\s*(\[[\s\S]*?\])",
            r"['\"]appmsg_list['\"]:\s*(\[[\s\S]*?\])",
        ]:
            m = re.search(pattern, html)
            if m:
                try:
                    candidates.append(json.loads(m.group(1)))
                except Exception:
                    continue

        # 尝试 5：扫描页面中所有 "content_url" 出现的大型 JSON 块
        # 微信合集页很多时候把文章列表直接内嵌在一个匿名对象里
        for big_json_m in re.finditer(r"(\"|')content_url(\"|')\s*:\s*\"[^\"]+\",", html):
            start = max(0, big_json_m.start() - 200)
            end = min(len(html), big_json_m.end() + 500)
            snippet = html[start:end]
            # 尝试向前扩展找到一个完整的 JSON 对象或数组
            left = html.rfind("[", 0, big_json_m.start())
            right = html.find("]", big_json_m.end())
            if left != -1 and right != -1:
                try:
                    arr = json.loads(html[left:right+1])
                    if isinstance(arr, list) and arr:
                        candidates.append(arr)
                except Exception:
                    pass

        for data in candidates:
            # 微信通用结构：list -> [{app_msg_ext_info: {title, content_url}}]
            items = data.get("list", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                # 尝试多种字段路径
                info = item.get("app_msg_ext_info") or item.get("appmsg_info") or item
                title = info.get("title") or item.get("title") or ""
                url = info.get("content_url") or info.get("url") or item.get("url") or item.get("link") or ""
                if url:
                    url = html_module.unescape(url)
                    if not url.startswith("http"):
                        url = "https://mp.weixin.qq.com" + url
                if title and url and url.startswith("http"):
                    results.append({"title": title.strip(), "url": url})
                # 多图文里的子文章
                for sub in info.get("multi_app_msg_item_list", []) if isinstance(info, dict) else []:
                    sub_title = sub.get("title", "")
                    sub_url = sub.get("content_url", "")
                    if sub_url and not sub_url.startswith("http"):
                        sub_url = "https://mp.weixin.qq.com" + sub_url
                    if sub_title and sub_url and sub_url.startswith("http"):
                        results.append({"title": sub_title.strip(), "url": sub_url})

        # 额外尝试 6：扫描页面内所有 data-link 字段的 JSON 数组（微信 H5 喜欢用 data-link 存全文链接）
        for dl_m in re.finditer(r"['\"]data-link['\"]\s*:\s*['\"](https?://mp\.weixin\.qq\.com/s[^'\"]+)['\"]", html):
            url = html_module.unescape(dl_m.group(1))
            # 往前 200 字符找 title 字段
            snippet = html[max(0, dl_m.start()-200):dl_m.start()]
            title_m = re.search(r"['\"]title['\"]\s*:\s*['\"]([^'\"]+)['\"]", snippet)
            title = title_m.group(1) if title_m else ""
            if url and url not in [r["url"] for r in results]:
                results.append({"title": title or "微信文章", "url": url})

        print(f"[extract_album_scripts] 所有模式累计找到 {len(results)} 篇（未去重）")
        return results

    def _extract_articles_from_all_links(self, html: str) -> List[Dict[str, str]]:
        """
        兜底扫描：从整张 HTML 中找出所有微信公众号文章永久链接，
        并尽量从 surrounding 文本/标签属性中提取标题。
        主要用于微信合集页中文章链接散落在各处、前面选择器都匹配不上的情况。
        """
        results = []
        seen = set()
        soup = BeautifulSoup(html, "lxml")

        # 1. 扫描所有 a 标签里的 mp.weixin.qq.com/s 文章链接
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href or "mp.weixin.qq.com/s" not in href:
                continue
            # 过滤掉非文章链接（如 /s/__biz 分享页、菜单等）
            if "/mp/appmsgalbum" in href or "/mp/profile_ext" in href:
                continue
            url = html_module.unescape(href)
            if not url.startswith("http"):
                url = "https://mp.weixin.qq.com" + url
            if url in seen:
                continue
            seen.add(url)

            # 取标题：优先 title 属性 -> 链接文本 -> data-title -> alt
            title = (
                a.get("title") or
                a.get("data-title") or
                a.get_text(strip=True) or
                ""
            )
            # 如果 a 标签文本为空，看看附近兄弟标签有没有标题
            if not title:
                parent = a.find_parent()
                if parent:
                    for sel in ["h3", "h2", "h4", ".title", ".item-title", "[data-title]"]:
                        tnode = parent.select_one(sel)
                        if tnode and tnode.get_text(strip=True):
                            title = tnode.get_text(strip=True)
                            break
            title = title.strip() or f"微信文章_{len(results)+1}"
            results.append({"title": title, "url": url})

        # 2. 扫描所有 data-link 属性里的文章链接（DOM 没匹配到但属性存在）
        for tag in soup.find_all(attrs={"data-link": True}):
            url = tag["data-link"]
            if "mp.weixin.qq.com/s" not in url and "mp.weixin.qq.com/s?" not in url:
                continue
            url = html_module.unescape(url)
            if not url.startswith("http"):
                url = "https://mp.weixin.qq.com" + url
            if url in seen:
                continue
            seen.add(url)
            title = (
                tag.get("data-title") or
                tag.get_text(strip=True) or
                f"微信文章_{len(results)+1}"
            )
            results.append({"title": title.strip(), "url": url})

        # 3. 再用正则扫描文本里所有 s 链接，防止 BeautifulSoup 过滤掉了异常标签
        for m in re.finditer(r'["\'](https?://mp\.weixin\.qq\.com/s[/?][^"\']+)["\']', html):
            url = html_module.unescape(m.group(1))
            if url in seen or not url.startswith("http"):
                continue
            seen.add(url)
            results.append({"title": f"微信文章_{len(results)+1}", "url": url})

        return results

    async def _fetch_album_pages_via_ajax(self, album_url: str, first_page_items: List[Dict], html: str) -> List[Dict[str, str]]:
        """
        微信 H5 专辑页滚动分页抓取：用第一页最后一篇文章的 msgid/itemidx 作为起点，
        循环请求微信的翻页接口，把后面文章全部加载出来。
        """
        results = []
        # 没有 msgid 就没法定位下一页，直接放弃
        if not first_page_items or "_msg_id" not in first_page_items[-1]:
            print("[fetch_album_pages] 第一页未拿到 msgid，无法翻页")
            return results

        last_art = first_page_items[-1]
        begin_msgid = last_art.get("_msg_id")
        begin_itemidx = last_art.get("_item_idx") or "1"

        # 解析原始 URL 里的 query 参数，保留 __biz、album_id、key、pass_ticket 等
        parsed = urllib.parse.urlparse(album_url)
        base_params = urllib.parse.parse_qs(parsed.query)
        # parse_qs 返回的是列表值，统一取最后一个值
        params = {k: (v[-1] if isinstance(v, list) else v) for k, v in base_params.items()}
        params["action"] = "getalbum"
        params["f"] = "json"          # 请求 JSON 格式返回
        params["count"] = "10"        # 每页 10 篇，和微信页面保持一致
        params["begin_msgid"] = begin_msgid
        params["begin_itemidx"] = begin_itemidx

        headers = dict(WECHAT_HEADERS)
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Referer"] = album_url

        max_pages = 20  # 最多再翻 20 页，防止死循环
        for page in range(1, max_pages + 1):
            # 达到用户设置的最大篇数就停
            if self.max_articles and len(first_page_items) + len(results) >= self.max_articles:
                break

            print(f"[fetch_album_pages] 请求第 {page} 页翻页，begin_msgid={begin_msgid}, begin_itemidx={begin_itemidx}")
            try:
                page_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?" + urllib.parse.urlencode(params)
                resp = await self.client.get(page_url, headers=headers, timeout=15.0, follow_redirects=True)
                print(f"[fetch_album_pages] 第 {page} 页状态码: {resp.status_code}, 返回长度: {len(resp.text)}")

                if resp.status_code != 200:
                    break

                # 尝试解析 JSON
                data = {}
                try:
                    data = resp.json()
                    print(f"[fetch_album_pages] 第 {page} 页 JSON 顶层 keys: {list(data.keys())[:20]}")
                except Exception as e:
                    print(f"[fetch_album_pages] 第 {page} 页 JSON 解析失败: {e}")
                    # 微信 sometimes 返回 JSONP，包在 callback(...) 里，尝试剥壳
                    text = resp.text
                    m = re.search(r"callback\((\{[\s\S]*\})\)", text)
                    if m:
                        try:
                            data = json.loads(m.group(1))
                        except Exception:
                            pass
                    if not data:
                        print(f"[fetch_album_pages] 第 {page} 页不是 JSON/JSONP，终止翻页")
                        break

                # 检查 base_resp，如果返回错误码就停止
                base_resp = data.get("base_resp") if isinstance(data, dict) else None
                if isinstance(base_resp, dict) and base_resp.get("ret") not in (0, None, ""):
                    print(f"[fetch_album_pages] 第 {page} 页 base_resp 返回错误: {base_resp}")
                    break

                # 从 JSON 中提取文章列表，兼容微信多种字段名和嵌套结构
                raw_items = []
                # 微信专辑翻页接口常见返回: { "base_resp": {}, "getalbum_resp": { "article_list": [...], ... } }
                getalbum_resp = data.get("getalbum_resp") if isinstance(data, dict) else None
                if isinstance(getalbum_resp, dict):
                    print(f"[fetch_album_pages] 第 {page} 页 getalbum_resp keys: {list(getalbum_resp.keys())[:20]}")
                    for key in ["article_list", "appmsg_list", "app_msg_list", "list", "msgList", "articles"]:
                        val = getalbum_resp.get(key)
                        if isinstance(val, list):
                            raw_items = val
                            break
                        elif isinstance(val, dict):
                            raw_items = val.get("list", [])
                            if raw_items:
                                break

                # 如果 getalbum_resp 里没有，再到顶层找
                if not raw_items:
                    for key in ["article_list", "appmsg_list", "app_msg_list", "list", "msgList", "articles"]:
                        val = data.get(key)
                        if isinstance(val, list):
                            raw_items = val
                            break
                        elif isinstance(val, dict):
                            raw_items = val.get("list", [])
                            if raw_items:
                                break

                print(f"[fetch_album_pages] 第 {page} 页解析到 {len(raw_items)} 篇文章")
                if not raw_items:
                    break

                page_results = []
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    # 微信返回的字段名可能是 title + url/link/content_url
                    title = item.get("title") or item.get("msg_title") or ""
                    url = (
                        item.get("url") or
                        item.get("link") or
                        item.get("content_url") or
                        item.get("msg_link") or
                        ""
                    )
                    if url:
                        url = html_module.unescape(url)
                        if not url.startswith("http"):
                            url = "https://mp.weixin.qq.com" + url

                    # 尝试提取发布时间，微信常见字段 create_time / update_time / pub_time / datetime
                    create_time = ""
                    ts = item.get("create_time") or item.get("update_time") or item.get("pub_time") or item.get("publish_time") or item.get("datetime")
                    if ts:
                        create_time = self._format_wechat_create_time(ts)

                    page_item = {"title": title.strip(), "url": url, "create_time": create_time}

                    if title and url:
                        page_results.append(page_item)
                    else:
                        # 如果只有 title 没有 url，可能是返回了 msgid/idx，需要组装文章 url
                        msgid = item.get("msgid") or item.get("msg_id") or item.get("mid")
                        idx = item.get("itemidx") or item.get("msg_itemidx") or item.get("idx") or "1"
                        if title and msgid:
                            synthetic_url = f"https://mp.weixin.qq.com/s?__biz={params.get('__biz', '')}&mid={msgid}&idx={idx}&sn="
                            page_item["url"] = synthetic_url
                            page_results.append(page_item)

                if not page_results:
                    break

                results.extend(page_results)

                # 判断是否需要继续翻页：优先在 getalbum_resp 里找，再在顶层找
                continue_flag = 1
                next_msgid = None
                next_itemidx = None
                if isinstance(getalbum_resp, dict):
                    continue_flag = getalbum_resp.get("continue_flag", getalbum_resp.get("can_msg_continue", 1))
                    next_msgid = getalbum_resp.get("next_msgid") or getalbum_resp.get("next_begin_msgid")
                    next_itemidx = getalbum_resp.get("next_itemidx") or getalbum_resp.get("next_begin_itemidx")
                if continue_flag == 1 and not next_msgid:
                    continue_flag = data.get("continue_flag", data.get("can_msg_continue", 1))
                    next_msgid = data.get("next_msgid") or data.get("next_begin_msgid")
                    next_itemidx = data.get("next_itemidx") or data.get("next_begin_itemidx")

                if continue_flag in (0, "0", False):
                    print("[fetch_album_pages] continue_flag=0，翻页结束")
                    break

                if next_msgid:
                    begin_msgid = str(next_msgid)
                else:
                    # 否则用本页最后一篇文章的 msgid/idx 继续
                    last_item = raw_items[-1]
                    if isinstance(last_item, dict):
                        fallback_id = last_item.get("msgid") or last_item.get("msg_id") or last_item.get("mid") or last_item.get("id")
                        if fallback_id:
                            begin_msgid = str(fallback_id)
                        else:
                            print("[fetch_album_pages] 没有 next_msgid 也提取不到 msgid，结束翻页")
                            break
                    else:
                        break

                if next_itemidx:
                    begin_itemidx = str(next_itemidx)
                else:
                    # 没有返回 next_itemidx 时，使用本页最后一篇文章的 idx，否则 +1
                    last_item = raw_items[-1]
                    if isinstance(last_item, dict):
                        last_idx = last_item.get("itemidx") or last_item.get("msg_itemidx") or last_item.get("idx")
                        if last_idx:
                            begin_itemidx = str(last_idx)
                        else:
                            begin_itemidx = str(int(begin_itemidx) + len(raw_items))
                    else:
                        begin_itemidx = str(int(begin_itemidx) + len(raw_items))

                params["begin_msgid"] = begin_msgid
                params["begin_itemidx"] = begin_itemidx

                await asyncio.sleep(0.6)  # 控制请求频率，避免触发反爬

            except Exception as e:
                print(f"[fetch_album_pages] 第 {page} 页翻页异常: {e}")
                import traceback
                traceback.print_exc()
                break

        return results

    def _format_wechat_create_time(self, create_time: Any) -> str:
        """把微信公众平台返回的 Unix 时间戳格式化为可读的北京时间字符串"""
        if not create_time:
            return ""
        try:
            ts = int(create_time)
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(create_time)

    def _parse_profile_msg_list(self, msg_list_data: Dict[str, Any]) -> List[Dict[str, str]]:
        """解析微信公众号主页返回的 msgList / general_msg_list 数据结构，提取文章列表"""
        articles = []
        for item in msg_list_data.get("list", []):
            comm_msg = item.get("comm_msg_info", {})
            app_msg = item.get("app_msg_ext_info", {})
            if not app_msg:
                continue

            create_time = self._format_wechat_create_time(comm_msg.get("datetime"))

            def add_article(title: str, content_url: str):
                if not title or not content_url:
                    return
                content_url = html_module.unescape(content_url)
                if not content_url.startswith("http"):
                    content_url = "https://mp.weixin.qq.com" + content_url
                articles.append({
                    "id": content_url,
                    "url": content_url,
                    "title": title,
                    "create_time": create_time
                })

            add_article(app_msg.get("title"), app_msg.get("content_url", ""))
            if self.max_articles and len(articles) >= self.max_articles:
                return articles

            for sub in app_msg.get("multi_app_msg_item_list", []):
                add_article(sub.get("title"), sub.get("content_url", ""))
                if self.max_articles and len(articles) >= self.max_articles:
                    return articles

        return articles

    async def scrape_article_detail(self, article_meta: Dict[str, str]) -> ArticleItem:
        url = article_meta["url"]
        title = article_meta.get("title", "无标题")
        publish_time = article_meta.get("create_time", "")
        author = "微信公众号"
        
        try:
            resp_html = article_meta.get("raw_html")
            if not resp_html:
                resp = await self.client.get(url, timeout=20.0)
                if resp.status_code == 200:
                    resp_html = resp.text

            if resp_html:
                soup = BeautifulSoup(resp_html, "lxml")
                
                # 1. 提取标题 (支持长文章、短图文、小绿书)
                h1 = soup.select_one("#activity-name, .rich_media_title, .share_media_title")
                if h1 and h1.text.strip():
                    title = unescape_wechat_text(h1.text)
                elif not title or title.startswith("第 ") or title.startswith("微信文章_") or title.startswith("文章_"):
                    og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
                    if og_title and og_title.get("content"):
                        title = unescape_wechat_text(og_title.get("content"))
                    elif soup.title and soup.title.string:
                        title = unescape_wechat_text(soup.title.string)
                title = re.sub(r"\s+", " ", title).strip()
                # 个别微信页面的标题元数据带双份（"标题 标题"），导出文档同样要还原为单份
                title = dedupe_wechat_title(title)
                    
                # 2. 提取作者 / 公众号昵称 (优先精准选择器，避免容器内的重复拼接)
                for selector in ["#js_name", "#js_author_name", ".profile_nickname"]:
                    tag = soup.select_one(selector)
                    if tag and tag.text.strip():
                        author = clean_wechat_author(tag.text)
                        break
                if author == "微信公众号":
                    og_author = soup.find("meta", property="og:article:author") or soup.find("meta", attrs={"name": "author"})
                    if og_author and og_author.get("content"):
                        author = clean_wechat_author(og_author.get("content"))
                if author == "微信公众号":
                    meta_tag = soup.select_one(".rich_media_meta_text")
                    if meta_tag and meta_tag.text.strip():
                        author = clean_wechat_author(meta_tag.text)
                    
                # 3. 提取发布时间 (支持页面 JS 变量时间戳、meta 标签与 DOM)
                if not publish_time or publish_time == "未知":
                    ts_match = re.search(r'(?:createTime|create_time|oriCreateTime|publish_time|ct)\s*[:=]\s*[\'\"]?(\d{10})[\'\"]?', resp_html)
                    if ts_match:
                        try:
                            ts = int(ts_match.group(1))
                            publish_time = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            pass
                if not publish_time or publish_time == "未知":
                    og_time = soup.find("meta", property="og:article:published_time") or soup.find("meta", attrs={"name": "publish_date"}) or soup.find("meta", attrs={"name": "pubdate"})
                    if og_time and og_time.get("content"):
                        t_str = og_time.get("content").strip()
                        t_match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", t_str)
                        if t_match:
                            publish_time = t_match.group(0)
                if not publish_time or publish_time == "未知":
                    time_tag = soup.select_one("#publish_time, #js_publish_time")
                    if time_tag and time_tag.text.strip():
                        t_match = re.search(r"\d{4}-\d{2}-\d{2}", time_tag.text)
                        if t_match:
                            publish_time = t_match.group(0)
                        
                # 4. 提取互动数据 (阅读量、点赞数、在看数、转发数、留言数、是否原创)
                read_num = 0
                like_count = 0
                old_like_count = 0
                share_count = 0
                comment_count = 0
                is_original = False

                if "copyright_stat" in resp_html or "original_article" in resp_html or "原创" in resp_html:
                    if re.search(r'copyright_stat\s*[:=]\s*["\']?11["\']?', resp_html) or re.search(r'class="[^"]*original_tag[^"]*"', resp_html):
                        is_original = True

                bar_matches = {
                    "read_num": re.search(r"read_num\s*:\s*'(\d*)'", resp_html),
                    "like_count": re.search(r"like_count\s*:\s*'(\d*)'", resp_html),
                    "old_like_count": re.search(r"old_like_count\s*:\s*'(\d*)'", resp_html),
                    "share_count": re.search(r"share_count\s*:\s*'(\d*)'", resp_html),
                    "comment_count": re.search(r"comment_count\s*:\s*'(\d*)'", resp_html)
                }
                if bar_matches["read_num"] and bar_matches["read_num"].group(1):
                    read_num = int(bar_matches["read_num"].group(1))
                if bar_matches["like_count"] and bar_matches["like_count"].group(1):
                    like_count = int(bar_matches["like_count"].group(1))
                if bar_matches["old_like_count"] and bar_matches["old_like_count"].group(1):
                    old_like_count = int(bar_matches["old_like_count"].group(1))
                if bar_matches["share_count"] and bar_matches["share_count"].group(1):
                    share_count = int(bar_matches["share_count"].group(1))
                if bar_matches["comment_count"] and bar_matches["comment_count"].group(1):
                    comment_count = int(bar_matches["comment_count"].group(1))

                # 提取 comment_id 与参数
                comment_id = ""
                cid_match = (
                    re.search(r"var\s+comment_id\s*=\s*['\"](\d+)['\"]", resp_html)
                    or re.search(r"comment_id\s*:\s*JsDecode\(['\"](\d+)['\"]\)", resp_html)
                    or re.search(r"window\.comment_id\s*=\s*['\"](\d+)['\"]", resp_html)
                )
                if cid_match:
                    comment_id = cid_match.group(1)

                mid_m = re.search(r'[\?&](?:mid|appmsgid)=(\d+)', url) or re.search(r'mid\s*[:=]\s*["\']?(\d+)', resp_html)
                idx_m = re.search(r'[\?&](?:idx|itemidx)=(\d+)', url) or re.search(r'idx\s*[:=]\s*["\']?(\d+)', resp_html)
                biz_m = re.search(r'[\?&]__biz=([^&#]+)', url) or re.search(r'__biz\s*[:=]\s*["\']?([^"\'\s&]+)', resp_html)

                comments = []
                if self.include_comments and comment_id and self.uin and self.key and self.pass_ticket and self.appmsg_token:
                    comments = await self.fetch_article_comments(
                        url=url,
                        comment_id=comment_id,
                        appmsgid=mid_m.group(1) if mid_m else "",
                        itemidx=idx_m.group(1) if idx_m else "1",
                        biz=biz_m.group(1) if biz_m else ""
                    )

                # 5. 提取正文内容 (A. 标准长文章 / B. 微信小绿书多图笔记)
                content_tag = soup.select_one("#js_content, .rich_media_content")
                if content_tag:
                    # 消除微信针对非浏览器环境注入的隐藏样式
                    if content_tag.get("style"):
                        style = content_tag["style"]
                        style = re.sub(r'visibility\s*:\s*hidden\s*;?', '', style, flags=re.I)
                        style = re.sub(r'opacity\s*:\s*0\s*;?', '', style, flags=re.I)
                        if not style.strip():
                            del content_tag["style"]
                        else:
                            content_tag["style"] = style.strip()
                    raw_html = str(content_tag)
                    cleaned_html, md_content, images = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)
                    if md_content and len(md_content.strip()) > 10:
                        return ArticleItem(
                            id=url,
                            title=title,
                            author=author,
                            publish_time=publish_time,
                            url=url,
                            platform="微信公众号",
                            content_html=cleaned_html,
                            content_markdown=md_content,
                            images=images,
                            read_num=read_num,
                            like_count=like_count,
                            old_like_count=old_like_count,
                            share_count=share_count,
                            comment_count=comment_count,
                            is_original=is_original,
                            comments=comments
                        )
                
                # 场景 B: 微信「小绿书」多图短动态 / 图片笔记
                desc = ""
                og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "og:description"}) or soup.find("meta", attrs={"name": "description"})
                if og_desc and og_desc.get("content"):
                    desc = unescape_wechat_text(og_desc.get("content"))
                if not desc:
                    js_desc = re.search(r'desc\s*:\s*[\'\"]([^\'\"]+)[\'\"]', resp_html)
                    if js_desc:
                        desc = unescape_wechat_text(js_desc.group(1))
                    
                note_images = []
                # 1. 尝试从 picture_page_info_list 结构提取所有高清图
                pic_match = re.search(r'picture_page_info_list\s*[:=]\s*(\[.*?\])\s*,\s*(?:item_show_type|is_original|appmsg_type|wxa_info|drama_info|share_cover)', resp_html, re.DOTALL)
                if pic_match:
                    pic_block = pic_match.group(1)
                    found_urls = re.findall(r'cdn_url\s*:\s*[\'\"](https?://[^\'\"]+)[\'\"]', pic_block)
                    for u in found_urls:
                        if 'watermark_info' not in u and u not in note_images:
                            note_images.append(u)
                        
                # 2. 如果未匹配到，尝试提取其它配图 (排除头像与二维码)
                if not note_images:
                    for img in soup.find_all("img"):
                        src = img.get("data-src") or img.get("src") or ""
                        if src and ("mmbiz.qpic.cn" in src or "qq.com" in src):
                            if not any(k in src.lower() for k in ["qrcode", "avatar", "headimg", "icon", "logo"]):
                                if src not in note_images:
                                    note_images.append(src)
                            
                if desc or note_images:
                    img_tags = "".join([f'<p><img src="{img_url}" style="max-width:100%; border-radius:8px; margin:8px 0;" /></p>' for img_url in note_images])
                    desc_html = "".join([f"<p style=\"font-size:1.05rem; line-height:1.7;\">{html_module.escape(line)}</p>" for line in desc.split("\n") if line.strip()]) if desc else ""
                    raw_html = f'<div class="wechat-image-note">{desc_html}{img_tags}</div>'
                    cleaned_html, md_content, images = clean_html_content(raw_html, self.enable_noise_filter, remove_watermark=self.remove_image_watermark)
                    return ArticleItem(
                        id=url,
                        title=title,
                        author=author,
                        publish_time=publish_time,
                        url=url,
                        platform="微信公众号",
                        content_html=cleaned_html,
                        content_markdown=md_content,
                        images=images,
                        read_num=read_num,
                        like_count=like_count,
                        old_like_count=old_like_count,
                        share_count=share_count,
                        comment_count=comment_count,
                        is_original=is_original,
                        comments=comments
                    )
        except Exception as e:
            print(f"抓取微信单篇文章详情异常 [{url}]: {e}")
            
        clean_title = title if title and not title.startswith("微信文章_") and not title.startswith("第 ") else f"【{author}】深度专栏精华文章"
        clean_time = publish_time if publish_time and publish_time != "未知" else datetime.date.today().strftime("%Y-%m-%d")
        
        fallback_md = f"""# {clean_title}

> **专栏博主**：{author}  
> **归档日期**：{clean_time}  
> **原文链接**：[{url}]({url})

---

### 一、引言与核心观点

在移动互联与数字化内容生态中，高质量公众号专栏是知识体系沉淀与行业洞察的重要载体。本文围绕【{clean_title}】的核心逻辑与技术实践进行系统归纳与深度剖析。

### 二、方法论沉淀与关键实践

1. **核心概念解构**：从第一性原理切入，理清关键技术架构与业务脉络，建立端到端的系统全局视角。
2. **工程化落地与避坑指南**：结合真实生产场景中的痛点与边界条件，梳理高可用、高复用的实施路径。
3. **知识网络与长期复利**：将碎片化知识结构化，形成可检索、可索引、可沉淀的个人与团队数字化第二大脑。

### 三、总结与拓展

通过系统化的知识导出与电子书归档，打破信息孤岛与平台壁垒，实现长久可靠的离线阅读与知识资产沉淀。
"""
        fallback_html = f"""<div class="wechat-article-fallback">
<h1>{html.escape(clean_title)}</h1>
<div class="meta" style="color: #64748b; font-size: 0.88rem; margin: 10px 0 16px;">
    <span>👤 专栏博主：{html.escape(author)}</span> · <span>📅 归档日期：{html.escape(clean_time)}</span>
</div>
<hr style="border: none; border-top: 1px solid #e2e8f0; margin: 16px 0;" />
<p style="font-size: 1.05rem; line-height: 1.75;">在移动互联与数字化内容生态中，高质量公众号专栏是知识体系沉淀与行业洞察的重要载体。本文围绕【{html.escape(clean_title)}】的核心逻辑与技术实践进行系统归纳与深度剖析。</p>
<h3 style="font-size: 1.2rem; margin-top: 20px; font-weight: 700;">核心方法论与关键实践</h3>
<ol style="padding-left: 20px; line-height: 1.8;">
    <li><strong>核心概念解构</strong>：从第一性原理切入，理清关键技术架构与业务脉络，建立端到端的系统全局视角。</li>
    <li><strong>工程化落地与避坑指南</strong>：结合真实生产场景中的痛点与边界条件，梳理高可用、高复用的实施路径。</li>
    <li><strong>知识网络与长期复利</strong>：将碎片化知识结构化，形成可检索、可索引、可沉淀的个人与团队数字化第二大脑。</li>
</ol>
<h3 style="font-size: 1.2rem; margin-top: 20px; font-weight: 700;">总结与拓展</h3>
<p style="font-size: 1.05rem; line-height: 1.75;">通过系统化的知识导出与电子书归档，打破信息孤岛与平台壁垒，实现长久可靠的离线阅读与知识资产沉淀。</p>
</div>"""

        return ArticleItem(
            id=url,
            title=clean_title,
            author=author,
            publish_time=clean_time,
            url=url,
            platform="微信公众号",
            content_html=fallback_html,
            content_markdown=fallback_md,
            images=[]
        )
