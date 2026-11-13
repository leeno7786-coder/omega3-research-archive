"""Durable admission keeps four semantic lane FIFOs independent."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from pressure_loop import cognition_queue as cq
from pressure_loop.contributors import make_contributor
from core.pipelines import lane_for_contributor, work_kind_for_contributor
import cognition_chain
from pressure_loop import loop, pipeline_dispatch, pressure


@dataclass
class _Ignition:
    dominant_contributor: object
    pipeline_id: int = 0
    work_kind: str = ""


def _item(lane: str, sequence: int, *, source: str = "omega"):
    return make_contributor(
        source,
        f"{lane}_work",
        1.0,
        {
            "arrival_seq": sequence,
            "cycle_id": f"{lane}-{sequence}",
            "lane": lane,
        },
    )


def test_machine_work_without_a_semantic_lane_is_rejected():
    unclassified = make_contributor("omega", "unknown_machine_work", 1.0,
                                    {"cycle_id": "machine-cycle"})
    with pytest.raises(ValueError, match="has no semantic lane"):
        lane_for_contributor(unclassified)


@pytest.mark.parametrize("kind,expected", [
    ("agent_motor_dispatch", "agent"),
    ("agent_continuation", "agent"),
    ("agent_boot_restore", "agent"),
    ("window_reflection_ready", "window"),
])
@pytest.mark.parametrize("identity", ["lane", "omega_lane", "restored_lane"])
def test_fixed_contributor_types_reject_a_conflicting_lane(kind, expected, identity):
    metadata = {"cycle_id": "fixed-owner"}
    if identity != "restored_lane":
        metadata[identity] = "chat"
    contributor = make_contributor("omega", kind, 1.0, metadata)
    if identity == "restored_lane":
        object.__setattr__(contributor, "lane", "chat")

    with pytest.raises(ValueError, match="fixed.*lane"):
        lane_for_contributor(contributor)
    with pytest.raises(ValueError, match="fixed.*lane"):
        cq.enqueue_input(contributor, operator_activity=False)
    assert cq.peek_inputs() == ()
    assert lane_for_contributor(make_contributor("omega", kind, 1.0, {"lane": expected})) == expected


def test_fixed_contributor_rejects_conflicting_secondary_identity():
    contributor = make_contributor(
        "omega", "agent_continuation", 1.0,
        {"lane": "agent", "omega_lane": "browser", "cycle_id": "conflict"},
    )
    with pytest.raises(ValueError, match="fixed.*lane"):
        cq.enqueue_input(contributor, operator_activity=False)
    assert cq.peek_inputs() == ()


def test_specialist_lane_handoffs_are_paced_as_operator_work():
    handoff = make_contributor("omega", "lane_work_dispatch", 1.0,
                               {"lane": "browser", "cycle_id": "browser-cycle"})
    work_kind = work_kind_for_contributor(handoff)
    assert work_kind == "operator_lane_work"
    assert cognition_chain._is_operator_work(
        SimpleNamespace(work_kind=work_kind, opened_by="lane_work_dispatch"))


def test_each_pipeline_leases_only_its_lane_and_preserves_lane_fifo():
    chat_first = _item("chat", 1, source="operator")
    browser_first = _item("browser", 2)
    chat_second = _item("chat", 3, source="operator")
    agent_first = _item("agent", 4)
    browser_second = _item("browser", 5)
    window_first = _item("window", 6)
    for item in (
        chat_first,
        browser_first,
        chat_second,
        agent_first,
        browser_second,
        window_first,
    ):
        cq.enqueue_input(item, operator_activity=False)

    # A global FIFO would lease chat_first to pipeline 1 and stall Browser.
    browser_claim = cq.lease_next_input(1)
    chat_claim = cq.lease_next_input(0)
    agent_claim = cq.lease_next_input(2)
    window_claim = cq.lease_next_input(3)
    assert browser_claim.metadata["cycle_id"] == "browser-2"
    assert chat_claim.metadata["cycle_id"] == "chat-1"
    assert agent_claim.metadata["cycle_id"] == "agent-4"
    assert window_claim.metadata["cycle_id"] == "window-6"

    # A lane remains serialized until its current owner acknowledges.
    assert cq.lease_next_input(1) is None
    assert cq.acknowledge_input(browser_claim, 1) is True
    next_browser = cq.lease_next_input(1)
    assert next_browser.metadata["cycle_id"] == "browser-5"
    assert cq.lease_next_input(0) is None


def test_an_incomplete_chat_arrival_does_not_block_browser_admission():
    cq.reserve_operator_arrival("chat-cycle", arrival_seq=1)
    browser = _item("browser", 2)
    queued_chat = _item("chat", 3, source="operator")
    cq.enqueue_input(browser, operator_activity=False)
    cq.enqueue_input(queued_chat, operator_activity=False)

    browser_claim = cq.lease_next_input(1)
    assert browser_claim.metadata["cycle_id"] == "browser-2"
    assert cq.lease_next_input(0) is None


def test_pressure_tick_continues_when_an_earlier_lane_is_empty(monkeypatch):
    browser = _item("browser", 1)
    cq.enqueue_input(browser, operator_activity=False)
    dispatched = []
    monkeypatch.setattr(pressure, "gather_contributors", lambda *_a, **_k: [])
    monkeypatch.setattr(pressure, "retry_unresolved_stops", lambda: None)
    monkeypatch.setattr(pressure, "settle_retired_jobs", lambda: None)
    monkeypatch.setattr(
        pressure,
        "evaluate",
        lambda rows, **_k: _Ignition(dominant_contributor=rows[0]),
    )
    monkeypatch.setattr(pipeline_dispatch, "raise_pending_fatal", lambda: None)
    monkeypatch.setattr(pipeline_dispatch, "free_pipeline_ids", lambda: (0, 1, 2, 3))

    def dispatch(_store, _ignition, contributor, *, pipeline_id, work_kind, on_not_admitted):
        dispatched.append((pipeline_id, contributor.metadata["cycle_id"]))

    monkeypatch.setattr(pipeline_dispatch, "dispatch_cascade", dispatch)
    runtime = SimpleNamespace(episode_store=None, shared_state=object())

    assert loop._run_pressure_tick_body(runtime) is True
    assert dispatched == [(1, "browser-1")]


def test_a_quarantined_lane_does_not_abort_admission_in_other_lanes(monkeypatch):
    browser = _item("browser", 1)
    agent = _item("agent", 2)
    cq.enqueue_input(browser, operator_activity=False)
    cq.enqueue_input(agent, operator_activity=False)
    fault = RuntimeError("browser lane quarantined")
    dispatched = []
    monkeypatch.setattr(pressure, "gather_contributors", lambda *_a, **_k: [])
    monkeypatch.setattr(pressure, "retry_unresolved_stops", lambda: None)
    monkeypatch.setattr(pressure, "settle_retired_jobs", lambda: None)
    monkeypatch.setattr(pressure, "evaluate",
                        lambda rows, **_k: _Ignition(dominant_contributor=rows[0]))
    monkeypatch.setattr(pipeline_dispatch, "raise_pending_fatal", lambda: None)
    monkeypatch.setattr(pipeline_dispatch, "free_pipeline_ids", lambda: (1, 2))
    monkeypatch.setattr(pipeline_dispatch, "pipeline_faults", lambda: {1: fault})

    def dispatch(_store, _ignition, contributor, *, pipeline_id, work_kind, on_not_admitted):
        if pipeline_id == 1:
            on_not_admitted()
            raise fault
        assert cq.acknowledge_input(contributor, pipeline_id)
        dispatched.append((pipeline_id, contributor.metadata["cycle_id"]))

    monkeypatch.setattr(pipeline_dispatch, "dispatch_cascade", dispatch)
    runtime = SimpleNamespace(episode_store=None, shared_state=object())

    assert loop._run_pressure_tick_body(runtime) is True
    assert dispatched == [(2, "agent-2")]
    assert [row.metadata["cycle_id"] for row in cq.peek_inputs()] == ["browser-1"]


def test_unjudgeable_specialist_stop_fails_loudly_without_dropping_the_lane_head(monkeypatch):
    from core.required_substrate import RequiredSubstrateError

    handoff = make_contributor(
        "omega",
        "lane_work_dispatch",
        1.0,
        {
            "arrival_seq": 1,
            "cycle_id": "browser-handoff",
            "original_cycle_id": "browser-origin",
            "handoff_id": "handoff-1",
            "lane": "browser",
        },
    )
    assert cq.enqueue_input(handoff, operator_activity=False)
    now = {"value": 10.0}
    monkeypatch.setattr(
        "pressure_loop.omega_successor.cancellation_state",
        lambda _metadata: ("unknown", "stop ledger unreadable"),
    )
    monkeypatch.setattr(loop, "_clock", lambda: now["value"])
    monkeypatch.setattr(loop, "UNJUDGEABLE_HEAD_LIMIT_S", 120.0)
    monkeypatch.setattr(loop, "_UNJUDGEABLE_SINCE", {})

    assert loop._lease_runnable_input(1) == (None, True)
    now["value"] = 131.0
    with pytest.raises(RequiredSubstrateError, match="cancellation authority cannot be read"):
        loop._lease_runnable_input(1)

    assert [row.metadata["handoff_id"] for row in cq.peek_inputs()] == ["handoff-1"]
