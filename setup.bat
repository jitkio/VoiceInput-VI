@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Voice Input - Setup

where python >nul 2>nul
if errorlevel 1 (
    echo [X] python not found in PATH. Install Python 3.11+ first.
    pause
    exit /b 1
)

echo ==========================================
echo   Voice Input - one-time setup
echo   1/3  pip install requirements
echo   2/3  download GGUF models (~5.5 GB)
echo   3/3  check llama.cpp
echo ==========================================
echo.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] pip install failed
    pause
    exit /b 1
)
echo.

python download_models.py
if errorlevel 1 (
    echo [X] model download failed
    pause
    exit /b 1
)
echo.

if not exist "llama.cpp\llama-server.exe" (
    echo [!] llama.cpp not found in .\llama.cpp\
    echo     Download a Windows build from:
    echo     https://github.com/ggml-org/llama.cpp/releases
    echo     Unzip it into the "llama.cpp" folder next to this file.
    echo     (or set LLAMA_DIR / edit [llama] dir in config.toml)
    echo.
    pause
    exit /b 0
)

echo [OK] Setup finished. Double-click start_all.bat to run.
pause
