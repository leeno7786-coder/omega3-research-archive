"""§8.8 — skill-mediated execution self-modification denylist.

A qwen skill dispatch may write real source inside the operator workspace — that is the
point of skill-mediated execution. But it must NEVER modify the machinery that keeps it
safe, nor its own mind state:

  * the staged-approval **gate** (``guardian/state/apply_authorization.json``) — a
    run that writes the gate could self-grant tokens or graduation, and the gate
    is EXCLUDED from the mind checkpoint, so a mind restore cannot undo it;
     * the **safety code** — the guard, the AUTO dispatcher, the live-chat executor,
       the mind-checkpoint rollback, and the mind-state surface definition;
  * any **mind-state** dir (the gitignored cognitive surface — memory lobes /
    indexes, guardian/deepsleep/pressure state).

This module is the classifier. ``auto_loop.execute.skill_execution_guards`` and the
skill foldback path inspect ledgers through :func:`self_mod_violations`; a non-empty
result turns the dispatch into a hard, fully-reverted failure.

Classification fails SAFE: an unparseable or out-of-jail path counts as a
violation. A false positive merely refuses one APPLY; a false negative defeats the
whole guard — so ambiguity blocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from core.mind_state_surface import MIND_STATE_DIRS
from core.paths import OMEGA_ROOT

# Safety-critical CODE files (OMEGA_ROOT-relative, posix) a skill dispatch must not edit.
# These are git-tracked (so a failed APPLY's git rollback recovers them), but
# letting a run touch them at all is a self-modification attempt and is blocked
# outright — the executor improving its OWN dispatch/guard is never legitimate
# autonomous work. Includes this module, so the denylist cannot disarm itself.
PROTECTED_CODE_FILES: frozenset[str] = frozenset(
    {
        "auto_loop/execute/skill_execution_guards.py",
        "auto_loop/execute/run.py",
        "auto_loop/execute/brain_skill_executor.py",
        "auto_loop/execute/self_mod_denylist.py",
        "guardian/mind_checkpoint.py",
        "core/mind_state_surface.py",
        # The §11 dev-pill GPU hotswap the guard itself invokes (prepare/release_dev_pill_
        # runtime) — editing it could break the release path and starve cognition of the
        # GPU. It is part of the guard's own call surface.
        "runtime/model_control.py",
        # The seam-9 capability machinery: SELECTION + STEP-4 reinforcement (catalog) and
        # the capability INVENTORY (qwen_skills). An APPLY rigging how its own capabilities
        # are chosen, credited, or sourced is self-dealing, never legitimate work.
        "memory/capability_catalog.py",
        "runtime/qwen_skills.py",
    }
)

# Mind-state dirs (posix, OMEGA_ROOT-relative) — reused, single source of truth.
_PROTECTED_DIRS: tuple[str, ...] = tuple(Path(d).as_posix() for d in MIND_STATE_DIRS)

_LEDGER_FILE_KEYS = (
    "touched_files",
    "kept",
    "rolled_back",
    "files",
    "modified",
    "created",
    "deleted",
)
_MALFORMED_LEDGER_FILE_ENTRY = "<malformed-ledger-file-entry>"


def is_protected_path(
    path: Any,
    *,
    omega_root: Path | str = OMEGA_ROOT,
    workspace_root: Path | str | None = None,
) -> bool:
    """True if a skill dispatch writing ``path`` is a self-modification (must be blocked).

    A write INSIDE the operator ``workspace_root`` is the POINT of skill-mediated execution —
    legitimate, never self-mod. The workspace is DISJOINT from OMEGA_ROOT (sandbox invariant), so
    it must be passed: without it, an absolute workspace path (e.g. <omega-workspace>/foo.py)
    resolves OUTSIDE OMEGA_ROOT and the fail-safe flags EVERY real build as a violation.

    A relative path is joined to the workspace (qwen writes relative to its jail/cwd), not the
    Omega tree. Outside the workspace, the path is matched against the protected code files +
    mind-state dirs. Fails SAFE: an empty/unparseable path, or one outside BOTH the workspace and
    OMEGA_ROOT (an escape), returns True.
    """
    raw = str(path).strip().replace("\\", "/")
    if not raw:
        return True  # empty / whitespace — malformed, fail safe
    try:
        root = Path(omega_root).resolve()
        wroot = Path(workspace_root).resolve() if workspace_root else None
        p = Path(raw)
        if not p.is_absolute():
            # qwen writes RELATIVE TO ITS WORKSPACE (cwd/jail), not the Omega tree.
            p = (wroot or root) / p
        p = p.resolve()
    except (ValueError, OSError):
        return True  # unparseable (null bytes, bad chars) — fail safe
    if wroot is not None:
        try:
            p.relative_to(wroot)
            return False  # inside the operator workspace — legitimate skill work, never self-mod
        except ValueError:
            pass  # not in the workspace — fall through to the Omega-tree / escape checks
    try:
        rel = p.relative_to(root).as_posix()
    except ValueError:
        return (
            True  # outside BOTH the workspace and OMEGA_ROOT — a skill write must never land here
        )
    if rel in PROTECTED_CODE_FILES:
        return True
    return any(rel == d or rel.startswith(d + "/") for d in _PROTECTED_DIRS)


def _touched_files(outcome: Any) -> List[str]:
    """Every file path a skill outcome's ledger reports touching.
    reports changes via the ledger, not a raw diff).

    File-list entries fail safe: bridge-shaped objects yield their ``path``;
    malformed entries become a sentinel violation. Missing/non-dict ledgers still
    produce no violation because there is no claimed write evidence to classify.
    """
    if not isinstance(outcome, dict):
        return []
    ledger = outcome.get("ledger")
    if not isinstance(ledger, dict):
        return []
    files: List[str] = []
    for key in _LEDGER_FILE_KEYS:
        value = ledger.get(key)
        if isinstance(value, (list, tuple)):
            for entry in value:
                if isinstance(entry, (str, Path)):
                    files.append(str(entry))
                elif isinstance(entry, dict):
                    path = entry.get("path")
                    if isinstance(path, (str, Path)):
                        files.append(str(path))
                    else:
                        files.append(_MALFORMED_LEDGER_FILE_ENTRY)
                else:
                    files.append(_MALFORMED_LEDGER_FILE_ENTRY)
    return files


def self_mod_violations(
    outcome: Any,
    *,
    omega_root: Path | str = OMEGA_ROOT,
    workspace_root: Path | str | None = None,
) -> List[str]:
    """The protected paths a skill outcome touched, as reported in its ledger.

    Empty list = clean. A NON-empty list means the run attempted self-modification:
    the caller must block + fully revert the dispatch.
    malformed file-list entries return a sentinel so ambiguity blocks.

    ``workspace_root`` MUST be passed for live skill outcomes: writes inside the operator
    workspace are legitimate, and without it absolute workspace paths fail safe and block
    every real build. Omitting it preserves the strict OMEGA_ROOT-only classification (tests).
    """
    return [
        f
        for f in _touched_files(outcome)
        if f == _MALFORMED_LEDGER_FILE_ENTRY
        or is_protected_path(f, omega_root=omega_root, workspace_root=workspace_root)
    ]
