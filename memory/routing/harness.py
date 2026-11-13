"""Phase 7 — deterministic routing test harness and metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, List, Optional

from auto_loop.mril_tools.router import route_memory_tool
from auto_loop.mril_tools.subdomain_helpers import subdomain_for
from memory.dewey_map import UNSORTED_SUBDOMAIN_Y
from memory.gates.subdomain_override import apply_subdomain_override
from memory.gates.undecided_hold import route_undecided_subdomain
from memory.routing.corpus import RoutingCase, iter_all_cases


@dataclass
class CaseResult:
    case: RoutingCase
    grammar_y: Optional[int]
    decided: bool
    undecided: bool
    write_coord: List[int]
    recall_tool_lobe: int
    recall_tool_subdomain: Optional[int]
    subdomain_correct: bool
    symmetry_ok: bool
    recall_lobe_match: bool


@dataclass
class RoutingMetrics:
    total: int = 0
    decided_count: int = 0
    undecided_count: int = 0
    subdomain_correct_count: int = 0
    symmetry_ok_count: int = 0
    recall_lobe_match_count: int = 0
    failures: List[CaseResult] = field(default_factory=list)
    edge_cases: List[CaseResult] = field(default_factory=list)

    @property
    def decidability_rate(self) -> float:
        return self.decided_count / self.total if self.total else 0.0

    @property
    def subdomain_accuracy(self) -> float:
        return self.subdomain_correct_count / self.decided_count if self.decided_count else 0.0

    @property
    def symmetry_rate(self) -> float:
        return self.symmetry_ok_count / self.total if self.total else 0.0

    @property
    def recall_lobe_accuracy(self) -> float:
        return self.recall_lobe_match_count / self.total if self.total else 0.0

    @property
    def unsorted_rate(self) -> float:
        return self.undecided_count / self.total if self.total else 0.0


def simulate_write_coord(
    text: str,
    lobe: int,
    *,
    dynamic_y_lobes: FrozenSet[int],
) -> tuple[List[int], bool]:
    """Mirror single-lobe gate ingest Y resolution (no allocator)."""
    coord = [lobe, 0, 0]
    label_id = f"{lobe}.00"
    coord, label_id = apply_subdomain_override(text, coord, label_id)
    coord, label_id, undecided = route_undecided_subdomain(
        text,
        lobe,
        coord,
        label_id,
        dynamic_y_lobes=dynamic_y_lobes,
    )
    if lobe in dynamic_y_lobes:
        # Dynamic lobes skip _unsorted; allocator not simulated in harness.
        undecided = False
    return coord, undecided


def evaluate_case(
    case: RoutingCase,
    *,
    dynamic_y_lobes: FrozenSet[int] = frozenset(),
) -> CaseResult:
    lobe = case.expected_lobe
    grammar_y = subdomain_for(case.text, lobe)
    write_coord, undecided = simulate_write_coord(
        case.text,
        lobe,
        dynamic_y_lobes=dynamic_y_lobes,
    )

    recall = route_memory_tool(case.text)
    recall_lobe = int((recall.get("allowed_lobes") or [2])[0])
    recall_sub = recall.get("subdomain")

    if undecided:
        symmetry_ok = grammar_y is None and int(write_coord[1]) == UNSORTED_SUBDOMAIN_Y
    elif grammar_y is not None:
        symmetry_ok = int(write_coord[1]) == int(grammar_y)
    else:
        symmetry_ok = int(write_coord[1]) == 0 and grammar_y is None

    if grammar_y is not None:
        subdomain_correct = int(grammar_y) == int(case.expected_y)
    elif undecided:
        subdomain_correct = int(write_coord[1]) == UNSORTED_SUBDOMAIN_Y
    elif int(write_coord[1]) == 0:
        subdomain_correct = int(case.expected_y) == 0
    else:
        subdomain_correct = False

    if lobe in dynamic_y_lobes:
        decided = grammar_y is not None
    else:
        decided = grammar_y is not None or undecided

    recall_lobe_match = recall_lobe == lobe

    return CaseResult(
        case=case,
        grammar_y=grammar_y,
        decided=decided,
        undecided=undecided,
        write_coord=write_coord,
        recall_tool_lobe=recall_lobe,
        recall_tool_subdomain=recall_sub,
        subdomain_correct=subdomain_correct,
        symmetry_ok=symmetry_ok,
        recall_lobe_match=recall_lobe_match,
    )


def run_harness(
    cases: Optional[Iterable[RoutingCase]] = None,
    *,
    dynamic_y_lobes: FrozenSet[int] = frozenset(),
    collect_failures: bool = True,
) -> RoutingMetrics:
    metrics = RoutingMetrics()
    for case in cases or iter_all_cases():
        result = evaluate_case(case, dynamic_y_lobes=dynamic_y_lobes)
        metrics.total += 1
        if result.decided:
            metrics.decided_count += 1
        if result.undecided:
            metrics.undecided_count += 1
            metrics.edge_cases.append(result)
        if result.subdomain_correct:
            metrics.subdomain_correct_count += 1
        if result.symmetry_ok:
            metrics.symmetry_ok_count += 1
        if result.recall_lobe_match:
            metrics.recall_lobe_match_count += 1
        if collect_failures and not (
            result.symmetry_ok and (result.subdomain_correct or result.undecided)
        ):
            metrics.failures.append(result)
    return metrics
