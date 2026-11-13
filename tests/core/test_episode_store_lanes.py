"""Physical episode ownership, SRAM collisions, and thread-bound telemetry."""

from __future__ import annotations

import threading

import pytest

from memory import episode_sram
from pressure_loop.episode import EpisodeStore


@pytest.fixture(autouse=True)
def _fresh_sram():
    episode_sram.reset()
    yield
    episode_sram.reset()


# ── store: per-lane carriers ─────────────────────────────────────────────────


def test_cross_lane_opens_hold_concurrent_carriers():
    store = EpisodeStore()
    window_ep = store.open(opened_by="window_reflection_ready", pipeline_id=3, work_kind="reflection")
    chat_ep = store.open(opened_by="operator_intent", pipeline_id=0, work_kind="operator_turn")

    assert window_ep.pipeline_id == 3
    assert chat_ep.pipeline_id == 0
    assert store.current(pipeline_id=3) is window_ep
    assert store.current(pipeline_id=0) is chat_ep
    assert store.active_episodes() == (window_ep, chat_ep)


def test_same_lane_second_open_fails_closed():
    store = EpisodeStore()
    first = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn")

    with pytest.raises(RuntimeError, match="pipeline 0"):
        store.open(opened_by="operator_command", pipeline_id=0, work_kind="operator_turn")

    assert store.current(pipeline_id=0) is first


def test_cascade_thread_binding_resolves_own_episode():
    """Two cascade threads, one episode each — every ambient read mid-cascade
    resolves to the calling thread's own carrier, never the other lane's."""
    store = EpisodeStore()
    opened = threading.Barrier(2)
    seen: dict[str, object] = {}
    errors: list[BaseException] = []

    def cascade(opened_by: str, key: str):
        try:
            pipeline_id = 0 if key == "chat" else 3
            work_kind = "operator_turn" if key == "chat" else "reflection"
            ep = store.open(opened_by=opened_by, pipeline_id=pipeline_id, work_kind=work_kind)
            opened.wait(timeout=5)
            # Both lanes are live now; this thread must still see ITS episode.
            seen[key] = store.current()
            assert store.current() is ep
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)

    chat = threading.Thread(target=cascade, args=("operator_query", "chat"))
    window = threading.Thread(target=cascade, args=("window_reflection_ready", "window"))
    chat.start()
    window.start()
    chat.join()
    window.join()

    assert not errors
    assert getattr(seen["chat"], "pipeline_id", None) == 0
    assert getattr(seen["window"], "pipeline_id", None) == 3


def test_unbound_observer_still_sees_cognition_in_flight():
    """Unbound observers use explicit physical lookup or active_episodes()."""
    store = EpisodeStore()
    assert store.current() is None

    store.open(opened_by="window_reflection_ready", pipeline_id=3, work_kind="reflection")
    observed: list[object] = []
    t = threading.Thread(target=lambda: observed.append(store.current()))
    t.start()
    t.join()
    assert observed[0] is None  # no ambient borrowing

    store.open(opened_by="operator_intent", pipeline_id=0, work_kind="operator_turn")
    t2 = threading.Thread(target=lambda: observed.append(store.current()))
    t2.start()
    t2.join()
    assert observed[1] is None


def test_unbound_close_with_multiple_lanes_is_ambiguous():
    store = EpisodeStore()
    store.open(opened_by="window_reflection_ready", pipeline_id=3, work_kind="reflection")
    store.open(opened_by="operator_intent", pipeline_id=0, work_kind="operator_turn")

    errors: list[BaseException] = []

    def unbound_close():
        try:
            store.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=unbound_close)
    t.start()
    t.join()

    assert len(errors) == 1
    assert "owning cascade thread" in str(errors[0])
    assert len(store.active_episodes()) == 2  # nothing was erased


def test_close_clears_only_the_owning_lane(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )
    store = EpisodeStore(reflection_window_path=tmp_path / "reflection_window.json")
    window_ep = store.open(opened_by="window_reflection_ready", pipeline_id=3, work_kind="reflection")
    window_ep.global_ = {"action_goal": {"intent": "REFLECT"}, "outcome": {"status": "success"}}
    window_ep.meta = {}
    errors: list[BaseException] = []

    def chat_cascade():
        try:
            ep = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn")
            ep.global_ = {
                "action_goal": {"intent": "RESPOND"},
                "outcome": {"status": "success"},
            }
            ep.meta = {}
            closed = store.close()
            assert closed is ep
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=chat_cascade)
    t.start()
    t.join()

    assert not errors
    assert store.current(pipeline_id=0) is None
    assert store.current(pipeline_id=3) is window_ep  # untouched by the chat close

    closed_window = store.close()  # this thread is bound to the reflection episode
    assert closed_window is window_ep
    assert store.active_episodes() == ()


# ── SRAM: per-episode buckets, lane-scoped stranding ─────────────────────────


def test_sram_buckets_coexist_across_lanes_and_target_by_id():
    episode_sram.open_bucket("ep-chat", pipeline_id=0)
    episode_sram.open_bucket("ep-window", pipeline_id=3)

    assert episode_sram.hold_turn_for_episode("ep-chat", "user", "hello there")
    assert episode_sram.hold_turn_for_episode("ep-window", "assistant", "inner note")

    chat_items = episode_sram.pending("ep-chat")
    window_items = episode_sram.pending("ep-window")
    assert [item.text for item in chat_items] == ["hello there"]
    assert [item.text for item in window_items] == ["inner note"]


def test_sram_same_lane_stranded_bucket_refuses_loudly():
    episode_sram.open_bucket("ep-chat-1", pipeline_id=0)
    episode_sram.hold_turn_for_episode("ep-chat-1", "user", "unfinished intake")

    with pytest.raises(RuntimeError, match="still has pending intake"):
        episode_sram.open_bucket("ep-chat-2", pipeline_id=0)

    # The other lane is a live concurrent episode, never blocked by chat's strand.
    episode_sram.open_bucket("ep-window-1", pipeline_id=3)
    assert episode_sram.is_open("ep-window-1")
    assert episode_sram.pending("ep-chat-1")  # strand stays intact for diagnosis


def test_sram_thread_binding_routes_unqualified_holds():
    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def cascade(ep_id: str, pipeline_id: int, text: str):
        try:
            episode_sram.open_bucket(ep_id, pipeline_id=pipeline_id)
            ready.wait(timeout=5)
            # Both buckets exist now; the unqualified hold must hit OURS.
            episode_sram.hold_turn("user", text)
            assert episode_sram.current_episode_id() == ep_id
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    a = threading.Thread(target=cascade, args=("ep-a", 0, "chat words"))
    b = threading.Thread(target=cascade, args=("ep-b", 3, "window words"))
    a.start()
    b.start()
    a.join()
    b.join()

    assert not errors
    assert [i.text for i in episode_sram.pending("ep-a")] == ["chat words"]
    assert [i.text for i in episode_sram.pending("ep-b")] == ["window words"]


def test_sram_flush_and_close_touch_only_the_named_bucket():
    episode_sram.open_bucket("ep-chat", pipeline_id=0)
    episode_sram.open_bucket("ep-window", pipeline_id=3)
    episode_sram.hold_turn_for_episode("ep-chat", "user", "chat turn")
    episode_sram.hold_turn_for_episode("ep-window", "user", "window turn")

    drained: list[str] = []

    def consolidate(items):
        drained.extend(item.text for item in items)
        return {"enabled": True, "consolidated": len(items)}

    episode_sram.close_bucket("ep-chat", consolidate=consolidate)
    assert drained == ["chat turn"]
    assert not episode_sram.is_open("ep-chat")
    assert episode_sram.is_open("ep-window")
    assert [i.text for i in episode_sram.pending("ep-window")] == ["window turn"]

    result = episode_sram.flush("ep-gone")
    assert result["skipped"] == [{"text": "", "reason": "episode_mismatch"}]
    assert [i.text for i in episode_sram.pending("ep-window")] == ["window turn"]


# ── telemetry: per-thread episode stamps ─────────────────────────────────────


def test_telemetry_episode_id_is_thread_local():
    from guardian.telemetry import current_episode_id, set_episode_id

    ready = threading.Barrier(2)
    stamps: dict[str, str] = {}
    errors: list[BaseException] = []

    def cascade(ep_id: str):
        try:
            set_episode_id(ep_id)
            ready.wait(timeout=5)
            stamps[ep_id] = current_episode_id()
            set_episode_id(None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    a = threading.Thread(target=cascade, args=("ep-lane-chat",))
    b = threading.Thread(target=cascade, args=("ep-lane-meta",))
    a.start()
    b.start()
    a.join()
    b.join()

    assert not errors
    assert stamps == {"ep-lane-chat": "ep-lane-chat", "ep-lane-meta": "ep-lane-meta"}
    # This thread never set one — it stamps honestly.
    assert current_episode_id() == "no-episode"
