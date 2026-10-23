@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Voice Input

REM 停止：VoiceInput.bat stop  （或双击本文件后按 Ctrl+C）
if /I "%~1"=="stop" goto :stop

where python >nul 2>nul
if errorlevel 1 (
    echo [X] 没找到 Python。先装 Python 3.11+ 并勾选 "Add to PATH"。
    echo     https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 依赖缺失时自动安装
python -c "import numpy,sounddevice,pyperclip,pynput,PIL,pystray" >nul 2>nul
if errorlevel 1 (
    echo [setup] 正在安装 Python 依赖…
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [X] 依赖安装失败，手动运行: python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

REM 环境自检：llama.cpp / 模型是否就位
python voice_input.py --check >nul 2>nul
if errorlevel 1 (
    echo.
    echo [setup] 缺少 llama.cpp 或模型，开始自动下载（约 1.5 GB + 5.5 GB）
    echo         想手动准备就按 Ctrl+C 退出，看 README 的「放置模型」一节。
    echo.
    python voice_input.py --setup
    if errorlevel 1 (
        echo [X] 自动准备失败
        pause
        exit /b 1
    )
)

echo ==========================================
echo   Voice Input  ^|  本地离线语音输入
echo.
echo   按住 CapsLock 说话，松开自动上屏
echo   托盘图标右键：暂停 / 开机自启 / 日志 / 退出
echo   关闭本窗口或 Ctrl+C 退出（模型服务会一起关）
echo ==========================================
echo.

python -u voice_input.py

echo.
echo [OK] 已退出，模型服务已关闭。
pause
exit /b 0

:stop
echo 正在停止 Voice Input …
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*voice_input.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
taskkill /F /IM llama-server.exe >nul 2>&1
echo 已停止。
pause
exit /b 0
