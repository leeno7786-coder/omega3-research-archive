"""Fix 3 — recall-query intents for the uncovered lobes (RED → GREEN).

route_memory_tool had no intent for goals/constraints (3), tools (5),
sentiment/outcome (4), milestones/events (9), artifacts/roadmap (11), scenes
(7), journal (6) or person-by-name (1) — those recalls fell through to
knowledge (2). And "open questions" misrouted to reasoning (3) though its home
is identity self_inquiry (0,5). Add the intents; keep existing intents intact.
"""

from __future__ import annotations

from auto_loop.mril_tools.router import route_memory_tool


def _lobe(query: str) -> int:
    return route_memory_tool(query)["lobe"]


def _entry(query: str) -> dict:
    return route_memory_tool(query)


# ── new recall intents ───────────────────────────────────────────────────
def test_recall_goals_to_reasoning():
    assert _lobe("what goals did we set") == 3


def test_recall_objective_to_reasoning():
    assert _lobe("what is our objective for offline mode") == 3


def test_recall_constraints_to_reasoning():
    assert _lobe("what constraints did we agree on") == 3


def test_recall_tools_to_procedure():
    assert _lobe("what tools can you use") == 5


def test_recall_cognitive_tools_to_procedure():
    assert _lobe("which cognitive tools are registered") == 5


def test_recall_feel_to_situation():
    assert _lobe("how did I feel about the launch") == 4


def test_recall_outcome_to_situation():
    assert _lobe("what was the outcome of the migration") == 4


def test_recall_milestones_to_time():
    assert _lobe("what milestones have we hit") == 9


def test_recall_events_to_time():
    assert _lobe("what events happened overnight") == 9


def test_recall_artifacts_to_dev():
    assert _lobe("what artifacts did the build produce") == 11


def test_recall_roadmap_to_dev():
    assert _lobe("what's the roadmap for the distro") == 11


def test_recall_scenes_to_vision():
    assert _lobe("what scenes have you captured") == 7


def test_recall_journal_to_journal():
    assert _lobe("what did deepsleep consolidate last night") == 6


def test_recall_person_by_name_to_persona():
    assert _lobe("who do I know named Sarah") == 1


def test_recall_person_by_name_searches_whole_persona_lobe():
    assert "subdomain" not in _entry("who do I know named Sarah")


def test_recall_people_category_question_routes_to_whole_persona_lobe():
    entry = _entry("who are the doctors I know?")

    assert entry["lobe"] == 1
    assert "subdomain" not in entry


def test_recall_named_person_to_persona():
    assert _lobe("who is Sheela") == 1


def test_recall_family_relationship_to_persona():
    assert _lobe("who is my sister") == 1


def test_recall_family_relationship_searches_whole_persona_lobe():
    assert "subdomain" not in _entry("who is my sister")


def test_recall_workplace_to_persona():
    assert _lobe("where do I work") == 1


def test_recall_occupation_to_persona():
    assert _lobe("what is my occupation") == 1


def test_recall_open_questions_to_identity():
    # self_inquiry lives at identity (0,5), not reasoning
    assert _lobe("what are my open questions") == 0


# ── REGRESSION: existing intents unchanged ────────────────────────────────
def test_existing_who_am_i_identity():
    assert _lobe("who am i to you") == 0


def test_creator_question_routes_to_whole_identity_lobe():
    entry = _entry("who is your creator?")

    assert entry["lobe"] == 0
    assert "subdomain" not in entry


def test_omega_self_description_routes_to_whole_identity_lobe():
    entry = _entry("what are you?")

    assert entry["lobe"] == 0
    assert "subdomain" not in entry


def test_omega_purpose_routes_to_whole_identity_lobe():
    entry = _entry("what is your purpose?")

    assert entry["lobe"] == 0
    assert "subdomain" not in entry


def test_existing_what_do_you_know_knowledge():
    assert _lobe("what do you know about black holes") == 2


def test_existing_how_to_procedure():
    assert _lobe("how to enroll a new face") == 5


def test_existing_default_knowledge():
    assert _lobe("blizzard penguin teapot zzz") == 2


def test_existing_face_vision():
    assert _lobe("recognize this face") == 7


def test_existing_why_decide_reasoning():
    assert _lobe("why did we decide to drop the classifier") == 3


def test_existing_remember_about_me_persona():
    assert _lobe("what do you remember about me") == 1


def test_named_person_memory_question_routes_to_whole_persona_lobe():
    entry = _entry("What do you remember about Mitchell Ray?")

    assert entry["lobe"] == 1
    assert "subdomain" not in entry


def test_named_person_knowledge_phrase_routes_to_persona_before_knowledge_catchall():
    entry = _entry("What do you know about Mitchell Ray?")

    assert entry["lobe"] == 1
    assert "subdomain" not in entry


def test_named_person_recall_imperative_routes_to_persona():
    entry = _entry("Tell me everything you know about Noah")

    assert entry["lobe"] == 1
    assert "subdomain" not in entry


def test_multi_person_org_role_question_routes_to_whole_persona_lobe():
    entry = _entry("What do Mitchell and Noah do at Omega AI?")

    assert entry["lobe"] == 1
    assert "subdomain" not in entry


def test_operator_and_named_person_org_role_question_routes_to_persona():
    entry = _entry("what do Mitchell and I both do at Omega AI?")

    assert entry["lobe"] == 1
    assert "subdomain" not in entry


# ── verdict region-refusal predicate (review finding 2026-07-18) ─────────
# route_names_non_recallable_lobe feeds the recall verdict's unrecoverable
# region veto: it must fire ONLY on the high-precision journal/vision intent
# regexes, never on the greedy grammar-probe dispatch tier (single generic
# words like "patterns"/"display" probe to lobe 7 for DISPATCH, which is
# recoverable — a whole-verdict veto is not).


def test_veto_predicate_names_journal_for_deepsleep():
    from auto_loop.mril_tools.router import route_names_non_recallable_lobe

    assert route_names_non_recallable_lobe("DeepSleep private replay consolidation journal") == 6
    assert route_names_non_recallable_lobe("what journal did we keep") == 6


def test_veto_predicate_names_vision_for_face_and_scenes():
    from auto_loop.mril_tools.router import route_names_non_recallable_lobe

    assert route_names_non_recallable_lobe("who is this face") == 7
    assert route_names_non_recallable_lobe("what scenes do you remember") == 7


def test_veto_predicate_ignores_greedy_probe_vocabulary():
    from auto_loop.mril_tools.router import route_names_non_recallable_lobe

    for query in (
        "what patterns have you noticed in how I make decisions?",
        "show me the monitor setup we discussed",
        "summarize the picture of our roadmap",
        "display the connector budget numbers",
    ):
        # dispatch may still probe these to lobe 7 — the veto must not.
        assert route_names_non_recallable_lobe(query) is None, query
