"""Phase 7 — routing harness, metrics, and edge-case catalog."""

from memory.routing.corpus import ADVERSARIAL_CASES, RoutingCase, iter_all_cases
from memory.routing.edge_case_catalog import append_entry, load_entries, unsorted_rate
from memory.routing.harness import CaseResult, RoutingMetrics, evaluate_case, run_harness

__all__ = [
    "ADVERSARIAL_CASES",
    "RoutingCase",
    "CaseResult",
    "RoutingMetrics",
    "append_entry",
    "evaluate_case",
    "iter_all_cases",
    "load_entries",
    "run_harness",
    "unsorted_rate",
]
