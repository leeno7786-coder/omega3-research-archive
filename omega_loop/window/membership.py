"""Fixed ten-member cohort manifests and exact canonical hydration (WO-09).

CONTRACTS.md section 4. Before this module a reflection window was a list of episode ids, and
``pressure_loop/temporal_store.hydrate_reflection_window`` rebuilt its evidence by scanning
today's lobe-9 subdomain and keeping, per id, whichever document had the greatest
``(closed_at, id)`` (BOUNDARY_MAP L12). Nothing bound a window to what it was sealed with, so a
later document naming one of its episodes silently replaced that member's evidence.

The cohort is now fixed by exact reference, in three steps that happen in three places:

1. **Membership is pinned as each member joins.** The DeepSleep commit region appends a closing
   episode AFTER its sealed record committed and recall rebuilt over it, and the append pins the
   ``CanonicalRef`` the terminus just bound (``EpisodeStore.commit_reflection_window_append``).
   The append order IS the finish-line order — the append runs inside the one commit region, in
   stamp order. When the tenth member locks the window, its finish-line position, observed close
   time and the trajectory revision then in force are recorded as the cohort's cutoff.
2. **The cohort is sealed against a snapshot** when a window episode consumes it
   (:func:`seal_window`): every pinned reference is resolved through the WO-07 commit view
   against the served projection, the live configured size is asserted against the contract,
   the finish-line order and the cutoff are re-verified against the sealed records, and
   completeness is earned by publication membership, never assumed.
3. **Hydration reads exactly the pinned revisions** (:func:`hydrate_window`): each member is
   read by its record id and its content hash is recomputed. Missing, corrupt or changed members
   raise; nothing is filtered away and nothing is re-chosen by clock.

Nothing here writes. The module holds no store and no clock of its own: membership lives in the
existing reflection checkpoint (references only), order comes from the existing finish-line
stamp, publication from the existing commit view, and configuration from the existing governed
``config/reflection_window.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from schema.continuity_json import (
    CanonicalEpisodeUnavailable,
    CanonicalRef,
    WindowMembershipError,
    WindowReferenceError,
)
from schema.episode_record import EpisodeRecord
from schema.window_evidence import FOCAL_MEMBER_COUNT, SnapshotRef, WindowManifest

#: The revision of the policy this module reads a cohort under. Bumped when what a seal checks,
#: or how hydration resolves a member, changes — a replay names the rules it was read under.
READER_POLICY_REVISION = "omega_loop.window.membership/1"

#: ``SnapshotRef.authority_snapshot_ref`` for every window today, stated rather than invented.
#: The connection authority is per connection (``mcp_platform`` revisions) and WO-07 records it
#: as not captured on every episode carrier; there is no window-wide authority snapshot to bind.
#: The window's own control authority is WO-15's.
AUTHORITY_SNAPSHOT_UNBOUND = (
    "not_captured: no connection authority snapshot is bound to a reflection window"
)

#: Publication states a sealed member may hold. Anything else (pending, failed, unknown) is
#: not a member at all — never a smaller cohort.
_ELIGIBLE = frozenset({"canonical_committed", "recall_published"})

_REFLECTION_WORK = "reflection"


@dataclass(frozen=True)
class HydratedMember:
    """One member read by its exact reference: the sealed record and the document holding it."""

    ref: CanonicalRef
    record: EpisodeRecord
    body: Mapping[str, Any]


# ── the cohort as the checkpoint recorded it ─────────────────────────────────


def _window_id(window: Any) -> str:
    return str(getattr(window, "window_id", "") or "")


def _require_filled_cohort(window: Any) -> list[str]:
    """The window is a filled cohort under the configuration it was collected with."""
    from pressure_loop.reflection_window import (
        STATUS_LOCKED,
        configured_window_size,
        window_configuration_fingerprint,
    )

    window_id = _window_id(window)
    configured = configured_window_size()
    if configured != FOCAL_MEMBER_COUNT:
        raise WindowMembershipError(
            f"the configured reflection window size is {configured}, but a focal cohort is "
            f"exactly {FOCAL_MEMBER_COUNT} members; no cohort is sealed under a size the "
            "contract does not hold"
        )
    episodes = [str(item) for item in (getattr(window, "episodes", None) or ())]
    if str(getattr(window, "status", "") or "") != STATUS_LOCKED:
        raise WindowMembershipError(
            f"window {window_id!r} is still collecting ({len(episodes)} of {configured}); only "
            "a filled cohort is sealed"
        )
    if len(episodes) != configured:
        raise WindowMembershipError(
            f"window {window_id!r} holds {len(episodes)} members, not {configured}; a cohort is "
            "never sealed smaller or larger than its configured size"
        )
    if len(set(episodes)) != len(episodes):
        raise WindowMembershipError(f"window {window_id!r} names a member twice")
    collected_under = str(getattr(window, "configuration_fingerprint", "") or "")
    live = window_configuration_fingerprint()
    if collected_under != live:
        raise WindowMembershipError(
            f"window {window_id!r} was collected under configuration {collected_under or 'none'!r} "
            f"and the live configuration is {live!r}; a cohort is sealed under the rules it "
            "was collected by"
        )
    return episodes


def _pinned_refs(window: Any, episodes: list[str]) -> list[tuple[str, CanonicalRef]]:
    refs = dict(getattr(window, "member_refs", None) or {})
    pinned: list[tuple[str, CanonicalRef]] = []
    for episode_id in episodes:
        raw = refs.get(episode_id)
        if raw is None:
            raise WindowMembershipError(
                f"member {episode_id!r} of window {_window_id(window)!r} has no pinned canonical "
                "reference: nothing records which sealed record joined the cohort, and a member "
                "is never dropped to make a smaller one"
            )
        pinned.append((episode_id, CanonicalRef.coerce(raw, where=f"member_refs[{episode_id}]")))
    return pinned


# ── the snapshot ─────────────────────────────────────────────────────────────


def window_snapshot(window: Any) -> SnapshotRef:
    """The snapshot a filled cohort is sealed against, from its checkpoint and the served view.

    ``generation_id`` names the recall projection being served now — the retained generation
    under a generation owner, the compiled graph's snapshot hash under the legacy owner.
    ``coverage_clock`` and ``visibility_cutoff`` are the cohort's cutoff: the finish-line
    position and observed close time of the member that filled it, recorded at the lock. The
    trajectory revisions were recorded at open and at the lock. None of it is recomputed here.
    """
    from memory.episode_commit_view import published_projection_id

    window_id = _window_id(window)
    cutoff = dict(getattr(window, "cutoff", None) or {})
    if not cutoff:
        raise WindowMembershipError(
            f"window {window_id!r} recorded no cutoff when it filled; a snapshot is never bound "
            "to an invented cutoff"
        )
    start = str(getattr(window, "trajectory_revision_at_start", "") or "")
    at_cutoff = str(getattr(window, "trajectory_revision_at_cutoff", "") or "")
    if not start or not at_cutoff:
        raise WindowMembershipError(
            f"window {window_id!r} recorded no trajectory revision at "
            f"{'start' if not start else 'cutoff'}"
        )
    return SnapshotRef(
        generation_id=published_projection_id(),
        coverage_clock={"clock_epoch_id": cutoff["clock_epoch_id"],
                        "commit_seq": cutoff["commit_seq"]},
        visibility_cutoff=str(cutoff["closed_at"]),
        trajectory_revision_at_start=start,
        trajectory_revision_at_cutoff=at_cutoff,
        authority_snapshot_ref=AUTHORITY_SNAPSHOT_UNBOUND,
        reader_policy_revision=READER_POLICY_REVISION,
    )


# ── seal ─────────────────────────────────────────────────────────────────────


def _finish_line_position(episode_id: str, record: EpisodeRecord) -> tuple[str, int]:
    clock = record.temporal["coverage_clock"]
    finish = record.temporal["finish_line"]
    if not isinstance(clock, Mapping) or not isinstance(finish, Mapping):
        raise WindowMembershipError(
            f"member {episode_id!r} has no finish-line position; unknown order is not invented, "
            "and a record without one cannot be a focal member"
        )
    if int(finish["commit_seq"]) != int(clock["commit_seq"]):
        raise WindowReferenceError(
            f"member {episode_id!r} carries two different finish-line numbers"
        )
    return str(clock["clock_epoch_id"]), int(clock["commit_seq"])


def seal_window(window, *, snapshot: SnapshotRef) -> WindowManifest:
    """Fix *window*'s cohort against *snapshot*, ordered by the existing finish-line authority.

    Every member is the exact reference its commit region pinned. Each is resolved through the
    WO-07 commit view against the snapshot: a pending, failed or unknown record raises; a record
    that is committed but not in the snapshot's recall projection seals as a member of an
    INCOMPLETE cohort, which is never hydrated. Failures, cancellations and idle turns are
    members like any other closed episode; a reflection never is.
    """
    from memory.episode_commit_view import resolve_episode_commit

    snap = SnapshotRef.coerce(snapshot)
    window_id = _window_id(window)
    episodes = _require_filled_cohort(window)
    members: list[dict[str, Any]] = []
    last: tuple[str, int] | None = None
    last_record: EpisodeRecord | None = None
    for episode_id, ref in _pinned_refs(window, episodes):
        view = resolve_episode_commit(ref, snapshot=snap)
        status = view.memory_commit_status
        if status not in _ELIGIBLE or view.record is None:
            raise WindowMembershipError(
                f"member {episode_id!r} of window {window_id!r} is {status!r} ({view.reason}); "
                "a cohort admits only canonically committed members"
            )
        record = view.record
        if record.episode_id != episode_id:
            raise WindowReferenceError(
                f"member {episode_id!r} is pinned to a record of episode {record.episode_id!r}"
            )
        if str(record.identity["work_kind"]) == _REFLECTION_WORK:
            raise WindowMembershipError(
                f"member {episode_id!r} is a reflection, not fresh experience; a window never "
                "reflects on reflections"
            )
        position = _finish_line_position(episode_id, record)
        if position[0] != snap.coverage_clock.clock_epoch_id:
            raise WindowMembershipError(
                f"member {episode_id!r} was stamped under clock epoch {position[0]!r}, not the "
                f"cohort's {snap.coverage_clock.clock_epoch_id!r}; order across clock epochs is "
                "not comparable and is never invented"
            )
        if last is not None and position[1] <= last[1]:
            raise WindowMembershipError(
                f"member {episode_id!r} (commit_seq {position[1]}) does not follow the member "
                f"before it (commit_seq {last[1]}); the checkpoint order disagrees with the "
                "finish-line authority"
            )
        last, last_record = position, record
        members.append({
            "episode_id": episode_id,
            "ref": ref,
            "finish_line_commit_seq": position[1],
            "cascade_status": record.outcome.cascade_status,
            "memory_commit_status": status,
        })

    if last is None or last_record is None:
        raise WindowMembershipError(f"window {window_id!r} has no members to seal")
    if last[1] != snap.coverage_clock.commit_seq:
        raise WindowMembershipError(
            f"window {window_id!r}'s cutoff is commit_seq {snap.coverage_clock.commit_seq}, but "
            f"its last member was stamped {last[1]}; the cutoff is the member that filled it"
        )
    closed_at = last_record.temporal["closed_at"]
    if not closed_at.is_observed or str(closed_at.value) != snap.visibility_cutoff:
        raise WindowMembershipError(
            f"window {window_id!r}'s visibility cutoff {snap.visibility_cutoff!r} is not the "
            "observed close time of the member that filled it"
        )
    return WindowManifest(
        window_id=window_id,
        # Membership is fixed at the lock and never revised, so a cohort has one version.
        window_version=1,
        configuration_fingerprint=str(window.configuration_fingerprint),
        scope="focal",
        snapshot=snap,
        complete=all(row["memory_commit_status"] == "recall_published" for row in members),
        members=tuple(members),
    )


# ── hydrate ──────────────────────────────────────────────────────────────────


def _hydrate_members(
    manifest: WindowManifest, *, include_supplements: bool,
) -> tuple[HydratedMember, ...]:
    """Validate immutable member content, optionally adding snapshot-bound body evidence."""
    from memory.episode_commit_view import canonical_episode_body, snapshot_graph
    from memory.episode_event_publication import graph_supplements

    sealed = WindowManifest.coerce(manifest)
    if not sealed.complete:
        unpublished = [row["episode_id"] for row in sealed.members
                       if row["memory_commit_status"] != "recall_published"]
        raise WindowMembershipError(
            f"window {sealed.window_id!r} is not complete: {unpublished} are not published in "
            f"snapshot {sealed.snapshot.generation_id[:12]}…; an incomplete cohort is never "
            "hydrated for reflection"
        )
    hydrated: list[HydratedMember] = []
    for row in sealed.members:
        episode_id = str(row["episode_id"])
        ref: CanonicalRef = row["ref"]
        body, why = canonical_episode_body(ref.record_id)
        if body is None:
            raise CanonicalEpisodeUnavailable(
                f"member {episode_id!r}: the pinned record {ref.record_id!r} is missing ({why})"
            )
        try:
            record = EpisodeRecord.from_dict(body["episode_record"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CanonicalEpisodeUnavailable(
                f"member {episode_id!r}: the pinned record {ref.record_id!r} is corrupt: {exc}"
            ) from exc
        if record.content_sha256() != ref.content_sha256:
            raise CanonicalEpisodeUnavailable(
                f"member {episode_id!r}: the record under {ref.record_id!r} no longer holds the "
                f"pinned revision {ref.content_sha256[:12]}…; a changed member is never read as "
                "the one the window was sealed with"
            )
        if record.episode_id != episode_id:
            raise WindowReferenceError(
                f"member {episode_id!r} resolves to a record of episode {record.episode_id!r}"
            )
        if (int(record.temporal["finish_line"]["commit_seq"]) != int(row["finish_line_commit_seq"])
                or record.outcome.cascade_status != row["cascade_status"]):
            raise WindowReferenceError(
                f"member {episode_id!r}: the manifest's finish line or cascade status disagrees "
                "with the sealed record it pins"
            )
        # The body is the document freshly parsed from disk for this read: no caller shares it.
        # Later terminus failures are immutable children of this exact seal. Read them from
        # the manifest's projection, never from today's canonical files or pending intake.
        if include_supplements:
            graph = snapshot_graph(sealed.snapshot)
            body["supplemental_events"] = [
                event.to_dict() for event in graph_supplements(graph, ref)
            ]
        hydrated.append(HydratedMember(ref=ref, record=record, body=body))
    return tuple(hydrated)


def hydrate_window_members(manifest: WindowManifest) -> tuple[HydratedMember, ...]:
    """Read exact member bodies and later evidence visible in the manifest's snapshot."""
    return _hydrate_members(manifest, include_supplements=True)


def hydrate_window(manifest: WindowManifest) -> tuple[EpisodeRecord, ...]:
    """Read immutable sealed records; later projection activity cannot change these bytes."""
    return tuple(member.record for member in _hydrate_members(manifest, include_supplements=False))


def seal_and_hydrate(window: Any) -> tuple[WindowManifest, tuple[HydratedMember, ...]]:
    """Seal a staged cohort against the served projection and read it, as one consistent read.

    Held under the ONE commit lock: the legacy projection is recompiled only inside a
    terminus's commit region, so no terminus can replace the served graph between the seal
    proving each member's publication and hydration reading the members. Read-only; nothing
    inside acquires a pipeline lock, so the ordering law (own pipeline lock → commit lock) holds.
    """
    from core.cognition_lock import commit_lock

    with commit_lock():
        manifest = seal_window(window, snapshot=window_snapshot(window))
        return manifest, hydrate_window_members(manifest)


__all__ = [
    "AUTHORITY_SNAPSHOT_UNBOUND",
    "READER_POLICY_REVISION",
    "HydratedMember",
    "hydrate_window",
    "hydrate_window_members",
    "seal_and_hydrate",
    "seal_window",
    "window_snapshot",
]
