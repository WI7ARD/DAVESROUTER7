@echo off
REM One-click setup for AI PCB Router with GPU routing (Intel Iris Xe / Arc or NVIDIA).
REM Double-click this file in the repository folder. Needs Python 3.12 from python.org.
setlocal
cd /d "%~dp0"
if not exist .venv (
    echo Creating the Python environment...
    py -3.12 -m venv .venv || (echo Python 3.12 not found: install it from python.org & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
echo Installing AI PCB Router...
python -m pip install --upgrade pip >nul
python -m pip install -e . || (echo Install failed & pause & exit /b 1)
echo Detecting your GPU and installing GPU support...
python -m pcbrouter --setup-gpu
echo.
echo Starting AI PCB Router (Tools ^> Set Up GPU... to check or change GPU settings)...
start "" pythonw -m pcbrouter
endlocal
