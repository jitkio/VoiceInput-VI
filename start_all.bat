@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Local Voice Input

where python >nul 2>nul
if errorlevel 1 (
    echo [X] python not found in PATH
    pause
    exit /b 1
)

if not exist "%~dp0config.toml" (
    echo [X] config.toml not found
    pause
    exit /b 1
)

echo ==========================================
echo   Local Voice Input
echo.
echo   1. Wait for "service ready" (10-20 sec on first run)
echo   2. HOLD CapsLock, speak, release
echo   3. Text is pasted at your cursor
echo.
echo   Tray: right-click the tray icon to pause / autostart / quit.
echo   Quit: press Ctrl+C or just close this window.
echo   The model servers will be shut down too.
echo ==========================================
echo.

python -u voice_input.py

echo.
echo [OK] Exited, model servers are closed.
pause
