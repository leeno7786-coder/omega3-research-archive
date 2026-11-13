"""Step 1 of the metacognitive pressure evolution — CHARACTERIZATION LOCK.

These are GOLDEN tests: they pin the behavior of the pressure -> ignition ->
intent -> action pipeline. They were authored at Step 0 (when application_gap->APPLY
was newly ON and OPERATIONAL_PRACTICE_BAND was OFF) and have since been CONSCIOUSLY
FLIPPED as later steps changed the defaults (the allowlist goldens at Step 3; note
OPERATIONAL_PRACTICE_BAND is now ON since Step 5b). Each flip is documented inline so
any UNINTENDED ignition-behavior change still FAILS LOUDLY.

A characterization test asserts WHAT IS, not what should be. Every assertion
here passes against the code as shipped at Step 0. Each guards a later step — but
read the scope honestly: some are NECESSARY-BUT-INSUFFICIENT structural locks (a
*conformant* later step could keep them green), in which case that step carries
its own dedicated lock and the note below says so. They still earn their place:
a non-conformant change trips them, and they document the current contract.

  Step 2/3 (Omega level overlay over pressure_params):
    - test_omega_levels_overlay_present_since_step2  (FLIPPED at Step 2 from the
      absent-module tripwire: pressure_loop/omega_levels.py now exists)
    - test_apply_omega_episode_result_is_a_pressure_noop  (still green: the shadow
      write targets the SEPARATE omega_levels store, leaving global_ == {outcome,
      omega_result}. Pins the genuine inert-channel invariant.)
    - test_allowed_pressure_params_is_bounded_three_key_set /
      test_filter_pressure_keeps_widened_deltas_*  (FLIPPED at Step 3: the adapter
      widened with execution_debt_delta; the allowlist stays bounded)
    - test_resolve_hybrid_winner_weights_* / _field_id_to_type_* (the gate +
      provenance the overlay must not reroute)
  Step 4 (intent_decision overrides _map_opened_by_to_intent):
    - the opened_by->intent + run() directive/fallback locks below. The
      ALLOWED_INTENTS->IDLE net is unreachable today but becomes live-reachable
      once Step 4's resolver can emit arbitrary strings — a Step-4 defensive guard.
  Step 6 (emergent trajectory):
    - test_episode_ignition_emit_* characterizes evaluate()'s trajectory_snapshot
      as a None-when-absent passthrough. NOTE: Step 6 synthesizes trajectory in
      make_trajectory (omega_loop/package/run.py), NOT in evaluate's passthrough,
      so the real Step-6 lock lives there (out of Step-1 scope). This is a
      structural lock on the emit contract, not a Step-6 tripwire.
  Intent->action static floor (config-drift guard, NOT a migration-step tripwire):
    - test_intent_to_action_map_is_static_default_no_silent_override pins that the
      config/meta.yaml (+ policy/symphony.policy.yaml meta.action) overlay is value-identical to
      the static default and that ASK_OPERATOR + APPLY survive only via that floor.
      No migration step (0-8) edits this table; this guards silent YAML drift.
      (Step 7's OMEGA_LEVELS_REPLACE targets the pressure-level overlay, never the
      intent->action map — so this is deliberately NOT framed as a Step-7 lock.)

DELIBERATELY NOT DUPLICATED (already pinned elsewhere — keep those green too):
  - Most opened_by->intent rows: tests/core/test_slice1_global_a_adversarial.py
    (direct, via form_intent). Only operator_priority->ASK_OPERATOR and literal
    None are added here as the genuinely-unpinned rows.
  - Step 5 (learn->apply bleed) band-OFF partition: the [0.60,target) -> learning
    (NOT application) split is already locked by
    tests/core/test_operational_practice_band.py::
    test_band_off_domain_in_band_is_learning_not_application — that is the Step-5
    tripwire; not re-pinned here.

DEFERRED (documented, not yet locked): a Step-8 "emit-surface closure" golden
(no new event kinds) — omitted to avoid a brittle assertion over incidental
telemetry; revisit when the record-only SignalEvent bus lands.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402

# global_loop/intent/__init__.py re-exports the `run` FUNCTION as the package
# attribute `intent.run`, which shadows the submodule for `import ... as`. Fetch
# the real module object from sys.modules so gir.run() and the monkeypatch of
# gir._map_opened_by_to_intent bind the module, not the function.
gir = importlib.import_module("global_loop.intent.run")  # noqa: E402
from cognition_chain import _apply_omega_episode_result  # noqa: E402
from global_loop.intent.run import _map_opened_by_to_intent  # noqa: E402
from meta_loop.action.run import (  # noqa: E402
    _INTENT_TO_ACTION_DEFAULT,
    _intent_to_action_map,
)
from omega_loop.episode.prompts import (  # noqa: E402
    _ALLOWED_PRESSURE_PARAMS,
    _filter_pressure,
)
from omega_loop.episode.types import OmegaEpisodeResult  # noqa: E402
from pressure_loop.contributors import (  # noqa: E402
    EpisodeIgnition,
    make_contributor,
)
from pressure_loop.policy import (  # noqa: E402
    meta_intent_to_action,
    pressure_open_threshold,
    pressure_winner_ceiling,
    pressure_winner_margin,
)
from pressure_loop.pressure import (  # noqa: E402
    _FIELD_ID_TO_TYPE,
    clear_pending_contributors,
    evaluate,
    resolve_hybrid_winner_weights,
)
from tests.core.rebuild_helpers import form_intent  # noqa: E402


def _ign(opened_by, message="", *, weight=1.0, aggregate=0.8, metadata=None):
    meta = {"message": message} if message else {}
    if metadata:
        meta.update(metadata)
    c = make_contributor("x", opened_by, weight, meta)
    return EpisodeIgnition(
        episode_id="ep-1",
        aggregate_pressure=aggregate,
        opened_by=opened_by,
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


# ── Step 2/3: the per-episode pressure_params channel is INERT today ─────────
def test_allowed_pressure_params_is_bounded_five_key_set():
    # CONSCIOUSLY FLIPPED again (operator 2026-06-30): added the VERIFY lever
    # verification_debt_delta (-> verification_needed -> USE_SKILL) and the REINFORCE lever
    # confidence_debt_delta (-> confidence_reinforcement -> LEARN) so the mind can raise its own
    # verify/reinforce pressure. (2026-06-27 added clarification_debt_delta, the ASK-OPERATOR
    # lever.) Still BOUNDED — bare type names / unknown keys stay out, and operator-supremacy
    # types are structurally rejected downstream.
    assert _ALLOWED_PRESSURE_PARAMS == frozenset(
        {
            "application_debt_delta",
            "synthesis_debt_delta",
            "execution_debt_delta",
            "mastery_debt_delta",
            "clarification_debt_delta",
            "verification_debt_delta",
            "confidence_debt_delta",
        }
    )


def test_filter_pressure_keeps_widened_deltas_drops_unknown_and_non_mapping():
    # CONSCIOUSLY FLIPPED at Step 3: execution_debt_delta is now RETAINED. The bare
    # type name "execution_gap" and other non-allowlisted keys are still DROPPED, so
    # the allowlist stays bounded (delta keys in, raw type names / junk out).
    assert _filter_pressure(
        {
            "application_debt_delta": 0.1,
            "execution_debt_delta": 0.7,
            "execution_gap": 0.9,
            "learning_debt": 0.5,
            "junk": 1,
        }
    ) == {"application_debt_delta": 0.1, "execution_debt_delta": 0.7}
    assert _filter_pressure(None) == {}
    assert _filter_pressure("x") == {}
    # in-allowlist but non-float value is skipped (float() raises -> continue)
    assert _filter_pressure({"synthesis_debt_delta": "notafloat"}) == {}


def test_apply_omega_episode_result_is_a_pressure_noop():
    # global_ invariant (still green after Step 2): _apply_omega_episode_result
    # stores the omega result verbatim and — SINCE STEP 2 — shadow-writes
    # pressure_params into the SEPARATE pressure_loop.omega_levels overlay store,
    # but it NEVER routes a pressure key into global_. global_ gains ONLY
    # omega_result (+ outcome when cognized). The overlay/shadow-write behavior is
    # locked in tests/core/test_step2_omega_levels.py.
    ep = SimpleNamespace(global_={"outcome": {"artifacts": {}}})
    result = OmegaEpisodeResult(
        episode_id="e",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params={"application_debt_delta": 0.9, "synthesis_debt_delta": 0.9},
        response_text=None,
        synthesis=None,
        reflection=None,
    )
    _apply_omega_episode_result(ep, result)
    assert set(ep.global_.keys()) == {"outcome", "omega_result"}
    assert ep.global_["omega_result"]["pressure_params"] == {
        "application_debt_delta": 0.9,
        "synthesis_debt_delta": 0.9,
    }


# ── Step 2/3: the hybrid ignition gate + the field-id provenance map ─────────
def test_resolve_hybrid_winner_weights_open_and_suppress_returns():
    r = resolve_hybrid_winner_weights
    # clear winner at/above ceiling, gap >= margin -> opens (empty suppress)
    assert r([("a", 1.0), ("b", 0.2)], ceiling=1.0, margin=0.05) == ("a", 1.0, "")
    assert r([("a", 0.10), ("b", 0.02)], ceiling=0.10, margin=0.05) == ("a", 0.10, "")
    # below ceiling -> suppress
    assert r([("a", 0.05)], ceiling=0.10, margin=0.05) == (None, 0.05, "below_ceiling")
    assert r([], ceiling=0.10, margin=0.05) == (None, 0.0, "below_ceiling")
    # A near-tie OPENS on the first signal in — margin is accepted and ignored
    # (operator 2026-08-15: a lane runs one episode from one input, so signals
    # arriving together are a queue, not an ambiguity; the loser waits for the
    # next ignition). Suppressing here dropped BOTH signals and parked
    # autonomous cognition for 7m23s.
    assert r([("bravo", 1.0), ("alpha", 0.96)], ceiling=1.0, margin=0.05) == (
        "bravo",
        1.0,
        "",
    )
    # Dead even: stable order means first-in wins, whatever the margin says.
    assert r([("alpha", 1.0), ("bravo", 1.0)], ceiling=1.0, margin=0.05) == (
        "alpha",
        1.0,
        "",
    )
    assert r([("bravo", 1.0), ("alpha", 0.95)], ceiling=1.0, margin=0.05) == (
        "bravo",
        1.0,
        "",
    )


def test_pressure_winner_ceiling_override_clamps_and_margin_in_range():
    # config-tune-tolerant code contracts (NOT the tunable live float):
    assert pressure_winner_ceiling(override=0.5) == 0.5
    assert pressure_winner_ceiling(override=2.0) == 1.0
    assert pressure_winner_ceiling(override=-1.0) == 0.0
    # no override -> ceiling derives from policy open_threshold (the wiring lock)
    assert pressure_winner_ceiling() == pressure_open_threshold()
    assert 0.0 <= pressure_winner_margin() <= 1.0
    # invalid/out-of-range policy values fall back to safe defaults
    assert pressure_open_threshold({"pressure": {"open_threshold": 1.5}}) == 1.0
    assert pressure_winner_margin({"pressure": {"winner_margin": "bad"}}) == 0.05


def test_field_id_to_type_map_keyset_and_remaps():
    # operator_priority added (2026-06-30): the operator-directed priority reader (->
    # operator_priority -> ASK_OPERATOR). Identity-mapped like the autonomous-drive readers.
    assert set(_FIELD_ID_TO_TYPE) == {
        "curiosity_gap",
        "operator_intent",
        "operator_message",
        "epistemic_emptiness",
        "idle_probe_aggregate",
        "learning_debt",
        "execution_gap",
        "operator_priority",
    }
    assert _FIELD_ID_TO_TYPE["operator_intent"] == "operator_command"
    assert _FIELD_ID_TO_TYPE["operator_message"] == "operator_query"
    assert _FIELD_ID_TO_TYPE["execution_gap"] == "execution_gap"
    assert _FIELD_ID_TO_TYPE["operator_priority"] == "operator_priority"
    # application_gap is NOT a reader field-id: it reaches the contest only as a
    # deepsleep-reseed Contributor (see the end-to-end USE_SKILL test below). A
    # Step-2/3 overlay must not turn it into a reader id.
    assert "application_gap" not in _FIELD_ID_TO_TYPE


# ── intent->action static floor + config/meta.yaml (+ policy/symphony.policy.yaml) drift guard
def test_intent_to_action_map_is_static_default_no_silent_override():
    merged = _intent_to_action_map()
    # Every SHIPPED default route is preserved unchanged in the merged map. (Key-
    # superset, not full equality: a later additive YAML row for a NEW intent is
    # behaviour-neutral and must not false-trip this lock. _intent_to_action_map
    # resolves through _meta_section, which deep-merges policy/symphony.policy.yaml meta.action
    # over config/meta.yaml — so this also guards against policy drift.)
    for k, v in _INTENT_TO_ACTION_DEFAULT.items():
        assert merged[k] == v, (k, v)
    overlay = meta_intent_to_action()
    for k, v in overlay.items():
        assert _INTENT_TO_ACTION_DEFAULT[k] == v, (k, v)  # no silent override
    # ASK_OPERATOR is absent from the yaml overlay and survives ONLY because the static
    # default supplies it (floor-only key). APPLY is a legacy shim → USE_SKILL.
    assert "ASK_OPERATOR" not in overlay
    assert merged["ASK_OPERATOR"] == "ASK_OPERATOR"
    assert merged["APPLY"] == "USE_SKILL"
    assert overlay.get("APPLY", "USE_SKILL") == "USE_SKILL"
    assert merged["USE_SKILL"] == "USE_SKILL"
    # SELF_INQUIRY was REMOVED (operator directive): it collapsed to IDLE and is no
    # longer a key in the action map. Like any unknown intent it falls through to the
    # IDLE default; IDLE:IDLE remains the explicit self-noop route.
    assert "SELF_INQUIRY" not in merged
    assert merged["IDLE"] == "IDLE"
    assert merged.get("NOT_A_REAL_INTENT", "IDLE") == "IDLE"


# ── Step 4: opened_by->intent rows genuinely unpinned at the unit level ──────
def test_operator_priority_routes_to_ask_operator():
    # the mind's own won ask (source "omega") keeps its own route; absent from
    # the adversarial param lists, so pinned here.
    assert _map_opened_by_to_intent("operator_priority", "What next?") == "ASK_OPERATOR"


def test_none_and_empty_opened_by_route_to_idle():
    # (opened_by or "").strip() -> "" -> catch-all IDLE. SELF_INQUIRY was REMOVED
    # (operator directive); the unknown/empty opened_by catch-all now yields IDLE.
    assert _map_opened_by_to_intent(None, "") == "IDLE"
    assert _map_opened_by_to_intent("", "") == "IDLE"


# ── Step 4: run() directive override + invalid-directive fallthrough + IDLE net
def test_run_directive_intent_overrides_map_verbatim():
    ign = _ign(
        "operator_command",
        "learn and build a logo",
        metadata={"directive_intent": "DOCUMENT_CREATE"},
    )
    assert gir.run(ign) == "DOCUMENT_CREATE"


def test_run_invalid_directive_falls_through_to_opaque_operator_turn():
    ign = _ign(
        "operator_command",
        "run the build",
        metadata={"directive_intent": "NONSENSE"},
    )
    # An invalid directive cannot bypass the ordinary operator interpretation
    # boundary. AUTO receives the opaque turn and Mistral decides whether it
    # needs tools.
    assert gir.run(ign) == "RESPOND"


def test_run_coerces_disallowed_mapped_intent_to_idle(monkeypatch):
    # the ALLOWED_INTENTS->IDLE safety net is unreachable today (every map
    # return is allowed); force a bogus inference to lock that the net holds.
    monkeypatch.setattr(gir, "_map_opened_by_to_intent", lambda ob, m, **_: "BOGUS_INTENT")
    assert gir.run(_ign("operator_command", "run the build")) == "IDLE"


# ── Step 6: episode_ignition producer emit + trajectory passthrough ──────────
def test_episode_ignition_emit_carries_required_structured_keys():
    clear_pending_contributors()
    events: list[dict] = []
    drive = make_contributor("autonomous", "curiosity_gap", 1.0, {})
    with patch("guardian.terminal_feedback._publish_event", side_effect=events.append):
        ign = evaluate([drive], active_episode=False, open_threshold=0.1, neuromod={})
    assert ign is not None
    emitted = [
        e for e in events if e.get("kind") == "data" and e.get("phase") == "episode_ignition"
    ]
    assert len(emitted) == 1, f"expected 1 episode_ignition emit, got {len(emitted)}"
    e = emitted[0]
    for key in (
        "episode_id",
        "aggregate_pressure",
        "opened_by",
        "dominant_contributor",
        "contributors",
        "trajectory_snapshot",
    ):
        assert key in e, (key, sorted(e))
    assert e["opened_by"] == "curiosity_gap"
    # Step-6 passthrough: trajectory is None when no slot is set (never synthesized)
    assert e["trajectory_snapshot"] is None
    assert 0.0 <= e["aggregate_pressure"] <= 1.0
    assert set(e["dominant_contributor"]) >= {"source", "type", "weight"}
    assert e["contributors"] and set(e["contributors"][0]) >= {
        "source",
        "type",
        "weight",
        "metadata",
    }


# ── Step 4: the reachable USE_SKILL path, end-to-end — BOTH gap types ─────────
@pytest.mark.parametrize("gap_type", ["application_gap", "execution_gap"])
def test_gap_contributor_opens_use_skill_episode_end_to_end(gap_type):
    # Both gap contributors, end to end. application_gap reaches the contest via
    # _RESEED_STAGING (NOT a _FIELD_ID_TO_TYPE reader id); execution_gap via the
    # read_execution_gap reader. Either way the autonomous Contributor must win with
    # its type preserved and route to USE_SKILL — never the pre-Step-0 IDLE
    # dead-end. FLIPPED LIVE (Phase 6): the gap now opens a USE_SKILL episode (the
    # skill-mediated agentic route) — the legacy APPLY action has RETIRED. This is the
    # permanent, symmetric form of the manual mutation check (which proved a :39
    # regression flips the route to the IDLE catch-all). Emission paths are pinned separately
    # (test_deepsleep_mastery / test_execution_gap); this joins gap -> evaluate ->
    # form_intent -> USE_SKILL.
    clear_pending_contributors()
    # Post-guard contract (2026-07-08): USE_SKILL is reachable only with a
    # dispatchable objective — a gap episode without one downgrades honestly
    # (test_use_skill_dispatchability_guard pins that side). Hold the objective in
    # the episode SRAM state so this test keeps pinning the reachable route.
    from memory import episode_sram
    from memory.build_objective import BuildObjective, hold_build_objective

    episode_sram.open_bucket(f"ep-{gap_type}", pipeline_id=0)
    hold_build_objective(
        BuildObjective(
            episode_id=f"ep-{gap_type}",
            objective="practice python by building the parser",
            origin_opened_by="operator_query",
        )
    )
    try:
        gap = make_contributor("autonomous", gap_type, 0.4, {"domain": "python"})
        ign = evaluate([gap], active_episode=False, open_threshold=0.1, neuromod={})
        assert ign is not None
        assert ign.opened_by == gap_type
        assert form_intent(ign).intent == "USE_SKILL"
    finally:
        episode_sram.reset()
