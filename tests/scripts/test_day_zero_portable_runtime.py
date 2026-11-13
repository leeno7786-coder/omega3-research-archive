"""Factory reset coverage for the sibling portable runtimes on the SSD.

The product tree is not the whole product.  ``Omega.exe`` launches LM Studio from
``<ssd-root>/LM Studio`` (``omega_launcher.LM_STUDIO_ROOT_RELATIVE``), and that
runtime keeps the operator's conversations, identity, OAuth credentials and
caches BESIDE ``OMEGA_ROOT`` — where every existing wipe in day-zero is blind to
them.  A factory ship must not carry the previous operator's chat history, and
§0 of the law makes that a product rule, not a preference.

The split under test: operator state goes, the shipped application and its model
weights stay (``extensions/`` alone is ~9.5 GB of engines and weights here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.day_zero_reset as day_zero_reset


def _seed_portable_ssd(root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Build a miniature SSD: product tree, LM Studio (Windows), LM-Studio-Linux.

    Returns ``(operator_state, shipped)`` — the paths a factory reset must remove
    and the paths it must leave untouched.
    """
    product = root / "Omega"
    (product / "core").mkdir(parents=True)
    (product / "boot.py").write_text("# product marker\n", encoding="utf-8")

    studio = root / "LM Studio"
    internal = studio / ".internal"
    linux = root / "LM-Studio-Linux"

    operator_state = {
        "conversation": studio / "conversations" / "1789213972447.conversation.json",
        "mcp_oauth": studio / "credentials" / "mcp-oauth" / "token.json",
        "mcp_config": studio / "mcp.json",
        "identity": internal / "local-identity.json",
        "api_key": internal / "lms-key-2",
        "prediction_history": internal / "api-prediction-history" / "run.json",
        "retrieval": internal / "retrieval-sessions" / "session.json",
        "parsed_docs": internal / "parsed-documents-cache" / "doc.bin",
        "rag_chunks": internal / "cached-rag-pipeline-chunks" / "chunk.bin",
        "attached_file": internal / "files" / "operator-upload.pdf",
        "ui_state": internal / "ui-state" / "global.json",
        "hotkeys": internal / "global-hotkeys" / "map.json",
        "projects": internal / "projects-registry.json",
        "downloads": internal / "download-jobs-info.json",
        "single_downloads": internal / "single-downloads-info.json",
        "conversation_config": internal / "conversation-config.json",
        "server_logs": internal / "server-logs-state.json",
        "synced_mcp": internal / "last-synced-mcp-state.json",
        "artifact_perms": internal / "artifact-permissions-list.json",
        "extension_pack": internal / "persistent-extension-pack-state.json",
        "pid_lock": internal / "llmster-pid.lock",
        "model_defaults": internal / "user-concrete-model-default-config" / "m.gguf.json",
        "path_backups": internal / "portable-path-backups" / "20260913" / "state.json",
        "temp_download": internal / "temp-downloads" / "partial.bin",
        # Written by Omega's own Linux start command (omega_launcher.LINUX_STARTUP_COMMAND).
        "linux_log": linux / "lm-studio.log",
    }
    shipped = {
        "app": studio / "LM Studio.exe",
        "launcher": studio / "Launch Portable.cmd",
        "locale": studio / "locales" / "en-US.pak",
        "resources": studio / "resources" / "app" / "package.json",
        "engine": studio / "extensions" / "backends" / "llama.dll",
        "engine_weights": studio / "extensions" / "models" / "engine.gguf",
        "hub_manifest": studio / "hub" / "models" / "manifest.json",
        "bundled_weights": internal / "bundled-models" / "shipped.gguf",
        "install_location": internal / "app-install-location.json",
        "portable_marker": internal / "portable-state.json",
        "engine_index": internal / "internal-engine-index.json",
        "linux_appimage": linux / "lm-studio.AppImage",
    }
    for path in (*operator_state.values(), *shipped.values()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return operator_state, shipped


def test_factory_reset_wipes_sibling_portable_runtime_operator_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_state, shipped = _seed_portable_ssd(tmp_path)
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", tmp_path / "Omega")

    report = day_zero_reset.wipe_portable_runtime_state()

    still_there = sorted(name for name, path in operator_state.items() if path.exists())
    assert still_there == [], f"operator state survived the factory reset: {still_there}"
    gone = sorted(name for name, path in shipped.items() if not path.exists())
    assert gone == [], f"the factory reset destroyed shipped files: {gone}"
    assert (tmp_path / "LM Studio").is_dir(), "the runtime itself is not removed"
    assert report["removed_files"] + report["removed_dirs"] > 0
    assert "LM Studio" in report["runtimes"]


def test_portable_runtime_wipe_is_idempotent_and_absence_is_normal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent runtime is "nothing to wipe", never a failure (law §2.1 read side)."""
    _seed_portable_ssd(tmp_path)
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", tmp_path / "Omega")
    day_zero_reset.wipe_portable_runtime_state()

    second = day_zero_reset.wipe_portable_runtime_state()

    assert (second["removed_files"], second["removed_dirs"]) == (0, 0)

    bare = tmp_path / "bare"
    (bare / "Omega" / "core").mkdir(parents=True)
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", bare / "Omega")
    empty = day_zero_reset.wipe_portable_runtime_state()
    assert (empty["removed_files"], empty["removed_dirs"], empty["runtimes"]) == (0, 0, [])


def test_portable_ssd_root_matches_the_launchers_own_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSD root is where ``Omega.exe`` lives, not the product tree."""
    _seed_portable_ssd(tmp_path)
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", tmp_path / "Omega")
    assert day_zero_reset.portable_ssd_root() == tmp_path

    # A product tree with no sibling runtime is its own root: nothing outside it is ours.
    solo = tmp_path / "solo" / "Omega3.0"
    (solo / "core").mkdir(parents=True)
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", solo)
    assert day_zero_reset.portable_ssd_root() == solo


def test_reset_never_follows_a_runtime_target_out_of_the_ssd_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every removal is proven to be a child of its runtime before it happens."""
    _seed_portable_ssd(tmp_path)
    outside = tmp_path.parent / "outside-the-ssd"
    outside.mkdir(exist_ok=True)
    keep = outside / "operator-document.pdf"
    keep.write_bytes(b"not ours")
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", tmp_path / "Omega")

    day_zero_reset.wipe_portable_runtime_state()

    assert keep.exists(), "a wipe reached outside the SSD root"


def test_day_zero_runs_the_portable_runtime_wipe() -> None:
    """The step is wired into the reset, not merely available to it."""
    source = Path(day_zero_reset.__file__).read_text(encoding="utf-8")
    assert "def wipe_portable_runtime_state(" in source
    main_body = source.split("def main(", 1)[1]
    assert "wipe_portable_runtime_state()" in main_body, (
        "wipe_portable_runtime_state must be called from main(), not only defined"
    )
