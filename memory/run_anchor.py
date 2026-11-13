"""Host-resolved identity of one active Coding Agent run.

A run anchor is the exact job, its canonical project, the conversation thread it runs in,
and the instruction revision the caller is acting on. It is resolved from durable job
control state only; the operator's words never name a job, and a sole running job is never
selected by guess. Each way the anchor cannot be resolved is its own error, raised with the
underlying cause attached, so a caller cannot mistake it for a run with nothing to recall.

The writer side of the same identity is :func:`run_provenance_for_job`: coding/dev records
written during a run carry it under ``enrichment.run_provenance`` so run-scoped recall can
select them exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

RUN_PROVENANCE_KEY = "run_provenance"
RUN_PROVENANCE_FIELDS = ("job_id", "project_root", "thread_id", "instruction_revision")


class RunAnchorError(RuntimeError):
    """The active run could not be identified exactly."""


class RunAnchorMissing(RunAnchorError):
    """A required host-owned identity (job, thread or revision) was not supplied."""


class RunAnchorUnknownJob(RunAnchorError):
    """No durable job record exists for the named job."""


class RunAnchorUnreadable(RunAnchorError):
    """The durable job record exists but cannot be trusted."""


class RunAnchorNotRunning(RunAnchorError):
    """The named job is not an active run."""


class RunAnchorProjectUnbound(RunAnchorError):
    """The named job has not selected a project, so it has no run scope."""


class RunAnchorThreadMismatch(RunAnchorError):
    """The job's recorded thread is absent or differs from the caller's thread."""


class RunAnchorUnknownRevision(RunAnchorError):
    """The instruction revision is not one this job has recorded."""


class RunAnchorAmbiguous(RunAnchorError):
    """Another active run claims the same thread or an overlapping project."""


@dataclass(frozen=True)
class RunAnchor:
    job_id: str
    project_root: str
    thread_id: str
    instruction_revision: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "project_root": self.project_root,
            "thread_id": self.thread_id,
            "instruction_revision": self.instruction_revision,
        }


def _load_job(job_id: str):
    from auto_loop.execute import job_control as jobs

    try:
        return jobs.get_job(job_id)
    except KeyError as exc:
        raise RunAnchorUnknownJob(f"no durable job {job_id!r}") from exc
    except jobs.JobRecordInvalid as exc:
        raise RunAnchorUnreadable(f"durable job {job_id!r} is unreadable: {exc}") from exc
    except ValueError as exc:  # invalid opaque id shape
        raise RunAnchorUnknownJob(f"invalid job id {job_id!r}: {exc}") from exc


def _required_text(value: Any, name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise RunAnchorMissing(f"run anchor requires a host-owned {name}")
    return text


def resolve_run_anchor(*, job_id: Any, thread_id: Any, instruction_revision: Any) -> RunAnchor:
    """Resolve the exact active run named by host-owned identities, or raise."""
    job_key = _required_text(job_id, "job_id")
    thread = _required_text(thread_id, "thread_id")
    if instruction_revision is None:
        raise RunAnchorMissing("run anchor requires a host-owned instruction_revision")

    job = _load_job(job_key)
    if job.status != "running":
        raise RunAnchorNotRunning(f"job {job_key} is {job.status}, not running")
    if not job.project_root:
        raise RunAnchorProjectUnbound(f"job {job_key} has not selected a project")

    from auto_loop.execute import job_control as jobs

    try:
        project = jobs.canonical_project(job.project_root, job.workspace_root)
    except ValueError as exc:
        raise RunAnchorUnreadable(f"job {job_key} project scope is invalid: {exc}") from exc
    if str(project) != job.project_root:
        raise RunAnchorUnreadable(f"job {job_key} project identity changed after binding")

    if not job.thread_id:
        raise RunAnchorThreadMismatch(f"job {job_key} has no recorded thread")
    if job.thread_id != thread:
        raise RunAnchorThreadMismatch(f"job {job_key} runs in thread {job.thread_id}, not {thread}")

    known = {0, *(row.revision for row in job.revisions if row.kind == "instruction")}
    if type(instruction_revision) is not int or instruction_revision not in known:
        raise RunAnchorUnknownRevision(
            f"job {job_key} has no instruction revision {instruction_revision!r}")

    try:
        running = jobs.active_jobs()
    except jobs.JobRecordInvalid as exc:
        raise RunAnchorUnreadable(f"active job set is unreadable: {exc}") from exc
    for other in running:
        if other.job_id == job_key:
            continue
        if other.thread_id and other.thread_id == thread:
            raise RunAnchorAmbiguous(f"thread {thread} is claimed by jobs {job_key} and {other.job_id}")
        if other.project_root:
            theirs = Path(other.project_root).resolve()
            if project.is_relative_to(theirs) or theirs.is_relative_to(project):
                raise RunAnchorAmbiguous(
                    f"project {project} overlaps active job {other.job_id}")

    return RunAnchor(job_key, str(project), thread, instruction_revision)


def run_provenance_for_job(job_id: str) -> Dict[str, Any]:
    """The run identity a coding/dev record written for ``job_id`` must carry.

    Records exactly what the job says — an unbound project or unrecorded thread is written
    as empty and therefore never matches an anchor; it is not invented. An unknown or
    unreadable job raises.
    """
    job_key = _required_text(job_id, "job_id")
    job = _load_job(job_key)
    return {
        "job_id": job.job_id,
        "project_root": job.project_root,
        "thread_id": job.thread_id,
        "instruction_revision": int(job.applied_revision),
    }


def validate_run_provenance(value: Any) -> Dict[str, Any]:
    """Return an exact copy of a well-formed run provenance mapping, or raise ValueError."""
    if not isinstance(value, Mapping) or set(value) != set(RUN_PROVENANCE_FIELDS):
        raise ValueError(f"run provenance must have exactly {RUN_PROVENANCE_FIELDS}")
    for name in ("job_id", "project_root", "thread_id"):
        if type(value[name]) is not str:
            raise ValueError(f"run provenance {name} must be text")
    if not value["job_id"]:
        raise ValueError("run provenance job_id is required")
    revision = value["instruction_revision"]
    if type(revision) is not int or revision < 0:
        raise ValueError("run provenance instruction_revision must be a non-negative integer")
    return {name: value[name] for name in RUN_PROVENANCE_FIELDS}
