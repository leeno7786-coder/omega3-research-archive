"""Run scope cannot substitute for canonical claim and source authenticity."""

from __future__ import annotations

import json

import pytest

from core import paths
from memory.engine import walker
from memory.projection_receipt import receipt_path, write_projection_receipt
from memory.run_anchor import RunAnchor
from memory.run_scoped_recall import (
    RunScopedRecallUnavailable, RunScopedVerificationFailed, recall_run_scoped,
)
from memory.source_ledger.store import artifact_path, record_source


def _seed(tmp_path, *, suffix="one", anchor=None):
    profile = paths.MEMORY_ROOT.parent
    anchor = anchor or RunAnchor("job-one", str(tmp_path / "project"), "thread-one", 0)
    source = record_source(
        "The synthetic parser uses Python.", source_type="operator_chat",
        origin_id=f"source-{suffix}", root=profile,
    )
    record = {
        "id": f"dev-{suffix}", "coord": [11, 0, 0], "type": "dev_observation",
        "title": "Synthetic parser", "text": source.content,
        "provenance": dict(source.citation()),
        "enrichment": {"run_provenance": anchor.as_dict()},
    }
    path = paths.MEMORY_ROOT / "1100_dev" / f"{record['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    index = json.loads(walker.INDEX_PATH.read_text(encoding="utf-8"))
    index[record["id"]] = {"coord": record["coord"], "path": str(path)}
    walker.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    write_projection_receipt(record, root=profile)
    return anchor, record, path, source


@pytest.mark.parametrize("damage,reason", [
    ("claim", "canonical_claim_hash_mismatch"),
    ("receipt", "missing_projection_receipt"),
    ("source", "source ledger artifact missing"),
    ("source_content", "stored content hash mismatch"),
    ("citation", "no source citation"),
])
def test_run_scope_rejects_unprovenanced_or_damaged_claim(tmp_path, damage, reason):
    anchor, record, path, source = _seed(tmp_path)
    profile = paths.MEMORY_ROOT.parent
    if damage == "claim":
        record["text"] = "The parser was changed without updating its claim proof."
    elif damage == "receipt":
        receipt_path(record["id"], root=profile).unlink()
    elif damage == "source":
        artifact_path(source.source_id, root=profile).unlink()
    elif damage == "source_content":
        source_path = artifact_path(source.source_id, root=profile)
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        payload["content"] = "Altered source content"
        source_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        record["provenance"] = {}
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(RunScopedVerificationFailed, match=reason):
        recall_run_scoped(anchor, "parser", allowed_lobes=[11], limit=5)


def test_valid_scoped_claim_returns_verified_source_citation(tmp_path):
    anchor, record, _path, source = _seed(tmp_path)
    result = recall_run_scoped(anchor, "parser", allowed_lobes=[11], limit=5)
    assert result.status == "verified"
    assert result.records[0].memory_id == record["id"]
    assert result.records[0].source_citation == dict(source.citation())


def test_source_proof_is_checked_only_after_scope(tmp_path):
    anchor, record, _path, _source = _seed(tmp_path)
    unrelated = RunAnchor("other-job", str(tmp_path / "other"), "other-thread", 0)
    _, other, _, _ = _seed(tmp_path, suffix="other", anchor=unrelated)
    receipt_path(other["id"], root=paths.MEMORY_ROOT.parent).unlink()
    result = recall_run_scoped(anchor, "parser", allowed_lobes=[11], limit=5)
    assert [row.memory_id for row in result.records] == [record["id"]]


def test_unreadable_source_proof_preserves_cause_as_recall_unavailable(tmp_path):
    anchor, _record, _path, source = _seed(tmp_path)
    artifact_path(source.source_id, root=paths.MEMORY_ROOT.parent).write_text("[]", encoding="utf-8")
    with pytest.raises(RunScopedRecallUnavailable, match="proof unavailable") as caught:
        recall_run_scoped(anchor, "parser", allowed_lobes=[11], limit=5)
    assert isinstance(caught.value.__cause__, AttributeError)
