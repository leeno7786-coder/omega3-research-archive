[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,
    [string]$ChecksumPath,
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Archive = [System.IO.Path]::GetFullPath($Archive)
if (-not $InstallDir) {
    $InstallDir = Join-Path (Split-Path -Parent $Archive) "Omega"
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
if (-not $ChecksumPath) { $ChecksumPath = "$Archive.sha256" }
$ChecksumPath = [System.IO.Path]::GetFullPath($ChecksumPath)

function Assert-File([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
}

function Assert-SafeSibling([string]$Anchor, [string]$Candidate, [string]$ExpectedPrefix) {
    $parent = [System.IO.Path]::GetFullPath((Split-Path -Parent $Anchor)).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $full = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $full.StartsWith($parent, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not (Split-Path -Leaf $full).StartsWith($ExpectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing destructive installer operation outside the exact Omega target: $full"
    }
}

function Remove-SafeSibling([string]$Anchor, [string]$Candidate, [string]$ExpectedPrefix) {
    Assert-SafeSibling $Anchor $Candidate $ExpectedPrefix
    if (Test-Path -LiteralPath $Candidate) {
        Remove-Item -LiteralPath $Candidate -Recurse -Force
    }
}

function Copy-TreeContents([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        Copy-Item -LiteralPath $item.FullName -Destination $Destination -Recurse -Force
    }
}

Assert-File $Archive "Omega release archive"
Assert-File $ChecksumPath "Omega checksum file"
$expected = ((Get-Content -LiteralPath $ChecksumPath -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
$actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expected -ne $actual) { throw "Omega archive checksum mismatch" }

$leaf = Split-Path -Leaf $InstallDir
if (-not $leaf -or $InstallDir -eq [System.IO.Path]::GetPathRoot($InstallDir)) {
    throw "InstallDir must name a dedicated Omega product directory"
}
$parent = Split-Path -Parent $InstallDir
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$pending = Join-Path $parent "$leaf.pending-$PID"
$previous = Join-Path $parent "$leaf.previous-$PID"
$scratch = Join-Path $parent ".$leaf.install-$PID"
$stateBackup = Join-Path $scratch "preserved-state"
$preservedPaths = @(
    "Omega\guardian\state",
    "Omega\guardian\ledger",
    "Omega\memory\000_identity",
    "Omega\memory\100_personas",
    "Omega\memory\200_knowledge",
    "Omega\memory\300_reasoning",
    "Omega\memory\400_situational",
    "Omega\memory\500_tools",
    "Omega\memory\600_journal",
    "Omega\memory\700_vision",
    "Omega\memory\800_coding",
    "Omega\memory\900_temporal",
    "Omega\memory\1000_workflows",
    "Omega\memory\1100_dev",
    "Omega\memory\source_ledger"
)

Assert-SafeSibling $InstallDir $pending "$leaf.pending-"
Assert-SafeSibling $InstallDir $previous "$leaf.previous-"
Assert-SafeSibling $InstallDir $scratch ".$leaf.install-"
Remove-SafeSibling $InstallDir $pending "$leaf.pending-"
Remove-SafeSibling $InstallDir $previous "$leaf.previous-"
Remove-SafeSibling $InstallDir $scratch ".$leaf.install-"
New-Item -ItemType Directory -Path $scratch -Force | Out-Null

$movedExisting = $false
try {
    Expand-Archive -LiteralPath $Archive -DestinationPath $scratch -Force
    Assert-File (Join-Path $scratch "Omega.exe") "portable startup executable in archive"
    Assert-File (Join-Path $scratch "Omega\boot.py") "Omega boot entrypoint in archive"

    if (Test-Path -LiteralPath $InstallDir -PathType Container) {
        foreach ($relative in $preservedPaths) {
            $existing = Join-Path $InstallDir $relative
            if (Test-Path -LiteralPath $existing -PathType Container) {
                Copy-TreeContents $existing (Join-Path $stateBackup $relative)
            }
        }
    }

    New-Item -ItemType Directory -Path $pending -Force | Out-Null
    Copy-TreeContents $scratch $pending
    if (Test-Path -LiteralPath (Join-Path $pending "preserved-state")) {
        Remove-Item -LiteralPath (Join-Path $pending "preserved-state") -Recurse -Force
    }

    foreach ($relative in $preservedPaths) {
        $saved = Join-Path $stateBackup $relative
        if (Test-Path -LiteralPath $saved -PathType Container) {
            Copy-TreeContents $saved (Join-Path $pending $relative)
        }
    }

    if (Test-Path -LiteralPath $InstallDir) {
        Move-Item -LiteralPath $InstallDir -Destination $previous
        $movedExisting = $true
    }
    Move-Item -LiteralPath $pending -Destination $InstallDir
    if ($movedExisting) { Remove-SafeSibling $InstallDir $previous "$leaf.previous-" }
} catch {
    if ($movedExisting -and -not (Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $previous)) {
        Move-Item -LiteralPath $previous -Destination $InstallDir
    }
    Remove-SafeSibling $InstallDir $pending "$leaf.pending-"
    throw
} finally {
    if (Test-Path -LiteralPath $scratch) {
        Remove-SafeSibling $InstallDir $scratch ".$leaf.install-"
    }
}

Write-Host "Omega installed successfully."
Write-Host "  Product root: $InstallDir"
Write-Host "  HUD launcher: $(Join-Path $InstallDir 'Omega\scripts\hud-only.bat')"
