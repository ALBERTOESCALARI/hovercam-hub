@echo off
REM HoverCam hub for Windows. Starts go2rtc + the hub and restarts the hub if it stops.
REM Runs fine as a startup task (no window): output goes to hub.log.
REM Install once as a task (admin terminal):
REM   schtasks /Create /TN "HoverCam Hub" /TR "%USERPROFILE%\hovercam-hub\start-hub.bat" /SC ONSTART /DELAY 0001:00 /RU SYSTEM /RL HIGHEST /F
cd /d "%~dp0"
if "%HOVERCAM_API_URL%"=="" set HOVERCAM_API_URL=https://hovercam-api.hoverlayer.com
if "%HOVERCAM_STATE_DIR%"=="" set HOVERCAM_STATE_DIR=state-prod
REM ffmpeg from winget lives in the user's WinGet Links folder; add it so this works before sign-in too.
set "PATH=%~dp0..\AppData\Local\Microsoft\WinGet\Links;%PATH%"
REM git (for self-update from GitHub) when installed with winget/Git for Windows.
set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
if not exist go2rtc.exe (echo go2rtc.exe missing - download go2rtc_win64.zip from GitHub releases & exit /b 1)
if not exist .venv\Scripts\python.exe (
  py -3.12 -m venv .venv || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -e .
)
REM Fresh go2rtc config each start (go2rtc saves camera logins into it; the hub re-adds streams).
tasklist /FI "IMAGENAME eq go2rtc.exe" | find /I "go2rtc.exe" >nul || (copy /Y go2rtc.base.yaml go2rtc.yaml >nul & start "go2rtc" /min go2rtc.exe -config go2rtc.yaml)
:loop
echo %date% %time% starting hub >> hub.log
.venv\Scripts\python.exe -m hub >> hub.log 2>&1
ping -n 6 127.0.0.1 >nul
goto loop
