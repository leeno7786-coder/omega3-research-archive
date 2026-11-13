"""OQ2 — dead tick-WAL contributor cache export/import must not exist."""

from __future__ import annotations

import pytest


def test_export_contributor_cache_state_removed():
    from pressure_loop import pressure_field

    with pytest.raises(AttributeError):
        _ = pressure_field.export_contributor_cache_state


def test_import_contributor_cache_state_removed():
    from pressure_loop import pressure_field

    with pytest.raises(AttributeError):
        _ = pressure_field.import_contributor_cache_state
