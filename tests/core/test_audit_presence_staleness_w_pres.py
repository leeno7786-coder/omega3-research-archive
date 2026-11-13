"""
test_audit_presence_staleness_w_pres.py — W-PRES gate.

W-PRES is the CONTRACT-SAFE answer to comprehensive-review H5/M6
(watcher dies -> UNKNOWN forever). The audit's fix (stale UNKNOWN ->
ABSENT TTL) was REJECTED: it violates 59b57db (degraded/UNKNOWN must
HOLD without decay; a runtime blip must never flip presence). W-PRES
only emits a Guardian OBSERVABILITY event when the watcher heartbeat
goes stale, mutating NO presence state.

The module exposes the PURE staleness predicate (`presence_is_stale`);
the observability emit is owned by the consumer (hud/server.py), so
this gate covers the predicate + its config + the no-collateral
contract. (The earlier in-module `emit_presence_staleness` wrapper was
orphaned — no production caller used it; the HUD calls the predicate
directly — and was removed.)

Risk-ordered:
  R1 CONTRACT-SAFETY (existential): the W-PRES surface has NO path
     that mutates presence state / reaches ABSENT / touches the
     PresenceMachine. Structural + behavioural.
  R2 staleness predicate correctness (pure; fresh/aged/None/garbage/
     tz-naive).
  R4 config consumed live from config/sensory.yaml (heartbeat_max_age_seconds);
     defaults mirror.
  R5 no-collateral: the 5 original presence cfg keys + the S2
     hold-no-decay machine are untouched (additive only).

Deterministic. ASCII. PASS/FAIL + sys.exit (plain-script canonical).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


import sensory.operator_presence as op  # noqa: E402
from sensory.operator_presence import (  # noqa: E402
    presence_is_stale,
    PresenceMachine,
    Observation,
    PRESENT,
    ABSENT,
    UNKNOWN,
)

T0 = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


# ── R1: CONTRACT-SAFETY (existential) ───────────────────────────────
print("\n[R1] contract-safety: W-PRES mutates NO presence state")
src = (ROOT / "sensory" / "operator_presence.py").read_text(encoding="utf-8")
_seg = src[src.index("def presence_is_stale") : src.index("def run_watcher")]


def _code_only(seg: str) -> str:
    """Strip comments + triple-quoted docstrings so the structural
    check tests CODE, not the safety prose (the S2/S3/S4
    BREACH-in-comment lesson — prose documenting the boundary is
    allowed; only code tokens are a violation)."""
    out, i, n = [], 0, len(seg)
    in_doc = False
    for ln in seg.splitlines():
        s = ln.strip()
        if '"""' in ln:
            # toggle on each triple-quote (handles single-line docstrings)
            in_doc = not in_doc if ln.count('"""') % 2 else in_doc
            continue
        if in_doc:
            continue
        if s.startswith("#"):
            continue
        out.append(ln.split("  #")[0])
    return "\n".join(out)


_code = _code_only(_seg)
check(
    "W-PRES CODE contains NO 'ABSENT' token (cannot fabricate "
    "absence; prose documenting the boundary is allowed)",
    "ABSENT" not in _code,
    detail="ABSENT used as code",
)
check(
    "W-PRES CODE does NOT reference PresenceMachine / .observe / "
    ".state (no state-machine reach in code)",
    "PresenceMachine" not in _code and ".observe(" not in _code and ".state" not in _code,
    detail="state-machine token in code",
)
check(
    "W-PRES surface performs NO state/shared-state write "
    "(no shared_state, no write(, no = ABSENT/UNKNOWN/PRESENT)",
    "shared_state" not in _seg
    and ".write(" not in _seg
    and "= ABSENT" not in _seg
    and "= UNKNOWN" not in _seg
    and "= PRESENT" not in _seg,
)
# behavioural: a stale verdict is a bool observability fact, never a state
check(
    "a stale verdict is a bool observability fact, NOT a state "
    "(presence_is_stale returns bool, never ABSENT/UNKNOWN)",
    isinstance(presence_is_stale(None, now=T0), bool),
)

# ── R2: staleness predicate correctness (pure) ──────────────────────
print("\n[R2] staleness predicate (fresh / aged / none / garbage / naive)")
fresh = (T0 - timedelta(seconds=5)).isoformat()
old = (T0 - timedelta(seconds=120)).isoformat()
check(
    "fresh heartbeat (5s < 90) -> NOT stale",
    presence_is_stale(fresh, now=T0, max_age_seconds=90) is False,
)
check(
    "aged heartbeat (120s > 90) -> stale",
    presence_is_stale(old, now=T0, max_age_seconds=90) is True,
)
check(
    "None heartbeat (watcher never started/dead) -> stale",
    presence_is_stale(None, now=T0, max_age_seconds=90) is True,
)
check(
    "unparseable heartbeat -> stale (loud, never silent)",
    presence_is_stale("not-a-timestamp", now=T0, max_age_seconds=90) is True,
)
_naive = datetime(2026, 5, 19, 11, 58, 0).isoformat()  # no tzinfo, 120s old
check(
    "tz-naive heartbeat handled (assumed UTC), 120s -> stale",
    presence_is_stale(_naive, now=T0, max_age_seconds=90) is True,
)
check(
    "exactly at threshold is NOT stale (strict >)",
    presence_is_stale((T0 - timedelta(seconds=90)).isoformat(), now=T0, max_age_seconds=90)
    is False,
)

# ── R4: config consumed live via load_subsystem_bundle('sensory') ───
print("\n[R4] heartbeat_max_age_seconds consumed from config/sensory.yaml")
cfg = op.presence_cfg()
check(
    "presence_cfg exposes heartbeat_max_age_seconds",
    "heartbeat_max_age_seconds" in cfg and float(cfg["heartbeat_max_age_seconds"]) > 0,
    detail=str(cfg.get("heartbeat_max_age_seconds")),
)
from config.loader import load_subsystem_bundle  # noqa: E402
import logging

log = logging.getLogger("omega.tests.core.test_audit_presence_staleness_w_pres")

_bundle = load_subsystem_bundle("sensory")
_live = (((_bundle.get("config") or {}).get("sensory") or {}).get("presence") or {}).get(
    "heartbeat_max_age_seconds"
)
check(
    "sensory.yaml :: sensory.presence.heartbeat_max_age_seconds is real "
    "and is what presence_cfg returns (live, not phantom)",
    _live is not None and float(cfg["heartbeat_max_age_seconds"]) == float(_live),
    detail=f"bundle={_live} cfg={cfg.get('heartbeat_max_age_seconds')}",
)
check(
    "default mirrors (op._DEFAULTS carries the key for graceful degradation)",
    "heartbeat_max_age_seconds" in op._DEFAULTS,
)

# ── R5: no-collateral (additive only) ───────────────────────────────
print("\n[R5] additive only — S2 hysteresis keys + hold-no-decay intact")
for k in (
    "acquire_threshold",
    "release_threshold",
    "acquire_frames",
    "release_seconds",
    "degraded_holds_last_state",
):
    check(f"original presence cfg key preserved: {k}", k in cfg)
# the S2 59b57db invariant itself, re-proven locally (degraded HOLDS,
# no decay) — W-PRES must not have perturbed PresenceMachine
m2 = PresenceMachine()
for i in range(6):
    m2.observe(Observation("Noah", 0.9, False), operator_name="Noah", now=T0 + timedelta(seconds=i))
assert m2.state == PRESENT
deg = [
    m2.observe(
        Observation(None, 0.0, True), operator_name="Noah", now=T0 + timedelta(seconds=100 + i)
    )
    for i in range(40)
]
check(
    "S2 contract intact post-W-PRES: long degraded streak -> UNKNOWN, "
    "HOLD, no decay, no ENTER/EXIT (59b57db unperturbed)",
    m2.state == UNKNOWN and all(e is None for _, e in deg),
)

print(f"\nTOTAL  PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
