from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from browser_agent.bootstrap import (
    EDGE_DEBUG_PROFILE_RELATIVE,
    BrowserBootstrap,
    BrowserBootstrapError,
)
from core.paths import OMEGA_ROOT


def _write_driver_bundle(tmp_path, *, platform_key: str = "windows-x64", version: str = "153.0.4234.32"):
    bundle_root = tmp_path / "runtimes" / "browser" / "edgedriver"
    bundle_root.mkdir(parents=True)
    archive_name = {
        "windows-x64": "edgedriver_win64.zip",
        "windows-x86": "edgedriver_win32.zip",
        "linux-x64": "edgedriver_linux64.zip",
    }[platform_key]
    binary_name = "msedgedriver.exe" if platform_key.startswith("windows") else "msedgedriver"
    archive_path = bundle_root / archive_name
    with zipfile.ZipFile(archive_path, "w") as package:
        package.writestr(binary_name, b"driver fixture")
    manifest = {
        "version": version,
        "assets": {
            platform_key: {
                "archive": archive_name,
                "binary": binary_name,
                "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            }
        },
    }
    (bundle_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _bootstrap_with_bundled_driver(tmp_path, *, platform_system: str, platform_machine: str):
    _write_driver_bundle(
        tmp_path,
        platform_key=("windows-x64" if platform_system == "Windows" else "linux-x64"),
    )
    state = {"debugger": True, "driver": False}
    launches: list[tuple[str, ...]] = []

    def probe(kind: str, _address: str) -> bool:
        return state[kind]

    def launch(command: list[str], _cwd):
        launches.append(tuple(command))
        state["driver"] = True
        return object()

    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        edge_version_locator=lambda: "153.0.4234.32",
        probe=probe,
        launcher=launch,
        edge_running_checker=lambda _path: False,
        platform_system=lambda: platform_system,
        platform_machine=lambda: platform_machine,
    )
    return bootstrap, launches


def test_shipped_driver_archives_match_the_manifest() -> None:
    bundle_root = OMEGA_ROOT / "runtimes" / "browser" / "edgedriver"
    manifest = json.loads((bundle_root / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == "154.0.4258.37"
    for asset in manifest["assets"].values():
        archive = bundle_root / asset["archive"]
        assert archive.is_file()
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == asset["sha256"].lower()


def test_bootstrap_starts_portable_debug_edge_and_driver(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    _write_driver_bundle(tmp_path)
    state = {"debugger": False, "driver": False}
    launches: list[tuple[str, ...]] = []

    def probe(kind: str, _address: str) -> bool:
        return state[kind]

    def launch(command: list[str], _cwd):
        launches.append(tuple(command))
        if command[0] == "edge.exe":
            state["debugger"] = True
        if command[0].endswith("msedgedriver.exe"):
            state["driver"] = True
        return object()

    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        edge_version_locator=lambda: "153.0.4234.32",
        probe=probe,
        launcher=launch,
        edge_running_checker=lambda _path: False,
        platform_system=lambda: "Windows",
        platform_machine=lambda: "AMD64",
    )

    receipt = bootstrap.ensure(debugger_address="127.0.0.1:9222", webdriver_endpoint="http://127.0.0.1:9515")

    assert receipt == {
        "edge_started": True,
        "driver_started": True,
        "debugger_address": "127.0.0.1:9222",
        "webdriver_endpoint": "http://127.0.0.1:9515",
    }
    assert launches == [
        (
            "edge.exe",
            f"--user-data-dir={tmp_path / EDGE_DEBUG_PROFILE_RELATIVE}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "about:blank",
        ),
        (
            str(tmp_path / "runtimes" / "bin" / "edge-webdriver" / "bundled" / "windows-x64" / "msedgedriver.exe"),
            "--port=9515",
        ),
    ]
    assert (tmp_path / EDGE_DEBUG_PROFILE_RELATIVE).is_dir()


def test_bootstrap_fails_honestly_when_edge_is_missing(tmp_path) -> None:
    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: None,
        probe=lambda _kind, _address: False,
        launcher=lambda _command, _cwd: object(),
    )

    try:
        bootstrap.ensure(debugger_address="127.0.0.1:9222", webdriver_endpoint="http://127.0.0.1:9515")
    except BrowserBootstrapError as exc:
        assert str(exc) == "edge_executable_unavailable"
    else:
        raise AssertionError("missing Edge must not become a sandbox launch")


def test_bootstrap_provisions_a_missing_driver_into_the_omega_tree(tmp_path) -> None:
    bootstrap, launches = _bootstrap_with_bundled_driver(
        tmp_path,
        platform_system="Windows",
        platform_machine="AMD64",
    )

    receipt = bootstrap.ensure(debugger_address="127.0.0.1:9222", webdriver_endpoint="http://127.0.0.1:9515")

    assert receipt["driver_started"] is True
    assert launches == [
        (
            str(tmp_path / "runtimes" / "bin" / "edge-webdriver" / "bundled" / "windows-x64" / "msedgedriver.exe"),
            "--port=9515",
        )
    ]


def test_bootstrap_selects_the_bundled_win32_driver_on_x86(tmp_path) -> None:
    _write_driver_bundle(tmp_path, platform_key="windows-x86")
    state = {"debugger": True, "driver": False}
    launches: list[tuple[str, ...]] = []

    def launch(command: list[str], _cwd):
        launches.append(tuple(command))
        state["driver"] = True
        return object()

    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        edge_version_locator=lambda: "153.0.4234.32",
        probe=lambda kind, _address: state[kind],
        launcher=launch,
        edge_running_checker=lambda _path: False,
        platform_system=lambda: "Windows",
        platform_machine=lambda: "x86",
    )

    bootstrap.ensure(debugger_address="127.0.0.1:9222", webdriver_endpoint="http://127.0.0.1:9515")

    assert launches[0][0].endswith("windows-x86\\msedgedriver.exe")


def test_bootstrap_rejects_a_bundled_driver_for_a_different_edge_release(tmp_path) -> None:
    _write_driver_bundle(tmp_path, version="152.0.0.0")
    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        edge_version_locator=lambda: "153.0.4234.32",
        probe=lambda kind, _address: kind == "debugger",
        launcher=lambda _command, _cwd: object(),
        edge_running_checker=lambda _path: False,
        platform_system=lambda: "Windows",
        platform_machine=lambda: "AMD64",
    )

    with pytest.raises(BrowserBootstrapError, match="edgedriver_bundle_incompatible"):
        bootstrap.ensure(debugger_address="127.0.0.1:9222", webdriver_endpoint="http://127.0.0.1:9515")


def test_bootstrap_starts_an_isolated_debug_edge_when_personal_edge_is_running(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    _write_driver_bundle(tmp_path)
    launches: list[tuple[str, ...]] = []
    state = {"debugger": False, "driver": True}

    def launch(command, _cwd):
        launches.append(tuple(command))
        state["debugger"] = True
        return object()

    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        edge_version_locator=lambda: "153.0.4234.32",
        probe=lambda kind, _address: state[kind],
        launcher=launch,
        edge_running_checker=lambda _path: True,
        wait_s=0.001,
        sleep=lambda _seconds: None,
        platform_system=lambda: "Windows",
        platform_machine=lambda: "AMD64",
    )

    bootstrap.ensure(debugger_address="127.0.0.1:9222", webdriver_endpoint="http://127.0.0.1:9515")

    assert launches == [
        (
            "edge.exe",
            f"--user-data-dir={tmp_path / EDGE_DEBUG_PROFILE_RELATIVE}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "about:blank",
        )
    ]


def test_bootstrap_restarts_edge_only_with_explicit_approval(tmp_path) -> None:
    _write_driver_bundle(tmp_path)
    state = {"debugger": False, "driver": False}
    restarts: list[tuple[str, int]] = []
    launches: list[tuple[str, ...]] = []

    def restart(edge_path: str, port: int):
        restarts.append((edge_path, port))
        state["debugger"] = True
        return object()

    def launch(command: list[str], _cwd):
        launches.append(tuple(command))
        state["driver"] = True
        return object()

    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        edge_version_locator=lambda: "153.0.4234.32",
        probe=lambda kind, _address: state[kind],
        launcher=launch,
        edge_running_checker=lambda _path: True,
        edge_restarter=restart,
        platform_system=lambda: "Windows",
        platform_machine=lambda: "AMD64",
    )

    bootstrap.ensure(
        debugger_address="127.0.0.1:9222",
        webdriver_endpoint="http://127.0.0.1:9515",
        restart_existing_edge=True,
    )

    assert restarts == [("edge.exe", 9222)]
    assert launches[0][0].endswith("windows-x64\\msedgedriver.exe")


def test_bootstrap_opens_a_new_window_in_the_portable_debug_profile(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    launches: list[tuple[str, ...]] = []
    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        probe=lambda _kind, _address: True,
        launcher=lambda command, _cwd: launches.append(tuple(command)) or object(),
    )

    bootstrap.ensure_debugger(debugger_address="127.0.0.1:9222")
    bootstrap.open_new_window("http://127.0.0.1:13310")

    assert launches == [
        (
            "edge.exe",
            f"--user-data-dir={tmp_path / EDGE_DEBUG_PROFILE_RELATIVE}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--new-window",
            "http://127.0.0.1:13310",
        )
    ]


def test_bootstrap_uses_the_hud_url_for_the_first_debug_edge_page(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    _write_driver_bundle(tmp_path)
    launches: list[tuple[str, ...]] = []
    state = {"debugger": False}

    def launch(command: list[str], _cwd):
        launches.append(tuple(command))
        state["debugger"] = True
        return object()

    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        probe=lambda kind, _address: state[kind],
        launcher=launch,
        wait_s=0.001,
        sleep=lambda _seconds: None,
    )

    receipt = bootstrap.ensure_debugger(
        debugger_address="127.0.0.1:9222",
        initial_url="http://127.0.0.1:13310",
    )

    assert receipt["initial_url_opened"] is True
    assert launches == [
        (
            "edge.exe",
            f"--user-data-dir={tmp_path / EDGE_DEBUG_PROFILE_RELATIVE}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "http://127.0.0.1:13310",
        )
    ]


def test_bootstrap_refuses_a_window_open_without_a_prepared_debugger(tmp_path) -> None:
    bootstrap = BrowserBootstrap(
        omega_root=tmp_path,
        edge_locator=lambda: "edge.exe",
        launcher=lambda _command, _cwd: object(),
    )

    with pytest.raises(BrowserBootstrapError, match="browser_debugger_not_prepared"):
        bootstrap.open_new_window("http://127.0.0.1:13310")


def test_the_automation_profile_never_restores_the_last_session(tmp_path) -> None:
    """Operator 2026-09-15: Edge treated a forced stop as a crash and reopened Omega's tabs."""
    _write_driver_bundle(tmp_path)
    state = {"debugger": False, "driver": False}

    def probe(kind: str, _address: str) -> bool:
        return state[kind]

    def launch(command: list[str], _cwd):
        if command[0] == "edge.exe":
            prefs = json.loads((tmp_path / EDGE_DEBUG_PROFILE_RELATIVE / "Default" / "Preferences").read_text())
            assert prefs["session"]["restore_on_startup"] == 5, "the preference is pinned before Edge starts"
            assert prefs["profile"]["exit_type"] == "Normal"
            state["debugger"] = True
        if command[0].endswith("msedgedriver.exe"):
            state["driver"] = True
        return object()

    prefs_path = tmp_path / EDGE_DEBUG_PROFILE_RELATIVE / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(json.dumps({"session": {"restore_on_startup": 1}, "profile": {"exit_type": "Crashed"},
                                      "browser": {"keep": True}}), encoding="utf-8")
    bootstrap = BrowserBootstrap(
        omega_root=tmp_path, edge_locator=lambda: "edge.exe", edge_version_locator=lambda: "153.0.4234.32",
        probe=probe, launcher=launch, edge_running_checker=lambda _path: False,
        platform_system=lambda: "Windows", platform_machine=lambda: "AMD64",
    )

    bootstrap.ensure(debugger_address="127.0.0.1:9222", webdriver_endpoint="http://127.0.0.1:9515")

    assert json.loads(prefs_path.read_text())["browser"] == {"keep": True}, "other preferences survive"
