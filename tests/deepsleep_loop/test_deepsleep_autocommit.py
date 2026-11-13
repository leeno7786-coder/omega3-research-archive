"""DeepSleep consolidations auto-commit — no guardian staging orphans."""

from __future__ import annotations

import pytest

from deepsleep_loop.consolidate_memory.run import _journal_envelope, _write_envelope
from memory.api import WriteReceipt

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def test_write_envelope_uses_mind_owned_intake_not_propose(monkeypatch):
    """DeepSleep products remain Guardian-free at the SRAM drain boundary."""
    captured: list[dict[str, object]] = []

    def _capture(envelope, *, propose=True, bypass_guardian=False):
        captured.append(
            {"propose": propose, "bypass_guardian": bypass_guardian, "type": envelope.type}
        )
        return WriteReceipt(
            cycle_id=envelope.cycle_id,
            staging_ref=None,
            path="memory/600_journal/fake.json",
            validation_status="committed",
            message="committed directly (verification bypassed)",
        )

    monkeypatch.setattr("deepsleep_loop.consolidate_memory.run.intake_memory", _capture)

    env = _journal_envelope("ep-autocommit", {"intent": "LEARN", "action": "web_fetch"})
    assert env is not None

    receipt = _write_envelope(env)

    assert receipt is not None
    assert receipt.validation_status == "committed"
    assert captured == [
        {"propose": False, "bypass_guardian": True, "type": "consolidation_journal"}
    ]


def test_write_envelope_leaves_no_staging_orphans(tmp_path, monkeypatch):
    """Integration: direct commit writes canonical memory; staging dir stays empty."""
    import memory.engine.validate_write as vw
    import memory.promotion as prom
    from memory.encoding import EncodingResult

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True)
    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True)
    (mem_root / "tmp_writes").mkdir(parents=True)

    monkeypatch.setattr(prom, "STAGING_DIR", staging_dir)
    monkeypatch.setattr(vw, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(vw, "TMP_DIR", mem_root / "tmp_writes")
    monkeypatch.setattr(
        "memory.encoding.encode_committed_memory",
        lambda md, fp, **_kw: EncodingResult(ok=True),
    )

    env = _journal_envelope(
        "ep-autocommit-2",
        {"intent": "LEARN", "action": "web_fetch", "omega_reasoning": {"what": "ok"}},
    )
    assert env is not None

    receipt = _write_envelope(env)

    assert receipt is not None
    assert receipt.validation_status == "committed"
    assert receipt.path is not None
    assert list(staging_dir.glob("staging-*.json")) == []
    assert any(mem_root.rglob("*.json")), (
        "direct commit should land a canonical memory record under the hermetic root"
    )
