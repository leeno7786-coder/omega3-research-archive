"""Intake must have an explicit or thread-bound physical episode owner."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from memory import episode_sram as sram


def _hold(kind):
    if kind == "perception":
        return sram.hold_perception("A source-backed observation.")
    if kind == "turn":
        return sram.hold_turn("user", "A conversation turn.")
    envelope = SimpleNamespace(id="record-a", type="concept", payload={})
    return sram.hold_memory(envelope, propose=False, bypass_guardian=True, skip_embed=False)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("kind", ["perception", "turn", "memory"])
def test_unbound_worker_cannot_choose_an_open_episode(kind, count):
    for pipeline in range(count):
        sram.open_bucket(f"episode-{pipeline}", pipeline_id=pipeline)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match="bound|binding|owner"):
            pool.submit(_hold, kind).result(timeout=5)
    for pipeline in range(count):
        assert sram.pending(f"episode-{pipeline}") == []


def test_explicit_episode_is_preserved_from_an_unbound_worker():
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.open_bucket("episode-b", pipeline_id=1)
    envelope = SimpleNamespace(id="record-a", type="concept", payload={"text": "fact"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        ref = pool.submit(
            sram.hold_memory,
            envelope,
            propose=False,
            bypass_guardian=True,
            skip_embed=False,
            episode_id="episode-a",
        ).result(timeout=5)
    assert ref == "sram:episode-a:0"
    assert sram.pending("episode-a")[0].envelope.payload == {"text": "fact"}
    assert sram.pending("episode-b") == []


@pytest.mark.parametrize("episode_id", ["", "   "])
def test_empty_episode_id_is_rejected(episode_id):
    with pytest.raises(ValueError, match="episode"):
        sram.open_bucket(episode_id, pipeline_id=0)
    assert not sram.is_open()


def test_episode_identity_cannot_move_to_another_pipeline():
    sram.open_bucket("episode-a", pipeline_id=0)
    with pytest.raises(RuntimeError, match="pipeline"):
        sram.open_bucket("episode-a", pipeline_id=1)
    assert sram.bound_episode_id() == "episode-a"


def test_stale_worker_binding_cannot_adopt_another_episode():
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(sram.open_bucket, "episode-old", pipeline_id=0).result(timeout=5)
        sram.clear_bucket("episode-old")
        sram.open_bucket("episode-other", pipeline_id=1)
        with pytest.raises(RuntimeError, match="bound|binding|owner"):
            pool.submit(sram.hold_turn, "user", "stale worker").result(timeout=5)
    assert sram.pending("episode-other") == []


@pytest.mark.parametrize("operation", ["flush", "close_bucket", "clear_bucket"])
def test_unbound_lifecycle_cannot_choose_the_newest_bucket(operation):
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.hold_turn("user", "protected")
    sram.open_bucket("episode-b", pipeline_id=1)
    kwargs = {} if operation == "clear_bucket" else {"consolidate": lambda items: {}}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match="bound|binding|owner"):
            pool.submit(getattr(sram, operation), **kwargs).result(timeout=5)
    assert sram.is_open("episode-b")
    assert [item.text for item in sram.pending("episode-a")] == ["protected"]
