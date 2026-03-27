@echo off
color 0B

title AJ Robotics - Dreamer XBee Responder
echo.
echo  ============================================
echo    AJ Robotics - Dreamer XBee Responder
echo    XBee COM18 @ 115200 baud
echo    API: http://127.0.0.1:5001
echo  ============================================
echo.

cd /d "%~dp0"

echo  Starting XBee Responder service...
echo.
python scripts\dreamer_xbee_service.py

echo.
echo  Service stopped. Press any key to close.
pause >nul
