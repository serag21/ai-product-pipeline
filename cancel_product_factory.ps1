param([string]$Product = "products/little-haunts-club-spooky-neighbors-15")
$productRoot = Join-Path $PSScriptRoot $Product
New-Item -ItemType File -Path (Join-Path $productRoot "cancel.request") -Force | Out-Null
Write-Host "Cancellation requested. The supervisor will stop after saving its current page state."
