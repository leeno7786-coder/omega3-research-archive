"""Stress tests — deterministic memory routing across all gates and API paths."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from auto_loop.mril_tools.router import route_memory_tool
from auto_loop.mril_tools.subdomain_helpers import subdomain_for
from memory.dewey_map import UNSORTED_SUBDOMAIN_Y
from memory.gates.lobe_gates import (
    CodingGate,
    DevGate,
    IdentityGate,
    JournalGate,
    KnowledgeGate,
    PersonasGate,
    ReasoningGate,
    SituationalGate,
    TemporalGate,
    ToolsGate,
    VisionGate,
    WorkflowsGate,
)
from memory.routing.code_workflow_disambiguation import suggest_code_workflow_lobe
from memory.routing.corpus import ADVERSARIAL_CASES
from memory.routing.harness import run_harness

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

LOBE_GATES = {
    0: IdentityGate,
    1: PersonasGate,
    2: KnowledgeGate,
    3: ReasoningGate,
    4: SituationalGate,
    5: ToolsGate,
    6: JournalGate,
    7: VisionGate,
    8: CodingGate,
    9: TemporalGate,
    10: WorkflowsGate,
    11: DevGate,
}


@pytest.mark.parametrize(
    "gate_cls",
    list(LOBE_GATES.values()),
    ids=[g.__name__ for g in LOBE_GATES.values()],
)
def test_all_production_gates_single_lobe_contract(gate_cls):
    assert len(gate_cls.READABLE_LOBES) == 1
    assert len(gate_cls.WRITABLE_LOBES) == 1
    assert gate_cls.READABLE_LOBES[0] == gate_cls.WRITABLE_LOBES[0]


@pytest.mark.parametrize(
    "case",
    [c for c in ADVERSARIAL_CASES if c.source == "symmetry"],
    ids=lambda c: f"lobe{c.expected_lobe}_y{c.expected_y}",
)
def test_symmetry_smoke_ingest_matches_expected_coord(case):
    from memory.gates import base as gate_base

    gate_cls = LOBE_GATES[case.expected_lobe]
    captured: dict = {}

    def _capture_atomic_write(meta, update_index=True, **kwargs):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
    ):
        result = gate_cls().ingest(
            case.text,
            metadata={"type": "stress", "title": f"stress/{case.expected_lobe}"},
        )

    assert result is not None
    coord = captured["meta"]["coord"]
    assert coord[0] == case.expected_lobe
    grammar_y = subdomain_for(case.text, case.expected_lobe)
    if grammar_y is not None:
        assert coord[1] == grammar_y
    else:
        assert coord[1] == case.expected_y


def test_tools_gate_registry_scope_dev_overrides_grammar():
    from memory.gates import base as gate_base

    captured: dict = {}

    def _capture_atomic_write(meta, update_index=True, **kwargs):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
    ):
        result = ToolsGate().ingest(
            "web fetch tool schema catalog entry",
            metadata={
                "type": "skill",
                "title": "stress/dev-tool",
                "registry_scope": "dev",
            },
        )

    assert result is not None
    assert captured["meta"]["coord"] == [5, 1, 0]


def test_tools_gate_registry_scope_cognitive():
    from memory.gates import base as gate_base

    captured: dict = {}

    def _capture_atomic_write(meta, update_index=True, **kwargs):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
    ):
        result = ToolsGate().ingest(
            "dev pipeline coding project tool",
            metadata={
                "type": "skill",
                "title": "stress/cog-tool",
                "registry_scope": "cognitive",
            },
        )

    assert result is not None
    assert captured["meta"]["coord"] == [5, 0, 0]


def test_write_memory_preserves_explicit_coord(monkeypatch, tmp_path):
    """API write path must not grammar-reroute envelope coords."""
    captured: dict = {}

    def _capture_atomic_write(meta, update_index=True, **kwargs):
        captured["coord"] = list(meta["coord"])
        path = tmp_path / f"{meta['id']}.json"
        path.write_text("{}", encoding="utf-8")
        return str(path)

    monkeypatch.setattr(
        "memory.promotion.atomic_write",
        _capture_atomic_write,
    )

    from memory.api import CanonicalEnvelope, write_memory

    explicit = [4, 7, 0]
    env = CanonicalEnvelope(
        cycle_id="stress-explicit-coord",
        coord=explicit,
        type="situational",
        payload={
            "title": "stress/explicit",
            "summary": "identity text that would route elsewhere",
            "text": "I am Noah who am I constitution",
        },
        source="test:stress",
    )
    write_memory(env, propose=False, bypass_guardian=True, skip_embed=True)
    assert captured["coord"] == explicit


@pytest.mark.parametrize(
    "text,expected_lobe",
    [
        ("async def fetch_data(): pass", 8),
        ("class Interface trait struct compiler", 8),
        ("I refactored step by step then tested the workflow pipeline", 10),
        ("how to walk through the debugging procedure finally", 10),
    ],
)
def test_code_workflow_disambiguation_markers(text, expected_lobe):
    assert suggest_code_workflow_lobe(text) == expected_lobe


def test_route_memory_tool_disambiguates_code_vs_workflow():
    code_route = route_memory_tool("async def coroutine library framework decorator")
    assert code_route["tool"] == "remember_code"
    assert code_route["lobe"] == 8

    workflow_route = route_memory_tool(
        "I refactored step by step then tested the workflow pipeline"
    )
    assert workflow_route["tool"] == "remember_workflow"
    assert workflow_route["lobe"] == 10


@pytest.mark.parametrize(
    "case",
    [c for c in ADVERSARIAL_CASES if c.source == "adversarial"],
    ids=lambda c: f"lobe{c.expected_lobe}",
)
def test_adversarial_text_lands_in_unsorted(case):
    from memory.gates import base as gate_base

    gate_cls = LOBE_GATES[case.expected_lobe]
    captured: dict = {}

    def _capture_atomic_write(meta, update_index=True, **kwargs):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
    ):
        result = gate_cls().ingest(
            case.text,
            metadata={"type": "stress", "title": f"stress/unsorted/{case.expected_lobe}"},
        )

    assert result is not None
    assert captured["meta"]["coord"] == [case.expected_lobe, UNSORTED_SUBDOMAIN_Y, 0]
    assert captured["meta"]["provenance"]["undecided"] is True


def test_full_corpus_symmetry_no_failures():
    metrics = run_harness(collect_failures=True)
    symmetry_failures = [f for f in metrics.failures if not f.symmetry_ok]
    assert metrics.symmetry_rate >= 0.99
    assert not symmetry_failures, (
        f"{len(symmetry_failures)} symmetry failures: "
        f"{[(f.case.text[:40], f.write_coord) for f in symmetry_failures[:5]]}"
    )


def test_concurrent_gate_ingest_isolated(monkeypatch):
    """Parallel ingests on the same gate must not cross-contaminate coords."""
    from memory.gates import base as gate_base

    captures: list[list[int]] = []
    lock = threading.Lock()

    def _capture_atomic_write(meta, update_index=True, **kwargs):
        with lock:
            captures.append(list(meta["coord"]))
        return f"<test>/{meta['id']}.json"

    texts = [
        "blizzard penguin teapot alpha",
        "what are the constraints beta",
        "Goal: ship deterministic routing gamma",
        "blizzard penguin teapot delta",
    ]

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
    ):

        def _ingest_one(text: str):
            ReasoningGate().ingest(
                text,
                metadata={"type": "stress", "title": "stress/concurrent"},
            )

        threads = [threading.Thread(target=_ingest_one, args=(t,)) for t in texts]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)

    assert len(captures) == len(texts)
    ys = sorted(c[1] for c in captures)
    assert ys.count(UNSORTED_SUBDOMAIN_Y) == 2
    assert ys.count(0) == 1
    assert ys.count(2) == 1
    assert all(c[0] == 3 for c in captures)


def test_how_to_regex_routes_procedure_lobe_5_not_workflow():
    """Explicit how-to prefix → lobe 5 tools procedures (MRIL contract)."""
    route = route_memory_tool("how to use the web fetch tool step by step")
    assert route["tool"] == "remember_procedure"
    assert route["lobe"] == 5


def test_dynamic_lobe_skips_unsorted_hold_on_gate_ingest():
    from memory.gates import base as gate_base

    captured: dict = {}

    def _capture_atomic_write(meta, update_index=True, **kwargs):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset({2, 8})),
        patch(
            "memory.subdomain_allocator.get_or_create_subdomain",
            return_value=(3, "stress_dynamic"),
        ),
    ):
        result = KnowledgeGate().ingest(
            "blizzard penguin teapot",
            metadata={"type": "concept", "title": "stress/dynamic"},
        )

    assert result is not None
    assert captured["meta"]["coord"][1] != UNSORTED_SUBDOMAIN_Y
    assert captured["meta"]["provenance"].get("undecided") is not True


def test_harness_dynamic_lobe_does_not_count_grammar_miss_as_undecided():
    from memory.routing.corpus import RoutingCase
    from memory.routing.harness import evaluate_case

    case = RoutingCase(
        text="blizzard penguin teapot",
        expected_lobe=2,
        expected_y=0,
        label_id="2.00",
        source="stress",
    )
    result = evaluate_case(case, dynamic_y_lobes=frozenset({2}))
    assert result.undecided is False
    assert result.write_coord[1] != UNSORTED_SUBDOMAIN_Y
