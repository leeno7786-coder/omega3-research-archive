"""E3 marker RETIRED — the signal must be a live read, not an advisory copy.

Operator directive 2026-06-24: *"if the operator sends a request through the HUD,
it adds to pressure and then when the current episode, if there is one, ends,
don't reset the pressure system, just kick it off on the next queue. We can let
the mind know by injecting an id into the current running episode so when it
performs metacog it won't get confused."*

The QUEUE half of that directive is delivered and covered elsewhere (the durable
FIFO preserves the request and fires it next cycle — tests/pressure/
test_queue_is_sole_gate.py, tests/core/test_queue_boxes_red.py).

The MARKER half never worked. `_inject_pending_request_ids` wrote
`episode.meta["pending_operator_requests"]`, and nothing read it:
`omega_loop/episode/packet.py` does not touch `episode.meta` at all, and the key
appears nowhere in `omega_loop/`. So the marker reached no prompt and no
metacognition — a §2.1 write with no reader. Its write site was additionally
unreachable after `ef116281` made scheduling transactional, since `active` is
computed from `store.current()` on the very thread blocked inside the cascade.

It is deleted rather than repaired because it duplicated state the queue already
owns. The correct shape is a LIVE read of the durable line at packet-build time
(`operator_queue.peek_inputs()`), which cannot drift from the thing it
describes. That is booked as remaining work under AGENTS.md §10 #16 — it needs a
reader in the Omega prompt to be worth writing, or it repeats this defect.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_no_advisory_pending_marker_is_written_anywhere():
    """The retired marker must not come back as a write nobody reads."""
    import pressure_loop.pressure as pressure_mod

    assert not hasattr(pressure_mod, "_inject_pending_request_ids"), (
        "the advisory pending-request marker was reintroduced; the signal belongs "
        "as a live read of the operator queue at packet-build time, with a reader"
    )


def test_the_queue_itself_still_carries_the_waiting_turn(tmp_path, monkeypatch):
    """The half of the directive that matters: the request is preserved and fires next.

    This is the durable line, which is now also the single gate (§4.0) — so
    "a turn is waiting" is answerable at any moment by asking it, with no marker
    to keep in sync.
    """
    import pressure_loop.cognition_queue as oq
    from pressure_loop.contributors import from_operator_message

    # Per-test line: the real guardian/state box carries the operator's actual
    # backlog and must never be read or written by the suite.
    monkeypatch.setattr(oq, "_queue_path", lambda: tmp_path / "operator_queue.json")
    oq._reset_cache_for_tests()
    try:
        turn = from_operator_message(
            {
                "message": "answer me when you're done",
                "ingress_class": "epistemic_request",
                "weight": 1.0,
                "cycle_id": "c-waiting",
                "arrival_seq": 1,
            }
        )
        assert oq.enqueue_input(turn, operator_activity=False)
        waiting = oq.peek_inputs()
        assert len(waiting) == 1
        assert waiting[0].metadata.get("cycle_id") == "c-waiting"
    finally:
        oq._reset_cache_for_tests()
