"""O1 — the charter/ledger probe failure path must emit a failure-log signal.

_probe_charter collapsed charter.load / charter.verify / ledger.verify into one
try; its except only log.debug'd and returned a degraded dict with NO emit_data,
so those integrity-unknown failures never reached failures.jsonl (lost
observability vs FAILURE_LOG_COVERAGE rows 54-56). The fix adds one honest
emit_data("guardian", "charter_probe_failed", status="degraded", ok=False, ...).
"""

from __future__ import annotations


def test_charter_probe_failure_emits(monkeypatch):
    from hud import telemetry
    import guardian.terminal_feedback as tf
    import guardian.hmac_ledger as hl

    calls = []
    monkeypatch.setattr(tf, "emit_data", lambda *a, **k: calls.append((a, k)))

    def _boom(*a, **k):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(hl, "HMACLedger", _boom)
    # bypass the probe cache so the except path actually runs
    monkeypatch.setattr(telemetry, "_CHARTER_CACHE", None)

    result = telemetry._probe_charter()

    # honest degraded dict is still returned (shape-correct)
    assert result["mode"] == "unknown"
    assert result["verify_ok"] is False
    # the failure MUST be emitted, not just log.debug'd
    assert any("charter" in str(c[0]).lower() or "charter" in str(c[1]).lower() for c in calls), (
        f"no charter failure emit captured: {calls}"
    )
