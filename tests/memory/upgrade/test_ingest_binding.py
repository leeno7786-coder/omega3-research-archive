"""Worker bindings preserve public API intake and restore thread ownership."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from memory import api
from memory import episode_sram as sram


def test_bound_worker_intakes_through_public_api_without_committing():
    from memory.engine import validate_write

    sram.open_bucket("episode-a", pipeline_id=0)
    sram.open_bucket("episode-b", pipeline_id=1)
    envelope = api.CanonicalEnvelope(
        cycle_id="action-a",
        coord=[2, 0, 0],
        type="concept",
        payload={"text": "retained fact"},
        source="memory.upgrade.test",
    )

    def worker():
        with sram.bind_episode("episode-a", pipeline_id=0):
            result = api.intake_memory(envelope)
        return result, sram.bound_episode_id()

    with ThreadPoolExecutor(max_workers=1) as pool:
        receipt, binding = pool.submit(worker).result(timeout=5)
    assert receipt.validation_status == "held"
    assert receipt.path is None
    assert binding is None
    assert sram.pending("episode-b") == []
    assert sram.pending("episode-a")[0].envelope.payload == {"text": "retained fact"}
    records = [
        path
        for lobe in validate_write.MEMORY_ROOT.glob("[0-9]*_*")
        for path in lobe.rglob("*.json")
    ]
    assert records == []
    assert sram.bound_episode_id() == "episode-b"


@pytest.mark.parametrize("fail", [False, True])
def test_binding_restores_the_previous_owner(fail):
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.open_bucket("episode-b", pipeline_id=1)
    try:
        with sram.bind_episode("episode-a", pipeline_id=0):
            assert sram.bound_episode_id() == "episode-a"
            sram.hold_turn("user", "owned by a")
            if fail:
                raise OSError("action failed")
    except OSError:
        assert fail
    assert sram.bound_episode_id() == "episode-b"
    assert [item.text for item in sram.pending("episode-a")] == ["owned by a"]
    assert sram.pending("episode-b") == []


def test_binding_rejects_the_wrong_physical_pipeline():
    sram.open_bucket("episode-a", pipeline_id=0)
    with pytest.raises(RuntimeError, match="pipeline"):
        with sram.bind_episode("episode-a", pipeline_id=1):
            pytest.fail("wrong pipeline was admitted")


def test_binding_never_opens_an_unknown_episode():
    with pytest.raises(RuntimeError, match="no episode SRAM bucket"):
        with sram.bind_episode("missing", pipeline_id=0):
            pytest.fail("missing episode was created")
    assert not sram.is_open()


def test_a_drain_cannot_rebind_to_another_episode():
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.hold_turn("user", "original")
    sram.open_bucket("episode-b", pipeline_id=1)

    def consolidate(items):
        with pytest.raises(RuntimeError, match="drain"):
            with sram.bind_episode("episode-b", pipeline_id=1):
                pytest.fail("drain ownership escaped")
        assert sram.bound_episode_id() == "episode-a"
        return {"turns": [item.text for item in items]}

    sram.flush("episode-a", consolidate=consolidate)
    assert sram.bound_episode_id() == "episode-b"
