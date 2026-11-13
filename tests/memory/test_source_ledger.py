from __future__ import annotations

import hashlib
import json

import pytest

from memory.source_ledger import store as source_store
from memory.source_ledger.store import record_operator_chat_source, record_source, read_source


def test_record_source_writes_hashed_span_addressed_artifact(tmp_path):
    artifact = record_source(
        "hello source ledger",
        source_type="operator_chat",
        origin_id="cycle-1",
        root=tmp_path,
    )

    artifact_path = (
        tmp_path / "memory" / "source_ledger" / "artifacts" / f"{artifact.source_id}.json"
    )
    assert artifact_path.exists()
    stored = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert stored["type"] == "source_artifact"
    assert stored["source_id"] == artifact.source_id
    assert stored["source_type"] == "operator_chat"
    assert stored["origin_id"] == "cycle-1"
    assert stored["content_hash"] == hashlib.sha256(b"hello source ledger").hexdigest()
    assert stored["span_start"] == 0
    assert stored["span_end"] == len("hello source ledger")
    assert stored["span_hash"] == stored["content_hash"]

    reread = read_source(artifact.source_id, root=tmp_path)
    assert reread == artifact


def test_record_source_is_immutable_for_existing_source_id(tmp_path):
    record_source("first value", source_id="src_fixed", root=tmp_path)

    with pytest.raises(ValueError, match="immutable source artifact"):
        record_source("second value", source_id="src_fixed", root=tmp_path)


def test_record_source_reuses_matching_artifact(tmp_path):
    event_time = "2026-08-03T17:23:55+00:00"
    metadata = {"created_at": event_time, "source_session_timestamp": event_time}
    first = record_source(
        "same value", source_type="memory_write", metadata=metadata, root=tmp_path
    )
    second = record_source(
        "same value", source_type="memory_write", metadata=metadata, root=tmp_path
    )

    assert second.source_id == first.source_id
    assert second.content_hash == first.content_hash


def test_operator_chat_without_timestamp_uses_the_canonical_intake_clock(tmp_path, monkeypatch):
    intake_time = "2026-08-03T17:23:55.316280+00:00"
    monkeypatch.setattr(source_store, "now_iso", lambda: intake_time, raising=False)

    citation = record_operator_chat_source(
        "remember this",
        cycle_id="cycle-live",
        root=tmp_path,
    )

    artifact = read_source(citation["source_id"], root=tmp_path)
    assert artifact is not None
    assert artifact.created_at == intake_time
    assert artifact.source_session_timestamp == intake_time
    assert citation["source_session_timestamp"] == intake_time


def test_generic_source_without_timestamp_uses_the_canonical_intake_clock(tmp_path, monkeypatch):
    intake_time = "2026-08-03T17:26:29.218149+00:00"
    monkeypatch.setattr(source_store, "now_iso", lambda: intake_time, raising=False)

    artifact = record_source(
        "episode outcome",
        source_type="lobe:temporal",
        origin_id="episode-live",
        root=tmp_path,
    )

    assert artifact.created_at == intake_time
    assert artifact.source_session_timestamp == intake_time


def test_operator_chat_preserves_injected_session_timestamp(tmp_path):
    citation = record_operator_chat_source(
        "remember this",
        cycle_id="cycle-timestamped",
        session_timestamp="2026-07-30T10:00:00+00:00",
        root=tmp_path,
    )

    artifact = read_source(citation["source_id"], root=tmp_path)
    assert artifact is not None
    assert artifact.created_at == "2026-07-30T10:00:00+00:00"
    assert artifact.source_session_timestamp == "2026-07-30T10:00:00+00:00"


def test_create_staging_records_source_and_stamps_memory_provenance(tmp_path, monkeypatch):
    import memory.engine.validate_write as validate_write
    import memory.promotion as promotion

    monkeypatch.setattr(
        promotion,
        "STAGING_DIR",
        tmp_path / "guardian" / "state" / "staging",
    )
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")

    staging_ref = promotion.create_staging(
        {
            "cycle_id": "cycle-1",
            "coord": [2, 0, 1],
            "type": "concept",
            "source": "omega:test",
            "confidence": 0.8,
            "payload": {
                "title": "Ledger-backed memory",
                "summary": "Canonical records cite immutable source artifacts.",
                "text": "Canonical records cite immutable source artifacts.",
            },
        }
    )

    staging_path = promotion.STAGING_DIR / f"{staging_ref}.json"
    staged = json.loads(staging_path.read_text(encoding="utf-8"))
    provenance = staged["memory_dict"]["provenance"]

    assert provenance["source_id"].startswith("src_")
    assert provenance["source_type"] == "omega:test"
    assert provenance["span_start"] == 0
    assert provenance["span_end"] == len("Canonical records cite immutable source artifacts.")
    assert provenance["span_hash"]
    assert (
        tmp_path / "memory" / "source_ledger" / "artifacts" / f"{provenance['source_id']}.json"
    ).exists()


def test_record_source_bytes_writes_base64_artifact_with_byte_hash(tmp_path):
    raw = b"raw provider payload"
    artifact = source_store.record_source_bytes(
        raw,
        source_type="workspace_gmail",
        origin_id="msg-1@hist-9",
        root=tmp_path,
    )

    assert artifact.metadata.get("content_encoding") == "base64"
    assert artifact.metadata.get("content_kind") == "bytes"
    assert artifact.content_hash == hashlib.sha256(raw).hexdigest()
    assert artifact.content == "cmF3IHByb3ZpZGVyIHBheWxvYWQ="

    stored = json.loads(
        (tmp_path / "memory" / "source_ledger" / "artifacts" / f"{artifact.source_id}.json")
        .read_text(encoding="utf-8")
    )
    assert stored["content_hash"] == hashlib.sha256(raw).hexdigest()


def test_record_source_bytes_is_immutable_for_same_origin(tmp_path):
    first = source_store.record_source_bytes(b"first", origin_id="obj@rev", root=tmp_path)
    with pytest.raises(ValueError, match="immutable source artifact"):
        source_store.record_source_bytes(b"second", origin_id="obj@rev", root=tmp_path)
    assert first.source_id.startswith("src_")


def test_record_source_bytes_reuses_identical_bytes(tmp_path):
    first = source_store.record_source_bytes(
        b"same bytes",
        source_type="workspace_gmail",
        origin_id="msg-1@hist-1",
        root=tmp_path,
    )
    second = source_store.record_source_bytes(
        b"same bytes",
        source_type="workspace_gmail",
        origin_id="msg-1@hist-1",
        root=tmp_path,
    )
    assert second.source_id == first.source_id
    assert second.content_hash == first.content_hash


def test_record_source_bytes_citation_matches_text_artifact_shape(tmp_path):
    artifact = source_store.record_source_bytes(b"payload", root=tmp_path)
    citation = artifact.citation()
    assert set(citation) == {
        "source_id",
        "source_type",
        "span_start",
        "span_end",
        "span_hash",
        "source_content_hash",
        "source_session_id",
        "source_session_timestamp",
    }
    assert citation["source_id"] == artifact.source_id
    assert citation["source_content_hash"] == artifact.content_hash


@pytest.mark.parametrize("bad", [None, "not-bytes", 42])
def test_record_source_bytes_rejects_non_bytes(tmp_path, bad):
    with pytest.raises((TypeError, ValueError)):
        source_store.record_source_bytes(bad, root=tmp_path)
