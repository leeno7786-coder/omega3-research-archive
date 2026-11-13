"""Missing governance.yaml is valid — no ERROR spam."""

from __future__ import annotations

import logging

from config import loader


def test_load_governance_config_missing_file_returns_empty_dict(caplog, tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "_CONFIG_DIR", tmp_path)
    loader.clear_cache()

    with caplog.at_level(logging.DEBUG, logger="omega.config.loader"):
        result = loader.load_governance_config()

    assert result == {}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
