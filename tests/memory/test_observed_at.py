"""observed_at sidecar — the real wall-clock creation date of a record.

Record bytes are deterministic content-hashes and the write-ordinal gives ORDER not DATES, so
date-range temporal recall ("what did we build yesterday") needs a real calendar timestamp. This
sidecar maps memory_id -> the wall-clock ISO the record was FIRST committed (assign-once), kept OUT
of the record bytes so the deterministic-bytes invariant is untouched.
"""

from __future__ import annotations

import json

from memory.observed_at import all_observed_at, observed_at_for, stamp_observed_at


def test_stamp_records_the_injected_time(tmp_path):
    p = tmp_path / "observed_at.json"
    got = stamp_observed_at(p, "2.00.a", now="2026-07-15T10:00:00+00:00")
    assert got == "2026-07-15T10:00:00+00:00"
    assert observed_at_for(p, "2.00.a") == "2026-07-15T10:00:00+00:00"


def test_assign_once_preserves_the_original_date(tmp_path):
    p = tmp_path / "observed_at.json"
    stamp_observed_at(p, "2.00.a", now="2026-07-15T10:00:00+00:00")
    # a re-commit (decay / embed re-write) must NOT move the creation date
    again = stamp_observed_at(p, "2.00.a", now="2026-07-16T23:59:00+00:00")
    assert again == "2026-07-15T10:00:00+00:00"
    assert observed_at_for(p, "2.00.a") == "2026-07-15T10:00:00+00:00"


def test_all_observed_at_returns_the_full_map(tmp_path):
    p = tmp_path / "observed_at.json"
    stamp_observed_at(p, "2.00.a", now="2026-07-15T10:00:00+00:00")
    stamp_observed_at(p, "2.00.b", now="2026-07-16T10:00:00+00:00")
    assert all_observed_at(p) == {
        "2.00.a": "2026-07-15T10:00:00+00:00",
        "2.00.b": "2026-07-16T10:00:00+00:00",
    }


def test_missing_id_and_absent_sidecar_are_safe(tmp_path):
    p = tmp_path / "nope.json"
    assert stamp_observed_at(p, "") is None
    assert observed_at_for(p, "anything") is None
    assert all_observed_at(p) == {}


def test_real_now_is_iso_utc_when_not_injected(tmp_path):
    p = tmp_path / "observed_at.json"
    got = stamp_observed_at(p, "2.00.a")
    assert isinstance(got, str) and "T" in got  # ISO-8601 wall-clock
    from datetime import datetime

    datetime.fromisoformat(got)  # parses -> a real timestamp, not a content hash


def test_map_persists_as_json(tmp_path):
    p = tmp_path / "observed_at.json"
    stamp_observed_at(p, "2.00.a", now="2026-07-15T10:00:00+00:00")
    assert json.loads(p.read_text(encoding="utf-8")) == {"2.00.a": "2026-07-15T10:00:00+00:00"}
