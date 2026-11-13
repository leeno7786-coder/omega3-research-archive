from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_direct_write_memory(tmp_path, monkeypatch):
    import core.paths as paths
    import memory.encoding_journal as encoding_journal
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.promotion as promotion
    import memory.queues as queues
    import memory.strength_index as strength_index

    memory_root = tmp_path / "memory"
    index_path = memory_root / "indexes" / "coord_index.json"
    staging_dir = tmp_path / "guardian" / "state" / "staging"
    queue_dir = tmp_path / "guardian" / "state" / "queues"

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{}", encoding="utf-8")
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    queue_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(paths, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(promotion, "STAGING_DIR", staging_dir)
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    monkeypatch.setattr(encoding_journal, "JOURNAL_PATH", tmp_path / "encoding_journal.jsonl")
    monkeypatch.setattr(queues, "REFLECTION_QUEUE_PATH", queue_dir / "reflection_eligible.jsonl")
    monkeypatch.setattr(queues, "DEEPSLEEP_QUEUE_PATH", queue_dir / "deepsleep_candidates.jsonl")
    monkeypatch.setattr(
        strength_index,
        "STRENGTH_INDEX_PATH",
        tmp_path / "guardian" / "state" / "strength_index.json",
    )
    monkeypatch.setattr(strength_index, "_singleton", None)

    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    walker._coord_index_generation = 0
    return tmp_path


def _direct_write(
    *, cycle_id: str, coord: list[int], title: str, text: str, domain: str = "bob", enrichment=None
):
    from memory.api import CanonicalEnvelope, write_memory

    receipt = write_memory(
        CanonicalEnvelope(
            cycle_id=cycle_id,
            coord=coord,
            type="concept",
            payload={
                "title": title,
                "summary": text,
                "text": text,
                "domain": domain,
                "subtopic": "direct_write_hooks",
                "enrichment": dict(enrichment or {}),
            },
            source="test.direct_write",
            confidence=0.95,
        ),
        propose=False,
        bypass_guardian=True,
        skip_embed=True,
    )
    assert receipt.validation_status == "committed", receipt
    assert receipt.path
    return str(receipt.path)


def _record(memory_id: str) -> dict:
    from memory.engine import walker

    walker.load_coord_index()
    raw = (walker.COORD_INDEX.get(memory_id) or {}).get("path")
    assert raw, f"missing coord_index path for {memory_id}"
    # coord_index entries are profile-relative by design; resolve like recall does.
    path = walker.resolve_coord_index_path(raw)
    assert path, f"coord_index path did not resolve for {memory_id}: {raw!r}"
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _name_enrichment(name: str) -> dict:
    return {
        "entities": [{"name": name, "facet": "name", "slot": "who", "rule": "test"}],
        "linked_persons": [name],
    }


def test_direct_cognitive_writes_run_entity_linker_between_persona_and_situational(
    isolated_direct_write_memory,
):
    persona_id = _direct_write(
        cycle_id="cycle-persona",
        coord=[1, 1, 1],
        title="Bob relationship",
        text="Bob is my dad.",
        enrichment=_name_enrichment("Bob"),
    )
    situational_id = _direct_write(
        cycle_id="cycle-situational",
        coord=[4, 0, 1],
        title="Bob likes chicken",
        text="Bob likes chicken.",
        enrichment=_name_enrichment("Bob"),
    )

    persona = _record(persona_id)
    situational = _record(situational_id)

    assert situational_id in set(persona.get("links") or [])
    assert persona_id in set(situational.get("links") or [])


def test_direct_cognitive_writes_run_supersedes_chain_hooks(isolated_direct_write_memory):
    prior_id = _direct_write(
        cycle_id="cycle-prior",
        coord=[1, 1, 1],
        title="Bob favorite food",
        text="Bob favorite food is chicken.",
        enrichment=_name_enrichment("Bob"),
    )
    new_id = _direct_write(
        cycle_id="cycle-correction",
        coord=[1, 1, 2],
        title="Bob favorite food",
        text="Bob favorite food is fish.",
        enrichment=_name_enrichment("Bob"),
    )

    prior = _record(prior_id)
    new = _record(new_id)

    assert new.get("supersedes") == prior_id
    assert new.get("version") == 2
    assert prior.get("superseded_by") == new_id


def test_supersedes_versions_do_not_invalidate_source_projection_receipts(
    isolated_direct_write_memory,
):
    from memory.projection_receipt import verify_projection_receipt

    prior_id = _direct_write(
        cycle_id="cycle-receipt-prior",
        coord=[1, 1, 1],
        title="Bob favorite food",
        text="Bob favorite food is chicken.",
        enrichment=_name_enrichment("Bob"),
    )
    new_id = _direct_write(
        cycle_id="cycle-receipt-correction",
        coord=[1, 1, 2],
        title="Bob favorite food",
        text="Bob favorite food is fish.",
        enrichment=_name_enrichment("Bob"),
    )

    prior = _record(prior_id)
    new = _record(new_id)

    assert prior.get("version") == 1
    assert new.get("version") == 2
    assert verify_projection_receipt(prior, root=isolated_direct_write_memory).ok
    assert verify_projection_receipt(new, root=isolated_direct_write_memory).ok

    prior = _record(prior_id)
    new = _record(new_id)
    assert verify_projection_receipt(prior, root=isolated_direct_write_memory).ok
    assert verify_projection_receipt(new, root=isolated_direct_write_memory).ok
