"""
test_presence_test_surface.py — gate for hud/presence_test.py, the S2
operator-presence LIVE TEST surface.

Proves the harness is correct GLUE over the real S2 machine, with NO
live models and NO reimplemented hysteresis:

  - GET / serves the page; /api/presence/config exposes the live
    locked hysteresis (acquire .82 / release .65 / 6 / 20) — proving
    presence_cfg() is consumed from omega.yaml (convergent with S2).
  - tick path = loader.identify -> Observation.from_identify ->
    PresenceMachine.observe: 6 operator frames -> PRESENT +
    PRESENCE_ENTER (5 not enough); raw_identify echoed.
  - closed-world: a non-operator high-confidence face NEVER latches;
    no enrollment -> never PRESENT.
  - hysteresis band [.65,.82): held PRESENT, no flap, no event.
  - release: sustained sub-.65 past release_seconds -> ABSENT +
    PRESENCE_EXIT (clock injected via _utcnow seam).
  - degraded (runtime down): -> UNKNOWN, HOLD, no decay, no
    ENTER/EXIT; recovery to the same stable state -> no spurious
    re-ENTER (the 59b57db contract, end to end).
  - ENROLL derives the embedding from identify and enters the durable
    cognition queue with a correlated cycle. It does not expose a learned
    identity before DeepSleep commits its memory. No-face / degraded
    enrollment fails LOUD (no silent pass).
  - STRUCTURAL anti-spaghetti: reuses sensory.operator_presence
    (PresenceMachine/Observation/enrolled_operator_name) and the real
    anchor_store; reimplements NEITHER the machine NOR the hysteresis.

Deterministic. ASCII only. PASS/FAIL + sys.exit (plain-script
canonical, mirrors test_audit_operator_presence_s2.py).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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


from fastapi.testclient import TestClient  # noqa: E402

import hud.presence_test as pt  # noqa: E402
import logging

log = logging.getLogger("omega.tests.core.test_presence_test_surface")


OPR = "Noah"
T0 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
_CLOCK: List[datetime] = [T0]
_EMB512 = [0.01] * 512


class _FakeFaceRecognition:
    """Scripted identify/enroll for presence_test — no network."""

    def __init__(self) -> None:
        self.script: Dict[str, Any] = {
            "recognized_name": None,
            "confidence": 0.0,
            "degraded": False,
            "embedding": list(_EMB512),
        }
        self.enrolled: List[tuple] = []

    def set(
        self,
        name: Optional[str],
        conf: float,
        degraded: bool = False,
        embedding: Optional[list] = "keep",
    ) -> None:
        s: Dict[str, Any] = {
            "recognized_name": name,
            "confidence": conf,
            "degraded": degraded,
        }
        if embedding == "keep":
            s["embedding"] = list(_EMB512)
        elif embedding is not None:
            s["embedding"] = embedding
        self.script = s

    def identify(self, raw: bytes) -> Dict[str, Any]:
        return dict(self.script)

    def enroll(self, raw: bytes, name: str) -> Dict[str, Any]:
        self.enrolled.append((len(raw or b""), name))
        return {"ok": True, "name": name}


class FakeAnchorStore:
    """Stand-in for the brain-side anchor_store — records enroll,
    exposes names() exactly like the real one (sorted keys)."""

    def __init__(self) -> None:
        self._a: Dict[str, Any] = {}

    def seed(self, name: str) -> None:
        self._a[name] = {"embedding": list(_EMB512), "sample_count": 1}

    def clear(self) -> None:
        self._a.clear()

    def names(self) -> List[str]:
        return sorted(self._a.keys())

    def enroll(self, name: str, embedding: Any) -> Dict[str, Any]:
        if not (isinstance(embedding, list) and len(embedding) == 512):
            raise ValueError("embedding must be 512-dim")
        # Mirror the real anchor_store: repeated enroll of the same name
        # grows sample_count (running mean), capped at 5.
        prev = self._a.get(name, {}).get("sample_count", 0)
        sc = min(prev + 1, 5)
        self._a[name] = {"embedding": list(embedding), "sample_count": sc}
        return {"name": name, "sample_count": sc, "anchored_at": "t"}


_FAKE = _FakeFaceRecognition()
_ANCHOR = FakeAnchorStore()
_EVENTS: List[str] = []

# Inject the seams (module-global lookups -> attribute patch is enough).
pt.identify = _FAKE.identify
_QUEUED = []


def _queue_face_memory(**payload):
    _QUEUED.append(payload)
    return {"ok": True, "queued": True, "cycle_id": f"face-cycle-{len(_QUEUED)}"}


pt.queue_face_memory = _queue_face_memory
pt.get_anchor_store = lambda: _ANCHOR
pt._utcnow = lambda: _CLOCK[0]
pt._emit_presence_event = lambda ev, **kw: _EVENTS.append(ev)
# enrolled_operator_name mirrors the REAL resolution shape: names()[0]
# (closed-world: first/only enrolled anchor) — driven by the fake store
# so enrollment genuinely flips it.
pt.enrolled_operator_name = lambda: _ANCHOR.names()[0] if _ANCHOR.names() else None

client = TestClient(pt.app)

IMG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def tick() -> Dict[str, Any]:
    r = client.post("/api/presence/tick", json={"image_b64": IMG})
    assert r.status_code == 200, r.text
    return r.json()


def reset() -> None:
    assert client.post("/api/presence/reset").status_code == 200
    _CLOCK[0] = T0


# ── page + config (presence_cfg consumed live) ──────────────────────
print("\n[surface] page + config")
check("GET / serves the test page", client.get("/").status_code == 200)
cfg = client.get("/api/presence/config").json()
check(
    "config exposes the locked hysteresis from omega.yaml (acquire .82 / release .65 / 6 / 20)",
    cfg["thresholds"]
    == {"acquire": 0.82, "release": 0.65, "acquire_frames": 6, "release_seconds": 20.0},
    detail=str(cfg.get("thresholds")),
)

# ── acquire: 6 operator frames -> PRESENT + ENTER ───────────────────
print("\n[acquire] 6 operator frames -> PRESENT (5 not enough)")
reset()
_ANCHOR.clear()
_ANCHOR.seed(OPR)
_FAKE.set(OPR, 0.9)
states = [tick() for _ in range(5)]
check(
    "5 frames -> not yet PRESENT",
    all(s["state"] != "PRESENT" for s in states),
    detail=str([s["state"] for s in states]),
)
s6 = tick()
check("6th frame -> PRESENT", s6["state"] == "PRESENT", detail=s6["state"])
check("PRESENCE_ENTER fired on the 6th", s6["event"] == "PRESENCE_ENTER", detail=str(s6["event"]))
check(
    "raw_identify echoed (verify-don't-trust diagnostic present)",
    s6.get("raw_identify", {}).get("recognized_name") == OPR,
    detail=str(s6.get("raw_identify", {}).get("recognized_name")),
)
check(
    "acquire progress surfaced (count/frames)",
    s6["acquire_count"] >= 6 and s6["acquire_frames"] == 6,
    detail=f"{s6['acquire_count']}/{s6['acquire_frames']}",
)

# ── hysteresis band: hold, no flap ──────────────────────────────────
print("\n[band] conf in [.65,.82) holds PRESENT, no event")
band = [tick() for _ in range(4) if (_FAKE.set(OPR, 0.70) or True)]
check(
    "band frames hold PRESENT with no transition event",
    all(b["state"] == "PRESENT" and b["event"] is None for b in band),
    detail=str([(b["state"], b["event"]) for b in band]),
)

# ── release: sustained sub-.65 past window -> ABSENT + EXIT ──────────
print("\n[release] sustained sub-.65 past release_seconds -> ABSENT+EXIT")
_FAKE.set(OPR, 0.40)
r1 = tick()
check(
    "first sub-release frame -> still PRESENT (timer running)",
    r1["state"] == "PRESENT" and r1["event"] is None,
    detail=f"{r1['state']}/{r1['event']}",
)
_CLOCK[0] = T0 + timedelta(seconds=19)
r2 = tick()
check(
    "19s into release window -> still PRESENT (no flap)",
    r2["state"] == "PRESENT",
    detail=r2["state"],
)
_CLOCK[0] = T0 + timedelta(seconds=20)
r3 = tick()
check(
    "20s sustained sub-release -> ABSENT + PRESENCE_EXIT",
    r3["state"] == "ABSENT" and r3["event"] == "PRESENCE_EXIT",
    detail=f"{r3['state']}/{r3['event']}",
)

# ── closed-world ────────────────────────────────────────────────────
print("\n[closed-world] non-operator + no-enrollment never latch")
reset()
_ANCHOR.clear()
_ANCHOR.seed(OPR)
_FAKE.set("Guest", 0.99)
guest = [tick() for _ in range(20)]
check(
    "20 high-confidence GUEST frames -> never PRESENT", all(g["state"] != "PRESENT" for g in guest)
)
reset()
_ANCHOR.clear()  # nobody enrolled
_FAKE.set(OPR, 0.99)
noenr = [tick() for _ in range(20)]
check("no enrollment (anchor empty) -> never PRESENT", all(n["state"] != "PRESENT" for n in noenr))

# ── degraded: UNKNOWN + HOLD + clean resume (59b57db) ───────────────
print("\n[degraded] runtime-down -> UNKNOWN+HOLD; no decay; clean resume")
reset()
_ANCHOR.clear()
_ANCHOR.seed(OPR)
_FAKE.set(OPR, 0.9)
for _ in range(6):
    tick()
assert pt._MACHINE.state == "PRESENT"
_FAKE.set(None, 0.0, degraded=True, embedding=None)
deg = []
for i in range(40):
    _CLOCK[0] = T0 + timedelta(seconds=100 + i)
    deg.append(tick())
check("degraded -> UNKNOWN", all(d["state"] == "UNKNOWN" for d in deg), detail=str(deg[0]["state"]))
check("degraded streak emits NO ENTER/EXIT (hold, not flap)", all(d["event"] is None for d in deg))
_FAKE.set(OPR, 0.9)
_CLOCK[0] = T0 + timedelta(seconds=300)
rec = tick()
check(
    "recovery to the SAME stable state -> resume, NO spurious ENTER",
    rec["state"] == "PRESENT" and rec["event"] is None,
    detail=f"{rec['state']}/{rec['event']}",
)
check(
    "Guardian emitter invoked on real transitions only",
    "PRESENCE_ENTER" in _EVENTS and "PRESENCE_EXIT" in _EVENTS,
)

# ── ENROLL: robust per-sample, degraded-tolerant, case-canonical ───
print("\n[enroll] per-sample · degraded-tolerant · multi-sample · canonical")
_ANCHOR.clear()
_QUEUED.clear()
check(
    "before enroll: enrolled_operator is None (anchor empty)", pt.enrolled_operator_name() is None
)

# A degraded frame is a SKIP, not a hard failure (the live-test bug).
_FAKE.set(None, 0.0, degraded=True, embedding=None)
dgr = client.post("/api/presence/enroll", json={"image_b64": IMG, "name": "noah"}).json()
check(
    "degraded sample -> good:false, NOT persisted, NOT 500, enrolled_operator still reported",
    dgr["good"] is False
    and dgr["ok"] is False
    and _ANCHOR.names() == []
    and "enrolled_operator" in dgr,
    detail=str(dgr),
)
# A no-face frame is likewise a skip, not fatal.
_FAKE.set(None, 0.0, embedding=None)
nf = client.post("/api/presence/enroll", json={"image_b64": IMG, "name": "noah"}).json()
check(
    "no-face sample -> good:false skip, not persisted",
    nf["good"] is False and _ANCHOR.names() == [],
    detail=str(nf),
)

# Good frames accumulate -> sample_count grows (running mean), and
# enrolled_operator flips. Typed 'noah' (lowercase).
_FAKE.set(None, 0.0)  # face present (512-d emb), not yet recognized
samples_seen = []
for _ in range(6):
    g = client.post("/api/presence/enroll", json={"image_b64": IMG, "name": "noah"}).json()
    samples_seen.append(g)
check(
    "good sample -> good:true, ok:true, queued:true with a cycle",
    samples_seen and all(s.get("queued") and s.get("good") and s.get("cycle_id") for s in samples_seen),
    detail=str(samples_seen),
)
check(
    "repeated samples keep distinct durable work identities",
    len({s["cycle_id"] for s in samples_seen}) == 6,
    detail=str(samples_seen),
)
check(
    "enrollment does not update the cache before DeepSleep",
    _ANCHOR.names() == [],
    detail=str(_ANCHOR.names()),
)
check(
    "enrollment carries complete sample through the queue",
    _QUEUED and _QUEUED[-1]["name"] == "noah" and len(_QUEUED[-1]["embedding"]) == 512,
)
check(
    "queued enrollment does not claim a learned operator",
    pt.enrolled_operator_name() is None,
)

# Case-canonical: with 'Noah' already stored, enrolling 'noah'
# REUSES the stored casing — no second anchor, gate stays consistent.
_ANCHOR.clear()
_ANCHOR.seed("Noah")
_FAKE.set(None, 0.0)
cc = client.post("/api/presence/enroll", json={"image_b64": IMG, "name": "noah"}).json()
check(
    "typed 'noah' with stored 'Noah' -> reuses 'Noah', no dup anchor",
    _ANCHOR.names() == ["Noah"]
    and cc["name"] == "Noah"
    and cc["ok"] is True
    and cc["enrolled_operator"] == "Noah",
    detail=f"{_ANCHOR.names()} {cc}",
)

bad = client.post("/api/presence/enroll", json={"image_b64": IMG})
check("enroll without name -> 400 (fail loud)", bad.status_code == 400, detail=str(bad.status_code))

# ── STRUCTURAL anti-spaghetti: convergent with S2, not parallel ─────
print("\n[structural] reuses S2; reimplements neither machine nor hysteresis")
src = (ROOT / "hud" / "presence_test.py").read_text(encoding="utf-8")
check(
    "imports the real S2 objects from sensory.operator_presence",
    "from sensory.operator_presence import" in src
    and "PresenceMachine" in src
    and "Observation" in src
    and "enrolled_operator_name" in src,
)
check(
    "reuses the real brain-side anchor_store (no parallel store)",
    "from runtime.face.anchor_store import get_anchor_store" in src,
)
check(
    "does NOT reimplement the machine (no own `class PresenceMachine` / `def observe(`)",
    "class PresenceMachine" not in src and "def observe(" not in src,
)
check(
    "does NOT define its own hysteresis thresholds",
    "acquire_threshold =" not in src
    and "release_threshold =" not in src
    and "release_seconds =" not in src,
)

print(f"\nTOTAL  PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
