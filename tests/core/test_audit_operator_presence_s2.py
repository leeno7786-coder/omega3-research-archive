"""
test_audit_operator_presence_s2.py — S2 gate for the Operator Presence
Contract (Presence-Gated Identity Loop).

Spec: docs/research/Enrollment is closed-world.txt + operator binding
answer #2 (hysteresis, supersedes the txt). Deterministic — synthetic
Observation sequences + injected `now`; NO webcam, NO live models.

Asserts (each fails pre-S2 -> ModuleNotFoundError):
  - acquire hysteresis: 6 consecutive >=0.82 operator frames -> PRESENT
    (5 not enough); PRESENCE_ENTER fires once on the 6th.
  - closed-world: a non-operator enrolled anchor NEVER latches.
  - release hysteresis: PRESENT then sustained <0.65 for >=20s ->
    ABSENT (within 20s still PRESENT); PRESENCE_EXIT at release.
  - band [0.65,0.82): held PRESENT, no flap, no event.
  - DEGRADED (59b57db): -> UNKNOWN, HOLD last state, NO decay, NO
    ENTER/EXIT; long degraded streak does NOT drop a PRESENT latch;
    recovery to the same stable state emits NO spurious re-ENTER.
  - no enrollment (operator_name None) -> can never be PRESENT.
  - B7 untouched: anchor_store.RECOGNITION_THRESHOLD == 0.4 and
    operator_presence neither references it nor writes the anchor store
    (structural — presence is behavioral policy, not identity truth).
  - presence config consumed from config/sensory.yaml (live, not phantom).

ASCII only. PASS/FAIL + sys.exit.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging

log = logging.getLogger("omega.tests.core.test_audit_operator_presence_s2")

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
    PresenceMachine,
    Observation,
    PRESENT,
    ABSENT,
    UNKNOWN,
)

T0 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
OPR = "Noah"


def mk_obs(name, conf, degraded=False):
    return Observation(recognized_name=name, confidence=conf, degraded=degraded)


# ── acquire hysteresis ──────────────────────────────────────────────
print("\n[acquire] 6 consecutive >=0.82 operator frames -> PRESENT")
m = PresenceMachine()
ev_seen = []
for i in range(5):
    st, ev = m.observe(mk_obs(OPR, 0.9), operator_name=OPR, now=T0 + timedelta(seconds=i))
    ev_seen.append(ev)
check("5 frames -> NOT yet PRESENT (still acquiring)", m.state != PRESENT, detail=m.state)
st, ev = m.observe(mk_obs(OPR, 0.9), operator_name=OPR, now=T0 + timedelta(seconds=5))
check("6th frame -> PRESENT", st == PRESENT, detail=st)
check(
    "PRESENCE_ENTER fired exactly once, on the 6th",
    ev == "PRESENCE_ENTER" and all(e is None for e in ev_seen),
    detail=f"6th={ev} prior={ev_seen}",
)

# ── closed-world: non-operator never latches ────────────────────────
print("\n[closed-world] a non-operator enrolled anchor never latches")
m2 = PresenceMachine()
for i in range(20):
    m2.observe(mk_obs("Guest", 0.99), operator_name=OPR, now=T0 + timedelta(seconds=i))
check("20 high-confidence GUEST frames -> never PRESENT", m2.state != PRESENT, detail=m2.state)
m2b = PresenceMachine()
for i in range(20):
    m2b.observe(mk_obs(OPR, 0.99), operator_name=None, now=T0 + timedelta(seconds=i))
check("no enrollment (operator_name None) -> never PRESENT", m2b.state != PRESENT, detail=m2b.state)

# ── release hysteresis ──────────────────────────────────────────────
print("\n[release] PRESENT then sub-0.65 sustained >=20s -> ABSENT")
m3 = PresenceMachine()
for i in range(6):
    m3.observe(mk_obs(OPR, 0.9), operator_name=OPR, now=T0 + timedelta(seconds=i))
assert m3.state == PRESENT
st, ev = m3.observe(
    mk_obs(OPR, 0.40), operator_name=OPR, now=T0 + timedelta(seconds=30)
)  # release timer starts
check(
    "first sub-release frame -> still PRESENT (timer running)",
    st == PRESENT and ev is None,
    detail=f"{st}/{ev}",
)
st, ev = m3.observe(mk_obs(OPR, 0.40), operator_name=OPR, now=T0 + timedelta(seconds=30 + 19))
check("19s into release window -> still PRESENT (no flap)", st == PRESENT, detail=st)
st, ev = m3.observe(mk_obs(OPR, 0.40), operator_name=OPR, now=T0 + timedelta(seconds=30 + 20))
check(
    "20s sustained sub-release -> ABSENT + PRESENCE_EXIT",
    st == ABSENT and ev == "PRESENCE_EXIT",
    detail=f"{st}/{ev}",
)

# ── hysteresis band [0.65, 0.82): hold, no flap ─────────────────────
print("\n[band] PRESENT, confidence in [0.65,0.82) -> hold, no event")
m4 = PresenceMachine()
for i in range(6):
    m4.observe(mk_obs(OPR, 0.9), operator_name=OPR, now=T0 + timedelta(seconds=i))
flaps = []
for i in range(10):
    st, ev = m4.observe(mk_obs(OPR, 0.70), operator_name=OPR, now=T0 + timedelta(seconds=10 + i))
    flaps.append((st, ev))
check(
    "band frames hold PRESENT with zero transition events",
    all(s == PRESENT and e is None for s, e in flaps),
    detail=str(flaps[:3]),
)

# ── DEGRADED: hold, no decay, no spurious re-ENTER (59b57db) ─────────
print("\n[degraded] runtime-down -> UNKNOWN + HOLD; no decay; resume clean")
m5 = PresenceMachine()
for i in range(6):
    m5.observe(mk_obs(OPR, 0.9), operator_name=OPR, now=T0 + timedelta(seconds=i))
assert m5.state == PRESENT
deg_events = []
for i in range(50):  # long outage — would blow past release_seconds
    st, ev = m5.observe(
        mk_obs(None, 0.0, degraded=True), operator_name=OPR, now=T0 + timedelta(seconds=100 + i)
    )
    deg_events.append((st, ev))
check("degraded -> UNKNOWN", m5.state == UNKNOWN, detail=m5.state)
check(
    "long degraded streak emits NO ENTER/EXIT (hold, not flap)",
    all(e is None for _, e in deg_events),
)
check(
    "degraded does NOT decay the held PRESENT latch (50s outage > release_seconds, still held)",
    all(s == UNKNOWN for s, _ in deg_events),
)
st, ev = m5.observe(mk_obs(OPR, 0.9), operator_name=OPR, now=T0 + timedelta(seconds=200))
check(
    "recovery to the SAME stable state -> resume, NO spurious PRESENCE_ENTER",
    st == PRESENT and ev is None,
    detail=f"{st}/{ev}",
)

print("\n[degraded->genuine-exit] held PRESENT, operator truly gone post-outage")
m6 = PresenceMachine()
for i in range(6):
    m6.observe(mk_obs(OPR, 0.9), operator_name=OPR, now=T0 + timedelta(seconds=i))
for i in range(5):
    m6.observe(
        mk_obs(None, 0.0, degraded=True), operator_name=OPR, now=T0 + timedelta(seconds=50 + i)
    )
# recovery but operator is gone, sustained below release past window
seq = []
for k in range(3):
    st, ev = m6.observe(
        mk_obs(None, 0.0), operator_name=OPR, now=T0 + timedelta(seconds=100 + k * 25)
    )
    seq.append((st, ev))
check(
    "recovery into a genuinely-changed world fires PRESENCE_EXIT "
    "across the sequence (held PRESENT -> operator left during "
    "outage) and settles ABSENT",
    any(e == "PRESENCE_EXIT" for _, e in seq) and seq[-1][0] == ABSENT,
    detail=str(seq),
)

# ── B7 untouched + presence is behavioral, not identity-truth ───────
print("\n[B7] anchor RECOGNITION_THRESHOLD untouched; presence writes nothing")
from runtime.face.anchor_store import RECOGNITION_THRESHOLD  # noqa: E402

check(
    "anchor_store.RECOGNITION_THRESHOLD is still 0.4 (B7 contract)",
    RECOGNITION_THRESHOLD == 0.4,
    detail=str(RECOGNITION_THRESHOLD),
)
src = (ROOT / "sensory" / "operator_presence.py").read_text(encoding="utf-8")
# Precise (not brittle-substring): the invariant is that presence does
# not IMPORT or USE the B7 threshold as code — documenting the boundary
# in prose is correct, not a violation (the BREACH-in-comment lesson).
check(
    "operator_presence does NOT import/use RECOGNITION_THRESHOLD "
    "as code (prose mention of the B7 boundary is allowed)",
    "import RECOGNITION_THRESHOLD" not in src
    and "RECOGNITION_THRESHOLD," not in src
    and "RECOGNITION_THRESHOLD)" not in src
    and "= RECOGNITION_THRESHOLD" not in src
    and ">= RECOGNITION_THRESHOLD" not in src,
    detail="RECOGNITION_THRESHOLD used as a code token",
)
check(
    "operator_presence performs NO anchor-store write "
    "(no .enroll(/.forget(/.add( on the store; reads names() only)",
    not any(
        w in src for w in (".enroll(", ".forget(", ".add_anchor(", "anchor_store.save", "_anchors[")
    ),
)

# ── config consumed (live, not phantom) ─────────────────────────────
print("\n[config] presence block consumed from config/sensory.yaml")
cfg = op.presence_cfg()
check(
    "presence_cfg yields the locked hysteresis (acquire .82 / release .65 / 6 / 20)",
    cfg["acquire_threshold"] == 0.82
    and cfg["release_threshold"] == 0.65
    and cfg["acquire_frames"] == 6
    and cfg["release_seconds"] == 20,
    detail=str(cfg),
)
from config.loader import load_subsystem_bundle  # noqa: E402

_bundle = load_subsystem_bundle("sensory")
_live = ((_bundle.get("config") or {}).get("sensory") or {}).get("presence")
check(
    "sensory.yaml :: sensory.presence block exists (config is real)",
    isinstance(_live, dict),
    detail=str(_live),
)
check(
    "presence_cfg consumes the live sensory bundle (not defaults alone)",
    isinstance(_live, dict) and all(cfg.get(key) == value for key, value in _live.items()),
    detail=f"bundle={_live} cfg={cfg}",
)

# ── B7 bridge: real cosine (~0.55) latches after 6 frames ───────────
print("\n[b7_bridge] 6 operator frames at 0.55 conf -> PRESENT")
m = PresenceMachine()
ev_seen = []
for i in range(5):
    st, ev = m.observe(mk_obs(OPR, 0.55), operator_name=OPR, now=T0 + timedelta(seconds=i))
    ev_seen.append(ev)
check("5 frames at 0.55 -> NOT yet PRESENT", m.state != PRESENT, detail=m.state)
st, ev = m.observe(mk_obs(OPR, 0.55), operator_name=OPR, now=T0 + timedelta(seconds=5))
check("6th frame at 0.55 -> PRESENT", m.state == PRESENT, detail=m.state)
check("6th frame emits PRESENCE_ENTER", ev == "PRESENCE_ENTER", detail=repr(ev))

print("\n[b7_bridge] case-insensitive operator name match")
m2 = PresenceMachine()
for i in range(6):
    m2.observe(mk_obs("noah", 0.6), operator_name="Noah", now=T0 + timedelta(seconds=i))
check("noah vs Noah latches PRESENT", m2.state == PRESENT, detail=m2.state)

print(f"\nTOTAL  PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
