"""Dense semantic embedding modules are retired in favor of the Ettin retina."""

from __future__ import annotations

from pathlib import Path


def test_dense_embedding_modules_are_absent() -> None:
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "boot.py").exists())
    for name in ("local_embed.py", "semantic_state_engine.py", "embed_backfill.py", "embed_contracts.py"):
        assert not (root / "memory" / name).exists(), name


def test_embedding_lane_is_the_ettin_perception_lane() -> None:
    from memory import recall_lanes

    assert recall_lanes._LANE_RUNNERS[recall_lanes.LANE_EMBEDDING] is recall_lanes._run_perception_lane
