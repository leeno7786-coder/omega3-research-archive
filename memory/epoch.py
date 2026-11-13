"""Memory epochs — snapshot-read bookkeeping for lane-parallel episodes.

Phase 4 cut 12 (operator design 2026-08-10, ratified §5): *"every episode
captures a memory_epoch at open; reads see epoch + its own local writes; the
commit appends immutable records, bumps the global epoch, updates graph/DME.
Parallel reads/cognition/candidate-writes; SERIALIZED atomic canonical
commits (the timekeeper finish line IS this order). Record memory_epoch_start /
memory_epoch_commit / causal_parent_episode_ids."*

**One ordering authority.** The epoch is ``core.timekeeper``'s ``commit_seq``,
not a new counter and not ``memory.write_ordinal``. Memory order is finish-line
order by operator law; giving the epoch its own counter would create a second
authority that eventually disagrees with the first, and a disagreement between
"which memory did I read" and "which memory landed first" is exactly the bug
this bookkeeping exists to make visible. So: epoch at admission =
``timekeeper.last_commit_seq()``; epoch at commit = this episode's own stamp's
``commit_seq``; the episodes whose rows fall in ``(start, commit]`` are the
``causal_parent_episode_ids`` — memory that landed while this episode was
thinking, which its own reads could not have seen.

**Observational-first (cut 12 is not cut 13).** Nothing here changes a lock, a
lock's scope, or what a recall returns. The read substrates below cannot be
rewound to an earlier snapshot — a recompiled taxonomy graph and a reloaded DME
pack replace their predecessor on disk — so an episode whose epoch has moved
under it is served the NEXT snapshot and the divergence is RECORDED. That is
the pin-with-fallback contract: honest, never silent. Cut 13 splits the locks
using this vocabulary; the receipts it will need must already be truthful.

**The pin resolves through the SRAM bucket.** ``episode_sram`` already owns the
per-episode, cascade-thread-bound identity, so the pin keys off the same
resolution instead of minting a second binding. Two resolvers would eventually
disagree about which episode a read belongs to, and on the day recall does
merge the local hold (see the recorded gap below) that disagreement would put
one episode's snapshot next to another episode's writes.

**ATTRIBUTION IS APPROXIMATE, AND SAYS SO** (operator ruling 2026-08-11,
carried review risk 11). The resolver is cascade-thread-bound with a
most-recently-opened FALLBACK. Under one lane the fallback cannot be wrong.
Under four it can: a worker thread a cascade spawns inherits no binding, so its
read is attributed to whichever episode opened LAST — which may be another
lane's. The read is still served correctly; what is uncertain is whose receipt
it lands on.

Exactness was considered and REJECTED as not cheap, on two grounds. Every
mechanism that makes it exact — thread-keyed pins, lane-keyed pins, an
inherited context — has to propagate the binding at every thread spawn in the
tree, because the binding is what is missing; keying the pins differently does
not conjure it. And doing it here, for pins only, would build the SECOND
resolver this module was written to avoid: on the day recall merges the local
hold, a read attributed to episode A by the pin resolver and to episode B by
the SRAM resolver puts one episode's snapshot beside another's writes, which is
strictly worse than a receipt that admits it inferred.

So the approximation is NAMED instead. Every observation and every receipt
carries ``attribution``: ``thread_bound`` when the calling thread owns the
episode it was attributed to, ``sole_open_bucket`` when the fallback was used
but only one episode was open (which makes it exact anyway), and ``inferred``
when the fallback chose among several. The pin counts its inferred reads, so a
receipt says how much of itself is approximate rather than leaving the reader
to guess. A receipt that quietly rounds is the silent failure this bookkeeping
exists to prevent; a receipt that says "I inferred this attribution, and here
is how many times" is honest at the grain the system actually has.

Recorded gap, measured 2026-08-11: the ratified §5 sentence "reads see epoch +
its own local writes" is only HALF true in this tree. The bucket holds an
episode's uncommitted intake, but recall deliberately does NOT merge it —
``memory.api`` line ~450 records the operator ruling of 2026-07-16 that
perception-before-consolidation is structural, so a fact stated this turn lands
in memory at DeepSleep and this turn's recall never sees it. That is a design
choice, not an oversight, and cut 12 does not change it. What cut 12 asserts is
narrower and true: reads see the epoch, and the epoch is honest about itself.

**Kill switch ``OMEGA_MEMORY_EPOCHS=0``**: no pin is opened, no observation is
recorded, the episode/journal epoch fields stay at their defaults and
``recall_receipt`` reports nothing — Phase-3 behaviour. It does NOT revert
``memory.api``'s switch from the live ``COORD_INDEX`` to
``coord_index_snapshot()``: that is a read-consistency fix (AUDIT-5's own
recommendation), not epoch behaviour, and an operator turning epochs off must
not get a dictionary-mutated-during-iteration race back.

**Failure posture.** The epoch counter read is pure memory and is allowed to
fail hard. Capturing the substrate identities (a graph hash file, the coord
index generation, the DME pack hash) touches disk, and a missing/unreadable
identity is recorded as a DEGRADED surface rather than crashing a cascade that
has not started thinking yet — an unpinnable surface is a fact about the
substrate, and the receipt says so by name.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("omega.memory.epoch")

_OFF = {"0", "false", "no", "off"}

# Surfaces an episode pins its reads to. Named constants because the receipt
# keys are read by the journal, the HUD and (from cut 13) the commit region.
SURFACE_GRAPH = "graph"
SURFACE_COORD_INDEX = "coord_index"
SURFACE_DME = "dme"

# Sentinel for "this surface had no identity to pin". Distinct from the coord
# index's own 0, which means "never loaded in this process" — see the capture
# branch in ``open_episode_epoch`` for why that is also unpinnable.
UNPINNABLE_GENERATION = -1

# How a read's pin was attributed to an episode (risk 11 — see the module
# docstring). These ride every observation and every receipt because the
# difference between them is the difference between a fact and an inference,
# and only one of the three is an inference.
ATTRIBUTION_THREAD_BOUND = "thread_bound"  # the calling thread owns this episode
ATTRIBUTION_SOLE_BUCKET = "sole_open_bucket"  # fallback, but nothing to be wrong about
ATTRIBUTION_INFERRED = "inferred"  # fallback among several open episodes

_lock = threading.RLock()
_pins: Dict[str, "EpochPin"] = {}


def epochs_enabled() -> bool:
    """Kill switch: OMEGA_MEMORY_EPOCHS=0 → no epoch bookkeeping at all."""
    return (os.environ.get("OMEGA_MEMORY_EPOCHS") or "1").strip().lower() not in _OFF


@dataclass
class EpochPin:
    """One episode's snapshot-read pin: the memory state it opened against.

    ``epoch`` is the ordering authority (timekeeper commit_seq). The three
    substrate identities are what the read paths compare against to answer
    "was I served the memory I pinned?" — they are evidence, not order.

    ``surfaces`` holds the MOST RECENT observation per surface within this
    episode. A single ``read_memory`` observes graph, coord index and DME in
    the same call, so the receipt it emits is that read's own truth; a surface
    that a later read skips keeps its previous (still-honest) observation
    rather than silently disappearing from the receipt.
    """

    episode_id: str
    pipeline_id: int
    work_kind: str
    epoch: int
    graph_snapshot_hash: str = ""
    coord_index_generation: int = UNPINNABLE_GENERATION
    dme_snapshot_hash: str = ""
    captured_at: str = ""
    # Surfaces that had no identity to pin at capture time (missing compiled
    # graph, DME packs absent, import failure). Recorded, never silent.
    degraded: List[str] = field(default_factory=list)
    surfaces: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    honored_reads: int = 0
    fallback_reads: int = 0
    # Reads attributed to this episode by the most-recently-opened fallback
    # while more than one episode held a bucket (risk 11). They may belong to
    # another lane. Counted, never guessed at: a receipt with
    # ``inferred_reads > 0`` is telling its reader how much of itself is an
    # inference. NOT the same as ``fallback_reads``, which is about the memory
    # SNAPSHOT moving under a read; this one is about WHOSE read it was.
    inferred_reads: int = 0
    # The first time this episode was served a different snapshot than it
    # pinned — kept verbatim because the FIRST divergence is the one that
    # explains a surprising recall; later ones are usually its consequence.
    first_divergence: Optional[Dict[str, Any]] = None

    def pinned_value(self, surface: str) -> Any:
        if surface == SURFACE_GRAPH:
            return self.graph_snapshot_hash
        if surface == SURFACE_COORD_INDEX:
            return self.coord_index_generation
        if surface == SURFACE_DME:
            return self.dme_snapshot_hash
        raise ValueError(f"unknown memory-epoch surface {surface!r}")

    def to_dict(self) -> Dict[str, Any]:
        """The pin as it rides the episode carrier (and the HUD)."""
        return {
            "epoch": int(self.epoch),
            "graph_snapshot_hash": str(self.graph_snapshot_hash),
            "coord_index_generation": int(self.coord_index_generation),
            "dme_snapshot_hash": str(self.dme_snapshot_hash),
            "captured_at": str(self.captured_at),
            "degraded": list(self.degraded),
        }


# ── capture ─────────────────────────────────────────────────────────────────


def _capture_graph_snapshot_hash() -> str:
    from core.paths import MEMORY_ROOT
    from memory.taxonomy import paths as P

    from memory.projection.generation import GenerationIntegrityError, active_graph_snapshot_hash

    try:  # Cut 13: the active generation's hash once a manifest exists (never the stale legacy marker)
        published = active_graph_snapshot_hash(MEMORY_ROOT)
    except GenerationIntegrityError:
        published = None
    if published is not None:
        return published
    marker = P.compiled_root(MEMORY_ROOT) / "latest_snapshot_hash.txt"
    if not marker.exists():
        return ""
    return marker.read_text(encoding="utf-8").strip()


def _capture_coord_index_generation() -> int:
    from memory.engine.walker import coord_index_generation

    return int(coord_index_generation())


def _capture_dme_snapshot_hash() -> str:
    """The DME packs an episode's reads will be served — resident ones only.

    Never the graph's ``latest_snapshot_hash.txt``: the packs lag the graph by
    a rebuild, so pinning the graph's hash here would report a divergence on
    every read of a perfectly consistent pack set. Never a forced load either
    (see ``recall_hook.resident_snapshot_hash``). "" means unpinnable, which
    the caller records as a degraded surface.
    """
    from core.paths import MEMORY_ROOT
    from memory.dme.recall_hook import resident_snapshot_hash

    return str(resident_snapshot_hash(MEMORY_ROOT) or "")


_CAPTURERS = (
    (SURFACE_GRAPH, _capture_graph_snapshot_hash),
    (SURFACE_COORD_INDEX, _capture_coord_index_generation),
    (SURFACE_DME, _capture_dme_snapshot_hash),
)


def _prune_orphan_pins(*, keep: str = "") -> None:
    """Drop pins whose SRAM bucket is gone (drained episodes that skipped the
    terminus cleanup — ``episode_sram.open_bucket`` reaps those same slots).

    Such a pin is already unreachable through ``active_pin`` (its episode can
    no longer be the resolved bucket), so this is hygiene rather than
    correctness — but an unbounded registry in a process that runs for weeks
    is its own kind of bug.
    """
    try:
        from memory import episode_sram
    except Exception:  # noqa: BLE001
        return
    with _lock:
        candidates = [eid for eid in _pins if eid != keep]
    # The bucket check runs OUTSIDE this module's lock: episode_sram has its
    # own, and holding both at once would create the only lock pair in this
    # subsystem — a deadlock waiting for the day something calls the other way.
    orphans = [eid for eid in candidates if not episode_sram.is_open(eid)]
    if not orphans:
        return
    with _lock:
        for episode_id in orphans:
            _pins.pop(episode_id, None)


def open_episode_epoch(episode: Any) -> Optional[EpochPin]:
    """Capture this episode's memory epoch and open its read pin.

    Called from the ignition seam beside ``_open_episode_sram`` — the same
    instant the episode's read-your-own-writes overlay is created, so the
    snapshot and the overlay describe one moment. Writes
    ``memory_epoch_start`` + ``memory_epoch_pin`` onto the carrier; both ride
    the enriched body and the journal row like ``lane`` and ``timekeeper``.

    Returns None when the kill switch is off or the carrier has no id.
    """
    if not epochs_enabled():
        return None
    episode_id = str(getattr(episode, "episode_id", "") or "")
    if not episode_id:
        return None

    from core.symphony_time import now_iso
    from core.timekeeper import last_commit_seq

    pin = EpochPin(
        episode_id=episode_id,
        pipeline_id=episode.pipeline_id,
        work_kind=episode.work_kind,
        epoch=int(last_commit_seq()),
        captured_at=now_iso(),
    )
    for surface, capture in _CAPTURERS:
        try:
            value = capture()
        except Exception as exc:  # noqa: BLE001 — a degraded surface is a fact
            log.warning(
                "memory epoch %s: %s identity unavailable at capture (%s) — "
                "reads on that surface will report degraded, not silence",
                episode_id,
                surface,
                exc,
            )
            pin.degraded.append(surface)
            continue
        if surface == SURFACE_GRAPH:
            pin.graph_snapshot_hash = str(value)
            if not pin.graph_snapshot_hash:
                pin.degraded.append(surface)
        elif surface == SURFACE_COORD_INDEX:
            # Generation 0 means the walker has never loaded the index in this
            # process — that is not a memory state, it is the absence of one.
            # Pinning against it would make the first recall of every boot
            # report a fallback that is really just lazy loading, and a receipt
            # that cries wolf is worth less than no receipt.
            pin.coord_index_generation = (
                int(value) if int(value) > 0 else UNPINNABLE_GENERATION
            )
            if pin.coord_index_generation == UNPINNABLE_GENERATION:
                pin.degraded.append(surface)
        else:
            pin.dme_snapshot_hash = str(value)
            if not pin.dme_snapshot_hash:
                pin.degraded.append(surface)

    _prune_orphan_pins(keep=episode_id)
    with _lock:
        _pins[episode_id] = pin

    try:
        episode.memory_epoch_start = int(pin.epoch)
        episode.memory_epoch_pin = pin.to_dict()
    except Exception:  # noqa: BLE001 — SimpleNamespace carriers in tests
        log.debug("memory epoch: carrier %s rejected epoch fields", episode_id)
    return pin


def close_episode_epoch(episode_id: str) -> Optional[EpochPin]:
    """Drop the pin after the terminus. Returns it for a last read."""
    with _lock:
        return _pins.pop(str(episode_id or ""), None)


def resolve_active_pin() -> tuple[Optional[EpochPin], str]:
    """The calling thread's pin AND how it was attributed (risk 11).

    ``episode_sram.current_episode_id()`` stays the one resolver: reusing it
    means a read can never report one episode's snapshot while sitting inside
    another episode's held intake. What this adds is the second, honest
    question — was that answer the thread's OWN binding, or the
    most-recently-opened fallback, and was there more than one episode for the
    fallback to be wrong about? See the module docstring for why the
    approximation is named rather than engineered away.

    The attribution is "" when there is no pin at all.
    """
    if not epochs_enabled():
        return None, ""
    with _lock:
        if not _pins:
            return None, ""
    try:
        from memory import episode_sram

        # ONE acquisition for all three: asked separately, a bucket closing in
        # between can turn "the fallback chose among two" into "there was only
        # one", which is the rounding this whole surface exists to stop.
        episode_id, bound_id, open_buckets = episode_sram.resolve_binding()
    except Exception:  # noqa: BLE001 — no bucket machinery, no pin
        return None, ""
    if not episode_id:
        return None, ""
    with _lock:
        pin = _pins.get(str(episode_id))
    if pin is None:
        return None, ""
    if bound_id and str(bound_id) == str(episode_id):
        return pin, ATTRIBUTION_THREAD_BOUND
    if open_buckets <= 1:
        # The fallback picked the only candidate, so it cannot have picked
        # wrong. Distinguished from thread_bound anyway: the receipt should say
        # which question it answered, not just that the answer happens to hold.
        return pin, ATTRIBUTION_SOLE_BUCKET
    return pin, ATTRIBUTION_INFERRED


def active_pin() -> Optional[EpochPin]:
    """The calling thread's pin, resolved through the SRAM bucket binding.

    Attribution-blind by design — callers that only need the pin should not
    have to think about how it was reached. The two callers that PUBLISH what
    they found (``observe``, ``recall_receipt``) use ``resolve_active_pin``
    instead, because publishing an inference as a fact is the failure risk 11
    named.
    """
    pin, _attribution = resolve_active_pin()
    return pin


def pin_for_episode(episode_id: str) -> Optional[EpochPin]:
    with _lock:
        return _pins.get(str(episode_id or ""))


# ── observation (pin-with-fallback, recorded) ───────────────────────────────


def observe(surface: str, served: Any) -> Dict[str, Any]:
    """Record what a read was actually served on ``surface``.

    The substrates are forward-only: a recompiled graph or a reloaded DME pack
    has replaced what this episode pinned, and there is no earlier state to
    read. So the read is served the NEXT snapshot and this records the
    fallback. The returned dict is the receipt fragment for that surface.

    Unpinned reads (no active episode — HUD peeks, diagnostics, boot) return
    ``{"pinned": False}``: an honest "this read had no epoch", not a fake one.

    ``attribution`` (risk 11) says whether this observation is known to belong
    to the episode it was recorded on, or was inferred by the SRAM resolver's
    most-recently-opened fallback while several lanes held buckets.
    """
    pin, attribution = resolve_active_pin()
    if pin is None:
        return {"pinned": False, "surface": str(surface), "served": served}

    pinned_value = pin.pinned_value(surface)
    degraded = surface in pin.degraded
    # A degraded surface was never pinned, so it cannot have diverged — saying
    # "fallback" there would blame the read for a capture-time gap.
    honored = degraded or (served == pinned_value)
    observation: Dict[str, Any] = {
        "pinned": True,
        "surface": str(surface),
        "epoch": int(pin.epoch),
        "pinned_value": pinned_value,
        "served": served,
        "honored": bool(honored),
        "degraded": bool(degraded),
        "attribution": attribution,
    }
    if not honored:
        observation["fallback"] = "next_snapshot"

    with _lock:
        pin.surfaces[str(surface)] = observation
        if attribution == ATTRIBUTION_INFERRED:
            pin.inferred_reads += 1
        if honored:
            pin.honored_reads += 1
        else:
            pin.fallback_reads += 1
            if pin.first_divergence is None:
                pin.first_divergence = dict(observation)
                log.info(
                    "memory epoch %s (epoch=%s): %s moved under this episode "
                    "(pinned=%r served=%r) — served the next snapshot, recorded",
                    pin.episode_id,
                    pin.epoch,
                    surface,
                    pinned_value,
                    served,
                )
    return observation


def recall_receipt() -> Dict[str, Any]:
    """The ``memory_epoch`` block for a recall's ``meta``.

    ``honored`` is the whole-episode answer, not this read's: once an episode
    has been served a snapshot it did not pin, every later read in it inherits
    that doubt, and a receipt that forgot would be the silent failure this cut
    exists to prevent.

    ``attribution`` / ``inferred_reads`` are the risk-11 half: the first says
    how THIS receipt was attributed to this episode, the second how many of the
    episode's recorded reads were attributed by inference rather than by a
    thread that owned them. ``attribution_exact`` is the whole-episode verdict,
    read the same way ``honored`` is — an episode that inferred once is an
    episode whose receipt is approximate, and it says so instead of rounding.
    """
    pin, attribution = resolve_active_pin()
    if pin is None:
        return {"pinned": False}
    with _lock:
        return {
            "pinned": True,
            "episode_id": pin.episode_id,
            "pipeline_id": pin.pipeline_id,
            "work_kind": pin.work_kind,
            "epoch": int(pin.epoch),
            "surfaces": {name: dict(obs) for name, obs in pin.surfaces.items()},
            "degraded": list(pin.degraded),
            "honored": pin.fallback_reads == 0,
            "honored_reads": int(pin.honored_reads),
            "fallback_reads": int(pin.fallback_reads),
            "attribution": attribution,
            "inferred_reads": int(pin.inferred_reads),
            "attribution_exact": pin.inferred_reads == 0
            and attribution != ATTRIBUTION_INFERRED,
            "first_divergence": (
                dict(pin.first_divergence) if pin.first_divergence else None
            ),
        }


# ── sealing (the commit half) ───────────────────────────────────────────────


def seal_episode_epoch(episode: Any) -> Dict[str, Any]:
    """Close the epoch interval inside the commit region, right after the stamp.

    ``memory_epoch_commit`` is this episode's OWN stamp's ``commit_seq`` — no
    second counter is consulted, which is the whole point of putting the epoch
    on the timekeeper. ``causal_parent_episode_ids`` are the journal rows in
    ``(start, commit]``: every episode that crossed the finish line while this
    one was thinking, whose writes this episode's snapshot could not contain.
    That set is the honest answer to "what did I not know when I decided this".

    Ordering note, RE-CHECKED and settled by cut 13. Cut 12 could assume a
    complete interval: one process-wide lock spanned gate entry through the
    journal append, so every lower ``commit_seq`` had already archived its row
    before this ran. Cut 13 moved the stamp inside a narrower commit lock and
    deliberately left ``archive_episode`` outside it, so a lower-seq row can
    still be in flight here — and the decision was to accept the gap and NAME
    it, not to re-seal after the commit region. Re-sealing would have to run
    outside the lock that makes the interval meaningful, which trades a brief
    honest gap for a set that is complete and wrong. So a short set reports
    ``causal_parents_complete=False``, the flag rides the carrier onto the
    PriorEpisode row beside the set it qualifies, and short is now the ORDINARY
    reading for overlapping lanes rather than a defect report.

    Returns the sealed epoch block; also written onto the carrier so it rides
    the enriched body and the journal row.
    """
    if not epochs_enabled():
        return {}

    stamp = getattr(episode, "timekeeper", None)
    stamp = stamp if isinstance(stamp, dict) else {}
    try:
        commit_seq = int(stamp.get("commit_seq") or 0)
    except (TypeError, ValueError):
        commit_seq = 0
    if commit_seq <= 0:
        # Sealing before the stamp is a caller ordering bug, not a substrate
        # condition: the commit epoch IS the stamp, so there is nothing honest
        # to record. Cognition-adjacent bookkeeping fails loud.
        raise RuntimeError(
            "seal_episode_epoch requires a finish-line stamp; "
            "call it after core.timekeeper.finish_line_stamp"
        )

    episode_id = str(getattr(episode, "episode_id", "") or "")
    try:
        start = int(getattr(episode, "memory_epoch_start", 0) or 0)
    except (TypeError, ValueError):
        start = 0

    if commit_seq <= start + 1:
        # The interval (start, commit] holds exactly one sequence number, and
        # it is this episode's OWN stamp — commit_seq values are unique, so no
        # other episode can be in there. Proven empty without touching the
        # journal, which matters because this is the common case (a lane
        # thinking alone) and the alternative is a whole-file read at every
        # deepsleep gate.
        parents: List[str] = []
        complete = True
    else:
        from pressure_loop.episode_journal import episodes_committed_between

        parents, complete = episodes_committed_between(
            start,
            commit_seq,
            exclude_episode_id=episode_id,
        )

    sealed = {
        "start": start,
        "commit": commit_seq,
        "causal_parent_episode_ids": list(parents),
        "causal_parents_complete": bool(complete),
    }
    try:
        episode.memory_epoch_commit = commit_seq
        episode.causal_parent_episode_ids = list(parents)
        # The flag travels on the carrier as its own field, not only inside the
        # pin block: the pin rides the enriched body, but the PriorEpisode row
        # is what carries the parent SET, and a set that can be short needs its
        # qualifier in the same record. Cut 13 made short the ordinary case for
        # overlapping lanes (archive_episode is outside the commit region), so
        # a row without the flag is a row that under-reports silently.
        episode.causal_parents_complete = bool(complete)
        pin_block = getattr(episode, "memory_epoch_pin", None)
        if isinstance(pin_block, dict):
            pin_block["causal_parents_complete"] = bool(complete)
    except Exception:  # noqa: BLE001 — SimpleNamespace carriers in tests
        log.debug("memory epoch: carrier %s rejected sealed epoch fields", episode_id)

    pin = pin_for_episode(episode_id)
    if pin is not None and (pin.fallback_reads or pin.inferred_reads):
        # Two different admissions, deliberately in one line: the snapshot moved
        # under this episode (fallback_reads), and/or some of the reads credited
        # to it were attributed by the SRAM resolver's most-recently-opened
        # fallback while other lanes held buckets (inferred_reads, risk 11).
        log.info(
            "memory epoch %s sealed: start=%s commit=%s parents=%s "
            "fallback_reads=%s inferred_reads=%s (fallback = read across an "
            "epoch change; inferred = attribution the resolver could not prove)",
            episode_id,
            start,
            commit_seq,
            len(parents),
            pin.fallback_reads,
            pin.inferred_reads,
        )
    return sealed


def reset_for_tests() -> None:
    """Drop every pin — hermetic-test hygiene, never called in production."""
    with _lock:
        _pins.clear()
