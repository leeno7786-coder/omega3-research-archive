"""P3-S44 component A: dewey_map.category_for_domain.

Maps a fine dewey subdomain to the coarse category the search/source
catalog is keyed on, so a learning dispatch against 'acoustics' can
route to the 'science' curated source while the wiki search still uses
the precise subdomain. Coding subdomains map to their web_sources
catalog domain (language/concern group).
"""

from memory.dewey_map import category_for_domain


def test_knowledge_subdomain_maps_to_coarse_category():
    # The code-owned KNOWLEDGE_CATEGORY_KEYS Y-index is stable:
    # 0=science 1=mathematics 2=philosophy 3=technology 4=engineering
    # 5=humanities 6=social_science 7=medicine 8=world 9=synthesis
    assert category_for_domain("acoustics") == "science"  # Y=0
    assert category_for_domain("optics") == "science"  # Y=0
    assert category_for_domain("topology") == "mathematics"  # Y=1
    assert category_for_domain("epistemology") == "philosophy"  # Y=2
    assert category_for_domain("machine_learning") == "technology"  # Y=3
    assert category_for_domain("materials_science") == "engineering"  # Y=4
    assert category_for_domain("pharmacology") == "medicine"  # Y=7


def test_coarse_category_passes_through():
    # A coarse category name is already a category.
    assert category_for_domain("science") == "science"
    assert category_for_domain("mathematics") == "mathematics"


def test_coding_subdomain_maps_to_catalog_domain():
    assert category_for_domain("python_async", coding=True) == "python"
    assert category_for_domain("python_ml", coding=True) == "python"
    assert category_for_domain("rust_systems", coding=True) == "rust"
    assert category_for_domain("typescript_react", coding=True) == "typescript"
    assert category_for_domain("go_concurrency", coding=True) == "go"
    assert category_for_domain("devops_docker", coding=True) == "devops"


def test_unknown_domain_returns_none():
    assert category_for_domain("not_a_real_domain_xyzzy") is None
    assert category_for_domain("") is None


def test_every_dispatchable_subdomain_resolves():
    """P3-S44 invariant: every lobe-2 rotation subdomain and every coding
    subdomain resolves to a real category — nothing routes to a
    non-existent area. Guards against a subdomain being added to the
    rotation/taxonomy without a category mapping."""
    from memory.dewey_map import SUBDOMAIN_MAP, CODING_DOMAINS, KNOWLEDGE_CATEGORY_KEYS

    lobe2 = [n for (x, _y), n in SUBDOMAIN_MAP.items() if x == 2]
    unresolved = [n for n in lobe2 if category_for_domain(n) is None]
    assert not unresolved, f"lobe-2 subdomains with no category: {unresolved}"

    coding_unresolved = [n for n in CODING_DOMAINS if not category_for_domain(n, coding=True)]
    assert not coding_unresolved, f"coding subdomains with no category: {coding_unresolved}"

    # Every resolved knowledge category is a real catalog key.
    for n in lobe2:
        assert category_for_domain(n) in KNOWLEDGE_CATEGORY_KEYS
