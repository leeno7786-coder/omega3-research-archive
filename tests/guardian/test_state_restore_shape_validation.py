"""Medium #18 — restore bypassed canonical shape validation; corrupt-key
reporting was dead.

`restore_at_boot` wrote every persisted key straight through the permissive
`SharedState.write`, which accepts any shape. So a durable canonical value
that was corrupt / forward-drifted on disk (hand-edited save file, partial
write, schema downgrade) got rehydrated into the inter-loop wire unvalidated,
and the RestoreReport's `corrupt_keys` / `keys_dropped_stale` /
`pressure_evidence_degraded` degradation signals only ever fired if the write
itself raised — which the permissive surface never does. Net effect: the
"report missing/corrupt keys" guarantee (P2-S17 RB#4) was unreachable.

Fix: gate each canonical key through `shared_state_keys.validate_write` before
the permissive write. A shape violation drops the value (it does NOT reach the
wire) and is counted as corrupt so the degradation reporting actually fires.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import guardian.state_persistence as sp  # noqa: E402
from core.shared_state import SharedState  # noqa: E402
from core.shared_state_keys import (  # noqa: E402
    LAST_GLOBAL_GOALS,
    LAST_META_ORDERS,
)


def _write_save_file(path: Path, data: dict) -> None:
    # Omit `saved_at` so the hydrate-age gate is skipped (deterministic).
    path.write_text(
        json.dumps({"schema_version": 1, "data": data}),
        encoding="utf-8",
    )


def test_corrupt_canonical_value_is_dropped_not_rehydrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_path = tmp_path / "save_state.json"
    monkeypatch.setattr(sp, "SAVE_PATH", save_path)
    _write_save_file(
        save_path,
        {
            # valid per _validate_global_goals
            LAST_GLOBAL_GOALS: {"cycle_id": "c1", "goals": []},
            # invalid: LAST_META_ORDERS expects a CycleEnvelope shape
            LAST_META_ORDERS: {"not": "an envelope"},
        },
    )

    ss = SharedState()
    report = sp.restore_at_boot(ss)

    # the corrupt durable value must NOT reach the inter-loop wire
    assert ss.read(LAST_META_ORDERS) is None
    # the valid one is rehydrated
    assert ss.read(LAST_GLOBAL_GOALS) == {"cycle_id": "c1", "goals": []}

    # the degradation reporting (previously dead) now fires on a shape violation
    assert report.keys_restored == 1
    assert report.keys_dropped_stale == 1
    assert LAST_META_ORDERS in report.corrupt_keys
    assert report.pressure_evidence_degraded is True
    assert report.ok is False


def test_all_valid_snapshot_restores_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a fully valid snapshot must NOT be falsely flagged corrupt (no over-reject)
    save_path = tmp_path / "save_state.json"
    monkeypatch.setattr(sp, "SAVE_PATH", save_path)
    _write_save_file(
        save_path,
        {LAST_GLOBAL_GOALS: {"cycle_id": "c1", "goals": [{"id": "g1"}]}},
    )

    ss = SharedState()
    report = sp.restore_at_boot(ss)

    assert report.keys_restored == 1
    assert report.keys_dropped_stale == 0
    assert report.corrupt_keys == []
    assert report.pressure_evidence_degraded is False
    assert report.ok is True
    assert ss.read(LAST_GLOBAL_GOALS) == {"cycle_id": "c1", "goals": [{"id": "g1"}]}
