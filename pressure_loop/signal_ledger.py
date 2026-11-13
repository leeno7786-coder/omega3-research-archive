"""Record-only SignalEvent ledger — Step 8 (the final step) of the metacognitive
pressure evolution.

(docs/architecture/metacognitive-pressure-evolution.md §7 row 8, §7.1 SignalEvent
contract, §5 "record-only Phase 8".)

A append-only audit substrate (north-star #1: "the substrate exists + is audited").
Every meaningful cognitive event — an episode outcome, a mastery change, … — can be
RECORDED here as a structured :class:`SignalEvent` for offline analysis and as the
ground layer a future signal-driven design could read. This step is deliberately
RECORD-ONLY and MINIMAL:

  - NO CONSUMER: nothing reads this ledger to make a decision. It is substrate +
    audit; this module exposes ``record`` only (no read/resolve/compose). The doc
    forbids a "soft classifier on the hot path".
  - NO BIG-BANG: Step 8 wires exactly TWO producers (Omega result + mastery update),
    both at clean per-episode/per-window seams that already do a failure-soft write.
    The other §7.1 producers (operator ingress, onboarding, recall, StrengthIndex,
    AUTO/APPLY result, neuromod, DeepSleep) are deferred to separate additive commits.
  - NOT ON THE HOT TICK: the producers run at episode-close / consolidation, never
    inside gather_contributors / evaluate — zero added cognition latency.
  - FLAG-GATED + BYTE-IDENTICAL: ``SIGNALS_RECORD=False`` (default) → ``record`` does
    literally nothing (no file, no I/O) on its very first line. Flipping it ON changes
    NO cognition behavior — the only difference is whether the side audit file grows —
    so there is no behavioral flip (no 8a/8b split). The operator flips it when they
    want the trail.
  - HONEST FIELDS (feedback_honest_signals_over_defaults): every measured-quantity
    field (valence/confidence/salience/uncertainty/cost) is Optional and OMITTED from
    the record when not actually measured at the producing seam — never fabricated to 0.

NB distinct from ``guardian.terminal_feedback.emit_signal`` (a differently-shaped
live "signal" telemetry: name/value/components/narrative). This is the structured
§7.1 SignalEvent audit ledger; do not conflate or merge them onto the firehose.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from core.paths import OMEGA_ROOT

# Flag (Step-0 idiom: module constant, NO env var, direct-edit toggle). Default OFF =
# nothing recorded = byte-identical. Flipping ON changes NO cognition behavior.
SIGNALS_RECORD: bool = True

_LEDGER_PATH = OMEGA_ROOT / "guardian" / "logs" / "signals.jsonl"
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class SignalEvent:
    """A structured §7.1 audit record. ``source`` + ``kind`` are required at the call
    site; ``id``/``ts``/``episode_id`` auto-fill; the measured-quantity fields are
    Optional and omitted from the written record when None (never fabricated)."""

    source: str  # producer id, e.g. "omega_result"
    kind: str  # event class, e.g. "outcome"
    id: str = ""  # auto: uuid4 hex
    ts: str = ""  # auto: ISO8601 UTC
    episode_id: str = ""  # auto: current episode if empty
    domain: Optional[str] = None
    valence: Optional[float] = None  # [-1,1] good/bad direction
    confidence: Optional[float] = None  # [0,1]
    salience: Optional[float] = None  # [0,1] magnitude/importance
    uncertainty: Optional[float] = None  # [0,1]
    cost: Optional[float] = None
    evidence_refs: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


def _record_dict(ev: SignalEvent) -> Dict[str, Any]:
    """Serialize, dropping the Optional measured-quantity fields that are None (honest
    signals — a null field means 'not measured here', not 'measured zero'). Empty
    evidence_refs/meta are also dropped to keep the line compact."""
    d = asdict(ev)
    for k in ("domain", "valence", "confidence", "salience", "uncertainty", "cost"):
        if d.get(k) is None:
            d.pop(k, None)
    if not d.get("evidence_refs"):
        d.pop("evidence_refs", None)
    if not d.get("meta"):
        d.pop("meta", None)
    return d


def record(event: SignalEvent) -> None:
    """Append one SignalEvent to the audit ledger — FAILURE-SOFT, RECORD-ONLY.

    Default-OFF gate is the FIRST statement, before any work: flag-OFF does zero I/O
    (no file created), so it is byte-identical. Auto-fills id/ts/episode_id. A write
    failure is swallowed (it rides the fail-hard cognition-close path as failure-soft
    substrate — an audit-write hiccup must never break an episode)."""
    if not SIGNALS_RECORD:
        return
    try:
        from guardian.telemetry.writer import _iso_now, current_episode_id

        ev = event
        fills: Dict[str, Any] = {}
        if not ev.id:
            fills["id"] = uuid.uuid4().hex
        if not ev.ts:
            fills["ts"] = _iso_now()
        if not ev.episode_id:
            fills["episode_id"] = current_episode_id()
        if fills:
            from dataclasses import replace

            ev = replace(ev, **fills)
        line = json.dumps(_record_dict(ev), ensure_ascii=False, default=str) + "\n"
        with _WRITE_LOCK:
            _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_LEDGER_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        try:
            import logging

            logging.getLogger("omega.signal_ledger").debug(
                "signal record failed (non-fatal)", exc_info=True
            )
        except Exception:
            pass
