from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT
from auto_loop.execute import brain_skill_executor
from scripts import codex_link
from scripts import omega_launcher


_BARE_OMEGA_PYTHON_COMMAND = re.compile(
    r"(?<![A-Za-z0-9_.\\/-])python(?:3)?(?:\.exe)?\s+(?:(?:-m\s+)?(?:boot\.py|hud|scripts))",
    re.IGNORECASE,
)


# Same host-path detector as scripts/verify_portable.ps1 Test-HostAbsolutePath:
# drive-rooted paths, but not URI schemes such as ``https://``.
_HOST_DRIVE_PATH = re.compile(
    r"(?<![A-Za-z0-9_?])[A-Za-z]:(?:\\+|/(?!/))(?=[A-Za-z0-9._ -]+(?:\\+|/))"
)
_HOST_UNIX_PATH = re.compile(r'(?<![A-Za-z0-9_/:])/(home|Users|root|tmp)/[^\s"\']+')

_SHIPPED_ROOTS = (
    "auto_loop",
    "autonomic",
    "browser_agent",
    "browser_agent_extension",
    "cognition",
    "config",
    "core",
    "deepsleep_loop",
    "global_loop",
    "guardian",
    "hud",
    "knowledge_extractor",
    "mcp_platform",
    "memory",
    "meta_loop",
    "omega_loop",
    "plugin_platform",
    "policy",
    "pressure_loop",
    "runtime",
    "schema",
    "scripts",
    "sensory",
    "skills",
    "models",
    "runtimes",
)
_SHIPPED_ROOT_FILES = (
    "boot.py",
    "cognition_chain.py",
    "install.ps1",
    "LICENSE",
    "README.md",
    "PROFILE_README.md",
    "requirements.txt",
    "skills-lock.json",
    "playwright.config.ts",
)
_SHIPPED_TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".cfg",
    ".conf",
    ".ini",
    ".js",
    ".json",
    ".mjs",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
_SKIP_SHIPPED_PARTS = {
    ".git",
    ".github",
    ".agents",
    ".venv-brain",
    "python-3.13-embed",
    "node_modules",
    "tests",
    "site-packages",
    "vendor",
    "__pycache__",
    "indexes",
    "taxonomy_store",
    "checkpoints",
    "state",
    "logs",
    "ledger",
    "telemetry",
    "audit",
    "100_personas",
    "200_knowledge",
    "300_reasoning",
    "400_situational",
    "500_tools",
    "600_journal",
    "700_vision",
    "800_coding",
    "900_temporal",
    "1000_workflows",
    "1100_dev",
}
_MAX_SCAN_BYTES = 1_000_000


def _host_absolute_path_hits(text: str) -> list[str]:
    hits = [match.group(0) for match in _HOST_DRIVE_PATH.finditer(text)]
    hits.extend(match.group(0) for match in _HOST_UNIX_PATH.finditer(text))
    return hits


def _shipped_text_files() -> Iterator[Path]:
    for name in _SHIPPED_ROOT_FILES:
        path = OMEGA_ROOT / name
        if path.is_file():
            yield path
    for root_name in _SHIPPED_ROOTS:
        root = OMEGA_ROOT / root_name
        if not root.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SKIP_SHIPPED_PARTS and not name.startswith(".venv")
            )
            base = Path(directory)
            if _SKIP_SHIPPED_PARTS & set(base.relative_to(OMEGA_ROOT).parts):
                continue
            for filename in sorted(filenames):
                path = base / filename
                if path.suffix.lower() not in _SHIPPED_TEXT_SUFFIXES:
                    continue
                relative = path.relative_to(OMEGA_ROOT)
                # Operator memory records and generated bundles are not product
                # source. A live coord_index can lawfully name an operator path.
                if relative.parts[:1] == ("memory",) and path.suffix.lower() != ".py":
                    continue
                if relative.parts[:2] == ("hud", "omega_interface.html"):
                    continue
                try:
                    if path.stat().st_size > _MAX_SCAN_BYTES:
                        continue
                except OSError:
                    continue
                yield path


def test_the_guard_recognises_a_host_drive_letter() -> None:
    assert _host_absolute_path_hits(r"J:\Omega3.0\.venv-brain\Scripts\python.exe")
    assert _host_absolute_path_hits(r"home = C:\Users\builder\Omega3.0\python-3.13-embed")
    assert _host_absolute_path_hits("root, so ``J:\\\\models\\\\qwen3-tts``.")
    assert not _host_absolute_path_hits("https://docs.python.org/3/")
    assert not _host_absolute_path_hits(r".\.venv-brain\Scripts\python.exe scripts\boot.py")
    assert not _host_absolute_path_hits("root: ../models/qwen3-tts")


def test_shipped_sources_contain_no_host_absolute_paths() -> None:
    files = list(_shipped_text_files())
    assert files, "shipped-source scan found no files — the walker is looking in the wrong place"

    offenders: dict[str, list[str]] = {}
    for path in files:
        hits = _host_absolute_path_hits(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            offenders[str(path.relative_to(OMEGA_ROOT))] = sorted(set(hits))
    assert not offenders, f"shipped source contains host-bound paths: {offenders}"


def test_intent_router_records_paths_relative_to_the_repo(tmp_path: Path) -> None:
    from core.paths import product_relative_path

    repo = tmp_path / "Omega3.0"
    target = repo / "models" / "intent"
    target.mkdir(parents=True)

    recorded = product_relative_path(target, omega_root=repo)
    assert recorded == str(Path("models") / "intent")
    assert not _host_absolute_path_hits(str(recorded))

    with pytest.raises(ValueError, match="escapes"):
        product_relative_path(tmp_path / "other" / "models", omega_root=repo)


def test_production_python_does_not_default_to_host_home() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _shipped_text_files():
        if path.suffix.lower() != ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [marker for marker in ("Path.home()",) if marker in text]
        if hits:
            offenders[str(path.relative_to(OMEGA_ROOT))] = hits
    assert not offenders, f"production code defaults to the host home directory: {offenders}"


def test_codex_link_requires_an_explicit_portable_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_CMD", raising=False)
    with pytest.raises(RuntimeError, match="CODEX_CMD"):
        codex_link.resolve_codex_command()

    monkeypatch.setenv("CODEX_CMD", r"D:\portable-tools\codex.exe")
    assert codex_link.resolve_codex_command() == r"D:\portable-tools\codex.exe"


def test_installer_does_not_default_into_the_host_user_profile() -> None:
    installer = (OMEGA_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "LOCALAPPDATA" not in installer
    assert "GetTempPath" not in installer
    assert "$scratch = Join-Path $parent" in installer


def test_release_signers_have_no_machine_profile_default() -> None:
    for name in ("sign_marketplace_catalog.py", "provision_oauth_profiles.py"):
        source = (OMEGA_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "LOCALAPPDATA" not in source


@pytest.mark.parametrize(
    "relative_path",
    (
        "scripts/cursor_bus_watcher.py",
        "scripts/devin_bus_watcher.py",
        "scripts/omega3_env_check.mjs",
        "scripts/omega3_review_swarm_runner.mjs",
        "scripts/install_systemd_user.sh",
        "scripts/systemd/omega.service.in",
        "scripts/omega_bus_peek.sh",
        "scripts/setfit_dataset",
    ),
)
def test_retired_paths_are_absent(relative_path: str) -> None:
    assert not (OMEGA_ROOT / relative_path).exists()


def test_vastai_skill_has_no_implicit_host_home_or_temp_paths() -> None:
    offenders = {}
    for path in (OMEGA_ROOT / "skills" / "vastai").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [
            marker
            for marker in ("Path.home()", "expanduser(", 'DEFAULT_BUNDLE_DIR = "/tmp"')
            if marker in text
        ]
        if hits:
            offenders[str(path.relative_to(OMEGA_ROOT))] = hits
    assert not offenders, f"VastAI skill has host-local path defaults: {offenders}"


def test_host_package_manager_mcp_config_is_not_part_of_omega() -> None:
    assert not (OMEGA_ROOT / ".mcp.json").exists()


def test_browser_extension_has_no_machine_local_mcp_config() -> None:
    assert not (OMEGA_ROOT / "browser_agent_extension" / ".cursor" / "mcp.json").exists()


@pytest.mark.parametrize(
    "relative_path",
    (
        "boot.py",
        "hud/README.md",
        "hud/spawner.py",
        "hud/standalone.py",
        "hud/scripts/build_hud.py",
        "hud/ui-src/README.md",
        "scripts/README.md",
        "scripts/agent_bus.py",
        "scripts/hud-only.bat",
        "scripts/routing_grill_1000.py",
        "scripts/runtime_direct.py",
        "scripts/seed_identity_lobe.py",
        "scripts/shutdown.py",
    ),
)
def test_shipped_launch_instructions_use_the_portable_interpreter(relative_path: str) -> None:
    source = (OMEGA_ROOT / relative_path).read_text(encoding="utf-8")
    assert not _BARE_OMEGA_PYTHON_COMMAND.search(source), relative_path


@pytest.mark.skipif(os.name != "nt", reason="resolves the Windows shell; pathlib cannot fake Windows paths off Windows")
def test_command_executor_fails_when_the_required_shell_is_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(brain_skill_executor.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", r"C:\\missing-windows-root")

    with pytest.raises(FileNotFoundError, match="portable shell"):
        brain_skill_executor._shell_prefix()


def test_longmemeval_runner_has_no_machine_specific_default_paths() -> None:
    source = (OMEGA_ROOT / "benchmarks" / "longmemeval" / "run_eval.py").read_text(
        encoding="utf-8"
    )
    assert r'G:\LongMemEval' not in source
    assert "LONGMEMEVAL_ROOT" in source

    embed_source = (OMEGA_ROOT / "benchmarks" / "longmemeval" / "local_embed.py").read_text(
        encoding="utf-8"
    )
    assert "omega-runtime" not in embed_source


def test_omega_runtime_assets_are_owned_by_the_product_tree() -> None:
    source = (OMEGA_ROOT / "core" / "paths.py").read_text(encoding="utf-8")
    assert "OMEGA_ROOT.parent / \"omega-runtime\"" not in source
    assert "OMEGA_RUNTIME_ROOT" not in source
    assert omega_launcher.product_root(OMEGA_ROOT / "Omega.exe") == OMEGA_ROOT


def test_packaged_launcher_resolves_the_sibling_hidden_product_tree(monkeypatch, tmp_path) -> None:
    hidden_product = tmp_path / "Omega"
    marker = hidden_product / "config" / "portable_product.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(omega_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(omega_launcher.sys, "executable", str(tmp_path / "Omega.exe"))

    assert omega_launcher.product_root() == hidden_product.resolve()


def test_external_ssd_launcher_resolves_the_omega3_product_tree(monkeypatch, tmp_path) -> None:
    hidden_product = tmp_path / "Omega3.0" / "Omega"
    marker = hidden_product / "config" / "portable_product.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(omega_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(omega_launcher.sys, "executable", str(tmp_path / "Omega.exe"))

    assert omega_launcher.product_root() == hidden_product.resolve()


def test_root_launcher_prefers_flat_omega3_source_over_older_omega_package(
    monkeypatch, tmp_path
) -> None:
    old_package = tmp_path / "Omega"
    current_product = tmp_path / "Omega3.0"
    for product in (old_package, current_product):
        marker = product / "config" / "portable_product.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(omega_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(omega_launcher.sys, "executable", str(tmp_path / "Omega.exe"))

    assert omega_launcher.product_root() == current_product.resolve()


def test_retired_external_runtime_is_not_referenced_by_day_zero() -> None:
    source = (OMEGA_ROOT / "scripts" / "day_zero_reset.py").read_text(encoding="utf-8")

    assert "OMEGA_RUNTIME_ROOT" not in source
    assert "omega-runtime" not in source.lower()


def test_portable_release_has_the_current_product_and_runtime_layout() -> None:
    for relative_path in ("scripts/build_portable.ps1", "scripts/verify_portable.ps1", "install.ps1"):
        source = (OMEGA_ROOT / relative_path).read_text(encoding="utf-8")
        assert "omega-runtime" not in source.lower(), relative_path
    source = (OMEGA_ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8")
    assert "Omega.exe" in source
    assert "config/portable_product.json" in source


def test_release_scripts_do_not_require_powershell_7_relative_path_api() -> None:
    for relative_path in ("scripts/build_portable.ps1", "scripts/verify_portable.ps1"):
        source = (OMEGA_ROOT / relative_path).read_text(encoding="utf-8")
        assert "GetRelativePath" not in source, relative_path


def test_portable_builder_scopes_git_to_the_selected_source_root() -> None:
    source = (OMEGA_ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8")
    assert '"safe.directory=$Root"' in source


def test_portable_builder_uses_windows_powershell_5_encoding_values() -> None:
    source = (OMEGA_ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8")
    assert "utf8NoBOM" not in source


def test_portable_release_tools_do_not_require_host_specific_hash_or_temp_cmdlets() -> None:
    for relative_path in ("scripts/build_portable.ps1", "scripts/verify_portable.ps1"):
        source = (OMEGA_ROOT / relative_path).read_text(encoding="utf-8")
        assert "Get-FileHash" not in source, relative_path
        assert "GetTempPath" not in source, relative_path


def test_live_surfaces_do_not_name_the_retired_runtime_service() -> None:
    roots = [
        OMEGA_ROOT / "runtime",
        OMEGA_ROOT / "memory" / "dme",
        OMEGA_ROOT / "hud" / "server.py",
    ]
    offenders: list[str] = []
    for root in roots:
        paths = (root,) if root.is_file() else root.rglob("*.py")
        for path in paths:
            if "omega-runtime" in path.read_text(encoding="utf-8", errors="replace").lower():
                offenders.append(str(path.relative_to(OMEGA_ROOT)))
    assert not offenders, f"retired runtime identity remains in live surfaces: {offenders}"


def test_lm_studio_launcher_is_resolved_from_the_ssd_root() -> None:
    ssd_root = OMEGA_ROOT.parent / "portable ssd"
    expected = ssd_root / "LM Studio" / "Launch Portable.cmd"
    assert omega_launcher.lm_studio_launcher_path(ssd_root) == expected


def test_linux_lm_studio_runtime_is_resolved_from_the_ssd_root() -> None:
    ssd_root = OMEGA_ROOT.parent / "portable ssd"
    expected = ssd_root / "LM-Studio-Linux" / "run-ai.sh"
    assert omega_launcher.linux_runtime_script_path(ssd_root) == expected


def test_lm_studio_launcher_does_not_fall_back_to_a_host_install(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="Launch Portable.cmd"):
        omega_launcher.require_lm_studio_launcher(tmp_path)


def test_windows_hud_launcher_routes_through_omega_exe() -> None:
    source = (OMEGA_ROOT / "scripts" / "hud-only.bat").read_text(encoding="utf-8")
    assert "Omega.exe" in source
    assert "--launch-hud" in source
    assert "OMEGA_HUD_OPEN_BROWSER=1" in source
    assert ".venv-brain\\Scripts\\python.exe" not in source


def test_windows_lm_studio_launcher_routes_through_omega_exe_when_shipped() -> None:
    launcher = OMEGA_ROOT.parent / "LM Studio" / "Launch Portable.cmd"
    if not launcher.is_file():
        pytest.skip("LM Studio payload is not present in this source checkout")
    source = launcher.read_text(encoding="utf-8")
    assert "Omega.exe" in source
    assert "--launch-lm-studio" in source
    assert "OMEGA_BROKER_AUTHORIZED" in source
    assert "dir /b /ad /a" in source


def test_startup_exe_builder_uses_the_portable_brain_interpreter() -> None:
    builder = (OMEGA_ROOT / "scripts" / "build_launcher.ps1").read_text(encoding="utf-8")
    assert "Scripts\\python.exe" in builder
    assert "-m PyInstaller" in builder
    assert "--windowed" in builder
    assert "Omega.exe" in builder
    assert "scripts.lmstudio_updater" in builder


def test_launcher_reports_a_boot_failure_instead_of_disappearing(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        omega_launcher,
        "run_setup_wizard",
        lambda: (_ for _ in ()).throw(RuntimeError("boot failed")),
    )
    monkeypatch.setattr(omega_launcher, "show_launcher_error", seen.append)

    omega_launcher.main([])

    assert seen == ["Omega could not start: boot failed"]
