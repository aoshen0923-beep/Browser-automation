@echo off
rem Double-click to open a terminal where `quizpilot` works (UTF-8 for Chinese).
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0quizpilot.exe" (
  echo.
  echo [Error] quizpilot.exe is not in this folder.
  echo You probably opened the zip without extracting it.
  echo Close this window, right-click quizpilot-windows.zip, choose "Extract All",
  echo then double-click quizpilot-shell.bat inside the extracted folder.
  echo.
  pause
  exit /b 1
)
set PATH=%~dp0;%PATH%
if not exist quizpilot.toml quizpilot init
echo.
echo quizpilot is ready. Try:  quizpilot --help
echo.
cmd /k
