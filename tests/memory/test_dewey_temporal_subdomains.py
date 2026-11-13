# F:/Omega3.0/tests/memory/test_dewey_temporal_subdomains.py
"""The (9,4) capability subdomain exists and resolves to a mnemonic dir name
(parity with (9,3) history) — so capability records written by
temporal_store.write_capability_to_temporal land in 900_temporal/capability."""

from memory.dewey_map import SUBDOMAIN_MAP, resolve_path


def test_capability_subdomain_9_4_named():
    assert SUBDOMAIN_MAP.get((9, 4)) == "capability"


def test_resolve_path_9_4_is_capability():
    assert resolve_path([9, 4, 0]).replace("\\", "/").endswith("900_temporal/capability")
