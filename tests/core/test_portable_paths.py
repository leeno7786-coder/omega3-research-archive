from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT
from auto_loop.execute.brain_skill_executor import (
    _HOST_TOOLCHAIN_SCRUB,
    _portable_subprocess_env,
)


_DRIVE_ABSOLUTE = re.compile(r"(?m)^[^#\r\n]*[A-Za-z]:[\\/]")


def test_brain_venv_metadata_is_relative_to_the_bundled_embed() -> None:
    venv = OMEGA_ROOT / ".venv-brain"
    cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    pth = (venv / "Scripts" / "python313._pth").read_text(encoding="utf-8")

    assert "home = ..\\python-3.13-embed" in cfg
    assert not _DRIVE_ABSOLUTE.search(cfg)
    assert pth.splitlines() == [
        "python313.zip",
        ".",
        r"..\Lib\site-packages",
        r"..\..",
        "import site",
    ]


def test_portable_runtime_assets_are_derived_from_omega_root() -> None:
    from core.paths import PORTABLE_RUNTIME_ROOT, RUNTIME_BIN_ROOT, RUNTIME_MODELS_ROOT

    assert PORTABLE_RUNTIME_ROOT == OMEGA_ROOT / "runtime"
    assert RUNTIME_MODELS_ROOT == OMEGA_ROOT / "models"
    assert RUNTIME_BIN_ROOT == OMEGA_ROOT / "runtimes" / "bin"


def test_portable_python_path_is_derived_from_the_product_root() -> None:
    from core.paths import portable_python_path

    assert portable_python_path(platform="win32") == (
        OMEGA_ROOT / ".venv-brain" / "Scripts" / "python.exe"
    )
    assert portable_python_path(OMEGA_ROOT / "moved", platform="win32") == (
        OMEGA_ROOT / "moved" / ".venv-brain" / "Scripts" / "python.exe"
    )


def test_linux_portable_python_is_the_bundled_linux_interpreter() -> None:
    """Linux has no embeddable distribution and a venv's pyvenv.cfg ``home`` is absolute,
    so the SSD carries one self-locating CPython whose own site-packages is the package
    environment. It never resolves to a host interpreter or to the Windows venv."""
    from core.paths import portable_python_path

    assert portable_python_path(platform="linux") == (
        OMEGA_ROOT / "python-3.13-linux" / "bin" / "python3"
    )
    assert portable_python_path(OMEGA_ROOT / "moved", platform="linux") == (
        OMEGA_ROOT / "moved" / "python-3.13-linux" / "bin" / "python3"
    )


def test_runtime_child_launchers_use_the_product_venv() -> None:
    sources = (
        OMEGA_ROOT / "runtime" / "supervisor.py",
        OMEGA_ROOT / "hud" / "server.py",
        OMEGA_ROOT / "omega_loop" / "episode" / "workspace_tests.py",
        OMEGA_ROOT / "mcp_platform" / "transports" / "stdio.py",
    )

    for path in sources:
        source = path.read_text(encoding="utf-8")
        assert "portable_python_path" in source, path
        assert "sys.executable" not in source, path


def test_hud_audio_uploads_stay_inside_the_product_tree() -> None:
    source = (OMEGA_ROOT / "hud" / "server.py").read_text(encoding="utf-8")

    assert "STAGING_ROOT" in source
    assert "NamedTemporaryFile" not in source


def test_connector_secret_store_cannot_follow_the_external_operator_workspace(monkeypatch) -> None:
    from runtime import brain_cloud_config
    import core.paths as core_paths

    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", r"C:\\Users\\someone\\workspace")
    assert brain_cloud_config._connector_store_root() == (
        core_paths.OMEGA_ROOT / "guardian" / "state" / "connectors"
    )


def test_auto_child_commands_use_the_machines_toolchain() -> None:
    """Project commands run on the ATTACHED MACHINE's toolchain (operator rule 2026-09-15).

    Omega's portable interpreter used to come first on PATH. It is built from the Windows
    embeddable distribution, so the working directory is not on its sys.path (a project's
    own module never imported) and its pip.exe exits 1 printing nothing; the agent looped
    on both for a whole build. The machine's PATH answers instead. Omega's dependencies
    stay safe a different way: the variables that would drag its interpreter into a host
    python are scrubbed, nothing installs into its venv from here, and its own interpreter
    remains reachable as OMEGA_TEST_PYTHON for callers that explicitly want it.

    This replaces test_auto_child_commands_prioritize_the_local_brain_venv, which pinned
    the venv-first PATH that 3c63a680 deliberately retired.
    """
    env = _portable_subprocess_env()

    assert env["PATH"] == os.environ.get("PATH", ""), "the machine's PATH is inherited as-is"
    assert env["OMEGA_ROOT"] == str(OMEGA_ROOT)
    for name in _HOST_TOOLCHAIN_SCRUB:
        assert name not in env, f"{name} would drag Omega's interpreter into a host python"

    scripts = OMEGA_ROOT / ".venv-brain" / ("Scripts" if os.name == "nt" else "bin")
    if scripts.is_dir():
        assert Path(env["OMEGA_TEST_PYTHON"]).parent.resolve() == scripts.resolve()


def test_live_skill_and_retina_resolvers_do_not_default_to_host_home() -> None:
    files = (
        OMEGA_ROOT / "hud" / "qwen_skills.py",
        OMEGA_ROOT / "meta_loop" / "skill" / "run.py",
        OMEGA_ROOT / "runtime" / "qwen_skills.py",
        OMEGA_ROOT / "memory" / "ettin_encoder.py",
    )

    for path in files:
        assert "Path.home()" not in path.read_text(encoding="utf-8")


def test_coord_index_paths_cannot_escape_the_active_memory_tree(monkeypatch, tmp_path: Path) -> None:
    from memory.engine import walker

    memory_root = tmp_path / "profile" / "memory"
    inside = memory_root / "000_identity" / "record.json"
    outside = tmp_path / "other-profile" / "memory" / "record.json"
    inside.parent.mkdir(parents=True)
    outside.parent.mkdir(parents=True)
    inside.write_text("{}", encoding="utf-8")
    outside.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(walker, "MEMORY_ROOT", memory_root)

    assert walker.resolve_coord_index_path(inside) == inside.resolve()
    assert walker.resolve_coord_index_path(outside) is None


def test_coord_index_writer_refuses_a_path_outside_the_profile(tmp_path: Path) -> None:
    from memory.engine.reindex import profile_relative_path

    memory_root = tmp_path / "profile" / "memory"
    with pytest.raises(ValueError, match="outside"):
        profile_relative_path(tmp_path / "other-profile" / "record.json", memory_root=memory_root)


def test_retired_standalone_agent_trees_are_absent() -> None:
    assert not (OMEGA_ROOT / "qwen-agent-tui").exists()
    assert not (OMEGA_ROOT / "omega_cli_ts").exists()
    assert not (OMEGA_ROOT / "skills" / "hud-dev-pane-operator").exists()


def test_hud_does_not_open_the_retired_agent_transport() -> None:
    paths = (
        OMEGA_ROOT / "hud" / "ui-src",
        OMEGA_ROOT / "hud" / "omega_interface.html",
    )
    retired = ("omega_cli_ts", "qwen-agent-tui", "/ws/cli", ":8092", "/api/dev/launch")
    for root in paths:
        files = (root,) if root.is_file() else root.rglob("*.js")
        for path in files:
            text = path.read_text(encoding="utf-8")
            assert not any(marker in text for marker in retired), path
