"""Wire the vision recall lane for visual-modality queries (robotics vision).

The vision lane is lobe-mapped (LANE_VISION -> lobe 7) and uses the generic
runner, so it works — but no posture weights it, so it never dispatched.
QuerySpec already carries `modality` (default "text") and recall ignored it.

For the robotics vision deployment, a visual-modality query must surface lobe-7
vision memories. compile_recall_plan now applies `_apply_modality_lane_bias`:
when modality is visual (vision/image/visual), the vision lane gets a strong
weight. Text-modality recall is completely unchanged (vision stays 0).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.recall_lanes import LANE_VISION, run_recall_lanes  # noqa: E402
from memory.recall_compiler import (  # noqa: E402
    compile_recall_plan,
    _apply_modality_lane_bias,
)


def test_modality_bias_enables_vision_for_visual_modalities():
    for modality in ("vision", "image", "visual", "VISION", " Image "):
        out = _apply_modality_lane_bias({}, modality)
        assert float(out.get(LANE_VISION, 0.0) or 0.0) > 0.0, modality


def test_modality_bias_leaves_text_recall_untouched():
    # text (and unknown) modalities must NOT enable the vision lane
    assert _apply_modality_lane_bias({"identity": 2.0}, "text") == {"identity": 2.0}
    assert float(_apply_modality_lane_bias({}, "audio").get(LANE_VISION, 0.0) or 0.0) == 0.0


def test_modality_bias_never_lowers_an_explicit_vision_weight():
    # max() semantics — a posture/explicit higher weight is preserved
    out = _apply_modality_lane_bias({LANE_VISION: 3.0}, "vision")
    assert out[LANE_VISION] == 3.0


class _FakeQS:
    def __init__(self, text: str, modality: str = "text", limit: int = 10) -> None:
        self.cycle_id = "test-vision-lane"
        self.text = text
        self.modality = modality
        self.limit = limit
        self.task_scope = None
        self.recall_policy_id = None
        self.domain = None


def test_compile_plan_weights_vision_only_for_visual_query():
    plan_v = compile_recall_plan(_FakeQS("what do you see", modality="vision"))
    assert float(plan_v.lane_weights.get(LANE_VISION, 0.0) or 0.0) > 0.0

    plan_t = compile_recall_plan(_FakeQS("what do you see", modality="text"))
    assert float(plan_t.lane_weights.get(LANE_VISION, 0.0) or 0.0) == 0.0


# Synthetic lobe-7 vision memory for the end-to-end dispatch test.
_SYN_INDEX = {
    "7.0.vvv": {"id": "7.0.vvv", "coord": [7, 0, 0], "path": "FAKE/vision_vvv.json"},
}
_SYN_META = {
    "FAKE/vision_vvv.json": {
        "id": "7.0.vvv",
        "coord": [7, 0, 0],
        "type": "vision",
        "title": "red ball on the table",
        "summary": "I see a red ball on the table",
        "text": "I see a red ball on the table.",
        "tags": ["vision", "object"],
        "provenance": {"ingested_at": "2026-06-01T00:00:00Z"},
    },
}


def _fake_load(path):
    return _SYN_META.get(path)


def test_vision_lane_runs_end_to_end_for_visual_query():
    # Realistic robotics flow: the vision pipeline produces scene-descriptor
    # terms ("red ball ... table") that overlap the stored vision memory. The
    # vision lane is a generic lobe-7 lane scored by term overlap, so the query
    # must carry the scene labels — modality only DISPATCHES the lane.
    plan = compile_recall_plan(_FakeQS("red ball on the table", modality="vision"))
    hits = run_recall_lanes(plan, _SYN_INDEX, _fake_load)

    vision_hits = [h for h in hits if h.lane == LANE_VISION]
    assert vision_hits, "vision lane produced no hits — it was not dispatched"
    assert vision_hits[0].memory_id == "7.0.vvv"


def test_remember_vision_tool_requests_visual_modality():
    from types import SimpleNamespace

    from auto_loop.mril_tools.remember import invoke_remember_tool

    captured = {}

    def _read(spec):
        captured["modality"] = spec.modality
        return SimpleNamespace(
            status="verified",
            reason="",
            answer_payload={"candidates": [], "meta": {}},
        )

    result = invoke_remember_tool(
        {"tool": "remember_vision", "query": "what did I see"},
        "vision-tool-test",
        memory=_read,
    )

    assert result["lane"] == LANE_VISION
    assert captured["modality"] == "vision"
