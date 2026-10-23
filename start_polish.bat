@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM llama.cpp 目录：优先用环境变量 LLAMA_DIR，否则用项目下的 llama.cpp\
if defined LLAMA_DIR (set "LLAMA=%LLAMA_DIR%") else (set "LLAMA=%~dp0llama.cpp")
set "ME=%~dp0"
set "MODEL=%ME%models\Qwen3.5-4B-Q4_K_M.gguf"

echo [POLISH] Qwen3.5-4B  -^>  http://127.0.0.1:8082
echo.

if not exist "%LLAMA%\llama-server.exe" (
    echo [X] llama-server.exe not found: %LLAMA%\llama-server.exe
    echo     Download from https://github.com/ggml-org/llama.cpp/releases
    pause
    exit /b 1
)
if not exist "%MODEL%" (
    echo [X] Model not found: %MODEL%
    echo     Run: python download_models.py --only polish
    pause
    exit /b 1
)

"%LLAMA%\llama-server.exe" -m "%MODEL%" --host 127.0.0.1 --port 8082 -ngl 99 -c 4096 -np 1 -t 8 --temp 0.2 --no-warmup

echo.
echo [POLISH stopped] exit code %errorlevel%
pause
