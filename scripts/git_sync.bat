@echo off
:: Auto git pull - runs periodically to keep repo in sync
cd /d "%~dp0.."
git pull --ff-only origin main >nul 2>&1
