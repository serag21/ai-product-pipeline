param(
    [string]$Product,
    [string]$RunId,
    [switch]$Enable,
    [string]$ConfirmPublished
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$example = Get-Content -LiteralPath (Join-Path $repo "config\recurring_production.example.json") -Raw | ConvertFrom-Json
$localPath = Join-Path $repo "config\recurring_production.local.json"
$config = if (Test-Path -LiteralPath $localPath) { Get-Content -LiteralPath $localPath -Raw | ConvertFrom-Json } else { $example }
if ($Product) { $config.product = $Product }
if ($RunId) { $config.run_id = $RunId }

# Scheduling cannot be enabled until every required Halloween launch channel
# has a persisted, verified public URL and the owner supplies an explicit phrase.
$config.enabled = $false
if ($Enable) {
    if ($ConfirmPublished -ne "ALL CHANNELS LIVE") { throw "To activate after release, pass -ConfirmPublished 'ALL CHANNELS LIVE'." }
    if ([string]::IsNullOrWhiteSpace([string]$config.product) -or [string]::IsNullOrWhiteSpace([string]$config.run_id)) {
        throw "Set a future product folder and run ID before enabling the schedule. The Halloween book itself is not a future production target."
    }
    $releaseRun = Join-Path $repo "products\little-haunts-club-spooky-neighbors-17\review_runs\factory-main"
    $channelStatePath = Join-Path $releaseRun "distribution_state.json"
    if (-not (Test-Path -LiteralPath $channelStatePath)) { throw "Halloween channel state is missing; scheduling stays disabled." }
    $channels = (Get-Content -LiteralPath $channelStatePath -Raw | ConvertFrom-Json).channels
    foreach ($name in @("etsy", "kdp", "payhip")) {
        $channel = $channels.$name
        if ($null -eq $channel -or $channel.status -ne "Published" -or -not $channel.public_url_verified -or [string]::IsNullOrWhiteSpace([string]$channel.public_url)) {
            throw "Halloween channel '$name' is not Published with a verified public URL; scheduling stays disabled."
        }
    }
    $productRoot = if ([IO.Path]::IsPathRooted([string]$config.product)) { [string]$config.product } else { Join-Path $repo ([string]$config.product) }
    if (-not (Test-Path -LiteralPath (Join-Path $productRoot "prompts.json"))) { throw "The future product folder must already contain prompts.json." }
    $config.enabled = $true
}
$config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $localPath -Encoding UTF8

$taskName = [string]$config.task_name
$runner = Join-Path $repo "scripts\run_biweekly_production.ps1"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`"" -WorkingDirectory $repo
$start = (Get-Date).AddMinutes(15)
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Days 14) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Hours 24)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if ($config.enabled) {
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    Write-Host "Biweekly production schedule enabled. It targets '$($config.product)' / '$($config.run_id)' and resumes the saved factory state."
} else {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Write-Host "Biweekly production task registered but DISABLED. No book was started."
    Write-Host "It will not enable until Halloween Etsy, KDP, and Payhip pages each have a verified public URL and you explicitly pass -Enable -ConfirmPublished 'ALL CHANNELS LIVE'."
}
