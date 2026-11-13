"""Boot-time taxonomy compilation.

Episode rebuilds are owned synchronously by DeepSleep. No background rebuild may
outlive an episode or observe a different memory root after test/runtime teardown.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.required_substrate import RequiredSubstrateError
from memory.engine.reindex import reindex_and_compile

log = logging.getLogger("omega.memory.taxonomy.compile_trigger")

def compile_on_boot(root: Path | str | None = None):
    """Synchronously rebuild every recall projection or fail boot attribution."""
    try:
        snap = reindex_and_compile(root=root)
    except Exception as exc:
        failure = RequiredSubstrateError.from_exception(
            "memory_taxonomy",
            "compile",
            exc,
        )
        if failure is exc:
            raise
        raise failure from exc
    if snap is None:
        raise RequiredSubstrateError(
            "memory_taxonomy",
            "compile",
            reason="snapshot_missing",
        )
    log.info(
        "compile-on-boot rebuilt coord index + graph: %s nodes, hash=%s",
        snap.node_count,
        snap.snapshot_hash[:12],
    )
    return snap
