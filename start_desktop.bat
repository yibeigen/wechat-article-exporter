@echo off
title BlogDistiller 桌面端
echo ========================================================
echo   正在启动 BlogDistiller 桌面端（需要后台运行）...
echo ========================================================
cd /d "%~dp0"
npx electron .\desktop
