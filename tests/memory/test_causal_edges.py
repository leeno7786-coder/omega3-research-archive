"""T11: typed causal edges — host-committed facts with their own durable records, projected
into the graph at rebuild.

Cross-lane cognitive integration spec, section 9. Pinned here: the fact writer's rules; each
fact as its OWN intake record (durable at acceptance, deduplicated by a fold-stable event id,
recovered by boot adoption like any pending intake, drained to the Dewey filesystem by the
real SRAM flush); the extractor; and the REAL compiler projecting a canonical causal fact into
a ``MemoryEdge`` whose evidence is the accepting episode's own event. The write sites
themselves are exercised with real folds in ``tests/omega_loop/test_cognitive_transaction.py``
(section 13), ``tests/core/test_window_lane.py`` and
``tests/core/test_cognition_chain_config_wiring.py``.
"""

import json
from types import SimpleNamespace

import pytest

import memory.taxonomy.graph_store as G
from memory import causal_edges, episode_sram, intake_journal
from memory.taxonomy.compiler import compile_memory_graph
from memory.taxonomy.extract import extract_from_event
from memory.taxonomy.schemas import MemoryEvent


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(intake_journal, "journal_root", lambda: tmp_path / "intake")
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    yield
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()


def _episode(episode_id="ep-1", pipeline_id=0):
    return SimpleNamespace(episode_id=episode_id, pipeline_id=pipeline_id, meta={})


def _in_bucket(episode, action):
    if not episode_sram.is_open(episode.episode_id):
        episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
        return action()


def _record(receipt):
    return intake_journal.record_by_id(receipt["journal_record_id"])


def _restart():
    """The process died: nothing in memory survives; the journal on disk does."""
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()


FACT = {"kind": "modified", "source": "mail:m-1", "target": "intake:r-1",
        "detail": {"purpose": "operator_instruction"}}


# ── the fact writer ───────────────────────────────────────────────────────────

def test_a_fact_is_its_own_intake_record_with_the_episodes_event_as_evidence():
    episode = _episode()

    receipt = _in_bucket(episode, lambda: causal_edges.commit_causal_fact(episode, **FACT))

    assert receipt["accepted"] and receipt["status"] == "held" and not receipt["replayed"]
    assert receipt["event_id"] == f"causal:{receipt['fact_id']}"
    assert receipt["detail"] == {"purpose": "operator_instruction"}
    record = _record(receipt)
    assert record.status == intake_journal.PENDING and record.owner_episode_id == "ep-1"
    enrichment = record.payload["payload"]["enrichment"]
    assert (enrichment["canonical"], enrichment["event_kind"]) == (True, "causal_edge")
    assert enrichment["event_payload"] == {
        "fact_id": receipt["fact_id"], "kind": "modified", "source": "mail:m-1",
        "target": "intake:r-1", "evidence_event_id": "episode-ep-1",
        "detail": {"purpose": "operator_instruction"}}
    assert record.payload["source"] == causal_edges.SOURCE_TAG and record.payload["coord"][0] == 4
    assert causal_edges.causal_facts(episode) == [receipt], "noted on the carrier; durable in the journal"


def test_the_facts_identity_is_the_triple_not_the_episode():
    assert causal_edges.fact_id_for("modified", "mail:m", "intake:r") == \
        causal_edges.fact_id_for("modified", "mail:m", "intake:r")
    assert causal_edges.fact_id_for("modified", "mail:m", "intake:r") != \
        causal_edges.fact_id_for("informed", "mail:m", "intake:r")
    assert causal_edges.fact_id_for("modified", "mail:m", "intake:r") != \
        causal_edges.fact_id_for("modified", "intake:r", "mail:m")


def test_the_same_fact_presented_again_is_one_record_recovered_into_the_presenters_bucket():
    """Replay, or a second fold establishing the same relationship, after the first presenter
    died: the record on file stands — its payload, its evidence, its event id — and nothing
    is duplicated; and because its owner is gone it is re-owned by the presenter and admitted
    into the presenter's bucket, so the presenter's drain commits it."""
    first = _episode("ep-1")
    original = _in_bucket(first, lambda: causal_edges.commit_causal_fact(first, **FACT))
    before = _record(original)
    _restart()
    second = _episode("ep-2", pipeline_id=1)

    again = _in_bucket(second, lambda: causal_edges.commit_causal_fact(second, **FACT))

    assert again["replayed"] and again["accepted"] and again["status"] == "held"
    assert again["journal_record_id"] == original["journal_record_id"]
    assert len(intake_journal.records()) == 1
    record = _record(again)
    assert (record.owner_episode_id, record.owner_pipeline_id, record.status) == ("ep-2", 1, intake_journal.PENDING)
    assert (record.payload, record.event_id, record.fingerprint) == (before.payload, before.event_id, before.fingerprint), \
        "the original fact, under the original evidence"
    assert [i.journal_record_id for i in episode_sram.pending("ep-2")] == [record.record_id]


def test_a_replay_receipt_reports_the_fact_as_recorded_not_as_proposed():
    """P3 (T11 review): the record on file stands under the FIRST presenter's evidence and
    detail. A replay that proposes the same triple with its own evidence and a different
    detail gets a receipt that agrees with the preserved record — on the carrier as in the
    journal — never one built from its proposal."""
    first = _episode("ep-1")
    original = _in_bucket(first, lambda: causal_edges.commit_causal_fact(first, **FACT))
    _restart()
    replay = _episode("ep-replay", pipeline_id=1)

    again = _in_bucket(replay, lambda: causal_edges.commit_causal_fact(
        replay, **dict(FACT, detail={"purpose": "operator_instruction", "statement_kind": "decision"})))

    recorded = _record(again).payload["payload"]["enrichment"]["event_payload"]
    assert again["evidence_event_id"] == "episode-ep-1" == recorded["evidence_event_id"]
    assert again["detail"] == {"purpose": "operator_instruction"} == recorded["detail"]
    assert {k: again[k] for k in ("fact_id", "kind", "source", "target")} == \
        {k: recorded[k] for k in ("fact_id", "kind", "source", "target")}
    assert causal_edges.causal_facts(replay) == [again]
    assert (again["journal_record_id"], again["replayed"]) == (original["journal_record_id"], True)


def test_a_first_presenters_receipt_is_its_own_proposal():
    episode = _episode()
    receipt = _in_bucket(episode, lambda: causal_edges.commit_causal_fact(episode, **FACT))
    assert not receipt["replayed"] and receipt["evidence_event_id"] == "episode-ep-1"
    assert receipt["detail"] == FACT["detail"]


def test_a_pending_fact_left_for_the_replay_is_drained_by_the_replay():
    """Reviewer's probe, verbatim: journal a fact, restart; boot reconciles with
    cascade_replay=True, intentionally leaving it for the replay; the replay presents it
    (accepted, held); drain the replay — the fact is committed, not stranded under the dead
    original episode."""
    first = _episode("ep-1")
    original = _in_bucket(first, lambda: causal_edges.commit_causal_fact(first, **FACT))
    _restart()
    report = intake_journal.reconcile_at_boot(episode_id="ep-1", pipeline_id=0, cascade_replay=True)
    assert (report["pending"], report["released"]) == (1, 0), "left for the replay, not released"

    replay = _episode("ep-replay", pipeline_id=0)
    again = _in_bucket(replay, lambda: causal_edges.commit_causal_fact(replay, **FACT))
    assert (again["accepted"], again["status"], again["journal_record_id"]) == (
        True, "held", original["journal_record_id"])
    result = episode_sram.close_bucket("ep-replay")

    assert not result.get("errors") and len(result["memories"]) == 1, result
    record = _record(original)
    assert record.status == intake_journal.COMMITTED and record.owner_episode_id == "ep-replay"
    assert len(intake_journal.records()) == 1


def test_a_fact_a_live_owner_still_holds_is_not_stolen():
    """Another live episode (its bucket open) already holds this fact pending: it will drain
    it. The presenter gets the record as it stands and takes nothing into its own bucket."""
    holder = _episode("ep-holder", pipeline_id=0)
    original = _in_bucket(holder, lambda: causal_edges.commit_causal_fact(holder, **FACT))
    other = _episode("ep-other", pipeline_id=1)

    again = _in_bucket(other, lambda: causal_edges.commit_causal_fact(other, **FACT))

    assert (again["journal_record_id"], again["status"], again["replayed"]) == (
        original["journal_record_id"], "held", True)
    assert _record(original).owner_episode_id == "ep-holder"
    assert episode_sram.pending("ep-other") == []
    assert [i.journal_record_id for i in episode_sram.pending("ep-holder")] == [original["journal_record_id"]]


@pytest.mark.parametrize("kind", ["caused", "", "INFORMED", None])
def test_only_the_five_kinds_are_facts(kind):
    with pytest.raises(causal_edges.CausalFactRefused, match="kind"):
        causal_edges.commit_causal_fact(_episode(), kind=kind, source="episode:a", target="memory:b")
    assert causal_edges.EDGE_KINDS == {"informed", "modified", "observed", "continued", "superseded"}


@pytest.mark.parametrize("reference", ["ep-1", "episode:", "model:whatever", "", None, "decision:x"])
def test_only_host_references_are_edged(reference):
    with pytest.raises(causal_edges.CausalFactRefused, match="prefixed host reference"):
        causal_edges.commit_causal_fact(_episode(), kind="informed", source=reference, target="intake:r1")
    with pytest.raises(causal_edges.CausalFactRefused, match="prefixed host reference"):
        causal_edges.commit_causal_fact(_episode(), kind="informed", source="mail:m1", target=reference)


def test_a_fact_needs_the_episode_it_is_evidenced_by():
    with pytest.raises(causal_edges.CausalFactRefused, match="episode"):
        causal_edges.commit_causal_fact(SimpleNamespace(episode_id="", meta={}), kind="observed",
                                        source="episode:a", target="window:w")


def test_a_batch_is_all_or_nothing():
    """A refusal while checking the LAST fact must not leave the earlier ones behind: every
    fact is checked before any is written."""
    episode = _episode()

    with pytest.raises(causal_edges.CausalFactRefused):
        _in_bucket(episode, lambda: causal_edges.commit_causal_facts(episode, [
            {"kind": "informed", "source": "mail:m1", "target": "intake:r1"},
            {"kind": "caused", "source": "mail:m2", "target": "intake:r2"}]))

    assert intake_journal.records() == () and causal_edges.causal_facts(episode) == []


def test_valid_inputs_never_exhaust_a_cap():
    """32 bound messages x 8 accepted decisions, plus constraints, a supersession and the
    continuation: committed whole. The bound is what memory accepted times the binder's rows."""
    episode = _episode()
    facts = [{"kind": "modified", "source": f"mail:m{m}", "target": f"intake:r{r}"}
             for m in range(32) for r in range(8)]
    facts += [{"kind": "modified", "source": f"mail:m{m}", "target": f"intake:c{r}"}
              for m in range(32) for r in range(8)]
    facts += [{"kind": "superseded", "source": "intake:r0", "target": "memory:old"},
              {"kind": "continued", "source": "episode:ep-1", "target": "continuation:s"}]

    receipts = _in_bucket(episode, lambda: causal_edges.commit_causal_facts(episode, facts))

    assert len(receipts) == 32 * 8 * 2 + 2 and all(r["accepted"] for r in receipts)
    assert len(intake_journal.records()) == len(receipts)


def test_a_carrier_with_a_foreign_meta_still_takes_the_note():
    episode = SimpleNamespace(episode_id="ep-1", pipeline_id=0, meta=None)
    _in_bucket(episode, lambda: causal_edges.commit_causal_fact(episode, kind="observed",
                                                                 source="episode:ep-1", target="window:w"))
    assert len(causal_edges.causal_facts(episode)) == 1


def test_the_carriers_note_reports_only_facts():
    """A carrier's meta is model-touched territory elsewhere: a row under the note key that
    is not a fact of a known kind is not a fact."""
    episode = _episode()
    receipt = _in_bucket(episode, lambda: causal_edges.commit_causal_fact(episode, **FACT))
    episode.meta[causal_edges.META_KEY] += ["junk", {"kind": "caused", "source": "a", "target": "b"},
                                            {"source": "mail:m", "target": "intake:r"}]

    assert causal_edges.causal_facts(episode) == [receipt]
    assert causal_edges.causal_facts({"meta": episode.meta}) == [receipt]


def test_a_fact_outside_any_bucket_is_refused_not_lost():
    """No SRAM bucket bound: the intake is refused up front, the same as any memory intake.
    Nothing is written, nothing is noted."""
    episode = _episode()
    with pytest.raises(causal_edges.CausalFactRefused, match="bucket") as raised:
        causal_edges.commit_causal_fact(episode, **FACT)
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "no episode SRAM bucket is open" in str(raised.value.__cause__)
    assert intake_journal.records() == () and causal_edges.causal_facts(episode) == []


# ── recovery: the fact's own record ───────────────────────────────────────────

def test_a_pending_fact_survives_boot_adoption_and_is_not_duplicated_on_replay():
    """Reviewer's probe: the fact was journalled, the process died before its terminus; boot
    released the pending record, another episode adopted and committed it. A replay that
    presents the fact again gets THAT record — committed — and records nothing new."""
    first = _episode("ep-1")
    original = _in_bucket(first, lambda: causal_edges.commit_causal_fact(first, **FACT))
    _restart()                                              # died before the terminus

    report = intake_journal.reconcile_at_boot(episode_id="ep-1", pipeline_id=0, cascade_replay=False)
    assert (report["pending"], report["released"]) == (1, 1)
    episode_sram.open_bucket("ep-adopter", pipeline_id=0)          # adopts what boot released
    [adopted] = episode_sram.pending("ep-adopter")
    assert adopted.journal_record_id == original["journal_record_id"]
    result = episode_sram.close_bucket("ep-adopter")               # the real drain commits it
    assert not result.get("errors") and len(result["memories"]) == 1, result
    _restart()

    replay = _episode("ep-replay", pipeline_id=1)
    again = _in_bucket(replay, lambda: causal_edges.commit_causal_fact(replay, **FACT))

    assert again["journal_record_id"] == original["journal_record_id"]
    assert (again["status"], again["replayed"], again["accepted"]) == ("committed", True, True)
    assert len(intake_journal.records()) == 1, "recovered by adoption, never duplicated"
    assert episode_sram.pending("ep-replay") == [], "a committed fact is not re-admitted"


def test_boot_adoption_carries_a_pending_fact_into_the_adopters_bucket():
    """The SRAM side of the same recovery: the next bucket opened on the pipeline adopts the
    released fact as intake, so the ordinary drain commits it."""
    first = _episode("ep-1")
    original = _in_bucket(first, lambda: causal_edges.commit_causal_fact(first, **FACT))
    _restart()
    intake_journal.reconcile_at_boot(episode_id="ep-1", pipeline_id=0, cascade_replay=False)

    episode_sram.open_bucket("ep-adopter", pipeline_id=0)

    [item] = episode_sram.pending("ep-adopter")
    assert item.journal_record_id == original["journal_record_id"] and item.kind == "memory"
    assert _record(original).owner_episode_id == "ep-adopter"


def test_a_fact_that_was_never_journalled_is_journalled_by_the_replay():
    """The first attempt died BEFORE the fact reached intake: nothing to adopt, so the replay
    that establishes the same relationship journals it — once."""
    replay = _episode("ep-replay")

    receipt = _in_bucket(replay, lambda: causal_edges.commit_causal_fact(replay, **FACT))

    assert not receipt["replayed"] and _record(receipt).owner_episode_id == "ep-replay"
    assert _record(receipt).payload["payload"]["enrichment"]["event_payload"]["evidence_event_id"] == \
        "episode-ep-replay", "evidenced by the episode that actually journalled it"


def test_the_real_drain_commits_a_fact_beside_its_statement():
    """Reviewer's probe: a committed statement beside a still-pending fact. Both are intake
    records of the same bucket, so the same flush drains both — and the fact lands on the
    Dewey filesystem as a canonical situational record the compiler reads."""
    from core.paths import MEMORY_ROOT
    from memory.api import CanonicalEnvelope, intake_memory

    episode = _episode("ep-1")

    def accept():
        statement = intake_memory(CanonicalEnvelope(
            cycle_id="ep-1-decision-0", coord=[4, 1, 0], type="decision",
            payload={"text": "Store todos in SQLite", "title": "decision", "summary": "Store todos"},
            source="omega_inference"), propose=False, bypass_guardian=True)
        fact = causal_edges.commit_causal_fact(
            episode, kind="modified", source="mail:m-1", target=f"intake:{statement.journal_record_id}")
        return statement, fact

    statement, fact = _in_bucket(episode, accept)
    assert _record(fact).status == intake_journal.PENDING

    result = episode_sram.close_bucket("ep-1")

    assert not result.get("errors") and len(result["memories"]) == 2, result
    assert intake_journal.record_by_id(statement.journal_record_id).status == intake_journal.COMMITTED
    fact_record = _record(fact)
    assert fact_record.status == intake_journal.COMMITTED and fact_record.canonical_path
    path = MEMORY_ROOT / fact_record.canonical_path
    if not path.exists():
        [path] = [p for p in MEMORY_ROOT.rglob("*.json") if p.name.startswith(fact_record.memory_id)]
    canonical = json.loads(path.read_text(encoding="utf-8"))
    enrichment = canonical["enrichment"]
    assert enrichment["canonical"] is True and enrichment["event_kind"] == "causal_edge"
    assert enrichment["event_payload"]["target"] == f"intake:{statement.journal_record_id}"
    assert enrichment["event_payload"]["evidence_event_id"] == "episode-ep-1"


# ── the projection ────────────────────────────────────────────────────────────

def _event(fact, episode_id="ep-9"):
    return MemoryEvent(event_id=f"causal-{fact.get('fact_id', 'x')}", event_kind="causal_edge",
                       episode_id=episode_id, source=causal_edges.SOURCE_TAG, created_at="",
                       content_hash="h", payload=dict(fact, evidence_event_id=f"episode-{episode_id}"))


def test_the_extractor_names_two_entities_and_one_claim_evidenced_by_the_episode():
    event = _event({"kind": "informed", "source": "mail:m-2", "target": "intake:r-2"})

    entities, claims = extract_from_event(event)

    assert [(e.entity_kind, e.canonical_name) for e in entities] == [("mail", "mail:m-2"),
                                                                     ("intake", "intake:r-2")]
    [c] = claims
    assert (c.subject, c.predicate, c.object) == ("mail:m-2", "informed", "intake:r-2")
    assert c.source_event_id == "episode-ep-9" and c.evidence_event_ids == ("episode-ep-9",)
    assert c.source_event_id != event.event_id, "the episode's event is the evidence, not the fact's"


def test_the_extractor_ignores_a_record_of_no_known_kind():
    assert extract_from_event(_event({"kind": "caused", "source": "episode:a", "target": "memory:b"})) == ([], [])


@pytest.mark.parametrize(
    "fact",
    [
        {"kind": "observed", "source": "model:x", "target": "window:w"},
        {"kind": "observed", "source": "episode:", "target": "window:w"},
        {"kind": "observed", "source": "episode:ep-9", "target": "window:"},
    ],
)
def test_the_extractor_ignores_a_record_with_invalid_host_references(fact):
    assert extract_from_event(_event(fact)) == ([], [])


def _canonical_causal_record(memory_root, *, mid, episode_id, fact):
    """A lobe-4 canonical record as the drain writes one from the fact's envelope."""
    record = {
        "id": mid, "coord": [4, 3, 0], "type": "situational",
        "title": f"Causal edge in {episode_id}", "summary": "causal edge", "text": "causal edge",
        "provenance": {"origin_id": causal_edges.SOURCE_TAG, "origin_type": "ingest",
                       "author": causal_edges.SOURCE_TAG, "ingested_at": "2026-09-17T12:00:00+00:00",
                       "confidence": 1.0},
        "enrichment": {"canonical": True, "episode_id": episode_id, "event_kind": "causal_edge",
                       "event_id": f"causal-{mid}", "event_source": causal_edges.SOURCE_TAG,
                       "event_payload": dict(fact, evidence_event_id=f"episode-{episode_id}")},
    }
    (memory_root / f"{mid}.json").write_text(json.dumps(record), encoding="utf-8")
    return {"coord": [4, 3, 0], "path": f"memory/{mid}.json"}


@pytest.mark.parametrize("kind, edge_type", [("informed", "INFORMED"), ("modified", "MODIFIED"),
                                             ("observed", "OBSERVED"), ("continued", "CONTINUED"),
                                             ("superseded", "SUPERSEDED")])
def test_a_rebuild_projects_a_canonical_causal_fact_as_a_memory_edge(kind, edge_type, tmp_path):
    """The REAL compiler, over a canonical record in the shape the drain writes: an edge row
    whose evidence is the episode's journal event. Nothing wrote graph.sqlite directly."""
    memory_root = tmp_path / "graph" / "memory"
    (memory_root / "indexes").mkdir(parents=True)
    index = {"f1": _canonical_causal_record(memory_root, mid="f1", episode_id="ep-9", fact={
        "kind": kind, "source": "episode:ep-9", "target": "memory:m-7"})}
    (memory_root / "indexes" / "coord_index.json").write_text(json.dumps(index), encoding="utf-8")

    compile_memory_graph(root=tmp_path / "graph")

    conn = G.connect(root=tmp_path / "graph")
    try:
        rows = conn.execute(
            "SELECT e.edge_type, e.evidence_event_id, s.label, t.label FROM edges e "
            "JOIN nodes s ON s.node_id = e.source_id JOIN nodes t ON t.node_id = e.target_id "
            "WHERE e.edge_type = ?", (edge_type,)).fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in rows] == [(edge_type, "episode-ep-9", "episode:ep-9", "memory:m-7")]


def test_capture_routes_a_causal_edge_to_the_situational_why_facet():
    from memory.taxonomy.canonical_capture import EVENT_KIND_TO_FACET, SITUATIONAL_FACETS

    assert EVENT_KIND_TO_FACET["causal_edge"] == "why" and "why" in SITUATIONAL_FACETS


def test_nothing_writes_the_graph_directly():
    import inspect

    import cognition_chain
    import omega_loop.episode.transaction as tx
    import pressure_loop.window_lane as window_lane

    for module in (causal_edges, cognition_chain, tx, window_lane):
        source = inspect.getsource(module)
        assert "graph_store" not in source and "insert_edge" not in source, module.__name__
