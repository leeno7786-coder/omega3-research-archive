"""
test_repair7_approve_request_failure_path.py — REPAIR-7

Regression test for guardian/decision.py:791-799 — H1 in the audit.

THE BUG (pre-fix):
    `approve_request(...)` happy path mints a verification_id +
    intervention then constructs a full Decision with all five
    bound-posture fields. The failure path (commit_promotion returns
    False) constructs a Decision missing four required positional
    fields:

        Decision(kind="deny",        # only kind given
                 reason="commit_promotion failed",
                 request_id=..., staging_ref=...,
                 verification_id="", committed=False, cycle_id=...)

    Decision's dataclass requires `kind`, `subtype`, `response_mode`,
    `persistence_policy`, `escalation` — all without defaults. The
    failure path raised TypeError at construction every time
    commit_promotion returned False.

THE FIX:
    Delegate the failure path to the existing
    `_decision_for_commit_failure(...)` helper that the auto-decide
    path uses (decision.py:613, 646). It returns a properly-bound
    REJECT decision (subtype="security_block", response_mode=
    RESPONSE_MODE_SECURITY_BLOCK, persistence_policy="hard_deny",
    escalation=False) and surfaces "commit failed: <reason>" in the
    reason field — same shape the rest of the module already produces
    for this failure mode.

THE TEST:
    Monkey-patches `decision._resolve_staging_ref`, `_read_staging`,
    and `commit_promotion` so we can drive approve_request without a
    live staging surface. Asserts:
      - Pre-fix bug would raise TypeError (sanity-check the bug shape).
      - Post-fix returns a Decision with all five bound-posture fields
        populated correctly.
      - Reason names "commit failed" so operators can grep for the
        failure mode.
      - committed=False; staging_ref + cycle_id round-trip.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Match the rest of the test suite — make stdout safe for non-ASCII on
# the Windows default console (cp1252) so labels with arrows render.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


import guardian.decision as gd
from guardian.decision import (
    approve_request,
    Decision,
    DECISION_REJECT,
    RESPONSE_MODE_SECURITY_BLOCK,
)
import logging

log = logging.getLogger("omega.tests.regression.test_repair7_approve_request_failure_path")


# =====================================================================
section("1. Failure path returns a properly-bound REJECT Decision")
# =====================================================================

# Patch the seams. _resolve_staging_ref normally consults disk; our
# stub returns a synthetic ref so approve_request proceeds to the
# commit branch.
_orig_resolve = gd._resolve_staging_ref
_orig_read = gd._read_staging
_orig_commit = gd.commit_promotion


def _stub_resolve(_):
    return "staging-test-7-001"


def _stub_read(_):
    return {
        "guardian_request_id": "req-test-7-001",
        "cycle_id": "cyc-test-7-001",
    }


def _failing_commit(_staging_ref, _approver_id):
    # Mimic the contract: (committed: bool, canonical_ref: str, rollback: dict)
    return False, "", {}


gd._resolve_staging_ref = _stub_resolve
gd._read_staging = _stub_read
gd.commit_promotion = _failing_commit

try:
    decision = approve_request(
        "req-test-7-001",
        approver_id="operator:test",
        reason="manual operator approve (test)",
    )
    check(
        "approve_request did NOT raise TypeError on commit failure", isinstance(decision, Decision)
    )
    check(
        "decision.kind == REJECT (commit failure → REJECT for safety)",
        decision.kind == DECISION_REJECT,
        f"got kind={decision.kind!r}",
    )
    check(
        "decision.subtype == 'security_block'",
        decision.subtype == "security_block",
        f"got subtype={decision.subtype!r}",
    )
    check(
        "decision.response_mode == RESPONSE_MODE_SECURITY_BLOCK",
        decision.response_mode == RESPONSE_MODE_SECURITY_BLOCK,
        f"got response_mode={decision.response_mode!r}",
    )
    check("decision.persistence_policy == 'hard_deny'", decision.persistence_policy == "hard_deny")
    check("decision.escalation is False", decision.escalation is False)
    check("decision.committed is False (commit didn't happen)", decision.committed is False)
    check(
        "decision.reason names 'commit failed' (greppable failure mode)",
        "commit failed" in (decision.reason or ""),
        f"reason={decision.reason!r}",
    )
    check(
        "decision.request_id round-trips through approve_request",
        decision.request_id == "req-test-7-001",
        f"got request_id={decision.request_id!r}",
    )
    check("decision.staging_ref preserved", decision.staging_ref == "staging-test-7-001")
    check("decision.cycle_id preserved from staging record", decision.cycle_id == "cyc-test-7-001")
    check(
        "decision.verification_id is a non-empty string (synthetic operator id)",
        isinstance(decision.verification_id, str) and len(decision.verification_id) > 0,
        f"vid={decision.verification_id!r}",
    )
finally:
    gd._resolve_staging_ref = _orig_resolve
    gd._read_staging = _orig_read
    gd.commit_promotion = _orig_commit


# =====================================================================
section("2. Happy path still returns a valid ALLOW Decision")
# =====================================================================


def _ok_commit(_staging_ref, _approver_id):
    return True, "canonical-test-7-001", {"rollback": "ok"}


gd._resolve_staging_ref = _stub_resolve
gd._read_staging = _stub_read
gd.commit_promotion = _ok_commit

# Patch _emit_intervention so the happy path doesn't write an audit file
# during the test.
_orig_emit = gd._emit_intervention


def _stub_emit(**_kwargs):
    return "intv-test-7-001"


gd._emit_intervention = _stub_emit

try:
    decision = approve_request(
        "req-test-7-002",
        approver_id="operator:test",
        reason="manual operator approve (test, happy)",
    )
    check("happy path returns a Decision", isinstance(decision, Decision))
    check("happy decision.kind == ALLOW", decision.kind == "ALLOW", f"got kind={decision.kind!r}")
    check("happy decision.committed is True", decision.committed is True)
    check(
        "happy decision.canonical_ref preserved", decision.canonical_ref == "canonical-test-7-001"
    )
    check("happy decision.intervention_id preserved", decision.intervention_id == "intv-test-7-001")
finally:
    gd._resolve_staging_ref = _orig_resolve
    gd._read_staging = _orig_read
    gd.commit_promotion = _orig_commit
    gd._emit_intervention = _orig_emit


# =====================================================================
section("SUMMARY")
# =====================================================================

print(f"  PASS: {PASS}")
print(f"  FAIL: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
