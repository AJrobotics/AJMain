@echo off
:: Usage: restart_agent.bat MachineName
:: Kills existing python agents and starts a new one via schtasks
set MACHINE=%1
if "%MACHINE%"=="" set MACHINE=Gram

:: Kill existing python processes
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak >nul

:: Get the directory where this script lives (scripts\), go up one level for AJMain
set SCRIPTDIR=%~dp0
set AJMAIN=%SCRIPTDIR%..

:: Create and run scheduled task
schtasks /Create /TN "AJAgent_%MACHINE%" /TR "powershell -ExecutionPolicy Bypass -File \"%SCRIPTDIR%start_agent_bg.ps1\" -Machine %MACHINE%" /SC ONCE /ST 00:00 /F >nul 2>&1
schtasks /Run /TN "AJAgent_%MACHINE%" >nul 2>&1
