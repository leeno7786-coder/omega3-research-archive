"""The HUD identity-panel charter probe must not re-verify the full HMAC chain
on every ~2s refresh — it caches on the ledger chain file's mtime/size."""

from __future__ import annotations

import hud.telemetry as tel
from guardian import hmac_ledger


def test_probe_charter_caches_ledger_verify_across_refreshes(monkeypatch, tmp_path):
    # Stable ledger chain file so the cache key does not change mid-test.
    chain = tmp_path / "chain.jsonl"
    chain.write_text("", encoding="utf-8")
    monkeypatch.setattr(hmac_ledger, "DEFAULT_LEDGER_PATH", chain)

    # Fresh cache for this test (attrs may not exist pre-fix -> raising=False).
    monkeypatch.setattr(tel, "_CHARTER_CACHE", None, raising=False)
    monkeypatch.setattr(tel, "_CHARTER_CACHE_KEY", None, raising=False)

    # Count full-chain verify() calls.
    calls: list[int] = []
    orig_verify = hmac_ledger.HMACLedger.verify

    def counting_verify(self):
        calls.append(1)
        return orig_verify(self)

    monkeypatch.setattr(hmac_ledger.HMACLedger, "verify", counting_verify)

    # Five HUD refreshes with an unchanged ledger.
    for _ in range(5):
        tel._probe_charter()

    assert len(calls) == 1, (
        f"ledger.verify() ran {len(calls)}x across 5 refreshes — expected 1 "
        "(full chain re-verified every refresh = read amplification)"
    )


def test_probe_charter_reverifies_when_ledger_changes(monkeypatch, tmp_path):
    """A real ledger write (mtime/size change) DOES invalidate the cache."""
    chain = tmp_path / "chain.jsonl"
    chain.write_text("", encoding="utf-8")
    monkeypatch.setattr(hmac_ledger, "DEFAULT_LEDGER_PATH", chain)
    monkeypatch.setattr(tel, "_CHARTER_CACHE", None, raising=False)
    monkeypatch.setattr(tel, "_CHARTER_CACHE_KEY", None, raising=False)

    calls: list[int] = []
    orig_verify = hmac_ledger.HMACLedger.verify

    def counting_verify(self):
        calls.append(1)
        return orig_verify(self)

    monkeypatch.setattr(hmac_ledger.HMACLedger, "verify", counting_verify)

    tel._probe_charter()
    tel._probe_charter()
    assert len(calls) == 1  # cached
    # Simulate a ledger write: change the chain file (mtime + size).
    chain.write_text('{"seq":0}\n', encoding="utf-8")
    tel._probe_charter()
    assert len(calls) == 2  # re-verified after the change
