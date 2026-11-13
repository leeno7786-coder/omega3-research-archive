"""
HMAC Ledger — the silent ghost in the machine
================================================

Append-only, tamper-evident chain of signed records. Each entry's HMAC
includes the previous entry's HMAC, creating an unbreakable chain. If
a single byte changes anywhere — past, present, or in the key itself —
the ghost knows.

Design principles:
    - Write once. Never modify. Never delete.
    - The system reads from it, verifies against it, but cannot change it.
    - Silent. No logs, no announcements. It just IS.
    - If the chain breaks, it returns False. That's all. The Guardian
      decides what to do with that information.

Chain structure:

    Entry 0 (genesis):  HMAC(key, "OMEGA_GENESIS" + payload)
    Entry 1:            HMAC(key, entry_0.hmac + payload)
    Entry 2:            HMAC(key, entry_1.hmac + payload)
    ...
    Entry N:            HMAC(key, entry_N-1.hmac + payload)

If entry 3 is tampered with, entries 4..N all fail verification because
their prev_hmac no longer matches. The ghost catches it at the first
broken link.

The HMAC key is derived from a seed that exists only once, at birth.
It never leaves the machine. It never crosses the network.

Production mode:
    Methods that mutate the chain (`birth`, `append`) honour
    `guardian.production_mode.is_production_mode()`. When OFF, they
    silently no-op and return defaults consistent with "no chain
    sealed yet". This lets dev sessions exercise everything around the
    ledger without permanently sealing a charter.

    `verify()` always works (returns True with empty chain in dev) so
    callers don't need to special-case mode.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.paths import OMEGA_ROOT
from guardian.production_mode import is_production_mode
from guardian.terminal_feedback import emit_phase

log = logging.getLogger("omega.guardian.hmac_ledger")


def _emit_ledger_data(event_name: str, *, ok: bool, **data: Any) -> None:
    payload = {"ok": ok, **data}
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("guardian", event_name, status="ok" if ok else "fail", **payload)
    except Exception:
        pass
    try:
        from guardian.data_logger import emit_event

        emit_event("omega.guardian.hmac_ledger", event_name, **payload)
    except Exception:
        pass


# ── Constants ────────────────────────────────────────────

_GENESIS_MARKER = b"OMEGA_GENESIS"
_HASH_ALGO = "sha256"
_ENCODING = "utf-8"

# Default ledger paths — sacred. Excluded from day_zero_reset.
# 2026-05-08: relocated under guardian/ledger/ per the sole-writer
# doctrine. Pre-relocation paths under a retired legacy ledger dir never had
# a production birth on this instance (chain doesn't exist yet), so
# the move costs nothing and brings the sacred files into the same
# tree as every other guardian write.
DEFAULT_LEDGER_PATH = OMEGA_ROOT / "guardian" / "ledger" / "chain.jsonl"
DEFAULT_KEY_PATH = OMEGA_ROOT / "guardian" / "ledger" / "key.bin"

# Ledger entry types
ENTRY_GENESIS = "genesis"  # Birth record
ENTRY_CONSTITUTIONAL = "constitutional"  # A rule that defines what Omega IS
ENTRY_STRUCTURE = "structure"  # Architectural invariant
ENTRY_IDENTITY = "identity"  # Identity assertion
ENTRY_SEAL = "seal"  # Periodic integrity seal
ENTRY_PROMOTION = "promotion"  # Memory-promotion seal (3.0 addition)
ENTRY_INTERVENTION = "intervention"  # Guardian decision (3.0 addition)


class LedgerEntry:
    """A single link in the HMAC chain. Immutable once created."""

    __slots__ = (
        "seq",
        "entry_type",
        "payload",
        "payload_hash",
        "timestamp",
        "prev_hmac",
        "entry_hmac",
    )

    def __init__(
        self,
        seq: int,
        entry_type: str,
        payload: Dict[str, Any],
        payload_hash: str,
        timestamp: str,
        prev_hmac: str,
        entry_hmac: str,
    ):
        self.seq = seq
        self.entry_type = entry_type
        self.payload = payload
        self.payload_hash = payload_hash
        self.timestamp = timestamp
        self.prev_hmac = prev_hmac
        self.entry_hmac = entry_hmac

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "entry_type": self.entry_type,
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "timestamp": self.timestamp,
            "prev_hmac": self.prev_hmac,
            "entry_hmac": self.entry_hmac,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "LedgerEntry":
        return LedgerEntry(
            seq=d["seq"],
            entry_type=d["entry_type"],
            payload=d["payload"],
            payload_hash=d["payload_hash"],
            timestamp=d["timestamp"],
            prev_hmac=d["prev_hmac"],
            entry_hmac=d["entry_hmac"],
        )


class HMACLedger:
    """The silent ghost.

    Append-only HMAC chain. Writes to disk as JSONL — one entry per
    line, each independently parseable but cryptographically chained.
    """

    def __init__(
        self,
        ledger_path: Optional[Path] = None,
        key_path: Optional[Path] = None,
    ):
        self._ledger_path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH
        self._key_path = Path(key_path) if key_path else DEFAULT_KEY_PATH
        self._key: Optional[bytes] = None
        self._chain: List[LedgerEntry] = []
        self._born = False
        self._append_lock = threading.Lock()

    # ── Birth ────────────────────────────────────────────

    def birth(self, identity: Dict[str, Any]) -> bool:
        """Create the genesis entry. Once in Omega's lifetime.

        Returns True if birth succeeded, False if already born OR
        production mode is OFF.
        """
        if not is_production_mode():
            log.debug("HMACLedger.birth() deferred — dev mode")
            return False

        if self._ledger_path.exists():
            return False  # Already born. You only get one.

        seed = os.urandom(32)
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.write_bytes(seed)
        self._key = seed

        genesis_payload = {
            "event": "birth",
            "identity": identity,
            "key_fingerprint": hashlib.sha256(seed).hexdigest()[:16],
        }

        entry = self._mint_entry(
            seq=0,
            entry_type=ENTRY_GENESIS,
            payload=genesis_payload,
            prev_hmac=_GENESIS_MARKER.hex(),
        )

        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._ledger_path, "w", encoding=_ENCODING) as f:
            f.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")

        self._chain = [entry]
        self._born = True
        return True

    # ── Load ─────────────────────────────────────────────

    def load(self) -> bool:
        """Load the ledger from disk. Returns False if missing or corrupt
        OR production mode is OFF."""
        if not is_production_mode():
            return False

        if not self._ledger_path.exists() or not self._key_path.exists():
            return False

        self._key = self._key_path.read_bytes()
        self._chain = []

        with open(self._ledger_path, "r", encoding=_ENCODING) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    self._chain.append(LedgerEntry.from_dict(d))
                except (json.JSONDecodeError, KeyError) as exc:
                    # Corrupt/truncated chain line — load aborts and the
                    # charter will fail to verify. Production-mode only
                    # (load() early-returns in dev). This is the single
                    # highest-stakes integrity blind spot; surface the
                    # root cause structured. Metadata only — never the
                    # ledger line content.
                    log.error("HMACLedger.load: corrupt chain entry — %s", type(exc).__name__)
                    emit_phase(
                        "guardian",
                        "recover",
                        status="fail",
                        summary=(f"HMAC chain corrupt — load aborted: {type(exc).__name__}"),
                        component="hmac_ledger",
                        operation="recover",
                        failure_class="chain_entry_corrupt",
                        error_type=type(exc).__name__,
                        chain_seq=len(self._chain),
                        reason=str(exc)[:120],
                        recovery="retry",
                    )
                    return False

        self._born = len(self._chain) > 0
        return self._born

    # ── Append ───────────────────────────────────────────

    def append(
        self,
        entry_type: str,
        payload: Dict[str, Any],
    ) -> Optional[LedgerEntry]:
        """Append a new entry. Returns None in dev mode or on failure."""
        try:
            emit_phase(
                "guardian",
                "hmac_append",
                status="enter",
                summary=f"entry_type={entry_type}",
                entry_type=entry_type,
            )
        except Exception:
            pass
        if not is_production_mode():
            return None

        with self._append_lock:
            if not self._born or not self._key:
                return None

            prev = self._chain[-1]
            entry = self._mint_entry(
                seq=prev.seq + 1,
                entry_type=entry_type,
                payload=payload,
                prev_hmac=prev.entry_hmac,
            )

            try:
                with open(self._ledger_path, "a", encoding=_ENCODING) as f:
                    f.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
                self._chain.append(entry)
                # HMAC ledger append is a constitutional-state event:
                # identity charter, rules hash, lineage seal. Sparse by
                # design — one emission per real ledger advance is the
                # right grain. verify() stays silent (probed every
                # telemetry tick — emit there would flood the audit).
                emit_phase(
                    "guardian",
                    "hmac_append",
                    status="ok",
                    summary=f"entry_type={entry_type} seq={entry.seq}",
                    entry_type=entry_type,
                    seq=entry.seq,
                    payload_hash=entry.payload_hash[:16],
                )
                _emit_ledger_data(
                    "ledger_write",
                    ok=True,
                    entry_type=entry_type,
                    seq=entry.seq,
                )
                return entry
            except Exception as e:
                log.error("HMACLedger append failed: %s", e)
                emit_phase(
                    "guardian",
                    "hmac_append",
                    status="fail",
                    summary=f"{type(e).__name__}: {e}",
                    entry_type=entry_type,
                    recovery="retry",
                )
                _emit_ledger_data(
                    "ledger_write",
                    ok=False,
                    entry_type=entry_type,
                    error=str(e)[:120],
                )
                return None

    # ── Verify ───────────────────────────────────────────

    def verify(self) -> Tuple[bool, int]:
        """Walk the chain. Verify every link.

        Returns (is_valid, last_valid_seq).
        In dev mode (no chain): returns (True, -1) — "deferred, valid by
        default until production rollout".
        """
        if not is_production_mode():
            _emit_ledger_data("ledger_verify", ok=True, mode="dev", last_valid_seq=-1)
            return True, -1

        if not self._born or not self._key or not self._chain:
            _emit_ledger_data("ledger_verify", ok=False, reason="chain_not_loaded")
            return False, -1

        for i, entry in enumerate(self._chain):
            if entry.seq != i:
                _emit_ledger_data("ledger_verify", ok=False, last_valid_seq=i - 1)
                return False, i - 1

            computed_payload_hash = self._hash_payload(entry.payload)
            if computed_payload_hash != entry.payload_hash:
                _emit_ledger_data("ledger_verify", ok=False, last_valid_seq=i - 1)
                return False, i - 1

            if i == 0:
                expected_prev = _GENESIS_MARKER.hex()
            else:
                expected_prev = self._chain[i - 1].entry_hmac

            if entry.prev_hmac != expected_prev:
                _emit_ledger_data("ledger_verify", ok=False, last_valid_seq=i - 1)
                return False, i - 1

            expected_hmac = self._compute_hmac(
                entry.prev_hmac,
                entry.payload_hash,
                entry.seq,
                entry.entry_type,
                entry.timestamp,
            )
            if not hmac.compare_digest(entry.entry_hmac, expected_hmac):
                _emit_ledger_data("ledger_verify", ok=False, last_valid_seq=i - 1)
                return False, i - 1

        last_seq = self._chain[-1].seq
        _emit_ledger_data("ledger_verify", ok=True, last_valid_seq=last_seq)
        return True, last_seq

    def verify_entry(self, seq: int) -> bool:
        """Verify a single entry against its predecessor."""
        if not is_production_mode():
            return True
        if seq < 0 or seq >= len(self._chain):
            return False

        entry = self._chain[seq]
        if self._hash_payload(entry.payload) != entry.payload_hash:
            return False

        if seq == 0:
            expected_prev = _GENESIS_MARKER.hex()
        else:
            expected_prev = self._chain[seq - 1].entry_hmac

        if entry.prev_hmac != expected_prev:
            return False

        expected_hmac = self._compute_hmac(
            entry.prev_hmac,
            entry.payload_hash,
            entry.seq,
            entry.entry_type,
            entry.timestamp,
        )
        return hmac.compare_digest(entry.entry_hmac, expected_hmac)

    # ── Query ────────────────────────────────────────────

    def get_entry(self, seq: int) -> Optional[LedgerEntry]:
        if 0 <= seq < len(self._chain):
            return self._chain[seq]
        return None

    def get_entries_by_type(self, entry_type: str) -> List[LedgerEntry]:
        return [e for e in self._chain if e.entry_type == entry_type]

    def get_genesis(self) -> Optional[LedgerEntry]:
        if self._chain:
            return self._chain[0]
        return None

    @property
    def length(self) -> int:
        return len(self._chain)

    @property
    def is_born(self) -> bool:
        return self._born

    @property
    def head_hmac(self) -> str:
        if self._chain:
            return self._chain[-1].entry_hmac
        return ""

    # ── Internal ─────────────────────────────────────────

    def _mint_entry(
        self,
        seq: int,
        entry_type: str,
        payload: Dict[str, Any],
        prev_hmac: str,
    ) -> LedgerEntry:
        # Deep copy via JSON roundtrip — deterministic, no external refs
        frozen_payload = json.loads(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload_hash = self._hash_payload(frozen_payload)
        entry_hmac = self._compute_hmac(
            prev_hmac,
            payload_hash,
            seq,
            entry_type,
            ts,
        )
        return LedgerEntry(
            seq=seq,
            entry_type=entry_type,
            payload=frozen_payload,
            payload_hash=payload_hash,
            timestamp=ts,
            prev_hmac=prev_hmac,
            entry_hmac=entry_hmac,
        )

    def _compute_hmac(
        self,
        prev_hmac: str,
        payload_hash: str,
        seq: int,
        entry_type: str,
        timestamp: str,
    ) -> str:
        msg = f"{prev_hmac}|{seq}|{entry_type}|{timestamp}|{payload_hash}"
        return hmac.new(
            self._key,
            msg.encode(_ENCODING),
            _HASH_ALGO,
        ).hexdigest()

    @staticmethod
    def _hash_payload(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode(_ENCODING)).hexdigest()
