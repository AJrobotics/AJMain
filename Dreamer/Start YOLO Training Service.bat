@echo off
color 0D

title AJ Robotics - YOLO Training Service

echo.
echo  ============================================
echo    AJ Robotics - YOLO Training Service
echo    API: http://127.0.0.1:5002
echo    Datasets: datasets\
echo    Runs: runs\
echo  ============================================
echo.

cd /d "%~dp0.."

echo  Starting Training Service...
echo.
python Dreamer\training_service.py

echo.
echo  Service stopped. Press any key to close.
pause >nul
