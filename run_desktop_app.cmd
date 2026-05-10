@echo off
cd /d "%~dp0"
python desktop_app.py --width 1440 --height 920
if errorlevel 1 (
  echo.
  echo Research Assistant failed to start. See the error above.
  pause
)
