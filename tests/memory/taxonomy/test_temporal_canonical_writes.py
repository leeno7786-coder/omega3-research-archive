# F:/Omega3.0/tests/memory/taxonomy/test_temporal_canonical_writes.py
import json
from unittest.mock import patch

import pytest

import pressure_loop.temporal_store as TS

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def _capture_envelope(monkeyfn):
    calls = []

    def fake_write(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        calls.append(
            {
                "env": env,
                "propose": propose,
                "bypass_guardian": bypass_guardian,
                "skip_embed": skip_embed,
            }
        )

        class _R:  # minimal WriteReceipt stand-in
            validation_status = "committed"
            path = "memory/x.json"

        return _R()

    return calls, fake_write


def test_episode_write_now_embeds_with_full_slot_json():
    calls, fake = _capture_envelope(None)
    body = {
        "episode_id": "ep-1",
        "closed_at": "2026-06-29T00:00:00+00:00",
        "cascade_status": "completed",
        "tool_calls": [{"tool_name": "write_file"}],
        "files_changed": ["fib.py"],
        "tests_run": ["test_fib.py"],
        "diff_summary": "added fib",
    }
    with patch("memory.api.write_memory", fake):
        TS.write_episode_to_temporal(body)
    assert len(calls) == 1
    c = calls[0]
    assert c["skip_embed"] is False  # embed now ON
    assert c["bypass_guardian"] is True and c["propose"] is False
    assert c["env"].coord == [9, 1, 0]
    text = json.loads(c["env"].payload["text"])
    assert text["files_changed"] == ["fib.py"]  # full slot JSON, not 240-char summary
    assert text["tests_run"] == ["test_fib.py"]
    assert c["env"].payload["enrichment"]["event_kind"] == "auto_outcome"


def test_consolidation_write_lands_at_9_3():
    calls, fake = _capture_envelope(None)
    rec = {
        "episode_id": "ep-2",
        "event_kind": "deepsleep_summary",
        "summary": "window consolidated",
        "text": "{}",
    }
    # Note: using monkeypatch/patch of write_memory since it's imported at runtime inside
    with patch("memory.api.write_memory", fake):
        TS.write_consolidation_to_temporal(rec)
    c = calls[0]
    assert c["env"].coord == [9, 3, 0]
    assert c["skip_embed"] is False and c["bypass_guardian"] is True
    assert c["env"].payload["enrichment"]["event_kind"] == "deepsleep_summary"


def test_capability_write_lands_at_9_4():
    calls, fake = _capture_envelope(None)
    rec = {
        "episode_id": "ep-3",
        "event_kind": "capability_reinforced",
        "skill_name": "fib_builder",
        "signal": "reinforce",
    }
    with patch("memory.api.write_memory", fake):
        TS.write_capability_to_temporal(rec)
    c = calls[0]
    assert c["env"].coord == [9, 4, 0]
    assert c["env"].payload["enrichment"]["event_kind"] == "capability_reinforced"


# ── a refused lobe-9 intake is never a written record ───────────────────────
#
# These writers called intake_memory bare, so a rejected envelope returned to the
# caller looking exactly like a held one and the consolidation/capability/trajectory
# pass reported success over a record that was never taken. Same discipline as
# write_episode_to_temporal, which already refuses to let a caller believe that.


def _refusing_write(validation_status="rejected", message="coord not admitted"):
    calls = []

    def fake_write(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        calls.append(env)

        class _R:
            pass

        receipt = _R()
        if validation_status is not None:
            receipt.validation_status = validation_status
        if message is not None:
            receipt.message = message
        return receipt

    return calls, fake_write


def test_consolidation_write_raises_when_intake_is_refused():
    calls, fake = _refusing_write()
    rec = {"episode_id": "ep-4", "event_kind": "deepsleep_summary", "summary": "s"}
    with patch("memory.api.write_memory", fake):
        with pytest.raises(RuntimeError) as exc:
            TS.write_consolidation_to_temporal(rec)
    assert len(calls) == 1
    assert "consolidation" in str(exc.value) and "coord not admitted" in str(exc.value)


def test_capability_write_raises_when_intake_is_refused():
    calls, fake = _refusing_write()
    rec = {"episode_id": "ep-5", "event_kind": "capability_weakened", "signal": "weaken"}
    with patch("memory.api.write_memory", fake):
        with pytest.raises(RuntimeError) as exc:
            TS.write_capability_to_temporal(rec)
    assert len(calls) == 1
    assert "capability" in str(exc.value) and "coord not admitted" in str(exc.value)


def test_shared_temporal_write_raises_on_an_unusable_receipt():
    """A receipt carrying no validation_status is not evidence the record was held."""
    calls, fake = _refusing_write(validation_status=None, message=None)
    with patch("memory.api.write_memory", fake):
        with pytest.raises(RuntimeError) as exc:
            TS.write_consolidation_to_temporal({"episode_id": "ep-6"})
    assert len(calls) == 1
    assert "consolidation" in str(exc.value)


def test_trajectory_write_raises_when_intake_is_refused():
    calls, fake = _refusing_write(message="trajectory envelope invalid")
    traj = {"target_domain": "python", "target_capability": "coding"}
    with patch("memory.api.write_memory", fake):
        with pytest.raises(RuntimeError) as exc:
            TS.write_trajectory(traj)
    assert len(calls) == 1
    assert "trajectory" in str(exc.value) and "trajectory envelope invalid" in str(exc.value)


def test_a_reconciled_named_trajectory_write_still_returns_without_writing(monkeypatch):
    """The duplicate-event early return survives the refusal gate: it never reaches intake."""
    from memory import intake_journal

    class _Journalled:
        status = intake_journal.COMMITTED

    monkeypatch.setattr(intake_journal, "record_by_id", lambda _rid: _Journalled())
    calls, fake = _refusing_write()
    with patch("memory.api.write_memory", fake):
        TS.write_trajectory(
            {"target_domain": "python", "target_capability": "coding"},
            event_id="trajectory:window-x:quantum",
        )
    assert calls == [], "a reconciled retry re-entered intake"
