@echo off
color 0A
title AJ Agent - Dreamer

echo.
echo  ============================================
echo    AJ Robotics Agent - Dreamer
echo    http://127.0.0.1:5000
echo  ============================================
echo.

cd /d "%~dp0.."
python -m agent.start_agent --machine Dreamer

echo.
echo  Agent stopped. Press any key to close.
pause >nul
