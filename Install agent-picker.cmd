@echo off
cd /d "%~dp0"
py -3 scripts\install-windows.py
if errorlevel 1 (
  echo Installation failed. Install Python 3.9 or later from https://www.python.org/downloads/windows/ and retry.
)
pause
