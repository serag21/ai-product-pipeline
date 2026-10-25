param(
    [string]$Product = "products/little-haunts-club-spooky-neighbors-17",
    [string]$RunId = "factory-main",
    [switch]$InstallAtLogon,
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Project virtualenv missing; run .\setup.ps1 first." }
$worker = Join-Path $PSScriptRoot "scripts\product_factory.py"
$productRoot = Join-Path $PSScriptRoot $Product
$serviceOutput = Join-Path $productRoot "factory-service.log"
$serviceError = Join-Path $productRoot "factory-service-error.log"

if ($InstallAtLogon) {
    $taskName = "AI Product Pipeline - $RunId"
    $arguments = "-u `"$worker`" --product `"$Product`" --run-id `"$RunId`""
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Installed and started '$taskName'. It resumes this run at each Windows sign-in."
    Write-Host "Status: $productRoot\factory_status.json"
    Write-Host "Log:    $productRoot\factory.log"
    exit 0
}

if ($Foreground) {
    & $python -u $worker --product $Product --run-id $RunId
    exit $LASTEXITCODE
}

$pidFile = Join-Path $productRoot "factory.pid"
if (Test-Path -LiteralPath $pidFile) {
    $oldPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw), [ref]$oldPid)
    if ($oldPid -gt 0 -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "Factory supervisor is already running (PID $oldPid)."
        exit 0
    }
}
$argLine = "-u `"$worker`" --product `"$Product`" --run-id `"$RunId`""
$process = Start-Process -FilePath $python -ArgumentList $argLine -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $serviceOutput -RedirectStandardError $serviceError
Set-Content -LiteralPath $pidFile -Value $process.Id -NoNewline
Write-Host "Started persistent local product factory (PID $($process.Id))."
Write-Host "It waits for vision config, then resumes automatically through packaging and QA."
Write-Host "Status: $productRoot\factory_status.json"
Write-Host "Log:    $productRoot\factory.log"
