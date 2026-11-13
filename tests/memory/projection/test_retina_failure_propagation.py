"""Perception failures survive the lane and public recall boundaries (M05)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import core.paths as core_paths
from core import timekeeper as TK
from memory import api, recall_lanes, retina
from memory.projection import generation as G
from tests.memory.projection._fixtures import (
    build_and_publish,
    redirect_timekeeper,
    seed_records_only,
    write_record,
)
from tests.memory.projection.test_generation_semantic_retina import CountingEncoder


@pytest.fixture
def profile(tmp_path, monkeypatch):
    redirect_timekeeper(tmp_path)
    retina.unload()
    monkeypatch.setattr(retina, "encoder", lambda: CountingEncoder())
    root = Path(core_paths.MEMORY_ROOT).parent
    monkeypatch.setattr(api, "OMEGA_ROOT", root)
    yield root
    retina.unload()
    TK.reset_for_tests()
    from memory.dme import matmul_engine, recall_hook

    recall_hook.force_unload_all_sessions()
    matmul_engine.unregister_plane()


def _plan(query="cell membranes"):
    return SimpleNamespace(
        original_query=query,
        limit_per_lane=5,
        lane_weights={recall_lanes.LANE_EMBEDDING: 1.0, recall_lanes.LANE_HOT: 0.0},
    )


def _break_encoder(monkeypatch):
    cause = RuntimeError("retina-regression-forward-failed")

    class BrokenEncoder:
        def encode(self, _texts):
            raise cause

    monkeypatch.setattr(retina, "encoder", lambda: BrokenEncoder())
    return cause


def test_query_encoder_failure_survives_recall_lane_dispatch(profile, monkeypatch):
    seed_records_only(profile)
    build_and_publish(profile)
    cause = _break_encoder(monkeypatch)

    with G.projection_read_transaction(profile):
        with pytest.raises(retina.RetinaUnavailable, match="retina-regression-forward-failed") as error:
            recall_lanes.run_recall_lanes(_plan(), {}, lambda _id: None)
    assert error.value.__cause__ is cause


@pytest.mark.parametrize("boundary", ["retina", "public_recall"])
def test_artifact_io_error_is_an_explicit_retina_failure(profile, monkeypatch, boundary):
    seed_records_only(profile)
    build_and_publish(profile)
    cause = PermissionError("retina-regression-artifact-sharing-violation")
    real_read = G.GenerationView.read_bytes

    def denied_read(view, artifact):
        if artifact == retina.INDEX_FILE:
            raise cause
        return real_read(view, artifact)

    monkeypatch.setattr(G.GenerationView, "read_bytes", denied_read)
    with pytest.raises(retina.RetinaUnavailable, match="retina-regression-artifact-sharing-violation") as error:
        if boundary == "retina":
            with G.projection_read_transaction(profile):
                retina.window("neuron synapse", 5)
        else:
            api.read_memory(api.QuerySpec(
                cycle_id="retina-artifact-io-failure", text="neuron synapse",
                allowed_lobes=[2], allowed_subdomains=[0], denied_lobes=[],
            ))
    assert error.value.__cause__ is cause


def test_query_encoder_failure_survives_public_read_memory(profile, monkeypatch):
    seed_records_only(profile)
    build_and_publish(profile)
    cause = _break_encoder(monkeypatch)

    with pytest.raises(retina.RetinaUnavailable, match="retina-regression-forward-failed") as error:
        api.read_memory(api.QuerySpec(
            cycle_id="retina-failure-public", text="neuron synapse",
            allowed_lobes=[2], allowed_subdomains=[0], denied_lobes=[],
        ))
    assert error.value.__cause__ is cause


@pytest.mark.parametrize("operation", ["window", "is_loaded", "retrieval_k", "stats", "record_ids"])
def test_unavailable_generation_cannot_be_read_as_an_empty_retina(profile, operation):
    build_and_publish(profile)
    (G.projection_root(profile) / G.MANIFEST_FILE).write_bytes(b"{corrupt manifest")

    with G.projection_read_transaction(profile) as context:
        assert context.mode == "unavailable" and context.reason
        with pytest.raises(retina.RetinaUnavailable) as error:
            if operation == "window":
                retina.window("cell membranes", 5)
            else:
                getattr(retina, operation)()
        assert context.reason in str(error.value)


def test_declared_empty_generation_remains_a_legitimate_empty_lane(profile):
    build_and_publish(profile)
    with G.projection_read_transaction(profile):
        assert not retina.is_loaded()
        assert retina.window("cell membranes", 5) == []
        assert recall_lanes.run_recall_lanes(_plan(), {}, lambda _id: None) == []


def test_record_ids_are_bound_to_the_callers_generation(profile):
    seed_records_only(profile)
    build_and_publish(profile)
    with G.projection_read_transaction(profile):
        assert set(retina.record_ids()) == {"k1", "k2", "k3", "k4"}
        write_record(profile, "k5", title="Glia", summary="Glial cells support neurons.")
        build_and_publish(profile)
        assert set(retina.record_ids()) == {"k1", "k2", "k3", "k4"}
    with G.projection_read_transaction(profile):
        assert set(retina.record_ids()) == {"k1", "k2", "k3", "k4", "k5"}


def test_no_signal_query_remains_a_legitimate_empty_lane(profile):
    seed_records_only(profile)
    build_and_publish(profile)
    with G.projection_read_transaction(profile):
        assert retina.window("", 5) == []
        assert recall_lanes.run_recall_lanes(_plan(""), {}, lambda _id: None) == []


def test_unbound_legacy_introspection_does_not_claim_a_loaded_generation(profile):
    # No read transaction or fixed-name load has occurred. Inspecting load state
    # stays legal; it is distinct from a generation explicitly marked unavailable.
    assert G.current_read_context() is None
    assert not retina.is_loaded()
    assert retina.legacy_view() is None

