@echo off
chcp 65001 >nul
echo ========================================================
echo   BlogDistiller - 博主知识蒸馏与多平台文章批量导出器
echo ========================================================
echo.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [提示] 正在初始化 Python 虚拟环境...
    uv venv .venv
    uv pip install fastapi "uvicorn[standard]" httpx beautifulsoup4 markdownify lxml python-docx jinja2 pydantic playwright --python .\.venv\Scripts\python.exe
)

echo [提示] 正在启动 BlogDistiller 服务...
echo [访问地址] 请在浏览器中打开: http://127.0.0.1:8000
echo.

.\.venv\Scripts\python.exe run.py
pause
