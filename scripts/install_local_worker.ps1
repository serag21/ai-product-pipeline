$ErrorActionPreference = "Stop"

$repoUrl = "https://github.com/serag21/ai-product-pipeline.git"
$installRoot = "D:\AIProductPipelineWorker"
$stateDir = Join-Path $env:LOCALAPPDATA "AIProductPipelineWorker"
$taskName = "AI Product Pipeline Worker"

Write-Host "== AI Product Pipeline background worker setup =="
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git was not found on PATH." }
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python launcher 'py' was not found." }
if (-not (Test-Path "D:\")) { throw "D: drive was not found; this installer is configured to keep generated work off the nearly-full C: drive." }

if (-not (Test-Path (Join-Path $installRoot ".git"))) {
    if (Test-Path $installRoot) {
        $existing = Get-ChildItem -Force $installRoot -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $existing) { throw "$installRoot already exists and is not a Git checkout. Nothing was deleted." }
    } else {
        New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    }
    Write-Host "Cloning the isolated worker checkout to $installRoot ..."
    & git clone $repoUrl $installRoot
    if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
} else {
    $origin = (& git -C $installRoot remote get-url origin).Trim()
    if ($origin -notmatch "serag21/ai-product-pipeline") { throw "Existing checkout at $installRoot has an unexpected origin: $origin" }
}

Write-Host "Updating the isolated worker checkout..."
& git -C $installRoot fetch origin main
if ($LASTEXITCODE -ne 0) { throw "Could not fetch main from GitHub." }
& git -C $installRoot checkout -B main origin/main
if ($LASTEXITCODE -ne 0) { throw "Could not switch the worker checkout to origin/main." }

$python = Join-Path $installRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating the worker virtual environment..."
    & py -3.12 -m venv (Join-Path $installRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Python 3.12 was not found; trying the default Python installation..."
        & py -m venv (Join-Path $installRoot ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create the Python virtual environment." }
    }
}
$pythonw = Join-Path $installRoot ".venv\Scripts\pythonw.exe"

Write-Host "Installing the existing pipeline dependencies..."
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $python -m pip install -r (Join-Path $installRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Pipeline dependency installation failed." }

Write-Host "Checking worker configuration and GitHub connectivity..."
& $python (Join-Path $installRoot "scripts\local_worker.py") --check --state-dir $stateDir
if ($LASTEXITCODE -ne 0) { throw "The worker self-check failed. See the output above." }

if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
$workerScript = Join-Path $installRoot "scripts\local_worker.py"
$runner = if (Test-Path $pythonw) { $pythonw } else { $python }
$arguments = '"{0}" --poll-seconds 90 --state-dir "{1}"' -f $workerScript, $stateDir
$action = New-ScheduledTaskAction -Execute $runner -Argument $arguments -WorkingDirectory $installRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 365) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Write-Host "Registering and starting the background task..."
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Poll owner-created AI Product Pipeline jobs, run approved local ComfyUI tasks, and publish review artifacts." -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Worker task registered: $taskName"
Write-Host "Worker checkout: $installRoot"
Write-Host "Logs: $(Join-Path $stateDir 'worker.log')"
Write-Host "State: $(Join-Path $stateDir 'state.json')"
Write-Host "ComfyUI can be started normally; queued generation jobs will wait until its API responds."
Write-Host "The worker only accepts allow-listed GitHub jobs created by serag21; issue content is never executed as shell."
Write-Host "Ready. In ChatGPT, ask for a job to be queued and the assistant can create the trigger issue."
