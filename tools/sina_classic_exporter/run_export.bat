@echo off
chcp 65001 >nul
title 新浪博客电脑端原版经典排版离线导出工具
echo ======================================================================
echo   新浪博客电脑端原版经典排版离线导出工具 (Sina Classic Exporter)
echo   1:1 还原新浪博客 PC 端双栏排版 · 内置毫秒级即时搜索 · 纯单文件
echo ======================================================================
echo.

cd /d "%~dp0..\.."

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

echo [提示] 默认导出专栏: 《西游正解》 (156篇)
echo.
set /p TARGET_URL="请输入新浪博客专栏或主页链接 (直接回车默认导出《西游正解》): "

if "%TARGET_URL%"=="" (
    set "TARGET_URL=https://blog.sina.com.cn/s/articlelist_5320406686_13_1.html"
)

echo.
echo [正在执行] 启动抓取与原版模板离线导出...
echo.

"%PYTHON_EXE%" tools\sina_classic_exporter\export_classic_sina.py --url "%TARGET_URL%"

echo.
echo [完成] 请在 downloads 目录下查看生成的 HTML 文件！
pause
