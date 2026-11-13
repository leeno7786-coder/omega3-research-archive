"""Pinned history, host relations and a reproducible retrieval audit for a sealed window (WO-10).

``collect_history(manifest, episodes, *, limits)`` answers three questions about a sealed cohort
without adding a store, a clock or a model pass:

1. **What led to what, by the host's own record.** Relations come only from evidence a host
   component recorded: the admission origin (``triggered_by``), the ``continued`` causal fact
   (``continued_from``), the recall exposure (``admitted_from`` / ``read_from`` — only ADMITTED
   inputs; a record retrieved and then excluded was never supplied), artifact and verification
   receipts bound to exact bytes (``produces`` / ``tests``), operator feedback (``evaluates``),
   the sealed concurrent-commit set (``concurrent_commit``) and the finish-line order
   (``commit_precedes``). Chronology and information dependency are separate relations:
   committing during another episode is never reading from it. Nothing a model says becomes a
   relation; a reflection is a *prior finding* with the citations the host recorded for it.
2. **What history the cohort may cite, as of its snapshot.** The corpus is the recall projection
   the manifest's ``SnapshotRef`` names, read through ``memory.episode_commit_view.
   snapshot_graph``: a retained, receipt-bound generation (immutable, so rewriting today's files
   cannot change an earlier replay) or, under the legacy owner, the served graph only while it is
   still the one sealed against — after a recompile the history is
   ``HistoricalSnapshotUnavailable``. Visibility is bounded by the snapshot's coverage clock, so a
   record committed after the cutoff does not exist for this read. **Retention is the existing
   lease-aware GC (D-13-4):** it keeps the active generation, the previous one and every leased
   one. This read enrols as a reader while it reads; a replay that must outlive two later
   publications is the caller's to lease, and without one the snapshot is collected and reads as
   unavailable — never as today's graph.
3. **How the selection was made, so it can be reproduced.** A pinned policy
   (``HISTORY_POLICY_REVISION``): explicit related records first, then prior findings, then
   scoped comparable cases, then counterexamples; canonical-id order inside each class; the cap
   applied last. The audit records the scope, every considered / returned / admitted id and every
   omission with its reason, and the counterexample search's own state (``search_not_run``,
   ``searched_no_matches``, ``searched_found_matches`` or ``reader_failed``).

**Relation kinds with no host evidence are not produced.** ``revises`` needs the prior bytes an
artifact revision replaced, and the receipts record only the new digest, so a revision is
reported as ``produces`` and never promoted to ``revises`` on the strength of a shared path.

Reflections are never primary evidence. Reflections that cite the same members are ONE support
unit (a retried reflection over one cohort adds nothing), and no historical support ever joins
the focal cohort or its count.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from memory.operator_feedback import OPERATOR_FEEDBACK_RECEIVED
from schema.continuity_json import (
    CanonicalEpisodeUnavailable,
    CanonicalRef,
    ContinuityContractError,
    Observation,
    WindowMembershipError,
    WindowReferenceError,
    content_hash,
)
from schema.episode_record import EpisodeRecord
from schema.window_evidence import HistoryBundle, HistoryLimits, SnapshotRef, WindowManifest

#: The retrieval policy this module implements. A ``HistoryLimits`` naming any other revision is
#: refused: a replay must be read under the rules it names, not whatever the code does today.
HISTORY_POLICY_REVISION = "omega_loop.window.history/1"

#: Every relation kind this module can produce, each from one host evidence source.
HOST_RELATION_KINDS: tuple[str, ...] = (
    "admitted_from",
    "commit_precedes",
    "concurrent_commit",
    "continued_from",
    "evaluates",
    "produces",
    "read_from",
    "tests",
    "triggered_by",
)

#: Retrieval classes, in admission priority order.
RETRIEVAL_CLASSES: tuple[str, ...] = ("explicit", "prior_finding", "comparable", "counterexample")

_POLARITY = {"success": 1, "failure": -1, "partial": -1, "blocked": -1}
_NO_CRITERIA = "none:"
_REFLECTION = "reflection"

_ORIGIN = "admission.origin"
_OPENED = "admission.episode_opened"
_EXPOSURE = "auto.input_exposure"
_ARTIFACT = "auto.artifact_revision"
_VERIFICATION = "auto.verification_result"
_FEEDBACK = OPERATOR_FEEDBACK_RECEIVED
_MANIFEST = "window.manifest_admitted"


def history_limits() -> HistoryLimits:
    """The governed history limits (``config/reflection_window.yaml`` → ``history``). Fail-hard."""
    from pressure_loop.reflection_window import window_configuration

    section = window_configuration().get("history")
    if not isinstance(section, Mapping):
        raise RuntimeError("reflection window configuration has no 'history' section")
    try:
        return HistoryLimits(
            record_cap=section["record_cap"],
            token_candidate_cap=section["token_candidate_cap"],
            retrieval_policy_revision=section["retrieval_policy_revision"],
        )
    except (KeyError, ContinuityContractError) as exc:
        raise RuntimeError(f"reflection window history limits are invalid: {exc}") from exc


# ── the corpus, as of the snapshot ───────────────────────────────────────────


@dataclass(frozen=True)
class _Entry:
    episode_id: str
    ref: CanonicalRef
    record: EpisodeRecord
    position: tuple[str, int]


@dataclass
class _Corpus:
    entries: dict[str, _Entry]
    facts: list[tuple[str, Mapping[str, Any]]]
    omitted: list[dict[str, str]]
    unreadable: list[str]


def _snapshot_rows(graph: Path) -> list[tuple[Any, ...]]:
    """Every event row of the snapshot's graph, in storage order (callers never rely on it)."""
    from memory.taxonomy.graph_store import connect_readonly

    connection = connect_readonly(graph)
    try:
        return [tuple(row) for row in connection.execute(
            "SELECT event_id, event_kind, episode_id, payload_json FROM events"
        ).fetchall()]
    finally:
        connection.close()


def _position(record: EpisodeRecord) -> Optional[tuple[str, int]]:
    clock = record.temporal["coverage_clock"]
    if not isinstance(clock, Mapping):
        return None
    return str(clock["clock_epoch_id"]), int(clock["commit_seq"])


def _record_id(episode_id: str) -> str:
    from memory.episode_record_seal import episode_record_id

    return episode_record_id(episode_id)


def _read_corpus(snapshot: SnapshotRef) -> _Corpus:
    from memory.episode_commit_view import leased_snapshot_graph

    # Enrolled as a reader of the generation while the rows are read, so lease-aware GC cannot
    # collect the snapshot under this read; an already collected one is unavailable, loudly.
    with leased_snapshot_graph(snapshot) as graph:
        rows = _snapshot_rows(graph)
    epoch, cutoff = snapshot.coverage_clock.clock_epoch_id, snapshot.coverage_clock.commit_seq
    corpus = _Corpus(entries={}, facts=[], omitted=[], unreadable=[])
    for row in sorted(rows, key=lambda r: str(r[0])):
        event_id, kind = str(row[0]), str(row[1] or "")
        try:
            payload = json.loads(row[3] or "{}")
        except ValueError:
            payload = None
        if kind == "causal_edge":
            if isinstance(payload, Mapping) and payload.get("kind") and payload.get("source") \
                    and payload.get("target"):
                corpus.facts.append((event_id, payload))
            continue
        if not event_id.startswith("episode-"):
            continue
        sealed = payload.get("episode_record") if isinstance(payload, Mapping) else None
        if not isinstance(sealed, Mapping):
            corpus.omitted.append({"id": event_id,
                                   "reason": "unsealed: the record predates sealed episode records"})
            continue
        try:
            record = EpisodeRecord.from_dict(dict(sealed))
        except (ValueError, KeyError, TypeError) as exc:
            corpus.unreadable.append(event_id)
            corpus.omitted.append({"id": event_id, "reason": f"unreadable: {exc}"[:500]})
            continue
        position = _position(record)
        if position is None:
            corpus.omitted.append({"id": event_id, "reason": "unsequenced: the record has no "
                                   "finish-line position, and order is never invented"})
            continue
        if position[0] != epoch:
            corpus.omitted.append({"id": event_id, "reason": "clock_epoch_mismatch: order across "
                                   "clock epochs is not comparable"})
            continue
        if position[1] > cutoff:
            continue  # after the cutoff: it does not exist for this read, not even as an omission
        ref = CanonicalRef(record_id=_record_id(record.episode_id), record_version=1,
                           content_sha256=record.content_sha256())
        corpus.entries[record.episode_id] = _Entry(record.episode_id, ref, record, position)
    return corpus


# ── the members ──────────────────────────────────────────────────────────────


def _checked_members(manifest: WindowManifest, episodes: Iterable[Any],
                     corpus: _Corpus) -> list[_Entry]:
    records = tuple(episodes)
    if len(records) != manifest.member_count:
        raise WindowReferenceError(
            f"{len(records)} records were supplied for {manifest.member_count} sealed members"
        )
    members: list[_Entry] = []
    for row, record in zip(manifest.members, records):
        if not isinstance(record, EpisodeRecord):
            raise ContinuityContractError("collect_history takes the hydrated EpisodeRecords")
        ref: CanonicalRef = row["ref"]
        if record.episode_id != row["episode_id"] or record.content_sha256() != ref.content_sha256:
            raise WindowReferenceError(
                f"supplied record {record.episode_id!r} is not the sealed member "
                f"{row['episode_id']!r} ({ref.content_sha256[:12]}…); history is collected for "
                "exactly the manifest's members, in its order"
            )
        entry = corpus.entries.get(record.episode_id)
        if entry is None or entry.ref.content_sha256 != ref.content_sha256:
            raise CanonicalEpisodeUnavailable(
                f"the snapshot {manifest.snapshot.generation_id[:12]}… does not hold sealed member "
                f"{record.episode_id!r} at the revision the manifest pins"
            )
        members.append(entry)
    return members


# ── relations from host evidence ─────────────────────────────────────────────


def _events(record: EpisodeRecord, kind: str) -> list[Any]:
    return [event for event in record.events["by_value"] if event.event_kind == kind]


def _ep(episode_id: str) -> str:
    return f"episode:{episode_id}"


def _link(kind: str, source: str, target: str, evidence: Iterable[str],
          detail: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    return {"relation_kind": kind, "source": source, "target": target,
            "evidence_refs": sorted({str(item) for item in evidence if str(item)}),
            "author": "host", "detail": dict(detail or {})}


def _observed_value(value: Any) -> Any:
    """The value of an Observation, however the sealed payload spelled it; None when absent."""
    if isinstance(value, Observation):
        return value.value if value.is_observed else None
    if isinstance(value, Mapping) and "state" in value:
        return value.get("value") if value.get("state") == "observed" else None
    return value


def _artifact(path: Any, sha: Any) -> str:
    return f"artifact:{path}@{sha}"


def _producers(entries: Iterable[_Entry]) -> dict[str, str]:
    """artifact ref → the EARLIEST visible episode that produced those exact bytes."""
    found: dict[str, tuple[tuple[str, int], str]] = {}
    for entry in entries:
        for event in _events(entry.record, _ARTIFACT):
            payload = event.payload
            if payload.get("change_kind") == "deleted" or not payload.get("content_sha256"):
                continue
            key = _artifact(payload.get("path"), payload.get("content_sha256"))
            prior = found.get(key)
            if prior is None or entry.position < prior[0]:
                found[key] = (entry.position, entry.episode_id)
    return {key: episode_id for key, (_pos, episode_id) in found.items()}


def _relations(members: list[_Entry], corpus: _Corpus) -> list[dict[str, Any]]:
    member_ids = {entry.episode_id for entry in members}
    visible = corpus.entries
    rows: list[dict[str, Any]] = []

    for before, after in zip(members, members[1:]):
        rows.append(_link("commit_precedes", _ep(before.episode_id), _ep(after.episode_id),
                          (before.ref.record_id, after.ref.record_id),
                          {"before_commit_seq": before.position[1],
                           "after_commit_seq": after.position[1]}))

    producers = _producers(visible.values())
    for entry in members:
        record, source = entry.record, _ep(entry.episode_id)
        concurrent = record.temporal["concurrent_commits"]
        if isinstance(concurrent, Mapping):
            for peer in concurrent.get("episode_ids") or ():
                rows.append(_link("concurrent_commit", source, _ep(str(peer)),
                                  (f"{entry.ref.record_id}#temporal.concurrent_commits",),
                                  {"set_complete": bool(concurrent.get("complete"))}))
        for event in _events(record, _ORIGIN):
            payload = event.payload
            request = str(payload.get("queue_id") or payload.get("cycle_id") or "")
            if request:
                rows.append(_link("triggered_by", source, f"request:{request}", (event.event_id,), {
                    "contributor_source": str(payload.get("contributor_source") or ""),
                    "contributor_type": str(payload.get("contributor_type") or ""),
                    "opened_by": str(payload.get("opened_by") or ""),
                    "arrival_seq": int(payload.get("arrival_seq") or 0),
                }))
        for event in _events(record, _EXPOSURE):
            for memory_id in event.payload.get("admitted") or ():
                memory_id = str(memory_id)
                rows.append(_link("admitted_from", source, f"memory:{memory_id}", (event.event_id,)))
                read = next((other for other in visible.values()
                             if other.ref.record_id == memory_id), None)
                if read is not None and read.episode_id != entry.episode_id:
                    rows.append(_link("read_from", source, _ep(read.episode_id),
                                      (event.event_id,), {"memory_id": memory_id}))
        for event in _events(record, _ARTIFACT):
            payload = event.payload
            if payload.get("change_kind") != "deleted" and payload.get("content_sha256"):
                rows.append(_link("produces", source,
                                  _artifact(payload.get("path"), payload.get("content_sha256")),
                                  (event.event_id,), {"revision": payload.get("revision"),
                                                      "change_kind": payload.get("change_kind")}))
        for event in _events(record, _VERIFICATION):
            payload = event.payload
            target = _artifact(payload.get("path"), payload.get("content_sha256"))
            producer = producers.get(target)
            rows.append(_link("tests", source, target, (event.event_id,), {
                "criterion": str(payload.get("criterion") or ""),
                "passed": bool(payload.get("passed")),
                "produced_by": _ep(producer) if producer else None,
            }))

    for fact_event, fact in corpus.facts:
        if fact.get("kind") != "continued":
            continue
        recorder = str(fact.get("evidence_event_id") or "").removeprefix("episode-")
        continuation = str(dict(fact.get("detail") or {}).get("continuation_episode_id") or "")
        parent = str(fact.get("source") or "").removeprefix("episode:")
        if recorder not in visible or continuation not in visible or parent not in visible:
            continue  # a fact is as-of too: its evidence must exist in this snapshot
        if continuation in member_ids or parent in member_ids:
            rows.append(_link("continued_from", _ep(continuation), _ep(parent),
                              (fact_event, str(fact.get("evidence_event_id") or "")),
                              {"continuation": str(fact.get("target") or "")}))

    for entry in visible.values():
        for event in _events(entry.record, _FEEDBACK):
            about = str(event.payload.get("about_episode_id") or "")
            if about in visible and (about in member_ids or entry.episode_id in member_ids):
                rows.append(_link("evaluates", _ep(entry.episode_id), _ep(about), (event.event_id,), {
                    "feedback": str(event.payload.get("feedback") or ""),
                    "received_after_close": bool(event.payload.get("received_after_close")),
                }))

    unique = {json.dumps(row, sort_keys=True, default=str): row for row in rows}
    return [unique[key] for key in sorted(unique)]


# ── retrieval ────────────────────────────────────────────────────────────────


def _polarity(record: EpisodeRecord) -> int:
    return _POLARITY.get(record.outcome.task_outcome, 0)


def _scope_keys(record: EpisodeRecord) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    thread = record.identity["thread_id"]
    if isinstance(thread, str) and thread.strip():
        keys.add(("thread_id", thread))
    criteria = record.objective["criteria_revision"]
    if isinstance(criteria, str) and criteria.strip() and not criteria.startswith(_NO_CRITERIA):
        keys.add(("criteria_revision", criteria))
    return keys


def _cites(entry: _Entry) -> tuple[list[str], list[str], list[str]]:
    """(cited record ids, window ids, evidence event ids) a reflection's host record names."""
    cited, windows, evidence = set(), set(), set()
    for event in _events(entry.record, _MANIFEST):
        for ref in event.payload.get("member_refs") or ():
            cited.add(str(dict(ref).get("record_id") or ""))
        windows.add(str(event.payload.get("window_id") or ""))
        evidence.add(event.event_id)
    return sorted(cited - {""}), sorted(windows - {""}), sorted(evidence)


def _classify(members: list[_Entry], corpus: _Corpus,
              links: list[dict[str, Any]]) -> tuple[dict[str, list], str]:
    member_ids = {entry.episode_id for entry in members}
    history = {eid: entry for eid, entry in corpus.entries.items() if eid not in member_ids}
    related: dict[str, set[str]] = {}
    for row in links:
        # A relation's endpoints, plus the episode a ``tests`` relation found producing the
        # exact bytes checked: that producer is related to the member by the same receipts.
        ends = [row["source"], row["target"], dict(row["detail"]).get("produced_by") or ""]
        for end in ends:
            eid = end.removeprefix("episode:") if end.startswith("episode:") else ""
            if eid in history:
                related.setdefault(eid, set()).add(f"relation:{row['relation_kind']}")

    member_scopes = [(entry, _scope_keys(entry.record)) for entry in members]
    polar = [(entry, keys) for entry, keys in member_scopes if _polarity(entry.record)]
    classes: dict[str, list] = {name: [] for name in RETRIEVAL_CLASSES}
    counter_found = False

    reflections = [entry for entry in history.values() if entry.record.identity["work_kind"] == _REFLECTION]
    for entry in sorted(history.values(), key=lambda e: e.ref.record_id):
        if entry.record.identity["work_kind"] == _REFLECTION:
            continue
        reasons = set(related.get(entry.episode_id, ()))
        if reasons:
            classes["explicit"].append((entry, sorted(reasons)))
            continue
        keys = _scope_keys(entry.record)
        shared = [(member, sorted(keys & member_keys)) for member, member_keys in member_scopes
                  if keys & member_keys]
        if not shared:
            continue
        opposite = [member for member, _keys in shared
                    if _polarity(member.record) and _polarity(entry.record)
                    and _polarity(member.record) != _polarity(entry.record)]
        scope = sorted({f"comparable:{name}" for _member, names in shared for name, _v in names})
        if opposite:
            counter_found = True
            classes["counterexample"].append(
                (entry, scope + sorted(f"counters:{m.episode_id}" for m in opposite)))
        else:
            classes["comparable"].append((entry, scope))

    groups: dict[tuple[str, ...], list[_Entry]] = {}
    for entry in reflections:
        cited, _windows, _evidence = _cites(entry)
        key = tuple(cited) if cited else (f"uncited:{entry.ref.record_id}",)
        groups.setdefault(key, []).append(entry)
    for key in sorted(groups, key=lambda k: min(e.ref.record_id for e in groups[k])):
        classes["prior_finding"].append(sorted(groups[key], key=lambda e: e.ref.record_id))

    if not polar:
        status = "search_not_run"
    elif corpus.unreadable:
        status = "reader_failed"
    elif counter_found:
        status = "searched_found_matches"
    else:
        status = "searched_no_matches"
    return classes, status


def _as_of(entry: _Entry) -> dict[str, Any]:
    return {"clock_epoch_id": entry.position[0], "commit_seq": entry.position[1]}


def _summary(record: EpisodeRecord) -> str:
    outcome = record.outcome
    return (f"{record.identity['work_kind']} episode ({record.identity['lane']}): "
            f"task_outcome={outcome.task_outcome}, verification={outcome.verification_status}, "
            f"cascade={outcome.cascade_status}")[:500]


# ── direction changes ────────────────────────────────────────────────────────


def _direction_changes(members: list[_Entry]) -> list[dict[str, Any]]:
    from pressure_loop.reflection_window import trajectory_revision

    rows: list[dict[str, Any]] = []
    for kind in ("trajectory", "connection_authority"):
        last: Optional[tuple[str, _Entry]] = None
        gap: list[str] = []
        for entry in members:
            revision, authority, evidence = _direction_of(entry, kind, trajectory_revision)
            if revision is None:
                gap.append(entry.episode_id)
                continue
            if last is not None and revision != last[0]:
                rows.append({
                    "change_id": content_hash([kind, last[0], revision, entry.episode_id])[:24],
                    "support_class": "direction_change",
                    "kind": kind,
                    "from_revision": last[0],
                    "to_revision": revision,
                    "effective_episode_id": entry.episode_id,
                    "effective_position": _as_of(entry),
                    "authority": authority,
                    "evidence_refs": evidence,
                    "unobserved_between": list(gap),
                })
            last, gap = (revision, entry), []
    return rows


def _direction_of(entry: _Entry, kind: str, trajectory_revision) -> tuple[Optional[str], str, list[str]]:
    if kind == "trajectory":
        for event in _events(entry.record, _OPENED):
            snapshot = _observed_value(event.payload.get("trajectory_reference"))
            if isinstance(snapshot, Mapping) and snapshot:
                cycle = str(snapshot.get("cycle_id") or "")
                authority = "operator" if cycle == "onboarding" else ("omega" if cycle else "unknown")
                return trajectory_revision(dict(snapshot)), authority, [event.event_id]
        return None, "unknown", []
    for event in _events(entry.record, _ORIGIN):
        revision = str(event.payload.get("authority_revision") or "")
        if revision:
            return f"connection_authority:{revision}", "operator", [event.event_id]
    return None, "unknown", []


# ── the bundle ───────────────────────────────────────────────────────────────


def collect_history(manifest: WindowManifest, episodes: tuple[EpisodeRecord, ...], *,
                    limits: HistoryLimits) -> HistoryBundle:
    """Pinned history, typed host relations and a reproducible retrieval audit for a sealed cohort.

    ``episodes`` are the hydrated member records, in manifest order (``hydrate_window``). The
    corpus is read as of ``manifest.snapshot``; history never includes a focal member, never
    counts a reflection as primary evidence, and never changes the cohort or its count.
    """
    sealed = WindowManifest.coerce(manifest)
    if not isinstance(limits, HistoryLimits):
        raise ContinuityContractError("collect_history takes HistoryLimits")
    if limits.retrieval_policy_revision != HISTORY_POLICY_REVISION:
        raise ContinuityContractError(
            f"history limits name retrieval policy {limits.retrieval_policy_revision!r}, and this "
            f"code implements {HISTORY_POLICY_REVISION!r}; a history is read under the policy it names"
        )
    if not sealed.complete:
        raise WindowMembershipError(
            f"window {sealed.window_id!r} is not complete; history is collected only for a cohort "
            "every member of which is published in its snapshot"
        )
    corpus = _read_corpus(sealed.snapshot)
    members = _checked_members(sealed, episodes, corpus)
    links = _relations(members, corpus)
    classes, status = _classify(members, corpus, links)

    returned: list[str] = []
    admitted: list[str] = []
    omitted = list(corpus.omitted)
    primary: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    units_used = 0
    cited_by: dict[str, list[str]] = {}

    ordered: list[tuple[str, Any]] = []
    for name in RETRIEVAL_CLASSES:
        ordered.extend((name, item) for item in classes[name])
    for name, item in ordered:
        ids = ([entry.ref.record_id for entry in item] if name == "prior_finding"
               else [item[0].ref.record_id])
        returned.extend(ids)
        if units_used >= limits.record_cap:
            omitted.extend({"id": rid, "reason": "cap"} for rid in ids)
            continue
        units_used += 1
        admitted.extend(ids)
        if name == "prior_finding":
            cited, windows, evidence = set(), set(), set()
            for entry in item:
                c, w, e = _cites(entry)
                cited.update(c)
                windows.update(w)
                evidence.update(e)
            unit_id = item[0].ref.record_id
            for rid in cited:
                cited_by.setdefault(rid, []).append(unit_id)
            findings.append({
                "unit_id": unit_id,
                "support_class": "prior_finding",
                "author": "model",
                "reflection_episode_ids": [entry.episode_id for entry in item],
                "refs": [entry.ref for entry in item],
                "as_of": _as_of(max(item, key=lambda e: e.position)),
                "cites": sorted(cited),
                "window_ids": sorted(windows),
                "summary": (f"{len(item)} reflection record(s) over "
                            f"{', '.join(sorted(windows)) or 'an uncited window'}")[:500],
                "evidence_refs": sorted(evidence) or [entry.ref.record_id for entry in item],
            })
        else:
            entry, reasons = item
            primary.append({
                "unit_id": entry.ref.record_id,
                "support_class": "historical",
                "retrieval_class": name,
                "ref": entry.ref,
                "episode_id": entry.episode_id,
                "as_of": _as_of(entry),
                "task_outcome": entry.record.outcome.task_outcome,
                "verification_status": entry.record.outcome.verification_status,
                "cascade_status": entry.record.outcome.cascade_status,
                "summary": _summary(entry.record),
                "reasons": list(reasons),
                "evidence_refs": [entry.ref.record_id],
            })
    for row in primary:
        row["cited_by"] = sorted(cited_by.get(row["unit_id"], ()))

    member_ids = {entry.episode_id for entry in members}
    considered = sorted(
        [entry.ref.record_id for eid, entry in corpus.entries.items() if eid not in member_ids]
        + [row["id"] for row in corpus.omitted]
    )
    snapshot = sealed.snapshot
    scope = (
        f"snapshot {snapshot.generation_id} as of {snapshot.coverage_clock.clock_epoch_id}:"
        f"{snapshot.coverage_clock.commit_seq}; sealed lobe-9 episode records and host causal "
        "facts in that projection; classes explicit > prior_finding > comparable > "
        "counterexample, canonical record id within each; comparable keys thread_id and "
        f"criteria_revision; record cap {limits.record_cap} in support units; token candidate "
        f"cap {limits.token_candidate_cap} carried for the verified request budget (WO-12), not "
        "enforced here"
    )
    return HistoryBundle(
        primary_support=tuple(primary),
        prior_findings=tuple(findings),
        intervening_direction_changes=tuple(_direction_changes(members)),
        links=tuple(links),
        retrieval_audit={
            "query_policy_revision": HISTORY_POLICY_REVISION,
            "scope": scope,
            "considered_ids": considered,
            "returned_ids": returned,
            "admitted_ids": admitted,
            "omitted": sorted(omitted, key=lambda row: (row["id"], row["reason"])),
            "counterexample_search_status": status,
            "cap": limits.record_cap,
        },
    )


__all__ = [
    "HISTORY_POLICY_REVISION",
    "HOST_RELATION_KINDS",
    "RETRIEVAL_CLASSES",
    "collect_history",
    "history_limits",
]
