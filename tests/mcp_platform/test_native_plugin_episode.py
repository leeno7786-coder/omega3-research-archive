"""Native plugin evidence survives the real canonical episode memory seam.

The MCP peer, installed catalog and inference are fixtures; the production SDK,
AUTO fold, DeepSleep body/capture builders, SRAM drain, canonical writer and
reflection-window reader are real. Test-wide memory isolation redirects all
writes to tmp_path and substitutes only vector encoding. This is not a complete
cognition-cycle or projection-rebuild acceptance test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from mcp import types

from auto_loop._auto_state import AutoInput
from auto_loop.fold.run import _fold_episode_auto
from auto_loop.promptpack.run import build_prompt_pack
from core import paths
from deepsleep_loop.intake.run import build_deepsleep_input_for_episode
from deepsleep_loop.loop import _capture_closed_episode_to_taxonomy
from memory import episode_sram
from pressure_loop.episode_journal import enriched_episode_from_episode
from pressure_loop.temporal_store import _load_subdomain_metas
from tests.core.test_auto_execution import _call, _mem, _pack
from tests.mcp_platform.test_native_mcp_roundtrip import _roundtrip_fixture, _RoundtripModel, _run_auto


def test_two_native_sdk_results_survive_sram_canonical_write_and_cold_hydration(tmp_path, monkeypatch):
    fixture, transport = _roundtrip_fixture("structured")
    episode_id = "sdk-roundtrip-episode"

    class TwoCalls(_RoundtripModel):
        def generate_completion(self, **kwargs):
            self.calls.append(("generate", kwargs))
            pass_number = len(self.calls)
            if pass_number <= 2:
                # Two distinct observations, supplied solely by the SDK peer.
                count = pass_number + 1
                transport.behavior.tool_result = types.CallToolResult(
                    content=[types.TextContent(text=json.dumps({"count": count}))],
                    structuredContent={"count": count},
                    _meta={"private_fixture_marker": "never-model-visible"},
                )
                return {"tool_calls": [_call("call_plugin_tool", {
                    "candidate_id": self.binding.candidate_id,
                    "arguments": {"query": f"calendar observation {pass_number}"},
                }, f"observation-{pass_number}")]}
            assert pass_number == 3
            return {"text": "The two calendar observations were read.", "tool_calls": []}

    model = TwoCalls(fixture.binding, fixture.plugin_runtime)
    assert paths.MEMORY_ROOT.is_relative_to(tmp_path), "canonical writes must stay in isolated test memory"
    episode_sram.open_bucket(episode_id, pipeline_id=0)
    with episode_sram.bind_episode(episode_id, pipeline_id=0):
        result = _run_auto(model, monkeypatch, tmp_path)
        assert transport.behavior.calls.count("tools/call") == 2
        envelope = _mem()
        pack = _pack()
        folded = _fold_episode_auto(
            AutoInput(episode_id, {}, {}, pack, {}, None),
            status="success", cycle_kind="success", memory_envelope=envelope,
            model_profile=None, model_id=None, prompt_pack=build_prompt_pack(pack, envelope),
            exec_result=result, runtime_ms=1,
        )
        episode = SimpleNamespace(
            episode_id=episode_id, opened_by="operator_query", pipeline_id=0,
            work_kind="operator_chat", cascade_status="completed", auto=folded,
            pressure={}, global_={"intent_decision": {"intent": "RESPOND"}},
            meta={"execution_action_pack": pack}, omega={}, brain_turns=[],
        )
        body = enriched_episode_from_episode(episode)
        body["closed_at"] = "2026-01-01T00:00:00Z"
        intake = build_deepsleep_input_for_episode(body, episode_id)
        expected_records = intake.episode_window[0]["outputs"]["operator_tool_calls"]
        assert [row["result"]["plugin_result"]["data"]["structured_content"]["count"]
                for row in expected_records] == [2, 3]
        assert "never-model-visible" not in json.dumps(expected_records)

        capture = _capture_closed_episode_to_taxonomy(episode_id, intake.episode_window[0])
        assert not capture["errors"], capture
        assert not _load_subdomain_metas(1), "canonical episode must not appear before SRAM drains"
        drained = episode_sram.close_bucket(episode_id)
        assert not drained["errors"], drained
        assert not drained["skipped"], drained

    canonical = [record for record in _load_subdomain_metas(1) if record.get("type") == "episode"]
    assert len(canonical) == 1
    persisted = json.loads(canonical[0]["text"])
    assert persisted["outputs"]["operator_tool_calls"] == expected_records
    assert persisted["outputs"]["plugin_result"]["data"]["structured_content"] == {"count": 3}
    assert len(persisted["outputs"]["plugin_step_results"]) == 2

    # The canonical filesystem alone holds both results (read cold, from the lobe-9 record).
    # WO-09 retired the id-scan window hydration this used to go through: a reflection window
    # now reads only the exact sealed record each member was pinned to.
    restored = persisted["outputs"]["operator_tool_calls"]
    assert restored == expected_records
    assert [row["result"]["plugin_plan"]["steps"][0]["arguments"]["query"]
            for row in restored] == ["calendar observation 1", "calendar observation 2"]
