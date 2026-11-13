"""Transactional updater for the Windows portable LM Studio payload.

The official LM Studio download is an installer rather than a documented
portable archive. Omega therefore consumes a vetted JSON manifest that points
to a portable ZIP assembled for the product. The updater replaces only the
application paths named by that manifest; models, settings, credentials, and
other user data remain outside the update transaction.
"""

from __future__ import annotations

import hashlib
import json
import base64
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


class UpdateError(RuntimeError):
    """Raised when an LM Studio update cannot be completed safely."""


@dataclass(frozen=True)
class PortableUpdateManifest:
    """One verified portable LM Studio release description."""

    version: str
    artifact_url: str
    sha256: str
    root: str
    replace_paths: tuple[str, ...]
    launcher: str


@dataclass(frozen=True)
class UpdateCheck:
    """Result of comparing the installed payload with a release manifest."""

    current_version: str
    latest_version: str
    available: bool
    manifest: PortableUpdateManifest


@dataclass(frozen=True)
class UpdateResult:
    """Result of a successful payload swap."""

    version: str
    launcher_path: Path
    backup_path: Path


@dataclass(frozen=True)
class OfficialUpdateCheck:
    """Result of checking LM Studio's official Windows release feed."""

    current_version: str
    latest_version: str
    available: bool
    installer_url: str
    release_notes: str


@dataclass(frozen=True)
class OfficialUpdateResult:
    """Result of installing an official signed LM Studio installer."""

    version: str
    installer_path: Path
    launcher_path: Path


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"^v?(?P<core>\d+(?:\.\d+)*)(?:(?P<pre>-[^+]+))?(?:\+(?P<build>.+))?$",
    re.IGNORECASE,
)
_OFFICIAL_BUILD_RE = re.compile(r"^[0-9A-Za-z._-]+$")

OFFICIAL_VERSION_FEED = "https://versions-prod.lmstudio.ai/update/win32/x86/{version}"
OFFICIAL_INSTALLER_URL = "https://updates.lmstudio.ai/LM-Studio-{version}-{build}-win32-x64.exe"
OFFICIAL_SIGNER_NAME = "Element Labs Inc."
_OFFICIAL_UPDATER_RELATIVE = (
    Path("profile") / "AppData" / "Local" / "lm-studio-updater" / "pending"
)


def _version_key(value: str) -> tuple[tuple[int, ...], tuple[Any, ...], bool]:
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise UpdateError(f"invalid LM Studio version: {value!r}")
    core = tuple(int(part) for part in match.group("core").split("."))
    prerelease = match.group("pre")
    if prerelease is None:
        return core, (), True
    parts: list[Any] = []
    for part in prerelease[1:].split("."):
        parts.append(int(part) if part.isdigit() else part.casefold())
    return core, tuple(parts), False


def compare_versions(left: str, right: str) -> int:
    """Compare LM Studio semantic versions without a third-party dependency."""
    left_core, left_pre, left_release = _version_key(left)
    right_core, right_pre, right_release = _version_key(right)
    max_length = max(len(left_core), len(right_core))
    left_numbers = left_core + (0,) * (max_length - len(left_core))
    right_numbers = right_core + (0,) * (max_length - len(right_core))
    if left_numbers != right_numbers:
        return -1 if left_numbers < right_numbers else 1
    if left_release != right_release:
        return 1 if left_release else -1
    if left_pre == right_pre:
        return 0
    return -1 if left_pre < right_pre else 1


def _safe_relative_path(value: str, label: str) -> str:
    candidate = str(value).replace("\\", "/").strip()
    path = Path(candidate)
    if not candidate or path.is_absolute() or ":" in candidate:
        raise UpdateError(f"{label} must be a relative path")
    parts = tuple(part for part in candidate.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise UpdateError(f"{label} contains an unsafe path")
    return "/".join(parts)


def parse_manifest(payload: Any) -> PortableUpdateManifest:
    """Validate the strict Windows portable-release manifest contract."""
    if not isinstance(payload, dict):
        raise UpdateError("LM Studio update manifest must be a JSON object")
    if payload.get("schema") != 1:
        raise UpdateError("LM Studio update manifest has an unsupported schema")
    if str(payload.get("product") or "").strip().casefold() != "lm studio":
        raise UpdateError("LM Studio update manifest has the wrong product")
    if payload.get("platform") != "windows-x64":
        raise UpdateError("LM Studio update manifest is not for windows-x64")
    version = str(payload.get("version") or "").strip()
    _version_key(version)
    artifact_url = str(payload.get("artifact_url") or "").strip()
    scheme = urlparse(artifact_url).scheme.casefold()
    if scheme not in {"https", "file"}:
        raise UpdateError("LM Studio update artifact must use https or file")
    if str(payload.get("artifact_type") or "").strip().casefold() != "zip":
        raise UpdateError("LM Studio portable updates require a zip artifact")
    digest = str(payload.get("sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise UpdateError("LM Studio update manifest has an invalid sha256")
    root = _safe_relative_path(str(payload.get("root") or ""), "manifest root")
    raw_paths = payload.get("replace_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise UpdateError("LM Studio update manifest has no replace paths")
    replace_paths = tuple(_safe_relative_path(str(path), "replace path") for path in raw_paths)
    launcher = _safe_relative_path(str(payload.get("launcher") or ""), "launcher")
    return PortableUpdateManifest(
        version=version,
        artifact_url=artifact_url,
        sha256=digest,
        root=root,
        replace_paths=replace_paths,
        launcher=launcher,
    )


def fetch_manifest(url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch one JSON manifest; network failures remain visible to the caller."""
    request = urllib.request.Request(
        str(url),
        headers={"Accept": "application/json", "User-Agent": "Omega/3.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) >= 400:
                raise UpdateError(f"LM Studio update feed returned HTTP {response.status}")
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"LM Studio update feed returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise UpdateError("LM Studio update feed could not be read") from exc
    if not isinstance(body, dict):
        raise UpdateError("LM Studio update feed returned a non-object")
    return body


def installed_version(app_root: Path) -> str:
    """Read the installed LM Studio version from its app-owned package metadata."""
    package_path = Path(app_root).resolve() / "resources" / "app" / "package.json"
    try:
        payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"LM Studio version metadata is unreadable: {package_path}") from exc
    version = str(payload.get("version") or "").strip() if isinstance(payload, dict) else ""
    _version_key(version)
    return version


def _release_core(version: str) -> str:
    match = _VERSION_RE.fullmatch(str(version).strip())
    if match is None:
        raise UpdateError(f"invalid LM Studio version: {version!r}")
    return match.group("core")


def _official_installer_name(version: str, build: str) -> str:
    if not _OFFICIAL_BUILD_RE.fullmatch(build):
        raise UpdateError("official LM Studio update has an invalid build")
    return f"LM-Studio-{version}-{build}-win32-x64.exe"


def check_official_update(
    app_root: Path,
    *,
    fetch_version: Callable[[str], dict[str, Any]] = fetch_manifest,
) -> OfficialUpdateCheck:
    """Check the official stable Windows feed for the portable app root."""
    if os.name != "nt":
        raise UpdateError("the portable LM Studio updater requires Windows")
    current = installed_version(app_root)
    current_core = _release_core(current)
    feed_url = OFFICIAL_VERSION_FEED.format(version=current_core)
    payload = fetch_version(feed_url)
    if not isinstance(payload, dict):
        raise UpdateError("LM Studio official update feed returned a non-object")
    version = str(payload.get("version") or "").strip()
    build = str(payload.get("build") or "").strip()
    if not version or not build:
        raise UpdateError("LM Studio official update feed omitted version or build")
    _version_key(version)
    if not _OFFICIAL_BUILD_RE.fullmatch(build):
        raise UpdateError("LM Studio official update feed has an invalid build")
    latest = f"{version}+{build}"
    _version_key(latest)
    _official_installer_name(version, build)
    notes = payload.get("releaseNotes") or payload.get("release_notes") or ""
    if not isinstance(notes, str):
        raise UpdateError("LM Studio official update feed has invalid release notes")
    return OfficialUpdateCheck(
        current_version=current,
        latest_version=latest,
        available=compare_versions(current, latest) < 0,
        installer_url=OFFICIAL_INSTALLER_URL.format(version=version, build=build),
        release_notes=notes,
    )


def official_pending_directory(app_root: Path) -> Path:
    """Return the portable LM Studio updater cache directory."""
    return Path(app_root).resolve() / _OFFICIAL_UPDATER_RELATIVE


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_cached_official_installer(
    app_root: Path,
    check: OfficialUpdateCheck,
) -> Path | None:
    """Return a cached installer only when LM Studio's cache receipt matches it."""
    pending = official_pending_directory(app_root).resolve()
    info_path = pending / "update-info.json"
    if not info_path.is_file():
        return None
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"LM Studio cached update metadata is unreadable: {info_path}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("LM Studio cached update metadata must be a JSON object")
    name = str(payload.get("fileName") or "").strip()
    digest = str(payload.get("sha512") or "").strip()
    expected_name = Path(urllib.parse.urlparse(check.installer_url).path).name
    if name != expected_name:
        return None
    try:
        expected_digest = base64.b64decode(digest, validate=True)
    except (ValueError, TypeError):
        return None
    if len(expected_digest) != hashlib.sha512().digest_size:
        return None
    installer = (pending / name).resolve()
    if installer.parent != pending or not installer.is_file():
        return None
    if bytes.fromhex(_sha512(installer)) != expected_digest:
        return None
    return installer


def _default_verify_signature(installer_path: Path) -> bool:
    """Require a valid Authenticode signature from the LM Studio publisher."""
    if os.name != "nt":
        raise UpdateError("official LM Studio installer verification requires Windows")
    command = (
        "Import-Module Microsoft.PowerShell.Security -Force; "
        "$signature = Get-AuthenticodeSignature -LiteralPath $env:OMEGA_LM_INSTALLER_PATH; "
        "if ($signature.Status -ne 'Valid') { exit 1 }; "
        "$name = $signature.SignerCertificate.GetNameInfo("
        "[System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false); "
        f"if ($name -ne '{OFFICIAL_SIGNER_NAME}') {{ exit 2 }}"
    )
    powershell = shutil.which("powershell.exe") or "powershell.exe"
    environment = dict(os.environ)
    environment["OMEGA_LM_INSTALLER_PATH"] = str(installer_path)
    system_root = Path(environment.get("SystemRoot") or environment.get("WINDIR") or "")
    system_modules = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
    environment["PSModulePath"] = str(system_modules)
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0


def apply_official_update(
    app_root: Path,
    check: OfficialUpdateCheck,
    *,
    installer_path: Path,
    verify_signature: Callable[[Path], bool] = _default_verify_signature,
    run_installer: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    is_running: Callable[[], bool] | None = None,
    relauncher: Callable[[Path], None] | None = None,
) -> OfficialUpdateResult:
    """Install one verified official release into the existing portable root."""
    root = Path(app_root).resolve()
    if not root.is_dir():
        raise UpdateError(f"portable LM Studio directory is missing: {root}")
    if not check.available:
        raise UpdateError("LM Studio is already up to date")
    installer = Path(installer_path).resolve()
    if not installer.is_file():
        raise UpdateError(f"LM Studio update installer is missing: {installer}")
    if not verify_signature(installer):
        raise UpdateError("LM Studio update installer signature is invalid")
    running_checker = is_running or (lambda: _default_is_running(root))
    if running_checker():
        raise UpdateError("LM Studio is still running; close it before updating")
    command = [str(installer), "/S", f"/D={root}"]
    kwargs: dict[str, object] = {
        "cwd": str(root.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "check": False,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = run_installer(command, **kwargs)
    except OSError as exc:
        raise UpdateError("LM Studio official installer could not be started") from exc
    if completed.returncode != 0:
        raise UpdateError(
            f"LM Studio official installer failed with exit code {completed.returncode}"
        )
    installed = installed_version(root)
    if compare_versions(installed, check.latest_version) < 0:
        raise UpdateError(
            f"LM Studio installer completed without installing {check.latest_version}; "
            f"found {installed}"
        )
    launcher = root / "Launch Portable.cmd"
    if not launcher.is_file():
        raise UpdateError(f"portable LM Studio launcher is missing after update: {launcher}")
    (relauncher or _default_relauncher)(launcher)
    return OfficialUpdateResult(installed, installer, launcher)


def update_official_portable(
    app_root: Path,
    *,
    fetch_version: Callable[[str], dict[str, Any]] = fetch_manifest,
    downloader: Callable[[str, Path], None] | None = None,
    verify_signature: Callable[[Path], bool] = _default_verify_signature,
    run_installer: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    is_running: Callable[[], bool] | None = None,
    relauncher: Callable[[Path], None] | None = None,
) -> OfficialUpdateResult | None:
    """Check, acquire, verify, install, and relaunch the portable LM Studio app."""
    check = check_official_update(app_root, fetch_version=fetch_version)
    if not check.available:
        return None
    installer = find_cached_official_installer(app_root, check)
    if installer is None:
        pending = official_pending_directory(app_root)
        pending.mkdir(parents=True, exist_ok=True)
        installer = pending / Path(urllib.parse.urlparse(check.installer_url).path).name
        temporary = installer.with_suffix(installer.suffix + ".tmp")
        try:
            (downloader or _download_to)(check.installer_url, temporary)
            if not temporary.is_file():
                raise UpdateError("LM Studio downloader produced no installer")
            temporary.replace(installer)
        except OSError as exc:
            raise UpdateError("LM Studio official installer could not be staged") from exc
        finally:
            temporary.unlink(missing_ok=True)
    return apply_official_update(
        app_root,
        check,
        installer_path=installer,
        verify_signature=verify_signature,
        run_installer=run_installer,
        is_running=is_running,
        relauncher=relauncher,
    )


_PORTABLE_SETTINGS_RELATIVE = (
    Path("settings.json"),
    Path("apps") / "bionic" / "settings.json",
)


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Replace one LM Studio settings file without leaving a partial JSON file."""
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".omega-lmstudio-settings-",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise UpdateError(f"LM Studio settings could not be written: {path}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def normalize_portable_settings(
    app_root: Path,
    *,
    model_root: Path | None = None,
) -> tuple[Path, ...]:
    """Bind every known LM Studio settings file to the portable model folder.

    LM Studio versions have stored ``downloadsFolder`` either at the app root
    or under ``apps/bionic``.  Read and validate all existing files before
    changing any of them so a corrupt settings file is a visible startup
    failure instead of a partially-normalized install.
    """
    root = Path(app_root).resolve()
    if not root.is_dir():
        raise UpdateError(f"portable LM Studio directory is missing: {root}")
    models = (Path(model_root) if model_root is not None else root / "models").resolve()
    pending: list[tuple[Path, dict[str, Any]]] = []
    for relative in _PORTABLE_SETTINGS_RELATIVE:
        path = root / relative
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError(f"LM Studio settings are unreadable: {path}") from exc
        if not isinstance(payload, dict):
            raise UpdateError(f"LM Studio settings must be a JSON object: {path}")
        payload["downloadsFolder"] = str(models)
        pending.append((path, payload))

    for path, payload in pending:
        _write_json_atomically(path, payload)
    return tuple(path for path, _payload in pending)


def check_for_update(
    app_root: Path,
    *,
    manifest_url: str = "",
    fetch_manifest_fn: Callable[[str], dict[str, Any]] = fetch_manifest,
    fetch_manifest: Callable[[str], dict[str, Any]] | None = None,
) -> UpdateCheck:
    """Read a manifest and report whether it is newer than the installed app."""
    source = str(manifest_url).strip()
    if not source:
        raise UpdateError("LM Studio portable update feed is not configured")
    reader = fetch_manifest or fetch_manifest_fn
    manifest = parse_manifest(reader(source))
    current = installed_version(app_root)
    return UpdateCheck(
        current_version=current,
        latest_version=manifest.version,
        available=compare_versions(current, manifest.version) < 0,
        manifest=manifest,
    )


def _download_to(url: str, destination: Path, *, timeout: float = 60.0) -> None:
    request = urllib.request.Request(str(url), headers={"Accept": "application/zip"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise UpdateError("LM Studio update artifact could not be downloaded") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_zip(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            for member in members:
                try:
                    _safe_relative_path(member.filename, "archive path")
                except UpdateError as exc:
                    raise UpdateError(f"unsafe archive path: {member.filename}") from exc
                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise UpdateError(f"unsafe archive path: {member.filename}")
                if member.is_dir():
                    continue
                target = (destination / member.filename).resolve()
                if destination.resolve() not in target.parents:
                    raise UpdateError(f"unsafe archive path: {member.filename}")
            archive.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise UpdateError("LM Studio update artifact is not a valid zip") from exc


def _acquire_lock(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("x", encoding="ascii")
    except FileExistsError as exc:
        raise UpdateError("another LM Studio update is already running") from exc
    with handle:
        handle.write(str(os.getpid()))


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _move_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def _default_is_running(app_root: Path) -> bool:
    if os.name != "nt":
        raise UpdateError("the portable LM Studio updater requires Windows")
    result = subprocess.run(
        ["tasklist.exe", "/fi", "IMAGENAME eq LM Studio.exe", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise UpdateError("could not determine whether LM Studio is running")
    return any("LM Studio.exe".casefold() in line.casefold() for line in result.stdout.splitlines())


def _default_relauncher(launcher_path: Path) -> None:
    command = f'call "{launcher_path}"'
    subprocess.Popen(
        command,
        cwd=str(launcher_path.parent),
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def apply_update(
    app_root: Path,
    manifest: PortableUpdateManifest,
    *,
    downloader: Callable[[str, Path], None] = _download_to,
    is_running: Callable[[], bool] | None = None,
    relauncher: Callable[[Path], None] = _default_relauncher,
) -> UpdateResult:
    """Download, verify, stage, swap, and relaunch one portable release."""
    root = Path(app_root).resolve()
    if not root.is_dir():
        raise UpdateError(f"portable LM Studio directory is missing: {root}")
    running_checker = is_running or (lambda: _default_is_running(root))
    if running_checker():
        raise UpdateError("LM Studio is still running; close it before updating")

    work = root.parent / f".omega-lmstudio-update-{os.getpid()}"
    backup = root.parent / f".omega-lmstudio-backup-{manifest.version}-{os.getpid()}"
    if work.exists():
        raise UpdateError(f"LM Studio update staging path already exists: {work}")
    _acquire_lock(work / "update.lock")
    try:
        if backup.exists():
            raise UpdateError(f"LM Studio backup path already exists: {backup}")
        archive_path = work / "lmstudio.zip"
        downloader(manifest.artifact_url, archive_path)
        if not archive_path.is_file():
            raise UpdateError("LM Studio downloader produced no artifact")
        if _sha256(archive_path) != manifest.sha256:
            raise UpdateError("LM Studio update artifact checksum does not match the manifest")
        payload = work / "payload"
        payload.mkdir()
        _extract_zip(archive_path, payload)
        release_root = payload / Path(manifest.root)
        if not release_root.is_dir():
            raise UpdateError("LM Studio update archive has no declared payload root")

        sources: list[tuple[str, Path, Path]] = []
        for relative in manifest.replace_paths:
            source = release_root / Path(relative)
            target = root / Path(relative)
            if not source.exists() and not source.is_symlink():
                raise UpdateError(f"LM Studio update is missing declared payload: {relative}")
            if root.resolve() not in target.resolve().parents:
                raise UpdateError(f"LM Studio target escapes the portable directory: {relative}")
            sources.append((relative, source, target))

        backup.mkdir(parents=True)
        installed: list[Path] = []
        try:
            for relative, _source, target in sources:
                if target.exists() or target.is_symlink():
                    _move_path(target, backup / Path(relative))
            for relative, source, target in sources:
                _move_path(source, target)
                installed.append(target)
            launcher_path = root / Path(manifest.launcher)
            if not launcher_path.is_file():
                raise UpdateError(f"portable LM Studio launcher is missing after update: {launcher_path}")
            relauncher(launcher_path)
        except Exception:
            for target in reversed(installed):
                _remove_path(target)
            for relative, _source, target in reversed(sources):
                old = backup / Path(relative)
                if old.exists() or old.is_symlink():
                    _move_path(old, target)
            raise
        return UpdateResult(manifest.version, launcher_path, backup)
    finally:
        if work.exists():
            shutil.rmtree(work)
        lock_path = work / "update.lock"
        if lock_path.exists():
            lock_path.unlink()


__all__ = [
    "OFFICIAL_INSTALLER_URL",
    "OFFICIAL_SIGNER_NAME",
    "OFFICIAL_VERSION_FEED",
    "OfficialUpdateCheck",
    "OfficialUpdateResult",
    "PortableUpdateManifest",
    "UpdateCheck",
    "UpdateError",
    "UpdateResult",
    "apply_update",
    "apply_official_update",
    "check_official_update",
    "check_for_update",
    "compare_versions",
    "fetch_manifest",
    "find_cached_official_installer",
    "installed_version",
    "normalize_portable_settings",
    "official_pending_directory",
    "parse_manifest",
    "update_official_portable",
]
