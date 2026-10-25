param(
    [string]$Product = "products/little-haunts-club-spooky-neighbors-17",
    [string]$RunId = "factory-main",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Local Python environment is missing. Run .\setup.ps1 first."
}
$Url = "http://127.0.0.1:$Port/"
Write-Host "Starting the local human approval dashboard at $Url"
Write-Host "Press Ctrl+C to stop. Decisions are saved in the selected factory run."
Set-Location -LiteralPath $RepoRoot
& $Python "scripts\human_approval_dashboard.py" --product $Product --run-id $RunId --host 127.0.0.1 --port $Port
