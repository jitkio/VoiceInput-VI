@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Stop Voice Input

echo Stopping voice input client ...

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*voice_input.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo Stopping model servers ...
taskkill /F /IM llama-server.exe >nul 2>&1

timeout /t 1 >nul
echo.
echo Done. Running processes:
tasklist /FI "IMAGENAME eq llama-server.exe" 2>nul | find /I "llama-server" >nul && (echo   ! llama-server still running) || (echo   ok: no llama-server)
pause
