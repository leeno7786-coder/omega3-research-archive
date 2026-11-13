"""Meta B criteria checks — the objective's acceptance criteria against the receipts it recorded.

WO-06 step 2. Before this, Meta B classified an episode from AUTO's ``cycle_kind`` alone, so a
build that ran to completion and failed its own acceptance criterion folded up as ``completed``
and nothing downstream could tell. This phase reads the criteria the objective actually carried
(the non-shadow motor program's ``outcome_contract``) and the verification and artifact receipts
WO-05 captured into this episode's working events, and checks one against the other.

Deterministic by construction: no model, no clock, no heuristic. The same criteria and the same
recorded events always yield the same checks.

A check belongs to the bytes it examined. A pass recorded against revision 1 of a file does not
verify revision 2 of it, so every result names both the revision it checked and the revision
that is current, and a pass on superseded bytes is ``stale`` rather than ``passed``.

Missing evidence is never a pass: a criterion with no receipt is ``not_performed``, and evidence
that could not be read is recorded as unavailable with its cause. A receipt whose own shape is
wrong is a producer contract violation and fails loudly, naming the event.
"""

from __future__ import annotations

from typing import Any, Mapping

from schema.continuity_json import ContinuityContractError, content_hash, unfreeze

METHOD = "meta_b.criteria_check"
METHOD_VERSION = 1

#: One criterion's result against the current revision of what it checked.
CHECK_STATUSES: tuple[str, ...] = ("passed", "failed", "stale", "not_performed")

#: The check set as a whole. ``no_criteria`` means the objective recorded nothing to check, which
#: is different from criteria that went unchecked (``not_performed``) and from evidence that
#: could not be read (``unknown``).
CHECK_VERDICTS: tuple[str, ...] = (
    "passed",
    "failed",
    "partial",
    "not_performed",
    "unknown",
    "no_criteria",
)

_VERIFICATION_FIELDS = ("criterion", "path", "revision", "content_sha256", "passed")
_ARTIFACT_FIELDS = ("path", "revision", "content_sha256", "change_kind")


def criteria_of(motor_program: Any) -> tuple[tuple[str, ...], str]:
    """The objective's acceptance criteria and the revision of the contract that states them."""
    if not isinstance(motor_program, Mapping):
        return (), ""
    contract = motor_program.get("outcome_contract")
    if not isinstance(contract, Mapping):
        return (), ""
    criteria = tuple(
        str(item) for item in (contract.get("acceptance_criteria") or ()) if str(item).strip()
    )
    program_id = str(motor_program.get("program_id") or "")
    revision = f"motor_program:{program_id}:{content_hash(unfreeze(dict(contract)))[:16]}"
    return criteria, revision


def _receipt(event: Any, fields: tuple[str, ...], what: str) -> dict[str, Any]:
    payload = unfreeze(getattr(event, "payload", None))
    where = f"{what} receipt in event {event.event_id!r}"
    if not isinstance(payload, dict):
        raise ContinuityContractError(f"{where} is not a JSON object")
    missing = [name for name in fields if name not in payload]
    if missing:
        raise ContinuityContractError(f"{where} is missing {missing}; it cannot be checked")
    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ContinuityContractError(f"{where} has a non-integer revision {revision!r}")
    digest = payload["content_sha256"]
    if not isinstance(digest, str) or (not digest and what == "verification"):
        raise ContinuityContractError(f"{where} names no content digest")
    if what == "verification":
        if not isinstance(payload["passed"], bool):
            raise ContinuityContractError(f"{where} has a non-boolean result {payload['passed']!r}")
        if not isinstance(payload["criterion"], str) or not payload["criterion"].strip():
            raise ContinuityContractError(f"{where} names no criterion")
    return payload


def _unavailable(criteria: tuple[str, ...], revision: str, reason: str) -> dict[str, Any]:
    return {
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "criteria_revision": revision,
        "criteria": list(criteria),
        "results": [],
        "capture": {"state": "unavailable", "reason": reason},
        "verdict": "unknown",
    }


def run(episode_id: str, motor_program: Any) -> dict[str, Any]:
    """Check the objective's acceptance criteria against this episode's recorded receipts."""
    criteria, revision = criteria_of(motor_program)
    if not criteria:
        return {
            "method": METHOD,
            "method_version": METHOD_VERSION,
            "criteria_revision": revision,
            "criteria": [],
            "results": [],
            "capture": {"state": "not_applicable",
                        "reason": "the objective recorded no acceptance criteria"},
            "verdict": "no_criteria",
        }

    from memory.auto_evidence import ARTIFACT_REVISION, VERIFICATION_RESULT
    from memory.episode_events import read_episode_events

    try:
        events = read_episode_events(str(episode_id or ""))
    except ContinuityContractError as exc:
        # The evidence exists somewhere but could not be read: that is an unknown result with a
        # cause, never a clean "nothing was checked".
        return _unavailable(criteria, revision, f"{type(exc).__name__}: {exc}")

    current: dict[str, tuple[int, str, str]] = {}
    receipts: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        if event.event_kind == ARTIFACT_REVISION:
            artifact = _receipt(event, _ARTIFACT_FIELDS, "artifact")
            path = str(artifact["path"])
            held = current.get(path)
            if held is None or artifact["revision"] > held[0]:
                current[path] = (artifact["revision"], str(artifact["content_sha256"]),
                                 event.event_id)
        elif event.event_kind == VERIFICATION_RESULT:
            receipts.append((event.event_id, _receipt(event, _VERIFICATION_FIELDS, "verification")))

    results = [_check(criterion, receipts, current) for criterion in criteria]
    statuses = [row["status"] for row in results]
    if "failed" in statuses:
        verdict = "failed"
    elif all(status == "passed" for status in statuses):
        verdict = "passed"
    elif "passed" in statuses:
        verdict = "partial"
    else:
        verdict = "not_performed"
    return {
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "criteria_revision": revision,
        "criteria": list(criteria),
        "results": results,
        "capture": {"state": "observed", "reason": ""},
        "verdict": verdict,
    }


def _check(criterion: str, receipts: list[tuple[str, dict[str, Any]]],
           current: dict[str, tuple[int, str, str]]) -> dict[str, Any]:
    rows = [(event_id, receipt) for event_id, receipt in receipts if receipt["criterion"] == criterion]
    if not rows:
        return {"criterion": criterion, "status": "not_performed", "evidence_event_ids": [],
                "path": "", "checked_revision": None, "checked_sha256": "",
                "current_revision": None, "current_sha256": ""}

    def now(receipt: dict[str, Any]) -> tuple[int, str]:
        # With no later revision recorded, the bytes the check examined are the current ones.
        held = current.get(str(receipt["path"]))
        return (held[0], held[1]) if held else (receipt["revision"], str(receipt["content_sha256"]))

    on_current = [(event_id, receipt) for event_id, receipt in rows
                  if str(receipt["content_sha256"]) == now(receipt)[1]]
    failing = [row for row in on_current if not row[1]["passed"]]
    passing = [row for row in on_current if row[1]["passed"]]
    if failing:
        status, deciding = "failed", failing
    elif passing:
        status, deciding = "passed", passing
    else:
        status = "stale"
        deciding = [max(rows, key=lambda row: row[1]["revision"])]
    receipt = deciding[0][1]
    current_revision, current_sha = now(receipt)
    return {
        "criterion": criterion,
        "status": status,
        "evidence_event_ids": [event_id for event_id, _ in deciding],
        "path": str(receipt["path"]),
        "checked_revision": receipt["revision"],
        "checked_sha256": str(receipt["content_sha256"]),
        "current_revision": current_revision,
        "current_sha256": current_sha,
    }


__all__ = ["CHECK_STATUSES", "CHECK_VERDICTS", "METHOD", "METHOD_VERSION", "criteria_of", "run"]
