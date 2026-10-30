# BlogDistiller · Windows 内置 Python 内核构建脚本
#
# 用法：python scripts/build_win_kernel.py [--win7]
# 产物：desktop/python-win*/    独立 CPython + 预装全部后端依赖
#       desktop/browsers-win*/  Playwright Chromium 渲染内核
#       desktop/python-win*/kernel.lock.json  构建指纹（运行时自检用）
#
# 为什么要这个脚本：客户端原先在用户首次启动时才联网下载 Python、装 pip、
# 装依赖、装 Chromium，四段远程链路任一段失败就卡在"本地服务未能启动"。
# 这里把这些全部搬到构建期完成，随安装包一起交付，用户侧首启零网络。
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
WIN7 = "--win7" in sys.argv[1:]
KERNEL = DESKTOP / ("python-win7" if WIN7 else "python-win")
BROWSERS = DESKTOP / ("browsers-win7" if WIN7 else "browsers-win")
CACHE = DESKTOP / ".kernel_cache"
PY_EXE = KERNEL / "python.exe"
REQUIREMENTS = ROOT / ("requirements-win7.txt" if WIN7 else "requirements.txt")

PS_RELEASE = "20241002" if WIN7 else "20241016"
PY_VERSION = "3.8.20" if WIN7 else "3.12.7"
ASSET = f"cpython-{PY_VERSION}+{PS_RELEASE}-x86_64-pc-windows-msvc-install_only.tar.gz"
# 摘要固定写死：从同一镜像取 .sha256 无从发现镜像被投毒，只有内建期望值才是真校验
ASSET_SHA256 = ("41b6709fec9c56419b7de1940d1f87fa62045aff81734480672dcb807eedc47e" if WIN7
                else "f05531bff16fa77b53be0776587b97b466070e768e6d5920894de988bdcd547a")
DOWNLOAD_URLS = [
    f"https://registry.npmmirror.com/-/binary/python-build-standalone/{PS_RELEASE}/{urllib.parse.quote(ASSET)}",
    f"https://github.com/astral-sh/python-build-standalone/releases/download/{PS_RELEASE}/{ASSET}",
]

# 与 desktop/main.js 的 checkDeps() 探测清单保持一致，改一处必须改两处
IMPORT_PROBE = "import fastapi,uvicorn,httpx,bs4,markdownify,lxml,docx,jinja2,pydantic,PIL"
PIP_INDEXES = [
    ["-i", "https://mirrors.aliyun.com/pypi/simple/", "--trusted-host", "mirrors.aliyun.com"],
    ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "--trusted-host", "pypi.tuna.tsinghua.edu.cn"],
]


def log(msg):
    print(f"[kernel] {msg}", flush=True)


def download(url):
    dest = CACHE / ASSET
    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("content-length") or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if total:
                print(f"\r[kernel] 下载 {got * 100 // total}% ({got // 1048576}/{total // 1048576} MB)", end="", flush=True)
    print()
    return dest


def fetch_kernel():
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / ASSET
    if cached.exists() and hashlib.sha256(cached.read_bytes()).hexdigest() == ASSET_SHA256:
        log(f"复用缓存 {ASSET}")
        return cached
    for url in DOWNLOAD_URLS:
        try:
            log(f"下载 {url}")
            path = download(url)
        except Exception as e:
            log(f"该线路失败：{e}，换下一线路")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != ASSET_SHA256:
            path.unlink(missing_ok=True)
            raise SystemExit(f"[kernel] 摘要不符，拒绝使用该产物\n  期望 {ASSET_SHA256}\n  实际 {digest}")
        return path
    raise SystemExit("[kernel] 全部线路均不可达，请检查网络后重试")


def extract(tar_path):
    if KERNEL.exists():
        shutil.rmtree(KERNEL)
    staging = DESKTOP / ".kernel_extract"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    log("解压解释器")
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(staging)
    shutil.move(str(staging / "python"), str(KERNEL))
    staging.rmdir()
    if not PY_EXE.exists():
        raise SystemExit(f"[kernel] 解压后未找到 {PY_EXE}")


def run_python(args, env=None, timeout=1800):
    return subprocess.run([str(PY_EXE), *args], env=env, timeout=timeout, check=False)


def install_deps():
    req = REQUIREMENTS
    for index in PIP_INDEXES:
        log(f"安装后端依赖（{index[1]}）")
        code = run_python(["-m", "pip", "install", "--no-cache-dir", "-r", str(req),
                           "--disable-pip-version-check", *index]).returncode
        if code == 0:
            return
        log("该镜像安装失败，换下一个")
    raise SystemExit("[kernel] 依赖安装失败：阿里云与清华镜像均不可用")


def deploy_win7_runtime():
    """把 UCRT/VC 运行库放进内核目录（Win7 未装 KB2999226 时 Python 3.8 起不来）。

    源目录 desktop/win7-ucrt 受版本管理，重建内核后必须重新部署，
    否则包里的 python.exe 会在 Win7 上因缺少 api-ms-win-crt-* 直接退出。
    """
    if not WIN7:
        return
    src = DESKTOP / "win7-ucrt"
    dlls = sorted(src.glob("*.dll")) if src.is_dir() else []
    if not dlls:
        raise SystemExit("[kernel] 缺少 desktop/win7-ucrt 运行库，Win7 版打包后 Python 将无法启动")
    for f in dlls:
        shutil.copy2(f, KERNEL / f.name)
    log(f"已部署 {len(dlls)} 个 Win7 运行库（UCRT + msvcp140）到内核目录")


def install_browsers():
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS)}
    if BROWSERS.exists():
        shutil.rmtree(BROWSERS)
    BROWSERS.mkdir(parents=True)
    log("安装 Playwright Chromium（约 170MB）")
    if run_python(["-m", "playwright", "install", "chromium"], env=env, timeout=3600).returncode != 0:
        raise SystemExit("[kernel] Chromium 安装失败，知乎专栏等动态页面通道将不可用")


def verify():
    log("按客户端 checkDeps 清单做导入自检")
    if run_python(["-c", IMPORT_PROBE]).returncode != 0:
        raise SystemExit("[kernel] 内置依赖自检未通过，不要使用该内核发包")
    # 新版 headless 与完整 Chromium 分包；Win7 的 Playwright 1.29 只有完整 Chromium。
    patterns = ("chromium-*",) if WIN7 else ("chromium_headless_shell-*", "chromium-*")
    for pattern in patterns:
        if not any(BROWSERS.glob(pattern)):
            raise SystemExit(f"[kernel] {BROWSERS} 下缺少 {pattern}，部分渲染通道将不可用")


def write_lock(tar_path):
    req = REQUIREMENTS
    frozen = subprocess.run([str(PY_EXE), "-m", "pip", "freeze", "--disable-pip-version-check"],
                            capture_output=True, text=True, check=True).stdout
    (KERNEL / "kernel.lock.json").write_text(json.dumps({
        "python": PY_VERSION,
        "release": PS_RELEASE,
        "kernel_sha256": hashlib.sha256(tar_path.read_bytes()).hexdigest(),
        "requirements_sha256": hashlib.sha256(req.read_bytes()).hexdigest(),
        "packages": sorted(l for l in frozen.splitlines() if l.strip()),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    if sys.version_info < (3, 8):
        raise SystemExit("[kernel] 构建脚本需要 Python 3.8+")
    tar_path = fetch_kernel()
    extract(tar_path)
    install_deps()
    deploy_win7_runtime()
    install_browsers()
    verify()
    write_lock(tar_path)
    size = sum(f.stat().st_size for f in KERNEL.rglob("*") if f.is_file()) / 1048576
    bsize = sum(f.stat().st_size for f in BROWSERS.rglob("*") if f.is_file()) / 1048576
    log(f"完成：内核 {size:.0f} MB + Chromium {bsize:.0f} MB，可执行 npx electron-builder --win")


if __name__ == "__main__":
    main()
