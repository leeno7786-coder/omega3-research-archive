"""The superseded Python WS bridge is not part of the live skill route."""

from __future__ import annotations

from pathlib import Path


def test_python_qwen_bridge_and_direct_invoker_are_absent() -> None:
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "boot.py").exists())

    assert not (root / "runtime" / "qwen_cli_client.py").exists()

    from auto_loop.skill import run as skill_run

    assert not hasattr(skill_run, "default_qwen_skill_invoker")
