"""Hot buffer — the DME's in-session working memory (the short arc).

The live session's turns are held here UN-COMPILED: circling back to
something said five seconds ago costs an in-process lookup, never a
store round-trip. This is how the old Omega kept conversation alive,
rebuilt on v2's deterministic spine:

  * ``append`` is O(1); the buffer is capped FIFO so a marathon session
    can't grow without bound.
  * ``score`` is DETERMINISTIC token overlap (set-cosine) with a
    recency tiebreak — same buffer + same query → same ranking, no
    model in the loop.
  * Session close flushes every turn through a consolidation callable
    (production passes the normal ingest path) and only clears the
    buffer if consolidation succeeds — hot is where memories live
    BEFORE they're memories: nothing is remembered twice, nothing is
    silently dropped.

Consumers:
  * ``pressure_loop.operator_chat`` appends the user turn on ingress
    and the assistant turn on egress.
  * ``auto_loop.promptpack`` injects the most recent turns verbatim
    into the reply contract (the multiturn block).
  * ``memory.recall_lanes`` runs the HOT lane over ``score`` so
    query-relevant in-session content surfaces through recall receipts
    ahead of the compiled lanes.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from guardian.external_ingress import harden

log = logging.getLogger("omega.memory.hot_buffer")

DEFAULT_CAP = 64

_TOKEN_RX = re.compile(r"[a-z0-9]+")

# Deliberately tiny stopword set: the buffer scores short conversational
# turns where near-every content word matters. Heavy stopwording is the
# compiled store's job.
_STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "to",
    "of",
    "and",
    "or",
    "in",
    "on",
    "at",
    "it",
    "my",
    "me",
    "i",
    "you",
    "your",
    "what",
    "which",
    "who",
    "that",
    "this",
    "do",
    "does",
    "did",
}


def _tokens(text: str) -> set:
    return {t for t in _TOKEN_RX.findall(str(text or "").lower()) if t not in _STOP}


@dataclass(frozen=True)
class HotTurn:
    turn_id: str
    session_id: str
    role: str
    text: str
    seq: int
    ts: float


class HotBuffer:
    """Capped, thread-safe, in-process turn buffer for ONE session."""

    def __init__(self, session_id: str, cap: int = DEFAULT_CAP) -> None:
        self.session_id = str(session_id)
        self.cap = max(1, int(cap))
        self._turns: List[HotTurn] = []
        self._seq = 0
        self._lock = threading.RLock()

    def append(self, role: str, text: str) -> HotTurn:
        clean = str(text or "").strip()
        with self._lock:
            self._seq += 1
            turn = HotTurn(
                turn_id=f"hot:{self.session_id}:{self._seq}",
                session_id=self.session_id,
                role=str(role or "user"),
                text=clean,
                seq=self._seq,
                ts=time.time(),
            )
            self._turns.append(turn)
            if len(self._turns) > self.cap:
                self._turns = self._turns[-self.cap :]
            return turn

    def turns(self) -> List[HotTurn]:
        with self._lock:
            return list(self._turns)

    def recent(self, n: int = 8) -> List[HotTurn]:
        """The last ``n`` turns, oldest-first — the multiturn block."""
        with self._lock:
            return list(self._turns[-max(0, int(n)) :])

    def score(self, query_text: str) -> List[Tuple[HotTurn, float]]:
        """Deterministic relevance ranking of turns against a query.

        Set-cosine token overlap; ties break by recency (later seq
        first). Turns with zero overlap are omitted. Empty query → [].
        """
        q = _tokens(query_text)
        if not q:
            return []
        scored: List[Tuple[HotTurn, float]] = []
        for turn in self.turns():
            t = _tokens(turn.text)
            if not t:
                continue
            overlap = len(q & t)
            if overlap == 0:
                continue
            score = overlap / ((len(q) * len(t)) ** 0.5)
            scored.append((turn, round(score, 6)))
        scored.sort(key=lambda pair: (-pair[1], -pair[0].seq))
        return scored

    def clear(self) -> None:
        with self._lock:
            self._turns = []


# ---------------------------------------------------------------------
# Module session singleton — the live boot has ONE active operator
# session at a time; begin/close bracket it.
# ---------------------------------------------------------------------

_lock = threading.RLock()
_current: Optional[HotBuffer] = None


def begin_session(session_id: str, cap: int = DEFAULT_CAP) -> HotBuffer:
    """Open (or replace) the active session buffer."""
    global _current
    with _lock:
        _current = HotBuffer(session_id, cap=cap)
        return _current


def current_hot_buffer() -> Optional[HotBuffer]:
    with _lock:
        return _current


def append_turn(role: str, text: str, session_id: str = "operator") -> HotTurn:
    """Append to the active session, opening one lazily if needed —
    ingress points shouldn't have to manage the lifecycle."""
    global _current
    with _lock:
        if _current is None:
            _current = HotBuffer(session_id)
        return _current.append(role, text)


def close_session(
    consolidate: Optional[Callable[[List[Dict[str, Any]]], Any]] = None,
) -> int:
    """Flush the active session through ``consolidate`` and clear it.

    Returns the number of turns handed to consolidation (0 when no
    session is open). FAIL-SAFE: if consolidation raises, the session
    stays open with its turns intact so a retry can flush — the buffer
    never drops turns on a failed ingest.
    """
    global _current
    with _lock:
        buf = _current
        if buf is None:
            return 0
        turns = [
            {
                "turn_id": t.turn_id,
                "session_id": t.session_id,
                "role": t.role,
                "text": t.text,
                "seq": t.seq,
                "ts": t.ts,
            }
            for t in buf.turns()
        ]
    if consolidate is not None and turns:
        consolidate(turns)  # raises → session stays open (fail-safe)
    with _lock:
        _current = None
    return len(turns)


def render_recent_block(n: int = 8, exclude_text: Optional[str] = None) -> str:
    """Render the live session's recent turns as a SYSTEM-directive block
    (gemma follows system directives more reliably than user-prompt
    content — operator directive 2026-06-29). Returns "" when there is
    no session or no turns to show.

    ``exclude_text`` drops the trailing turn when it matches (the
    current user message rides the user prompt itself — rendering it
    again would duplicate it).
    """
    buf = current_hot_buffer()
    if buf is None:
        return ""
    turns = buf.recent(max(1, int(n)))
    if turns and exclude_text is not None:
        tail = turns[-1]
        if " ".join(tail.text.split()) == " ".join(str(exclude_text).split()):
            turns = turns[:-1]
    if not turns:
        return ""
    lines = []
    for t in turns:
        speaker = "operator" if t.role == "user" else "omega"
        text = harden(t.text) if t.role == "user" else t.text
        lines.append(f"- [{speaker}] {text}")
    return (
        "Current conversation (this session, most recent last):\n"
        + "\n".join(lines)
        + "\nContinue this conversation naturally — the turns above are "
        "context the operator expects you to remember."
    )


def _reset_for_tests() -> None:
    global _current
    with _lock:
        _current = None
