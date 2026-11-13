[CmdletBinding(DefaultParameterSetName = "Archive")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Archive")]
    [string]$Archive,
    [Parameter(ParameterSetName = "Archive")]
    [string]$ChecksumPath,
    [Parameter(Mandatory = $true, ParameterSetName = "ReleaseRoot")]
    [string]$ReleaseRoot,
    [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$temporaryRoot = $null

function Assert-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Portable release is missing required file: $Path"
    }
}

function Assert-Directory([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Portable release is missing required directory: $Path"
    }
}

function Get-PortableSha256([string]$Path) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Get-PortableRelativePath([string]$Base, [string]$Candidate) {
    # Windows PowerShell 5 lacks the newer relative-path helper.
    $baseFull = [System.IO.Path]::GetFullPath($Base).TrimEnd('\', '/')
    $candidateFull = [System.IO.Path]::GetFullPath($Candidate)
    if ($candidateFull.Equals($baseFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return "."
    }
    $prefix = $baseFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $candidateFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the relative-path base: $candidateFull"
    }
    return $candidateFull.Substring($prefix.Length)
}

function Assert-PortFree([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $Port is already in use; refusing to disturb an existing service" }
}

function Wait-Http([string]$Url, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing
            if ($response.StatusCode -eq 200) { return }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw "Timed out waiting for $Url"
}

function Test-HostAbsolutePath([string]$Text) {
    # Match drive-rooted paths, but do not mistake URI schemes such as
    # ``s://`` for a Windows drive path.
    if ($Text -cmatch '(?<![A-Za-z0-9_?])[A-Za-z]:(?:\\+|/(?!/))(?=[A-Za-z0-9._ -]+(?:\\+|/))') { return $true }
    if ($Text -cmatch '(?<![A-Za-z0-9_/:])/(home|Users|root|tmp)/[^\s"'']+') { return $true }
    return $false
}

function Assert-NoAbsolutePaths([string]$Root) {
    $omega = Join-Path $Root "Omega"
    $textExtensions = @(
        ".bat", ".cmd", ".cfg", ".conf", ".css", ".csv", ".html", ".ini",
        ".js", ".json", ".mjs", ".md", ".pth", ".ps1", ".py", ".sh", ".toml",
        ".ts", ".txt", ".xml", ".yaml", ".yml"
    )
    $targets = @(
        Get-ChildItem -LiteralPath $omega -Recurse -File -ErrorAction Stop |
            Where-Object {
                $_.Extension -in $textExtensions -and
                # Third-party package sources and their documentation are not
                # Omega-owned configuration; upstream examples commonly show
                # illustrative absolute paths.
                $_.FullName -notmatch '[\\/](?:\.venv-brain|python-3\.13-embed|\.venv-vitis|python-3\.12-embed)[\\/]Lib[\\/]site-packages[\\/]' -and
                $_.FullName -notmatch '[\\/]vendor[\\/]'
            } |
            ForEach-Object { $_.FullName }
        (Join-Path $Root "release-manifest.json")
    )
    foreach ($launcher in @(
        (Join-Path $Root "LM Studio\Launch Portable.cmd"),
        (Join-Path $Root "LM-Studio-Linux\run-ai.sh")
    )) {
        if (Test-Path -LiteralPath $launcher -PathType Leaf) { $targets += $launcher }
    }
    foreach ($path in $targets | Sort-Object -Unique) {
        $text = Get-Content -LiteralPath $path -Raw -ErrorAction Stop
        if (Test-HostAbsolutePath $text) {
            $relative = Get-PortableRelativePath $Root $path
            throw "Portable release contains an absolute path in $relative"
        }
    }
}

function Assert-FactoryMemory([string]$Root) {
    $omega = Join-Path $Root "Omega"
    $memory = Join-Path $omega "memory"
    $operatorLobes = @(
        "100_personas", "200_knowledge", "300_reasoning", "400_situational",
        "500_tools", "600_journal", "700_vision", "800_coding", "900_temporal",
        "1000_workflows", "1100_dev"
    )
    foreach ($lobe in $operatorLobes) {
        if (Test-Path -LiteralPath (Join-Path $memory $lobe)) {
            throw "Portable release contains factory memory residue: memory/$lobe"
        }
    }

    $identity = Join-Path $memory "000_identity"
    if (Test-Path -LiteralPath $identity -PathType Container) {
        foreach ($file in Get-ChildItem -LiteralPath $identity -Recurse -File) {
            $relative = (Get-PortableRelativePath $omega $file.FullName).Replace('\', '/')
            if ($relative -notmatch '^memory/000_identity/constitution/') {
                throw "Portable release contains factory memory residue: $relative"
            }
        }
    }

    $indexes = Join-Path $memory "indexes"
    if (Test-Path -LiteralPath $indexes -PathType Container) {
        foreach ($file in Get-ChildItem -LiteralPath $indexes -Recurse -File) {
            if ($file.Name -notin @("label_map.json", "README.md")) {
                $relative = (Get-PortableRelativePath $omega $file.FullName).Replace('\', '/')
                throw "Portable release contains factory memory residue: $relative"
            }
        }
    }

    foreach ($rootName in @("dme", "integrity", "projection", "source_ledger")) {
        $store = Join-Path $memory $rootName
        if (-not (Test-Path -LiteralPath $store -PathType Container)) { continue }
        foreach ($file in Get-ChildItem -LiteralPath $store -Recurse -File) {
            if ($file.Extension -ne ".py") {
                $relative = (Get-PortableRelativePath $omega $file.FullName).Replace('\', '/')
                throw "Portable release contains factory memory residue: $relative"
            }
        }
    }

    foreach ($rootName in @("quarantine", "recall_verifier", "sense_registry", "taxonomy_store")) {
        $store = Join-Path $memory $rootName
        if (-not (Test-Path -LiteralPath $store -PathType Container)) { continue }
        $file = Get-ChildItem -LiteralPath $store -Recurse -File | Select-Object -First 1
        if ($file) {
            $relative = (Get-PortableRelativePath $omega $file.FullName).Replace('\', '/')
            throw "Portable release contains factory memory residue: $relative"
        }
    }
}

function Assert-PortableVendorRuntime([string]$Root) {
    $windows = Join-Path $Root "LM Studio"
    if (Test-Path -LiteralPath $windows -PathType Container) {
        Assert-File (Join-Path $windows "LM Studio.exe")
        Assert-File (Join-Path $windows "Launch Portable.cmd")
        foreach ($relative in @(
            "models", "profile", "updates", "extensions", "settings.json", "apps\bionic\settings.json"
        )) {
            if (Test-Path -LiteralPath (Join-Path $windows $relative)) {
                throw "Portable release contains LM Studio user state: LM Studio/$relative"
            }
        }
    }

    $linux = Join-Path $Root "LM-Studio-Linux"
    if (Test-Path -LiteralPath $linux -PathType Container) {
        Assert-File (Join-Path $linux "lm-studio.AppImage")
        Assert-File (Join-Path $linux "run-ai.sh")
        foreach ($relative in @("models", ".data", "updates")) {
            if (Test-Path -LiteralPath (Join-Path $linux $relative)) {
                throw "Portable release contains Linux LM Studio user state: LM-Studio-Linux/$relative"
            }
        }
    }
}

function Assert-ReleaseSecrets([string]$Root) {
    $forbidden = @()
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction Stop) {
        $relative = (Get-PortableRelativePath $Root $file.FullName).Replace('\', '/')
        # Match the release builder's explicit runtime-code allowance. All
        # other telemetry artifacts still count as operator state.
        if ($relative -in @("Omega/guardian/telemetry/__init__.py", "Omega/guardian/telemetry/writer.py")) {
            continue
        }
        if ($relative -match '(^|/)(guardian/(release-secrets|state|logs|ledger|audit|checkpoints|telemetry)|browser/(?:[^/]+/)*(session|sessions|profile|profiles)|browser-agent/(?:[^/]+/)*(session|sessions|profile|profiles)|browser_agent_extension/(?:[^/]+/)*(session|sessions|profile|profiles))(/|\.)' -or
            $relative -match '(^|/)guardian/state/mcp/authorization/((oauth[-_.]?vault)|(tokens?)|(refresh[-_.]?tokens?)|(access[-_.]?tokens?)|(connections?)|(grants?)|(credentials?)|(secrets?))(?:[-_.][^/]*)?$') {
            $forbidden += $relative
            continue
        }
        # Inspect only small text-like files; never print their contents.
        if ($file.Length -gt 4MB -or $file.Extension.ToLowerInvariant() -notin @('.json', '.yaml', '.yml', '.toml', '.env', '.sealed')) { continue }
        try {
            $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop
            if ($text -match '(?i)\"(?:access|refresh)[_-]?token\"\s*:|(?i)\"passphrase\"\s*:') {
                $forbidden += $relative
            }
        } catch { }
    }
    if ($forbidden.Count -gt 0) {
        throw "Portable release contains forbidden credential/state artifact(s): $($forbidden -join ', ')"
    }
}

function Assert-Manifest([string]$Root) {
    $manifestPath = Join-Path $Root "release-manifest.json"
    Assert-File $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.product -ne "Omega" -or $manifest.platform -ne "windows-x64") {
        throw "Release manifest identifies the wrong product or platform"
    }
    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        if ([System.IO.Path]::IsPathRooted($relative) -or $relative.Contains("..")) {
            throw "Release manifest contains an unsafe path: $relative"
        }
        $candidate = Join-Path $Root $relative
        Assert-File $candidate
        $actual = Get-PortableSha256 $candidate
        if ($actual -ne [string]$entry.sha256) {
            throw "Release manifest checksum mismatch: $relative"
        }
        if ((Get-Item -LiteralPath $candidate).Length -ne [int64]$entry.size) {
            throw "Release manifest size mismatch: $relative"
        }
    }
}

function Assert-VitisLane([string]$Root) {
    # The NPU provider child's own cp312 stack (runtime/vitis_lane.py). It must
    # be complete and bound only by relative paths, exactly like the brain venv.
    $venv = Join-Path $Root "Omega\.venv-vitis"
    $embed = Join-Path $Root "Omega\python-3.12-embed"
    foreach ($relative in @("python.exe", "python312.dll", "python312._pth")) {
        Assert-File (Join-Path $embed $relative)
    }
    foreach ($relative in @("Scripts\python.exe", "Scripts\python312.dll", "Scripts\python312._pth", "pyvenv.cfg", "omega-vitis-lane.json")) {
        Assert-File (Join-Path $venv $relative)
    }
    $cfg = Get-Content -LiteralPath (Join-Path $venv "pyvenv.cfg") -Raw
    if ($cfg -notmatch '(?m)^home = \.\.\\python-3\.12-embed\s*$') {
        throw "Portable vitis venv does not point at its sibling python-3.12-embed"
    }
    $pth = Get-Content -LiteralPath (Join-Path $venv "Scripts\python312._pth")
    $expectedPth = @("python312.zip", ".", "..\Lib\site-packages", "..\..", "import site")
    if (($pth -join "`n") -ne ($expectedPth -join "`n")) {
        throw "Portable vitis venv path file does not bind its site-packages and Omega locally"
    }
    $embedPth = @(Get-Content -LiteralPath (Join-Path $embed "python312._pth") | ForEach-Object { $_.Trim() })
    if ($embedPth -contains "import site") {
        throw "Portable vitis embed enables import site; the embed must stay a base"
    }
    $provider = @(Get-ChildItem -LiteralPath (Join-Path $venv "Lib\site-packages") -Directory -Filter "onnxruntime_vitisai-*.dist-info" -ErrorAction SilentlyContinue)
    if ($provider.Count -ne 1) {
        throw "Portable vitis lane is missing onnxruntime_vitisai"
    }
    foreach ($item in Get-ChildItem -LiteralPath (Join-Path $venv "Scripts") -Force) {
        if ($item.Name -ieq "python.exe" -or
            $item.Extension -in @(".dll", ".pyd", ".zip", "._pth")) {
            continue
        }
        throw "Portable vitis venv contains a non-runtime interpreter entry point: $($item.FullName)"
    }
}

function Assert-StaticRelease([string]$Root) {
    $requiredFiles = @(
        "Omega.exe",
        "Omega\.venv-brain\Scripts\python.exe",
        "Omega\python-3.13-embed\python.exe",
        "Omega\scripts\hud-only.bat",
        "Omega\scripts\shutdown.py",
        "release-manifest.json"
    )
    foreach ($relative in $requiredFiles) { Assert-File (Join-Path $Root $relative) }
    foreach ($relative in @("Omega\models", "Omega\runtimes", "Omega\guardian\logs", "Omega\guardian\telemetry")) {
        Assert-Directory (Join-Path $Root $relative)
    }

    $cfg = Get-Content -LiteralPath (Join-Path $Root "Omega\.venv-brain\pyvenv.cfg") -Raw
    if ($cfg -notmatch '(?m)^home = \.\.\\python-3\.13-embed\s*$') {
        throw "Portable venv does not point at its sibling embedded Python"
    }
    $pth = Get-Content -LiteralPath (Join-Path $Root "Omega\.venv-brain\Scripts\python313._pth")
    $expectedPth = @("python313.zip", ".", "..\Lib\site-packages", "..\..", "import site")
    if (($pth -join "`n") -ne ($expectedPth -join "`n")) {
        throw "Portable venv path file does not bind site-packages and Omega locally"
    }
    $embeddedScripts = Join-Path $Root "Omega\python-3.13-embed\Scripts"
    if (Test-Path -LiteralPath $embeddedScripts) {
        throw "Portable embedded runtime contains build-only Scripts directory: $embeddedScripts"
    }
    $venvScripts = Join-Path $Root "Omega\.venv-brain\Scripts"
    foreach ($item in Get-ChildItem -LiteralPath $venvScripts -Force) {
        if ($item.Name -ieq "python.exe" -or
            $item.Extension -in @(".dll", ".pyd", ".zip", "._pth")) {
            continue
        }
        throw "Portable venv contains a non-runtime interpreter entry point: $($item.FullName)"
    }
    if (Test-Path -LiteralPath (Join-Path $Root "Omega\.mcp.json")) {
        throw "Developer .mcp.json must not ship in the portable release"
    }

    Assert-VitisLane $Root
    Assert-NoAbsolutePaths $Root
    Assert-ReleaseSecrets $Root
    Assert-FactoryMemory $Root
    Assert-PortableVendorRuntime $Root
    Assert-Manifest $Root
}

try {
    if ($PSCmdlet.ParameterSetName -eq "Archive") {
        $Archive = [System.IO.Path]::GetFullPath($Archive)
        Assert-File $Archive
        if (-not $ChecksumPath) { $ChecksumPath = "$Archive.sha256" }
        $ChecksumPath = [System.IO.Path]::GetFullPath($ChecksumPath)
        Assert-File $ChecksumPath
        $expected = ((Get-Content -LiteralPath $ChecksumPath -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
        $actual = Get-PortableSha256 $Archive
        if ($expected -ne $actual) { throw "Archive checksum mismatch" }

        $archiveDirectory = [System.IO.Path]::GetFullPath((Split-Path -Parent $Archive))
        $temporaryRoot = Join-Path $archiveDirectory ".omega-verify-$PID-$(Get-Random)"
        New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
        Expand-Archive -LiteralPath $Archive -DestinationPath $temporaryRoot -Force
        $ReleaseRoot = $temporaryRoot
    }

    $ReleaseRoot = [System.IO.Path]::GetFullPath($ReleaseRoot)
    Assert-StaticRelease $ReleaseRoot

    if (-not $SkipLaunch) {
        Assert-PortFree 13310

        $omega = Join-Path $ReleaseRoot "Omega"
        $python = Join-Path $omega ".venv-brain\Scripts\python.exe"
        $oldPath = $env:PATH
        $hudProcess = $null
        try {
            $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
            & $python -I -c "import pathlib,cryptography,numpy,onnxruntime,spacy,sys,torch; from core.paths import OMEGA_ROOT; assert pathlib.Path(sys.executable).resolve().is_relative_to(pathlib.Path(OMEGA_ROOT).resolve()); print(spacy.__version__, numpy.__version__, onnxruntime.__version__, torch.__version__)"
            if ($LASTEXITCODE -ne 0) { throw "Staged Python failed its isolated native-runtime imports" }
            # Import evidence for the NPU lane only: provider registration is
            # not placement, which only a profiled run on a supported driver proves.
            $vitisPython = Join-Path $omega ".venv-vitis\Scripts\python.exe"
            & $vitisPython -I -c "import pathlib,sys,onnxruntime; from core.paths import OMEGA_ROOT; assert pathlib.Path(sys.executable).resolve().is_relative_to(pathlib.Path(OMEGA_ROOT).resolve()); assert 'VitisAIExecutionProvider' in onnxruntime.get_available_providers(); print(sys.version.split()[0], onnxruntime.__version__)"
            if ($LASTEXITCODE -ne 0) { throw "Staged Vitis lane failed its isolated provider import" }

            $hudProcess = Start-Process -FilePath $python -ArgumentList @(
                (Join-Path $omega "hud\standalone.py"), "--port", "13310", "--no-boot"
            ) -WorkingDirectory $omega -WindowStyle Hidden -PassThru
            Wait-Http "http://127.0.0.1:13310/healthz" 60
        } finally {
            if ($hudProcess -and -not $hudProcess.HasExited) {
                Stop-Process -Id $hudProcess.Id -Force -ErrorAction SilentlyContinue
            }
            $env:PATH = $oldPath
        }

        Start-Sleep -Milliseconds 500
        Assert-PortFree 13310
    }

    Write-Host "Portable Omega verification passed: $ReleaseRoot"
} finally {
    if ($temporaryRoot -and (Test-Path -LiteralPath $temporaryRoot)) {
        $archiveDirectory = [System.IO.Path]::GetFullPath((Split-Path -Parent $Archive)).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        $resolved = [System.IO.Path]::GetFullPath($temporaryRoot)
        if ($resolved.StartsWith($archiveDirectory, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolved).StartsWith(".omega-verify-$PID-")) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
