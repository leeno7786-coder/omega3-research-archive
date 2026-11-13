import threading

import pytest

from pressure_loop import cognition_queue as cq
from pressure_loop.contributors import make_contributor


def _turn(seq, cycle):
    return make_contributor("operator", "operator_query", 1,
                            {"arrival_seq": seq, "cycle_id": cycle})


def test_reservation_publication_error_does_not_orphan_intake(monkeypatch):
    persist = cq._persist_mutation
    calls = 0
    def published_then_failed():
        nonlocal calls
        calls += 1
        persist()
        if calls == 1:
            raise OSError("post publication")
    monkeypatch.setattr(cq, "_persist_mutation", published_then_failed)
    with pytest.raises(OSError, match="post publication"):
        cq.reserve_operator_arrival("failed")
    cq.enqueue_input(_turn(20, "later"), operator_activity=False)
    assert cq.lease_next_input(0).metadata["cycle_id"] == "later"


def test_screen_failure_remains_primary_when_cleanup_also_fails(monkeypatch):
    from pressure_loop import operator_chat
    primary = ValueError("screen failed")
    def screened(**kwargs):
        raise primary
    def cleanup(*args, **kwargs):
        raise OSError("cleanup failed")
    monkeypatch.setattr(operator_chat, "_screen_and_enqueue_operator_chat", screened)
    monkeypatch.setattr(operator_chat, "abandon_arrival_reservation", cleanup)
    with pytest.raises(ValueError) as failure:
        operator_chat.handle_operator_chat("one", cycle_id="one")
    assert failure.value is primary
    assert "cleanup failed" in " ".join(primary.__notes__)


def test_delayed_earlier_screen_holds_all_physical_workers(monkeypatch):
    from pressure_loop import operator_chat
    first = cq.reserve_operator_arrival("first")
    second = cq.reserve_operator_arrival("second")
    entered, release = threading.Event(), threading.Event()
    def screened(**kwargs):
        if kwargs["cycle_id"] == "first":
            entered.set()
            assert release.wait(3)
        cq.enqueue_input(_turn(kwargs["arrival_seq"], kwargs["cycle_id"]), operator_activity=False)
        return {"ok": True, "queued": True}
    monkeypatch.setattr(operator_chat, "_screen_and_enqueue_operator_chat", screened)
    thread = threading.Thread(target=lambda: operator_chat.handle_operator_chat(
        text="one", cycle_id="first", arrival_seq=first))
    thread.start()
    try:
        assert entered.wait(2)
        operator_chat.handle_operator_chat(text="two", cycle_id="second", arrival_seq=second)
        assert all(cq.lease_next_input(pid) is None for pid in range(4))
    finally:
        release.set()
        thread.join(3)
    first_row = cq.lease_next_input(0)
    assert first_row.metadata["cycle_id"] == "first"
    assert cq.acknowledge_input(first_row, 0)
    assert cq.lease_next_input(0).metadata["cycle_id"] == "second"


@pytest.mark.parametrize("raises", [False, True])
def test_rejected_screen_settles_admission_barrier(monkeypatch, raises):
    from pressure_loop import operator_chat
    first = cq.reserve_operator_arrival("first")
    cq.enqueue_input(_turn(first + 1, "later"), operator_activity=False)
    def screened(**kwargs):
        if raises:
            raise ValueError("screen failed")
        return {"ok": False, "error": "guardian_withheld"}
    monkeypatch.setattr(operator_chat, "_screen_and_enqueue_operator_chat", screened)
    if raises:
        with pytest.raises(ValueError, match="screen failed"):
            operator_chat.handle_operator_chat(text="one", cycle_id="first", arrival_seq=first)
    else:
        assert not operator_chat.handle_operator_chat(text="one", cycle_id="first", arrival_seq=first)["ok"]
    assert cq.lease_next_input(0).metadata["cycle_id"] == "later"


def test_reservation_cannot_be_consumed_by_different_cycle():
    seq = cq.reserve_operator_arrival("first")
    with pytest.raises(ValueError, match="conflict"):
        cq.enqueue_input(_turn(seq, "impostor"), operator_activity=False)
    cq._reset_cache_for_tests()
    cq.enqueue_input(_turn(seq + 1, "later"), operator_activity=False)
    assert cq.lease_next_input(0) is None


def test_canceled_screen_cannot_publish_after_later_work_was_released():
    seq = cq.reserve_operator_arrival("first")
    cq.abandon_arrival_reservation(seq, cycle_id="first")
    with pytest.raises(ValueError, match="unreserved"):
        cq.enqueue_input(_turn(seq, "first"), operator_activity=False, admission_cycle_id="first")
    assert cq.queue_depth() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["allow", "reject", "cancel"])
async def test_http_selector_preserves_arrival_until_settlement(monkeypatch, outcome):
    import asyncio
    import uuid
    import hud.server as server
    from pressure_loop import operator_chat
    app = server.create_app()
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/chat")
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    entered, release = asyncio.Event(), asyncio.Event()
    hops = 0
    async def thread_hop(call):
        nonlocal hops
        hops += 1
        if hops == 1:
            entered.set()
            await release.wait()
            if outcome == "reject":
                raise ValueError("selector rejected")
        return call()
    monkeypatch.setattr(server, "run_in_threadpool", thread_hop)
    monkeypatch.setattr(server, "_chat_plugin_selector", lambda *_: None)
    monkeypatch.setattr(server.hud_chat_history, "record_chat_result", lambda *_a, **_k: None)
    def screened(**kwargs):
        cq.enqueue_input(_turn(kwargs["arrival_seq"], kwargs["cycle_id"]), operator_activity=False)
        return {"ok": True, "queued": True, "cycle_id": kwargs["cycle_id"]}
    monkeypatch.setattr(operator_chat, "_screen_and_enqueue_operator_chat", screened)
    earlier = asyncio.create_task(endpoint({"text": "first", "cycle_id": first}))
    await asyncio.wait_for(entered.wait(), 2)
    await endpoint({"text": "second", "cycle_id": second})
    assert all(cq.lease_next_input(pid) is None for pid in range(4))
    if outcome == "cancel":
        earlier.cancel()
        with pytest.raises(asyncio.CancelledError):
            await earlier
    else:
        release.set()
        await earlier
    head = cq.lease_next_input(0)
    assert head.metadata["cycle_id"] == (first if outcome == "allow" else second)


@pytest.mark.asyncio
async def test_http_plugin_selector_rejection_keeps_conflict_status_and_settles(monkeypatch):
    import hud.server as server
    from fastapi import HTTPException
    app = server.create_app()
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/chat")
    def selector(*args):
        raise server.PluginDirectoryError("plugin_chat_selector_invalid")
    monkeypatch.setattr(server, "_chat_plugin_selector", selector)
    with pytest.raises(HTTPException) as failure:
        await endpoint({"text": "first"})
    assert failure.value.status_code == 409
    cq.enqueue_input(_turn(20, "later"), operator_activity=False)
    assert cq.lease_next_input(0).metadata["cycle_id"] == "later"
