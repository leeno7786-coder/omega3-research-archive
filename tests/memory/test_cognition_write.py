"""T10: structural epistemic provenance — the one cognition write boundary.

Cross-lane cognitive integration spec, section 7. BOUNDARY tests over the real intake
journal, the real promotion staging and the real source ledger. Pinned:

1. the actor — a closed set, assigned by the write site; an unknown or missing actor is refused.
2. a model-supplied actor is ignored — every host-authority key is stripped from the payload.
3. derived products — ``omega_inference`` AND ``reflection`` must name what they were derived
   from; other actors need not.
4. the class is structural — the actor reaches the source ledger's ``source_type`` and the
   canonical record's ``provenance.source_type``; ``derived_from`` is durable in the journal.
5. the two cross-lane commit sites use the boundary: the fold's knowledge products
   (``omega_inference``) and the window reflection's synthesis (``reflection``).

Scope, stated rather than tested around: ``derived_from`` is host-written references, checked
for shape, not resolved against the stores; and only CROSS-LANE commits go through the
boundary — DeepSleep's per-episode consolidation products and the other intake sites keep
their existing source tags.
"""

import json

import pytest

from memory import cognition_write as boundary
from memory import episode_sram, intake_journal
from memory.source_ledger.store import read_source

PAYLOAD = {"text": "Store todos in SQLite", "title": "decision", "summary": "Store todos in SQLite"}


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


def _write(*, open_bucket=True, **overrides):
    fields = dict(actor="omega_inference", cycle_id="ep-1-decision-0", coord=[4, 1, 0],
                  type="decision", payload=dict(PAYLOAD), confidence=0.8,
                  derived_from=["episode:ep-1"])
    fields.update(overrides)
    if open_bucket:
        episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        return boundary.cognition_write(**fields)


def _never_written(monkeypatch):
    import memory.api as api

    monkeypatch.setattr(api, "intake_memory",
                        lambda *a, **k: pytest.fail("a refused write reached intake"))


# ── 1. the actor ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("actor", ["", None, "omega", "model", "omega.fold.knowledge_ingest",
                                   "OPERATOR"])
def test_a_write_without_a_closed_source_actor_is_refused(actor, monkeypatch):
    _never_written(monkeypatch)

    with pytest.raises(boundary.CognitionWriteRefused, match="source_actor"):
        _write(actor=actor)


def test_the_closed_set_is_the_specs():
    assert boundary.SOURCE_ACTORS == {"external_source", "operator", "omega_inference",
                                      "agent_observation", "reflection", "verified_artifact"}
    assert boundary.DERIVED_ACTORS == {"omega_inference", "reflection"}


def test_recall_does_not_treat_omega_inference_as_independent_evidence(monkeypatch):
    """Acceptance item 7: a recall consumer must check provenance, not the text.

    The flag has to survive on the RetrievalCandidate ``read_memory`` returns —
    labeling ``source_type`` on the canonical file is not enforcement if the
    gate rebuild drops it.
    """
    import json

    import memory.api as memory_api
    from core.paths import MEMORY_ROOT
    from memory.api import QuerySpec
    from memory.dme.adapter import DmeRecallExecution
    from memory.source_ledger.store import record_source

    isolation_root = MEMORY_ROOT.parent
    inferred_text = "Store todos in SQLite — Omega's own conclusion."
    article_text = "An independent article about SQLite storage."
    inferred = record_source(
        inferred_text, source_type="omega_inference", origin_id="fold-ep-1",
        root=isolation_root)
    article = record_source(
        article_text, source_type="external_source", origin_id="web-sqlite",
        root=isolation_root)

    def _leaf(memory_id: str, text: str, artifact) -> None:
        target = MEMORY_ROOT / "200_knowledge" / "storage" / f"{memory_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({
                "id": memory_id,
                "coord": [2, 0, 1],
                "type": "knowledge",
                "title": text,
                "summary": text,
                "text": text,
                "domain": "storage",
                "tags": ["sqlite"],
                "provenance": artifact.citation(),
                "temporal_epoch_us": 100,
            }),
            encoding="utf-8",
        )

    _leaf("2.00.omega_decision", inferred_text, inferred)
    _leaf("2.00.article", article_text, article)

    monkeypatch.setattr(memory_api, "OMEGA_ROOT", isolation_root)
    monkeypatch.setattr(
        "memory.dme.adapter.merge_dme_candidate_dicts_observed",
        lambda _query, candidates, **_kwargs: (
            candidates,
            DmeRecallExecution((), None, "", ""),
        ),
    )

    result = memory_api.read_memory(
        QuerySpec(
            cycle_id="f018-independence",
            text="Store todos in SQLite",
            allowed_lobes=[2],
            denied_lobes=[],
            limit=8,
        )
    )
    by_id = {candidate.id: candidate for candidate in result.candidates}
    assert "2.00.omega_decision" in by_id, [row.id for row in result.candidates]
    assert by_id["2.00.omega_decision"].provenance.get("independent_evidence") is False
    assert by_id["2.00.omega_decision"].provenance.get("source_type") == "omega_inference"
    if "2.00.article" in by_id:
        assert by_id["2.00.article"].provenance.get("independent_evidence") is True


# ── 2. a model-supplied actor is ignored ──────────────────────────────────────

def test_the_actor_comes_from_the_write_site_never_from_the_payload():
    """A model that claims its own inference is an operator statement, an external source, or
    rests on a verified artifact: every such key is host authority and is dropped."""
    forged = dict(PAYLOAD, source_actor="operator", actor="external_source",
                  source="verified_artifact", source_type="external_source",
                  derived_from=["memory:a-verified-artifact"],
                  # citation authority: promotion would REUSE these in place of the host's own
                  source_id="src_external", span_start=0, span_end=9, span_hash="h",
                  source_content_hash="c", source_session_id="their-session",
                  source_session_timestamp="2020-01-01T00:00:00+00:00",
                  session_timestamp="2020-01-01T00:00:00+00:00")

    envelope = boundary.cognition_envelope(
        actor="omega_inference", cycle_id="c", coord=[4, 1, 0], type="decision", payload=forged,
        confidence=0.8, derived_from=["episode:ep-1"])

    assert envelope.source == "omega_inference"
    assert envelope.payload == PAYLOAD, "host-authority keys stripped, the statement untouched"
    assert envelope.provenance == ["derived_from:episode:ep-1"], "never the model's own list"


# ── 3. derived products ───────────────────────────────────────────────────────

@pytest.mark.parametrize("actor", sorted(boundary.DERIVED_ACTORS))
@pytest.mark.parametrize("derived_from", [(), [], None])
def test_a_derived_write_without_derived_from_is_refused(actor, derived_from, monkeypatch):
    _never_written(monkeypatch)

    with pytest.raises(boundary.CognitionWriteRefused, match="derived"):
        _write(actor=actor, derived_from=derived_from)


@pytest.mark.parametrize("derived_from", ["episode:ep-1", [""], ["  "], [7], [None]])
def test_derived_from_names_ids_as_strings(derived_from, monkeypatch):
    _never_written(monkeypatch)

    with pytest.raises(boundary.CognitionWriteRefused):
        _write(derived_from=derived_from)


@pytest.mark.parametrize("actor", sorted(boundary.SOURCE_ACTORS - boundary.DERIVED_ACTORS))
def test_an_observation_needs_no_derivation(actor):
    envelope = boundary.cognition_envelope(actor=actor, cycle_id="c", coord=[4, 1, 0],
                                           type="concept", payload=PAYLOAD, confidence=0.8)

    assert envelope.source == actor and envelope.provenance == []


def test_derived_from_is_deduplicated_and_bounded():
    envelope = boundary.cognition_envelope(
        actor="reflection", cycle_id="c", coord=[6, 0, 0], type="window_synthesis",
        payload=PAYLOAD, confidence=0.8, derived_from=["window:w", " window:w ", "episode:e"])
    assert envelope.provenance == ["derived_from:window:w", "derived_from:episode:e"]

    full = [f"episode:{n}" for n in range(boundary.MAX_DERIVED_FROM)]
    assert len(boundary.cognition_envelope(actor="reflection", cycle_id="c", coord=[6, 0, 0],
                                           type="x", payload=PAYLOAD, confidence=0.8,
                                           derived_from=full).provenance) == len(full)
    with pytest.raises(boundary.CognitionWriteRefused, match="more than"):
        boundary.cognition_envelope(actor="reflection", cycle_id="c", coord=[6, 0, 0], type="x",
                                    payload=PAYLOAD, confidence=0.8,
                                    derived_from=full + ["episode:one-more"])


# ── 4. the class is structural ────────────────────────────────────────────────

def test_an_accepted_write_is_durable_in_the_journal_with_its_actor_and_derivation():
    from memory.api import intake_receipt_accepted

    receipt = _write(derived_from=["episode:ep-1", "mail:m-7"])

    assert intake_receipt_accepted(receipt)
    record = intake_journal.record_by_id(receipt.journal_record_id)
    assert record.payload["source"] == "omega_inference"
    assert record.payload["provenance"] == ["derived_from:episode:ep-1", "derived_from:mail:m-7"]


@pytest.mark.parametrize("actor", ["omega_inference", "reflection"])
def test_the_actor_reaches_the_source_ledgers_source_type(actor, tmp_path):
    """Through the REAL promotion staging: the ledger artifact is recorded under the actor and
    the canonical record cites it — the class is structural, not something the text says."""
    import memory.api as api
    import memory.promotion as promotion

    envelope = boundary.cognition_envelope(
        actor=actor, cycle_id="ep-1-x-0", coord=[4, 1, 0], type="decision",
        payload=dict(PAYLOAD, source_type="external_source"), confidence=0.8,
        derived_from=["episode:ep-1"])

    staging_ref = promotion.create_staging(api._envelope_to_dict(envelope))

    staged = json.loads((promotion.STAGING_DIR / f"{staging_ref}.json").read_text(encoding="utf-8"))
    provenance = staged["memory_dict"]["provenance"]
    assert provenance["source_type"] == actor
    artifact = read_source(provenance["source_id"])
    assert artifact is not None and artifact.source_type == actor


def test_a_payload_cannot_cite_an_existing_source_in_place_of_the_actors(tmp_path):
    """Reviewer's probe: the payload named an EXISTING external artifact's source_id.
    Promotion reused it, so the inference cited the external artifact and carried no
    provenance.source_type at all."""
    import memory.api as api
    import memory.promotion as promotion
    from memory.source_ledger.store import record_source

    external = record_source("An independent article.", source_type="external_source",
                             origin_id="web")
    envelope = boundary.cognition_envelope(
        actor="omega_inference", cycle_id="ep-1-x-0", coord=[4, 1, 0], type="decision",
        payload=dict(PAYLOAD, source_id=external.source_id, span_start=0, span_end=5),
        confidence=0.8, derived_from=["episode:ep-1"])

    staging_ref = promotion.create_staging(api._envelope_to_dict(envelope))

    staged = json.loads((promotion.STAGING_DIR / f"{staging_ref}.json").read_text(encoding="utf-8"))
    provenance = staged["memory_dict"]["provenance"]
    assert provenance["source_type"] == "omega_inference"
    assert provenance["source_id"] != external.source_id
    assert read_source(provenance["source_id"]).source_type == "omega_inference"


def test_a_canonical_memory_resolves_to_its_actor_and_derivation():
    """The journal binds a record to the canonical memory it became; read backwards, that is
    the lookup T11's causal edges need — no second writer, no new memory field."""
    receipt = _write(derived_from=["episode:ep-1", "memory:mem-recalled"])
    intake_journal.mark_committed(receipt.journal_record_id, memory_id="mem-canonical-1",
                                  canonical_path="4/1/mem-canonical-1.json")
    _write(open_bucket=False, cycle_id="ep-1-decision-1",
           payload=dict(PAYLOAD, text="another, never committed"))
    intake_journal.reset_cache_for_tests()                      # as after a restart

    assert boundary.derivation_of("mem-canonical-1") == {
        "actor": "omega_inference", "journal_record_id": receipt.journal_record_id,
        "event_id": receipt.event_id, "derived_from": ["episode:ep-1", "memory:mem-recalled"]}
    assert boundary.derivation_of("mem-nobody-wrote") is None
    assert boundary.derivation_of("") is None


def test_only_a_cognition_write_has_a_derivation():
    """A memory that entered through any other intake site has an actor outside the closed
    set: it resolves to no derivation rather than to a guessed one."""
    from memory.api import CanonicalEnvelope, intake_memory

    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        receipt = intake_memory(CanonicalEnvelope(cycle_id="ep-1-other", coord=[4, 1, 0],
                                                  type="concept", payload=dict(PAYLOAD),
                                                  source="omega.auto.fetched_ingest"),
                                propose=False, bypass_guardian=True)
    intake_journal.mark_committed(receipt.journal_record_id, memory_id="mem-fetched")

    assert boundary.derivation_of("mem-fetched") is None


# ── 5. the two cross-lane commit sites ────────────────────────────────────────

def test_the_folds_knowledge_products_commit_as_omega_inference(monkeypatch):
    from memory.knowledge_ingest import ingest_knowledge_products

    products = [
        {"kind": "fold_statement", "domain": "storage", "statement": "SQLite fits", "confidence": 0.7},
        {"kind": "decision", "domain": "storage", "statement": "Store todos in SQLite",
         "rationale": "asked", "supersedes_id": "mem-old", "confidence": 0.8,
         # A model row trying to name its own class.
         "source_actor": "operator", "source_type": "external_source"},
        {"kind": "user_constraint", "domain": "storage", "statement": "Use SQLite",
         "source_episode_id": "note-cycle", "confidence": 0.9},
    ]
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        receipts = ingest_knowledge_products("ep-1", products, derived_from=["mail:m-7"])

    records = [intake_journal.record_by_id(row["journal_record_id"]) for row in receipts]
    assert [record.payload["source"] for record in records] == ["omega_inference"] * 3
    assert [record.payload["provenance"] for record in records] == [
        ["derived_from:episode:ep-1", "derived_from:mail:m-7"],
        ["derived_from:episode:ep-1", "derived_from:mail:m-7"],
        ["derived_from:episode:ep-1", "derived_from:mail:m-7", "derived_from:episode:note-cycle"],
    ]
    assert all("source_actor" not in record.payload["payload"] for record in records)
    # The model's supersession proposal travels in the payload for the supersede hook; it is
    # not evidence, so it is NOT a derivation (a decision naming "never-existed" passed the
    # real parser and durably recorded that invented dependency).
    assert records[1].payload["payload"]["supersedes_id"] == "mem-old"
    assert not any("mem-old" in ref for record in records for ref in record.payload["provenance"])


RECALL = [{"tool": "remember_knowledge",
           "evidence": [{"memory_id": "mem-a", "title": "SQLite notes"},
                        {"memory_id": "mem-b"}, {"memory_id": "mem-a"}, {"memory_id": ""}],
           "nonresponsive_evidence": [{"memory_id": "mem-unrelated"}]},
          {"tool": "remember_dev", "evidence": [{"memory_id": "mem-c"}]},
          "not a row"]


def test_the_fold_names_the_memories_recall_showed_it():
    import cognition_chain as chain

    assert chain._recalled_memory_ids({"recall": RECALL}) == ["mem-a", "mem-b", "mem-c"], \
        "responsive evidence only, once each"
    assert chain._recalled_memory_ids({}) == [] and chain._recalled_memory_ids(None) == []
    many = {"recall": [{"evidence": [{"memory_id": f"m{n}"} for n in range(500)]}]}
    assert len(chain._recalled_memory_ids(many)) == 500, "the frozen evidence is never cut"


def test_selection_and_intake_agree_on_how_much_a_conclusion_can_name():
    """Everything selection can admit for one conclusion fits the write boundary — its
    mandatory references, every mail row the binder admits, and the recall capacity — so a
    valid fold can never be refused at intake."""
    import cognition_chain as chain
    from omega_loop.episode.types import CROSS_LANE_MAIL_ROWS

    assert (chain._MANDATORY_DERIVATION_REFS + CROSS_LANE_MAIL_ROWS
            + chain._recall_ref_capacity()) == boundary.MAX_DERIVED_FROM
    assert chain._recall_ref_capacity() >= 32


def _fold_over(monkeypatch, *, recall_ids, mail_ids, constraint=True):
    import cognition_chain as chain
    from types import SimpleNamespace

    monkeypatch.setattr("omega_loop.episode.packet.fold_replay_refused", lambda episode: "")
    monkeypatch.setattr("omega_loop.episode.packet.while_omega_mail_owned",
                        lambda episode, action: action())
    global_ = {"omega_result": {},
               "omega_packet": {"domain": "storage",
                                "recall": [{"evidence": [{"memory_id": one} for one in recall_ids]}]},
               "omega_mail": {"disposition": "bound", "bound_mail_ids": list(mail_ids)}}
    constraints = ({"statement": "Use SQLite", "source_episode_id": "note-cycle"},) if constraint else ()
    result = SimpleNamespace(knowledge_to_ingest=(), user_constraints=constraints, decisions=())
    episode = SimpleNamespace(episode_id="ep-1", global_=global_, pressure={}, meta={}, pipeline_id=0)
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        chain._ingest_fold_knowledge(episode, result, global_)
    [receipt] = global_["omega_result"]["delta_receipts"]
    if not receipt["accepted"]:
        return receipt
    return intake_journal.record_by_id(receipt["journal_record_id"]).payload["provenance"]


def test_a_full_binder_and_a_full_recall_are_accepted_together(monkeypatch):
    """Reviewer's probe: the real binder admitted 32 messages, the packet held 32 recall ids,
    and intake REFUSED the decision — 32 + 32 + the mandatory references exceeded 64."""
    from omega_loop.episode.types import CROSS_LANE_MAIL_ROWS

    provenance = _fold_over(monkeypatch, recall_ids=[f"mem-{n}" for n in range(32)],
                            mail_ids=[f"m-{n}" for n in range(CROSS_LANE_MAIL_ROWS)])

    assert len(provenance) == 2 + 32 + CROSS_LANE_MAIL_ROWS
    assert provenance[0] == "derived_from:episode:ep-1"
    assert provenance[-1] == "derived_from:episode:note-cycle"


def test_a_conclusion_names_its_evidence_completely_or_is_not_recorded(monkeypatch):
    """Reviewer's probe: an overflowing conclusion cited an evictable fold binding — after
    eviction the journal still named the fold and the evidence list was gone — or, with no
    durable fold identity, a bare COUNT. Neither is evidence. The journal record is what lives
    as long as the memory does, so it names everything inline; past that it is REFUSED."""
    import cognition_chain as chain

    capacity = chain._recall_ref_capacity()
    at_capacity = _fold_over(monkeypatch, recall_ids=[f"mem-{n}" for n in range(capacity)],
                             mail_ids=[f"m-{n}" for n in range(32)])
    assert len(at_capacity) == boundary.MAX_DERIVED_FROM, "everything, inline, at the boundary"
    assert not any(ref.startswith(("derived_from:fold:", "derived_from:recall-unnamed"))
                   for ref in at_capacity)


def test_evidence_beyond_what_a_record_can_name_refuses_the_statement_not_the_fold(monkeypatch):
    import cognition_chain as chain

    before = len(intake_journal.records())
    receipt = _fold_over(monkeypatch, mail_ids=["m-1"],
                         recall_ids=[f"mem-{n}" for n in range(chain._recall_ref_capacity() + 1)])

    assert (receipt["accepted"], receipt["status"], receipt["journal_record_id"]) == (False, "refused", None)
    assert "completely or not at all" in receipt["refusal"]
    assert len(intake_journal.records()) == before, "nothing journalled, and nothing raised"


def test_the_frozen_derivation_keeps_too_much_detectable_without_breaching_the_manifest():
    import cognition_chain as chain
    from types import SimpleNamespace

    capacity = chain._recall_ref_capacity()
    episode = SimpleNamespace(episode_id="ep-1", global_={"omega_packet": {"recall": [
        {"evidence": [{"memory_id": f"mem-{n}"} for n in range(capacity + 500)]}]}})

    frozen = chain._fold_derivation(episode)

    assert len(frozen["recall"]) == capacity + 1
    assert chain._derivation_standing(frozen)[1], "still refused after a restart"
    assert not chain._derivation_standing({**frozen, "recall": frozen["recall"][:capacity]})[1]


def test_the_references_are_complete_and_inline():
    import cognition_chain as chain

    assert chain._derivation_refs({"episode_id": "ep-1", "recall": ["a", "b"], "mail": ["m"]}) == [
        "memory:a", "memory:b", "mail:m"]


def test_recalled_evidence_reaches_the_committed_derivation(monkeypatch):
    """Reviewer's probe, chain to intake: identified recall evidence was supplied and the
    committed derivation held only the current episode."""
    import cognition_chain as chain
    from types import SimpleNamespace

    monkeypatch.setattr("omega_loop.episode.packet.fold_replay_refused", lambda episode: "")
    monkeypatch.setattr("omega_loop.episode.packet.while_omega_mail_owned",
                        lambda episode, action: action())
    global_ = {"omega_result": {}, "omega_packet": {"domain": "storage", "recall": RECALL}}
    result = SimpleNamespace(knowledge_to_ingest=(), user_constraints=(),
                             decisions=({"statement": "Store todos in SQLite", "rationale": "asked",
                                         "supersedes_id": "never-existed"},))
    episode = SimpleNamespace(episode_id="ep-1", global_=global_, pressure={}, meta={},
                              pipeline_id=0)
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        chain._ingest_fold_knowledge(episode, result, global_)

    [receipt] = global_["omega_result"]["delta_receipts"]
    record = intake_journal.record_by_id(receipt["journal_record_id"])
    assert record.payload["provenance"] == ["derived_from:episode:ep-1", "derived_from:memory:mem-a",
                                            "derived_from:memory:mem-b", "derived_from:memory:mem-c"]


def test_a_recovered_statement_names_the_episode_it_arose_in():
    from memory.knowledge_ingest import ingest_knowledge_products

    episode_sram.open_bucket("ep-replay", pipeline_id=0)
    with episode_sram.bind_episode("ep-replay", pipeline_id=0):
        [receipt] = ingest_knowledge_products(
            "ep-replay", [{"kind": "decision", "statement": "Store todos in SQLite",
                           "rationale": "asked", "confidence": 0.8}],
            derived_from=["memory:original-memory"], origin_episode_id="ep-original")

    record = intake_journal.record_by_id(receipt["journal_record_id"])
    assert record.payload["provenance"] == ["derived_from:episode:ep-original",
                                            "derived_from:memory:original-memory"]


@pytest.mark.parametrize("disposition, named", [("bound", ["mail:m-7", "mail:m-9"]),
                                                ("ownership_lost", []), ("none", [])])
def test_the_fold_passes_the_mail_it_reasoned_over(disposition, named, monkeypatch):
    """The chain names the fold's bound mail as what its conclusions rest on — and only mail
    it actually holds."""
    import cognition_chain as chain
    import memory.knowledge_ingest as knowledge_ingest
    from types import SimpleNamespace

    seen = {}
    monkeypatch.setattr(knowledge_ingest, "ingest_knowledge_products",
                        lambda ep, statements, **k: seen.update(k) or [])
    monkeypatch.setattr("global_loop.intersections.fold_binding", lambda fold_id: None)
    monkeypatch.setattr("omega_loop.episode.packet.fold_replay_refused", lambda episode: "")
    monkeypatch.setattr("omega_loop.episode.packet.while_omega_mail_owned",
                        lambda episode, action: action())
    global_ = {"omega_result": {}, "omega_packet": {"domain": "storage"},
               "omega_mail": {"disposition": disposition, "bound_mail_ids": ["m-7", "m-9"]}}
    result = SimpleNamespace(knowledge_to_ingest=(), user_constraints=(),
                             decisions=({"statement": "Store todos in SQLite", "rationale": "asked"},))
    episode = SimpleNamespace(episode_id="ep-1", global_=global_, pressure={}, meta={})

    chain._ingest_fold_knowledge(episode, result, global_)

    assert seen["derived_from"] == named


def test_a_window_reflections_synthesis_commits_as_reflection():
    from deepsleep_loop.consolidate_memory.run import _window_synthesis_envelope

    envelope = _window_synthesis_envelope("ep-window", {
        "window_id": "w-42", "reflection_source": "model",
        "reflection": {"summary": "Two lookups circling one gap."}})

    assert envelope.source == "reflection"
    assert envelope.provenance == ["derived_from:window:w-42", "derived_from:episode:ep-window"]
    assert envelope.type == "window_synthesis" and "Two lookups" in envelope.payload["text"]


# ── 6. review round 5 (2026-09-17): a normal return from intake is not acceptance ──────

def _learn(monkeypatch, *, recall_count):
    import cognition_chain as chain
    from types import SimpleNamespace

    monkeypatch.setattr("omega_loop.episode.packet.fold_replay_refused", lambda episode: "")
    monkeypatch.setattr("omega_loop.episode.packet.while_omega_mail_owned",
                        lambda episode, action: action())
    global_ = {"omega_result": {"mastery_effects": [{"domain": "storage", "knowledge_mastery": 0.9}]},
               "omega_packet": {"domain": "storage", "prior_mastery": {"knowledge_mastery": 0.2},
                                "recall": [{"evidence": [{"memory_id": f"mem-{n}"}
                                                         for n in range(recall_count)]}]}}
    result = SimpleNamespace(decisions=(), user_constraints=(), knowledge_to_ingest=(
        SimpleNamespace(domain="storage", statement="SQLite fits a single-user todo app", confidence=0.7),))
    episode = SimpleNamespace(episode_id="ep-1", global_=global_, pressure={}, meta={}, pipeline_id=0)
    episode_sram.open_bucket("ep-1", pipeline_id=0)
    with episode_sram.bind_episode("ep-1", pipeline_id=0):
        chain._ingest_fold_knowledge(episode, result, global_)
    return global_["omega_result"]


def test_refused_knowledge_moves_no_mastery(monkeypatch):
    """Reviewer's probe: prior knowledge mastery 0.2; intake refused the only statement and
    journalled nothing — and the proposed 0.9 stayed on the carrier, because the gate asked
    whether receipts EXISTED, not whether any was accepted."""
    import cognition_chain as chain

    omega = _learn(monkeypatch, recall_count=chain._recall_ref_capacity() + 1)

    [receipt] = omega["knowledge_receipts"]
    assert (receipt["accepted"], receipt["status"]) == (False, "refused"), "the refusal evidence stays"
    assert omega["mastery_effects"] == [{"domain": "storage", "knowledge_mastery": 0.2}]
    assert intake_journal.records() == ()


def test_accepted_knowledge_still_moves_mastery(monkeypatch):
    omega = _learn(monkeypatch, recall_count=3)

    assert omega["knowledge_receipts"][0]["accepted"] is True
    assert omega["mastery_effects"] == [{"domain": "storage", "knowledge_mastery": 0.9}]
