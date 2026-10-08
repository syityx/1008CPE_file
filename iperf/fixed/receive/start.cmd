@echo off
chcp 65001 >nul
cd /d "%~dp0..\..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File iperf\launch.ps1 -Mode fixed -Role receive
pause
