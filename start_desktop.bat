@echo off
title BlogDistiller Desktop
cd /d "%~dp0"

echo ========================================================
echo   BlogDistiller WeChat Article Exporter Desktop
echo ========================================================
echo.

:: 1. Launch via local Electron engine (reflects latest code in desktop/)
if exist "%~dp0desktop\node_modules\electron\dist\electron.exe" (
    echo [*] Starting via local Electron engine in desktop/...
    cd /d "%~dp0desktop"
    node_modules\electron\dist\electron.exe .
    goto :done
)

:: 2. Launch via packaged release in dist/win-unpacked
if exist "%~dp0dist\win-unpacked" (
    echo [*] Starting packaged executable in dist/win-unpacked/...
    cd /d "%~dp0dist\win-unpacked"
    for /f "delims=" %%i in ('dir /b *.exe 2^>nul') do (
        "%%~fi"
        goto :done
    )
)

:: 3. Fallback to npm start
echo [*] Falling back to npm start...
cd /d "%~dp0desktop"
call npm start

:done
echo.
echo ========================================================
echo [Notice] BlogDistiller application has exited.
echo ========================================================
pause
