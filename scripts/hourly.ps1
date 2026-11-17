# One hourly cycle (scan + post) on Windows, logged to data\logs\YYYY-MM-DD.log
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$LogDir = Join-Path $ProjectRoot "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "$(Get-Date -Format 'yyyy-MM-dd').log"
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") bot.py run *>> $LogFile

