"""Subprocess helper for D-13-27/40 hash-seed independence: run under a fresh interpreter
with PYTHONHASHSEED set by the parent. Usage:

    python _hashseed_child.py <profile_dir> <mode> <out_json>

mode = generation | drain | legacy. Copies <profile_dir> to a private scratch, builds, and writes
artifact hashes (+ the reference SRAM hash) to <out_json>.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


def _system_temp_dir() -> Path:
    """Avoid pytest's repo-adjacent temp override for child scratch trees."""
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(local_app_data) / "Temp"
            if candidate.is_dir():
                return candidate
    return Path(tempfile.gettempdir())


def main() -> None:
    src, mode, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    scratch = Path(tempfile.mkdtemp(prefix="hashseed-", dir=str(_system_temp_dir())))
    profile = scratch / "profile"
    shutil.copytree(src, profile)
    from tests.memory.projection import _fixtures as F
    from tests.memory.projection.dme_reference import reference_sram_bytes

    F.redirect_timekeeper(scratch)
    result: dict = {"mode": mode}
    if mode == "generation":
        from memory.projection import generation as G
        from memory.projection import build as B

        with G.projection_publication_authority(profile) as auth:
            cand = B.build_generation(profile, reason="hashseed", authority=auth)
            assert isinstance(cand, G.Candidate), cand
            d = cand.candidate_dir
            result["generation_id"] = cand.manifest.generation_id
            result["files"] = {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}
            result["reference_sram_sha256"] = hashlib.sha256(reference_sram_bytes(d)).hexdigest()
            result["registry"] = json.loads((d / "coordinate_registry.json").read_text())
    elif mode == "drain":                                          # Program 0 (Part B): the outbox consumer's build
        from memory.projection import generation as G
        from memory.projection import projector as P

        rep = P.drain_once(profile, reason="hashseed-drain")
        assert rep.status in ("published_new", "reused_existing"), rep.status
        d = G.projection_root(profile) / f"gen-{rep.generation_id}"
        result["generation_id"] = rep.generation_id
        result["status"] = rep.status
        result["files"] = {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file() and p.name != "generation.json"}
        result["reference_sram_sha256"] = hashlib.sha256(reference_sram_bytes(d)).hexdigest()
    else:
        from memory.dme.adapter import rebuild_sram_from_graph
        from memory.taxonomy.compiler import compile_memory_graph

        compile_memory_graph(root=profile)
        rebuild_sram_from_graph(root=profile)
        dme = profile / "memory" / "dme"
        result["sram_sha256"] = hashlib.sha256((dme / "sram.bin").read_bytes()).hexdigest()
        result["registry"] = json.loads((dme / "coordinate_registry.json").read_text())
    out.write_text(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
