@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 download_and_build_real100.py
) else (
  python download_and_build_real100.py
)
if errorlevel 1 (
  echo.
  echo FAILED. See the error above.
  pause
  exit /b 1
)
echo.
echo Finished. Send DAVESROUTER-Real100-Unrouted-Boards-and-Rules.zip to Claude.
pause
