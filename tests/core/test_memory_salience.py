"""Slice 3 B1 — compute_memory_salience + ambient latch."""

from autonomic import symphony_neuromod as sn


def _baseline_neuromod(**overrides):
    base = {
        "dopamine": 0.0,
        "plasticity": 0.0,
        "stress": 0.0,
        "strength": 0.0,
        "cycle_kind": "success",
    }
    base.update(overrides)
    return base


def test_salience_scale_rises_with_dopamine():
    low = sn.compute_memory_salience(_baseline_neuromod())
    high = sn.compute_memory_salience(_baseline_neuromod(dopamine=0.9))
    assert high["scale"] > low["scale"]


def test_salience_tau_factor_shortens_under_stress():
    baseline = sn.compute_memory_salience(_baseline_neuromod())
    stressed = sn.compute_memory_salience(_baseline_neuromod(stress=1.0))
    assert stressed["tau_factor"] < 1.0
    assert stressed["tau_factor"] < baseline["tau_factor"]


def test_salience_tau_factor_lengthens_under_serotonin():
    baseline = sn.compute_memory_salience(_baseline_neuromod())
    aversive = sn.compute_memory_salience(_baseline_neuromod(dopamine=-0.8))
    assert aversive["tau_factor"] > 1.0
    assert aversive["tau_factor"] > baseline["tau_factor"]


def test_salience_tau_factor_uses_explicit_serotonin():
    baseline = sn.compute_memory_salience(_baseline_neuromod())
    explicit = sn.compute_memory_salience(_baseline_neuromod(serotonin=0.8))
    assert explicit["tau_factor"] > baseline["tau_factor"]


def test_salience_non_dict_is_baseline():
    assert sn.compute_memory_salience(None) == {"scale": 1.0, "tau_factor": 1.0}


def test_latch_roundtrip():
    sn._CURRENT_MEMORY_SALIENCE = None
    assert sn.current_memory_salience() is None
    sn.set_current_memory_salience(_baseline_neuromod(dopamine=0.9))
    latched = sn.current_memory_salience()
    assert latched is not None
    assert latched["scale"] > 1.0
