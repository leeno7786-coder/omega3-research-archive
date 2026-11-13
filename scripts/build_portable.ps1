[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$PythonEmbedRoot,
    [string]$BrainVenvRoot,
    [string]$VitisEmbedRoot,
    [string]$VitisVenvRoot,
    [string]$LmStudioRoot,
    [string]$LinuxLmStudioRoot,
    [string]$OutputRoot,
    [string]$Version = "dev",
    [switch]$SkipHudBuild,
    [switch]$AllowDirtySource
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $ScriptRoot }
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
if (-not $PythonEmbedRoot) { $PythonEmbedRoot = Join-Path $SourceRoot "python-3.13-embed" }
if (-not $BrainVenvRoot) { $BrainVenvRoot = Join-Path $SourceRoot ".venv-brain" }
if (-not $OutputRoot) { $OutputRoot = Join-Path $SourceRoot "dist" }
# The Vitis lane: the cp312 stack the NPU provider child runs on, built by
# scripts/bootstrap_vitis_lane.py (onnxruntime-vitisai exists only for 3.12).
if (-not $VitisEmbedRoot) { $VitisEmbedRoot = Join-Path $SourceRoot "python-3.12-embed" }
if (-not $VitisVenvRoot) { $VitisVenvRoot = Join-Path $SourceRoot ".venv-vitis" }

$PythonEmbedRoot = [System.IO.Path]::GetFullPath($PythonEmbedRoot)
$BrainVenvRoot = [System.IO.Path]::GetFullPath($BrainVenvRoot)
$VitisEmbedRoot = [System.IO.Path]::GetFullPath($VitisEmbedRoot)
$VitisVenvRoot = [System.IO.Path]::GetFullPath($VitisVenvRoot)
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$SsdRoot = Split-Path -Parent $SourceRoot
if (-not $LmStudioRoot) {
    $candidate = Join-Path $SsdRoot "LM Studio"
    if (Test-Path -LiteralPath $candidate -PathType Container) { $LmStudioRoot = $candidate }
}
if (-not $LinuxLmStudioRoot) {
    $candidate = Join-Path $SsdRoot "LM-Studio-Linux"
    if (Test-Path -LiteralPath $candidate -PathType Container) { $LinuxLmStudioRoot = $candidate }
}
if ($LmStudioRoot) { $LmStudioRoot = [System.IO.Path]::GetFullPath($LmStudioRoot) }
if ($LinuxLmStudioRoot) { $LinuxLmStudioRoot = [System.IO.Path]::GetFullPath($LinuxLmStudioRoot) }

function Assert-RequiredDirectory([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Required $Label directory is missing: $Path"
    }
}

function Assert-RequiredFile([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required $Label file is missing: $Path"
    }
}

function Assert-SafeChild([string]$Parent, [string]$Candidate) {
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $candidateFull = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $candidateFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing filesystem mutation outside output root: $candidateFull"
    }
}

function Get-PortableRelativePath([string]$Base, [string]$Candidate) {
    # Windows PowerShell 5 lacks the newer relative-path helper. Keep release
    # tooling runnable on the inbox Windows shell without consulting
    # the host's current directory or any external path library.
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

function Remove-SafeTree([string]$Parent, [string]$Candidate) {
    Assert-SafeChild $Parent $Candidate
    if (Test-Path -LiteralPath $Candidate) {
        Remove-Item -LiteralPath $Candidate -Recurse -Force
    }
}

function Test-SkippedPart([string]$Name) {
    return $Name -in @(
        ".git", ".github", ".agents", ".codex", ".cursor", ".merge-scratch",
        ".pytest_cache", ".ruff_cache", ".cache", "__pycache__", ".codebase-memory",
        "node_modules", "tests",
        "benchmarks", "reports", "tmp-test-state", "unused-test-state",
        "python-embed-cpu", "python-embed-onnx-vitisai"
    )
}

function Test-FactoryMemoryPath([string]$Normalized) {
    # A release starts at factory state.  Keep the founding constitution and
    # memory implementation code, but never copy this checkout's operator
    # conversations, projects, images, journals, projections, or ledgers.
    if ($Normalized -match '^memory/(100_personas|200_knowledge|300_reasoning|400_situational|500_tools|600_journal|700_vision|800_coding|900_temporal|1000_workflows|1100_dev)(/|$)') {
        return $false
    }
    if ($Normalized -match '^memory/000_identity/(?!constitution(/|$))') {
        return $false
    }
    if ($Normalized -match '^memory/models(/|$)') {
        return $false
    }
    if ($Normalized -match '^memory/dme/' -and $Normalized -notmatch '\.py$') {
        return $false
    }
    if ($Normalized -match '^memory/indexes/' -and
        $Normalized -notmatch '^memory/indexes/(label_map\.json|README\.md)$') {
        return $false
    }
    if ($Normalized -match '^memory/(quarantine|recall_verifier|sense_registry|taxonomy_store)(/|$)') {
        return $false
    }
    if ($Normalized -match '^memory/(integrity|projection|source_ledger)/' -and
        $Normalized -notmatch '\.py$') {
        return $false
    }
    return $true
}

function Test-ReleaseRelativePath([string]$Relative) {
    $normalized = $Relative.Replace('\', '/').TrimStart('/')
    if ($normalized -match '^models/onnxs/kokoro(/|$)') { return $false }
    if ($normalized -match '^models/intent/(?:base|router-modernbert-base-[^/]+)(?:/|$)') { return $false }
    $parts = $normalized.Split('/')
    foreach ($part in $parts) {
        if (Test-SkippedPart $part) { return $false }
    }
    if ($normalized -match '^memory/' -and -not (Test-FactoryMemoryPath $normalized)) {
        return $false
    }
    # Factory artifacts never carry operator state or credential material.
    # Keep this independent of Git so source enumeration has one contract.
    # These are runtime modules, not the telemetry data stored beside them.
    if ($normalized -in @("guardian/telemetry/__init__.py", "guardian/telemetry/writer.py")) {
        return $true
    }
    if ($normalized -match '^guardian/(release-secrets|state|logs|ledger|audit|checkpoints|telemetry)(/|$)') {
        return $false
    }
    if ($normalized -match '(^|/)guardian/state/mcp/authorization/((oauth[-_.]?vault)|(tokens?)|(refresh[-_.]?tokens?)|(access[-_.]?tokens?)|(connections?)|(grants?)|(credentials?)|(secrets?))(?:[-_.][^/]*)?$') {
        return $false
    }
    if ($normalized -match '(^|/)(browser|browser-agent|browser_agent_extension)/(?:[^/]*/)*(session|sessions|profile|profiles)(/|\.)') {
        return $false
    }
    if ($normalized -match '(^|/)browser/(session|sessions)(/|$)') {
        return $false
    }
    if ($normalized -match '(^|/)(guardian/(state|logs|ledger|audit|checkpoints|telemetry))(/|$)') {
        return $false
    }
    if ($normalized -match '^pressure_loop/state(/|$)') { return $false }
    if ($normalized -match '^knowledge_extractor/state(/|$)') { return $false }
    if ($normalized -match '^deepsleep_loop/state(/|$)') { return $false }
    if ($normalized -match '(^|/)memory/(taxonomy_store|tmp_writes)(/|$)') { return $false }
    if ($normalized -match '(^|/)memory/indexes/.*\.json$' -and
        $normalized -notmatch '^memory/indexes/label_map\.json$') { return $false }
    if ($normalized -match '(^|/)memory/700_vision/images(/|$)') { return $false }
    if ($normalized -match '(^|/)memory/source_ledger/projection_receipts(/|$)') { return $false }
    if ($normalized -match '^runtimes/npu/(linux|src)(/|$)') { return $false }
    if ($normalized -match '^runtimes/bin/crispasr/vulkan/(crispasr|.*\.so(?:\..*)?)$') { return $false }
    if ($normalized -in @("scripts/install_systemd_user.sh", "scripts/omega_bus_peek.sh")) {
        return $false
    }
    if ($normalized -match '(^|/)[^/]+\.test\.js$') { return $false }
    if ($normalized -in @(".mcp.json", ".coverage", "AGENTS.md", "pytest.ini", "requirements-dev.txt")) {
        return $false
    }

    $allowedRoots = @(
        "auto_loop", "autonomic", "browser_agent", "browser_agent_extension", "cognition", "config", "core",
        "deepsleep_loop", "global_loop", "guardian", "hud", "knowledge_extractor", "organ",
        "mcp_platform", "memory", "meta_loop", "omega_loop",
        "plugin_platform", "policy", "pressure_loop", "runtime", "schema",
        "scripts", "sensory", "skills", "models", "runtimes"
    )
    $allowedRootFiles = @(
        "boot.py", "cognition_chain.py", "install.ps1", "LICENSE", "README.md",
        "PROFILE_README.md", "requirements.txt", "skills-lock.json"
    )
    return $parts[0] -in $allowedRoots -or $normalized -in $allowedRootFiles
}

function Copy-OneFile([string]$Source, [string]$Destination) {
    $parent = Split-Path -Parent $Destination
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Copy-FilteredTree([string]$From, [string]$To) {
    $resolvedFrom = (Resolve-Path -LiteralPath $From).Path
    New-Item -ItemType Directory -Path $To -Force | Out-Null
    $isPortablePythonTree =
        $resolvedFrom -match '(^|[\\/])(?:\.venv-brain|python-3\.13-embed|\.venv-vitis|python-3\.12-embed)([\\/]|$)'
    $isEmbeddedPythonRoot =
        ([System.IO.Path]::GetFileName($resolvedFrom) -in @("python-3.13-embed", "python-3.12-embed"))
    $isEmbeddedPythonScriptsRoot =
        $resolvedFrom -match '[\\/]python-3\.1[23]-embed[\\/]Scripts$'
    $isVenvScriptsRoot =
        $resolvedFrom -match '[\\/](?:\.venv-brain|\.venv-vitis)[\\/]Scripts$'
    $isWindowsNpuRoot =
        ([System.IO.Path]::GetFileName($resolvedFrom) -ieq "npu") -and
        ([System.IO.Path]::GetFileName([System.IO.Path]::GetDirectoryName($resolvedFrom)) -ieq "runtimes")
    $isWindowsCrispAsrRoot =
        $resolvedFrom -match '[\\/]runtimes[\\/]bin[\\/]crispasr[\\/]vulkan$'
    $isOmegaModelOnnxRoot =
        $resolvedFrom -ieq (Join-Path (Join-Path $SourceRoot "models") "onnxs")
    $isOmegaIntentModelsRoot =
        $resolvedFrom -ieq (Join-Path (Join-Path $SourceRoot "models") "intent")
    foreach ($item in Get-ChildItem -LiteralPath $resolvedFrom -Force) {
        if (Test-SkippedPart $item.Name) { continue }
        # Kokoro is retired; shipped voice is Qwen3-TTS from the sibling
        # portable payload declared in config/tts.yaml. Keep old source assets
        # on the builder's SSD, but do not carry an unused speech engine.
        if ($isOmegaModelOnnxRoot -and $item.Name -ieq "kokoro") { continue }
        # The deployed hash-pinned CPU router is models/intent/router. Its
        # ModernBERT base and timestamped training runs are not runtime inputs.
        if ($isOmegaIntentModelsRoot -and
            ($item.Name -ieq "base" -or $item.Name -match '^router-modernbert-base-')) { continue }
        # PyInstaller is a build-time dependency for producing Omega.exe, not
        # a runtime dependency.  Its hook examples contain developer-machine
        # paths and it must not enter the shipped venv.
        if ($isPortablePythonTree -and
            $item.Name -match '^(PyInstaller|_pyinstaller_hooks_contrib|pyinstaller_hooks_contrib-.*\.dist-info|pyinstaller-.*\.dist-info|_pytest|pytest|pytest_.*|pytest-.*\.dist-info|coverage|coverage-.*\.dist-info|ruff|ruff-.*\.dist-info|mypy|mypy-.*\.dist-info|mypy_extensions|mypy_extensions-.*\.dist-info|pycodestyle|pycodestyle-.*\.dist-info|pyflakes|pyflakes-.*\.dist-info|mccabe\.py|mccabe-.*\.dist-info|flake8|flake8-.*\.dist-info|bandit|bandit-.*\.dist-info|pip|pip-.*\.dist-info|setuptools|setuptools-.*\.dist-info|wheel|wheel-.*\.dist-info|_virtualenv.*)$') {
            continue
        }
        if ($isPortablePythonTree -and
            $item.PSIsContainer -and $item.Name -ieq "sboms") {
            continue
        }
        # The embedded Windows interpreter does not use the Unix terminfo/man
        # tree.  It contains dangling symlinks that Windows PowerShell cannot
        # copy, so exclude only the top-level tree from the embedded runtime.
        if ($isEmbeddedPythonRoot -and $item.PSIsContainer -and $item.Name -ieq "share") {
            continue
        }
        # The embedded bundle's Scripts directory contains only pip and
        # virtualenv build entry points.  The venv supplies the runtime
        # interpreter separately, so this directory is not a shipped
        # dependency.
        if ($isEmbeddedPythonRoot -and $item.PSIsContainer -and $item.Name -ieq "Scripts") {
            continue
        }
        # Runtime starts the venv with python.exe and imports its services as
        # modules.  Console wrappers (pip, pytest, ruff, uvicorn, and the
        # third-party CLIs) are developer tooling, not product dependencies.
        # Keep the interpreter's adjacent binary support files because the
        # Windows executable resolves its DLLs/PYDs beside itself.
        if ($isVenvScriptsRoot -and
            -not ($item.Name -ieq "python.exe" -or
                $item.Extension -in @(".dll", ".pyd", ".zip", "._pth"))) {
            continue
        }
        # The Windows product carries the Windows VitisAI/DirectML payload.  The
        # sibling Linux provider tree is a separate host artifact and must never
        # inflate or contaminate the Windows release.
        if ($isWindowsNpuRoot -and $item.PSIsContainer -and $item.Name -ieq "linux") {
            continue
        }
        if ($isWindowsNpuRoot -and $item.PSIsContainer -and $item.Name -ieq "src") {
            continue
        }
        # The Windows release uses the CrispASR .exe and DLL payload.  The
        # same source directory may also contain the POSIX binary and shared
        # objects; they are not loadable on Windows and must not enter the
        # Windows artifact.
        if ($isWindowsCrispAsrRoot -and
            ($item.Name -ieq "crispasr" -or $item.Name -like "*.so*")) {
            continue
        }
        $destination = Join-Path $To $item.Name
        if ($item.PSIsContainer) {
            Copy-FilteredTree $item.FullName $destination
        } else {
            Copy-OneFile $item.FullName $destination
        }
    }
}

function Copy-EttinSnapshot([string]$SourceRoot, [string]$OmegaStage) {
    $source = Join-Path $SourceRoot "memory\models\models--tasksource--ettin-17m-embed\snapshots"
    Assert-RequiredDirectory $source "Ettin encoder snapshot"
    Copy-FilteredTree $source (Join-Path $OmegaStage "memory\models\models--tasksource--ettin-17m-embed\snapshots")
}

function Get-QwenTtsContract([string]$SourceRoot) {
    # config/tts.yaml is the single source of truth for where the speech payload
    # lives.  The builder reads that contract rather than repeating it, so the
    # shipped config and the staged weights cannot drift apart into a release
    # whose boot greeting has nothing to speak with.
    $configPath = Join-Path $SourceRoot "config\tts.yaml"
    Assert-RequiredFile $configPath "qwen tts configuration"
    $contract = @{}
    $inQwen = $false
    foreach ($line in Get-Content -LiteralPath $configPath) {
        if ($line -match '^\s{2}qwen:\s*$') { $inQwen = $true; continue }
        if (-not $inQwen) { continue }
        if ($line.Trim().Length -eq 0) { continue }
        if ($line -notmatch '^\s{4}\S') { break }
        if ($line -match '^\s{4}#') { continue }
        if ($line -match '^\s{4}([A-Za-z0-9_]+):\s*(\S.*?)\s*$') {
            $contract[$Matches[1]] = $Matches[2]
        }
    }
    foreach ($key in @("root", "binary", "model", "codec")) {
        if (-not $contract.ContainsKey($key)) {
            throw "Speech config declares no qwen tts ${key}: $configPath"
        }
        if ([System.IO.Path]::IsPathRooted($contract[$key])) {
            throw "Speech config binds the qwen tts $key to a host path and cannot ship: $($contract[$key])"
        }
    }
    return $contract
}

function Copy-QwenTtsPayload([string]$SourceRoot, [string]$OmegaStage, [string]$ReleaseStage) {
    # The voice ships with the product, at the path the shipped config resolves
    # to from the product tree.  A missing payload fails the build instead of
    # producing a mute release that reports success (AGENTS.md section 2).
    $contract = Get-QwenTtsContract $SourceRoot
    $sourcePayload = [System.IO.Path]::GetFullPath((Join-Path $SourceRoot $contract["root"]))
    Assert-RequiredDirectory $sourcePayload "qwen tts payload"
    # ``binary`` resolves against the product tree; ``model`` and ``codec``
    # resolve against the payload root, exactly as runtime/qwen_tts.py does.
    # The sibling download also holds a Hugging Face cache, an unused base
    # model, and a separate ROCm checkpoint. The Windows release selects the
    # custom-voice GGUF + codec and Vulkan backend only; copying the rest adds
    # several GiB and can trip credential-shaped vocabulary keys in the
    # release secret scan.
    $sourceBinary = [System.IO.Path]::GetFullPath((Join-Path $SourceRoot $contract["binary"]))
    $sourceModel = [System.IO.Path]::GetFullPath((Join-Path $sourcePayload $contract["model"]))
    $sourceCodec = [System.IO.Path]::GetFullPath((Join-Path $sourcePayload $contract["codec"]))
    Assert-RequiredFile $sourceBinary "qwen tts binary"
    Assert-RequiredFile $sourceModel "qwen tts model"
    Assert-RequiredFile $sourceCodec "qwen tts codec"
    Assert-SafeChild $sourcePayload $sourceBinary
    Assert-SafeChild $sourcePayload $sourceModel
    Assert-SafeChild $sourcePayload $sourceCodec

    $destination = [System.IO.Path]::GetFullPath((Join-Path $OmegaStage $contract["root"]))
    Assert-SafeChild $ReleaseStage $destination
    New-Item -ItemType Directory -Path $destination -Force | Out-Null

    $runtimeFiles = @($sourceBinary, $sourceModel, $sourceCodec)
    $runtimeFiles += @(Get-ChildItem -LiteralPath $sourcePayload -File -Force -ErrorAction Stop |
        Where-Object { $_.Extension -ieq ".dll" } |
        ForEach-Object { [System.IO.Path]::GetFullPath($_.FullName) })
    $copied = @{}
    foreach ($sourceFile in $runtimeFiles) {
        Assert-SafeChild $sourcePayload $sourceFile
        $relative = Get-PortableRelativePath $sourcePayload $sourceFile
        if ($copied.ContainsKey($relative)) { continue }
        $target = [System.IO.Path]::GetFullPath((Join-Path $destination $relative))
        Assert-SafeChild $ReleaseStage $target
        Copy-OneFile $sourceFile $target
        $copied[$relative] = $true
    }
    Assert-RequiredFile `
        ([System.IO.Path]::GetFullPath((Join-Path $OmegaStage $contract["binary"]))) "staged qwen tts binary"
    return $destination
}

# Operator state a portable LM Studio keeps BESIDE its application files. A release
# carries the app and its bundled weights; it never carries the builder's chat history,
# identity, tokens or caches (law §0 — every instance belongs to someone else). Kept in
# step with scripts/day_zero_reset.PORTABLE_RUNTIME_STATE, which clears the same surfaces
# from an installed SSD.
$script:PortableRuntimeStateDirs = @(
    "conversations", "credentials", "api-prediction-history", "retrieval-sessions",
    "parsed-documents-cache", "cached-rag-pipeline-chunks", "files", "temp-downloads",
    "ui-state", "global-hotkeys", "config-presets-drafts", "portable-path-backups",
    "portable-acl-backups", "staged-updates-app", "user-concrete-model-default-config",
    "skills"
)
$script:PortableRuntimeStateFiles = @(
    "mcp.json", "local-identity.json", "lms-key-2", "conversation-config.json",
    "download-jobs-info.json", "single-downloads-info.json", "projects-registry.json",
    "server-logs-state.json", "last-synced-mcp-state.json", "artifact-permissions-list.json",
    "persistent-extension-pack-state.json", "llmster-pid.lock", "model-index-cache.json",
    "gguf-metadata-cache.json"
)

function Test-PortableAppPath([string]$Relative, [string]$Platform) {
    $normalized = $Relative.Replace('\', '/').TrimStart('/')
    $parts = $normalized.Split('/')
    if (@($parts | Where-Object { $_ -in @("models", "profile", ".data", "updates", "extensions") }).Count -gt 0) {
        return $false
    }
    if (@($parts | Where-Object { $_ -in $script:PortableRuntimeStateDirs }).Count -gt 0) {
        return $false
    }
    if ($script:PortableRuntimeStateFiles -contains $parts[-1]) { return $false }
    if ($normalized -like "*.log") { return $false }
    if ($Platform -eq "windows" -and
        $normalized -in @("settings.json", "apps/bionic/settings.json")) {
        return $false
    }
    return $true
}

function Copy-PortableAppTree([string]$From, [string]$To, [string]$Platform) {
    $resolvedFrom = (Resolve-Path -LiteralPath $From).Path
    New-Item -ItemType Directory -Path $To -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $resolvedFrom -Force) {
        $relative = Get-PortableRelativePath $resolvedFrom $item.FullName
        if (-not (Test-PortableAppPath $relative $Platform)) { continue }
        $destination = Join-Path $To $item.Name
        if ($item.PSIsContainer) {
            Copy-PortableAppTree $item.FullName $destination $Platform
        } else {
            Copy-OneFile $item.FullName $destination
        }
    }
}

function Assert-NoGitLfsPointers([string]$Root) {
    $pointers = @()
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction Stop |
        Where-Object { $_.Length -lt 2048 }) {
        try {
            $first = Get-Content -LiteralPath $file.FullName -TotalCount 1 -ErrorAction Stop
            if ($first -eq "version https://git-lfs.github.com/spec/v1") {
                $pointers += (Get-PortableRelativePath $Root $file.FullName).Replace('\', '/')
            }
        } catch { }
    }
    if ($pointers.Count -gt 0) {
        throw "Runtime contains unresolved Git LFS pointer(s); restore the release payload first: $($pointers -join ', ')"
    }
}

function Get-ReleaseSourceFiles([string]$Root) {
    # Git enumeration only answers for the repository whose root this IS. A tree
    # nested inside someone else's checkout — a test fixture, an unpacked copy —
    # is untracked there, so Git reports the wrong repository and lists nothing:
    # a hollow release that still exits 0 (AGENTS.md section 2.1).
    $insideGit = $false
    $gitPrefix = @("-c", "safe.directory=$Root", "-C", $Root)
    try {
        $topLevel = @(& git @gitPrefix rev-parse --show-toplevel 2>$null)[0]
    } catch {
        $topLevel = $null
    }
    if ($topLevel) {
        $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
        $topFull = [System.IO.Path]::GetFullPath([string]$topLevel).TrimEnd('\', '/')
        $insideGit = $topFull -ieq $rootFull
    }
    if ($insideGit) {
        $dirty = @(& git @gitPrefix status --porcelain --untracked-files=no)
        if ($dirty.Count -gt 0 -and -not $AllowDirtySource) {
            throw "Source tree has tracked changes; commit or revert them before building a release."
        }
        $tracked = @(& git @gitPrefix ls-files | Where-Object { Test-ReleaseRelativePath $_ })
        $untracked = @(
            & git @gitPrefix ls-files --others --exclude-standard |
                Where-Object { Test-ReleaseRelativePath $_ }
        )
        $portableSetupAssets = @(
            "config/portable_product.json",
            "config/portable_help.md",
            "config/portable_privacy.md",
            "config/portable_ip.md",
            "config/portable_hero.png",
            "scripts/lmstudio_updater.py"
        )
        return @($tracked + $untracked + $portableSetupAssets |
            Where-Object { Test-ReleaseRelativePath $_ } |
            Sort-Object -Unique)
    }

    return @(
        Get-ChildItem -LiteralPath $Root -Recurse -File -Force |
            ForEach-Object { Get-PortableRelativePath $Root $_.FullName } |
            Where-Object { Test-ReleaseRelativePath $_ }
    )
}

function Write-PortableUtf8([string]$Path, [string]$Value) {
    $encoding = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
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

function Set-PortableVenvMetadata([string]$OmegaRoot) {
    $venvRoot = Join-Path $OmegaRoot ".venv-brain"
    $scriptsRoot = Join-Path $venvRoot "Scripts"
    $cfg = @(
        "home = ..\python-3.13-embed",
        "implementation = CPython",
        "version_info = 3.13.14",
        "include-system-site-packages = false",
        "omega-portable = true"
    ) -join "`n"
    Write-PortableUtf8 (Join-Path $venvRoot "pyvenv.cfg") $cfg

    $pth = @(
        "python313.zip",
        ".",
        "..\Lib\site-packages",
        "..\..",
        "import site"
    ) -join "`n"
    Write-PortableUtf8 (Join-Path $scriptsRoot "python313._pth") $pth

    Get-ChildItem -LiteralPath $scriptsRoot -File -ErrorAction Stop |
        Where-Object { $_.Name -like "activate*" -or $_.Name -eq "deactivate.bat" } |
        Remove-Item -Force
}

function Set-PortableVitisMetadata([string]$OmegaRoot) {
    # Same shape as the brain venv: the lane's venv names its sibling embed
    # relatively and its ._pth binds its own site-packages and the product root.
    $venvRoot = Join-Path $OmegaRoot ".venv-vitis"
    $lock = Get-Content -LiteralPath (Join-Path $venvRoot "omega-vitis-lane.json") -Raw | ConvertFrom-Json
    if ([string]$lock.python -notmatch '^3\.12\.\d+$') {
        throw "Vitis lane lock does not record a CPython 3.12 interpreter: $($lock.python)"
    }
    $cfg = @(
        "home = ..\python-3.12-embed",
        "implementation = CPython",
        "version_info = $($lock.python)",
        "include-system-site-packages = false",
        "omega-portable = true",
        "omega-lane = vitis"
    ) -join "`n"
    Write-PortableUtf8 (Join-Path $venvRoot "pyvenv.cfg") $cfg
    $pth = @("python312.zip", ".", "..\Lib\site-packages", "..\..", "import site") -join "`n"
    Write-PortableUtf8 (Join-Path $venvRoot "Scripts\python312._pth") $pth
}

function Write-ReleaseManifest([string]$Stage, [string]$ReleaseVersion, [string[]]$Layout) {
    $entries = @()
    foreach ($file in Get-ChildItem -LiteralPath $Stage -Recurse -File | Sort-Object FullName) {
        $relative = (Get-PortableRelativePath $Stage $file.FullName).Replace('\', '/')
        if ($relative -eq "release-manifest.json") { continue }
        $entries += [ordered]@{
            path = $relative
            size = [int64]$file.Length
            sha256 = Get-PortableSha256 $file.FullName
        }
    }
    $manifest = [ordered]@{
        schema_version = 1
        product = "Omega"
        version = $ReleaseVersion
        platform = "windows-x64"
        layout = $Layout
        files = $entries
    }
    $json = $manifest | ConvertTo-Json -Depth 6
    Write-PortableUtf8 (Join-Path $Stage "release-manifest.json") $json
}

if ($Version -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    throw "Version contains unsupported characters: $Version"
}

Assert-RequiredDirectory $SourceRoot "Omega source"
Assert-RequiredDirectory $PythonEmbedRoot "embed bundle"
Assert-RequiredDirectory $BrainVenvRoot "venv bundle"
Assert-RequiredDirectory (Join-Path $SourceRoot "models") "models"
Assert-RequiredDirectory (Join-Path $SourceRoot "runtimes") "runtimes"
Assert-RequiredFile (Join-Path $SourceRoot "Omega.exe") "portable startup executable"
Assert-RequiredFile (Join-Path $SourceRoot "boot.py") "Omega boot entrypoint"
Assert-RequiredFile (Join-Path $SourceRoot "scripts\hud-only.bat") "HUD launcher"
Assert-RequiredFile (Join-Path $SourceRoot "scripts\shutdown.py") "shutdown entrypoint"
Assert-RequiredFile (Join-Path $PythonEmbedRoot "python.exe") "embed interpreter"
Assert-RequiredFile (Join-Path $BrainVenvRoot "Scripts\python.exe") "venv interpreter"
Assert-RequiredDirectory $VitisEmbedRoot "vitis embed"
Assert-RequiredDirectory $VitisVenvRoot "vitis venv"
Assert-RequiredFile (Join-Path $VitisEmbedRoot "python.exe") "vitis embed interpreter"
Assert-RequiredFile (Join-Path $VitisVenvRoot "Scripts\python.exe") "vitis venv interpreter"
Assert-RequiredFile (Join-Path $VitisVenvRoot "omega-vitis-lane.json") "vitis lane lock"
if ($LmStudioRoot) {
    Assert-RequiredDirectory $LmStudioRoot "Windows LM Studio portable payload"
    Assert-RequiredFile (Join-Path $LmStudioRoot "LM Studio.exe") "Windows LM Studio executable"
    Assert-RequiredFile (Join-Path $LmStudioRoot "Launch Portable.cmd") "Windows LM Studio launcher"
}
if ($LinuxLmStudioRoot) {
    Assert-RequiredDirectory $LinuxLmStudioRoot "Linux LM Studio portable payload"
    Assert-RequiredFile (Join-Path $LinuxLmStudioRoot "lm-studio.AppImage") "Linux LM Studio AppImage"
    Assert-RequiredFile (Join-Path $LinuxLmStudioRoot "run-ai.sh") "Linux LM Studio launcher"
}

if (-not $SkipHudBuild) {
    $hudBuilder = Join-Path $SourceRoot "hud\scripts\build_hud.py"
    Assert-RequiredFile $hudBuilder "HUD builder"
    & (Join-Path $BrainVenvRoot "Scripts\python.exe") $hudBuilder --check
    if ($LASTEXITCODE -ne 0) { throw "HUD bundle is stale or failed validation" }
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$packageName = "omega-$Version-windows-x64"
$stageRoot = Join-Path $OutputRoot "$packageName.stage"
$tempStage = Join-Path $OutputRoot "$packageName.stage.tmp-$PID"
$archivePath = Join-Path $OutputRoot "$packageName.zip"
$checksumPath = "$archivePath.sha256"

Remove-SafeTree $OutputRoot $tempStage
New-Item -ItemType Directory -Path $tempStage -Force | Out-Null

try {
    $omegaStage = Join-Path $tempStage "Omega"
    New-Item -ItemType Directory -Path $omegaStage -Force | Out-Null
    # Ship the canonical logging directory even at factory reset. Logs are
    # generated by this installation; developer diagnostic data is excluded.
    $logsStage = Join-Path $omegaStage "guardian\logs"
    New-Item -ItemType Directory -Path $logsStage -Force | Out-Null
    Copy-OneFile (Join-Path $SourceRoot "Omega.exe") (Join-Path $tempStage "Omega.exe")

    foreach ($relative in Get-ReleaseSourceFiles $SourceRoot) {
        $source = Join-Path $SourceRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        Copy-OneFile $source (Join-Path $omegaStage $relative)
    }

    Copy-FilteredTree (Join-Path $SourceRoot "models") (Join-Path $omegaStage "models")
    Copy-FilteredTree (Join-Path $SourceRoot "runtimes") (Join-Path $omegaStage "runtimes")
    $qwenPayloadStage = Copy-QwenTtsPayload $SourceRoot $omegaStage $tempStage
    $releaseLayout = @("Omega.exe", "Omega")
    # The speech payload keeps the layout the config names. When that is beside
    # the product tree, it is a top-level component of the release and the
    # manifest says so.
    $qwenTop = (Get-PortableRelativePath $tempStage $qwenPayloadStage).Split(
        [System.IO.Path]::DirectorySeparatorChar)[0]
    if ($releaseLayout -notcontains $qwenTop) { $releaseLayout += $qwenTop }
    if ($LmStudioRoot) {
        Copy-PortableAppTree $LmStudioRoot (Join-Path $tempStage "LM Studio") "windows"
        $releaseLayout += "LM Studio"
    }
    if ($LinuxLmStudioRoot) {
        Copy-PortableAppTree $LinuxLmStudioRoot (Join-Path $tempStage "LM-Studio-Linux") "linux"
        $releaseLayout += "LM-Studio-Linux"
    }
    Assert-NoGitLfsPointers $tempStage
    Copy-EttinSnapshot $SourceRoot $omegaStage
    Copy-FilteredTree $PythonEmbedRoot (Join-Path $omegaStage "python-3.13-embed")
    Copy-FilteredTree $BrainVenvRoot (Join-Path $omegaStage ".venv-brain")
    Set-PortableVenvMetadata $omegaStage
    Copy-FilteredTree $VitisEmbedRoot (Join-Path $omegaStage "python-3.12-embed")
    Copy-FilteredTree $VitisVenvRoot (Join-Path $omegaStage ".venv-vitis")
    Set-PortableVitisMetadata $omegaStage
    Write-ReleaseManifest $tempStage $Version $releaseLayout

    Remove-SafeTree $OutputRoot $stageRoot
    Move-Item -LiteralPath $tempStage -Destination $stageRoot

    if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
    if (Test-Path -LiteralPath $checksumPath) { Remove-Item -LiteralPath $checksumPath -Force }
    Compress-Archive -Path (Join-Path $stageRoot "*") -DestinationPath $archivePath -CompressionLevel Optimal
    # PowerShell's ZIP provider may store the sentinel file but omit the
    # directory record itself. Add the canonical directory entry explicitly
    # so consumers can detect the logging root without creating it first.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open(
        $archivePath,
        [System.IO.Compression.ZipArchiveMode]::Update
    )
    try {
        if (-not $zip.GetEntry("Omega/guardian/logs/")) {
            $zip.CreateEntry("Omega/guardian/logs/") | Out-Null
        }
    } finally {
        $zip.Dispose()
    }
    $hash = Get-PortableSha256 $archivePath
    Set-Content -LiteralPath $checksumPath -Value "$hash  $packageName.zip`n" -Encoding ascii
} catch {
    Remove-SafeTree $OutputRoot $tempStage
    throw
}

Write-Host "Portable Omega staged: $stageRoot"
Write-Host "Portable Omega archive: $archivePath"
Write-Host "SHA-256: $checksumPath"
