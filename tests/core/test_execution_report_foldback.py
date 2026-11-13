from __future__ import annotations

from types import SimpleNamespace

import cognition_chain
import pytest
from memory import episode_sram
from memory.build_objective import BuildObjective, hold_build_objective
from memory.execution_report import ExecutionReport, current_execution_report
from omega_loop.episode.prompts import _build_in_progress_block


def test_skill_foldback_builds_complete_execution_report_in_sram() -> None:
    episode_sram.open_bucket("ep-report", pipeline_id=0)
    episode = SimpleNamespace(
        id="ep-report",
        global_={"omega_packet": {"build_in_progress": []}},
    )
    qwen_result = {
        "status": "yielded",
        "summary": "Implemented the parser; one contract is still red.",
        "progress_digest": "Added parser.py and ran the focused tests.",
        "files_changed": ["src/parser.py"],
        "commands_run": ["pytest tests/test_parser.py -q"],
        "tests_run": ["pytest tests/test_parser.py -q"],
        "tool_calls": [{"tool_name": "patch_file", "status": "success"}],
        "unresolved_blockers": ["round-trip contract still fails"],
        "rounds_used": 2,
    }
    folded = {
        "global_b": {
            "objective_complete": False,
            "more_work_needed": True,
            "unresolved_blockers": ["round-trip contract still fails"],
        }
    }
    verification = {
        "all_green": False,
        "checks": [
            {"name": "focused tests", "ok": False, "detail": "1 failed"},
            {"name": "file exists", "ok": True, "detail": "src/parser.py"},
        ],
    }

    cognition_chain._hold_execution_report_from_fold(
        episode,
        qwen_result,
        folded,
        build_task="Implement the parser and prove the round trip",
        pass_gate=("focused tests pass", "round trip preserves fields"),
        verification=verification,
        original_request="Build the parser",
        origin_opened_by="operator_queue",
    )

    report = current_execution_report()
    assert report is not None
    assert report.build_task == "Implement the parser and prove the round trip"
    assert report.pass_gate == ("focused tests pass", "round trip preserves fields")
    assert "changed src/parser.py" in report.did
    assert "ran pytest tests/test_parser.py -q" in report.did
    assert report.did_not == ("round-trip contract still fails",)
    assert "file exists: src/parser.py" in report.succeeded
    assert "focused tests: 1 failed" in report.failed
    assert report.failed[-1] == "round-trip contract still fails"

    cognition_chain._refresh_packet_wip_from_execution_report(episode)
    packet = episode.global_["omega_packet"]
    assert packet["execution_report"]["pass_gate"] == [
        "focused tests pass",
        "round trip preserves fields",
    ]
    assert packet["build_in_progress"][0]["objective"] == report.build_task
    assert packet["build_in_progress"][0]["errors"][-1] == "round-trip contract still fails"
    assert packet["build_in_progress"][0]["verification"] == verification
    rendered = _build_in_progress_block(
        SimpleNamespace(build_in_progress=tuple(packet["build_in_progress"]))
    )
    assert "PASS GATE (authored by Omega):" in rendered
    assert "focused tests pass" in rendered
    assert "DID:" in rendered and "changed src/parser.py" in rendered
    assert "DID NOT:" in rendered and "round-trip contract still fails" in rendered
    assert "SUCCEEDED:" in rendered and "file exists: src/parser.py" in rendered
    assert "FAILED:" in rendered and "focused tests: 1 failed" in rendered


def test_skill_foldback_failure_is_not_reported_as_a_completed_cascade(monkeypatch) -> None:
    episode = SimpleNamespace(
        id="ep-fold-fail",
        episode_id="ep-fold-fail",
        meta={
            "execution_action_pack": {
                "action": "USE_SKILL",
                "execution_inputs": {"objective": "repair the parser"},
            }
        },
        auto={
            "outputs": {
                "skill_objective": "repair the parser",
                "skill_result": {
                    "episode_id": "ep-fold-fail",
                    "status": "success",
                    "summary": "builder returned",
                },
            }
        },
        global_={},
    )
    monkeypatch.setattr(
        "meta_loop.skill.foldback.fold_skill_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fold boundary broke")),
    )

    with pytest.raises(RuntimeError, match="fold boundary broke"):
        cognition_chain._run_skill_foldback(episode)

    assert episode.meta["skill_foldback_error"] == "fold boundary broke"


def test_practice_domain_survives_build_objective_into_execution_report() -> None:
    episode_sram.open_bucket("ep-practice-report", pipeline_id=0)
    hold_build_objective(
        BuildObjective(
            episode_id="ep-practice-report",
            objective="build a parser practice artifact",
            origin_opened_by="omega_practice",
            practice_domain="compiler-design",
        )
    )

    cognition_chain._hold_execution_report_from_fold(
        SimpleNamespace(id="ep-practice-report"),
        {"status": "success", "summary": "parser built"},
        {"global_b": {"objective_complete": True, "more_work_needed": False}},
        build_task="build a parser practice artifact",
        pass_gate=("practice battery passes",),
        verification={"all_green": True, "checks": []},
        original_request="",
        origin_opened_by="omega_practice",
    )

    report = current_execution_report()
    assert report is not None
    assert report.metadata["practice_domain"] == "compiler-design"


def test_accepted_practice_report_defers_grounding_to_deepsleep(monkeypatch) -> None:
    from memory.execution_report import hold_execution_report

    episode_sram.open_bucket("ep-practice-accepted", pipeline_id=0)
    hold_execution_report(
        ExecutionReport(
            episode_id="ep-practice-accepted",
            build_task="build a parser practice artifact",
            pass_gate=("practice battery passes",),
            did=("changed parser.py",),
            did_not=(),
            succeeded=("practice battery passes",),
            failed=(),
            verification={"all_green": True},
            origin_opened_by="omega_practice",
            metadata={"practice_domain": "compiler-design", "objective_complete": True},
        )
    )
    grounded = []
    monkeypatch.setattr(
        "pressure_loop.practice_mint.apply_practice_success",
        lambda domain: grounded.append(domain) or 0.6,
    )
    monkeypatch.setattr("core.workspace_port.record_port", lambda *_a, **_k: "completed/path")
    monkeypatch.setattr(
        cognition_chain, "_emit_build_complete_to_operator", lambda *_a, **_k: None
    )
    result = SimpleNamespace(
        control_decision={"decision": "DONE", "pressure": None, "synthesis": "The practice gate is satisfied."},
        build_accepted=True,
    )

    cognition_chain._apply_build_report_decision(
        SimpleNamespace(id="ep-practice-accepted"),
        result,
    )

    assert grounded == []
    report = current_execution_report()
    assert report is not None
    assert report.omega_intent == "ACCEPT"
    assert report.omega_reason == "The practice gate is satisfied."
    assert report.metadata["practice_grounding_pending"] is True


def test_deepsleep_grounds_the_accepted_practice_report_once(monkeypatch) -> None:
    from deepsleep_loop import loop as deepsleep
    from memory.execution_report import hold_execution_report

    episode_sram.open_bucket("ep-practice-deepsleep", pipeline_id=0)
    hold_execution_report(
        ExecutionReport(
            episode_id="ep-practice-deepsleep",
            build_task="build a parser practice artifact",
            pass_gate=("practice battery passes",),
            did=("changed parser.py",),
            did_not=(),
            succeeded=("practice battery passes",),
            failed=(),
            verification={"all_green": True},
            origin_opened_by="omega_practice",
            omega_intent="ACCEPT",
            omega_reason="The practice gate is satisfied.",
            metadata={
                "practice_domain": "compiler-design",
                "practice_grounding_pending": True,
            },
        )
    )
    grounded = []
    monkeypatch.setattr(
        "pressure_loop.practice_mint.apply_practice_success",
        lambda domain, *, episode_id: grounded.append((domain, episode_id)) or 0.6,
    )

    deepsleep._ground_practice_report_from_sram("ep-practice-deepsleep")
    deepsleep._ground_practice_report_from_sram("ep-practice-deepsleep")

    assert grounded == [("compiler-design", "ep-practice-deepsleep")]
    report = current_execution_report()
    assert report is not None
    assert report.metadata["practice_grounding_pending"] is False
    assert report.metadata["practice_grounded"] is True
