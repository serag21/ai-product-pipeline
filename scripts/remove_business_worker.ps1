$ErrorActionPreference = "Stop"
$TaskName = "Little Haunts Business Worker"
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host "Task is not registered: $TaskName"
    exit 0
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task: $TaskName"
Write-Host "Worker state and logs were preserved under $env:LOCALAPPDATA\LittleHauntsBusiness"
