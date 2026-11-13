"""CognitiveEpisode + EpisodeStore — S1 scaffold (+ S7 sensory).

Episodes are the convergent truth object that survives the epoch
boundary the SharedState cascade bus cannot cross. Open at pressure-
detected, close at completion; all movements write to the SAME episode
regardless of where they sit in the (legacy) loop chain. That is the
structural cure for the SharedState-as-5-incompatible-roles disease
documented in project_sharedstate_five_incompatible_roles.

S1 scope (this packet) — what this file DOES:
  - Holds the CognitiveEpisode dataclass shape (matches the schema at
    docs/architecture/symphony_episode_schema.json, minimum-required
    field set; richer typing lands in S2+ with movement runners).
  - Holds the EpisodeStore thread-safe holder: at most one active
    episode per orchestrator instance, with open / close / current.

S7 addition — `append_sensory` + `make_sensory_record`:
  - SENSORY_INJECT movement (cue sheet `sensory_inject`) writes onto
    episode.sensory[] instead of orphaning chat on the SharedState
    cascade bus. The append API is a thin canonical writer the brain
    receiver + future SENSORY_INJECT callers route through so the
    schema shape is enforced in ONE place. No-op (returns False) when
    no active episode -- callers are best-effort.

S1 scope — what this file does NOT do:
  - No movement runners (META_A, AUTO, ...). Those land in S2+.
  - No CueDesk / signal pub-sub (S3+).
    - No long-term memory/archive persistence. Episode-start is the only
        save point; DeepSleep is the durable archive owner.
  - No writes to SharedState LAST_* keys for episode-local data.
    Re-routing episode slots through the cascade bus would re-create
    the disease this convergence is curing.
  - Does NOT clear core.shared_state_keys.LAST_OMEGA_TRAJECTORY on
    episode close — trajectory is GUIDANCE (persistent across
    episodes), explicitly carved out by the cue sheet `guidance.keys`
    contract. The deepsleep cascade clears the bus; episode close
    must not.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from core.pipelines import validate_pipeline_id

log = logging.getLogger("omega.pressure_loop.episode")


def _now_iso() -> str:
    # Phase 2 §P2-S20: route every cognitive timestamp through the
    # single temporal authority.
    from core.symphony_time import now_iso

    return now_iso()


# Symphony S7: sensory record source enum -- mirrors
# docs/architecture/symphony_episode_schema.json $.sensory[].source.
# Validation lives in `make_sensory_record` so callers that build
# records by hand still go through one place when they reach
# `EpisodeStore.append_sensory`.
SENSORY_SOURCES: tuple[str, ...] = (
    "user",
    "system",
    "autonomous",
    "curiosity",
)


def make_sensory_record(
    *,
    source: str,
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a schema-valid sensory record dict.

    Per symphony_episode_schema.json $.sensory[] item shape:
        {ts, source, kind, payload}

    Args:
        source: one of SENSORY_SOURCES ("user"/"system"/"autonomous"/
            "curiosity"). Raises ValueError otherwise -- bad source
            is a caller bug, not silent drift.
        kind: free-form short string describing the event
            (e.g. "operator_message", "brain_response", "ask_user").
            Empty/None raises ValueError.
        payload: arbitrary dict (text refs, cycle_id, curiosity_id,
            ...). Defaults to empty dict. Always copied so the caller
            mutating it later cannot retro-actively change the record.
        ts: optional ISO8601 timestamp; defaults to now-UTC.

    Validation lives here (not on append_sensory) because callers
    sometimes want to build the record + inspect it before deciding
    whether to append (e.g. brain_receiver dropping a record when no
    episode is open via the no-op append).
    """
    if source not in SENSORY_SOURCES:
        raise ValueError(f"sensory.source must be one of {SENSORY_SOURCES}, got {source!r}")
    if not (isinstance(kind, str) and kind.strip()):
        raise ValueError("sensory.kind must be a non-empty string")
    return {
        "ts": ts or _now_iso(),
        "source": source,
        "kind": kind.strip(),
        "payload": dict(payload or {}),
    }


def _outcome_axes_of(record: Any) -> Optional[dict]:
    axes = getattr(record, "outcome_axes", None)
    if axes is None and isinstance(record, dict):
        axes = record.get("outcome_axes")
    return dict(axes) if isinstance(axes, dict) else None


def _prior_episode_success(record: Any) -> bool:
    """True only when the recorded TASK outcome is success (WO-06).

    It used to answer with ``cascade_status == "completed"``, so a build that ran the whole
    cascade and failed its acceptance criterion counted as a success. Cascade completion, action
    success, task success and a passing check are different facts; this reads the one it names
    from the row's outcome axes. A row with no axes (written before WO-06) is not a success.
    """
    axes = _outcome_axes_of(record)
    return bool(axes) and axes.get("task_outcome") == "success"


def _member_cutoff(body: Any) -> dict[str, Any]:
    """A window member's replay cutoff, read from the sealed record the terminus committed.

    WO-09: the finish-line position (clock epoch + commit_seq) and the observed close time of
    the episode whose append locks a window. Read from the sealed record itself — the same
    values the member's record carries — so the cutoff cannot drift from the record it names.
    A pinned append without a readable sealed record is a contract failure, not a cohort with
    an invented cutoff.
    """
    from collections.abc import Mapping

    from schema.continuity_json import ContinuityContractError
    from schema.episode_record import EpisodeRecord

    sealed = body.get("episode_record") if isinstance(body, dict) else None
    if not isinstance(sealed, dict):
        raise ContinuityContractError(
            "a window member pinned to a sealed record must carry that record in its body"
        )
    record = EpisodeRecord.from_dict(sealed)
    clock = record.temporal["coverage_clock"]
    closed_at = record.temporal["closed_at"]
    if not isinstance(clock, Mapping):
        raise ContinuityContractError(
            f"episode {record.episode_id!r} has no finish-line position to cut a window at"
        )
    if not closed_at.is_observed:
        raise ContinuityContractError(
            f"episode {record.episode_id!r} has no observed close time to cut a window at"
        )
    return {
        "clock_epoch_id": str(clock["clock_epoch_id"]),
        "commit_seq": int(clock["commit_seq"]),
        "closed_at": str(closed_at.value),
    }


#: Task outcomes that are attempts at the objective, whatever their result — the denominator
#: of a task success rate. not_applicable, cancelled and unknown are counted but excluded.
_ATTEMPTED_TASK_OUTCOMES = frozenset({"success", "failure", "partial", "blocked"})
_PERFORMED_VERIFICATION = frozenset({"passed", "failed", "partial"})


@dataclass(frozen=True)
class OutcomePopulation:
    """Recent episode outcomes as counts, with rates that are ``None`` when nothing qualifies.

    A single success rate cannot say how many cases it rests on, or that it rests on none: a
    neutral 0.5 for an empty journal reads exactly like a measured coin flip. Every count here
    is a population with its own denominator, and an undefined rate stays undefined.
    """

    total: int = 0
    task_successes: int = 0
    task_failures: int = 0
    partial: int = 0
    blocked: int = 0
    cancelled: int = 0
    not_applicable: int = 0
    unknown: int = 0
    verified_successes: int = 0
    unverified_successes: int = 0
    verification_performed: int = 0
    verification_passed: int = 0

    @property
    def attempted(self) -> int:
        return self.task_successes + self.task_failures + self.partial + self.blocked

    @property
    def task_success_rate(self) -> Optional[float]:
        """Task successes over attempted tasks, verified or not; ``None`` with no attempts."""
        return self.task_successes / self.attempted if self.attempted else None

    @property
    def verified_success_rate(self) -> Optional[float]:
        """Verified task successes over episodes a host check examined; ``None`` with none."""
        if not self.verification_performed:
            return None
        return self.verified_successes / self.verification_performed

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(attempted=self.attempted, task_success_rate=self.task_success_rate,
                   verified_success_rate=self.verified_success_rate)
        return out

    @classmethod
    def from_axes(cls, rows: Any) -> "OutcomePopulation":
        """Count outcome-axes rows. A row without axes is unknown, never a success."""
        from schema.continuity_json import require_enum
        from schema.episode_record import TASK_OUTCOMES, VERIFICATION_STATUSES

        counts = {name: 0 for name in cls.__dataclass_fields__}
        for axes in rows or ():
            counts["total"] += 1
            if not isinstance(axes, dict):
                counts["unknown"] += 1
                continue
            task = require_enum(axes.get("task_outcome"), allowed=TASK_OUTCOMES,
                                where="outcome_axes.task_outcome")
            verification = require_enum(axes.get("verification_status"),
                                        allowed=VERIFICATION_STATUSES,
                                        where="outcome_axes.verification_status")
            bucket = {"success": "task_successes", "failure": "task_failures"}.get(task, task)
            counts[bucket] += 1
            if verification in _PERFORMED_VERIFICATION:
                counts["verification_performed"] += 1
                if verification == "passed":
                    counts["verification_passed"] += 1
            if task == "success":
                if verification == "passed":
                    counts["verified_successes"] += 1
                else:
                    counts["unverified_successes"] += 1
        return cls(**counts)


@dataclass
class CognitiveEpisode:
    """Single wake-cycle cognitive truth object.

    Aligns with docs/architecture/symphony_episode_schema.json — the
    schema's required field set (episode_id / opened_at / status /
    pressure) plus the movement slots that will be structured in S2+.

    Slot field naming: `global_` (trailing underscore) is used because
    `global` is a Python reserved word. JSON serialization (S2+) will
    map global_ -> global to honor the schema. Other slots
    (meta / auto / omega / sensory) match the schema names exactly.

    `state` mirrors the cue sheet's episode_states list (idle / open /
    meta_dispatch / auto_running / meta_reflect / global_orient /
    global_act / omega_conduct / complete / sleeping). S1 only uses
    'open' and 'complete'; richer transitions land with movement
    runners in S2+.
    """

    episode_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Cross-lane integration spec section 1: the cognitive thread this episode
    # belongs to. Host-generated and immutable for the thread's life; its value is
    # the episode id of the thread's FIRST episode, inherited verbatim by every
    # successor. Empty means "no thread was recorded" (pre-T2 carriers), which is
    # distinct from being the root of one.
    thread_id: str = ""
    opened_at: str = field(default_factory=_now_iso)
    closed_at: Optional[str] = None
    state: str = "open"
    pressure: Dict[str, Any] = field(default_factory=dict)
    # symphony S6 (reconciled to v0.2 schema 2026-05-21 S1 audit):
    # episode origin per docs/architecture/symphony_episode_schema.json
    # $.opened_by enum (curiosity_gap / operator_intent /
    # operator_message / contradiction / outstanding_question /
    # neuromod_spike / guardian / persistence_threshold / idle_probe).
    # `idle_review` was renamed to `idle_probe` in the v0.2 reconcile
    # to match the cue sheet's pressure.contributors id. CueDesk sets
    # this from the dominant pressure contributor on open. Default
    # 'curiosity_gap' for harness backward compat with S1-S5 tests
    # that opened episodes without specifying it.
    opened_by: str = "curiosity_gap"

    # Movement slots — dict / list placeholders for S1. Structured
    # types land per-movement in S2+ as their runners get implemented.
    meta: Dict[str, Any] = field(default_factory=dict)
    auto: Dict[str, Any] = field(default_factory=dict)
    global_: Dict[str, Any] = field(default_factory=dict)
    omega: Dict[str, Any] = field(default_factory=dict)
    sensory: List[Dict[str, Any]] = field(default_factory=list)
    # Symphony Phase 2 §P2-S1: per-turn limbic ingress + BrainLoop
    # receipts. Each entry is a dict with the per-turn record shape
    # declared in docs/architecture/symphony_episode_schema.json
    # ($.brain_turns[]). The list (not a single dict) preserves order
    # so Global/Meta/Omega can read the whole lived exchange in P2-S2+;
    # see [[project-p2-s1-locked-design]] for the operator lock.
    brain_turns: List[Dict[str, Any]] = field(default_factory=list)
    # Historical Symphony archive timestamp. DeepSleep now runs while the
    # carrier is `closing`; the field stays explicit for schema compatibility.
    archived_at: Optional[str] = None

    # Phase 2 §P2-S12 — compact temporal envelope captured at the
    # CueDesk tick boundary just before this episode opened. Carries
    # the wall-clock + tick / phase / cycle / epoch context every
    # downstream organ (Global, Meta, Auto, Omega, DeepSleep,
    # response_hold outlet, tick-state WAL) references so they share
    # one canonical "this is the moment this episode lived" record.
    # Empty dict when temporal capture was unavailable or skipped —
    # the schema slot stays stable.
    # Shape: see core.episode_temporal.TEMPORAL_FIELDS.
    temporal: Dict[str, Any] = field(default_factory=dict)

    # Phase 2 §P2-S13 — compact learning-state envelope composed at
    # the CueDesk tick boundary from existing substrates
    # (StrengthIndex snapshot, knowledge_mastery.json, recent recall
    # receipts). Carries "what the system knows + how strong + where
    # the gaps are" so every downstream organ shares one canonical
    # learning moment for routing decisions. Empty dict when the
    # composer was unavailable or skipped — the slot stays stable.
    # Shape: see pressure_loop.symphony_learning_state.LEARNING_STATE_FIELDS.
    learning_state: Dict[str, Any] = field(default_factory=dict)

    # Windowed reflection honesty: distinguishes full cascade completion from
    # early abort (run_global_a None) or mid-cascade exception. IDLE episodes
    # that run the whole cascade with skipped packs remain "completed".
    cascade_status: str = "completed"

    # Physical ownership and semantic provenance are independent.
    pipeline_id: int = -1
    work_kind: str = "autonomous"

    # Phase 3 cut 3 — the timekeeper's finish-line stamp
    # (core.timekeeper.finish_line_stamp): (commit_seq, commit_ms, event_date,
    # lane). Memory order is FINISH-LINE arrival, not ignition order. Phase 4
    # cut 13 moved WHERE it is taken: no longer at deepsleep gate entry but as
    # the first statement inside the serialized commit region
    # (core.commit_region), which is what keeps stamp order equal to landed
    # order now that the lane-local half of the terminus runs unserialized.
    # Empty until the episode reaches that region.
    timekeeper: Dict[str, Any] = field(default_factory=dict)

    # Phase 4 cut 12 — memory epochs (operator design 2026-08-10 §5). The
    # epoch IS the timekeeper's commit_seq: ONE ordering authority for both
    # "which memory did I read" and "which memory landed first".
    #
    # memory_epoch_start: the epoch observed at admission
    #   (core.timekeeper.last_commit_seq), beside the SRAM bucket open.
    # memory_epoch_commit: this episode's OWN finish-line commit_seq, sealed
    #   at the deepsleep gate. Always > start, because taking the stamp is
    #   itself the bump; commit == start + 1 is the quiet case — nothing else
    #   crossed the line while this episode was thinking.
    # causal_parent_episode_ids: the episodes whose journal rows fall in
    #   (start, commit] — memory that landed mid-thought and therefore could
    #   NOT be in this episode's reads. The honest answer to "what did I not
    #   know when I decided this".
    # causal_parents_complete: whether that set is the WHOLE interval. Cut 13
    #   leaves archive_episode outside the commit region, so a lower-seq row
    #   can still be in flight when this episode seals — a short set is now the
    #   ordinary case for overlapping lanes, not an anomaly. The flag rides the
    #   carrier so it reaches the journal row beside the set it qualifies; a
    #   parent list that can be short without saying so is exactly the silent
    #   under-report cuts 12 and 13 both refuse.
    # memory_epoch_pin: the substrate identities the reads pinned against
    #   (graph snapshot hash, coord index generation, DME pack hash) plus the
    #   surfaces that had nothing to pin. Evidence for the receipt, not order.
    #
    # All stay at their defaults under OMEGA_MEMORY_EPOCHS=0 and on episodes
    # that never reach the commit region — including causal_parents_complete,
    # whose False-by-default reads as "nothing was sealed here", never as a
    # sealed-and-short set.
    memory_epoch_start: int = 0
    memory_epoch_commit: int = 0
    causal_parent_episode_ids: List[str] = field(default_factory=list)
    causal_parents_complete: bool = False
    memory_epoch_pin: Dict[str, Any] = field(default_factory=dict)


class EpisodeStore:
    """One active carrier per physical pipeline; ambient access is thread-bound.

    Observers use current(pipeline_id=...) or active_episodes(). Returned
    carriers are live mutable objects; phase writes belong to their owner.
    """

    def __init__(
        self,
        *,
        reflection_window_path: Any = None,
        trajectory_path: Any = None,
    ) -> None:
        self._lock = threading.Lock()
        self._active: Dict[int, CognitiveEpisode] = {}
        self._cascade_binding = threading.local()
        self._last_closed_body: Optional[dict[str, Any]] = None
        self._reflection_window_path = reflection_window_path
        self._trajectory_path = trajectory_path
        self._reflection_window: Optional[Any] = None
        self._window_ready = False
        self._window_staged_episode_ids: set[str] = set()
        self._load_reflection_window_state()

    # ── carrier resolution ────────────────────────────────────────────

    def _bound_episode(self) -> Optional[CognitiveEpisode]:
        return getattr(self._cascade_binding, "episode", None)

    def _resolve_active_locked(self) -> Optional[CognitiveEpisode]:
        """The calling thread's episode, never an observer fallback."""
        bound = self._bound_episode()
        if bound is not None and self._active.get(bound.pipeline_id) is bound:
            return bound
        return None

    def open(
        self,
        pressure: Optional[Dict[str, Any]] = None,
        *,
        opened_by: str,
        temporal: Optional[Dict[str, Any]] = None,
        learning_state: Optional[Dict[str, Any]] = None,
        pipeline_id: int,
        work_kind: str,
    ) -> CognitiveEpisode:
        """Open and bind a carrier; a physical owner cannot be overwritten."""
        pid = validate_pipeline_id(pipeline_id)
        with self._lock:
            holder = self._active.get(pid)
            if holder is not None:
                raise RuntimeError(
                    f"cannot open pipeline {pid} while episode {holder.episode_id} owns it"
                )
            # THREAD IDENTITY (spec section 1). A continuation inherits its thread
            # verbatim from the contributor that opened it — the same
            # contributors[].detail channel stage_omega_successor reads its origin
            # from — and anything else roots a new thread at itself. The host writes
            # the id; a model never does.
            rows = (pressure.get("contributors") or []) if isinstance(pressure, dict) else []
            inherited = next((str(dict(row.get("detail") or {}).get("thread_id") or "")
                              for row in rows if row.get("id") == opened_by), "")
            identity = str(uuid.uuid4())
            ep = CognitiveEpisode(
                episode_id=identity,
                thread_id=inherited or identity,
                state="open",
                pressure=dict(pressure or {}),
                opened_by=opened_by,
                temporal=dict(temporal or {}),
                learning_state=dict(learning_state or {}),
                pipeline_id=pid,
                work_kind=str(work_kind),
            )
            self._active[pid] = ep
            self._cascade_binding.episode = ep

        # Phase B-5 logging: episode-open heartbeat. Emitted OUTSIDE
        # the lock so an audit-emit failure cannot deadlock the
        # episode lifecycle. Carries opened_by (the dominant
        # contributor name like 'evidence_terminal' / 'operator_intent'
        # / 'epistemic_emptiness') and the episode_id so remote debug
        # can correlate the open with the subsequent chain events.
        # No user content — pressure aggregate value is safe metadata.
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "episode_open",
                episode_id=str(ep.episode_id),
                opened_by=str(opened_by),
                summary=f"opened_by={opened_by}",
            )
        except Exception:
            pass

        return ep

    def close(
        self,
        *,
        cascade_status: str = "completed",
        before_finalize=None,
        after_archive=None,
    ) -> Optional[CognitiveEpisode]:
        """Commit required terminus work, then clear the active episode.

        ``before_finalize`` runs after the close timestamp and enriched body are
        prepared but while this episode still owns the active carrier. DeepSleep
        uses that transaction seam to commit memory before finalization. If any
        required work fails, the same carrier is restored to ``open`` and may be
        retried; it is never exposed as a completed episode.

        cascade_status records whether the cognition cascade completed
        end-to-end ("completed"), failed early / threw ("failed"), or stopped
        intentionally before a required fold ("deferred").

        Close must run on the owning cascade thread.

        Does NOT touch SharedState. The trajectory key
        (LAST_OMEGA_TRAJECTORY) is guidance and persists across
        episodes by the cue sheet contract — episode close has no
        business reaching into the bus."""
        with self._lock:
            bound = self._bound_episode()
            if bound is not None and self._active.get(bound.pipeline_id) is bound:
                ep = bound
            elif not self._active:
                return None
            else:
                raise RuntimeError(
                    "close() requires the owning cascade thread; active pipelines "
                    f"{sorted(self._active)}"
                )
            pid = ep.pipeline_id
            if ep.state == "closing":
                raise RuntimeError(f"episode {ep.episode_id} close already in progress")
            ep.cascade_status = str(cascade_status or "completed")
            ep.state = "closing"
            ep.closed_at = _now_iso()

        from pressure_loop.episode_journal import enriched_episode_from_episode

        ep_id = str(ep.episode_id)
        try:
            body = enriched_episode_from_episode(ep)
            try:
                from pressure_loop.pressure_field import refresh_neuromod_tonic

                refresh_neuromod_tonic(body.get("neuromod"))
            except Exception:
                pass
            # Phase 4 cut 13 moved the reflection-window append OUT of this
            # position and into the serialized commit region, which runs inside
            # ``before_finalize`` below (see commit_reflection_window_append).
            # The window is ONE global durable structure every lane appends to,
            # and this position was pre-stamp: with lanes closing concurrently,
            # two episodes could land in the window in the opposite order to
            # their finish-line stamps, and the ten bodies the reflection reads
            # would not be the ten memory committed in that order. The retry
            # semantics are unchanged — the append still raises inside this same
            # try, just one frame deeper.
            if before_finalize is not None:
                before_finalize(ep, body)
            self._last_closed_body = body
        except Exception:
            # Required close work did not commit. Keep the same carrier active so
            # the caller can retry instead of silently advancing to a new episode.
            with self._lock:
                if self._active.get(pid) is ep:
                    ep.state = "open"
                    ep.closed_at = None
            raise

        # Building pressure resets only after required close projections commit.
        try:
            from pressure_loop.pressure_field import reset_pressure_accumulator

            reset_pressure_accumulator()
        except Exception as exc:  # noqa: BLE001 - drive reset must never block close
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "episode_close",
                    status="degraded",
                    reason="accumulator_reset_failed",
                    episode_id=ep_id,
                    ok=False,
                    error=str(exc),
                )
            except Exception:
                pass

        # The historical continuity archive remains failure-soft, but runs only
        # after retryable close work succeeds so a retry cannot duplicate rows.
        from pressure_loop.episode_journal import archive_episode

        try:
            archive_episode(ep)
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "episode_journal_archive",
                    status="degraded",
                    ok=False,
                    episode_id=ep_id,
                    error=str(exc),
                )
            except Exception:
                pass
        else:
            # ``after_archive`` (cross-lane integration T6): the archive above RETURNED, so
            # a durable record of this episode now exists — the one point at which a caller
            # may act on "the outcome is recorded". It does not run when the archive failed,
            # because this close tolerates that failure and a caller must not mistake a
            # completed close for a durable outcome. Failure-soft: nothing here may fail a
            # close whose required work already committed.
            if after_archive is not None:
                try:
                    after_archive(ep)
                except Exception as exc:  # noqa: BLE001 - never fails the close
                    try:
                        from guardian.terminal_feedback import emit_data

                        emit_data(
                            "pressure",
                            "episode_after_archive",
                            status="degraded",
                            ok=False,
                            episode_id=ep_id,
                            error=str(exc),
                        )
                    except Exception:
                        pass

        with self._lock:
            if self._active.get(pid) is not ep:
                raise RuntimeError(f"episode {ep_id} lost active ownership during close")
            ep.state = "complete"
            self._active.pop(pid, None)
            if self._bound_episode() is ep:
                self._cascade_binding.episode = None
            self._window_staged_episode_ids.discard(ep_id)

        # Phase B-5 logging: episode-close heartbeat (outside lock,
        # symmetric with the open emit above). Lets remote debug see
        # the full open → close arc with episode_id correlation,
        # opened_by (cause) preserved, closed_at iso for duration math.
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "episode_close",
                episode_id=str(ep.episode_id),
                cascade_status=str(getattr(ep, "cascade_status", "completed") or "completed"),
                opened_by=str(getattr(ep, "opened_by", "") or ""),
                summary=(f"cascade_status={getattr(ep, 'cascade_status', 'completed')}"),
            )
        except Exception:
            pass

        return ep

    def restore(self, episode: CognitiveEpisode) -> CognitiveEpisode:
        """Install a recovered active episode.

        Restore-from-snapshot helper. Intentionally not named ``open``:
        no new id, timestamp,
        pressure snapshot, or opened_by decision is created during
        restoration.
        """
        if not isinstance(episode, CognitiveEpisode):
            raise TypeError(
                f"EpisodeStore.restore requires a CognitiveEpisode, got {type(episode).__name__}"
            )
        with self._lock:
            pid = validate_pipeline_id(episode.pipeline_id)
            holder = self._active.get(pid)
            if holder is not None and holder is not episode:
                raise RuntimeError(f"cannot restore pipeline {pid} while episode {holder.episode_id} owns it")
            self._active[pid] = episode
            self._cascade_binding.episode = episode
            return episode

    def current(self, pipeline_id: Optional[int] = None) -> Optional[CognitiveEpisode]:
        """Return this thread's carrier, or explicitly observe a physical owner."""
        pid = validate_pipeline_id(pipeline_id) if pipeline_id is not None else None
        with self._lock:
            if pid is not None:
                return self._active.get(pid)
            return self._resolve_active_locked()

    def active_episodes(self) -> tuple[CognitiveEpisode, ...]:
        """Every live carrier, oldest-opened first (all lanes)."""
        with self._lock:
            return tuple(self._active.values())

    def last_closed_body(self) -> Optional[dict[str, Any]]:
        """Most-recent closed-episode enriched body (window-independent;
        carries neuromod). None at boot. Read-only; never mutates window/_active;
        never raises."""
        return self._last_closed_body

    def recent_outcome_population(self, n: int = 10) -> "OutcomePopulation":
        """The outcomes of the most recent *n* closed episodes, as a typed population (WO-06).

        Reads each journal row's outcome axes. An empty journal is an empty population whose
        rates are ``None`` — never a neutral 0.5 standing in for a measurement. Read-only.
        """
        from pressure_loop.episode_journal import journal_window

        window_n = int(n)
        if window_n <= 0:
            return OutcomePopulation()
        return OutcomePopulation.from_axes(
            _outcome_axes_of(pe) for pe in journal_window(window_n)
        )

    def append_sensory(self, record: Dict[str, Any]) -> bool:
        """Symphony S7: append a sensory record to the active episode's
        sensory[] list. Returns True on append, False on no-op (no
        active episode).

        The record is shape-validated via `make_sensory_record` round-
        tripping required keys -- callers that built the record via
        the helper see no extra check; callers passing a hand-rolled
        dict get validated here (raises ValueError on missing/bad
        fields).

        The append is performed under the store lock so concurrent
        appends from multiple movements (brain ingress + curiosity
        sensory + autonomous initiation) cannot interleave a partial
        write. The episode's sensory list itself is a regular list;
        readers iterating it without acquiring this lock may see a
        snapshot that is one element behind a concurrent append (the
        list reference itself is stable, the contents are mutated in
        place).

        Returning False (no-op) is by design -- the brain receiver
        ALWAYS tries to append, regardless of whether CueDesk has
        opened an episode this tick. Pre-symphony cycles + symphony
        cycles outside an active window both flow through the same
        receiver path; the no-op silently drops the record so the
        receiver doesn't have to know about episode state."""
        # Validate first so a bad record cannot lock-and-leak
        required = {"ts", "source", "kind", "payload"}
        missing = required - set(record.keys())
        if missing:
            raise ValueError(f"sensory record missing required keys: {sorted(missing)}")
        if record["source"] not in SENSORY_SOURCES:
            raise ValueError(
                f"sensory.source must be one of {SENSORY_SOURCES}, got {record['source']!r}"
            )
        if not (isinstance(record["kind"], str) and record["kind"].strip()):
            raise ValueError("sensory.kind must be a non-empty string")
        if not isinstance(record["payload"], dict):
            raise ValueError("sensory.payload must be a dict")

        with self._lock:
            ep = self._resolve_active_locked()
            if ep is None:
                log.debug(
                    "episode_store.append_sensory: no active episode "
                    "-- record dropped (source=%s kind=%s)",
                    record.get("source"),
                    record.get("kind"),
                )
                return False
            # Copy the record into the list so subsequent caller-side
            # mutation of the source dict does not retroactively
            # change the recorded sensory entry. payload is copied
            # via dict(...) one level deep -- sufficient for the
            # text/cycle_id/curiosity_id shapes the schema describes.
            ep.sensory.append(
                {
                    "ts": record["ts"],
                    "source": record["source"],
                    "kind": record["kind"],
                    "payload": dict(record["payload"]),
                }
            )
        return True

    @property
    def window_ready(self) -> bool:
        """True when a locked 10-episode window awaits Omega reflection."""
        return bool(self._window_ready)

    def get_active_window(self) -> Optional[Any]:
        """Return the in-memory reflection window (read-only for callers)."""
        return self._reflection_window

    def get_locked_window(self) -> Optional[Any]:
        """Return the locked window awaiting reflection — STAGED first (cut 10).

        The double buffer means the maintained window is never locked for long:
        it is moved to durable staging the instant it fills and the collecting
        half starts over. Every caller that ever asked "is a window waiting?"
        still gets the same answer here, it just comes from the staging file now.
        The maintained-window branch below is the kill-switch/legacy read (with
        OMEGA_WINDOW_LANE=0 the locked window is archived at the lock seam, so it
        answers None there too).
        """
        from pressure_loop import window_lane

        staged = window_lane.peek_staged_window(window_path=self._reflection_window_path)
        if staged is not None:
            return staged
        window = self._reflection_window
        if window is None:
            return None
        if getattr(window, "is_ready_for_reflection", lambda: False)():
            return window
        return None

    def episode_in_reflection_window(self, episode_id: str) -> bool:
        """True when episode_id is already recorded in the maintained window."""
        eid = str(episode_id)
        with self._lock:
            if eid in self._window_staged_episode_ids:
                return True
            window = self._reflection_window
            if window is not None and eid in list(getattr(window, "episodes", None) or []):
                return True
        # WO-07: a tenth episode's append locks its window and double-buffers it into STAGING,
        # leaving the collecting buffer empty. A terminus retried after that must still find the
        # episode where it went, or its append lands a second time in the next window.
        from pressure_loop import window_lane

        return any(
            eid in list(getattr(staged, "episodes", None) or [])
            for staged in window_lane.staged_windows(window_path=self._reflection_window_path)
        )

    def episode_staged_for_window_reflection(self, episode_id: str) -> bool:
        """True when episode 10 was staged inline for Omega window reflection."""
        with self._lock:
            return str(episode_id) in self._window_staged_episode_ids

    def is_tenth_episode_window_reflection(self) -> bool:
        """True when the next staged episode completes a 10-episode window."""
        from pressure_loop.reflection_window import WINDOW_SIZE

        with self._lock:
            window = self._reflection_window
            if window is None or window.is_locked():
                return False
            return len(list(getattr(window, "episodes", None) or [])) == WINDOW_SIZE - 1

    def stage_episode_for_window_reflection(
        self, episode, *, cascade_status: str = "completed"
    ) -> None:
        """Append the active episode as the 10th body and lock the window."""
        from pressure_loop.episode_journal import enriched_episode_from_episode

        eid = str(getattr(episode, "episode_id", "") or "")
        if not eid:
            raise RuntimeError("stage_episode_for_window_reflection requires episode_id")
        episode.cascade_status = str(cascade_status or "completed")
        body = enriched_episode_from_episode(episode)
        with self._lock:
            self._window_staged_episode_ids.add(eid)
        self._append_episode_to_reflection_window(
            eid,
            cascade_status=str(cascade_status or "completed"),
            body=body,
            inline_omega_staging=True,
        )

    def commit_reflection_window_append(
        self,
        episode: Any,
        body: dict[str, Any] | None,
        *,
        mastery_delta: dict[str, Any] | None = None,
        record_ref: Any = None,
    ) -> None:
        """Append a closing episode to the reflection window — Phase 4 cut 13.

        WO-09: ``record_ref`` is the CanonicalRef the terminus just bound for this episode's
        sealed record. It is pinned onto the window with the member, in the same store-lock
        acquisition, so a cohort's membership is fixed by exact reference the moment each
        member joins. When this append locks the window, the member's own sealed finish-line
        position and observed close time become the cohort's replay cutoff.

        Called from the serialized commit region of that episode's DeepSleep
        terminus (deepsleep_loop/loop.py), AFTER the finish-line stamp, so the
        window collects episodes in the order memory committed them. Before this
        cut the append ran in ``close()`` before the terminus, serialized only by
        the one process-wide cognition lock; per-lane locks would have left it
        racing on a single durable file with an order nobody had decided.

        The two guards travel with the call because they are about WHICH
        episodes belong in a window, not about when the append happens:

        * a WINDOW-lane episode never joins a window (cut 10) — it is the
          reflection ON one, and counting it would make every tenth window
          partly a reflection on reflections;
        * an episode already in the window (or already staged inline) is not
          appended twice.

        ``mastery_delta`` (risk 8, ruling 2026-08-11) is this episode's
        ``journal_entry["mastery_effects"]``, and it rides the SAME call as the
        body deliberately. It used to be a second call
        (``append_episode_mastery_delta``) made from ``cognition_chain`` AFTER
        the commit region had already closed, under nothing but the lane lock —
        so between the append here and the annotation there, another lane's
        terminus could close a tenth episode, lock the window and double-buffer
        it away. The delta then landed on whatever ``self._reflection_window``
        had become: the NEXT window, or nothing at all. Passing it here makes
        "which window does this episode belong to" a question asked exactly
        once, inside one store-lock acquisition, by the statement that answers
        it for the body.

        Fail-hard: the window is durable continuity, so a failed append
        propagates through ``before_finalize`` into ``close()``'s restore path
        and the carrier stays retryable.
        """
        ep_id = str(getattr(episode, "episode_id", "") or "")
        if not ep_id:
            raise RuntimeError("commit_reflection_window_append requires an episode id")
        if episode.work_kind == "reflection":
            return
        if self.episode_in_reflection_window(ep_id):
            return
        self._append_episode_to_reflection_window(
            ep_id,
            cascade_status=str(getattr(episode, "cascade_status", "completed") or "completed"),
            body=body,
            mastery_delta=mastery_delta,
            record_ref=record_ref,
            cutoff=_member_cutoff(body) if record_ref is not None else None,
        )

    def consume_window_ready(self) -> Optional[Any]:
        """Return the maintained locked window when reflection is pending."""
        with self._lock:
            window = self.get_locked_window()
            if window is not None:
                self._window_ready = False
            return window

    def archive_and_reset_window(
        self, window: Any, *, omega_outcome: Optional[Dict[str, Any]] = None
    ) -> None:
        """Archive a completed window and release whichever buffer held it.

        Cut 10 split the two halves apart. A STAGED window is released by
        removing its staging row and NOTHING ELSE — the active window file now
        holds a different, still-collecting window, and clearing it here (as this
        method always did) would throw away every episode closed since the lock.
        Only the maintained window still takes the full reset.
        """
        from pressure_loop import window_lane
        from pressure_loop.reflection_window import (
            archive_reflection_window,
            clear_reflection_lease,
            clear_reflection_window,
        )

        episodes = list(getattr(window, "episodes", None) or [])
        if not episodes:
            raise RuntimeError("archive_and_reset_window requires a non-empty window")
        try:
            archive_reflection_window(window, omega_outcome=omega_outcome)
        except Exception:  # noqa: BLE001
            log.exception("reflection window archive failed (non-fatal)")
        window_id = str(getattr(window, "window_id", "") or "")
        if window_lane.unstage_window(window_id, window_path=self._reflection_window_path):
            with self._lock:
                self._window_ready = window_lane.has_staged_window(
                    window_path=self._reflection_window_path
                )
            return
        with self._lock:
            self._reflection_window = None
            self._window_ready = False
        clear_reflection_window(self._reflection_window_path)
        clear_reflection_lease()

    def _load_reflection_window_state(self) -> None:
        from pressure_loop import window_lane
        from pressure_loop.reflection_window import (
            load_reflection_window,
            reconcile_stale_reflection_on_boot,
            reflection_window_path,
        )

        path = self._reflection_window_path or reflection_window_path()
        reconcile_stale_reflection_on_boot(window_path=path)
        window = load_reflection_window(path)
        self._reflection_window = window
        if window is not None and window.is_ready_for_reflection():
            self._window_ready = True
        # A window staged before the crash is still owed a reflection: the
        # durable staging file is the truth, this flag is only its in-memory
        # shadow (cut 10). The pressure gather reads the file, not this.
        if window_lane.has_staged_window(window_path=self._reflection_window_path):
            self._window_ready = True

    def _double_buffer_locked_window(self, window: Any) -> None:
        """Swap a just-locked window out of the collecting buffer. Call under the lock.

        Order is the whole point: the DURABLE staging write happens first, then
        the active window file is dropped. A crash between them leaves the window
        both staged and active — ``reconcile_stale_reflection_on_boot`` knows that
        shape and clears the active copy instead of archiving ten episodes
        unreflected. The reverse order would lose the window outright.

        The fresh collecting window is opened LAZILY by the next episode's
        append: ``open_reflection_window`` needs the id of the episode that
        starts it, which does not exist yet at lock time. The effect is what the
        design asks for — the eleventh close begins a new window with no
        knowledge that a reflection is in flight.
        """
        from pressure_loop import window_lane
        from pressure_loop.reflection_window import clear_reflection_window

        if window_lane.window_lane_enabled():
            window_lane.stage_locked_window(window, window_path=self._reflection_window_path)
            self._window_ready = True
        else:
            # Kill switch: verbatim pre-cut-10 behaviour, one seam earlier. The
            # window is filed away unreflected under the same outcome string the
            # ignition seam used, and anything staged before the flip is drained
            # with it so the switch is a drain valve rather than a trap door.
            window_lane.archive_unreflected(window, window_path=self._reflection_window_path)
            self._window_ready = False
        clear_reflection_window(self._reflection_window_path)
        self._reflection_window = None

    def _append_episode_to_reflection_window(
        self,
        episode_id: str,
        *,
        cascade_status: str = "completed",
        body: dict[str, Any] | None = None,
        inline_omega_staging: bool = False,
        mastery_delta: dict[str, Any] | None = None,
        record_ref: Any = None,
        cutoff: dict[str, Any] | None = None,
    ) -> None:
        from pressure_loop.reflection_window import (
            append_episode_to_window,
            open_reflection_window,
            save_reflection_window,
        )

        with self._lock:
            window = self._reflection_window
            if window is not None and str(episode_id) in list(
                getattr(window, "episodes", None) or []
            ):
                return
            if window is not None and window.is_locked():
                if window.is_ready_for_reflection():
                    self._window_ready = True
                return

            if window is None:
                window = open_reflection_window(
                    episode_id,
                    trajectory_path=self._trajectory_path,
                )
            from copy import deepcopy

            previous_window = deepcopy(self._reflection_window)
            previous_ready = self._window_ready
            try:
                window = append_episode_to_window(
                    window, episode_id, cascade_status=cascade_status, body=body,
                    record_ref=record_ref, cutoff=cutoff,
                )
                # Risk 8: the episode's mastery delta is annotated onto the
                # window it JUST JOINED, in the same lock acquisition and before
                # anything can lock or swap that window. This is the whole fix —
                # a tenth episode's delta now rides the window it completed into
                # staging instead of being dropped on the floor a moment later.
                if mastery_delta:
                    window.episode_mastery_deltas.append(
                        {
                            "episode_id": str(episode_id),
                            "mastery_effects": dict(mastery_delta),
                        }
                    )
                self._reflection_window = window
                if window.is_ready_for_reflection():
                    self._window_ready = True
                # The tenth body lands durably BEFORE the double buffer swaps,
                # so a crash in between finds a complete locked window on disk.
                save_reflection_window(window, self._reflection_window_path)
                if window.is_ready_for_reflection():
                    # Phase 4 cut 10: the locked window moves to staging and the
                    # collecting half starts over. Before this cut it stayed here
                    # until the NEXT episode's ignition seam archived it
                    # unreflected — and every close in between was silently
                    # dropped by the "no 11th append" rule.
                    self._double_buffer_locked_window(window)
            except Exception:
                self._reflection_window = previous_window
                self._window_ready = previous_ready
                raise

    # ``append_episode_mastery_delta`` was REMOVED (risk 8, ruling 2026-08-11).
    # It annotated ``self._reflection_window`` — whatever window happened to be
    # collecting at the moment of the call — from outside the commit region, so
    # with four lanes live it could land one episode's delta on the next
    # episode's window, or on no window at all. The delta now travels with the
    # body through ``commit_reflection_window_append``, which is the one place
    # that knows which window this episode joined. A second entry point that
    # could disagree with that answer is the bug, not a convenience.

    def append_brain_turn(self, record: Dict[str, Any]) -> bool:
        """Symphony Phase 2 §P2-S1: append a brain-turn receipt to the
        active episode's brain_turns[] list. Returns True on append,
        False on no-op (no active episode). Mirrors append_sensory.

        Required keys (per docs/architecture/symphony_episode_schema.json
        $.brain_turns[] + PACKET_P2_S1.md §4):
            cycle_id, ingress_class, outcome_hint, action_kind.

        Optional but encouraged: ingress_confidence, pressure_contribution,
        response_summary, memory_operation, tool_or_memory_context,
        store_receipt. Callers pass a hand-rolled dict; this method
        validates the required keys + value types here.

        Returning False (no-op) is by design — the brain receiver
        ALWAYS tries to append after the cycle returns, regardless of
        whether CueDesk has opened an episode this tick. The no-op
        silently drops the record so the receiver path does not need
        to know about episode state."""
        required = {"cycle_id", "ingress_class", "outcome_hint", "action_kind"}
        missing = required - set(record.keys())
        if missing:
            raise ValueError(f"brain_turn record missing required keys: {sorted(missing)}")
        for k in ("cycle_id", "ingress_class", "outcome_hint", "action_kind"):
            v = record[k]
            if not (isinstance(v, str) and v.strip()):
                raise ValueError(f"brain_turn.{k} must be a non-empty string, got {v!r}")

        # Copy mutable optional dict fields one level deep so caller-side
        # post-append mutation cannot retroactively change the receipt.
        with self._lock:
            ep = self._resolve_active_locked()
            if ep is None:
                log.debug(
                    "episode_store.append_brain_turn: no active episode "
                    "-- record dropped (cycle_id=%s ingress_class=%s)",
                    record.get("cycle_id"),
                    record.get("ingress_class"),
                )
                return False
            ep.brain_turns.append(
                {
                    "cycle_id": record["cycle_id"],
                    "ingress_class": record["ingress_class"],
                    "ingress_confidence": float(record.get("ingress_confidence", 0.0)),
                    "pressure_contribution": float(record.get("pressure_contribution", 0.0)),
                    "outcome_hint": record["outcome_hint"],
                    "action_kind": record["action_kind"],
                    "response_summary": dict(record.get("response_summary") or {}),
                    "memory_operation": dict(record.get("memory_operation") or {}),
                    "tool_or_memory_context": dict(record.get("tool_or_memory_context") or {}),
                    "store_receipt": dict(record.get("store_receipt") or {}),
                }
            )
        return True
