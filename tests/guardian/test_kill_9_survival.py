"""
test_kill_9_survival.py -- AUDIT-6 / Phase TEST

End-to-end proof that AUDIT-4's encoding journal survives a real
kill -9. Spawns _audit6_victim.py as a subprocess, lets it land an
open journal entry + a fake canonical file, kills the process, and
verifies that recover_unclosed (the boot recovery path) cleanly
rolls back the in-flight write.

Per audit issue L4 + production-required:
  > No explicit `kill -9` survival test
  > Staging file persistence assumed but not stress-tested

This is the integration test that proves the durability claims.

Five test groups:
  1. Subprocess launch + ready marker received
  2. Kill -9 lands; victim is gone; canonical file + journal survive
  3. find_unclosed surfaces the orphan; matches what the victim wrote
  4. recover_unclosed deletes the canonical file + appends abort
  5. Idempotence: second recovery is a no-op
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
import logging

log = logging.getLogger("omega.tests.guardian.test_kill_9_survival")


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VICTIM_SCRIPT = ROOT / "tests" / "_audit6_victim.py"


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


def wait_for_marker(marker_file: Path, timeout_s: float = 5.0) -> bool:
    """Poll for the victim's ready marker. Returns True if the marker
    appeared within timeout, False otherwise."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if marker_file.exists():
            return True
        time.sleep(0.05)
    return False


# =====================================================================
# Setup — temp dir, victim subprocess, kill it
# =====================================================================

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    memory_id = "0.00.6666"
    marker_file = tempdir / "victim_ready.marker"
    journal_path = tempdir / "encoding_journal.jsonl"
    fake_path = tempdir / f"{memory_id}.json"

    # ── 1. SUBPROCESS LAUNCH ─────────────────────────────────────
    section("1. SUBPROCESS LAUNCH")

    proc = subprocess.Popen(
        [sys.executable, str(VICTIM_SCRIPT), str(tempdir), memory_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    ready = wait_for_marker(marker_file, timeout_s=10.0)
    check("victim subprocess wrote ready marker within 10s", ready, f"marker_path={marker_file}")
    if not ready:
        try:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=2)
            print(f"      victim stdout: {stdout!r}")
            print(f"      victim stderr: {stderr!r}")
        except Exception:
            pass
        # Bail — no point running other groups
        print(f"\nTOTAL: PASS={PASS}  FAIL={FAIL}")
        sys.exit(1)

    marker_data = json.loads(marker_file.read_text(encoding="utf-8"))
    check(
        "marker has journal_id",
        isinstance(marker_data.get("journal_id"), str) and len(marker_data["journal_id"]) > 0,
    )
    check("marker has memory_id matching argv", marker_data.get("memory_id") == memory_id)
    check("victim is still alive (poll() == None)", proc.poll() is None)

    # ── 2. KILL -9 LANDS ─────────────────────────────────────────
    section("2. KILL -9 LANDS, ARTIFACTS SURVIVE")

    proc.kill()  # TerminateProcess on Windows; SIGKILL on POSIX
    try:
        proc.wait(timeout=5.0)
        check("victim process exited after kill", True)
    except subprocess.TimeoutExpired:
        check("victim process exited after kill", False, "did not exit in 5s")
        proc.terminate()

    # Verify the artifacts the victim landed are still on disk
    check("fake canonical file survived kill", fake_path.exists(), f"path={fake_path}")
    check("encoding_journal.jsonl survived kill", journal_path.exists(), f"path={journal_path}")

    # The journal must contain the open entry — proving the fsync
    # made it past the kill boundary
    journal_lines = journal_path.read_text(encoding="utf-8").strip().split("\n")
    journal_entries = [json.loads(line) for line in journal_lines if line.strip()]
    check(
        "journal has at least 1 entry (the open)",
        len(journal_entries) >= 1,
        f"got {len(journal_entries)}",
    )
    open_entries = [e for e in journal_entries if e.get("op") == "open"]
    check("journal has exactly 1 open entry", len(open_entries) == 1, f"got {len(open_entries)}")
    check("open entry's memory_id matches", open_entries[0]["memory_id"] == memory_id)
    check(
        "no close/abort entries (kill landed mid-encoding)",
        not any(e.get("op") in ("close", "abort") for e in journal_entries),
    )

    # ── 3. find_unclosed SURFACES THE ORPHAN ─────────────────────
    section("3. find_unclosed SURFACES THE ORPHAN")

    # Reload encoding_journal in the parent + redirect JOURNAL_PATH
    import memory.encoding_journal as ej

    importlib.reload(ej)
    ej.JOURNAL_PATH = journal_path

    unclosed = ej.find_unclosed_entries()
    check("find_unclosed returns exactly 1 entry", len(unclosed) == 1, f"got {len(unclosed)}")
    check("unclosed entry's memory_id matches", unclosed[0]["memory_id"] == memory_id)
    recorded_path = Path(unclosed[0]["final_path"])
    if not recorded_path.is_absolute():
        recorded_path = (ej._journal_profile_root() / recorded_path).resolve()
    check(
        "unclosed entry's final_path matches the fake path we wrote",
        recorded_path == fake_path.resolve(),
    )

    # ── 4. recover_unclosed CLEANS UP ────────────────────────────
    section("4. recover_unclosed ROLLS BACK + ABORTS")

    # Stub rollback function — counts calls + actually deletes the
    # fake canonical file (mimicking rollback_canonical_write's
    # behavior without needing the real index-unpatch path)
    rollback_calls = []

    def fake_rollback(memory_dict, final_path):
        rollback_calls.append((memory_dict.get("id"), final_path))
        try:
            Path(final_path).unlink()
            return {"memory_file_removed": True, "index_entry_removed": True, "errors": []}
        except FileNotFoundError:
            return {"memory_file_removed": False, "index_entry_removed": True, "errors": []}

    results = ej.recover_unclosed(fake_rollback)

    check("recover_unclosed returned 1 result", len(results) == 1, f"got {len(results)}")
    check("rollback_fn was called once", len(rollback_calls) == 1)
    check("rollback_fn called with correct memory_id", rollback_calls[0][0] == memory_id)
    check("rollback_fn called with correct final_path", rollback_calls[0][1] == str(fake_path))
    check("fake canonical file is GONE after rollback", not fake_path.exists())

    # Verify the journal now has an abort entry
    journal_lines = journal_path.read_text(encoding="utf-8").strip().split("\n")
    journal_entries = [json.loads(line) for line in journal_lines if line.strip()]
    abort_entries = [e for e in journal_entries if e.get("op") == "abort"]
    check("journal now has 1 abort entry", len(abort_entries) == 1, f"got {len(abort_entries)}")
    check("abort reason names 'boot_recovery'", "boot_recovery" in abort_entries[0]["reason"])

    # ── 5. IDEMPOTENCE: SECOND RECOVERY IS A NO-OP ───────────────
    section("5. IDEMPOTENCE")

    rollback_calls_before = len(rollback_calls)
    second_results = ej.recover_unclosed(fake_rollback)
    check(
        "second recover_unclosed returns empty list", second_results == [], f"got {second_results}"
    )
    check("rollback_fn NOT called again", len(rollback_calls) == rollback_calls_before)

    # find_unclosed should now report nothing
    still_unclosed = ej.find_unclosed_entries()
    check("find_unclosed empty after recovery", still_unclosed == [], f"got {still_unclosed}")


# =====================================================================
# REPORT
# =====================================================================
print()
print("=" * 72)
print(f"TOTAL: PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
