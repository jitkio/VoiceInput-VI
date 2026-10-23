@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM llama.cpp 目录：优先用环境变量 LLAMA_DIR，否则用项目下的 llama.cpp\
if defined LLAMA_DIR (set "LLAMA=%LLAMA_DIR%") else (set "LLAMA=%~dp0llama.cpp")
set "ME=%~dp0"

echo [ASR] Qwen3-ASR-1.7B  -^>  http://127.0.0.1:8081
echo.

if not exist "%LLAMA%\llama-server.exe" (
    echo [X] llama-server.exe not found: %LLAMA%\llama-server.exe
    echo     Download from https://github.com/ggml-org/llama.cpp/releases
    pause
    exit /b 1
)
if not exist "%ME%models\Qwen3-ASR-1.7B-Q8_0.gguf" (
    echo [X] Model not found: %ME%models\Qwen3-ASR-1.7B-Q8_0.gguf
    echo     Run: python download_models.py
    pause
    exit /b 1
)

"%LLAMA%\llama-server.exe" -m "%ME%models\Qwen3-ASR-1.7B-Q8_0.gguf" --mmproj "%ME%models\mmproj-Qwen3-ASR-1.7B-Q8_0.gguf" --host 127.0.0.1 --port 8081 -ngl 99 -c 4096 -np 1 -t 8 --no-warmup

echo.
echo [ASR stopped] exit code %errorlevel%
pause
