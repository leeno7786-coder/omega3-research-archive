"""redirect_memory_to_tmp must isolate the strength_index singleton.

The isolation helper redirects every memory PATH surface (MEMORY_ROOT, coord index,
staging, embed queue) to tmp, but the strength_index lived at an OMEGA_ROOT-relative
path (guardian/state/strength_index.json) and its process-global singleton was never
reset. So an "isolated" test's readback ran the real recall pipeline's lazy decay +
sustained-floor demotion against the LIVE strength_index — whose persisted last_access
for a deterministic content-hash id was stale (days old) — slamming confidence to the
floor and demoting a freshly-written record (episode -> observation). This closes that
leak: after redirect, the strength_index must be fresh and tmp-backed.
"""

from __future__ import annotations


def test_redirect_isolates_strength_index_singleton_and_path(tmp_path, monkeypatch):
    import memory.strength_index as si_mod
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    # A live-ish singleton carrying stale cross-run state — the leak source.
    poisoned = si_mod.StrengthIndex(tmp_path / "live_strength.json")
    poisoned._records["9.99.stale"] = {"last_access": "2020-01-01T00:00:00+00:00"}
    monkeypatch.setattr(si_mod, "_singleton", poisoned)

    redirect_memory_to_tmp(tmp_path / "iso", monkeypatch)

    si = si_mod.ensure_strength_index()
    # The stale live record must not leak into the isolated store.
    assert si.get_record("9.99.stale") is None
    # And the isolated index must persist under tmp, never live guardian/state.
    assert str(si._persist_path).startswith(str(tmp_path / "iso"))
