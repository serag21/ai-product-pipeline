param(
    [string]$Product = "products/little-haunts-club-targeted-iteration-lab-6",
    [string]$RunId = "targeted-review",
    [int]$MaxAttempts = 2,
    [string]$VisionEndpoint = $env:VISION_API_BASE_URL,
    [string]$VisionModel = $env:VISION_MODEL
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Project virtualenv not found. Run .\setup.ps1 first." }
$worker = Join-Path $PSScriptRoot "scripts\product_review_worker.py"
$workerArgs = @("$worker", "--product", $Product, "--run-id", $RunId, "--max-attempts", "$MaxAttempts")
if ($VisionEndpoint -or $VisionModel) {
    if (-not $VisionEndpoint -or -not $VisionModel) { throw "Set both VISION_API_BASE_URL and VISION_MODEL, or leave both unset for manual review." }
    $workerArgs += @("--vision-endpoint", $VisionEndpoint, "--vision-model", $VisionModel)
    if ($env:VISION_API_KEY) { $workerArgs += @("--vision-api-key", $env:VISION_API_KEY) }
}
& $python @workerArgs
exit $LASTEXITCODE
