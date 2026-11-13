"""RED-first: a LEARN topic must resolve to an ACTUAL article link with real
content. The live failure: the seeded topic reached web_fetch as a query the
exact-title + full-text search + DDG-instant-answer backend could not resolve
(e.g. the squashed slug 'autonomousai' -> full-text search returns []), so
web_fetch returned ZERO results (web_fetch.summary=0.0) and nothing grounded.
Wikipedia opensearch fuzzy-resolves any topic to the best real article — wire it
in so the fetch returns a real link + content.
"""

from __future__ import annotations

import schema.tools.web_fetch as wf

_ARTICLE = {
    "url": "https://en.wikipedia.org/wiki/Autonomous_AI",
    "title": "Autonomous AI",
    "content": "An autonomous agent is an AI system that can perform complex tasks independently.",
    "source": "wikipedia",
}


def _stub_legacy_empty(monkeypatch):
    # The legacy resolvers return nothing for the squashed slug (the live bug).
    monkeypatch.setattr(wf, "_search_wikipedia", lambda q, limit=3: [])
    monkeypatch.setattr(wf, "_search_ddg", lambda q, max_results=2: [])


def test_opensearch_resolves_slug_to_real_article(monkeypatch):
    _stub_legacy_empty(monkeypatch)
    monkeypatch.setattr(
        wf,
        "_opensearch_titles",
        lambda q, limit=3: ["Autonomous AI"] if "autonomous" in q.lower() else [],
    )
    monkeypatch.setattr(
        wf,
        "_fetch_wikipedia_article",
        lambda t: _ARTICLE if t == "Autonomous AI" else None,
    )

    out = wf.web_fetch("autonomousai", max_results=2)
    results = out.get("results") or []
    assert results, "opensearch must resolve a slug topic to a real article (was ZERO)"
    assert results[0]["url"].endswith("Autonomous_AI")
    assert "autonomous agent" in results[0]["content"].lower()


def test_wikipedia_article_url_encodes_spaces_as_underscores(monkeypatch):
    """A multi-word title must hit the REST summary API with spaces as '_'
    (or %20), NOT '+': quote_plus('Autonomous AI') = 'Autonomous+AI' 404s, so
    every multi-word topic silently returned None and the fetch came back ZERO."""
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "title": "Autonomous AI",
                "extract": "An autonomous agent is an AI system that performs complex tasks " * 3,
                "content_urls": {
                    "desktop": {"page": "https://en.wikipedia.org/wiki/Autonomous_AI"}
                },
            }

    def _spy_get(url, *a, **k):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(wf.requests, "get", _spy_get)
    article = wf._fetch_wikipedia_article("Autonomous AI")
    assert article is not None
    assert "+" not in captured["url"], f"space must not be '+' in {captured['url']}"
    assert "Autonomous_AI" in captured["url"]


def test_opensearch_not_used_for_url_queries(monkeypatch):
    """A URL query is fetched directly and must NOT be re-routed through
    opensearch resolution."""
    called = {"n": 0}

    def _spy(q, limit=3):
        called["n"] += 1
        return []

    monkeypatch.setattr(wf, "_opensearch_titles", _spy)

    monkeypatch.setattr(
        wf,
        "_safe_direct_fetch",
        lambda url: {
            "url": url,
            "title": url,
            "content": "hello world",
            "source": "direct",
        },
    )
    out = wf.web_fetch("https://example.com/page", max_results=2)
    assert called["n"] == 0
    assert (out.get("results") or [])[0]["source"] == "direct"


# =====================================================================
# arXiv backend + Meta A source routing (operator 2026-07-08)
# =====================================================================

_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1234.5678v1</id>
    <title>Attention Is All You Need</title>
    <summary>We propose the Transformer, a model architecture based solely on attention.</summary>
  </entry>
</feed>"""


class _ArxivResp:
    status_code = 200
    text = _ARXIV_ATOM


def test_search_arxiv_parses_papers(monkeypatch):
    monkeypatch.setattr(wf.requests, "get", lambda *a, **k: _ArxivResp())
    out = wf._search_arxiv("transformer attention", max_results=3)
    assert len(out) == 1
    assert out[0]["source"] == "arxiv"
    assert "Attention Is All You Need" in out[0]["title"]
    assert "Transformer" in out[0]["content"]
    assert out[0]["url"].startswith("http://arxiv.org/abs/")


def test_source_arxiv_routes_to_arxiv_first(monkeypatch):
    # source="arxiv" (Meta's choice) uses the arXiv backend; wiki/ddg stay stubbed
    _stub_legacy_empty(monkeypatch)
    monkeypatch.setattr(wf, "_resolve_via_opensearch", lambda q, limit=3: [])
    monkeypatch.setattr(
        wf,
        "_search_arxiv",
        lambda q, max_results=3: [
            {
                "url": "http://arxiv.org/abs/9.9",
                "title": "Paper",
                "content": "body",
                "source": "arxiv",
            }
        ],
    )
    res = wf.web_fetch("backpropagation", max_results=3, source="arxiv")
    assert any(r.get("source") == "arxiv" for r in res.get("results", []))


def test_no_source_keeps_general_path(monkeypatch):
    # absent source -> arXiv is NOT called; the general opensearch path runs
    called = {"arxiv": False}

    def _spy_arxiv(q, max_results=3):
        called["arxiv"] = True
        return []

    monkeypatch.setattr(wf, "_search_arxiv", _spy_arxiv)
    monkeypatch.setattr(wf, "_resolve_via_opensearch", lambda q, limit=3: [dict(_ARTICLE)])
    _stub_legacy_empty(monkeypatch)
    wf.web_fetch("some general topic", max_results=3)
    assert called["arxiv"] is False
