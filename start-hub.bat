@echo off
REM HoverCam hub for Windows: starts go2rtc and the hub, restarts the hub if it stops.
REM Put a shortcut to this file in shell:startup to run it when you sign in.
cd /d "%~dp0"
if "%HOVERCAM_API_URL%"=="" set HOVERCAM_API_URL=https://hovercam-api.onrender.com
if "%HOVERCAM_STATE_DIR%"=="" set HOVERCAM_STATE_DIR=state-prod

if not exist go2rtc.exe (
  echo go2rtc.exe is missing. Download go2rtc_win64.zip from
  echo https://github.com/AlexxIT/go2rtc/releases/latest and put go2rtc.exe in this folder.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Setting up Python environment, one time...
  py -3.12 -m venv .venv || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -e .
)

tasklist /FI "IMAGENAME eq go2rtc.exe" | find /I "go2rtc.exe" >nul || start "go2rtc" /min go2rtc.exe -config go2rtc.yaml

:loop
.venv\Scripts\python.exe -m hub
echo Hub stopped. Restarting in 5 seconds (close this window to stop)...
timeout /t 5 >nul
goto loop
