"""Recency phrasings ("what did you learn last", "latest", "most recent") must select
the TEMPORAL_RECALL posture (recent_first ordering) — not fall through to
BACKGROUND_LOOKUP / MULTI_POSTURE with no temporal bias.

The recency phrases live in a SEPARATE selection rule (not merged into the general
temporal list) so their short pattern set scores each match high via
matched/(len*0.3) — merging would dilute the general list and drop already-working
phrasings like "what happened" below the lock threshold.
"""

from __future__ import annotations

import pytest

from memory.recall_posture import POSTURE_CATALOG, select_recall_posture


@pytest.mark.parametrize(
    "q",
    [
        "what did you learn last?",
        "what did you learn last",
        "what's the latest?",
        "the last thing you learned",
        "most recent thing you worked on",
        "what have you learned lately",
    ],
)
def test_recency_phrasings_select_temporal_recall(q):
    decision = select_recall_posture(q)
    assert decision.posture == "TEMPORAL_RECALL", f"{q!r} -> {decision.posture}"


def test_temporal_recall_posture_orders_recent_first():
    # The posture recency phrasings resolve to must actually carry the recency sort.
    assert POSTURE_CATALOG["TEMPORAL_RECALL"].temporal_bias == "recent_first"


@pytest.mark.parametrize(
    "q",
    [
        "what happened yesterday",
        "what did we do last time",
    ],
)
def test_existing_temporal_phrasings_still_lock_temporal_recall(q):
    # Regression guard: the new recency rule must NOT dilute the general temporal
    # list and push these below the lock threshold into MULTI_POSTURE.
    decision = select_recall_posture(q)
    assert decision.posture == "TEMPORAL_RECALL", f"{q!r} -> {decision.posture}"
