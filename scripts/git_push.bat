@echo off
:: Quick git add, commit, and push
cd /d "%~dp0.."
git add -A
git commit -m "Auto sync from %COMPUTERNAME%" 2>nul
git push origin main
