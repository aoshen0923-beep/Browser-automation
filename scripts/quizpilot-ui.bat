@echo off
rem Double-click to open the quizpilot answering page in your browser.
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0quizpilot.exe" (
  echo.
  echo [Error] quizpilot.exe is not in this folder.
  echo Right-click quizpilot-windows.zip, choose "Extract All",
  echo then double-click quizpilot-ui.bat inside the extracted folder.
  echo.
  pause
  exit /b 1
)
set PATH=%~dp0;%PATH%
if not exist quizpilot.toml quizpilot init
quizpilot ui
pause
