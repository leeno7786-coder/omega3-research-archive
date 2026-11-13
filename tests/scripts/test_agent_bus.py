"""Unit tests for scripts/agent_bus.py — append-only inter-agent bus."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.agent_bus as bus  # noqa: E402
from core.paths import portable_python_path

PYTHON = portable_python_path()


@pytest.fixture
def bus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agents = tmp_path / ".agents"
    agents.mkdir()
    (agents / "cursors").mkdir()
    monkeypatch.setenv("AGENT_BUS_AGENTS_DIR", str(agents))
    monkeypatch.setattr(bus, "AGENTS_DIR", agents)
    return agents


def _post(
    home: Path,
    *,
    from_id: str = "claude",
    to: str = "cursor",
    kind: str = "work_order",
    subject: str = "Fix flood",
    body: str = "details",
    ref: str | None = None,
) -> dict:
    return bus.post(
        from_id=from_id,
        to=to,
        kind=kind,
        subject=subject,
        body=body,
        ref=ref,
    )


def test_post_appends_well_formed_line_with_id_and_ts(bus_home: Path) -> None:
    msg = _post(bus_home)
    assert len(msg["id"]) == 8
    assert msg["ts"].endswith("Z")
    assert msg["from"] == "claude"
    assert msg["to"] == "cursor"
    assert msg["kind"] == "work_order"


def test_read_filters_by_recipient_and_advances_cursor(bus_home: Path) -> None:
    _post(bus_home, to="cursor", subject="for cursor")
    _post(bus_home, to="claude", subject="for claude")
    _post(bus_home, to="cursor", subject="for cursor 2")

    first = bus.read(for_id="cursor", emit="plain")
    assert "for cursor" in first
    assert "for claude" not in first
    assert "for cursor 2" in first

    second = bus.read(for_id="cursor", emit="plain")
    assert second == ""

    _post(bus_home, to="cursor", subject="new after drain")
    third = bus.read(for_id="cursor", emit="plain")
    assert "new after drain" in third


def test_broadcast_reaches_both_agents_independently(bus_home: Path) -> None:
    _post(bus_home, to="all", subject="broadcast", body="hi all")

    c = bus.read(for_id="cursor", emit="plain")
    cl = bus.read(for_id="claude", emit="plain")
    assert "broadcast" in c
    assert "broadcast" in cl
    assert bus.read(for_id="cursor", emit="plain") == ""
    assert bus.read(for_id="claude", emit="plain") == ""


def test_emit_cursor_stop_with_unread(bus_home: Path) -> None:
    _post(bus_home, kind="work_order", subject="Do thing")
    raw = bus.read(for_id="cursor", emit="cursor-stop")
    data = json.loads(raw)
    assert "followup_message" in data
    assert "[AGENT-BUS" in data["followup_message"]
    assert "Do thing" in data["followup_message"]


def test_emit_cursor_stop_empty_bus(bus_home: Path) -> None:
    raw = bus.read(for_id="cursor", emit="cursor-stop")
    assert json.loads(raw) == {}


def test_emit_claude_context_with_unread(bus_home: Path) -> None:
    _post(bus_home, to="claude", from_id="cursor", kind="status", subject="done")
    raw = bus.read(for_id="claude", emit="claude-context")
    data = json.loads(raw)
    assert "hookSpecificOutput" in data
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "done" in ctx
    assert "[AGENT-BUS" in ctx


def test_emit_claude_context_empty(bus_home: Path) -> None:
    raw = bus.read(for_id="claude", emit="claude-context")
    assert raw.strip() == "" or json.loads(raw) == {}


def test_body_truncation_in_emit(bus_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bus, "BODY_MAX", 50)
    big = "x" * 200
    msg = _post(bus_home, body=big)
    raw = bus.read(for_id="cursor", emit="cursor-stop")
    data = json.loads(raw)
    text = data["followup_message"]
    assert f"id={msg['id']}" in text
    assert "[full: .agents/bus.jsonl" in text
    full_line = json.loads((bus_home / "bus.jsonl").read_text(encoding="utf-8").strip())
    assert len(full_line["body"]) == 200


def test_unknown_kind_warns_but_still_posts_and_delivers(
    bus_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bus.post(from_id="claude", to="cursor", kind="typo_kind", subject="s", body="b")
    err = capsys.readouterr().err
    assert "typo_kind" in err or "unknown" in err.lower()
    out = bus.read(for_id="cursor", emit="plain")
    assert "s" in out


def test_failure_soft_corrupt_line_and_missing_cursor(bus_home: Path) -> None:
    (bus_home / "bus.jsonl").write_text('{"bad json\n', encoding="utf-8")
    _post(bus_home, subject="good")
    out = bus.read(for_id="cursor", emit="cursor-stop")
    data = json.loads(out)
    assert "followup_message" in data
    assert "good" in data["followup_message"]

    (bus_home / "bus.jsonl").unlink(missing_ok=True)
    assert json.loads(bus.read(for_id="cursor", emit="cursor-stop")) == {}


def test_cli_post_and_read_roundtrip(bus_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BUS_AGENTS_DIR", str(bus_home))
    subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts" / "agent_bus.py"),
            "post",
            "--from",
            "claude",
            "--to",
            "cursor",
            "--kind",
            "work_order",
            "--subject",
            "cli",
            "--body",
            "via cli",
        ],
        cwd=ROOT,
        check=True,
        env={**__import__("os").environ, "AGENT_BUS_AGENTS_DIR": str(bus_home)},
    )
    proc = subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts" / "agent_bus.py"),
            "read",
            "--for",
            "cursor",
            "--emit",
            "plain",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "AGENT_BUS_AGENTS_DIR": str(bus_home)},
    )
    assert proc.returncode == 0
    assert "cli" in proc.stdout
