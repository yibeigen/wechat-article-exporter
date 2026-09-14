@echo off
@rem BlogDistiller · 桌面端客户端启动脚本
title BlogDistiller 桌面端
echo ========================================================
echo   正在启动 BlogDistiller 桌面端（需要后台运行）...
echo ========================================================
cd /d "%~dp0"
npx electron .\desktop
