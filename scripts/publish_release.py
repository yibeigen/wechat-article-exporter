# BlogDistiller · 版本发布与制品打包脚本
# 用法：设置环境变量 GITHUB_TOKEN 后运行 `py -3 scripts/publish_release.py`
#       （也兼容旧方式：py -3 scripts/publish_release.py <GITHUB_TOKEN>）
# 特性：幂等可重跑——已上传的附件自动改名复用/跳过，缺失的才补传，网络中断自动重试
import os
import sys
import time
import httpx
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "yibeigen/wechat-article-exporter"
TAG = "v1.2.8"
TITLE = "BlogDistiller (博萃) 文章导出助手 v1.2.8 官方正式版"
DIST_DIR = Path(r"E:\Tools\Web\下载各个平台\dist_1.2.8_final")
API_BASE = f"https://api.github.com/repos/{REPO}"

# 发布说明（全中文）。注意：GitHub 附件名只保留英文/数字/点/横线，
# 中文名会被自动改写，所以这里写的文件名必须与下方 ASSETS 的英文附件名一致
BODY = """# 🎉 BlogDistiller (博萃) 文章导出助手 v1.2.8 正式发布！

BlogDistiller 是一款多平台博主文章批量抓取、去广告清洗与多格式合并导出工具，
支持 **微信公众号 · 知乎 · 微博 · 新浪博客 · 简书 · CSDN · 掘金 · 博客园 · 51CTO** 等 9 大主流平台。

### 🆕 本版本更新（相对 v1.2.7 的变化）：
1. **🐛 修复微博「官方扫码登录」窗口一闪而过**：微博对未登录访客也会下发名为 SUB 的访客凭证，旧逻辑一看到 SUB 就误判登录成功并在 2 秒内关窗，二维码根本来不及扫。现在须经微博官方接口验证 login=true 才算登录成功；
2. **🐛 修复微博扫码成功后界面仍显示未登录**：weibo.com 桌面端登录凭证与 m.weibo.cn 抓取通道域隔离、互不认可。现在登录成功后会自动经 m.weibo.cn 换取移动端凭证并合并保存，扫码完成即真正可用；
3. **🐛 修复重复点击「官方扫码登录」把已登录账号踢下线**：不再清空登录会话，按钮改为幂等——已是登录态时直接静默同步凭证并显示账号信息，不弹窗、不登出；
4. **✨ 知乎扫码登录后端验证制**：分区残留的旧凭证先验证有效性，过期才要求重新扫码；已登录时同样一键静默同步，界面直接显示真实账号昵称。

> 💡 Windows 7 / 8.1 兼容版本次未重新构建，仍为上一条 release 的 v1.2.7 版本，如需请前往历史 release 下载。

---

### 📥 下载与使用指引（见下方 Assets 附件）：
- **Windows 10 / 11 用户**：下载附件 `BlogDistiller-Setup-1.2.8.exe` 双击安装。

> 💡 首次启动会显示初始化进度页（下载/部署本地内核，约 3~10 分钟，视网速而定），请保持网络畅通耐心等待；完成后自动进入工作台，以后启动无需再等。
"""

# 附件清单：(本地文件路径, GitHub 附件名)。附件名必须全英文，否则会被 GitHub 改写
ASSETS = [
    (DIST_DIR / "BlogDistiller 文章导出助手 Setup 1.2.8.exe", "BlogDistiller-Setup-1.2.8.exe"),
]


def publish_github_release(token: str):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "BlogDistiller-Release-Bot"
    }

    with httpx.Client(timeout=httpx.Timeout(600.0, write=600.0)) as client:
        # 第一步：幂等获取 Release——已存在就复用，不存在才创建（脚本可安全重跑）
        print(f"🚀 正在查询 Release: {TAG} ...")
        resp = client.get(f"{API_BASE}/releases/tags/{TAG}", headers=headers)
        if resp.status_code == 200:
            release_data = resp.json()
            print(f"ℹ️ Release 已存在，直接复用 (ID: {release_data['id']})")
        else:
            print(f"🚀 正在创建 Release: {TAG} ...")
            resp = client.post(f"{API_BASE}/releases", headers=headers, json={
                "tag_name": TAG, "name": TITLE, "body": BODY,
                "draft": False, "prerelease": False
            })
            if resp.status_code not in (200, 201):
                print(f"❌ 创建 Release 失败: {resp.status_code} - {resp.text}")
                return
            release_data = resp.json()
        release_id = release_data["id"]
        upload_url = release_data["upload_url"].split("{")[0]

        # 第二步：把发布说明同步为最新内容（本地 BODY 为准，保证文件名与附件对得上）
        resp = client.patch(f"{API_BASE}/releases/{release_id}", headers=headers,
                            json={"name": TITLE, "body": BODY})
        print("✅ 发布说明已更新" if resp.status_code == 200 else f"⚠️ 发布说明更新失败: {resp.status_code}")

        # 第三步：拉取当前已有附件列表，用于幂等判断
        resp = client.get(f"{API_BASE}/releases/{release_id}/assets", headers=headers)
        existing = resp.json() if resp.status_code == 200 else []
        print(f"📋 当前已有附件: {[a['name'] for a in existing] or '无'}")

        for path, asset_name in ASSETS:
            if not path.exists():
                print(f"⚠️ 本地文件不存在跳过: {path}")
                continue
            size = path.stat().st_size

            # 情况 A：目标附件名已存在且大小一致 → 无需处理
            hit = next((a for a in existing if a["name"] == asset_name), None)
            if hit and hit["size"] == size:
                print(f"✅ 附件已存在，跳过: {asset_name}")
                continue

            # 情况 B：同名但大小不一致（历史残留）→ 删除后重传
            if hit:
                print(f"🗑️ 同名附件大小不符，删除重传: {asset_name}")
                client.delete(f"{API_BASE}/releases/assets/{hit['id']}", headers=headers)

            # 情况 C：之前传过但名字被 GitHub 改写了（大小一致视为同一文件）→ 直接改名复用，省去重传
            same_size = next((a for a in existing if a["size"] == size and a["name"] != asset_name), None)
            if same_size:
                resp = client.patch(f"{API_BASE}/releases/assets/{same_size['id']}", headers=headers,
                                    json={"name": asset_name})
                if resp.status_code == 200:
                    print(f"✏️ 已有附件改名复用: {same_size['name']} → {asset_name}")
                    continue
                print(f"⚠️ 改名失败({resp.status_code})，改为重新上传")

            # 情况 D：缺失 → 上传，网络中断自动重试 3 次（退避 5/10/20 秒）
            print(f"📦 正在上传: {asset_name} ({size / (1024*1024):.1f} MB) ...")
            upload_headers = {
                "Authorization": f"token {token}",
                "Content-Type": "application/octet-stream",
                "User-Agent": "BlogDistiller-Release-Bot"
            }
            for attempt in range(1, 4):
                try:
                    with open(path, "rb") as f:
                        # 中文/空格文件名必须经 params 让 httpx 自动 URL 编码，不能手拼 URL
                        upload_resp = client.post(
                            upload_url,
                            params={"name": asset_name},
                            content=f.read(),
                            headers=upload_headers,
                        )
                    if upload_resp.status_code in (200, 201):
                        print(f"✅ 上传成功: {asset_name}")
                        break
                    print(f"❌ 上传失败: {asset_name} - {upload_resp.status_code} - {upload_resp.text}")
                except Exception as e:
                    print(f"⚠️ 第 {attempt} 次上传网络异常: {type(e).__name__}: {e}")
                if attempt < 3:
                    wait = 5 * attempt * 2
                    print(f"⏳ {wait} 秒后重试...")
                    time.sleep(wait)
            else:
                print(f"❌ 连续 3 次上传失败，放弃: {asset_name}")

    print(f"\n🎉 官方正式版 {TAG} 发布完毕！\n👉 查看链接: https://github.com/{REPO}/releases/tag/{TAG}")


if __name__ == "__main__":
    # Token 优先从环境变量 GITHUB_TOKEN 读取，避免在命令行参数中明文暴露
    github_token = os.environ.get("GITHUB_TOKEN") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not github_token:
        print("用法：先设置环境变量 GITHUB_TOKEN，或 python scripts/publish_release.py <GITHUB_TOKEN>")
        sys.exit(1)
    publish_github_release(github_token)
