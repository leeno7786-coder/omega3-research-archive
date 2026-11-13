"""Global B phase — existence-based grounding_basis (no fabrication)."""

from __future__ import annotations

from typing import Any, List, Mapping, Tuple

from global_loop.assemble import auto_inner


def run(
    auto_outcome: Mapping[str, Any],
    meta_reflection: Mapping[str, Any],
) -> Tuple[bool, List[str]]:
    from pressure_loop.policy import (
        global_b_ground_auto_output_paths,
        global_b_ground_meta_disposition_path,
    )

    outputs = dict(auto_inner(auto_outcome).get("outputs") or {})
    basis: List[str] = []
    for output_key, path in global_b_ground_auto_output_paths().items():
        if outputs.get(output_key) is not None:
            basis.append(path)

    disposition = (meta_reflection.get("execution") or {}).get("disposition")
    meta_path = global_b_ground_meta_disposition_path()
    if disposition:
        basis.append(meta_path)

    grounded = bool(basis) and disposition is not None
    return grounded, basis
