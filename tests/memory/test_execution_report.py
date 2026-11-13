from __future__ import annotations

import pytest

from memory import episode_sram
from memory.execution_report import ExecutionReport, current_execution_report, hold_execution_report


def _report() -> ExecutionReport:
    return ExecutionReport(
        episode_id="ep-build",
        build_task="Implement the portable launcher",
        pass_gate=("launcher uses .venv-brain", "missing venv fails closed"),
        did=("updated scripts/hud-only.bat",),
        did_not=("boot the HUD",),
        succeeded=("portable interpreter test passed",),
        failed=("live boot not run",),
        summary="Portable launcher implemented; live boot remains.",
        progress_digest="Changed the launcher and ran its focused contract test.",
        files_changed=("scripts/hud-only.bat",),
        verification={"all_green": False},
        original_request="Fix the launcher and prove it.",
        origin_opened_by="operator_queue",
        rounds_used=1,
    )


def test_execution_report_requires_complete_restart_contract() -> None:
    with pytest.raises(ValueError, match="pass_gate"):
        ExecutionReport(
            episode_id="ep-build",
            build_task="Implement the portable launcher",
            pass_gate=(),
            did=("updated launcher",),
            did_not=(),
            succeeded=("test passed",),
            failed=(),
        )


def test_execution_report_lands_in_open_episode_sram_as_dev_memory() -> None:
    episode_sram.open_bucket("ep-build", pipeline_id=0)

    ref = hold_execution_report(_report())

    assert ref == "sram:ep-build:0"
    pending = episode_sram.pending()
    assert len(pending) == 1
    assert pending[0].kind == "memory"
    assert pending[0].envelope.coord == [11, 1, 0]
    assert pending[0].envelope.type == "execution_report"
    assert pending[0].envelope.payload["build_task"] == "Implement the portable launcher"
    assert pending[0].envelope.payload["pass_gate"] == [
        "launcher uses .venv-brain",
        "missing venv fails closed",
    ]
    assert current_execution_report() == _report()


def test_execution_report_cannot_be_stashed_without_an_episode() -> None:
    with pytest.raises(RuntimeError, match="no episode SRAM bucket is open"):
        hold_execution_report(_report())
