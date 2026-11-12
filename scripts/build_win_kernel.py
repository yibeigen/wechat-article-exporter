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


def prune_win7_arm_binaries():
    """剔除 pip/setuptools 自带的 ARM64 启动器 exe。

    这些文件对 x64 客户端无用，却是「安装后随包缺失」的 4 个文件：
    Win7 安装后 resources 树与构建树相比恰好少这 4 个，导致真机哈希门禁对不上。
    直接从内核源树剔除，保证「构建树 = 安装树」，也减掉约 370KB。
    """
    if not WIN7:
        return
    removed = []
    for pattern in ("Lib/site-packages/pip/_vendor/distlib/*arm*.exe",
                    "Lib/site-packages/setuptools/*arm64*.exe"):
        for f in KERNEL.glob(pattern):
            f.unlink()
            removed.append(f.name)
    if removed:
        log(f"已剔除 ARM64 启动器 {len(removed)} 个: {', '.join(sorted(removed))}")


def install_browsers():
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS)}
    if BROWSERS.exists():
        shutil.rmtree(BROWSERS)
    BROWSERS.mkdir(parents=True)
    # Win10/11 只装 headless shell：抓取与 PDF 全部走 headless=True，三条扫码登录走系统
    # Edge/Chrome，完整 Chromium（437MB）没有任何调用方。Win7 的 Playwright 1.29 不分包，
    # headless 也依赖完整 Chromium，必须整份保留。
    args = ["-m", "playwright", "install", "chromium"] + ([] if WIN7 else ["--only-shell"])
    log("安装 Playwright Chromium（约 170MB）" if WIN7 else "安装 Playwright headless shell（约 280MB）")
    if run_python(args, env=env, timeout=3600).returncode != 0:
        raise SystemExit("[kernel] Chromium 安装失败，知乎专栏等动态页面通道将不可用")


def prune():
    """剔除 Win10/11 内核与浏览器里运行期永不加载的部分。

    PDB 是调试符号；include/libs 只在编译 C 扩展时用，而依赖全是预编译 wheel；
    tkinter/idlelib/tcl 是 GUI 工具链，后端零引用；语言包只留中英文，
    Chromium 找不到对应语言包时回落 en-US。Win7 内核一律不动，避免碰真机哈希门禁。
    """
    if WIN7:
        return
    freed = 0

    def drop_file(f):
        nonlocal freed
        freed += f.stat().st_size
        f.unlink()

    for f in KERNEL.rglob("*.pdb"):
        drop_file(f)
    for rel in ("include", "libs", "tcl", "Lib/tkinter", "Lib/idlelib", "Lib/turtledemo"):
        d = KERNEL / rel
        if d.is_dir():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)
    for name in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
        f = KERNEL / "DLLs" / name
        if f.exists():
            drop_file(f)
    keep_locales = {"en-US.pak", "zh-CN.pak", "zh-TW.pak"}
    for loc in BROWSERS.glob("chromium*/**/locales"):
        for pak in loc.iterdir():
            if pak.name not in keep_locales:
                drop_file(pak)
    # 旧构建残留的完整 Chromium 也一并清掉，保证 prune 单独重跑就能收敛到目标形态
    for d in BROWSERS.glob("chromium-*"):
        if d.is_dir():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)
    log(f"已剔除完整 Chromium/调试符号/GUI 工具链/多余语言包，省下 {freed / 1048576:.0f} MB")


def verify():
    log("按客户端 checkDeps 清单做导入自检")
    if run_python(["-c", IMPORT_PROBE]).returncode != 0:
        raise SystemExit("[kernel] 内置依赖自检未通过，不要使用该内核发包")
    # Win10/11 只交付 headless shell；Win7 的 Playwright 1.29 只有完整 Chromium。
    patterns = ("chromium-*",) if WIN7 else ("chromium_headless_shell-*",)
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


def patch_win7_pyd_loader():
    """CPython 3.8 的 dynload 用 LOAD_LIBRARY_SEARCH_DEFAULT_DIRS|DLL_LOAD_DIR 加载 .pyd。
    Win7 未装 KB2533623 时这两个标志位不被识别，LoadLibraryExW 直接返回 87（「参数错误」），
    所有扩展模块都导不进来，后端服务起不来。改成 3.7 时代的
    LOAD_WITH_ALTERED_SEARCH_PATH（0x8）：Win7 认识，且仍然优先搜索 pyd 自身目录。"""
    dll = KERNEL / "python38.dll"
    data = bytearray(dll.read_bytes())
    old = b"\x41\xb8\x00\x11\x00\x00"  # mov r8d, 0x1100
    hits = []
    start = 0
    while True:
        i = data.find(old, start)
        if i < 0:
            break
        hits.append(i)
        start = i + 1
    if not hits:
        if data.count(b"\x33\xd2\x41\xb8\x08\x00\x00\x00") == 1:
            log("python38.dll 加载标志位已是 Win7 兼容值，跳过")
            return
        raise SystemExit("[kernel] python38.dll 里找不到扩展模块加载标志位，拒绝继续")
    if len(hits) != 1 or data[hits[0] - 2:hits[0]] != b"\x33\xd2":
        raise SystemExit(f"[kernel] python38.dll 加载标志位定位不唯一/上下文不符：{hits}")
    data[hits[0] + 2:hits[0] + 6] = b"\x08\x00\x00\x00"
    dll.write_bytes(bytes(data))
    log("已把 python38.dll 扩展模块加载改为 Win7 兼容的 ALTERED_SEARCH")


def _pe_imports(path):
    """只读 PE 导入表里的 DLL 名（纯标准库，避免构建脚本依赖 pefile）。"""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    pe_off = int.from_bytes(data[0x3c:0x40], "little")
    if data[pe_off:pe_off + 4] != b"PE\x00\x00":
        return []
    coff = pe_off + 4
    n_sec = int.from_bytes(data[coff + 2:coff + 4], "little")
    opt_size = int.from_bytes(data[coff + 16:coff + 18], "little")
    opt = coff + 20
    dd = opt + (112 if int.from_bytes(data[opt:opt + 2], "little") == 0x20b else 96)
    imp_rva = int.from_bytes(data[dd + 8:dd + 12], "little")
    if not imp_rva:
        return []
    sections = []
    for i in range(n_sec):
        s = opt + opt_size + i * 40
        sections.append((int.from_bytes(data[s + 12:s + 16], "little"),
                         max(int.from_bytes(data[s + 8:s + 12], "little"),
                             int.from_bytes(data[s + 16:s + 20], "little")),
                         int.from_bytes(data[s + 20:s + 24], "little")))

    def rva2off(rva):
        for vaddr, size, raw in sections:
            if vaddr <= rva < vaddr + size:
                return raw + (rva - vaddr)
        return None

    off = rva2off(imp_rva)
    names = []
    while off is not None:
        desc = data[off:off + 20]
        if len(desc) < 20 or desc == b"\x00" * 20:
            break
        name_off = rva2off(int.from_bytes(desc[12:16], "little"))
        if name_off is not None:
            names.append(data[name_off:data.find(b"\x00", name_off)].decode("ascii", "ignore").lower())
        off += 20
    return names


def _is_runtime_lib(name):
    return (name.startswith("api-ms-win-") or name in ("ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll")
            or name.startswith("msvcp140"))


def place_applocal_deps():
    """Win7 侧 .pyd 只按「自身目录优先」搜索依赖，内核根目录不再参与搜索。
    根目录里的 api-ms-win-crt-*、msvcp140、vcruntime140_1 等必须复制到用到它们的扩展模块旁边，
    否则 import 报「找不到指定的模块」。解释器本体 python38.dll 已随 python.exe 常驻，不复制。"""
    root_dlls = {p.name.lower(): p for p in KERNEL.glob("*.dll") if _is_runtime_lib(p.name.lower())}
    targets = [KERNEL / "DLLs"]
    targets += [p.parent for p in (KERNEL / "Lib" / "site-packages").rglob("*.pyd")]
    copied = 0
    for d in dict.fromkeys(targets):
        for _ in range(3):
            present = {p.name.lower() for p in d.iterdir()}
            missing = set()
            for f in d.iterdir():
                if f.suffix.lower() in (".pyd", ".dll"):
                    missing.update(n for n in _pe_imports(f) if n in root_dlls and n not in present)
            if not missing:
                break
            for name in missing:
                shutil.copy2(root_dlls[name], d / name)
                copied += 1
    log(f"已把 {copied} 个根目录运行库复制到扩展模块目录（Win7 依赖搜索）")


def patch_ctypes_loader():
    """ctypes.CDLL 在 Windows 上默认给 LoadLibraryExW 传 LOAD_LIBRARY_SEARCH_* 标志，
    Win7 未装 KB2533623 时同样直接报 87（参数错误）——ctypes.windll.kernel32 都会失败。
    Win7 版把默认标志改回 0（Python 3.7 语义），显式传 winmode 的调用方不受影响。"""
    f = KERNEL / "Lib" / "ctypes" / "__init__.py"
    src = f.read_text(encoding="utf-8")
    old1 = "mode = nt._LOAD_LIBRARY_SEARCH_DEFAULT_DIRS"
    old2 = "mode |= nt._LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR"
    if src.count(old1) != 1 or src.count(old2) != 1:
        raise SystemExit("[kernel] ctypes/__init__.py 结构与预期不符，拒绝改标准库")
    src = src.replace(old1, "mode = 0  # Win7 无 LOAD_LIBRARY_SEARCH_*（KB2533623），退回旧搜索语义")
    src = src.replace(old2, "mode |= 0")
    f.write_text(src, encoding="utf-8")
    log("已把 ctypes 默认 DLL 加载标志改为 Win7 兼容（0）")


def main():
    if sys.version_info < (3, 8):
        raise SystemExit("[kernel] 构建脚本需要 Python 3.8+")
    tar_path = fetch_kernel()
    extract(tar_path)
    install_deps()
    deploy_win7_runtime()
    if WIN7:
        patch_win7_pyd_loader()
        patch_ctypes_loader()
        place_applocal_deps()
        prune_win7_arm_binaries()
    install_browsers()
    prune()
    verify()
    write_lock(tar_path)
    size = sum(f.stat().st_size for f in KERNEL.rglob("*") if f.is_file()) / 1048576
    bsize = sum(f.stat().st_size for f in BROWSERS.rglob("*") if f.is_file()) / 1048576
    log(f"完成：内核 {size:.0f} MB + Chromium {bsize:.0f} MB，可执行 npx electron-builder --win")


if __name__ == "__main__":
    main()
