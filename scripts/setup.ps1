# One-time setup on Windows (PowerShell). Run from the repository folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Error "Python launcher 'py' not found. Install Python 3.11+ from https://www.python.org/downloads/ (tick 'Add to PATH')."
}
if (-not (Test-Path .venv)) { py -3 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path quizpilot.toml)) { & .\.venv\Scripts\quizpilot.exe init }

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1. .\.venv\Scripts\Activate.ps1"
Write-Host "  2. setx DEEPSEEK_API_KEY `"sk-...`"   (then open a new terminal)"
Write-Host "  3. Put your DeepSeek model ID in quizpilot.toml"
Write-Host "  4. quizpilot chrome   and log in to your sites"
