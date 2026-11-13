"""Atomic durable job controls: project ownership, one writer, and stop.

Unfinished agent work continues through Omega successor episodes (operator 2026-09-14),
never a durable job continuation. A continuation_ready status survives only in records
written before that change, and boot stops those jobs.

Revision labels are immutable global arrival numbers, not dense mailbox offsets.
Locks cover short record mutations only, never inference or tool execution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import math
from collections.abc import Collection, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from pressure_loop.cognition_queue import earlier_operator_turns_interpreted

log = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_STATUSES = {"running", "completed", "failed", "stopped", "continuation_ready"}
_LOCK = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_EXECUTING: dict[str, str] = {}
_EXECUTING_THREADS: dict[str, threading.Thread] = {}
_DEFERRED_SETTLEMENTS: dict[str, tuple[JobSnapshot, str, str]] = {}
_SCOPE_LOCK = threading.RLock()


class JobRecordInvalid(ValueError):
    """Durable corruption is an integrity failure, not a rejected model argument."""


@dataclass(frozen=True)
class JobRevision:
    revision: int
    kind: str
    text: str
    source_cycle_id: str
    arrival_seq: int


# One task's steps, carried from its first episode to its last. The agent lane runs one
# tool batch per episode, so without this every episode would start from the raw tool log.
MAX_JOB_PLAN_STEPS = 24
_PLAN_STEP_LIMIT = 300


# What this job's calls have already produced. The agent lane runs one tool batch per
# episode, so run.py's batch-local ledger cannot see a repeat that crosses episodes — and
# that is the only kind this lane can make.
MAX_JOB_ECHOES = 64
# Every path this TASK has changed, across all its episodes. A batch that touches
# nothing is not a task that has built nothing, and Omega judges the task.
MAX_JOB_TOUCHED_PATHS = 64
# Two reproduced results in a row is a fixed point, not a slow step.
MAX_JOB_STALLED_CALLS = 2


@dataclass(frozen=True)
class JobToolEcho:
    """What one exact tool call last produced. Execution control state, never memory."""

    signature: str
    digest: str


@dataclass(frozen=True)
class JobPlanStep:
    """One step of the build's plan. ``step`` is host-assigned; a model authors no id."""

    step: int
    text: str
    done: bool = False


@dataclass(frozen=True)
class JobStopReceipt:
    """The prefix (observed_revision, revision) was superseded, not observed."""

    revision: int
    observed_revision: int


@dataclass(frozen=True)
class JobExecutionCeiling:
    allowed_tools: tuple[str, ...]
    max_steps: int
    deadline: float
    max_files_changed: int | None


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    cycle_id: str
    objective: str
    workspace_root: str
    status: str
    latest_revision: int
    applied_revision: int
    revisions: tuple[JobRevision, ...]
    summary: str = ""
    owner_cycle_id: str = ""
    stop_receipts: tuple[JobStopReceipt, ...] = ()
    execution_ceiling: JobExecutionCeiling | None = None
    tool_steps: int = 0
    attempt_generation: int = 0
    terminal_carrier_generation: int = -1
    project_root: str = ""  # Explicitly unbound for pre-project-scope records.
    # The cognitive thread the job runs in (cross-lane T7), recorded when the job opens. An
    # operator turn sent while the job's batch is still executing is addressed through it:
    # no successor is staged yet, so the successor ledger cannot answer.
    thread_id: str = ""
    # The task's own steps, written by the first episode and cleared as the work lands.
    plan: tuple[JobPlanStep, ...] = ()
    echoes: tuple[JobToolEcho, ...] = ()
    stalled_calls: int = 0
    touched_paths: tuple[str, ...] = ()

    @property
    def handled_revision(self) -> int:
        return max(self.applied_revision, self.stop_receipts[-1].revision if self.stop_receipts else 0)


@dataclass(frozen=True)
class JobControlPoll:
    revision_cursor: int
    stop_requested: bool


def jobs_root() -> Path:
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "jobs"


def _path(job_id: str) -> Path:
    if type(job_id) is not str or not _ID.fullmatch(job_id):
        raise ValueError("invalid opaque job_id")
    root = jobs_root().resolve()
    path = (root / f"{job_id}.json").resolve()
    if path.parent != root:
        raise ValueError("job path escapes jobs root")
    return path


@contextmanager
def _mutex(job_id: str):
    # One ordering for all short control mutations, including cross-job claims.
    # Neither this mutex nor a per-job mutex spans model/tool execution.
    with _SCOPE_LOCK:
        key = str(_path(job_id))
        with _LOCK:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            yield


def canonical_project(project_root: str | Path, workspace_root: str | Path) -> Path:
    workspace = Path(workspace_root).resolve()
    target = Path(project_root)
    target = (target if target.is_absolute() else workspace / target).resolve()
    if not target.is_relative_to(workspace):
        raise ValueError("project scope escapes original workspace authority")
    if target.exists() and not target.is_dir():
        raise ValueError("project scope must be a directory")
    return target


def project_conflict(project_root: str | Path, *, exclude_job_id: str = "", running_only: bool = False) -> JobSnapshot | None:
    with _SCOPE_LOCK:
        target = Path(project_root).resolve()
        for job in active_jobs():
            if job.job_id == exclude_job_id or not job.project_root or (running_only and job.status != "running"):
                continue
            other = Path(job.project_root).resolve()
            if target.is_relative_to(other) or other.is_relative_to(target):
                return job
    return None


class ProjectClaimConflict(ValueError):
    """Another job owns the project this one needs — ordinary contention, not corruption.

    Typed so a caller can REFUSE with the owner named instead of failing whatever it was
    doing. A ``ValueError`` still, so the handlers that already treat contention as a
    transcript fact the model reads next (``_start_job``) are unchanged.
    """

    def __init__(self, owner_job_id: str, message: str) -> None:
        super().__init__(message)
        self.owner_job_id = str(owner_job_id)


def _check_project_claim(project: Path, job_id: str, *, running_only: bool = False) -> None:
    conflict = project_conflict(project, exclude_job_id=job_id, running_only=running_only)
    if conflict:
        raise ProjectClaimConflict(
            conflict.job_id,
            f"project owned by job {conflict.job_id}; stop that job or choose another project")


def bind_project(job_id: str, *, cycle_id: str, project_root: str,
                 attempt_generation: int | None = None) -> JobSnapshot:
    with _mutex(job_id):
        job = get_job(job_id)
        if job.status != "running" or job.owner_cycle_id != cycle_id:
            raise ValueError("project binding requires the genuine current job owner")
        if attempt_generation is not None and attempt_generation != job.attempt_generation:
            raise ValueError("project binding belongs to a stale attempt")
        project = canonical_project(project_root, job.workspace_root)
        if job.project_root and Path(job.project_root) != project:
            raise ValueError("a job's selected project scope is immutable")
        _check_project_claim(project, job_id, running_only=True)
        return _publish(replace(job, project_root=str(project)))


def owned_project(job_id: str, cycle_id: str) -> Path:
    with _mutex(job_id):
        job = get_job(job_id)
        if job.status != "running" or job.owner_cycle_id != cycle_id or not job.project_root:
            raise ValueError("mutable work requires start_job and current project ownership")
        project = canonical_project(job.project_root, job.workspace_root)
        if str(project) != job.project_root:
            raise ValueError("selected project identity changed after binding")
        _check_project_claim(project, job_id, running_only=True)
        return project


def _validate(job: JobSnapshot) -> JobSnapshot:
    _path(job.job_id)
    if any(type(getattr(job, name)) is not str for name in (
        "cycle_id", "objective", "workspace_root", "status", "summary", "owner_cycle_id"
    )) or not job.cycle_id or not Path(job.workspace_root).is_absolute():
        raise ValueError("invalid job identity/workspace")
    if job.status not in _STATUSES or (job.status == "running" and not job.owner_cycle_id):
        raise ValueError("invalid job status/owner")
    if type(job.thread_id) is not str or len(job.thread_id) > 128:
        raise ValueError("invalid job thread")
    if type(job.project_root) is not str or (job.project_root and not Path(job.project_root).is_absolute()):
        raise ValueError("invalid project scope")
    if job.project_root and not Path(job.project_root).is_relative_to(Path(job.workspace_root)):
        raise ValueError("project scope exceeds recorded workspace authority")
    for cursor in (job.latest_revision, job.applied_revision):
        if type(cursor) is not int or cursor < 0:
            raise ValueError("invalid job revision cursor")
    previous = 0
    for row in job.revisions:
        if (type(row.revision) is not int or type(row.arrival_seq) is not int
            or row.revision != row.arrival_seq or row.revision <= previous
            or row.kind not in {"instruction", "stop"}
            or type(row.text) is not str or type(row.source_cycle_id) is not str
            or not row.source_cycle_id or (row.kind == "instruction" and not row.text.strip())
            or (row.kind == "stop" and row.text)):
            raise ValueError("invalid job revision")
        previous = row.revision
    if job.latest_revision != previous or job.applied_revision not in {0, *(r.revision for r in job.revisions)}:
        raise ValueError("invalid job revision bounds")
    previous_stop = 0
    for receipt in job.stop_receipts:
        if (type(receipt.revision) is not int or type(receipt.observed_revision) is not int
            or receipt.revision <= previous_stop or not 0 <= receipt.observed_revision < receipt.revision
            or receipt.observed_revision not in {0, *(r.revision for r in job.revisions)}
            or not any(r.revision == receipt.revision and r.kind == "stop" for r in job.revisions)):
            raise ValueError("invalid stop disposition receipt")
        previous_stop = receipt.revision
    if type(job.tool_steps) is not int or job.tool_steps < 0:
        raise ValueError("invalid durable tool count")
    if type(job.attempt_generation) is not int or job.attempt_generation < 0:
        raise ValueError("invalid continuation attempt generation")
    if (type(job.terminal_carrier_generation) is not int
        or not -1 <= job.terminal_carrier_generation <= job.attempt_generation):
        raise ValueError("invalid terminal carrier disposition")
    if type(job.echoes) is not tuple or len(job.echoes) > MAX_JOB_ECHOES:
        raise ValueError("invalid job tool echoes")
    for echo in job.echoes:
        if (type(echo) is not JobToolEcho or type(echo.signature) is not str
                or type(echo.digest) is not str or not echo.signature or not echo.digest):
            raise ValueError("invalid job tool echo")
    if len({echo.signature for echo in job.echoes}) != len(job.echoes):
        raise ValueError("duplicate job tool echo signature")
    if type(job.stalled_calls) is not int or job.stalled_calls < 0:
        raise ValueError("invalid job stalled call count")
    if (type(job.touched_paths) is not tuple
            or len(job.touched_paths) > MAX_JOB_TOUCHED_PATHS
            or any(type(path) is not str or not path for path in job.touched_paths)):
        raise ValueError("invalid job touched paths")
    if len(set(job.touched_paths)) != len(job.touched_paths):
        raise ValueError("duplicate job touched path")
    if type(job.plan) is not tuple or len(job.plan) > MAX_JOB_PLAN_STEPS:
        raise ValueError("invalid job plan")
    for position, step in enumerate(job.plan, start=1):
        if (type(step) is not JobPlanStep or step.step != position
                or type(step.text) is not str or not step.text.strip()
                or len(step.text) > _PLAN_STEP_LIMIT or type(step.done) is not bool):
            raise ValueError("invalid job plan step")
    ceiling = job.execution_ceiling
    if ceiling is not None and (
        type(ceiling.allowed_tools) is not tuple or any(type(name) is not str or not name for name in ceiling.allowed_tools)
        or type(ceiling.max_steps) is not int or ceiling.max_steps < 0
        or type(ceiling.deadline) not in (int, float) or not math.isfinite(ceiling.deadline)
        or (ceiling.max_files_changed is not None and (type(ceiling.max_files_changed) is not int or ceiling.max_files_changed < 0))
    ):
        raise ValueError("invalid native execution ceiling")
    return job


def get_job(job_id: str) -> JobSnapshot:
    with _mutex(job_id):
        path = _path(job_id)
        if not path.exists():
            raise KeyError(f"unknown job {job_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                # The initial job schema represents attempt zero. Preserve
                # already queued/restored carrier identity during upgrade.
                payload.setdefault("attempt_generation", 0)
                payload.setdefault("terminal_carrier_generation", -1)
                payload.setdefault("project_root", "")
                payload.setdefault("thread_id", "")
                payload.setdefault("plan", [])
                payload.setdefault("echoes", [])
                payload.setdefault("stalled_calls", 0)
                payload.setdefault("touched_paths", [])
            if not isinstance(payload, dict) or set(payload) != {f.name for f in fields(JobSnapshot)}:
                raise ValueError("invalid job record fields")
            if type(payload["revisions"]) is not list:
                raise ValueError("invalid job revisions")
            payload["revisions"] = tuple(JobRevision(**row) for row in payload["revisions"])
            payload["stop_receipts"] = tuple(JobStopReceipt(**row) for row in payload["stop_receipts"])
            if type(payload["plan"]) is not list:
                raise ValueError("invalid job plan")
            payload["plan"] = tuple(JobPlanStep(**row) for row in payload["plan"])
            if type(payload["echoes"]) is not list:
                raise ValueError("invalid job tool echoes")
            payload["echoes"] = tuple(JobToolEcho(**row) for row in payload["echoes"])
            if type(payload["touched_paths"]) is not list:
                raise ValueError("invalid job touched paths")
            payload["touched_paths"] = tuple(str(path) for path in payload["touched_paths"])
            if payload["execution_ceiling"] is not None:
                ceiling = payload["execution_ceiling"]
                ceiling["allowed_tools"] = tuple(ceiling["allowed_tools"])
                payload["execution_ceiling"] = JobExecutionCeiling(**ceiling)
            job = _validate(JobSnapshot(**payload))
            if job.job_id != job_id:
                raise ValueError("job file identity mismatch")
            return job
        except (ValueError, TypeError, KeyError) as exc:
            raise JobRecordInvalid(f"invalid durable job {job_id}: {exc}") from exc


def _publish(job: JobSnapshot) -> JobSnapshot:
    _validate(job)
    path = _path(job.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(asdict(job), stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return job


def open_job(job_id: str, cycle_id: str, objective: str, workspace_root: Path, *, execution_ceiling: JobExecutionCeiling | None = None,
             project_root: str | Path | None = None, thread_id: str = "") -> JobSnapshot:
    """Open a job, or reopen it for the same cycle, objective and workspace.

    ``thread_id`` is the cognitive thread of the episode opening the job. It is part of the
    job's identity: a reopen naming a DIFFERENT thread is refused like any other identity
    conflict, because operator mail addressed to the job would otherwise be pinned to one
    thread while the job folds in another. An empty value asserts nothing, and a record
    opened before threads were recorded adopts the first thread a reopen names.
    """
    thread = str(thread_id or "")
    with _mutex(job_id):
        root = str(Path(workspace_root).resolve())
        if _path(job_id).exists():
            job = get_job(job_id)
            if (job.cycle_id, job.objective, job.workspace_root) != (cycle_id, objective, root):
                raise ValueError("job reopen identity conflict")
            if project_root is not None and str(canonical_project(project_root, root)) != job.project_root:
                raise ValueError("job reopen project conflict")
            if thread and job.thread_id and thread != job.thread_id:
                raise ValueError("job reopen thread conflict")
            if thread and not job.thread_id:
                job = _publish(replace(job, thread_id=thread))
            if job.status != "running":
                # The operator's task continues through an Omega successor: its next tool
                # batch takes the same project back under the same single writer.
                if job.project_root:
                    _check_project_claim(Path(job.project_root), job_id, running_only=True)
                job = _publish(replace(job, status="running", owner_cycle_id=cycle_id))
            return job
        project = canonical_project(project_root, root) if project_root is not None else None
        if project is not None:
            _check_project_claim(project, job_id)
        return _publish(JobSnapshot(job_id, cycle_id, objective, root, "running", 0, 0, (), owner_cycle_id=cycle_id,
                                    execution_ceiling=execution_ceiling, project_root=str(project) if project else "",
                                    thread_id=thread))


def job_tool_ceiling(job: JobSnapshot, *, continuation: bool) -> tuple[str, ...] | None:
    ceiling = job.execution_ceiling
    if ceiling is None:
        return None
    if job.tool_steps >= ceiling.max_steps or time.time() >= ceiling.deadline:
        return ()
    if continuation and ceiling.max_files_changed is not None:
        return tuple(name for name in ceiling.allowed_tools if name in {
            "list_directory", "read_files", "search_repo", "inspect_git_diff", "remember_dev",
        })
    return ceiling.allowed_tools


def reserve_job_tool(job_id: str, name: str, *, continuation: bool) -> str:
    """Durably consume a native step before effects, never reset bounds on restart.

    A continuation with a file-count bound cannot safely run an arbitrary
    writer/command: retain read-only capabilities and report the limitation.
    """
    with _mutex(job_id):
        job = get_job(job_id)
        ceiling = job.execution_ceiling
        if ceiling is None:
            return ""
        if name not in ceiling.allowed_tools:
            return "native execution ceiling forbids this tool"
        if job.tool_steps >= ceiling.max_steps or time.time() >= ceiling.deadline:
            return "native execution ceiling exhausted its original step/time bound"
        if continuation and ceiling.max_files_changed is not None and name not in {
            "list_directory", "read_files", "search_repo", "inspect_git_diff", "remember_dev",
        }:
            return "native execution ceiling cannot preserve the original file-count bound for this continuation tool"
        _publish(replace(job, tool_steps=job.tool_steps + 1))
        return ""


def active_jobs() -> tuple[JobSnapshot, ...]:
    return tuple(job for path in sorted(jobs_root().glob("*.json"))
                 if (job := get_job(path.stem)).status == "running")


def _append(job_id: str, kind: str, text: str, source_cycle_id: str, arrival_seq: int) -> JobRevision:
    if type(arrival_seq) is not int or arrival_seq <= 0 or type(source_cycle_id) is not str or not source_cycle_id:
        raise ValueError("invalid control arrival identity")
    if type(text) is not str or (kind == "instruction" and not text.strip()):
        raise ValueError("invalid control instruction")
    row = JobRevision(arrival_seq, kind, text, source_cycle_id, arrival_seq)
    with _mutex(job_id):
        job = get_job(job_id)
        for prior in job.revisions:
            if prior.arrival_seq == arrival_seq:
                if prior != row:
                    raise ValueError("job revision conflict")
                return prior
        if type(arrival_seq) is not int or arrival_seq <= job.handled_revision:
            raise ValueError("job control arrival precedes observed boundary")
        revisions = tuple(sorted((*job.revisions, row), key=lambda r: (r.arrival_seq, r.source_cycle_id)))
        _publish(replace(job, revisions=revisions, latest_revision=revisions[-1].revision))
    _wake()
    return row


def request_stop(job_id: str, *, source_cycle_id: str, arrival_seq: int) -> JobRevision:
    return _append(job_id, "stop", "", source_cycle_id, arrival_seq)


def request_instruction(job_id: str, text: str, *, source_cycle_id: str, arrival_seq: int) -> JobRevision:
    """An explicit job-control instruction for a job already running; its batch reads it mid-run.

    Retained ONLY for explicit job control (cross-lane spec T7). Ordinary chat sent during a
    build never comes here: it is pinned as ``operator_instruction`` mail for the job's thread
    and reaches Omega at the fold after the batch, so the batch in flight executes under the
    state it started with (non-preemption).
    """
    return _append(job_id, "instruction", text, source_cycle_id, arrival_seq)


def revisions_after(job_id: str, revision: int) -> tuple[JobRevision, ...]:
    return tuple(row for row in get_job(job_id).revisions if row.revision > revision)


def poll_job_controls(messages: list[dict[str, object]], job_id: str, after_revision: int) -> JobControlPoll:
    cursor = max(after_revision, get_job(job_id).handled_revision)
    stop = False
    for row in revisions_after(job_id, cursor):
        if not earlier_operator_turns_interpreted(row.arrival_seq):
            break
        cursor = row.revision
        if row.kind == "stop":
            stop = True
            break
        messages.append({"role": "user", "content": f"Job revision {row.revision}: {row.text}"})
    return JobControlPoll(cursor, stop)


def acknowledge_job_controls(job_id: str, through_revision: int) -> JobSnapshot:
    with _mutex(job_id):
        job = get_job(job_id)
        if any(row.revision == through_revision and row.kind == "stop" for row in job.revisions):
            return job  # stop disposition is independent of model observation
        return _publish(replace(job, applied_revision=max(job.applied_revision, through_revision)))


def acknowledge_job_stop(job_id: str, through_revision: int) -> JobSnapshot:
    with _mutex(job_id):
        job = get_job(job_id)
        if any(receipt.revision == through_revision for receipt in job.stop_receipts):
            return job
        receipt = JobStopReceipt(through_revision, job.applied_revision)
        return _publish(replace(job, stop_receipts=(*job.stop_receipts, receipt)))


def enter_writer(job_id: str, cycle_id: str) -> bool:
    """One invocation owns tools; the mutex is released before any slow work."""
    with _mutex(job_id):
        job = get_job(job_id)
        key = str(_path(job_id))
        if job.status != "running" or job.owner_cycle_id != cycle_id or key in _EXECUTING:
            return False
        _EXECUTING[key] = cycle_id
        _EXECUTING_THREADS[key] = threading.current_thread()
        return True


def set_job_plan(job_id: str, steps: Collection[str]) -> JobSnapshot:
    """Record the task's steps. The host numbers them; the model supplies the words only."""
    texts = [" ".join(str(text or "").split())[:_PLAN_STEP_LIMIT] for text in steps]
    texts = [text for text in texts if text]
    if not texts:
        raise ValueError("a job plan needs at least one step")
    if len(texts) > MAX_JOB_PLAN_STEPS:
        raise ValueError(f"a job plan holds at most {MAX_JOB_PLAN_STEPS} steps")
    with _mutex(job_id):
        job = get_job(job_id)
        done = {step.text: step.done for step in job.plan}
        plan = tuple(
            JobPlanStep(position, text, bool(done.get(text, False)))
            for position, text in enumerate(texts, start=1)
        )
        return _publish(replace(job, plan=plan))


def complete_plan_steps(job_id: str, numbers: Collection[int]) -> JobSnapshot:
    """Clear the named steps. Clearing a step nobody planned is a mistake, not a no-op."""
    wanted = {int(number) for number in numbers}
    with _mutex(job_id):
        job = get_job(job_id)
        known = {step.step for step in job.plan}
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError(f"no such plan step: {', '.join(str(number) for number in unknown)}")
        plan = tuple(
            replace(step, done=True) if step.step in wanted else step for step in job.plan
        )
        return _publish(replace(job, plan=plan))


def record_tool_echo(job_id: str, signature: str, digest: str) -> bool:
    """Remember what this call produced; True when it reproduced an earlier result.

    `signature` identifies the exact call (tool plus arguments) and `digest` fingerprints
    what came back. A hit means this step of the task added nothing the task did not
    already have — the shape that cost seven live episodes on 2026-09-20.
    """
    key = str(signature)
    body = str(digest)
    if not key or not body:
        raise ValueError("a tool echo needs both a signature and a digest")
    with _mutex(job_id):
        job = get_job(job_id)
        repeat = any(echo.signature == key and echo.digest == body for echo in job.echoes)
        # One entry per signature: the newest result for a call is the one to compare against.
        kept = tuple(echo for echo in job.echoes if echo.signature != key)
        echoes = (JobToolEcho(key, body), *kept)[:MAX_JOB_ECHOES]
        stalled = job.stalled_calls + 1 if repeat else 0
        _publish(replace(job, echoes=echoes, stalled_calls=stalled))
        return repeat


def record_touched_paths(job_id: str, paths: Collection[str]) -> JobSnapshot:
    """Fold one batch's changed paths into the TASK's ledger, oldest kept, newest appended.

    The agent lane runs one batch per episode, so `touched_files` on a receipt only ever
    describes that episode. Omega judges whether the OPERATOR's request is satisfied, and
    for that it needs what the whole task has changed.
    """
    fresh = [text for text in (" ".join(str(path or "").split()) for path in paths) if text]
    with _mutex(job_id):
        job = get_job(job_id)
        merged = list(job.touched_paths)
        for path in fresh:
            if path not in merged:
                merged.append(path)
        # Keep the EARLIEST evidence when the ledger is full: the first write a task made
        # is the proof that it built something, and that is the claim Omega got wrong.
        return _publish(replace(job, touched_paths=tuple(merged[:MAX_JOB_TOUCHED_PATHS])))


def job_is_stalled(job: JobSnapshot) -> bool:
    """The task has reproduced its own results enough times that only ending it is honest."""
    return job.stalled_calls >= MAX_JOB_STALLED_CALLS


def render_job_plan(job: JobSnapshot) -> str:
    """The plan as the next episode reads it: what is done, and what is left."""
    return "\n".join(
        f"{step.step}. [{'done' if step.done else 'open'}] {step.text}" for step in job.plan
    )


def plan_is_complete(job: JobSnapshot) -> bool:
    """True only when a plan exists and every step of it is cleared."""
    return bool(job.plan) and all(step.done for step in job.plan)


def reclaim_writer(job_id: str, cycle_id: str) -> JobSnapshot | None:
    """Take back a job whose writer died with the process, for the continuation of its work.

    A job the HOST stopped is not a finished job: ``reconcile_jobs_at_boot``'s own contract is
    that unfinished work continues through an Omega successor, and ``open_job`` reopens a
    non-running job for that successor's batch. The continuation restored at boot reaches
    ``enter_writer`` first, though, and boot had cleared the owner and settled the status — so
    it was refused, told "this job already has an active writer", and the operator's task
    stalled with its accepted decisions undelivered (review 2026-09-19, found through T14).

    Only a job with NO live writer is reclaimed, and only one the host stopped: an executing
    job, one another cycle owns, and one the OPERATOR stopped (a stop revision or its receipt)
    are all refused exactly as before, as are ``completed`` and ``failed``. Those all return
    ``None``.

    Raises ``ProjectClaimConflict`` when another job has taken the project meanwhile — which
    it can, because the project was unowned while this job was stopped. That is the one
    EXPECTED failure here and callers must handle it, naming the owner; nothing is written
    before the check, so both job records are left exactly as they were.
    """
    with _mutex(job_id):
        job = get_job(job_id)
        if str(_path(job_id)) in _EXECUTING or job.owner_cycle_id or job.status != "stopped":
            return None
        if any(row.kind == "stop" for row in job.revisions) or job.stop_receipts:
            return None
        if job.project_root:
            _check_project_claim(Path(job.project_root), job_id, running_only=True)
        return _publish(replace(job, status="running", owner_cycle_id=cycle_id, summary=""))


def leave_writer(job_id: str, cycle_id: str) -> None:
    """Release this invocation, applying an unchanged fold settlement before another can enter."""
    settled = False
    with _mutex(job_id):
        key = str(_path(job_id))
        if (_EXECUTING.get(key) != cycle_id
                or _EXECUTING_THREADS.get(key) is not threading.current_thread()):
            return
        pending = _DEFERRED_SETTLEMENTS.get(key)
        if pending is not None:
            expected, status, summary = pending
            job = get_job(job_id)
            if (job.status == "running"
                    and (job.owner_cycle_id, job.latest_revision, job.attempt_generation)
                    == (expected.owner_cycle_id, expected.latest_revision, expected.attempt_generation)):
                _publish(replace(job, status=status, summary=summary, owner_cycle_id=""))
                settled = True
            else:
                log.warning("deferred settlement for job %s discarded after its state changed", job_id)
        _EXECUTING.pop(key)
        _EXECUTING_THREADS.pop(key, None)
        _DEFERRED_SETTLEMENTS.pop(key, None)
    if settled:
        _hand_off_held_mail()
        _wake()


def settle_if_idle(job_id: str, *, expected_owner: str, expected_revision: int,
                   status: str, summary: str) -> JobSnapshot | None:
    """CAS an orphaned running job to terminal only when no invocation is executing.

    Returns None on any ownership, revision, status or execution mismatch and never defers.
    Takes only job locks, so the caller may hold the successor ledger lock while proving
    no viable continuation, unpublished transaction, queue item or carrier remains. Performs
    no handoff or wake: the caller invokes those only after releasing its outer locks.
    """
    if status not in {"completed", "failed", "stopped"}:
        raise ValueError("invalid final job status")
    with _mutex(job_id):
        job = get_job(job_id)
        key = str(_path(job_id))
        if (job.status != "running" or job.owner_cycle_id != expected_owner
                or job.latest_revision != expected_revision or key in _EXECUTING):
            return None
        job = _publish(replace(job, status=status, summary=summary, owner_cycle_id=""))
        _DEFERRED_SETTLEMENTS.pop(key, None)
        return job


def settle_job_when_idle(job_id: str, *, status: str, summary: str,
                         expected_owner_cycle_id: str | None = None,
                         expected_revision: int | None = None) -> JobSnapshot | None:
    """Settle a fold without releasing any invocation that is still executing.

    Returns None when an optional owner/revision comparison fails, the unchanged running
    snapshot when deferred, or the terminal snapshot when settled (including an already
    settled job). A newer request replaces the pending request for the active invocation.
    The caller must hold no successor, mail or job locks: immediate settlement hands off
    held mail after releasing this function's locks.

    While deferred, the durable job stays running and retains its project. Its matching
    leave_writer applies the request atomically before releasing the reservation, unless
    the owner, revision or attempt changed. Executor finish_job takes precedence. Requests
    are process-local: a crash leaves unfinished work for ordinary boot reconciliation,
    never a completed receipt for a writer whose outcome is unknown. A publication failure
    retains the reservation and request for retry or conservative boot recovery.
    """
    if status not in {"completed", "failed", "stopped"}:
        raise ValueError("invalid final job status")
    with _mutex(job_id):
        job = get_job(job_id)
        if ((expected_owner_cycle_id is not None and job.owner_cycle_id != expected_owner_cycle_id)
                or (expected_revision is not None and job.latest_revision != expected_revision)):
            return None
        if job.status != "running":
            return job
        key = str(_path(job_id))
        if key in _EXECUTING:
            _DEFERRED_SETTLEMENTS[key] = (job, status, summary)
            return job
        job = _publish(replace(job, status=status, summary=summary, owner_cycle_id=""))
        _DEFERRED_SETTLEMENTS.pop(key, None)
    _hand_off_held_mail()
    _wake()
    return job


def finish_job(job_id: str, *, status: str, summary: str) -> JobSnapshot:
    """Finalize an invocation whose tools have returned; external folds use settle_job_when_idle.

    Both executor callers invoke this in their exit paths before leave_writer. Their actual
    execution outcome supersedes any deferred fold request. Call without other store locks.
    """
    if status not in {"completed", "failed", "stopped"}:
        raise ValueError("invalid final job status")
    with _mutex(job_id):
        job = get_job(job_id)
        job = _publish(replace(job, status=status, summary=summary, owner_cycle_id=""))
        key = str(_path(job_id))
        _EXECUTING.pop(key, None)
        _EXECUTING_THREADS.pop(key, None)
        _DEFERRED_SETTLEMENTS.pop(key, None)
    # AFTER the settled status is published: operator mail pinned before this point is
    # visible to the sweep, and ingress re-checks the status after pinning, so a pin racing
    # this settlement is handed off by one of the two (cross-lane T7).
    _hand_off_held_mail()
    _wake()
    return job


def reconcile_jobs_at_boot(restorable_job_ids: Collection[str]) -> tuple[JobSnapshot, ...]:
    """Stop every job whose writer died with the process and is not being restored.

    Unfinished work continues through Omega successors, so a stopped job is not requeued.
    """
    for path in sorted(jobs_root().glob("*.json")):
        with _mutex(path.stem):
            job = get_job(path.stem)
            restorable = job.job_id in restorable_job_ids
            if isinstance(restorable_job_ids, Mapping):
                restorable = restorable and restorable_job_ids[job.job_id] == job.owner_cycle_id
            if job.status == "continuation_ready" or (job.status == "running" and not restorable):
                _publish(replace(job, status="stopped", owner_cycle_id="",
                                 summary=job.summary or "interrupted before it finished"))
    _hand_off_held_mail(at_boot=True)
    return active_jobs()


def _hand_off_held_mail(*, at_boot: bool = False) -> None:
    """Operator mail held for a job that can no longer fold becomes an ordinary turn (T7).

    Never fails a settlement: the mail stays durable and the next sweep — another
    settlement, an episode terminus, ingress or boot — retries it.
    """
    try:
        from pressure_loop import held_instruction_handoff as handoff

        if at_boot:
            handoff.reconcile_handoffs_at_boot()
        else:
            handoff.hand_off_orphaned_instructions()
    except Exception:  # noqa: BLE001 - see docstring
        log.warning("held operator mail handoff failed; a later sweep retries", exc_info=True)


def _wake() -> None:
    from pressure_loop.loop import notify_work

    notify_work()


def reset_for_tests() -> None:
    with _SCOPE_LOCK, _LOCK:
        _EXECUTING.clear()
        _EXECUTING_THREADS.clear()
        _DEFERRED_SETTLEMENTS.clear()
