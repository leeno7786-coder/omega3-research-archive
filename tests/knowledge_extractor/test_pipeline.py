"""Knowledge extractor pipeline — deterministic ingest/derive/store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_extractor import extract
from knowledge_extractor._types import KnowledgeArtifact, Source
from knowledge_extractor import derive, ingest, store
from pressure_loop.policy import clear_policy_cache, knowledge_artifact_store_path

_SAMPLE_CONTENT = (
    "Quantum Computing enables researchers to explore entanglement and superposition. "
    "Scientists have developed new algorithms that solve optimization problems faster. "
    "The Quantum Computing field continues to attract investment from technology companies."
)

_SAMPLE_SOURCE = Source(
    kind="web_result",
    content=_SAMPLE_CONTENT,
    title="Quantum Computing Overview",
    source_ref="https://host/x",
)


@pytest.fixture
def artifact_store_file(tmp_path, monkeypatch):
    store_path = tmp_path / "artifacts.jsonl"
    monkeypatch.setattr(
        "pressure_loop.policy._KNOWLEDGE_CONFIG_PATH",
        tmp_path / "missing-knowledge.yaml",
    )
    clear_policy_cache()

    def _path(policy=None):
        return str(store_path)

    monkeypatch.setattr(
        "pressure_loop.policy.knowledge_artifact_store_path",
        _path,
    )
    return store_path


def test_extract_produces_deterministic_artifact(artifact_store_file):
    first = extract(_SAMPLE_SOURCE)
    second = extract(_SAMPLE_SOURCE)

    assert isinstance(first, KnowledgeArtifact)
    assert first.domain == "host"
    assert first.concepts
    assert first.facts
    assert 0.0 <= first.confidence <= 1.0
    assert first.source_kind == "web_result"
    assert first.source_ref == "https://host/x"
    assert first.artifact_id
    assert first == second


def test_store_writes_one_jsonl_line(artifact_store_file):
    normalized = ingest.run(_SAMPLE_SOURCE)
    fields = derive.run(normalized)
    artifact = KnowledgeArtifact(
        domain=fields["domain"],
        concepts=tuple(fields["concepts"]),
        facts=tuple(fields["facts"]),
        relationships=tuple(fields["relationships"]),
        mastery_tags=tuple(fields["mastery_tags"]),
        confidence=float(fields["confidence"]),
        source_ref=normalized.get("source_ref"),
        source_kind=str(normalized.get("source_kind") or ""),
        artifact_id=str(fields["artifact_id"]),
    )

    store.run(artifact)

    lines = artifact_store_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["artifact_id"] == artifact.artifact_id
    assert row["domain"] == "host"


def test_knowledge_config_default_path():
    clear_policy_cache()
    assert knowledge_artifact_store_path().endswith("knowledge_extractor/state/artifacts.jsonl")


def test_pipeline_phases_stable_without_store_side_effect():
    normalized = ingest.run(_SAMPLE_SOURCE)
    fields_a = derive.run(normalized)
    fields_b = derive.run(normalized)
    assert fields_a == fields_b
