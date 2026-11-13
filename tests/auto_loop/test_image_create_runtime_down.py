"""§7-P1-3(d) — IMAGE_CREATE with the runtime unavailable.

Pins the live AUTO IMAGE_CREATION executor (`auto_loop/execute/run.py::execute`,
the `image_tool` runtime:false inline path) at the runtime seam:

  * runtime DOWN  → the `image_tool` socket returns ``None`` →
    ``failure_kind == "runtime_unavailable"`` and **no** image artifact.
  * runtime UP    → the socket returns PNG bytes → ``failure_kind is None``,
    an ``image_artifact`` (with the content sha256) is produced, and
    ``model_id == "image_tool"``.

RED doctrine: assert the configured VALUE on the down path
(``failure_kind == "runtime_unavailable"``) AND the FAIL-MODE direction
(the happy path produces a different, success outcome — the gate flips both
ways), not mere presence-of-behavior.

The only thing stubbed is the runtime boundary: ``runtime.socket.invoke_runtime_tool``
(imported inside the IMAGE_CREATION branch at call time, so patching the module
attribute is picked up) and, on the happy path, ``runtime.image_store.persist_generated_image``
so the test never touches disk. The executor's own failure logic is exercised
unchanged.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional
from unittest.mock import patch

from auto_loop._auto_state import MemoryEnvelope, PromptPack
from auto_loop.execute.run import execute


# ── fixtures / builders ───────────────────────────────────────────────


def _prompt_pack(prompt: str = "a serene mountain lake") -> PromptPack:
    return PromptPack(
        prompt_type="IMAGE_SYSTEM",
        system_prompt="render an image",
        developer_prompt=None,
        user_prompt=prompt,
        context_blocks={},
        response_mode=None,
        tool_call_spec=None,
        image_spec={"prompt": prompt, "width": 512, "height": 512, "steps": 4},
    )


def _empty_envelope() -> MemoryEnvelope:
    return MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )


def _image_execution_pack() -> Dict[str, Any]:
    """A well-formed IMAGE_CREATION pack — tool present, so the executor
    reaches the runtime socket (not the invalid_execution_pack branch)."""
    return {
        "action": "IMAGE_CREATION",
        "tool": "image_tool",
        "execution_inputs": {},
    }


def _run_image_execute(invoke_runtime_tool_stub) -> Dict[str, Any]:
    """Drive ``execute`` for IMAGE_CREATION with the socket stubbed.

    A sentinel ``runtime_client`` is passed so the executor never reaches
    ``runtime.client.get_runtime_client()``; the IMAGE branch ignores the
    client object and routes entirely through the patched socket.
    """
    sentinel_client = object()
    with patch(
        "runtime.socket.invoke_runtime_tool",
        side_effect=invoke_runtime_tool_stub,
    ):
        return execute(
            _prompt_pack(),
            "IMAGE_GENERATION",
            _image_execution_pack(),
            _empty_envelope(),
            episode_id="ep_img_test",
            runtime_client=sentinel_client,
        )


# ── runtime DOWN: failure_kind == "runtime_unavailable" ───────────────


def test_image_create_runtime_down_yields_runtime_unavailable():
    """Runtime down → socket returns None → runtime_unavailable, no artifact."""

    def _down(*_args: Any, **_kwargs: Any) -> Optional[bytes]:
        # invoke_runtime_tool returns None when the runtime/capability is
        # unavailable (socket declined / no worker).
        return None

    result = _run_image_execute(_down)

    assert result["failure_kind"] == "runtime_unavailable"
    assert result["failure_reason"] == "image_tool socket declined or returned None"
    # No artifact and no model id on the failure path.
    assert "image_artifact" not in result["outputs"]
    assert result["model_id"] is None


# ── runtime UP: the happy path differs (gate flips the other way) ─────


def test_image_create_runtime_up_succeeds_and_differs_from_down():
    """Runtime up → socket returns PNG bytes → success, NOT runtime_unavailable."""
    png_bytes = b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES"
    expected_sha = hashlib.sha256(png_bytes).hexdigest()

    def _up(tool: str, **_kwargs: Any) -> bytes:
        assert tool == "image_tool"
        return png_bytes

    # Keep persistence off-disk: stub the canonical image store. Persist is
    # failure-soft in the executor, but stubbing avoids any real file write
    # and proves the success path produces the artifact regardless.
    class _Saved:
        name = "img.png"

        def __str__(self) -> str:
            return "/fake/store/img.png"

    with patch(
        "runtime.image_store.persist_generated_image",
        return_value=_Saved(),
    ):
        result = _run_image_execute(_up)

    # FAIL-MODE direction: the gate flips — success, not runtime_unavailable.
    assert result["failure_kind"] is None
    assert result["failure_kind"] != "runtime_unavailable"
    assert result["failure_reason"] is None

    artifact = result["outputs"].get("image_artifact")
    assert artifact is not None
    # The artifact pins the exact content the socket returned.
    assert artifact["sha256"] == expected_sha
    assert artifact["bytes_len"] == len(png_bytes)
    assert artifact["prompt"] == "a serene mountain lake"
    assert result["model_id"] == "image_tool"


# ── the two paths are genuinely distinct outcomes ────────────────────


def test_image_create_down_and_up_outcomes_are_distinct():
    """Same executor, same pack — only the runtime seam differs → opposite
    failure_kind. This pins both directions of the gate in one assertion."""
    png_bytes = b"\x89PNG\r\n\x1a\nFAKE"

    down = _run_image_execute(lambda *a, **k: None)

    class _Saved:
        name = "img.png"

        def __str__(self) -> str:
            return "/fake/store/img.png"

    with patch(
        "runtime.image_store.persist_generated_image",
        return_value=_Saved(),
    ):
        up = _run_image_execute(lambda *a, **k: png_bytes)

    assert down["failure_kind"] == "runtime_unavailable"
    assert up["failure_kind"] is None
    assert down["failure_kind"] != up["failure_kind"]
