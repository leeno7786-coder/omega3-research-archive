"""Tool registry + Meta capability map routing (spec §6 / §7)."""

from __future__ import annotations

from pressure_loop.policy import (
    clear_policy_cache,
    runtime_tool_names,
    runtime_tool_socket,
    tool_models,
    tool_registry,
)
from meta_loop._meta_a_state import MetaAState
from meta_loop.tool.run import _CAPABILITY_TOOL_MAP_DEFAULT, _capability_tool_map, run


def _state(*, intent: str, text: str, target_str: str = "") -> MetaAState:
    return MetaAState(
        mi=None,
        episode_id="ep-test",
        intent=intent,
        action="USE_TOOL",
        source={},
        text=text,
        target_str=target_str,
        constraints={},
        meta={},
    )


def test_runtime_tools_map_to_endpoints_not_models():
    clear_policy_cache()
    assert runtime_tool_names() == (
        "image_tool",
        "vision_reasoning_tool",
        "face_tool",
    )
    img = runtime_tool_socket("image_tool")
    assert img.endpoint == "/image/generate"
    assert img.method == "image_generate"
    assert img.capability == "image_generation"
    vision = runtime_tool_socket("vision_reasoning_tool")
    assert vision.endpoint == "/vision/describe"
    assert vision.method == "vision_describe"
    face = runtime_tool_socket("face_tool")
    assert face.endpoint == "/face/identify"
    assert face.method == "face_identify"


def test_tool_registry_merges_runtime_sockets():
    clear_policy_cache()
    reg = tool_registry()
    assert "vision_reasoning_tool" in reg
    assert "image_tool" in reg
    assert "face_tool" in reg
    assert reg["image_tool"]["socket"]["endpoint"] == "/image/generate"
    # sd_turbo left the runtime registry long before this was noticed;
    # /image/generate binds to flux2_klein (2026-08-13).
    assert reg["image_tool"]["socket"]["slot"] == "flux2_klein"
    assert tool_models("image_tool") == {
        "endpoint": "/image/generate",
        "capability": "image_generation",
        "method": "image_generate",
    }
    assert tool_models("vision_reasoning_tool")["method"] == "vision_describe"
    assert tool_models("web_fetch") == {}
    assert "image_understanding" in reg["vision_reasoning_tool"]["capabilities"]
    assert reg["image_tool"]["capabilities"] == ["image_generation"]
    assert "detect_faces" in reg["face_tool"]["capabilities"]


def test_capability_map_has_section7_entries():
    clear_policy_cache()
    merged = _capability_tool_map()
    for cap, tool in _CAPABILITY_TOOL_MAP_DEFAULT.items():
        assert merged.get(cap) == tool
    assert merged["image_understanding"] == "vision_reasoning_tool"
    assert merged["video_understanding"] == "vision_reasoning_tool"
    assert merged["object_localization"] == "vision_reasoning_tool"
    assert merged["scene_analysis"] == "vision_reasoning_tool"
    assert merged["face_recognition"] == "face_tool"
    assert merged["age_gender"] == "face_tool"
    assert merged["head_pose"] == "face_tool"


def test_use_tool_routes_image_understanding_to_vision_reasoning():
    st = _state(intent="USE_TOOL", text="understand image contents", target_str="photo.jpg")
    out = run(st)
    assert out.tool == "vision_reasoning_tool"
    assert out.execution_inputs["image_ref"] == "photo.jpg"
    assert "question" in out.execution_inputs


def test_use_tool_routes_face_cue_to_face_tool():
    st = _state(intent="USE_TOOL", text="recognize this person", target_str="selfie.png")
    out = run(st)
    assert out.tool == "face_tool"
    assert out.execution_inputs["image_ref"] == "selfie.png"
    assert out.execution_inputs["operation"] == "identify_faces"


def test_use_tool_routes_ocr_cue_to_ocr_tool():
    st = _state(intent="USE_TOOL", text="ocr this scan text", target_str="scan.png")
    out = run(st)
    assert out.tool == "ocr_tool"
    assert out.execution_inputs == {"query": "scan.png"}


def test_use_tool_existing_routes_unchanged():
    file_st = _state(intent="USE_TOOL", text="read file report.pdf", target_str="")
    assert run(file_st).tool == "file_reader"

    desktop_st = _state(intent="USE_TOOL", text="click window automate ui", target_str="")
    assert run(desktop_st).tool == "desktop_tool"

    web_st = _state(intent="USE_TOOL", text="find latest news", target_str="")
    assert run(web_st).tool == "web_fetch"
