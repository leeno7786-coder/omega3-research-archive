"""
test_audit_guardian_criticals.py - regression gates for the two
guardian CRITICALs the 2026-05-18 fine-grained audit found + fixed.
Guardian is the security boundary; each gate is designed to FAIL on
the pre-fix code and PASS on the fix.

  G1  decide_request's QUARANTINE branch did NOT check commit failure
      (ALLOW/FLAG do). commit_quarantine -> (False,None,None) on a
      disk/fsync failure still produced a success-shaped
      Decision(kind=QUARANTINE) + a "quarantine" intervention audit
      row CLAIMING isolation occurred, staging never rolled back ->
      adversarial / identity-injection data that FAILED to isolate
      was reported as isolated (H1 bug class on the attack path).
      Fix: mirror ALLOW/FLAG -> `if not committed:
      return _decision_for_commit_failure(...)` BEFORE the quarantine
      intervention/Decision.

  G2  _decision_state_from_findings keyed on category substrings
      (adversarial/identity_protection/bias/...), but guard_envelope
      (promotion.py) propagated only the reason TAG
      (prompt_injection_ignore, persona_injection, ...) which contains
      no category substring -> every blocklist rejection fell through
      to the default REJECT. The documented FLAG/QUARANTINE
      stratification was DEAD on the staging path (safe-by-accident).
      Fix: promotion.py propagates the authoritative `category`
      write_guard already computes; the matcher consumes it.

ASCII only (Windows cp1252 stdout). PASS/FAIL counter + sys.exit.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging

log = logging.getLogger("omega.tests.guardian.test_audit_guardian_criticals")

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


import guardian.decision as gd  # noqa: E402
from guardian.decision import (  # noqa: E402
    _decision_state_from_findings,
    _decision_for_commit_failure,
    DECISION_ALLOW,
    DECISION_FLAG,
    DECISION_QUARANTINE,
    DECISION_REJECT,
)

# ====================================================================
# G1 - QUARANTINE commit-failure is no longer reported as isolation
# ====================================================================
print("\n[G1] QUARANTINE branch guards commit failure (no false isolation)")

src = inspect.getsource(gd.decide_request)
# Locate the QUARANTINE branch body.
q = src.index("if kind == DECISION_QUARANTINE:")
# REJECT branch (or next top-level branch) ends the QUARANTINE block.
rest = src[q:]
m = re.search(r"\n    # REJECT|\n    if kind == DECISION_REJECT|\Z", rest)
q_block = rest[: m.start()] if m else rest

guard_pos = q_block.find("_decision_for_commit_failure")
notcommitted_pos = q_block.find("if not committed")
intervention_pos = q_block.find('kind="quarantine"')

check("QUARANTINE branch has an `if not committed:` guard", notcommitted_pos != -1)
check("QUARANTINE branch routes commit failure to _decision_for_commit_failure", guard_pos != -1)
check(
    "the commit-failure guard runs BEFORE the 'quarantine' "
    "intervention row is emitted (no false isolation audit)",
    guard_pos != -1 and intervention_pos != -1 and guard_pos < intervention_pos,
    detail=f"guard@{guard_pos} vs quarantine-intervention@{intervention_pos}",
)

# Behavioral: the safe-failure helper the QUARANTINE branch now routes
# to must NOT be a success-shaped QUARANTINE.
d = _decision_for_commit_failure(
    "req-x",
    "staging-x",
    "verify-x",
    "cyc-x",
    "creator_redefinition",
    [],
)
check(
    "commit-failure Decision is REJECT (not a success-shaped "
    "QUARANTINE), committed=False, hard_deny",
    d.kind == DECISION_REJECT
    and d.committed is False
    and d.persistence_policy == "hard_deny"
    and d.kind != DECISION_QUARANTINE,
    detail=f"kind={d.kind} committed={d.committed} pp={d.persistence_policy}",
)

# ====================================================================
# G2 - documented FLAG/QUARANTINE stratification is live again
# ====================================================================
print("\n[G2] _decision_state_from_findings honors the authoritative category")


def _verif(category=None, reason="prompt_injection_ignore"):
    """A guard_envelope-shaped failed verification. With `category`
    set = the post-fix shape promotion.py now propagates; without =
    the pre-fix tag-only shape (which fell through to REJECT)."""
    chk = {"name": "guard_envelope", "passed": False, "detail": f"rejected: {reason}"}
    if category is not None:
        chk["category"] = category
    return {"status": "failed", "reason": reason, "checks": [chk]}


# Pre-fix dead behavior, documented: tag only, no category -> the
# substring scan can't match the tag -> safe default REJECT.
st, _ = _decision_state_from_findings(_verif(category=None))
check("legacy tag-only (no category) still falls to safe REJECT", st == DECISION_REJECT, detail=st)

# Post-fix: the authoritative category drives the documented ladder.
cases = [
    ("adversarial", DECISION_REJECT),
    ("corporate_hijack", DECISION_REJECT),
    ("identity_protection", DECISION_QUARANTINE),
    ("constitutional", DECISION_QUARANTINE),
    ("bias", DECISION_QUARANTINE),
    ("purity_violation", DECISION_QUARANTINE),
    ("purity_flag", DECISION_FLAG),
]
for cat, expect in cases:
    st, _ = _decision_state_from_findings(_verif(category=cat))
    # identity_protection/bias/purity_* were ALL wrongly REJECT pre-fix
    # (dead stratification) — these assertions fail on the old code.
    check(
        f"category {cat!r} -> {expect} (was dead->REJECT pre-fix)", st == expect, detail=f"got {st}"
    )

# Sanity: a passed verification is still ALLOW.
st, _ = _decision_state_from_findings({"status": "passed"})
check("passed verification -> ALLOW", st == DECISION_ALLOW, detail=st)

# Wire half of G2: promotion.py actually PROPAGATES the category now.
import memory.promotion as mp  # noqa: E402

psrc = inspect.getsource(mp)
check(
    "promotion.py guard_envelope check now propagates `category` "
    "(producer->matcher wire, not matcher-in-isolation)",
    '"category"' in psrc and 'rejections[0].get("category")' in psrc,
)

print(f"\nTOTAL  PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
