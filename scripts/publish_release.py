# BlogDistiller · 版本发布与制品打包脚本
import os
import sys
import json
import httpx
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "yibeigen/wechat-article-exporter"
TAG = "v1.2.0"
TITLE = "BlogDistiller (博萃) 文章导出助手 v1.2.0 官方正式版"
DIST_DIR = Path(r"E:\Tools\Web\下载各个平台\dist")

BODY = """# 🎉 BlogDistiller (博萃) 文章导出助手 v1.2.0 正式发布！

BlogDistiller 是一款多平台博主文章批量抓取、去广告清洗与多格式合并导出工具，
支持 **微信公众号 · 知乎 · 微博 · 新浪博客 · 简书 · CSDN · 掘金 · 博客园 · 51CTO** 等 9 大主流平台。

### 🆕 本版本更新：
1. **✒️ 品牌更名**：产品名由「微信文章导出助手」正式更名为「**文章导出助手**」，不再局限于单一平台；
2. **🪟 新增 Win7 专属版**：基于 Electron 22 + Python 3.8.10 稳定技术链构建，完美兼容 Windows 7 / 8.1 老电脑；
3. **🏠 本地优先架构**：全流程本地运算，抓取走您自己的家庭网络，彻底规避云端流量与风控瓶颈；
4. **📥 断点续跑**：任务中断后重启服务可一键续跑，已抓取篇目自动跳过，不重复下载。

---

### 📥 下载与使用指引（按您的系统版本二选一）：
- **Windows 10 / 11 用户**：下载 `BlogDistiller 文章导出助手 Setup 1.2.0.exe` 双击安装；
- **Windows 7 / 8.1 用户**：下载 Win7 专属版安装包 `BlogDistiller-Win7-Setup-x64.exe` 双击安装。

> 💡 安装包首次启动会自动部署本地 Python 运行内核，请耐心等待片刻。
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
        
        # 上传二进制文件（标准版 exe 在 dist 根目录，Win7 版 exe 在 dist/win7/ 子目录）
        assets = [
            DIST_DIR / "BlogDistiller 文章导出助手 Setup 1.2.0.exe",
            DIST_DIR / "win7" / "BlogDistiller 文章导出助手 Setup 1.2.0.exe",
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
                # 关键：中文+空格文件名必须通过 params 传参让 httpx 自动完成 URL 编码，
                # 直接拼在 URL 字符串里会因非法字符导致上传失败
                upload_resp = client.post(
                    upload_url,
                    params={"name": asset.name},
                    content=f.read(),
                    headers=upload_headers,
                    timeout=httpx.Timeout(600.0, write=600.0)
                )
            if upload_resp.status_code in (200, 201):
                print(f"✅ 上传成功: {asset.name}")
            else:
                print(f"❌ 上传失败: {asset.name} - {upload_resp.status_code} - {upload_resp.text}")

    print(f"\n🎉 官方正式版 v1.2.0 发布完毕！\n👉 查看链接: https://github.com/{REPO}/releases/tag/{TAG}")

if __name__ == "__main__":
    # Token 优先从环境变量 GITHUB_TOKEN 读取，避免在命令行参数中明文暴露；
    # 也兼容旧的 argv 传参方式：python scripts/publish_release.py <GITHUB_TOKEN>
    github_token = os.environ.get("GITHUB_TOKEN") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not github_token:
        print("用法：先设置环境变量 GITHUB_TOKEN，或 python scripts/publish_release.py <GITHUB_TOKEN>")
        sys.exit(1)
    publish_github_release(github_token)
