"""Omega 3.0 Memory System — gate-based, schema-validated, indexed.

Architecture:
  memory/engine/     — validate_write, walker, reindex (SetFit retired)
  memory/gates/      — 12 per-lobe gates
  memory/routing/    — deterministic grammar harness + edge catalog
  memory/enrichment/ — decomposers + entity linker
  memory/strength_index.py — per-memory robustness tracker
  memory/core.py     — shared pipeline utilities
  memory/dewey_map.py — coordinate system + knowledge-domain map

All write/recall routing is deterministic: ``subdomain_for``,
``route_memory_tool``, and ``walker`` grammar seeds. No SetFit / CLASSIFY.
"""
