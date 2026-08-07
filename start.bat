@echo off
REM ===== exam-drill launcher =====
REM Double-click to start. Settings: server_config.txt (same folder).
REM The URL to open is shown on screen after startup.
chcp 65001 >nul
cd /d "%~dp0"
echo Starting server... (settings: server_config.txt)
echo.
python serve.py
if errorlevel 9009 (
  echo.
  echo Python was not found. Install Python 3.9+ from https://www.python.org/downloads/
)
echo.
echo Server stopped. Press any key to close this window.
pause >nul
