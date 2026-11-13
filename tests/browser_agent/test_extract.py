"""Structured page extracts stay bounded page data, including when a page hides a secret in a URL."""
from __future__ import annotations

import pytest

from browser_agent.extract import bound_extract


def test_search_results_keep_public_links_and_drop_token_urls() -> None:
    bound = bound_extract({
        "success": True,
        "mode": "search_results",
        "data": [
            {"title": "Pricing", "url": "https://example.test/pricing?token=secret", "snippet": "Plans from $42"},
            {"title": "Bad", "url": "javascript:alert(1)", "snippet": "ignore"},
        ],
    })

    assert bound["success"] is True and bound["mode"] == "search_results"
    assert bound["data"] == [{"title": "Pricing", "url": "https://example.test/pricing", "snippet": "Plans from $42"}]


def test_a_page_that_is_not_an_extract_result_fails_instead_of_looking_empty() -> None:
    with pytest.raises(ValueError, match="not an object"):
        bound_extract(["pricing"])
    refused = bound_extract({"success": False, "mode": "selector", "error": "No elements: #missing"})
    assert refused == {"success": False, "mode": "selector", "error": "No elements: #missing"}


def test_article_text_and_tables_are_capped() -> None:
    bound = bound_extract({
        "success": True,
        "mode": "article",
        "data": {"text": "word " * 5000, "excerpt": "word " * 200, "word_count": 5000, "selector_hint": "article"},
    })
    assert len(bound["data"]["text"]) <= 6000
    assert bound["data"]["selector_hint"] == "article"

    table = bound_extract({
        "success": True,
        "mode": "table",
        "data": [{"caption": "Prices", "headers": ["Plan", "Cost"], "rows": [["Pro", "$42"], ["Team", "$99"]]}],
    })
    assert table["data"][0]["rows"] == [["Pro", "$42"], ["Team", "$99"]]
