"""User-controlled permission modes for the live chat agent surface.

The mode is an operator preference, not a replacement for skill contracts,
workspace jail checks, or signed plugin authority. It is the final user-facing
switch for ordinary chat tools and chat-originated skill dispatches.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.paths import GUARDIAN_STATE_ROOT
from runtime.atomic_store import atomic_write_bytes

CHAT_PERMISSION_MODES = ("read_only", "allow_edits", "always_allow")
# Fresh installs start at the narrowest chat-tool authority. The operator can change
# this through the visible /allow-edits and /always-allow controls.
DEFAULT_CHAT_PERMISSION_MODE = "read_only"
CHAT_PERMISSION_PATH = GUARDIAN_STATE_ROOT / "live-chat" / "permission-mode.json"


class ChatPermissionModeUnreadable(RuntimeError):
    """Persisted chat permission state is present but cannot be trusted."""

_MODE_LABELS = {
    "read_only": "read-only",
    "allow_edits": "allow-edits",
    "always_allow": "always-allow",
}
_MODE_ALIASES = {
    "read-only": "read_only",
    "readonly": "read_only",
    "read_only": "read_only",
    "ro": "read_only",
    "allow-edits": "allow_edits",
    "allow_edits": "allow_edits",
    "edits": "allow_edits",
    "edit": "allow_edits",
    "always-allow": "always_allow",
    "always_allow": "always_allow",
    "always": "always_allow",
}

_READ_ONLY_TOOLS = frozenset(
    {"list_directory", "read_files", "search_repo", "inspect_git_diff", "web_search", "web_fetch", "browser_control"}
)
_ALWAYS_ALLOW_TOOLS = frozenset({"restore_project_from_memory"})
_READ_ONLY_COMMAND_RE = re.compile(
    r"\s*git\s+(?:(?:--no-pager)\s+)?(?:status(?:\s+--short)?|"
    r"diff(?:\s+--no-ext-diff)?(?:\s+--)?|log(?:\s+--oneline)?|"
    r"show(?:\s+--stat)?)\s*\Z",
    re.IGNORECASE,
)


def normalize_chat_permission_mode(value: Any) -> str:
    """Return one canonical mode or raise for an unknown operator value."""
    raw = str(value or "").strip().lower().replace(" ", "-")
    mode = _MODE_ALIASES.get(raw, raw if raw in CHAT_PERMISSION_MODES else "")
    if mode not in CHAT_PERMISSION_MODES:
        raise ValueError(f"unknown chat permission mode: {value!r}")
    return mode


def chat_permission_label(mode: str) -> str:
    return _MODE_LABELS[normalize_chat_permission_mode(mode)]


def load_chat_permission_mode() -> str:
    try:
        text = CHAT_PERMISSION_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_CHAT_PERMISSION_MODE
    except (OSError, UnicodeError):
        raise ChatPermissionModeUnreadable("chat_permission_mode_unreadable") from None
    try:
        payload = json.loads(text)
        if type(payload) is not dict:
            raise ValueError("permission state must be an object")
        return normalize_chat_permission_mode(payload.get("mode"))
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeError):
        raise ChatPermissionModeUnreadable("chat_permission_mode_unreadable") from None


def set_chat_permission_mode(mode: Any) -> dict[str, Any]:
    canonical = normalize_chat_permission_mode(mode)
    payload = {"mode": canonical, "label": _MODE_LABELS[canonical]}
    atomic_write_bytes(
        CHAT_PERMISSION_PATH,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    return payload


def chat_permission_status(mode: str | None = None) -> dict[str, Any]:
    canonical = normalize_chat_permission_mode(mode or load_chat_permission_mode())
    return {
        "mode": canonical,
        "label": _MODE_LABELS[canonical],
        "commands": [
            "/read-only",
            "/allow-edits",
            "/always-allow",
        ],
    }


def _mode_message(status: dict[str, Any]) -> str:
    label = status["label"]
    if status["mode"] == "read_only":
        detail = "The agent may read, search, inspect, and fetch, but cannot edit files, run commands, or change jobs. Plugin operations use their Settings scopes and signed authority independently."
    elif status["mode"] == "allow_edits":
        detail = "The agent may edit the selected workspace; destructive commands and command-shaped mutations still require /always-allow. Plugin operations use their Settings scopes and execution policy."
    else:
        detail = "The agent may use the declared workspace tools without this chat-mode gate; hard safety denylists, jail boundaries, skill contracts, and plugin authority still apply."
    return f"Chat permission mode: {label}. {detail}"


def handle_chat_permission_command(text: str) -> dict[str, Any] | None:
    """Handle a permission slash command, or return ``None`` for normal chat."""
    parts = str(text or "").strip().split()
    if not parts:
        return None
    command = parts[0].lower()
    direct = _MODE_ALIASES.get(command.lstrip("/"))
    if direct is not None and len(parts) == 1:
        return {"command": command, "changed": True, "status": chat_permission_status(set_chat_permission_mode(direct)["mode"]), "message": _mode_message(chat_permission_status(direct))}
    if command not in {"/mode", "/permission", "/permissions"}:
        return None
    if len(parts) == 1 or parts[1].lower() in {"status", "show"}:
        status = chat_permission_status()
        return {"command": command, "changed": False, "status": status, "message": _mode_message(status)}
    if len(parts) == 2:
        try:
            mode = normalize_chat_permission_mode(parts[1])
        except ValueError:
            return {
                "command": command,
                "changed": False,
                "status": chat_permission_status(),
                "message": "Unknown mode. Use /read-only, /allow-edits, or /always-allow.",
            }
        status = chat_permission_status(set_chat_permission_mode(mode)["mode"])
        return {"command": command, "changed": True, "status": status, "message": _mode_message(status)}
    return {
        "command": command,
        "changed": False,
        "status": chat_permission_status(),
        "message": "Use /mode, /read-only, /allow-edits, or /always-allow.",
    }


def command_requires_always_allow(name: str, args: dict[str, Any]) -> bool:
    """Return whether a chat command is mutation-shaped rather than read-only."""
    if name == "run_tests":
        # The dedicated test handler accepts only a closed runner grammar and
        # rewrites direct runners to the portable project Python.
        try:
            from auto_loop.execute.brain_skill_executor import _TEST_RUNNER_RE

            command = str(args.get("command") or args.get("cmd") or "")
            return not _TEST_RUNNER_RE.fullmatch(command) or bool(
                re.search(r"[;&|<>]", command)
            )
        except (ImportError, AttributeError):
            return True
    if name == "run_command":
        command = str(args.get("command") or args.get("cmd") or "")
        if _READ_ONLY_COMMAND_RE.fullmatch(command):
            return False
        # The shell has no filesystem sandbox. Treat every command outside the
        # tiny, closed inspection set as mutation-capable.
        return True
    return False


def operator_tool_permission_denial(mode: str, name: str, args: dict[str, Any]) -> str:
    """Return a user-facing denial reason, or an empty string when permitted."""
    canonical = normalize_chat_permission_mode(mode)
    if canonical == "always_allow":
        return ""
    if name == "browser_control":
        # Existing-browser control is its own user-attached connection scope;
        # chat workspace edit mode must not become a second approval gate.
        return ""
    if canonical == "read_only" and name not in _READ_ONLY_TOOLS:
        return f"{chat_permission_label(canonical)} mode blocks {name}; use /allow-edits to permit workspace edits"
    if canonical == "allow_edits" and name in _ALWAYS_ALLOW_TOOLS:
        return f"{name} can replace workspace files; use /always-allow"
    if canonical == "allow_edits" and command_requires_always_allow(name, args):
        return f"{name} contains a mutation-shaped command; use /always-allow for this command"
    return ""


def skill_permission_denial(mode: str, safety_class: str) -> str:
    """Return the chat-mode denial for a skill safety class, if any."""
    canonical = normalize_chat_permission_mode(mode)
    safety = str(safety_class or "").strip()
    if safety not in {
        "read_only",
        "network_read",
        "write_safe",
        "command_safe",
        "destructive",
        "desktop_control",
        "self_modification",
    }:
        return "unknown skill safety class is blocked"
    if canonical == "read_only" and safety not in {"read_only", "network_read"}:
        return f"read-only mode blocks {safety or 'write-capable'} skill dispatch; use /allow-edits"
    if canonical == "allow_edits" and safety in {"command_safe", "destructive", "desktop_control"}:
        return f"{safety} skill dispatch requires /always-allow"
    return ""


__all__ = [
    "CHAT_PERMISSION_MODES",
    "DEFAULT_CHAT_PERMISSION_MODE",
    "CHAT_PERMISSION_PATH",
    "ChatPermissionModeUnreadable",
    "chat_permission_status",
    "command_requires_always_allow",
    "handle_chat_permission_command",
    "load_chat_permission_mode",
    "normalize_chat_permission_mode",
    "operator_tool_permission_denial",
    "set_chat_permission_mode",
    "skill_permission_denial",
]
