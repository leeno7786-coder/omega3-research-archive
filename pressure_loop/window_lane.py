"""Durable reflection-work context (historical module name: window_lane).

What this cut fixes is not isolation, it is AMNESIA. The reflection window has
been collecting ten enriched episode bodies per fill since Model C, locking
itself at the tenth close — and then ``cognition_chain._archive_locked_
reflection_window`` filed it away UNREFLECTED, with the literal outcome
``{"status": "archived_by_per_episode_cycle"}``. Every machine part of the
reflection existed (``stage_episode_for_window_reflection``,
``consume_window_ready``, ``build_deepsleep_input_for_window``) and none of it
had a production caller. The mind has never once looked back over ten episodes.

The shape, exactly as ratified:

    tenth close                      next FCFS admission, any P0-P3
    ───────────                      ─────────────────────────────
    window locks                     window_reflection_ready ignites
    │                                → carried staged window
    ├─ (1) stage it durably    ────► → canonical cascade + Omega window fold
    │      (the DOUBLE BUFFER)       → archive with the REAL outcome
    └─ (2) drop the active copy      → UNSTAGE  ← the commit point
       a fresh window collects       → window-scope DeepSleep
       from the very next close      → close

**The double buffer is why collection never blocks on reflection.** The locked
window moves to ``guardian/state/reflection_window_staged.json`` and the active
window file is dropped, so the eleventh episode's close opens a fresh collecting
window with no idea a reflection is in flight. Before this cut the locked window
sat in the ONE window file until the next episode's ignition seam archived it;
episodes 11+ were silently dropped on the floor by ``append_episode_to_window``'s
"no 11th append" rule for as long as it sat there.

**The staged file IS the pressure.** There is no window operator line and there
never will be — a queue would be a second durable record of the same fact. The
gather synthesizes ``window_reflection_ready`` for exactly as long as staging is
non-empty, the omega-levels pattern: durable state on disk, projected into the
tick, consumed by the episode that acts on it.

**Exactly-once, stated honestly.** The staged-window identity and shared-queue
lease prevent two episodes from owning one reflection at once; no dedicated
worker is involved. Across a CRASH the
guarantee is at-most-once for the irreversible half and at-least-once for the
replayable half, split at ``unstage_reflection_window``: the archive before it is
keyed by window_id (a replay overwrites), the window-scope DeepSleep after it is
not replayable (mastery/identity/trajectory deltas would double), so the row is
dropped between them. A crash before the archive re-reflects the window; a crash
after the unstage loses that window's consolidation and never doubles it.

**Kill switch ``OMEGA_WINDOW_LANE=0``** stops the mind MINTING window work and
nothing else. The staging write and the contributor are gated; the restore, the
fold, the archive and the unstage are NOT. That asymmetry is deliberate and is
the cut-9 review's finding applied before it could recur: a switch that also
gated the consumption side would strand a window already staged when the switch
flipped — reflected by nobody, drained by nobody. With the switch off the lock
seam archives unreflected verbatim (the pre-cut-10 outcome string, unchanged)
and drains any row staged before the flip, so the switch is a real drain valve.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

WINDOW_REFLECTION_READY = "window_reflection_ready"
from pressure_loop.contributors import Contributor, make_contributor

log = logging.getLogger("omega.pressure_loop.window_lane")

_OFF = {"0", "false", "no"}

# The episode.meta key a WINDOW episode carries: "these ten episodes are mine to
# reflect on." Named like agent_lane's DISPATCH_META_KEY for the same reason —
# every later stage reads one place instead of re-scanning the pressure snapshot.
WINDOW_META_KEY = "window_lane_reflection"

# The omega_outcome status an unreflected archive carries. Kept VERBATIM from the
# pre-cut-10 ignition-seam archive so a switched-off appliance writes the exact
# archive rows it always did, and so anything reading the archive history can
# still tell "nobody reflected on this" from "the mind reflected on this".
UNREFLECTED_STATUS = "archived_by_per_episode_cycle"


def window_lane_enabled() -> bool:
    """Kill switch: OMEGA_WINDOW_LANE=0 → locked windows archive unreflected.

    Gates MINTING only (see the module docstring): staging and the synthesized
    contributor. Work already staged is always honoured, and the switch-off path
    drains it rather than stranding it.
    """
    return (os.environ.get("OMEGA_WINDOW_LANE") or "1").strip().lower() not in _OFF


def _emit(event: str, **fields: Any) -> None:
    """Observability only — never the reason a reflection succeeds or fails."""
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("pressure", event, **fields)
    except Exception:
        log.debug("window lane telemetry failed", exc_info=True)


def _meta(episode) -> Dict[str, Any]:
    return dict(getattr(episode, "meta", None) or {})


def _episode_id(episode) -> str:
    return str(getattr(episode, "episode_id", "") or "").strip()


# ── the collecting side: lock, stage, keep collecting ────────────────────────


def staged_path(window_path: Any = None):
    """The staging file beside ``window_path``'s collecting window."""
    from pressure_loop.reflection_window import staged_window_path

    return staged_window_path(window_path)


def stage_locked_window(window, *, window_path: Any = None) -> bool:
    """Move one locked window into durable staging. Returns False if already there."""
    from pressure_loop.reflection_window import stage_reflection_window

    staged = stage_reflection_window(window, staged_path(window_path))
    _emit(
        "window_lane_stage",
        window_id=str(getattr(window, "window_id", "") or ""),
        summary=(
            f"window {getattr(window, 'window_id', '')} staged for reflection "
            f"({getattr(window, 'episode_count', 0)} episodes)"
        ),
        episode_count=int(getattr(window, "episode_count", 0) or 0),
        staged=bool(staged),
    )
    return staged


def archive_unreflected(window, *, window_path: Any = None) -> None:
    """The kill-switch path: file the window away exactly as cut 9 and earlier did,
    then DRAIN anything staged before the switch flipped so nothing is stranded.

    UNSTAGE ONLY WHAT ARCHIVED. ``archive_reflection_window`` is failure-soft and
    returns ``Path | None``; this used to ignore that and clear the staging file
    unconditionally, so on a full or failing volume every staged window vanished
    with no record anywhere. That is the exact loss RISK-9 refused to accept at
    the window lane's own commit point (``commit_reflection``), and it was still
    reachable here — through the kill switch, which is the lever an operator
    reaches for when something is ALREADY wrong. A safety lever must not be the
    thing that destroys the evidence.

    So a row whose archive did not land stays staged and is owed to the next
    boot, exactly as an uncommitted reflection is. The bound RISK-9 needed does
    not apply: this path runs once when the switch flips, not in a loop.
    """
    from pressure_loop.reflection_window import (
        archive_reflection_window,
        load_staged_windows,
        save_staged_windows,
        staged_window_lock,
    )

    locked_archived = (
        archive_reflection_window(window, omega_outcome={"status": UNREFLECTED_STATUS})
        is not None
    )
    if not locked_archived:
        log.critical(
            "window lane kill switch: the LOCKED window %s could not be archived — "
            "it is not staged, so this is the only notice of it",
            getattr(window, "window_id", "?"),
        )
    path = staged_path(window_path)
    # The drain is a read-modify-write like the other two, and it is the most
    # destructive of them: it REWRITES the file. Held under the staged FIFO's
    # lock so a window locking on another lane between the read and the write is
    # not archived by nobody (cut 10 review).
    with staged_window_lock():
        stranded = load_staged_windows(path)
        if not stranded:
            return
        kept = [
            row
            for row in stranded
            if archive_reflection_window(row, omega_outcome={"status": UNREFLECTED_STATUS})
            is None
        ]
        # Not [] — everything that failed to archive is still owed.
        save_staged_windows(kept, path)
    if kept:
        log.critical(
            "window lane kill switch: %d of %d staged window(s) could not be archived "
            "— left staged rather than dropped; the next boot still owes them",
            len(kept),
            len(stranded),
        )
    _emit(
        "window_lane_drain",
        summary=(
            f"OMEGA_WINDOW_LANE off: {len(stranded) - len(kept)} of {len(stranded)} "
            f"staged window(s) archived unreflected"
        ),
        drained=len(stranded) - len(kept),
        still_staged=len(kept),
        locked_archived=locked_archived,
        ok=not kept and locked_archived,
    )


def staged_windows(*, window_path: Any = None) -> List[Any]:
    from pressure_loop.reflection_window import load_staged_windows

    return load_staged_windows(staged_path(window_path))


def peek_staged_window(*, window_path: Any = None):
    """The oldest staged window without consuming it, or None."""
    rows = staged_windows(window_path=window_path)
    return rows[0] if rows else None


def has_staged_window(*, window_path: Any = None) -> bool:
    """Cheap "is a reflection owed?" — a stat, not a parse, in the common case."""
    return staged_path(window_path).is_file()


def unstage_window(window_id: str, *, window_path: Any = None) -> bool:
    from pressure_loop.reflection_window import unstage_reflection_window

    return unstage_reflection_window(window_id, staged_path(window_path))


# ── the tick side: the staged file projected as pressure ─────────────────────


def pressure_contributor(*, window_path: Any = None) -> Optional[Contributor]:
    """Synthesize ``window_reflection_ready`` while a window awaits reflection.

    ``source="omega"`` — this is the mind's own work (the autonomous-authority
    filter in ``gather_contributors`` admits only operator and omega sources).
    Weight 1.0 preserves the contributor's pressure meaning. Admission remains
    strict FCFS in the shared queue; weight and work kind select no worker.

    Returns None when the switch is off — a stranded staged row is drained by
    the lock seam instead (``archive_unreflected``).
    """
    if not window_lane_enabled():
        return None
    if not has_staged_window(window_path=window_path):
        return None
    window = peek_staged_window(window_path=window_path)
    if window is None:
        return None
    window_id = str(getattr(window, "window_id", "") or "")
    if not window_id:
        return None
    # WO-07: a cohort is released only when every member is READABLE — its sealed record in the
    # recall projection being served. A generation owner publishes after the commit region, so a
    # member can be appended before it is published; releasing then would hand the reflection
    # an episode hydration cannot read. The cohort waits, loudly, and is re-checked every tick.
    from memory.episode_commit_view import window_members_unpublished

    unpublished = window_members_unpublished(
        getattr(window, "episodes", None) or (),
        member_refs=getattr(window, "member_refs", None),
    )
    if unpublished:
        _emit(
            "window_held_unpublished",
            summary=(
                f"window {window_id} waits: {len(unpublished)} member(s) not yet in the served "
                "recall projection"
            ),
            window_id=window_id,
            unpublished=list(unpublished),
        )
        return None
    return make_contributor(
        "omega",
        WINDOW_REFLECTION_READY,
        1.0,
        {
            # The lane is stamped so cut 8's partition never has to re-derive it,
            # and so a reader of the ignition snapshot can see where this was
            # meant to run. lane_for_contributor also knows the TYPE, so the two
            # agree by construction.
            "window_id": window_id,
            "episode_count": int(getattr(window, "episode_count", 0) or 0),
            "cycle_id": f"window:{window_id}",
            # No message: this contributor is not an utterance and there is
            # nothing for an intent inference to read. Global A maps its type
            # straight to REFLECT_WINDOW (global_loop/intent/run.py).
        },
    )


# ── the window episode: carry, reflect, commit ───────────────────────────────


def restore_window(episode) -> Optional[Any]:
    """Lift the staged window onto the episode's meta at open.

    Called once inside the admission seam, beside durable job context restore.
    NOT gated on the kill switch: an episode that was
    already dispatched must always find its work.

    Returns the ReflectionWindow, or None when this is not a window episode.
    """
    if episode.opened_by != WINDOW_REFLECTION_READY:
        return None
    window = peek_staged_window()
    if window is None:
        # Fail-hard. A window episode exists ONLY because the staged file was
        # non-empty when the tick gathered, and nothing but this episode's own
        # terminus ever drains it (single carrier, single lane thread). An empty
        # staging here means the durable record and the ignition disagree, which
        # is a programming error, not a race to be papered over.
        raise RuntimeError(
            "window-lane episode opened with no staged reflection window — "
            "the staged file and the ignition disagree"
        )
    from pressure_loop.temporal_store import hydrate_reflection_window

    # Staging owns ordered episode ids and the exact reference each member joined as
    # (WO-09). Seal the cohort and read exactly those revisions at the consumption
    # boundary; a missing, changed or unpublished member fails here, before the window
    # can be reflected or unstaged.
    window = hydrate_reflection_window(window)
    meta = _meta(episode)
    meta[WINDOW_META_KEY] = {
        "window_id": window.window_id,
        "episode_count": int(window.episode_count),
        "episodes": list(window.episodes),
    }
    manifest = getattr(window, "manifest", None)
    if manifest is not None:
        # The reflection's own record names the exact cohort it read: the digest on its meta,
        # and (WO-10) the member references as an admission event in its sealed record — the
        # host citation that makes it a prior finding about THESE members.
        from memory import episode_sram
        from memory.cognition_capture import capture_window_manifest
        from schema.continuity_json import content_hash

        meta[WINDOW_META_KEY]["manifest_sha256"] = content_hash(manifest.to_dict())
        # A thread that owns no episode records nothing: a window restored outside the admission
        # seam has no episode to attribute the admission to. One bound to a DIFFERENT episode is
        # refused loudly by record_event rather than silently attributed here.
        if episode_sram.bound_episode_id() is not None:
            capture_window_manifest(episode, manifest)
    episode.meta = meta
    # The window object itself rides an attribute, not meta: episode.meta is
    # serialized into the enriched body and the journal row, and ten full episode
    # bodies nested inside an eleventh would multiply the journal by an order of
    # magnitude for a payload that is already durable in staging.
    setattr(episode, "_window_lane_window", window)
    _emit(
        "window_lane_execute",
        episode_id=_episode_id(episode),
        summary=f"reflecting on window {window.window_id} ({window.episode_count} episodes)",
        window_id=window.window_id,
        episode_count=int(window.episode_count),
    )
    return window


def carried_window(episode) -> Optional[Any]:
    """The window this episode must reflect on, or None for an ordinary episode.

    TYPE-CHECKED, not merely present: ``getattr(episode, ..., None)`` is not a
    presence test on a test double — a MagicMock episode auto-creates every
    attribute asked of it, so a bare getattr answered "yes, I carry a window" for
    every mocked episode in the suite and sent an ordinary cascade down the
    reflection path. This work context carries a ReflectionWindow and nothing else.
    """
    from pressure_loop.reflection_window import ReflectionWindow

    window = getattr(episode, "_window_lane_window", None)
    return window if isinstance(window, ReflectionWindow) else None


def is_window_episode(episode) -> bool:
    return carried_window(episode) is not None


def reflection_committed(episode) -> bool:
    """Did this episode's reflection reach the archive AND leave staging?

    The gate on the window-scope terminus. A fold that FAILED leaves the window
    staged for a later retry — and consolidating a window whose reflection never
    happened would apply mastery/identity/trajectory deltas that the retry then
    applies again. So a failed window episode closes through the ordinary
    per-episode terminus (over its own, near-empty body) and the window is
    reflected properly next time.
    """
    return bool(dict(_meta(episode).get(WINDOW_META_KEY) or {}).get("archived"))


def commit_reflection(episode, window, outcome: Dict[str, Any]) -> bool:
    """Archive with the REAL outcome, then unstage. In that order, always.

    TOTAL — returns True/False and never raises. This is cut 10's no-fail ruling
    carried to the last statement that still broke it (RISK-9). The ruling was
    scoped to the reflection MODEL pass; the archive underneath it kept the old
    posture, so a filesystem fault raised out of the window cascade, the window
    stayed staged, the next tick's contributor re-ignited it, and the same fault
    raised again — crash, restage, crash, with no bound on the loop and a whole
    model pass burned per turn. A substrate fault has to be loud, not fatal.

    Three outcomes, in the order they are attempted:

    1. The full archive lands (verified read-back, not ``is_file()``) — unstage,
       and the window-scope terminus consolidates as normal.
    2. It does not, but an HONEST FAILED record does — a small payload naming
       the window, its ten episode ids, the reflection status and the reason.
       Unstage on the strength of THAT: nothing is lost with no record anywhere,
       the reflection outcome itself is real and travels on the episode into
       consolidation, and the loop is broken because the window is gone from
       staging. The archive says plainly that it is degraded.
    3. Neither lands — the volume is genuinely gone. Leave the window staged
       (it is still durable there, which is the whole reason we may decline to
       unstage), emit CRITICAL telemetry, and return False. ``reflection_committed``
       then reads False and the episode closes through the ORDINARY per-episode
       terminus, which is the path already designed for a fold that failed. The
       next window ignition retries and self-heals when the volume returns.

    Only case 3 re-ignites, and case 3 does not crash.
    """
    from pressure_loop.reflection_window import (
        archive_failed_reflection_window,
        archive_reflection_window,
    )

    window_id = str(getattr(window, "window_id", "") or "")
    outcome = dict(outcome or {})
    archived = archive_reflection_window(window, omega_outcome=outcome)
    degraded_reason = ""
    if archived is None:
        degraded_reason = "full reflection archive did not land"
        archived = archive_failed_reflection_window(
            window, reason=degraded_reason, omega_outcome=outcome
        )

    meta = _meta(episode)
    record = dict(meta.get(WINDOW_META_KEY) or {})

    if archived is None:
        record["archived"] = False
        record["status"] = "archive_failed"
        record["archive_error"] = "no archive record could be written"
        meta[WINDOW_META_KEY] = record
        episode.meta = meta
        log.critical(
            "window %s could not be archived AT ALL (neither the full record nor a "
            "failed-archive marker) — leaving it staged and closing this episode "
            "through the ordinary terminus; the reflection substrate is unwritable",
            window_id,
        )
        _emit(
            "window_lane_commit",
            episode_id=_episode_id(episode),
            ok=False,
            summary=(
                f"window {window_id} NOT archived — staged for retry, no crash, "
                "reflection substrate unwritable"
            ),
            window_id=window_id,
            status="archive_failed",
            unstaged=False,
        )
        return False

    unstage_window(window_id)
    record["archived"] = True
    record["status"] = str(outcome.get("status") or "reflected")
    if degraded_reason:
        record["archive_degraded"] = True
        record["archive_error"] = degraded_reason
    meta[WINDOW_META_KEY] = record
    episode.meta = meta
    # CAUSAL EDGE (spec section 9, T11): this reflection OBSERVED the archived window.
    # Recorded AFTER the carrier's meta is put back: the copy taken above would otherwise
    # overwrite it.
    from memory.causal_edges import commit_causal_fact

    commit_causal_fact(episode, kind="observed", source=f"episode:{_episode_id(episode)}",
                       target=f"window:{window_id}",
                       detail={"episode_count": int(getattr(window, "episode_count", 0) or 0)})
    if degraded_reason:
        log.error(
            "window %s archived DEGRADED (%s) — an honest failed-archive record "
            "landed at %s and the window was unstaged; its reflection outcome is "
            "real and still consolidates, but the ten episode bodies are not on disk",
            window_id,
            degraded_reason,
            archived,
        )
    _emit(
        "window_lane_commit",
        episode_id=_episode_id(episode),
        ok=True,
        summary=(
            f"window {window_id} archived with a real reflection and unstaged"
            if not degraded_reason
            else f"window {window_id} archived DEGRADED ({degraded_reason}) and unstaged"
        ),
        window_id=window_id,
        status=record["status"],
        archive_degraded=bool(degraded_reason),
        unstaged=True,
    )
    return True


def carried_outcome(episode) -> Dict[str, Any]:
    """The reflection this episode produced (empty before the fold runs)."""
    outcome = getattr(episode, "_window_lane_outcome", None)
    return dict(outcome) if isinstance(outcome, dict) else {}


def set_carried_outcome(episode, outcome: Dict[str, Any]) -> None:
    setattr(episode, "_window_lane_outcome", dict(outcome or {}))
