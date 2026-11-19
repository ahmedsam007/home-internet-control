@echo off
rem Double-click to start D-Link DSL Control AND let phones on the home Wi-Fi use it.
rem The first time, Windows may ask to allow Python on the network: choose "Allow" (Private networks).
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
%PY% run.py --lan %*
pause
