from __future__ import annotations

import json

import pytest

from hud import chat_history


def test_missing_chat_history_is_a_valid_empty_transcript(tmp_path, monkeypatch) -> None:
    path = tmp_path / "chat_history.json"
    monkeypatch.setattr(chat_history, "_HISTORY_PATH", path)

    assert chat_history.list_messages() == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{broken", "unreadable"),
        ({"messages": []}, "must contain a JSON list"),
        (["not a message"], "contains an invalid message"),
    ],
)
def test_corrupt_chat_history_is_not_recast_as_empty(
    tmp_path, monkeypatch, payload, message
) -> None:
    path = tmp_path / "chat_history.json"
    monkeypatch.setattr(chat_history, "_HISTORY_PATH", path)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        chat_history.list_messages()


def test_append_does_not_overwrite_corrupt_chat_history(tmp_path, monkeypatch) -> None:
    path = tmp_path / "chat_history.json"
    monkeypatch.setattr(chat_history, "_HISTORY_PATH", path)
    original = "{broken"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable"):
        chat_history.append_message({"role": "operator", "content": "hello"})

    assert path.read_text(encoding="utf-8") == original
