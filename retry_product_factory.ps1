param([string]$Product = "products/little-haunts-club-spooky-neighbors-15", [string]$RunId = "factory-main")
$runRoot = Join-Path (Join-Path (Join-Path $PSScriptRoot $Product) "review_runs") $RunId
New-Item -ItemType File -Path (Join-Path $runRoot "retry.request") -Force | Out-Null
Write-Host "Factory retry requested for run '$RunId'."
