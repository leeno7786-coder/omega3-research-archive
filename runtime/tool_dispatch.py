"""
Deterministic tool dispatch helpers (GLOBAL → META → AUTO).

Ownership:
  - META selects tool + carries real ``synthesis["domain"]`` (subdomain).
  - AUTO binds routing params from the meta order; the model may only
    choose the web_fetch ``query`` string at execution time.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, TypeVar

from memory.dewey_map import category_for_domain

log = logging.getLogger("omega.runtime.tool_dispatch")

T = TypeVar("T")

# Params the model may influence on web_fetch (query variants only).
_MODEL_QUERY_KEYS = frozenset({"query", "topic", "search_query", "domain_query"})

# Optional routing params AUTO binds from the meta order (not the model).
_BINDABLE_OPTIONAL = ("domain", "max_results")


def coarse_category_for_subdomain(subdomain: str) -> Optional[str]:
    """Resolve a real dewey subdomain to its coarse category for web_fetch.

    Strict resolution only — no ``coding=True`` catch-all. This is the value
    bound in meta/auto orders; ``web_fetch`` maps it to a catalog domain.
    """
    d = (subdomain or "").strip().lower()
    if not d:
        return None
    return category_for_domain(d)


def deterministic_args_from_order(
    tool_name: str,
    order_domain: str,
    meta_bound: Dict[str, Any],
) -> Dict[str, Any]:
    """Build canonical execution args for AUTO from the meta order.

    ``meta_bound`` comes from ``orders.tool_calls[*].args`` (META assembly).
    For web_fetch, ensures ``domain`` (catalog routing) and ``max_results``
    are set deterministically from the order subdomain.
    """
    args = dict(meta_bound or {})
    if str(tool_name or "") != "web_fetch":
        return args
    sd = (order_domain or "").strip()
    if sd and not args.get("domain"):
        cat = coarse_category_for_subdomain(sd)
        if cat:
            args["domain"] = cat
    if "max_results" not in args:
        try:
            from pressure_loop.policy import auto_web_fetch_default_max_results

            args["max_results"] = auto_web_fetch_default_max_results()
        except Exception:
            args["max_results"] = 3
    return args
