@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Build VoiceInput.exe

where python >nul 2>nul
if errorlevel 1 (
    echo [X] python not found in PATH
    pause
    exit /b 1
)

echo ==========================================
echo   Build VoiceInput.exe (PyInstaller, onedir)
echo ==========================================
echo.

REM PyInstaller 6.22 打包 skia 会崩（原生冲突），这里固定用 6.11.1 的隔离环境
if not exist ".venv_build\Scripts\python.exe" (
    echo [1/5] create build venv
    python -m venv .venv_build
    if errorlevel 1 goto :fail
)
set "PY=.venv_build\Scripts\python.exe"

echo [2/5] install build deps
"%PY%" -m pip -q install --upgrade pip
"%PY%" -m pip -q install "pyinstaller==6.11.1"
"%PY%" -m pip -q install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/5] icon
"%PY%" voice_input.py --make-icon app.ico
if errorlevel 1 goto :fail

echo [4/5] pyinstaller
"%PY%" -m PyInstaller --noconfirm --clean --onedir --name VoiceInput ^
    --icon app.ico ^
    --collect-binaries skia ^
    --collect-all numpy ^
    --hidden-import pystray ^
    --hidden-import PIL.ImageDraw ^
    --hidden-import pynput.keyboard._win32 ^
    voice_input.py
if errorlevel 1 goto :fail

echo [5/5] config + model folders
copy /Y config.toml dist\VoiceInput\config.toml >nul
mkdir "dist\VoiceInput\models" 2>nul
mkdir "dist\VoiceInput\llama" 2>nul
echo 把 gguf 模型放到这个目录（或在 dist\VoiceInput 下运行: VoiceInput.exe --setup）>"dist\VoiceInput\models\README.txt"
echo 把 llama.cpp 解压到这个目录，保证 llama\llama-server.exe 存在>"dist\VoiceInput\llama\README.txt"

echo.
echo 输出: dist\VoiceInput\VoiceInput.exe
echo 分发时把整个 dist\VoiceInput 文件夹拷走（models 和 llama 一起带上即可开箱用）。
echo.
if "%~1"=="" pause
exit /b 0

:fail
echo.
echo [X] Build failed.
pause
exit /b 1
