[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$PythonVenvRoot,
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $ScriptRoot }
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
if (-not $PythonVenvRoot) { $PythonVenvRoot = Join-Path $SourceRoot ".venv-brain" }
if (-not $OutputPath) { $OutputPath = Join-Path $SourceRoot "Omega.exe" }
$PythonVenvRoot = [System.IO.Path]::GetFullPath($PythonVenvRoot)
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

$python = Join-Path $PythonVenvRoot "Scripts\python.exe"
$entrypoint = Join-Path $SourceRoot "scripts\omega_launcher.py"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Portable PyInstaller is missing: $python"
}
if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw "Portable launcher source is missing: $entrypoint"
}

$buildRoot = Join-Path $SourceRoot ".launcher-build"
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
$outputParent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Path $outputParent -Force | Out-Null

# The wizard loads no ML stack at run time; the excludes stop PyInstaller from
# following function-level imports deep in the product into torch/scipy/etc.
# Mirrors scripts/build_launcher_linux.sh.
$excludedModules = @(
    "torch", "torchaudio", "torchvision", "transformers", "tokenizers", "accelerate",
    "safetensors", "huggingface_hub", "scipy", "sklearn", "pandas", "numpy", "numba",
    "llvmlite", "spacy", "thinc", "librosa", "soundfile", "cv2", "onnx", "onnxruntime",
    "sympy", "matplotlib", "pyarrow", "fastapi", "uvicorn", "starlette", "playwright",
    "lxml", "reportlab", "xhtml2pdf", "mypy", "bandit"
)
$excludeArgs = foreach ($module in $excludedModules) { "--exclude-module"; $module }

# pyvenv.cfg carries a relative home, so the venv resolves its base interpreter
# only from the product root.
Push-Location -LiteralPath $SourceRoot
try {
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name Omega `
        --hidden-import runtime.cloud_providers `
        --hidden-import scripts.lmstudio_updater `
        --hidden-import cryptography.hazmat.primitives.ciphers.aead `
        @excludeArgs `
        --distpath $outputParent `
        --workpath $buildRoot `
        --specpath $buildRoot `
        $entrypoint
    if ($LASTEXITCODE -ne 0) {
        throw "Portable Omega startup executable build failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$built = Join-Path $outputParent "Omega.exe"
if (-not (Test-Path -LiteralPath $built -PathType Leaf)) {
    throw "PyInstaller completed without producing the startup executable: $built"
}
if ($built -ne $OutputPath) {
    Move-Item -LiteralPath $built -Destination $OutputPath -Force
}
if (Test-Path -LiteralPath $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
Write-Host "Portable Omega startup executable: $OutputPath"
