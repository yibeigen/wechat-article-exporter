import subprocess
import time
import sys
import os
import json
import urllib.request
import urllib.error

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def find_free_port(start_port=8020, max_port=8050):
    import socket
    for p in range(start_port, max_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    return 8020

def run_test():
    print("=" * 60)
    print("🚀 BlogDistiller 客户端自治模式 (Local-First Desktop) E2E 测试")
    print("=" * 60)

    port = find_free_port()
    python_exe = os.path.abspath(os.path.join(".venv", "Scripts", "python.exe"))
    if not os.path.exists(python_exe):
        python_exe = sys.executable
    
    print(f"[1/6] 启动本地 Python 引擎: {python_exe} on port {port}")
    cmd = [python_exe, "run.py", "--port", str(port), "--host", "127.0.0.1", "--no-reload"]
    proc_env = os.environ.copy()
    proc_env["PYTHONUNBUFFERED"] = "1"
    log_file = open("test_server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=os.getcwd(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=proc_env
    )

    try:
        # 2. 等待健康检查响应
        print("[2/6] 轮询 /api/health 健康探活接口...")
        ready = False
        health_url = f"http://127.0.0.1:{port}/api/health"
        for i in range(35):
            time.sleep(0.5)
            if proc.poll() is not None:
                log_file.flush()
                with open("test_server.log", "r", encoding="utf-8", errors="replace") as f:
                    print(f"❌ Python 进程提前退出，退出码: {proc.returncode}\n{f.read()}")
                return False
            try:
                with urllib.request.urlopen(health_url, timeout=1.5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        print(f"      ✅ 健康检查通过: {data}")
                        ready = True
                        break
            except Exception as e:
                pass

        if not ready:
            log_file.flush()
            with open("test_server.log", "r", encoding="utf-8", errors="replace") as f:
                print(f"❌ 引擎未在预期时间内就绪，日志内容:\n{f.read()}")
            return False

        # 3. 检查客户端版本分发接口
        print("[3/6] 检查 /api/client/version 分发与升级接口...")
        ver_url = f"http://127.0.0.1:{port}/api/client/version"
        with urllib.request.urlopen(ver_url, timeout=2.0) as resp:
            ver_data = json.loads(resp.read().decode("utf-8"))
            print(f"      ✅ 客户端版本接口响应: version={ver_data.get('version')}, standard={ver_data.get('standard_download_url')}")

        # 4. 创建本地抓取与导出任务 (Custom URLs)
        print("[4/6] 触发本地文章抓取与导出任务 (POST /api/tasks)...")
        task_url = f"http://127.0.0.1:{port}/api/tasks"
        payload = {
            "platform": "custom_urls",
            "target": "https://httpbin.org/html",
            "export_formats": ["md", "txt", "html"],
            "download_images": False,
            "use_cache": False
        }
        req = urllib.request.Request(
            task_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        task_id = None
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            task_resp = json.loads(resp.read().decode("utf-8"))
            task_id = task_resp.get("task_id")
            print(f"      ✅ 任务成功创建! task_id: {task_id}")

        if not task_id:
            print("❌ 任务ID获取失败")
            return False

        # 5. 轮询任务进度直至完成
        print(f"[5/6] 轮询任务状态 (GET /api/tasks/{task_id})...")
        status_url = f"http://127.0.0.1:{port}/api/tasks/{task_id}"
        final_status = None
        task_data = {}
        for i in range(40):
            time.sleep(1.0)
            try:
                with urllib.request.urlopen(status_url, timeout=3.0) as resp:
                    task_data = json.loads(resp.read().decode("utf-8"))
                    status = task_data.get("status")
                    msg = task_data.get("message", "")
                    progress = task_data.get("progress_percent", 0)
                    print(f"      [进度 {progress:.0f}%] 状态: {status} - {msg}", flush=True)
                    if status in ["completed", "failed", "cancelled"]:
                        final_status = status
                        break
            except Exception as e:
                print(f"      轮询等待中: {e}", flush=True)

        if final_status != "completed":
            print(f"❌ 任务未完成，最终状态: {final_status}")
            return False

        # 6. 验证产物文件生成与落盘
        print("[6/6] 验证生成产物下载与落盘...")
        files = task_data.get("export_files") or task_data.get("files", {})
        print(f"      产物文件字典: {files}")
        if not files:
            print("❌ 未生成任何产物文件")
            return False

        # 测试 ZIP 下载 (中文文件名需 quote 编码)
        import urllib.parse
        zip_rel = files.get("zip") or list(files.values())[0]
        quoted_rel = urllib.parse.quote(zip_rel, safe="/:?=&")
        dl_url = f"http://127.0.0.1:{port}{quoted_rel}" if quoted_rel.startswith("/") else f"http://127.0.0.1:{port}/{quoted_rel}"
        with urllib.request.urlopen(dl_url, timeout=5.0) as resp:
            content_length = resp.headers.get("Content-Length")
            disposition = resp.headers.get("Content-Disposition", "")
            print(f"      ✅ 导出文件下载响应正常: 大小={content_length} bytes, Disposition={disposition}")

        print("=" * 60)
        print("🎉 恭喜！客户端自治模式全链路端到端自动化测试 100% 通过！")
        print("=" * 60)
        return True

    finally:
        print("[清理] 终止测试 Python 子进程...")
        try:
            log_file.close()
        except Exception:
            pass
        if sys.platform == "win32":
            os.system(f"taskkill /pid {proc.pid} /T /F >nul 2>nul")
        else:
            proc.terminate()
        time.sleep(0.5)

if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
