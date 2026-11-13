"""Deterministic adapter around the real cognition-chain motor-program path."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import Path
import sys
from typing import Any

import cognition_chain
from auto_loop.motor_adapter import execution_evidence_from_fold
from memory.pathway_performance import recent_pathway_outcomes
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import EpisodeStore


@dataclass(frozen=True)
class EpisodeResultView:
    omega_result: dict[str, Any]
    motor_program: dict[str, Any]
    execution_evidence: dict[str, Any]
    attempt_memory: dict[str, Any]
    pathway_outcome: dict[str, Any]
    qwen_directive: str
    qwen_dispatch: dict[str, Any]
    qwen_result: dict[str, Any]
    execution_report: dict[str, Any]
    project_files: dict[str, bytes]
    dev_artifacts: tuple[dict[str, Any], ...]


class MotorProgramHarness:
    """Drive real episodes while replacing only external executor/model seams."""

    def __init__(self, *, monkeypatch, root: Path) -> None:
        self._monkeypatch = monkeypatch
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_root = self.root / "memory"
        self.memory_root.mkdir(parents=True, exist_ok=True)
        index_root = self.memory_root / "indexes"
        index_root.mkdir(parents=True, exist_ok=True)
        (index_root / "coord_index.json").write_text("{}\n", encoding="utf-8")
        self.workspace_root = self.root.parent / "workspace"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.completed_root = self.root / "completed"
        self.artifacts_root = self.memory_root / "1100_dev" / "artifacts"
        self.mastery_path = self.root / "state" / "mastery.json"
        self.pathway_path = self.root / "state" / "pathway_performance.json"
        self.store = EpisodeStore(
            reflection_window_path=self.root / "state" / "reflection_window.json",
            trajectory_path=self.root / "state" / "trajectory.json",
        )
        self._omega_payloads: list[dict[str, Any]] = []
        self._qwen_results: list[dict[str, Any]] = []
        self._operator_tool_results: list[dict[str, Any]] = []
        self._episode_counter = 0
        self._current_qwen_call: tuple[str, dict[str, Any]] | None = None
        self.rollback_captures: list[dict[str, Any]] = []
        self._install_isolation()
        self._install_external_seams()

    def run_code_episode(
        self,
        *,
        objective: str,
        qwen_result: dict[str, Any],
    ) -> EpisodeResultView:
        """Machine-minted skill work retains motor authority and evidence coverage."""
        self._qwen_results.append(deepcopy(qwen_result))
        self._omega_payloads.append(self._verified_apply_payload())
        return self._run(
            source="omega",
            opened_by="execution_gap",
            metadata={"lane": "agent", "message": objective, "directive_intent": "USE_SKILL"},
        )

    def _install_isolation(self) -> None:
        import config.loader as config_loader
        import core.dev_rollback_capture as dev_rollback_capture
        import core.hud_path_guard as hud_path_guard
        import core.paths as core_paths
        import core.workspace_port as workspace_port
        import deepsleep_loop.mastery_store as mastery_store
        import guardian.data_logger as data_logger
        import guardian.telemetry.writer as telemetry_writer
        import hud.chat_history as chat_history
        import memory.dev_project_store as dev_project_store
        import memory.engine.reindex as reindex
        import memory.engine.validate_write as validate_write
        import memory.engine.walker as walker
        import memory.engine.walker_loader as walker_loader
        import memory.knowledge_router as knowledge_router
        import memory.pathway_performance as pathway_performance
        import memory.promotion as promotion
        import memory.queues as queues
        import memory.routing.edge_case_catalog as edge_case_catalog
        import memory.taxonomy.paths as taxonomy_paths
        import pressure_loop.pressure as pressure
        import pressure_loop.policy as policy
        import pressure_loop.proactive_chat_messages as proactive_chat_messages

        original_loader = config_loader.load_subsystem_bundle

        def isolated_bundle(name: str):
            bundle = deepcopy(original_loader(name))
            if name == "cognition_chain":
                chain = bundle.setdefault("config", {}).setdefault("cognition_chain", {})
                chain.setdefault("motor_programs", {})["shadow_mode"] = False
            return bundle

        self._monkeypatch.setattr(config_loader, "load_subsystem_bundle", isolated_bundle)
        # The harness owns the full active cascade. Execution contracts are the only
        # configurable phase; cognition and per-episode DeepSleep are mandatory.
        self._monkeypatch.setattr(
            "meta_loop.execution_contract.motor_programs_enabled", lambda policy=None: True
        )
        self._monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(self.workspace_root))
        self._monkeypatch.setenv("OMEGA_OPERATOR_COMPLETED", str(self.completed_root))
        self._monkeypatch.setattr(core_paths, "MEMORY_ROOT", self.memory_root)
        self._monkeypatch.setattr(core_paths, "GUARDIAN_LOGS_ROOT", self.root / "guardian" / "logs")
        self._monkeypatch.setattr(
            core_paths, "GUARDIAN_STATE_ROOT", self.root / "guardian" / "state"
        )
        self._monkeypatch.setattr(core_paths, "LEDGER_ROOT", self.root / "guardian" / "ledger")
        self._monkeypatch.setattr(
            core_paths, "MEMORY_TAXONOMY_ROOT", self.memory_root / "taxonomy_store"
        )
        self._monkeypatch.setattr(
            core_paths,
            "STAGING_ROOT",
            self.root / "guardian" / "state" / "staging",
        )
        self._monkeypatch.setattr(
            core_paths,
            "SANDBOX_RUNS_ROOT",
            self.root / "guardian" / "state" / "sandbox" / "runs",
        )
        self._monkeypatch.setattr(
            core_paths,
            "TEST_SCRATCH_ROOT",
            self.root / "guardian" / "state" / "test_scratch",
        )
        self._monkeypatch.setattr(
            core_paths, "FACE_THUMBNAILS_ROOT", self.memory_root / "700_vision" / "images"
        )
        self._monkeypatch.setattr(
            core_paths, "COGNITIVE_TOOL_LOBE_ROOT", self.memory_root / "500_tools" / "cognitive"
        )
        self._monkeypatch.setattr(
            core_paths,
            "TOOL_AUTHORITY_AUDIT_LOG",
            self.root / "guardian" / "logs" / "tool_authority.jsonl",
        )
        self._monkeypatch.setattr(hud_path_guard, "OMEGA_ROOT", self.root)
        self._monkeypatch.setattr(
            hud_path_guard, "GUARDIAN_STATE_ROOT", self.root / "guardian" / "state"
        )
        self._monkeypatch.setattr(
            hud_path_guard, "_operator_workspace_root", lambda: self.workspace_root
        )
        self._monkeypatch.setattr(dev_project_store, "OMEGA_ROOT", self.root)
        self._monkeypatch.setattr(dev_rollback_capture, "OMEGA_ROOT", self.root)
        real_record_file_snapshots = dev_rollback_capture.record_file_snapshots

        def capture_file_snapshots(*args, **kwargs):
            records = real_record_file_snapshots(*args, **kwargs)
            self.rollback_captures.extend(deepcopy(records))
            return records

        self._monkeypatch.setattr(
            dev_rollback_capture,
            "record_file_snapshots",
            capture_file_snapshots,
        )
        self._monkeypatch.setattr(workspace_port, "OMEGA_ROOT", self.root)
        self._monkeypatch.setattr(
            workspace_port,
            "_PORT_STATE_PATH",
            self.root / "guardian" / "state" / "workspace_port_pending.json",
        )
        self._monkeypatch.setattr(workspace_port, "_pending", None)
        self._monkeypatch.setattr(pressure, "_RESEED_STAGING", {})
        self._monkeypatch.setattr(pressure, "_RESEED_SUPPRESS", {})
        self._monkeypatch.setattr(data_logger, "_LOG_DIR", self.root / "guardian" / "logs")
        self._monkeypatch.setattr(
            telemetry_writer, "TELEMETRY_DIR", self.root / "guardian" / "telemetry"
        )
        self._monkeypatch.setattr(
            telemetry_writer, "FAILURE_LOG", self.root / "guardian" / "logs" / "failures.jsonl"
        )
        self._monkeypatch.setattr(
            chat_history,
            "_HISTORY_PATH",
            self.root / "guardian" / "state" / "hud" / "chat_history.json",
        )
        self._monkeypatch.setattr(
            chat_history, "GUARDIAN_STATE_ROOT", self.root / "guardian" / "state"
        )
        # The durable operator lines (cut 7) — the agent line is now part of the
        # cascade this harness drives (cut 9), so it must be per-harness or one
        # test's dispatched build would be leased by the next test's episode.
        import pressure_loop.cognition_queue as operator_queue

        self._monkeypatch.setattr(
            operator_queue,
            "_queue_path",
            lambda: self.root / "guardian" / "state" / "operator_input_queue.json",
        )
        operator_queue._reset_cache_for_tests()
        self._monkeypatch.setattr(proactive_chat_messages, "_MESSAGES", [])
        self._monkeypatch.setattr(proactive_chat_messages, "_LOADED", False)
        self._monkeypatch.setattr(
            proactive_chat_messages,
            "_store_path",
            lambda: self.root / "guardian" / "state" / "proactive_chat_messages.json",
        )
        self._monkeypatch.setattr(reindex, "MEMORY_ROOT", self.memory_root)
        self._monkeypatch.setattr(
            reindex, "INDEX_PATH", self.memory_root / "indexes" / "coord_index.json"
        )
        self._monkeypatch.setattr(validate_write, "MEMORY_ROOT", self.memory_root)
        self._monkeypatch.setattr(validate_write, "TMP_DIR", self.memory_root / "tmp_writes")
        self._monkeypatch.setattr(
            validate_write, "INDEX_PATH", self.memory_root / "indexes" / "coord_index.json"
        )
        self._monkeypatch.setattr(walker, "MEMORY_ROOT", self.memory_root)
        self._monkeypatch.setattr(
            walker, "INDEX_PATH", self.memory_root / "indexes" / "coord_index.json"
        )
        self._monkeypatch.setattr(walker_loader, "MEMORY_ROOT", self.memory_root)
        self._monkeypatch.setattr(
            walker_loader, "INDEX_PATH", self.memory_root / "indexes" / "coord_index.json"
        )
        staging_root = self.root / "guardian" / "state" / "staging"
        quarantine_root = self.root / "guardian" / "state" / "quarantine"
        self._monkeypatch.setattr(promotion, "STAGING_DIR", staging_root)
        self._monkeypatch.setattr(promotion, "QUARANTINE_DIR", quarantine_root)
        self._monkeypatch.setattr(
            promotion, "QUARANTINE_INDEX", quarantine_root / "quarantine_index.json"
        )
        self._monkeypatch.setattr(
            knowledge_router, "_KNOWLEDGE_LOBE_DIR", self.memory_root / "200_knowledge"
        )
        queues_root = self.root / "guardian" / "state" / "queues"
        self._monkeypatch.setattr(queues, "_QUEUES_DIR", queues_root)
        self._monkeypatch.setattr(
            queues, "REFLECTION_QUEUE_PATH", queues_root / "reflection_eligible.jsonl"
        )
        self._monkeypatch.setattr(
            queues, "DEEPSLEEP_QUEUE_PATH", queues_root / "deepsleep_candidates.jsonl"
        )
        self._monkeypatch.setattr(
            edge_case_catalog,
            "_DEFAULT_PATH",
            self.root / "guardian" / "state" / "routing_edge_catalog.jsonl",
        )
        self._monkeypatch.setattr(
            taxonomy_paths, "MEMORY_TAXONOMY_ROOT", self.memory_root / "taxonomy_store"
        )
        for module_name in (
            "memory.api",
            "memory.encoding",
            "memory.recall_verifier",
            "memory.rollback",
            "memory.sense_registry",
            "memory.source_ledger.store",
            "memory.taxonomy.canonical_events",
            "memory.taxonomy.compiler",
        ):
            module = importlib.import_module(module_name)
            if hasattr(module, "OMEGA_ROOT"):
                self._monkeypatch.setattr(module, "OMEGA_ROOT", self.root)
        self._monkeypatch.setattr(
            policy, "deepsleep_mastery_path", lambda policy=None: self.mastery_path
        )
        for name in (
            "deepsleep_journal_path",
            "deepsleep_identity_path",
            "deepsleep_semantic_path",
            "deepsleep_trajectory_path",
            "deepsleep_failure_path",
            "deepsleep_projection_path",
            "deepsleep_query_path",
            "episode_journal_path",
        ):
            self._monkeypatch.setattr(
                policy,
                name,
                lambda policy=None, _name=name: self.root / "state" / f"{_name}.json",
            )
        self._monkeypatch.setattr(mastery_store, "_repo_root", lambda: self.root)
        self._monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", self.pathway_path)
        state_paths = {
            "memory.strength_index": ("STRENGTH_INDEX_PATH", "strength_index.json"),
            "pressure_loop.open_questions": ("OPEN_QUESTIONS_PATH", "open_questions.jsonl"),
            "pressure_loop.signal_ledger": ("_LEDGER_PATH", "signals.jsonl"),
            "memory.encoding_journal": ("JOURNAL_PATH", "encoding_journal.jsonl"),
            "memory.execution_experience": ("_LEDGER_PATH", "execution_experience.json"),
            "memory.knowledge_router": ("_INDEX_PATH", "knowledge_z_index.json"),
        }
        for module_name, (attribute, filename) in state_paths.items():
            module = importlib.import_module(module_name)
            self._monkeypatch.setattr(module, attribute, self.root / "state" / filename)

    def _install_external_seams(self) -> None:
        skill_module = importlib.import_module("auto_loop.skill.run")

        def runtime_generate(**kwargs):
            operation = str(kwargs.get("operation") or "")
            if operation == "auto_response" and self._operator_tool_results:
                return self._operator_tool_results.pop(0)
            if operation == "episode_cognition":
                return {"text": json.dumps(self._omega_payloads.pop(0))}
            raise AssertionError(f"unexpected model operation: {operation}")

        def qwen_invoker(_directive, _dispatch, **_kwargs):
            self._current_qwen_call = (
                str(_directive),
                deepcopy(_dispatch.to_dict()),
            )
            requested = deepcopy(self._qwen_results.pop(0))
            tests_pass = self._executor_checks_pass(requested.get("tool_calls") or ())
            objective = str(getattr(_dispatch, "objective", "") or "")
            return self._run_executor_subprocess(
                objective=objective,
                tests_pass=tests_pass,
                summary=str(requested.get("summary") or "executor run complete"),
                # The DISPATCH names the episode that is executing. Since cut 9
                # that is not the episode that planned (an operator build runs
                # in its own agent episode), and the builder's artifact
                # snapshots must be attributed to the executing one or the
                # episode's own files are invisible to it.
                episode_id=str(getattr(_dispatch, "episode_id", "") or ""),
                dispatch=_dispatch,
                directive=str(_directive),
            )

        self._monkeypatch.setattr("runtime.socket.invoke_runtime_generate", runtime_generate)
        self._monkeypatch.setattr(skill_module, "default_brain_skill_invoker", qwen_invoker)
        self._monkeypatch.setattr(
            skill_module, "operator_workspace_root", lambda: str(self.workspace_root)
        )
        self._monkeypatch.setattr(
            "core.hud_path_guard._operator_workspace_root",
            lambda: self.workspace_root,
        )

    def _run_agent_episode(self):
        """Follow a dispatched build through its dedicated agent FIFO on worker two.

        This is the pressure tick's job in production: lease the agent line and
        ignite it. The harness does it inline so the e2e still covers ONE
        operator ask end to end across its chat and agent episodes.
        """
        from pressure_loop.cognition_queue import lease_next_input, work_kind_for_contributor

        row = lease_next_input(2)
        assert row is not None, "the dispatched build never reached the agent FIFO"
        ignition = EpisodeIgnition(
            pipeline_id=2,
            work_kind=work_kind_for_contributor(row),
            episode_id=f"motor-harness-agent-{self._episode_counter}",
            aggregate_pressure=1.0,
            opened_by=row.type,
            dominant_contributor=row,
            contributors=(row,),
            trajectory_snapshot=None,
        )
        from core.pipeline_state import mark_ready

        mark_ready(2, ignition)
        episode = cognition_chain.ignite_episode(self.store, ignition)
        assert episode is not None
        return episode

    def _run(self, *, source: str, opened_by: str, metadata: dict[str, Any], operator_tools: bool = False):
        from core.pipeline_state import mark_ready
        from core.pipelines import lane_for_contributor, pipeline_for_lane
        from pressure_loop.cognition_queue import enqueue_input, lease_next_input, work_kind_for_contributor

        self._episode_counter += 1
        contributor = make_contributor(source, opened_by, 0.95,
            {"cycle_id": f"motor-harness-cycle-{self._episode_counter}", **metadata})
        enqueue_input(contributor, operator_activity=False)
        pipeline_id = pipeline_for_lane(lane_for_contributor(contributor))
        contributor = lease_next_input(pipeline_id)
        assert contributor is not None
        ignition = EpisodeIgnition(
            pipeline_id=pipeline_id,
            work_kind=work_kind_for_contributor(contributor),
            episode_id=f"motor-harness-{self._episode_counter}",
            aggregate_pressure=0.95,
            opened_by=opened_by,
            dominant_contributor=contributor,
            contributors=(contributor,),
            trajectory_snapshot=None,
        )
        mark_ready(pipeline_id, ignition)
        episode = cognition_chain.ignite_episode(self.store, ignition)
        assert episode is not None
        self.last_chat_episode = episode if pipeline_id == 0 else None
        outputs = dict((episode.auto or {}).get("outputs") or {})
        specialist = dict(outputs.get("specialist_handoff") or {})
        if specialist.get("lane") == "agent":
            # Global A's Coding Agent handoff is a different path from the
            # older motor-program dispatch. Follow the durable Agent FIFO in
            # its own pressure episode before returning this operator build.
            episode = self._run_agent_episode()
        elif specialist:
            raise AssertionError(f"unexpected specialist lane in coding harness: {specialist}")
        else:
            from pressure_loop.agent_lane import handed_off

            if handed_off(episode) is not None:
                # A skill-backed motor program also moves to its Agent episode.
                episode = self._run_agent_episode()
        if operator_tools:
            return episode
        omega_result = dict((episode.global_ or {}).get("omega_result") or {})
        motor_program = dict((episode.meta or {}).get("motor_program") or {})
        assert motor_program, {
            "meta_action": dict((episode.meta or {}).get("execution_action_pack") or {}).get(
                "action"
            ),
            "meta_keys": sorted((episode.meta or {}).keys()),
            "motor_planning_failure": dict(
                (episode.meta or {}).get("motor_planning_failure") or {}
            ),
            "auto_status": dict(episode.auto or {}).get("status"),
            "auto_action": dict(episode.auto or {}).get("action"),
        }
        evidence = execution_evidence_from_fold(episode).to_dict()
        outputs = dict((episode.auto or {}).get("outputs") or {})
        qwen_result = dict(outputs.get("qwen_skill_result") or outputs.get("skill_result") or {})
        attempt = self._attempt_memory(motor_program["program_id"])
        outcomes = recent_pathway_outcomes(episode_ids=[episode.episode_id], cap=5)
        pathway_outcome = outcomes[-1] if outcomes else {}
        qwen_directive = ""
        qwen_dispatch: dict[str, Any] = {}
        if self._current_qwen_call is not None:
            qwen_directive, qwen_dispatch = self._current_qwen_call
        packet = dict((episode.global_ or {}).get("omega_packet") or {})
        execution_report = dict(packet.get("execution_report") or {})
        dev_artifacts = self._persisted_dev_artifacts(episode.episode_id)
        project_files = {
            str(record["relative_path"]): str(record.get("content") or "").encode("utf-8")
            for record in dev_artifacts
            if not record.get("deleted")
        }
        self._current_qwen_call = None
        return EpisodeResultView(
            omega_result=omega_result,
            motor_program=motor_program,
            execution_evidence=evidence,
            attempt_memory=attempt,
            pathway_outcome=pathway_outcome,
            qwen_directive=qwen_directive,
            qwen_dispatch=qwen_dispatch,
            qwen_result=qwen_result,
            execution_report=execution_report,
            project_files=project_files,
            dev_artifacts=dev_artifacts,
        )

    @staticmethod
    def _control(
        decision: str = "DONE",
        *,
        gap: str = "",
        pressure: dict[str, Any] | None = None,
        synthesis: str = "harness fold",
    ) -> dict[str, Any]:
        """Omega's strict per-episode control envelope."""
        assessment = dict.fromkeys(
            ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
            "harness episode evidence",
        )
        return {
            "assessment": {**assessment, "gap": gap, "evidence": ["harness receipt"]},
            "decision": decision,
            "pressure": pressure,
            "synthesis": synthesis,
        }

    @staticmethod
    def _verified_apply_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "control": MotorProgramHarness._control(
                synthesis="implementation and executor-owned verification are complete"
            ),
            "mastery": {
                "knowledge_mastery": 0.25,
                "application_mastery": 0.25,
                "rationale": "executor facts satisfy artifact and verification gates",
            },
            "synthesis": "implementation and executor-owned verification are complete",
            "episode_assessment": {
                "completed": True,
                "quality": 1.0,
                "novelty_score": 0.8,
                "reason": "verified evidence",
            },
            "what_changed": {
                "did": ["implemented"],
                "failed": [],
                "learned": ["finite differences"],
                "verified": ["pytest"],
            },
        }
        return payload

    @staticmethod
    def _executor_checks_pass(tool_calls: Any) -> bool:
        checks = []
        for call in tool_calls or ():
            if not isinstance(call, dict):
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            command = str(args.get("command") or "").lower()
            if "pytest" not in command:
                continue
            result = call.get("result") if isinstance(call.get("result"), dict) else {}
            checks.append(
                str(call.get("status") or "").lower() in {"success", "passed", "ok"}
                and int(result.get("exit_code", result.get("returncode", 0)) or 0) == 0
            )
        return bool(checks) and all(checks)

    def _run_executor_subprocess(
        self, *, objective: str, tests_pass: bool, summary: str, episode_id: str = "", dispatch=None, directive=""
    ) -> dict[str, Any]:
        """Script the brain's decisions; native Mistral tools produce all evidence."""
        from auto_loop.execute.brain_skill_executor import run_brain_skill_dispatch

        files = self._executor_files(objective, tests_pass)
        calls = self._executor_tool_calls(files, test_tool_name="run_tests")
        class Brain:
            def __init__(self):
                self.passes = 0
            def generate_completion(self, prompt, **kwargs):
                self.passes += 1
                if self.passes == 1:
                    return {"ok": True, "tool_calls": [{"id": "claim", "function": {
                        "name": "start_job", "arguments": '{"project_root":"."}'}}]}
                return {"ok": True, "tool_calls": calls} if self.passes == 2 else {"ok": True, "text": summary}
        result = run_brain_skill_dispatch(directive, dispatch, client=Brain(), workspace_root=self.workspace_root,
                                         cycle_id=episode_id, job_id=episode_id)
        from memory.dev_artifact_intake import hold_dev_artifact_snapshots
        hold_dev_artifact_snapshots(episode_id=episode_id, objective=objective, paths=tuple(files))
        # Normalize paths without changing the executor's actual command/result receipts.
        result["ledger"]["touched_files"] = [
            {**row, "path": Path(row["path"]).relative_to(self.workspace_root).as_posix()}
            for row in result["ledger"]["touched_files"]
        ]
        source_rel = next(iter(files))
        result["ledger"]["artifacts"] = [{"path": source_rel, "kind": "implementation",
            "sha256": hashlib.sha256((self.workspace_root / source_rel).read_bytes()).hexdigest()}]
        return result

    @staticmethod
    def _executor_files(objective, tests_pass):
        gradient = "gradient" in objective.casefold()
        source_rel = Path("src/gradient_check.py" if gradient else "src/stats.py")
        test_rel = Path("tests/test_gradient_check.py" if gradient else "tests/test_stats.py")
        if gradient:
            source = (
                "def finite_difference(fn, x, epsilon=1e-6):\n"
                "    return (fn(x + epsilon) - fn(x - epsilon)) / (2 * epsilon)\n"
            )
            expected = "4.0" if tests_pass else "999.0"
            test = (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
                "from gradient_check import finite_difference\n\n"
                "def test_quadratic_gradient():\n"
                f"    assert abs(finite_difference(lambda x: x * x, 2.0) - {expected}) < 1e-4\n"
            )
        else:
            source = (
                "def mean(values):\n"
                "    values = list(values)\n"
                "    if not values:\n"
                "        raise ValueError('values must not be empty')\n"
                "    return sum(values) / len(values)\n"
            )
            assertion = (
                "assert mean([1, 2, 3]) == 2" if tests_pass else "assert mean([1, 2, 3]) == 999"
            )
            test = (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
                "from stats import mean\n\n"
                "def test_mean():\n"
                f"    {assertion}\n"
            )
        return {source_rel.as_posix(): source, test_rel.as_posix(): test}

    @staticmethod
    def _plan_call(steps=("write the package", "run its tests")):
        # The first step of an agent task is writing its steps down; mutable work waits
        # for the plan exactly as it waits for a claimed project.
        return {"id": "plan", "type": "function", "function": {
            "name": "task_plan", "arguments": json.dumps({"steps": list(steps)})}}

    @staticmethod
    def _executor_tool_calls(files, *, test_tool_name="run_command"):
        calls = [{"id": f"write-{Path(path).name}", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": path, "content": content})}}
            for path, content in files.items()]
        test = next(path for path in files if path.startswith("tests/"))
        command = f"pytest -q {test}"
        calls.append({"id": f"run-{Path(test).stem}", "type": "function", "function": {
            "name": test_tool_name, "arguments": json.dumps({"command": command})}})
        return calls

    def run_operator_tools_episode(self, *, objective, tests_pass):
        files = self._executor_files(objective, tests_pass)
        # One agent episode runs one tool batch (operator 2026-09-14): the project claim
        # comes first in the batch, then the writes and the test run.
        self._operator_tool_results = [
            {"ok": True, "tool_calls": [{"id": "claim", "function": {
                "name": "start_job", "arguments": '{"project_root":"."}'}},
                self._plan_call()]
             + self._executor_tool_calls(files, test_tool_name="run_tests")},
        ]
        # This operator request now completes two episodes: Chat acknowledges
        # the durable handoff, then the Agent lane runs the tools. Omega's
        # mandatory fold executes once in each lane episode.
        self._omega_payloads.append(self._verified_apply_payload())
        self._omega_payloads.append(self._verified_apply_payload())
        from tests.helpers.intent_router import stub_router

        # Global A routes the operator's build request to the agent lane.
        stub_router(self._monkeypatch, "CODING_AGENT")
        return self._run(source="operator", opened_by="operator_query", metadata={"message": objective}, operator_tools=True)

    def _persisted_dev_artifacts(self, episode_id: str) -> tuple[dict[str, Any], ...]:
        """Read builder snapshots only after DeepSleep has committed the SRAM bucket."""
        records: list[dict[str, Any]] = []
        for path in self.memory_root.rglob("*.json"):
            try:
                wrapper = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            enrichment = wrapper.get("enrichment") if isinstance(wrapper, dict) else None
            if (
                isinstance(enrichment, dict)
                and wrapper.get("type") == "dev_artifact_snapshot"
                and enrichment.get("episode_id") == episode_id
            ):
                record = dict(enrichment)
                record["content"] = str(wrapper.get("text") or "")
                records.append(record)
        return tuple(sorted(records, key=lambda record: str(record.get("relative_path") or "")))

    def _attempt_memory(self, program_id: str) -> dict[str, Any]:
        for path in self.memory_root.rglob("*.json"):
            try:
                wrapper = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
            text = (
                wrapper.get("text")
                if isinstance(wrapper, dict) and isinstance(wrapper.get("text"), str)
                else payload.get("text")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(text, str):
                continue
            try:
                record = json.loads(text)
            except ValueError:
                # Canonical display text is bounded.  The immutable SourceLedger
                # retains the complete attempt body and is the authoritative place
                # to recover a large tool ledger/stdout record.
                provenance = wrapper.get("provenance") if isinstance(wrapper, dict) else None
                source_id = (
                    str(provenance.get("source_id") or "") if isinstance(provenance, dict) else ""
                )
                artifact_path = (
                    self.memory_root / "source_ledger" / "artifacts" / f"{source_id}.json"
                )
                if not source_id or not artifact_path.is_file():
                    continue
                try:
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                    record = json.loads(str(artifact.get("content") or ""))
                except (OSError, TypeError, ValueError):
                    continue
            if record.get("program_id") == program_id and "motor_program" in record:
                return record
        raise AssertionError(f"motor execution evidence memory missing for {program_id}")
