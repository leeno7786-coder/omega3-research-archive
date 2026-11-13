"""Telemetry panel visual upgrade — structural contracts on omega_interface.html."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
HUD_HTML = ROOT / "hud" / "omega_interface.html"


@pytest.fixture(scope="module")
def html() -> str:
    return HUD_HTML.read_text(encoding="utf-8")


def test_double_bezel_shell_wraps_td_cards(html: str) -> None:
    assert "td-shell" in html
    assert "function _tdCard(" in html
    assert 'class="td-shell' in html or "td-shell ${" in html


def test_hero_shell_for_signature_visuals(html: str) -> None:
    assert "function _tdHeroShell(" in html
    assert "_tdHeroShell(" in html
    assert "td-hero-shell" in html


def test_panel_status_rail(html: str) -> None:
    assert "function _tdPanelRail(" in html
    assert "td-panel-rail" in html


def test_bar_row_drilldown_helpers(html: str) -> None:
    assert "function _tdBarDiagnosticsHtml(" in html
    assert "td-bar-hit" in html
    assert "td-bar-drill" in html
    assert "aria-expanded" in html.split("function _tdBarList")[1].split("function _tdHeatmap")[0]


def test_staggered_panel_enter_css(html: str) -> None:
    assert "animation-delay: 0.10s" in html
    assert "td-panel-entering" in html
    assert "conv-pulse-core-wrap" in html
    assert "transform: scale(1.35)" in html


def test_metric_delta_tracking_helpers(html: str) -> None:
    assert "function _tdDeltaAttr(" in html
    assert "data-td-delta" in html
    assert "_tdMetricPrev" in html
