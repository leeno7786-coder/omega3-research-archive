"""
test_web_sources_config.py — curated source catalog
=====================================================

Verifies the config/web_sources.yaml catalog + its consumer surface in
tools.web_fetch. Per the operator's curation directive (2026-05-07):

    A. Catalog loads cleanly via config.loader.load_web_sources_config
       and all required fields are present per entry.
    B. Wikipedia is in the `defer` tier (per policy: don't lean on it)
       and at least 5 primary-tier sources exist (so the lean is real).
    C. tools.web_fetch.get_credible_sources filters by domain + tier
       ceiling correctly.
    D. tools.web_fetch._detect_query_domain routes representative
       queries to the right configured domain.
    E. tools.web_fetch.web_fetch returns the new credible_sources
       field and the right shape for both URL-form and text queries.

This is OFFLINE — no network. URL liveness is verified by
scripts/verify_web_sources.py (the credit-check, run before any URL
lands in the catalog).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{label}  {detail}")


def section(label: str) -> None:
    print()
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)


# =====================================================================
# A. Catalog loads + schema
# =====================================================================
section("A. config/web_sources.yaml — schema + load")

from config.loader import load_web_sources_config

cfg = load_web_sources_config()
check("A1.a load_web_sources_config returns a dict", isinstance(cfg, dict))

check(
    "A1.b schema_version is 1",
    cfg.get("schema_version") == 1,
    detail=f"got {cfg.get('schema_version')!r}",
)

sources = cfg.get("sources") or []
check(
    "A1.c sources is a non-empty list",
    isinstance(sources, list) and len(sources) >= 20,
    detail=f"got {len(sources)} entries",
)

tier_order = cfg.get("tier_order") or []
check(
    "A1.d tier_order is set",
    isinstance(tier_order, list) and len(tier_order) >= 3,
    detail=f"got {tier_order!r}",
)

domain_defaults = cfg.get("domain_defaults") or {}
check(
    "A1.e domain_defaults is set",
    isinstance(domain_defaults, dict) and len(domain_defaults) >= 3,
    detail=f"got {len(domain_defaults)} entries",
)

# Per-entry schema
required_fields = {"url", "name", "tier", "domain"}
bad_entries = [
    (i, s)
    for i, s in enumerate(sources)
    if not isinstance(s, dict) or not required_fields.issubset(s.keys())
]
check(
    "A2.a every source has url/name/tier/domain", not bad_entries, detail=f"bad={len(bad_entries)}"
)

# All tiers must be in the tier_order list
unknown_tier_entries = [s for s in sources if s.get("tier") not in tier_order]
check(
    "A2.b all entry tiers appear in tier_order",
    not unknown_tier_entries,
    detail=(
        f"unknown={[s.get('name') for s in unknown_tier_entries[:3]]}"
        if unknown_tier_entries
        else ""
    ),
)

# All URLs https
non_https = [s for s in sources if not (s.get("url") or "").startswith("https://")]
check(
    "A2.c all URLs are https://",
    not non_https,
    detail=(f"non_https={[s.get('url') for s in non_https]}" if non_https else ""),
)


# =====================================================================
# B. Operator policy: lean on primary, defer Wikipedia
# =====================================================================
section("B. Operator policy compliance")

primary_sources = [s for s in sources if s.get("tier") == "primary"]
check(
    "B1.a at least 5 primary-tier sources",
    len(primary_sources) >= 5,
    detail=f"got {len(primary_sources)}",
)

# Wikipedia must exist AND be in defer tier
wiki = [s for s in sources if "wikipedia" in (s.get("url") or "").lower()]
check("B1.b Wikipedia is present in catalog", len(wiki) >= 1, detail=f"found {len(wiki)} entries")

if wiki:
    check(
        "B1.c Wikipedia is in `defer` tier",
        all(s.get("tier") == "defer" for s in wiki),
        detail=f"tiers={[s.get('tier') for s in wiki]}",
    )

# tier_order: primary should come before defer
try:
    primary_idx = tier_order.index("primary")
    defer_idx = tier_order.index("defer")
    check(
        "B1.d tier_order puts primary before defer",
        primary_idx < defer_idx,
        detail=f"primary={primary_idx} defer={defer_idx}",
    )
except ValueError as exc:
    check("B1.d tier_order has both 'primary' and 'defer'", False, detail=str(exc))

# Multiple distinct domains covered (not just AI)
domains_covered = sorted({s.get("domain") for s in sources})
check(
    "B2.a at least 8 distinct domains covered",
    len(domains_covered) >= 8,
    detail=f"got {len(domains_covered)}: {domains_covered[:8]}...",
)

# Coding+AI specifically per operator directive
required_domains = {"ai_research", "python", "coding", "ml_frameworks"}
present = required_domains & set(domains_covered)
check(
    "B2.b coding + AI domains all represented",
    present == required_domains,
    detail=f"missing={sorted(required_domains - present)!r}",
)


# =====================================================================
# C. get_credible_sources — filtering surface
# =====================================================================
section("C. get_credible_sources filtering")

from schema.tools.web_fetch import get_credible_sources

# Domain filter
py = get_credible_sources("python")
check("C1.a get_credible_sources('python') returns >=1", len(py) >= 1, detail=f"got {len(py)}")
check(
    "C1.b all returned sources have domain='python'", all(s.get("domain") == "python" for s in py)
)

# Tier ceiling — primary only
ai_primary = get_credible_sources("ai_research", tier_ceiling="primary")
check(
    "C1.c tier_ceiling='primary' includes only primary",
    all(s.get("tier") == "primary" for s in ai_primary),
    detail=f"tiers={sorted({s.get('tier') for s in ai_primary})!r}",
)

# Tier ceiling — academic includes primary + academic
ai_academic = get_credible_sources("ai_research", tier_ceiling="academic")
check(
    "C1.d tier_ceiling='academic' includes primary + academic",
    set(s.get("tier") for s in ai_academic) <= {"primary", "academic"},
    detail=f"tiers={sorted({s.get('tier') for s in ai_academic})!r}",
)

# limit honored
truncated = get_credible_sources("ai_research", tier_ceiling="defer", limit=2)
check("C1.e limit truncates result list", len(truncated) <= 2, detail=f"got {len(truncated)}")

# Unknown domain returns []
mystery = get_credible_sources("__nonexistent_domain__")
check("C1.f unknown domain returns empty list", mystery == [], detail=f"got {mystery!r}")


# =====================================================================
# D. Domain detection — keyword routing
# =====================================================================
section("D. _detect_query_domain — keyword routing")

from schema.tools.web_fetch import _detect_query_domain

cases = [
    ("asyncio in python", "python"),
    ("rust borrow checker", "rust"),
    ("docker compose v2", "devops"),
    ("transformer attention paper", "ai_research"),
    ("philosophy of mind", "philosophy"),
    ("typescript generics", "typescript"),
    ("pytorch dataloader", "ml_frameworks"),
    ("https://example.com/x", None),  # URL form bypasses catalog
    ("", None),
]
for q, expected in cases:
    got = _detect_query_domain(q)
    check(
        f"D1 detect_query_domain({q!r:35s})",
        got == expected,
        detail=f"expected={expected!r} got={got!r}",
    )


# =====================================================================
# E. web_fetch returns credible_sources field
# =====================================================================
section("E. web_fetch — credible_sources surface (offline-only checks)")

from schema.tools.web_fetch import web_fetch
import unittest.mock as mock
import logging

log = logging.getLogger("omega.tests.runtime.test_web_sources_config")


# Stub every network entry point so this test stays offline. The opensearch resolver (text
# queries) and the direct fetch (URL queries) were unstubbed, so the "offline" script made
# real requests to Wikipedia (found 2026-09-15).
with (
    mock.patch("schema.tools.web_fetch._search_wikipedia", return_value=[]),
    mock.patch("schema.tools.web_fetch._search_ddg", return_value=[]),
    mock.patch("schema.tools.web_fetch._fetch_wikipedia_article", return_value=None),
    mock.patch("schema.tools.web_fetch._resolve_via_opensearch", return_value=[]),
    mock.patch(
        "schema.tools.web_fetch._safe_direct_fetch",
        side_effect=lambda url: {"url": url, "title": url[:80], "content": "offline stub", "source": "direct"},
    ),
):
    d = web_fetch("asyncio in python", max_results=3)

    check(
        "E1.a response is a dict (LLM-protocol boundary serializes)",
        isinstance(d, dict),
        detail=f"type={type(d).__name__}",
    )
    check("E1.a response has 'credible_sources' field", "credible_sources" in d)
    check(
        "E1.b response has 'domain' field", "domain" in d, detail=f"got domain={d.get('domain')!r}"
    )
    check(
        "E1.c domain detected as 'python'",
        d.get("domain") == "python",
        detail=f"got {d.get('domain')!r}",
    )
    check(
        "E1.d credible_sources is non-empty for python query",
        len(d.get("credible_sources") or []) >= 1,
        detail=f"got {len(d.get('credible_sources') or [])}",
    )
    if d.get("credible_sources"):
        sample = d["credible_sources"][0]
        check(
            "E1.e each credible_source has url+name+tier+domain+note",
            all(k in sample for k in ("url", "name", "tier", "domain", "note")),
            detail=f"keys={sorted(sample.keys())!r}",
        )

    # URL-form query: credible_sources should be [] (URL bypasses catalog)
    url_d = web_fetch("https://en.wikipedia.org/wiki/Photosynthesis")
    check(
        "E2.a URL-form query returns credible_sources=[]",
        url_d.get("credible_sources") == [],
        detail=f"got {url_d.get('credible_sources')!r}",
    )
    check(
        "E2.b URL-form query returns domain=''",
        url_d.get("domain") == "",
        detail=f"got {url_d.get('domain')!r}",
    )


# =====================================================================
# F. web_fetch schema artifacts describe the current response shape
# =====================================================================
section("F. web_fetch schema artifacts")

schema_path = ROOT / "schema" / "tools" / "web_fetch.json"
tool_schema = json.loads(schema_path.read_text(encoding="utf-8"))
output_props = (tool_schema.get("returns_schema") or {}).get("properties") or {}
credible_source_props = (
    (output_props.get("credible_sources") or {}).get("items", {}).get("properties", {})
)

check("F1.a tool JSON returns_schema includes domain", "domain" in output_props)
check("F1.b tool JSON returns_schema includes credible_sources", "credible_sources" in output_props)
check(
    "F1.c credible_sources item schema has expected keys",
    all(k in credible_source_props for k in ("url", "name", "tier", "domain", "note")),
    detail=f"keys={sorted(credible_source_props)}",
)


# =====================================================================
# Summary
# =====================================================================

print()
print("=" * 70)
print(f"  PASSED: {PASS}")
print(f"  FAILED: {FAIL}")
if FAILURES:
    print()
    print("FAILURES:")
    for f in FAILURES:
        print(f"  {f}")
print("=" * 70)

raise SystemExit(0 if FAIL == 0 else 1)
