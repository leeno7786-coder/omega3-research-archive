"""Phase 7D — filesystem layout for the deterministic memory taxonomy substrate.

All paths derive from ``MEMORY_TAXONOMY_ROOT`` (core/paths.py) — the Store that
lives UNDER the memory system at memory/taxonomy_store/.
Every function accepts an optional ``root`` override so tests and tools can redirect
to a scratch directory without touching the live substrate.

Layout:
    memory/taxonomy_store/
      manifests/{compiler_manifest,schema_manifest,latest_snapshot}.json
      compiled/{graph.sqlite,graph_snapshot.json,latest_snapshot_hash.txt}
      indexes/{lexical,entity,claim,episode}.sqlite
      audit/{replay_report.json,graph_diff.json,recall_log.jsonl}
"""

from __future__ import annotations

from pathlib import Path

from core.paths import MEMORY_TAXONOMY_ROOT


def taxonomy_root(root: Path | str | None = None) -> Path:
    """Resolve the taxonomy store root.

    ``root=None`` uses the active profile's ``MEMORY_TAXONOMY_ROOT``.

    Explicit overrides may be either:
    - a profile / OMEGA_ROOT style directory that contains ``memory/``,
    - the ``memory/`` directory itself (maps to ``memory/taxonomy_store``), or
    - the taxonomy store directory itself (name ``taxonomy_store`` or a scratch
      laid out as the store).

    Profile-style roots must map to ``<root>/memory/taxonomy_store`` — not
    ``<root>/compiled`` — so audit/compile/DME share one graph with live workers
    that call ``compile_memory_graph(root=None)``.
    """
    if root is None:
        return MEMORY_TAXONOMY_ROOT
    base = Path(root)
    if base.name == "taxonomy_store":
        return base
    if base.name == "memory":
        return base / "taxonomy_store"
    profile_store = base / "memory" / "taxonomy_store"
    if profile_store.exists() or (base / "memory").exists():
        return profile_store
    return base


def manifests_root(root: Path | str | None = None) -> Path:
    return taxonomy_root(root) / "manifests"


def compiled_root(root: Path | str | None = None) -> Path:
    return taxonomy_root(root) / "compiled"


def indexes_root(root: Path | str | None = None) -> Path:
    return taxonomy_root(root) / "indexes"


def audit_root(root: Path | str | None = None) -> Path:
    return taxonomy_root(root) / "audit"
