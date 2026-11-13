"""
test_production_charter.py -- AUDIT-7 / Phase TEST

Tests the production-mode charter HMAC verification per audit
production-required #2.

Exercises:
  - is_production_mode() resolution (env / sentinel / config / default)
  - HMACLedger birth + append + verify in production mode
  - HMACLedger.verify() catches tampering at the right seq
  - HMACLedger restart simulation (new instance reads back same files)
  - IdentityCharter end-to-end (birth -> rules + structure + identity in chain)
  - IdentityCharter restart simulation (load + verify across "process boot")
  - Dev-mode contract: birth/append return defaults; verify returns (True, -1)

Eight test groups:
  1. is_production_mode resolution (default + env var + sentinel)
  2. HMACLedger.birth() under prod mode writes chain + key
  3. HMACLedger.append() under prod mode grows chain durably
  4. HMACLedger.verify() catches tampering at the right entry
  5. HMACLedger restart: new instance loads + verifies same files
  6. IdentityCharter.birth() under prod mode populates the chain
  7. IdentityCharter.verify() returns (True, []) on a clean chain
  8. Dev mode: birth/append/verify follow the deferred contract
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
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


# Helpers — set/clear the env var, override sentinel path
def with_prod_env(value: bool):
    """Context-manager-ish helper. Returns a (before, after) pair the
    caller restores via os.environ assignment."""
    if value:
        os.environ["OMEGA_PRODUCTION_MODE"] = "1"
    else:
        os.environ.pop("OMEGA_PRODUCTION_MODE", None)


# =====================================================================
# 1. is_production_mode RESOLUTION
# =====================================================================
section("1. is_production_mode RESOLUTION")

# Save + clear the env to start clean
_saved_env = os.environ.pop("OMEGA_PRODUCTION_MODE", None)

import guardian.production_mode as pm
import logging

log = logging.getLogger("omega.tests.guardian.test_production_charter")

importlib.reload(pm)

with tempfile.TemporaryDirectory() as tmp:
    fake_sentinel = Path(tmp) / "ledger" / ".activated"
    with patch.object(pm, "_SENTINEL_PATH", fake_sentinel):
        # Clean state — no sentinel, no env, no config flag (probe will
        # fail to load governance.yaml and return False)
        check("default (no env, no sentinel) -> False", pm.is_production_mode() is False)

        # Env var set
        with_prod_env(True)
        try:
            check("env OMEGA_PRODUCTION_MODE=1 -> True", pm.is_production_mode() is True)
        finally:
            with_prod_env(False)

        # Env truthy variants
        for variant in ("true", "yes", "on"):
            os.environ["OMEGA_PRODUCTION_MODE"] = variant
            try:
                check(
                    f"env OMEGA_PRODUCTION_MODE={variant!r} -> True",
                    pm.is_production_mode() is True,
                )
            finally:
                with_prod_env(False)

        # Sentinel file
        fake_sentinel.parent.mkdir(parents=True, exist_ok=True)
        fake_sentinel.write_text("activated", encoding="utf-8")
        check("sentinel file present -> True", pm.is_production_mode() is True)
        check("sentinel_exists() reports True", pm.sentinel_exists() is True)

# Restore env if it was set before this test
if _saved_env is not None:
    os.environ["OMEGA_PRODUCTION_MODE"] = _saved_env


# =====================================================================
# 2. HMACLedger.birth() UNDER PROD MODE
# =====================================================================
section("2. HMACLedger.birth() UNDER PROD MODE")

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    ledger_path = tempdir / "chain.jsonl"
    key_path = tempdir / "key.bin"

    # Force prod mode via env
    with_prod_env(True)
    try:
        # Reload modules so they pick up the env state
        import guardian.production_mode as pm

        importlib.reload(pm)
        import guardian.hmac_ledger as hl

        importlib.reload(hl)

        check("is_production_mode() True under env", pm.is_production_mode() is True)

        ledger = hl.HMACLedger(ledger_path=ledger_path, key_path=key_path)

        check("ledger.is_born initially False", ledger.is_born is False)
        check("ledger.length initially 0", ledger.length == 0)

        born = ledger.birth({"name": "Omega-test", "creator": "Noah"})
        check("birth() returned True", born is True)
        check("ledger_path now exists", ledger_path.exists())
        check("key_path now exists", key_path.exists())
        check("key file is non-empty (32 bytes per design)", key_path.stat().st_size == 32)
        check("ledger.is_born is True", ledger.is_born is True)
        check("ledger.length == 1 (genesis)", ledger.length == 1)

        # Birth twice — second call must return False
        born2 = ledger.birth({"name": "Omega-test"})
        check("second birth() returns False (already born)", born2 is False)
    finally:
        with_prod_env(False)


# =====================================================================
# 3. HMACLedger.append() GROWS CHAIN DURABLY
# =====================================================================
section("3. HMACLedger.append() GROWS CHAIN")

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    ledger_path = tempdir / "chain.jsonl"
    key_path = tempdir / "key.bin"

    with_prod_env(True)
    try:
        import guardian.production_mode as pm

        importlib.reload(pm)
        import guardian.hmac_ledger as hl

        importlib.reload(hl)

        ledger = hl.HMACLedger(ledger_path=ledger_path, key_path=key_path)
        ledger.birth({"name": "Omega-test"})

        e1 = ledger.append(hl.ENTRY_CONSTITUTIONAL, {"rule_id": "C001", "text": "test rule 1"})
        check("first append returned an entry", e1 is not None)
        check("first append has seq == 1", e1.seq == 1)

        e2 = ledger.append(hl.ENTRY_CONSTITUTIONAL, {"rule_id": "C002", "text": "test rule 2"})
        check("second append returned an entry", e2 is not None)
        check("second append has seq == 2", e2.seq == 2)
        check("second entry.prev_hmac == first entry.entry_hmac", e2.prev_hmac == e1.entry_hmac)

        check("ledger.length == 3", ledger.length == 3)

        # Verify chain.jsonl has 3 lines on disk
        on_disk = ledger_path.read_text(encoding="utf-8").strip().split("\n")
        check("chain.jsonl has 3 lines on disk", len(on_disk) == 3)
    finally:
        with_prod_env(False)


# =====================================================================
# 4. HMACLedger.verify() CATCHES TAMPERING
# =====================================================================
section("4. HMACLedger.verify() CATCHES TAMPERING")

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    ledger_path = tempdir / "chain.jsonl"
    key_path = tempdir / "key.bin"

    with_prod_env(True)
    try:
        import guardian.production_mode as pm

        importlib.reload(pm)
        import guardian.hmac_ledger as hl

        importlib.reload(hl)

        ledger = hl.HMACLedger(ledger_path=ledger_path, key_path=key_path)
        ledger.birth({"name": "Omega-test"})
        ledger.append(hl.ENTRY_CONSTITUTIONAL, {"rule_id": "C001", "text": "rule 1"})
        ledger.append(hl.ENTRY_CONSTITUTIONAL, {"rule_id": "C002", "text": "rule 2"})
        ledger.append(hl.ENTRY_CONSTITUTIONAL, {"rule_id": "C003", "text": "rule 3"})

        # Clean chain verifies
        valid, last = ledger.verify()
        check(
            "clean chain verify() = (True, last_seq)",
            valid is True and last == 3,
            f"got ({valid}, {last})",
        )

        # Tamper with seq 2's payload IN MEMORY (not on disk)
        ledger._chain[2].payload["text"] = "TAMPERED rule 2"
        valid, last = ledger.verify()
        check(
            "verify() catches tampered payload at seq 2",
            valid is False and last == 1,
            f"got ({valid}, {last}); expected (False, 1)",
        )
    finally:
        with_prod_env(False)


# =====================================================================
# 5. HMACLedger RESTART (NEW INSTANCE, SAME FILES)
# =====================================================================
section("5. HMACLedger RESTART -- LOAD VERIFIES")

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    ledger_path = tempdir / "chain.jsonl"
    key_path = tempdir / "key.bin"

    with_prod_env(True)
    try:
        import guardian.production_mode as pm

        importlib.reload(pm)
        import guardian.hmac_ledger as hl

        importlib.reload(hl)

        # First "process": birth + append
        ledger1 = hl.HMACLedger(ledger_path=ledger_path, key_path=key_path)
        ledger1.birth({"name": "Omega-test"})
        for i in range(5):
            ledger1.append(hl.ENTRY_CONSTITUTIONAL, {"rule_id": f"C{i:03d}", "text": f"rule {i}"})
        check("first instance length == 6 (genesis + 5)", ledger1.length == 6)

        # Second "process": new instance, same files
        ledger2 = hl.HMACLedger(ledger_path=ledger_path, key_path=key_path)
        loaded = ledger2.load()
        check("second instance load() returned True", loaded is True)
        check("second instance length == 6", ledger2.length == 6)
        check("second instance is_born == True", ledger2.is_born is True)

        valid, last = ledger2.verify()
        check("loaded chain verifies", valid is True and last == 5, f"got ({valid}, {last})")
        check("genesis preserved", ledger2.get_genesis() is not None)
        check(
            "genesis identity matches",
            ledger2.get_genesis().payload.get("identity", {}).get("name") == "Omega-test",
        )
    finally:
        with_prod_env(False)


# =====================================================================
# 6. IdentityCharter.birth() POPULATES CHAIN
# =====================================================================
section("6. IdentityCharter.birth() POPULATES CHAIN")

# =====================================================================
# 6. IdentityCharter.birth() ON CLEAN CHAIN
# =====================================================================
# REMOVED: identity_charter module deleted per REVISED_DELETION_PLAN.md
# This test is no longer applicable.

# =====================================================================
# 7. IdentityCharter.verify() ON CLEAN CHAIN
# =====================================================================
# REMOVED: identity_charter module deleted per REVISED_DELETION_PLAN.md
# This test is no longer applicable.

# =====================================================================
# 8. DEV MODE — DEFERRED CONTRACT
# =====================================================================
section("8. DEV MODE -- DEFERRED CONTRACT")

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    ledger_path = tempdir / "chain.jsonl"
    key_path = tempdir / "key.bin"
    sentinel_path = tempdir / "ledger" / ".activated"

    # Ensure prod mode is OFF
    with_prod_env(False)

    import guardian.production_mode as pm

    importlib.reload(pm)
    import guardian.hmac_ledger as hl

    importlib.reload(hl)

    with patch.object(pm, "_SENTINEL_PATH", sentinel_path):
        check("is_production_mode() False in dev", pm.is_production_mode() is False)

        ledger = hl.HMACLedger(ledger_path=ledger_path, key_path=key_path)

        born = ledger.birth({"name": "Omega-dev"})
        check("ledger.birth() returns False in dev", born is False)
        check("ledger_path NOT created in dev", not ledger_path.exists())

        appended = ledger.append(hl.ENTRY_CONSTITUTIONAL, {"rule_id": "X"})
        check("ledger.append() returns None in dev", appended is None)

        valid, last = ledger.verify()
        check(
            "ledger.verify() returns (True, -1) in dev",
            valid is True and last == -1,
            f"got ({valid}, {last})",
        )

        # REMOVED: identity_charter module deleted per REVISED_DELETION_PLAN.md
        # Charter tests no longer applicable in dev mode.


# =====================================================================
# REPORT
# =====================================================================
# Cleanup: ensure env is restored
os.environ.pop("OMEGA_PRODUCTION_MODE", None)

print()
print("=" * 72)
print(f"TOTAL: PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
