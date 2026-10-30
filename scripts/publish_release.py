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
TAG = "v1.3.0"
TITLE = "BlogDistiller (博萃) 文章导出助手 v1.3.0 官方正式版"
DIST_DIR = Path(r"E:\Tools\Web\下载各个平台\dist_1.3.0_final")
DIST_WIN7_DIR = Path(r"E:\Tools\Web\下载各个平台\dist\win7")
DIST_MAC_DIR = Path(r"E:\Tools\Web\下载各个平台\dist\mac")   # mac 包目录（GitHub Actions artifacts 解压后）
API_BASE = f"https://api.github.com/repos/{REPO}"

# 发布说明（全中文）。注意：GitHub 附件名只保留英文/数字/点/横线，
# 中文名会被自动改写，所以这里写的文件名必须与下方 ASSETS 的英文附件名一致
BODY = """# 🎉 BlogDistiller (博萃) 文章导出助手 v1.3.0 正式发布！

BlogDistiller 是一款多平台博主文章批量抓取、去广告清洗与多格式合并导出工具，
支持 **微信公众号 · 知乎 · 微博 · 新浪博客 · 简书 · CSDN · 掘金 · 博客园 · 51CTO** 等 9 大主流平台。

## 🆕 本版本更新（相对 v1.2.9）

- **顶栏「刷新」新增 GitHub 新版本检查**：
  - 有新版时弹窗列出「当前版本 → 最新版」与更新说明，一键前往官方发布页下载；
  - 没新版才刷新页面；结果缓存 10 分钟，6 秒超时，GitHub 连不上也不卡界面。
- **安装包自带 Python 运行环境与 Chromium 浏览器内核**：
  - 首次打开不再联网下载运行环境，没装过 Python、网络受限或被杀软拦下载的电脑也能直接用；
  - 不再走"检测到系统 Python 就拿来用"的老路（32 位解释器会无声崩溃），随包内核是验证过的 64 位版本。
- **修复"多点一次图标就把正在运行的软件打成空壳"**：检测到已有实例只唤起它的窗口，不再误杀后台服务。
- **启动提速（打包版实测二次启动 9.2 秒 → 6.6 秒）**：
  - 热更新清单拉取加 3 秒硬超时，服务器抽风不再让启动页无限干等；
  - 上次正常退出的话，下次启动跳过 2~6 秒的孤儿进程扫描；
  - 服务就绪探测间隔 2 秒 → 0.3 秒，不再白等。
- **启动页文案讲实话**：暖启动不再显示"正在检测本地 Python 运行环境"，只在真正体检时才出现。
- **Win7 安装包补齐系统运行库（2026-10-30 重打包）**：
  - 未装 KB2999226 的纯净 Win7 上，旧包内置的 Python 3.8 会因缺 UCRT 运行库静默退出，表现为「本地服务未能启动」；
  - 新包把 UCRT 与 VC 运行库随包内置，无需再装任何系统补丁；
  - 启动失败时错误页不再空白：直接显示退出码与真实原因，并提供「查看启动日志」按钮。
- **macOS 安装包重新构建（2026-10-30）**：
  - 修复 Intel 版包内误带 arm64 浏览器内核、导致 Intel Mac 打开后无法抓取的问题；
  - macOS 构建流水线新增 Mach-O 架构校验与包内 Chromium 自检，架构不符直接构建失败。

## 📥 下载与使用指引（见下方 Assets 附件）

- **Windows 10 / 11 用户**：下载附件 `BlogDistiller-Setup-1.3.0.exe` 双击安装；
- **Windows 7 / 8.1 用户**：下载附件 `BlogDistiller-Win7-Setup-1.3.0-x64.exe` 双击安装（Electron 22 + Python 3.8.20 + Chromium 109 兼容技术链）；
- **macOS 用户（M1/M2/M3/M4 芯片）**：下载附件 `BlogDistiller-macOS-1.3.0-arm64.dmg`，双击打开后把 App 拖入「应用程序」；
- **macOS 用户（Intel 芯片）**：下载附件 `BlogDistiller-macOS-1.3.0-x64.dmg`。

> ⚠️ **macOS 首次打开指引**：安装包未购买 Apple 开发者证书签名，首次打开若提示「无法验证开发者」，请**右键 App 图标 → 打开 → 再点打开**（只需一次）；或在「系统设置 → 隐私与安全性」中点击「仍要打开」。

> 💡 **首次启动**：安装包已内置 Python 运行环境与浏览器内核，无需联网部署；启动页只是拉起本地服务引擎，通常几秒到十几秒进入工作台（正常退出后再开会更快）。已安装旧版的用户直接覆盖安装即可。
"""

# 附件清单：(本地文件路径, GitHub 附件名)。附件名必须全英文，否则会被 GitHub 改写
ASSETS = [
    (DIST_DIR / "BlogDistiller 文章导出助手 Setup 1.3.0.exe", "BlogDistiller-Setup-1.3.0.exe"),
    (DIST_WIN7_DIR / "BlogDistiller 文章导出助手 Setup 1.3.0.exe", "BlogDistiller-Win7-Setup-1.3.0-x64.exe"),
    # macOS 版（GitHub Actions 云端构建，本地下载 artifacts 解压到 dist/mac/ 后按英文名归位）
    (DIST_MAC_DIR / "BlogDistiller-macOS-arm64.dmg", "BlogDistiller-macOS-1.3.0-arm64.dmg"),
    (DIST_MAC_DIR / "BlogDistiller-macOS-x64.dmg", "BlogDistiller-macOS-1.3.0-x64.dmg"),
]


def publish_github_release(token: str):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "BlogDistiller-Release-Bot"
    }

    # 代理支持：本机直连 GitHub 不稳定时，通过 GITHUB_PROXY 环境变量指定本地代理
    #（如 http://127.0.0.1:7897），不设置则直连
    proxy = os.environ.get("GITHUB_PROXY") or os.environ.get("HTTPS_PROXY")
    with httpx.Client(timeout=httpx.Timeout(600.0, write=600.0), proxy=proxy) as client:
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
