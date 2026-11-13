"""Meta A warrant + catalog for model-owned web research."""

from __future__ import annotations

from cognition.web_research import (
    merge_research_pages,
    pages_from_operator_records,
    research_catalog,
    stamp_web_research,
    warrants_web_research,
)


def test_learn_always_warrants_research():
    assert warrants_web_research(intent="LEARN", text="", target="Dijkstra") is True


def test_knowledge_question_warrants_chat_research():
    assert warrants_web_research(intent="RESPOND", text="what is asyncio", target="") is True
    assert warrants_web_research(intent="RESPOND", text="explain Kubernetes ingress", target="") is True
    assert warrants_web_research(intent="RESPOND", text="search the web for the latest VLMs", target="") is True


def test_remember_and_ack_do_not_warrant_research():
    assert warrants_web_research(intent="RESPOND", text="remember this: I take tea at 4", target="") is False
    assert warrants_web_research(intent="RESPOND", text="ok thanks", target="") is False
    assert warrants_web_research(intent="USE_TOOL", text="ok thanks", target="") is False
    assert warrants_web_research(intent="USE_TOOL", text="what is asyncio", target="") is True


def test_python_catalog_includes_official_docs():
    rows = research_catalog(domain="python", query="asyncio event loop")
    urls = {str(row.get("url") or "") for row in rows}
    assert any("docs.python.org" in url for url in urls)
    assert all(row.get("url") and row.get("name") for row in rows)


def test_unknown_domain_still_returns_primary_urls():
    rows = research_catalog(domain="", query="an obscure made-up topic xyzzy")
    assert rows
    assert all(str(row.get("url") or "").startswith("http") for row in rows)


def test_merge_research_pages_collapses_hits():
    merged = merge_research_pages(
        [
            {
                "query": "asyncio",
                "domain": "python",
                "results": [
                    {"url": "https://docs.python.org/3/library/asyncio.html", "title": "asyncio"}
                ],
                "credible_sources": [{"name": "Python docs", "url": "https://docs.python.org/3/"}],
            },
            {
                "results": [
                    {"url": "https://docs.python.org/3/library/asyncio.html", "title": "dup"},
                    {"url": "https://en.wikipedia.org/wiki/Asyncio", "title": "wiki"},
                ]
            },
        ]
    )
    urls = [row["url"] for row in merged["results"]]
    assert urls == [
        "https://docs.python.org/3/library/asyncio.html",
        "https://en.wikipedia.org/wiki/Asyncio",
    ]
    assert merged["query"] == "asyncio"
    assert merged["domain"] == "python"
    assert merged["total_results"] == 2


def test_pages_from_operator_records_reads_tool_meta():
    pages = pages_from_operator_records(
        [
            {"tool_name": "list_directory", "result": {"page": {"results": [{"url": "no"}]}}},
            {
                "tool_name": "web_search",
                "result": {
                    "page": {
                        "query": "asyncio",
                        "results": [{"url": "https://docs.python.org/3/library/asyncio.html"}],
                    }
                },
            },
        ]
    )
    assert pages[0]["query"] == "asyncio"


def test_meta_a_respond_stamps_knowledge_seeking():
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.tool.run import run as meta_a_run

    state = MetaAState(
        mi=None,
        episode_id="ep-chat-research",
        intent="RESPOND",
        action="RESPONSE",
        source={},
        text="what is asyncio",
        target_str="what is asyncio",
        constraints={},
        meta={},
    )
    out = meta_a_run(state)
    assert out.execution_inputs["web_research"] is True
    assert out.execution_inputs["query"] == "what is asyncio"
    assert out.execution_inputs["web_catalog"]
    assert "source" not in out.execution_inputs
    assert any("docs.python.org" in str(row.get("url") or "") for row in out.execution_inputs["web_catalog"])


def test_meta_a_respond_does_not_stamp_remember():
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.tool.run import run as meta_a_run

    state = MetaAState(
        mi=None,
        episode_id="ep-chat-remember",
        intent="RESPOND",
        action="RESPONSE",
        source={},
        text="remember this: I take tea at 4",
        target_str="remember this: I take tea at 4",
        constraints={},
        meta={},
    )
    out = meta_a_run(state)
    assert out.execution_inputs.get("web_research") is not True
    assert "web_catalog" not in out.execution_inputs


def test_meta_a_use_tool_stamps_knowledge_seeking():
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.tool.run import run as meta_a_run

    state = MetaAState(
        mi=None,
        episode_id="ep-tool-research",
        intent="USE_TOOL",
        action="USE_TOOL",
        source={},
        text="what is asyncio",
        target_str="what is asyncio",
        constraints={},
        meta={},
    )
    out = meta_a_run(state)
    assert out.tool == "web_fetch"
    assert out.execution_inputs["web_research"] is True
    assert out.execution_inputs["web_catalog"]


def test_meta_a_use_tool_does_not_stamp_an_ack():
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.tool.run import run as meta_a_run

    state = MetaAState(
        mi=None,
        episode_id="ep-tool-ack",
        intent="USE_TOOL",
        action="USE_TOOL",
        source={},
        text="ok thanks",
        target_str="ok thanks",
        constraints={},
        meta={},
    )
    out = meta_a_run(state)
    assert out.execution_inputs.get("web_research") is not True
    assert "web_catalog" not in out.execution_inputs


def test_stamp_preserves_existing_query():
    inputs = {"query": "data pipelines", "domain": "data-pipelines"}
    stamp_web_research(inputs, seed="data pipelines", domain="data-pipelines")
    assert inputs["query"] == "data pipelines"
    assert inputs["web_research"] is True
    assert inputs["web_catalog"]
