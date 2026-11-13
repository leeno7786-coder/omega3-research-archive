"""Deterministic tool dispatch — same order → same routing params."""

from runtime.tool_dispatch import (
    coarse_category_for_subdomain,
    deterministic_args_from_order,
)


def test_coarse_category_strict_no_coding_catchall():
    assert coarse_category_for_subdomain("cryptography") == "technology"
    assert coarse_category_for_subdomain("not_a_real_domain_xyz") is None


def test_deterministic_web_fetch_args_from_order_domain():
    bound = deterministic_args_from_order(
        "web_fetch",
        "aerospace_engineering",
        {"query": "aerospace engineering"},
    )
    assert bound["domain"] == "engineering"
    assert bound["max_results"] == 3
    assert bound["query"] == "aerospace engineering"
