"""Host-owned continuation identities and revision checks; persistence stays in pressure."""

from contextlib import nullcontext
import hashlib
import json
import logging

log = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def work_lock(metadata):
    if metadata.get("agent_job_id"):
        from auto_loop.execute import job_control

        return job_control._mutex(metadata["agent_job_id"])
    if metadata.get("browser_workflow_id"):
        from meta_loop.browser import workflow

        return workflow._LOCK
    return nullcontext()


def work_revision(metadata):
    if metadata.get("agent_job_id"):
        from auto_loop.execute import job_control

        try:
            return job_control.get_job(metadata["agent_job_id"]).latest_revision
        except KeyError:
            return -1
    if metadata.get("browser_workflow_id"):
        from meta_loop.browser.workflow import browser_workflow_receipt

        receipt = browser_workflow_receipt(metadata["browser_workflow_id"])
        return receipt["revision"] if receipt is not None else -1
    return 0


def terminal_work_reason(metadata):
    """Completed/failed jobs cannot acquire another batch; host-stopped jobs can recover."""
    if metadata.get("agent_job_id"):
        from auto_loop.execute import job_control

        try:
            job = job_control.get_job(metadata["agent_job_id"])
        except KeyError:
            return ""
        if job.status in {"completed", "failed"}:
            return f"the continued job is {job.status}"
    return ""


def identity(metadata, revision):
    work = ("job", metadata["agent_job_id"]) if metadata.get("agent_job_id") else (
        ("workflow", metadata["browser_workflow_id"]) if metadata.get("browser_workflow_id")
        else ("objective", metadata.get("original_cycle_id") or metadata["original_objective"],
              metadata.get("domain", ""), metadata.get("topic", "")))
    logical = _digest([work, metadata["directive_intent"], metadata["resolution_conditions"]])
    thread = metadata.get("thread_id") or metadata["parent_episode_id"]
    return {"logical_continuation_id": logical, "expected_work_revision": revision,
            "key": _digest([thread, logical, revision])}


def stamp(metadata):
    with work_lock(metadata):
        try:
            revision = work_revision(metadata)
        except Exception as exc:
            log.warning("continuation revision cannot be read: %s", exc)
            revision = None
        metadata["continuation_claim"] = identity(metadata, revision)


def validate(metadata):
    claim = metadata.get("continuation_claim")
    if claim is None:
        return
    if (type(claim) is not dict or "expected_work_revision" not in claim
            or (claim.get("expected_work_revision") is not None
                                  and type(claim.get("expected_work_revision")) is not int)
            or claim != identity(metadata, claim["expected_work_revision"])):
        raise ValueError("invalid continuation ownership identity")


def incoming(episode):
    rows = dict(getattr(episode, "pressure", None) or {}).get("contributors") or ()
    return next((dict(row.get("detail") or {}) for row in rows
                 if row.get("id") == getattr(episode, "opened_by", None)), {})
