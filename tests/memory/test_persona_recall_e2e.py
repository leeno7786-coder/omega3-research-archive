"""END-TO-END persona recall seam — the WRITE move (persona_ingest fan-out,
Slices 1-2/4) must be matched by a working READ. This is the exact failure the
operator hit: "I told it I have a sister named Miranda and it's clueless." Here
we seed the statement through the real ingest path into an ISOLATED tmp store,
then fire the real recall APIs and assert Miranda is actually surfaced.

Nothing is minted live: `tests/memory/conftest.py` installs the autouse
`redirect_memory_to_tmp` fixture (covers memory.api's whole write+read chain),
and this suite additionally patches `core.persona_router`'s own OMEGA_ROOT /
_INDEX_PATH globals to tmp (the shared fixture doesn't touch them) — exactly as
tests/memory/test_persona_ingest.py does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CANONICAL_TEXT = (
    "I'm Noah, I have a sister named Miranda, a son named Abel, "
    "and I go to college for cybersecurity."
)


@pytest.fixture(autouse=True)
def _isolate_persona_router(tmp_path, monkeypatch):
    """Align persona_router's OWN root with the conftest's redirected MEMORY_ROOT.

    `redirect_memory_to_tmp` (tests/memory/conftest.py) points
    core.paths.MEMORY_ROOT at ``tmp_path/_canonical_memory_isolation/memory``.
    persona_router.search_by_decimal builds its person_stub scan dir from its
    own OMEGA_ROOT global as ``OMEGA_ROOT/memory/100_personas/person_stub/<id>``,
    so OMEGA_ROOT must resolve to that SAME isolation parent — otherwise the
    scan looks under the wrong root and recall_for_person returns nothing even
    though the stub is on disk (mirrors the live invariant MEMORY_ROOT ==
    OMEGA_ROOT/memory).
    """
    import core.persona_router as pr

    isolation_root = tmp_path / "_canonical_memory_isolation"
    monkeypatch.setattr(pr, "OMEGA_ROOT", isolation_root)
    monkeypatch.setattr(
        pr, "_INDEX_PATH", isolation_root / "guardian" / "state" / "persona_index.json"
    )


def _seed():
    from memory.persona_ingest import ingest_persona_statement

    return ingest_persona_statement(CANONICAL_TEXT, speaker="operator")


def _blob(record: dict) -> str:
    import json

    return json.dumps(record).lower()


def test_recall_for_person_surfaces_the_sister():
    """The deterministic backbone: after ingesting the statement, a
    persona-scoped recall for "miranda" must return her stub — proving the
    write landed, passes RecallVerifier, and is retrievable by scope."""
    from core.persona_router import recall_for_person

    seed = _seed()
    assert {r.person_name for r in seed.person_records} == {"miranda", "abel"}

    hits = recall_for_person("miranda")
    assert hits, (
        "recall_for_person('miranda') returned nothing — the sister stub is not retrievable"
    )

    joined = " ".join(_blob(h) for h in hits)
    assert "miranda" in joined
    assert "sister" in joined, "the recalled record does not carry the sister relationship"


def test_recall_verified_freeform_who_is_my_sister():
    """The real failure repro: a free-form 'who is my sister?' through the
    verified recall path must surface Miranda. Scoped to the persona lobe
    (allowed_lobes=[1]) so the assertion is about retrieval+link-walk, not
    SetFit lobe classification."""
    from memory.api import QuerySpec, recall_verified

    _seed()

    query = QuerySpec(
        cycle_id="persona-recall-e2e",
        text="who is my sister?",
        allowed_lobes=[1],
        limit=10,
    )
    verdict = recall_verified(query)

    payload = getattr(verdict, "answer_payload", None) or {}
    candidates = payload.get("candidates") or []
    blob = " ".join(str(c).lower() for c in candidates)
    assert "miranda" in blob, (
        f"recall_verified did not surface the sister — status={getattr(verdict, 'status', '?')}, "
        f"candidates={candidates!r}"
    )


def _sister_blob(query):
    from memory.api import recall_verified

    verdict = recall_verified(query)
    payload = getattr(verdict, "answer_payload", None) or {}
    return " ".join(str(c).lower() for c in (payload.get("candidates") or []))


def test_fresh_intake_is_held_in_sram_until_flush_then_recallable():
    """The race the operator caught, cured STRUCTURALLY (operator 2026-07-16): a fact
    STATED this turn is HELD in the per-episode SRAM bucket, NOT written to system memory,
    so this turn's recall cannot surface it ("Omega remembers something before it's told").
    At DeepSleep/close the bucket flushes into system memory and the fact recalls normally
    next turn. No pre-write, no recall-time exclusion filter — the ordering is correct by
    construction."""
    import memory.episode_sram as sram
    from memory.api import QuerySpec, recall_verified

    sram.reset()
    query = QuerySpec(cycle_id="sram-hold-x", text="who is my sister?", allowed_lobes=[1], limit=10)

    def _cand_blob(q):
        v = recall_verified(q)
        payload = getattr(v, "answer_payload", None) or {}
        return " ".join(str(c).lower() for c in (payload.get("candidates") or []))

    # THIS turn: the fact is only HELD in the bucket — system memory is untouched, so
    # recall cannot see it.
    sram.open_bucket("ep-sram-e2e", pipeline_id=0)
    sram.hold_perception(CANONICAL_TEXT, speaker="operator")
    try:
        assert "miranda" not in _cand_blob(query), (
            "a fact held in the SRAM bucket must NOT be recallable this turn"
        )

        # DeepSleep/close: flush the held intake into system memory all at once.
        sram.flush("ep-sram-e2e")  # default consolidator runs the real persona ingest

        # Next turn: the fact recalls normally.
        assert "miranda" in _cand_blob(query), (
            "after the SRAM flush the stated fact must recall normally"
        )
    finally:
        sram.reset()


def test_explicit_third_person_remember_command_is_recallable_after_flush(monkeypatch):
    """Exact live regression: a named-person remember command crosses the SRAM
    close seam and recalls every sibling fact without entering a build route or
    collapsing the facts into one supersession chain.
    """
    import memory.episode_sram as sram
    import memory.persona_ingest as persona_ingest
    from core.persona_router import recall_for_person
    from memory.api import QuerySpec, recall_verified

    monkeypatch.setattr(persona_ingest, "_resolve_operator_name", lambda: "Noah Lee")
    text = (
        "Remember Mitchell Ray. He is Noahs best friend, and he is also part "
        "owner of Omega AI LLC. He has 2 daughters and he is married."
    )

    sram.reset()
    sram.open_bucket("ep-remember-mitchell", pipeline_id=0)
    sram.hold_perception(
        text,
        speaker="operator",
        ingress_class="operator_command",
        chat_intent="command",
    )
    try:
        result = sram.flush("ep-remember-mitchell")
        assert result["ingested"] == [text]

        hits = recall_for_person("Mitchell Ray")
        blob = " ".join(_blob(hit) for hit in hits)
        assert "mitchell ray" in blob
        assert "friend" in blob
        assert "part owner of omega ai llc" in blob
        assert "2 daughters" in blob
        assert "married" in blob
        assert all(not hit.get("superseded_by") for hit in hits)

        verdict = recall_verified(
            QuerySpec(
                cycle_id="recall-mitchell-chat",
                text="What do you remember about Mitchell?",
                allowed_lobes=[1],
                limit=10,
            )
        )
        candidates = (getattr(verdict, "answer_payload", None) or {}).get("candidates") or []
        chat_recall_blob = " ".join(_blob(candidate) for candidate in candidates)
        assert "mitchell ray" in chat_recall_blob
        assert "friend" in chat_recall_blob
        assert "part owner of omega ai llc" in chat_recall_blob
        assert "2 daughters" in chat_recall_blob
        assert "married" in chat_recall_blob

        spouse_verdict = recall_verified(
            QuerySpec(
                cycle_id="recall-mitchell-spouse",
                text="Who has a spouse?",
                allowed_lobes=[1],
                limit=10,
            )
        )
        spouse_candidates = (getattr(spouse_verdict, "answer_payload", None) or {}).get(
            "candidates"
        ) or []
        assert spouse_candidates
        spouse_primary = _blob(spouse_candidates[0])
        assert "mitchell ray is married" in spouse_primary
        assert "2 daughters" not in spouse_primary
        assert all(
            candidate.get("semantic_recall") is True
            or (
                bool(candidate.get("recall_basis_id"))
                and candidate.get("recall_basis_validated") is True
            )
            for candidate in spouse_candidates[1:]
        ), "cross-facet extras require actual semantic admission or a validated recall basis"

        children_verdict = recall_verified(
            QuerySpec(
                cycle_id="recall-mitchell-children",
                text="Who has children?",
                allowed_lobes=[1],
                limit=10,
            )
        )
        children_candidates = (getattr(children_verdict, "answer_payload", None) or {}).get(
            "candidates"
        ) or []
        assert children_candidates
        children_primary = _blob(children_candidates[0])
        assert "mitchell ray has 2 daughters" in children_primary
        assert "is married" not in children_primary
        assert all(
            candidate.get("semantic_recall") is True
            or (
                bool(candidate.get("recall_basis_id"))
                and candidate.get("recall_basis_validated") is True
            )
            for candidate in children_candidates[1:]
        ), "cross-facet extras require actual semantic admission or a validated recall basis"
    finally:
        sram.reset()
