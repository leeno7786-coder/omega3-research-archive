"""Test episode temporal threading (Phase 2 §P2-S12)."""

from __future__ import annotations

import pytest

from core.episode_temporal import (
    capture_temporal_context,
    compact_temporal_summary,
    default_temporal_provider,
    TEMPORAL_FIELDS,
)


def test_temporal_fields_constant():
    """TEMPORAL_FIELDS should match the documented schema."""
    assert TEMPORAL_FIELDS == (
        "captured_at_iso",
        "wall_clock_seconds",
        "tick_count",
        "phase",
        "cycle_id",
        "epoch_id",
        "source",
    )


def test_capture_temporal_context_basic():
    """Basic temporal context capture with defaults."""
    envelope = capture_temporal_context()
    assert isinstance(envelope, dict)
    assert envelope["source"] in ("live", "unavailable")
    assert envelope["tick_count"] >= 0
    assert envelope["phase"] == "pressure"
    assert isinstance(envelope["captured_at_iso"], str)
    assert isinstance(envelope["wall_clock_seconds"], float)
    assert isinstance(envelope["cycle_id"], str)
    assert isinstance(envelope["epoch_id"], str)


def test_capture_temporal_context_with_params():
    """Temporal context capture with explicit parameters."""
    envelope = capture_temporal_context(
        tick_count=42,
        phase="global",
        cycle_id="test-cycle-123",
        epoch_id="test-epoch-456",
    )
    assert envelope["tick_count"] == 42
    assert envelope["phase"] == "global"
    assert envelope["cycle_id"] == "test-cycle-123"
    assert envelope["epoch_id"] == "test-epoch-456"
    assert envelope["source"] == "live"


def test_capture_temporal_context_test_determinism():
    """Test determinism via now_iso/now_wall overrides."""
    envelope = capture_temporal_context(
        now_iso="2026-06-09T12:00:00+00:00",
        now_wall=1717929600.0,
    )
    assert envelope["captured_at_iso"] == "2026-06-09T12:00:00+00:00"
    assert envelope["wall_clock_seconds"] == 1717929600.0


def test_compact_temporal_summary():
    """Compact summary reduces envelope to small subset."""
    full = capture_temporal_context(tick_count=5)
    summary = compact_temporal_summary(full)
    assert set(summary.keys()) == {"captured_at_iso", "source", "tick_count"}
    assert summary["tick_count"] == 5
    assert summary["source"] in ("live", "unavailable")


def test_compact_temporal_summary_empty():
    """Compact summary handles empty envelope gracefully."""
    summary = compact_temporal_summary(None)
    assert summary == {}
    summary = compact_temporal_summary({})
    assert summary == {}


def test_default_temporal_provider():
    """Default provider returns callable that builds fresh envelopes."""
    provider = default_temporal_provider()
    assert callable(provider)
    envelope1 = provider()
    envelope2 = provider()
    # Each call should produce a fresh envelope (different timestamps)
    assert isinstance(envelope1, dict)
    assert isinstance(envelope2, dict)
    assert envelope1["captured_at_iso"] != envelope2["captured_at_iso"]


def test_temporal_envelope_threaded_to_episode():
    """Temporal envelope is threaded onto episode.temporal."""
    from pressure_loop.episode import EpisodeStore

    temporal = capture_temporal_context(tick_count=10, phase="pressure")
    store = EpisodeStore()
    episode = store.open(
        opened_by="test",
        pressure={"aggregate": 0.5},
        temporal=temporal,
        pipeline_id=0,
        work_kind="autonomous",
    )
    assert episode is not None
    assert episode.temporal == temporal
    assert episode.temporal["tick_count"] == 10
    assert episode.temporal["phase"] == "pressure"
    store.close()


def test_temporal_envelope_defaults_to_empty_dict():
    """Episode.temporal defaults to empty dict when not provided."""
    from pressure_loop.episode import EpisodeStore

    store = EpisodeStore()
    episode = store.open(
        opened_by="test",
        pressure={"aggregate": 0.5},
        pipeline_id=0,
        work_kind="autonomous",
    )
    assert episode is not None
    assert episode.temporal == {}
    store.close()
