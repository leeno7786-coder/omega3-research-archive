"""
Web Fetch Tool
==============

Fetches knowledge content from a curated, tiered set of credible
sources defined in `config/web_sources.yaml`. Called by the model
during autonomous loop inference (phase 3) and by the brain UI lane
through the singular-mind dispatch path.

Source policy (per the operator's curation directive 2026-05-07):
    - Primary + academic sources (vendor docs, lab publications, peer
      review) are the canonical content path.
    - Wikipedia is kept but lives in the `defer` tier and is consulted
      AFTER higher-tier sources have been exhausted. A lot of editors,
      low barrier to BS sliding in — fine for orientation, never the
      authoritative read.
    - DuckDuckGo's instant-answer API is supplementary.

Result shape:
    {
        "query": "<the operator/system query>",
        "results": [...],            # fetched content blobs
        "credible_sources": [...],   # CONFIGURED URLs the calling model
                                     # can direct-fetch for primary content
        "domain": "<detected domain or empty>",
        "total_results": <int>,
    }

`credible_sources` is the new field: when a query maps to a domain
that has configured primary sources, those URLs are surfaced so the
calling model can decide to direct-fetch them (web_fetch already
handles URL-form queries — pass a full URL and it fetches it).
"""

import ipaddress
import logging
import random
import re
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("omega.schema.tools.web_fetch")

_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


# Manifest lives in `schema/tools/web_fetch.json` (canonical
# ToolSpec). This file is pure implementation — no parallel manifest.

_TIMEOUT = 15
# Full-article ingest width (2026-07-19): the mind learns from the article
# BODY now, not the lead paragraph. The comprehension phase re-caps per row;
# this is the transport ceiling.
_MAX_CONTENT = 24000

_WIKI_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
_WIKI_ACTION_API = "https://en.wikipedia.org/w/api.php"
_WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"
_DDG_URL = "https://api.duckduckgo.com/"

# Identify the client to upstream APIs. Wikipedia's policy explicitly
# requires a descriptive User-Agent (https://meta.wikimedia.org/wiki/User-Agent_policy)
# and a missing UA can result in blocks. DuckDuckGo treats requests with
# no UA as low-confidence. One module-level constant so every site stays
# in lockstep.
_UA_HEADER = {"User-Agent": "Omega3.0/1.0 (+https://github.com/leeno7786-coder/Omega3.0)"}

# Content-Type prefixes accepted for direct-URL fetch parsing as text.
# Anything else (PDF, image, archive, octet-stream) is skipped — the
# regex-strip-tags downstream is text-only and would corrupt binary.
_TEXT_CONTENT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")

# Knowledge subdomain -> concrete Wikipedia topics. Used as a last-resort
# topical anchor when the configured-sources path returns nothing AND the
# raw Wikipedia search comes up empty. The credible-sources catalog in
# config/web_sources.yaml is the primary routing surface.
_DOMAIN_TOPICS = {
    "science": [
        "Neuroscience",
        "Cell_biology",
        "Quantum_mechanics",
        "Photosynthesis",
        "Evolution",
        "Genetics",
        "Ecology",
    ],
    "mathematics": [
        "Topology",
        "Linear_algebra",
        "Calculus",
        "Number_theory",
        "Graph_theory",
        "Statistics",
        "Group_theory",
    ],
    "philosophy": [
        "Epistemology",
        "Ethics",
        "Existentialism",
        "Philosophy_of_mind",
        "Metaphysics",
        "Aesthetics",
    ],
    "technology": [
        "Machine_learning",
        "Artificial_intelligence",
        "Cryptography",
        "Computer_science",
        "Deep_learning",
        "Robotics",
    ],
    "engineering": [
        "Electrical_engineering",
        "Signal_processing",
        "Control_theory",
        "Thermodynamics",
        "Materials_science",
    ],
    "humanities": ["Linguistics", "Literary_theory", "Music_theory", "Art_history", "Rhetoric"],
    "social_science": [
        "Psychology",
        "Sociology",
        "Economics",
        "Game_theory",
        "Cognitive_bias",
        "Anthropology",
    ],
    "medicine": ["Pharmacology", "Immunology", "Epidemiology", "Neurology", "Pathology"],
    "world": [
        "Ancient_Rome",
        "History_of_science",
        "Jurisprudence",
        "Urban_planning",
        "Agricultural_science",
    ],
    "synthesis": [
        "Interdisciplinarity",
        "Systems_theory",
        "Neuromorphic_engineering",
        "Computational_linguistics",
    ],
}


# =====================================================================
# Credible-sources catalog (config/web_sources.yaml)
# =====================================================================
# Lazy-loaded + cached. The catalog is the librarian: when a query maps
# to a domain, the catalog tells us which trusted URLs the calling model
# should direct-fetch. We don't auto-scrape every URL because each site
# has a different (or no) search API — per-site adapters get brittle
# fast. Surfacing the URLs as model-visible hints lets the model's own
# routing pick the right primary source.
#
# Tier ordering (primary → academic → reference → community → defer)
# enforces the operator's directive: Wikipedia is in `defer`, consulted
# last; primary docs and lab publications come first.

# Keywords mapping a free-text query to a configured domain. First
# match wins; we walk in declaration order, so put more-specific
# domains before more-general ones.
_DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "python": ["python", "cpython", "pypi", "asyncio", "pydantic", "fastapi", "django", "flask"],
    "rust": ["rust", "cargo", "crate", "rustc"],
    "go": ["golang", "go module", "go pkg", "go-lang"],
    "typescript": ["typescript", "ts type", " tsc ", "tsconfig"],
    "nodejs": ["node.js", "nodejs", "npm ", "express.js"],
    "web": ["html", "css ", "javascript", "browser api", "dom api", "fetch api", "web api"],
    "ml_frameworks": ["pytorch", "tensorflow", "numpy", "scikit-learn", "sklearn", "scipy"],
    "ai_research": [
        "llm",
        "transformer",
        "attention",
        "rlhf",
        "interpretability",
        "mech interp",
        "diffusion model",
        "alignment",
        "fine-tun",
        "embedding model",
        "arxiv",
        "anthropic",
        "openai",
        "deepmind",
        "meta ai",
        "fair ",
    ],
    "devops": ["docker", "kubernetes", "k8s", "container", "helm", "compose"],
    "philosophy": ["philosophy", "ethics", "epistemology", "metaphysics", "existential"],
    "mathematics": ["theorem", "proof", "topology", "category theory", "algebra", " calculus"],
    "medicine": ["pubmed", "clinical trial", "pharmacology", "epidemiolog", "immunolog"],
    "science": ["physics", "chemistry", "biology", "astronomy"],
    "tech_news": ["arstechnica", "ieee spectrum", "tech news"],
    "coding": [
        "github",
        "stackoverflow",
        "stack overflow",
        "code",
        "library",
        "framework",
        "function",
        "class",
        "method",
        "api",
    ],
}


_sources_cache: Optional[List[Dict[str, Any]]] = None
_tier_order_cache: Optional[List[str]] = None
_domain_defaults_cache: Optional[Dict[str, str]] = None


class WebSourcesUnavailable(RuntimeError):
    """The credible-source catalog (config/web_sources.yaml) could not be loaded.

    Never an empty catalog: "no configured source matched" and "the catalog could not be read"
    must not share a return value (AGENTS.md section 2.1). The cause travels as ``__cause__``.
    """


def _load_sources_catalog() -> tuple:
    """Lazy-load + cache config/web_sources.yaml. Returns
    (sources_list, tier_order, domain_defaults).

    A catalog that cannot be loaded raises ``WebSourcesUnavailable`` and caches nothing, so the
    next call tries again. It used to cache an empty catalog for the life of the process and
    degrade to a legacy path — a fallback AGENTS.md section 2 forbids."""
    global _sources_cache, _tier_order_cache, _domain_defaults_cache
    if _sources_cache is not None:
        return _sources_cache, _tier_order_cache, _domain_defaults_cache
    try:
        from config.loader import load_web_sources_config

        cfg = load_web_sources_config() or {}
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "tools",
            "web_sources_config_load_failed",
            status="failed",
            ok=False,
            error=str(exc),
        )
        raise WebSourcesUnavailable(
            f"config/web_sources.yaml could not be loaded: {type(exc).__name__}: {exc}"
        ) from exc
    _sources_cache = list(cfg.get("sources") or [])
    _tier_order_cache = list(
        cfg.get("tier_order") or ["primary", "academic", "reference", "community", "defer"]
    )
    _domain_defaults_cache = dict(cfg.get("domain_defaults") or {})
    return _sources_cache, _tier_order_cache, _domain_defaults_cache


def _detect_query_domain(query: str) -> Optional[str]:
    """Map a free-text query to a configured domain key, or None when
    no keyword pattern matches. URL-form queries return None — those
    bypass the catalog entirely."""
    if not query or query.startswith(("http://", "https://")):
        return None
    q = " " + query.lower().strip() + " "
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if kw in q:
                return domain
    return None


# P3-S44: bridge the coarse knowledge categories that are NOT themselves
# config/web_sources.yaml catalog domains to their closest catalog
# domain. Categories that ARE catalog domains (science, mathematics,
# philosophy, medicine) and all coding catalog domains pass through
# unchanged. This lets Meta hand web_fetch the substrate's KNOWN dewey
# category so credible_sources resolve deterministically instead of
# being guessed from query keywords (which miss for fine subdomains
# like 'acoustics').
_CATEGORY_TO_CATALOG: Dict[str, str] = {
    "technology": "ai_research",
    "engineering": "science",
    "humanities": "general",
    "social_science": "general",
    "world": "general",
    "synthesis": "general",
}


def _resolve_catalog_domain(domain: Optional[str]) -> Optional[str]:
    """Resolve a caller-supplied domain hint to a web_sources catalog
    domain. Returns None for an empty hint (caller then falls back to
    keyword detection).

    The hint may arrive at ANY granularity — a fine dewey subdomain
    ('psychology', 'acoustics'), a coarse category ('social_science'),
    or a catalog domain ('science', 'python'). The model frequently
    emits the bare subdomain in its tool_call, so normalize through
    ``dewey_map.category_for_domain`` FIRST (subdomain -> coarse
    category; a category or unknown passes through), THEN bridge the
    coarse knowledge categories that are not themselves catalog domains.
    Without the normalize step a model-supplied subdomain like
    'psychology' is not a catalog domain and credible_sources come back
    empty."""
    if not domain:
        return None
    d = domain.strip().lower()
    if not d:
        return None
    # Normalize a fine subdomain to its coarse category. category_for_domain
    # returns the category for a knowledge subdomain, the catalog domain for
    # a coding subdomain, the same value for a coarse category key, or None
    # for an unknown token (left as-is below).
    try:
        from memory.dewey_map import category_for_domain

        coarse = category_for_domain(d) or category_for_domain(d, coding=True)
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "tools",
            "web_fetch_category_resolution_failed",
            status="degraded",
            ok=False,
            error=str(exc),
            domain=d,
        )
        coarse = None
    resolved = coarse or d
    return _CATEGORY_TO_CATALOG.get(resolved, resolved)


def get_credible_sources(
    domain: Optional[str] = None,
    *,
    tier_ceiling: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Return configured sources for a domain, ordered by tier.

    Args:
        domain: domain key (matches config/web_sources.yaml entries).
            None returns ALL primary-tier sources across domains.
        tier_ceiling: lowest tier to include (default: per-domain
            from config, or 'community' if unspecified).
        limit: max sources to return.

    Returns: list of {url, name, tier, domain, note} dicts.
    """
    sources, tier_order, domain_defaults = _load_sources_catalog()
    if not sources:
        return []

    ceiling = tier_ceiling or domain_defaults.get(domain or "", "community")
    try:
        ceiling_idx = tier_order.index(ceiling)
    except ValueError:
        ceiling_idx = len(tier_order) - 1

    matched: List[Dict[str, Any]] = []
    for src in sources:
        if domain is not None and src.get("domain") != domain:
            continue
        try:
            tier_idx = tier_order.index(src.get("tier", ""))
        except ValueError:
            continue
        if tier_idx > ceiling_idx:
            continue
        matched.append(
            {
                "url": src.get("url", ""),
                "name": src.get("name", ""),
                "tier": src.get("tier", ""),
                "domain": src.get("domain", ""),
                "note": src.get("note", ""),
                "tier_rank": tier_idx,
            }
        )

    # Sort by tier rank (primary first), then by name for stability
    matched.sort(key=lambda s: (s["tier_rank"], s["name"]))
    return [{k: v for k, v in s.items() if k != "tier_rank"} for s in matched[: max(0, limit)]]


def _fetch_wikipedia_article(topic: str) -> dict:
    """Fetch the FULL plain-text article body (action API extracts).

    The REST page/summary API returns only the LEAD PARAGRAPH — an hour of
    LEARN episodes ingested ~300 chars per topic and the mind "learned" article
    intros (live 2026-07-19, operator: "not ingesting full information").
    explaintext extracts return the whole article (Kubernetes: ~32k chars vs
    ~300). The summary API stays as the fallback."""
    try:
        resp = requests.get(
            _WIKI_ACTION_API,
            params={
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "redirects": 1,
                "format": "json",
                "titles": topic,
            },
            timeout=_TIMEOUT,
            headers=_UA_HEADER,
        )
        if resp.status_code == 200:
            pages = (resp.json().get("query") or {}).get("pages") or {}
            for page in pages.values():
                if not isinstance(page, dict) or "missing" in page:
                    continue
                extract = str(page.get("extract") or "")
                title = str(page.get("title") or topic)
                if extract and len(extract) > 50:
                    return {
                        "url": "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                        "title": title,
                        "content": extract[:_MAX_CONTENT],
                        "source": "wikipedia",
                    }
    except Exception as exc:
        log.debug("Wikipedia full-article fetch failed for '%s': %s", topic, exc)
    try:
        # Fallback: lead-paragraph summary. The REST summary API keys on the
        # page title with spaces as '_' (or %20) — quote_plus would emit '+'
        # and 404 every multi-word title.
        resp = requests.get(
            f"{_WIKI_API}{quote(topic.replace(' ', '_'))}", timeout=_TIMEOUT, headers=_UA_HEADER
        )
        if resp.status_code == 200:
            data = resp.json()
            extract = data.get("extract", "")
            if extract and len(extract) > 50:
                return {
                    "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
                    "title": data.get("title", topic),
                    "content": extract[:_MAX_CONTENT],
                    "source": "wikipedia",
                }
    except Exception as exc:
        log.debug("Wikipedia summary fallback failed for '%s': %s", topic, exc)
    return None


def _search_wikipedia(query: str, limit: int = 3) -> List[dict]:
    """Search Wikipedia for articles matching query."""
    results = []
    try:
        resp = requests.get(
            _WIKI_SEARCH,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
                "format": "json",
            },
            timeout=_TIMEOUT,
            headers=_UA_HEADER,
        )
        data = resp.json()
        for hit in data.get("query", {}).get("search", []):
            title = hit.get("title", "")
            article = _fetch_wikipedia_article(title)
            if article:
                results.append(article)
    except Exception as exc:
        log.debug("Wikipedia search failed: %s", exc)
    return results


def _opensearch_titles(query: str, limit: int = 3) -> List[str]:
    """Wikipedia opensearch: fuzzy-resolve ANY topic — a phrase OR a squashed
    slug ('autonomousai') — to the best-matching REAL article titles. This is
    the reliable resolver where exact-title + full-text search return nothing
    (full-text 'autonomousai' -> []; opensearch -> ['Autonomous AI', ...])."""
    try:
        resp = requests.get(
            _WIKI_SEARCH,
            params={
                "action": "opensearch",
                "search": query,
                "limit": limit,
                "namespace": 0,
                "format": "json",
            },
            timeout=_TIMEOUT,
            headers=_UA_HEADER,
        )
        data = resp.json()
        # opensearch returns [query, [titles], [descriptions], [urls]]
        if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
            return [str(t) for t in data[1] if str(t).strip()]
    except Exception as exc:
        log.debug("Wikipedia opensearch failed for '%s': %s", query, exc)
    return []


def _resolve_via_opensearch(query: str, limit: int = 3) -> List[dict]:
    """Resolve a topic to REAL article(s) + content: opensearch -> best titles
    -> fetch each article. The 'topic must resolve to an actual link where
    information can be found' guarantee."""
    results: List[dict] = []
    for title in _opensearch_titles(query, limit=limit):
        article = _fetch_wikipedia_article(title)
        if article:
            results.append(article)
        if len(results) >= limit:
            break
    return results


_ARXIV_API = "http://export.arxiv.org/api/query"


def _search_arxiv(query: str, max_results: int = 3) -> List[dict]:
    """Search the arXiv API for real research papers (operator 2026-07-08:
    Meta routes research/academic topics here instead of the encyclopedia).
    Returns title + abstract per paper. Failure-soft -> []."""
    results: List[dict] = []
    try:
        import xml.etree.ElementTree as ET

        resp = requests.get(
            _ARXIV_API,
            params={
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": max(1, int(max_results)),
            },
            timeout=_TIMEOUT,
            headers=_UA_HEADER,
        )
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.text)
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", "", ns) or "").strip()
            summary = (entry.findtext("a:summary", "", ns) or "").strip()
            link = (entry.findtext("a:id", "", ns) or "").strip()
            if not title or not summary:
                continue
            results.append(
                {
                    "url": link,
                    "title": title[:120],
                    "content": f"{title}. {summary}"[:_MAX_CONTENT],
                    "source": "arxiv",
                }
            )
            if len(results) >= max_results:
                break
    except Exception as exc:
        log.debug("arXiv search failed: %s", exc)
    return results[:max_results]


def _search_ddg(query: str, max_results: int = 2) -> List[dict]:
    """Search DuckDuckGo instant answer API."""
    results = []
    try:
        resp = requests.get(
            _DDG_URL,
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=_TIMEOUT,
            headers=_UA_HEADER,
        )
        data = resp.json()
        if data.get("AbstractText"):
            results.append(
                {
                    "url": data.get("AbstractURL", ""),
                    "title": data.get("Heading", query),
                    "content": data["AbstractText"][:_MAX_CONTENT],
                    "source": "duckduckgo",
                }
            )
    except Exception as exc:
        log.debug("DDG search failed: %s", exc)
    return results[:max_results]


def _validate_public_http_url(url: str) -> tuple[str, ...]:
    """Return public target addresses or reject the URL before any connection."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("direct fetch requires an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("direct fetch does not allow URL credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("direct fetch URL has an invalid port") from exc
    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not literal_ip.is_global:
            raise ValueError(f"direct fetch target is not public: {literal_ip}")
        return (str(literal_ip),)

    addresses = socket.getaddrinfo(
        parsed.hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    if not addresses:
        raise ValueError("direct fetch target did not resolve")
    public_addresses: list[str] = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(f"direct fetch target is not public: {ip}")
        public_addresses.append(str(ip))
    return tuple(dict.fromkeys(public_addresses))


class _PinnedTLSAdapter(HTTPAdapter):
    """Connect to a validated IP while verifying TLS for the original hostname."""

    def __init__(self, server_hostname: str) -> None:
        self._server_hostname = server_hostname
        super().__init__()

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        pool_kwargs["assert_hostname"] = self._server_hostname
        pool_kwargs["server_hostname"] = self._server_hostname
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


def _pinned_request_target(url: str, address: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise ValueError("direct fetch URL has no hostname")
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port

    target_host = f"[{address}]" if ":" in address else address
    target_netloc = f"{target_host}:{port}" if parsed.port is not None else target_host
    request_url = urlunsplit((parsed.scheme, target_netloc, parsed.path or "/", parsed.query, ""))

    hostname = parsed.hostname.encode("idna").decode("ascii")
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None and port != default_port:
        host_header = f"{host_header}:{port}"
    return request_url, host_header


def _read_streaming_text(response: requests.Response) -> str:
    """Read the decoded response with a hard cap on decompressed bytes."""
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > _MAX_RESPONSE_BYTES:
            raise ValueError("direct fetch response exceeds transport cap")

    body = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("direct fetch response exceeds transport cap")

    content_type = response.headers.get("Content-Type") or ""
    charset_match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    charset = charset_match.group(1).strip(chr(34) + "'") if charset_match else "utf-8"
    try:
        return bytes(body).decode(charset, errors="replace")
    except LookupError:
        return bytes(body).decode("utf-8", errors="replace")


def _safe_direct_fetch(url: str) -> dict:
    """Fetch one public text URL while revalidating every redirect target."""
    session = requests.Session()
    session.trust_env = False
    current = url
    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            addresses = _validate_public_http_url(current)
            request_url, host_header = _pinned_request_target(current, addresses[0])
            parsed = urlsplit(current)
            if parsed.scheme == "https":
                session.mount(
                    "https://",
                    _PinnedTLSAdapter(str(parsed.hostname)),
                )
            request_headers = {**_UA_HEADER, "Host": host_header}
            response = session.get(
                request_url,
                timeout=_TIMEOUT,
                headers=request_headers,
                allow_redirects=False,
                stream=True,
            )
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location or redirect_count >= _MAX_REDIRECTS:
                        raise ValueError("direct fetch redirect limit exceeded")
                    current = urljoin(current, location)
                    continue

                response.raise_for_status()
                content_type = (response.headers.get("Content-Type") or "").lower()
                if not any(content_type.startswith(prefix) for prefix in _TEXT_CONTENT_TYPES):
                    return {
                        "url": current,
                        "title": current[:80],
                        "content": (f"[skipped: non-text content_type={content_type!r}]"),
                        "source": "direct",
                    }

                text = re.sub(r"<[^>]+>", " ", _read_streaming_text(response))
                text = re.sub(r"\s+", " ", text).strip()
                return {
                    "url": current,
                    "title": current[:80],
                    "content": text[:_MAX_CONTENT],
                    "source": "direct",
                }
            finally:
                response.close()
    finally:
        session.close()

    raise ValueError("direct fetch redirect limit exceeded")


def web_fetch(query: str, max_results: int = 3, domain: Optional[str] = None, **kwargs) -> dict:
    """
    Fetch knowledge content from the web.

    Strategy:
        1. URL-form query → direct fetch (unchanged from prior behavior).
        2. Text query →
            a. Detect query domain via keyword match.
            b. Pull credible_sources for that domain from
               config/web_sources.yaml (primary + academic tiers first).
            c. Run the existing Wikipedia search + DDG fallback to
               populate `results` (Wikipedia results are tagged
               `source="wikipedia"` so the calling model can de-emphasize
               them per the deprioritization policy).
            d. Surface credible_sources as a separate field — the
               calling model can choose to direct-fetch any of those
               URLs in a follow-up turn for higher-trust content.

    Returns a dict matching the canonical returns_schema declared in
    `schema/tools/web_fetch.json`. The LLM-protocol boundary
    serializes to JSON; tools return native dicts.
    """
    query = str(query or "").strip()
    if not query:
        return {
            "query": "",
            "results": [],
            "credible_sources": [],
            "domain": "",
            "total_results": 0,
            "error": "query is required",
        }
    parsed_query = urlsplit(query)
    if parsed_query.scheme and parsed_query.scheme.lower() not in {"http", "https"}:
        return {
            "query": query,
            "results": [],
            "credible_sources": [],
            "domain": "",
            "total_results": 0,
            "error": "only http:// and https:// URLs are allowed",
        }
    try:
        max_results = max(1, min(10, int(max_results or 3)))
    except (TypeError, ValueError):
        max_results = 3
    results = []
    fetch_error = ""
    # P3-S44: prefer the caller's known dewey category (mapped to a
    # catalog domain) for source routing; fall back to keyword detection
    # only when no usable hint was supplied.
    detected_domain: Optional[str] = _resolve_catalog_domain(domain) or _detect_query_domain(query)

    # If query is a URL, fetch directly
    if parsed_query.scheme.lower() in {"http", "https"} and parsed_query.netloc:
        try:
            results.append(_safe_direct_fetch(query))
        except Exception as exc:
            fetch_error = str(exc) or f"{type(exc).__name__}: direct fetch failed"
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "tools",
                    "web_fetch_direct_fetch_failed",
                    status="fail",
                    ok=False,
                    error=fetch_error,
                    query=query,
                )
            except Exception:
                pass
    else:
        # SOURCE ROUTING (operator 2026-07-08): Meta A picks the best API for the
        # specific topic Omega chose. A research/academic topic goes to arXiv
        # (real papers) FIRST; everything falls back to the Wikipedia + DDG path.
        # source="arxiv" is set by Meta A's _best_learning_source; anything else
        # (or absent) keeps the general behaviour.
        source = str(kwargs.get("source") or "").strip().lower()
        if source == "arxiv":
            results.extend(_search_arxiv(query, max_results=max_results))

        # PRIMARY: resolve the topic to a REAL article link via opensearch.
        # This is the reliable "topic -> actual link with information" step —
        # it succeeds for phrases AND squashed slugs that the exact-title +
        # full-text search backend misses (which returned ZERO live, leaving the
        # mind nothing to learn from).
        if len(results) < max_results:
            results.extend(_resolve_via_opensearch(query, limit=max_results - len(results)))

        # Supplements (only if opensearch did not fill the quota): the legacy
        # domain-topic anchor, raw full-text search, and the DDG instant-answer.
        if len(results) < max_results:
            query_lower = query.lower().strip()
            topic = None
            for domain, topics in _DOMAIN_TOPICS.items():
                if domain in query_lower or query_lower in domain:
                    topic = random.choice(topics)
                    break
            if topic:
                article = _fetch_wikipedia_article(topic)
                if article:
                    results.append(article)

        if len(results) < max_results:
            results.extend(_search_wikipedia(query, limit=max_results - len(results)))

        if len(results) < max_results:
            results.extend(_search_ddg(query, max_results=max_results - len(results)))

    # Dedupe by URL
    seen = set()
    deduped = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            deduped.append(r)

    # Pull credible-source hints for the query's domain. Empty list if
    # the query was a URL or no domain matched. The calling model uses
    # these to decide whether to direct-fetch a primary source on the
    # next turn (e.g. for "asyncio in python", the catalog returns
    # docs.python.org and peps.python.org; the model can then call
    # web_fetch(query="https://docs.python.org/3/library/asyncio.html")
    # to pull authoritative content).
    credible_sources = get_credible_sources(detected_domain) if detected_domain else []

    response = {
        "query": query,
        "results": deduped[:max_results],
        "credible_sources": credible_sources,
        "domain": detected_domain or "",
        "total_results": len(deduped),
    }
    if fetch_error:
        response["error"] = fetch_error

    log.info(
        "web_fetch: query='%s' domain=%s -> %d results, %d credible_sources",
        query[:50],
        detected_domain or "-",
        len(deduped),
        len(credible_sources),
    )
    try:
        from guardian.terminal_feedback import emit_phase, emit_signal

        emit_phase(
            "tools",
            "web_fetch",
            status="ok" if deduped else ("fail" if fetch_error else "empty"),
            summary=(
                f"query={query[:60]!r} "
                f"domain={detected_domain or '-'} "
                f"results={len(deduped)} "
                f"credible={len(credible_sources)} "
                f"sources={sorted({r.get('source', '?') for r in deduped})}"
            ),
            query_preview=query[:100],
        )
        emit_signal(
            "tools.web_fetch.summary",
            float(len(deduped)),
            domain=detected_domain or "",
            credible_source_count=len(credible_sources),
            total_after_dedupe=len(deduped),
            returned=len(response["results"]),
            primary_url=bool(parsed_query.scheme.lower() in {"http", "https"} and parsed_query.netloc),
            narrative="web_fetch complete",
        )
    except Exception:
        pass
    return response
