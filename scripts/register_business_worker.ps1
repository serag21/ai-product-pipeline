$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Worker = Join-Path $RepoRoot "scripts\business_worker.py"
$TaskName = "Little Haunts Business Worker"

if (-not (Test-Path $Python)) {
    throw "Missing project Python environment at $Python. Run the repository setup first."
}
if (-not (Test-Path $Worker)) {
    throw "Missing worker script at $Worker."
}

$Branch = (& git -C $RepoRoot branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $Branch -ne "main") {
    throw "The canonical checkout must be on branch 'main' before registering the worker. Current branch: $Branch"
}

$Arguments = '"' + $Worker + '" run --poll-seconds 45'
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Watches the approved AI Product Pipeline job queue and runs the deterministic local ComfyUI generation worker." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Registered and started: $TaskName"
Write-Host "The worker runs at logon. It starts ComfyUI if needed, serializes generation jobs, and pushes review sheets to GitHub."
Write-Host "Local state and logs: $env:LOCALAPPDATA\LittleHauntsBusiness"
Write-Host "No Etsy listing is published by this worker."
