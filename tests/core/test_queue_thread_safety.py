"""Thread safety for durable operator queue + system message store."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from pressure_loop.contributors import from_operator_message


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    import pressure_loop.cognition_queue as queue_mod
    import pressure_loop.system_messages as sm_mod

    monkeypatch.setattr(
        queue_mod,
        "_queue_path",
        lambda: tmp_path / "operator_input_queue.json",
    )
    queue_mod._reset_cache_for_tests()
    monkeypatch.setattr(
        sm_mod,
        "_store_path",
        lambda: tmp_path / "system_messages.json",
    )
    sm_mod._reset_cache_for_tests()
    yield


def _msg(text: str, cycle_id: str):
    return from_operator_message(
        {
            "message": text,
            "ingress_class": "epistemic_request",
            "weight": 1.0,
            "cycle_id": cycle_id,
        }
    )


def test_concurrent_enqueue_deduped_once():
    from pressure_loop.cognition_queue import enqueue_input, peek_inputs

    contributor = _msg("race me", "c-race")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: enqueue_input(contributor, operator_activity=False), range(32)))

    assert sum(1 for ok in results if ok) == 1
    assert len(peek_inputs()) == 1


def test_concurrent_system_message_respond_single_enqueue():
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.system_messages import append_system_query, respond_to_message

    rec = append_system_query(
        text="Concurrent reply?",
        episode_id="ep-race",
        opened_by="operator_priority",
    )

    def _respond(_: int):
        return respond_to_message(rec["id"], "my answer")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_respond, range(8)))

    assert sum(1 for o in outcomes if o is not None) == 8
    assert len(peek_inputs()) == 1
