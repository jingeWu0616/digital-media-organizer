@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0organizer_app.py"
) else (
    python "%~dp0organizer_app.py"
    if errorlevel 1 pause
)
