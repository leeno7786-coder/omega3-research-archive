"""Build-recipe guards for the signed real-SDK acceptance peer fixture."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.mcp_platform import test_current_client_acceptance as acceptance


@pytest.fixture
def compiler_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def compile_peer(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        dist = Path(command[command.index("--distpath") + 1])
        dist.mkdir(parents=True)
        # Only this recipe test substitutes compilation; the acceptance tests
        # still build and execute the real MCP SDK peer.
        (dist / "omega-mcp-fake.exe").write_bytes(b"compiled-peer-fixture")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(acceptance.subprocess, "run", compile_peer)
    return commands


def test_compiled_peer_extracts_outside_every_signed_package(
    tmp_path: Path,
    compiler_commands: list[list[str]],
) -> None:
    cache = tmp_path / "session-cache"
    first, *_ = acceptance._signed_python_package(tmp_path / "first-source", cache)
    second, *_ = acceptance._signed_python_package(tmp_path / "second-source", cache)

    assert len(compiler_commands) == 1
    command = compiler_commands[0]
    runtime_root = Path(command[command.index("--runtime-tmpdir") + 1])
    assert runtime_root.is_absolute()
    assert runtime_root.is_dir()
    assert runtime_root.is_relative_to(cache.resolve())
    assert not runtime_root.is_relative_to(first.resolve())
    assert not runtime_root.is_relative_to(second.resolve())
    expected_files = {
        Path("manifest.json"),
        Path("tool_catalog.json"),
        Path("resource_catalog.json"),
        Path("Scripts/omega-mcp-fake.exe"),
    }
    for package in (first, second):
        assert {
            path.relative_to(package) for path in package.rglob("*") if path.is_file()
        } == expected_files


def test_compiled_peer_cache_tracks_dependency_build_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compiler_commands: list[list[str]],
) -> None:
    cache = tmp_path / "session-cache"
    acceptance._signed_python_package(tmp_path / "first-source", cache)
    excluded = getattr(acceptance, "_PEER_EXCLUDED_MODULES", ())
    monkeypatch.setattr(
        acceptance,
        "_PEER_EXCLUDED_MODULES",
        (*excluded, "fixture_unused_module"),
        raising=False,
    )
    acceptance._signed_python_package(tmp_path / "second-source", cache)
    acceptance._signed_python_package(tmp_path / "third-source", cache)

    assert len(compiler_commands) == 2
    assert "fixture_unused_module" not in compiler_commands[0]
    command = compiler_commands[1]
    assert command[command.index("fixture_unused_module") - 1] == "--exclude-module"
