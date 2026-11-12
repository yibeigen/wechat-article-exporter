# BlogDistiller · 版本发布与制品打包脚本
# 用法：设置环境变量 GITHUB_TOKEN 后运行 `py -3 scripts/publish_release.py`
#       （也兼容旧方式：py -3 scripts/publish_release.py <GITHUB_TOKEN>）
# 特性：幂等可重跑——已上传的附件自动改名复用/跳过，缺失的才补传，网络中断自动重试
import hashlib
import json
import os
import subprocess
import sys
import time
import httpx
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "yibeigen/wechat-article-exporter"
TAG = "v1.4.0"
TITLE = "BlogDistiller (博萃) 文章导出助手 v1.4.0 官方正式版"
DIST_DIR = Path(r"E:\Tools\Web\下载各个平台\dist_1.4.0_final")
DIST_WIN7_DIR = Path(r"E:\Tools\Web\下载各个平台\dist\win7")
DIST_MAC_DIR = Path(r"E:\Tools\Web\下载各个平台\dist\mac")   # mac 包目录（GitHub Actions artifacts 解压后）
API_BASE = f"https://api.github.com/repos/{REPO}"

# 发布说明（全中文）。注意：GitHub 附件名只保留英文/数字/点/横线，
# 中文名会被自动改写，所以这里写的文件名必须与下方 ASSETS 的英文附件名一致
BODY = """# 🎉 BlogDistiller (博萃) 文章导出助手 v1.4.0 正式发布！

BlogDistiller 是一款多平台博主文章批量抓取、去广告清洗与多格式合并导出工具，
支持 **微信公众号 · 知乎 · 微博 · 新浪博客 · 简书 · 搜狐号 · 百家号 · CSDN · 掘金 · 博客园 · 51CTO** 等 11 大主流平台。

## 🆕 本版本更新（相对 v1.3.0）

- **平台矩阵扩到 11 家，新增【搜狐号】与【百家号】**：
  - 搜狐号直连官方翻页接口，全量遍历作者历史文章；百家号用无头浏览器穿透百度动态渲染；
  - 两者正文纯净直取、发布时间精确到秒，实测各 6/6 篇，md/pdf/docx/zip 四种产物全部落盘。
- **Win10/11 安装包 448 MB → 263 MB（少 185 MB，约 41%）**：
  - 剔除运行期永不加载的完整 Chromium（437 MB 未压缩）、40 个 PDB 调试符号、217 个多余语言包、
    tkinter/IDLE/Tcl 工具链、C 头文件与导入库，以及 8 个零引用 npm 依赖和 46 张仅文档引用的图片；
  - 全部抓取与 PDF 通道走 `headless=True`（用保留的 `chromium_headless_shell`），三条扫码登录走系统 Edge/Chrome；
  - 瘦身后已在随包内核上重跑端到端：百家号 4 篇/4 产物、新浪博客分组 12 篇/7 产物、搜狐号 30 篇/3 产物全部通过；
  - **Win7 兼容包的 Python 与浏览器内核完全不动**——其 Playwright 1.29 不分包，无头模式同样依赖完整 Chromium，不能删；
    只同步剔掉零引用 npm 依赖，242 MB → 232 MB。
- **修复 51CTO 检索恒为 0 篇**：系统代理变量里的 IPv6 回环地址（`NO_PROXY` 含 `[::1]`）让 HTTP 客户端
  在构造阶段就抛 `Invalid port: ':1]'`，请求根本没发出去；全项目 15 处客户端统一不再继承环境代理，
  实测恢复 6/6 篇 · 1.5 秒。
- **修复磁盘只读/权限不足时后端启动即崩**：缓存降级分支靠 ⚠️ 符号通报，Windows cp936 控制台编码不了它
  直接抛 `UnicodeEncodeError`，而该逻辑在 import 期执行，异常把整个后端启动带崩；现在管道输出统一 UTF-8，
  真控制台保留原编码只放开错误处理。
- **清单被截断时不再谎报「无遗漏」**：知乎登录墙、CSDN 风控截断以前会被显示成「全部分页深度穿透完成」，
  现在以后端截断标记为准，写明标称篇数、实得篇数、差额与中断原因，并给出处置建议。
- **搜狐视频简报页失败原因说清**：只有播放器没有正文的条目直接标明「搜狐视频简报，无法导出为文档」，
  不再笼统提示提取失败让人反复重试。
- **扫码登录兜底链补齐**：微博/知乎/微信读书的登录窗口按 Edge → Chrome 依次尝试，
  都不可用时提示「请安装 Microsoft Edge 后重试」，不再抛看不懂的英文路径错误。
- **修复重启后目标输入框被微信链接串台**：恢复历史配置时微信合集链接会无条件写回目标输入框，
  上次用搜狐号/知乎等平台的用户重启后框里会躺着一条微信链接；现在仅微信平台才同步，实测双向恢复均正确。
- **修复百家号在 Win7 兼容包上检索为空、正文抓取失败**：作者主页自身 XHR 发出的 feed 会被百度风控回空壳（list=null），
  现改为用浏览器逐页顶层导航官方 JSONP feed（作者页 cookie + 版本令牌 + 翻页游标）直取全量清单；
  正文同理——httpx 被百度按 TLS 指纹拦成空壳（搜索引擎/谷歌/桌面三种 UA 均拿不到 window.jsonData），
  改用常驻浏览器顶层导航文章页兜底（commit 后等 window.jsonData 就绪即取，close() 统一回收）。
  浏览器启动统一关闭 QUIC/HTTP2（Win7 内置 Chromium 109 在 QUIC/UDP 受限的网络下会挂起），另保留纯 HTTP feed 兜底通道。
  实测全量翻页、发布时间精确到秒，Win7 真机六用例（含百家号）全部产出真 PDF。
- **Win7 包下载落盘名统一成与附件一致的 ASCII 名**：新旧两版只差不到 1 万字节、
  浏览器另存后都显示 231.2M，名字必须能区分是哪一份。

## 📥 下载与使用指引（见下方 Assets 附件）

- **Windows 10 / 11 用户**：下载附件 `BlogDistiller-Setup-1.4.0.exe` 双击安装；
- **Windows 7 / 8.1 用户**：下载附件 `BlogDistiller-Win7-Setup-1.4.0-win7-latest.exe` 双击安装；
- **macOS 用户（M1/M2/M3/M4 芯片）**：下载附件 `BlogDistiller-macOS-1.4.0-arm64.dmg`，双击打开后把 App 拖入「应用程序」；
- **macOS 用户（Intel 芯片）**：下载附件 `BlogDistiller-macOS-1.4.0-x64.dmg`。
- **macOS 系统要求**：macOS 13 Ventura 及以上（安装包内核为 Electron 44，低于 13 的系统无法启动）。

> ⚠️ **macOS 首次打开指引**：安装包未购买 Apple 开发者证书签名，首次打开若提示「无法验证开发者」，请**右键 App 图标 → 打开 → 再点打开**（只需一次）；或在「系统设置 → 隐私与安全性」中点击「仍要打开」。

> 💡 **首次启动**：安装包已内置 Python 运行环境与浏览器内核，无需联网部署；启动页只是拉起本地服务引擎，通常几秒到十几秒进入工作台（正常退出后再开会更快）。已安装旧版的用户直接覆盖安装即可。

> 🔎 **已知限制**：博客园、51CTO、自定义多链接在「检索清单」阶段拿不到发布时间（发布时间在正文抓取阶段才补齐），
> 因此这三个来源的日期筛选会保留全部篇目并明确提示「N 篇无发布时间已保留」，不会静默丢弃。
"""

# 附件清单：(本地文件路径, GitHub 附件名)。附件名必须全英文，否则会被 GitHub 改写
ASSETS = [
    (DIST_DIR / "BlogDistiller 文章导出助手 Setup 1.4.0.exe", "BlogDistiller-Setup-1.4.0.exe"),
    (DIST_WIN7_DIR / "BlogDistiller 文章导出助手 Setup 1.4.0.exe", "BlogDistiller-Win7-Setup-1.4.0-win7-latest.exe"),
    # macOS 版（GitHub Actions 云端构建，本地下载 artifacts 解压到 dist/mac/ 后按英文名归位）
    (DIST_MAC_DIR / "BlogDistiller-macOS-arm64.dmg", "BlogDistiller-macOS-1.4.0-arm64.dmg"),
    (DIST_MAC_DIR / "BlogDistiller-macOS-x64.dmg", "BlogDistiller-macOS-1.4.0-x64.dmg"),
]


def verify_win7_artifact():
    """发布前用产物自身反证，不接受"说明里写了已修"。
    2026-10-30 事故：发布说明已写上 UCRT 修复与启动日志，但当时 dist/win7 里躺着的是
    修复提交（15:05）之前 14:20 构建的旧包，直接传上附件——Win7 用户装完照样打不开。
    """
    res = DIST_WIN7_DIR / "win-unpacked" / "resources"
    py = res / "python"
    if not py.exists():
        return ["win-unpacked/resources/python 不存在：Win7 包没构建或构建失败"]
    problems = []
    if not (py / "api-ms-win-crt-runtime-l1-1-0.dll").exists():
        problems.append("包内缺 app-local UCRT（resources/python 下无 api-ms-win-crt-*.dll），"
                        "纯净 Win7 上 Python 会无输出静默退出")
    asar = res / "app.asar"
    if asar.exists() and b"bootLog" not in asar.read_bytes():
        problems.append("app.asar 里没有 bootLog：主进程仍是修复前的旧版本，错误页会一片空白")
    checker = Path(__file__).resolve().parent / "check_win7_compat.py"
    r = subprocess.run([sys.executable, str(checker), str(DIST_WIN7_DIR / "win-unpacked")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        problems.append("Win7 依赖门禁不通过：\n" + (r.stdout or r.stderr)[-700:])
    problems += verify_win7_runtime()
    return problems


# Win7 真机验证记录：由 scratch/win7-selftest.bat 在装好系统的 Win7 上生成，
# 落盘 %USERPROFILE%\Desktop\BD_win7_verify.json 后拷到 dist/win7/win7_verified.json。
# 记录里绑的是整棵 resources 树的清单摘要（app.asar + 内置 Python + 浏览器内核全算进去），
# 换任何一层都算不匹配——「说明里写了已修」不再等于发布许可。
WIN7_VERIFY_FILE = DIST_WIN7_DIR / "win7_verified.json"
WIN7_RESOURCES = DIST_WIN7_DIR / "win-unpacked" / "resources"


# resources 树里这几个目录是运行期写出来的（会话缓存/导出产物/热更备份），
# 装了用过就变，算进摘要会让"同一个包"对不上；只摘要发布时不可变的部分。
MUTABLE_DIRS = ("__pycache__", "_hotfix_tmp", "_hotfix_backup", "data", "downloads", "logs")
# Chromium 渲染 PDF 时会在浏览器内核目录里落一个 debug.log（2026-11-04 Win7 真机实测 139B），
# 同样是运行期产物，不排除会让装过的机器和构建机算出的摘要对不上。
MUTABLE_FILES = (".pyc", "debug.log")


def resources_digest(root):
    """与 scratch/win7-selftest.bat 里 echo 出来的那段脚本同算法、同遍历序，
    构建机与 Win7 真机各算一次，对得上才证明验证的就是眼前这个包。"""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(x for x in dirnames if x not in MUTABLE_DIRS)
        for name in sorted(filenames):
            if name.endswith(MUTABLE_FILES):
                continue
            p = Path(dirpath) / name
            h.update(str(p.relative_to(root)).replace("\\", "/").encode() + b"|")
            with p.open("rb") as fp:
                for chunk in iter(lambda: fp.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()


def verify_win7_runtime():
    if not WIN7_RESOURCES.exists():
        return ["win-unpacked/resources 不存在：Win7 包没构建或构建失败"]
    if not WIN7_VERIFY_FILE.exists():
        return ["缺 Win7 真机验证记录 dist/win7/win7_verified.json："
                "静态门禁只能证明「没有已知的 Win7 拦路虎」，不能证明「点得开」。"
                "2026-10-30 就是在没跑过真机的情况下把包发出去的。"]
    try:
        rec = json.loads(WIN7_VERIFY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return ["win7_verified.json 解析失败: %r" % e]
    problems = []
    if not str(rec.get("os_build", "")).startswith("6.1"):
        problems.append("验证记录的系统版本不是 Windows 7（os_build=%s）" % rec.get("os_build"))
    if not rec.get("service_healthy"):
        problems.append("验证记录里本地服务没有跑通（service_healthy=%s）" % rec.get("service_healthy"))
    want = resources_digest(WIN7_RESOURCES)
    if rec.get("resources_sha256") != want:
        problems.append("验证记录对应的包与当前构建不是同一份（记录=%s 当前=%s）——换包即作废，重新验证"
                        % (str(rec.get("resources_sha256"))[:12], want[:12]))
    return problems


def publish_github_release(token: str):
    bad = verify_win7_artifact()
    if bad:
        print("❌ Win7 包自检不通过，终止发布（别把没修好的包当修好的发出去）：")
        for b in bad:
            print("  - " + b)
        sys.exit(1)
    print("✅ Win7 包自检通过：UCRT 随包 + 启动日志诊断 + 依赖门禁全绿")
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "BlogDistiller-Release-Bot"
    }

    # 代理支持：本机直连 GitHub 不稳定时，通过 GITHUB_PROXY 环境变量指定本地代理
    #（如 http://127.0.0.1:7897），不设置则直连。
    # trust_env=False：本机 NO_PROXY 含 IPv6 回环 [::1]，httpx 默认继承环境代理时
    # 会把它当 URL 解析抛 Invalid port: ':1]'（与 51CTO 检索恒 0 篇同一病根）；
    # 关掉环境继承，只认上面显式传入的 proxy，未设代理即直连。
    proxy = os.environ.get("GITHUB_PROXY") or os.environ.get("HTTPS_PROXY")
    with httpx.Client(timeout=httpx.Timeout(600.0, write=600.0), proxy=proxy, trust_env=False) as client:
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
                        # 中文/空格文件名必须经 params 让 httpx 自动 URL 编码，不能手拼 URL。
                        # content 直接传文件对象：httpx 按 64KB 分块流式上传并用 fstat 设 Content-Length，
                        # 避免 f.read() 把 500MB 级 mac dmg 整个读进内存（本机内存吃紧时会 OOM/重度换页）。
                        upload_resp = client.post(
                            upload_url,
                            params={"name": asset_name},
                            content=f,
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
