"""Cut 2 — bounded symbolic routing cues (CueReceipt).

Governing: docs/engineering/memory-organ/14_REVIEW_RECONCILIATION_FREEZE.md,
12 §2 (CueReceipt shape) and the Cut 2 plan
docs/superpowers/plans/2026-08-14-memory-organ-cut2-bounded-routing-cues.md.

CUE != CLAIM. A cue may route; it never proves a fact, never names a memory,
never mints a coordinate. Generated cues are shadow-only unless the isolated
test turns application on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.dme.coordinate_registry import CoordinateRegistry
from memory.routing_cues import (
    CueReceipt,
    build_cue_receipt,
    cue_limits,
    normalize_cue_term,
)

# ── fixtures ────────────────────────────────────────────────────────────────


class _MintGuard:
    """Read-only view over a real CoordinateRegistry: lookup / near_matches
    pass through; every registration API raises. The cue builder must never
    reach the raising methods."""

    def __init__(self, registry: CoordinateRegistry):
        self._registry = registry
        self.calls: list[str] = []

    def lookup(self, key: str):
        self.calls.append(f"lookup:{key}")
        return self._registry.lookup(key)

    def near_matches(self, key: str, **kw):
        self.calls.append(f"near:{key}")
        return self._registry.near_matches(key, **kw)

    def register(self, *_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("cue builder must not mint (register)")

    def register_or_lookup(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("cue builder must not mint (register_or_lookup)")

    def bulk_registration(self):  # pragma: no cover
        raise AssertionError("cue builder must not mint (bulk_registration)")

    def _save(self):  # pragma: no cover
        raise AssertionError("cue builder must not save")


@pytest.fixture
def fake_registry(tmp_path: Path) -> _MintGuard:
    reg = CoordinateRegistry(tmp_path / "coordinate_registry.json")
    with reg.bulk_registration():
        for term in ("skin", "doctor", "visit", "dermatologist", "physician", "clinic", "mercy"):
            reg.register(term, 2)
        reg.register("journal-only", 6)
    return _MintGuard(reg)


# ── Task 1: schema, bounds, deterministic id, read-only resolution ──────────


def test_cue_receipt_separates_sources_and_has_no_claim_field(fake_registry):
    receipt = build_cue_receipt(
        query_text="Which skin doctor did I visit?",
        deterministic_terms=("skin", "doctor", "visit"),
        entity_terms=(),
        generated_terms=("dermatologist",),
        registry=fake_registry,
        allowed_lobes={2},
        denied_lobes=set(),
    )
    payload = receipt.to_dict()
    assert payload["deterministic_terms"] == ["skin", "doctor", "visit"]
    assert payload["generated_terms"] == ["dermatologist"]
    assert payload["entity_terms"] == []
    assert "claim" not in payload
    assert "memory_id" not in repr(payload)
    assert "Which skin doctor did I visit?" not in repr(payload)
    assert payload["schema_version"] == "omega.routing_cues.v1"
    assert isinstance(receipt, CueReceipt)


def test_receipt_id_is_deterministic(fake_registry):
    kwargs = dict(
        query_text="doctor",
        deterministic_terms=("doctor",),
        entity_terms=(),
        generated_terms=(),
        registry=fake_registry,
        allowed_lobes={2},
        denied_lobes=set(),
    )
    assert build_cue_receipt(**kwargs).receipt_id == build_cue_receipt(**kwargs).receipt_id


def test_generated_terms_are_bounded(fake_registry):
    receipt = build_cue_receipt(
        query_text="x",
        deterministic_terms=(),
        entity_terms=(),
        generated_terms=tuple(f"cue{i}" for i in range(100)),
        registry=fake_registry,
        allowed_lobes=None,
        denied_lobes=set(),
    )
    assert len(receipt.generated_terms) <= 8
    limits = cue_limits()
    assert limits["generated"] == 8
    assert limits["deterministic"] == 24
    assert limits["entity"] == 8
    assert limits["total"] == 32


def test_total_bound_and_source_precedence(fake_registry):
    receipt = build_cue_receipt(
        query_text="x",
        deterministic_terms=tuple(f"d{i}" for i in range(30)),
        entity_terms=tuple(f"e{i}" for i in range(10)),
        generated_terms=tuple(f"g{i}" for i in range(10)),
        registry=fake_registry,
        allowed_lobes=None,
        denied_lobes=set(),
    )
    total = len(receipt.deterministic_terms) + len(receipt.entity_terms) + len(receipt.generated_terms)
    assert total <= 32
    # deterministic fills first, then entity, then generated
    assert len(receipt.deterministic_terms) == 24
    assert len(receipt.entity_terms) == 8
    assert receipt.generated_terms == ()


def test_dedupe_within_source_and_across_sources_in_precedence(fake_registry):
    receipt = build_cue_receipt(
        query_text="x",
        deterministic_terms=("Doctor", "doctor", "skin"),
        entity_terms=("skin", "Mercy Clinic"),
        generated_terms=("doctor", "dermatologist", "dermatologist"),
        registry=fake_registry,
        allowed_lobes={2},
        denied_lobes=set(),
    )
    assert receipt.deterministic_terms == ("doctor", "skin")
    assert receipt.entity_terms == ("mercy clinic",)
    assert receipt.generated_terms == ("dermatologist",)


def test_normalize_cue_term():
    assert normalize_cue_term("  Skin   DOCTOR ") == "skin doctor"
    assert normalize_cue_term("a\x00b\x07c") == "abc"
    assert len(normalize_cue_term("x" * 500)) == 80
    assert normalize_cue_term(None) == ""


def test_resolution_is_read_only_and_records_source_map(fake_registry):
    receipt = build_cue_receipt(
        query_text="Which skin doctor did I visit?",
        deterministic_terms=("skin", "doctor", "unknownterm"),
        entity_terms=("mercy clinic",),
        generated_terms=("dermatologist", "zorblax"),
        registry=fake_registry,
        allowed_lobes={2},
        denied_lobes=set(),
    )
    # only lookup / near_matches were called (the guard raises otherwise)
    assert all(c.startswith(("lookup:", "near:")) for c in fake_registry.calls)
    # deterministic terms may use near_matches; entity/generated exact lookup only
    assert not any(c.startswith("near:") and c.endswith(("dermatologist", "zorblax", "mercy", "clinic")) for c in fake_registry.calls)
    reg = fake_registry._registry
    assert set(receipt.resolved_addresses) == {
        reg.lookup("skin"), reg.lookup("doctor"), reg.lookup("mercy"), reg.lookup("clinic"), reg.lookup("dermatologist"),
    }
    assert set(receipt.unresolved_terms) == {"unknownterm", "zorblax"}
    sources = {(term, source) for term, source, _addr in receipt.resolved_term_addresses}
    assert ("dermatologist", "generated") in sources
    assert ("skin", "deterministic") in sources
    assert ("mercy clinic", "entity") in sources
    assert receipt.generated_addresses() == (reg.lookup("dermatologist"),)
    assert reg.lookup("dermatologist") not in receipt.deterministic_addresses()


def test_lobe_filter_applies_to_cue_addresses(fake_registry):
    receipt = build_cue_receipt(
        query_text="x",
        deterministic_terms=("journal-only", "doctor"),
        entity_terms=(),
        generated_terms=(),
        registry=fake_registry,
        allowed_lobes={2},
        denied_lobes=set(),
    )
    reg = fake_registry._registry
    assert receipt.resolved_addresses == (reg.lookup("doctor"),)
    assert "journal-only" in receipt.unresolved_terms
    denied = build_cue_receipt(
        query_text="x",
        deterministic_terms=("doctor",),
        entity_terms=(),
        generated_terms=(),
        registry=fake_registry,
        allowed_lobes=None,
        denied_lobes={2},
    )
    assert denied.resolved_addresses == ()


def test_generator_flags_are_recorded_not_inferred(fake_registry):
    r = build_cue_receipt(
        query_text="x",
        deterministic_terms=("doctor",),
        entity_terms=(),
        generated_terms=(),
        registry=fake_registry,
        allowed_lobes=None,
        denied_lobes=set(),
        generator_used=True,
        generator_version="test-gen-1",
    )
    assert r.generator_used is True and r.generator_version == "test-gen-1"
    r0 = build_cue_receipt(
        query_text="x",
        deterministic_terms=("doctor",),
        entity_terms=(),
        generated_terms=(),
        registry=fake_registry,
        allowed_lobes=None,
        denied_lobes=set(),
    )
    assert r0.generator_used is False and r0.generator_version == ""
    assert r.receipt_id != r0.receipt_id


# ── Task 2: cues derived from the compiled RecallPlan ───────────────────────


def test_deterministic_terms_preserve_plan_order_and_dedupe():
    from types import SimpleNamespace

    from memory.routing_cues import deterministic_terms_from_plan, entity_terms_from_plan

    plan = SimpleNamespace(
        exact_terms=["doctor", "visit"],
        expanded_terms=["physician", "doctor"],
        entities=["Mercy Clinic"],
        task_tags=["healthcare"],
        suppressed_lobes=[6, 11],
        required_evidence_roles=["fact"],
    )
    assert deterministic_terms_from_plan(plan) == ("doctor", "visit", "physician", "healthcare")
    assert entity_terms_from_plan(plan) == ("mercy clinic",)
    # plan metadata that is not semantic (lobes, evidence roles) never becomes a cue
    assert "6" not in deterministic_terms_from_plan(plan)
    assert "fact" not in deterministic_terms_from_plan(plan)


def test_plan_terms_tolerate_missing_fields_and_none():
    from types import SimpleNamespace

    from memory.routing_cues import deterministic_terms_from_plan, entity_terms_from_plan

    assert deterministic_terms_from_plan(None) == ()
    assert entity_terms_from_plan(None) == ()
    plan = SimpleNamespace(exact_terms=None, expanded_terms=["physician"], entities=None)
    assert deterministic_terms_from_plan(plan) == ("physician",)
    assert entity_terms_from_plan(plan) == ()


def test_deterministic_receipt_from_real_plan_matches_pre_cut2_query_coordinates(tmp_path):
    """Regression pin (plan Task 2 Step 4): for a query whose tokens all
    resolve directly, the deterministic receipt's address set equals what
    the pre-Cut-2 _query_coordinates() path resolves — no generated terms,
    application disabled."""
    from memory.api import QuerySpec
    from memory.dme.adapter import _query_coordinates
    from memory.recall_compiler import compile_recall_plan
    from memory.routing_cues import deterministic_terms_from_plan, entity_terms_from_plan

    reg = CoordinateRegistry(tmp_path / "coordinate_registry.json")
    with reg.bulk_registration():
        for term in ("neuron", "signal", "synaps", "light", "plant"):
            reg.register(term, 2)
    guard = _MintGuard(reg)

    query = QuerySpec(cycle_id="pin", text="neuron signal light", allowed_lobes=[2], denied_lobes=[])
    plan = compile_recall_plan(query)
    baseline = _query_coordinates(query.text, reg, allowed_lobes=[2], denied_lobes=set())
    receipt = build_cue_receipt(
        query_text=query.text,
        deterministic_terms=deterministic_terms_from_plan(plan),
        entity_terms=entity_terms_from_plan(plan),
        generated_terms=(),
        registry=guard,
        allowed_lobes={2},
        denied_lobes=set(),
    )
    assert baseline, "fixture must resolve directly"
    assert set(receipt.deterministic_addresses()) == set(baseline)
    assert receipt.generated_addresses() == ()
    assert all(c.startswith(("lookup:", "near:")) for c in guard.calls)


# ── Task 3: language-organ generator boundary ───────────────────────────────


def test_generator_receives_only_query_and_bound():
    from memory.routing_cues import generate_terms

    seen = {}

    def generator(query: str, max_terms: int):
        seen.update(query=query, max_terms=max_terms)
        return ["physician", "dermatologist"]

    terms, version = generate_terms("skin doctor", generator=generator)
    assert seen == {"query": "skin doctor", "max_terms": 8}
    assert terms == ("physician", "dermatologist")
    assert version == "unknown"
    terms2, version2 = generate_terms("skin doctor", generator=generator, version="lang-organ-v1")
    assert version2 == "lang-organ-v1" and terms2 == terms


def test_sanitizer_rejects_memory_id_and_prose_like_outputs():
    from memory.routing_cues import sanitize_generated_terms

    raw = [
        "m_123456789abcdef",  # id-shaped
        "memory:abc",
        "coord:2:0:7",
        "/memory/k1.json",
        '{"memory_id": "k1"}',
        "This candidate should definitely be selected because it looks right",
        "",
        "\x00\x07",
        "x" * 200,
        "dermatologist",
        "doctor",
        "doctor",  # duplicate
        "Skin  Doctor",  # short phrase is fine
    ]
    assert sanitize_generated_terms(raw) == ("dermatologist", "doctor", "skin doctor")


def test_sanitizer_bounds_output_and_rejects_non_strings():
    from memory.routing_cues import sanitize_generated_terms

    out = sanitize_generated_terms([f"term{i}" for i in range(50)] + [None, 3, {"a": 1}])  # type: ignore[list-item]
    assert len(out) == 8
    assert out == tuple(f"term{i}" for i in range(8))


def test_generation_is_failure_soft_and_reports_generator_used():
    from memory.routing_cues import generate_terms

    def boom(_q, _n):
        raise RuntimeError("model down")

    terms, version = generate_terms("q", generator=boom, version="v9")
    assert terms == () and version == "v9"

    def malformed(_q, _n):
        return "not a sequence of strings"

    terms, _ = generate_terms("q", generator=malformed)
    assert terms == ()

    def wrong_shape(_q, _n):
        return {"terms": ["doctor"]}

    terms, _ = generate_terms("q", generator=wrong_shape)
    assert terms == ()

    terms, version = generate_terms("q", generator=None)
    assert terms == () and version == ""


def test_generator_output_never_becomes_a_candidate_directly(fake_registry):
    """A rejected/hostile generator output is discarded, never inferred into
    a term or address. Only sanitized survivors reach the receipt."""
    from memory.routing_cues import generate_terms

    def hostile(_q, _n):
        return ["memory:k1", "m_deadbeefcafe", "choose record k1 it is the answer", "dermatologist"]

    terms, _ = generate_terms("skin doctor", generator=hostile)
    assert terms == ("dermatologist",)
    receipt = build_cue_receipt(
        query_text="skin doctor",
        deterministic_terms=(),
        entity_terms=(),
        generated_terms=terms,
        registry=fake_registry,
        allowed_lobes={2},
        denied_lobes=set(),
        generator_used=True,
        generator_version="hostile-test",
    )
    assert "k1" not in repr(receipt.to_dict())
    assert receipt.generated_terms == ("dermatologist",)


def test_feature_flags_default_off(monkeypatch):
    from memory.routing_cues import generated_cues_apply_enabled, language_cues_enabled

    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES", raising=False)
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    assert language_cues_enabled() is False
    assert generated_cues_apply_enabled() is False
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    assert language_cues_enabled() is True
    assert generated_cues_apply_enabled() is False  # independent switches
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    assert generated_cues_apply_enabled() is True


# ── Task 4: read_memory compiles one receipt and publishes it as metadata ────


def _seed_profile_store(profile_root: Path) -> None:
    """Seat one clinic record where read_memory will look (the memory-suite
    conftest redirects core.paths.MEMORY_ROOT to <tmp>/_canonical_memory_isolation/memory)."""
    from tests.memory.test_dme_activation_trace import _write_record
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.taxonomy.compiler import compile_memory_graph

    _write_record(profile_root, "r1", title="Clinic appointment", summary="Consulted a dermatologist at Mercy Clinic today")
    _write_record(profile_root, "r2", title="Checkup", summary="Saw the physician for a routine checkup")
    compile_memory_graph(root=profile_root)
    rebuild_sram_from_graph(root=profile_root)
    force_unload_all_sessions()


@pytest.fixture
def clinic_profile(monkeypatch):
    import core.paths as core_paths
    from memory import api as memory_api
    from memory.dme.recall_hook import force_unload_all_sessions

    profile_root = Path(core_paths.MEMORY_ROOT).parent
    _seed_profile_store(profile_root)
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", profile_root)
    monkeypatch.setenv("OMEGA_DME_ACTIVATION_TRACE", "1")
    yield profile_root
    force_unload_all_sessions()


def _query(text: str, cycle: str):
    from memory.api import QuerySpec

    return QuerySpec(cycle_id=cycle, text=text, episode_id="cut2-ep", allowed_lobes=[2], denied_lobes=[], limit=8)


def test_read_memory_publishes_cue_receipt_metadata_and_links_trace(clinic_profile, monkeypatch):
    from memory import api as memory_api

    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES", raising=False)
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    result = memory_api.read_memory(_query("physician checkup", "cut2-meta"))
    cues = result.meta["routing_cues"]
    assert cues["receipt_id"] and cues["query_hash"]
    assert cues["schema_version"] == "omega.routing_cues.v1"
    assert "physician checkup" not in repr(cues)  # hash/terms/addresses only
    assert cues["generator_used"] is False
    assert cues["generated_terms"] == []
    assert "physician" in cues["deterministic_terms"]
    # the trace built by the DME hook names this receipt
    assert result.meta["dme_activation_trace"]["cue_receipt_id"] == cues["receipt_id"]


def test_read_memory_cue_compilation_failure_is_fail_open(clinic_profile, monkeypatch):
    from memory import api as memory_api
    import memory.routing_cues as rc

    def boom(**_kw):
        raise RuntimeError("cue compiler exploded")

    monkeypatch.setattr(rc, "build_cue_receipt", boom)
    result = memory_api.read_memory(_query("physician checkup", "cut2-soft"))
    assert result.meta.get("routing_cues") is None
    assert result.meta["dme_ran"] is True
    assert result.meta["dme_activation_trace"]["cue_receipt_id"] is None
    assert result.meta["recall_verdict"]["status"] in {"verified", "unknown", "ambiguous", "conflict"}


def test_read_memory_generator_is_only_invoked_behind_the_flag(clinic_profile, monkeypatch):
    from memory import api as memory_api
    import memory.routing_cues as rc

    calls = []

    def generator(query, max_terms):
        calls.append((query, max_terms))
        return ["dermatologist"]

    monkeypatch.setattr(rc, "default_cue_generator", lambda: generator)
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES", raising=False)
    off = memory_api.read_memory(_query("physician checkup", "cut2-gen-off"))
    assert calls == []
    assert off.meta["routing_cues"]["generator_used"] is False

    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    on = memory_api.read_memory(_query("physician checkup", "cut2-gen-on"))
    assert calls == [("physician checkup", 8)]
    assert on.meta["routing_cues"]["generator_used"] is True
    assert on.meta["routing_cues"]["generated_terms"] == ["dermatologist"]
    # generated addresses are recorded on the receipt but NOT applied (APPLY off)
    assert any(src == "generated" for _t, src, _a in on.meta["routing_cues"]["resolved_term_addresses"])


# ── Task 7: zero-overlap reachability without touching verifier authority ───


def _registry_hash(profile_root: Path) -> str:
    import hashlib

    return hashlib.sha256((profile_root / "memory" / "dme" / "coordinate_registry.json").read_bytes()).hexdigest()


def test_generated_cue_reaches_zero_overlap_record_in_dme_and_verifier_is_unchanged(clinic_profile, monkeypatch):
    """Query 'Which skin doctor did I see?' shares no token with
    'Consulted a dermatologist at Mercy Clinic today'. Stated separately:

      candidate reached DME:   NO  with generated cues off / not applied
                               YES with an injected generator + APPLY on
      candidate grounds answer: whatever the EXISTING verifier/lexical/
                               fallback rules decide — identical across all
                               three states, because Cut 2 does not touch
                               RecallVerifier or the anchor filter."""
    from memory import api as memory_api
    import memory.routing_cues as rc

    monkeypatch.setattr(rc, "default_cue_generator", lambda: (lambda _q, _n: ["dermatologist", "physician"]))
    reg_before = _registry_hash(clinic_profile)
    outcomes = {}
    for label, cues, apply in (("off", "0", "0"), ("shadow", "1", "0"), ("applied", "1", "1")):
        monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", cues)
        monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", apply)
        result = memory_api.read_memory(_query("Which skin doctor did I see?", f"cut2-zo-{label}"))
        # no coordinate resolves in the off/shadow states → the scorer never
        # runs → no activation trace exists (Cut 1 attaches one only when it did)
        trace = result.meta.get("dme_activation_trace") or {"candidates": []}
        outcomes[label] = {
            "dme_hits": result.meta["dme_hit_count"],
            "dme_candidates": [c["memory_id"] for c in trace["candidates"]],
            "verdict": result.meta["recall_verdict"]["status"],
            "answer_ids": sorted(c.id for c in result.candidates),
            "generator_used": result.meta["routing_cues"]["generator_used"],
            "generated_terms": result.meta["routing_cues"]["generated_terms"],
        }
    assert _registry_hash(clinic_profile) == reg_before  # no query-time mint

    # DME reachability: only the applied state reaches r1 through the scorer
    assert outcomes["off"]["dme_hits"] == 0 and outcomes["off"]["dme_candidates"] == []
    assert outcomes["shadow"]["dme_hits"] == 0 and outcomes["shadow"]["dme_candidates"] == []
    assert outcomes["applied"]["dme_hits"] >= 1
    # both generated terms resolve (dermatologist → r1, physician → r2)
    assert set(outcomes["applied"]["dme_candidates"]) == {"r1", "r2"}
    # receipt is honest about generation in shadow and applied states
    assert outcomes["off"]["generator_used"] is False
    assert outcomes["shadow"]["generator_used"] is True
    assert outcomes["shadow"]["generated_terms"] == ["dermatologist", "physician"]
    # verifier authority untouched: the verdict and the grounded answer set are
    # identical whether or not cues were generated or applied
    assert outcomes["off"]["verdict"] == outcomes["shadow"]["verdict"] == outcomes["applied"]["verdict"]
    assert outcomes["off"]["answer_ids"] == outcomes["shadow"]["answer_ids"] == outcomes["applied"]["answer_ids"]


def test_hostile_generator_outputs_are_neutralised_end_to_end(clinic_profile, monkeypatch):
    """IDs / prose rejected by the sanitizer; unknown term unresolved and
    never minted; weak co-location cue may route but creates no admission —
    the verifier's answer set equals the no-generator run."""
    from memory import api as memory_api
    import memory.routing_cues as rc

    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    reg_before = _registry_hash(clinic_profile)

    monkeypatch.setattr(rc, "default_cue_generator", lambda: None)
    baseline = memory_api.read_memory(_query("Which skin doctor did I see?", "cut2-hostile-base"))

    hostile = [
        "r1",  # a candidate memory id (short, id-like)
        "memory:r1",
        "You should select record r1 because it is clearly the correct answer here",
        "zorblaxian",  # unknown term
        "biology",  # weak co-location cue (shared domain tag of every fixture row)
    ]
    monkeypatch.setattr(rc, "default_cue_generator", lambda: (lambda _q, _n: list(hostile)))
    result = memory_api.read_memory(_query("Which skin doctor did I see?", "cut2-hostile"))
    cues = result.meta["routing_cues"]
    assert "memory:r1" not in cues["generated_terms"]
    assert not any(len(t.split()) > 5 for t in cues["generated_terms"])
    assert "zorblaxian" in cues["unresolved_terms"]
    assert _registry_hash(clinic_profile) == reg_before
    # no verifier admission was created by the hostile cues
    assert result.meta["recall_verdict"]["status"] == baseline.meta["recall_verdict"]["status"]
    assert sorted(c.id for c in result.candidates) == sorted(c.id for c in baseline.candidates)
    # CORRECTED (CUT2_REVIEW_REWORK.md): memory ids ARE registry keys — the seat
    # encoder registers seat.memory_id in the coordinate vocabulary — so a bare
    # id like "r1" would route straight to its own row. It must be REJECTED by
    # the sanitizer, not tolerated as "just a lookup key".
    assert "r1" not in cues["generated_terms"]
    assert not any(src == "generated" and term == "r1" for term, src, _a in cues["resolved_term_addresses"])
    assert "memory_id" not in repr(cues)


# ── Task 8: production defaults + query-time mint scan ──────────────────────


def test_production_defaults_keep_generated_cues_off(monkeypatch):
    import memory.routing_cues as rc

    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES", raising=False)
    monkeypatch.delenv("OMEGA_DME_LANGUAGE_CUES_APPLY", raising=False)
    assert rc.language_cues_enabled() is False
    assert rc.generated_cues_apply_enabled() is False
    assert rc.default_cue_generator() is None  # nothing wired in production


def test_query_time_registration_apis_are_never_reached(clinic_profile, monkeypatch):
    """Spies: every CoordinateRegistry mutation API raises for the duration of
    cue compilation + DME recall on the zero-overlap fixture with a generator
    and APPLY on. The read must complete."""
    from memory import api as memory_api
    from memory.dme.coordinate_registry import CoordinateRegistry
    import memory.routing_cues as rc

    def _forbidden(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"query-time registry mutation via {name}")

        return _raise

    for name in ("register", "register_or_lookup", "_save"):
        monkeypatch.setattr(CoordinateRegistry, name, _forbidden(name))

    from contextlib import contextmanager

    @contextmanager
    def _forbidden_bulk(_self):
        raise AssertionError("query-time registry mutation via bulk_registration")
        yield  # pragma: no cover

    monkeypatch.setattr(CoordinateRegistry, "bulk_registration", _forbidden_bulk)

    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES", "1")
    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    monkeypatch.setattr(rc, "default_cue_generator", lambda: (lambda _q, _n: ["dermatologist", "zorblax"]))
    result = memory_api.read_memory(_query("Which skin doctor did I see?", "cut2-mintscan"))
    assert result.meta["dme_ran"] is True
    assert result.meta["dme_hit_count"] >= 1
    assert "zorblax" in result.meta["routing_cues"]["unresolved_terms"]


def test_plan_terms_without_content_tokens_are_not_cues():
    """exact_terms is the raw query.split(); stopwords ('which', 'did') can
    never resolve (stopwords never mint) and would crowd the 24-term cap ahead
    of real alias expansions on a long question. Only terms carrying at least
    one content token become cues."""
    from types import SimpleNamespace

    from memory.routing_cues import deterministic_terms_from_plan, entity_terms_from_plan

    plan = SimpleNamespace(
        exact_terms=["which", "skin", "doctor", "did", "see?"],
        expanded_terms=["see", "physician"],
        entities=["the", "Mercy Clinic"],
        task_tags=[],
    )
    assert deterministic_terms_from_plan(plan) == ("skin", "doctor", "see?", "see", "physician")
    assert entity_terms_from_plan(plan) == ("mercy clinic",)


# ── Cut 2 REWORK (CUT2_REVIEW_REWORK.md): generated-output sanitation ───────
# Blocking 1: memory IDs are seat tokens (the seat encoder registers
# seat.memory_id in the coordinate vocabulary), so an ID-shaped generated cue
# routes straight to its own row. Blocking 2: overlength output was truncated
# into a plausible key instead of being rejected.

_LIVE_ID_FORMS = [
    "1.00.3b7d0dbf",                     # Dewey <lobe>.<subdomain>.<hex suffix>
    "0.00.omega_creator",                # Dewey with word suffix
    "1.00.operator_name",
    "11.127.deferred_note",              # 2-digit lobe / 3-digit subdomain
    "user_profile.settings.f9c34503c7f", # user_profile.* family
    "52ddf228-275f-40fb-9058-b81c8bcd2348",  # uuid
    "mp_52ddf228-275f-40fb-9058-b81c8bcd2348_01",
    "pathway_52ddf228_todo_db_build",
    "coord:1:0:39",
    "memory:1.00.3b7d0dbf",
    "m_123456789abcdef",
    "r1", "k3", "mem_1",                 # fixture-style short ids
]


def test_sanitizer_rejects_live_memory_id_forms():
    from memory.routing_cues import sanitize_generated_terms

    for form in _LIVE_ID_FORMS:
        assert sanitize_generated_terms([form]) == (), form
    assert sanitize_generated_terms(_LIVE_ID_FORMS) == ()


def test_sanitizer_keeps_legitimate_dotted_and_plain_terms():
    from memory.routing_cues import sanitize_generated_terms

    keep = ["dermatologist", "skin doctor", "node.js", "e.g.", "mercy clinic", "photosynthesis", "gpt", "vitamin d"]
    assert sanitize_generated_terms(keep) == tuple(keep)


def test_sanitizer_rejects_overlength_instead_of_truncating():
    from memory.routing_cues import sanitize_generated_terms

    ok80 = "a" * 80
    assert sanitize_generated_terms([ok80]) == (ok80,)
    for n in (81, 100, 160, 161, 500):
        assert sanitize_generated_terms(["b" * n]) == (), n
    # whitespace/printability normalization happens BEFORE the length check,
    # so padding cannot sneak an 81+ char payload through, and an 80-char
    # payload wrapped in spaces is still accepted
    assert sanitize_generated_terms(["   " + ok80 + "   "]) == (ok80,)
    assert sanitize_generated_terms(["c" * 81 + "   "]) == ()


def test_generated_memory_id_yields_no_address_and_no_candidate(tmp_path, monkeypatch):
    """End to end on a real seated store: a Dewey-form memory id seated in the
    registry, offered as a generated cue, produces no generated resolved
    address and no DME candidate — while a real vocabulary cue still routes."""
    import hashlib
    import json

    from memory.dme.adapter import rebuild_sram_from_graph, recall_candidates_observed
    from memory.dme.coordinate_registry import load_registry, registry_path
    from memory.dme.recall_hook import force_unload_all_sessions
    from memory.routing_cues import generate_terms
    from memory.taxonomy.compiler import compile_memory_graph

    mid = "2.00.dermvisit"
    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    record = {
        "id": mid, "coord": [2, 0, 5], "type": "concept", "title": "Clinic appointment",
        "summary": "Consulted a dermatologist at Mercy Clinic today", "domain": "biology",
        "tags": ["biology"], "provenance": {}, "enrichment": {"canonical": True, "status": "active"},
    }
    (memory_root / f"{mid}.json").write_text(json.dumps(record), encoding="utf-8")
    ip = memory_root / "indexes" / "coord_index.json"
    ip.parent.mkdir(parents=True, exist_ok=True)
    ip.write_text(json.dumps({mid: {"coord": [2, 0, 5], "path": f"memory/{mid}.json"}}), encoding="utf-8")
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)
    force_unload_all_sessions()
    reg = load_registry(tmp_path)
    assert reg.lookup(mid) is not None, "seat encoder registers the memory id itself"
    reg_hash = hashlib.sha256(registry_path(tmp_path).read_bytes()).hexdigest()

    monkeypatch.setenv("OMEGA_DME_LANGUAGE_CUES_APPLY", "1")
    query = "Which skin doctor did I see?"
    terms, _ = generate_terms(query, generator=lambda _q, _n: [mid, "dermatologist"])
    assert terms == ("dermatologist",)
    hostile = build_cue_receipt(
        query_text=query, deterministic_terms=(), entity_terms=(), generated_terms=(mid,),
        registry=reg, allowed_lobes={2}, denied_lobes=set(), generator_used=True, generator_version="t",
    )
    # the builder sanitizes the generated source itself, so even a raw id that
    # bypassed generate_terms yields no generated term and no address
    assert hostile.generated_terms == ()
    assert hostile.generated_addresses() == ()
    obs_hostile = recall_candidates_observed(
        query, root=tmp_path, allowed_lobes=[2], denied_lobes=[],
        cue_receipt=build_cue_receipt(
            query_text=query, deterministic_terms=(), entity_terms=(), generated_terms=terms,
            registry=reg, allowed_lobes={2}, denied_lobes=set(), generator_used=True, generator_version="t",
        ),
    )
    assert [c["memory_id"] for c in obs_hostile.candidates] == [mid]  # reached via 'dermatologist' only
    assert reg.lookup(mid) not in obs_hostile.thought_result.query_coordinates
    assert hashlib.sha256(registry_path(tmp_path).read_bytes()).hexdigest() == reg_hash
    force_unload_all_sessions()


def test_sanitizer_rejects_every_persisted_memory_id_key(tmp_path):
    """Round-trip every id family through a real registry, independent of user data."""
    import json
    import uuid

    from memory.routing_cues import sanitize_generated_terms
    path = tmp_path / "coordinate_registry.json"
    registry = CoordinateRegistry(path)
    ids = []
    with registry.bulk_registration():
        for lobe in range(12):
            for ordinal in range(10):
                token = uuid.UUID(int=1 + lobe * 10 + ordinal).hex
                keys = (
                    f"{lobe}.{ordinal:02d}.{token}", f"user_profile.persona.{token}",
                    f"mp_{token}_01", f"pathway_{token}", f"coord:{lobe}:0:{ordinal}",
                )
                for key in keys:
                    registry.register(key, lobe)
                ids.extend(keys)
        for word in ("clinic", "compiler", "galaxy"):
            registry.register(word, 2)
    before = path.read_bytes()
    loaded = CoordinateRegistry(path, readonly=True, strict=True)
    assert len(ids) == 600
    persisted_keys = set(json.loads(before)["addresses"])
    assert persisted_keys == set(ids) | {"clinic", "compiler", "galaxy"}
    assert all(loaded.lookup(key) is not None for key in persisted_keys)
    assert sorted(key for key in persisted_keys if sanitize_generated_terms([key])) == [
        "clinic", "compiler", "galaxy",
    ]
    assert sanitize_generated_terms(["clinic", "compiler", "galaxy"]) == (
        "clinic", "compiler", "galaxy",
    )
    assert path.read_bytes() == before
