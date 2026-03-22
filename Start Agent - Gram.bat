@echo off
color 0B
title AJ Agent - Gram

echo.
echo  ============================================
echo    AJ Robotics Agent - Gram
echo    http://127.0.0.1:5000
echo  ============================================
echo.

cd /d "%~dp0"
python -m agent.start_agent --machine Gram

echo.
echo  Agent stopped. Press any key to close.
pause >nul
