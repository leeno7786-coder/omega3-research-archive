"""The canonical cognitive cascade as behaviour (plan Task 14, steps 2, 3 and 5).

one ingress gate → four dedicated semantic FIFOs → four bound physical pipelines
one admitted input → one episode
Global A → Meta A → AUTO → Meta B → Global B → Omega
Omega DONE → close; Omega PRESSURE → staged successor → new episode
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from tests.helpers.blueprint_driver import BlueprintDriver, browser_action, browser_decision

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

HOME = "https://huggingface.co/"


@pytest.fixture
def driver(monkeypatch, tmp_path):
    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    driver.browser.pages[HOME] = "Hugging Face – The AI community building the future."
    return driver


def test_open_hugging_face_is_one_run_one_navigate_and_done(driver):
    """The control case: one agent run reaches the page, Omega proves it, no successor."""
    driver.browser_actions.extend([browser_action("navigate", url=HOME), browser_action("done", summary="open")])
    driver.omega.append(browser_decision("DONE", synthesis="Hugging Face is open."))

    episode = driver.view(driver.operator_turn("open Hugging Face", route="BROWSER_AGENT", cycle_id="hf"))

    assert (episode.intent, episode.lane) == ("BROWSE", "browser")
    assert episode.workflow["objective"] == "open Hugging Face" and episode.run["status"] == "done"
    assert driver.browser.actions == [("navigate", HOME)]
    assert [g["operation"] for g in driver.generations] == [
        "episode_cognition",  # Chat durably routes before the Browser lane starts.
        "auto_browser_agent",
        "auto_browser_agent",
        "episode_cognition",
    ]
    assert episode.control["decision"] == "DONE" and episode.control["pressure"] is None
    assert driver.staged_successors() == []
    acknowledgement = driver.reply("hf")
    assert "queued" in acknowledgement["text"].casefold()
    assert "browser" in acknowledgement["text"].casefold()
    from pressure_loop.operator_chat_responses import get_lane_updates

    completion = get_lane_updates("hf")
    assert completion is not None and completion["status"] == "completed"
    assert completion["updates"][-1]["text"] == "Hugging Face is open."


def test_current_browser_state_comes_from_live_evidence_not_memory(driver, monkeypatch):
    """Memory says the last page was omega2ai.com; the browser shows huggingface.co."""
    driver.browser_actions.extend([browser_action("navigate", url=HOME), browser_action("done", summary="open")])
    driver.omega.append(browser_decision("DONE", synthesis="The browser is on huggingface.co."))

    # What the mind remembers: its previous browser action opened omega2ai.com.
    episode = driver.view(driver.operator_turn(
        "what page is the browser on right now? open Hugging Face and tell me",
        route="BROWSER_AGENT", cycle_id="live", previous=("BROWSER_AGENT", "open the browser to omega2ai.com")))

    browser_folds = [fold for fold in driver.folds if fold["pipeline_id"] == 1]
    assert len(browser_folds) == 1, "the Browser lane owns the live-state fold"
    (fold,) = browser_folds
    observation = fold["prompt"].split("browser_run", 1)[1].split("CROSS-LANE MAIL", 1)[0]
    assert "huggingface.co" in observation, "Omega judges current state from the live observation"
    assert "omega2ai.com" not in observation
    assert "Memory may describe prior browser state but can never prove what the browser shows now" in fold["system"]
    assert "live browser/tool observation" in fold["system"].split("persistent memory")[0]
    # The browser lane never recalls memory to answer current state.
    assert dict(episode.outputs.get("memory") or {}).get("memory_status", "skipped") == "skipped"
    acknowledgement = driver.reply("live")
    assert "queued" in acknowledgement["text"].casefold()
    assert "browser" in acknowledgement["text"].casefold()
    from pressure_loop.operator_chat_responses import get_lane_updates

    completion = get_lane_updates("live")
    assert completion is not None and completion["status"] == "completed"
    assert completion["updates"][-1]["text"] == "The browser is on huggingface.co."


def test_real_meta_b_chat_failure_does_not_block_handed_off_browser_lane(driver, monkeypatch):
    """A failed Chat fold leaves its durable Browser handoff runnable on Browser P1."""
    from core import pipeline_state
    from pressure_loop import cognition_queue as cq, pipeline_dispatch
    from pressure_loop.contributors import EpisodeIgnition, make_contributor
    from pressure_loop.cognition_queue import work_kind_for_contributor
    from tests.helpers.intent_router import stub_router

    pipeline_dispatch.shutdown_for_tests(wait=True)
    pipeline_state._CACHE.clear()
    stub_router(monkeypatch, "BROWSER_AGENT")
    driver.browser.pages[HOME] = "Hugging Face"
    driver.browser_actions.extend([
        browser_action("navigate", url=HOME), browser_action("done", summary="browser lane"),
    ])
    driver.omega.append(browser_decision("DONE", synthesis="The Browser lane completed."))

    import meta_loop.meta_b as meta_b

    real_meta_b = meta_b.run_meta_b_front

    def fail_only_pipeline_zero(episode):
        if episode.pipeline_id == 0:
            raise RuntimeError("Meta B failed on Chat P0")
        return real_meta_b(episode)

    monkeypatch.setattr(meta_b, "run_meta_b_front", fail_only_pipeline_zero)

    def dispatch(leased, pipeline_id: int, cycle_id: str) -> None:
        ignition = EpisodeIgnition(
            pipeline_id=pipeline_id,
            work_kind=work_kind_for_contributor(leased),
            episode_id=f"isolation-{cycle_id}",
            aggregate_pressure=1.0,
            opened_by=leased.type,
            dominant_contributor=leased,
            contributors=(leased,),
            trajectory_snapshot=None,
        )
        pipeline_dispatch.dispatch_cascade(
            driver.store,
            ignition,
            leased,
            pipeline_id=pipeline_id,
            work_kind=ignition.work_kind,
            on_not_admitted=lambda: cq.release_input_lease(pipeline_id),
        )

    try:
        chat = make_contributor("operator", "operator_query", 1.0, {
            "cycle_id": "meta-b-fails",
            "message": "open Hugging Face and report the result",
            "chat_bubble_waiting": True,
            "directive_intent": "RESPOND",
        })
        assert cq.enqueue_input(chat, operator_activity=False)
        leased_chat = cq.lease_next_input(0)
        assert leased_chat is not None
        dispatch(leased_chat, 0, "meta-b-fails")
        pipeline_dispatch._FUTURES[0].result(timeout=30)
        assert pipeline_dispatch.pipeline_faults().keys() == {0}
        assert pipeline_state.read_envelope(0).state == "FAILED"
        failed_body = driver.store.last_closed_body()
        assert failed_body is not None
        failed_meta_b = next(
            row for row in failed_body["phase_trail"] if row["phase"] == "meta_b"
        )
        assert failed_meta_b["status"] == "failed"
        assert "Meta B failed on Chat P0" in failed_meta_b["cause"]
        assert driver.reply("meta-b-fails") is not None

        browser = cq.lease_next_input(1)
        assert browser is not None and browser.metadata["lane"] == "browser"
        dispatch(browser, 1, "browser-after-chat-failure")
        pipeline_dispatch._FUTURES[1].result(timeout=30)
        assert pipeline_dispatch.pipeline_faults().keys() == {0}
        assert pipeline_state.read_envelope(1).state != "FAILED"
        assert driver.browser.actions == [("navigate", HOME)]
    finally:
        pipeline_dispatch.shutdown_for_tests(wait=True)
        pipeline_state._CACHE.clear()


def test_four_dedicated_lanes_run_at_once_and_chat_waits_for_its_own_lane(monkeypatch):
    """Four lanes overlap; the second Chat turn waits for Chat even when Agent frees."""
    from core.shared_state import SharedState
    from pressure_loop import cognition_queue as cq, loop, pipeline_dispatch, pressure
    from pressure_loop.contributors import make_contributor
    from pressure_loop.episode import EpisodeStore

    pipeline_dispatch.shutdown_for_tests(wait=True)
    store = EpisodeStore()
    runtime = SimpleNamespace(episode_store=store, shared_state=SharedState())
    entered: "queue.Queue[tuple[int, str]]" = queue.Queue()
    releases: dict[str, threading.Event] = {}
    kinds = [
        ("omega", "browser_work", {"lane": "browser", "arrival_seq": 1, "cycle_id": "browser-turn"}),
        ("omega", "agent_continuation", {"arrival_seq": 2, "cycle_id": "agent-turn"}),
        ("operator", "operator_query", {"arrival_seq": 3, "cycle_id": "chat-turn", "message": "hello omega"}),
        ("omega", "window_reflection_ready", {"arrival_seq": 4, "cycle_id": "window-1", "window_id": "win-1",
                                              "episode_count": 10}),
        ("operator", "operator_query", {"arrival_seq": 5, "cycle_id": "fifth-turn", "message": "and one more"}),
    ]
    monkeypatch.setattr(pressure, "gather_contributors", lambda *_a, **_k: [])

    def ignite(episode_store, ignition):
        pid = ignition.pipeline_id
        contributor = ignition.dominant_contributor
        cycle = contributor.metadata["cycle_id"]
        episode = episode_store.open(opened_by=ignition.opened_by, pipeline_id=pid, work_kind=ignition.work_kind)
        assert cq.acknowledge_input(contributor, pid)
        entered.put((pid, cycle))
        assert releases.setdefault(cycle, threading.Event()).wait(5)
        episode_store.close()
        return episode

    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    for source, kind, metadata in kinds:
        releases.setdefault(metadata["cycle_id"], threading.Event())
        assert cq.enqueue_input(make_contributor(source, kind, 1.0, metadata), operator_activity=False)
    try:
        assert loop._run_pressure_tick_body(runtime)
        started = [entered.get(timeout=4) for _ in range(4)]
        assert dict(started) == {
            0: "chat-turn",
            1: "browser-turn",
            2: "agent-turn",
            3: "window-1",
        }
        active = store.active_episodes()
        assert len(active) == 4
        assert sorted(ep.work_kind for ep in active) == [
            "agent_continuation", "autonomous", "operator_turn", "reflection",
        ]
        # A second Chat turn stays queued while every lane is occupied.
        assert cq.queue_depth() == 1

        # Agent frees, but its pipeline cannot borrow the waiting Chat turn.
        releases["agent-turn"].set()
        deadline = time.monotonic() + 2
        while 2 not in pipeline_dispatch.free_pipeline_ids() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not loop._run_pressure_tick_body(runtime)
        assert cq.queue_depth() == 1

        # Once Chat itself frees, the waiting turn opens on P0.
        releases["chat-turn"].set()
        deadline = time.monotonic() + 2
        while 0 not in pipeline_dispatch.free_pipeline_ids() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert loop._run_pressure_tick_body(runtime)
        assert entered.get(timeout=4) == (0, "fifth-turn")
        assert cq.queue_depth() == 0
    finally:
        for event in releases.values():
            event.set()
        pipeline_dispatch.shutdown_for_tests(wait=True)
