"""DeepSleep journal store (Slice 6a) — file-backed JSONL, failure-soft."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deepsleep_loop._deepsleep_state import JournalEntry
from deepsleep_loop.store_locks import JOURNAL, guarded

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def _journal_line(entry: JournalEntry) -> str:
    return json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True)


@guarded(JOURNAL)
def append_journal_entry(entry: JournalEntry, path: str | Path | None = None) -> None:
    """Append one JournalEntry JSONL line.

    Fail-hard: the journal is THE continuity record. A write failure CRASHES —
    silently dropping a journal line corrupts continuity (the canonical Cat 3)."""
    from pressure_loop.policy import deepsleep_journal_path

    from pressure_loop.durable_write import append_jsonl_line

    resolved = _resolve_path(path or deepsleep_journal_path())
    append_jsonl_line(resolved, _journal_line(entry))


#: How far back a retried terminus looks for the journal row its earlier attempt appended.
#: Rows are large (an embedded episode body), so the scan reads backwards from the end of the
#: file and stops here rather than parsing the whole continuity record on every terminus.
RETRY_TAIL_ROWS = 64


def _tail_lines(path: Path, limit: int) -> list[str]:
    """The last *limit* non-empty lines of *path*, oldest first, read from the end."""
    chunk = 1 << 16
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        buffer = b""
        while position > 0 and buffer.count(b"\n") <= limit:
            step = min(chunk, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
    if position > 0:
        # The first line may be a partial read, possibly cut inside a multi-byte character:
        # drop it as BYTES, before decoding.
        newline = buffer.find(b"\n")
        buffer = buffer[newline + 1:] if newline >= 0 else b""
    lines = [line for line in buffer.decode("utf-8").splitlines() if line.strip()]
    return lines[-limit:]


@guarded(JOURNAL)
def find_journal_entry(
    episode_id: str,
    scope: str | None,
    path: str | Path | None = None,
    *,
    tail: int = RETRY_TAIL_ROWS,
) -> JournalEntry | None:
    """The row an earlier attempt of THIS terminus appended, or None (WO-07).

    Keyed by the row's own ``episode_id`` (the window id at window scope) and ``scope``. A
    corrupt row in the tail is continuity corruption and raises, exactly as ``read_journal``.
    """
    from pressure_loop.policy import deepsleep_journal_path

    resolved = _resolve_path(path or deepsleep_journal_path())
    if not resolved.is_file():
        return None
    for raw in reversed(_tail_lines(resolved, int(tail))):
        payload = json.loads(raw)
        if (
            isinstance(payload, dict)
            and str(payload.get("episode_id") or "") == str(episode_id or "")
            and payload.get("scope") == scope
        ):
            return JournalEntry.from_dict(payload)
    return None


@guarded(JOURNAL)
def read_journal(n: int | None = None, path: str | Path | None = None) -> list[JournalEntry]:
    """Read the last N JournalEntry records (oldest -> newest).

    Fail-hard: a MISSING journal (first boot), empty file, or zero window is
    absence → []. A read/parse failure on an EXISTING journal (corrupt JSON
    line, decode/IO error) is continuity corruption → it propagates and CRASHES;
    the mind must not silently reason on a truncated past."""
    from pressure_loop.policy import deepsleep_journal_path

    resolved = _resolve_path(path or deepsleep_journal_path())
    if not resolved.is_file():
        return []

    window = int(n if n is not None else 50)
    if window <= 0:
        return []

    lines = resolved.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []

    start = max(0, len(lines) - window)
    out: list[JournalEntry] = []
    for raw in lines[start:]:
        raw = raw.strip()
        if not raw:
            continue
        payload = json.loads(raw)
        if isinstance(payload, dict):
            out.append(JournalEntry.from_dict(payload))
    return out
