"""FU-1 Task 1: build_memory_dict must stamp a distinct top-level
write_strength field, kept semantically separate from
provenance.confidence so the deep-sleep readers (mastery / consolidation
/ pruning) stop falling through to their hardcoded 0.5 default.

Real gate-written lobe-2 fragments today carry provenance.confidence but
NO top-level write_strength (confirmed against
memory/200_knowledge/biology/photosynthesis/2.04.e6a6e92f.json).
"""

from memory.core import build_memory_dict


def test_write_strength_stamped_as_distinct_top_level_field():
    mem = build_memory_dict(
        text="Acoustics is the branch of physics concerned with sound.",
        coord=[2, 0, 0],
        source_tag="lobe:knowledge",
        metadata={
            "type": "knowledge_fetch",
            "domain": "acoustics",
            "confidence": 0.8,  # semantic confidence (provenance)
            "write_strength": 0.72,  # distinct encoding strength
        },
    )
    # Distinct top-level field — what mastery/consolidation/pruning read.
    assert mem["write_strength"] == 0.72
    # Provenance confidence stays separate (NOT overwritten by write_strength).
    assert mem["provenance"]["confidence"] == 0.8
    # They are independent values — no double-counting collapse.
    assert mem["write_strength"] != mem["provenance"]["confidence"]


def test_no_write_strength_key_when_metadata_omits_it():
    # Other gates (no write_strength in meta) must be byte-unaffected:
    # the field is absent, not defaulted onto the fragment.
    mem = build_memory_dict(
        text="some identity note",
        coord=[0, 0, 0],
        source_tag="lobe:identity",
        metadata={"type": "concept"},
    )
    assert "write_strength" not in mem
