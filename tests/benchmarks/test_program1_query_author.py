from __future__ import annotations

import json

import pytest

from benchmarks.memory_organ.program1.query_author import (
    AuthoringValidationError,
    build_request_payload,
    parse_batch_response,
    validate_query,
)


def _job(candidate_id: str = "c1", stratum: str = "ordinary_synonyms") -> dict:
    return {
        "candidate_id": candidate_id,
        "partition": "development",
        "stratum": stratum,
        "candidate_text": "Pressure may open an episode, but pressure never chooses a concrete tool.",
        "contrast_role": None,
        "hard_negative_text": None,
        "expected_output_fields": ("query",),
    }


def test_request_is_query_only_and_disables_thinking() -> None:
    payload = build_request_payload([_job()], seed=17)
    assert payload["temperature"] == 0
    assert payload["seed"] == 17
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    response_schema = json.dumps(payload["response_format"], sort_keys=True)
    assert "candidate_text" not in response_schema
    assert "candidate_id" not in response_schema
    assert '"i"' in response_schema
    system = payload["messages"][0]["content"]
    user = payload["messages"][1]["content"]
    assert "query" in system
    assert "candidate_id" not in user
    assert "target memory" not in user.lower()
    assert "hard contrast" not in user.lower()
    assert '"A"' in user


def test_parse_batch_response_requires_exact_batch_indices_and_query_only_items() -> None:
    jobs = [_job("c1"), _job("c2", "morphology_variation")]
    envelope = {
        "choices": [{"message": {"content": json.dumps({
            "items": [
                {"i": 0, "query": "stress can trigger an episode but cannot select a tool"},
                {"i": 1, "query": "which tool was selected after an episode opened"},
            ]
        })}}]
    }
    parsed = parse_batch_response(jobs, envelope)
    assert parsed == {
        "c1": "stress can trigger an episode but cannot select a tool",
        "c2": "which tool was selected after an episode opened",
    }

    bad = {
        "choices": [{"message": {"content": json.dumps({
            "items": [{"i": 0, "query": "x", "candidate_text": "forbidden"},
                      {"i": 1, "query": "y"}]
        })}}]
    }
    with pytest.raises(AuthoringValidationError, match="unexpected fields"):
        parse_batch_response(jobs, bad)


    duplicate = {
        "choices": [{"message": {"content": json.dumps({
            "items": [{"i": 0, "query": "first"}, {"i": 0, "query": "second"}]
        })}}]
    }
    with pytest.raises(AuthoringValidationError, match="duplicate batch index"):
        parse_batch_response(jobs, duplicate)


def test_zero_overlap_validation_uses_frozen_content_tokens() -> None:
    job = _job("z", "zero_token_overlap_paraphrase")
    job["forbidden_content_tokens"] = ["dobsonian", "observ", "saturn"]
    job["candidate_text"] = "Observed Saturn through the Dobsonian"
    validate_query(job, "Which telescope did I use?")
    with pytest.raises(AuthoringValidationError, match="shares content tokens"):
        validate_query(job, "Which Dobsonian telescope did I use?")


def test_hard_pair_prompt_contains_target_and_contrast_but_output_stays_query_only() -> None:
    job = _job("hard", "temporal_update")
    job["candidate_text"] = "Later, the inspection moved to Thursday afternoon after the schedule changed."
    job["contrast_role"] = "later"
    job["hard_negative_text"] = "Earlier, the inspection happened on Tuesday morning."
    payload = build_request_payload([job], seed=3)
    user = payload["messages"][1]["content"]
    assert '"A"' in user and '"B"' in user
    assert "TARGET MEMORY" not in user
    assert "HARD CONTRAST MEMORY" not in user
    assert "later" in user
    validate_query(job, "When did the later inspection occur after the schedule changed?")


def test_empty_or_overlong_query_is_refused() -> None:
    job = _job()
    with pytest.raises(AuthoringValidationError, match="empty"):
        validate_query(job, "   ")
    with pytest.raises(AuthoringValidationError, match="too long"):
        validate_query(job, "x" * 121)


def test_temporal_author_query_must_encode_the_requested_time_role() -> None:
    earlier = _job("earlier", "temporal_update")
    earlier["candidate_text"] = "Earlier, the office discussed healthy snack ideas before the gala plan."
    earlier["contrast_role"] = "earlier"
    earlier["hard_negative_text"] = "Later, the charity gala was added to the fundraising page."
    with pytest.raises(AuthoringValidationError, match="temporal role"):
        validate_query(earlier, "office healthy snack ideas")
    validate_query(earlier, "Which healthy snack ideas were discussed earlier before the gala plan?")

    current = _job("current", "temporal_update")
    current["candidate_text"] = "The current active route Gamma handles telemetry."
    current["contrast_role"] = "current"
    current["hard_negative_text"] = "The legacy route Beta was retired."
    with pytest.raises(AuthoringValidationError, match="temporal role"):
        validate_query(current, "Which route handles telemetry?")
    validate_query(current, "Which route Gamma is active now for telemetry?")


def test_meta_evaluation_language_is_never_a_lawful_authored_query() -> None:
    job = _job("meta", "adversarial_semantic_near_neighbor")
    job["candidate_text"] = "The cache misses increased after the routing table changed."
    job["hard_negative_text"] = "The tool failures increased after the routing table changed."
    for query in (
        "What distinguishes the target memory from the hard contrast memory?",
        "Which candidate memory is correct for this test case?",
        "What does the source memory say in this evaluation?",
    ):
        with pytest.raises(AuthoringValidationError, match="evaluation/meta language"):
            validate_query(job, query)


def test_hard_natural_query_must_name_a_target_distinguishing_detail() -> None:
    job = _job("near", "adversarial_semantic_near_neighbor")
    job["candidate_text"] = "The cache misses increased after the routing table changed."
    job["hard_negative_text"] = "The tool failures increased after the routing table changed."
    with pytest.raises(AuthoringValidationError, match="distinguishing target detail"):
        validate_query(job, "What changed after the routing table changed?")
    validate_query(job, "What happened to cache misses after the routing table changed?")


def test_hard_author_prompt_exposes_model_blind_a_only_surface_terms() -> None:
    job = _job("details", "adversarial_semantic_near_neighbor")
    job["candidate_text"] = "The tensor MatMul path returns a NumPy array with a validated output shape."
    job["hard_negative_text"] = "The graph path returns a validated result through the same runtime backend."
    payload = build_request_payload([job], seed=9)
    authored_job = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])[0]
    terms = authored_job["A_detail_terms"]
    assert terms
    assert any(term.lower() in {"tensor", "matmul", "numpy", "array", "shape"} for term in terms)
    assert "graph" not in {term.lower() for term in terms}


# ── P1-A authoring-feasibility fixes (measured 0/8 temporal, 0/12 zero-overlap before this) ──
# The acceptance test must accept exactly what the INSTRUCTION asks for. The instruction promises
# "explicit time wording matching the requested role"; the old whitelist accepted six surface words
# per role, so natural realisations ("originally", "previously", "initially") were rejected. This is
# a coverage bug in the test, not a relaxation: a query with NO time marking is still refused.

def _temporal(role: str) -> dict:
    job = _job(f"t-{role}", "temporal_update")
    job["contrast_role"] = role
    job["candidate_text"] = "The Gamma allocator handled telemetry batching for the ingest lane."
    job["hard_negative_text"] = "The Delta allocator handles telemetry batching for the ingest lane."
    return job


@pytest.mark.parametrize("role,query", [
    ("earlier", "Which allocator originally handled telemetry batching for Gamma?"),
    ("earlier", "Which allocator previously handled the Gamma telemetry batching?"),
    ("earlier", "Which allocator initially handled telemetry batching for Gamma?"),
    ("earlier", "Which allocator handled Gamma telemetry batching before the change?"),
    ("historical", "Which allocator formerly handled telemetry batching for Gamma?"),
    ("historical", "Which allocator was deprecated for Gamma telemetry batching?"),
    ("later", "Which allocator handled Gamma telemetry batching afterwards?"),
    ("later", "Which allocator subsequently handled telemetry batching for Gamma?"),
    ("current", "Which allocator now handles telemetry batching for Gamma?"),
    ("current", "Which allocator presently handles Gamma telemetry batching?"),
])
def test_natural_time_wording_satisfies_the_requested_role(role: str, query: str) -> None:
    validate_query(_temporal(role), query)


@pytest.mark.parametrize("role", ["earlier", "historical", "later", "current"])
def test_a_query_with_no_time_marking_is_still_refused(role: str) -> None:
    with pytest.raises(AuthoringValidationError, match="temporal role"):
        validate_query(_temporal(role), "Which allocator handled telemetry batching for Gamma?")


def test_zero_overlap_job_gives_the_author_surface_words_not_stems() -> None:
    from benchmarks.memory_organ.program1.query_author import _job_payload

    job = _job("z1", "zero_token_overlap_paraphrase")
    job["candidate_text"] = "Completed gaming hours were logged for the Argentina France match."
    job["forbidden_content_tokens"] = ["argentina", "complet", "franc", "gam", "hour", "log", "match"]
    payload = _job_payload(job, output_index=0)
    forbidden = payload["forbidden_content_tokens"]
    # the author cannot run the stemmer in its head: it must be shown the WORDS to avoid
    assert "Completed" in forbidden and "gaming" in forbidden and "hours" in forbidden
    assert "complet" not in forbidden and "gam" not in forbidden
    for word in forbidden:
        assert word in job["candidate_text"]
