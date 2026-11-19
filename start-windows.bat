@echo off
rem Double-click to start Home Internet Control. Close this window to stop it.
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 is not installed. Get it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup. Then try again.
  pause
  exit /b 1
)
%PY% run.py %*
pause
