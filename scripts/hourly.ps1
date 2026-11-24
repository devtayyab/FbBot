# One hourly cycle (scan + post) on Windows, logged to data\logs\YYYY-MM-DD.log
# Task Scheduler: run "only when user is logged on" (the browser window must be able to open)
# and set "Do not start a new instance" so two runs never share a Chrome profile.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path "data\logs" | Out-Null
$LogFile = "data\logs\$(Get-Date -Format 'yyyy-MM-dd').log"
# UTF-8 output (Urdu names, emoji); cmd redirects the raw bytes so the log stays UTF-8.
$env:PYTHONUTF8 = "1"
cmd /c ".venv\Scripts\python.exe bot.py run >> $LogFile 2>&1"
exit $LASTEXITCODE
