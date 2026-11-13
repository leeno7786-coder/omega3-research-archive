"""
test_encoding_journal.py -- AUDIT-4 / Phase TEST

Tests the write-ahead encoding journal added per audit issue M1.
Closes the kill -9-mid-encoding window: an open journal entry
without a matching close = crash victim; boot recovery rolls back.

Six test groups:
  1. Public surface — open / step / close / abort / recovery exposed
  2. Open -> close cycle creates clean trail (no unclosed entries)
  3. Open -> abort cycle creates clean trail (no unclosed entries)
  4. Open without close = unclosed (find_unclosed surfaces it)
  5. recover_unclosed: rollback called for each unclosed; abort entry
     written; second invocation is a no-op (idempotent)
  6. Compaction: drops old close+abort, keeps open+step + recent
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    print(title)
    print("=" * 72)


def fresh_journal_module(tmp_path: Path):
    """Reload encoding_journal with JOURNAL_PATH pointed at tmp.
    The module reads OMEGA_ROOT at import time to compute
    JOURNAL_PATH; we patch the module-level constant directly."""
    import memory.encoding_journal as ej

    importlib.reload(ej)
    ej.JOURNAL_PATH = tmp_path / "encoding_journal.jsonl"
    return ej


# =====================================================================
# 1. PUBLIC SURFACE
# =====================================================================
section("1. PUBLIC SURFACE")

import memory.encoding_journal as ej
import logging

log = logging.getLogger("omega.tests.memory.test_encoding_journal")


for name in (
    "journal_open",
    "journal_step",
    "journal_close",
    "journal_abort",
    "find_unclosed_entries",
    "recover_unclosed",
    "compact_journal",
    "JOURNAL_PATH",
):
    check(f"{name} is exported", hasattr(ej, name))


# =====================================================================
# 2. OPEN -> CLOSE CYCLE
# =====================================================================
section("2. OPEN -> CLOSE CYCLE")

with tempfile.TemporaryDirectory() as tmp:
    ej = fresh_journal_module(Path(tmp))

    jid = ej.journal_open("0.00.0001", "staging-abc", "/path/to/0.00.0001.json")
    check("journal_open returned a non-empty id", isinstance(jid, str) and len(jid) > 0)
    check("journal file exists after open", ej.JOURNAL_PATH.exists())

    ej.journal_step(jid, "0.00.0001", "atomic_write")
    ej.journal_step(jid, "0.00.0001", "embed")
    ej.journal_close(jid, "0.00.0001", ["atomic_write", "embed"])

    unclosed = ej.find_unclosed_entries()
    check("find_unclosed empty after close", unclosed == [], f"got {unclosed}")

    # Verify on-disk shape
    lines = ej.JOURNAL_PATH.read_text(encoding="utf-8").strip().split("\n")
    entries = [json.loads(line) for line in lines]
    check(
        "journal has exactly 4 entries (open, step, step, close)",
        len(entries) == 4,
        f"got {len(entries)}",
    )
    ops = [e["op"] for e in entries]
    check(
        "ops in order: open, step, step, close",
        ops == ["open", "step", "step", "close"],
        f"got {ops}",
    )
    check("all entries share the journal_id", all(e["journal_id"] == jid for e in entries))


# =====================================================================
# 3. OPEN -> ABORT CYCLE
# =====================================================================
section("3. OPEN -> ABORT CYCLE")

with tempfile.TemporaryDirectory() as tmp:
    ej = fresh_journal_module(Path(tmp))

    jid = ej.journal_open("0.00.0002", "staging-def", "/path/to/0.00.0002.json")
    ej.journal_step(jid, "0.00.0002", "atomic_write")
    ej.journal_abort(
        jid,
        "0.00.0002",
        "encoding_failed: step=embed reason=FLM down",
        {"memory_file_removed": True, "index_entry_removed": True},
    )

    unclosed = ej.find_unclosed_entries()
    check("find_unclosed empty after abort", unclosed == [])

    lines = ej.JOURNAL_PATH.read_text(encoding="utf-8").strip().split("\n")
    entries = [json.loads(line) for line in lines]
    check("journal has 3 entries (open, step, abort)", len(entries) == 3)
    abort_entry = entries[-1]
    check("abort entry op is 'abort'", abort_entry["op"] == "abort")
    check("abort entry has reason", "encoding_failed" in abort_entry["reason"])
    check(
        "abort entry carries rollback summary",
        abort_entry["rollback"].get("memory_file_removed") is True,
    )


# =====================================================================
# 4. OPEN WITHOUT CLOSE = UNCLOSED
# =====================================================================
section("4. OPEN WITHOUT CLOSE")

with tempfile.TemporaryDirectory() as tmp:
    ej = fresh_journal_module(Path(tmp))

    # Two memories — one fully closed, one crashed mid-flight
    jid_closed = ej.journal_open("0.00.0010", "staging-closed", "/path/closed.json")
    ej.journal_close(jid_closed, "0.00.0010", ["atomic_write"])

    jid_crashed = ej.journal_open("0.00.0011", "staging-crashed", "/path/crashed.json")
    ej.journal_step(jid_crashed, "0.00.0011", "atomic_write")
    ej.journal_step(jid_crashed, "0.00.0011", "embed")
    # NO close/abort — simulating crash

    unclosed = ej.find_unclosed_entries()
    check("find_unclosed has exactly 1 entry", len(unclosed) == 1, f"got {len(unclosed)}")
    check(
        "unclosed entry is the crashed one (memory_id 0.00.0011)",
        unclosed[0]["memory_id"] == "0.00.0011",
    )
    check(
        "unclosed entry's last_step_completed == 'embed'",
        unclosed[0]["last_step_completed"] == "embed",
    )
    check("unclosed entry preserves final_path", unclosed[0]["final_path"] == "/path/crashed.json")
    check("unclosed entry preserves staging_ref", unclosed[0]["staging_ref"] == "staging-crashed")


# =====================================================================
# 5. RECOVER_UNCLOSED — calls rollback + appends abort + idempotent
# =====================================================================
section("5. RECOVER_UNCLOSED")

with tempfile.TemporaryDirectory() as tmp:
    ej = fresh_journal_module(Path(tmp))

    # Set up 2 unclosed entries
    jid_a = ej.journal_open("0.00.0020", "staging-a", "/path/a.json")
    ej.journal_step(jid_a, "0.00.0020", "atomic_write")
    jid_b = ej.journal_open("0.00.0021", "staging-b", "/path/b.json")

    # Stub rollback function (records calls)
    rollback_calls = []

    def fake_rollback(memory_dict, final_path):
        rollback_calls.append((memory_dict.get("id"), final_path))
        return {"memory_file_removed": True, "index_entry_removed": True, "errors": []}

    results = ej.recover_unclosed(fake_rollback)

    check("recover_unclosed returned 2 results", len(results) == 2, f"got {len(results)}")
    check("rollback called twice (one per unclosed entry)", len(rollback_calls) == 2)
    rolled_back_ids = {c[0] for c in rollback_calls}
    check("rollback called for both memory_ids", rolled_back_ids == {"0.00.0020", "0.00.0021"})

    # After recovery, find_unclosed should return empty
    still_unclosed = ej.find_unclosed_entries()
    check("find_unclosed empty after recovery", still_unclosed == [], f"got {still_unclosed}")

    # Idempotence: second call should find nothing + invoke rollback zero more times
    rollback_calls_before = len(rollback_calls)
    second_results = ej.recover_unclosed(fake_rollback)
    check("second recover_unclosed call returns empty list", second_results == [])
    check(
        "second recover_unclosed call did NOT re-invoke rollback",
        len(rollback_calls) == rollback_calls_before,
    )

    # Verify abort entries appended for each recovered memory
    lines = ej.JOURNAL_PATH.read_text(encoding="utf-8").strip().split("\n")
    entries = [json.loads(line) for line in lines]
    abort_entries = [e for e in entries if e["op"] == "abort"]
    check("two abort entries appended (one per recovery)", len(abort_entries) == 2)
    check(
        "abort reasons name 'boot_recovery'",
        all("boot_recovery" in e["reason"] for e in abort_entries),
    )


# =====================================================================
# 6. COMPACTION
# =====================================================================
section("6. COMPACTION")

with tempfile.TemporaryDirectory() as tmp:
    ej = fresh_journal_module(Path(tmp))

    # Mix of: old closed, recent closed, open (no close), step
    old_iso = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    new_iso = datetime.now(timezone.utc).isoformat()

    # Hand-craft journal lines so we can control timestamps
    lines = [
        # old close — should drop
        json.dumps(
            {
                "op": "close",
                "journal_id": "old1",
                "memory_id": "m1",
                "steps_completed": [],
                "closed_at": old_iso,
            }
        ),
        # old abort — should drop
        json.dumps(
            {
                "op": "abort",
                "journal_id": "old2",
                "memory_id": "m2",
                "reason": "x",
                "rollback": {},
                "aborted_at": old_iso,
            }
        ),
        # recent close — should keep
        json.dumps(
            {
                "op": "close",
                "journal_id": "new1",
                "memory_id": "m3",
                "steps_completed": [],
                "closed_at": new_iso,
            }
        ),
        # open + step (no close) — should keep BOTH
        json.dumps(
            {
                "op": "open",
                "journal_id": "in1",
                "memory_id": "m4",
                "staging_ref": "s",
                "final_path": "/p",
                "started_at": old_iso,
            }
        ),
        json.dumps(
            {
                "op": "step",
                "journal_id": "in1",
                "memory_id": "m4",
                "step": "embed",
                "completed_at": old_iso,
            }
        ),
    ]
    ej.JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    ej.JOURNAL_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = ej.compact_journal(keep_recent_seconds=7 * 86400)

    check("compaction reports total_before=5", result["total_before"] == 5, f"got {result}")
    check(
        "compaction dropped 2 (the old close + old abort)", result["dropped"] == 2, f"got {result}"
    )
    check("compaction kept 3 (recent close + open + step)", result["kept"] == 3, f"got {result}")

    # Verify on-disk
    after = ej.JOURNAL_PATH.read_text(encoding="utf-8").strip().split("\n")
    after_entries = [json.loads(line) for line in after]
    after_ops = sorted(e["op"] for e in after_entries)
    check(
        "on-disk has close + open + step (not the 2 old ones)",
        after_ops == ["close", "open", "step"],
        f"got {after_ops}",
    )


# =====================================================================
# 7. COMPACTION — paired journal_id (audit HIGH-1)
# =====================================================================
section("7. COMPACTION PAIRED journal_id")

with tempfile.TemporaryDirectory() as tmp:
    ej = fresh_journal_module(Path(tmp))
    old_iso = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    # Same journal_id: old open + step + close must compact together.
    # Keeping open while dropping close orphans find_unclosed_entries.
    paired_lines = [
        json.dumps(
            {
                "op": "open",
                "journal_id": "paired1",
                "memory_id": "m5",
                "staging_ref": "s",
                "final_path": "/p5",
                "started_at": old_iso,
            }
        ),
        json.dumps(
            {
                "op": "step",
                "journal_id": "paired1",
                "memory_id": "m5",
                "step": "embed",
                "completed_at": old_iso,
            }
        ),
        json.dumps(
            {
                "op": "close",
                "journal_id": "paired1",
                "memory_id": "m5",
                "steps_completed": ["embed"],
                "closed_at": old_iso,
            }
        ),
        # True crash victim (open only) — must survive compaction
        json.dumps(
            {
                "op": "open",
                "journal_id": "crash1",
                "memory_id": "m6",
                "staging_ref": "s",
                "final_path": "/p6",
                "started_at": old_iso,
            }
        ),
    ]
    ej.JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    ej.JOURNAL_PATH.write_text("\n".join(paired_lines) + "\n", encoding="utf-8")

    result = ej.compact_journal(keep_recent_seconds=7 * 86400)
    check("paired compaction dropped all 3 paired lines", result["dropped"] == 3, f"got {result}")
    check("paired compaction kept only the crash victim open", result["kept"] == 1, f"got {result}")

    unclosed = ej.find_unclosed_entries()
    check(
        "find_unclosed surfaces only crash1 (not paired1 ghost open)",
        len(unclosed) == 1 and unclosed[0].get("journal_id") == "crash1",
        f"got {[u.get('journal_id') for u in unclosed]}",
    )


# =====================================================================
# REPORT
# =====================================================================
print()
print("=" * 72)
print(f"TOTAL: PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
