@echo off
title BlogDistiller 博萃 - 本地工作台一键启动
chcp 65001 >nul
echo ========================================================
echo   BlogDistiller · 博萃 - 全网博文批量导出与知识归档助手
echo   本地私有化运行模式 (100%% 本地运算 · 零服务器消耗)
echo ========================================================
echo.

cd /d "%~dp0"

:: 检查是否已存在虚拟环境
if exist ".venv\Scripts\python.exe" goto start_service

echo [1/3] 正在检测本地 Python 运行环境...

:: 优先检查 uv 极速包管理器
where uv >nul 2>nul
if %errorlevel% equ 0 (
    echo [2/3] 检测到 uv 工具，正在极速初始化虚拟环境并安装依赖...
    uv venv .venv
    if exist "requirements.txt" (
        uv pip install -r requirements.txt --python .\.venv\Scripts\python.exe
    ) else (
        uv pip install fastapi "uvicorn[standard]" httpx beautifulsoup4 markdownify lxml python-docx jinja2 pydantic playwright pillow --python .\.venv\Scripts\python.exe
    )
    goto finish_install
)

:: 检查常规系统 python
where python >nul 2>nul
if %errorlevel% equ 0 (
    echo [2/3] 正在创建 Python 虚拟环境 (.venv)...
    python -m venv .venv
    echo [2/3] 正在自动安装项目依赖 (首次启动需要 1~2 分钟，请稍候)...
    if exist "requirements.txt" (
        .\.venv\Scripts\pip.exe install -r requirements.txt -q
    ) else (
        .\.venv\Scripts\pip.exe install fastapi "uvicorn[standard]" httpx beautifulsoup4 markdownify lxml python-docx jinja2 pydantic playwright pillow -q
    )
    goto finish_install
)

echo.
echo ========================================================
echo ❌ [错误] 未检测到系统 Python 环境！
echo 请先下载并安装 Python 3.10+ (安装时务必勾选 "Add Python to PATH"):
echo 官方下载地址: https://www.python.org/downloads/
echo ========================================================
echo.
pause
exit /b 1

:finish_install
echo [3/3] 正在安装 Playwright 网页渲染内核 (用于动态文章渲染)...
.\.venv\Scripts\playwright.exe install chromium >nul 2>nul
echo.
echo ✅ 运行环境初始化完成！
echo.

:start_service
echo [提示] 正在启动本地服务...
echo [地址] 本地控制台: http://127.0.0.1:8000
echo.
echo 正在拉起默认浏览器...
start "" "http://127.0.0.1:8000"

.\.venv\Scripts\python.exe run.py
pause
