"""Labeled routing corpus for the Phase 7 harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Tuple

from memory.routing.training_data import ROUTING_TRAINING_DATA


@dataclass(frozen=True)
class RoutingCase:
    """One labeled free-text routing example."""

    text: str
    expected_lobe: int
    expected_y: int
    label_id: str
    source: str


def _parse_label(label_id: str) -> Tuple[int, int]:
    parts = str(label_id).strip().split(".")
    if len(parts) < 2:
        raise ValueError(f"invalid label_id: {label_id!r}")
    return int(parts[0]), int(parts[1])


def iter_training_data_cases() -> Iterator[RoutingCase]:
    """Yield labeled examples from the deterministic routing corpus."""
    for label_id, examples in ROUTING_TRAINING_DATA.items():
        lobe, y = _parse_label(label_id)
        for text in examples:
            yield RoutingCase(
                text=str(text),
                expected_lobe=lobe,
                expected_y=y,
                label_id=label_id,
                source="training_data",
            )


# Adversarial / paraphrase smoke — tier-3 hold and grammar edge cases.
ADVERSARIAL_CASES: List[RoutingCase] = [
    RoutingCase("blizzard penguin teapot", 3, 127, "3.127", "adversarial"),
    RoutingCase("blizzard penguin teapot", 4, 127, "4.127", "adversarial"),
    RoutingCase("blizzard penguin teapot", 5, 127, "5.127", "adversarial"),
    RoutingCase("I am Noah", 0, 0, "0.00", "symmetry"),
    RoutingCase("Who am I to you?", 0, 6, "0.06", "symmetry"),
    RoutingCase("Photosynthesis converts light to sugar", 2, 0, "2.00", "symmetry"),
    RoutingCase("async def fetch_data(): pass", 8, 1, "8.01", "symmetry"),
    RoutingCase("Goal: ship deterministic routing", 3, 0, "3.00", "symmetry"),
    RoutingCase("Yesterday I felt frustrated debugging gates", 4, 7, "4.07", "symmetry"),
    RoutingCase("the web fetch tool schema", 5, 0, "5.00", "symmetry"),
    RoutingCase("meta synthesis returned idle", 6, 0, "6.00", "symmetry"),
    RoutingCase("recognize this face from the camera", 7, 1, "7.01", "symmetry"),
    RoutingCase("session started at noon", 9, 0, "9.00", "symmetry"),
    RoutingCase("how do we deploy the artifact", 11, 0, "11.00", "symmetry"),
]


def iter_all_cases(*, include_training: bool = True) -> Iterator[RoutingCase]:
    if include_training:
        yield from iter_training_data_cases()
    yield from ADVERSARIAL_CASES
