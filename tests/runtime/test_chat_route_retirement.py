"""The superseded standalone operator-chat completion module stays retired."""

from __future__ import annotations

from pathlib import Path


def test_standalone_chat_route_module_is_absent() -> None:
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "boot.py").exists())

    assert not (root / "runtime" / "chat_route.py").exists()
