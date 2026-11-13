"""Portable Omega setup wizard and SSD entrypoint.

``Omega.exe`` is the public SSD authority.  With no arguments it is a setup
surface: the first run walks the operator through the model seat, product
information, and privacy notice.  Windows child launchers call it with an
explicit ``--launch-hud`` or ``--launch-lm-studio`` command so hidden runtime
files never become the public entrypoint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from runtime.cloud_providers import (
    get_cloud_provider,
    public_cloud_providers,
    provider_endpoint,
)
from core.boot_status import read_boot_status, write_boot_status
from core.paths import portable_python_path
from scripts import lmstudio_updater


log = logging.getLogger("omega.portable_launcher")


LM_STUDIO_ROOT_RELATIVE = Path("LM Studio")
LINUX_LM_STUDIO_ROOT_RELATIVE = Path("LM-Studio-Linux")
LM_STUDIO_LAUNCHER_RELATIVE = LM_STUDIO_ROOT_RELATIVE / "Launch Portable.cmd"
LINUX_RUNTIME_SCRIPT_RELATIVE = LINUX_LM_STUDIO_ROOT_RELATIVE / "run-ai.sh"
LINUX_STARTUP_COMMAND = "nohup ./run-ai.sh > ./lm-studio.log 2>&1 &"
HUD_SCRIPT_RELATIVE = Path("hud") / "standalone.py"
OMEGA_BROKER_AUTHORIZED_ENV = "OMEGA_BROKER_AUTHORIZED"
OMEGA_HUD_OPEN_BROWSER_ENV = "OMEGA_HUD_OPEN_BROWSER"
LAUNCH_HUD_COMMAND = "--launch-hud"
LAUNCH_LM_STUDIO_COMMAND = "--launch-lm-studio"
LOCAL_BASE_URL = "http://127.0.0.1:1234/v1"
STARTUP_STATE_RELATIVE = Path("guardian/state/startup_setup.json")
LAUNCH_LOCK_RELATIVE = Path("guardian/state/launcher_lock.json")
MIND_BACKEND_RELATIVE = Path("guardian/state/mind_backend.json")
CONNECTOR_STORE_RELATIVE = Path("guardian/state/connectors")
FACTORY_RESET_SCRIPT_RELATIVE = Path("scripts/day_zero_reset.py")
PRODUCT_INFO_RELATIVE = Path("config/portable_product.json")
HERO_IMAGE_RELATIVE = Path("config/portable_hero.png")
FILE_ATTRIBUTE_HIDDEN = 0x00000002
LAUNCH_PASSWORD_MIN_LENGTH = 8
LAUNCH_PASSWORD_ITERATIONS = 600_000
BOOT_TIMER_ID = 1
BOOT_POLL_INTERVAL_MS = 500


@dataclass(frozen=True)
class BootProgress:
    """User-facing progress state for the asynchronous Omega startup handoff."""

    phase: str
    percent: int
    detail: str
    hint: str


_BOOT_PROGRESS_STAGES = (
    (0.0, "starting", 12, "Starting the portable Omega runtime…", "The setup window will stay open while boot continues."),
    (8.0, "loading", 30, "Loading security and runtime services…", "Omega is opening its local service boundary."),
    (18.0, "preparing", 52, "Preparing memory and model services…", "Large local runtimes may take a little longer on first boot."),
    (35.0, "hud", 72, "Starting the HUD and cognition services…", "Your browser surface will appear when the HUD is ready."),
    (60.0, "warming", 90, "Omega is still warming up…", "Boot continues in the background; you can hide this window safely."),
)


def boot_progress(
    elapsed_seconds: float,
    *,
    process_running: bool = True,
    returncode: int | None = None,
) -> BootProgress:
    """Return honest, non-blocking startup feedback for the setup window."""
    if not process_running:
        code = "unknown" if returncode is None else str(returncode)
        return BootProgress(
            phase="failed",
            percent=100,
            detail=f"Omega boot failed before completion (exit code {code}).",
            hint="The portable runtime was not ready. Check the bundled diagnostics before retrying.",
        )
    elapsed = max(0.0, float(elapsed_seconds))
    current = _BOOT_PROGRESS_STAGES[0]
    for stage in _BOOT_PROGRESS_STAGES:
        if elapsed >= stage[0]:
            current = stage
        else:
            break
    return BootProgress(
        phase=current[1],
        percent=current[2],
        detail=current[3],
        hint=current[4],
    )


@dataclass(frozen=True)
class LauncherVoiceSurfaces:
    hero_tts: str
    summary_tts: str


def launcher_voice_surfaces() -> LauncherVoiceSurfaces:
    """Operator-facing launcher labels for the runtime-reported TTS capability."""
    try:
        from runtime import tts as runtime_tts

        engine = str((runtime_tts.status() or {}).get("engine") or "").strip()
    except Exception:
        engine = ""
    label = engine or "TTS capability"
    return LauncherVoiceSurfaces(hero_tts=label, summary_tts=label)

# The setup wizard's palette (RGB). The Win32 wizard converts it to COLORREFs and
# the Linux wizard's renderer (scripts/omega_launcher_art.py) draws with it
# directly, so the two surfaces cannot drift apart.
OMEGA_PALETTE: dict[str, tuple[int, int, int]] = {
    "window": (10, 14, 23),
    "header": (14, 20, 32),
    "card": (19, 27, 42),
    "card_alt": (23, 33, 51),
    "control": (13, 21, 34),
    "text": (248, 250, 252),
    "muted": (139, 156, 181),
    "cyan": (0, 229, 255),
    "violet": (99, 102, 241),
    "ember": (255, 92, 56),
    "ember_hover": (255, 120, 90),
    "border": (34, 48, 73),
    "border_light": (50, 70, 100),
    "success": (16, 185, 129),
    "danger": (239, 68, 68),
}

# Native layout coordinates are authored in client space.  Keep the requested
# client size separate from the outer frame size so the title bar and borders
# cannot clip the footer or navigation controls.
SETUP_CLIENT_WIDTH = 940
SETUP_CLIENT_HEIGHT = 670
_SETUP_WINDOW_STYLE = 0x00CA0000
_SETUP_WINDOW_EX_STYLE = 0x02000000


def product_root(executable: Path | None = None) -> Path:
    """Return the Omega product directory for a source tree or packaged exe.

    The public launcher may live at the SSD root (for example
    ``<ssd-root>\\Omega.exe``) while the product tree is mounted below it
    (``<ssd-root>\\Omega\\`` or ``<ssd-root>\\Omega3.0\\Omega\\``).
    Resolve only candidates that carry the
    product marker; never assume the launcher's parent is the runtime tree.
    Prefer the current flat Omega3.0 tree when an older packaged Omega
    directory is also present.
    """
    frozen = executable is None and bool(getattr(sys, "frozen", False))
    if executable is not None:
        anchor = Path(executable)
    elif frozen:
        # In a PyInstaller one-file build __file__ points into the temporary
        # extraction directory. The exposed Omega.exe is the stable anchor for
        # the sibling hidden product tree on the SSD.
        anchor = Path(sys.executable)
    else:
        anchor = Path(__file__)
    anchor = anchor.resolve()
    # A frozen launcher (Omega.exe, or the Linux binary that has no suffix) and an
    # explicit ``.exe`` path both sit beside the product; a source run is
    # ``<product>/scripts/omega_launcher.py``.
    is_launcher = frozen or anchor.suffix.lower() == ".exe"
    parent = anchor.parent if is_launcher else anchor.parents[1]
    parent_resolved = parent.resolve()

    if (parent / "core").is_dir() or (parent / "boot.py").is_file():
        return parent
    candidates = [parent / "Omega3.0", parent / "Omega"]
    # Development and VeraCrypt-mounted layouts keep the public launcher one
    # level above the repository/product directory.  The marker check below
    # prevents a stale or unrelated folder from being selected accidentally.
    candidates.append(parent / "Omega3.0" / "Omega")

    # A mounted container may use a user-selected folder name.  Search one
    # bounded level for either that folder itself or its canonical ``Omega``
    # child; never recurse through the SSD or inspect arbitrary files.
    try:
        for child in sorted(parent.iterdir(), key=lambda path: path.name.casefold()):
            if child.is_dir():
                candidates.extend((child, child / "Omega"))
    except OSError:
        # Preserve the historical actionable metadata error below when the
        # removable root is unavailable or not yet mounted.
        pass

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        # Do not follow a junction/symlink out of the SSD root.  Apart from
        # making portability deterministic, this prevents an attacker from
        # replacing a mounted-folder name with a pointer into an arbitrary
        # host directory and tricking the bootstrap into launching it.
        if parent_resolved not in resolved.parents:
            continue
        if (resolved / PRODUCT_INFO_RELATIVE).is_file():
            return resolved

    # Keep the canonical error path stable for the setup dialog while the
    # hidden container is unmounted.
    return (parent / "Omega").resolve()


def ssd_root(product: Path | None = None) -> Path:
    """Return the directory that contains the hidden product tree."""
    root = (product or product_root()).resolve()
    if root.name.casefold() == "omega":
        return root.parent
    sibling_runtimes = (
        root.parent / LM_STUDIO_ROOT_RELATIVE,
        root.parent / LINUX_LM_STUDIO_ROOT_RELATIVE,
    )
    if any(path.is_dir() for path in sibling_runtimes):
        return root.parent
    return root


def hide_portable_runtime(
    product: Path,
    *,
    set_attributes: Callable[[str, int], int] | None = None,
) -> tuple[Path, ...]:
    """Hide the internal product folders while leaving ``Omega.exe`` exposed."""
    if os.name != "nt":
        return ()
    root = Path(product).resolve()
    portable_root = ssd_root(root)
    candidates = (
        root,
        portable_root / LM_STUDIO_ROOT_RELATIVE,
        portable_root / LINUX_LM_STUDIO_ROOT_RELATIVE,
    )
    # A source/flat layout may keep ``Omega.exe`` beside the product files.
    # Never hide the directory that contains the public launcher; packaged
    # releases place the executable beside the hidden ``Omega`` directory, so
    # that directory remains eligible for hiding.
    folders = tuple(
        path
        for path in candidates
        if path.is_dir() and not (path == root and (path / "Omega.exe").is_file())
    )
    if not folders:
        return ()
    setter = set_attributes
    if setter is None:
        import ctypes

        setter = ctypes.windll.kernel32.SetFileAttributesW
    for folder in folders:
        if not setter(str(folder), FILE_ATTRIBUTE_HIDDEN):
            raise OSError(f"could not hide portable runtime folder: {folder}")
    return folders


def lm_studio_launcher_path(ssd: Path | None = None) -> Path:
    """Resolve the operator-provided portable LM Studio command."""
    root = (ssd or ssd_root()).resolve()
    return root / LM_STUDIO_LAUNCHER_RELATIVE


def require_lm_studio_launcher(ssd: Path | None = None) -> Path:
    """Return the LM Studio launcher or fail with an actionable error."""
    path = lm_studio_launcher_path(ssd)
    if not path.is_file():
        raise FileNotFoundError(
            "portable LM Studio launcher is missing: "
            f"{path} (expected {LM_STUDIO_LAUNCHER_RELATIVE})"
        )
    return path


def linux_runtime_script_path(ssd: Path | None = None) -> Path:
    """Resolve the portable Linux LM Studio startup script from the SSD root."""
    root = (ssd or ssd_root()).resolve()
    return root / LINUX_RUNTIME_SCRIPT_RELATIVE


def require_linux_runtime_script(ssd: Path | None = None) -> Path:
    """Return the operator-provided Linux startup script or fail clearly."""
    path = linux_runtime_script_path(ssd)
    if not path.is_file():
        raise FileNotFoundError(
            "portable Linux runtime script is missing: "
            f"{path} (expected {LINUX_RUNTIME_SCRIPT_RELATIVE})"
        )
    return path


def _windows_directory_to_wsl(path: Path) -> str:
    """Convert an SSD directory to the WSL mount path for the same drive."""
    resolved = Path(path).resolve()
    drive = resolved.drive.rstrip(":")
    if len(drive) != 1 or not resolved.anchor:
        raise RuntimeError(f"Linux runtime requires a drive-backed SSD path: {resolved}")
    relative = resolved.relative_to(Path(resolved.anchor)).as_posix()
    return f"/mnt/{drive.lower()}" + (f"/{relative}" if relative != "." else "")


def launch_linux_runtime(
    ssd: Path | None = None,
    *,
    popen=subprocess.Popen,
) -> subprocess.Popen:
    """Start the portable Linux runtime through WSL without exposing a console."""
    script = require_linux_runtime_script(ssd)
    if os.name != "nt":
        return popen(
            ["sh", "-lc", LINUX_STARTUP_COMMAND],
            cwd=str(script.parent),
        )
    if shutil.which("wsl.exe") is None:
        raise FileNotFoundError(
            "WSL is required to launch the Linux runtime from Omega on Windows"
        )
    command: Sequence[str] = (
        "wsl.exe",
        "--cd",
        _windows_directory_to_wsl(script.parent),
        "sh",
        "-lc",
        LINUX_STARTUP_COMMAND,
    )
    kwargs: dict[str, object] = {
        "cwd": str(script.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return popen(list(command), **kwargs)


def launch_lm_studio(
    ssd: Path | None = None,
    *,
    popen=subprocess.Popen,
) -> subprocess.Popen:
    """Start the portable LM Studio GUI selected by the operator."""
    launcher = require_lm_studio_launcher(ssd)
    # Let Python invoke the Windows command interpreter with one command
    # string. Passing ``/c`` as a list argument makes CreateProcess quote the
    # embedded batch-file path again; cmd.exe then treats the quoted path as a
    # literal command and never runs the portable launcher.
    command = f'call "{launcher}"'
    kwargs: dict[str, object] = {
        "cwd": str(launcher.parent),
        "env": {
            **os.environ,
            # Launch Portable.cmd is itself a public entrypoint.  This marker
            # tells it that Omega has already authorized the child launch and
            # prevents a recursive Omega.exe -> Launch Portable.cmd loop.
            OMEGA_BROKER_AUTHORIZED_ENV: "1",
        },
        "shell": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return popen(command, **kwargs)


def launch_hud(
    product: Path | None = None,
    arguments: Sequence[str] = (),
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    """Start the HUD through the bundled interpreter owned by Omega.exe."""
    root = (product or product_root()).resolve()
    python = portable_python_path(root)
    script = root / HUD_SCRIPT_RELATIVE
    if not python.is_file():
        raise FileNotFoundError(f"portable Omega interpreter is missing: {python}")
    if not script.is_file():
        raise FileNotFoundError(f"portable HUD launcher is missing: {script}")
    environment = dict(os.environ)
    environment["OMEGA_ROOT"] = str(root)
    # ``hud.standalone`` owns the secondary HUD path.  Opt it into the same
    # debugger-enabled Edge session that the canonical boot path prepares.
    environment[OMEGA_HUD_OPEN_BROWSER_ENV] = "1"
    kwargs: dict[str, object] = {
        "cwd": str(root),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        # POSIX: the launcher window closes once the HUD is ready; its child must
        # not share the launcher's session (a closed terminal would hang it up).
        kwargs["start_new_session"] = True
    command = [str(python), str(script), *(str(argument) for argument in arguments)]
    return popen(command, **kwargs)


def update_lm_studio(
    ssd: Path | None = None,
    *,
    updater: Callable[..., Any] = lmstudio_updater.update_official_portable,
) -> lmstudio_updater.OfficialUpdateResult | None:
    """Update only the portable LM Studio root owned by this SSD."""
    root = (ssd or ssd_root()).resolve()
    app_root = root / LM_STUDIO_ROOT_RELATIVE
    require_lm_studio_launcher(root)
    return updater(app_root)


def launch_omega(
    product: Path | None = None,
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    """Start Omega from its bundled interpreter and open its user-facing HUD."""
    root = (product or product_root()).resolve()
    python = portable_python_path(root)
    boot = root / "boot.py"
    if not python.is_file():
        raise FileNotFoundError(f"portable Omega interpreter is missing: {python}")
    if not boot.is_file():
        raise FileNotFoundError(f"portable Omega boot entrypoint is missing: {boot}")
    write_boot_status(
        root,
        phase="starting",
        percent=4,
        detail="Starting the portable Omega runtime…",
    )
    environment = dict(os.environ)
    environment["OMEGA_ROOT"] = str(root)
    kwargs: dict[str, object] = {
        "cwd": str(root),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        # POSIX: the launcher window closes once the HUD is ready; its child must
        # not share the launcher's session (a closed terminal would hang it up).
        kwargs["start_new_session"] = True
    return popen([str(python), str(boot)], **kwargs)


def factory_reset(
    product: Path | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Reset only the portable Omega tree through its bundled interpreter.

    The setup executable owns the confirmation UI; this helper only validates
    the in-bundle entrypoint and runs it without a console or inherited streams.
    The reset script itself refuses to operate while Omega is live.
    """
    root = (product or product_root()).resolve()
    python = portable_python_path(root)
    script = root / FACTORY_RESET_SCRIPT_RELATIVE
    if not python.is_file():
        raise FileNotFoundError(f"portable Omega interpreter is missing: {python}")
    if not script.is_file():
        raise FileNotFoundError(f"portable factory reset script is missing: {script}")
    environment = dict(os.environ)
    environment["OMEGA_ROOT"] = str(root)
    kwargs: dict[str, object] = {
        "cwd": str(root),
        "env": environment,
        "check": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    runner([str(python), str(script)], **kwargs)


def startup_state_path(product: Path) -> Path:
    """Return the portable, non-secret setup-state path."""
    return Path(product).resolve() / STARTUP_STATE_RELATIVE


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object: {path}")
    return payload


def launch_lock_path(product: Path) -> Path:
    """Return the portable launch-lock verifier path."""
    return Path(product).resolve() / LAUNCH_LOCK_RELATIVE


def _read_launch_lock_record(product: Path) -> dict[str, Any] | None:
    path = launch_lock_path(product)
    if not path.is_file():
        return None
    record = _read_json_object(path, "Omega launch lock")
    if record.get("schema") != 1 or record.get("enabled") is not True:
        raise RuntimeError(f"Omega launch lock has an unsupported schema: {path}")
    if record.get("algorithm") != "pbkdf2-sha256":
        raise RuntimeError(f"Omega launch lock uses an unsupported algorithm: {path}")
    try:
        iterations = int(record.get("iterations"))
        salt = base64.b64decode(str(record.get("salt") or ""), validate=True)
        digest = base64.b64decode(str(record.get("digest") or ""), validate=True)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Omega launch lock is malformed: {path}") from exc
    if iterations < LAUNCH_PASSWORD_ITERATIONS or len(salt) < 16 or len(digest) != 32:
        raise RuntimeError(f"Omega launch lock is malformed: {path}")
    return {
        "schema": 1,
        "enabled": True,
        "algorithm": "pbkdf2-sha256",
        "iterations": iterations,
        "salt": salt,
        "digest": digest,
    }


def load_launch_lock(product: Path) -> dict[str, bool]:
    """Return the user-visible lock status without exposing verifier material."""
    return {"enabled": _read_launch_lock_record(product) is not None}


def save_launch_password(product: Path, password: str) -> None:
    """Store a salted launch-password verifier inside the portable product."""
    value = str(password or "")
    if len(value) < LAUNCH_PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"launch password must contain at least {LAUNCH_PASSWORD_MIN_LENGTH} characters"
        )
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        LAUNCH_PASSWORD_ITERATIONS,
    )
    _atomic_write_json(
        launch_lock_path(product),
        {
            "schema": 1,
            "enabled": True,
            "algorithm": "pbkdf2-sha256",
            "iterations": LAUNCH_PASSWORD_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "digest": base64.b64encode(digest).decode("ascii"),
        },
    )


def clear_launch_password(product: Path) -> None:
    """Disable the portable launch lock."""
    launch_lock_path(product).unlink(missing_ok=True)


def verify_launch_password(product: Path, password: str) -> bool:
    """Verify a password against the portable launch-lock record."""
    record = _read_launch_lock_record(product)
    if record is None:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        record["salt"],
        record["iterations"],
    )
    return hmac.compare_digest(candidate, record["digest"])


def load_startup_state(product: Path) -> dict[str, Any] | None:
    """Load saved setup state; a missing file means first run."""
    path = startup_state_path(product)
    if not path.is_file():
        return None
    state = _read_json_object(path, "Omega startup state")
    if state.get("schema") != 1 or state.get("setup_completed") is not True:
        raise RuntimeError(f"Omega startup state has an unsupported schema: {path}")
    backend = str(state.get("backend") or "").strip().lower()
    if backend not in {"local", "cloud"}:
        raise RuntimeError("Omega startup state has an invalid backend")
    model = str(state.get("model") or "").strip()
    if not model:
        raise RuntimeError("Omega startup state has no model")
    provider = str(state.get("provider") or ("lmstudio" if backend == "local" else "")).strip().lower()
    base_url = str(state.get("base_url") or "").strip()
    if backend == "local":
        if provider != "lmstudio" or base_url != LOCAL_BASE_URL:
            raise RuntimeError("Omega startup state has an invalid LM Studio route")
    else:
        get_cloud_provider(provider)
        provider_endpoint(provider, base_url or None)
    return {
        "schema": 1,
        "setup_completed": True,
        "backend": backend,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "local_model": str(state.get("local_model") or "").strip(),
        "api_key_configured": bool(state.get("api_key_configured")),
    }


def load_product_info(product: Path) -> dict[str, Any]:
    """Load product identity/legal metadata from the portable product tree."""
    path = Path(product).resolve() / PRODUCT_INFO_RELATIVE
    info = _read_json_object(path, "Omega product metadata")
    for key in (
        "product_name",
        "tagline",
        "privacy_policy",
        "intellectual_property",
        "help_guide",
        "hero_image",
    ):
        if key not in info:
            raise RuntimeError(f"Omega product metadata is missing {key!r}: {path}")
    return info


def _product_asset(product: Path, relative: str, label: str) -> Path:
    path = Path(product).resolve() / Path(relative)
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing from the portable product: {path}")
    return path


def load_help_guide(product: Path, info: dict[str, Any] | None = None) -> str:
    """Read the bundled operator guide used by the setup wizard."""
    metadata = info or load_product_info(product)
    relative = str(metadata.get("help_guide") or "").strip()
    if not relative:
        raise RuntimeError("Omega product metadata has no help guide path")
    path = _product_asset(product, relative, "Omega help guide")
    return path.read_text(encoding="utf-8")


def load_privacy_policy(product: Path, info: dict[str, Any] | None = None) -> str:
    """Read the bundled privacy notice used by the setup wizard."""
    metadata = info or load_product_info(product)
    policy = metadata.get("privacy_policy") or {}
    relative = str(policy.get("file") or "").strip()
    if not relative:
        raise RuntimeError("Omega product metadata has no privacy policy path")
    path = _product_asset(product, relative, "Omega privacy policy")
    return path.read_text(encoding="utf-8")


def _connector_store(product: Path) -> Path:
    return Path(product).resolve() / CONNECTOR_STORE_RELATIVE


def _secret_key(base_dir: Path) -> bytes | None:
    key_path = base_dir / "omega-connect.key"
    if not key_path.is_file():
        return None
    try:
        key = base64.b64decode(key_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"connector key is invalid: {key_path}") from exc
    if len(key) != 32:
        raise RuntimeError(f"connector key has invalid length: {key_path}")
    return key


def _ensure_secret_key(base_dir: Path) -> bytes:
    existing = _secret_key(base_dir)
    if existing is not None:
        return existing
    base_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    (base_dir / "omega-connect.key").write_text(
        base64.b64encode(key).decode("ascii"), encoding="utf-8"
    )
    return key


def _load_vault(product: Path) -> dict[str, Any]:
    path = _connector_store(product) / "omega-connect.secrets.json"
    if not path.is_file():
        return {}
    return _read_json_object(path, "connector vault")


def _stored_api_key(product: Path, provider_id: str) -> str | None:
    """Decrypt one existing key without ever putting it in setup JSON."""
    base_dir = _connector_store(product)
    key = _secret_key(base_dir)
    if key is None:
        return None
    payload = (_load_vault(product).get("api_keys") or {}).get(provider_id)
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise RuntimeError("connector vault contains an invalid provider record")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = base64.b64decode(str(payload.get("iv") or ""))
        tag = base64.b64decode(str(payload.get("tag") or ""))
        data = base64.b64decode(str(payload.get("data") or ""))
        plaintext = AESGCM(key).decrypt(iv, data + tag, None)
    except Exception as exc:  # noqa: BLE001 - invalid credentials must be visible
        raise RuntimeError("connector vault contains an undecryptable provider key") from exc
    return plaintext.decode("utf-8").strip() or None


def _save_api_key(product: Path, provider_id: str, value: str) -> None:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError("portable credential encryption is unavailable") from exc
    base_dir = _connector_store(product)
    key = _ensure_secret_key(base_dir)
    secret_path = base_dir / "omega-connect.secrets.json"
    blob = _load_vault(product)
    api_keys = dict(blob.get("api_keys") or {})
    iv = secrets.token_bytes(12)
    sealed = AESGCM(key).encrypt(iv, value.encode("utf-8"), None)
    api_keys[provider_id] = {
        "v": 1,
        "alg": "aes-256-gcm",
        "iv": base64.b64encode(iv).decode("ascii"),
        "tag": base64.b64encode(sealed[-16:]).decode("ascii"),
        "data": base64.b64encode(sealed[:-16]).decode("ascii"),
    }
    _atomic_write_json(secret_path, {"api_keys": api_keys})


def _normalise_form(payload: dict[str, Any]) -> dict[str, Any]:
    backend = str(payload.get("backend") or "").strip().lower()
    if backend not in {"local", "cloud"}:
        raise ValueError("backend must be local or cloud")
    model = str(payload.get("model") or payload.get("local_model") or "").strip()
    if not model:
        raise ValueError("model is required")
    if backend == "local":
        return {
            "backend": "local",
            "provider": "lmstudio",
            "model": model,
            "local_model": model,
            "base_url": LOCAL_BASE_URL,
            "api_key_env": "",
            "dialect": "openai",
        }
    provider_id = str(payload.get("provider") or "").strip().lower()
    provider = get_cloud_provider(provider_id)
    supplied_endpoint = str(payload.get("base_url") or "").strip()
    endpoint = provider_endpoint(provider.id, supplied_endpoint or None)
    return {
        "backend": "cloud",
        "provider": provider.id,
        "model": model,
        "local_model": "",
        "base_url": endpoint,
        "api_key_env": provider.api_key_env,
        "dialect": provider.dialect,
    }


def save_startup_configuration(
    product: Path,
    payload: dict[str, Any],
    *,
    api_key: str = "",
) -> dict[str, Any]:
    """Persist setup state and the backend descriptor after validating the form."""
    root = Path(product).resolve()
    form = _normalise_form(payload)
    entered_key = str(api_key or "").strip()
    if form["backend"] == "cloud":
        if entered_key:
            _save_api_key(root, form["provider"], entered_key)
        elif not _stored_api_key(root, form["provider"]):
            raise RuntimeError("a cloud provider API key is required")

    config = {
        "backend": form["backend"],
        "enabled": form["backend"] == "cloud",
        "provider": form["provider"],
        "model": form["model"],
        "local_model": form["local_model"],
        "base_url": form["base_url"],
        "api_key_env": form["api_key_env"],
        "dialect": form["dialect"],
    }
    _atomic_write_json(root / MIND_BACKEND_RELATIVE, config)
    state = {
        "schema": 1,
        "setup_completed": True,
        "backend": form["backend"],
        "provider": form["provider"],
        "model": form["model"],
        "base_url": form["base_url"],
        "local_model": form["local_model"],
        "api_key_configured": bool(
            form["backend"] == "cloud" and _stored_api_key(root, form["provider"])
        ),
    }
    _atomic_write_json(startup_state_path(root), state)
    return state


def discover_cloud_models_for_setup(
    provider_id: str,
    api_key: str,
    base_url: str | None = None,
    *,
    timeout: float = 10.0,
) -> list[str]:
    """Fetch model ids with the standard library so the EXE has no HTTP dependency."""
    provider = get_cloud_provider(provider_id)
    endpoint = provider_endpoint(provider.id, base_url)
    key = str(api_key or "").strip()
    if provider.api_key_env and not key:
        raise RuntimeError("a cloud provider API key is required to test the connection")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{endpoint}/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) >= 400:
                raise RuntimeError(f"cloud provider returned HTTP {response.status}")
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"cloud provider returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("cloud provider connection failed") from exc
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise RuntimeError("cloud provider returned an invalid model list")
    models = [
        str(row.get("id") or row.get("name") or "").strip()
        for row in rows
        if isinstance(row, dict)
    ]
    models = [model for model in models if model]
    if not models:
        raise RuntimeError("cloud provider returned no models")
    return models


def show_launcher_error(
    message: str,
    *,
    dialog: Callable[[str, str], Any] | None = None,
) -> None:
    """Make a packaged, windowless launch failure visible to the operator."""
    if dialog is not None:
        dialog("Omega", message)
        return
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "Omega", 0x10)
        return
    try:
        _tk_error_dialog("Omega", message)
    except Exception as exc:  # noqa: BLE001 - no display: stderr is the only channel left
        print(f"{message}\n(the error dialog could not open: {exc})", file=sys.stderr)


def _tk_error_dialog(title: str, message: str) -> None:
    import tkinter
    from tkinter import messagebox

    root = tkinter.Tk()
    root.withdraw()
    try:
        messagebox.showerror(title, message, parent=root)
    finally:
        root.destroy()


def open_external_url(
    url: str,
    *,
    opener: Callable[[str], Any] | None = None,
) -> None:
    """Open one verified public web URL from the setup wizard."""
    value = str(url or "").strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("only HTTP(S) website links can be opened")
    if opener is not None:
        opener(value)
        return
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise RuntimeError("Windows cannot open the product website")
        startfile(value)
        return
    import webbrowser

    if not webbrowser.open(value):
        raise RuntimeError("the product website could not be opened")


def validate_launch_password_fields(password: str, confirmation: str) -> None:
    """Both wizards: an optional launch password must be long enough and confirmed."""
    if not password and not confirmation:
        return
    if len(password) < LAUNCH_PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"launch password must contain at least {LAUNCH_PASSWORD_MIN_LENGTH} characters"
        )
    if password != confirmation:
        raise ValueError("launch password and confirmation do not match")


def setup_summary_text(product: Path, form: dict[str, Any], *, entered_key: str) -> str:
    """The "Ready to boot" summary shared by both wizards (newline-separated)."""
    voice_tts = launcher_voice_surfaces().summary_tts
    if form["backend"] == "local":
        return (
            f"  ENGINE ARCHITECTURE :  Local Airgapped (LM Studio)\n"
            f"  SERVER ENDPOINT     :  {LOCAL_BASE_URL}\n"
            f"  INFERENCE MODEL     :  {form.get('model') or '(not set)'}\n"
            f"  DATA PRIVACY        :  Airgapped Local Host Only (Zero Telemetry)\n\n"
            f"  VOICE SUBSYSTEM     :  Parakeet ONNX + {voice_tts}\n"
            f"  MEMORY SUBSYSTEM    :  Airgapped Portable DME Runtime (Dewey)"
        )
    provider = get_cloud_provider(form["provider"])
    has_saved_key = bool(_stored_api_key(product, provider.id))
    key_status = (
        "Vault Configured (AES Encrypted)"
        if has_saved_key
        else ("Entered — click Save key securely" if entered_key else "Missing / Not Configured")
    )
    return (
        f"  ENGINE ARCHITECTURE :  Cloud Seat ({provider.name})\n"
        f"  ENDPOINT URL        :  {form.get('base_url') or '(not set)'}\n"
        f"  INFERENCE MODEL     :  {form.get('model') or '(not set)'}\n"
        f"  API CREDENTIAL      :  {key_status}\n\n"
        f"  VOICE SUBSYSTEM     :  Parakeet ONNX + {voice_tts}\n"
        f"  MEMORY SUBSYSTEM    :  Airgapped Portable DME Runtime (Dewey)"
    )


def setup_info_text(product: Path, info: dict[str, Any]) -> str:
    """The "About Omega" page shared by both wizards (newline-separated)."""
    privacy = info["privacy_policy"]
    intellectual_property = info["intellectual_property"]
    application_number = str(
        intellectual_property.get("application_number") or "Not configured in this build"
    )
    filed_date = str(intellectual_property.get("filed_date") or "Not configured")
    contacts = ", ".join(str(contact) for contact in info.get("contacts") or ())
    return (
        f"{info['product_name']} — {info['tagline']}\n\n"
        f"{info.get('description', '')}\n\n"
        f"Website: {info.get('website', '')}\n"
        f"Support: {info.get('support_url', '')}\n\n"
        f"Contact: {contacts}\n\n"
        f"Privacy policy: {privacy.get('url', '')}\n"
        f"{privacy.get('summary', '')}\n\n"
        f"Bundled privacy notice\n{load_privacy_policy(product, info)}\n\n"
        f"Patent title: {intellectual_property.get('patent_title', '')}\n"
        f"Patent status: {intellectual_property.get('patent_status', '')}\n"
        f"Application number: {application_number}\n\n"
        f"Filed: {filed_date}\n\n"
        f"{load_help_guide(product, info)}"
    )


def interpret_boot_status(
    status: dict[str, Any] | None, elapsed_seconds: float
) -> tuple[BootProgress, str]:
    """Map boot.py's status file to what both wizards show and do next.

    The action is ``close`` (the HUD is ready), ``stop`` (boot failed; keep the
    window open on the cause) or ``continue`` (keep polling).
    """
    if status is not None:
        phase = str(status.get("phase") or "")
        detail = str(status.get("detail") or "").strip()
        if phase == "ready":
            return (
                BootProgress(
                    phase="ready",
                    percent=100,
                    detail=detail or "Omega HUD is ready.",
                    hint="The HUD has launched. Opening the setup window again is not required.",
                ),
                "close",
            )
        if phase == "failed":
            return (
                BootProgress(
                    phase="failed",
                    percent=100,
                    detail=detail or "Omega boot failed.",
                    hint="The portable runtime was not ready. Check the bundled diagnostics before retrying.",
                ),
                "stop",
            )
        if phase in {"starting", "loading", "memory", "warming", "hud"}:
            percent = status.get("percent")
            if isinstance(percent, int):
                return (
                    BootProgress(
                        phase=phase,
                        percent=max(0, min(99, percent)),
                        detail=detail or "Omega is starting…",
                        hint="Omega is working through its startup stages; this window will close when the HUD launches.",
                    ),
                    "continue",
                )
    return boot_progress(elapsed_seconds), "continue"


# DrawText flags. Plain integers, defined on every platform because SetupWizard's
# method signatures take them as defaults and those are evaluated at import time.
_DT_CENTER = 0x00000001
_DT_RIGHT = 0x00000002
_DT_VCENTER = 0x00000004
_DT_SINGLELINE = 0x00000020
_DT_WORDBREAK = 0x00000010
_DT_NOPREFIX = 0x00000800


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _WS_CHILD = 0x40000000
    _WS_TABSTOP = 0x00010000
    _WS_BORDER = 0x00800000
    _WS_VSCROLL = 0x00200000
    _WS_EX_CLIENTEDGE = 0x00000200
    _ES_LEFT = 0x0000
    _ES_MULTILINE = 0x0004
    _ES_PASSWORD = 0x0020
    _ES_AUTOVSCROLL = 0x0040
    _ES_READONLY = 0x0800
    _SS_LEFT = 0x0000
    _SS_LEFTNOWORDWRAP = 0x000C
    _CBS_DROPDOWN = 0x0002
    _CBS_DROPDOWNLIST = 0x0003
    _CB_ADDSTRING = 0x0143
    _CB_GETCURSEL = 0x0147
    _CB_RESETCONTENT = 0x014B
    _CB_SETCURSEL = 0x014E
    _CBN_SELCHANGE = 1
    _SW_HIDE = 0
    _SW_SHOW = 5
    _WM_COMMAND = 0x0111
    _WM_CLOSE = 0x0010
    _WM_DESTROY = 0x0002
    _WM_PAINT = 0x000F
    _WM_ERASEBKGND = 0x0014
    _WM_DRAWITEM = 0x002B
    _WM_TIMER = 0x0113
    _WM_SETFONT = 0x0030
    _WM_CTLCOLORBTN = 0x0135
    _WM_CTLCOLORSTATIC = 0x0138
    _WM_CTLCOLOREDIT = 0x0133
    _WM_CTLCOLORLISTBOX = 0x0134
    _BS_OWNERDRAW = 0x0000000B
    _ODS_SELECTED = 0x0001
    _ODS_DISABLED = 0x0004
    _TRANSPARENT = 1
    _MB_OK = 0x0000
    _MB_ICONERROR = 0x0010
    _MB_YESNO = 0x0004
    _MB_ICONWARNING = 0x0030
    _IDYES = 0x0006
    _BTN_BACK = 1001
    _BTN_NEXT = 1002
    _BTN_INFO = 1003
    _BTN_LM = 1004
    _BTN_TEST = 1005
    _BTN_BOOT = 1006
    _BTN_LATER = 1007
    _BTN_INFO_BACK = 1008
    _BTN_LINUX = 1009
    _BACKEND_COMBO = 1010
    _PROVIDER_COMBO = 1011
    _ENDPOINT_EDIT = 1012
    _MODEL_COMBO = 1013
    _KEY_EDIT = 1014
    _LOCAL_MODEL_EDIT = 1015
    _STATUS_LABEL = 1016
    _SUMMARY_LABEL = 1017
    _HELP_EDIT = 1018
    _BTN_RESET = 1019
    _BTN_WEBSITE = 1020
    _BTN_PRIVACY = 1021
    _BTN_UNLOCK = 1022
    _BTN_DISABLE_LOCK = 1023
    _LAUNCH_PASSWORD_EDIT = 1024
    _LAUNCH_PASSWORD_CONFIRM_EDIT = 1025
    _UNLOCK_PASSWORD_EDIT = 1026
    _SECURITY_STATUS_LABEL = 1027
    _BTN_LM_UPDATE = 1028
    _BTN_BOOT_HIDE = 1029
    _BTN_SAVE_KEY = 1030

    def _rgb(red: int, green: int, blue: int) -> int:
        return red | (green << 8) | (blue << 16)

    _OMEGA_COLORS = {name: _rgb(*rgb) for name, rgb in OMEGA_PALETTE.items()}

    class _DrawItemStruct(ctypes.Structure):
        _fields_ = [
            ("CtlType", wintypes.UINT),
            ("CtlID", wintypes.UINT),
            ("itemID", wintypes.UINT),
            ("itemAction", wintypes.UINT),
            ("itemState", wintypes.UINT),
            ("hwndItem", wintypes.HWND),
            ("hDC", wintypes.HDC),
            ("rcItem", wintypes.RECT),
            ("itemData", ctypes.c_ssize_t),
        ]

    class _PaintStruct(ctypes.Structure):
        _fields_ = [
            ("hdc", ctypes.c_void_p),
            ("fErase", wintypes.BOOL),
            ("rcPaint", wintypes.RECT),
            ("fRestore", wintypes.BOOL),
            ("fIncUpdate", wintypes.BOOL),
            ("rgbReserved", ctypes.c_ubyte * 32),
        ]


class SetupWizard:
    """Small dependency-free native Windows setup wizard."""

    def __init__(self, product: Path) -> None:
        self.product = Path(product).resolve()
        self.info = load_product_info(self.product)
        self.hero_path = _product_asset(self.product, str(self.info["hero_image"]), "Omega hero image")
        self.saved = load_startup_state(self.product)
        self.launch_lock = load_launch_lock(self.product)
        self.unlocked = not self.launch_lock["enabled"]
        self.disable_lock_requested = False
        self.catalog = public_cloud_providers()
        self.provider_ids = [str(provider["id"]) for provider in self.catalog]
        self.page = 0
        self.hwnd = None
        self.boot_requested = False
        self.boot_process: subprocess.Popen | None = None
        self.boot_started_at: float | None = None
        self.boot_timer_active = False
        self.boot_display = boot_progress(0.0)
        self.controls: dict[str, Any] = {}
        self.pages: dict[int, list[Any]] = {
            0: [],
            1: [],
            2: [],
            3: [],
            4: [],
            5: [],
            6: [],
            7: [],
        }
        self._callback = None
        self._user32 = None
        self._kernel32 = None
        self._instance = None
        self._gdi32 = None
        self._fonts: dict[str, Any] = {}
        self._brushes: dict[str, Any] = {}
        self._pens: dict[str, Any] = {}
        self._button_labels: dict[int, str] = {}

    def run(self) -> bool:
        if os.name != "nt":
            raise RuntimeError("the portable Omega setup wizard requires Windows")
        return self._run_native()

    def _run_native(self) -> bool:
        import ctypes
        from ctypes import wintypes

        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._init_theme()
        self._configure_native_api(ctypes, wintypes)
        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        self._instance = self._kernel32.GetModuleHandleW(None)
        self._init_hero_image()

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        @ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        def window_proc(hwnd, message, wparam, lparam):
            return self._window_proc(hwnd, message, wparam, lparam)

        self._callback = window_proc
        class_name = "OmegaPortableSetupWizard"
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = ctypes.cast(window_proc, ctypes.c_void_p)
        window_class.hInstance = self._instance
        window_class.hCursor = self._user32.LoadCursorW(None, ctypes.c_void_p(32512))
        window_class.hbrBackground = ctypes.c_void_p(6)
        window_class.lpszClassName = class_name
        if not self._user32.RegisterClassW(ctypes.byref(window_class)):
            error = self._kernel32.GetLastError()
            if error != 1410:
                raise RuntimeError(f"could not create the setup window (Win32 error {error})")

        screen_w = self._user32.GetSystemMetrics(0)
        screen_h = self._user32.GetSystemMetrics(1)
        frame = wintypes.RECT(0, 0, SETUP_CLIENT_WIDTH, SETUP_CLIENT_HEIGHT)
        if not self._user32.AdjustWindowRectEx(
            ctypes.byref(frame),
            _SETUP_WINDOW_STYLE,
            False,
            _SETUP_WINDOW_EX_STYLE,
        ):
            raise RuntimeError("could not calculate the setup window frame")
        win_w = frame.right - frame.left
        win_h = frame.bottom - frame.top
        win_x = max(20, (screen_w - win_w) // 2)
        win_y = max(20, (screen_h - win_h) // 2)

        self.hwnd = self._user32.CreateWindowExW(
            _SETUP_WINDOW_EX_STYLE,
            class_name,
            "Omega 3.0  //  Portable Cognitive Runtime Setup",
            _SETUP_WINDOW_STYLE,
            win_x,
            win_y,
            win_w,
            win_h,
            None,
            None,
            self._instance,
            None,
        )
        if not self.hwnd:
            raise RuntimeError("could not create the setup window")
        self._apply_dwm_attributes()
        self._user32.ShowWindow(self.hwnd, _SW_SHOW)
        self._user32.UpdateWindow(self.hwnd)
        message = wintypes.MSG()
        while self._user32.GetMessageW(__import__("ctypes").byref(message), None, 0, 0) > 0:
            self._user32.TranslateMessage(__import__("ctypes").byref(message))
            self._user32.DispatchMessageW(__import__("ctypes").byref(message))
        self._close_theme()
        return self.boot_requested

    def _apply_dwm_attributes(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            dwmapi = ctypes.windll.dwmapi
            dark_mode = wintypes.BOOL(True)
            hr = dwmapi.DwmSetWindowAttribute(
                self.hwnd,
                20,
                ctypes.byref(dark_mode),
                ctypes.sizeof(dark_mode),
            )
            if hr != 0:
                dwmapi.DwmSetWindowAttribute(
                    self.hwnd,
                    19,
                    ctypes.byref(dark_mode),
                    ctypes.sizeof(dark_mode),
                )
            corner = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(
                self.hwnd,
                33,
                ctypes.byref(corner),
                ctypes.sizeof(corner),
            )
            caption_color = wintypes.COLORREF(_OMEGA_COLORS["header"])
            dwmapi.DwmSetWindowAttribute(
                self.hwnd,
                35,
                ctypes.byref(caption_color),
                ctypes.sizeof(caption_color),
            )
        except Exception:
            pass

    def _configure_native_api(self, ctypes, wintypes) -> None:
        """Declare pointer-sized Win32 signatures used by the callback and painter."""
        pointer = ctypes.c_void_p
        lparam = ctypes.c_ssize_t
        wparam = ctypes.c_size_t
        self._user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wparam,
            lparam,
        ]
        self._user32.DefWindowProcW.restype = lparam
        self._user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, pointer]
        self._user32.LoadCursorW.restype = pointer
        self._user32.RegisterClassW.argtypes = [pointer]
        self._user32.RegisterClassW.restype = wintypes.ATOM
        self._user32.AdjustWindowRectEx.argtypes = [
            pointer,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._user32.AdjustWindowRectEx.restype = wintypes.BOOL
        self._user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            pointer,
            wintypes.HINSTANCE,
            pointer,
        ]
        self._user32.CreateWindowExW.restype = wintypes.HWND
        self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.UpdateWindow.argtypes = [wintypes.HWND]
        self._user32.UpdateWindow.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [pointer, wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.TranslateMessage.argtypes = [pointer]
        self._user32.TranslateMessage.restype = wintypes.BOOL
        self._user32.DispatchMessageW.argtypes = [pointer]
        self._user32.DispatchMessageW.restype = lparam
        self._user32.DestroyWindow.argtypes = [wintypes.HWND]
        self._user32.DestroyWindow.restype = wintypes.BOOL
        self._user32.PostQuitMessage.argtypes = [ctypes.c_int]
        self._user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, pointer]
        self._user32.SetTimer.restype = ctypes.c_size_t
        self._user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        self._user32.KillTimer.restype = wintypes.BOOL
        self._user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        self._user32.SetWindowTextW.restype = wintypes.BOOL
        self._user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wparam,
            lparam,
        ]
        self._user32.SendMessageW.restype = lparam
        self._user32.FillRect.argtypes = [wintypes.HDC, pointer, pointer]
        self._user32.FillRect.restype = ctypes.c_int
        self._user32.BeginPaint.argtypes = [wintypes.HWND, pointer]
        self._user32.BeginPaint.restype = wintypes.HDC
        self._user32.EndPaint.argtypes = [wintypes.HWND, pointer]
        self._user32.EndPaint.restype = wintypes.BOOL
        self._user32.GetClientRect.argtypes = [wintypes.HWND, pointer]
        self._user32.GetClientRect.restype = wintypes.BOOL
        self._user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, pointer, wintypes.UINT]
        self._user32.DrawTextW.restype = ctypes.c_int
        self._user32.InvalidateRect.argtypes = [wintypes.HWND, pointer, wintypes.BOOL]
        self._user32.InvalidateRect.restype = wintypes.BOOL
        self._user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self._user32.GetSystemMetrics.restype = ctypes.c_int
        self._gdi32.SelectObject.argtypes = [wintypes.HDC, pointer]
        self._gdi32.SelectObject.restype = pointer
        self._gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        self._gdi32.SetBkMode.restype = ctypes.c_int
        self._gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
        self._gdi32.SetTextColor.restype = wintypes.COLORREF
        self._gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self._gdi32.RoundRect.restype = wintypes.BOOL
        self._gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
        self._gdi32.CreatePen.restype = ctypes.c_void_p
        self._gdi32.GetStockObject.argtypes = [ctypes.c_int]
        self._gdi32.GetStockObject.restype = ctypes.c_void_p
        self._gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self._gdi32.MoveToEx.restype = wintypes.BOOL
        self._gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self._gdi32.LineTo.restype = wintypes.BOOL
        self._gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self._gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self._gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self._gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        self._gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        self._gdi32.BitBlt.restype = wintypes.BOOL
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self._gdi32.DeleteDC.restype = wintypes.BOOL

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == 1:
            self.hwnd = hwnd
            self._create_controls()
        elif message == _WM_PAINT:
            self._paint_hero(hwnd)
            return 0
        elif message == _WM_ERASEBKGND:
            return 1
        elif message == _WM_TIMER and int(wparam) == BOOT_TIMER_ID:
            self._poll_boot()
            return 0
        elif message == _WM_DRAWITEM:
            return self._draw_button(lparam)
        elif message in {
            _WM_CTLCOLORSTATIC,
            _WM_CTLCOLOREDIT,
            _WM_CTLCOLORLISTBOX,
            _WM_CTLCOLORBTN,
        }:
            import ctypes

            hdc = ctypes.c_void_p(wparam)
            is_edit = message in {_WM_CTLCOLOREDIT, _WM_CTLCOLORLISTBOX}
            is_title = lparam in {
                self.controls.get("title"),
                self.controls.get("subtitle"),
            }
            self._gdi32.SetBkMode(hdc, _TRANSPARENT)
            if is_edit:
                self._gdi32.SetTextColor(hdc, _OMEGA_COLORS["text"])
                return self._brushes["control"]
            elif is_title:
                self._gdi32.SetTextColor(hdc, _OMEGA_COLORS["text"])
                return self._brushes["card"]
            elif lparam == self.controls.get("status"):
                status_text = self._get_text(self.controls["status"])
                color = "success" if "OK" in status_text else ("danger" if "failed" in status_text.lower() else "muted")
                self._gdi32.SetTextColor(hdc, _OMEGA_COLORS[color])
                return self._brushes["card"]
            elif lparam == self.controls.get("security_status"):
                sec_text = self._get_text(self.controls["security_status"])
                color = "success" if "enabled" in sec_text else ("ember" if "disabled" in sec_text else "muted")
                self._gdi32.SetTextColor(hdc, _OMEGA_COLORS[color])
                return self._brushes["card"]
            elif lparam in {
                self.controls.get("boot_status"),
                self.controls.get("boot_detail"),
                self.controls.get("boot_elapsed"),
                self.controls.get("boot_hint"),
            }:
                color = "danger" if self.boot_display.phase == "failed" else (
                    "success" if self.boot_display.phase == "ready" else "cyan"
                )
                self._gdi32.SetTextColor(hdc, _OMEGA_COLORS[color])
                return self._brushes["card"]
            else:
                self._gdi32.SetTextColor(hdc, _OMEGA_COLORS["muted"])
                return self._brushes["card"]
        elif message == _WM_COMMAND:
            self._on_command(int(wparam) & 0xFFFF, (int(wparam) >> 16) & 0xFFFF)
        elif message == _WM_CLOSE:
            self._user32.DestroyWindow(hwnd)
        elif message == _WM_DESTROY:
            self._stop_boot_monitor()
            self._close_hero_image()
            self._user32.PostQuitMessage(0)
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _init_theme(self) -> None:
        """Create the small native theme once for the whole setup session."""
        import ctypes

        self._gdi32 = ctypes.windll.gdi32
        self._gdi32.CreateSolidBrush.restype = ctypes.c_void_p
        self._gdi32.CreateFontW.restype = ctypes.c_void_p
        self._gdi32.DeleteObject.argtypes = [ctypes.c_void_p]

        def handle(value):
            return value.value if isinstance(value, ctypes.c_void_p) else value

        self._fonts = {
            "brand": handle(self._gdi32.CreateFontW(
                -24, 0, 0, 0, 800, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "emblem": handle(self._gdi32.CreateFontW(
                -32, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "title": handle(self._gdi32.CreateFontW(
                -20, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "subtitle": handle(self._gdi32.CreateFontW(
                -13, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "section": handle(self._gdi32.CreateFontW(
                -13, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "body": handle(self._gdi32.CreateFontW(
                -13, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "mono": handle(self._gdi32.CreateFontW(
                -12, 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 2, "Consolas"
            )),
            "button": handle(self._gdi32.CreateFontW(
                -13, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "badge": handle(self._gdi32.CreateFontW(
                -11, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
            "small": handle(self._gdi32.CreateFontW(
                -11, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 2, "Segoe UI"
            )),
        }
        self._brushes = {
            name: handle(self._gdi32.CreateSolidBrush(color))
            for name, color in _OMEGA_COLORS.items()
        }
        self._pens = {
            name: handle(self._gdi32.CreatePen(0, 1, color))
            for name, color in _OMEGA_COLORS.items()
        }
        self._pens["null"] = handle(self._gdi32.GetStockObject(8))

    def _close_theme(self) -> None:
        if self._gdi32 is None:
            return
        for handle in (*self._fonts.values(), *self._brushes.values(), *self._pens.values()):
            if handle:
                self._gdi32.DeleteObject(handle)
        self._fonts.clear()
        self._brushes.clear()
        self._pens.clear()

    def _fill_rect(self, hdc, left: int, top: int, right: int, bottom: int, brush_name: str) -> None:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT(left, top, right, bottom)
        self._user32.FillRect(hdc, ctypes.byref(rect), self._brushes[brush_name])

    def _round_rect(
        self,
        hdc,
        left: int,
        top: int,
        right: int,
        bottom: int,
        brush_name: str,
        pen_name: str | None = None,
        radius: int = 8,
    ) -> None:
        pen_key = pen_name or brush_name
        pen_handle = self._pens.get(pen_key, self._pens["null"])
        brush_handle = self._brushes.get(brush_name, self._brushes["window"])
        prev_pen = self._gdi32.SelectObject(hdc, pen_handle)
        prev_brush = self._gdi32.SelectObject(hdc, brush_handle)
        self._gdi32.RoundRect(hdc, left, top, right, bottom, radius, radius)
        self._gdi32.SelectObject(hdc, prev_brush)
        self._gdi32.SelectObject(hdc, prev_pen)

    def _draw_line(
        self,
        hdc,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        pen_name: str = "border",
    ) -> None:
        import ctypes
        from ctypes import wintypes

        prev_pen = self._gdi32.SelectObject(hdc, self._pens[pen_name])
        pt = wintypes.POINT()
        self._gdi32.MoveToEx(hdc, x1, y1, ctypes.byref(pt))
        self._gdi32.LineTo(hdc, x2, y2)
        self._gdi32.SelectObject(hdc, prev_pen)

    def _draw_text(
        self,
        hdc,
        text: str,
        left: int,
        top: int,
        right: int,
        bottom: int,
        *,
        font: str = "body",
        color: str = "text",
        flags: int = _DT_NOPREFIX,
    ) -> None:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT(left, top, right, bottom)
        previous_font = self._gdi32.SelectObject(hdc, self._fonts[font])
        self._gdi32.SetBkMode(hdc, _TRANSPARENT)
        self._gdi32.SetTextColor(hdc, _OMEGA_COLORS[color])
        self._user32.DrawTextW(hdc, str(text), -1, ctypes.byref(rect), flags)
        self._gdi32.SelectObject(hdc, previous_font)

    def _draw_button(self, lparam) -> int:
        import ctypes

        item = ctypes.cast(lparam, ctypes.POINTER(_DrawItemStruct)).contents
        left, top, right, bottom = (
            item.rcItem.left,
            item.rcItem.top,
            item.rcItem.right,
            item.rcItem.bottom,
        )
        disabled = bool(item.itemState & _ODS_DISABLED)
        selected = bool(item.itemState & _ODS_SELECTED)
        ctl_id = item.CtlID

        if ctl_id == _BTN_BOOT:
            bg_brush = "ember_hover" if selected else ("ember" if not disabled else "border")
            border_pen = "ember_hover" if selected else ("ember" if not disabled else "border")
            text_color = "text"
        elif ctl_id in {_BTN_NEXT, _BTN_UNLOCK}:
            bg_brush = "cyan" if not disabled else "border"
            border_pen = "cyan" if not disabled else "border"
            text_color = "header"
        elif ctl_id == _BTN_RESET:
            bg_brush = "card_alt" if not selected else "border"
            border_pen = "danger" if not disabled else "border"
            text_color = "danger" if not disabled else "muted"
        else:
            bg_brush = "card_alt" if not selected else "border"
            border_pen = "border_light" if not selected else "cyan"
            text_color = "text" if not disabled else "muted"

        prev_pen = self._gdi32.SelectObject(item.hDC, self._pens[border_pen])
        prev_brush = self._gdi32.SelectObject(item.hDC, self._brushes[bg_brush])
        self._gdi32.RoundRect(item.hDC, left, top, right, bottom, 8, 8)
        self._gdi32.SelectObject(item.hDC, prev_brush)
        self._gdi32.SelectObject(item.hDC, prev_pen)

        offset = 1 if selected else 0
        self._draw_text(
            item.hDC,
            self._button_labels.get(int(ctl_id), ""),
            left,
            top + offset,
            right,
            bottom + offset,
            font="button",
            color=text_color,
            flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE | _DT_NOPREFIX,
        )
        return 1

    def _init_hero_image(self) -> None:
        import ctypes

        self._hero_image = None
        self._gdiplus = None
        self._gdiplus_token = None
        try:
            self._init_hero_image_impl(ctypes)
        except Exception as exc:  # noqa: BLE001 - the wizard must still be usable without art
            log.warning("portable hero image unavailable: %s", exc)

    def _init_hero_image_impl(self, ctypes) -> None:

        class GdiplusStartupInput(ctypes.Structure):
            _fields_ = [
                ("GdiplusVersion", ctypes.c_uint32),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", ctypes.c_bool),
                ("SuppressExternalCodecs", ctypes.c_bool),
            ]

        self._gdiplus = ctypes.windll.gdiplus
        self._gdiplus.GdiplusStartup.restype = ctypes.c_int
        self._gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
        self._gdiplus.GdipLoadImageFromFile.restype = ctypes.c_int
        self._gdiplus.GdipLoadImageFromFile.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._gdiplus.GdipCreateFromHDC.restype = ctypes.c_int
        self._gdiplus.GdipCreateFromHDC.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._gdiplus.GdipDrawImageRectI.restype = ctypes.c_int
        self._gdiplus.GdipDrawImageRectI.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._gdiplus.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
        self._gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        startup = GdiplusStartupInput(1, None, False, False)
        token = ctypes.c_void_p()
        status = self._gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None)
        if status != 0:
            raise RuntimeError(f"could not initialize the portable hero image (GDI+ status {status})")
        self._gdiplus_token = token
        image = ctypes.c_void_p()
        status = self._gdiplus.GdipLoadImageFromFile(
            ctypes.c_wchar_p(str(self.hero_path)), ctypes.byref(image)
        )
        if status != 0:
            self._gdiplus.GdiplusShutdown(self._gdiplus_token)
            raise RuntimeError(f"could not load the portable hero image (GDI+ status {status})")
        self._hero_image = image

    def _paint_hero(self, hwnd) -> None:
        import ctypes
        from ctypes import wintypes

        paint = _PaintStruct()
        hdc = self._user32.BeginPaint(hwnd, ctypes.byref(paint))
        client = wintypes.RECT()
        self._user32.GetClientRect(hwnd, ctypes.byref(client))
        width, height = client.right, client.bottom

        mem_dc = self._gdi32.CreateCompatibleDC(hdc)
        mem_bmp = self._gdi32.CreateCompatibleBitmap(hdc, width, height)
        old_bmp = self._gdi32.SelectObject(mem_dc, mem_bmp)

        # 1. Base window background
        self._fill_rect(mem_dc, 0, 0, width, height, "window")

        # 2. Header bar
        self._fill_rect(mem_dc, 0, 0, width, 86, "header")
        self._draw_line(mem_dc, 0, 86, width, 86, "border")

        # Brand emblem badge
        self._round_rect(mem_dc, 32, 16, 86, 70, "card_alt", "border_light", radius=12)
        self._draw_text(
            mem_dc,
            "Ω",
            32,
            16,
            86,
            70,
            font="emblem",
            color="cyan",
            flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
        )

        # Brand title & subtitle
        self._draw_text(mem_dc, "OMEGA 3.0", 102, 18, 550, 46, font="brand", color="text")
        self._draw_text(
            mem_dc,
            "AUTONOMOUS COGNITIVE ARCHITECTURE  //  PORTABLE RUNTIME",
            104,
            50,
            650,
            70,
            font="badge",
            color="muted",
        )

        # Header status pill
        badge_left = width - 210
        self._round_rect(mem_dc, badge_left, 26, width - 32, 60, "card_alt", "border", radius=8)
        self._draw_text(
            mem_dc,
            "●  SSD AIRGAPPED",
            badge_left,
            26,
            width - 32,
            60,
            font="badge",
            color="success",
            flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
        )

        # 3. Stepper navigation bar
        self._fill_rect(mem_dc, 0, 87, width, 138, "window")
        self._draw_line(mem_dc, 0, 138, width, 138, "border")

        if self.page in {0, 1, 2, 3, 4}:
            steps = [
                ("01", "SEAT SELECTION"),
                ("02", "SECURITY LOCK"),
                ("03", "MODEL ENGINE"),
                ("04", "READY TO BOOT"),
            ]
            active_step = 0 if self.page == 0 else (1 if self.page == 1 else (2 if self.page in {2, 3} else 3))
            for i, (num, label) in enumerate(steps):
                step_x = 32 + i * 224
                step_w = 208
                if i == active_step:
                    self._round_rect(mem_dc, step_x, 96, step_x + step_w, 128, "card", "cyan", radius=6)
                    self._draw_text(
                        mem_dc,
                        f"●  {num}  {label}",
                        step_x,
                        96,
                        step_x + step_w,
                        128,
                        font="badge",
                        color="cyan",
                        flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
                    )
                elif i < active_step:
                    self._round_rect(mem_dc, step_x, 96, step_x + step_w, 128, "window", "border", radius=6)
                    self._draw_text(
                        mem_dc,
                        f"✓  {num}  {label}",
                        step_x,
                        96,
                        step_x + step_w,
                        128,
                        font="badge",
                        color="text",
                        flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
                    )
                else:
                    self._round_rect(mem_dc, step_x, 96, step_x + step_w, 128, "window", "border", radius=6)
                    self._draw_text(
                        mem_dc,
                        f"    {num}  {label}",
                        step_x,
                        96,
                        step_x + step_w,
                        128,
                        font="badge",
                        color="muted",
                        flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
                    )
        elif self.page == 5:
            self._draw_text(
                mem_dc,
                "DOCUMENTATION  //  PRODUCT INFORMATION & OPERATOR GUIDE",
                52,
                98,
                width - 52,
                128,
                font="badge",
                color="cyan",
                flags=_DT_VCENTER | _DT_SINGLELINE,
            )
        elif self.page == 6:
            self._draw_text(
                mem_dc,
                "SECURITY CHECKPOINT  //  ENCRYPTED SESSION LOCK",
                52,
                98,
                width - 52,
                128,
                font="badge",
                color="ember",
                flags=_DT_VCENTER | _DT_SINGLELINE,
            )
        elif self.page == 7:
            self._draw_text(
                mem_dc,
                "BOOT SEQUENCE  //  PORTABLE SERVICES STARTING",
                52,
                98,
                width - 52,
                128,
                font="badge",
                color="cyan",
                flags=_DT_VCENTER | _DT_SINGLELINE,
            )

        # 4. Content card container
        card_top = 150
        card_bottom = 585
        if self.page == 0:
            # Left Card
            self._round_rect(mem_dc, 32, card_top, 562, card_bottom, "card", "border", radius=10)
            self._draw_line(mem_dc, 48, 222, 546, 222, "border")
            # Feature Chips
            self._round_rect(mem_dc, 52, 296, 202, 324, "card_alt", "border", radius=6)
            self._draw_text(mem_dc, "✓  100% PORTABLE", 52, 296, 202, 324, font="badge", color="success", flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE)
            self._round_rect(mem_dc, 212, 296, 372, 324, "card_alt", "border", radius=6)
            self._draw_text(mem_dc, "✓  SINGLE-FIFO MIND", 212, 296, 372, 324, font="badge", color="cyan", flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE)
            self._round_rect(mem_dc, 382, 296, 542, 324, "card_alt", "border", radius=6)
            self._draw_text(mem_dc, "✓  ZERO HOST POLLUTION", 382, 296, 542, 324, font="badge", color="muted", flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE)

            # Right Card (Hero & Substrate Info)
            self._round_rect(mem_dc, 580, card_top, 908, card_bottom, "card", "border", radius=10)
            self._round_rect(mem_dc, 598, 168, 890, 362, "card_alt", "border", radius=8)
            if self._hero_image is not None and self._gdiplus is not None:
                graphics = ctypes.c_void_p()
                if self._gdiplus.GdipCreateFromHDC(mem_dc, ctypes.byref(graphics)) == 0:
                    self._gdiplus.GdipDrawImageRectI(
                        graphics,
                        self._hero_image,
                        606,
                        176,
                        276,
                        178,
                    )
                    self._gdiplus.GdipDeleteGraphics(graphics)
            # Specs in Right Card
            self._draw_text(mem_dc, "PORTABLE SUBSTRATE PROFILE", 602, 378, 886, 396, font="badge", color="cyan")
            self._draw_line(mem_dc, 602, 402, 886, 402, "border")
            self._draw_text(mem_dc, "COGNITION", 602, 412, 710, 430, font="small", color="muted")
            self._draw_text(mem_dc, "Unified 4-Pipeline Pressure", 710, 412, 886, 430, font="small", color="text", flags=_DT_RIGHT | _DT_SINGLELINE)
            self._draw_text(mem_dc, "MEMORY", 602, 440, 710, 458, font="small", color="muted")
            self._draw_text(mem_dc, "12-Lobe Dewey Coordinates", 710, 440, 886, 458, font="small", color="text", flags=_DT_RIGHT | _DT_SINGLELINE)
            self._draw_text(mem_dc, "VOICE", 602, 468, 710, 486, font="small", color="muted")
            self._draw_text(mem_dc, f"Parakeet STT + {launcher_voice_surfaces().hero_tts}", 710, 468, 886, 486, font="small", color="text", flags=_DT_RIGHT | _DT_SINGLELINE)
            self._draw_text(mem_dc, "STORAGE", 602, 496, 710, 514, font="small", color="muted")
            self._draw_text(mem_dc, "Airgapped SSD Volume", 710, 496, 886, 514, font="small", color="text", flags=_DT_RIGHT | _DT_SINGLELINE)
            self._draw_text(mem_dc, "CONSENT", 602, 524, 710, 542, font="small", color="muted")
            self._draw_text(mem_dc, "Final at Connection (§0.1)", 710, 524, 886, 552, font="small", color="success", flags=_DT_RIGHT | _DT_SINGLELINE)
        else:
            self._round_rect(mem_dc, 32, card_top, width - 32, card_bottom, "card", "border", radius=10)
            self._draw_line(mem_dc, 48, 222, width - 48, 222, "border")

        if self.page == 7:
            # The setup window remains responsive while boot.py performs the
            # slow portable warmup. The actual text is updated by WM_TIMER;
            # this bar only paints the latest bounded progress value.
            self._draw_text(
                mem_dc,
                "OMEGA BOOT STATUS",
                52,
                248,
                width - 52,
                272,
                font="badge",
                color="cyan",
            )
            self._round_rect(mem_dc, 52, 352, width - 52, 374, "control", "border_light", radius=8)
            progress_width = max(0, min(width - 104, int((width - 104) * self.boot_display.percent / 100)))
            if progress_width:
                self._round_rect(
                    mem_dc,
                    52,
                    352,
                    52 + progress_width,
                    374,
                    "cyan" if self.boot_display.phase != "failed" else "danger",
                    None,
                    radius=8,
                )
            self._draw_text(
                mem_dc,
                f"{self.boot_display.percent}%",
                width - 150,
                382,
                width - 52,
                410,
                font="badge",
                color="cyan" if self.boot_display.phase != "failed" else "danger",
                flags=_DT_RIGHT | _DT_VCENTER | _DT_SINGLELINE,
            )

        # 5. Footer Action Bar
        self._fill_rect(mem_dc, 0, 595, width, height, "window")
        self._draw_line(mem_dc, 0, 595, width, 595, "border")
        self._draw_text(
            mem_dc,
            "OMEGA 3.0  //  LOCAL-FIRST COGNITIVE SYSTEM  •  OPERATOR CONSENT IS FINAL",
            160,
            615,
            width - 160,
            642,
            font="small",
            color="muted",
            flags=_DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
        )

        # BitBlt to display
        self._gdi32.BitBlt(hdc, 0, 0, width, height, mem_dc, 0, 0, 0x00CC0020)

        # Cleanup memory DC
        self._gdi32.SelectObject(mem_dc, old_bmp)
        self._gdi32.DeleteObject(mem_bmp)
        self._gdi32.DeleteDC(mem_dc)

        self._user32.EndPaint(hwnd, ctypes.byref(paint))

    def _close_hero_image(self) -> None:
        if getattr(self, "_hero_image", None) is not None:
            self._gdiplus.GdipDisposeImage(self._hero_image)
            self._hero_image = None
        if getattr(self, "_gdiplus_token", None) is not None:
            self._gdiplus.GdiplusShutdown(self._gdiplus_token)
            self._gdiplus_token = None

    def _control(self, page: int, kind: str, text: str, style: int, x: int, y: int, w: int, h: int, control_id: int, *, ex: int = 0):
        if kind == "BUTTON":
            style |= _BS_OWNERDRAW
        hwnd = self._user32.CreateWindowExW(
            ex,
            kind,
            text,
            _WS_CHILD | _WS_TABSTOP | style,
            x,
            y,
            w,
            h,
            self.hwnd,
            control_id,
            self._instance,
            None,
        )
        if not hwnd:
            raise RuntimeError(f"could not create setup control {control_id}")
        self.pages.setdefault(page, []).append(hwnd)
        if kind == "BUTTON":
            self._button_labels[control_id] = text
            self._user32.SendMessageW(hwnd, _WM_SETFONT, self._fonts["button"], 1)
        elif control_id == 2001:
            self._user32.SendMessageW(hwnd, _WM_SETFONT, self._fonts["title"], 1)
        elif control_id == 2002:
            self._user32.SendMessageW(hwnd, _WM_SETFONT, self._fonts["subtitle"], 1)
        elif control_id in {2102, 2202, 2203, 2302, 2304, 2402, 2403, 2404, 2405, 2701}:
            self._user32.SendMessageW(hwnd, _WM_SETFONT, self._fonts["section"], 1)
        elif control_id in {2303, _SUMMARY_LABEL, _HELP_EDIT}:
            self._user32.SendMessageW(hwnd, _WM_SETFONT, self._fonts["mono"], 1)
        elif control_id in {2103, 2204}:
            self._user32.SendMessageW(hwnd, _WM_SETFONT, self._fonts["small"], 1)
        else:
            self._user32.SendMessageW(hwnd, _WM_SETFONT, self._fonts["body"], 1)
        return hwnd

    def _create_controls(self) -> None:
        self.controls["title"] = self._control(-1, "STATIC", "", _SS_LEFT, 52, 164, 836, 26, 2001)
        self.controls["subtitle"] = self._control(-1, "STATIC", "", _SS_LEFT, 52, 190, 836, 24, 2002)
        self.controls["back"] = self._control(-1, "BUTTON", "Back", 0, 32, 608, 110, 38, _BTN_BACK)
        self.controls["next"] = self._control(-1, "BUTTON", "Next", 0, 798, 608, 110, 38, _BTN_NEXT)

        # Page 0: Welcome & Model Seat Selection
        self._control(0, "STATIC", "Welcome to Omega 3.0. Choose the model seat for the mind, review the product information, then save the setup before booting.", _SS_LEFT, 52, 232, 490, 54, 2101)
        self._control(0, "STATIC", "SELECT MIND MODEL SEAT", _SS_LEFT, 52, 340, 300, 20, 2102)
        self.controls["backend"] = self._control(0, "COMBOBOX", "", _CBS_DROPDOWNLIST | _WS_BORDER, 52, 365, 340, 160, _BACKEND_COMBO)
        self._combo_values(self.controls["backend"], ["Local LM Studio", "Cloud provider"])
        self._control(0, "STATIC", "Choose Local LM Studio for offline privacy, or Cloud Provider for hosted APIs.", _SS_LEFT, 52, 408, 490, 20, 2103)
        self.controls["info_button"] = self._control(0, "BUTTON", "Product info & help", 0, 52, 475, 210, 38, _BTN_INFO)
        self.controls["welcome_reset"] = self._control(0, "BUTTON", "Master reset", 0, 278, 475, 150, 38, _BTN_RESET)

        # Page 1: Security Setup
        self._control(1, "STATIC", "Protect this portable installation with an optional launch password. Passwords are never stored; only a salted PBKDF2 verifier is kept on the product drive.", _SS_LEFT, 52, 232, 836, 40, 2201)
        self._control(1, "STATIC", "NEW LAUNCH PASSWORD", _SS_LEFT, 52, 285, 300, 20, 2202)
        self.controls["launch_password"] = self._control(1, "EDIT", "", _ES_LEFT | _ES_PASSWORD | _WS_BORDER | 0x00000080, 52, 310, 420, 32, _LAUNCH_PASSWORD_EDIT)
        self._control(1, "STATIC", "CONFIRM PASSWORD", _SS_LEFT, 52, 355, 300, 20, 2203)
        self.controls["launch_password_confirm"] = self._control(1, "EDIT", "", _ES_LEFT | _ES_PASSWORD | _WS_BORDER | 0x00000080, 52, 380, 420, 32, _LAUNCH_PASSWORD_CONFIRM_EDIT)
        self._control(1, "STATIC", "Minimum 8 characters. Leave both fields blank to keep the current setting.", _SS_LEFT, 52, 422, 600, 20, 2204)
        self.controls["disable_lock"] = self._control(1, "BUTTON", "Disable launch lock", 0, 52, 465, 190, 36, _BTN_DISABLE_LOCK)
        self.controls["security_status"] = self._control(1, "STATIC", "", _SS_LEFT, 260, 473, 400, 24, _SECURITY_STATUS_LABEL)

        # Page 2: Local LM Studio
        self._control(2, "STATIC", "Local LM Studio keeps model traffic on this machine. Start the portable GUI, enable its OpenAI-compatible server, and enter the loaded model id.", _SS_LEFT, 52, 232, 836, 40, 2301)
        self._control(2, "BUTTON", "Launch LM Studio GUI", 0, 52, 285, 220, 38, _BTN_LM)
        self._control(2, "BUTTON", "Launch Linux runtime", 0, 286, 285, 220, 38, _BTN_LINUX)
        self.controls["lm_update"] = self._control(2, "BUTTON", "Update LM Studio", 0, 520, 285, 220, 38, _BTN_LM_UPDATE)
        self.controls["lm_update_status"] = self._control(2, "STATIC", "Omega controls portable updates.", _SS_LEFT, 520, 330, 368, 24, 2305)
        self._control(2, "STATIC", "LM STUDIO SERVER ENDPOINT", _SS_LEFT, 52, 345, 300, 20, 2302)
        self._control(2, "STATIC", LOCAL_BASE_URL, _SS_LEFTNOWORDWRAP, 52, 370, 454, 28, 2303)
        self._control(2, "STATIC", "LOADED MODEL IDENTIFIER", _SS_LEFT, 52, 415, 300, 20, 2304)
        self.controls["local_model"] = self._control(2, "EDIT", "", _ES_LEFT | _WS_BORDER | 0x00000080, 52, 440, 454, 32, _LOCAL_MODEL_EDIT)

        # Page 3: Cloud Provider
        self._control(3, "STATIC", "Choose a cloud seat, enter its endpoint and API key, then test it to discover model ids.", _SS_LEFT, 52, 230, 836, 32, 2401)
        self._control(3, "STATIC", "CLOUD PROVIDER", _SS_LEFT, 52, 270, 250, 20, 2402)
        self.controls["provider"] = self._control(3, "COMBOBOX", "", _CBS_DROPDOWNLIST | _WS_BORDER, 52, 294, 380, 200, _PROVIDER_COMBO)
        self._combo_values(self.controls["provider"], [str(provider["name"]) for provider in self.catalog])
        self._control(3, "STATIC", "ENDPOINT URL", _SS_LEFT, 52, 344, 250, 20, 2403)
        self.controls["endpoint"] = self._control(3, "EDIT", "", _ES_LEFT | _WS_BORDER | 0x00000080, 52, 368, 380, 32, _ENDPOINT_EDIT)
        self._control(3, "STATIC", "MODEL IDENTIFIER", _SS_LEFT, 460, 270, 250, 20, 2404)
        self.controls["model"] = self._control(3, "COMBOBOX", "", _CBS_DROPDOWN | _WS_BORDER | _WS_VSCROLL, 460, 294, 420, 180, _MODEL_COMBO)
        self._control(3, "STATIC", "API KEY (ENCRYPTED AT REST)", _SS_LEFT, 460, 344, 250, 20, 2405)
        self.controls["key"] = self._control(3, "EDIT", "", _ES_LEFT | _ES_PASSWORD | _WS_BORDER | 0x00000080, 460, 368, 420, 32, _KEY_EDIT)
        self.controls["save_key"] = self._control(3, "BUTTON", "Save key securely", 0, 460, 414, 200, 36, _BTN_SAVE_KEY)
        self.controls["test"] = self._control(3, "BUTTON", "Test connection", 0, 52, 435, 170, 36, _BTN_TEST)
        self.controls["status"] = self._control(3, "STATIC", "", _SS_LEFT, 52, 480, 836, 24, _STATUS_LABEL)

        # Page 4: Ready to Boot
        self._control(4, "STATIC", "Review your setup. Save for later keeps Omega stopped. Save & Boot Omega starts the backend services and the HUD.", _SS_LEFT, 52, 230, 836, 30, 2501)
        self.controls["summary"] = self._control(4, "STATIC", "", _SS_LEFT, 52, 268, 836, 180, _SUMMARY_LABEL)
        self.controls["later"] = self._control(4, "BUTTON", "Save for later", 0, 52, 470, 160, 38, _BTN_LATER)
        self.controls["reset"] = self._control(4, "BUTTON", "Master reset", 0, 228, 470, 160, 38, _BTN_RESET)
        self.controls["boot"] = self._control(4, "BUTTON", "Save & Boot Omega", 1, 660, 465, 228, 44, _BTN_BOOT)

        # Page 5: Product Info & Help
        self._control(5, "STATIC", "Product information and help", _SS_LEFT, 52, 230, 836, 22, 2601)
        self.controls["help"] = self._control(5, "EDIT", "", _ES_MULTILINE | _ES_AUTOVSCROLL | _ES_READONLY | _WS_BORDER | _WS_VSCROLL, 52, 256, 836, 250, _HELP_EDIT)
        self.controls["website"] = self._control(5, "BUTTON", "Open website", 0, 52, 520, 140, 36, _BTN_WEBSITE)
        self.controls["privacy"] = self._control(5, "BUTTON", "Privacy policy", 0, 206, 520, 160, 36, _BTN_PRIVACY)
        self.controls["info_back"] = self._control(5, "BUTTON", "Back to setup", 0, 738, 520, 150, 36, _BTN_INFO_BACK)

        # Page 6: Launch Lock
        self._control(6, "STATIC", "This portable Omega installation is locked.", _SS_LEFT, 52, 240, 836, 28, 2701)
        self._control(6, "STATIC", "Enter the launch password to continue to setup.", _SS_LEFT, 52, 275, 836, 24, 2702)
        self.controls["unlock_password"] = self._control(6, "EDIT", "", _ES_LEFT | _ES_PASSWORD | _WS_BORDER | 0x00000080, 52, 318, 400, 34, _UNLOCK_PASSWORD_EDIT)
        self.controls["unlock"] = self._control(6, "BUTTON", "Unlock Omega", 1, 52, 375, 180, 40, _BTN_UNLOCK)

        # Page 7: Non-blocking boot progress. Closing this window never stops
        # the child runtime; it only hides the setup surface.
        self.controls["boot_status"] = self._control(7, "STATIC", "Starting the portable Omega runtime…", _SS_LEFT, 52, 282, 836, 32, 2801)
        self.controls["boot_detail"] = self._control(7, "STATIC", "The setup window will stay open while boot continues.", _SS_LEFT, 52, 310, 836, 32, 2802)
        self.controls["boot_elapsed"] = self._control(7, "STATIC", "Elapsed: 0 seconds", _SS_LEFT, 52, 395, 300, 24, 2803)
        self.controls["boot_hint"] = self._control(7, "STATIC", "You can hide this window safely; Omega will continue in the background.", _SS_LEFT, 52, 430, 836, 32, 2804)
        self.controls["boot_hide"] = self._control(7, "BUTTON", "Hide window", 0, 738, 500, 150, 38, _BTN_BOOT_HIDE)

        self._load_saved_form()
        self._show_page(0 if self.unlocked else 6)

    def _combo_values(self, control, values: list[str]) -> None:
        import ctypes

        self._user32.SendMessageW(control, _CB_RESETCONTENT, 0, 0)
        for value in values:
            text = ctypes.c_wchar_p(str(value))
            self._user32.SendMessageW(
                control,
                _CB_ADDSTRING,
                0,
                ctypes.cast(text, ctypes.c_void_p).value,
            )

    def _set_text(self, control, value: str) -> None:
        self._user32.SetWindowTextW(control, str(value))

    def _get_text(self, control) -> str:
        return self._get_raw_text(control).strip()

    def _get_raw_text(self, control) -> str:
        import ctypes

        length = int(self._user32.GetWindowTextLengthW(control))
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(control, buffer, length + 1)
        return buffer.value

    def _load_saved_form(self) -> None:
        saved = self.saved or {}
        backend = str(saved.get("backend") or "local")
        self._user32.SendMessageW(self.controls["backend"], _CB_SETCURSEL, 1 if backend == "cloud" else 0, 0)
        self._set_text(self.controls["local_model"], saved.get("local_model") or (saved.get("model") if backend == "local" else ""))
        if backend == "cloud":
            provider = str(saved.get("provider") or "custom")
            if provider in self.provider_ids:
                self._user32.SendMessageW(self.controls["provider"], _CB_SETCURSEL, self.provider_ids.index(provider), 0)
            self._refresh_provider(preserve_model=str(saved.get("model") or ""), preserve_endpoint=str(saved.get("base_url") or ""))
        else:
            self._refresh_provider()

    def _refresh_provider(
        self,
        *,
        preserve_model: str = "",
        preserve_endpoint: str = "",
        clear_key: bool = False,
    ) -> None:
        index = int(self._user32.SendMessageW(self.controls["provider"], _CB_GETCURSEL, 0, 0))
        if index < 0 or index >= len(self.catalog):
            return
        provider = self.catalog[index]
        endpoint = preserve_endpoint or str(provider.get("base_url") or "")
        self._set_text(self.controls["endpoint"], endpoint)
        models = [str(model) for model in provider.get("models") or []]
        self._combo_values(self.controls["model"], models)
        self._set_text(self.controls["model"], preserve_model)
        self._set_text(
            self.controls["status"],
            "API key saved securely for this provider."
            if _stored_api_key(self.product, str(provider.get("id") or ""))
            else "",
        )
        if clear_key:
            self._set_text(self.controls["key"], "")

    def _show_page(self, page: int) -> None:
        self.page = page
        for control_list in self.pages.values():
            for control in control_list:
                self._user32.ShowWindow(control, _SW_HIDE)
        for control in self.pages.get(-1, []):
            self._user32.ShowWindow(control, _SW_SHOW)
        titles = {
            0: ("Welcome to Omega 3.0", "Set up the model seat before the first boot."),
            1: ("Security setup", "Protect this portable installation with an optional launch lock."),
            2: ("Local model setup", "LM Studio remains under your control on the portable drive."),
            3: ("Cloud model setup", "Choose a provider and model; Omega will not switch seats silently."),
            4: ("Ready to boot", "The saved state can be edited again from this wizard on the next launch."),
            5: ("About Omega 3.0", "Product identity, privacy, intellectual property, and a quick guide."),
            6: ("Omega launch lock", "Enter your password to continue."),
            7: ("Starting Omega 3.0", "The portable runtime is starting; this window will close when the HUD launches."),
        }
        self._set_text(self.controls["title"], titles[page][0])
        self._set_text(self.controls["subtitle"], titles[page][1])
        for control in self.pages.get(page, []):
            self._user32.ShowWindow(control, _SW_SHOW)
        show_back = page in {1, 2, 3, 4, 5}
        show_next = page in {0, 1, 2, 3}
        self._user32.ShowWindow(self.controls["back"], _SW_SHOW if show_back and page != 5 else _SW_HIDE)
        self._user32.ShowWindow(self.controls["next"], _SW_SHOW if show_next else _SW_HIDE)
        if page == 4:
            self._update_summary()
        if page == 1:
            status = "Launch lock is enabled." if self.launch_lock["enabled"] else "Launch lock is disabled."
            self._set_text(self.controls["security_status"], status)
        if page == 2:
            self._set_text(self.controls["lm_update_status"], "Omega controls portable updates.")
        if page == 6:
            self._set_text(self.controls["unlock_password"], "")
        if page == 7:
            self._set_text(self.controls["boot_status"], self.boot_display.detail)
            self._set_text(self.controls["boot_detail"], self.boot_display.hint)
        if self.hwnd:
            self._user32.InvalidateRect(self.hwnd, None, True)

    def _backend(self) -> str:
        selected = int(self._user32.SendMessageW(self.controls["backend"], _CB_GETCURSEL, 0, 0))
        return "cloud" if selected == 1 else "local"

    def _cloud_provider_id(self) -> str:
        selected = int(self._user32.SendMessageW(self.controls["provider"], _CB_GETCURSEL, 0, 0))
        if selected < 0 or selected >= len(self.provider_ids):
            raise ValueError("choose a cloud provider")
        return self.provider_ids[selected]

    def _form(self) -> dict[str, Any]:
        if self._backend() == "local":
            return {"backend": "local", "model": self._get_text(self.controls["local_model"])}
        return {
            "backend": "cloud",
            "provider": self._cloud_provider_id(),
            "base_url": self._get_text(self.controls["endpoint"]),
            "model": self._get_text(self.controls["model"]),
        }

    def _validate_page(self) -> dict[str, Any]:
        form = self._form()
        normalised = _normalise_form(form)
        if normalised["backend"] == "cloud":
            key = self._get_text(self.controls["key"])
            if not key and not _stored_api_key(self.product, normalised["provider"]):
                raise RuntimeError("a cloud provider API key is required")
        return normalised

    def _validate_security_page(self) -> None:
        validate_launch_password_fields(
            self._get_raw_text(self.controls["launch_password"]),
            self._get_raw_text(self.controls["launch_password_confirm"]),
        )

    def _apply_security_configuration(self) -> None:
        password = self._get_raw_text(self.controls["launch_password"])
        if password:
            save_launch_password(self.product, password)
            self.launch_lock = {"enabled": True}
        elif self.disable_lock_requested:
            clear_launch_password(self.product)
            self.launch_lock = {"enabled": False}

    def _message(self, message: str, *, title: str = "Omega setup") -> None:
        self._user32.MessageBoxW(self.hwnd, message, title, _MB_OK | _MB_ICONERROR)


    def _notice(self, message: str, *, title: str = "Omega setup") -> None:
        self._user32.MessageBoxW(self.hwnd, message, title, _MB_OK)


    def _confirm(self, message: str, *, title: str = "Confirm factory reset") -> bool:
        return (
            self._user32.MessageBoxW(
                self.hwnd,
                message,
                title,
                _MB_YESNO | _MB_ICONWARNING,
            )
            == _IDYES
        )

    def _update_summary(self) -> None:
        text = setup_summary_text(
            self.product, self._form(), entered_key=self._get_text(self.controls["key"])
        )
        self._set_text(self.controls["summary"], text.replace("\n", "\r\n"))

    def _stop_boot_monitor(self) -> None:
        if self.boot_timer_active and self.hwnd:
            self._user32.KillTimer(self.hwnd, BOOT_TIMER_ID)
        self.boot_timer_active = False

    def _render_boot_progress(self, display: BootProgress, elapsed_seconds: float) -> None:
        self.boot_display = display
        self._set_text(self.controls["boot_status"], display.detail)
        self._set_text(self.controls["boot_detail"], display.hint)
        self._set_text(self.controls["boot_elapsed"], f"Elapsed: {max(0, int(elapsed_seconds))} seconds")
        if self.hwnd:
            self._user32.InvalidateRect(self.hwnd, None, True)

    def _poll_boot(self) -> None:
        """Refresh boot feedback and close as soon as the HUD reports ready."""
        if self.boot_process is None or self.boot_started_at is None:
            return
        elapsed = max(0.0, time.monotonic() - self.boot_started_at)
        returncode = self.boot_process.poll()
        if returncode is not None:
            self._render_boot_progress(
                boot_progress(elapsed, process_running=False, returncode=returncode),
                elapsed,
            )
            self._stop_boot_monitor()
            return

        try:
            status = read_boot_status(self.product)
        except RuntimeError:
            status = None
        display, action = interpret_boot_status(status, elapsed)
        self._render_boot_progress(display, elapsed)
        if action != "continue":
            self._stop_boot_monitor()
        if action == "close":
            self._user32.DestroyWindow(self.hwnd)

    def _begin_boot(self) -> None:
        # Browser ownership belongs to ``boot.py``.  It starts the HUD server
        # first, then gives its URL to the one debugger-enabled Edge process.
        # Preparing Edge here would create a blank window before boot can bind
        # the HUD and is the source of the duplicate-window startup sequence.
        self.boot_process = launch_omega(self.product)
        self.boot_requested = True
        self.boot_started_at = time.monotonic()
        self.boot_display = boot_progress(0.0)
        self._show_page(7)
        timer = self._user32.SetTimer(
            self.hwnd,
            BOOT_TIMER_ID,
            BOOT_POLL_INTERVAL_MS,
            None,
        )
        if not timer:
            raise RuntimeError("could not start the Omega boot progress monitor")
        self.boot_timer_active = True
        self._poll_boot()

    def _on_command(self, control_id: int, notification: int) -> None:
        try:
            if control_id == _PROVIDER_COMBO and notification == _CBN_SELCHANGE:
                self._refresh_provider(clear_key=True)
            elif control_id == _BTN_INFO:
                self._set_text(self.controls["help"], self._info_text())
                self._show_page(5)
            elif control_id == _BTN_INFO_BACK:
                self._show_page(0)
            elif control_id == _BTN_WEBSITE:
                open_external_url(str(self.info.get("website") or ""))
            elif control_id == _BTN_PRIVACY:
                privacy = self.info.get("privacy_policy") or {}
                open_external_url(str(privacy.get("url") or ""))
            elif control_id == _BTN_UNLOCK:
                if verify_launch_password(
                    self.product,
                    self._get_raw_text(self.controls["unlock_password"]),
                ):
                    self.unlocked = True
                    self._show_page(0)
                else:
                    self._message("Incorrect launch password.", title="Omega launch lock")
            elif control_id == _BTN_BACK:
                self._show_page(1 if self.page in {2, 3} else 0)
            elif control_id == _BTN_NEXT:
                self._next()
            elif control_id == _BTN_LM:
                launch_lm_studio(ssd_root(self.product))
            elif control_id == _BTN_LINUX:
                launch_linux_runtime(ssd_root(self.product))
            elif control_id == _BTN_LM_UPDATE:
                self._set_text(self.controls["lm_update_status"], "Checking for updates...")
                result = update_lm_studio(ssd_root(self.product))
                if result is None:
                    self._set_text(self.controls["lm_update_status"], "LM Studio is up to date.")
                    self._notice("The portable LM Studio installation is already up to date.")
                else:
                    self._set_text(
                        self.controls["lm_update_status"],
                        f"Updated to {result.version}; restarting portable app.",
                    )
                    self._notice(f"LM Studio was updated to {result.version}.")
            elif control_id == _BTN_BOOT_HIDE:
                self._user32.DestroyWindow(self.hwnd)
            elif control_id == _BTN_TEST:
                self._test_connection()
            elif control_id == _BTN_SAVE_KEY:
                self._save_key()
            elif control_id == _BTN_LATER:
                self._save(False)
            elif control_id == _BTN_DISABLE_LOCK:
                self.disable_lock_requested = True
                self._set_text(self.controls["launch_password"], "")
                self._set_text(self.controls["launch_password_confirm"], "")
                self._set_text(self.controls["security_status"], "Launch lock will be disabled when saved.")
            elif control_id == _BTN_RESET:
                if self._confirm(
                    "Master reset will remove all Omega memory, saved model/provider settings, API keys, launch lock, HUD settings, plugin state, logs, and queued work. External folders are not touched. Continue?"
                ):
                    factory_reset(self.product)
                    self._notice(
                        "Omega has been returned to its out-of-box state. Set up a model seat again before booting.",
                        title="Omega factory reset complete",
                    )
                    self._user32.DestroyWindow(self.hwnd)
            elif control_id == _BTN_BOOT:
                self._save(True)
        except Exception as exc:  # noqa: BLE001 - a GUI boundary must show its cause
            self._message(str(exc))

    def _next(self) -> None:
        if self.page == 0:
            self._show_page(1)
        elif self.page == 1:
            self._validate_security_page()
            self._show_page(2 if self._backend() == "local" else 3)
        elif self.page in {2, 3}:
            self._validate_page()
            self._show_page(4)

    def _test_connection(self) -> None:
        form = self._form()
        normalised = _normalise_form(form)
        entered = self._get_text(self.controls["key"])
        key = entered or _stored_api_key(self.product, normalised["provider"]) or ""
        models = discover_cloud_models_for_setup(normalised["provider"], key, normalised["base_url"])
        existing = self._get_text(self.controls["model"])
        self._combo_values(self.controls["model"], models)
        self._set_text(self.controls["model"], existing or models[0])
        self._set_text(self.controls["status"], f"Connection OK — {len(models)} model(s) found.")

    def _save_key(self) -> None:
        """Explicitly persist the entered provider key in the encrypted vault."""
        form = self._form()
        if form["backend"] != "cloud":
            raise RuntimeError("select Cloud provider before saving an API key")
        provider_id = form["provider"]
        key = self._get_text(self.controls["key"])
        if not key:
            raise RuntimeError("enter an API key before saving it")
        _save_api_key(self.product, provider_id, key)
        self._set_text(self.controls["key"], "")
        provider = get_cloud_provider(provider_id)
        self._set_text(
            self.controls["status"],
            f"API key saved securely for {provider.name}.",
        )

    def _save(self, boot: bool) -> None:
        self._validate_security_page()
        form = self._form()
        if form["backend"] == "cloud" and self._get_text(self.controls["key"]):
            raise RuntimeError(
                "click Save key securely before saving the cloud setup"
            )
        save_startup_configuration(
            self.product,
            form,
            api_key="",
        )
        self._apply_security_configuration()
        if not boot:
            self._message("Setup saved. Omega is still stopped.", title="Omega setup saved")
            self._user32.DestroyWindow(self.hwnd)
            return
        self._begin_boot()

    def _info_text(self) -> str:
        return setup_info_text(self.product, self.info).replace("\n", "\r\n")


class TkSetupWizard:
    """The SetupWizard off Windows: the same pages, state, launches and look.

    ``scripts/omega_launcher_art.py`` paints each page exactly as the Win32
    ``_paint_hero`` does (same coordinates, palette and button styles, with the
    product's bundled fonts); the live inputs sit on top at the rectangles of
    ``_create_controls``. Every decision goes through the module-level functions
    both wizards share, so the two toolkits cannot drift.
    """

    LM_STUDIO_UPDATE_NOTE = "Portable LM Studio updates itself on this drive."

    def __init__(self, product: Path) -> None:
        self.product = Path(product).resolve()
        self.info = load_product_info(self.product)
        self.hero_path = _product_asset(self.product, str(self.info["hero_image"]), "Omega hero image")
        self.saved = load_startup_state(self.product)
        self.launch_lock = load_launch_lock(self.product)
        self.unlocked = not self.launch_lock["enabled"]
        self.disable_lock_requested = False
        self.catalog = public_cloud_providers()
        self.provider_ids = [str(provider["id"]) for provider in self.catalog]
        self.page = 0
        self.boot_requested = False
        self.boot_process: subprocess.Popen | None = None
        self.boot_started_at: float | None = None
        self.boot_display = boot_progress(0.0)
        self.state: dict[str, Any] = {}
        self.root: Any = None
        self._pressed: str | None = None

    def run(self) -> bool:
        self.build()
        self.show_page(0 if self.unlocked else 6)
        self.root.mainloop()
        return self.boot_requested

    # ── construction ────────────────────────────────────────────────────────
    def build(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from scripts import omega_launcher_art as art

        self._tk = tk
        self._art = art
        self.fonts = art.FontSet(self.product)
        self.state = {
            "hero": art.load_hero(self.hero_path),
            "hero_tts": launcher_voice_surfaces().hero_tts,
            "lm_update_status": self.LM_STUDIO_UPDATE_NOTE,
        }
        color = {name: "#%02x%02x%02x" % rgb for name, rgb in OMEGA_PALETTE.items()}
        self._color = color

        root = tk.Tk(className="Omega")
        self.root = root
        root.title("Omega 3.0 setup")
        root.geometry(f"{art.WIDTH}x{art.HEIGHT}")
        root.resizable(False, False)
        root.configure(bg=color["window"])
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        entry_font = ("nimbus sans l", -13)
        root.option_add("*TCombobox*Listbox.background", color["control"])
        root.option_add("*TCombobox*Listbox.foreground", color["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", color["cyan"])
        root.option_add("*TCombobox*Listbox.selectForeground", color["header"])
        root.option_add("*TCombobox*Listbox.font", entry_font)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(
            "Omega.TCombobox",
            fieldbackground=color["control"],
            background=color["card_alt"],
            foreground=color["text"],
            arrowcolor=color["text"],
            bordercolor=color["border_light"],
            lightcolor=color["control"],
            darkcolor=color["control"],
            selectbackground=color["control"],
            selectforeground=color["text"],
        )
        style.map(
            "Omega.TCombobox",
            fieldbackground=[("readonly", color["control"])],
            foreground=[("readonly", color["text"])],
            background=[("pressed", color["border"]), ("active", color["border"]), ("readonly", color["card_alt"])],
            arrowcolor=[("disabled", color["muted"]), ("readonly", color["text"])],
            bordercolor=[("focus", color["cyan"])],
        )
        style.configure(
            "Omega.Vertical.TScrollbar",
            background=color["card_alt"],
            troughcolor=color["control"],
            bordercolor=color["border"],
            arrowcolor=color["muted"],
        )

        canvas = tk.Canvas(root, width=art.WIDTH, height=art.HEIGHT, highlightthickness=0, bd=0, bg=color["window"])
        canvas.pack()
        self.canvas = canvas
        self._frame_item = canvas.create_image(0, 0, anchor="nw")
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<Motion>", self._on_motion)

        self.vars = {
            name: tk.StringVar()
            for name in (
                "backend", "launch_password", "launch_password_confirm", "local_model",
                "provider", "endpoint", "model", "key", "unlock_password",
            )
        }
        self.widgets: dict[str, Any] = {}
        self._field_items: dict[str, int] = {}
        for page_number, page in art.PAGES.items():
            for field in page.fields:
                widget = self._make_field(field, entry_font, ttk)
                self.widgets[field.name] = widget
                self._field_items[field.name] = canvas.create_window(
                    field.rect.x, field.rect.y, anchor="nw",
                    width=field.rect.w, height=field.rect.h,
                    window=widget, state="hidden",
                )
        self.widgets["backend"].configure(values=["Local LM Studio", "Cloud provider"])
        self.widgets["provider"].configure(values=[str(provider["name"]) for provider in self.catalog])
        self.widgets["provider"].bind(
            "<<ComboboxSelected>>",
            lambda _event: self._guard(lambda: self._refresh_provider(clear_key=True))(),
        )
        self.widgets["unlock_password"].bind("<Return>", lambda _event: self._guard(self._unlock)())
        self._load_saved_form()

    def _make_field(self, field: Any, entry_font: Any, ttk: Any) -> Any:
        tk = self._tk
        color = self._color
        if field.kind in {"combo", "combo_edit"}:
            return ttk.Combobox(
                self.canvas,
                textvariable=self.vars[field.name],
                state="readonly" if field.kind == "combo" else "normal",
                style="Omega.TCombobox",
                font=entry_font,
            )
        if field.kind == "help":
            frame = tk.Frame(self.canvas, bg=color["control"], highlightthickness=1, highlightbackground=color["border_light"])
            view = tk.Canvas(frame, bg=color["control"], highlightthickness=0, bd=0)
            bar = ttk.Scrollbar(frame, orient="vertical", command=view.yview, style="Omega.Vertical.TScrollbar")
            view.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            view.pack(side="left", fill="both", expand=True)
            for sequence, delta in (("<Button-4>", -3), ("<Button-5>", 3)):
                view.bind(sequence, lambda _event, step=delta: view.yview_scroll(step, "units"))
            view.bind("<MouseWheel>", lambda event: view.yview_scroll(-1 if event.delta > 0 else 1, "units"))
            self._help_view = view
            self._help_item = view.create_image(0, 0, anchor="nw")
            return frame
        return tk.Entry(
            self.canvas,
            textvariable=self.vars[field.name],
            show="•" if field.kind == "password" else "",
            bg=color["control"],
            fg=color["text"],
            insertbackground=color["cyan"],
            disabledbackground=color["control"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=color["border_light"],
            highlightcolor=color["cyan"],
            font=entry_font,
        )

    # ── painting & input ────────────────────────────────────────────────────
    def _render(self) -> None:
        frame = self._art.render_page(self.page, self.fonts, self.state, pressed=self._pressed)
        self._frame_photo = self._tk.PhotoImage(data=self._art.png_bytes(frame), format="png")
        self.canvas.itemconfigure(self._frame_item, image=self._frame_photo)

    def _on_press(self, event: Any) -> None:
        button = self._art.button_at(self.page, event.x, event.y)
        if button is not None:
            self._pressed = button.action
            self._render()

    def _on_release(self, event: Any) -> None:
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            return
        self._render()
        button = self._art.button_at(self.page, event.x, event.y)
        if button is not None and button.action == pressed:
            self._guard(lambda: self._dispatch(pressed))()

    def _on_motion(self, event: Any) -> None:
        over = self._art.button_at(self.page, event.x, event.y) is not None
        self.canvas.configure(cursor="hand2" if over else "")

    def _dispatch(self, action: str) -> None:
        actions: dict[str, Callable[[], None]] = {
            "back": self._back,
            "next": self._next,
            "info": self._open_about,
            "info_back": lambda: self.show_page(0),
            "website": lambda: open_external_url(str(self.info.get("website") or "")),
            "privacy": self._open_privacy,
            "reset": self._master_reset,
            "disable_lock": self._disable_lock,
            "launch_lm_studio": lambda: launch_linux_runtime(ssd_root(self.product)),
            "test": self._test_connection,
            "save_key": self._save_key,
            "later": lambda: self._save(False),
            "boot": lambda: self._save(True),
            "unlock": self._unlock,
            "boot_hide": self.root.destroy,
        }
        actions[action]()

    def _guard(self, action: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - a GUI boundary must show its cause
                self._message(str(exc))

        return run

    def _set(self, key: str, value: str) -> None:
        self.state[key] = value
        self._render()

    # ── state & navigation ──────────────────────────────────────────────────
    def _load_saved_form(self) -> None:
        saved = self.saved or {}
        backend = str(saved.get("backend") or "local")
        self.vars["backend"].set("Cloud provider" if backend == "cloud" else "Local LM Studio")
        self.vars["local_model"].set(saved.get("local_model") or (saved.get("model") if backend == "local" else "") or "")
        provider = str(saved.get("provider") or "custom") if backend == "cloud" else ""
        self.widgets["provider"].current(self.provider_ids.index(provider) if provider in self.provider_ids else 0)
        if backend == "cloud":
            self._refresh_provider(
                preserve_model=str(saved.get("model") or ""),
                preserve_endpoint=str(saved.get("base_url") or ""),
            )
        else:
            self._refresh_provider()

    def _refresh_provider(
        self,
        *,
        preserve_model: str = "",
        preserve_endpoint: str = "",
        clear_key: bool = False,
    ) -> None:
        index = self.widgets["provider"].current()
        if index < 0 or index >= len(self.catalog):
            return
        provider = self.catalog[index]
        self.vars["endpoint"].set(preserve_endpoint or str(provider.get("base_url") or ""))
        self.widgets["model"].configure(values=[str(model) for model in provider.get("models") or []])
        self.vars["model"].set(preserve_model)
        self.state["status"] = (
            "API key saved securely for this provider."
            if _stored_api_key(self.product, str(provider.get("id") or ""))
            else ""
        )
        if clear_key:
            self.vars["key"].set("")
        if self.page == 3:
            self._render()

    def show_page(self, page: int) -> None:
        self.page = page
        titles = {
            0: ("Welcome to Omega 3.0", "Set up the model seat before the first boot."),
            1: ("Security setup", "Protect this portable installation with an optional launch lock."),
            2: ("Local model setup", "LM Studio remains under your control on the portable drive."),
            3: ("Cloud model setup", "Choose a provider and model; Omega will not switch seats silently."),
            4: ("Ready to boot", "The saved state can be edited again from this wizard on the next launch."),
            5: ("About Omega 3.0", "Product identity, privacy, intellectual property, and a quick guide."),
            6: ("Omega launch lock", "Enter your password to continue."),
            7: ("Starting Omega 3.0", "The portable runtime is starting; this window will close when the HUD launches."),
        }
        self.state["title"], self.state["subtitle"] = titles[page]
        visible = {field.name for field in self._art.PAGES[page].fields}
        for name, item in self._field_items.items():
            self.canvas.itemconfigure(item, state="normal" if name in visible else "hidden")
        if page == 1:
            self.state["security_status"] = (
                "Launch lock is enabled." if self.launch_lock["enabled"] else "Launch lock is disabled."
            )
        if page == 4:
            self._update_summary()
        if page == 6:
            self.vars["unlock_password"].set("")
        if page == 7:
            self._render_boot_progress(self.boot_display, 0.0, paint=False)
        self._render()

    def _backend(self) -> str:
        return "cloud" if self.vars["backend"].get() == "Cloud provider" else "local"

    def _cloud_provider_id(self) -> str:
        index = self.widgets["provider"].current()
        if index < 0 or index >= len(self.provider_ids):
            raise ValueError("choose a cloud provider")
        return self.provider_ids[index]

    def _form(self) -> dict[str, Any]:
        if self._backend() == "local":
            return {"backend": "local", "model": self.vars["local_model"].get().strip()}
        return {
            "backend": "cloud",
            "provider": self._cloud_provider_id(),
            "base_url": self.vars["endpoint"].get().strip(),
            "model": self.vars["model"].get().strip(),
        }

    def _validate_page(self) -> dict[str, Any]:
        normalised = _normalise_form(self._form())
        if normalised["backend"] == "cloud":
            if not self.vars["key"].get().strip() and not _stored_api_key(self.product, normalised["provider"]):
                raise RuntimeError("a cloud provider API key is required")
        return normalised

    def _next(self) -> None:
        if self.page == 0:
            self.show_page(1)
        elif self.page == 1:
            validate_launch_password_fields(
                self.vars["launch_password"].get(), self.vars["launch_password_confirm"].get()
            )
            self.show_page(2 if self._backend() == "local" else 3)
        elif self.page in {2, 3}:
            self._validate_page()
            self.show_page(4)

    def _back(self) -> None:
        self.show_page(1 if self.page in {2, 3} else 0)

    def _update_summary(self) -> None:
        self.state["summary"] = setup_summary_text(
            self.product, self._form(), entered_key=self.vars["key"].get().strip()
        )

    # ── actions ─────────────────────────────────────────────────────────────
    def _message(self, message: str, *, title: str = "Omega setup") -> None:
        from tkinter import messagebox

        messagebox.showerror(title, message, parent=self.root)

    def _notice(self, message: str, *, title: str = "Omega setup") -> None:
        from tkinter import messagebox

        messagebox.showinfo(title, message, parent=self.root)

    def _confirm(self, message: str, *, title: str = "Confirm factory reset") -> bool:
        from tkinter import messagebox

        return bool(messagebox.askyesno(title, message, icon="warning", parent=self.root))

    def _open_about(self) -> None:
        block = self._art.render_text_block(self.fonts, setup_info_text(self.product, self.info), 816)
        self._help_photo = self._tk.PhotoImage(data=self._art.png_bytes(block), format="png")
        self._help_view.itemconfigure(self._help_item, image=self._help_photo)
        self._help_view.configure(scrollregion=(0, 0, block.width, block.height))
        self._help_view.yview_moveto(0)
        self.show_page(5)

    def _open_privacy(self) -> None:
        privacy = self.info.get("privacy_policy") or {}
        open_external_url(str(privacy.get("url") or ""))

    def _unlock(self) -> None:
        if verify_launch_password(self.product, self.vars["unlock_password"].get()):
            self.unlocked = True
            self.show_page(0)
        else:
            self._message("Incorrect launch password.", title="Omega launch lock")

    def _disable_lock(self) -> None:
        self.disable_lock_requested = True
        self.vars["launch_password"].set("")
        self.vars["launch_password_confirm"].set("")
        self._set("security_status", "Launch lock will be disabled when saved.")

    def _master_reset(self) -> None:
        if self._confirm(
            "Master reset will remove all Omega memory, saved model/provider settings, API keys, launch lock, HUD settings, plugin state, logs, and queued work. External folders are not touched. Continue?"
        ):
            factory_reset(self.product)
            self._notice(
                "Omega has been returned to its out-of-box state. Set up a model seat again before booting.",
                title="Omega factory reset complete",
            )
            self.root.destroy()

    def _test_connection(self) -> None:
        normalised = _normalise_form(self._form())
        entered = self.vars["key"].get().strip()
        key = entered or _stored_api_key(self.product, normalised["provider"]) or ""
        models = discover_cloud_models_for_setup(normalised["provider"], key, normalised["base_url"])
        existing = self.vars["model"].get().strip()
        self.widgets["model"].configure(values=models)
        self.vars["model"].set(existing or models[0])
        self._set("status", f"Connection OK — {len(models)} model(s) found.")

    def _save_key(self) -> None:
        form = self._form()
        if form["backend"] != "cloud":
            raise RuntimeError("select Cloud provider before saving an API key")
        key = self.vars["key"].get().strip()
        if not key:
            raise RuntimeError("enter an API key before saving it")
        _save_api_key(self.product, form["provider"], key)
        self.vars["key"].set("")
        self._set("status", f"API key saved securely for {get_cloud_provider(form['provider']).name}.")

    def _save(self, boot: bool) -> None:
        password = self.vars["launch_password"].get()
        validate_launch_password_fields(password, self.vars["launch_password_confirm"].get())
        form = self._form()
        if form["backend"] == "cloud" and self.vars["key"].get().strip():
            raise RuntimeError("click Save key securely before saving the cloud setup")
        save_startup_configuration(self.product, form, api_key="")
        if password:
            save_launch_password(self.product, password)
            self.launch_lock = {"enabled": True}
        elif self.disable_lock_requested:
            clear_launch_password(self.product)
            self.launch_lock = {"enabled": False}
        if not boot:
            self._notice("Setup saved. Omega is still stopped.", title="Omega setup saved")
            self.root.destroy()
            return
        self._begin_boot()

    # ── boot progress ───────────────────────────────────────────────────────
    def _render_boot_progress(self, display: BootProgress, elapsed_seconds: float, *, paint: bool = True) -> None:
        self.boot_display = display
        self.state.update(
            boot_status=display.detail,
            boot_detail=display.hint,
            boot_elapsed=f"Elapsed: {max(0, int(elapsed_seconds))} seconds",
            boot_phase=display.phase,
            boot_percent=display.percent,
        )
        if paint:
            self._render()

    def _begin_boot(self) -> None:
        # Browser ownership belongs to boot.py, exactly as on Windows.
        self.boot_process = launch_omega(self.product)
        self.boot_requested = True
        self.boot_started_at = time.monotonic()
        self.boot_display = boot_progress(0.0)
        self.show_page(7)
        self.root.after(BOOT_POLL_INTERVAL_MS, self._poll_boot)

    def _poll_boot(self) -> None:
        """Refresh boot feedback and close as soon as the HUD reports ready."""
        if self.boot_process is None or self.boot_started_at is None:
            return
        elapsed = max(0.0, time.monotonic() - self.boot_started_at)
        returncode = self.boot_process.poll()
        if returncode is not None:
            self._render_boot_progress(
                boot_progress(elapsed, process_running=False, returncode=returncode), elapsed
            )
            return
        try:
            status = read_boot_status(self.product)
        except RuntimeError:
            status = None
        display, action = interpret_boot_status(status, elapsed)
        self._render_boot_progress(display, elapsed)
        if action == "close":
            self.root.destroy()
        elif action == "continue":
            self.root.after(BOOT_POLL_INTERVAL_MS, self._poll_boot)


def setup_wizard_class(os_name: str | None = None) -> type:
    """The native wizard on Windows; the Tk wizard everywhere else."""
    return SetupWizard if (os_name or os.name) == "nt" else TkSetupWizard


def run_setup_wizard(product: Path | None = None) -> bool:
    """Run the setup wizard and return whether it launched Omega."""
    root = (product or product_root()).resolve()
    if getattr(sys, "frozen", False):
        hide_portable_runtime(root)
    return setup_wizard_class()(root).run()


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch the public Omega.exe setup surface or an authorized child launch."""
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if not arguments:
            run_setup_wizard()
            return
        command = arguments.pop(0).casefold()
        root = product_root()
        if getattr(sys, "frozen", False):
            hide_portable_runtime(root)
        if command == LAUNCH_HUD_COMMAND:
            launch_hud(root, arguments)
        elif command == LAUNCH_LM_STUDIO_COMMAND:
            launch_lm_studio(ssd_root(root))
        else:
            raise ValueError(
                f"unknown Omega launcher command: {command} "
                f"(expected {LAUNCH_HUD_COMMAND} or {LAUNCH_LM_STUDIO_COMMAND})"
            )
    except Exception as exc:  # noqa: BLE001 - the startup boundary must be visible
        show_launcher_error(f"Omega could not start: {exc}")


if __name__ == "__main__":
    main()


__all__ = [
    "TkSetupWizard",
    "interpret_boot_status",
    "setup_info_text",
    "setup_summary_text",
    "setup_wizard_class",
    "validate_launch_password_fields",
    "CONNECTOR_STORE_RELATIVE",
    "FACTORY_RESET_SCRIPT_RELATIVE",
    "FILE_ATTRIBUTE_HIDDEN",
    "HERO_IMAGE_RELATIVE",
    "LAUNCH_LOCK_RELATIVE",
    "LAUNCH_PASSWORD_ITERATIONS",
    "LAUNCH_PASSWORD_MIN_LENGTH",
    "LAUNCH_HUD_COMMAND",
    "LAUNCH_LM_STUDIO_COMMAND",
    "LM_STUDIO_LAUNCHER_RELATIVE",
    "LINUX_RUNTIME_SCRIPT_RELATIVE",
    "LINUX_STARTUP_COMMAND",
    "LOCAL_BASE_URL",
    "MIND_BACKEND_RELATIVE",
    "PRODUCT_INFO_RELATIVE",
    "BootProgress",
    "HUD_SCRIPT_RELATIVE",
    "OMEGA_BROKER_AUTHORIZED_ENV",
    "OMEGA_HUD_OPEN_BROWSER_ENV",
    "SetupWizard",
    "STARTUP_STATE_RELATIVE",
    "discover_cloud_models_for_setup",
    "boot_progress",
    "launch_lm_studio",
    "launch_hud",
    "update_lm_studio",
    "launch_linux_runtime",
    "linux_runtime_script_path",
    "launch_omega",
    "lm_studio_launcher_path",
    "load_help_guide",
    "load_privacy_policy",
    "load_launch_lock",
    "load_product_info",
    "load_startup_state",
    "hide_portable_runtime",
    "factory_reset",
    "clear_launch_password",
    "open_external_url",
    "main",
    "product_root",
    "require_lm_studio_launcher",
    "require_linux_runtime_script",
    "run_setup_wizard",
    "save_startup_configuration",
    "save_launch_password",
    "show_launcher_error",
    "ssd_root",
    "startup_state_path",
    "launch_lock_path",
    "verify_launch_password",
]
