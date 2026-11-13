"""
Tests for `pressure_loop.open_questions` — Omega's journal of open questions to
the operator.

Covers:
    A. record_open_question round-trips through list_open / get
    B. attach_user_answer transitions latest open → answered
    C. attach_user_answer on empty/no-open returns None safely
    D. Multiple opens — latest-by-asked_at wins on attach
    E. mark_resolved + mark_superseded transitions
    F. status_counts reflects current latest-state per question_id
    G. ensure_open_question is idempotent per curiosity_id
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
sys.path.insert(0, str(REPO))

# Redirect to a tmp file BEFORE importing the module
_tmp_dir = REPO / ".test_state_open_questions"
_tmp_dir.mkdir(exist_ok=True)
_tmp_path = _tmp_dir / "open_questions.jsonl"
if _tmp_path.exists():
    _tmp_path.unlink()

import pressure_loop.open_questions as oq  # noqa: E402
import logging

log = logging.getLogger("omega.tests.metacognition.test_open_questions")


oq.OPEN_QUESTIONS_PATH = _tmp_path

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK  ] {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))


def section(label: str) -> None:
    print()
    print("=" * 72)
    print(f"  {label}")
    print("=" * 72)


# =====================================================================
section("A. record_open_question round-trip")
# =====================================================================

if _tmp_path.exists():
    _tmp_path.unlink()

qid_a = oq.record_open_question(
    curiosity_id="cur-test-anomaly-1",
    act="tension_response",
    question_text="I'm noticing a sharp conflict between identity and stagnation.",
    asked_cycle="int_test_cycle_a",
    rationale="trigger=anomaly gap=identity",
)
check(
    "A1 record_open_question returns a question_id", qid_a is not None and qid_a.startswith("oq-")
)

q = oq.get(qid_a)
check("A2 get(qid) returns OpenQuestion", q is not None and q.question_id == qid_a)
check("A2b status='open'", q and q.status == "open")
check("A2c curiosity_id captured", q and q.curiosity_id == "cur-test-anomaly-1")
check("A2d question_text captured", q and "sharp conflict" in q.question_text)

open_list = oq.list_open()
check(
    "A3 list_open() includes the new question",
    len(open_list) == 1 and open_list[0].question_id == qid_a,
)


# =====================================================================
section("B. attach_user_answer transitions open -> answered")
# =====================================================================

answered_qid = oq.attach_user_answer(
    answer_text="Yes — I rebuilt the tension via the new policy.",
    answered_cycle="usr_reply_cycle_1",
)
check("B1 attach_user_answer returns the attached question_id", answered_qid == qid_a)

q = oq.get(qid_a)
check("B2 status flipped to 'answered'", q and q.status == "answered")
check("B2b answer_text captured", q and "rebuilt the tension" in q.answer_text)
check("B2c answered_cycle captured", q and q.answered_cycle == "usr_reply_cycle_1")
check("B2d original asked_cycle preserved", q and q.asked_cycle == "int_test_cycle_a")

# Open list should now be empty; answered list should contain it
check("B3 list_open() now empty", len(oq.list_open()) == 0)
ans_recent = oq.list_answered_recent(limit=5)
check(
    "B3b list_answered_recent contains the question",
    len(ans_recent) == 1 and ans_recent[0].question_id == qid_a,
)


# =====================================================================
section("C. attach_user_answer with no open questions returns None")
# =====================================================================

no_attach = oq.attach_user_answer(
    answer_text="random reply",
    answered_cycle="usr_orphan_cycle",
)
check("C1 attach with no open question returns None", no_attach is None)


# =====================================================================
section("D. Multiple opens — latest-by-asked_at wins on attach")
# =====================================================================

# Wipe state for a clean section
if _tmp_path.exists():
    _tmp_path.unlink()
oq._lock.acquire()
oq._lock.release()

qid_b1 = oq.record_open_question(
    curiosity_id="cur-test-d-older",
    act="follow_up_question",
    question_text="Older question.",
    asked_cycle="int_d1",
)
time.sleep(0.01)  # ensure asked_at differs
qid_b2 = oq.record_open_question(
    curiosity_id="cur-test-d-newer",
    act="tension_response",
    question_text="Newer question.",
    asked_cycle="int_d2",
)
check("D1 two open questions exist", len(oq.list_open()) == 2)

answered = oq.attach_user_answer(
    answer_text="reply",
    answered_cycle="usr_d_reply",
)
check(
    "D2 latest-asked question wins attachment",
    answered == qid_b2,
    detail=f"got {answered}, expected {qid_b2}",
)

still_open = oq.list_open()
check("D3 older question stays open", len(still_open) == 1 and still_open[0].question_id == qid_b1)


# =====================================================================
section("E. mark_resolved + mark_superseded")
# =====================================================================

resolved_ok = oq.mark_resolved(
    qid_b2,
    resolved_by="omega.phase_2_evaluate.epoch_test",
    resolved_note="processed",
)
check("E1 mark_resolved returns True", resolved_ok is True)
q = oq.get(qid_b2)
check("E1b status now 'resolved'", q and q.status == "resolved")
check("E1c resolved_by captured", q and "omega.phase_2_evaluate" in q.resolved_by)

# Idempotent re-resolve
re_resolved = oq.mark_resolved(qid_b2, resolved_by="omega.again")
check("E2 re-resolving already-resolved returns True (idempotent)", re_resolved is True)

# Supersede the older one
sup_ok = oq.mark_superseded(qid_b1, reason="newer question subsumed it")
check("E3 mark_superseded returns True", sup_ok is True)
q = oq.get(qid_b1)
check("E3b status now 'superseded'", q and q.status == "superseded")


# =====================================================================
section("F. status_counts")
# =====================================================================

counts = oq.status_counts()
check(
    "F1 status_counts returns dict with 4 status keys",
    set(counts.keys()) == {"open", "answered", "resolved", "superseded"},
)
check("F2 open=0 (all transitioned)", counts["open"] == 0)
check("F3 resolved=1", counts["resolved"] == 1)
check("F4 superseded=1", counts["superseded"] == 1)


# =====================================================================
section("G. ensure_open_question idempotency")
# =====================================================================

if _tmp_path.exists():
    _tmp_path.unlink()

qid_g1 = oq.ensure_open_question(
    curiosity_id="cur-test-g-waiting",
    act="follow_up_question",
    question_text="Waiting question.",
    asked_cycle="hold-g1",
)
qid_g2 = oq.ensure_open_question(
    curiosity_id="cur-test-g-waiting",
    act="follow_up_question",
    question_text="Waiting question repeated.",
    asked_cycle="hold-g2",
)
open_g = oq.list_open()
check("G1 first ensure returns question id", qid_g1 is not None and qid_g1.startswith("oq-"))
check("G2 second ensure returns existing question id", qid_g2 == qid_g1)
check(
    "G3 ensure does not duplicate open questions",
    len(open_g) == 1 and open_g[0].question_id == qid_g1,
    detail=f"open={[(q.question_id, q.curiosity_id) for q in open_g]!r}",
)


# =====================================================================
print()
print("=" * 72)
print(f"  PASS: {PASS}")
print(f"  FAIL: {FAIL}")
print("=" * 72)

# Cleanup
try:
    if _tmp_path.exists():
        _tmp_path.unlink()
    _tmp_dir.rmdir()
except OSError:
    pass

if FAIL:
    sys.exit(1)
