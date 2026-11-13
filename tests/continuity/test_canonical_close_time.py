"""The canonical episode document states the close time its sealed record carries.

Found while running WO-08 over the live history: every retained lobe-9 episode record stores
``enrichment.closed_at`` as an EMPTY string. The terminus hands ``write_episode_to_temporal`` the
enriched body, which has no ``closed_at`` key, so the document's metadata recorded an absence
while the sealed record inside the same document held the observed close time. Readers that
order episode documents by ``closed_at`` silently fell back to the ingestion timestamp.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from tests.helpers import continuity_terminus as ct


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def test_the_canonical_document_records_the_sealed_close_time():
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store), domain="quantum_physics")
    ct.close(store)

    [document] = ct.canonical_episode_documents(carrier.episode_id)
    sealed = json.loads(document["text"])["episode_record"]["temporal"]["closed_at"]
    assert sealed["state"] == "observed"
    assert document["enrichment"]["closed_at"] == sealed["value"], (
        "the document metadata records no close time while its sealed record holds one"
    )
