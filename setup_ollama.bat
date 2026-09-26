@echo off
REM Free local AI for AI PCB Router with Ollama (no API key; nothing leaves this PC).
REM Double-click in the repository folder after setup_gpu.bat (or any Python install).
setlocal
cd /d "%~dp0"
where ollama >nul 2>nul
if errorlevel 1 (
    echo Installing Ollama with winget...
    winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo Could not install automatically: download Ollama from https://ollama.com/download
        pause & exit /b 1
    )
    echo Please start Ollama from the Start menu once, then run this file again.
    pause & exit /b 0
)
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
REM Default model qwen2.5:7b (~4.7 GB, 16 GB RAM). With 8 GB RAM use: qwen2.5:3b
set MODEL=%1
if "%MODEL%"=="" set MODEL=qwen2.5:7b
python -m pcbrouter --setup-ollama %MODEL%
pause
endlocal
