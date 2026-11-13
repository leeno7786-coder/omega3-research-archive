"""A window episode runs Global A → Meta A → AUTO → Meta B → Global B → Omega.

Its reflection is Omega's window-lane fold over Global B's locked-window view; no
window module owns a second inference topology.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cognition_chain as chain
from pressure_loop import window_lane
from pressure_loop.contributors import EpisodeIgnition
from pressure_loop.episode import EpisodeStore
from pressure_loop import reflection_window
from pressure_loop.reflection_window import WINDOW_SIZE, ReflectionWindow

ROOT = Path(__file__).resolve().parents[2]


def _window(window_id: str) -> ReflectionWindow:
    return ReflectionWindow(
        window_id=window_id,
        started_at_episode="ep-0",
        trajectory_snapshot={"target_domain": "python"},
        episodes=[f"ep-{i}" for i in range(WINDOW_SIZE)],
        episode_bodies=[
            {
                "episode_id": f"ep-{i}",
                "intent": "LEARN",
                "action": "USE_TOOL",
                "outcome": {"status": "success"},
                "cascade_status": "completed",
            }
            for i in range(WINDOW_SIZE)
        ],
        status="locked",
        locked_at="2026-09-14T00:00:00Z",
    )


def _window_fold() -> dict:
    assessment = dict.fromkeys(
        ("who", "what", "when", "where", "why", "how"), "ten recorded LEARN episodes"
    )
    assessment.update(
        desired_state="steady python mastery",
        observed_state="ten successful lookups",
        gap="",
        evidence=["10 of 10 episodes completed"],
    )
    return {
        "control": {
            "assessment": assessment,
            "decision": "DONE",
            "pressure": None,
            "synthesis": "The window shows steady lookups.",
        },
        "reflection": {
            "summary": "Ten shallow lookups.",
            "reasoning": {
                "what": "answered questions",
                "why": "the operator asked",
                "identity": "prefers verified answers",
            },
            "emergent_observations": {
                "repeated_behaviors": ["reaches for recall first"],
                "stable_patterns": [],
            },
            "trajectory": {
                "decision": "continue",
                "target_domain": "python",
                "target_capability": "async",
                "reason": "the gap recurs",
            },
        },
        "trajectory_update": {
            "target_domain": "python",
            "target_capability": "async",
            "confidence": 0.6,
            "reason": "the gap recurs",
        },
    }


@pytest.fixture()
def model(monkeypatch):
    """Every runtime generate call, answered with a valid window fold unless queued otherwise."""
    seen: list[dict] = []
    replies: list[str] = []

    def generate(**kwargs):
        seen.append({"operation": kwargs.get("operation"), "prompt": str(kwargs.get("prompt") or "")})
        return {"text": replies.pop(0) if replies else json.dumps(_window_fold())}

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", generate)
    return SimpleNamespace(seen=seen, replies=replies)


@pytest.fixture()
def termini(monkeypatch):
    """Spy termini. Like the real ones, each DRAINS the closing episode's SRAM bucket
    (the window episode holds its own intake — since T11 the ``observed`` causal fact of
    the reflection it archived — and ``_close_episode_sram`` refuses a bucket left pending);
    what the drain would consolidate is recorded instead of written."""
    from memory import episode_sram

    windows: list[tuple] = []
    episodes: list[str] = []
    intake: list = []

    def drain(store):
        episode = store.current()
        if episode is not None and episode_sram.is_open(episode.episode_id):
            episode_sram.close_bucket(
                episode.episode_id,
                consolidate=lambda items: intake.extend(items) or {"enabled": True, "consolidated": len(items)},
            )

    def window_deepsleep(store, window, outcome, *, cascade_status="completed"):
        windows.append((window.window_id, dict(outcome), cascade_status))
        drain(store)
        store.close(cascade_status=cascade_status)

    def episode_deepsleep(store, episode, *, cascade_status):
        episodes.append(cascade_status)
        drain(store)
        store.close(cascade_status=cascade_status)

    monkeypatch.setattr("deepsleep_loop.loop.run_deepsleep_for_window", window_deepsleep)
    monkeypatch.setattr(chain, "_run_per_episode_terminus", episode_deepsleep)
    return SimpleNamespace(windows=windows, episodes=episodes, intake=intake)


def _observed_facts(termini):
    return [
        item.envelope.payload["enrichment"]["event_payload"]
        for item in termini.intake
        if item.kind == "memory"
        and dict(item.envelope.payload.get("enrichment") or {}).get("event_kind") == "causal_edge"
    ]


def install_stage_spies(monkeypatch, calls: list[str]) -> None:
    import auto_loop.auto
    import global_loop.loop
    import meta_loop.loop
    import meta_loop.meta_b

    for module, name, label in (
        (global_loop.loop, "run_global_a", "global_a"),
        (meta_loop.loop, "run_meta_a", "meta_a"),
        (auto_loop.auto, "run_auto_front", "auto"),
        (meta_loop.meta_b, "run_meta_b_front", "meta_b"),
        (global_loop.loop, "run_global_b", "global_b"),
        (chain, "_run_omega_episode_cognition", "omega"),
    ):
        real = getattr(module, name)

        def spy(*args, _real=real, _label=label, **kwargs):
            calls.append(_label)
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, name, spy)


def ignite_staged_window_episode(monkeypatch, window_id: str):
    """Stage a locked window, lease its synthesized pressure and ignite it for real."""
    from pressure_loop import cognition_queue, temporal_store

    window = _window(window_id)
    bodies = [dict(body) for body in window.episode_bodies]
    window_lane.stage_locked_window(window)

    def hydrate(staged: ReflectionWindow) -> ReflectionWindow:
        staged.episode_bodies = [dict(body) for body in bodies]
        return staged

    monkeypatch.setattr(temporal_store, "hydrate_reflection_window", hydrate)
    # WO-07 releases a cohort only once every member's sealed record is served. These
    # members are fixtures with no canonical records: keep the release rule and replace
    # only its membership reader, with this window's members marked served.
    import memory.episode_commit_view as commit_view

    served = set(window.episodes)

    def unpublished(episode_ids, *, member_refs=None):
        del member_refs
        members = tuple(dict.fromkeys(str(item) for item in episode_ids if str(item)))
        return tuple(episode_id for episode_id in members if episode_id not in served)

    monkeypatch.setattr(commit_view, "window_members_unpublished", unpublished)
    contributor = window_lane.pressure_contributor()
    assert contributor is not None
    cognition_queue.enqueue_input(contributor, operator_activity=False)
    contributor = cognition_queue.lease_next_input(3)
    ignition = EpisodeIgnition(
        pipeline_id=3,
        work_kind="reflection",
        episode_id=f"ig-{window_id}",
        aggregate_pressure=1.0,
        opened_by=contributor.type,
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )
    store = EpisodeStore()
    return store, chain.ignite_episode(store, ignition)


def test_window_episode_uses_global_meta_auto_meta_global_omega(monkeypatch, model, termini):
    calls: list[str] = []
    install_stage_spies(monkeypatch, calls)

    store, episode = ignite_staged_window_episode(monkeypatch, "win-canon")

    assert calls == ["global_a", "meta_a", "auto", "meta_b", "global_b", "omega"]
    decision = episode.global_["intent_decision"]
    assert (decision["intent"], decision["omega_lane"]) == ("REFLECT_WINDOW", "window")
    pack = episode.meta["execution_action_pack"]
    assert (pack["action"], pack["tool"], pack["memory_required"]) == (
        "WINDOW_REFLECTION",
        None,
        False,
    )
    assert episode.auto["outputs"]["window_reflection"] == {
        "window_id": "win-canon",
        "episode_count": WINDOW_SIZE,
        "status": "attached",
    }
    assert episode.global_["outcome"]["artifacts"]["window_view"]["window_id"] == "win-canon"
    # The whole window episode makes one inference: Omega's fold over Global B's view.
    assert [row["operation"] for row in model.seen] == ["episode_cognition"]
    assert '"window_id": "win-canon"' in model.seen[0]["prompt"]
    window_id, outcome, status = termini.windows[0]
    assert (window_id, outcome["reflection_source"], status) == ("win-canon", "model", "completed")
    assert outcome["trajectory_update"]["target_domain"] == "python"
    assert episode.omega["reflection"]["summary"] == "Ten shallow lookups."
    assert termini.episodes == []
    # The window episode hands its own intake to the window terminus: the fact that this
    # reflection OBSERVED the window it archived (T11), evidenced by the episode itself.
    [fact] = _observed_facts(termini)
    assert (fact["kind"], fact["source"], fact["target"]) == (
        "observed", f"episode:{episode.episode_id}", "window:win-canon")
    assert fact["evidence_event_id"] == f"episode-{episode.episode_id}"
    archived = json.loads((reflection_window.reflection_window_archive_dir() / "win-canon.json").read_text())
    assert archived["omega_outcome"]["reflection"]["summary"] == "Ten shallow lookups."
    assert window_lane.pressure_contributor() is None
    assert store.current(pipeline_id=0) is None


def test_a_contained_window_fold_failure_still_archives_the_counted_verdict(
    monkeypatch, model, termini
):
    """Operator ruling 2026-08-11: the window must not fail into a restage-crash loop."""
    model.replies.extend(["the weather is lovely", "still prose"])

    ignite_staged_window_episode(monkeypatch, "win-det")

    window_id, outcome, status = termini.windows[0]
    assert (window_id, outcome["reflection_source"], status) == ("win-det", "deterministic", "failed")
    assert "not parseable" in outcome["reflection_fallback_reason"]
    assert outcome["trajectory_update"] == {}
    assert termini.episodes == []
    assert [f["target"] for f in _observed_facts(termini)] == ["window:win-det"], \
        "the counted verdict was archived, so the window was observed"
    assert window_lane.pressure_contributor() is None


def test_a_cascade_that_raises_never_consolidates_the_window_it_did_not_reflect(
    monkeypatch, model, termini
):
    """A substrate failure leaves the window staged, so the terminus must not consolidate it."""

    def substrate_failure(_episode):
        raise RuntimeError("omega episode: missing Global B omega packet")

    monkeypatch.setattr(chain, "_run_omega_episode_cognition", substrate_failure)
    try:
        with pytest.raises(RuntimeError, match="missing Global B omega packet"):
            ignite_staged_window_episode(monkeypatch, "win-raise")
    finally:
        from core.cognition_lock import release_held_locks_for_tests

        release_held_locks_for_tests()

    assert termini.windows == []
    assert termini.episodes == ["failed"]
    assert _observed_facts(termini) == [], "nothing archived, nothing observed"
    assert window_lane.pressure_contributor() is not None


def test_window_reflection_owns_no_inference_topology():
    assert not hasattr(chain, "_start_window_reflection")
    source = (ROOT / "omega_loop" / "window" / "run.py").read_text(encoding="utf-8")
    for term in ("invoke_runtime_generate", "reason_fn", "phrase(", "runtime_client"):
        assert term not in source
    assert not (ROOT / "organ" / "promptorgan" / "packs" / "window_foldback").exists()
    tree = ast.parse((ROOT / "cognition_chain.py").read_text(encoding="utf-8-sig"))
    cascade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_ignite_episode_cascade"
    )
    attributes = {node.attr for node in ast.walk(cascade) if isinstance(node, ast.Attribute)}
    assert "is_window_episode" not in attributes
