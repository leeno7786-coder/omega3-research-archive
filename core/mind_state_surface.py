"""Canonical definition of Omega's live "mind state".

This is the production source of truth for *what constitutes the mind* on disk:
the gitignored runtime surface (memory lobes, guardian/deepsleep/pressure state)
that a reversible code-executor (APPLY) could mutate and that therefore must be
checkpointed before it runs. git cannot roll any of this back — it is all
gitignored — so :mod:`guardian.mind_checkpoint` is the only reversibility path.

`day_zero_reset` (a dev-only reset tool, not part of the shipped system) is NOT
the authority for this surface; this module is. Anything that needs to know
"what is mind state" imports from here.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List

from core.paths import OMEGA_ROOT

# The 12 cognitive memory lobes (coordinate-addressed JSON, gitignored runtime state).
_MEMORY_LOBES = (
    "000_identity",
    "100_personas",
    "200_knowledge",
    "300_reasoning",
    "400_situational",
    "500_tools",
    "600_journal",
    "700_vision",
    "800_coding",
    "900_temporal",
    "1000_workflows",
    "1100_dev",
)

# Directories (relative to OMEGA_ROOT) whose contents are live mind state. Code
# packages under memory/ (engine/, enrichment/, gates/) are git-tracked and NOT
# listed here — they are recoverable from git, so they are not mind state.
MIND_STATE_DIRS = (
    *(f"memory/{lobe}" for lobe in _MEMORY_LOBES),
    "memory/indexes",
    "guardian/state",
    "deepsleep_loop/state",
    "pressure_loop/state",
)

# Path globs (posix, relative to OMEGA_ROOT) excluded from a checkpoint:
#  - append-only telemetry/journals: restoring them to a prior point would
#    rewrite history and re-inject stale episodes; they are logs, not load-bearing
#    rollback state.
#  - transient atomic-write staging.
#  - factory scaffold shipped in the repo (not user state).
MIND_STATE_EXCLUDE_GLOBS = (
    "*.jsonl",
    "*.log",
    "memory/tmp_writes/*",
    "memory/indexes/label_map.json",
    "memory/indexes/README.md",
    # APPLY operator-approval ledger (apply_lock staged-gate). This is operator
    # CONTROL state, not mind state: a mind restore after a failed APPLY must
    # NEVER roll back / resurrect a spent approval token or reset graduation
    # progress. Excluding it decouples the safety gate from reversibility — the
    # token ledger persists across a mind restore by construction.
    "guardian/state/apply_authorization.json",
    # Operator-facing specialist progress is a delivery rail, not canonical
    # mind state. Restoring a mind checkpoint must not rewind chat updates.
    "guardian/state/operator_chat_lane_updates.json",
    # The browser agent's Edge profile (browser_agent.bootstrap keeps it under
    # guardian/state/browser). It is browser session and cache data, not mind
    # state, and its extension paths are deep enough that copying them into a
    # checkpoint passes Windows' 260-character limit on any machine without
    # long-path support -- which failed every checkpoint and so blocked every
    # write-type skill. It is also hundreds of MB copied before each APPLY.
    "guardian/state/browser/*",
)


def is_excluded(rel_path: str) -> bool:
    """True if a checkpoint should skip this OMEGA_ROOT-relative path."""
    rel = str(rel_path).replace("\\", "/")
    return any(fnmatch.fnmatch(rel, pattern) for pattern in MIND_STATE_EXCLUDE_GLOBS)


def mind_state_files(omega_root: Path | str = OMEGA_ROOT) -> List[Path]:
    """Every in-scope, non-excluded mind-state file under ``omega_root``.

    Absent directories (a fresh clone / first boot has none) are simply skipped,
    so the result is an honest reflection of what exists right now.
    """
    root = Path(omega_root)
    files: List[Path] = []
    for rel_dir in MIND_STATE_DIRS:
        base = root / rel_dir
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if is_excluded(path.relative_to(root).as_posix()):
                continue
            files.append(path)
    return sorted(files)
