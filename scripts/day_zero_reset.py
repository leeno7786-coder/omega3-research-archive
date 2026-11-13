r"""
Day-Zero / Factory-Ship Reset — scorched-earth wipe + canonical identity seeds.

Prepares a copy for a new operator: **no testing or prior-run state may
survive**. Wipes every runtime persistence surface, then re-seeds the full
founding constitution set into 000_identity/.

Factory reset removes:
    - Sibling portable runtime operator state on the SSD, beside the product tree:
      LM Studio conversations, local identity, API key, OAuth credentials, MCP
      wiring, attachments, retrieval/RAG caches, download history and UI state
      (shipped app, engines under extensions/ and bundled model weights preserved),
      plus the Linux runtime log Omega's own start command writes.
    - Entire guardian/state/ tree (HUD uploads, faces, personas, queues,
      credentials, save_state, strength_index, order_queue, TLS, staging, …)
      INCLUDING guardian/state/mcp/ (OAuth vault, connections, permissions,
      grants, registries, immutable package artifacts, and action ledger)
      INCLUDING guardian/state/plugins/ (installed package registry and journal)
      with no operator-state exceptions
    - guardian/audit/ (agent prose audit and other audit residue)
    - guardian/ledger/ (HMAC chain, key.bin, .activated — fresh instance)
    - guardian/checkpoints/ (mind-state restore points — else a wiped mind
      could be resurrected)
    - All memory lobe files (any extension) + runtime indexes (coord_index;
      label_map.json preserved — shipped classifier scaffold, not user data)
    - The compiled memory graph + taxonomy store (memory/taxonomy_store/:
      graph.sqlite, graph_snapshot.json, compile_state, dedup/lexical indexes,
      audit) — the graph is a DISPOSABLE PROJECTION rebuilt from canonical memory
    - deepsleep_loop/state/, pressure_loop/state/ (incl. the episode journal,
      resolved via the policy accessor so a config-redirected ledger is caught),
      knowledge_extractor/state/, guardian/logs/, guardian/telemetry data,
      legacy .omega_data/

The wipe is fail-closed: a locked file or unexpected state survivor aborts the
reset. Preflight refuses to run while boot.lock exists or the HUD listener is
reachable — stop Omega first.

Preserved (not operator/runtime state):
    - config/, schema/, source code
    - memory/indexes/label_map.json (static Dewey classifier map)
    - .agents/ (bus + cursors — dev coordination infrastructure, not brain
      state; wiping the bus while cursors survive desyncs readers past the
      new bus end -> silent message loss. Operator directive 2026-06-12.)
    - founding constitution definitions in memory/identity/seeds.py. The
      generated constitution records are recreated from those definitions.

Seeds are written via atomic_write (schema-valid + indexed). Idempotent.

    .\.venv-brain\Scripts\python.exe .\scripts\day_zero_reset.py
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.paths import (
    GUARDIAN_LOGS_ROOT,
    GUARDIAN_STATE_ROOT,
    LEDGER_ROOT,
    OMEGA_ROOT,
    STAGING_ROOT,
    ensure_child_path,
)


class DayZeroResetError(RuntimeError):
    """Day-zero could not complete a clean factory wipe."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

# 2026-05-06 lobe restructure: 1 personal → personas, 4 context →
# situational, plus new 8 (coding) and 10 (workflows). Old paths kept
# in the wipe set so any stale data from the prior layout also clears.
LOBE_DIRS = [
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
]

# Lobes RETIRED from the memory system: their directory must NOT be (re)created by the
# tree normalization, even if memory.dewey_map.LOBE_MAP still lists them. Lobe 5 (tools)
# was removed by the operator — tools live in the registry (config/tools.yaml), not as a
# memory lobe. A stale 500_tools dir is dropped and never rebuilt. Forward-compatible: if
# the lobe is later removed from LOBE_MAP, this skip simply becomes a no-op.
RETIRED_LOBES: frozenset[int] = frozenset({5})

# Audit + telemetry JSONL files to wipe
RUNTIME_JSONL = [
    "cycle_events.jsonl",
    "cycle_metrics.jsonl",
    "failure_audit.jsonl",
    "model_router_audit.jsonl",
    "lifecycle_audit.jsonl",
    "brain_telemetry.jsonl",
    "telemetry.jsonl",
    "deep_sleep_audit.jsonl",
    "omega_audit.jsonl",
    "goals.jsonl",
    "guardian_health.jsonl",  # GuardianNode non-healthy verdicts
    "guardian_interventions.jsonl",  # promotion-deny records
]

# Factory-ship preserve list (everything else under guardian/ + memory/ lobes
# is wiped). Model weights and shipped classifier scaffold are not user state.
FACTORY_PRESERVE = (
    "config/",
    "schema/",
    "memory/indexes/label_map.json",
)

log = logging.getLogger("omega.scripts.day_zero_reset")

# Per `project_guardian_sole_writer_doctrine` (2026-05-08): writes
# now land in `guardian/state/` (load-bearing) or `guardian/logs/`
# (audit). Day-zero wipes both surfaces so a portable product starts
# unattached to the development machine or a prior operator.
GUARDIAN_STATE_DIR = OMEGA_ROOT / "guardian" / "state"
GUARDIAN_LOGS_DIR = OMEGA_ROOT / "guardian" / "logs"

# A factory reset has no credential exception. All provider keys, connector
# vaults, plugin grants, and setup state are operator-owned data and are removed.
GUARDIAN_STATE_PRESERVE: frozenset[str] = frozenset()

_DEFAULT_HUD_PORT = 13310


# ---------------------------------------------------------------------
# Preflight + verify
# ---------------------------------------------------------------------


def hud_listener_reachable(host: str = "127.0.0.1", port: Optional[int] = None) -> bool:
    """Return True when something accepts TCP on the HUD listen address."""
    if port is None:
        try:
            port = int(os.environ.get("OMEGA_HUD_PORT") or _DEFAULT_HUD_PORT)
        except (TypeError, ValueError):
            port = _DEFAULT_HUD_PORT
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def preflight_substrate_stopped() -> None:
    """Refuse to wipe while Omega still looks live (avoids locked half-wipes)."""
    lock = GUARDIAN_STATE_ROOT / "boot.lock"
    if lock.is_file():
        raise DayZeroResetError(
            f"refusing day-zero: {lock} is present — stop Omega first, then re-run"
        )
    if hud_listener_reachable():
        try:
            port = int(os.environ.get("OMEGA_HUD_PORT") or _DEFAULT_HUD_PORT)
        except (TypeError, ValueError):
            port = _DEFAULT_HUD_PORT
        raise DayZeroResetError(
            f"refusing day-zero: HUD listener reachable on 127.0.0.1:{port} — "
            "stop Omega first, then re-run"
        )


def verify_day_zero_surfaces() -> set[str]:
    """Fail if unexpected Guardian state survives the reset.

    Returns the set of allowed survivor filenames under guardian/state/ (the preserve set
    that actually exists on disk).

    Memory-organ Cut 13 (D-13-65/70): the projection runtime surfaces are checked FIRST —
    even when guardian/state/ no longer exists — and preserved infrastructure lock files are
    allow-listed (never residue).
    """
    residue = projection_runtime_residue(OMEGA_ROOT / "memory")
    if residue:
        raise DayZeroResetError(
            "projection runtime state survived day-zero: " + ", ".join(sorted(residue)[:20])
        )
    if not GUARDIAN_STATE_ROOT.is_dir():
        return set()

    survivors = {child.name for child in GUARDIAN_STATE_ROOT.iterdir()}
    unexpected = survivors - GUARDIAN_STATE_PRESERVE
    if unexpected:
        raise DayZeroResetError(
            "unexpected guardian/state survivors after day-zero: "
            + ", ".join(sorted(unexpected))
        )
    return survivors


# ---------------------------------------------------------------------
# Wipe
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Memory-organ Cut 13 — projection runtime wipe + quiescence (D-13-65/70/75)
# ---------------------------------------------------------------------
# The projection runtime lives INSIDE the memory/projection Python package (like dme /
# source_ledger / integrity above): DATA ONLY, never rmtree(memory/projection). The four
# infrastructure lock files are PRESERVED (an unlink would let a newcomer lock a replacement
# inode on POSIX / fail partway on Windows) and allow-listed by the verifier.

PROJECTION_INFRASTRUCTURE_LOCKS: tuple[str, ...] = ("publication.lock", "lease_namespace.lock")
INDEXES_INFRASTRUCTURE_LOCKS: tuple[str, ...] = ("projection_delta.lock",)
LIFECYCLE_LOCK_NAME = "lifecycle.lock"   # guardian/lifecycle.lock — outside guardian/state, survives the wipe

PROJECTION_RUNTIME_SURFACES: dict = {
    "projection_files": (
        "manifest.json", "manifest.json.tmp", "publication_intent.json", "repair_intent.json", "lineage.jsonl",
        "repair_lineage.jsonl", "consumed.json", "outbox_epoch.json", "outbox_bootstrap_intent.json",
        "outbox_malformed.json", "outbox_recovery.jsonl", "coverage_seal.json",
    ),
    "projection_globs": ("*.tmp", ".*.tmp", "lineage.torn-*", "gen-*", "work-*"),
    "projection_dirs": ("quarantine", "leases", "registry_authority"),
    "indexes_files": ("projection_delta.jsonl", "projection_dirty.json"),
    "indexes_globs": ("projection_delta.jsonl.quarantine-*",),
}


def _is_package_file(path: Path) -> bool:
    return path.suffix == ".py" or path.name == "__pycache__" or "__pycache__" in path.parts


def _projection_surface_paths(memory_root: Path) -> list[Path]:
    proj = memory_root / "projection"
    idx = memory_root / "indexes"
    found: list[Path] = []
    if proj.is_dir():
        for name in PROJECTION_RUNTIME_SURFACES["projection_files"]:
            if (proj / name).exists():
                found.append(proj / name)
        for pattern in PROJECTION_RUNTIME_SURFACES["projection_globs"]:
            found.extend(p for p in proj.glob(pattern) if not _is_package_file(p) and p.name not in PROJECTION_INFRASTRUCTURE_LOCKS)
        for name in PROJECTION_RUNTIME_SURFACES["projection_dirs"]:
            if (proj / name).exists():
                found.append(proj / name)
    if idx.is_dir():
        for name in PROJECTION_RUNTIME_SURFACES["indexes_files"]:
            if (idx / name).exists():
                found.append(idx / name)
        for pattern in PROJECTION_RUNTIME_SURFACES["indexes_globs"]:
            found.extend(idx.glob(pattern))
    # unique, stable order
    seen: set[Path] = set()
    out: list[Path] = []
    for p in sorted(found):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def projection_runtime_residue(memory_root: Path) -> list[str]:
    """Runtime projection state that must NOT survive day-zero (infrastructure locks and
    package files are never residue)."""
    return [str(p.relative_to(memory_root)) for p in _projection_surface_paths(Path(memory_root))]


class ResetAuthority:
    """Open handles proving projection quiescence for the whole wipe (D-13-70)."""

    def __init__(self) -> None:
        self.handles: list[tuple[str, int]] = []

    def release(self) -> None:
        from memory.projection.generation import _unlock

        for _name, fd in reversed(self.handles):
            try:
                _unlock(fd)
                os.close(fd)
            except OSError:
                pass
        self.handles = []

    def release_generation_leases(self) -> int:
        """Release ONLY the per-generation lease handles; the four infrastructure handles
        (lifecycle, publication, outbox, lease namespace) stay held. Quiescence is not
        weakened: a reader enrols a lease under the namespace SHARED, and the namespace is
        still ours EXCLUSIVE, so no lease can appear between this release and the wipe.

        Needed on Windows, where an open handle pins its file: the wipe removes
        ``projection/leases`` and rmtree of a directory holding our own open lease files
        fails there (POSIX unlinks an open file without complaint)."""
        from memory.projection.generation import _unlock

        kept: list[tuple[str, int]] = []
        released = 0
        for name, fd in self.handles:
            if not name.startswith("generation lease "):
                kept.append((name, fd))
                continue
            try:
                _unlock(fd)
                os.close(fd)
            except OSError:
                pass
            released += 1
        self.handles = kept
        return released


_exclusion_hook = None   # test seam only: callable(stage_name) invoked before each acquisition
_active_authority: Optional[ResetAuthority] = None   # the authority of the exclusion currently in force


@contextmanager
def projection_reset_exclusion(memory_root: Path, *, lifecycle_lock: Optional[Path] = None):
    """Acquire, NONBLOCKING and fail-loud, in the ONE frozen order (D-13-75):
    lifecycle interlock EXCLUSIVE → publication authority EXCLUSIVE → outbox lock EXCLUSIVE →
    lease namespace EXCLUSIVE (no new reader/scorer can enrol) → every existing generation
    lease EXCLUSIVE (a held reader OR scorer lease simply fails quiescence). Any failure
    releases everything and refuses BEFORE any deletion. Handles are held through the wipe;
    the lock files themselves are never unlinked."""
    global _active_authority
    from memory.projection.generation import _open_lock_handle, _try_lock

    memory_root = Path(memory_root)
    proj = memory_root / "projection"
    lifecycle = lifecycle_lock if lifecycle_lock is not None else Path(OMEGA_ROOT) / "guardian" / LIFECYCLE_LOCK_NAME
    authority = ResetAuthority()

    def acquire(name: str, path: Path) -> None:
        if _exclusion_hook is not None:
            _exclusion_hook(name)                                   # test seam: pause/enrol BEFORE this acquisition
        fd = _open_lock_handle(path)
        if not _try_lock(fd, exclusive=True):
            os.close(fd)
            raise DayZeroResetError(
                f"refusing day-zero: {name} is held ({path}) — a reader, scorer, publisher or Omega process is still live; "
                "stop it and re-run"
            )
        authority.handles.append((name, fd))

    try:
        # (0)-(3): the STABLE gates first. Only once the lease NAMESPACE is held EXCLUSIVE can no
        # new generation lease appear (acquire_shared_lease enrols under the namespace SHARED), so
        # the lease enumeration below is CLOSED — enumerating before the namespace gate would let a
        # reader enrol in the gap and reset would yield quiescence over a live lease (review 73646a37 §2).
        acquire("lifecycle interlock", lifecycle)
        acquire("publication authority", proj / "publication.lock")
        acquire("outbox lock", memory_root / "indexes" / "projection_delta.lock")
        acquire("lease namespace", proj / "lease_namespace.lock")
        # (4): NOW enumerate every generation lease and take each EXCLUSIVE.
        leases_dir = proj / "leases"
        if leases_dir.is_dir():
            for lease in sorted(leases_dir.glob("*.lease")):
                acquire(f"generation lease {lease.name}", lease)
        _active_authority = authority
        yield authority
    finally:
        _active_authority = None
        authority.release()


def wipe_projection_runtime(memory_root: Optional[Path] = None) -> list[str]:
    """DATA ONLY: remove every runtime projection surface under memory/projection and the
    Cut-4 outbox / dirty marker under memory/indexes; preserve every *.py / __pycache__ and
    the infrastructure lock files. Call ONLY inside projection_reset_exclusion."""
    mem = Path(memory_root) if memory_root is not None else OMEGA_ROOT / "memory"
    removed: list[str] = []
    if os.name == "nt" and _active_authority is not None:
        # Windows cannot delete a file we hold open; drop the per-generation lease handles
        # (the namespace stays EXCLUSIVE) so ``leases`` below can actually go.
        _active_authority.release_generation_leases()
    for path in _projection_surface_paths(mem):
        if _is_package_file(path) or path.name in PROJECTION_INFRASTRUCTURE_LOCKS or path.name in INDEXES_INFRASTRUCTURE_LOCKS:
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(str(path.relative_to(mem)))
        except OSError:
            pass
    return removed



def wipe_corpus() -> int:
    """Delete every file under each lobe directory (any extension).

    Lobe + subdomain directories remain (empty) until normalize runs.
    """
    removed = 0
    for lobe in LOBE_DIRS:
        lobe_path = OMEGA_ROOT / "memory" / lobe
        if not lobe_path.is_dir():
            continue
        for f in lobe_path.rglob("*"):
            if not f.is_file():
                continue
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def wipe_memory_runtime_indexes() -> list[str]:
    """Remove runtime index sidecars; keep shipped label_map.json."""
    indexes = OMEGA_ROOT / "memory" / "indexes"
    removed: list[str] = []
    if not indexes.is_dir():
        return removed
    for f in indexes.glob("*.json"):
        if f.name == "label_map.json":
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError:
            pass
    return removed


def wipe_guardian_state_completely() -> dict:
    """Remove every file and subdirectory under guardian/state/.

    FULL FACTORY RESET (operator disposition): wipes ALL operator/testing residue here —
    face anchors, uploads, substrate snapshots, queues, the dynamic-Y registry,
    knowledge_z_index, the trajectory mirror, credential_ledger.enc, boot.lock,
    installed plugin state, MCP authorization/grants, and uploaded files.

    There are no credential or operator-state exceptions. A factory reset must
    produce a product that is not attached to the previous operator. Fail-closed:
    any OSError during removal aborts the reset (no half-wipe).
    """
    if not GUARDIAN_STATE_ROOT.is_dir():
        return {"removed_files": 0, "removed_dirs": 0, "preserved": 0}
    removed_files = 0
    removed_dirs = 0
    preserved = 0
    failures: list[str] = []
    for child in list(GUARDIAN_STATE_ROOT.iterdir()):
        try:
            if child.is_file():
                child.unlink()
                removed_files += 1
            elif child.is_dir():
                shutil.rmtree(child)
                removed_dirs += 1
        except OSError as exc:
            log.warning("guardian state removal failed for %s: %s", child, exc)
            failures.append(f"{child.name}: {exc}")
    if failures:
        raise DayZeroResetError(
            "guardian/state removal failed (stop Omega and retry): " + "; ".join(failures)
        )
    return {
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "preserved": preserved,
    }


def wipe_guardian_audit() -> dict:
    """Remove audit residue under guardian/audit/ (survived the state-tree wipe)."""
    audit_dir = OMEGA_ROOT / "guardian" / "audit"
    if not audit_dir.is_dir():
        return {"removed": 0, "bytes_freed": 0}
    removed = 0
    bytes_freed = 0
    failures: list[str] = []
    for f in list(audit_dir.rglob("*")):
        if not f.is_file():
            continue
        try:
            bytes_freed += f.stat().st_size
            f.unlink()
            removed += 1
        except OSError as exc:
            log.warning("guardian audit removal failed for %s: %s", f, exc)
            failures.append(f"{f.name}: {exc}")
    for child in list(audit_dir.iterdir()):
        if child.is_dir():
            try:
                shutil.rmtree(child)
            except OSError as exc:
                log.warning("guardian audit dir removal failed for %s: %s", child, exc)
                failures.append(f"{child.name}: {exc}")
    if failures:
        raise DayZeroResetError(
            "guardian/audit removal failed: " + "; ".join(failures)
        )
    return {"removed": removed, "bytes_freed": bytes_freed}


def wipe_guardian_ledger() -> list[str]:
    """Remove ledger chain + key material so the next boot births a fresh instance."""
    removed: list[str] = []
    if not LEDGER_ROOT.is_dir():
        return removed
    for name in ("chain.jsonl", "key.bin", ".activated"):
        path = LEDGER_ROOT / name
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed.append(name)
        except OSError as exc:
            log.warning("ledger removal failed for %s: %s", name, exc)
    return removed


def wipe_checkpoints() -> dict:
    """Remove every mind-state restore point under guardian/checkpoints/.

    Checkpoints (guardian/mind_checkpoint) are full snapshots of prior mind
    state — guardian/state + memory lobes + deepsleep/pressure state. They live
    as a SIBLING of guardian/state (so a checkpoint never captures itself) and
    are therefore outside the complete-state wipe. A factory reset must drop
    them, or restore_mind_state(<old_id>) could resurrect the wiped mind.
    """
    base = OMEGA_ROOT / "guardian" / "checkpoints"
    if not base.is_dir():
        return {"removed_checkpoints": 0, "bytes_freed": 0}
    removed = 0
    bytes_freed = 0
    for child in list(base.iterdir()):
        try:
            if child.is_dir():
                bytes_freed += sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                shutil.rmtree(child)
                removed += 1
            elif child.is_file():
                bytes_freed += child.stat().st_size
                child.unlink()
        except OSError as exc:
            log.warning("checkpoint removal failed for %s: %s", child, exc)
    return {"removed_checkpoints": removed, "bytes_freed": bytes_freed}


def wipe_pressure_loop_state() -> dict:
    """Remove all files under pressure_loop/state/ (episode journal + residue)."""
    base = OMEGA_ROOT / "pressure_loop" / "state"
    if not base.is_dir():
        return {"removed": 0}
    removed = 0
    for f in base.rglob("*"):
        if not f.is_file():
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return {"removed": removed}


def wipe_legacy_omega_data_tree() -> bool:
    """Remove the retired `.omega_data/` tree if it still exists on disk."""
    legacy = OMEGA_ROOT / ".omega_data"
    if not legacy.exists():
        return False
    try:
        shutil.rmtree(legacy)
        return True
    except OSError as exc:
        log.warning("legacy .omega_data tree removal failed: %s", exc)
        return False


# ── Sibling portable runtimes on the SSD ─────────────────────────────────────
# The product tree is not the whole product. ``Omega.exe`` starts LM Studio from
# ``<ssd-root>/LM Studio`` (omega_launcher.LM_STUDIO_ROOT_RELATIVE), which keeps the
# operator's conversations, identity, OAuth credentials and caches BESIDE OMEGA_ROOT,
# where every other wipe here is blind to them. A factory ship must not carry the
# previous operator's chat history (law §0: every instance belongs to someone else).
#
# The split is explicit in both directions, because this is a third-party tree: the
# entries below are operator state, and everything else — ``LM Studio.exe``, the
# launcher scripts, ``locales/``, ``resources/``, ``apps/``, ``bin/``, ``extensions/``
# (engines and their weights), ``hub/`` model bookkeeping, ``config-presets/``,
# ``.internal/bundled-models/`` (shipped weights) and the portable-install markers
# ``app-install-location.json`` / ``portable-state.json`` / ``internal-engine-index.json``
# — is shipped product and stays. A wipe that guessed the other way would brick the
# runtime it was meant to hand over clean.
PORTABLE_RUNTIME_STATE: dict[str, dict[str, tuple[str, ...]]] = {
    # Windows portable LM Studio.
    "LM Studio": {
        "dirs": (
            "conversations",                                 # operator chat history
            "credentials",                                   # mcp-oauth tokens
            ".internal/api-prediction-history",
            ".internal/retrieval-sessions",
            ".internal/parsed-documents-cache",
            ".internal/cached-rag-pipeline-chunks",
            ".internal/files",                               # operator attachments
            ".internal/temp-downloads",
            ".internal/ui-state",
            ".internal/global-hotkeys",
            ".internal/config-presets-drafts",
            ".internal/portable-path-backups",
            ".internal/portable-acl-backups",
            ".internal/staged-updates-app",
            ".internal/user-concrete-model-default-config",
            ".internal/skills",
        ),
        "files": (
            "mcp.json",                                      # operator MCP wiring
            ".internal/local-identity.json",                  # previous operator's identity
            ".internal/lms-key-2",                            # API key
            ".internal/conversation-config.json",
            ".internal/download-jobs-info.json",
            ".internal/single-downloads-info.json",
            ".internal/projects-registry.json",
            ".internal/server-logs-state.json",
            ".internal/last-synced-mcp-state.json",
            ".internal/artifact-permissions-list.json",
            ".internal/persistent-extension-pack-state.json",
            ".internal/llmster-pid.lock",
            ".internal/model-index-cache.json",               # names what THEY downloaded
            ".internal/gguf-metadata-cache.json",
        ),
    },
    # Linux AppImage runtime. Its own state lives in the host home directory, not on
    # the SSD; what lands here is the log Omega's start command redirects into the
    # runtime folder (omega_launcher.LINUX_STARTUP_COMMAND).
    "LM-Studio-Linux": {
        "dirs": (),
        "files": ("lm-studio.log", "nohup.out"),
    },
}


def portable_ssd_root(omega_root: Optional[Path] = None) -> Path:
    """Return the directory that holds the product tree and its sibling runtimes.

    Mirrors ``omega_launcher.ssd_root``: a tree named ``Omega`` sits one level below
    the SSD root, and any other tree is the root itself unless a sibling portable
    runtime proves otherwise. Without that proof nothing outside the product tree is
    ours to delete.
    """
    root = (Path(omega_root) if omega_root is not None else OMEGA_ROOT).resolve(strict=False)
    if root.name.casefold() == "omega":
        return root.parent
    if any((root.parent / name).is_dir() for name in PORTABLE_RUNTIME_STATE):
        return root.parent
    return root


def wipe_portable_runtime_state(ssd_root: Optional[Path] = None) -> dict:
    """Remove operator state from the portable runtimes beside the product tree.

    An absent runtime is "nothing to wipe", never a failure. A removal that fails
    aborts the reset, like every other wipe here: a half-wiped runtime must not be
    reported as a clean factory ship.
    """
    root = Path(ssd_root) if ssd_root is not None else portable_ssd_root()
    removed_files = 0
    removed_dirs = 0
    runtimes: list[str] = []
    failures: list[str] = []
    for name, spec in PORTABLE_RUNTIME_STATE.items():
        runtime = root / name
        if not runtime.is_dir():
            continue
        runtimes.append(name)
        for relative in spec["dirs"]:
            try:
                target = ensure_child_path(runtime, runtime / relative)
            except ValueError as exc:
                failures.append(f"{name}/{relative}: {exc}")
                continue
            if not target.is_dir():
                continue
            try:
                shutil.rmtree(target)
                removed_dirs += 1
            except OSError as exc:
                log.warning("portable runtime removal failed for %s: %s", target, exc)
                failures.append(f"{name}/{relative}: {exc}")
        for relative in spec["files"]:
            try:
                target = ensure_child_path(runtime, runtime / relative)
            except ValueError as exc:
                failures.append(f"{name}/{relative}: {exc}")
                continue
            if not target.is_file():
                continue
            try:
                target.unlink()
                removed_files += 1
            except OSError as exc:
                log.warning("portable runtime removal failed for %s: %s", target, exc)
                failures.append(f"{name}/{relative}: {exc}")
    if failures:
        raise DayZeroResetError(
            "portable runtime removal failed (close LM Studio and retry): "
            + "; ".join(failures)
        )
    return {
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "runtimes": runtimes,
    }


def wipe_staging() -> int:
    """Remove all staged envelope files (any name under staging/)."""
    staging_dir = STAGING_ROOT
    if not staging_dir.is_dir():
        return 0
    removed = 0
    for f in staging_dir.rglob("*"):
        if not f.is_file():
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def wipe_tmp_writes() -> int:
    tmp = OMEGA_ROOT / "memory" / "tmp_writes"
    if not tmp.exists():
        return 0
    removed = 0
    for f in tmp.glob("*"):
        if f.is_file():
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def wipe_runtime_jsonl() -> int:
    data_dir = GUARDIAN_LOGS_ROOT
    removed = 0
    for name in RUNTIME_JSONL:
        f = data_dir / name
        if f.exists():
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def wipe_logs() -> int:
    logs_dir = GUARDIAN_LOGS_ROOT
    if not logs_dir.exists():
        return 0
    removed = 0
    for f in logs_dir.glob("*"):
        if f.is_file():
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        elif f.is_dir():
            try:
                shutil.rmtree(f)
                removed += 1
            except OSError:
                pass
    return removed


def wipe_telemetry_firehose() -> dict:
    """Sweep legacy data strays from guardian/telemetry (package code preserved).

    All log data lives in guardian/logs since 2026-07-13 (wiped by step 8);
    this only catches files an older build may still drop in the old dir."""
    telemetry_dir = OMEGA_ROOT / "guardian" / "telemetry"
    if not telemetry_dir.is_dir():
        return {"removed": 0, "bytes_freed": 0}
    removed = 0
    bytes_freed = 0
    for pattern in ("*.jsonl", "*.log"):
        for f in telemetry_dir.glob(pattern):
            if not f.is_file():
                continue
            try:
                bytes_freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError:
                pass
    return {"removed": removed, "bytes_freed": bytes_freed}


def _resolve_policy_path(path) -> Path:
    """Resolve a policy-relative path (str or Path) against OMEGA_ROOT.

    Coerces str -> Path: policy accessors may return either, and calling `.is_absolute()` on a
    str raised AttributeError that a downstream broad except silently swallowed (#23). All
    policy paths are also confined to the portable product tree: a stale or malicious absolute
    redirect must never make factory reset delete an external workspace."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = OMEGA_ROOT / candidate
    try:
        return ensure_child_path(OMEGA_ROOT, candidate)
    except ValueError as exc:
        raise DayZeroResetError(
            f"policy path is outside portable Omega root: {candidate}"
        ) from exc


def wipe_deepsleep_state() -> dict:
    """Delete every file under the live deepsleep state directory (recursive)."""
    from pressure_loop.policy import deepsleep_mastery_path

    base = _resolve_policy_path(deepsleep_mastery_path()).parent
    if not base.is_dir():
        return {"removed": 0, "bytes_freed": 0}

    removed = 0
    bytes_freed = 0
    for f in base.rglob("*"):
        if not f.is_file():
            continue
        try:
            bytes_freed += f.stat().st_size
            f.unlink()
            removed += 1
        except OSError:
            pass
    return {"removed": removed, "bytes_freed": bytes_freed}


def wipe_knowledge_extractor_state() -> dict:
    """Delete the knowledge_extractor artifact store — the learned-knowledge artifacts
    that survive OUTSIDE every other wiped root (knowledge_extractor/state/ is a
    top-level sibling dir, not under guardian/ or memory/ or the loop state dirs). On a
    real run this was 585KB of stale artifacts from prior cognition — exactly the
    contamination class day-zero exists to clear. Resolve via the policy accessor; never
    hardcode (config/deepsleep.yaml may redirect it)."""
    try:
        from pressure_loop.policy import knowledge_artifact_store_path

        base = _resolve_policy_path(knowledge_artifact_store_path()).parent
    except ImportError:
        # Only a genuinely-absent accessor falls back to the default. A str-vs-Path or config
        # error must NOT be swallowed into wiping the wrong (default) store, leaving the
        # redirected one untouched (#23).
        base = OMEGA_ROOT / "knowledge_extractor" / "state"
    if not base.is_dir():
        return {"removed": 0, "bytes_freed": 0}
    removed = 0
    bytes_freed = 0
    for f in base.rglob("*"):
        if not f.is_file():
            continue
        try:
            bytes_freed += f.stat().st_size
            f.unlink()
            removed += 1
        except OSError:
            pass
    return {"removed": removed, "bytes_freed": bytes_freed}


# Non-lobe memory/ runtime stores (coverage audit 2026-07-18): NOT under a
# lobe dir, memory/indexes, or the taxonomy store, so every prior wipe
# skipped them — the DME substrate (sram + hop planes + coordinate
# registry), the raw-source provenance ledger (~6000 artifacts), the
# recall-verdict audit log, the sense registry, the quarantine corpus, and the integrity cache
# all SURVIVED a "factory reset". All rebuild lazily on a fresh boot.
MEMORY_RUNTIME_STORES = (
    "dme",
    "source_ledger",
    "recall_verifier",
    "sense_registry",
    "integrity",
)


def wipe_memory_runtime_stores() -> list[str]:
    """DATA ONLY. Three of the five stores live INSIDE Python packages
    (dme, source_ledger, integrity) — a whole-dir rmtree deletes CODE, and
    did exactly that on the first live wipe (2026-07-18; restored from git).
    Wipe each store's data shape precisely:
      dme:            top-level non-.py files (sram + hop planes + registry)
      source_ledger:  artifacts/ + projection_receipts/ data subtrees
      quarantine:     rejected/held memory records from prior operation
      integrity:      last_audit.json only
      recall_verifier, sense_registry: whole dirs (pure DATA — their code
                      is memory/recall_verifier.py / memory/sense_registry.py)
    """
    removed: list[str] = []
    mem = OMEGA_ROOT / "memory"

    dme = mem / "dme"
    if dme.is_dir():
        for f in dme.iterdir():
            if f.is_file() and f.suffix != ".py":
                try:
                    f.unlink()
                    removed.append(f"dme/{f.name}")
                except OSError:
                    pass

    artifacts = mem / "source_ledger" / "artifacts"
    if artifacts.is_dir():
        shutil.rmtree(artifacts, ignore_errors=True)
        removed.append("source_ledger/artifacts")
    receipts = mem / "source_ledger" / "projection_receipts"
    if receipts.is_dir():
        shutil.rmtree(receipts, ignore_errors=True)
        removed.append("source_ledger/projection_receipts")

    quarantine = mem / "quarantine"
    if quarantine.is_dir():
        try:
            shutil.rmtree(quarantine)
            removed.append("quarantine")
        except OSError as exc:
            raise DayZeroResetError(
                f"memory quarantine removal failed (stop Omega and retry): {exc}"
            ) from exc

    last_audit = mem / "integrity" / "last_audit.json"
    if last_audit.is_file():
        try:
            last_audit.unlink()
            removed.append("integrity/last_audit.json")
        except OSError:
            pass

    for name in ("recall_verifier", "sense_registry"):
        target = mem / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
    return removed


def wipe_stray_compiled_graphs() -> list[str]:
    """Pre-relocation taxonomy fossils: memory/compiled/ + <root>/compiled/
    (produced when compiler/graph_store were called with root=MEMORY_ROOT or
    root=OMEGA_ROOT before the taxonomy_store relocation)."""
    removed: list[str] = []
    for target in (OMEGA_ROOT / "memory" / "compiled", OMEGA_ROOT / "compiled"):
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(target.name if target.parent == OMEGA_ROOT else "memory/compiled")
    return removed


def wipe_boot_logs() -> int:
    """Root-level per-boot log residue (harness writes; gitignored *.log)."""
    removed = 0
    for pat in ("_brain_boot*.log", "boot_live*.log", "boot_v*.log"):
        for f in OMEGA_ROOT.glob(pat):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    boot_logs_dir = OMEGA_ROOT / "_boot_logs"
    if boot_logs_dir.is_dir():
        shutil.rmtree(boot_logs_dir, ignore_errors=True)
        removed += 1
    root_checkpoints = OMEGA_ROOT / "checkpoints"
    if root_checkpoints.is_dir():
        for f in root_checkpoints.glob("boot_*.log"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def wipe_taxonomy_store() -> dict:
    """Delete the Phase-7 memory-taxonomy store (append-only event ledger + compiled
    graph.sqlite + manifests + indexes + audit). It IS memory — it lives under the memory
    system at memory/taxonomy_store/ (relocated 2026-06-25) and is Guardian-free by
    write-path, so the nuclear guardian/state wipe and wipe_corpus (lobe dirs only) never
    reach it. Also drops the LEGACY data/memory_taxonomy/ tree from before the relocation,
    and prunes the now-empty top-level data/ dir.
    """
    from core.paths import MEMORY_TAXONOMY_ROOT

    removed = 0
    bytes_freed = 0
    for base in (MEMORY_TAXONOMY_ROOT, OMEGA_ROOT / "data" / "memory_taxonomy"):
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            try:
                bytes_freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError:
                pass
        # Drop the empty tree so no stale dir skeleton survives the reset.
        try:
            shutil.rmtree(base)
        except OSError as exc:
            log.warning("taxonomy store dir removal failed for %s: %s", base, exc)

    # Prune the legacy top-level data/ dir if it's now empty (pre-relocation fossil).
    legacy_data = OMEGA_ROOT / "data"
    try:
        if legacy_data.is_dir() and not any(legacy_data.iterdir()):
            legacy_data.rmdir()
    except OSError:
        pass
    return {"removed": removed, "bytes_freed": bytes_freed}


def wipe_episode_journal() -> bool:
    """Remove the pressure-loop episode journal if present."""
    from pressure_loop.policy import episode_journal_path

    path = _resolve_policy_path(episode_journal_path())
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        log.warning("episode journal removal failed: %s", exc)
        return False


def reset_dynamic_y_index() -> bool:
    """Wipe the dynamic Y registry to ensure a clean slate for dynamic
    subdomain allocation. This is essential for day-zero reset to prevent
    stale Y allocations from persisting across resets.

    Returns True if the file was removed or didn't exist, False on error.
    """
    from memory.subdomain_allocator import _index_path

    path = _index_path()
    if not path.is_file():
        return True
    try:
        path.unlink()
        return True
    except OSError as exc:
        log.warning("dynamic Y index reset failed: %s", exc)
        return False


def wipe_legacy_console_fossils() -> int:
    """Remove Windows console redirection fossils from the local tree."""
    removed = 0
    targets: list[Path] = []
    targets.extend(OMEGA_ROOT / name for name in ("nul", "$null"))
    for f in targets:
        if f.is_file():
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------
# Lobe tree normalization — lobes only, subdomains created on write
# ---------------------------------------------------------------------
#
# What this fixes: lazy-creation of subdirs by per-lobe writers means
# the on-disk tree slowly accumulates the SHAPE of every test, every
# persona, every knowledge_domain ever written. After a wipe the files
# are gone but the directory skeleton survives — a "museum of every
# prior write."
#
# The fix (operator doctrine 2026-06-29): keep the LOBE dirs, strip
# everything beneath. "i want the lobes to survive but not the folders
# beneath all of them ... the rest make on memory write already." Every
# subdomain folder is created lazily on first write — the universal
# write path (memory.engine.validate_write.final_path_for_meta) calls
# dest_dir.mkdir(parents=True) on EVERY memory — so day-zero pre-scaffolds
# nothing. A factory-fresh lobe is EMPTY; its dewey shape (situational
# facets, temporal buckets, persona dirs, knowledge clusters) re-grows
# organically as memory is written. The 2 identity seeds are the sole
# day-zero content (they land in 000_identity/constitution/ via the seed
# step, which runs AFTER this normalization).


def _drop_directory_tree(path: Path) -> int:
    """rmtree with safe error handling. Returns 1 on success, 0 on no-op."""
    if not path.exists() or not path.is_dir():
        return 0
    try:
        shutil.rmtree(path)
        return 1
    except OSError:
        return 0


def normalize_lobe_structure() -> dict:
    """Strip memory/ back to canonical LOBE dirs only — subdomains on write.

    Operator doctrine (2026-06-29): the lobes survive a factory reset; the
    folders beneath them do NOT — every subdomain is created lazily on the
    first memory write to it. So this:
      - Drops legacy / non-canonical lobe dirs (digit-prefixed, not in
        LOBE_MAP — e.g. the pre-2026-05-06 100_personal / 400_context).
      - Drops retired lobes entirely (5/500_tools — tools live in
        config/tools.yaml, not the memory system).
      - Leaves each surviving lobe dir EMPTY: drops every child (stale
        subdomain dirs from prior writes + orphan files at the lobe root).
    Non-lobe code/index dirs (indexes, tmp_writes, engine, enrichment,
    gates, __pycache__ — none digit-prefixed) are left untouched.

    Returns a counts dict for reporting.
    """
    from memory.dewey_map import LOBE_MAP

    memory_root = OMEGA_ROOT / "memory"
    counts = {
        "dropped_dirs": 0,
        "dropped_files": 0,
        "dropped_legacy": 0,
    }

    canonical_lobe_names = set(LOBE_MAP.values())

    # Pass 1: drop legacy / non-canonical LOBE dirs from memory/ so the reset
    # produces EXACTLY the canonical layout (operator: "create this exact layout").
    # Lobe dirs are digit-prefixed (000_identity .. 1100_dev); a digit-prefixed dir
    # NOT in canonical_lobe_names is a pre-restructure fossil (100_personal,
    # 400_context) and is dropped. A canonical-but-retired lobe (500_tools is still
    # in LOBE_MAP) is left for Pass 2's RETIRED_LOBES drop. Non-digit dirs (indexes,
    # tmp_writes, engine, enrichment, gates, __pycache__) are code/index modules,
    # never data — left untouched so a future module is never at risk.
    for child in list(memory_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in canonical_lobe_names:
            continue  # canonical lobe (incl. retired) — handled in Pass 2
        if not child.name[:1].isdigit():
            continue  # code/index module, not a lobe dir
        counts["dropped_legacy"] += _drop_directory_tree(child)

    # Pass 2: per-lobe normalization. Operator doctrine (2026-06-29): day-zero
    # creates the LOBE dir only and strips it back to EMPTY — every subdomain
    # folder is created lazily on first write (final_path_for_meta mkdir's the
    # dest dir). So a factory-fresh lobe carries no subdomain scaffold and no
    # museum of prior writes; the dewey shape re-grows as memory is written.
    for lobe_x, lobe_dir_name in LOBE_MAP.items():
        lobe_path = memory_root / lobe_dir_name

        # Retired lobes (5/tools — tools live in config/tools.yaml, not memory):
        # drop any stale dir and NEVER recreate it. LOBE_MAP may still list the lobe.
        if lobe_x in RETIRED_LOBES:
            counts["dropped_dirs"] += _drop_directory_tree(lobe_path)
            continue

        lobe_path.mkdir(parents=True, exist_ok=True)

        # Strip to an empty lobe: drop every child — stale subdomain dirs from
        # prior writes AND any orphan files at the lobe root. Subdomains reappear
        # on the first memory write to them.
        for child in list(lobe_path.iterdir()):
            if child.is_file():
                try:
                    child.unlink()
                    counts["dropped_files"] += 1
                except OSError:
                    pass
            elif child.is_dir():
                counts["dropped_dirs"] += _drop_directory_tree(child)

    return counts


# ---------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------


def seed_constitution_lobe() -> list[str]:
    """Re-seed the FULL founding constitution set into 000_identity/constitution/.

    The canonical source is memory.identity.seeds.CONSTITUTION_SEEDS — the SAME set
    scripts/seed_identity_lobe.py writes on a live boot — so day-zero can never drift
    behind the doctrine (the historical 2-anchor inline seed silently dropped the 5
    sovereignty/capability/substrate/guardian/persistence records added later). Coord
    [0, 0, 0] = identity.constitution (CONSTITUTION_Y=0); canonical=True so they never
    decay out of recall. Idempotent by id via atomic_write; returns the written paths.
    """
    from memory.core import build_memory_dict
    from memory.engine.validate_write import atomic_write
    from memory.identity.seeds import CONSTITUTION_SEEDS
    from memory.source_ledger.store import record_source

    # DETERMINISTIC WRITER PATH (the determinism-harness pattern, 3rd live
    # fix 2026-07-18): day-zero must write to disk with the runtime DOWN.
    # The production promotion pipeline (write_memory) is wrong for a reset
    # script — it enforces identity-must-embed (needs the embed server) and
    # rolled the seeds back twice. record_source + build_memory_dict +
    # atomic_write satisfies the canonical source contract (ledger-first
    # citations) with ZERO runtime dependencies. Embeds/enrichment for the
    # seeds come from the normal boot-time pipelines like any other record.
    paths: list[str] = []
    for seed in CONSTITUTION_SEEDS:
        artifact = record_source(
            seed.text,
            source_type="constitution_seed",
            origin_id=f"seed:day_zero:{seed.record_id}",
        )
        mem = build_memory_dict(
            text=seed.text,
            coord=[0, 0, 0],
            source_tag="seed:day_zero",
            metadata={
                "id": seed.record_id,
                "type": seed.record_type,
                "title": seed.title,
                "summary": seed.summary,
                "canonical": True,
                "confidence": 1.0,
                "origin_id": "seed:day_zero",
                "origin_type": "seed",
                "tags": list(seed.tags),
                **artifact.citation(),
            },
        )
        paths.append(atomic_write(mem))
    return paths


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------


def main() -> int:
    print("=" * 70)
    print("  Day-Zero / Factory-Ship Reset")
    print("=" * 70)

    print("\n[preflight] Refusing wipe while Omega still looks live...")
    preflight_substrate_stopped()
    print("    substrate appears stopped")
    print("\n[preflight] Claiming the projection reset exclusion (lifecycle → publication → outbox → lease namespace → leases)...")
    with projection_reset_exclusion(OMEGA_ROOT / "memory") as _reset_authority:
        print(f"    quiescence proved by {len(_reset_authority.handles)} held handle(s)")
        return _main_under_exclusion()


def _main_under_exclusion() -> int:

    print("\n[0] Removing legacy .omega_data/ tree (retired write root)...")
    legacy_gone = wipe_legacy_omega_data_tree()
    print(f"    {'removed' if legacy_gone else 'absent (no-op)'}")

    print("\n[1] Wiping memory corpus (all lobe files, any extension)...")
    n_corpus = wipe_corpus()
    print(f"    removed {n_corpus} memory files")

    print("\n[2] Wiping memory runtime indexes (coord_index; label_map preserved)...")
    idx_removed = wipe_memory_runtime_indexes()
    print(f"    removed: {', '.join(idx_removed) or 'none (no-op)'}")

    print("\n[3] Wiping guardian/state/ (operator + cognition + plugin/MCP state)...")
    state_wipe = wipe_guardian_state_completely()
    print(
        f"    removed {state_wipe['removed_files']} files, "
        f"{state_wipe['removed_dirs']} subtrees, "
        f"preserved {state_wipe.get('preserved', 0)} ({', '.join(sorted(GUARDIAN_STATE_PRESERVE)) or 'none'})"
    )

    print("\n[2b] Wiping projection runtime surfaces (memory-organ Cut 13; data-only, lock files preserved)...")
    proj_removed = wipe_projection_runtime()
    print(f"    removed {len(proj_removed)} projection runtime surface(s) with the clock (timekeeper.json) in this same run")

    print("\n[3a] Wiping guardian/audit/ (agent prose and audit residue)...")
    audit = wipe_guardian_audit()
    audit_mb = audit["bytes_freed"] / (1024 * 1024)
    print(f"    removed {audit['removed']} audit files ({audit_mb:.2f} MB freed)")

    print("\n[3b] Verifying guardian/state reset is complete...")
    survivors = verify_day_zero_surfaces()
    print(
        "    guardian/state survivors: "
        + (", ".join(sorted(survivors)) if survivors else "(none)")
    )

    print("\n[4] Wiping guardian/ledger/ (fresh HMAC instance on next boot)...")
    ledger_removed = wipe_guardian_ledger()
    print(f"    removed: {', '.join(ledger_removed) or 'none (no-op)'}")

    print("\n[4b] Wiping guardian/checkpoints/ (mind-state restore points)...")
    cps = wipe_checkpoints()
    cp_mb = cps["bytes_freed"] / (1024 * 1024)
    print(f"    removed {cps['removed_checkpoints']} checkpoints ({cp_mb:.2f} MB freed)")

    print("\n[5] Wiping staging envelopes...")
    n_stage = wipe_staging()
    print(f"    removed {n_stage} staged files")

    print("\n[6] Wiping tmp_writes...")
    n_tmp = wipe_tmp_writes()
    print(f"    removed {n_tmp} tmp files")

    print("\n[6b] Wiping sibling portable runtime state (LM Studio conversations, identity, credentials)...")
    portable = wipe_portable_runtime_state()
    if portable["runtimes"]:
        print(
            f"    {', '.join(portable['runtimes'])}: removed {portable['removed_files']} files "
            f"and {portable['removed_dirs']} directories (shipped app and model weights preserved)"
        )
    else:
        print("    no sibling portable runtime present on the SSD")

    print("\n[7] Wiping runtime JSONL streams...")
    n_jsonl = wipe_runtime_jsonl()
    print(f"    removed {n_jsonl} JSONL files")

    print("\n[8] Wiping guardian/logs/...")
    n_logs = wipe_logs()
    print(f"    removed {n_logs} log entries")

    print("\n[9] Wiping guardian/telemetry firehose (*.jsonl + *.log)...")
    firehose = wipe_telemetry_firehose()
    mb_freed = firehose["bytes_freed"] / (1024 * 1024)
    print(
        f"    removed {firehose['removed']} firehose files "
        f"({mb_freed:.2f} MB freed; *.py preserved)"
    )

    print("\n[10] Wiping deepsleep_loop/state/ (recursive)...")
    deepsleep = wipe_deepsleep_state()
    ds_mb = deepsleep["bytes_freed"] / (1024 * 1024)
    print(f"    removed {deepsleep['removed']} deepsleep state files ({ds_mb:.2f} MB freed)")

    print("\n[11] Wiping pressure_loop/state/...")
    pressure = wipe_pressure_loop_state()
    print(f"    removed {pressure['removed']} pressure-loop state files")

    print("\n[11a] Wiping episode journal via policy accessor (redirect-safe)...")
    # The directory sweep above clears the DEFAULT journal
    # (pressure_loop/state/episode_journal.jsonl). But episode_journal_path() is
    # config-redirectable (journal config YAML); when pointed outside that dir the
    # fixed-dir sweep misses it. Resolve the accessor so a redirected episodic ledger
    # never survives the reset.
    journal_gone = wipe_episode_journal()
    print(f"    {'removed' if journal_gone else 'absent (no-op)'}")

    print("\n[11b] Wiping knowledge_extractor/state/ (learned artifact store)...")
    kx = wipe_knowledge_extractor_state()
    kx_mb = kx["bytes_freed"] / (1024 * 1024)
    print(f"    removed {kx['removed']} artifact files ({kx_mb:.2f} MB freed)")

    print("\n[11c] Wiping memory taxonomy store (Phase-7 ledger + compiled graph)...")
    tax = wipe_taxonomy_store()
    tax_mb = tax["bytes_freed"] / (1024 * 1024)
    print(
        f"    removed {tax['removed']} taxonomy files ({tax_mb:.2f} MB freed; legacy data/ pruned)"
    )

    print(
        "\n[11d] Wiping non-lobe memory runtime stores (dme/source_ledger/"
        "recall_verifier/sense_registry/integrity)..."
    )
    stores = wipe_memory_runtime_stores()
    print(f"    removed {len(stores)} store dir(s): {', '.join(stores) or 'none present'}")

    print("\n[11e] Wiping stray pre-relocation compiled graphs...")
    strays = wipe_stray_compiled_graphs()
    print(f"    removed: {', '.join(strays) or 'none present'}")

    print("\n[11f] Wiping root-level boot logs...")
    boot_logs = wipe_boot_logs()
    print(f"    removed {boot_logs} boot log file(s)/dir(s)")

    print("\n[11g] Wiping stray bridge logs + Windows console fossils...")
    stray_logs = wipe_legacy_console_fossils()
    print(f"    removed {stray_logs} stray file(s)")

    print("\n[12] Normalizing lobe tree (lobes only; subdomains created on write)...")
    print("      (operator doctrine: lobes survive, the folders beneath them are")
    print("       made on memory write — strips stale subdomain dirs + legacy/retired)")
    norm = normalize_lobe_structure()
    print(
        f"      dropped: {norm['dropped_dirs']} subdomain/stale dirs / "
        f"{norm['dropped_files']} files / "
        f"{norm['dropped_legacy']} legacy lobe dirs"
    )
    from memory.dewey_map import LOBE_MAP

    n_lobes = len(set(LOBE_MAP) - RETIRED_LOBES)
    print(f"      lobes:   {n_lobes} canonical lobe dirs left empty (subdomains on write)")

    print("\n[13] Resetting dynamic Y registry...")
    dynamic_y_reset = reset_dynamic_y_index()
    print(f"      {'removed' if dynamic_y_reset else 'failed to remove'} dynamic Y index")

    print("\n[14] Seeding constitution (000_identity) — full founding doctrine set...")
    seed_paths = seed_constitution_lobe()
    for p in seed_paths:
        # Display-safe: receipts may carry an absolute path outside
        # OMEGA_ROOT (redirected roots) or a bare record id (async staging).
        try:
            shown = str(Path(p).relative_to(OMEGA_ROOT))
        except ValueError:
            shown = str(p)
        print(f"    {shown}")

    # Seeding uses the normal canonical write path, which emits audit entries.
    # A factory reset must finish empty, so remove those entries after the
    # deterministic founding records have been written.
    print("\n[15] Removing reset-generated logs...")
    final_logs = wipe_logs()
    print(f"    removed {final_logs} log entries emitted while seeding")

    print("\n" + "=" * 70)
    print("  Factory-ship reset complete.")
    print("=" * 70)
    print(
        f"  Corpus:       {len(seed_paths)} constitution seeds (identity + creator + "
        "memory_sovereignty, capability_delta, deterministic_substrate, guardian_boundary, "
        "persistence_directive)"
    )
    print("  Plugins/MCP:  installed state, grants, permissions, and OAuth vault cleared")
    print("  guardian/state: empty (no operator credentials survive factory reset)")
    print("  guardian/audit: cleared")
    print("  guardian/ledger: empty (fresh chain on next boot)")
    print("  guardian/checkpoints: empty (no mind-state restore points survive)")
    print("  Tools:        registry (config/tools.yaml) — not memory lobe 5")
    print("  Indexes:      coord_index rebuilt from seeds; label_map preserved")
    print("  Graph:        compiled memory graph + taxonomy store wiped (rebuilds from canonical)")
    print("  Deepsleep / pressure / logs / telemetry: cleared (.agents bus preserved)")
    print("  Episode log:  journal cleared via policy accessor (redirect-safe)")
    print(
        "  API keys:     cleared (credentials must be entered again by the new operator)"
    )
    print(
        "  Runtime:      Omega-owned local-service state was cleared with guardian/state"
    )
    print(
        "  External workspaces: untouched (reset is confined to the portable Omega tree)"
    )
    print("  Lobes:        canonical lobe dirs only, EMPTY (subdomains created on memory write)")
    print("  Dynamic Y:    registry wiped (every lobe seeds subdomains on first write)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DayZeroResetError as exc:
        print(f"\nDAY-ZERO ABORTED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
