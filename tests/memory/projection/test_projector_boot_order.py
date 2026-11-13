"""DeepSleep maintains canonical inputs under commit order, then publishes outside it."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.paths as core_paths
from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import dispatch as D
from memory.projection import generation as G
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only

REPO = Path(__file__).resolve().parents[3]


def test_deepsleep_dispatch_is_after_commit_region_and_uses_program0(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(core_paths, "OMEGA_ROOT", profile)
    monkeypatch.setattr(core_paths, "MEMORY_ROOT", profile / "memory")
    redirect_timekeeper(tmp_path)
    try:
        seed_records_only(profile)
        from deepsleep_loop import loop as DL

        order: list[tuple[str, bool]] = []
        counts = DL._maintain_projection_inputs_after_deepsleep(
            context_id="synthetic-episode",
            scope="episode",
            before_projection=lambda _root: order.append(("canonical-associations", PD.in_serialized_commit_region())),
        )
        assert isinstance(counts, dict)
        assert order == [("canonical-associations", False)]
        assert not (G.projection_root(profile) / G.MANIFEST_FILE).exists()

        original_dispatch = D.projection_dispatch

        def observe_dispatch(root=None, *, reason):
            order.append((f"dispatch:{reason}", PD.in_serialized_commit_region()))
            return original_dispatch(root, reason=reason)

        monkeypatch.setattr(D, "projection_dispatch", observe_dispatch)
        report = DL._publish_projection_after_deepsleep(
            context_id="synthetic-episode", scope="episode"
        )
        assert report.owner == "program0" and report.action == "published"
        assert order == [("canonical-associations", False), ("dispatch:deepsleep", False)]

        source = (REPO / "deepsleep_loop" / "loop.py").read_text(encoding="utf-8")
        region = source.index("    with region:")
        post_region_marker = source.index("    # The build reads a closed canonical snapshot", region)
        dispatch_call = source.index(
            "_publish_projection_after_deepsleep(\n            context_id=context_id,", post_region_marker
        )
        assert dispatch_call > post_region_marker > region
        assert source.count("_publish_projection_after_deepsleep(") == 2  # helper + one terminus call
        assert "reindex_and_compile" not in source
    finally:
        D.release_lifecycle_lock()
        TK.reset_for_tests()


def test_deepsleep_dispatch_failure_preserves_program0_reason_and_detail(tmp_path, monkeypatch):
    from core.required_substrate import RequiredSubstrateError
    from memory.projection.dispatch import DispatchReport

    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(core_paths, "OMEGA_ROOT", profile)
    monkeypatch.setattr(core_paths, "MEMORY_ROOT", profile / "memory")
    expected_detail = {"reason": "unscorable_coordinates", "error": "source ledger moved"}
    monkeypatch.setattr(
        D,
        "projection_dispatch",
        lambda _root=None, *, reason: DispatchReport(
            owner="program0",
            action="failed",
            publication=None,
            drain_status="failed",
            generation_id=None,
            detail=expected_detail,
        ),
    )

    from deepsleep_loop import loop as DL

    with pytest.raises(RequiredSubstrateError) as raised:
        DL._publish_projection_after_deepsleep(context_id="failed-episode", scope="episode")

    assert raised.value.reason == "unscorable_coordinates"
    assert raised.value.detail == str(expected_detail)
