@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 scripts\install.py %*
) else (
  python scripts\install.py %*
)
if errorlevel 1 (
  echo Installation failed. Install Python 3.9 or later from https://www.python.org/downloads/windows/ and retry.
)
pause
