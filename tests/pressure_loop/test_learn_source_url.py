"""Operator-provided learning SOURCE URL stash + extraction.

Operator directive 2026-07-22: "make any url i give it plausible." Any URL the
operator hands Omega — a directive, or an answer to Omega's own ASK for a source
— is the authoritative fetch target. This module is the one-shot stash + the URL
extractor that captures it from operator text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pressure_loop import learn_source_url as lsu  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    lsu._reset_for_tests()
    yield
    lsu._reset_for_tests()


# --- URL extraction -----------------------------------------------------------


def test_extract_first_url_plain():
    assert lsu.extract_first_url("https://owasp.org/www-project-top-ten/") == (
        "https://owasp.org/www-project-top-ten/"
    )


def test_extract_first_url_embedded_in_phrase():
    assert (
        lsu.extract_first_url("use this: https://owasp.org/Top10/ for the canonical list")
        == "https://owasp.org/Top10/"
    )


def test_extract_trims_trailing_sentence_punctuation():
    assert lsu.extract_first_url("see (https://owasp.org/Top10/).") == ("https://owasp.org/Top10/")


def test_extract_none_when_no_url():
    assert lsu.extract_first_url("just learn owasp top 10") == ""


def test_extract_first_of_many():
    assert lsu.extract_first_url("http://a.com/1 and https://b.com/2") == "http://a.com/1"


# --- the one-shot stash -------------------------------------------------------


def test_stash_set_peek_take_is_one_shot():
    lsu.set_source_url("https://owasp.org/Top10/")
    assert lsu.peek_source_url() == {"url": "https://owasp.org/Top10/", "domain": ""}
    assert lsu.take_source_url() == {"url": "https://owasp.org/Top10/", "domain": ""}
    assert lsu.take_source_url() is None  # consumed


def test_set_ignores_non_url():
    lsu.set_source_url("owasp top 10")
    assert lsu.peek_source_url() is None


def test_set_carries_optional_domain():
    lsu.set_source_url("https://owasp.org/Top10/", domain="appsec")
    assert lsu.peek_source_url()["domain"] == "appsec"


def test_fresh_set_replaces_stale():
    lsu.set_source_url("https://old.example/a")
    lsu.set_source_url("https://new.example/b")
    assert lsu.peek_source_url()["url"] == "https://new.example/b"


# --- capture_from_text: the operator-ingress entry point ----------------------


def test_capture_from_text_stashes_and_reports_true():
    assert lsu.capture_from_text("fetch https://owasp.org/Top10/ please") is True
    assert lsu.peek_source_url()["url"] == "https://owasp.org/Top10/"


def test_capture_from_text_no_url_is_false_noop():
    assert lsu.capture_from_text("just learn python asyncio") is False
    assert lsu.peek_source_url() is None


def test_capture_carries_domain():
    lsu.capture_from_text("https://owasp.org/Top10/", domain="appsec")
    assert lsu.peek_source_url()["domain"] == "appsec"
