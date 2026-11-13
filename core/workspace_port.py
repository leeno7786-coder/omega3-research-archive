"""Port a finished build out of the agent workspace into the OPERATOR'S directory, then
clear the workspace so the next build starts EMPTY.

Operator design (2026-07-03): the jailed workspace is the agent's scratchpad, not a home.
A dirty sandbox was the #1 turn-budget sink — a fresh build order landed in a workspace
holding previous projects, the model burned turns mapping/testing work it never did, and
the battery collected stale tests as phantom red checks. So every DETERMINISTIC build
ending records a port here, and the cascade tail executes it exactly once:

* verified-complete OPERATOR build   -> <completed_root>/            (their finished work)
* verified-complete AUTONOMOUS build -> <completed_root>/_practice/  (never lost, out of the way)
* stall/cap-STOPPED build            -> <completed_root>/_incomplete/ (inspectable, workspace freed)

The completed root is the operator's property: outside the repo, outside the jail (the
executor's allowedPaths is the workspace only), untouched by day-zero. Default is a repo
sibling next to the workspace (<omega-workspace> -> <omega-completed>); env
OMEGA_OPERATOR_COMPLETED overrides.

Merge semantics serve the update flow (operator: recall a completed project from memory,
rebuild, re-port): mirror-merge onto the SAME folder — overwrite changed files, add new
ones, never delete archive files the rebuild didn't touch.

Two-phase by construction (audit 2026-07-03): completion sites can only RECORD — the
dev-lobe persist, rollback capture and consolidation all read workspace files AFTER the
single-cycle completion site in the same fold. Execution happens once at the tail of
`_start_meta_b_global_b`, provably after every disk reader. `take_port` is episode-scoped:
a stale record from an episode whose tail never ran is DROPPED, never executed against a
later episode's in-flight files.

The clear NEVER touches:
* .git         — the build ledger is a per-dispatch git-status diff and the bridge only
                 re-inits at startup; wiping it mid-life makes every later build report
                 zero touched files (deliverables_exist red forever).
* .omega_data  — retired legacy state; never ported into a completed build.
* skills/      — the operator's drop-in skill catalog (read-only to the build path).

Everything is failure-soft and per-entry copy-THEN-delete (Windows: a lingering child
process can hold handles) — an entry that fails to copy is never deleted.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.core.workspace_port")

# Survive in place, never archived (see module docstring for the why of each).
EXCLUDE_FROM_PORT = frozenset({".git", ".omega_data", "skills"})
# Deleted during the clear but never archived — build byproducts, not work product.
# .venv/venv (2026-07-10): project-local virtual environments are per-build
# scaffolding, often hundreds of MB — never archive them into omega-completed.
JUNK_ENTRIES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv"}
)

# Module-local pending-port slot (one build ending at a time) — WRITE-THROUGH
# durable since 2026-07-08: the record was process-local and the executing tail
# only runs in the cascade fold, so a mind stopped right after a completion (or
# a pill-driven / paused-era build with no fold behind it) stranded the record
# and the workspace never cleared. The disk copy lets boot reconcile it.
_pending: Optional[dict[str, Any]] = None
_PORT_STATE_PATH = OMEGA_ROOT / "guardian" / "state" / "workspace_port_pending.json"


def _persist_pending() -> None:
    try:
        if _pending is None:
            if _PORT_STATE_PATH.exists():
                _PORT_STATE_PATH.unlink()
            return
        _PORT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PORT_STATE_PATH.write_text(__import__("json").dumps(_pending, indent=1), encoding="utf-8")
    except OSError:
        log.warning("workspace port record persist failed (non-fatal)", exc_info=True)


def _reset_for_tests() -> None:
    global _pending
    _pending = None
    try:
        if _PORT_STATE_PATH.exists():
            _PORT_STATE_PATH.unlink()
    except OSError:
        pass


def operator_completed_root() -> Path:
    env = os.environ.get("OMEGA_OPERATOR_COMPLETED")
    if env:
        return Path(env)
    return OMEGA_ROOT.parent / "omega-completed"


def destination_for(kind: str, origin_opened_by: str) -> Path:
    root = operator_completed_root()
    if str(kind) == "stopped":
        return root / "_incomplete"
    if not str(origin_opened_by or "").startswith("operator"):
        return root / "_practice"
    return root


def record_port(
    kind: str,
    *,
    episode_id: str,
    objective: str = "",
    origin_opened_by: str = "",
    original_request: str = "",
) -> Path:
    """Record that THIS episode's build ended (kind: completed|stopped). Returns the
    destination so the operator notice can name where the work is going."""
    global _pending
    dest = destination_for(kind, origin_opened_by)
    _pending = {
        "kind": str(kind),
        "episode_id": str(episode_id or ""),
        "objective": str(objective or "")[:400],
        "origin_opened_by": str(origin_opened_by or ""),
        "original_request": str(original_request or "")[:2000],
    }
    _persist_pending()
    return dest


def take_port(episode_id: str) -> Optional[dict[str, Any]]:
    """Consume the pending record IF it belongs to this episode. A mismatched record is
    dropped (its tail never ran — executing it now would clear a NEWER build's files)."""
    global _pending
    record, _pending = _pending, None
    _persist_pending()
    if record is None:
        return None
    if str(record.get("episode_id") or "") != str(episode_id or ""):
        log.warning(
            "dropping stale workspace-port record from episode %s (current %s)",
            record.get("episode_id"),
            episode_id,
        )
        return None
    return record


def reconcile_stale_port(
    *,
    workspace_root: Optional[Path] = None,
    completed_root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Execute a port record stranded by a previous session (boot-time repair).

    The in-session episode guard exists so a stale record never clears a
    NEWER build's in-flight files. At BOOT no build is in flight, so a
    persisted record from a dead session is safe — and owed: the build
    completed, the operator's work is still sitting in the workspace.
    Consumes the record either way; failure-soft (returns None)."""
    global _pending
    try:
        if not _PORT_STATE_PATH.exists():
            return None
        import json as _json

        record = _json.loads(_PORT_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            record = None
    except (OSError, ValueError):
        log.warning("stale port record unreadable — dropping", exc_info=True)
        record = None
    _pending = None
    _persist_pending()
    if record is None:
        return None
    log.info(
        "reconciling stranded workspace port from episode %s (kind=%s)",
        record.get("episode_id"),
        record.get("kind"),
    )
    try:
        return execute_port(record, workspace_root=workspace_root, completed_root=completed_root)
    except Exception:
        log.exception("stranded workspace port reconcile failed (non-fatal)")
        return None


def sweep_workspace_for_fresh_build(
    *,
    reason: str = "",
    workspace_root: Optional[Path] = None,
    completed_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Port workspace LEFTOVERS to _incomplete/ before a FRESH objective starts.

    Operator finding 2026-07-10: a build that never reached a deterministic
    ending (pressure quenched, reboot mid-build, operator kill) never executed
    its port — the next build then started inside the previous project's files
    and debris, the exact turn-budget sink the port exists to end ("they pile
    up" — the executor burns rounds mapping work it never did, and the battery
    collects stale tests as phantom red checks). Called at the practice-mint
    seams, where by definition NO build is in flight (mint only runs when the
    pending stash is empty), so the two-phase record/tail discipline does not
    apply. Move-only via execute_port: files land in _incomplete/, nothing is
    deleted unarchived. No-op on an already-clean workspace."""
    try:
        ws = workspace_root
        if ws is None:
            from core.hud_path_guard import _operator_workspace_root

            ws = _operator_workspace_root()
        ws = Path(ws) if ws else None
        if ws is None or not ws.is_dir():
            return {"ported": False, "skipped": "no_workspace"}
        leftovers = [e.name for e in ws.iterdir() if e.name not in EXCLUDE_FROM_PORT]
        if not leftovers:
            return {"ported": False, "skipped": "clean"}
        record = {
            "kind": "stopped",  # -> _incomplete/: inspectable, workspace freed
            "episode_id": "workspace-sweep",
            "objective": f"pre-build sweep: {reason}"[:400],
            "origin_opened_by": "",
        }
        result = execute_port(record, workspace_root=ws, completed_root=completed_root)
        log.info(
            "workspace swept before fresh build (%d entries -> %s): %s",
            len(result.get("moved") or []),
            result.get("dest"),
            reason[:80],
        )
        return result
    except Exception:
        log.exception("workspace sweep failed (non-fatal)")
        return {"ported": False, "skipped": "error"}


def _delete_entry(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    else:
        entry.unlink()


def execute_port(
    record: dict[str, Any],
    *,
    workspace_root: Optional[Path] = None,
    completed_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Mirror-merge the workspace into the operator directory, then clear it.

    Per-entry copy-then-delete: a failed copy leaves its entry in the workspace (the next
    completed build re-ports it). Junk entries are deleted without archiving; nested junk
    is filtered out of copies. Failure-soft end to end."""
    result: dict[str, Any] = {
        "ported": False,
        "dest": "",
        "moved": [],
        "failed": [],
        "junked": [],
        "cleared_rollback": 0,
    }
    try:
        if workspace_root is None:
            from core.hud_path_guard import _operator_workspace_root

            workspace_root = _operator_workspace_root()
        ws = Path(workspace_root) if workspace_root else None
        if ws is None or not ws.is_dir():
            log.warning("workspace port skipped — no workspace root (%s)", workspace_root)
            return result

        if completed_root is None:
            dest = destination_for(
                str(record.get("kind") or "completed"),
                str(record.get("origin_opened_by") or ""),
            )
        else:
            root = Path(completed_root)
            kind = str(record.get("kind") or "completed")
            origin = str(record.get("origin_opened_by") or "")
            dest = (
                root / "_incomplete"
                if kind == "stopped"
                else (root if origin.startswith("operator") else root / "_practice")
            )
        dest.mkdir(parents=True, exist_ok=True)
        result["dest"] = str(dest)
        ignore_junk = shutil.ignore_patterns(*JUNK_ENTRIES)

        for entry in sorted(ws.iterdir(), key=lambda p: p.name):
            name = entry.name
            if name in EXCLUDE_FROM_PORT:
                continue
            if name in JUNK_ENTRIES:
                try:
                    _delete_entry(entry)
                    result["junked"].append(name)
                except OSError as exc:
                    log.warning("workspace junk delete failed for %s: %s", entry, exc)
                continue
            try:
                target = dest / name
                if entry.is_dir() and not entry.is_symlink():
                    shutil.copytree(entry, target, dirs_exist_ok=True, ignore=ignore_junk)
                else:
                    shutil.copy2(entry, target)
            except OSError as exc:
                # copy failed -> the entry STAYS in the workspace (never delete unarchived work)
                log.warning("workspace port copy failed for %s: %s", entry, exc)
                result["failed"].append(name)
                continue
            try:
                _delete_entry(entry)
                result["moved"].append(name)
            except OSError as exc:
                # copied but not cleared — safe (archive has it; next port re-copies)
                log.warning("workspace clear failed for %s: %s", entry, exc)
                result["failed"].append(name)

        result["ported"] = True

        # The workspace is now (project-)empty: every workspace-scoped rollback row is
        # stale — restoring one would resurrect debris into the fresh workspace, the
        # exact problem this port exists to end (operator 2026-07-03).
        try:
            from core.dev_rollback_capture import clear_workspace_rollback_records

            result["cleared_rollback"] = clear_workspace_rollback_records(workspace_root=ws)
        except Exception:
            log.exception("rollback record retirement failed (non-fatal)")

        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "core",
                "workspace_port",
                status="ok",
                ok=True,
                summary=(
                    f"{record.get('kind')} build ported to {dest} "
                    f"({len(result['moved'])} moved, {len(result['failed'])} failed)"
                ),
                kind=str(record.get("kind") or ""),
                dest=str(dest),
                moved=len(result["moved"]),
                failed=len(result["failed"]),
                cleared_rollback=int(result["cleared_rollback"]),
                episode_id=str(record.get("episode_id") or ""),
            )
        except Exception:
            pass
    except Exception:
        log.exception("workspace port failed (non-fatal)")
    return result
