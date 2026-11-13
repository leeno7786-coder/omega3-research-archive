"""Deterministic web-research warrant, catalog, and result merge.

Meta A stamps; AUTO's model owns topic and website. No inference here.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_CATALOG_LIMIT = 8

_WEB_SEARCH_RE = re.compile(
    r"\b(?:search|look\s+up|find|browse|research)\b.{0,80}\b(?:web|online|internet|sources?)\b"
    r"|\b(?:web|online|internet)\s+(?:search|searches|lookup|research)\b",
    re.I,
)

_SEEKING_RE = re.compile(
    r"(?:"
    r"\b(?:learn|study|research|explain)\b"
    r"|\btell me about\b"
    r"|\blook\s*up\b"
    r"|\bsearch\b"
    r"|\bwhat(?:'s| is| are)\b"
    r"|\bhow (?:do|does|can|do i|to)\b"
    r"|\bwhy (?:do|does|is|are|did)\b"
    r")",
    re.I,
)


def warrants_web_research(*, intent: str, text: str, target: str = "") -> bool:
    """True when this Global A package should run model-owned web research."""
    intent_u = str(intent or "").strip().upper()
    if intent_u == "LEARN":
        return True
    blob = f"{text or ''} {target or ''}".strip()
    if not blob:
        return False
    from pressure_loop.contributors import infer_operator_command_kind

    if infer_operator_command_kind(blob) == "remember":
        return False
    if intent_u not in {"RESPOND", "USE_TOOL", "RESOLVE_QUERY"}:
        return False
    if infer_operator_command_kind(blob) == "learn":
        return True
    if _WEB_SEARCH_RE.search(blob):
        return True
    return bool(_SEEKING_RE.search(blob))


def research_catalog(domain: str | None, query: str, *, limit: int = _CATALOG_LIMIT) -> list[dict[str, Any]]:
    """Curated URLs for the prompt. Never empty when the YAML catalog loads."""
    from schema.tools.web_fetch import (
        _detect_query_domain,
        _resolve_catalog_domain,
        get_credible_sources,
    )

    resolved = _resolve_catalog_domain(domain) or _detect_query_domain(query)
    rows = list(get_credible_sources(resolved) if resolved else [])
    if not rows:
        rows = list(get_credible_sources(None))
    cap = max(1, int(limit or _CATALOG_LIMIT))
    return [
        {
            "url": str(row.get("url") or ""),
            "name": str(row.get("name") or ""),
            "tier": str(row.get("tier") or ""),
            "domain": str(row.get("domain") or ""),
            "note": str(row.get("note") or ""),
        }
        for row in rows[:cap]
        if str(row.get("url") or "").strip()
    ]


def stamp_web_research(inputs: dict[str, Any], *, seed: str, domain: str = "") -> dict[str, Any]:
    """Attach warrant + catalog onto execution_inputs. Seed query is preserved."""
    seed = str(seed or "").strip()
    domain = str(domain or inputs.get("domain") or "").strip()
    catalog = research_catalog(domain, seed)
    inputs["web_research"] = True
    inputs["web_catalog"] = catalog
    if seed and not str(inputs.get("query") or "").strip():
        inputs["query"] = seed
    if domain and not str(inputs.get("domain") or "").strip():
        inputs["domain"] = domain
    return inputs


def render_catalog_block(catalog: Any) -> str:
    lines = ["CREDIBLE SOURCES (prefer these; web_fetch a full http(s) URL for primary content):"]
    for row in catalog or ():
        if not isinstance(row, Mapping) or not row.get("url"):
            continue
        name = str(row.get("name") or "source").strip() or "source"
        tier = str(row.get("tier") or "").strip()
        note = str(row.get("note") or "").strip()
        label = f"{name} [{tier}]" if tier else name
        extra = f" — {note}" if note else ""
        lines.append(f"- {label}: {row['url']}{extra}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def merge_research_pages(pages: Any) -> dict[str, Any]:
    """Collapse one or more web_fetch dicts into the AUTO tool_result shape."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    catalog: list[dict[str, Any]] = []
    query = ""
    domain = ""
    for page in pages or ():
        if not isinstance(page, Mapping):
            continue
        query = query or str(page.get("query") or "")
        domain = domain or str(page.get("domain") or "")
        for row in page.get("results") or ():
            if not isinstance(row, Mapping):
                continue
            url = str(row.get("url") or "").strip()
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            results.append(dict(row))
        for src in page.get("credible_sources") or ():
            if isinstance(src, Mapping) and src.get("url"):
                catalog.append(dict(src))
    return {
        "query": query,
        "results": results,
        "credible_sources": catalog,
        "domain": domain,
        "total_results": len(results),
    }


def pages_from_operator_records(records: Any) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("tool_name") or "") not in {"web_search", "web_fetch"}:
            continue
        result = record.get("result")
        page = result.get("page") if isinstance(result, Mapping) else None
        if isinstance(page, Mapping):
            pages.append(dict(page))
    return pages
