"""Walker client wrapper — deterministic query interface (Phase 6)."""

import logging

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.memory.engine.walker_loader")

MEMORY_ROOT = OMEGA_ROOT / "memory"
INDEX_PATH = MEMORY_ROOT / "indexes" / "coord_index.json"


class WalkerClient:
    def __call__(self, query, coord=None, radius=2):
        from memory.engine.walker import (
            load_coord_index,
            load_meta_by_path,
            list_ids_in_neighborhood,
            list_ids_in_lobe,
            composite_score,
            assemble_context,
            COORD_INDEX,
            _tokenize,
            _grammar_seed,
        )

        load_coord_index()
        if coord and len(coord) >= 2:
            X, Y = int(coord[0]), int(coord[1])
            candidate_ids = list_ids_in_neighborhood(X, Y, radius)
        else:
            X, Y = _grammar_seed(query)
            if Y is not None:
                candidate_ids = list_ids_in_neighborhood(X, int(Y), radius)
            else:
                candidate_ids = list_ids_in_lobe(X)
        tokens = _tokenize(query)
        scored = []
        seed = (X, Y if Y is not None else None)
        for idstr in candidate_ids:
            entry = COORD_INDEX.get(idstr)
            if not entry:
                continue
            path = entry.get("path")
            meta = load_meta_by_path(path) if path else {}
            if not meta:
                continue
            S = composite_score(meta, seed, tokens)
            scored.append((S, meta))
        scored.sort(key=lambda x: x[0], reverse=True)
        result = assemble_context([m for _, m in scored[:20]])
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "walker_client_query",
                summary=f"hits={len(result)}",
                candidate_count=len(candidate_ids),
                result_count=len(result),
                coord=list(coord) if coord else None,
                ok=True,
            )
        except Exception:
            pass
        return result


def load_walker():
    client = WalkerClient()
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "load_walker",
            summary="ok",
            degraded=False,
            ok=True,
        )
    except Exception:
        pass
    return client
