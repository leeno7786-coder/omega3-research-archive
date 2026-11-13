"""Omega Phase 7 — deterministic memory taxonomy.

Doctrine:
    Filesystem = immutable truth substrate   (append-only event ledger — canonical)
    Graph      = deterministic cognitive projection   (compiled FROM the ledger)
    Indexes    = deterministic recall accelerators

This package is AUTONOMOUS memory: it is built Guardian-free (no guardian.* imports,
no decide_request / staging), it never writes through memory.api.write_memory, and its
data lives BESIDE its code under the memory system at memory/taxonomy_store/ — it IS
part of the actual memory; "Guardian-free" is the write-path, not the folder. See
docs/architecture/PHASE_7_MEMORY_TAXONOMY_MAP.md.
"""
