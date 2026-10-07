@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py %*
) else (
    where python >nul 2>nul && python app.py %* || echo Python не найден. См. README.md
)
pause
