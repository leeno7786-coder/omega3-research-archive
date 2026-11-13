"""One trait key across BOTH identity paths (operator finding 2026-07-03).

Live-confirmed dual-key split: the window-emergent path kept full-sentence trait
keys while the chat-reflection path _trait_slug-truncated to <=6 words, so the
SAME trait (focus_on_definition) split into two store records and never
accumulated evidence past the >=2 readback floor. `normalize_trait_name` is now
the single slug authority (<=6 words, <=48 chars) and `_trait_slug` delegates to
it. Also pins that `upsert_trait` carries the chat-reflection evidence/status
through to the canonical write (it silently stripped them).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deepsleep_loop._deepsleep_state import Trait
from deepsleep_loop.identity_store import normalize_trait_name, upsert_trait

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def _install_fake_canonical_backend(monkeypatch):
    state: dict[str, dict] = {}
    writes: list[dict] = []

    def _snapshot():
        return {
            mid: {"coord": list(meta.get("coord") or []), "path": f"/fake/{mid}.json"}
            for mid, meta in state.items()
        }

    def _write_memory(envelope, *, propose=True, bypass_guardian=False, **kwargs):
        memory_id = envelope.id or envelope.payload.get("id")
        state[memory_id] = {
            "id": memory_id,
            "coord": list(envelope.coord),
            "trait": dict(envelope.payload.get("trait") or {}),
        }
        writes.append({"id": memory_id, "payload": dict(envelope.payload)})
        return SimpleNamespace(
            cycle_id=envelope.cycle_id,
            staging_ref=None,
            path=memory_id,
            validation_status="committed",
            message="ok",
        )

    monkeypatch.setattr("memory.engine.walker.load_coord_index", lambda: state)
    monkeypatch.setattr("memory.engine.walker.coord_index_snapshot", _snapshot)
    monkeypatch.setattr(
        "memory.engine.walker.load_meta_by_path",
        lambda path: dict(state.get(Path(path).stem, {})),
    )
    monkeypatch.setattr("memory.api.write_memory", _write_memory)
    return state, writes


def test_normalize_trait_name_slugs_long_sentences():
    sentence = "I separate observation from evaluation before forming a judgment"
    key = normalize_trait_name(sentence)
    assert key == "i_separate_observation_from_evaluation_before"
    assert len(key) <= 48


def test_window_and_chat_paths_share_one_key():
    from omega_loop.episode.prompts import _trait_slug

    sentence = "Focus on definition before exploring implications of a question"
    assert _trait_slug(sentence) == normalize_trait_name(sentence)


def test_normalize_is_idempotent_on_existing_slugs():
    assert normalize_trait_name("focus_on_definition") == "focus_on_definition"
    assert normalize_trait_name("Persistent Learner") == "persistent_learner"
    assert normalize_trait_name(normalize_trait_name("Persistent Learner")) == "persistent_learner"


def test_upsert_trait_preserves_chat_reflection_metadata(monkeypatch, tmp_path):
    _, writes = _install_fake_canonical_backend(monkeypatch)

    upsert_trait(
        Trait(
            "focus_on_definition",
            strength=0.4,
            evidence_count=2,
            evidence="I anchored the reply on a definition first",
            status="confirmed",
        ),
        path=tmp_path / "identity.json",
    )

    payload = writes[0]["payload"]["trait"]
    assert payload["evidence"] == "I anchored the reply on a definition first"
    assert payload["status"] == "confirmed"


def test_trait_from_meta_reconstructs_evidence_and_status():
    """Read side of the same round-trip (cascade audit 2026-07-09): canonical readback
    dropped evidence/status, so load_identity rebuilt every trait with evidence="" and
    the chat-reflection self-model block rendered name+count only, losing what each
    trait means. _trait_from_meta must read both back."""
    from deepsleep_loop.identity_store import _trait_from_meta

    trait = _trait_from_meta(
        {
            "trait": {
                "name": "focus_on_definition",
                "strength": 0.7,
                "evidence_count": 3,
                "evidence": "I clarify terms before answering",
                "status": "confirmed",
            }
        }
    )
    assert trait is not None
    assert trait.evidence == "I clarify terms before answering"
    assert trait.status == "confirmed"
    assert trait.evidence_count == 3
