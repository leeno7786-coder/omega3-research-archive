"""Portable Windows release contracts.

These tests use tiny stand-ins for the embedded interpreter and substrate.  They
exercise the release tooling itself without copying the developer's ~1 GiB
environment into every test case.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell.exe")
BUILD_SCRIPT = OMEGA_ROOT / "scripts" / "build_portable.ps1"
VERIFY_SCRIPT = OMEGA_ROOT / "scripts" / "verify_portable.ps1"
INSTALL_SCRIPT = OMEGA_ROOT / "install.ps1"


def _write(path: Path, body: str = "fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _fixture_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "developer checkout"
    embed = source / "python-3.13-embed"
    venv = source / ".venv-brain"

    _write(source / "boot.py", "print('omega fixture')\n")
    _write(source / "LICENSE", "Omega test license\n")
    _write(source / "README.md", "Omega test release\n")
    _write(source / "Omega.exe", "portable launcher\n")
    _write(source / "scripts" / "hud-only.bat", "@echo off\r\n")
    _write(source / "scripts" / "shutdown.py", "print('shutdown')\n")
    _write(source / "scripts" / "lmstudio_updater.py", "UPDATE_ENGINE = True\n")
    _write(source / "scripts" / "install_systemd_user.sh", "#!/usr/bin/env bash\n")
    _write(source / "scripts" / "omega_bus_peek.sh", "#!/usr/bin/env bash\n")
    _write(source / "hud" / "omega_interface.html", "<html>Omega</html>\n")
    _write(
        source / "browser_agent_extension" / "agent-runner" / "browser-launch.test.js",
        'const fixture = "C:\\\\Users\\\\builder\\\\browser";\n',
    )
    _write(
        source / "browser_agent_extension" / ".codebase-memory" / "graph.db.zst",
        "developer graph state must not ship\n",
    )
    _write(source / "runtime" / "__init__.py", "")
    _write(
        source / "organ" / "promptorgan" / "__init__.py",
        "PROMPTORGAN_CODE = True\n",
    )
    _write(source / "guardian" / "telemetry" / "__init__.py", "")
    _write(source / "guardian" / "telemetry" / "writer.py", "TELEMETRY_CODE = True\n")
    _write(source / "guardian" / "telemetry" / "events.jsonl", '{"operator":"private"}\n')
    _write(source / "models" / "README.md", "Choose and download models during first-run setup.\n")
    _write(source / "models" / "manifest.json", "{}\n")
    _write(source / "models" / "imported_manifest.json", "{}\n")
    # The speech payload the shipped config points at.  It sits beside the
    # checkout on the builder's SSD exactly as it must sit beside the product
    # tree in the package, so the release carries it instead of shipping a
    # boot greeting with nothing to speak it.
    _write(
        source / "config" / "tts.yaml",
        "tts:\n"
        "  engine: qwen\n"
        "  qwen:\n"
        "    # Paths are relative to the portable Omega root.\n"
        "    root: ../models/qwen3-tts\n"
        "    binary: ../models/qwen3-tts/tts-server.exe\n"
        "    model: qwen-talker-fixture-Q8_0.gguf\n"
        "    codec: qwen-tokenizer-fixture-Q8_0.gguf\n",
    )
    qwen_payload = source.parent / "models" / "qwen3-tts"
    _write(qwen_payload / "tts-server.exe", "qwen sidecar\n")
    _write(qwen_payload / "qwen-talker-fixture-Q8_0.gguf", "talker weights\n")
    _write(qwen_payload / "qwen-tokenizer-fixture-Q8_0.gguf", "codec weights\n")
    _write(qwen_payload / "ggml-vulkan.dll", "vulkan backend\n")
    _write(qwen_payload / "qwen-talker-unused-base-Q8_0.gguf", "unused base model\n")
    _write(qwen_payload / ".cache" / "huggingface" / "download" / "model.lock", "cache lock\n")
    _write(
        qwen_payload / "rocm" / "Qwen3-TTS" / "vocab.json",
        '{"passphrase": 1234, "ordinary_token": 5678}\n',
    )
    _write(
        source / "memory" / "000_identity" / "constitution" / "founding.json",
        '{"id":"0.00.founding","coord":[0,0,0]}\n',
    )
    _write(
        source / "memory" / "000_identity" / "evolution" / "operator_trait.json",
        '{"id":"trait.operator","coord":[0,1,0],"path":"C:\\\\Users\\\\builder"}\n',
    )
    _write(
        source / "memory" / "400_situational" / "operator" / "conversation.json",
        '{"id":"4.01.conversation","coord":[4,1,0],"path":"C:\\\\Users\\\\builder"}\n',
    )
    _write(
        source / "memory" / "700_vision" / "images" / "operator.png",
        "developer face image\n",
    )
    _write(
        source / "memory" / "1100_dev" / "projects" / "_omega_project.json",
        '{"workspace_root":"I:\\\\omega-workspace"}\n',
    )
    _write(source / "memory" / "indexes" / "label_map.json", "{}\n")
    _write(source / "memory" / "indexes" / "coord_index.json", "{}\n")
    _write(source / "memory" / "dme" / "adapter.py", "DME_CODE = True\n")
    _write(source / "memory" / "dme" / "sram.bin", "stale developer projection\n")
    _write(source / "memory" / "projection" / "publication.lock", "stale lock\n")
    _write(source / "memory" / "source_ledger" / "store.py", "LEDGER_CODE = True\n")
    _write(
        source / "memory" / "source_ledger" / "artifacts" / "src-developer.json",
        '{"path":"C:\\\\Users\\\\builder"}\n',
    )
    _write(source / "memory" / "taxonomy_store" / "compiled" / "graph.sqlite", "stale graph\n")
    _write(source / "memory" / "integrity" / "audit.py", "INTEGRITY_CODE = True\n")
    _write(source / "memory" / "integrity" / "last_audit.json", "stale audit\n")
    _write(source / "models" / "onnxs" / "dme" / "overlap_matmul.onnx", "dme fixture\n")
    _write(
        source / "models" / "onnxs" / "kokoro" / "kokoro-v1.0.int8.onnx",
        "retired Kokoro model fixture\n",
    )
    _write(source / "models" / "intent" / "router" / "router.onnx", "deployed intent router\n")
    _write(source / "models" / "intent" / "base" / "pytorch_model.bin", "training base\n")
    _write(
        source / "models" / "intent" / "router-modernbert-base-20260914-204245" / "checkpoint" / "pytorch_model.bin",
        "training checkpoint\n",
    )
    _write(source / "runtimes" / "npu" / "onnxruntime.dll", "windows npu runtime\n")
    _write(source / "runtimes" / "npu" / "ryzenai-server.exe", "windows npu server\n")
    _write(source / "runtimes" / "npu" / "dme" / "overlap_matmul.xclbin", "dme bitstream\n")
    _write(source / "runtimes" / "bin" / "crispasr" / "vulkan" / "crispasr", "linux crispasr\n")
    _write(source / "runtimes" / "bin" / "crispasr" / "vulkan" / "libcrispasr.so.1", "linux crispasr\n")
    _write(source / "runtimes" / "bin" / "crispasr" / "vulkan" / "crispasr.exe", "windows crispasr\n")
    _write(source / "runtimes" / "npu" / "src" / "build.sh", "#!/bin/sh\n")
    _write(source / "runtimes" / "npu" / "linux" / "host-only.sh", "#!/bin/sh\n")
    for archive_name in ("edgedriver_win64.zip", "edgedriver_win32.zip", "edgedriver_linux64.zip"):
        _write(source / "runtimes" / "browser" / "edgedriver" / archive_name, archive_name)
    _write(
        source / "runtimes" / "browser" / "edgedriver" / "manifest.json",
        '{"version":"153.0.4234.32","assets":{}}\n',
    )
    _write(source / "runtimes" / "python-embed-cpu" / "python.exe", "dme python\n")
    _write(source / "runtimes" / "python-embed-onnx-vitisai" / "python.exe", "old npu python\n")
    _write(source / "memory" / "engine" / "release.py", "VERSION = 'new'\n")
    _write(
        source
        / "memory"
        / "models"
        / "models--tasksource--ettin-17m-embed"
        / "snapshots"
        / "fixture"
        / "model.safetensors",
        "ettin fixture\n",
    )
    _write(source / "plugin_platform" / "featured_catalog.json", "{}\n")
    _write(source / "tests" / "must-not-ship.txt")
    _write(source / "guardian" / "state" / "must-not-ship.json", "{}\n")
    _write(
        source / "pressure_loop" / "state" / "episode_journal.jsonl",
        '{"path":"C:\\\\Users\\\\builder\\\\session"}\n',
    )
    _write(
        source / "knowledge_extractor" / "state" / "artifacts.jsonl",
        '{"path":"C:\\\\Users\\\\builder\\\\knowledge"}\n',
    )
    _write(
        source / "deepsleep_loop" / "state" / "journal.jsonl",
        '{"path":"C:\\\\Users\\\\builder\\\\deepsleep"}\n',
    )
    _write(source / ".mcp.json", json.dumps({"cwd": r"F:\\Omega3.0"}))

    _write(embed / "python.exe")
    _write(embed / "python313.dll")
    _write(embed / "python313._pth", "python313.zip\n.\n")
    _write(embed / "Scripts" / "pip.exe")
    _write(embed / "share" / "terminfo" / "broken", "not part of Windows runtime\n")
    _write(
        embed / "Lib" / "site-packages" / "pip" / "_internal" / "utils" / "urls.py",
        'example = r"C:\\Users\\builder\\pip"\n',
    )
    _write(venv / "Scripts" / "python.exe")
    _write(venv / "Scripts" / "python313.dll")
    _write(venv / "Scripts" / "python313.zip")
    _write(venv / "Scripts" / "pytest.exe")
    _write(venv / "Scripts" / "uvicorn.exe")
    _write(venv / "Lib" / "site-packages" / "spacy" / "__init__.py", "")
    _write(venv / "Lib" / "site-packages" / "spacy" / "training" / "__init__.py", "")
    _write(
        venv / "Lib" / "site-packages" / "_pyinstaller_hooks_contrib" / "hook.py",
        "example = r'C:\\Python27\\Lib\\site-packages'\n",
    )
    _write(venv / "Lib" / "site-packages" / "PyInstaller" / "__init__.py", "")
    _write(
        venv / "Lib" / "site-packages" / "pyinstaller_hooks_contrib-2026.7.dist-info" / "METADATA",
        "Name: pyinstaller-hooks-contrib\n",
    )
    _write(
        venv / "Lib" / "site-packages" / "_pytest" / "_py" / "path.py",
        "example = r'a:/some/path/to/a/file.ext'\n",
    )
    _write(venv / "Lib" / "site-packages" / "pytest" / "__init__.py", "")
    _write(venv / "Lib" / "site-packages" / "coverage" / "__init__.py", "")
    _write(venv / "Lib" / "site-packages" / "ruff" / "__init__.py", "")
    _write(
        venv
        / "Lib"
        / "site-packages"
        / "ast_serialize-0.6.0.dist-info"
        / "sboms"
        / "mypy_parser.cyclonedx.json",
        '{"source":"C:\\\\Users\\\\builder\\\\source"}\n',
    )
    _write(
        venv / "pyvenv.cfg",
        "home = C:\\Users\\builder\\Omega3.0\\python-3.13-embed\n"
        "implementation = CPython\nversion_info = 3.13.14\n",
    )
    _write(venv / "Scripts" / "activate.bat", "C:\\Users\\builder\\Omega3.0\n")
    _vitis_lane_fixture(source)

    return source, embed, venv


def _vitis_lane_fixture(source: Path) -> tuple[Path, Path]:
    """The cp312 Vitis lane, shaped as scripts/bootstrap_vitis_lane.py lays it out."""
    embed = source / "python-3.12-embed"
    venv = source / ".venv-vitis"
    _write(embed / "python.exe")
    _write(embed / "python312.dll")
    _write(embed / "python312.zip")
    _write(embed / "python312._pth", "python312.zip\n.\n")
    _write(venv / "Scripts" / "python.exe")
    _write(venv / "Scripts" / "python312.dll")
    _write(venv / "Scripts" / "python312.zip")
    _write(venv / "Scripts" / "python312._pth", "stale\n")
    _write(venv / "Scripts" / "pip.exe")
    _write(
        venv / "pyvenv.cfg",
        "home = C:\\Users\\builder\\Omega3.0\\python-3.12-embed\nversion_info = 3.12.10\n",
    )
    _write(
        venv / "omega-vitis-lane.json",
        '{"schema": 1, "python": "3.12.10", "distributions": []}\n',
    )
    site = venv / "Lib" / "site-packages"
    _write(site / "onnxruntime" / "__init__.py", "__version__ = '1.23.2'\n")
    _write(site / "onnxruntime" / "capi" / "onnxruntime_vitisai_ep.dll", "vitis ep\n")
    _write(site / "onnxruntime_vitisai-1.23.2.dist-info" / "METADATA", "Name: onnxruntime_vitisai\n")
    _write(site / "voe" / "vendor.py", 'example = r"C:\\Users\\<user>\\AppData"\n')
    _write(site / "pip" / "__init__.py", "")
    return embed, venv


def _run_ps(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL, "PowerShell is required for the Windows release gate"
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ],
        cwd=OMEGA_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    source, embed, venv = _fixture_inputs(tmp_path)
    output = tmp_path / "release output"
    proc = _run_ps(
        BUILD_SCRIPT,
        "-SourceRoot",
        str(source),
        "-PythonEmbedRoot",
        str(embed),
        "-BrainVenvRoot",
        str(venv),
        "-OutputRoot",
        str(output),
        "-Version",
        "test.1",
        "-SkipHudBuild",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return output / "omega-test.1-windows-x64.stage", output / "omega-test.1-windows-x64.zip"


def _vendor_runtime_fixtures(tmp_path: Path) -> tuple[Path, Path]:
    windows = tmp_path / "LM Studio"
    linux = tmp_path / "LM-Studio-Linux"
    _write(windows / "LM Studio.exe", "windows app\n")
    _write(windows / "Launch Portable.cmd", "@echo off\r\n")
    _write(windows / "resources" / "app" / "package.json", '{"version":"0.4.0"}\n')
    _write(windows / "models" / "should-not-ship.gguf", "user model\n")
    _write(windows / "profile" / "settings.json", "user profile\n")
    _write(windows / "settings.json", '{"downloadsFolder":"C:\\\\Users\\\\builder"}\n')
    _write(windows / "extensions" / "models" / "downloaded.gguf", "user extension model\n")
    # Operator state a real portable LM Studio keeps beside the app (review 2026-09-19):
    # a shipped release that carries these hands the next operator the previous one's
    # chat history, identity and tokens (law §0).
    _write(windows / "conversations" / "1789213972447.conversation.json", '{"name":"private"}\n')
    _write(windows / "credentials" / "mcp-oauth" / "token.json", '{"access_token":"secret"}\n')
    _write(windows / "mcp.json", '{"servers":{"operator":{}}}\n')
    _write(windows / ".internal" / "local-identity.json", '{"userId":"builder"}\n')
    _write(windows / ".internal" / "lms-key-2", "operator-api-key\n")
    _write(windows / ".internal" / "download-jobs-info.json", '{"jobs":["their-model"]}\n')
    _write(windows / ".internal" / "ui-state" / "global.json", '{"window":1}\n')
    _write(windows / ".internal" / "files" / "operator-upload.pdf", "attachment\n")
    _write(windows / ".internal" / "bundled-models" / "shipped.gguf", "shipped weights\n")
    _write(linux / "lm-studio.AppImage", "linux app\n")
    _write(linux / "run-ai.sh", "#!/bin/sh\n")
    _write(linux / "models" / "should-not-ship.gguf", "user model\n")
    _write(linux / ".data" / "config" / "settings.json", "user profile\n")
    return windows, linux


def test_portable_build_includes_untracked_live_sources_in_a_git_checkout(
    tmp_path: Path,
):
    source, embed, venv = _fixture_inputs(tmp_path)
    _write(source / "runtime" / "untracked_live.py", "LIVE_SOURCE = True\n")

    subprocess.run(["git", "init", "--quiet"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Portable Fixture"], cwd=source, check=True)
    subprocess.run(["git", "add", "boot.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=source, check=True)

    output = tmp_path / "git release output"
    proc = _run_ps(
        BUILD_SCRIPT,
        "-SourceRoot",
        str(source),
        "-PythonEmbedRoot",
        str(embed),
        "-BrainVenvRoot",
        str(venv),
        "-OutputRoot",
        str(output),
        "-Version",
        "git-test.1",
        "-SkipHudBuild",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    packaged = output / "omega-git-test.1-windows-x64.stage" / "Omega" / "runtime" / "untracked_live.py"
    assert packaged.read_text(encoding="utf-8") == "LIVE_SOURCE = True\n"
    stage = packaged.parents[2]
    assert not (stage / "Omega" / "pressure_loop" / "state").exists()
    assert not (stage / "Omega" / "tests").exists()
    assert not (stage / "Omega" / ".git").exists()


def test_portable_build_stages_the_complete_relocatable_shape(tmp_path: Path):
    stage, archive = _build_fixture(tmp_path)
    required = {
        "Omega.exe",
        "Omega/organ/promptorgan/__init__.py",
        "Omega/.venv-brain/Scripts/python.exe",
        "Omega/python-3.13-embed/python.exe",
        "Omega/.venv-brain/Lib/site-packages/spacy/training/__init__.py",
        "Omega/scripts/hud-only.bat",
        "Omega/scripts/shutdown.py",
        "Omega/scripts/lmstudio_updater.py",
        "Omega/guardian/telemetry/__init__.py",
        "Omega/guardian/telemetry/writer.py",
        "Omega/models/README.md",
        "Omega/models/manifest.json",
        "Omega/models/imported_manifest.json",
        "Omega/models/onnxs/dme/overlap_matmul.onnx",
        "Omega/memory/000_identity/constitution/founding.json",
        "Omega/memory/indexes/label_map.json",
        "Omega/memory/dme/adapter.py",
        "Omega/memory/source_ledger/store.py",
        "Omega/memory/integrity/audit.py",
        "Omega/runtimes/npu/onnxruntime.dll",
        "Omega/runtimes/npu/ryzenai-server.exe",
        "Omega/runtimes/npu/dme/overlap_matmul.xclbin",
        "Omega/runtimes/browser/edgedriver/edgedriver_win64.zip",
        "Omega/runtimes/browser/edgedriver/edgedriver_win32.zip",
        "Omega/runtimes/browser/edgedriver/edgedriver_linux64.zip",
        "Omega/runtimes/browser/edgedriver/manifest.json",
        "release-manifest.json",
    }
    assert {
        path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()
    } >= required
    assert not (stage / "Omega" / "models" / "onnxs" / "kokoro").exists()
    assert (stage / "Omega" / "models" / "intent" / "router" / "router.onnx").is_file()
    assert not (stage / "Omega" / "models" / "intent" / "base").exists()
    assert not (
        stage / "Omega" / "models" / "intent" / "router-modernbert-base-20260914-204245"
    ).exists()
    assert archive.is_file()
    assert archive.with_suffix(".zip.sha256").is_file()
    assert (stage / "Omega" / "guardian" / "logs").is_dir()
    with zipfile.ZipFile(archive) as release_zip:
        assert "Omega/guardian/logs/" in {
            name.replace("\\", "/") for name in release_zip.namelist()
        }

    cfg = (stage / "Omega" / ".venv-brain" / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "C:\\Users" not in cfg
    assert "home = ..\\python-3.13-embed" in cfg
    pth = (stage / "Omega" / ".venv-brain" / "Scripts" / "python313._pth").read_text(
        encoding="utf-8"
    )
    assert pth.splitlines() == [
        "python313.zip",
        ".",
        r"..\Lib\site-packages",
        r"..\..",
        "import site",
    ]
    assert not (stage / "Omega" / ".mcp.json").exists()
    assert not (stage / "Omega" / "guardian" / "telemetry" / "events.jsonl").exists()
    assert not (stage / "Omega" / "tests").exists()
    assert not (
        stage
        / "Omega"
        / "browser_agent_extension"
        / "agent-runner"
        / "browser-launch.test.js"
    ).exists()
    assert not (stage / "Omega" / "browser_agent_extension" / ".codebase-memory").exists()
    assert not (stage / "Omega" / "scripts" / "install_systemd_user.sh").exists()
    assert not (stage / "Omega" / "scripts" / "omega_bus_peek.sh").exists()
    assert not (stage / "Omega" / "guardian" / "state" / "must-not-ship.json").exists()
    assert not (stage / "Omega" / "memory" / "000_identity" / "evolution").exists()
    assert not (stage / "Omega" / "memory" / "400_situational").exists()
    assert not (stage / "Omega" / "memory" / "700_vision" / "images").exists()
    assert not (stage / "Omega" / "memory" / "1100_dev").exists()
    assert not (stage / "Omega" / "memory" / "indexes" / "coord_index.json").exists()
    assert not (stage / "Omega" / "memory" / "dme" / "sram.bin").exists()
    assert not (stage / "Omega" / "memory" / "projection" / "publication.lock").exists()
    assert not (stage / "Omega" / "memory" / "source_ledger" / "artifacts").exists()
    assert not (stage / "Omega" / "memory" / "taxonomy_store").exists()
    assert not (stage / "Omega" / "memory" / "integrity" / "last_audit.json").exists()
    assert not (stage / "Omega" / "pressure_loop" / "state").exists()
    assert not (stage / "Omega" / "knowledge_extractor" / "state").exists()
    assert not (stage / "Omega" / "deepsleep_loop" / "state").exists()
    assert not (stage / "Omega" / ".venv-brain" / "Scripts" / "activate.bat").exists()
    assert not (stage / "Omega" / ".venv-brain" / "Lib" / "site-packages" / "PyInstaller").exists()
    assert not (
        stage
        / "Omega"
        / ".venv-brain"
        / "Lib"
        / "site-packages"
        / "_pyinstaller_hooks_contrib"
    ).exists()
    assert not (
        stage
        / "Omega"
        / ".venv-brain"
        / "Lib"
        / "site-packages"
        / "pyinstaller_hooks_contrib-2026.7.dist-info"
    ).exists()
    for dev_package in ("_pytest", "pytest", "coverage", "ruff"):
        assert not (
            stage / "Omega" / ".venv-brain" / "Lib" / "site-packages" / dev_package
        ).exists()
    assert not (
        stage
        / "Omega"
        / ".venv-brain"
        / "Lib"
        / "site-packages"
        / "ast_serialize-0.6.0.dist-info"
        / "sboms"
    ).exists()
    assert not (stage / "Omega" / "python-3.13-embed" / "share").exists()
    assert not (stage / "Omega" / "python-3.13-embed" / "Scripts").exists()
    assert not (stage / "Omega" / ".venv-brain" / "Scripts" / "pytest.exe").exists()
    assert not (stage / "Omega" / ".venv-brain" / "Scripts" / "uvicorn.exe").exists()
    assert not (
        stage
        / "Omega"
        / "python-3.13-embed"
        / "Lib"
        / "site-packages"
        / "pip"
    ).exists()
    assert not (stage / "Omega" / "runtimes" / "npu" / "linux").exists()
    assert not (stage / "Omega" / "runtimes" / "npu" / "src").exists()
    assert not (stage / "Omega" / "runtimes" / "bin" / "crispasr" / "vulkan" / "crispasr").exists()
    assert not (stage / "Omega" / "runtimes" / "bin" / "crispasr" / "vulkan" / "libcrispasr.so.1").exists()
    assert (stage / "Omega" / "runtimes" / "bin" / "crispasr" / "vulkan" / "crispasr.exe").is_file()
    assert not (stage / "Omega" / "runtimes" / "python-embed-cpu").exists()
    assert not (stage / "Omega" / "runtimes" / "python-embed-onnx-vitisai").exists()
    assert (
        stage
        / "Omega"
        / "memory"
        / "models"
        / "models--tasksource--ettin-17m-embed"
        / "snapshots"
        / "fixture"
        / "model.safetensors"
    ).is_file()
    assert not (stage / "omega-runtime").exists()


def test_portable_build_stages_the_vitis_lane_relocatably(tmp_path: Path):
    """The NPU provider child runs on its own cp312 stack inside the product."""
    stage, _archive = _build_fixture(tmp_path)
    omega = stage / "Omega"

    for relative in (
        "python-3.12-embed/python.exe",
        "python-3.12-embed/python312.dll",
        ".venv-vitis/Scripts/python.exe",
        ".venv-vitis/Scripts/python312.dll",
        ".venv-vitis/omega-vitis-lane.json",
        ".venv-vitis/Lib/site-packages/onnxruntime/capi/onnxruntime_vitisai_ep.dll",
        ".venv-vitis/Lib/site-packages/onnxruntime_vitisai-1.23.2.dist-info/METADATA",
    ):
        assert (omega / relative).is_file(), relative
    cfg = (omega / ".venv-vitis" / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "C:\\Users" not in cfg
    assert "home = ..\\python-3.12-embed" in cfg.splitlines()
    assert (omega / ".venv-vitis" / "Scripts" / "python312._pth").read_text(
        encoding="utf-8"
    ).splitlines() == ["python312.zip", ".", r"..\Lib\site-packages", r"..\..", "import site"]
    assert not (omega / ".venv-vitis" / "Scripts" / "pip.exe").exists()
    assert not (omega / ".venv-vitis" / "Lib" / "site-packages" / "pip").exists()

    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("absolute home", "vitis venv does not point"),
        ("embed site", "vitis embed"),
        ("no lock", "omega-vitis-lane.json"),
        ("no provider", "onnxruntime_vitisai"),
    ],
)
def test_portable_verifier_rejects_a_host_bound_or_incomplete_vitis_lane(
    tmp_path: Path, mutation: str, message: str
):
    stage, _archive = _build_fixture(tmp_path)
    omega = stage / "Omega"
    if mutation == "absolute home":
        _write(omega / ".venv-vitis" / "pyvenv.cfg", "home = C:\\Python312\n")
    elif mutation == "embed site":
        _write(omega / "python-3.12-embed" / "python312._pth", "python312.zip\n.\nimport site\n")
    elif mutation == "no lock":
        (omega / ".venv-vitis" / "omega-vitis-lane.json").unlink()
    else:
        shutil.rmtree(
            omega / ".venv-vitis" / "Lib" / "site-packages" / "onnxruntime_vitisai-1.23.2.dist-info"
        )

    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")

    assert proc.returncode != 0
    assert message in (proc.stdout + proc.stderr).lower()


def test_portable_verifier_launch_imports_the_vitis_provider_in_its_own_interpreter():
    source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert ".venv-vitis\\Scripts\\python.exe" in source
    assert "'VitisAIExecutionProvider' in onnxruntime.get_available_providers()" in source


def test_portable_build_stages_the_qwen_payload_where_the_shipped_config_resolves_it(
    tmp_path: Path,
):
    """The voice ships with the product, at the path its own config names.

    ``config/tts.yaml`` is the single source of truth for where the Qwen payload
    lives; the builder reads it rather than repeating it, so the two cannot drift
    into a release whose boot greeting has nothing to speak with.
    """
    stage, _ = _build_fixture(tmp_path)

    shipped = (stage / "Omega" / "config" / "tts.yaml").read_text(encoding="utf-8")
    configured = re.search(r"^\s+root:\s*(\S+)\s*$", shipped, re.M)
    assert configured, "the shipped speech config declares no Qwen payload root"
    root = configured.group(1)
    assert not Path(root).is_absolute(), f"shipped config binds a host path: {root}"

    payload = (stage / "Omega" / root).resolve()
    assert payload.is_relative_to(stage.resolve())
    assert (payload / "tts-server.exe").is_file()
    assert (payload / "qwen-talker-fixture-Q8_0.gguf").is_file()
    assert (payload / "qwen-tokenizer-fixture-Q8_0.gguf").is_file()
    assert (payload / "ggml-vulkan.dll").is_file()
    assert not (payload / "qwen-talker-unused-base-Q8_0.gguf").exists()
    assert not (payload / ".cache").exists()
    assert not (payload / "rocm").exists()

    manifest = json.loads((stage / "release-manifest.json").read_text(encoding="utf-8"))
    staged = {entry["path"] for entry in manifest["files"]}
    assert "models/qwen3-tts/tts-server.exe" in staged
    assert "models" in manifest["layout"]


def test_portable_build_bundles_fresh_model_free_lmstudio_payloads(tmp_path: Path):
    source, embed, venv = _fixture_inputs(tmp_path)
    windows, linux = _vendor_runtime_fixtures(tmp_path)
    output = tmp_path / "vendor release output"
    proc = _run_ps(
        BUILD_SCRIPT,
        "-SourceRoot",
        str(source),
        "-PythonEmbedRoot",
        str(embed),
        "-BrainVenvRoot",
        str(venv),
        "-OutputRoot",
        str(output),
        "-Version",
        "vendor-test.1",
        "-LmStudioRoot",
        str(windows),
        "-LinuxLmStudioRoot",
        str(linux),
        "-SkipHudBuild",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    stage = output / "omega-vendor-test.1-windows-x64.stage"
    assert (stage / "LM Studio" / "LM Studio.exe").is_file()
    assert (stage / "LM Studio" / "Launch Portable.cmd").is_file()
    assert (stage / "LM Studio" / "resources" / "app" / "package.json").is_file()
    assert (stage / "LM-Studio-Linux" / "lm-studio.AppImage").is_file()
    assert (stage / "LM-Studio-Linux" / "run-ai.sh").is_file()
    # Operator state never ships; the shipped app and its bundled weights do.
    assert not (stage / "LM Studio" / "conversations").exists()
    assert not (stage / "LM Studio" / "credentials").exists()
    assert not (stage / "LM Studio" / "mcp.json").exists()
    assert not (stage / "LM Studio" / ".internal" / "local-identity.json").exists()
    assert not (stage / "LM Studio" / ".internal" / "lms-key-2").exists()
    assert not (stage / "LM Studio" / ".internal" / "download-jobs-info.json").exists()
    assert not (stage / "LM Studio" / ".internal" / "ui-state").exists()
    assert not (stage / "LM Studio" / ".internal" / "files").exists()
    assert (stage / "LM Studio" / ".internal" / "bundled-models" / "shipped.gguf").is_file()
    assert not (stage / "LM Studio" / "models").exists()
    assert not (stage / "LM Studio" / "profile").exists()
    assert not (stage / "LM Studio" / "settings.json").exists()
    assert not (stage / "LM Studio" / "extensions").exists()
    assert not (stage / "LM-Studio-Linux" / "models").exists()
    assert not (stage / "LM-Studio-Linux" / ".data").exists()
    manifest = json.loads((stage / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["layout"] == [
        "Omega.exe",
        "Omega",
        "models",
        "LM Studio",
        "LM-Studio-Linux",
    ]


def test_portable_verifier_rejects_lmstudio_user_state(tmp_path: Path):
    source, embed, venv = _fixture_inputs(tmp_path)
    windows, linux = _vendor_runtime_fixtures(tmp_path)
    output = tmp_path / "vendor verification output"
    proc = _run_ps(
        BUILD_SCRIPT,
        "-SourceRoot",
        str(source),
        "-PythonEmbedRoot",
        str(embed),
        "-BrainVenvRoot",
        str(venv),
        "-OutputRoot",
        str(output),
        "-Version",
        "vendor-verify.1",
        "-LmStudioRoot",
        str(windows),
        "-LinuxLmStudioRoot",
        str(linux),
        "-SkipHudBuild",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    stage = output / "omega-vendor-verify.1-windows-x64.stage"
    _write(stage / "LM Studio" / "models" / "late-user-model.gguf", "user model\n")

    proc = _run_ps(
        VERIFY_SCRIPT,
        "-ReleaseRoot",
        str(stage),
        "-SkipLaunch",
    )
    assert proc.returncode != 0
    assert "user state" in (proc.stdout + proc.stderr).lower()


@pytest.mark.parametrize(
    "missing",
    [
        "models", "runtimes", "embed", "venv", "qwen tts payload",
        "vitis embed", "vitis venv", "vitis lane lock",
    ],
)
def test_portable_build_fails_closed_when_a_required_bundle_is_missing(
    tmp_path: Path, missing: str
):
    source, embed, venv = _fixture_inputs(tmp_path)
    selected = {
        "models": source / "models",
        "runtimes": source / "runtimes",
        "embed": embed,
        "venv": venv,
        "qwen tts payload": source.parent / "models" / "qwen3-tts",
        "vitis embed": source / "python-3.12-embed",
        "vitis venv": source / ".venv-vitis",
        "vitis lane lock": source / ".venv-vitis" / "omega-vitis-lane.json",
    }[missing]
    if selected.is_dir():
        shutil.rmtree(selected)
    else:
        selected.unlink()
    proc = _run_ps(
        BUILD_SCRIPT,
        "-SourceRoot",
        str(source),
        "-PythonEmbedRoot",
        str(embed),
        "-BrainVenvRoot",
        str(venv),
        "-OutputRoot",
        str(tmp_path / "out"),
        "-Version",
        "test.1",
        "-SkipHudBuild",
    )
    assert proc.returncode != 0
    assert f"required {missing}" in (proc.stdout + proc.stderr).lower()


def test_portable_build_rejects_unresolved_substrate_lfs_payloads(tmp_path: Path):
    source, embed, venv = _fixture_inputs(tmp_path)
    _write(
        source / "models" / "onnxs" / "dme" / "blocked.model",
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "0" * 64 + "\nsize 9000\n",
    )
    proc = _run_ps(
        BUILD_SCRIPT,
        "-SourceRoot",
        str(source),
        "-PythonEmbedRoot",
        str(embed),
        "-BrainVenvRoot",
        str(venv),
        "-OutputRoot",
        str(tmp_path / "out"),
        "-Version",
        "test.1",
        "-SkipHudBuild",
    )
    assert proc.returncode != 0
    assert "git lfs pointer" in (proc.stdout + proc.stderr).lower()


def test_portable_verifier_rejects_a_host_absolute_path(tmp_path: Path):
    stage, _archive = _build_fixture(tmp_path)
    _write(stage / "Omega" / "memory" / "indexes" / "coord_index.json", r'{"path":"F:\\Omega3.0\\memory\\record.json"}')
    _write(stage / "Omega" / "scripts" / "shutdown.py", 'ROOT = r"C:\\Users\\leeno\\Omega"\n')
    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode != 0
    assert "absolute path" in (proc.stdout + proc.stderr).lower()


def test_portable_verifier_rejects_a_host_absolute_path_in_python(tmp_path: Path):
    stage, _archive = _build_fixture(tmp_path)
    _write(stage / "Omega" / "scripts" / "shutdown.py", 'ROOT = r"C:\\Users\\leeno\\Omega"\n')
    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode != 0
    assert "absolute path" in (proc.stdout + proc.stderr).lower()


def test_portable_verifier_rejects_a_host_absolute_path_in_shipped_json(tmp_path: Path):
    stage, _archive = _build_fixture(tmp_path)
    _write(stage / "Omega" / "config" / "runtime.json", '{"root":"C:\\\\Users\\\\builder\\\\Omega"}\n')
    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode != 0
    assert "absolute path" in (proc.stdout + proc.stderr).lower()


def test_portable_verifier_accepts_uri_scheme_literals(tmp_path: Path):
    stage, _archive = _build_fixture(tmp_path)
    _write(
        stage / "Omega" / "scripts" / "uri.py",
        'url = "%s://" % scheme\nendpoint = "https://tmp/example"\n',
    )
    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_portable_verifier_accepts_lowercase_api_routes(tmp_path: Path):
    stage, _archive = _build_fixture(tmp_path)
    _write(
        stage / "Omega" / "skills" / "billing.py",
        'endpoint = "/users/me/invoices"\n',
    )
    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_portable_verifier_rejects_factory_memory_residue(tmp_path: Path):
    stage, _archive = _build_fixture(tmp_path)
    _write(
        stage / "Omega" / "memory" / "400_situational" / "operator" / "leaked.json",
        '{"id":"4.01.leaked","coord":[4,1,0]}\n',
    )
    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode != 0
    assert "factory memory" in (proc.stdout + proc.stderr).lower()


def test_portable_verifier_does_not_treat_third_party_examples_as_omega_paths(
    tmp_path: Path,
):
    stage, _archive = _build_fixture(tmp_path)
    _write(
        stage / "Omega" / ".venv-brain" / "Lib" / "site-packages" / "vendor.py",
        'example = r"C:\\Users\\<user>\\AppData\\Local"\n',
    )
    _write(
        stage / "Omega" / "python-3.13-embed" / "Lib" / "site-packages" / "vendor.py",
        'example = "/tmp/run/user/1000"\n',
    )
    _write(
        stage / "Omega" / "browser_agent_extension" / "src" / "shared" / "vendor" / "wasm.js",
        'const virtualRoot = "/tmp/virtual";\n',
    )
    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_portable_verifier_rejects_developer_python_entrypoints(tmp_path: Path):
    stage, _archive = _build_fixture(tmp_path)
    _write(stage / "Omega" / ".venv-brain" / "Scripts" / "pytest.exe")

    proc = _run_ps(VERIFY_SCRIPT, "-ReleaseRoot", str(stage), "-SkipLaunch")

    assert proc.returncode != 0
    assert "runtime interpreter" in (proc.stdout + proc.stderr).lower()


def test_portable_verifier_accepts_the_checksummed_archive(tmp_path: Path):
    _stage, archive = _build_fixture(tmp_path)
    proc = _run_ps(VERIFY_SCRIPT, "-Archive", str(archive), "-SkipLaunch")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "verification passed" in proc.stdout.lower()


def test_portable_verifier_imports_native_runtime_packages_with_host_path_stripped():
    source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert '$env:PATH = "$env:SystemRoot\\System32;$env:SystemRoot"' in source
    assert "import pathlib,cryptography,numpy,onnxruntime,spacy,sys,torch" in source


def test_installer_verifies_checksum_and_preserves_only_declared_user_state(tmp_path: Path):
    _stage, archive = _build_fixture(tmp_path)
    checksum_path = archive.with_suffix(".zip.sha256")
    install_root = tmp_path / "installed product"
    _write(install_root / "Omega" / "guardian" / "state" / "connections.json", "preserve\n")
    _write(install_root / "Omega" / "guardian" / "logs" / "old.log", "discard\n")
    _write(install_root / "Omega" / "memory" / "000_identity" / "identity.json", "preserve\n")
    _write(install_root / "Omega" / "memory" / "engine" / "release.py", "VERSION = 'old'\n")

    proc = _run_ps(
        INSTALL_SCRIPT,
        "-Archive",
        str(archive),
        "-ChecksumPath",
        str(checksum_path),
        "-InstallDir",
        str(install_root),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        install_root / "Omega" / "guardian" / "state" / "connections.json"
    ).read_text(encoding="utf-8") == "preserve\n"
    assert not (install_root / "Omega" / "guardian" / "logs" / "old.log").exists()
    assert (install_root / "Omega" / "boot.py").is_file()
    assert (
        install_root / "Omega" / "memory" / "000_identity" / "identity.json"
    ).read_text(encoding="utf-8") == "preserve\n"
    assert (
        install_root / "Omega" / "memory" / "engine" / "release.py"
    ).read_text(encoding="utf-8") == "VERSION = 'new'\n"

    checksum_path.write_text("0" * 64 + f"  {archive.name}\n", encoding="utf-8")
    rejected = _run_ps(
        INSTALL_SCRIPT,
        "-Archive",
        str(archive),
        "-ChecksumPath",
        str(checksum_path),
        "-InstallDir",
        str(tmp_path / "must not install"),
    )
    assert rejected.returncode != 0
    assert "checksum" in (rejected.stdout + rejected.stderr).lower()


def test_release_sources_contain_no_recorded_developer_absolute_paths():
    paths = [
        OMEGA_ROOT / "scripts" / "codex_link.py",
    ]
    offenders = {
        str(path.relative_to(OMEGA_ROOT)): line
        for path in paths
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if "F:\\Omega3.0" in line or "C:\\Users\\leeno" in line
    }
    assert not offenders


def test_model_manifest_contains_no_host_absolute_model_sources():
    for relative_path in ("models/manifest.json", "models/README.md"):
        text = (OMEGA_ROOT / relative_path).read_text(encoding="utf-8")
        assert "F:/" not in text, relative_path
        assert "F:\\" not in text, relative_path


def test_shipped_skill_docs_contain_no_host_user_paths():
    text = (OMEGA_ROOT / "skills" / "devops-workflow" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "/root/" not in text


def test_release_checksum_is_the_archive_sha256(tmp_path: Path):
    _stage, archive = _build_fixture(tmp_path)
    checksum = archive.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    assert checksum == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_installer_is_omega_specific_not_the_retired_product():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8", errors="replace").lower()
    assert "codebase-memory-mcp" not in text
    assert "omega" in text
