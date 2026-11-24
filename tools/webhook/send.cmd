@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" "%~dp0send_message.py"
) else (
  python "%~dp0send_message.py"
)
set "toolExit=%errorlevel%"
pause
exit /b %toolExit%
