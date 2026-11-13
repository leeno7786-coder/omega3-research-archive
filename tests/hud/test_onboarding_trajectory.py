"""Onboarding trajectory establish (flag-gated).

The Omega contract says "the operator sets [the trajectory] (onboarding, then
directives); you decompose". Today onboarding only seeds mastery GAPS, so the first
trajectory crystallizes lazily at the first Omega window-close — the system's first
~10 episodes run auto-driven (learning_debt) with NO mind-set agenda. These tests pin
the fix: under OMEGA_ONBOARDING_ESTABLISH the operator's own onboarding choices SET the
initial trajectory eagerly, so episodes are steered from episode 1. The explicit
onboarding LEARN is distinct from autonomous idle learning, which starts paused.
"""

import importlib

import pytest

from deepsleep_loop.mastery_store import normalize_domain_id

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def _isolate(tmp_path, monkeypatch):
    root = tmp_path / "omega_root"
    root.mkdir()
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    mastery_path = root / "mastery.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: mastery_path,
    )
    importlib.import_module("memory.knowledge_router")
    from memory.subdomain_flag import reset_flag_cache

    reset_flag_cache()
    # Hermetic: onboarding never performs synchronous subtopic inference in this path.
    monkeypatch.setattr("hud.onboarding_subtopics.decompose_subtopics", lambda *a, **k: {})
    return root, mastery_path


def test_build_onboarding_trajectory_from_operator_choices():
    from hud.operator_onboarding import build_onboarding_trajectory

    seeded = [{"subcategory": "Python"}, {"subcategory": "System Design"}]
    traj = build_onboarding_trajectory("software_engineer", seeded)

    assert traj is not None
    # target_domain == the FIRST subcategory, normalized to match the mastery gap key.
    assert traj["target_domain"] == normalize_domain_id("Python")
    assert traj["target_capability"] == normalize_domain_id("software_engineer")
    assert traj["intent"] == "explore_domain"
    assert traj["cycle_id"] == "onboarding"


def test_build_returns_none_when_no_subcategories():
    from hud.operator_onboarding import build_onboarding_trajectory

    assert build_onboarding_trajectory("software_engineer", []) is None
    assert build_onboarding_trajectory("software_engineer", [{"subcategory": "  "}]) is None


def test_flag_off_establishes_no_trajectory(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    # Phase-D flipped the default ON; pin OFF to test the off-branch (no trajectory).
    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", False)

    calls = []
    monkeypatch.setattr(
        "pressure_loop.temporal_store.write_trajectory",
        lambda *a, **k: calls.append((a, k)),
    )
    result = ob.seed_first_wake("software_engineer", ["Python"])

    assert calls == []  # never written
    assert "trajectory" not in result  # byte-identical return


def test_flag_on_establishes_trajectory_from_first_subcat(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", True)
    written = []
    monkeypatch.setattr("pressure_loop.temporal_store.read_trajectory", lambda: {})
    monkeypatch.setattr(
        "pressure_loop.temporal_store.write_trajectory",
        lambda traj, **k: written.append(traj),
    )

    result = ob.seed_first_wake("software_engineer", ["Python", "System Design"])

    assert len(written) == 1
    assert written[0]["target_domain"] == normalize_domain_id("Python")
    assert result["trajectory"]["target_domain"] == normalize_domain_id("Python")


def test_onboarding_enqueues_operator_learn_bootstrap(tmp_path, monkeypatch):
    """The bootstrap that makes onboarding CREATE LEARNING.

    Deterministic pressure-field readers never own ignition (``gather_contributors`` reads
    only operator ingress and Omega-authored successors), so the ``learning_debt`` the seeded
    mastery gaps raise never ignites on its own — the mind-primacy bootstrap is OPERATOR
    INTENT. Onboarding must enqueue an OPERATOR-sourced LEARN (source=="operator" is what
    gather reads) aimed at the operator's first focus, or the cold mind stays idle and the
    fresh topics sit unlearned. This pins that enqueue.
    """
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", True)
    monkeypatch.setattr("pressure_loop.temporal_store.read_trajectory", lambda: {})
    monkeypatch.setattr("pressure_loop.temporal_store.write_trajectory", lambda traj, **k: None)

    from pressure_loop import cognition_queue as operator_queue

    operator_queue._reset_cache_for_tests()
    operator_queue.clear_inputs()

    result = ob.seed_first_wake("data_scientist", ["Machine Learning", "Statistics"])

    queued = operator_queue.peek_inputs()
    assert len(queued) == 1, "onboarding must enqueue exactly one operator-intent LEARN"
    c = queued[0]
    assert c.source == "operator"  # operator ingress is what gather_contributors reads
    assert (
        c.metadata.get("directive_intent") == "LEARN"
    )  # directive rail -> LEARN (no word inference)
    assert "machine learning" in (c.metadata.get("message") or "").lower()  # first focus
    context = c.metadata.get("onboarding_context") or {}
    assert context["occupation"] == "data_scientist"
    assert [row["title"] for row in context["categories"]] == [
        "Machine Learning",
        "Statistics",
    ]
    assert result.get("learn_ignition") is True
    operator_queue.clear_inputs()  # don't leak into other tests


def test_flag_off_enqueues_no_learn_bootstrap(tmp_path, monkeypatch):
    """Flag OFF -> byte-identical legacy: no trajectory establish, so no operator-intent
    ignition either (the bootstrap pairs with the eager-trajectory establish)."""
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", False)
    from pressure_loop import cognition_queue as operator_queue

    operator_queue._reset_cache_for_tests()
    operator_queue.clear_inputs()

    ob.seed_first_wake("data_scientist", ["Machine Learning"])
    assert operator_queue.peek_inputs() == ()


def test_flag_on_does_not_clobber_existing_trajectory(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", True)
    written = []
    monkeypatch.setattr(
        "pressure_loop.temporal_store.read_trajectory",
        lambda: {"trajectory_id": "abc", "target_domain": "existing"},
    )
    monkeypatch.setattr(
        "pressure_loop.temporal_store.write_trajectory",
        lambda traj, **k: written.append(traj),
    )
    ignited = []
    monkeypatch.setattr(
        ob,
        "_ignite_onboarding_learn",
        lambda seeded, **kwargs: ignited.append((seeded, kwargs)) or True,
    )

    result = ob.seed_first_wake("software_engineer", ["Python"])

    assert written == []  # an existing setpoint is never clobbered by onboarding
    # Profile save is one-shot, so even a pre-existing trajectory must not swallow
    # the onboarding LEARN episode/foldback that expands the operator's work topics.
    assert result["learn_ignition"] is True
    assert len(ignited) == 1


def test_flag_on_publishes_live_signal_slot(tmp_path, monkeypatch):
    # Ignition reads the LAST_OMEGA_TRAJECTORY shared-state slot (not lobe 9), and that
    # slot is only hydrated from lobe 9 at boot -- so onboarding (which runs after boot)
    # must also publish the live signal for episode-1 to see the trajectory this session.
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob
    from core.shared_state import SharedState
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY

    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", True)
    monkeypatch.setattr("pressure_loop.temporal_store.read_trajectory", lambda: {})
    monkeypatch.setattr("pressure_loop.temporal_store.write_trajectory", lambda traj, **k: None)

    ss = SharedState()

    class _RT:
        shared_state = ss

    monkeypatch.setattr("pressure_loop.runtime.try_get_default_runtime", lambda: _RT())

    result = ob.seed_first_wake("software_engineer", ["Python"])

    slot = ss.read(LAST_OMEGA_TRAJECTORY)
    assert isinstance(slot, dict)  # the live signal was published
    assert slot["target_domain"] == normalize_domain_id("Python")
    assert slot["intent"] == "explore_domain"
    assert result["trajectory_live_signal"] is True


def test_flag_on_live_signal_soft_skips_without_runtime(tmp_path, monkeypatch):
    # No registered runtime (headless/test) -> the live-signal publish skips cleanly;
    # the durable lobe-9 write still happens (source of truth).
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", True)
    monkeypatch.setattr("pressure_loop.temporal_store.read_trajectory", lambda: {})
    monkeypatch.setattr("pressure_loop.temporal_store.write_trajectory", lambda traj, **k: None)
    monkeypatch.setattr("pressure_loop.runtime.try_get_default_runtime", lambda: None)

    result = ob.seed_first_wake("software_engineer", ["Python"])

    assert result["trajectory"]["target_domain"] == normalize_domain_id("Python")
    assert result["trajectory_live_signal"] is False  # deferred to next boot hydration


def test_episode1_ignition_carries_onboarding_trajectory(tmp_path, monkeypatch):
    # End-to-end: onboarding publishes the live signal, then the REAL pressure-ignition
    # path (pressure.evaluate) produces episode 1 carrying that trajectory as its
    # trajectory_snapshot -- i.e. episode 1 is steered by the operator-set agenda, not
    # trajectory-less. Ignition is model-free, so this is deterministic.
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob
    from core.shared_state import SharedState
    from pressure_loop.contributors import make_contributor
    from pressure_loop.pressure import evaluate

    monkeypatch.setattr(ob, "OMEGA_ONBOARDING_ESTABLISH", True)
    monkeypatch.setattr("pressure_loop.temporal_store.read_trajectory", lambda: {})
    monkeypatch.setattr("pressure_loop.temporal_store.write_trajectory", lambda traj, **k: None)

    ss = SharedState()

    class _RT:
        shared_state = ss

    monkeypatch.setattr("pressure_loop.runtime.try_get_default_runtime", lambda: _RT())

    # onboarding establishes the trajectory + publishes the live signal to `ss`
    ob.seed_first_wake("software_engineer", ["Python", "Distributed Systems"])

    # the cold-boot ignition: a contributor over threshold opens episode 1; evaluate
    # reads the LAST_OMEGA_TRAJECTORY slot from `ss` and attaches it to the ignition.
    debt = make_contributor("pressure_field", "learning_debt", 1.0, {})
    ignition = evaluate(
        [debt], active_episode=False, open_threshold=1.0, neuromod={}, shared_state=ss
    )

    assert ignition is not None  # episode 1 opens
    assert ignition.trajectory_snapshot is not None  # ...and it is NOT trajectory-less
    assert ignition.trajectory_snapshot["target_domain"] == normalize_domain_id("Python")
    assert ignition.trajectory_snapshot["cycle_id"] == "onboarding"
