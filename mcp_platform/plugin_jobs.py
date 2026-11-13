"""Durable standing-job records: locked, atomic, fail-closed, revision-guarded.

A ``PluginJob`` is the durable record a later runner replays on a schedule
or event trigger, under a standing grant bound to exactly one
``(job_id, job_revision)`` pair. ``job_revision`` is a canonical digest of
the job's template content only -- plugin/app/connection identity, title,
objective, trigger, primary read tool, autonomy level, and allowed tools.
It deliberately excludes every field the runner advances on its own
(``cursor``, ``ledger``, ``next_due_at``, ``runs_today``,
``consecutive_failures``, ``paused_reason``, ``standing_authority_id``,
``execution_revision``) so a standing grant survives normal run-to-run
progress and only goes stale when the operator actually edits the job.

``PluginJobStore`` copies the locking, atomic-write, and fail-closed-read
pattern used by ``mcp_platform.plugin_authority.PluginAuthorityStore`` on
top of the shared ``mcp_platform.state_store.McpStateStore`` primitive: one
process-wide advisory lock guards read-modify-write, writes land through a
pinned temp-file-then-replace boundary, and any document that fails to
parse or fails structural/self-digest validation raises
``plugin_jobs_store_unavailable`` -- it is never treated as an empty store.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.paths import MCP_PLUGIN_JOBS_STATE_PATH, MCP_STATE_ROOT
from core.symphony_time import now_iso
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import RiskClass
from mcp_platform.state_store import McpStateStore

_SCHEMA_VERSION = 1
_MAX_TITLE = 80
_MAX_OBJECTIVE = 2000
_MIN_INTERVAL_SECONDS = 300
_MIN_POLL_FALLBACK_SECONDS = 300
#: 2x plugin_authority._MAX_PLAN_STEPS -- headroom for act jobs whose plan
#: touches more tools than one direct-turn plan step ever does.
_MAX_ALLOWED_TOOLS = 8
_MAX_LEDGER = 50
_MAX_JOBS = 256
_RISK_CLASSES = frozenset(member.value for member in RiskClass)

#: Fields the runner advances on its own under the store lock. Never part
#: of `job_revision`, so a standing grant bound to (job_id, job_revision)
#: stays valid across ordinary run progress.
_MUTABLE_FIELDS = (
    "cursor",
    "ledger",
    "next_due_at",
    "runs_today",
    "consecutive_failures",
    "paused_reason",
    "standing_authority_id",
    "execution_revision",
)

#: Exact field set `job_revision` is derived from -- the operator-authored
#: template content. Deliberately excludes job_id/created_at/nonce (identity,
#: not template) and every `_MUTABLE_FIELDS` entry (runner-advanced state).
_REVISION_FIELDS = (
    "plugin_id",
    "app_id",
    "account_key_hash",
    "connection_revision",
    "connection_authority_revision",
    "capability_revision",
    "title",
    "objective",
    "trigger",
    "primary_read_tool",
    "autonomy_level",
    "allowed_tools",
    "wake",
    "enabled",
    "max_runs_per_day",
    "max_consecutive_failures",
)

#: Fields `PluginJobStore.update()` may never take from its `changes`
#: mapping -- they are identity, not template, and re-deriving them from
#: caller input would corrupt the record's self-verifying digests.
_UPDATE_FORBIDDEN_FIELDS = frozenset({"job_id", "job_revision", "created_at", "nonce"})


def _plain(value: object) -> object:
    """Recursively coerce Mapping/tuple into `canonical_bytes`-safe dict/list."""

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [_plain(item) for item in value]
    return value


def _job_id_document(
    *, plugin_id: object, app_id: object, account_key_hash: object, created_at: object, nonce: object
) -> dict[str, object]:
    return {
        "plugin_id": plugin_id,
        "app_id": app_id,
        "account_key_hash": account_key_hash,
        "created_at": created_at,
        "nonce": nonce,
    }


def _job_id_hash(**fields: object) -> str:
    return sha256_hex(canonical_bytes(_job_id_document(**fields)))


def _revision_document(values: Mapping[str, object]) -> dict[str, object]:
    return {key: _plain(values[key]) for key in _REVISION_FIELDS}


def _revision_hash(values: Mapping[str, object]) -> str:
    return sha256_hex(canonical_bytes(_revision_document(values)))


@dataclass(frozen=True, slots=True)
class PluginJob:
    job_id: str
    job_revision: str
    plugin_id: str
    app_id: str
    account_key_hash: str
    connection_revision: str
    connection_authority_revision: str
    capability_revision: str
    title: str
    objective: str
    trigger: Mapping[str, object]
    primary_read_tool: Mapping[str, object]
    autonomy_level: str
    allowed_tools: tuple[Mapping[str, object], ...]
    wake: bool
    enabled: bool
    max_runs_per_day: int
    max_consecutive_failures: int
    created_at: str
    nonce: str
    cursor: Mapping[str, object] = field(default_factory=dict)
    ledger: tuple[Mapping[str, object], ...] = ()
    next_due_at: str = ""
    runs_today: int = 0
    consecutive_failures: int = 0
    paused_reason: str | None = None
    standing_authority_id: str = ""
    execution_revision: str = ""

    def __post_init__(self) -> None:
        error = "plugin_job_invalid"
        if type(self.allowed_tools) is not tuple:
            object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        if type(self.ledger) is not tuple:
            object.__setattr__(self, "ledger", tuple(self.ledger))
        if type(self.title) is not str or not (1 <= len(self.title) <= _MAX_TITLE):
            raise ValueError(error)
        if type(self.objective) is not str or not (1 <= len(self.objective) <= _MAX_OBJECTIVE):
            raise ValueError(error)
        if self.autonomy_level not in ("observe", "act"):
            raise ValueError(error)
        if not (1 <= len(self.allowed_tools) <= _MAX_ALLOWED_TOOLS):
            raise ValueError(error)
        risk_classes: list[str] = []
        for entry in self.allowed_tools:
            if type(entry) is not dict or type(entry.get("risk_class")) is not str:
                raise ValueError(error)
            risk_classes.append(entry["risk_class"])
        if any(risk not in _RISK_CLASSES for risk in risk_classes):
            raise ValueError(error)
        if self.autonomy_level == "observe" and any(
            risk != RiskClass.READ_ONLY.value for risk in risk_classes
        ):
            raise ValueError(error)
        if any(risk == RiskClass.EXTERNAL_UNREVIEWED.value for risk in risk_classes):
            raise ValueError(error)
        if type(self.trigger) is not dict:
            raise ValueError(error)
        trigger_kind = self.trigger.get("kind")
        if trigger_kind == "schedule":
            interval = self.trigger.get("interval_seconds")
            jitter = self.trigger.get("jitter_seconds", 0)
            if type(interval) is not int or interval < _MIN_INTERVAL_SECONDS:
                raise ValueError(error)
            if type(jitter) is not int or not 0 <= jitter <= interval // 4:
                raise ValueError(error)
        elif trigger_kind == "event":
            poll = self.trigger.get("poll_fallback_seconds")
            if type(poll) is not int or poll < _MIN_POLL_FALLBACK_SECONDS:
                raise ValueError(error)
            if self.trigger.get("kind_watched") == "resource_updated" and not self.trigger.get(
                "resource_uri"
            ):
                raise ValueError(error)
        else:
            raise ValueError(error)
        if type(self.primary_read_tool) is not dict:
            raise ValueError(error)
        if type(self.wake) is not bool or type(self.enabled) is not bool:
            raise ValueError(error)
        if type(self.max_runs_per_day) is not int or not (1 <= self.max_runs_per_day <= 96):
            raise ValueError(error)
        if type(self.max_consecutive_failures) is not int or not (
            1 <= self.max_consecutive_failures <= 10
        ):
            raise ValueError(error)
        if len(self.ledger) > _MAX_LEDGER:
            raise ValueError(error)
        if self.paused_reason is not None and type(self.paused_reason) is not str:
            raise ValueError(error)
        expected_job_id = _job_id_hash(
            plugin_id=self.plugin_id,
            app_id=self.app_id,
            account_key_hash=self.account_key_hash,
            created_at=self.created_at,
            nonce=self.nonce,
        )
        if self.job_id != expected_job_id:
            raise ValueError(error)
        expected_revision = _revision_hash(
            {name: getattr(self, name) for name in _REVISION_FIELDS}
        )
        if self.job_revision != expected_revision:
            raise ValueError(error)

    @classmethod
    def new(cls, **kwargs: object) -> PluginJob:
        created_at = kwargs.pop("created_at", None) or now_iso()
        nonce = kwargs.pop("nonce", None) or os.urandom(16).hex()
        try:
            revision_values = {name: kwargs[name] for name in _REVISION_FIELDS}
        except KeyError:
            raise ValueError("plugin_job_invalid") from None
        job_id = _job_id_hash(
            plugin_id=revision_values["plugin_id"],
            app_id=revision_values["app_id"],
            account_key_hash=revision_values["account_key_hash"],
            created_at=created_at,
            nonce=nonce,
        )
        job_revision = _revision_hash(revision_values)
        return cls(
            job_id=job_id,
            job_revision=job_revision,
            created_at=created_at,
            nonce=nonce,
            **kwargs,
        )


def _to_dict(job: PluginJob) -> dict[str, object]:
    return {name: _plain(getattr(job, name)) for name in job.__dataclass_fields__}


def _from_dict(raw: Mapping[str, object]) -> PluginJob:
    if type(raw) is not dict:
        raise ValueError("plugin_jobs_store_unavailable")
    try:
        return PluginJob(**raw)
    except TypeError:
        raise ValueError("plugin_jobs_store_unavailable") from None


class PluginJobStore:
    """Locked, atomic, fail-closed store for durable standing-job records."""

    def __init__(self, *, state_root: Path | None = None) -> None:
        root = Path(state_root) if state_root is not None else MCP_STATE_ROOT
        self.document_path = root / MCP_PLUGIN_JOBS_STATE_PATH.name
        self._state = McpStateStore(
            state_root=root,
            document_path=self.document_path,
            invalid_path_reason="plugin_jobs_store_unavailable",
        )

    @staticmethod
    def _empty() -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, "jobs": {}}

    def _read_locked(self) -> dict[str, object]:
        raw = self._state.read_bytes()
        if raw is None:
            return self._empty()
        try:
            document = json.loads(raw.decode("utf-8"))
            if (
                type(document) is not dict
                or set(document) != {"schema_version", "jobs"}
                or document["schema_version"] != _SCHEMA_VERSION
                or type(document["jobs"]) is not dict
                or len(document["jobs"]) > _MAX_JOBS
            ):
                raise ValueError
            for job_id, raw_job in document["jobs"].items():
                if type(job_id) is not str:
                    raise ValueError
                job = _from_dict(raw_job)
                if job.job_id != job_id:
                    raise ValueError
            return document
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ValueError("plugin_jobs_store_unavailable") from None

    def _write_locked(self, document: dict[str, object]) -> None:
        self._state.write_bytes(canonical_bytes(document))

    def create(self, job: PluginJob) -> PluginJob:
        if type(job) is not PluginJob:
            raise ValueError("plugin_job_invalid")
        with self._state.locked():
            document = self._read_locked()
            if job.job_id in document["jobs"]:
                raise ValueError("plugin_job_invalid")
            if len(document["jobs"]) >= _MAX_JOBS:
                raise ValueError("plugin_jobs_store_limit_exceeded")
            document["jobs"][job.job_id] = _to_dict(job)
            self._write_locked(document)
        return job

    def get(self, job_id: str) -> PluginJob:
        with self._state.locked():
            document = self._read_locked()
            raw = document["jobs"].get(job_id)
        if raw is None:
            raise ValueError("plugin_job_not_found")
        return _from_dict(raw)

    def list_for_plugin(self, plugin_id: str) -> tuple[PluginJob, ...]:
        with self._state.locked():
            document = self._read_locked()
            matching = [raw for raw in document["jobs"].values() if raw["plugin_id"] == plugin_id]
        return tuple(_from_dict(raw) for raw in matching)

    def update(
        self, job_id: str, expected_revision: str, changes: Mapping[str, object]
    ) -> PluginJob:
        if type(changes) is not dict:
            raise ValueError("plugin_job_invalid")
        unknown_fields = set(changes) - set(PluginJob.__dataclass_fields__)
        if unknown_fields or _UPDATE_FORBIDDEN_FIELDS & set(changes):
            raise ValueError("plugin_job_invalid")
        with self._state.locked():
            document = self._read_locked()
            raw = document["jobs"].get(job_id)
            if raw is None:
                raise ValueError("plugin_job_not_found")
            current = _from_dict(raw)
            if current.job_revision != expected_revision:
                raise ValueError("plugin_job_revision_conflict")
            merged = {name: getattr(current, name) for name in current.__dataclass_fields__}
            merged.update(changes)
            new_revision = _revision_hash(merged)
            updated = replace(current, **changes, job_revision=new_revision)
            document["jobs"][job_id] = _to_dict(updated)
            self._write_locked(document)
        return updated

    def delete(self, job_id: str, expected_revision: str) -> None:
        with self._state.locked():
            document = self._read_locked()
            raw = document["jobs"].get(job_id)
            if raw is None:
                raise ValueError("plugin_job_not_found")
            if raw["job_revision"] != expected_revision:
                raise ValueError("plugin_job_revision_conflict")
            del document["jobs"][job_id]
            self._write_locked(document)

    def advance_state(
        self,
        job_id: str,
        *,
        cursor: object = None,
        receipt: object = None,
        next_due_at: object = None,
        paused_reason: object = None,
        execution_revision: object = None,
        runs_today: object = None,
    ) -> PluginJob:
        """Advance runner-owned state only. Never touches `job_revision`.

        `paused_reason` follows a three-way convention because `None` is
        itself the job's legitimate "not paused" state: omit the keyword to
        leave the current pause untouched, pass a reason string to set it,
        or pass `False` to explicitly clear an existing pause.
        """

        with self._state.locked():
            document = self._read_locked()
            raw = document["jobs"].get(job_id)
            if raw is None:
                raise ValueError("plugin_job_not_found")
            current = _from_dict(raw)
            ledger = list(current.ledger)
            if receipt is not None:
                ledger.append(receipt)
                ledger = ledger[-_MAX_LEDGER:]
            if paused_reason is False:
                next_paused_reason = None
            elif paused_reason is None:
                next_paused_reason = current.paused_reason
            else:
                next_paused_reason = paused_reason
            updated = replace(
                current,
                cursor=current.cursor if cursor is None else cursor,
                ledger=tuple(ledger),
                next_due_at=current.next_due_at if next_due_at is None else next_due_at,
                paused_reason=next_paused_reason,
                execution_revision=(
                    current.execution_revision
                    if execution_revision is None
                    else execution_revision
                ),
                runs_today=current.runs_today if runs_today is None else runs_today,
            )
            document["jobs"][job_id] = _to_dict(updated)
            self._write_locked(document)
        return updated

    def invalidate_for_connection(
        self, plugin_id: str, app_id: str, account_key_hash: str
    ) -> tuple[PluginJob, ...]:
        with self._state.locked():
            document = self._read_locked()
            changed: list[PluginJob] = []
            for job_id, raw in document["jobs"].items():
                if (
                    raw["plugin_id"] == plugin_id
                    and raw["app_id"] == app_id
                    and raw["account_key_hash"] == account_key_hash
                ):
                    current = _from_dict(raw)
                    updated = replace(current, paused_reason="authority_invalid")
                    document["jobs"][job_id] = _to_dict(updated)
                    changed.append(updated)
            if changed:
                self._write_locked(document)
        return tuple(changed)


__all__ = ["PluginJob", "PluginJobStore"]
