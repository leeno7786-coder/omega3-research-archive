"""Stage 4 — post-retirement guards (no ledger imports, survives the delete)."""

from __future__ import annotations

import importlib
import pytest


@pytest.mark.parametrize(
    "mod",
    [
        "memory.taxonomy.ledger",
        "memory.taxonomy.incremental_compiler",
        "memory.taxonomy.dedup_index",
        "memory.taxonomy.compile_state",
    ],
)
def test_retired_taxonomy_modules_unimportable(mod):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(mod)


def test_legacy_partitions_absent():
    from memory.taxonomy import paths as P

    # events/ and raw/ are gone; the helpers that referenced them are removed
    assert not hasattr(P, "events_root") or not P.events_root(None).exists()
    assert not hasattr(P, "raw_objects_root") or not P.raw_objects_root(None).exists()
