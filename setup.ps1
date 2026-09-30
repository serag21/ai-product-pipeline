param()

$ErrorActionPreference = "Stop"

Write-Host "== AI Product Pipeline setup =="

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3.12+ first."
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating Python virtual environment..."
    py -m venv .venv
}

Write-Host "Installing Python dependencies..."
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next:"
Write-Host "  1. Start your existing ComfyUI installation (run_nvidia_gpu.bat)."
Write-Host "  2. Export the Flux.2 workflow with File -> Export Workflow (API)."
Write-Host "  3. Save it as workflows\flux2_coloring_api.json"
Write-Host "  4. Run: .\.venv\Scripts\python.exe scripts\test_comfy.py"
