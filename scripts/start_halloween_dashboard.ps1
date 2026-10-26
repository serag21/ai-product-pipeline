param(
    [string]$Product = "products\little-haunts-club-spooky-neighbors-17",
    [string]$RunId = "factory-main",
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$dashboard = Join-Path $repo "scripts\human_approval_dashboard.py"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$productRoot = if ([IO.Path]::IsPathRooted($Product)) { $Product } else { Join-Path $repo $Product }
$runDir = Join-Path (Join-Path $productRoot "review_runs") $RunId
if (-not (Test-Path -LiteralPath $python)) { throw "Project Python environment was not found at $python" }
if (-not (Test-Path -LiteralPath (Join-Path $productRoot "prompts.json"))) { throw "Product manifest was not found under $productRoot" }
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$wantedProduct = (Get-Content -LiteralPath (Join-Path $productRoot "prompts.json") -Raw | ConvertFrom-Json).product_id
$portToUse = $Port
$existingDashboard = $null
while ($true) {
    $listener = Get-NetTCPConnection -LocalPort $portToUse -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { break }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $command = [string]$process.CommandLine
    if ($command.IndexOf($dashboard, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $command.IndexOf($RunId, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $command.IndexOf($productRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
        Stop-Process -Id $listener.OwningProcess -Force
        $deadline = (Get-Date).AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 250
            $listener = Get-NetTCPConnection -LocalPort $portToUse -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        } while ($listener -and (Get-Date) -lt $deadline)
        if ($listener) { throw "The existing Little Haunts dashboard did not release port $portToUse." }
        break
    }
    # The occupied port belongs to another local service. Leave it running and
    # find a free localhost port rather than stopping an unrelated application.
    $portToUse++
}

$url = "http://127.0.0.1:$portToUse/"
$stdout = Join-Path $runDir "dashboard-server.log"
$stderr = Join-Path $runDir "dashboard-server-error.log"
$argumentLine = "`"$dashboard`" --product `"$productRoot`" --run-id `"$RunId`" --port $portToUse"
$server = Start-Process -FilePath $python -ArgumentList $argumentLine -WorkingDirectory $repo `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 250
    try {
        $state = Invoke-RestMethod -Uri ($url + "api/state") -TimeoutSec 2
        if ($state.product.product_id -eq $wantedProduct -and $state.product.run_id -eq $RunId) {
            $ready = $true
            break
        }
    } catch { }
    if ($server.HasExited) { break }
}
if (-not $ready) {
    $diagnostic = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { "No server error log was created." }
    throw "Little Haunts dashboard did not become ready. Check $stderr. $diagnostic"
}

@{ pid = $server.Id; port = $portToUse; url = $url; product_id = $wantedProduct; run_id = $RunId; started_at = (Get-Date).ToString("o") } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runDir "dashboard_server.json") -Encoding UTF8
if (-not $NoBrowser) { Start-Process $url }
Write-Output "Little Haunts dashboard ready: $url (PID $($server.Id))"
