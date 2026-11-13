import importlib
from pathlib import Path

import pytest


def _setup_omega_root(tmp_path, monkeypatch):
    root = tmp_path / "omega_root"
    root.mkdir(exist_ok=True)
    import core.paths as paths

    # Patch the runtime OMEGA_ROOT so allocator/routers write under tmp
    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    return root


@pytest.mark.parametrize("lobe,lobe_dir", [(2, "200_knowledge"), (8, "800_coding")])
def test_dynamic_y_allocation_enabled_creates_subdomain(tmp_path, monkeypatch, lobe, lobe_dir):
    """With `dynamic_y_lobes` including the lobe, allocator must create
    a new Y-subdomain directory and return a stable dirname derived via the
    same name-derivation used by `memory.knowledge_router`.

    This is a RED-first test: the module `memory.subdomain_allocator` is
    expected to be implemented later. Running this now should fail (red).
    """
    omega_root = _setup_omega_root(tmp_path, monkeypatch)

    # Import the name-derivation helper after OMEGA_ROOT is patched.
    knowledge_router = importlib.import_module("memory.knowledge_router")

    # The allocator module is the future implementation (RED-first)
    allocator = importlib.import_module("memory.subdomain_allocator")

    # Derive the expected dirname using the upstream helper so names align
    dirname = knowledge_router._derive_name("Unique content about X", "X Title")

    y_index, created_dirname = allocator.get_or_create_subdomain(
        lobe=lobe,
        seed_text="Unique content about X",
        title="X Title",
        dynamic_y_lobes={2, 8},
    )

    assert created_dirname == dirname
    expected_dir = omega_root / "memory" / lobe_dir / dirname
    assert expected_dir.exists(), f"expected {expected_dir} to exist after allocation"


def test_dynamic_y_allocation_disabled_raises(tmp_path, monkeypatch):
    """When `dynamic_y_lobes` does NOT include the lobe, allocator must NOT
    create new Y-subdomains and should raise the explicit disabled error
    (both gate directions asserted).

    This is a RED-first test: it will fail until the allocator implements
    `DynamicSubdomainDisabledError` and the guarded behavior.
    """
    omega_root = _setup_omega_root(tmp_path, monkeypatch)

    importlib.import_module("memory.knowledge_router")
    allocator = importlib.import_module("memory.subdomain_allocator")

    with pytest.raises(allocator.DynamicSubdomainDisabledError):
        allocator.get_or_create_subdomain(
            lobe=2,
            seed_text="Some other content",
            title="Other",
            dynamic_y_lobes=set(),  # dynamic disabled
        )


def test_allocator_properties_first_idempotent_monotonic_persistent(tmp_path, monkeypatch):
    """Strengthened RED assertions:
    - first allocation in a blank lobe returns y == 0
    - idempotency: same seed twice -> identical (y, dirname) and next_y did not advance
    - monotonic: distinct seed produces incremented y
    - persistence: reloading index returns same y for same seed
    """
    omega_root = _setup_omega_root(tmp_path, monkeypatch)
    import memory.subdomain_allocator as allocator

    dynamic_flag = {2, 8}

    # Use lobe 2 (blank in this temp root)
    y1, dn1 = allocator.get_or_create_subdomain(
        lobe=2, seed_text="Alpha content", title="Alpha", dynamic_y_lobes=dynamic_flag
    )
    assert y1 == 0, "first allocation should return y == 0"

    # idempotency: repeat allocation for same seed
    y1b, dn1b = allocator.get_or_create_subdomain(
        lobe=2, seed_text="Alpha content", title="Alpha", dynamic_y_lobes=dynamic_flag
    )
    assert (y1b, dn1b) == (y1, dn1)

    # monotonic: a new distinct seed should get y == 1
    y2, dn2 = allocator.get_or_create_subdomain(
        lobe=2, seed_text="Beta content", title="Beta", dynamic_y_lobes=dynamic_flag
    )
    assert y2 == 1, "distinct seed should get next monotonic y == 1"

    # persistence: reload the index and ensure same mapping is returned
    # (simulate new process by re-importing the loader)
    idx = allocator._load_index()
    lob = idx.get("lobes", {}).get(str(2), {})
    subs = lob.get("subdomains", {})
    assert subs.get(dn1, {}).get("y") == y1
    assert subs.get(dn2, {}).get("y") == y2


# ── index corruption is fatal, never "rebuild empty" ────────────────────────


def _flag_lobes_dynamic(lobes=(2, 8)):
    """Flip the GLOBAL dynamic-lobe override, the way seed_first_wake does.

    get_or_create_subdomain takes the flag set as an ARGUMENT while
    resolve_subdomain_dir_name consults the global dynamic_y_lobes(). Both must
    agree or a coord allocated dynamically resolves through the static map (a
    "python" subdomain read back as "python_general") — so a test that only
    passes the argument is testing a regime the live system never runs in.
    """
    import memory.subdomain_flag as flag

    flag.reset_flag_cache()
    return flag.set_dynamic_y_lobes(list(lobes))


def test_unreadable_index_raises_instead_of_rebuilding_empty(tmp_path, monkeypatch):
    """THE 2026-08-15 onboarding terminus crash.

    A parse failure used to log a warning and return an EMPTY index. On the read
    path that turns every flagged coordinate into
        KeyError: dynamic subdomain not found for flagged lobe coord (8,0)
    which is exactly how the onboarding episode died in episode_sram_flush,
    taking the pressure loop down with it.

    On the WRITE path it is worse: get_or_create_subdomain loads, mutates and
    saves, so one transient read error overwrites the real index with an empty
    one — every mapping gone, next_y reset to 0, and the memories filed under
    the old y=0 orphaned.
    """
    _setup_omega_root(tmp_path, monkeypatch)
    allocator = importlib.import_module("memory.subdomain_allocator")
    _flag_lobes_dynamic()

    # A real subdomain exists...
    y, dirname = allocator.get_or_create_subdomain(
        lobe=8, seed_text="Python", title="Python", dynamic_y_lobes={2, 8}
    )
    assert allocator.resolve_subdomain_dir_name(8, y) == dirname

    # ...and then the index becomes unreadable.
    idx_path = allocator._index_path()
    idx_path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(allocator.DynamicSubdomainIndexCorrupt):
        allocator._load_index()

    # The file is NOT silently replaced with an empty index.
    assert idx_path.read_text(encoding="utf-8") == "{ this is not json"


def test_a_corrupt_index_never_reallocates_an_occupied_y(tmp_path, monkeypatch):
    """The data-loss half: a rebuild-empty would hand y=0 to a second dirname."""
    _setup_omega_root(tmp_path, monkeypatch)
    allocator = importlib.import_module("memory.subdomain_allocator")

    first_y, first_name = allocator.get_or_create_subdomain(
        lobe=8, seed_text="Python", title="Python", dynamic_y_lobes={2, 8}
    )
    allocator._index_path().write_text("", encoding="utf-8")

    with pytest.raises(allocator.DynamicSubdomainIndexCorrupt):
        allocator.get_or_create_subdomain(
            lobe=8, seed_text="Rust", title="Rust", dynamic_y_lobes={2, 8}
        )
    assert first_y == 0 and first_name  # the original mapping is untouched


def test_absent_index_is_a_legitimately_empty_first_run(tmp_path, monkeypatch):
    """Absent != unreadable. day_zero clears the index back to dark, and a
    genuine first run must still allocate from scratch."""
    _setup_omega_root(tmp_path, monkeypatch)
    allocator = importlib.import_module("memory.subdomain_allocator")
    _flag_lobes_dynamic()

    assert not allocator._index_path().exists()
    assert allocator._load_index()["lobes"] == {}

    y, dirname = allocator.get_or_create_subdomain(
        lobe=8, seed_text="Python", title="Python", dynamic_y_lobes={2, 8}
    )
    assert y == 0 and allocator.resolve_subdomain_dir_name(8, 0) == dirname
