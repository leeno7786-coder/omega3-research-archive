"""User-profile Edge and local EdgeDriver bootstrap helpers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from core.paths import OMEGA_ROOT


class BrowserBootstrapError(RuntimeError):
    """Stable setup failure shown by the browser connection tool."""

    def __init__(self, reason: str, *, details: dict[str, object] | None = None) -> None:
        self.reason = str(reason)
        self.details = dict(details or {})
        super().__init__(self.reason)


_BUNDLED_DRIVER_ROOT = Path("runtimes") / "browser" / "edgedriver"
_EXTRACTED_DRIVER_ROOT = Path("runtimes") / "bin" / "edge-webdriver" / "bundled"
# Keep the automation profile on the same portable tree as Omega.  A separate
# profile is required so Chromium-based Edge never routes this debugger launch
# into the operator's personal session (which would drop the remote-debugging
# flag).  Reusing this Omega-owned profile for later URLs is intentional: the
# profile singleton hands them to the one debugger-enabled process.
EDGE_DEBUG_PROFILE_RELATIVE = (
    Path("guardian") / "state" / "browser" / "edge-debug-profile"
)
_MAX_DRIVER_ARCHIVE_BYTES = 64 * 1024 * 1024


def _address_port(address: str) -> int:
    parsed = urlsplit("http://" + str(address or ""))
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
        raise BrowserBootstrapError("browser_debugger_address_invalid")
    return parsed.port


def _endpoint_port(endpoint: str) -> int:
    parsed = urlsplit(str(endpoint or ""))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
        raise BrowserBootstrapError("webdriver_endpoint_invalid")
    return parsed.port


def _which_edge() -> str | None:
    for name in ("msedge.exe", "msedge", "microsoft-edge", "microsoft-edge-stable"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        import winreg

        locations = (
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        )
        for root, key_name in locations:
            try:
                with winreg.OpenKey(root, key_name) as key:
                    value, _kind = winreg.QueryValueEx(key, None)
                    if isinstance(value, str) and value:
                        return value
            except OSError:
                continue
    return None


def _discover_edge_version() -> str | None:
    if os.name == "nt":
        import winreg

        locations = (
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Edge\BLBeacon"),
        )
        for root, key_name in locations:
            try:
                with winreg.OpenKey(root, key_name) as key:
                    value, _kind = winreg.QueryValueEx(key, "version")
                    if isinstance(value, str) and value.count(".") == 3:
                        return value
            except OSError:
                continue

    edge_path = _which_edge()
    if not edge_path:
        return None
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "check": False,
        "text": True,
        "timeout": 5,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run([edge_path, "--version"], **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", f"{result.stdout}\n{result.stderr}")
    if match:
        return match.group(1)
    return None


def _edge_process_running(edge_path: str) -> bool:
    """Return whether the user's Edge executable is already running."""
    executable = Path(edge_path).name
    if os.name == "nt":
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "check": False,
            "text": True,
        }
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                ["tasklist.exe", "/FI", f"IMAGENAME eq {executable}", "/NH"],
                **kwargs,
            )
        except OSError as exc:
            raise BrowserBootstrapError("edge_process_state_unavailable") from exc
        if result.returncode != 0:
            raise BrowserBootstrapError("edge_process_state_unavailable")
        return executable.casefold() in str(result.stdout or "").casefold()

    pgrep = shutil.which("pgrep")
    if not pgrep:
        raise BrowserBootstrapError("edge_process_state_unavailable")
    try:
        result = subprocess.run([pgrep, "-x", executable], capture_output=True, check=False)
    except OSError as exc:
        raise BrowserBootstrapError("edge_process_state_unavailable") from exc
    if result.returncode not in {0, 1}:
        raise BrowserBootstrapError("edge_process_state_unavailable")
    return result.returncode == 0


def _driver_platform_key(*, system: str | None = None, machine: str | None = None) -> str:
    """Select one shipped archive; unsupported hosts fail instead of falling back."""
    system_name = (system or platform.system()).strip().lower()
    machine_name = (machine or platform.machine()).strip().lower()
    if system_name == "windows":
        wide_machine = (os.environ.get("PROCESSOR_ARCHITEW6432") or "").strip().lower()
        if machine_name in {"amd64", "x86_64", "x64", "arm64"} or wide_machine in {
            "amd64",
            "x86_64",
            "x64",
            "arm64",
        }:
            if machine_name == "arm64" or wide_machine == "arm64":
                raise BrowserBootstrapError("edgedriver_platform_unsupported")
            return "windows-x64"
        if machine_name in {"x86", "i386", "i686", "i86pc"}:
            return "windows-x86"
    elif system_name == "linux" and machine_name in {"amd64", "x86_64", "x64"}:
        return "linux-x64"
    raise BrowserBootstrapError("edgedriver_platform_unsupported")


def _version_is_compatible(edge_version: str, driver_version: str) -> bool:
    return edge_version.split(".")[:3] == driver_version.split(".")[:3]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundled_driver(
    version: str,
    omega_root: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    if type(version) is not str or version.count(".") != 3:
        raise BrowserBootstrapError("edge_version_unavailable")
    platform_key = _driver_platform_key(system=system, machine=machine)
    bundle_root = omega_root / _BUNDLED_DRIVER_ROOT
    manifest_path = bundle_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bundle_version = manifest["version"]
        asset = manifest["assets"][platform_key]
        archive_name = asset["archive"]
        binary_name = asset["binary"]
        expected_hash = asset["sha256"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise BrowserBootstrapError("edgedriver_bundle_unavailable") from None
    if (
        type(bundle_version) is not str
        or bundle_version.count(".") != 3
        or type(archive_name) is not str
        or type(binary_name) is not str
        or type(expected_hash) is not str
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_hash)
        or Path(binary_name).name != binary_name
        or Path(binary_name).is_absolute()
        or not _version_is_compatible(version, bundle_version)
    ):
        raise BrowserBootstrapError("edgedriver_bundle_incompatible")

    archive_path = (bundle_root / archive_name).resolve()
    try:
        archive_path.relative_to(bundle_root.resolve())
        archive_size = archive_path.stat().st_size
    except (OSError, ValueError):
        raise BrowserBootstrapError("edgedriver_bundle_unavailable") from None
    try:
        archive_hash = _sha256_file(archive_path)
    except OSError as exc:
        raise BrowserBootstrapError("edgedriver_bundle_integrity_failed") from exc
    if archive_size > _MAX_DRIVER_ARCHIVE_BYTES or archive_hash.casefold() != expected_hash.casefold():
        raise BrowserBootstrapError("edgedriver_bundle_integrity_failed")

    destination = omega_root / _EXTRACTED_DRIVER_ROOT / platform_key
    driver_path = destination / binary_name
    marker_path = destination / ".archive.sha256"
    if driver_path.is_file() and marker_path.is_file():
        try:
            if marker_path.read_text(encoding="ascii").strip().casefold() == expected_hash.casefold():
                return str(driver_path)
        except (OSError, UnicodeError):
            pass

    try:
        with zipfile.ZipFile(archive_path) as package:
            matches = [
                entry
                for entry in package.infolist()
                if Path(entry.filename).name.casefold() == binary_name.casefold()
                and not Path(entry.filename).is_absolute()
                and ".." not in Path(entry.filename).parts
            ]
            if len(matches) != 1 or matches[0].is_dir():
                raise ValueError
            binary = package.read(matches[0])
        destination.mkdir(parents=True, exist_ok=True)
        temporary = destination / f".{binary_name}.tmp"
        temporary.write_bytes(binary)
        os.replace(temporary, driver_path)
        marker_temporary = destination / ".archive.sha256.tmp"
        marker_temporary.write_text(expected_hash, encoding="ascii")
        os.replace(marker_temporary, marker_path)
        if not os.name == "nt":
            driver_path.chmod(driver_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        for temporary_path in (
            destination / f".{binary_name}.tmp",
            destination / ".archive.sha256.tmp",
        ):
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise BrowserBootstrapError("edgedriver_bundle_install_failed") from exc
    return str(driver_path)


def _probe_http(url: str) -> bool:
    try:
        with urlopen(url, timeout=1.5) as response:  # noqa: S310 - URLs are localhost-only
            if response.status < 200 or response.status >= 300:
                return False
            body = response.read(32_768)
        if not body:
            return True
        payload = json.loads(body.decode("utf-8"))
        return isinstance(payload, dict)
    except (OSError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
        return False


class BrowserBootstrap:
    """Start a portable debugger-enabled Edge session and its EdgeDriver."""

    def __init__(
        self,
        *,
        omega_root: Path = OMEGA_ROOT,
        edge_locator: Callable[[], str | None] = _which_edge,
        edge_version_locator: Callable[[], str | None] = _discover_edge_version,
        probe: Callable[[str, str], bool] | None = None,
        launcher: Callable[[list[str], Path], Any] | None = None,
        edge_running_checker: Callable[[str], bool] = _edge_process_running,
        edge_restarter: Callable[[str, int], Any] | None = None,
        platform_system: Callable[[], str] = platform.system,
        platform_machine: Callable[[], str] = platform.machine,
        wait_s: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if wait_s <= 0:
            raise ValueError("browser bootstrap wait must be positive")
        self.omega_root = Path(omega_root)
        self.edge_locator = edge_locator
        self.edge_version_locator = edge_version_locator
        self.probe = probe or self._probe
        self.launcher = launcher or self._launch
        self.edge_running_checker = edge_running_checker
        self.edge_restarter = edge_restarter or self._restart_edge
        self.platform_system = platform_system
        self.platform_machine = platform_machine
        self.wait_s = float(wait_s)
        self.sleep = sleep
        self.edge_process: Any | None = None
        self.driver_process: Any | None = None
        # Set by ``ensure_debugger``.  A later HUD/window open must target the
        # already prepared debugger profile; launching an unbound Edge process
        # here creates a second browser that EdgeDriver cannot see.
        self._debugger_address: str | None = None

    def _debug_profile_path(self) -> Path:
        """Create and return the Omega-owned Edge automation profile."""
        root = self.omega_root.resolve()
        profile = (root / EDGE_DEBUG_PROFILE_RELATIVE).resolve()
        try:
            profile.relative_to(root)
        except ValueError as exc:
            raise BrowserBootstrapError("edge_debug_profile_outside_omega_root") from exc
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BrowserBootstrapError("edge_debug_profile_unavailable") from exc
        return profile

    def _pin_no_session_restore(self) -> None:
        """Make Omega's automation profile open a blank page on start, never the last session.

        Omega's tabs live only while Omega runs (operator 2026-09-15). Edge treats a forced
        stop as a crash and restores the "crashed" session; the profile preference says not
        to, and the exit is marked clean. Failure-soft: a preference that cannot be written
        never blocks the launch.
        """
        prefs_path = self._debug_profile_path() / "Default" / "Preferences"
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.is_file() else {}
            if not isinstance(prefs, dict):
                prefs = {}
            session = prefs.setdefault("session", {})
            profile = prefs.setdefault("profile", {})
            if not isinstance(session, dict) or not isinstance(profile, dict):
                return
            session["restore_on_startup"] = 5  # open the new-tab page
            session["startup_urls"] = []
            profile["exit_type"] = "Normal"
            profile["exited_cleanly"] = True
            prefs_path.parent.mkdir(parents=True, exist_ok=True)
            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _edge_launch_command(
        self,
        edge_path: str,
        *,
        debugger_port: int | None = None,
        initial_url: str | None = None,
        new_window_url: str | None = None,
    ) -> list[str]:
        """Build one deterministic Edge command for the portable session."""
        if initial_url is not None and new_window_url is not None:
            raise ValueError("Edge launch cannot combine an initial URL and a new-window URL")
        command = [edge_path, f"--user-data-dir={self._debug_profile_path()}"]
        if debugger_port is not None:
            command.extend(
                (
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={debugger_port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    # Omega's task window usually sits behind the HUD; Chromium would
                    # stop painting an occluded window and its screenshots would stall.
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                )
            )
            # Edge's internal new-tab target is a ``browser_ui`` surface that
            # EdgeDriver cannot attach to reliably.  Start one ordinary page
            # so auto-setup has a W3C target.  When boot already has the HUD
            # URL, use it as that first page so no blank window is created.
            if initial_url is not None:
                command.append(initial_url)
            elif new_window_url is None:
                command.append("about:blank")
        if new_window_url is not None:
            command.extend(("--new-window", new_window_url))
        return command

    def _probe(self, kind: str, address: str) -> bool:
        if kind == "debugger":
            return _probe_http("http://" + address + "/json/version")
        if kind == "driver":
            return _probe_http(address.rstrip("/") + "/status")
        raise BrowserBootstrapError("browser_probe_kind_invalid")

    def _launch(self, command: list[str], cwd: Path) -> Any:
        log_root = self.omega_root / "guardian" / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        sink = (log_root / "browser-driver.log").open("ab")
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": sink,
            "stderr": subprocess.STDOUT,
        }
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(command, **kwargs)
        finally:
            sink.close()

    def _restart_edge(self, edge_path: str, debugger_port: int) -> Any:
        executable = Path(edge_path).name
        if os.name == "nt":
            command = ["taskkill.exe", "/IM", executable, "/T", "/F"]
            kwargs: dict[str, Any] = {
                "capture_output": True,
                "check": False,
                "text": True,
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            pgrep = shutil.which("pkill")
            if not pgrep:
                raise BrowserBootstrapError("edge_restart_unavailable")
            command = [pgrep, "-x", executable]
            kwargs = {"capture_output": True, "check": False, "text": True}
        try:
            result = subprocess.run(command, **kwargs)
        except OSError as exc:
            raise BrowserBootstrapError("edge_restart_failed") from exc
        if result.returncode not in {0, 1}:
            raise BrowserBootstrapError(
                "edge_restart_failed",
                details={"exit_code": int(result.returncode)},
            )
        self.sleep(0.5)
        return self.launcher(
            self._edge_launch_command(edge_path, debugger_port=debugger_port),
            self.omega_root,
        )

    def _wait_for(self, kind: str, address: str) -> bool:
        deadline = time.monotonic() + self.wait_s
        while time.monotonic() < deadline:
            if self.probe(kind, address):
                return True
            self.sleep(0.25)
        return False

    def ensure_debugger(
        self,
        *,
        debugger_address: str,
        restart_existing_edge: bool = False,
        initial_url: str | None = None,
    ) -> dict[str, object]:
        debugger_port = _address_port(debugger_address)
        if initial_url is not None:
            parsed = urlsplit(str(initial_url or ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise BrowserBootstrapError("browser_window_url_invalid")
            initial_url = str(initial_url)
        self._debugger_address = debugger_address
        initial_url_opened = False
        edge_started = False
        if not self.probe("debugger", debugger_address):
            edge_path = self.edge_locator()
            if not edge_path:
                raise BrowserBootstrapError("edge_executable_unavailable")
            self._pin_no_session_restore()
            if restart_existing_edge and self.edge_running_checker(edge_path):
                self.edge_process = self.edge_restarter(edge_path, debugger_port)
            else:
                self.edge_process = self.launcher(
                    self._edge_launch_command(
                        edge_path,
                        debugger_port=debugger_port,
                        initial_url=initial_url,
                    ),
                    self.omega_root,
                )
                initial_url_opened = initial_url is not None
            edge_started = True
            if not self._wait_for("debugger", debugger_address):
                raise BrowserBootstrapError("edge_debugger_unavailable_after_launch")
        receipt: dict[str, object] = {
            "edge_started": edge_started,
            "debugger_address": debugger_address,
        }
        if initial_url is not None:
            # A custom restarter owns its command shape and may still launch
            # the safe blank page; callers must open the HUD afterward in that
            # case.  Keep this detail out of the legacy receipt unless an
            # initial URL was requested.
            receipt["initial_url_opened"] = initial_url_opened
        return receipt

    def open_new_window(self, url: str) -> Any:
        """Open a URL through the already prepared portable Edge profile.

        Chromium's profile singleton routes this second invocation into the
        existing debugger-enabled process.  Keep the debugger flags and
        profile on the command so a machine with a personal Edge session does
        not receive an unrelated window that EdgeDriver cannot attach to.
        """
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BrowserBootstrapError("browser_window_url_invalid")
        if self._debugger_address is None:
            raise BrowserBootstrapError("browser_debugger_not_prepared")
        edge_path = self.edge_locator()
        if not edge_path:
            raise BrowserBootstrapError("edge_executable_unavailable")
        return self.launcher(
            self._edge_launch_command(
                edge_path,
                debugger_port=_address_port(self._debugger_address),
                new_window_url=str(url),
            ),
            self.omega_root,
        )

    def ensure(
        self,
        *,
        debugger_address: str,
        webdriver_endpoint: str,
        restart_existing_edge: bool = False,
    ) -> dict[str, object]:
        debugger_receipt = self.ensure_debugger(
            debugger_address=debugger_address,
            restart_existing_edge=restart_existing_edge,
        )
        driver_port = _endpoint_port(webdriver_endpoint)
        driver_started = False

        if not self.probe("driver", webdriver_endpoint):
            version = self.edge_version_locator()
            if not version:
                raise BrowserBootstrapError("edge_version_unavailable")
            driver_path = _bundled_driver(
                version,
                self.omega_root,
                system=self.platform_system(),
                machine=self.platform_machine(),
            )
            self.driver_process = self.launcher(
                [driver_path, f"--port={driver_port}"],
                self.omega_root,
            )
            driver_started = True
            if not self._wait_for("driver", webdriver_endpoint):
                raise BrowserBootstrapError("edgedriver_unavailable_after_launch")

        return {
            "edge_started": bool(debugger_receipt["edge_started"]),
            "driver_started": driver_started,
            "debugger_address": debugger_address,
            "webdriver_endpoint": webdriver_endpoint,
        }

    def close(self) -> None:
        process = self.driver_process
        self.driver_process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
