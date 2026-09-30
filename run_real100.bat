@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"
python "%ROOT%tools\benchmark_real100.py" %*
endlocal
