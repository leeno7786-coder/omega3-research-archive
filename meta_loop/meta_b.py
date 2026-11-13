"""Slice 4 — Meta B compat re-exports (logic in phase sub-packages + loop)."""

from __future__ import annotations

from typing import Optional

from meta_loop import consistency as consistency_phase
from meta_loop import disposition as disposition_phase
from meta_loop import reflect as reflect_phase
from meta_loop.consistency.run import MetaBInput, meta_b_input_from_episode
from meta_loop.disposition.run import DISPOSITIONS, META_STATUSES, derive_disposition
from meta_loop.loop import run_meta_b
from meta_loop.reflect.run import MetaReflectionPack, meta_reflection_pack_to_dict

check_consistency = consistency_phase.run


def run_meta_b_front(episode) -> Optional[MetaReflectionPack]:
    """Classify Auto outcome; write episode.meta['reflection'] only."""
    mbi = meta_b_input_from_episode(episode)
    if mbi is None:
        return None
    consistency, critical = consistency_phase.run(mbi)
    disposition, status = disposition_phase.run(mbi, critical)
    reflection = reflect_phase.run(mbi, consistency, disposition, status)
    meta = dict(getattr(episode, "meta", None) or {})
    meta["reflection"] = meta_reflection_pack_to_dict(reflection)
    episode.meta = meta
    return reflection


__all__ = [
    "DISPOSITIONS",
    "META_STATUSES",
    "MetaBInput",
    "meta_b_input_from_episode",
    "check_consistency",
    "derive_disposition",
    "meta_reflection_pack_to_dict",
    "MetaReflectionPack",
    "run_meta_b_front",
    "run_meta_b",
]
