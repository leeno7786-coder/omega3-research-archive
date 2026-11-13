"""
test_coord_index_concurrency.py -- AUDIT-5 / Phase TEST

Tests the walker coord_index reload + snapshot pattern per audit
issue M2. Closes the rapid-update read-while-writing race window
and verifies the new public surface (snapshot + generation).

Six test groups:
  1. Public surface — coord_index_snapshot + coord_index_generation exposed
  2. Snapshot is independent — mutating snapshot doesn't affect COORD_INDEX
  3. Generation counter — bumps on reload, stable when index unchanged
  4. Diff-based swap — adds, removals, and updates all land correctly
  5. logging import is present — error path doesn't NameError
  6. Concurrency — hammer with concurrent reload + iteration; no
     RuntimeError, no torn reads
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
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


def write_index(path: Path, data: dict) -> None:
    """Write the index file atomically (tmp + rename) so mtime
    advances cleanly between writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


# =====================================================================
# 1. PUBLIC SURFACE
# =====================================================================
section("1. PUBLIC SURFACE")

import memory.engine.walker as walker
from memory.engine.walker import CoordIndexUnavailable
import logging

log = logging.getLogger("omega.tests.memory.test_coord_index_concurrency")


check(
    "coord_index_snapshot is exported",
    hasattr(walker, "coord_index_snapshot") and callable(walker.coord_index_snapshot),
)
check(
    "coord_index_generation is exported",
    hasattr(walker, "coord_index_generation") and callable(walker.coord_index_generation),
)
check("COORD_INDEX is still the same module-level dict", isinstance(walker.COORD_INDEX, dict))
check(
    "logging is imported (error path won't NameError)",
    hasattr(walker, "logging") or hasattr(walker, "log"),
)


# =====================================================================
# 2. SNAPSHOT IS INDEPENDENT
# =====================================================================
section("2. SNAPSHOT INDEPENDENCE")

with tempfile.TemporaryDirectory() as tmp:
    idx_path = Path(tmp) / "coord_index.json"
    write_index(idx_path, {"0.00.0001": {"path": "a.json", "coord": [0, 0, 1]}})

    with patch.object(walker, "INDEX_PATH", idx_path):
        # Reset module state
        walker._coord_index_mtime = 0.0
        walker._coord_index_generation = 0
        walker.COORD_INDEX.clear()

        walker.load_coord_index()
        snap = walker.coord_index_snapshot()

        check("snapshot is a separate dict object", snap is not walker.COORD_INDEX)
        check("snapshot has same content", snap == walker.COORD_INDEX)

        # Mutate the snapshot
        snap["0.99.9999"] = {"injected": True}

        check("mutating snapshot did NOT affect COORD_INDEX", "0.99.9999" not in walker.COORD_INDEX)


# =====================================================================
# 3. GENERATION COUNTER
# =====================================================================
section("3. GENERATION COUNTER")

with tempfile.TemporaryDirectory() as tmp:
    idx_path = Path(tmp) / "coord_index.json"
    write_index(idx_path, {"0.00.0001": {"path": "a.json", "coord": [0, 0, 1]}})

    with patch.object(walker, "INDEX_PATH", idx_path):
        walker._coord_index_mtime = 0.0
        walker._coord_index_generation = 0
        walker.COORD_INDEX.clear()

        gen0 = walker.coord_index_generation()
        check("initial generation is 0", gen0 == 0)

        walker.load_coord_index()
        gen1 = walker.coord_index_generation()
        check("generation bumps after first load", gen1 == 1, f"got gen1={gen1}")

        # Second call without changing the file — generation stable
        walker.load_coord_index()
        gen2 = walker.coord_index_generation()
        check("generation stable when file unchanged", gen2 == gen1, f"got gen2={gen2}")

        # Modify file (mtime advances), reload, generation bumps
        time.sleep(0.05)  # ensure mtime resolution kicks
        write_index(
            idx_path,
            {
                "0.00.0001": {"path": "a.json", "coord": [0, 0, 1]},
                "0.00.0002": {"path": "b.json", "coord": [0, 0, 2]},
            },
        )
        walker.load_coord_index()
        gen3 = walker.coord_index_generation()
        check("generation bumps after file change", gen3 == gen2 + 1, f"got gen3={gen3}")


# =====================================================================
# 4. DIFF-BASED SWAP
# =====================================================================
section("4. DIFF-BASED SWAP (adds, removals, updates)")

with tempfile.TemporaryDirectory() as tmp:
    idx_path = Path(tmp) / "coord_index.json"
    write_index(
        idx_path,
        {
            "0.00.0001": {"path": "a.json", "coord": [0, 0, 1]},
            "0.00.0002": {"path": "b.json", "coord": [0, 0, 2]},
            "0.00.0003": {"path": "c.json", "coord": [0, 0, 3]},
        },
    )

    with patch.object(walker, "INDEX_PATH", idx_path):
        walker._coord_index_mtime = 0.0
        walker._coord_index_generation = 0
        walker.COORD_INDEX.clear()

        walker.load_coord_index()
        check("initial load has 3 entries", len(walker.COORD_INDEX) == 3)

        # Update: drop 0001, change 0002's path, add 0004
        time.sleep(0.05)
        write_index(
            idx_path,
            {
                "0.00.0002": {"path": "b_renamed.json", "coord": [0, 0, 2]},
                "0.00.0003": {"path": "c.json", "coord": [0, 0, 3]},
                "0.00.0004": {"path": "d.json", "coord": [0, 0, 4]},
            },
        )
        walker.load_coord_index()

        check("0001 removed", "0.00.0001" not in walker.COORD_INDEX)
        check("0002 path updated", walker.COORD_INDEX["0.00.0002"]["path"] == "b_renamed.json")
        check("0003 unchanged", walker.COORD_INDEX["0.00.0003"]["path"] == "c.json")
        check("0004 added", "0.00.0004" in walker.COORD_INDEX)
        check("total count = 3 (0002, 0003, 0004)", len(walker.COORD_INDEX) == 3)


# =====================================================================
# 5. LOGGING IMPORT (error path doesn't NameError)
# =====================================================================
section("5. ERROR PATH DOES NOT NAMEERROR")

with tempfile.TemporaryDirectory() as tmp:
    idx_path = Path(tmp) / "coord_index.json"
    # Write invalid JSON to trigger the error path
    idx_path.write_text("not-valid-json-{[}", encoding="utf-8")

    with patch.object(walker, "INDEX_PATH", idx_path):
        walker._coord_index_mtime = 0.0
        walker.COORD_INDEX.clear()
        try:
            walker.load_coord_index()
            check("load_coord_index handles invalid JSON without NameError", True)
        except CoordIndexUnavailable as exc:
            check(
                "load_coord_index handles invalid JSON without NameError",
                True,
                f"typed failure: {exc}",
            )
        except Exception as exc:
            check(
                "load_coord_index handles invalid JSON without NameError",
                False,
                f"unexpected: {type(exc).__name__}: {exc}",
            )

    # Also: root-not-dict path
    idx_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with patch.object(walker, "INDEX_PATH", idx_path):
        walker._coord_index_mtime = 0.0
        walker.COORD_INDEX.clear()
        try:
            walker.load_coord_index()
            check("load_coord_index rejects non-dict root cleanly", True)
            check("COORD_INDEX stays empty after non-dict root", len(walker.COORD_INDEX) == 0)
        except CoordIndexUnavailable as exc:
            check(
                "load_coord_index rejects non-dict root cleanly",
                True,
                f"typed failure: {exc}",
            )
            check("COORD_INDEX stays empty after non-dict root", len(walker.COORD_INDEX) == 0)
        except Exception as exc:
            check(
                "load_coord_index rejects non-dict root cleanly",
                False,
                f"raised: {type(exc).__name__}: {exc}",
            )


# =====================================================================
# 6. CONCURRENCY — concurrent reload + snapshot iteration, no race
# =====================================================================
section("6. CONCURRENCY (snapshot iteration vs concurrent reload)")

with tempfile.TemporaryDirectory() as tmp:
    idx_path = Path(tmp) / "coord_index.json"
    # Seed with 50 entries
    seed = {f"0.00.{i:04d}": {"path": f"f{i}.json", "coord": [0, 0, i]} for i in range(50)}
    write_index(idx_path, seed)

    with patch.object(walker, "INDEX_PATH", idx_path):
        walker._coord_index_mtime = 0.0
        walker._coord_index_generation = 0
        walker.COORD_INDEX.clear()
        walker.load_coord_index()

        # Two threads:
        #   - WRITER: every 5 ms rewrites the file with shifted entries
        #   - READER: snapshots + iterates, counting cycles + errors
        stop_flag = threading.Event()
        errors: list = []
        snapshot_iterations = [0]
        direct_iterations = [0]

        def writer():
            i = 0
            while not stop_flag.is_set():
                i += 1
                new_data = {
                    f"0.00.{(j + i) % 200:04d}": {"path": f"f{j}.json", "coord": [0, 0, j]}
                    for j in range(50)
                }
                try:
                    write_index(idx_path, new_data)
                    walker.load_coord_index()
                except Exception as e:
                    errors.append(f"writer: {type(e).__name__}: {e}")
                time.sleep(0.005)

        def snapshot_reader():
            while not stop_flag.is_set():
                try:
                    snap = walker.coord_index_snapshot()
                    # Iterate the snapshot — should never raise
                    _ = sum(1 for _ in snap.items())
                    snapshot_iterations[0] += 1
                except Exception as e:
                    errors.append(f"snapshot_reader: {type(e).__name__}: {e}")
                time.sleep(0.001)

        def direct_reader():
            # Demonstrates the unsafe pattern — we EXPECT this to
            # potentially raise. We catch + count to characterize the
            # race, NOT to claim it never races.
            races = 0
            while not stop_flag.is_set():
                try:
                    _ = sum(1 for _ in walker.COORD_INDEX.items())
                    direct_iterations[0] += 1
                except RuntimeError:
                    races += 1
                except Exception as e:
                    errors.append(f"direct_reader: {type(e).__name__}: {e}")
                time.sleep(0.001)
            print(f"      (info) direct iteration races caught: {races}")

        threads = [
            threading.Thread(target=writer, daemon=True),
            threading.Thread(target=snapshot_reader, daemon=True),
            threading.Thread(target=direct_reader, daemon=True),
        ]
        for t in threads:
            t.start()
        time.sleep(0.5)  # 500 ms of hammering
        stop_flag.set()
        for t in threads:
            t.join(timeout=2.0)

        check(
            "snapshot_reader completed >= 50 iterations under load",
            snapshot_iterations[0] >= 50,
            f"got {snapshot_iterations[0]}",
        )
        check(
            "snapshot iteration NEVER raised RuntimeError",
            not any("snapshot_reader" in e for e in errors),
            f"errors: {[e for e in errors if 'snapshot_reader' in e][:3]}",
        )
        check(
            "writer completed without errors",
            not any("writer" in e for e in errors),
            f"errors: {[e for e in errors if 'writer' in e][:3]}",
        )
        check(
            "generation counter advanced under load",
            walker.coord_index_generation() > 1,
            f"gen={walker.coord_index_generation()}",
        )


# =====================================================================
# REPORT
# =====================================================================
print()
print("=" * 72)
print(f"TOTAL: PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
