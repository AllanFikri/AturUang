<#
run_logged.ps1 — jalankan perintah Python dan append output + timestamp ke session_log.md

Usage:
    .\scripts\run_logged.ps1 -Task "nama-task" -Cmd "python scripts\foo.py --arg"
    .\scripts\run_logged.ps1 -Task "shopeepay-v4-dryrun" -Cmd "python scripts\import_shopeepay_ocr_v4.py --dir H:\..."

Semua output (stdout + stderr) di-capture dan di-append ke session_log.md
dengan timestamp YYYY-MM-DD HH:MM:SS.
#>
param(
    [Parameter(Mandatory=$true)][string]$Task,
    [Parameter(Mandatory=$true)][string]$Cmd,
    [string]$LogFile = "C:\A User Main Storage\Downloads\aturuang_scratch\session_log.md"
)

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Header log
Add-Content -Path $LogFile -Value "`n---`n" -Encoding UTF8
Add-Content -Path $LogFile -Value "### [$ts] TASK: $Task" -Encoding UTF8
Add-Content -Path $LogFile -Value "Cmd: $Cmd" -Encoding UTF8
Add-Content -Path $LogFile -Value "" -Encoding UTF8
Add-Content -Path $LogFile -Value '```' -Encoding UTF8

# Jalankan perintah
Write-Host ">>> Jalankan: $Cmd"
$out = Invoke-Expression $Cmd 2>&1 | Out-String
Write-Host $out

# Append output
Add-Content -Path $LogFile -Value $out -Encoding UTF8
Add-Content -Path $LogFile -Value '```' -Encoding UTF8

# Footer info
$exitCode = $LASTEXITCODE
Add-Content -Path $LogFile -Value "exit_code: $exitCode" -Encoding UTF8

Write-Host ""
Write-Host "Logged: $Task @ $ts"
