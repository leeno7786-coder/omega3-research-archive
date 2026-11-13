"""The window spine must read the outcome Global B actually writes.

Live 2026-09-20. The window lane folded over ten episodes and reported:

    "10 consecutive episodes with identical structure: intent=USE_TOOL, action=RESPONSE,
     cascade_status=completed, empty outcome_status and outcome_detail, no turns
     recorded. No differentiation in content or behavior."
    ... "All episodes are hollow (no outcomes, no turns) and indistinguishable."

Those ten episodes were the todo-app build going round in a circle. The window lane is
the one mechanism that can see across episodes, it SAW the repetition, and it still
returned DONE with no trajectory — because every field it judges by was empty.

Not because the episodes had no outcome. Because the reader and the writer disagreed:

  * Global B writes ``episode.global_["outcome"] = asdict(GlobalOutcomePack)``, so the
    body's ``outcome`` is the whole PACK and the status lives at
    ``outcome["outcome"]["status"]``.
  * ``_episode_spine`` read ``outcome["status"]`` — one level too shallow — and
    ``outcome["detail"]``, a key the pack does not have at any level (it is ``summary``).
  * Its remaining fallback was ``outcome["goal_state"]``, a field law section 3 retired
    ("There is no goal / goal_state / goal-reflection in the v2 cascade"), which is the
    tell that the reader was written against an older shape and never revisited.

The existing view test passed throughout, because its fixture wrote the FLAT shape the
reader wanted rather than the shape the writer emits. So these tests build the body from
the real producer — the round-trip law section 2.1 point 3 asks for — and a drift on
either side now breaks a test instead of a live window.
"""

from __future__ import annotations

from global_loop.package.run import GlobalOutcomePack, global_outcome_pack_to_dict
from omega_loop.window.run import _episode_spine


def _real_outcome(*, status: str, summary: str) -> dict:
    """An episode body's ``outcome`` exactly as Global B writes it."""
    return global_outcome_pack_to_dict(
        GlobalOutcomePack(
            episode_id="ep-1",
            outcome={
                "omega_lane": "agent",
                "intent": "USE_TOOL",
                "trajectory_alignment": "aligned",
                "divergence_score": 0.0,
                "execution_outcome": {"executed": True, "status": status},
                "status": status,
                "grounded": True,
                "grounding_basis": ["agent_run"],
                "summary": summary,
                "failure_kind": "",
                "failure_reason": "",
            },
            observations={"memory_used": False, "warnings": []},
            artifacts={"original_objective": "build a basic todo app"},
            followups=[],
            consistency={},
        )
    )


def test_the_spine_reads_the_status_global_b_writes() -> None:
    body = {
        "episode_id": "ep-1",
        "intent": "USE_TOOL",
        "action": "RESPONSE",
        "cascade_status": "completed",
        "outcome": _real_outcome(status="success", summary="wrote app.py and its template"),
    }

    spine = _episode_spine(body, 0)

    assert spine["outcome_status"] == "success", (
        "the status is nested inside the pack; reading it at the top level is how ten "
        "episodes reached the window lane looking hollow"
    )
    assert spine["outcome_detail"] == "wrote app.py and its template", (
        "the pack names this field 'summary'; there is no 'detail' at any level"
    )


def test_a_failed_episode_is_distinguishable_from_a_successful_one() -> None:
    """The whole value of the window is telling episodes apart. Two outcomes, two spines."""
    good = _episode_spine(
        {"episode_id": "ep-1", "intent": "USE_TOOL", "action": "RESPONSE",
         "outcome": _real_outcome(status="success", summary="the tests pass")}, 0)
    bad = _episode_spine(
        {"episode_id": "ep-2", "intent": "USE_TOOL", "action": "RESPONSE",
         "outcome": _real_outcome(status="failed", summary="the tests fail")}, 1)

    assert (good["outcome_status"], bad["outcome_status"]) == ("success", "failed")
    assert good["outcome_detail"] != bad["outcome_detail"]


def test_the_spine_names_no_retired_field() -> None:
    """``goal_state`` was the reader's fallback. Law section 3 retired the whole concept.

    Asserted over the module's EXECUTABLE strings only: the docstring that records why
    the fallback is gone must not trip the guard against it.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("omega_loop/window/run.py").read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    live = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert "goal_state" not in live, (
        "the window spine still falls back to a field the v2 cascade does not have"
    )


def test_an_outcome_that_is_genuinely_absent_stays_empty() -> None:
    """An episode with no outcome must read as empty — not as a fabricated status."""
    spine = _episode_spine({"episode_id": "ep-3", "intent": "IDLE", "action": "IDLE"}, 0)

    assert spine["outcome_status"] == ""
    assert spine["outcome_detail"] == ""


# ---------------------------------------------------------------------------
# EARLY-V01, 2026-09-21. The reader/writer repair above fixed the OUTCOME field, and the
# window still could not tell ten episodes apart -- because three of the five fields the
# spine projects (`action`, `cascade_status`, `outcome_status`) hold exactly ONE value
# across all twenty real records of that build, measured in
# docs/superpowers/evidence/2026-09-21-early-v01/field_inventory.json.
#
# The evidence that separates them was already on disk and dropped by the projection:
# Omega's own per-episode synthesis, the job id, and `touched_files`. In the trial the
# current projection scored 0/9 at naming a stalled build; the same records projected
# wider scored 9/9.
#
# These tests pin the PROJECTION, not the model. They build `omega_result` through the
# real producer (`OmegaEpisodeResult.to_dict`, which cognition_chain writes at
# `global_["omega_result"]` and episode_journal lifts into the body), so drift on either
# side breaks a test rather than a live window.


def _omega_result(*, synthesis: str, decision: str = "PRESSURE", gap: str = "CRUD missing",
                  reason: str = "implement the CRUD routes", confidence: float = 0.9,
                  pressure_params: dict | None = None) -> dict:
    """``body["omega_result"]`` exactly as the real producer emits it."""
    from omega_loop.episode.types import OmegaEpisodeResult
    from schema.omega_control_decision import (
        OmegaAssessment,
        OmegaControlDecision,
        OmegaPressureDecision,
    )

    assessment = OmegaAssessment(
        who="omega", what="build a todo app", when="this episode", where="E:/todo_app",
        why="the operator asked", how="through the agent lane",
        desired_state="a working todo app", observed_state=synthesis,
        gap="" if decision == "DONE" else gap,
        evidence=("agent_run",),
    )
    pressure = None
    if decision != "DONE":
        pressure = OmegaPressureDecision(
            intent="USE_TOOL", confidence=confidence, reason=reason,
            setpoints={"application_debt_delta": 0.1},
            desired_state="a working todo app", observed_state=synthesis,
            resolution_conditions=("the routes exist",),
        )
    control = OmegaControlDecision(
        assessment=assessment, decision=decision, pressure=pressure, synthesis=synthesis
    )
    return OmegaEpisodeResult(
        episode_id="ep-1",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params=dict(pressure_params or {"application_debt_delta": 0.1}),
        response_text=None,
        synthesis=synthesis,
        reflection=None,
        intent_decision={"intent": "USE_TOOL", "confidence": confidence, "reason": reason},
        control_decision=control.to_dict(),
    ).to_dict()


def _live_agent_run(touched: list[str]) -> dict:
    """The shape auto_loop/execute/run.py writes to ``outputs["agent_run"]``.

    Field names taken from live record 9.01.6267a51c (project E:/todo_app, job 9f3df1e6).
    """
    return {
        "job_id": "9f3df1e6-2a77-49c9-b2c1-0211811508cc",
        "status": "batch_ran",
        "steps": 2,
        "project_root": "E:/todo_app",
        "project_files": ["app.py", "requirements.txt", "templates/index.html"],
        "touched_files": list(touched),
        "stopped_reason": "",
    }


def _build_body(*, n: int, synthesis: str, touched: list[str], decision: str = "PRESSURE",
                pressure_params: dict | None = None, with_agent_run: bool = True) -> dict:
    """A body whose CURRENTLY-PROJECTED fields are identical for every n.

    Only the receipts differ. That is the point: if two such bodies render to the same
    spine, the window is structurally unable to tell a stall from progress.
    """
    body = {
        "episode_id": f"ep-{n}",
        "intent": "USE_TOOL",
        "action": "RESPONSE",
        "cascade_status": "completed",
        "work_kind": "autonomous",
        "outcome": _real_outcome(status="completed", summary="Response generated successfully"),
        "omega_result": _omega_result(
            synthesis=synthesis, decision=decision, pressure_params=pressure_params
        ),
        "brain_turns": [],
    }
    if with_agent_run:
        body["outputs"] = {"agent_run": _live_agent_run(touched)}
    return body


def test_ten_episodes_differing_only_in_retained_receipts_are_distinguishable() -> None:
    """The EARLY-V01 finding, as a test.

    Every field the current spine projects is identical across these ten episodes. They
    differ only in evidence the episode already carries and the projection drops. If the
    spine cannot separate them, no model reading it can either -- which is exactly how
    twenty real episodes of a looping build reached the window looking indistinguishable.
    """
    import json

    syntheses = [
        "The project is claimed but implementation is empty; next is the Flask backend.",
        "The agent confirmed the directory but wrote no code; next is reading app.py.",
        "The project has a partial Flask app; next is completing CRUD.",
        "The plan is set but no validation occurred; next is validating app.py.",
        "app.py lacks CRUD; next is writing the missing routes.",
        "app.py is implemented with Flask CRUD; next is verifying requirements.txt.",
        "Core app.py remains unconfirmed; next is verifying it again.",
        "Existence verified; next is inspecting requirements.txt and index.html.",
        "Partial implementation; next batch must validate CRUD and run the app.",
        "No implementation has occurred yet; next must write the CRUD logic.",
    ]
    bodies = [
        _build_body(n=i, synthesis=s, touched=["E:/todo_app/app.py"] if i == 5 else [])
        for i, s in enumerate(syntheses)
    ]

    spines = [_episode_spine(b, i) for i, b in enumerate(bodies)]
    # `n` and `episode_id` are bookkeeping: they differ by construction and would make this
    # assertion pass no matter how blind the projection is. The question is whether the
    # EVIDENCE differs, so they are excluded.
    content = {
        json.dumps({k: v for k, v in s.items() if k not in ("n", "episode_id")}, sort_keys=True)
        for s in spines
    }

    assert len(content) == 10, (
        "ten episodes carrying ten different syntheses, and one that alone touched a file, "
        f"projected to {len(content)} distinct spine(s) once identifiers are set aside; the "
        "window cannot see a stall it is never shown"
    )


def test_the_spine_surfaces_the_receipts_that_prove_a_stall() -> None:
    """Named one by one, because each carried weight in the trial's winning arm."""
    spine = _episode_spine(
        _build_body(n=1, synthesis="app.py lacks CRUD; next is writing the routes.",
                    touched=[]), 0)

    flat = repr(spine)
    assert "app.py lacks CRUD" in flat, "Omega's own synthesis is the strongest single signal"
    assert "9f3df1e6" in flat, "one job id across a whole window is what makes a stall a stall"
    assert "requirements.txt" in flat, "an unchanged project file set is the stall's other half"
    assert "application_debt_delta" in flat, "debt that accumulates separates stall from progress"
    assert "PRESSURE" in flat, "the control decision records whether Omega chose to continue"


def test_an_agent_run_that_touched_nothing_is_not_the_same_as_no_agent_run() -> None:
    """Law section 2.1 at the projection boundary.

    ``touched_files: []`` means the agent RAN and changed nothing -- the single most
    important fact in a stalled window. No ``agent_run`` at all means there was nothing to
    report, as on a chat turn. Collapsing the two hides the stall behind the same rendering
    as an episode that never built anything, so the spine must not let a reader mistake one
    for the other.
    """
    ran_touched_nothing = _episode_spine(
        _build_body(n=1, synthesis="planned again", touched=[]), 0)
    never_ran = _episode_spine(
        _build_body(n=1, synthesis="planned again", touched=[], with_agent_run=False), 0)

    assert ran_touched_nothing != never_ran, (
        "an agent that ran and touched nothing renders identically to an episode with no "
        "agent run at all; that is the stall signal being discarded at the boundary"
    )


def test_a_widened_ten_episode_window_fits_the_evidence_budget() -> None:
    """Widening must not buy detail by silently losing episodes.

    ``_json_data_block`` clips through ``_harden_bounded``'s binary search, which cuts the
    TAIL -- the most recent episodes. A view that overflows would lose exactly the episodes
    the window most needs and report success, so the widened ten-episode view is measured
    against the real budget here rather than assumed to fit.

    This is a regression GUARD, not a RED test: it passes before the widening (the narrow
    view trivially fits) and must still pass after it.
    """
    from types import SimpleNamespace

    from omega_loop.episode.prompts import _json_data_block
    from omega_loop.window.run import build_window_view

    long_synthesis = (
        "The build has app.py and templates/index.html partially complete; the next "
        "concrete step is to update index.html with full CRUD UI forms, then verify the "
        "dependencies in requirements.txt and finally run the application end to end."
    )
    bodies = [
        _build_body(n=i, synthesis=f"{long_synthesis} (episode {i})", touched=[])
        for i in range(10)
    ]
    window = SimpleNamespace(
        window_id="w-1", episode_count=10, episode_bodies=bodies,
        trajectory_snapshot={}, cascade_status_by_episode={},
    )
    view = build_window_view(window)
    rendered = _json_data_block({"window": view, "trajectory_influencer": None}, 24000)

    assert len(view["episodes"]) == 10
    missing = [i for i in range(10) if f"ep-{i}" not in rendered]
    assert not missing, (
        f"the widened view overflowed the evidence budget ({len(rendered)} chars against "
        f"24000) and episodes {missing} were cut from the tail; widening must not silently "
        "drop window members"
    )
