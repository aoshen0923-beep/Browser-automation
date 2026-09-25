@echo off
rem Double-click to open a terminal where `quizpilot` works (UTF-8 for Chinese).
chcp 65001 >nul
cd /d "%~dp0"
set PATH=%~dp0;%PATH%
if not exist quizpilot.toml quizpilot init
echo.
echo quizpilot is ready. Try:  quizpilot --help
echo.
cmd /k
