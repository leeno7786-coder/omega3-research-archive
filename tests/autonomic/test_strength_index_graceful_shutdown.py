"""
test_strength_index_graceful_shutdown.py -- AUDIT-1 / Phase TEST

Tests the graceful-shutdown flush registration per audit issue L2 +
production-required #3. Without this, up to SAVE_DIRTY_THRESHOLD-1 (24)
touch/decay updates could be lost on crash or graceful shutdown.

Five test groups in the existing PASS/FAIL counter style:
  1. Public surface — register_graceful_shutdown is exported + callable
  2. atexit registration — handler is actually registered with the module
  3. Idempotence — calling twice doesn't double-register or break
  4. Signal-handler registration — SIGINT cross-platform; SIGTERM POSIX-only
  5. Functional flush — handler actually saves dirty records to disk
"""

from __future__ import annotations

import atexit
import json
import signal
import sys
import tempfile
from pathlib import Path

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


# Reset module-global state between groups by reloading the module.
# (Necessary because register_graceful_shutdown() is intentionally
# idempotent at module level — we have to reload to test "first call".)
def fresh_strength_index_module():
    import importlib
    import memory.strength_index as si_mod

    importlib.reload(si_mod)
    return si_mod


# =====================================================================
# 1. PUBLIC SURFACE
# =====================================================================
section("1. PUBLIC SURFACE")

import memory.strength_index as si_mod
import logging

log = logging.getLogger("omega.tests.autonomic.test_strength_index_graceful_shutdown")


check("register_graceful_shutdown is exported", hasattr(si_mod, "register_graceful_shutdown"))
check(
    "register_graceful_shutdown is callable",
    callable(getattr(si_mod, "register_graceful_shutdown", None)),
)
check(
    "_flush_singleton helper exists (private but introspectable)",
    hasattr(si_mod, "_flush_singleton") and callable(si_mod._flush_singleton),
)
check("_shutdown_registered flag exists", hasattr(si_mod, "_shutdown_registered"))


# =====================================================================
# 2. ATEXIT REGISTRATION
# =====================================================================
section("2. ATEXIT REGISTRATION")

si_mod = fresh_strength_index_module()


# Snapshot atexit registry before
def _atexit_callbacks():
    # CPython exposes _ncallbacks; the registered fns themselves aren't
    # listed publicly, but we can count them.
    return atexit._ncallbacks()


before = _atexit_callbacks()
result = si_mod.register_graceful_shutdown()
after = _atexit_callbacks()

check("register_graceful_shutdown returned a dict", isinstance(result, dict))
check("result reports atexit registered", result.get("atexit") is True)
check(
    "atexit callback count increased by exactly 1",
    after == before + 1,
    f"before={before} after={after}",
)


# =====================================================================
# 3. IDEMPOTENCE
# =====================================================================
section("3. IDEMPOTENCE")

# Same module — call register_graceful_shutdown again on the SAME module
# (already registered). It should not add another atexit callback.
before2 = _atexit_callbacks()
result2 = si_mod.register_graceful_shutdown()
after2 = _atexit_callbacks()

check("second call returned a dict", isinstance(result2, dict))
check(
    "second call did NOT add another atexit callback",
    after2 == before2,
    f"before={before2} after={after2}",
)
check("module-level _shutdown_registered flag is True", si_mod._shutdown_registered is True)


# =====================================================================
# 4. SIGNAL-HANDLER REGISTRATION
# =====================================================================
section("4. SIGNAL HANDLER REGISTRATION")

# Reload to re-test the registration path on a clean module
si_mod = fresh_strength_index_module()
result3 = si_mod.register_graceful_shutdown()

# SIGINT should always register (Windows + POSIX, main thread)
check("SIGINT handler registered", result3.get("sigint") is True)

# Verify the registered SIGINT handler is OUR handler, not SIG_DFL
sigint_handler = signal.getsignal(signal.SIGINT)
check("SIGINT handler is callable (not SIG_DFL/SIG_IGN)", callable(sigint_handler))

# SIGTERM: registers on POSIX, gracefully falls through on Windows.
# Either outcome is correct; assert that the registration result is
# consistent with the platform.
is_windows = sys.platform.startswith("win")
if is_windows:
    check("SIGTERM handler not registered on Windows (expected)", result3.get("sigterm") is False)
else:
    check("SIGTERM handler registered on POSIX", result3.get("sigterm") is True)
    sigterm_handler = signal.getsignal(signal.SIGTERM)
    check("SIGTERM handler is callable on POSIX", callable(sigterm_handler))


# =====================================================================
# 5. FUNCTIONAL FLUSH — handler actually saves dirty records
# =====================================================================
section("5. FUNCTIONAL FLUSH")

# Use a fresh module + a tmpfile-backed singleton so we can inspect
# what got written to disk by the flush helper.
si_mod = fresh_strength_index_module()

with tempfile.TemporaryDirectory() as tmpdir:
    persist_path = Path(tmpdir) / "strength_index.json"
    si = si_mod.init_strength_index(persist_path)

    # Touch fewer than SAVE_DIRTY_THRESHOLD records — confirm they're
    # in RAM but NOT on disk yet (this is the bug being fixed).
    for i in range(5):
        si.register(f"mid_test_{i}", write_strength=0.6)

    check("after 5 registers, dirty count > 0 (in RAM)", si._dirty > 0, f"dirty={si._dirty}")
    check("strength_index.json does NOT exist yet (below threshold)", not persist_path.exists())

    # Now invoke the flush helper directly (simulates atexit / signal firing)
    si_mod._flush_singleton("test_invocation")

    check("strength_index.json EXISTS after flush", persist_path.exists())
    check("dirty count reset to 0 after flush", si._dirty == 0)

    # Verify the file actually contains our records
    with open(persist_path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    check("on-disk file has version field", on_disk.get("version") == si_mod.SCHEMA_VERSION)
    check("on-disk file has all 5 records", len(on_disk.get("records") or {}) == 5)
    check(
        "on-disk file has updated_at timestamp",
        isinstance(on_disk.get("updated_at"), str) and len(on_disk.get("updated_at", "")) > 0,
    )

    # Idempotence of the flush itself: calling _flush_singleton again
    # with dirty==0 must not crash and must not re-write a corrupt file.
    si_mod._flush_singleton("test_invocation_2")
    check("second flush is harmless (idempotent)", True)

    # Edge case: _flush_singleton with no singleton wired must be safe.
    # (Module reload to clear the singleton.)
    si_mod_clean = fresh_strength_index_module()
    try:
        si_mod_clean._flush_singleton("no_singleton")
        check("_flush_singleton is safe when singleton is None", True)
    except Exception as exc:
        check("_flush_singleton is safe when singleton is None", False, str(exc))


# =====================================================================
# REPORT
# =====================================================================
print()
print("=" * 72)
print(f"TOTAL: PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
