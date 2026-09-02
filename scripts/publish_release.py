import os
import sys
import json
import httpx
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "yibeigen/wechat-article-exporter"
TAG = "v1.0.0"
TITLE = "BlogDistiller (博萃) 微信文章导出助手 v1.0.0 官方正式版"
DIST_DIR = Path(r"E:\Tools\Web\下载各个平台\dist")

BODY = """# 🎉 BlogDistiller (博萃) 微信文章导出助手 v1.0.0 正式发布！

BlogDistiller 是一款专为高效沉淀与知识归档打造的微信公众号文章全量抓取、排版与离线导出工具。

### ✨ 核心功能特性：
1. **📚 6 大主流格式全量导出**：
   - **PDF 电子书**：内置 Chromium 矢量排版引擎，配图真实落盘内嵌，绝不裂图；
   - **Word 文档 (.docx)**：真图内嵌居中、出版级字阶排版、段落与代码块规范切分；
   - **HTML 离线网页**：独立单文件离线电子书，脱离网络畅读；
   - **Markdown (.md)**：知识库与 Obsidian 笔记专属；
   - **纯文本 (.txt)** / **Excel 数据表 (.xlsx)**：方便 AI 投喂与数据分析。
2. **⚡ 公众号专属主页直达**：
   - 一键生成专属主页直达链接，2 步极速截获会话密钥，免扫码安全导出。
3. **💾 本地公众号历史档案库**：
   - 多公众号数据本地持久化沉淀，0 秒无缝切换，支持本地无限次极速导出。
4. **🔒 0 风控保障**：
   - 提取后本地缓存，0 重复网络请求，保障账号绝对安全。

---

### 📥 下载与使用指引：
- **安装版用户**：下载 `BlogDistiller 微信文章导出助手 Setup 1.0.0.exe` 进行安装。
- **免安装便携版用户**：下载 `BlogDistiller 微信文章导出助手-1.0.0-win.zip`，解压后双击即可直接运行！
"""

def publish_github_release(token: str):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "BlogDistiller-Release-Bot"
    }
    
    print(f"🚀 正在连接 GitHub API 创建 Release: {TAG} ...")
    create_url = f"https://api.github.com/repos/{REPO}/releases"
    payload = {
        "tag_name": TAG,
        "name": TITLE,
        "body": BODY,
        "draft": False,
        "prerelease": False
    }
    
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(create_url, json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            print(f"❌ 创建 Release 失败: {resp.status_code} - {resp.text}")
            return
        
        release_data = resp.json()
        upload_url = release_data["upload_url"].split("{")[0]
        print(f"✅ Release 创建成功！ID: {release_data['id']}")
        
        # 上传二进制文件
        assets = [
            DIST_DIR / "BlogDistiller 微信文章导出助手 Setup 1.0.0.exe",
            DIST_DIR / "BlogDistiller 微信文章导出助手-1.0.0-win.zip"
        ]
        
        for asset in assets:
            if not asset.exists():
                print(f"⚠️ 文件不存在跳过: {asset}")
                continue
            
            print(f"📦 正在上传文件: {asset.name} ({asset.stat().st_size / (1024*1024):.1f} MB) ...")
            upload_headers = {
                "Authorization": f"token {token}",
                "Content-Type": "application/octet-stream",
                "User-Agent": "BlogDistiller-Release-Bot"
            }
            with open(asset, "rb") as f:
                upload_resp = client.post(
                    f"{upload_url}?name={asset.name}",
                    content=f.read(),
                    headers=upload_headers,
                    timeout=300.0
                )
            if upload_resp.status_code in (200, 201):
                print(f"✅ 上传成功: {asset.name}")
            else:
                print(f"❌ 上传失败: {asset.name} - {upload_resp.status_code} - {upload_resp.text}")
                
    print(f"\n🎉 官方正式版 v1.0.0 发布完毕！\n👉 查看链接: https://github.com/{REPO}/releases/tag/{TAG}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/publish_release.py <GITHUB_TOKEN>")
        sys.exit(1)
    publish_github_release(sys.argv[1])
