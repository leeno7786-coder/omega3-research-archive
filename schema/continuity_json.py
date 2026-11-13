"""Strict parsing, canonical serialization, missingness and typed references.

The foundation of `omega.episode_continuity.v2` (CONTRACTS.md sections 1 and 8). Three
rules earn their strictness from measured defects in this repository:

**Duplicate keys are rejected on the raw string.** The live fold parses model output with
``json.loads`` (last-wins) and an ``ast.literal_eval`` fallback before any validator sees a
dict, so a second ``status`` silently overwrites the first. :func:`parse_json_object` is the
strict pass that has to run in front of that.

**"Could not run" never shares a value with "ran, found nothing."** AGENTS.md section 2.1.
:class:`Observation` makes the six missing-ness states nameable, so an outage, an absence,
a redaction and an explicitly empty result stop being interchangeable.

**Nothing is stringified on the way to bytes.** ``default=str`` turns evidence into a repr
and a hash into a lie, so :func:`canonical_bytes` admits only schema-admitted JSON values
and raises on anything else.

This module performs no filesystem or network access, holds no state, and owns no clock.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

CONTINUITY_SCHEMA_VERSION = "omega.episode_continuity.v2"

OBSERVATION_STATES: tuple[str, ...] = (
    "observed",
    "unknown",
    "not_applicable",
    "unavailable",
    "redacted",
    "not_captured",
)

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"\A[A-Za-z]:")

# ── errors ───────────────────────────────────────────────────────────────────


class ContinuityContractError(ValueError):
    """The shared base. Every continuity contract violation is one of these.

    A caller that catches this catches the whole vocabulary; a caller that catches a
    subclass has said which failure it is prepared to handle. It is never a valid empty
    record: an error means the work did not happen.
    """


class EventIdentityConflict(ContinuityContractError):
    """One event identity was presented again with different immutable content."""


class EpisodeOwnershipError(ContinuityContractError):
    """A record was presented by an owner that does not hold the episode."""


class EpisodeCaptureUnavailable(ContinuityContractError):
    """Capture could not run. Distinct from an episode that legitimately produced nothing."""


class CanonicalEpisodeUnavailable(ContinuityContractError):
    """A canonical episode a consumer requires is missing, unreadable or inconsistent."""


class HistoricalSnapshotUnavailable(ContinuityContractError):
    """The retained generation an as-of read requires is no longer available."""


class WindowMembershipError(ContinuityContractError):
    """A cohort that is not exactly the sealed, unique, eligible membership."""


class WindowCapacityExceeded(ContinuityContractError):
    """Mandatory window input does not fit. The sealed cohort is preserved, never shrunk."""

    def __init__(self, message: str, *, member_ids: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.member_ids: tuple[str, ...] = tuple(str(item) for item in member_ids)

    @property
    def member_count(self) -> int:
        return len(self.member_ids)


class TokenAccountingUnavailable(ContinuityContractError):
    """No verified counter. A character heuristic is not a token count."""


class WindowReferenceError(ContinuityContractError):
    """A citation that does not resolve to admitted, shown evidence."""


class WindowOutputError(ContinuityContractError):
    """Model output that the window response contract refuses."""


class StaleControlContext(ContinuityContractError):
    """A decision made against authority or trajectory state that has since moved."""


class MigrationConflict(ContinuityContractError):
    """An offline conversion whose source or destination no longer matches its record."""


# ── strict parsing ───────────────────────────────────────────────────────────


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ContinuityContractError(
                f"duplicate JSON object key {key!r}: a later value must never silently "
                "replace an earlier one"
            )
        seen.add(key)
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise ContinuityContractError(
        f"non-finite JSON value {name!r} is not an admitted schema value"
    )


def _finite_float(raw: str) -> float:
    """Convert a JSON number, refusing one that overflows to an infinity.

    ``parse_constant`` only sees the bare ``NaN`` / ``Infinity`` literals. An ordinary
    literal like ``1e9999`` overflows during normal float conversion, so without this hook it
    decoded to ``inf`` and the raw parser declared it acceptable — leaving the canonical
    serializer to reject it much later, or not at all. WO-02A C1.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:  # pragma: no cover - json only passes numerics
        raise ContinuityContractError(f"malformed JSON number {raw!r}: {exc}") from exc
    if not math.isfinite(value):
        raise ContinuityContractError(
            f"JSON number {raw!r} overflows to {value}; a non-finite value is not admitted"
        )
    return value


_DECODER = json.JSONDecoder(
    object_pairs_hook=_reject_duplicates,
    parse_constant=_reject_constant,
    parse_float=_finite_float,
)


def parse_json_object(raw: str) -> dict[str, object]:
    """Parse one JSON object, strictly, from *raw*.

    Rejects duplicate keys at any depth, non-finite numbers, a non-object root, malformed
    JSON and trailing non-whitespace material. Returns the parsed mapping unchanged: this
    function validates, it does not normalize.
    """
    if not isinstance(raw, str):
        raise ContinuityContractError(f"strict JSON parsing needs text, got {type(raw).__name__}")
    try:
        value, end = _DECODER.raw_decode(raw.lstrip())
    except ContinuityContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContinuityContractError(f"malformed JSON: {exc}") from exc
    remainder = raw.lstrip()[end:].strip()
    if remainder:
        raise ContinuityContractError(
            f"trailing material after the JSON object: {remainder[:40]!r}"
        )
    if not isinstance(value, dict):
        raise ContinuityContractError(
            f"a continuity document root must be a JSON object, got {type(value).__name__}"
        )
    return value


# ── canonical bytes ──────────────────────────────────────────────────────────


#: Contract types that may appear nested inside an accepted record and are serialized
#: through their own ``to_dict``. A REGISTRY, not duck-typing: admitting anything that
#: happens to expose ``to_dict`` is the same sin as ``default=str`` with extra steps, because
#: an arbitrary object would then decide its own serialized form. Each contract module
#: registers its own types at import (WO-02A C2).
_CONTRACT_TYPES: tuple[type, ...] = ()


def register_contract_type(cls: type) -> type:
    """Register *cls* as a nestable contract type. Usable as a decorator."""
    global _CONTRACT_TYPES
    if not isinstance(cls, type):
        raise ContinuityContractError("only a class can be registered as a contract type")
    if not callable(getattr(cls, "to_dict", None)):
        raise ContinuityContractError(f"{cls.__name__} must define to_dict to be nestable")
    if cls not in _CONTRACT_TYPES:
        _CONTRACT_TYPES = _CONTRACT_TYPES + (cls,)
    return cls


def contract_types() -> tuple[type, ...]:
    """The registered nestable contract types, for tests and diagnostics."""
    return _CONTRACT_TYPES


def _admitted(value: object, path: str) -> Any:
    """Return *value* as an admitted JSON value, or raise naming the offending path."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContinuityContractError(f"{path}: a non-finite number is not admitted")
        return value
    if isinstance(value, _CONTRACT_TYPES):
        return _admitted(value.to_dict(), path)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContinuityContractError(
                    f"{path}: object keys must be strings, got {type(key).__name__}"
                )
            out[key] = _admitted(item, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [_admitted(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ContinuityContractError(
        f"{path}: {type(value).__name__} is not an admitted schema value; "
        "canonical bytes never stringify an object, and only registered contract types "
        "serialize through their own to_dict"
    )


def canonical_bytes(value: object) -> bytes:
    """UTF-8 JSON with sorted keys, compact separators and preserved array order.

    No trailing newline, ``ensure_ascii=False``, ``allow_nan=False``, and no ``default``
    hook: an object this cannot represent is a contract violation, not something to
    stringify. Boolean is not an integer. Hash fields are never part of the bytes that
    produce them — the caller excludes them before calling.
    """
    return json.dumps(
        _admitted(value, "$"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: object) -> str:
    """The sha256 of :func:`canonical_bytes` for *value*."""
    import hashlib

    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# ── immutability helpers ─────────────────────────────────────────────────────


def freeze(value: Any, *, where: str = "$") -> Any:
    """Validate, then deep-copy immutably: mappings read-only, sequences tuples.

    **Validation comes first** (WO-02A C2). The predecessor did
    ``{str(key): ... for key, item in value.items()}``, so a mapping with keys ``1`` and
    ``"1"`` silently collapsed to one entry and the accepted record quietly lost a field.
    A non-string key is now refused before any conversion, a collision is never resolved by
    overwriting, and a value this contract cannot represent is refused here rather than at
    hashing time.

    Construction takes a copy so a caller mutating its own input afterwards cannot reach
    inside an accepted record, and the copy is read-only so a consumer cannot mutate the
    record it was handed.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContinuityContractError(f"{where}: a non-finite number is not admitted")
        return value
    if isinstance(value, _CONTRACT_TYPES):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContinuityContractError(
                    f"{where}: object keys must be strings, got {type(key).__name__} "
                    f"({key!r}); a non-string key must never be coerced into one"
                )
            if key in out:
                raise ContinuityContractError(f"{where}: duplicate object key {key!r}")
            out[key] = freeze(item, where=f"{where}.{key}")
        return MappingProxyType(out)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item, where=f"{where}[{index}]") for index, item in enumerate(value))
    raise ContinuityContractError(
        f"{where}: {type(value).__name__} is not an admitted schema value; "
        "an accepted record holds only JSON values and registered contract types"
    )


def unfreeze(value: Any) -> Any:
    """The JSON-ready mirror of :func:`freeze`, plus ``to_dict`` on typed members."""
    if isinstance(value, Mapping):
        return {str(key): unfreeze(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [unfreeze(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return unfreeze(to_dict())
    return value


def require_string_keys(value: Any, *, where: str) -> dict[str, Any]:
    """Return *value*'s items with their keys validated as strings, before any conversion.

    WO-02A closure F1. :func:`freeze` already refuses a non-string key, but it cannot detect a
    collision an upstream builder has already destroyed: a site that writes
    ``out[str(key)] = row`` collapses integer ``1`` and string ``"1"`` into one entry, and by
    the time ``freeze`` sees the result the discarded evidence is gone. Any builder that would
    stringify a key calls this first instead.
    """
    if not isinstance(value, Mapping):
        raise ContinuityContractError(f"{where} must be a JSON object, got {type(value).__name__}")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ContinuityContractError(
                f"{where} key {key!r} is {type(key).__name__}, not a string; a non-string key "
                "must never be stringified into one, because that can silently discard the "
                "entry it collides with"
            )
        if key in out:
            raise ContinuityContractError(f"{where} carries duplicate key {key!r}")
        out[key] = item
    return out


def require_keys(
    value: Any,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    where: str,
    error: type[ContinuityContractError] = ContinuityContractError,
) -> dict[str, Any]:
    """Accept a mapping with exactly *required* and at most *optional* keys."""
    if not isinstance(value, Mapping):
        raise error(f"{where} must be a JSON object, got {type(value).__name__}")
    present = set(value)
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - present)
    if missing:
        raise error(f"{where} is missing required field(s): {missing}")
    unknown = sorted(present - allowed)
    if unknown:
        raise error(f"{where} carries unknown field(s): {unknown}")
    return dict(value)


def require_text(value: Any, *, where: str, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContinuityContractError(f"{where} must be text, got {type(value).__name__}")
    if len(value) > limit:
        raise ContinuityContractError(f"{where} must be at most {limit} characters")
    if not allow_empty and not value.strip():
        raise ContinuityContractError(f"{where} must be nonempty")
    return value


def require_positive_int(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContinuityContractError(
            f"{where} must be an integer and never a boolean, got {type(value).__name__}"
        )
    if value <= 0:
        raise ContinuityContractError(f"{where} must be positive, got {value}")
    return value


def require_non_negative_int(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContinuityContractError(
            f"{where} must be an integer and never a boolean, got {type(value).__name__}"
        )
    if value < 0:
        raise ContinuityContractError(f"{where} must not be negative, got {value}")
    return value


def require_enum(value: Any, *, allowed: Sequence[str], where: str,
                 error: type[ContinuityContractError] = ContinuityContractError) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise error(f"{where} must be one of {list(allowed)}, got {value!r}")
    return value


def require_hex64(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise ContinuityContractError(f"{where} must be a 64-character lowercase hex digest")
    return value


def require_texts(value: Any, *, where: str, limit: int, maximum: int, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContinuityContractError(f"{where} must be a JSON array")
    rows = tuple(value)
    if not minimum <= len(rows) <= maximum:
        raise ContinuityContractError(f"{where} admits {minimum}..{maximum} items, got {len(rows)}")
    return tuple(require_text(item, where=f"{where}[{index}]", limit=limit) for index, item in enumerate(rows))


# ── Observation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Observation:
    """What is known about one fact, and how it is known.

    ``observed`` carries the value, including explicitly empty content — an empty string, an
    empty array and zero are real observations. Every other state carries ``value=None`` and
    a nonempty reason, so a consumer can never read a default as data. Historical absence is
    ``not_captured``, never ``failed``: the producer of a 2026-07 journal row did not fail,
    it did not exist yet.
    """

    state: str
    value: Any = None
    reason: str = ""

    def __post_init__(self) -> None:
        # Validate the state and the state/reason/value relationship BEFORE anything is
        # normalized or frozen (WO-02A C2): a conversion that runs first can destroy the
        # very thing the check was going to look at.
        require_enum(self.state, allowed=OBSERVATION_STATES, where="observation.state")
        if not isinstance(self.reason, str):
            raise ContinuityContractError("observation.reason must be text")
        if self.state == "observed":
            if self.reason.strip():
                raise ContinuityContractError(
                    "an observed observation records its value, not a reason it is absent"
                )
        else:
            if self.value is not None:
                raise ContinuityContractError(
                    f"a {self.state} observation must not carry a value; "
                    "an absent fact with a value is an invented one"
                )
            if not self.reason.strip():
                raise ContinuityContractError(f"a {self.state} observation must say why")
        object.__setattr__(self, "value", freeze(self.value, where="observation.value"))

    @classmethod
    def observed(cls, value: Any) -> "Observation":
        return cls(state="observed", value=value, reason="")

    @classmethod
    def missing(cls, state: str, *, reason: str) -> "Observation":
        if state == "observed":
            raise ContinuityContractError("observed is not a missing state")
        return cls(state=state, value=None, reason=reason)

    @property
    def is_observed(self) -> bool:
        return self.state == "observed"

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "value": unfreeze(self.value), "reason": self.reason}

    @classmethod
    def from_dict(cls, value: Any) -> "Observation":
        data = require_keys(value, required=("state", "value", "reason"), where="observation")
        return cls(state=data["state"], value=data["value"], reason=data["reason"])

    @classmethod
    def coerce(cls, value: Any, *, where: str) -> "Observation":
        """Accept an Observation or its dict form; refuse a bare value."""
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls.from_dict(value)
        raise ContinuityContractError(
            f"{where} must be an observation with an explicit state, not a bare "
            f"{type(value).__name__}"
        )


# ── references ───────────────────────────────────────────────────────────────


def _checked_relative_path(value: str) -> str:
    if value.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(value):
        raise ContinuityContractError(
            "a canonical locator must be profile-relative; an absolute path is host-bound"
        )
    parts = re.split(r"[\\/]+", value)
    if any(part == ".." for part in parts):
        raise ContinuityContractError("a canonical locator must not traverse upwards")
    return value


@dataclass(frozen=True)
class CanonicalRef:
    """A published canonical record, identified by id, version and content hash.

    ``relative_path`` is an optional convenience locator, profile-relative by contract:
    absolute drive or profile paths and parent traversal are refused, because a reference
    that names this machine cannot survive the SSD moving.
    """

    record_id: str
    record_version: int
    content_sha256: str
    relative_path: str = ""

    def __post_init__(self) -> None:
        require_text(self.record_id, where="canonical_ref.record_id", limit=512)
        require_positive_int(self.record_version, where="canonical_ref.record_version")
        require_hex64(self.content_sha256, where="canonical_ref.content_sha256")
        if not isinstance(self.relative_path, str):
            raise ContinuityContractError("canonical_ref.relative_path must be text")
        if self.relative_path:
            object.__setattr__(self, "relative_path", _checked_relative_path(self.relative_path))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "record_id": self.record_id,
            "record_version": self.record_version,
            "content_sha256": self.content_sha256,
        }
        if self.relative_path:
            out["relative_path"] = self.relative_path
        return out

    @classmethod
    def from_dict(cls, value: Any) -> "CanonicalRef":
        data = require_keys(
            value,
            required=("record_id", "record_version", "content_sha256"),
            optional=("relative_path",),
            where="canonical_ref",
        )
        return cls(
            record_id=data["record_id"],
            record_version=data["record_version"],
            content_sha256=data["content_sha256"],
            relative_path=data.get("relative_path", ""),
        )

    @classmethod
    def coerce(cls, value: Any, *, where: str) -> "CanonicalRef":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls.from_dict(value)
        raise ContinuityContractError(
            f"{where} must be a typed canonical reference, not a bare {type(value).__name__}"
        )


@dataclass(frozen=True)
class SourceRef:
    """An external or source-ledger object and revision.

    Deliberately NOT shaped like :class:`CanonicalRef`: it carries no record version and no
    canonical content hash, so it cannot be mistaken for proof that the thing it names was
    published into canonical memory.
    """

    source_id: str
    source_type: str
    revision: str = ""
    span_hash: str = ""

    def __post_init__(self) -> None:
        require_text(self.source_id, where="source_ref.source_id", limit=512)
        require_text(self.source_type, where="source_ref.source_type", limit=128)
        for name in ("revision", "span_hash"):
            if not isinstance(getattr(self, name), str):
                raise ContinuityContractError(f"source_ref.{name} must be text")

    def to_dict(self) -> dict[str, Any]:
        out = {"source_id": self.source_id, "source_type": self.source_type}
        if self.revision:
            out["revision"] = self.revision
        if self.span_hash:
            out["span_hash"] = self.span_hash
        return out

    @classmethod
    def from_dict(cls, value: Any) -> "SourceRef":
        data = require_keys(
            value,
            required=("source_id", "source_type"),
            optional=("revision", "span_hash"),
            where="source_ref",
        )
        return cls(
            source_id=data["source_id"],
            source_type=data["source_type"],
            revision=data.get("revision", ""),
            span_hash=data.get("span_hash", ""),
        )


@dataclass(frozen=True)
class CoverageClockRef:
    """``core.timekeeper``'s coverage clock, compared identity-then-number."""

    clock_epoch_id: str
    commit_seq: int

    def __post_init__(self) -> None:
        require_text(self.clock_epoch_id, where="coverage_clock.clock_epoch_id", limit=128)
        require_non_negative_int(self.commit_seq, where="coverage_clock.commit_seq")

    def covers(self, other: "CoverageClockRef") -> bool:
        return self.clock_epoch_id == other.clock_epoch_id and self.commit_seq >= other.commit_seq

    def to_dict(self) -> dict[str, Any]:
        return {"clock_epoch_id": self.clock_epoch_id, "commit_seq": self.commit_seq}

    @classmethod
    def from_dict(cls, value: Any) -> "CoverageClockRef":
        data = require_keys(
            value, required=("clock_epoch_id", "commit_seq"), where="coverage_clock"
        )
        return cls(clock_epoch_id=data["clock_epoch_id"], commit_seq=data["commit_seq"])


# Nestable contract types this module owns. Registered after definition so `freeze` passes
# them through unchanged and `canonical_bytes` serializes them through their own to_dict.
for _contract_type in (Observation, CanonicalRef, SourceRef, CoverageClockRef):
    register_contract_type(_contract_type)
del _contract_type


__all__ = [
    "CONTINUITY_SCHEMA_VERSION",
    "OBSERVATION_STATES",
    "contract_types",
    "register_contract_type",
    "CanonicalEpisodeUnavailable",
    "CanonicalRef",
    "ContinuityContractError",
    "CoverageClockRef",
    "EpisodeCaptureUnavailable",
    "EpisodeOwnershipError",
    "EventIdentityConflict",
    "HistoricalSnapshotUnavailable",
    "MigrationConflict",
    "Observation",
    "SourceRef",
    "StaleControlContext",
    "TokenAccountingUnavailable",
    "WindowCapacityExceeded",
    "WindowMembershipError",
    "WindowOutputError",
    "WindowReferenceError",
    "canonical_bytes",
    "content_hash",
    "freeze",
    "parse_json_object",
    "require_enum",
    "require_hex64",
    "require_keys",
    "require_non_negative_int",
    "require_positive_int",
    "require_text",
    "require_texts",
    "unfreeze",
]
