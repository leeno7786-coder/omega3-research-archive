"""Operator-provided URL overrides the LEARN fetch target.

Operator directive 2026-07-22: "make any url i give it plausible, it searched
for the top ten but got stuck using wikipedia." When the operator hands Omega a
source URL, that URL IS the LEARN fetch target — web_fetch direct-fetches a
URL-form query, bypassing the Wikipedia opensearch fuzzy-resolve that otherwise
strands the mind on the encyclopedia. The mastery DOMAIN is inherited from
whatever the normal cascade resolved, so crediting is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cognition.topic_resolution.resolve_topic as rt  # noqa: E402
from pressure_loop import learn_source_url as lsu  # noqa: E402
from pressure_loop import learn_topic_choice as ltc  # noqa: E402


class _State:
    meta = {}
    mi = None
    target_str = ""
    text = ""
    episode_id = "ep-url"


@pytest.fixture(autouse=True)
def _fresh():
    lsu._reset_for_tests()
    ltc._reset_for_tests()
    yield
    lsu._reset_for_tests()
    ltc._reset_for_tests()


# --- _apply_operator_url_override: the override primitive ----------------------


def test_override_inherits_domain_from_resolved_topic():
    lsu.set_source_url("https://owasp.org/www-project-top-ten/")
    base = rt._resolved("OWASP Top 10", "mastery_gap", domain="appsec")
    out = rt._apply_operator_url_override(base)
    assert out.topic == "https://owasp.org/www-project-top-ten/"
    assert out.domain == "appsec"  # mastery domain preserved, NOT the URL host
    assert out.source == "operator_url"


def test_override_falls_back_to_url_host_when_no_topic():
    lsu.set_source_url("https://owasp.org/www-project-top-ten/")
    out = rt._apply_operator_url_override(rt._no_topic())
    assert out.topic == "https://owasp.org/www-project-top-ten/"
    assert out.domain == "owasp"  # host label, since the cascade found no topic
    assert out.source == "operator_url"


def test_override_uses_stashed_domain_hint_when_no_topic():
    lsu.set_source_url("https://example.org/x", domain="appsec")
    out = rt._apply_operator_url_override(rt._no_topic())
    assert out.topic == "https://example.org/x"
    assert out.domain == "appsec"


def test_no_pending_url_is_identity():
    base = rt._resolved("quicksort", "mastery_gap", domain="algorithms")
    out = rt._apply_operator_url_override(base)
    assert out.topic == "quicksort"
    assert out.source == "mastery_gap"
    assert out.domain == "algorithms"


def test_override_is_one_shot():
    lsu.set_source_url("https://owasp.org/x")
    rt._apply_operator_url_override(rt._no_topic())  # consumes
    base = rt._resolved("quicksort", "mastery_gap", domain="algorithms")
    out = rt._apply_operator_url_override(base)
    assert out.source == "mastery_gap"  # nothing left to override with


# --- resolve_learning_topic: end-to-end through the cascade -------------------


def test_resolve_learning_topic_applies_url_override():
    # A concrete mind choice gives the cascade a real topic+domain (appsec);
    # the operator URL then overrides only the fetch target.
    ltc.set_chosen_topic("appsec", "OWASP Top 10 vulnerabilities 2021")
    lsu.set_source_url("https://owasp.org/www-project-top-ten/")
    resolved = rt.resolve_learning_topic(_State())
    assert resolved.topic == "https://owasp.org/www-project-top-ten/"
    assert resolved.source == "operator_url"
    assert resolved.domain == "appsec"


def test_resolve_learning_topic_unchanged_without_url():
    ltc.set_chosen_topic("appsec", "OWASP Top 10 vulnerabilities 2021")
    resolved = rt.resolve_learning_topic(_State())
    assert resolved.topic == "OWASP Top 10 vulnerabilities 2021"
    assert resolved.source == "omega_choice"


# --- the fetch-target plumbing already carries a URL verbatim -----------------


def test_search_phrase_passes_url_verbatim():
    from meta_loop.tool.run import _search_phrase

    url = "https://owasp.org/www-project-top-ten/"
    assert _search_phrase(url) == url
