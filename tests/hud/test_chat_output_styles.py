"""Contracts for rich assistant output in the HUD and AUTO prompt guidance."""

import shutil
import subprocess
from pathlib import Path

import pytest

from pressure_loop.policy import auto_system_prompt


ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
MONOLITH = ROOT / "hud" / "omega_interface.html"


def test_chat_renderer_supports_rich_blocks():
    source = (UI_SRC / "core" / "chat-renderer.js").read_text(encoding="utf-8")
    assert "function _renderMarkdown(" in source
    assert "chat-code-copy" in source
    assert "chat-mermaid" in source
    assert "function _renderCodeBlock(" in source


def test_thinking_block_is_a_collapsed_memory_disclosure():
    chat = (UI_SRC / "core" / "chat-feed.js").read_text(encoding="utf-8")
    boot = (UI_SRC / "core" / "boot.js").read_text(encoding="utf-8")
    shell = (UI_SRC / "shell.html").read_text(encoding="utf-8")
    assert "thinking-toggle" in chat
    assert "Thought for " in chat
    assert '<details class="thinking-details" open>' in chat
    assert "Reasoning breakdown" not in chat
    assert "_safeReasoningSummary" not in chat
    assert "Memory hits used · " in chat
    # A resolved block folds away only when the turn did NO tool work: a build's trace
    # is the plan, the calls and the diffs the operator was just watching, and collapsing
    # that hid all of it behind one line (2026-09-20). Behaviour is pinned by
    # tests/hud/test_hud_task_plan_trace.mjs; this is the source contract.
    assert "toggleEl.open = workedTools > 0" in chat
    assert "'Worked for '" in chat
    assert "response_mode: prompt.response_mode" in boot
    assert "detailsEl && !detailsEl.open" not in chat
    assert ".cockpit-shell .thinking-toggle" in shell
    assert ".cockpit-shell .feed .line.assistant .chat-code-block" in shell


def test_reply_stream_does_not_paint_assistant_prose_into_the_thinking_box():
    chat = (UI_SRC / "core" / "chat-feed.js").read_text(encoding="utf-8")
    wiring = (UI_SRC / "core" / "chat-wiring.js").read_text(encoding="utf-8")
    assert "_paintPending" not in wiring
    assert "copyEl.innerHTML = _renderMarkdown(text)" not in wiring
    assert "traceNarration(block, streamed)" not in wiring
    assert "traceReasoning(block, env.text, false)" not in wiring
    assert "traceReasoning(" not in chat


def test_mermaid_renderer_is_local_and_failure_soft():
    source = (UI_SRC / "core" / "mermaid-lite.js").read_text(encoding="utf-8")
    assert "window.OmegaMermaid" in source
    assert "foreignObject" not in source
    assert "return null" in source


def test_manifest_orders_renderer_around_legacy_boot():
    manifest = (UI_SRC / "manifest.json").read_text(encoding="utf-8")
    assert '"file": "core/chat-renderer.js"' in manifest
    assert '"file": "core/mermaid-lite.js"' in manifest
    assert manifest.index('"file": "core/mermaid-lite.js"') < manifest.index(
        '"file": "core/boot.js"'
    )
    assert manifest.index('"file": "core/boot.js"') < manifest.index(
        '"file": "core/chat-renderer.js"'
    )


def test_model_prompt_contains_format_guidance():
    prompt = auto_system_prompt("RESPONSE_QUESTION")
    assert "fenced code blocks" in prompt
    assert "Mermaid" in prompt


def test_monolith_contains_rich_output_modules():
    html = MONOLITH.read_text(encoding="utf-8")
    assert "@module core/chat-renderer.js" in html
    assert "@module core/mermaid-lite.js" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_chat_renderer_runtime_behaviour() -> None:
    script = ROOT / "tests" / "hud" / "test_chat_renderer.mjs"
    result = subprocess.run(
        ["node", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_chat_thinking_memory_runtime_behaviour() -> None:
    script = ROOT / "tests" / "hud" / "test_chat_thinking.mjs"
    result = subprocess.run(
        ["node", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_chat_browser_frames_runtime_behaviour() -> None:
    """The browser agent's screenshots append under their action rows (operator 2026-09-15)."""
    script = ROOT / "tests" / "hud" / "test_chat_browser_frames.mjs"
    result = subprocess.run(
        ["node", str(script)], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
