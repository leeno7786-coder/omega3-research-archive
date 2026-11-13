"""Settings proactive-history UI contracts."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"


def test_settings_markup_exposes_permanent_proactive_history_tab():
    markup = (UI_SRC / "panels" / "settings-markup.js").read_text(encoding="utf-8")

    assert 'data-settings-view="proactive_history"' in markup
    assert 'data-settings-view-panel="proactive_history"' in markup
    assert "questions · results · notices" in markup


def test_settings_history_renderer_is_read_only_and_uses_projection_endpoint():
    source = (UI_SRC / "panels" / "settings-proactive-history.js").read_text(encoding="utf-8")

    assert "async function _renderSettingsProactiveHistory" in source
    assert "fetch('/api/proactive/history'" in source
    assert "Question" in source
    assert "Result" in source
    assert "Notice" in source
    assert "response.text" in source
    assert "Cleared" in source
    assert "/respond" not in source
    assert "method: 'DELETE'" not in source


def test_settings_history_tab_lazy_refreshes_when_selected():
    wire = (UI_SRC / "panels" / "settings-wire.js").read_text(encoding="utf-8")

    assert "if (id === 'proactive_history')" in wire
    assert '[data-settings-view-panel="proactive_history"]' in wire
    assert "_renderSettingsProactiveHistory(panel)" in wire


def test_history_renderer_is_bundled_before_settings_wire():
    manifest = json.loads((UI_SRC / "manifest.json").read_text(encoding="utf-8"))
    panels = manifest["panels"]
    ids = [entry["id"] for entry in panels]

    assert "settings_proactive_history" in ids
    assert ids.index("settings_proactive_history") < ids.index("settings_wire")
