"""WO-11 — the window's numbers, each with the population it was computed over.

`build_window_metrics(episodes, *, history)` turns a sealed cohort and its pinned history into
:class:`~schema.window_evidence.WindowMetrics`: rows carrying a numerator population, a
denominator population, exclusions and unknowns *with their reasons*, the stratum the row was
computed inside, and this module's computation version.

What it refuses to do is the point:

* **A completed cascade is not a task success, and a task success is not a verified one.** Five
  axes move independently (WO-06), so they are five populations, never one "success rate".
* **A rate over nothing is an undefined observation.** Not 0, not 0.5, not 100%. The row still
  reports its counts and says why it could not be measured.
* **Attempts are not trials.** Attempts are distinct recorded operation tokens; independent
  cases come only from host-recorded job / continuation / thread identity, merged
  conservatively. A record with none of them leaves independence unknown — counting cases by
  episode id would always return "n independent cases", a number with no content.
* **Material progress is recorded change**: a criterion newly satisfied, a real artifact
  revision, a blocker that later cleared, an authorized change of direction, a verified
  intermediate output. Repeats with nothing changed are *no observed progress*, which is not a
  failure; blocked work is *waiting*; uncaptured work is *not observable*.
* **Incomparable cases are never pooled.** Every rate is computed inside a stratum of
  (criteria revision, lane, work kind, producer revisions); when the cohort spans more than one,
  the pooled metric id exists only as an explicit incomparable observation.
* **Durations come from recorded measurements only** — `elapsed_ns` observations inside one
  boot. Nothing is subtracted across boots and no timestamp difference is invented. A model's
  self-reported confidence is never a number here.

Pure: it reads the sealed records and the bundle it is given, touches no clock, no store and no
configuration, and mutates nothing.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from schema.continuity_json import ContinuityContractError, Observation
from schema.episode_record import EpisodeRecord
from schema.window_evidence import HistoryBundle, WindowMetrics

#: This module's own version. Every row carries it, so a number can never be compared across
#: computations that did not mean the same thing by it.
METRICS_COMPUTATION_VERSION = "omega_loop.window.metrics/1"

# ── vocabularies read off the axes (never re-declared) ───────────────────────

_UNKNOWN = "unknown"
_PERFORMED_VERIFICATION = frozenset({"passed", "failed", "partial"})
_VERIFIED = "passed"
_INAPPLICABLE_TASK = "not_applicable"
_UNATTEMPTED_ACTION = "not_attempted"
_WAITING_TASK = frozenset({"blocked", "cancelled"})
_WAITING_ACTION = frozenset({"blocked", "cancelled", "skipped", "not_attempted"})

#: The bases computed inside a stratum. When the cohort spans several, each of these also gets an
#: explicit incomparable row under its unsuffixed id, so nothing downstream can find a pooled rate.
_STRATIFIED_BASES: tuple[str, ...] = (
    "cascade_completed",
    "action_succeeded",
    "task_success_applicable",
    "verified_success",
    "unverified_attempts",
    "capture_failures",
    "attempts",
    "independent_cases",
    "progress_observed",
    "progress_none_observed",
    "progress_waiting",
    "progress_not_observable",
    "elapsed_measured",
)

_PROGRESS_STATES = (
    ("progress_observed", "a recorded change: this is the only state that claims movement"),
    ("progress_none_observed",
     "the same criteria and artifact revisions came back: no observed change, which is "
     "not a failure verdict"),
    ("progress_waiting", "blocked or cancelled on a named condition, which is not a failure"),
    ("progress_not_observable",
     "nothing was captured that could show movement either way: missing observability, "
     "not a failure"),
)


# ── reading the record ───────────────────────────────────────────────────────


def _observed(value: Any) -> Any:
    """The value an observation carries, or ``None`` when it records an absence."""
    if isinstance(value, Observation):
        return value.value if value.is_observed else None
    if isinstance(value, Mapping) and set(value) == {"state", "value", "reason"}:
        return value["value"] if value["state"] == "observed" else None
    return value


def _text(value: Any) -> str:
    plain = _observed(value)
    return str(plain) if isinstance(plain, str) else ""


def _position(record: EpisodeRecord) -> tuple[int, int, str]:
    """The record's own finish-line position. An unstamped one sorts last, never invented."""
    finish = _observed(record.temporal.get("finish_line"))
    if isinstance(finish, Mapping):
        try:
            return (0, int(finish["commit_seq"]), record.episode_id)
        except (KeyError, TypeError, ValueError):
            pass
    return (1, 0, record.episode_id)


def _events(record: EpisodeRecord) -> tuple[Any, ...]:
    return tuple(record.events["by_value"])


def _unavailable(record: EpisodeRecord) -> tuple[str, ...]:
    return tuple(str(ref) for ref in record.events["coverage"]["unavailable_refs"])


def _assessment(record: EpisodeRecord, axis: str) -> Mapping[str, Any]:
    row = record.outcome.assessments.get(axis) if record.outcome.assessments else None
    return row if isinstance(row, Mapping) else {}


def _artifact_revisions(record: EpisodeRecord) -> tuple[str, ...]:
    found = []
    for axis in ("verification_status", "task_outcome"):
        revision = _assessment(record, axis).get("artifact_revision")
        if isinstance(revision, str) and revision.strip():
            found.append(revision)
    return tuple(sorted(set(found)))


def _criteria_revision(record: EpisodeRecord) -> str:
    revision = record.objective.get("criteria_revision")
    return _text(revision) or ""


def _producer_revisions(record: EpisodeRecord) -> tuple[str, ...]:
    revisions = set()
    for event in _events(record):
        producer = event.producer if isinstance(event.producer, Mapping) else {}
        version = producer.get("component_version")
        if isinstance(version, str) and version.strip():
            revisions.add(version)
    return tuple(sorted(revisions))


def _stratum_key(record: EpisodeRecord) -> tuple[str, str, str, tuple[str, ...]]:
    """What makes two cases comparable: the criteria they were judged against, the task scope
    they ran in, and the producer revisions that produced their evidence.

    Deliberately NOT the boot id: a restart is not a revision of anything, and including it
    would shatter every population for no epistemic gain. The boot is the clock scope of a
    duration (see `_elapsed_row`), which is a different question.
    """
    return (
        _criteria_revision(record),
        _text(record.identity.get("lane")),
        _text(record.identity.get("work_kind")),
        _producer_revisions(record),
    )


def _case_identities(record: EpisodeRecord) -> list[tuple[int, str]]:
    """The host-recorded identities that can make this record part of a case, strongest first.

    A durable job, then a continuation root, then the thread. ``attempt_id`` is deliberately
    absent: it distinguishes retries, so treating it as a case identity would inflate
    independence — which is the whole defect this metric exists to prevent.
    """
    found: list[tuple[int, str]] = []
    job = _text(record.identity.get("job_id"))
    if job:
        found.append((0, f"job:{job}"))
    continuation = _text(record.identity.get("continuation_id"))
    if continuation:
        found.append((1, f"continuation:{continuation}"))
    thread = _text(record.identity.get("thread_id"))
    if thread:
        found.append((2, f"thread:{thread}"))
    return found


def _capture_gap(record: EpisodeRecord) -> str:
    """Why this record's own capture is incomplete, or ``""``."""
    coverage = record.events["coverage"]
    if _unavailable(record):
        return "event bytes were not retained"
    try:
        if int(coverage["referenced"]) > int(coverage["retained"]):
            return "more events were referenced than retained"
    except (KeyError, TypeError, ValueError):
        return "the event coverage counts are unreadable"
    if tuple(record.coverage.get("inaccessible") or ()):
        return "part of the episode was inaccessible"
    return ""


def _missing_observation(record: EpisodeRecord) -> str:
    """What this record says it could not see. Distinct from a capture failure: an unrecorded
    surface is a declared blind spot, not lost bytes."""
    gap = _capture_gap(record)
    if gap:
        return gap
    for field, phrase in (("unrecorded", "unrecorded"), ("causal_gaps", "a causal gap")):
        if tuple(record.coverage.get(field) or ()):
            return f"the episode declares {phrase}"
    return ""


# ── rows ─────────────────────────────────────────────────────────────────────


def _row(
    metric_id: str,
    *,
    population: Sequence[str],
    numerator: Sequence[str],
    excluded: Sequence[tuple[str, str]] = (),
    unknown: Sequence[tuple[str, str]] = (),
    stratification: Mapping[str, Any],
    note: str = "",
    measurement: Mapping[str, Any] | None = None,
    empty_reason: str = "",
) -> dict[str, Any]:
    """One row. An empty population is an undefined observation, never a zero rate."""
    omissions = [{"id": ref, "reason": reason} for ref, reason in tuple(excluded) + tuple(unknown)]
    base: dict[str, Any] = {
        "metric_id": metric_id,
        "excluded": len(tuple(excluded)),
        "unknown": len(tuple(unknown)),
        "omissions": omissions,
        "stratification": dict(stratification),
        "computation_version": METRICS_COMPUTATION_VERSION,
    }
    if note:
        base["note"] = note
    if not population:
        reasons = sorted({reason for _, reason in tuple(excluded) + tuple(unknown)})
        base.update({
            "observation": "undefined",
            "numerator": 0,
            "denominator": 0,
            "population_refs": [],
            "numerator_refs": [],
            "reason": empty_reason or (
                "no member entered this population"
                + (f" ({', '.join(reasons)})" if reasons else "")
            ),
        })
        return base
    base.update({
        "observation": "measured",
        "numerator": len(tuple(numerator)),
        "denominator": len(tuple(population)),
        "population_refs": list(population),
        "numerator_refs": list(numerator),
    })
    if measurement is not None:
        base["measurement"] = dict(measurement)
    return base


def _undefined(
    metric_id: str,
    *,
    reason: str,
    stratification: Mapping[str, Any],
    unknown: Sequence[tuple[str, str]] = (),
    excluded: Sequence[tuple[str, str]] = (),
    note: str = "",
) -> dict[str, Any]:
    row = _row(
        metric_id,
        population=(),
        numerator=(),
        excluded=excluded,
        unknown=unknown,
        stratification=stratification,
        note=note,
        empty_reason=reason,
    )
    row["reason"] = reason
    return row


# ── the outcome populations ──────────────────────────────────────────────────


def _outcome_rows(members: Sequence[EpisodeRecord], stratification: Mapping[str, Any]
                  ) -> list[dict[str, Any]]:
    ids = [record.episode_id for record in members]
    axes = {record.episode_id: record.outcome for record in members}
    rows: list[dict[str, Any]] = []

    # cascade completion: the cascade axis alone, which says nothing about the task.
    population = [eid for eid in ids if axes[eid].cascade_status != _UNKNOWN]
    rows.append(_row(
        "cascade_completed",
        population=population,
        numerator=[eid for eid in population if axes[eid].cascade_status == "completed"],
        unknown=[(eid, "unknown") for eid in ids if axes[eid].cascade_status == _UNKNOWN],
        stratification=stratification,
        note="the cascade ran to its terminus; it is not a statement about the task",
    ))

    # the action the episode took.
    excluded = [(eid, _UNATTEMPTED_ACTION) for eid in ids
                if axes[eid].action_status == _UNATTEMPTED_ACTION]
    unknown = [(eid, "unknown") for eid in ids if axes[eid].action_status == _UNKNOWN]
    population = [eid for eid in ids
                  if axes[eid].action_status not in (_UNATTEMPTED_ACTION, _UNKNOWN)]
    rows.append(_row(
        "action_succeeded",
        population=population,
        numerator=[eid for eid in population if axes[eid].action_status == "succeeded"],
        excluded=excluded,
        unknown=unknown,
        stratification=stratification,
    ))

    # the applicable task outcome. An idle episode has no task to succeed at.
    excluded = [(eid, _INAPPLICABLE_TASK) for eid in ids
                if axes[eid].task_outcome == _INAPPLICABLE_TASK]
    unknown = [(eid, "unknown") for eid in ids if axes[eid].task_outcome == _UNKNOWN]
    applicable = [eid for eid in ids
                  if axes[eid].task_outcome not in (_INAPPLICABLE_TASK, _UNKNOWN)]
    rows.append(_row(
        "task_success_applicable",
        population=applicable,
        numerator=[eid for eid in applicable if axes[eid].task_outcome == "success"],
        excluded=excluded,
        unknown=unknown,
        stratification=stratification,
    ))

    # verified success: an applicable task whose check was actually performed.
    verified_population: list[str] = []
    verified_excluded: list[tuple[str, str]] = []
    verified_unknown: list[tuple[str, str]] = []
    for eid in ids:
        outcome = axes[eid]
        if outcome.task_outcome == _INAPPLICABLE_TASK:
            verified_excluded.append((eid, _INAPPLICABLE_TASK))
        elif outcome.task_outcome == _UNKNOWN:
            verified_unknown.append((eid, "task_outcome unknown"))
        elif outcome.verification_status == _UNKNOWN:
            verified_unknown.append((eid, "verification_status unknown"))
        elif outcome.verification_status not in _PERFORMED_VERIFICATION:
            verified_excluded.append((eid, "verification_not_performed"))
        else:
            verified_population.append(eid)
    rows.append(_row(
        "verified_success",
        population=verified_population,
        numerator=[eid for eid in verified_population
                   if axes[eid].verification_status == _VERIFIED],
        excluded=verified_excluded,
        unknown=verified_unknown,
        stratification=stratification,
        note="a model's claim is not a check; only a performed verification enters this population",
    ))

    # the claims nobody checked.
    unverified_population = [eid for eid in applicable if axes[eid].verification_status != _UNKNOWN]
    rows.append(_row(
        "unverified_attempts",
        population=unverified_population,
        numerator=[eid for eid in unverified_population
                   if axes[eid].verification_status == "not_performed"],
        excluded=excluded,
        unknown=[(eid, "verification_status unknown") for eid in applicable
                 if axes[eid].verification_status == _UNKNOWN],
        stratification=stratification,
    ))

    # capture failures, read off the records' own coverage.
    gaps = {record.episode_id: _capture_gap(record) for record in members}
    rows.append(_row(
        "capture_failures",
        population=ids,
        numerator=[eid for eid in ids if gaps[eid]],
        stratification=stratification,
        note="; ".join(sorted({gap for gap in gaps.values() if gap})) or "no capture gap recorded",
    ))
    return rows


# ── attempts, cases, progress ────────────────────────────────────────────────


def _attempt_row(members: Sequence[EpisodeRecord], stratification: Mapping[str, Any]
                 ) -> dict[str, Any]:
    """Distinct recorded operation tokens, over the events that were referenced.

    The population keeps each record's own event sequence: permutation stability is about the
    order the *records* arrive in, never about reordering what an episode recorded.
    """
    population: list[str] = []
    numerator: list[str] = []
    duplicates: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    seen: set[str] = set()
    for record in members:
        for event in _events(record):
            population.append(event.event_id)
            token = str(event.operation_id)
            if token in seen:
                duplicates.append((event.event_id, "duplicate_operation_token"))
                continue
            seen.add(token)
            numerator.append(event.event_id)
        for ref in _unavailable(record):
            population.append(ref)
            unknown.append((ref, "event_bytes_not_retained"))
    return _row(
        "attempts",
        population=population,
        numerator=numerator,
        excluded=duplicates,
        unknown=unknown,
        stratification=stratification,
        note="one attempt is one distinct recorded operation token, not one episode",
    )


def _cases(members: Sequence[EpisodeRecord]) -> tuple[list[dict[str, Any]], list[str]]:
    """Connected components over host-recorded identities, plus the members that recorded none.

    Merging is conservative on purpose: two members that share ANY recorded identity are one
    case. Independence is the claim that needs evidence, so the doubt goes to fewer cases.
    """
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    identityless: list[str] = []
    per_member: dict[str, list[tuple[int, str]]] = {}
    for record in members:
        identities = _case_identities(record)
        if not identities:
            identityless.append(record.episode_id)
            continue
        per_member[record.episode_id] = identities
        first = identities[0][1]
        for _, key in identities[1:]:
            union(first, key)

    grouped: dict[str, list[str]] = {}
    for record in members:
        identities = per_member.get(record.episode_id)
        if not identities:
            continue
        grouped.setdefault(find(identities[0][1]), []).append(record.episode_id)

    cases: list[dict[str, Any]] = []
    for root, episode_ids in grouped.items():
        keys = sorted({key for eid in episode_ids for _, key in per_member[eid]})
        strongest = min(rank for eid in episode_ids for rank, _ in per_member[eid])
        name = "+".join(
            sorted({key for eid in episode_ids for rank, key in per_member[eid]
                    if rank == strongest})
        )
        cases.append({
            "case_key": name,
            "root": root,
            "episode_ids": sorted(episode_ids),
            "identities": keys,
        })
    cases.sort(key=lambda case: case["case_key"])
    return cases, sorted(identityless)


def _case_members(case: Mapping[str, Any], members: Sequence[EpisodeRecord]
                  ) -> list[EpisodeRecord]:
    inside = set(case["episode_ids"])
    return sorted((record for record in members if record.episode_id in inside), key=_position)


def _independence_row(cases: Sequence[Mapping[str, Any]], identityless: Sequence[str],
                      members: Sequence[EpisodeRecord], stratification: Mapping[str, Any]
                      ) -> dict[str, Any]:
    population = sorted(eid for case in cases for eid in case["episode_ids"])
    representatives = [_case_members(case, members)[0].episode_id for case in cases]
    strat = dict(stratification)
    strat["case_keys"] = [str(case["case_key"]) for case in cases]
    strat["case_members"] = {str(case["case_key"]): list(case["episode_ids"]) for case in cases}
    return _row(
        "independent_cases",
        population=population,
        numerator=sorted(representatives),
        unknown=[(eid, "no_recorded_case_identity") for eid in identityless],
        stratification=strat,
        note="a case is host-recorded job / continuation / thread identity, never an episode id",
        empty_reason=(
            "no member recorded a case identity (job, continuation or thread), so independence "
            "is unknown; counting cases by episode id would always return one case per episode"
        ),
    )


def _progress(case: Mapping[str, Any], members: Sequence[EpisodeRecord],
              direction_episodes: Iterable[str]) -> tuple[str, list[str], str]:
    """One case's progress state, the recorded changes behind it, and its condition."""
    ordered = _case_members(case, members)
    evidence: list[str] = []
    satisfied: set[str] = set()
    artifacts: set[str] = set()
    blocked_seen = False
    directions = set(direction_episodes)

    for record in ordered:
        outcome = record.outcome
        revision = _criteria_revision(record)
        if outcome.verification_status in ("passed", "partial"):
            if revision and revision not in satisfied:
                evidence.append("criterion_newly_satisfied")
            satisfied.add(revision)
        if outcome.verification_status == "partial":
            evidence.append("verified_intermediate_output")
        revisions = set(_artifact_revisions(record))
        if revisions and artifacts and revisions - artifacts:
            evidence.append("artifact_revised")
        artifacts |= revisions
        if outcome.task_outcome in _WAITING_TASK or outcome.action_status in _WAITING_ACTION:
            blocked_seen = True
        elif blocked_seen and outcome.task_outcome not in (_UNKNOWN,):
            evidence.append("blocker_resolved")
        if record.episode_id in directions:
            evidence.append("authorized_scope_change")

    condition = ""
    for record in ordered:
        gaps = tuple(record.coverage.get("causal_gaps") or ())
        if gaps:
            condition = str(gaps[0])
            break

    if evidence:
        return "progress_observed", sorted(set(evidence)), condition

    waiting = [record for record in ordered
               if record.outcome.task_outcome in _WAITING_TASK
               or record.outcome.action_status in _WAITING_ACTION]
    if waiting:
        return "progress_waiting", [], condition or "no condition was recorded"

    unknown_task = all(record.outcome.task_outcome == _UNKNOWN for record in ordered)
    unperformed = all(record.outcome.verification_status not in _PERFORMED_VERIFICATION
                      for record in ordered)
    if unknown_task and unperformed:
        missing = next((_missing_observation(record) for record in ordered
                        if _missing_observation(record)), "")
        if missing:
            return "progress_not_observable", [], missing
    return "progress_none_observed", [], condition


def _progress_rows(cases: Sequence[Mapping[str, Any]], identityless: Sequence[str],
                   members: Sequence[EpisodeRecord], history: HistoryBundle,
                   stratification: Mapping[str, Any]) -> list[dict[str, Any]]:
    direction_episodes = {
        str(row.get("episode_id")) for row in history.intervening_direction_changes
        if isinstance(row, Mapping) and row.get("episode_id")
    }
    states: dict[str, str] = {}
    evidence: dict[str, list[str]] = {}
    conditions: dict[str, str] = {}
    representatives: dict[str, str] = {}
    for case in cases:
        key = str(case["case_key"])
        state, found, condition = _progress(case, members, direction_episodes)
        states[key] = state
        representatives[key] = _case_members(case, members)[0].episode_id
        if found:
            evidence[key] = found
        if condition:
            conditions[key] = condition

    strat = dict(stratification)
    strat["case_keys"] = [str(case["case_key"]) for case in cases]
    strat["case_members"] = {str(case["case_key"]): list(case["episode_ids"]) for case in cases}
    strat["progress_evidence"] = evidence
    strat["conditions"] = conditions

    population = sorted(representatives.values())
    rows = []
    for base, note in _PROGRESS_STATES:
        rows.append(_row(
            base,
            population=population,
            numerator=sorted(ref for key, ref in representatives.items() if states[key] == base),
            unknown=[(eid, "no_recorded_case_identity") for eid in identityless],
            stratification=strat,
            note=note,
            empty_reason=(
                "no case could be formed: no member recorded a job, continuation or thread "
                "identity, so progress per case is unknown"
            ),
        ))
    return rows


def _elapsed_row(members: Sequence[EpisodeRecord], stratification: Mapping[str, Any]
                 ) -> dict[str, Any]:
    """Elapsed nanoseconds the producers actually recorded, inside one boot.

    Two boots do not share a monotonic clock, so their values are never added and no duration is
    derived by subtracting timestamps. A population with no measurement at all is undefined.
    """
    population: list[str] = []
    measured: list[str] = []
    unknown: list[tuple[str, str]] = []
    total = 0
    boots: set[str] = set()
    for record in members:
        for event in _events(record):
            population.append(event.event_id)
            elapsed = _observed(event.elapsed_ns)
            if isinstance(elapsed, int) and not isinstance(elapsed, bool):
                measured.append(event.event_id)
                total += elapsed
                boots.add(str(event.boot_id))
            else:
                unknown.append((event.event_id, "no_elapsed_recorded"))

    if not measured:
        return _undefined(
            "elapsed_measured",
            reason=("no elapsed measurement was recorded for this population; a duration is "
                    "never derived by subtracting timestamps"),
            stratification=stratification,
            unknown=unknown,
        )
    if len(boots) > 1:
        return _undefined(
            "elapsed_measured",
            reason=(f"the measurements span {len(boots)} boots ({', '.join(sorted(boots))}); "
                    "elapsed values from different boots share no clock and are never added"),
            stratification=stratification,
            unknown=[(ref, "cross_boot_measurement") for ref in population],
        )
    note = ""
    if unknown:
        note = (f"the total covers only the {len(measured)} recorded measurement(s); "
                f"{len(unknown)} event(s) recorded none")
    return _row(
        "elapsed_measured",
        population=population,
        numerator=measured,
        unknown=unknown,
        stratification=stratification,
        note=note,
        measurement={"units": "ns", "total": total, "clock_scope": sorted(boots)[0]},
    )


# ── cohort rows ──────────────────────────────────────────────────────────────


def _publication_row(members: Sequence[EpisodeRecord], scope: Mapping[str, Any]
                     ) -> dict[str, Any]:
    return _undefined(
        "publication_failures",
        reason=(
            "memory_commit_status is not a field of the sealed record (WO-02 decision 5): it is "
            "exposed by EpisodeCommitView and sealed into the window manifest, and this "
            "computation receives neither, so a publication failure cannot be counted here"
        ),
        stratification=dict(scope),
        unknown=[(record.episode_id, "publication_status_not_in_this_input")
                 for record in members],
    )


def _history_row(history: HistoryBundle, scope: Mapping[str, Any]) -> dict[str, Any]:
    status = str(history.retrieval_audit.get("counterexample_search_status") or "")
    units: list[str] = []
    comparable: list[str] = []
    other: list[tuple[str, str]] = []
    for row in history.primary_support:
        if not isinstance(row, Mapping) or not row.get("unit_id"):
            continue
        unit = str(row["unit_id"])
        units.append(unit)
        retrieval_class = str(row.get("retrieval_class") or "")
        if retrieval_class in ("comparable", "counterexample"):
            comparable.append(unit)
        else:
            other.append((unit, f"retrieval_class {retrieval_class!r} is not a comparison"))
    return _row(
        "history_comparable_cases",
        population=units,
        numerator=comparable,
        excluded=other,
        stratification=dict(scope),
        note=(f"counterexample search status: {status}; a comparison whose counterexample search "
              "did not run is not a complete comparison"),
        empty_reason="the pinned history admitted no primary support unit to compare against",
    )


def _cooccurrence_row(members: Sequence[EpisodeRecord], history: HistoryBundle,
                      scope: Mapping[str, Any]) -> dict[str, Any]:
    ids = [record.episode_id for record in members]
    inside = set(ids)
    together: set[str] = set()
    for row in history.links:
        if not isinstance(row, Mapping) or str(row.get("kind")) != "concurrent_commit":
            continue
        for end in (row.get("source"), row.get("target")):
            episode_id = str(end or "").split("episode:")[-1]
            if episode_id in inside:
                together.add(episode_id)
    for record in members:
        concurrent = record.temporal.get("concurrent_commits")
        if isinstance(concurrent, Mapping):
            named = {str(eid) for eid in concurrent.get("episode_ids") or ()}
            if named & inside:
                together.add(record.episode_id)
                together |= named & inside
    return _row(
        "concurrent_commit_cooccurrences",
        population=ids,
        numerator=sorted(together),
        stratification=dict(scope),
        note=("a co-occurrence count, not causation: committing inside the same window is not "
              "evidence that one episode influenced another"),
        empty_reason="the cohort has no member whose co-occurrence could be counted",
    )


# ── the entry point ──────────────────────────────────────────────────────────


def build_window_metrics(episodes: tuple[EpisodeRecord, ...], *,
                         history: HistoryBundle) -> WindowMetrics:
    """Reproducible measurements for a sealed cohort, each with its own population.

    Rows are identical under any order of *episodes*: strata are ordered by their own key and
    members by their recorded finish-line position, so nothing depends on how the caller
    happened to pass them.
    """
    if not isinstance(history, HistoryBundle):
        raise ContinuityContractError(
            "build_window_metrics takes the pinned HistoryBundle collect_history returned"
        )
    members = []
    for index, record in enumerate(episodes):
        if not isinstance(record, EpisodeRecord):
            raise ContinuityContractError(
                f"build_window_metrics takes sealed EpisodeRecord values; episodes[{index}] is "
                f"{type(record).__name__}"
            )
        members.append(record)
    members.sort(key=_position)

    scope: dict[str, Any] = {
        "scope": "cohort",
        "members": len(members),
        "retrieval_policy_revision": str(
            history.retrieval_audit.get("query_policy_revision") or ""
        ),
    }

    strata: dict[tuple[str, str, str, tuple[str, ...]], list[EpisodeRecord]] = {}
    for record in members:
        strata.setdefault(_stratum_key(record), []).append(record)
    ordered_keys = sorted(strata)

    rows: list[dict[str, Any]] = []
    for index, key in enumerate(ordered_keys, start=1):
        stratum_id = f"s{index}"
        criteria, lane, work_kind, producers = key
        stratification: dict[str, Any] = {
            "stratum_id": stratum_id,
            "criteria_revision": criteria,
            "lane": lane,
            "work_kind": work_kind,
            "producer_revisions": list(producers),
            "members": len(strata[key]),
        }
        inside = strata[key]
        cases, identityless = _cases(inside)
        stratum_rows = _outcome_rows(inside, stratification)
        stratum_rows.append(_attempt_row(inside, stratification))
        stratum_rows.append(_independence_row(cases, identityless, inside, stratification))
        stratum_rows.extend(_progress_rows(cases, identityless, inside, history, stratification))
        stratum_rows.append(_elapsed_row(inside, stratification))
        for row in stratum_rows:
            row["metric_id"] = f"{row['metric_id']}@{stratum_id}"
        rows.extend(stratum_rows)

    if len(ordered_keys) > 1:
        described = "; ".join(
            f"s{index} (criteria {key[0]!r}, {key[1]}/{key[2]}, producers {list(key[3])})"
            for index, key in enumerate(ordered_keys, start=1)
        )
        for base in _STRATIFIED_BASES:
            rows.append(_undefined(
                base,
                reason=(f"the population spans {len(ordered_keys)} incomparable strata: "
                        f"{described}. Pooling them would be a rate over cases that were not "
                        "comparable, so the pooled id carries no measurement"),
                stratification=dict(scope, strata=len(ordered_keys)),
                unknown=[(record.episode_id, "incomparable_stratum") for record in members],
            ))

    rows.append(_publication_row(members, scope))
    rows.append(_history_row(history, scope))
    rows.append(_cooccurrence_row(members, history, scope))
    if not members:
        # Nothing was measurable, and every row says so with its own reason. An empty cohort is
        # not a cohort with zero successes.
        for base in _STRATIFIED_BASES:
            rows.append(_undefined(
                base,
                reason="the cohort carried no sealed episode record, so nothing was measured",
                stratification=dict(scope),
            ))
    return WindowMetrics(computation_version=METRICS_COMPUTATION_VERSION, rows=tuple(rows))


__all__ = ["METRICS_COMPUTATION_VERSION", "build_window_metrics"]
