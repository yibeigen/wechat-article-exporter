import asyncio
import argparse
import sys
from pathlib import Path

# 适配 Windows 控制台 UTF-8 输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 添加 backend 到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import TaskCreateRequest, PlatformEnum, ExportFormatEnum
from app.task_manager import task_manager

async def run_cli():
    parser = argparse.ArgumentParser(description="BlogDistiller CLI - 博主多平台文章批量导出工具")
    parser.add_argument("--platform", required=True, choices=[p.value for p in PlatformEnum], help="目标平台")
    parser.add_argument("--target", required=True, help="博主主页链接、ID或文章URL")
    parser.add_argument("--formats", default="md,html,pdf,docx,txt", help="导出的格式逗号分隔 (md,html,pdf,docx,txt)")
    parser.add_argument("--no-noise-filter", action="store_true", help="关闭智能去噪")
    parser.add_argument("--max", type=int, default=None, help="最大抓取篇数")
    parser.add_argument("--author", default=None, help="自定义博主名")

    args = parser.parse_args()

    format_list = []
    for f in args.formats.split(","):
        f = f.strip().lower()
        if f in [e.value for e in ExportFormatEnum]:
            format_list.append(ExportFormatEnum(f))

    req = TaskCreateRequest(
        platform=PlatformEnum(args.platform),
        target=args.target,
        export_formats=format_list,
        enable_noise_filter=not args.no_noise_filter,
        max_articles=args.max,
        author_name_override=args.author
    )

    print(f"🚀 正在启动任务: 平台={args.platform}, 目标={args.target}...")
    task_id = task_manager.create_task(req)

    while True:
        task = task_manager.get_task(task_id)
        if not task:
            break
        print(f"\r[{task.status.value.upper()}] {task.progress_percent}% - {task.message}", end="", flush=True)
        if task.status.value in ["completed", "failed"]:
            print("\n")
            if task.status.value == "completed":
                print("🎉 导出产物:")
                for fmt, url in task.export_files.items():
                    print(f"  - {fmt.upper()}: {url}")
            else:
                print(f"❌ 失败原因: {task.error_message}")
            break
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    asyncio.run(run_cli())
