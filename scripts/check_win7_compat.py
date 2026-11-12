"""Win7 兼容性静态门禁：扫一遍目标目录里所有 PE 二进制（exe/dll/pyd）的导入表，
找出 Windows 7 加载器满足不了的依赖。

为什么需要它：Win7 上「本地服务未能启动」且错误页一片空白，根因是随包 Python 内核
里有二进制静态依赖了 Win7 不存在的运行库/API。这类问题在 Win11 开发机上 100% 跑不出来，
只有真 Win7 才会暴露——而 Win7 真机不可能每台都备着。静态扫导入表能在构建期 3 秒内拦下。

四类致命信号（任一命中即 exit 1）：
  1) 静态导入 Win8+/Win10+ 才有的 kernel32/user32 等导出函数 → STATUS_ENTRYPOINT_NOT_FOUND
  2) 依赖 api-ms-win-* / ext-ms-win-* 却没在包内 app-local 附带同名 dll
     → Win7 无 API Set 解析层，找不到文件即 STATUS_DLL_NOT_FOUND（UCRT 缺失就是这一类）
  3) 垫片把符号"导出转发"给某个 dll，而该 dll 既不在包内也不是 Win7 自带
     → 转发目标不在导入表里，只扫导入表会漏检（ucrtbase.dll 缺失就是这一类）
  4) 解析不出 PE → 直接报错，不允许静默跳过（静默跳过曾让这份门禁给出过假的"全绿"）

用法：
  py -3 scripts/check_win7_compat.py desktop/python-win7
  py -3 scripts/check_win7_compat.py "dist/win7/win-unpacked/resources/python"
"""
import struct
import sys
from pathlib import Path

# Win7(6.1) 不存在、Win8+ 才导出的函数。
# 只放确认过的：FlsSetValue=Vista、GetAdaptersAddresses=XP、SetProcessInformation=Win7、
# NtSetInformationProcess=XP 这些都是 Win7 就有的，误加会让门禁满屏假阳性。
WIN8_PLUS = {
    "kernel32.dll": {
        "WaitOnAddress", "WakeByAddressSingle", "WakeByAddressAll",
        "GetSystemTimePreciseAsFileTime", "GetOverlappedResultEx",
        "CreateFile2", "SetProcessValidCallTargets", "QueryPoolInformation",
        "VerifyOnSchema", "GetFirmwareEnvironmentVariableEx",
    },
    "ntdll.dll": {
        "RtlGuardCheckLongJumpTarget",
        "__guard_check_icall_fptr", "__guard_dispatch_icall_fptr",
        "NtAllocateVirtualMemoryEx", "NtCreateWaitCompletionPacket",
    },
    "user32.dll": {"GetDpiForWindow", "GetDpiForSystem", "SetProcessDpiAwarenessContext",
                   "GetAwarenessFromDpiAwarenessContext"},
    "shell32.dll": {"SHGetKnownFolderItem"},
}
WIN10_PLUS = {
    "kernel32.dll": {"SetThreadDescription", "CreatePseudoConsole",
                     "GetSystemCpuSetInformation", "IsProcessBeingDebugged"},
}
# 微软 SDK 的 app-local 垫片本身就带 CFG 位，Win7 加载器忽略它，不算问题
SHIM_PREFIX = ("api-ms-win", "ext-ms-win")
# 只在特定功能被调用时才加载的可选组件：Win7 上加载失败只影响该功能，不会挡住启动。
# 加进这里必须写清"谁加载它、失败后果是什么"，否则等于给门禁开后门。
# notification_helper.exe = Chromium 桌面 toast 通知助手，chrome.exe 不静态依赖它。
OPTIONAL_HELPERS = {"notification_helper.exe"}
# Win7 SP1 的 System32 一定自带这些真实 dll，转发目标落到它们身上就不算缺失。
# 新增条目须先确认 Win7 自带，否则等于把 STATUS_DLL_NOT_FOUND 洗成绿色。
WIN7_SYSTEM_DLLS = {
    "advapi32.dll", "kernel32.dll", "ntdll.dll", "user32.dll", "gdi32.dll",
    "shell32.dll", "ole32.dll", "oleaut32.dll", "normaliz.dll", "msvcrt.dll",
    "version.dll", "secur32.dll", "crypt32.dll", "ws2_32.dll",
}


def _rva_to_off(rva, sections):
    for vsize, va, rawptr, rawsize in sections:
        if va <= rva < va + max(vsize, rawsize):
            return rawptr + (rva - va)
    return None


def _forwards(data, opt, sections, is64):
    """导出表里的"转发名"（api-ms-win-*.dll 全靠它把符号转给 ucrtbase 等真实 dll）。
    转发目标不出现在导入表里，静态扫导入会漏检——Win7 上目标缺失一样是 STATUS_DLL_NOT_FOUND。"""
    dd_off = opt + (112 if is64 else 96)
    exp_rva, exp_size = struct.unpack_from("<II", data, dd_off)
    base = _rva_to_off(exp_rva, sections)
    if not exp_rva or base is None or base + 40 > len(data):
        return set()
    num_names = struct.unpack_from("<I", data, base + 24)[0]
    addr_rva = struct.unpack_from("<I", data, base + 28)[0]
    name_rva = struct.unpack_from("<I", data, base + 32)[0]
    ord_rva = struct.unpack_from("<I", data, base + 36)[0]
    a, n, o = (_rva_to_off(x, sections) for x in (addr_rva, name_rva, ord_rva))
    if None in (a, n, o):
        return set()
    lo, hi = exp_rva, exp_rva + exp_size
    out = set()
    for i in range(num_names):
        name_off = n + i * 4
        if name_off + 4 > len(data) or a + i * 4 + 4 > len(data):
            break
        # 名字表的第 i 项，其地址要经 NameOrdinals[i] 映射回 AddressOfFunctions
        target = struct.unpack_from("<I", data, a + struct.unpack_from("<H", data, o + i * 2)[0] * 4)[0]
        if lo <= target < hi:                       # RVA 落在导出目录内 → 是转发名字符串
            off = _rva_to_off(target, sections)
            s = data[off:data.index(b"\0", off)].decode("ascii", "replace")
            dll = s.split("#")[0].split(".")[0].lower()
            if dll and not dll.startswith("!"):     # "!..." 是 Win10+ 的 downlevel 转发标记
                out.add(dll)
    return out


def parse_imports(path):
    """返回 {"imports": {dll小写名: {函数名}}, "cfg": bool}；非 PE 返回 None。"""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return None
    coff = pe + 4
    _, nsec = struct.unpack_from("<HH", data, coff)
    sizeopt = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic not in (0x10B, 0x20B):
        return None
    is64 = magic == 0x20B
    dd_off = opt + (112 if is64 else 96)
    imp_rva = struct.unpack_from("<I", data, dd_off + 8)[0]
    sections = []
    for i in range(nsec):
        b = opt + sizeopt + i * 40
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, b + 8)
        sections.append((vsize, va, rawptr, rawsize))

    imports = {}
    if imp_rva:
        base = _rva_to_off(imp_rva, sections)
        i = 0
        while base is not None:
            e = base + i * 20
            if e + 20 > len(data):
                break
            oft, _, _, name_rva, ft = struct.unpack_from("<IIIII", data, e)
            if name_rva == 0 and ft == 0:
                break
            no = _rva_to_off(name_rva or oft or ft, sections)
            if no is None:
                break
            dll = data[no:data.index(b"\0", no)].decode("ascii", "replace").lower()
            to = _rva_to_off(oft or ft, sections)
            names = imports.setdefault(dll, set())
            while to is not None:
                val = struct.unpack_from("<Q" if is64 else "<I", data, to)[0]
                to += 8 if is64 else 4
                if val == 0:
                    break
                if val & (1 << 63 if is64 else 1 << 31):
                    names.add("#ordinal%d" % (val & 0xFFFF))
                    continue
                fo = _rva_to_off(val, sections)
                if fo is None:
                    break
                names.add(data[fo + 2:data.index(b"\0", fo + 2)].decode("ascii", "replace"))
            i += 1
    cfg = bool(struct.unpack_from("<H", data, opt + 70)[0] & 0x4000)
    return {"imports": imports, "cfg": cfg, "forwards": _forwards(data, opt, sections, is64)}


def check(root):
    root = Path(root)
    files = sorted(p for p in root.rglob("*")
                   if p.suffix.lower() in (".exe", ".dll", ".pyd", ".so"))
    shipped = {p.name.lower() for p in files}
    problems, unparsed, cfg_other, skipped_optional = [], [], [], []
    for p in files:
        if p.name.lower() in OPTIONAL_HELPERS:
            skipped_optional.append(str(p.relative_to(root)))
            continue
        try:
            info = parse_imports(p)
        except Exception as e:
            unparsed.append("%s: %r" % (p.relative_to(root), e))
            continue
        if info is None:
            unparsed.append("%s: 不是可解析的 PE" % p.relative_to(root))
            continue
        rel = str(p.relative_to(root))
        for dll, names in info["imports"].items():
            if (dll.startswith("api-ms-win") or dll.startswith("ext-ms-win")) and dll not in shipped:
                problems.append("%s 依赖 %s，但包内没有同名 dll（Win7 会 STATUS_DLL_NOT_FOUND）" % (rel, dll))
            for table, label in ((WIN8_PLUS, "Win8+"), (WIN10_PLUS, "Win10+")):
                bad = sorted(names & table.get(dll, set()))
                if bad:
                    problems.append("%s 静态导入 %s 函数 %s!%s（Win7 会 STATUS_ENTRYPOINT_NOT_FOUND）"
                                    % (rel, label, dll, ", ".join(bad)))
        for dll in sorted(info["forwards"]):
            n = dll if dll.endswith(".dll") else dll + ".dll"
            if n not in shipped and n not in WIN7_SYSTEM_DLLS:
                problems.append("%s 的导出转发目标 %s 包内没有、Win7 也不自带（Win7 会 STATUS_DLL_NOT_FOUND）"
                                % (rel, n))
        if info["cfg"] and not Path(rel).name.lower().startswith(SHIM_PREFIX):
            cfg_other.append(rel)
    print("扫描目录: %s" % root)
    print("二进制 %d 个 | 解析失败 %d 个" % (len(files), len(unparsed)))
    if not files:
        print("  [FAIL] 目录里一个 PE 二进制都没有：路径写错或包没构建，这种『通过』是假的")
        return 1
    for msg in problems:
        print("  [FAIL] %s" % msg)
    for msg in unparsed[:10]:
        print("  [WARN] %s" % msg)
    if cfg_other:
        print("  [INFO] 带 CFG 位的非垫片镜像 %d 个（Win7 忽略该位，仅备查）" % len(cfg_other))
    if skipped_optional:
        print("  [INFO] 豁免按需加载的可选组件 %d 个：%s" % (len(skipped_optional), "; ".join(skipped_optional)))
    if problems or unparsed:
        print("门禁不通过：Win7 上大概率起不来。")
        return 1
    print("门禁通过：未发现 Win7 加载不了的依赖。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(check(sys.argv[1]))
