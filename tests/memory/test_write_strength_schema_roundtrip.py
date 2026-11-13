"""Pin the FU-1 write_strength write path through the REAL serializer + the
REAL validation gate.

The live autonomous learning write path is:
    memory_writer._persist_knowledge_fetch
      -> KnowledgeGate().ingest(content, metadata={..., "write_strength": ws})
      -> gates/base._write  (forwards metadata verbatim)
      -> memory.core.build_memory_dict  (stamps top-level write_strength)
      -> memory.engine.validate_write.atomic_write  (validates vs memory_schema.json)

This test exercises build_memory_dict (the real serializer the live path uses)
+ atomic_write (the real gate), into a hermetic tmp store. On `main` it FAILS:
atomic_write raises ValueError("Schema validation failed: Additional properties
are not allowed ('write_strength' was unexpected)") — the guardian/logs/memory.log:327
signature. After the schema fix it PASSES.
"""

import json
from pathlib import Path

import memory.engine.validate_write as vw
from memory.core import build_memory_dict
from memory.engine.validate_write import atomic_write


def test_write_strength_survives_schema_and_roundtrips(tmp_path, monkeypatch):
    # Hermetic store — never touch the real memory/ tree.
    monkeypatch.setattr(vw, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(vw, "TMP_DIR", tmp_path / "tmp_writes")

    # The exact serializer the live gate path uses, carrying a top-level
    # write_strength via metadata (same shape memory_writer builds).
    mem = build_memory_dict(
        text="Education is the transmission of knowledge and skills.",
        coord=[2, 0, 0],
        source_tag="lobe:knowledge",
        metadata={
            "type": "knowledge_fetch",
            "domain": "education",
            "confidence": 0.85,
            "write_strength": 0.85,
        },
    )
    # FU-1: serializer stamps a DISTINCT top-level field.
    assert mem["write_strength"] == 0.85
    assert mem.get("provenance", {}).get("confidence") == 0.85  # kept separate

    # The real gate: validate vs memory_schema.json, then write. update_index=False
    # keeps the test off the shared coord_index.
    path = atomic_write(mem, update_index=False)

    # Round-trip: the field survived validation + serialization to disk.
    written = json.loads(Path(path).read_text(encoding="utf-8"))
    assert written["write_strength"] == 0.85
