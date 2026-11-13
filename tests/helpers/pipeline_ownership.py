"""Test-only physical ownership helpers for direct cognition cascade probes."""

from __future__ import annotations

from dataclasses import replace

from pressure_loop.cognition_queue import deserialize_input, work_kind_for_contributor


def bind_ignition(ignition, *, pipeline_id: int = 0, arrival_seq: int = 1):
    """Give a directly-invoked ignition the identity dispatch normally supplies."""
    original = ignition.dominant_contributor
    metadata = dict(original.metadata)
    metadata.setdefault("arrival_seq", arrival_seq)
    metadata.setdefault("cycle_id", str(ignition.episode_id))
    work_kind = work_kind_for_contributor(original)
    owner = deserialize_input(
        {
            "source": original.source,
            "type": original.type,
            "weight": original.weight,
            "metadata": metadata,
            "queue_id": f"test-{ignition.episode_id}-{arrival_seq}",
            "work_kind": work_kind,
        }
    )
    contributors = tuple(
        owner if contributor is original else contributor
        for contributor in ignition.contributors
    )
    return replace(
        ignition,
        dominant_contributor=owner,
        contributors=contributors,
        pipeline_id=pipeline_id,
        work_kind=work_kind,
    )
