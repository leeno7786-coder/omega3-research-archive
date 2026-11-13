"""
test_repair9_hygiene_sweep.py — REPAIR-9

Asserts the low-severity hygiene items are fixed:

  L1  boot._shutdown's `taskkill /im llama-server.exe` is dead code —
       llama.cpp/llama-server was retired in build_log/0022 (Qwen 1.7B
       → embedding_gemma at L0). Delete the block.

  L2  memory/gates/base.py:402 iterates COORD_INDEX directly, which
       races with concurrent `load_coord_index` calls. Migrate to the
       safe `coord_index_snapshot()` helper (added in AUDIT-5).

  L3  (skipped) tests/metacognition/test_global_loop.py does not exist.

  L4  memory/write_guard.py identity_guard removed per
       REVISED_DELETION_PLAN.md — verify no imports or references remain.

  L5  (bonus, surfaced in REPAIR-2; subsequently obsoleted) The
       kind→role map for mxbai_relevance was inconsistent with the
       loader's role binding. The mxbai reranker has since been
       retired entirely (single recall pipeline doctrine — see
       project_single_recall_pipeline). The L5 section is removed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
import logging

log = logging.getLogger("omega.tests.regression.test_repair9_hygiene_sweep")


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


# =====================================================================
section("L1. boot._shutdown — dead llama-server taskkill removed")
# =====================================================================

boot_text = (ROOT / "boot.py").read_text(encoding="utf-8")
# Check for the live invocation, not text mentions — a docstring naming
# the retirement is fine (audit trail) and the legacy comment elsewhere
# explaining tiny_cognitive's history is also fine.
check(
    "boot.py no longer invokes taskkill via subprocess",
    'subprocess.run(["taskkill"' not in boot_text and "subprocess.run(['taskkill'" not in boot_text,
)
check(
    "boot.py no longer targets llama-server.exe in any subprocess call",
    'llama-server.exe"' not in boot_text and "llama-server.exe'" not in boot_text,
)


# =====================================================================
section("L2. memory/gates/base.py — uses coord_index_snapshot()")
# =====================================================================

base_text = (ROOT / "memory" / "gates" / "base.py").read_text(encoding="utf-8")

# The old anti-pattern: direct iteration over COORD_INDEX.items() in a
# read path (races with concurrent reloads). Audit explicitly flagged
# the line at base.py:402.
check("memory/gates/base.py imports coord_index_snapshot", "coord_index_snapshot" in base_text)
check(
    "memory/gates/base.py no longer iterates COORD_INDEX.items() directly",
    "COORD_INDEX.items()" not in base_text,
    "still has direct iteration",
)


# =====================================================================
# L3 skipped — tests/metacognition/test_global_loop.py does not exist
# =====================================================================

# =====================================================================
section("L4. memory/write_guard.py — identity_guard removed")
# =====================================================================

wg_text = (ROOT / "memory" / "write_guard.py").read_text(encoding="utf-8")

# identity_guard was removed per REVISED_DELETION_PLAN.md - verify no imports remain
check(
    "write_guard does NOT import identity_guard",
    "from guardian.identity_guard" not in wg_text,
    "identity_guard import still present",
)
check(
    "write_guard does NOT reference identity_guard in comments",
    "identity_guard" not in wg_text or "identity_guard_removed" in wg_text,
    "identity_guard comment reference still present",
)


# L5 retired — mxbai reranker fully removed; see build_log/0096.


# =====================================================================
section("SUMMARY")
# =====================================================================

print(f"  PASS: {PASS}")
print(f"  FAIL: {FAIL}")
# pytest never imports plain scripts (tests/conftest.py collect_ignore), so the exit code
# is safe to restore; without it a FAIL here always exited 0.
sys.exit(0 if FAIL == 0 else 1)
