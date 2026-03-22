@echo off
color 0A

:: Detect machine name from hostname
set "MACHINE=%COMPUTERNAME%"
if /i "%COMPUTERNAME%"=="Dreamer" set "MACHINE=Dreamer"
if /i "%COMPUTERNAME%"=="Dongchul_Gram" set "MACHINE=Gram"

title AJ Robotics - %MACHINE%
echo.
echo  ============================================
echo    AJ Robotics - %MACHINE%
echo    http://127.0.0.1:5000
echo  ============================================
echo.

cd /d "%~dp0"

:: Gmail SMTP for SMS alerts (via Verizon email-to-SMS gateway)
set "GMAIL_USER=Dreamittogether@gmail.com"
set "GMAIL_APP_PASSWORD=ybxgmceixhqbscas"

:: Ensure SSH can find keys (HOME must point to user profile)
set "HOME=%USERPROFILE%"

:: Open SSH tunnel to Christy for Vision Server (port 5100)
echo  Opening SSH tunnel to Christy:5100...
start /b ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -f -N -L 5100:localhost:5100 ajrobotics@192.168.1.94 2>nul
echo  SSH tunnel ready.
echo.

echo  Starting server...
echo.
start "" "http://127.0.0.1:5000" 2>nul
python app.py

echo.
echo  Server stopped. Press any key to close.
pause >nul
