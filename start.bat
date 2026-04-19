@echo off
cd /d %~dp0
call venv\Scripts\activate

echo.
echo  SneakPeek — Starting...
echo  Make sure IP Webcam is running on your phone first.
echo.

start "SneakPeek Engine" cmd /k "python main.py"
timeout /t 3 /nobreak > nul
start "SneakPeek UI" cmd /k "python ui\app.py"
timeout /t 2 /nobreak > nul
start http://localhost:5000

echo  Both terminals opened. Browser launching...
echo  Close this window when done.
pause
