from __future__ import annotations

import json

import pytest

from mcp_platform.plugin_argument_constraints import (
    canonicalize_argument_constraints,
    evaluate_argument_constraints,
)
from mcp_platform.plugin_jobs import PluginJob, PluginJobStore


TOOL_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "priority": {"type": "string"}},
    "required": ["label"],
}


def test_canonicalize_rejects_undeclared_key() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        canonicalize_argument_constraints({"nonexistent": {"required": True}}, TOOL_SCHEMA)


def test_evaluate_one_of_and_pattern_and_forbid_extra_keys() -> None:
    constraints = canonicalize_argument_constraints(
        {
            "label": {"one_of": ["bug", "triage"], "required": True},
            "priority": {"pattern": "^p[0-3]$"},
        },
        TOOL_SCHEMA,
    )
    assert evaluate_argument_constraints(constraints, {"label": "bug", "priority": "p1"})
    assert not evaluate_argument_constraints(constraints, {"label": "urgent"})
    assert not evaluate_argument_constraints(constraints, {"label": "bug", "extra": "x"})


def test_one_of_bounded_at_32_values() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        canonicalize_argument_constraints(
            {"label": {"one_of": [f"v{i}" for i in range(33)]}}, TOOL_SCHEMA
        )


def test_pattern_must_be_anchored_and_bounded() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        canonicalize_argument_constraints({"label": {"pattern": "bug"}}, TOOL_SCHEMA)


def _job_kwargs(**overrides) -> dict:
    base = dict(
        plugin_id="linear.workspace",
        app_id="linear.workspace.mcp",
        account_key_hash="a" * 64,
        connection_revision="b" * 64,
        connection_authority_revision="c" * 64,
        capability_revision="d" * 64,
        title="Triage new issues",
        objective="Watch the inbox and triage anything new.",
        trigger={"kind": "schedule", "interval_seconds": 900, "jitter_seconds": 60,
                 "active_hours": None, "timezone": "UTC"},
        primary_read_tool={"tool_name": "list_issues", "arguments": {"state": "new"}},
        autonomy_level="observe",
        allowed_tools=(
            {
                "tool_name": "list_issues",
                "tool_schema_sha256": "e" * 64,
                "risk_class": "read_only",
                "argument_constraints": {},
            },
        ),
        wake=False,
        enabled=True,
        max_runs_per_day=48,
        max_consecutive_failures=5,
    )
    base.update(overrides)
    return base


def test_store_create_read_and_revision_conflict(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    job = store.create(PluginJob.new(**_job_kwargs()))
    assert store.get(job.job_id).job_revision == job.job_revision
    with pytest.raises(ValueError, match="plugin_job_revision_conflict"):
        store.update(job.job_id, "0" * 64, {"enabled": False})


def test_store_malformed_document_fails_closed(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    store.create(PluginJob.new(**_job_kwargs()))
    store.document_path.write_bytes(b"not json")
    with pytest.raises(ValueError, match="plugin_jobs_store_unavailable"):
        store.list_for_plugin("linear.workspace")


def test_ledger_is_bounded_to_50_receipts(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    job = store.create(PluginJob.new(**_job_kwargs()))
    for i in range(60):
        job = store.advance_state(
            job.job_id,
            receipt={"outcome": "unchanged", "at": f"2026-01-01T00:00:{i:02d}Z"},
        )
    assert len(job.ledger) == 50


def test_invalidate_for_connection_pauses_and_returns_jobs(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    job = store.create(PluginJob.new(**_job_kwargs()))
    changed = store.invalidate_for_connection("linear.workspace", "linear.workspace.mcp", "a" * 64)
    assert changed[0].job_id == job.job_id
    assert changed[0].paused_reason == "authority_invalid"


# --- Pattern grammar: bounded, not merely measured (review round 1, finding 1) ---


def test_pattern_rejects_lookaround() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid_pattern_lookaround"):
        canonicalize_argument_constraints({"label": {"pattern": "^(?=a)a$"}}, TOOL_SCHEMA)


def test_pattern_rejects_conditional() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid_pattern_conditional"):
        canonicalize_argument_constraints({"label": {"pattern": "^(?(1)a|b)$"}}, TOOL_SCHEMA)


def test_pattern_rejects_backreference() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid_pattern_backreference"):
        canonicalize_argument_constraints({"label": {"pattern": r"^(a)\1$"}}, TOOL_SCHEMA)


def test_pattern_rejects_quantified_group() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid_pattern_quantified_group"):
        canonicalize_argument_constraints({"label": {"pattern": "^(ab)+$"}}, TOOL_SCHEMA)


def test_pattern_rejects_too_many_quantifiers() -> None:
    pattern = "^" + "".join(f"{chr(ord('a') + i)}?" for i in range(11)) + "$"
    with pytest.raises(ValueError, match="plugin_job_invalid_pattern_too_many_quantifiers"):
        canonicalize_argument_constraints({"label": {"pattern": pattern}}, TOOL_SCHEMA)


def test_pattern_rejects_pathological_backtracking_at_canonicalization() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid_pattern_quantified_group"):
        canonicalize_argument_constraints({"label": {"pattern": "^(a+)+$"}}, TOOL_SCHEMA)


def test_pattern_value_over_512_chars_fails_fast() -> None:
    constraints = canonicalize_argument_constraints({"label": {"pattern": "^.*$"}}, TOOL_SCHEMA)
    assert evaluate_argument_constraints(constraints, {"label": "x" * 512})
    assert not evaluate_argument_constraints(constraints, {"label": "x" * 513})


def test_pattern_legal_pattern_with_several_quantifiers_still_evaluates() -> None:
    constraints = canonicalize_argument_constraints(
        {"label": {"pattern": r"^[a-z]+-[0-9]{2,4}-[A-Z]?[a-z]*$"}}, TOOL_SCHEMA
    )
    assert evaluate_argument_constraints(constraints, {"label": "bug-1234-Xyz"})
    assert not evaluate_argument_constraints(constraints, {"label": "BUG-1-xyz"})


# --- Constraint-grammar coverage gaps (review round 1, finding 2) ---


def test_more_than_one_shape_kind_rejected() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        canonicalize_argument_constraints(
            {"label": {"fixed": "bug", "one_of": ["bug"]}}, TOOL_SCHEMA
        )


def test_required_must_be_bool() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        canonicalize_argument_constraints({"label": {"required": "yes"}}, TOOL_SCHEMA)


def test_fixed_and_max_length_constraint_kinds() -> None:
    constraints = canonicalize_argument_constraints(
        {"label": {"fixed": "bug"}, "priority": {"max_length": 3}}, TOOL_SCHEMA
    )
    assert evaluate_argument_constraints(constraints, {"label": "bug", "priority": "p1"})
    assert not evaluate_argument_constraints(constraints, {"label": "triage", "priority": "p1"})
    assert not evaluate_argument_constraints(constraints, {"label": "bug", "priority": "toolong"})


def test_canonicalize_rejects_non_string_key_without_crashing() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        canonicalize_argument_constraints(
            {1: {"required": True}, "label": {"required": True}}, TOOL_SCHEMA
        )


# --- Store invariants (review round 1, finding 2) ---


def test_advance_state_never_changes_job_revision(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    job = store.create(PluginJob.new(**_job_kwargs()))
    original_revision = job.job_revision
    updated = store.advance_state(
        job.job_id,
        cursor={"since": "2026-01-01T00:00:00Z"},
        receipt={"outcome": "changed", "at": "2026-01-01T00:00:00Z"},
        next_due_at="2026-01-01T00:15:00Z",
        paused_reason="rate_limited",
        execution_revision="f" * 64,
        runs_today=3,
    )
    assert updated.job_revision == original_revision
    cleared = store.advance_state(job.job_id, paused_reason=False)
    assert cleared.paused_reason is None
    assert cleared.job_revision == original_revision


def test_store_tampered_record_fails_closed(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    job = store.create(PluginJob.new(**_job_kwargs()))
    document = json.loads(store.document_path.read_bytes().decode("utf-8"))
    # Valid JSON, but `enabled` is flipped without recomputing job_revision --
    # the self-verifying digest in PluginJob.__post_init__ must catch this.
    document["jobs"][job.job_id]["enabled"] = False
    store.document_path.write_bytes(json.dumps(document).encode("utf-8"))
    with pytest.raises(ValueError, match="plugin_jobs_store_unavailable"):
        store.get(job.job_id)


def test_get_not_found(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    with pytest.raises(ValueError, match="plugin_job_not_found"):
        store.get("f" * 64)


def test_delete_happy_path_and_revision_conflict(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    job = store.create(PluginJob.new(**_job_kwargs()))
    with pytest.raises(ValueError, match="plugin_job_revision_conflict"):
        store.delete(job.job_id, "0" * 64)
    store.delete(job.job_id, job.job_revision)
    with pytest.raises(ValueError, match="plugin_job_not_found"):
        store.get(job.job_id)


def test_update_rejects_unknown_field(tmp_path) -> None:
    store = PluginJobStore(state_root=tmp_path)
    job = store.create(PluginJob.new(**_job_kwargs()))
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        store.update(job.job_id, job.job_revision, {"bogus_field": 1})


# --- PluginJob.__post_init__ branch coverage (review round 1, finding 2) ---


def test_trigger_schedule_requires_minimum_interval() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(
            **_job_kwargs(
                trigger={"kind": "schedule", "interval_seconds": 100, "jitter_seconds": 0}
            )
        )


def test_trigger_schedule_requires_bounded_jitter() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(
            **_job_kwargs(
                trigger={"kind": "schedule", "interval_seconds": 900, "jitter_seconds": 300}
            )
        )


def test_trigger_event_requires_minimum_poll_fallback() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(
            **_job_kwargs(trigger={"kind": "event", "poll_fallback_seconds": 100})
        )


def test_trigger_event_resource_updated_requires_resource_uri() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(
            **_job_kwargs(
                trigger={
                    "kind": "event",
                    "poll_fallback_seconds": 300,
                    "kind_watched": "resource_updated",
                }
            )
        )


def test_trigger_invalid_kind_rejected() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(trigger={"kind": "bogus"}))


def test_external_unreviewed_risk_class_always_rejected() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(
            **_job_kwargs(
                autonomy_level="act",
                allowed_tools=(
                    {
                        "tool_name": "list_issues",
                        "tool_schema_sha256": "e" * 64,
                        "risk_class": "external_unreviewed",
                        "argument_constraints": {},
                    },
                ),
            )
        )


def test_observe_mode_rejects_non_read_only_tool() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(
            **_job_kwargs(
                autonomy_level="observe",
                allowed_tools=(
                    {
                        "tool_name": "update_issue",
                        "tool_schema_sha256": "e" * 64,
                        "risk_class": "additive_write",
                        "argument_constraints": {},
                    },
                ),
            )
        )


def test_title_bounds() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(title=""))
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(title="x" * 81))


def test_objective_bounds() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(objective=""))
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(objective="x" * 2001))


def test_max_runs_per_day_bounds() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(max_runs_per_day=0))
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(max_runs_per_day=97))


def test_max_consecutive_failures_bounds() -> None:
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(max_consecutive_failures=0))
    with pytest.raises(ValueError, match="plugin_job_invalid"):
        PluginJob.new(**_job_kwargs(max_consecutive_failures=11))
