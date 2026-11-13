"""Current HUD UI invariants for proactive delivery and session-only chat."""

from __future__ import annotations

from pathlib import Path


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
UI_SRC = ROOT / "hud" / "ui-src"


def test_proactive_messages_are_owned_by_side_pinboard_not_chat_feed():
    chat = (UI_SRC / "core" / "chat-feed.js").read_text(encoding="utf-8")
    system_messages = (UI_SRC / "panels" / "system_messages.js").read_text(encoding="utf-8")
    live = (UI_SRC / "core" / "live-wiring.js").read_text(encoding="utf-8")

    assert "/api/chat/proactive" not in chat
    assert "_refreshProactiveChatFeed" not in chat
    assert "_surfaceAskOperatorInChat" not in system_messages
    assert "fetch('/api/proactive/notices'" in system_messages
    assert "window._appendProactiveNotice" in system_messages
    assert "/delivered" in system_messages
    assert "window._appendProactiveNotice" in live
    signal_handler = live.split("window.addEventListener('omega:signal'", 1)[1]
    signal_handler = signal_handler.split("})();", 1)[0]
    assert "appendLine(" not in signal_handler


def test_every_proactive_store_emitter_refreshes_the_side_pinboard():
    live = (UI_SRC / "core" / "live-wiring.js").read_text(encoding="utf-8")
    data_handler = live.split("window.addEventListener('omega:data'", 1)[1]
    data_handler = data_handler.split("window.addEventListener('omega:signal'", 1)[0]

    for phase in (
        "system_message_appended",
        "system_message_responded",
        "proactive_chat_appended",
        "result_presented_to_operator",
    ):
        assert phase in data_handler
    assert "_refreshProactivePinboard()" in data_handler


def test_chat_feed_js_has_no_boot_rehydration():
    """The chat panel starts fresh; durable outcomes live in their own panels."""
    src = (UI_SRC / "core" / "chat-feed.js").read_text(encoding="utf-8")
    assert "async function _rehydrateChatHistory" not in src
