from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from tests.helpers.motor_program_harness import MotorProgramHarness
from tests.helpers.project_runner import put_pytest_on_path

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


@pytest.fixture(autouse=True)
def _authorized_operator_tool_state(tmp_path, tmp_path_factory, monkeypatch):
    """Exercise the explicitly authorized write path without live permission state.

    The builds run their project's ``pytest -q`` with the machine's toolchain, so the
    project gets a runner on PATH rather than depending on the host having one."""
    from auto_loop.execute import chat_permissions

    monkeypatch.setattr(chat_permissions, "CHAT_PERMISSION_PATH", tmp_path / "permission-mode.json")
    chat_permissions.set_chat_permission_mode("always-allow")
    monkeypatch.setenv("OMEGA_AUTONOMOUS_SKILL_EXEC", "on")
    put_pytest_on_path(tmp_path_factory.mktemp("project-runner"), monkeypatch)


_MUTABLE_TARGETS = (
    "memory/200_knowledge",
    "memory/400_situational",
    "memory/500_tools",
    "memory/600_journal",
    "memory/900_temporal",
    "memory/source_ledger/artifacts",
    "memory/recall_verifier/verdict_log.jsonl",
    "memory/indexes/coord_index.json",
    "memory/taxonomy_store",
    "memory/dme/coordinate_registry.json",
    "memory/dme/hop_plane.bin",
    "memory/dme/hop_plane_strong.bin",
    "memory/dme/hop_plane_weak.bin",
    "memory/dme/manifest.json",
    "memory/dme/sram.bin",
    "memory/dme/sram_index.json",
    "memory/dme/sram_meta.json",
    "memory/sense_registry",
    "memory/1100_dev",
    "guardian/state/staging",
    "guardian/state/quarantine",
    "guardian/state/queues",
    "guardian/state/routing_edge_catalog.jsonl",
    "guardian/state/sandbox/rollback",
    "guardian/state/encoding_journal.jsonl",
    "guardian/state/knowledge_z_index.json",
    "guardian/state/strength_index.json",
    "deepsleep_loop/state/pathway_performance.json",
)


def _real_roots() -> tuple[Path, ...]:
    """Return unpatched checkouts whose durable state the harness must not mutate."""
    checkout = Path(__file__).resolve().parents[2]
    roots = {checkout}
    from core.paths import OMEGA_ROOT

    operational = Path(OMEGA_ROOT)
    if operational.is_dir():
        roots.add(operational.resolve())
    return tuple(sorted(roots, key=lambda path: str(path).casefold()))


def _real_state_snapshot() -> dict[str, str]:
    """Hash names and bytes without consulting any monkeypatched path global."""
    snapshot: dict[str, str] = {}
    for root in _real_roots():
        root_key = str(root.resolve())
        for relative in _MUTABLE_TARGETS:
            target = root / relative
            target_key = f"{root_key}::{relative}"
            if not target.exists():
                snapshot[target_key] = "<missing>"
                continue
            if target.is_file():
                snapshot[target_key] = hashlib.sha256(target.read_bytes()).hexdigest()
                continue
            snapshot[target_key] = "<directory>"
            for path in sorted(
                (candidate for candidate in target.rglob("*") if candidate.is_file()),
                key=lambda candidate: candidate.as_posix().casefold(),
            ):
                child = path.relative_to(target).as_posix()
                snapshot[f"{target_key}/{child}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


@pytest.fixture(autouse=True)
def real_omega_state_is_immutable_during_e2e():
    before = _real_state_snapshot()
    yield
    after = _real_state_snapshot()
    assert after == before, "motor E2E escaped its tmp root and changed real Omega state"


@pytest.fixture
def harness(monkeypatch, tmp_path) -> MotorProgramHarness:
    # Deliberately differ from tests/core/conftest.py's tmp memory root.  The
    # harness must own its isolation instead of passing only because a parent
    # pytest fixture happened to redirect the same globals first.
    return MotorProgramHarness(monkeypatch=monkeypatch, root=tmp_path / "motor-harness")


def test_harness_owns_every_cached_writable_memory_path(harness):
    import core.paths as core_paths
    import hud.chat_history as chat_history
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.engine.walker_loader as walker_loader
    import memory.knowledge_router as knowledge_router
    import memory.promotion as promotion
    import memory.queues as queues
    import memory.rollback as rollback
    import memory.routing.edge_case_catalog as edge_case_catalog

    memory_root = harness.memory_root
    state_root = harness.root / "guardian" / "state"
    index_path = memory_root / "indexes" / "coord_index.json"
    assert validate_write.MEMORY_ROOT == memory_root
    assert validate_write.TMP_DIR == memory_root / "tmp_writes"
    assert validate_write.INDEX_PATH == index_path
    assert walker.MEMORY_ROOT == memory_root
    assert walker.INDEX_PATH == index_path
    assert walker_loader.MEMORY_ROOT == memory_root
    assert walker_loader.INDEX_PATH == index_path
    assert promotion.STAGING_DIR == state_root / "staging"
    assert promotion.QUARANTINE_DIR == state_root / "quarantine"
    assert promotion.QUARANTINE_INDEX == (state_root / "quarantine" / "quarantine_index.json")
    assert knowledge_router._KNOWLEDGE_LOBE_DIR == memory_root / "200_knowledge"
    assert queues._QUEUES_DIR == state_root / "queues"
    assert queues.REFLECTION_QUEUE_PATH == (state_root / "queues" / "reflection_eligible.jsonl")
    assert queues.DEEPSLEEP_QUEUE_PATH == (state_root / "queues" / "deepsleep_candidates.jsonl")
    assert edge_case_catalog._DEFAULT_PATH == state_root / "routing_edge_catalog.jsonl"
    assert rollback.OMEGA_ROOT == harness.root
    assert chat_history.GUARDIAN_STATE_ROOT == state_root
    assert chat_history._HISTORY_PATH == state_root / "hud" / "chat_history.json"
    assert core_paths.MEMORY_ROOT == memory_root
    assert core_paths.MEMORY_TAXONOMY_ROOT == memory_root / "taxonomy_store"


def test_post_commit_failure_rolls_back_only_harness_canonical_state(harness, monkeypatch):
    import memory.encoding as encoding
    import memory.rollback as rollback
    from memory.api import CanonicalEnvelope, write_memory
    from memory.encoding import EncodingResult

    real_rollback = rollback.rollback_canonical_write
    observed_rollbacks = []

    def capture_rollback(memory_dict, final_path):
        result = real_rollback(memory_dict, final_path)
        observed_rollbacks.append((dict(memory_dict), Path(final_path), dict(result)))
        return result

    monkeypatch.setattr(rollback, "rollback_canonical_write", capture_rollback)
    monkeypatch.setattr(
        encoding,
        "encode_committed_memory",
        lambda *_args, **_kwargs: EncodingResult(
            ok=False,
            failed_step="reflection_queue",
            failure_reason="forced harness post-commit failure",
        ),
    )
    before = _real_state_snapshot()
    with pytest.raises(RuntimeError, match="direct memory commit failed"):
        write_memory(
            CanonicalEnvelope(
                cycle_id="motor-harness-rollback",
                coord=[4, 5, 0],
                type="motor_harness_rollback_probe",
                payload={
                    "title": "motor harness rollback probe",
                    "summary": "force rollback after a temporary canonical commit",
                    "text": "this record must not survive its failed post-commit step",
                    "subtopic": "motor_harness_rollback_probe",
                },
                source="tests.motor_program_harness",
                confidence=1.0,
            ),
            propose=False,
            bypass_guardian=True,
        )
    after = _real_state_snapshot()

    assert after == before
    [(memory_dict, final_path, rollback_result)] = observed_rollbacks
    assert final_path.is_relative_to(harness.memory_root)
    assert not final_path.exists()
    assert rollback_result["memory_file_removed"] is True
    assert rollback_result["index_entry_removed"] is True
    assert rollback_result["errors"] == []
    index_path = harness.memory_root / "indexes" / "coord_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert memory_dict["id"] not in index


def test_agent_harness_leases_numeric_worker_and_publishes_bound_ready(harness, monkeypatch):
    from core.pipeline_state import read_envelope
    from pressure_loop import cognition_queue as cq
    from pressure_loop.contributors import make_contributor

    row = make_contributor("omega", "agent_motor_dispatch", 1,
        {"cycle_id": "harness-agent-binding", "causal_parent_episode_id": "parent"})
    assert cq.enqueue_input(row, operator_activity=False)
    seen = []

    def ignite(store, ignition):
        seen.append(ignition)
        assert store is harness.store
        assert ignition.pipeline_id == 2
        assert ignition.work_kind == "agent_continuation"
        envelope = read_envelope(2)
        assert envelope.state == "READY"
        assert envelope.queue_id == ignition.dominant_contributor.queue_id
        assert envelope.work_kind == ignition.work_kind
        assert not cq.acknowledge_input(ignition.dominant_contributor, 1)
        assert cq.acknowledge_input(ignition.dominant_contributor, 2)
        return ignition

    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    episode = harness._run_agent_episode()
    assert seen == [episode]
    assert cq.queue_depth() == 0


def test_qwen_is_a_limb_not_a_second_planner(harness):
    episode = harness.run_code_episode(
        objective="build a tested statistics package",
        qwen_result={
            "status": "success",
            "summary": "implemented the package and ran its test suite",
            "files_changed": ["src/stats.py", "tests/test_stats.py"],
            "diff_summary": {
                "files_changed": 2,
                "paths": ["src/stats.py", "tests/test_stats.py"],
            },
            "evidence": {
                "diff_summary": {
                    "files_changed": 2,
                    "paths": ["src/stats.py", "tests/test_stats.py"],
                },
                "tests_run": ["pytest -q"],
            },
            "tool_calls": [
                {
                    "tool_call_id": "write-stats",
                    "tool_name": "write_file",
                    "args": {"path": "src/stats.py"},
                    "status": "success",
                },
                {
                    "tool_call_id": "write-tests",
                    "tool_name": "write_file",
                    "args": {"path": "tests/test_stats.py"},
                    "status": "success",
                },
                {
                    "tool_call_id": "run-pytest",
                    "tool_name": "run_tests",
                    "args": {"command": "pytest -q"},
                    "status": "success",
                    "stdout": "1 passed",
                    "result": {"exit_code": 0},
                },
            ],
            "executor_identity": {"name": "Qwen", "role": "new mind"},
            "replacement_authority": {
                "objective": "replace Omega's objective",
                "motor_program": {"program_id": "executor-owned"},
            },
        },
    )
    program = episode.motor_program
    assert episode.execution_evidence["program_id"] == episode.motor_program["program_id"]
    assert episode.execution_evidence["episode_id"] == program["episode_id"]
    assert episode.attempt_memory["program_id"] == program["program_id"]
    assert episode.attempt_memory["outcome_id"] == program["outcome_id"]
    assert episode.attempt_memory["episode_id"] == program["episode_id"]
    assert (
        episode.attempt_memory["motor_program"]["parent_program_id"] == program["parent_program_id"]
    )
    assert (
        episode.attempt_memory["motor_program"]["outcome_contract"] == program["outcome_contract"]
    )
    assert "executor_identity" not in episode.attempt_memory
    assert episode.qwen_dispatch["objective"] == "build a tested statistics package"
    assert (
        episode.qwen_dispatch["motor_program"]["outcome_contract"]["objective"]
        == "build a tested statistics package"
    )
    assert "## Objective\nbuild a tested statistics package" in episode.qwen_directive
    assert "replace Omega's objective" not in episode.qwen_directive
    raw = episode.qwen_result["raw_bridge_result"]
    assert "executor_identity" not in raw
    assert "replacement_authority" not in raw

    report = episode.execution_report
    assert report["build_task"] == "build a tested statistics package"
    # The pass gate is the bound execution contract's acceptance criteria.
    assert report["pass_gate"] == program["outcome_contract"]["acceptance_criteria"]
    assert report["metadata"]["status"] == "success"
    assert report["metadata"]["objective_complete"] is True
    assert report["verification"]["all_green"] is True
    assert "executor_identity" not in report
    assert "replacement_authority" not in report
    assert set(report["files_changed"]) == {"src/stats.py", "tests/test_stats.py"}
    assert set(episode.project_files) == set(report["files_changed"])


def test_failed_executor_check_stays_red_without_fabricated_receipts(harness):
    episode = harness.run_code_episode(
        objective="build a tested statistics package",
        qwen_result={
            "status": "success",
            "files_changed": ["src/stats.py", "tests/test_stats.py"],
            "tool_calls": [
                {
                    "tool_call_id": "run-pytest-red",
                    "tool_name": "run_tests",
                    "args": {"command": "pytest -q"},
                    "status": "failure",
                    "stderr": "1 failed",
                    "result": {"exit_code": 1},
                }
            ],
        },
    )

    [verification] = episode.execution_evidence["verification_results"]
    assert verification["requested"] in episode.motor_program["evidence_contract"]["required_checks"]
    assert verification["status"] == "failed"
    assert verification["failure_reason"] == "executor check failed"
    assert verification["executor_provenance"]["source"] == "qwen_tool_call"
    assert verification["executor_provenance"]["tool_call_id"] == "run-test_stats"
    assert verification["executor_provenance"]["exit_code"] == 1
    assert episode.pathway_outcome["verification_verdict"] != "verified"
    assert episode.pathway_outcome["knowledge_delta"] == 0.0
    assert episode.pathway_outcome["application_delta"] == 0.0
    assert episode.execution_report["verification"]["all_green"] is False
    assert len(episode.dev_artifacts) == 2
    assert {record["episode_id"] for record in episode.dev_artifacts} == {
        episode.motor_program["episode_id"]
    }
    assert {record["relative_path"] for record in episode.dev_artifacts} == {
        "src/stats.py",
        "tests/test_stats.py",
    }
    assert all("content_sha256" in record for record in episode.dev_artifacts)


@pytest.mark.parametrize("tests_pass", [True, False])
def test_ordinary_operator_build_uses_real_auto_tools_and_durable_job(harness, tests_pass):
    from auto_loop.execute.job_control import get_job

    episode = harness.run_operator_tools_episode(
        objective="build a tested statistics package", tests_pass=tests_pass
    )
    chat_episode = harness.last_chat_episode
    assert chat_episode is not None and chat_episode.pipeline_id == 0
    chat_outputs = chat_episode.auto["outputs"]
    assert chat_outputs["specialist_handoff"]["lane"] == "agent"
    assert chat_outputs["deterministic_response_kind"] == "specialist_lane_handoff"
    assert episode.pipeline_id == 2
    assert not (episode.meta or {}).get("motor_program")
    outputs = episode.auto["outputs"]
    calls = outputs["operator_tool_calls"]
    assert [call["tool_name"] for call in calls] == [
        "start_job", "task_plan", "write_file", "write_file", "run_tests"]
    assert (harness.workspace_root / "src/stats.py").is_file()
    assert (harness.workspace_root / "tests/test_stats.py").is_file()
    command = calls[-1]
    assert command["status"] == ("success" if tests_pass else "failure")
    assert command["result"]["exit_code"] == (0 if tests_pass else 1)
    assert ("passed" if tests_pass else "failed") in command["output"]
    job = get_job(calls[0]["job_id"])
    assert job.objective == "build a tested statistics package"
    assert Path(job.workspace_root) == harness.workspace_root
    assert all(call["job_id"] == job.job_id for call in calls)
    assert job.owner_cycle_id == ""
    artifacts = harness._persisted_dev_artifacts(episode.episode_id)
    assert {record["relative_path"] for record in artifacts} == {"src/stats.py", "tests/test_stats.py"}
    assert all(record["content"] and record["content_sha256"] for record in artifacts)
