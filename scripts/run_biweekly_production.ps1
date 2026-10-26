$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repo "config\recurring_production.local.json"
if (-not (Test-Path -LiteralPath $configPath)) { exit 0 }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if (-not $config.enabled) { exit 0 }
if ([string]::IsNullOrWhiteSpace([string]$config.product) -or [string]::IsNullOrWhiteSpace([string]$config.run_id)) {
    throw "Biweekly production is enabled without a product path and run ID; no work was started."
}

# The release gate is checked on every scheduled invocation, not just activation.
$releaseRun = Join-Path $repo "products\little-haunts-club-spooky-neighbors-17\review_runs\factory-main"
$channelStatePath = Join-Path $releaseRun "distribution_state.json"
if (-not (Test-Path -LiteralPath $channelStatePath)) { throw "Halloween distribution is not complete; scheduled production remains gated." }
$channels = (Get-Content -LiteralPath $channelStatePath -Raw | ConvertFrom-Json).channels
foreach ($name in @("etsy", "kdp", "payhip")) {
    $channel = $channels.$name
    if ($null -eq $channel -or $channel.status -ne "Published" -or -not $channel.public_url_verified -or [string]::IsNullOrWhiteSpace([string]$channel.public_url)) {
        throw "Halloween channel '$name' has no verified public listing URL; scheduled production remains gated."
    }
}

$product = [string]$config.product
$productRoot = if ([IO.Path]::IsPathRooted($product)) { $product } else { Join-Path $repo $product }
if (-not (Test-Path -LiteralPath (Join-Path $productRoot "prompts.json"))) {
    throw "Configured future product has no prompts.json: $productRoot"
}
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Project virtualenv is missing." }

# A cross-session file lock complements Task Scheduler's IgnoreNew setting.
# The product_factory Windows mutex also prevents a second worker for this book.
$logRoot = Join-Path $repo "products\worker-runs"
$lockPath = Join-Path $logRoot "scheduled-production.lock"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
try {
    $lock = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch [System.IO.IOException] {
    Write-Output "A scheduled production job already holds the lock; exiting without overlap."
    exit 0
}
try {
    $worker = Join-Path $repo "scripts\product_factory.py"
    $logPath = Join-Path $logRoot ("scheduled-production-{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
    "[$(Get-Date -Format o)] Starting $product / $($config.run_id)" | Set-Content -LiteralPath $logPath -Encoding UTF8
    & $python -u $worker --product $product --run-id ([string]$config.run_id) 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) { throw "Product worker exited with code $LASTEXITCODE. See $logPath" }
    "[$(Get-Date -Format o)] Production run completed." | Add-Content -LiteralPath $logPath -Encoding UTF8
} catch {
    "[$(Get-Date -Format o)] Failed: $_" | Add-Content -LiteralPath $logPath -Encoding UTF8 -ErrorAction SilentlyContinue
    throw
} finally {
    $lock.Dispose()
}
