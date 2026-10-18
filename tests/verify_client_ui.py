import os
import sys
import time
import subprocess
import urllib.request
import json

sys.stdout.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright

CURRENT_ARTIFACT_DIR = r"C:\Users\30574\.gemini\antigravity-ide\brain\ba9ee0b0-bfd5-445b-89bd-b1b4aedf924e"
os.makedirs(CURRENT_ARTIFACT_DIR, exist_ok=True)

port = 8000
python_exe = os.path.abspath(os.path.join(".venv", "Scripts", "python.exe"))
if not os.path.exists(python_exe):
    python_exe = sys.executable

server_proc = None

def is_server_ready():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False

print("=" * 60)
print("🚀 BlogDistiller 桌面端改造 UI 与功能全自动回归验证")
print("=" * 60)

if not is_server_ready():
    print(f"[1/4] 启动本地 Python 服务引擎 (端口: {port})...")
    cmd = [python_exe, "run.py", "--port", str(port), "--host", "127.0.0.1", "--no-reload"]
    proc_env = os.environ.copy()
    proc_env["PYTHONUNBUFFERED"] = "1"
    server_proc = subprocess.Popen(
        cmd,
        cwd=os.getcwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=proc_env
    )
    for _ in range(30):
        time.sleep(0.5)
        if is_server_ready():
            print("      ✅ 本地服务就绪！")
            break
    if not is_server_ready():
        print("❌ 本地服务未能按时就绪")
        if server_proc:
            server_proc.kill()
        sys.exit(1)
else:
    print(f"[1/4] 本地 Python 服务已在端口 {port} 运行中，直接复用。")

try:
    print("[2/4] 启动 Chromium 无头浏览器进行客户端模式 UI 渲染与断言...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        
        url = f"http://127.0.0.1:{port}/app?client_mode=1&port={port}"
        print(f"      打开页面: {url}")
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(1.5)
        
        print("[3/4] 验证 4 项核心客户端优化指标...")
        
        # 1. 验证顶部导航栏
        badge_visible = page.is_visible("#clientModeBadgeBtn")
        desktop_btn_hidden = not page.is_visible("#desktopModalNavBtn")
        ext_btn_hidden = not page.is_visible("#navExtensionStatusBtn")
        print(f"      ① 客户端绿色徽章展示: {badge_visible}")
        print(f"         - '桌面客户端' 按钮已隐藏: {desktop_btn_hidden}")
        print(f"         - '本地IP直连助手' 按钮已隐藏: {ext_btn_hidden}")
        assert badge_visible, "客户端徽章未显示"
        assert desktop_btn_hidden, "桌面客户端下载按钮未隐藏"
        assert ext_btn_hidden, "浏览器扩展按钮未隐藏"
        page.screenshot(path=os.path.join(CURRENT_ARTIFACT_DIR, "verified_sanitized_navbar.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 100})
        
        # 2. 验证“本地IP直连助手”与“本地住宅 IP 直连抓取通道”已彻底从界面中剔除
        has_relay_card = page.locator("#localRelaySwitchCard").count() > 0
        has_ext_btn = page.locator("#navExtensionStatusBtn").count() > 0
        first_switch_text = page.locator("#noiseFilterSwitch").locator("xpath=../..").inner_text()
        print(f"      ② 彻底移除旧扩展中继卡片与导航按钮:")
        print(f"         - localRelaySwitchCard 卡片已彻底从 DOM 移除: {not has_relay_card}")
        print(f"         - navExtensionStatusBtn 按钮已彻底从 DOM 移除: {not has_ext_btn}")
        print(f"         - 第 5 节起始项为: {first_switch_text.splitlines()[0]}")
        assert not has_relay_card, "旧版本本地住宅IP通道开关仍残留在页面中！"
        assert not has_ext_btn, "旧版本本地IP直连助手按钮仍残留在页面中！"
        assert "智能文本降噪与广告过滤" in first_switch_text, "第 5 节首项未正确展示文本降噪选项"
        page.locator("#noiseFilterSwitch").locator("xpath=../..").scroll_into_view_if_needed()
        time.sleep(0.5)
        page.screenshot(path=os.path.join(CURRENT_ARTIFACT_DIR, "verified_clean_section5.png"))
        
        # 3. 验证知乎专属登录卡片
        if page.is_visible("#communityModal"):
            try:
                page.evaluate("document.getElementById('communityModal').style.display = 'none'")
            except Exception:
                pass
        page.wait_for_selector(".platform-chip[data-platform='zhihu']", timeout=15000)
        page.locator(".platform-chip[data-platform='zhihu']").click(force=True)
        time.sleep(1.0)
        zhihu_visible = page.is_visible("#zhihuAuthBox")
        zhihu_text = page.locator("#zhihuAuthBox").inner_text()
        print(f"      ③ 知乎登录卡片展示: {zhihu_visible}")
        print(f"         - 包含内容: {zhihu_text.replace(chr(10), ' ')[:70]}...")
        assert zhihu_visible, "切换到知乎时登录卡片未显示"
        page.locator("#zhihuAuthBox").scroll_into_view_if_needed()
        time.sleep(0.5)
        page.screenshot(path=os.path.join(CURRENT_ARTIFACT_DIR, "verified_zhihu_auth_box.png"))
        
        # 4. 验证微博专属登录卡片
        page.locator(".platform-chip[data-platform='weibo']").click(force=True)
        time.sleep(1.0)
        weibo_visible = page.is_visible("#weiboAuthBox")
        weibo_text = page.locator("#weiboAuthBox").inner_text()
        print(f"      ④ 微博登录卡片展示: {weibo_visible}")
        print(f"         - 包含内容: {weibo_text.replace(chr(10), ' ')[:70]}...")
        assert weibo_visible, "切换到微博时登录卡片未显示"
        page.locator("#weiboAuthBox").scroll_into_view_if_needed()
        time.sleep(0.5)
        page.screenshot(path=os.path.join(CURRENT_ARTIFACT_DIR, "verified_weibo_auth_box.png"))
        
        # 6. 验证在普通浏览器直接打开 /app (无 client_mode) 也同样绝无旧扩展中继残留
        web_page = browser.new_page(viewport={"width": 1280, "height": 900})
        web_url = f"http://127.0.0.1:{port}/app"
        print(f"      ⑥ 检查普通浏览器直开模式: {web_url}")
        web_page.goto(web_url, wait_until="networkidle", timeout=30000)
        time.sleep(1.0)
        assert web_page.locator("#localRelaySwitchCard").count() == 0, "普通浏览器模式下仍残留旧扩展通道卡片！"
        assert web_page.locator("#navExtensionStatusBtn").count() == 0, "普通浏览器模式下仍残留旧扩展助手按钮！"
        print("         - 普通浏览器直开模式下扩展残留彻底为 0: True")
        web_page.screenshot(path=os.path.join(CURRENT_ARTIFACT_DIR, "verified_pure_browser_mode.png"))
        web_page.close()
        
        browser.close()
        
    print("[4/4] 验证完成，所有断言 100% 通过！截图已保存至产物目录。")
    print("=" * 60)
    print("🎉 恭喜！桌面端模式 4 项优化已全部通过自动化测试！")
    print("=" * 60)

finally:
    if server_proc:
        print("[清理] 关闭测试用 Python 服务进程...")
        if sys.platform == "win32":
            os.system(f"taskkill /pid {server_proc.pid} /T /F >nul 2>nul")
        else:
            server_proc.terminate()
