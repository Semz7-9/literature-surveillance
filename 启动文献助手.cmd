@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Project virtual environment was not found.
    echo Run: py -3.11 -m venv .venv
    echo Then: .venv\Scripts\python.exe -m pip install -e .
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m src.desktop
if errorlevel 1 (
    echo.
    echo [ERROR] The application failed to start. See the message above.
    pause
    exit /b 1
)
