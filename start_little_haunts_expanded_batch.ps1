param(
    [string]$Product = "products/little-haunts-club-spooky-neighbors-17",
    [string]$RunId = "factory-main",
    [switch]$InstallAtLogon,
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Worker = Join-Path $RepoRoot "scripts\expanded_little_haunts_batch.py"
$ProductRoot = Join-Path $RepoRoot $Product
$RunDir = Join-Path $ProductRoot "review_runs\$RunId"
$Log = Join-Path $RunDir "expanded_batch.log"
if (-not (Test-Path -LiteralPath $Python)) { throw "Local Python environment is missing: $Python" }
if (-not (Test-Path -LiteralPath $Worker)) { throw "Expanded batch worker is missing: $Worker" }
New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

if ($InstallAtLogon) {
    $TaskName = "Little Haunts Expanded Production - $RunId"
    $Arguments = "-u `"$Worker`" --product `"$Product`" --run-id `"$RunId`""
    $Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $RepoRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Resume the bounded, local Little Haunts candidate production run." -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Installed and started '$TaskName'. It resumes the saved batch at Windows sign-in."
    Write-Host "Progress: $RunDir\candidate_library.json"
    Write-Host "Log:      $Log"
    exit 0
}

if ($Foreground) {
    & $Python -u $Worker --product $Product --run-id $RunId *>> $Log
    exit $LASTEXITCODE
}

$ArgsLine = "-u `"$Worker`" --product `"$Product`" --run-id `"$RunId`""
$Process = Start-Process -FilePath $Python -ArgumentList $ArgsLine -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
Write-Host "Started the resumable Little Haunts production process (PID $($Process.Id))."
Write-Host "Progress: $RunDir\candidate_library.json"
Write-Host "Log:      $Log"
