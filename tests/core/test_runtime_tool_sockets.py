"""Runtime.yaml sockets — tools, services, generate, registry slots."""

from __future__ import annotations

from pressure_loop.policy import (
    clear_policy_cache,
    runtime_generate_socket,
    runtime_action_socket,
    runtime_registry_slots,
    runtime_service_names,
    runtime_service_socket,
    runtime_tool_socket,
)


def test_generate_socket_is_the_backend_neutral_mind_surface():
    # The connector selects the actual cloud/local backend and model. The mind
    # socket must remain a stable surface label, never a provider/model address.
    clear_policy_cache()
    gen = runtime_generate_socket()
    assert gen.endpoint == "/generate"
    assert gen.method == "generate_completion"
    assert gen.capability == "coding_model"
    assert gen.slot == "mind"


def test_cognition_operations_share_one_generate_socket():
    clear_policy_cache()
    generate = runtime_generate_socket()
    for operation in ("auto_response", "episode_cognition", "auto_browser_agent", "intent_escalation",
                      "knowledge_comprehension"):
        action = runtime_action_socket(operation)
        assert (action.endpoint, action.capability, action.method, action.slot) == (
            generate.endpoint,
            generate.capability,
            generate.method,
            generate.slot,
        )
        assert action.name == operation


def test_substrate_services_no_classify_setfit():
    clear_policy_cache()
    names = runtime_service_names()
    assert "classify" not in names
    assert names == ("stt", "tts", "voice")

    stt = runtime_service_socket("stt")
    assert stt.slot == "parakeet_tdt_06b_v3"
    tts = runtime_service_socket("tts")
    assert tts.slot == "qwen3-tts"


def test_runtime_tools_map_to_model_slots():
    clear_policy_cache()
    img = runtime_tool_socket("image_tool")
    assert img.endpoint == "/image/generate"
    # sd_turbo left the runtime registry long before this was noticed;
    # /image/generate binds to flux2_klein (2026-08-13).
    assert img.slot == "flux2_klein"
    vision = runtime_tool_socket("vision_reasoning_tool")
    assert vision.slot == "optional"
    face = runtime_tool_socket("face_tool")
    assert face.slots["detector"] == "face_det_10g"
    assert face.slots["embedding"] == "face_w600k_r50"
    assert face.slots["landmarks_2d"] == "face_2d106det"


def test_registry_slots_flatten_all_hints():
    clear_policy_cache()
    slots = runtime_registry_slots()
    assert slots["generate"] == "mind"
    assert "embed" not in slots
    assert "classify.lobe" not in slots
    assert slots["image_tool"] == "flux2_klein"
    assert slots["face_tool.detector"] == "face_det_10g"
