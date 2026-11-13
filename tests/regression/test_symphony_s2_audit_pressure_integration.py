"""
test_symphony_s2_audit_pressure_integration.py — v0.2 §S2 audit

Proves the SWING §S2 acceptance lines:

  1. Curiosity, operator directive, neuromod spike, outstanding
     question, AND GUARDIAN each produce bounded pressure.
  2. Pressure output is trace data: it opens/holds, but never names
     Meta (or any movement) as default.

Section coverage (per operator spec — BINARY semantics; one
Guardian intervention is an authority block, not an ambient mood):
  §1  Guardian wire — fresh adversarial (reject/flag/etc) → 1.0
      (matches legacy `_DEFAULT_GUARDIAN_INTERVENTION_THRESHOLD = 1`
      "any new intervention fires" contract)
  §2  Guardian wire — fresh approve / auto-allow only → 0.0;
      any mixed stream with ≥1 fresh adversarial → 1.0
  §3  Guardian wire — stale adversarial outside window → 0.0;
      stale + ≥1 fresh adversarial → 1.0
  §4  Guardian wire — malformed / no timestamp / missing ledger → 0.0
  §5  PressureDecision structural invariant — includes guardian
      contributor in registry shape; carries NO first_movement,
      route, selected_movement, first_signal, or ignition field
      (those live on episode.ignition per cue sheet §S9).

Run:
  python tests/regression/test_symphony_s2_audit_pressure_integration.py
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pressure_loop import pressure_field as pf  # noqa: E402
from pressure_loop import cycle_cadence as cc  # noqa: E402
from core.shared_state import SharedState  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


# ---------------------------------------------------------------------
# Fixture: temp OMEGA_ROOT so read_guardian() reads a controlled JSONL
# ---------------------------------------------------------------------


def _make_intervention(
    *,
    kind: str,
    ts: str | None,
    intervention_id: str = "interv-test",
) -> dict:
    """Build a minimal Guardian intervention JSON record. `ts=None`
    yields a record with NO timestamp field, exercising the
    helper's 'malformed → excluded' path."""
    rec = {
        "intervention_id": intervention_id,
        "guardian_id": "guardian.decision",
        "target_id": "target-test",
        "target_kind": "memory_promotion",
        "kind": kind,
        "reason": "S2 audit fixture",
        "verification_id": "verify-test",
    }
    if ts is not None:
        rec["timestamp"] = ts
    return rec


def _write_ledger(tmp_root: Path, entries: list[dict]) -> Path:
    """Write entries as JSONL under <tmp_root>/guardian/logs/
    guardian_interventions.jsonl (the canonical helper path)."""
    log_dir = tmp_root / "guardian" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "guardian_interventions.jsonl"
    with log_file.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_file


def _with_tmp_omega_root(entries: list[dict]):
    """Context: monkey-patch cycle_cadence.OMEGA_ROOT to a tmp dir
    seeded with the supplied ledger entries. Returns (cleanup_fn,
    fresh_shared_state) so the caller drives read_guardian against
    the fixture."""
    tmp_dir = tempfile.TemporaryDirectory()
    tmp_root = Path(tmp_dir.name)
    _write_ledger(tmp_root, entries)
    original_root = cc.OMEGA_ROOT
    cc.OMEGA_ROOT = tmp_root

    def cleanup() -> None:
        cc.OMEGA_ROOT = original_root
        tmp_dir.cleanup()

    return cleanup, SharedState()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# Time anchors. `_PRESSURE_WINDOW_SECONDS` is 600s in cycle_cadence;
# we drive fixture timestamps relative to "now" to guarantee stable
# in-window / out-of-window classification regardless of test run
# wall-clock.
NOW = datetime.now(timezone.utc)
FRESH = _iso(NOW - timedelta(seconds=30))  # well inside window
STALE = _iso(NOW - timedelta(seconds=cc._PRESSURE_WINDOW_SECONDS + 60))


# =====================================================================
section("1. Guardian wire — one fresh adversarial → 1.0 (authority block)")
# =====================================================================

# Single fresh adversarial → 1.0 (binary semantics: one block opens
# attention; matches legacy `_DEFAULT_GUARDIAN_INTERVENTION_THRESHOLD = 1`
# "any new intervention fires" contract).
cleanup, ss = _with_tmp_omega_root(
    [
        _make_intervention(kind="reject", ts=FRESH, intervention_id="i-1"),
    ]
)
try:
    v1 = pf.read_guardian(ss)
    check(
        "one fresh 'reject' → 1.0 (authority block, not soft mood)",
        v1 == 1.0,
        f"got {v1!r}",
    )
finally:
    cleanup()

# Each adversarial kind individually counted at full strength.
for kind in ("reject", "quarantine", "flag", "block", "deny"):
    cleanup, ss = _with_tmp_omega_root(
        [
            _make_intervention(kind=kind, ts=FRESH, intervention_id=f"i-{kind}"),
        ]
    )
    try:
        v = pf.read_guardian(ss)
        check(
            f"one fresh {kind!r} → 1.0 (each adversarial kind fires fully)",
            v == 1.0,
            f"got {v!r}",
        )
    finally:
        cleanup()

# Multiple adversarials still cap at 1.0 (binary contract: more
# interventions don't add headroom — the contributor is saturated by
# the first one, downstream routing distinguishes severity).
cleanup, ss = _with_tmp_omega_root(
    [_make_intervention(kind="deny", ts=FRESH, intervention_id=f"i-many-{i}") for i in range(10)]
)
try:
    v_many = pf.read_guardian(ss)
    check(
        "10 fresh adversarials → still 1.0 (binary semantics; no overflow)",
        v_many == 1.0,
        f"got {v_many!r}",
    )
finally:
    cleanup()


# =====================================================================
section("2. Guardian wire — approve-only → 0.0; mix with ≥1 deny → 1.0")
# =====================================================================

# Pure approve stream — the default-allow doctrine's normal case.
cleanup, ss = _with_tmp_omega_root(
    [_make_intervention(kind="approve", ts=FRESH, intervention_id=f"i-ok-{i}") for i in range(10)]
)
try:
    v_approve = pf.read_guardian(ss)
    check(
        "10 fresh 'approve' → 0.0 (phantom-pressure doctrine: benign "
        "auto-allow is NOT intervention)",
        v_approve == 0.0,
        f"got {v_approve!r}",
    )
finally:
    cleanup()

# Mix: 10 approves + 1 deny → 1.0 (one authority block among many
# benign auto-allows still opens attention).
cleanup, ss = _with_tmp_omega_root(
    [_make_intervention(kind="approve", ts=FRESH, intervention_id=f"i-ok-{i}") for i in range(10)]
    + [_make_intervention(kind="deny", ts=FRESH, intervention_id="i-deny")]
)
try:
    v_mix = pf.read_guardian(ss)
    check(
        "10 approves + 1 deny → 1.0 (the deny is the authority block)",
        v_mix == 1.0,
        f"got {v_mix!r}",
    )
finally:
    cleanup()

# Other benign / non-adversarial kinds from the InterventionKind enum
# that the filter must also NOT count:
for kind in ("release", "retract", "rollback"):
    cleanup, ss = _with_tmp_omega_root(
        [
            _make_intervention(kind=kind, ts=FRESH, intervention_id=f"i-{kind}"),
        ]
    )
    try:
        v = pf.read_guardian(ss)
        check(
            f"one fresh {kind!r} → 0.0 (not in adversarial set)",
            v == 0.0,
            f"got {v!r}",
        )
    finally:
        cleanup()


# =====================================================================
section("3. Guardian wire — stale-only → 0.0; stale + 1 fresh → 1.0")
# =====================================================================

# Pure stale stream — every entry is older than the 600s window.
cleanup, ss = _with_tmp_omega_root(
    [_make_intervention(kind="reject", ts=STALE, intervention_id=f"i-old-{i}") for i in range(5)]
)
try:
    v_stale = pf.read_guardian(ss)
    check(
        f"5 stale 'reject' (>{int(cc._PRESSURE_WINDOW_SECONDS)}s old) "
        f"→ 0.0 (helper time-window excludes)",
        v_stale == 0.0,
        f"got {v_stale!r}",
    )
finally:
    cleanup()

# Mix: stale adversarials + 1 fresh adversarial → 1.0 (the fresh one
# is sufficient on its own).
cleanup, ss = _with_tmp_omega_root(
    [_make_intervention(kind="flag", ts=STALE, intervention_id=f"i-old-{i}") for i in range(5)]
    + [
        _make_intervention(kind="flag", ts=FRESH, intervention_id="i-new"),
    ]
)
try:
    v_mixed = pf.read_guardian(ss)
    check(
        "5 stale + 1 fresh adversarial → 1.0 (fresh one fires fully)",
        v_mixed == 1.0,
        f"got {v_mixed!r}",
    )
finally:
    cleanup()


# =====================================================================
section("4. Guardian wire — malformed / no timestamp → 0.0")
# =====================================================================

# No timestamp field at all (helper must exclude per its docstring).
cleanup, ss = _with_tmp_omega_root(
    [_make_intervention(kind="reject", ts=None, intervention_id=f"i-mal-{i}") for i in range(5)]
)
try:
    v_no_ts = pf.read_guardian(ss)
    check(
        "5 adversarials with NO timestamp field → 0.0 "
        "(stale-log-must-not-keep-pressure-hot contract)",
        v_no_ts == 0.0,
        f"got {v_no_ts!r}",
    )
finally:
    cleanup()

# Unparseable timestamp string.
cleanup, ss = _with_tmp_omega_root(
    [
        _make_intervention(kind="deny", ts="not-an-iso-timestamp", intervention_id="i-bad-ts"),
    ]
)
try:
    v_bad_ts = pf.read_guardian(ss)
    check(
        "adversarial with unparseable timestamp → 0.0",
        v_bad_ts == 0.0,
        f"got {v_bad_ts!r}",
    )
finally:
    cleanup()

# Missing ledger file entirely → 0.0 (helper short-circuits on
# path.exists() == False; we exercise that by pointing OMEGA_ROOT at
# a tmp dir we DON'T seed).
tmp = tempfile.TemporaryDirectory()
try:
    original_root = cc.OMEGA_ROOT
    cc.OMEGA_ROOT = Path(tmp.name)
    try:
        v_missing = pf.read_guardian(SharedState())
        check(
            "missing guardian_interventions.jsonl → 0.0",
            v_missing == 0.0,
            f"got {v_missing!r}",
        )
    finally:
        cc.OMEGA_ROOT = original_root
finally:
    tmp.cleanup()


# =====================================================================
section("5. PressureDecision structural invariant — no first_movement")
# =====================================================================

# (a) The dataclass itself.
_FORBIDDEN_ROUTE_FIELDS = {
    "route",
    "first_movement",
    "selected_movement",
    "first_signal",
    "ignition",
}
decision_fields = {f.name for f in dataclasses.fields(pf.PressureDecision)}
leaked = decision_fields & _FORBIDDEN_ROUTE_FIELDS
check(
    "PressureDecision dataclass has NO route/first_movement-style field "
    "(those live on episode.ignition per cue sheet §S9)",
    not leaked,
    f"leaked fields: {sorted(leaked)}" if leaked else "",
)

# (b) The snapshot the decision emits to episode.pressure (the shape
# downstream consumers see).
dec_invariant = pf.evaluate_pressure(SharedState(), require_presence=False)
snap = dec_invariant.to_snapshot()
top_leak = set(snap.keys()) & _FORBIDDEN_ROUTE_FIELDS
check(
    "to_snapshot() top-level has no route/first_movement key",
    not top_leak,
    f"leaked: {sorted(top_leak)}" if top_leak else "",
)
contributor_leak: set[str] = set()
for c in snap.get("contributors", []):
    contributor_leak |= set(c.keys()) & _FORBIDDEN_ROUTE_FIELDS
check(
    "to_snapshot() per-contributor entries have no route/first_movement key",
    not contributor_leak,
    f"leaked: {sorted(contributor_leak)}" if contributor_leak else "",
)

# (c) Guardian reader exists and can be attached to the registry
# (default _DEFAULT_READERS omits guardian until the HUD/CueDesk wire
# path attaches it — see build_registry extras).
check(
    "read_guardian callable + CONTRIBUTOR_GUARDIAN id stable",
    pf.CONTRIBUTOR_GUARDIAN == "guardian" and callable(pf.read_guardian),
)
dec_with_guardian = pf.evaluate_pressure(
    SharedState(),
    require_presence=False,
    registry=pf.build_registry(
        extras={pf.CONTRIBUTOR_GUARDIAN: pf.read_guardian},
    ),
)
snap_guardian = dec_with_guardian.to_snapshot()
check(
    "evaluate_pressure() carries 'guardian' when registry includes reader",
    pf.CONTRIBUTOR_GUARDIAN in dec_with_guardian.contributors,
)
check(
    "to_snapshot() lists 'guardian' contributor by id",
    any(c.get("id") == pf.CONTRIBUTOR_GUARDIAN for c in snap_guardian.get("contributors", [])),
)

# (d) The decision's emits surface (signals the caller publishes) is
# strictly PRESSURE_OPEN-or-empty — it must never name a route signal.
_FORBIDDEN_EMIT_SIGNALS = {
    "IGNITE_META_EXECUTIVE",
    "IGNITE_GLOBAL_PRELUDE",
    "IGNITE_OMEGA_PRELUDE",
    "IGNITE_RESPONSE_HOLD",
    "META_PRE_AUTO",
}
emit_leak = set(dec_invariant.emits) & _FORBIDDEN_EMIT_SIGNALS
check(
    "PressureDecision.emits never carries an IGNITE_* or movement signal",
    not emit_leak,
    f"leaked: {sorted(emit_leak)}" if emit_leak else "",
)


# =====================================================================
section("SUMMARY")
print()
print(f"  PASS={PASS}  FAIL={FAIL}")
if FAIL:
    sys.exit(1)
