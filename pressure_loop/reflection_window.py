"""Reflection window state — EpisodeStore-owned 10-episode Omega input window."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

_LOG = logging.getLogger(__name__)


def window_configuration() -> dict[str, Any]:
    """The governed ``reflection_window`` configuration section. Fail-hard.

    WO-09 (BOUNDARY_MAP L11): this used to sit behind a bare ``except Exception`` that turned
    a missing, unreadable or malformed configuration into a silent default of 10. The window
    size is the membership rule of every cohort; a configuration that cannot be read is a
    failure, never a number.
    """
    try:
        from config.loader import load_subsystem_bundle

        bundle = load_subsystem_bundle("reflection_window")
    except Exception as exc:  # noqa: BLE001 - re-raised with its cause attached
        raise RuntimeError(f"reflection window configuration is unreadable: {exc}") from exc
    section = ((bundle or {}).get("config") or {}).get("reflection_window")
    if not isinstance(section, dict):
        raise RuntimeError(
            "reflection window configuration has no 'reflection_window' section"
        )
    return dict(section)


def configured_window_size() -> int:
    """The live configured cohort size, read from governed configuration. Fail-hard."""
    section = window_configuration()
    if "window_size" not in section:
        raise RuntimeError("reflection window configuration names no window_size")
    raw = section["window_size"]
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise RuntimeError(
            f"reflection window configuration window_size {raw!r} is not a positive integer"
        )
    return raw


def window_configuration_fingerprint() -> str:
    """The identity of the configuration a window collects under (sha256 of its section)."""
    from schema.continuity_json import content_hash

    return content_hash({"subsystem": "reflection_window", "config": window_configuration()})


def trajectory_revision(trajectory: dict[str, Any] | None) -> str:
    """A revision reference for one trajectory snapshot: its id and its content digest.

    ``trajectory:none`` means the read succeeded and there is no trajectory. A read that
    FAILED is :func:`trajectory_unavailable`, never this — "could not read" and "there is
    nothing" must not share a value (AGENTS.md section 2.1).
    """
    from schema.continuity_json import content_hash

    snapshot = dict(trajectory or {})
    if not snapshot:
        return "trajectory:none"
    identity = str(snapshot.get("trajectory_id") or "unidentified")[:96]
    return f"trajectory:{identity}:{content_hash(snapshot)}"


def trajectory_unavailable(exc: BaseException) -> str:
    """The explicit revision reference of a trajectory read that failed, with its cause."""
    return f"unavailable: {type(exc).__name__}: {exc}"[:256]


def current_trajectory_revision() -> tuple[dict[str, Any], str]:
    """The lobe-9 trajectory now, and its revision reference (or the read's failure)."""
    try:
        from pressure_loop.temporal_store import read_trajectory

        snapshot = dict(read_trajectory() or {})
    except Exception as exc:  # noqa: BLE001 - recorded as an explicit unavailable revision
        _LOG.warning("lobe-9 trajectory read failed; recorded as unavailable: %s", exc)
        return {}, trajectory_unavailable(exc)
    return snapshot, trajectory_revision(snapshot)


WINDOW_SIZE = configured_window_size()
STATUS_COLLECTING = "collecting"
STATUS_LOCKED = "locked"
VALID_STATUSES = frozenset({STATUS_COLLECTING, STATUS_LOCKED})
_LEGACY_EPISODE_STATE_FIELDS = frozenset({"episode_bodies", "episode_mastery_deltas"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def reflection_window_path() -> Path:
    return _resolve_path("guardian/state/reflection_window.json")


def current_trajectory_path() -> Path:
    return _resolve_path("guardian/state/current_trajectory.json")


def staged_window_path(window_path: str | Path | None = None) -> Path:
    """The durable STAGING file — the double buffer's back half (Phase 4 cut 10).

    Derived from the ACTIVE window's path rather than hard-coded, because the two
    files are one structure: whoever redirects the collecting window (the whole
    test suite does, session-wide) must redirect its staging with it, or a test's
    locked window becomes durable pressure in the next test's process.
    """
    return _resolve_path(window_path or reflection_window_path()).with_name(
        "reflection_window_staged.json"
    )


# The staged FIFO is READ-MODIFY-WRITTEN from two lanes at once (cut 10 review).
# The stage side runs inside a closing episode's terminus — on the chat, agent or
# meta lane thread, under the cognition lock — while the unstage side runs in the
# WINDOW lane's unlocked model-pass middle, which is where the reflection fold
# spends its seconds. Nothing else serialized them, so the two interleavings both
# corrupted the file:
#
#   stage loads [W1] → unstage saves []          → stage saves [W1, W2]
#       W1 is RESURRECTED and reflected + window-consolidated a second time,
#       which is exactly the mastery/identity/trajectory double-count the
#       unstage-as-commit-point exists to prevent;
#   unstage loads [W1] → stage saves [W1, W2]    → unstage saves []
#       W2 is LOST — ten closed episodes with no record anywhere, and the
#       collecting half has already moved on, so nothing ever re-stages them.
#
# One process-wide lock, held across each mutator's whole load→modify→save, is
# the entire fix: every writer is in this process (the lanes are threads) and the
# critical section is one small file. It is always the INNERMOST lock — the stage
# side already holds the store lock and the cognition lock when it arrives here,
# and nothing inside these sections acquires anything (file IO only) — so the
# order is only ever (cognition → store → staged) and no cycle is possible.
_STAGED_LOCK = threading.RLock()


@contextmanager
def staged_window_lock() -> Iterator[None]:
    """Hold the staged FIFO for a whole read-modify-write.

    Exported for the one mutator that does not live here — the kill switch's
    drain (``pressure_loop.window_lane.archive_unreflected``) archives every
    staged row and then clears the file, and a stage landing between those two
    steps would be archived by neither.
    """
    with _STAGED_LOCK:
        yield


def load_staged_windows(path: str | Path | None = None) -> list[ReflectionWindow]:
    """The staged FIFO, oldest first. Fail-hard on corruption.

    A staged window is TEN CLOSED EPISODES the mind has not reflected on yet.
    Reading a corrupt staging file as "nothing staged" would silently discard
    them and, worse, present as an idle mind — so the JSON error propagates
    exactly as ``load_reflection_window``'s does.
    """
    resolved = _resolve_path(path or staged_window_path())
    if not resolved.is_file():
        return []
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"corrupted staged reflection windows: {exc}") from exc
    except FileNotFoundError:
        return []
    if not isinstance(raw, list):
        raise RuntimeError("staged reflection window root must be a list")
    out: list[ReflectionWindow] = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"staged reflection window root row {index} must be an object"
            )
        window = ReflectionWindow.from_dict(row)
        if not window.window_id:
            raise RuntimeError(f"staged reflection window row {index} is missing window_id")
        out.append(window)
    return out


def save_staged_windows(
    windows: list[ReflectionWindow],
    path: str | Path | None = None,
) -> None:
    """Persist the staged FIFO (fail-hard, ATOMIC) — or delete it when empty.

    Deleting on empty is what makes the pressure read cheap: the gather asks
    "does this file exist?" every tick, and the answer is no for all but the
    handful of ticks between a window locking and its reflection landing.

    ``write_text_atomic``, not ``write_text_fsync``, and by that function's own
    stated rule: this store is READ FAIL-HARD (``load_staged_windows`` raises on
    a JSON error rather than reporting an idle mind), and a truncating in-place
    write leaves a half-file behind on two ordinary events. A power cut mid-write
    would brick the appliance — every subsequent tick's ``gather_contributors``
    reads this file and would raise forever. Worse, it does not even take a
    crash: the tick thread reads the staged file with no lock while a lane thread
    is writing it, so an in-place truncate is a live window in which a perfectly
    healthy appliance reads its own half-written file and dies. A temp sibling
    plus ``os.replace`` closes both — the reader sees the old complete file or
    the new complete file and never anything in between, which is also why the
    read side deliberately does NOT take ``_STAGED_LOCK`` (the tick must never
    queue behind a lane's fsync).
    """
    from pressure_loop.durable_write import write_text_atomic

    resolved = _resolve_path(path or staged_window_path())
    if not windows:
        try:
            if resolved.is_file():
                resolved.unlink()
        except OSError as exc:
            raise RuntimeError(f"staged reflection windows could not be cleared: {exc}") from exc
        return
    payload = (
        json.dumps(
            [w.to_dict() for w in windows],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    write_text_atomic(resolved, payload)


def stage_reflection_window(
    window: ReflectionWindow,
    path: str | Path | None = None,
) -> bool:
    """Append one locked window to the durable staging FIFO. Idempotent by id.

    False means an identical window was already staged — the only way to reach
    that is a crash between this write and the active-window clear, replayed on
    the next lock. Re-staging must merge, never duplicate: the window would
    otherwise be reflected (and window-consolidated) twice.

    The whole load→append→save runs under ``_STAGED_LOCK``: the window lane can
    be unstaging a reflected window on its own thread at this exact instant (see
    the lock's comment), and a read taken before that save with a write landing
    after it resurrects a window the mind has already consolidated.
    """
    resolved = _resolve_path(path or staged_window_path())
    with _STAGED_LOCK:
        staged = load_staged_windows(resolved)
        if any(w.window_id == window.window_id for w in staged):
            return False
        staged.append(window)
        save_staged_windows(staged, resolved)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "stage_reflection_window",
            path=str(resolved),
            ok=True,
            window_id=window.window_id,
            episode_count=window.episode_count,
            staged_depth=len(staged),
        )
    except Exception:
        pass
    return True


def unstage_reflection_window(window_id: str, path: str | Path | None = None) -> bool:
    """Drop one reflected window from staging. THE commit point of cut 10.

    Everything before this call is replayable: the archive is keyed by window_id
    so a re-reflection overwrites rather than duplicates. Everything after it —
    the window-scope DeepSleep — is NOT replayable, which is why the removal
    happens between them (operator plan: "staged file deleted only after archive
    commits"). A crash on either side costs one reflection or one consolidation;
    neither ever double-counts mastery.

    Under ``_STAGED_LOCK`` for the whole read-modify-write: a tenth episode
    closing on another lane stages its window into this same file while the fold
    that leads here is still running, and an unlocked rewrite of a list read
    before that stage would drop the new window on the floor.
    """
    resolved = _resolve_path(path or staged_window_path())
    wid = str(window_id or "")
    with _STAGED_LOCK:
        staged = load_staged_windows(resolved)
        remaining = [w for w in staged if w.window_id != wid]
        if len(remaining) == len(staged):
            return False
        save_staged_windows(remaining, resolved)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "unstage_reflection_window",
            path=str(resolved),
            ok=True,
            window_id=wid,
            staged_depth=len(remaining),
        )
    except Exception:
        pass
    return True


def reflection_window_archive_dir() -> Path:
    return _resolve_path("memory/600_journal/reflection_windows")


def reflection_lease_path() -> Path:
    """Crash marker: Omega window reflection in flight (cleared on success/quarantine)."""
    return _resolve_path("guardian/state/reflection_lease.json")


def read_reflection_lease(
    path: str | Path | None = None,
) -> Optional[dict[str, Any]]:
    resolved = _resolve_path(path or reflection_lease_path())
    try:
        if not resolved.is_file():
            return None
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        return dict(raw) if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_reflection_lease(window_id: str, *, path: str | Path | None = None) -> None:
    from pressure_loop.durable_write import write_text_fsync

    resolved = _resolve_path(path or reflection_lease_path())
    payload = (
        json.dumps(
            {
                "window_id": str(window_id),
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    write_text_fsync(resolved, payload)


def clear_reflection_lease(path: str | Path | None = None) -> None:
    resolved = _resolve_path(path or reflection_lease_path())
    try:
        if resolved.is_file():
            resolved.unlink()
    except OSError as exc:
        _LOG.debug("reflection lease clear failed (non-fatal): %s", exc)


def reconcile_stale_reflection_on_boot(
    *,
    window_path: str | Path | None = None,
    lease_path: str | Path | None = None,
) -> Optional[dict[str, Any]]:
    """Archive and clear a locked window left by interrupted/failed Omega reflection.

    Ensures reboot never hands ``ignite_episode`` a blocking pending window.
    Episode bodies are preserved under ``memory/600_journal/reflection_windows/``.
    """
    wp = _resolve_path(window_path or reflection_window_path())
    lp = _resolve_path(lease_path or reflection_lease_path())
    window = load_reflection_window(wp)
    lease = read_reflection_lease(lp)

    if window is None:
        if lease:
            clear_reflection_lease(lp)
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "reconcile_reflection_window",
                    ok=True,
                    reason="orphan_lease_cleared",
                )
            except Exception:
                pass
            return {"reason": "orphan_lease_cleared"}
        return None

    if not window.is_ready_for_reflection() and not lease:
        return None

    # Cut 10: a locked window that is ALREADY STAGED is not stale — it is work
    # waiting for the window lane. The only way to see one here is a crash
    # between the staging write and the active-window clear, and archiving it
    # unreflected would throw away the ten episodes the reflection is for. Clear
    # the stale active copy and leave the staged row to ignite.
    if any(w.window_id == window.window_id for w in load_staged_windows(staged_window_path(wp))):
        clear_reflection_window(wp)
        clear_reflection_lease(lp)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "reconcile_reflection_window",
                ok=True,
                window_id=window.window_id,
                episode_count=window.episode_count,
                reason="already_staged_for_reflection",
            )
        except Exception:
            pass
        return {
            "window_id": window.window_id,
            "episode_count": window.episode_count,
            "reason": "already_staged_for_reflection",
        }

    # WO-09 staging recovery: a crash AFTER the locked window reached disk and BEFORE the
    # staging write leaves a complete, sealed-at-membership cohort that nothing staged. It used
    # to be archived unreflected here, so ten real episodes were filed away and never looked
    # back over. With the window lane on, it is staged exactly as the lock would have staged it
    # (idempotent by window id), then the active copy is dropped — one recoverable cohort. The
    # kill switch keeps its drain semantics and still archives unreflected below.
    from pressure_loop.window_lane import window_lane_enabled

    if not lease and window.is_ready_for_reflection() and window_lane_enabled():
        stage_reflection_window(window, staged_window_path(wp))
        clear_reflection_window(wp)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "reconcile_reflection_window",
                ok=True,
                window_id=window.window_id,
                episode_count=window.episode_count,
                reason="staged_on_recovery",
            )
        except Exception:
            pass
        return {
            "window_id": window.window_id,
            "episode_count": window.episode_count,
            "reason": "staged_on_recovery",
        }

    reason = "locked_window_pending"
    if lease:
        reason = "reflection_lease_present"
    outcome = {
        "status": "interrupted",
        "reason": reason,
        "lease": lease,
        "reconciled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    archive_reflection_window(window, omega_outcome=outcome)
    clear_reflection_window(wp)
    clear_reflection_lease(lp)
    try:
        from guardian.terminal_feedback import emit_data
        from guardian.data_logger import emit_event

        emit_data(
            "pressure",
            "reconcile_reflection_window",
            ok=True,
            window_id=window.window_id,
            episode_count=window.episode_count,
            reason=reason,
        )
        emit_event(
            "omega.pressure",
            "reconcile_reflection_window",
            ok=True,
            window_id=window.window_id,
            reason=reason,
        )
    except Exception:
        pass
    _LOG.warning(
        "reconciled stale reflection window %s (%d episodes, reason=%s)",
        window.window_id,
        window.episode_count,
        reason,
    )
    return {
        "window_id": window.window_id,
        "episode_count": window.episode_count,
        "reason": reason,
    }


_CUTOFF_FIELDS = ("clock_epoch_id", "commit_seq", "closed_at")


def _checked_member_refs(raw: Any, episodes: list[str]) -> dict[str, dict[str, Any]]:
    """Validate pinned member references: exact CanonicalRefs, each naming a member."""
    from schema.continuity_json import CanonicalRef

    if not isinstance(raw, dict):
        raise RuntimeError("reflection checkpoint member_refs must be an object")
    members = set(episodes)
    checked: dict[str, dict[str, Any]] = {}
    for episode_id, ref in raw.items():
        if str(episode_id) not in members:
            raise RuntimeError(
                f"reflection checkpoint pins a reference for {episode_id!r}, which is not a member"
            )
        checked[str(episode_id)] = CanonicalRef.coerce(
            ref, where=f"reflection_window.member_refs[{episode_id}]"
        ).to_dict()
    return checked


def _checked_cutoff(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError("reflection checkpoint cutoff must be an object")
    if not raw:
        return {}
    missing = [name for name in _CUTOFF_FIELDS if name not in raw]
    if missing:
        raise RuntimeError(f"reflection checkpoint cutoff is missing {missing}")
    seq = raw["commit_seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise RuntimeError(f"reflection checkpoint cutoff commit_seq {seq!r} is not a stamp")
    return {
        "clock_epoch_id": str(raw["clock_epoch_id"]),
        "commit_seq": seq,
        "closed_at": str(raw["closed_at"]),
    }


@dataclass
class ReflectionWindow:
    window_id: str
    started_at_episode: str
    trajectory_snapshot: dict[str, Any]
    episodes: list[str] = field(default_factory=list)
    episode_bodies: list[dict[str, Any]] = field(default_factory=list)
    cascade_status_by_episode: dict[str, str] = field(default_factory=dict)
    episode_mastery_deltas: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_COLLECTING
    locked_at: str = ""
    # WO-09 — exact references and control metadata, all durable:
    #   member_refs         episode id -> the CanonicalRef the commit region bound when that
    #                       episode joined (never looked up again by episode id later)
    #   configuration_fingerprint / trajectory_revision_at_start — recorded at open
    #   trajectory_revision_at_cutoff / cutoff — recorded when the tenth member locks it
    #   manifest            the sealed WindowManifest, once a window episode has sealed it
    member_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    configuration_fingerprint: str = ""
    trajectory_revision_at_start: str = ""
    trajectory_revision_at_cutoff: str = ""
    cutoff: dict[str, Any] = field(default_factory=dict)
    manifest: Any = None
    # Transient hydration cache (like episode_bodies): the exact sealed records.
    episode_records: list[Any] = field(default_factory=list)

    @property
    def episode_count(self) -> int:
        return len(self.episodes)

    def to_dict(self) -> dict[str, Any]:
        # This file is a durable reflection checkpoint, not a second episode
        # store. Keep this whitelist explicit: a new dataclass field must not
        # become durable episode state by accident. Full bodies, sealed records
        # and mastery effects are transient hydration caches rebuilt from the
        # exact canonical lobe-9 records when a window is consumed.
        out = {
            "window_id": self.window_id,
            "started_at_episode": self.started_at_episode,
            "trajectory_snapshot": dict(self.trajectory_snapshot),
            "episodes": [str(episode_id) for episode_id in self.episodes],
            "cascade_status_by_episode": {
                str(episode_id): str(status)
                for episode_id, status in self.cascade_status_by_episode.items()
            },
            "status": self.status,
            "locked_at": self.locked_at,
            "episode_count": self.episode_count,
            "member_refs": {
                str(episode_id): dict(ref) for episode_id, ref in self.member_refs.items()
            },
            "configuration_fingerprint": self.configuration_fingerprint,
            "trajectory_revision_at_start": self.trajectory_revision_at_start,
            "trajectory_revision_at_cutoff": self.trajectory_revision_at_cutoff,
            "cutoff": dict(self.cutoff),
        }
        if self.manifest is not None:
            out["manifest"] = self.manifest.to_dict()
        return out

    def cascade_status_for(self, episode_id: str) -> str:
        return str(self.cascade_status_by_episode.get(str(episode_id)) or "completed")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReflectionWindow":
        legacy_fields = sorted(_LEGACY_EPISODE_STATE_FIELDS.intersection(d))
        if legacy_fields:
            fields = ", ".join(legacy_fields)
            raise RuntimeError(
                "legacy episode state in reflection checkpoint; "
                f"remove {fields} and hydrate from canonical lobe-9 records"
            )
        status = str(d.get("status") or STATUS_COLLECTING)
        if status not in VALID_STATUSES:
            status = STATUS_COLLECTING
        episodes = d.get("episodes") or []
        if not isinstance(episodes, list):
            episodes = []
        raw_status = d.get("cascade_status_by_episode") or {}
        cascade_status_by_episode = (
            {str(k): str(v) for k, v in raw_status.items()} if isinstance(raw_status, dict) else {}
        )
        snapshot = d.get("trajectory_snapshot")
        members = [str(e) for e in episodes]
        manifest = d.get("manifest")
        if manifest is not None:
            from schema.window_evidence import WindowManifest

            manifest = WindowManifest.from_dict(manifest)
        return cls(
            window_id=str(d.get("window_id") or ""),
            started_at_episode=str(d.get("started_at_episode") or ""),
            trajectory_snapshot=dict(snapshot) if isinstance(snapshot, dict) else {},
            episodes=members,
            cascade_status_by_episode=cascade_status_by_episode,
            status=status,
            locked_at=str(d.get("locked_at") or ""),
            member_refs=_checked_member_refs(d.get("member_refs") or {}, members),
            configuration_fingerprint=str(d.get("configuration_fingerprint") or ""),
            trajectory_revision_at_start=str(d.get("trajectory_revision_at_start") or ""),
            trajectory_revision_at_cutoff=str(d.get("trajectory_revision_at_cutoff") or ""),
            cutoff=_checked_cutoff(d.get("cutoff") or {}),
            manifest=manifest,
        )

    def is_locked(self) -> bool:
        return self.status == STATUS_LOCKED

    def is_ready_for_reflection(self) -> bool:
        return self.is_locked() and self.episode_count >= WINDOW_SIZE


def load_current_trajectory(path: str | Path | None = None) -> dict[str, Any]:
    """Load guardian/state/current_trajectory.json (failure-soft)."""
    resolved = _resolve_path(path or current_trajectory_path())
    try:
        if not resolved.is_file():
            try:
                from guardian.terminal_feedback import emit_data
                from guardian.data_logger import emit_event

                emit_data(
                    "pressure",
                    "load_current_trajectory",
                    path=str(resolved),
                    ok=True,
                    found=False,
                )
                emit_event(
                    "omega.pressure",
                    "load_current_trajectory",
                    path=str(resolved),
                    ok=True,
                    found=False,
                )
            except Exception:
                pass
            return {}
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        result = dict(raw) if isinstance(raw, dict) else {}
        try:
            from guardian.terminal_feedback import emit_data
            from guardian.data_logger import emit_event

            emit_data(
                "pressure",
                "load_current_trajectory",
                path=str(resolved),
                ok=True,
                found=True,
            )
            emit_event(
                "omega.pressure",
                "load_current_trajectory",
                path=str(resolved),
                ok=True,
                found=True,
            )
        except Exception:
            pass
        return result
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("current_trajectory load failed (non-fatal): %s", exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "load_current_trajectory",
                path=str(resolved),
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass
        return {}


def save_current_trajectory(trajectory: dict[str, Any], path: str | Path | None = None) -> None:
    """Persist guardian/state/current_trajectory.json (failure-soft)."""
    resolved = _resolve_path(path or current_trajectory_path())
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(trajectory or {})
        resolved.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            from guardian.terminal_feedback import emit_data
            from guardian.data_logger import emit_event

            emit_data(
                "pressure",
                "save_current_trajectory",
                path=str(resolved),
                ok=True,
            )
            emit_event(
                "omega.pressure",
                "save_current_trajectory",
                path=str(resolved),
                ok=True,
            )
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("current_trajectory save failed (non-fatal): %s", exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "save_current_trajectory",
                path=str(resolved),
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass


def load_reflection_window(
    path: str | Path | None = None,
) -> Optional[ReflectionWindow]:
    """Load active reflection window from disk."""
    resolved = _resolve_path(path or reflection_window_path())
    try:
        if not resolved.is_file():
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "load_reflection_window",
                    path=str(resolved),
                    ok=True,
                    found=False,
                )
            except Exception:
                pass
            return None
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"corrupted reflection window: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("reflection window root must be an object")
        window = ReflectionWindow.from_dict(raw)
        if not window.window_id:
            raise RuntimeError("reflection window is missing window_id")
        try:
            from guardian.terminal_feedback import emit_data
            from guardian.data_logger import emit_event

            emit_data(
                "pressure",
                "load_reflection_window",
                path=str(resolved),
                ok=True,
                window_id=window.window_id,
                episode_count=window.episode_count,
                status=window.status,
            )
            emit_event(
                "omega.pressure",
                "load_reflection_window",
                path=str(resolved),
                ok=True,
                window_id=window.window_id,
            )
        except Exception:
            pass
        return window
    except FileNotFoundError:
        return None


def save_reflection_window(window: ReflectionWindow, path: str | Path | None = None) -> None:
    """Persist active reflection window (fail-hard, fsynced)."""
    from pressure_loop.durable_write import write_text_fsync

    resolved = _resolve_path(path or reflection_window_path())
    payload = json.dumps(window.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        write_text_fsync(resolved, payload)
        try:
            from guardian.terminal_feedback import emit_data
            from guardian.data_logger import emit_event

            emit_data(
                "pressure",
                "save_reflection_window",
                path=str(resolved),
                ok=True,
                window_id=window.window_id,
                episode_count=window.episode_count,
                status=window.status,
            )
            emit_event(
                "omega.pressure",
                "save_reflection_window",
                path=str(resolved),
                ok=True,
                window_id=window.window_id,
            )
        except Exception:
            pass
    except Exception as exc:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "save_reflection_window",
                path=str(resolved),
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass
        raise


def clear_reflection_window(path: str | Path | None = None) -> None:
    """Remove active reflection window file after archive (failure-soft)."""
    resolved = _resolve_path(path or reflection_window_path())
    try:
        removed = resolved.is_file()
        if removed:
            resolved.unlink()
        try:
            from guardian.terminal_feedback import emit_data
            from guardian.data_logger import emit_event

            emit_data(
                "pressure",
                "clear_reflection_window",
                path=str(resolved),
                ok=True,
                removed=removed,
            )
            emit_event(
                "omega.pressure",
                "clear_reflection_window",
                path=str(resolved),
                ok=True,
                removed=removed,
            )
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("reflection_window clear failed (non-fatal): %s", exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "clear_reflection_window",
                path=str(resolved),
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass


def _archive_payload_sort_key(path: Path) -> tuple[float, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        window = payload.get("window") if isinstance(payload, dict) else None
        window_id = ""
        if isinstance(window, dict):
            window_id = str(window.get("window_id") or "")
        return (path.stat().st_mtime, window_id)
    except Exception:
        return (0.0, "")


def load_latest_archived_reflection_window(
    archive_dir: str | Path | None = None,
) -> ReflectionWindow | None:
    """Return the most recently archived reflection window, if any."""
    resolved = _resolve_path(archive_dir or reflection_window_archive_dir())
    if not resolved.is_dir():
        return None
    candidates = sorted(
        (p for p in resolved.glob("*.json") if p.is_file()),
        key=_archive_payload_sort_key,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            window_dict = payload.get("window") if isinstance(payload, dict) else None
            if isinstance(window_dict, dict):
                return ReflectionWindow.from_dict(window_dict)
        except Exception:
            continue
    return None


def _write_archive_payload(
    window_id: str,
    payload: dict[str, Any],
    archive_dir: str | Path | None,
) -> Path | None:
    """Write one archive record IDEMPOTENTLY and VERIFIABLY. Path, or None.

    Both adverbs are load-bearing for the window lane's commit point (RISK-9):

    IDEMPOTENT — keyed by ``window_id`` and published with ``os.replace``, so a
    retry after a crash overwrites rather than duplicating, and a reader on a
    parallel tick sees either the old whole file or the new whole file. The
    previous in-place ``write_text`` could leave a truncated record that
    ``is_file()`` happily called "landed".

    VERIFIABLE — the payload is serialized BEFORE anything touches the
    filesystem (an unserializable outcome fails here, with nothing half-written
    on disk), and the published file is read back and re-parsed before this
    returns a path. The caller unstages ten episodes on the strength of that
    answer; it must be an observation, not an assumption.
    """
    tmp: Path | None = None
    try:
        resolved = _resolve_path(archive_dir or reflection_window_archive_dir())
        # Serialize first: a payload that cannot be encoded must not get as far
        # as creating a file.
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        resolved.mkdir(parents=True, exist_ok=True)
        out = resolved / f"{window_id}.json"
        tmp = resolved / f".{window_id}.json.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, out)
        tmp = None
        # Read back. "The write did not raise" is not the same claim as "ten
        # episodes are on disk" on a full or failing volume.
        json.loads(out.read_text(encoding="utf-8"))
        return out
    except Exception as exc:  # noqa: BLE001 -- failure-soft by contract
        _LOG.warning("reflection_window archive did not land for %s: %s", window_id, exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        return None


def archive_reflection_window(
    window: ReflectionWindow,
    *,
    omega_outcome: dict[str, Any] | None = None,
    archive_dir: str | Path | None = None,
) -> Path | None:
    """Archive a completed window (failure-soft).

    Returns the archive path on success, None when the record did not land.
    The return value is the contract the window lane's commit point reads —
    it used to have to guess with ``is_file()`` after a function that returned
    nothing whether it worked or not.
    """
    out = _write_archive_payload(
        window.window_id,
        {
            "window": window.to_dict(),
            "omega_outcome": dict(omega_outcome or {}),
        },
        archive_dir,
    )
    try:
        from guardian.terminal_feedback import emit_data
        from guardian.data_logger import emit_event

        emit_data(
            "pressure",
            "archive_reflection_window",
            path=str(out or ""),
            ok=out is not None,
            window_id=window.window_id,
            episode_count=window.episode_count,
        )
        emit_event(
            "omega.pressure",
            "archive_reflection_window",
            path=str(out or ""),
            ok=out is not None,
            window_id=window.window_id,
        )
    except Exception:
        pass
    return out


def archive_failed_reflection_window(
    window: ReflectionWindow,
    *,
    reason: str,
    omega_outcome: dict[str, Any] | None = None,
    archive_dir: str | Path | None = None,
) -> Path | None:
    """Record an HONEST failed archive for *window*. Path, or None.

    The fallback under ``commit_reflection`` (RISK-9). When the full record
    cannot be written, the choice is not "crash or lose ten episodes" — it is
    "write something smaller and true". This payload carries the window id, the
    episode ids, the reflection's status and the reason the full archive failed,
    and drops the bulk that most plausibly caused the failure: the ten nested
    episode bodies and the trajectory snapshot (an oversized, disk-full or
    unserializable payload is the failure mode this is the answer to).

    It occupies the SAME ``{window_id}.json`` key. One record per window, always
    — a later successful re-archive of the same window overwrites it, and the
    archive never grows two conflicting stories about one window.
    """
    payload = {
        "window": {
            "window_id": window.window_id,
            "started_at_episode": str(getattr(window, "started_at_episode", "") or ""),
            "episodes": [str(e) for e in (getattr(window, "episodes", None) or [])],
            "episode_count": int(getattr(window, "episode_count", 0) or 0),
            "status": str(getattr(window, "status", "") or ""),
        },
        "omega_outcome": {
            "status": str(dict(omega_outcome or {}).get("status") or "unknown"),
        },
        "archive_status": "failed",
        "archive_failure_reason": str(reason or "unknown"),
        "archived_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    out = _write_archive_payload(window.window_id, payload, archive_dir)
    try:
        from guardian.terminal_feedback import emit_data
        from guardian.data_logger import emit_event

        emit_data(
            "pressure",
            "archive_reflection_window_failed",
            path=str(out or ""),
            ok=out is not None,
            window_id=window.window_id,
            episode_count=int(getattr(window, "episode_count", 0) or 0),
            reason=str(reason or "unknown"),
        )
        emit_event(
            "omega.pressure",
            "archive_reflection_window_failed",
            path=str(out or ""),
            ok=out is not None,
            window_id=window.window_id,
            reason=str(reason or "unknown"),
        )
    except Exception:
        pass
    return out


def open_reflection_window(
    started_at_episode: str,
    *,
    trajectory_snapshot: dict[str, Any] | None = None,
    trajectory_path: str | Path | None = None,
) -> ReflectionWindow:
    """Create a new collecting window with an immutable trajectory snapshot.

    WO-09: the window records the configuration it collects under and the revision of the
    trajectory it starts from. A trajectory read that FAILS is recorded as an explicit
    ``unavailable:`` revision with its cause — never as an empty trajectory.
    """
    if trajectory_snapshot is not None:
        snapshot = dict(trajectory_snapshot)
        revision = trajectory_revision(snapshot)
    else:
        snapshot, revision = current_trajectory_revision()
    return ReflectionWindow(
        window_id=f"window_{uuid.uuid4().hex[:12]}",
        started_at_episode=str(started_at_episode),
        trajectory_snapshot=snapshot,
        episodes=[],
        status=STATUS_COLLECTING,
        configuration_fingerprint=window_configuration_fingerprint(),
        trajectory_revision_at_start=revision,
    )


def append_episode_to_window(
    window: ReflectionWindow,
    episode_id: str,
    *,
    cascade_status: str = "completed",
    body: dict[str, Any] | None = None,
    record_ref: Any = None,
    cutoff: dict[str, Any] | None = None,
) -> ReflectionWindow:
    """Append one episode; lock at WINDOW_SIZE (no 11th append).

    WO-09: ``record_ref`` is the CanonicalRef the commit region bound for this episode's
    sealed record, pinned here as the member joins — so the cohort is never re-derived from
    whatever record carries the episode id later. A member appended without one (only a
    synthetic caller does that) can never be sealed; the seal names it. ``cutoff`` is this
    member's finish-line position and observed close time, kept only by the append that locks
    the window: it is where the cohort's replay cutoff comes from.
    """
    if window.is_locked():
        return window
    if len(window.episodes) >= WINDOW_SIZE:
        window.status = STATUS_LOCKED
        if not window.locked_at:
            window.locked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return window

    eid = str(episode_id)
    pinned = None
    if record_ref is not None:
        from memory.episode_record_seal import episode_record_id
        from schema.continuity_json import CanonicalRef

        pinned = CanonicalRef.coerce(record_ref, where=f"append_episode_to_window[{eid}]")
        if pinned.record_id != episode_record_id(eid):
            raise ValueError(
                f"episode {eid!r} cannot join a window under {pinned.record_id!r}: a member is "
                "pinned only to its own sealed record"
            )
    window.episodes.append(eid)
    if body is not None:
        window.episode_bodies.append(dict(body))
    window.cascade_status_by_episode[eid] = str(cascade_status or "completed")
    if pinned is not None:
        window.member_refs[eid] = pinned.to_dict()
    if len(window.episodes) >= WINDOW_SIZE:
        window.status = STATUS_LOCKED
        if not window.locked_at:
            window.locked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if cutoff:
            window.cutoff = _checked_cutoff(dict(cutoff))
        # The trajectory that held when the cohort filled — recorded now, never recomputed
        # from whatever trajectory is current when the window is finally read.
        if not window.trajectory_revision_at_cutoff:
            window.trajectory_revision_at_cutoff = current_trajectory_revision()[1]
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "append_reflection_window",
            episode_id=eid,
            ok=True,
            window_id=window.window_id,
            episode_count=window.episode_count,
            status=window.status,
            cascade_status=str(cascade_status or "completed"),
        )
    except Exception:
        pass
    return window
