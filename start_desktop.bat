@echo off
title BlogDistiller 桌面客户端
echo ========================================================
echo   正在启动 BlogDistiller 微信文章导出助手 (桌面独立版)...
echo ========================================================
cd /d "%~dp0desktop"
npx electron .
