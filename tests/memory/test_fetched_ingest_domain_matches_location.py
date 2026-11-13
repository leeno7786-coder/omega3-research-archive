"""A fetched-knowledge record's stamped domain IS where it is filed.

OPERATOR RULING 2026-08-02: "it needs to match exactly where its recorded."

Live defect. A LEARN episode fetched a Machine Learning article and committed it
to ``memory/200_knowledge/coding/coding/fetched_knowledge/2.04.c7d7d0f0.json``:

    domain : "coding"          <- stamped on the record
    coord  : [2, 4, 0]         <- Y=4, a subdomain minted from that same string
    tags   : [fetched_knowledge, "world"]

Three names for one thing, none of them agreeing. ``coding`` is a LOBE name, not
a topic, and it is a key in neither ``KNOWLEDGE_DOMAINS`` nor ``CODING_DOMAINS``.
Nothing checked, so the dynamic-Y allocator minted it as a brand-new subdomain of
KNOWLEDGE -- a sibling of the real onboarding curriculum (``machine_learning``
Y=0, ``statistics`` Y=1, ``deep_learning`` Y=2, ``data_pipelines`` Y=3).

The article's correct home ALREADY EXISTED: ``machine_learning`` was allocated at
05:42:00, two and a half minutes before the misroute at 05:44:35.

Root shape: ``ingest_fetched_knowledge`` resolves the domain field TWICE and
independently -- once into ``payload["domain"]`` and once into ``_coord_for()``.
Two resolutions can disagree, so eventually they do. The cure is one resolution
that returns both, making the disagreement unrepresentable rather than merely
detectable.
"""

from __future__ import annotations

import importlib

import pytest

from memory import episode_sram


def _setup_omega_root(tmp_path, monkeypatch):
    root = tmp_path / "omega_root"
    root.mkdir(exist_ok=True)
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    # Pin the flag to the CONFIGURED production value. dynamic_y_lobes() reads
    # state under OMEGA_ROOT, so a tmp root reports the flag OFF and the resolver
    # would take the static-map branch — the opposite of the live regime these
    # tests exist to pin (doctrine: a RED test asserts the configured value).
    import memory.subdomain_flag as subdomain_flag

    monkeypatch.setattr(subdomain_flag, "dynamic_y_lobes", lambda: frozenset({2, 8}))
    return root


# ── the name-derivation regression ────────────────────────────────────────────
def test_derive_name_is_idempotent_on_an_already_canonical_key():
    """``_derive_name`` must not mangle a name it has already produced.

    Index keys are stored in derived form (``machine_learning``). Feeding one
    back in returns ``machinelearning`` -- a DIFFERENT key -- so any caller
    holding a canonical domain mints a duplicate phantom of a subdomain that
    already exists. Derivation has to be a fixed point or the index forks.
    """
    from memory.knowledge_router import _derive_name

    once = _derive_name("Machine Learning", None)
    assert once == "machine_learning"
    assert _derive_name(once, None) == once, (
        "derivation must be idempotent -- re-deriving a canonical key must not "
        "change it, or every canonical caller forks the subdomain index"
    )


@pytest.mark.parametrize("spelling", ["Machine Learning", "machine-learning", "machine_learning"])
def test_every_spelling_of_a_domain_resolves_to_the_same_subdomain(
    tmp_path, monkeypatch, spelling
):
    """One topic, one Y -- however the caller spells it.

    The episode records its domain hyphenated (``domain=machine-learning``, see
    guardian/logs/omega.jsonl 05:48:41) while the index key is underscored. If
    spelling decides identity, one topic scatters across several Y slots and
    recall can only ever find the fragment it happens to name.
    """
    _setup_omega_root(tmp_path, monkeypatch)
    fetched_ingest = importlib.import_module("memory.fetched_ingest")

    first, _coord = fetched_ingest.resolve_domain_and_coord(2, "Machine Learning")
    again, _coord2 = fetched_ingest.resolve_domain_and_coord(2, spelling)
    assert again == first, f"{spelling!r} must resolve to the same subdomain as 'Machine Learning'"


# ── a lobe name is not a topic ────────────────────────────────────────────────
@pytest.mark.parametrize("lobe_name", ["coding", "knowledge", "world"])
def test_a_lobe_name_is_refused_as_a_domain(tmp_path, monkeypatch, lobe_name):
    """``coding`` names a LOBE. It can never also name a subdomain inside one.

    Accepting it minted ``200_knowledge/coding/`` -- a folder asserting that
    coding is a kind of knowledge topic, sitting beside machine_learning and
    statistics. Fail closed: the caller passed the wrong field, and a learning
    action filed under a nonsense name is not a success worth keeping quiet
    about (§2 no-silent-empty; this module's own "MEMORY IS FIRST AND TRUTH").
    """
    _setup_omega_root(tmp_path, monkeypatch)
    fetched_ingest = importlib.import_module("memory.fetched_ingest")

    with pytest.raises(ValueError, match="lobe name|not a domain|illegal domain"):
        fetched_ingest.resolve_domain_and_coord(2, lobe_name)


def test_an_empty_domain_does_not_mint_a_subdomain(tmp_path, monkeypatch):
    """An absent domain is missing information, not a topic called 'knowledge'.

    ``_coord_for`` seeded the allocator with ``domain or "knowledge"``, so a
    blank domain minted a subdomain literally named ``knowledge`` inside the
    knowledge lobe.
    """
    _setup_omega_root(tmp_path, monkeypatch)
    fetched_ingest = importlib.import_module("memory.fetched_ingest")

    with pytest.raises(ValueError):
        fetched_ingest.resolve_domain_and_coord(2, "")


# ── the invariant itself ──────────────────────────────────────────────────────
def test_the_stamped_domain_is_the_directory_the_coord_points_at(tmp_path, monkeypatch):
    """label == location, structurally.

    One call returns both, so nothing downstream can stamp one value and file
    under another. This is the operator's ruling expressed as a contract.
    """
    _setup_omega_root(tmp_path, monkeypatch)
    fetched_ingest = importlib.import_module("memory.fetched_ingest")
    allocator = importlib.import_module("memory.subdomain_allocator")

    domain, coord = fetched_ingest.resolve_domain_and_coord(2, "Machine Learning")
    assert coord[0] == 2
    y, dirname = allocator.get_or_create_subdomain(
        lobe=2, seed_text=domain, title=domain, dynamic_y_lobes={2, 8},
    )
    assert coord[1] == y, "the coord must point at the subdomain the stamped domain names"
    assert domain == dirname, "the stamped domain must BE the directory name it is filed under"


def test_ingest_stamps_the_resolved_domain_not_the_caller_string(tmp_path, monkeypatch):
    """The record carries the resolved domain, never the raw caller argument.

    The live record was stamped ``coding`` -- the caller's string -- while its
    coordinate came from a separately-minted Y. Whatever resolution decides, the
    payload must agree with it.
    """
    _setup_omega_root(tmp_path, monkeypatch)
    fetched_ingest = importlib.import_module("memory.fetched_ingest")

    episode_sram.reset()
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    try:
        fetched_ingest.ingest_fetched_knowledge(
            "ep-1",
            learned="Machine Learning is a branch of AI concerned with statistical algorithms.",
            sources=[
                {
                    "name": "Machine learning",
                    "url": "https://en.wikipedia.org/wiki/Machine_learning",
                }
            ],
            domain="machine-learning",
        )
        envelope = episode_sram.pending()[0].envelope
    finally:
        episode_sram.reset()

    resolved, coord = fetched_ingest.resolve_domain_and_coord(2, "machine-learning")
    assert envelope.payload["domain"] == resolved
    assert envelope.coord == coord


def test_model_authored_lobe_name_domain_falls_back_to_content(monkeypatch):
    """The comprehension model sometimes names a LOBE as the domain ('coding',
    live crash 2026-08-10) — the model's word choice must never crash
    cognition. The ingest re-derives the topic from the learned content."""
    import memory.fetched_ingest as fi

    calls: list[str] = []

    def fake_resolve(lobe, domain):
        calls.append(domain)
        if domain == "coding":
            raise ValueError("illegal domain 'coding': that is a lobe name")
        return "rust_memory_management", [8, 4, 0]

    monkeypatch.setattr(fi, "resolve_domain_and_coord", fake_resolve)
    monkeypatch.setattr(
        "memory.routing.coding_vs_knowledge.classify_memory_record_lobe",
        lambda record: 8,
    )
    seen = {}

    def fake_intake(envelope, **kwargs):
        seen["envelope"] = envelope

        class R:
            validation_status = "committed"
            path = "/tmp/x.json"
            message = ""

        return R()

    monkeypatch.setattr("memory.api.intake_memory", fake_intake)
    monkeypatch.setattr("memory.api.intake_receipt_accepted", lambda r: True)

    fi.ingest_fetched_knowledge(
        "ep-fallback",
        learned="Rust ownership moves values; borrows never outlive owners.",
        sources=[{"name": "src", "url": "https://example.com/rust"}],
        domain="coding",
        kind_hint="memory management",
    )

    assert calls[0] == "coding"  # model's word tried first
    assert calls[1].startswith("memory management Rust ownership")  # content fallback
    assert seen["envelope"].payload["domain"] == "rust_memory_management"
