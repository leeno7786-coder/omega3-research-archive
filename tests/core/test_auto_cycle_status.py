"""_auto_cycle_status + disposition: a YIELD/PARTIAL is progress owed, not a failure.

Operator live finding 2026-07-01: a yielded build was classified failed, so the mind read
"previous attempt failed" and pivoted to diagnose a phantom failure instead of resuming. A yield
must be a NON-failure disposition so the mind gets the build_in_progress CONTINUE|FINISH prompt
and owns the decision.
"""

from __future__ import annotations

import importlib

from auto_loop.loop import _auto_cycle_status
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_catalog import PluginToolBinding
from meta_loop.disposition.run import derive_disposition
from pressure_loop.contributors import EpisodeIgnition, from_operator_message
from tests.core.rebuild_helpers import form_intent

global_intent = importlib.import_module("global_loop.intent.run")

_INTERPRETER = "RESPOND"


def _plugin_binding(
    *,
    candidate: str,
    capability: str,
    title: str,
    properties: dict[str, object],
    required: list[str],
) -> PluginToolBinding:
    input_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"item": {"type": "string"}},
        "required": ["item"],
        "additionalProperties": False,
    }
    return PluginToolBinding(
        candidate_id=candidate * 64,
        tool_name=f"tool_{capability}",
        plugin_id="plugin.synthetic",
        plugin_revision="a" * 64,
        app_id="app.synthetic",
        account_key_hash="b" * 64,
        connection_revision="c" * 64,
        capability=capability,
        title=title,
        description=title,
        input_schema=input_schema,
        input_schema_sha256=sha256_hex(canonical_bytes(input_schema)),
        output_schema=output_schema,
        output_schema_sha256=sha256_hex(canonical_bytes(output_schema)),
        risk_class=RiskClass.READ_ONLY,
        effect="read",
        requires_revision=False,
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )


def _catalog(monkeypatch, resolver) -> None:
    monkeypatch.setattr("mcp_platform.plugin_dispatch.plugin_tool_bindings", resolver)


def _live_chat_intent(message: str, *, ingress_class: str) -> str:
    contributor = from_operator_message(
        {
            "message": message,
            "ingress_class": ingress_class,
            "cycle_id": "cycle-plugin-chat",
        }
    )
    ignition = EpisodeIgnition(
        episode_id="ep-plugin-chat",
        aggregate_pressure=1.0,
        opened_by=contributor.type,
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )
    return form_intent(ignition).intent


def test_yield_is_partial_not_failed():
    assert _auto_cycle_status("skill_yielded", "USE_SKILL") == ("partial", "partial")


def test_partial_is_partial_not_failed():
    assert _auto_cycle_status("skill_partial", "USE_SKILL") == ("partial", "partial")


def test_real_failure_still_failed():
    assert _auto_cycle_status("skill_no_evidence", "USE_SKILL") == ("failed", "fail")
    assert _auto_cycle_status("tool_failed", "USE_TOOL") == ("failed", "fail")


def test_staged_hold_still_skipped():
    assert _auto_cycle_status("apply_pending_approval", "APPLY") == ("skipped", "skipped")
    assert _auto_cycle_status("skill_requires_confirmation", "USE_SKILL") == ("skipped", "skipped")


def test_plugin_permission_hold_is_pending_input_not_failure():
    assert _auto_cycle_status("pending_input", "PLUGIN_OPERATION") == (
        "skipped",
        "pending_input",
    )


def test_generic_plugin_eligibility_does_not_capture_ordinary_commands(monkeypatch):
    _catalog(monkeypatch, lambda: ())
    assert global_intent._map_opened_by_to_intent("operator_command", "inspect this repo") == (
        _INTERPRETER
    )


def test_one_planner_complete_candidate_reaches_native_model(monkeypatch):
    binding = _plugin_binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    _catalog(monkeypatch, lambda: (binding,))

    assert (
        global_intent._map_opened_by_to_intent("operator_command", "show tomorrow's events")
        == "RESPOND"
    )


def test_two_overlapping_tools_stay_on_native_model_rail(monkeypatch):
    first = _plugin_binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    second = _plugin_binding(
        candidate="b",
        capability="search_events",
        title="Search tomorrow events",
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    _catalog(monkeypatch, lambda: (first, second))

    assert (
        global_intent._map_opened_by_to_intent("operator_command", "show tomorrow's events")
        == "RESPOND"
    )


def test_incomplete_candidate_still_reaches_native_model(monkeypatch):
    binding = _plugin_binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        properties={"calendar_ref": {"type": "string"}},
        required=["calendar_ref"],
    )
    _catalog(monkeypatch, lambda: (binding,))

    assert (
        global_intent._map_opened_by_to_intent("operator_command", "show tomorrow's events")
        == "RESPOND"
    )


def test_ordinary_chat_with_signed_candidates_enters_native_model(monkeypatch):
    drive = _plugin_binding(
        candidate="a",
        capability="list_recent_files",
        title="List recent Drive files",
        properties={"query": {"type": "string"}},
        required=["query"],
    )
    calendar = _plugin_binding(
        candidate="b",
        capability="list_events",
        title="List tomorrow events",
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    _catalog(monkeypatch, lambda: (drive, calendar))

    assert (
        _live_chat_intent("ask Drive for recent files", ingress_class="phatic")
        == "RESPOND"
    )
    assert _live_chat_intent("show tomorrow's events", ingress_class="phatic") == "RESPOND"


def test_ordinary_question_without_plugin_overlap_stays_respond(monkeypatch):
    binding = _plugin_binding(
        candidate="a",
        capability="list_recent_files",
        title="List recent Drive files",
        properties={"query": {"type": "string"}},
        required=["query"],
    )
    _catalog(monkeypatch, lambda: (binding,))

    assert _live_chat_intent("What is Python?", ingress_class="epistemic_request") == "RESPOND"


def test_retrospective_plugin_question_stays_on_memory_recall_rail(monkeypatch):
    _catalog(monkeypatch, lambda: (_ for _ in ()).throw(AssertionError("routing must not consult catalog")))

    assert (
        _live_chat_intent("what did Slack say about shipping?", ingress_class="epistemic_request")
        == "RESPOND"
    )


def test_enabled_drive_catalog_routes_relevant_chat_without_stealing_ordinary_chat(
    tmp_path, monkeypatch
) -> None:
    from mcp_platform.plugin_lifecycle import PluginLifecycleService
    from mcp_platform.plugin_dispatch import plugin_tool_bindings as real_bindings
    from tests.mcp_platform.test_plugin_source_of_truth import seat_connected_plugin

    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "operator-account-key")
    lifecycle = PluginLifecycleService(state_root=tmp_path / "plugins")
    review = lifecycle.review("google.workspace.drive")
    lifecycle.install("google.workspace.drive", confirmation=review.sha256)
    # Only an installed *and currently authorized* plugin reaches the catalog.
    seat_connected_plugin(lifecycle, "google.workspace.drive")

    def _drive_only_catalog():
        return real_bindings(service=lifecycle)

    _catalog(monkeypatch, _drive_only_catalog)

    assert _drive_only_catalog()
    assert (
        _live_chat_intent("find recent files in Drive", ingress_class="phatic")
        == "RESPOND"
    )
    assert _live_chat_intent("What is Python?", ingress_class="epistemic_request") == "RESPOND"


def test_incomplete_plugin_chat_reaches_native_model(monkeypatch):
    binding = _plugin_binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        properties={"calendar_ref": {"type": "string"}},
        required=["calendar_ref"],
    )
    _catalog(monkeypatch, lambda: (binding,))

    assert _live_chat_intent("show tomorrow's events", ingress_class="phatic") == "RESPOND"


def test_fetch_command_with_plugin_candidates_is_not_stolen_by_build_verbs(monkeypatch):
    binding = _plugin_binding(
        candidate="a",
        capability="list_recent_files",
        title="List recent Drive files",
        properties={"query": {"type": "string"}},
        required=["query"],
    )
    _catalog(monkeypatch, lambda: (binding,))

    assert (
        _live_chat_intent("fetch recent files from Drive", ingress_class="operator_command")
        == "RESPOND"
    )


def test_learn_and_remember_are_not_stolen_by_plugin_eligibility(monkeypatch):
    binding = _plugin_binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    _catalog(monkeypatch, lambda: (binding,))

    assert _live_chat_intent("Learn Python", ingress_class="operator_command") == "LEARN"
    assert (
        _live_chat_intent(
            "Remember that tomorrow's events stay on the family calendar",
            ingress_class="operator_command",
        )
        == "RESPOND"
    )


def test_unplannable_operator_text_never_consults_the_catalog(monkeypatch):
    _catalog(
        monkeypatch,
        lambda: (_ for _ in ()).throw(AssertionError("catalog must not be consulted")),
    )
    pasted = "set this up\nfor tomorrow's events"

    assert global_intent._map_opened_by_to_intent("operator_command", pasted) == _INTERPRETER


def test_documented_property_schema_remains_a_plannable_candidate(monkeypatch):
    documented = _plugin_binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        properties={"start": {"type": "string", "description": "ISO start date"}},
        required=["start"],
    )
    _catalog(monkeypatch, lambda: (documented,))

    # Descriptions are bounded planner annotations, not invalid schema keywords.
    # The candidate must remain reachable instead of being misreported as a
    # disconnected catalog.
    assert (
        global_intent._map_opened_by_to_intent("operator_command", "show tomorrow's events")
        == "RESPOND"
    )


def test_one_unplannable_binding_does_not_hide_a_complete_candidate(monkeypatch):
    documented = _plugin_binding(
        candidate="a",
        capability="search_events",
        title="Search tomorrow events",
        properties={"start": {"type": "string", "description": "ISO start date"}},
        required=["start"],
    )
    plannable = _plugin_binding(
        candidate="b",
        capability="list_events",
        title="List tomorrow events",
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    _catalog(monkeypatch, lambda: (documented, plannable))

    assert (
        global_intent._map_opened_by_to_intent("operator_command", "show tomorrow's events")
        == "RESPOND"
    )


def test_unconnected_plugin_account_is_not_a_catalog_outage(monkeypatch):
    from mcp_platform.plugin_dispatch import PluginAccountUnconfigured

    _catalog(
        monkeypatch,
        lambda: (_ for _ in ()).throw(PluginAccountUnconfigured("plugin_account_unconfigured")),
    )

    assert (
        global_intent._map_opened_by_to_intent("operator_command", "show tomorrow's events")
        == _INTERPRETER
    )



def test_plugin_catalog_failure_does_not_crash_operator_command(monkeypatch):
    _catalog(monkeypatch, lambda: (_ for _ in ()).throw(OSError("registry unreadable")))

    # A generic command remains on the native live-chat tool rail when the
    # optional plugin catalog cannot answer.
    assert (
        global_intent._map_opened_by_to_intent(
            "operator_command",
            "inspect this repo",
            metadata={"operator_command_kind": "inspect"},
        )
        == _INTERPRETER
    )


def test_plugin_catalog_failure_does_not_steal_a_generic_question(monkeypatch):
    _catalog(monkeypatch, lambda: (_ for _ in ()).throw(OSError("registry unreadable")))

    assert global_intent._map_opened_by_to_intent("operator_query", "what is Python?") == "RESPOND"


def test_clean_success_and_idle_unchanged():
    assert _auto_cycle_status(None, "USE_SKILL") == ("success", "success")
    assert _auto_cycle_status(None, "IDLE") == ("idle", "idle")


def test_yield_threads_to_degraded_disposition_not_failed():
    # the whole chain: yield -> partial cycle_kind -> 'degraded' disposition (non-failure), so
    # global_b summarizes "partially completed", never "Execution failed".
    _status, cycle_kind = _auto_cycle_status("skill_yielded", "USE_SKILL")
    disposition, meta_status = derive_disposition({"cycle_kind": cycle_kind})
    assert disposition == "degraded" and meta_status == "partial"
    assert disposition != "failed"
