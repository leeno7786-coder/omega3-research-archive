"""Monotonic write-ordinal — the deterministic durable recency tie-breaker.

Committed records carry observed canonical time. This counter preserves an exact
write order when timestamps coincide, and is file-backed at a caller-supplied path
so isolated roots and replay runs get independent sequences.
"""

from __future__ import annotations

import json


def test_next_write_ordinal_is_monotonic(tmp_path):
    from memory.write_ordinal import next_write_ordinal

    p = tmp_path / "write_ordinal.json"
    assert [next_write_ordinal(p) for _ in range(5)] == [1, 2, 3, 4, 5]


def test_ordinal_is_durable_and_persisted(tmp_path):
    from memory.write_ordinal import next_write_ordinal

    p = tmp_path / "write_ordinal.json"
    next_write_ordinal(p)
    b = next_write_ordinal(p)
    assert p.exists()
    assert int(json.loads(p.read_text(encoding="utf-8"))["counter"]) == b == 2


def test_ordinal_isolated_per_path(tmp_path):
    # Two distinct roots keep independent counters — this is what gives isolation +
    # replay determinism a fresh per-root counter.
    from memory.write_ordinal import next_write_ordinal

    a = tmp_path / "a" / "write_ordinal.json"
    b = tmp_path / "b" / "write_ordinal.json"
    assert next_write_ordinal(a) == 1
    assert next_write_ordinal(a) == 2
    assert next_write_ordinal(b) == 1  # independent


def test_current_write_ordinal_reads_without_incrementing(tmp_path):
    from memory.write_ordinal import current_write_ordinal, next_write_ordinal

    p = tmp_path / "write_ordinal.json"
    assert current_write_ordinal(p) == 0
    next_write_ordinal(p)
    next_write_ordinal(p)
    assert current_write_ordinal(p) == 2
    assert current_write_ordinal(p) == 2  # read-only, stable
