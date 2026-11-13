"""WO-02 — the interface lock: signatures, error ownership, and the future-API manifest.

Two halves, and the second is the unusual one.

**Implemented now** (this order): every signature and field set below is asserted
executably, so a later order cannot quietly widen a contract it was handed.

**Not implemented yet**: WO-03..WO-17's function signatures live in a machine-readable
manifest, `schema/episode_continuity/signature_manifest.json`, and this file asserts the
manifest's shape AND that none of those names exists in production yet. That is deliberate:
the order forbids "fake-success production stubs or failing imports for future functions",
and a stub that returned an empty tuple would be exactly the silent-empty defect
AGENTS.md section 2.1 exists to forbid. The manifest is the contract; the absence is the
proof nothing has been faked.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT
from schema import continuity_json
from schema.continuity_json import (
    CONTINUITY_SCHEMA_VERSION,
    CanonicalEpisodeUnavailable,
    CanonicalRef,
    ContinuityContractError,
    EpisodeCaptureUnavailable,
    EpisodeOwnershipError,
    EventIdentityConflict,
    HistoricalSnapshotUnavailable,
    MigrationConflict,
    Observation,
    SourceRef,
    StaleControlContext,
    TokenAccountingUnavailable,
    WindowCapacityExceeded,
    WindowMembershipError,
    WindowOutputError,
    WindowReferenceError,
    canonical_bytes,
    parse_json_object,
)

MANIFEST_PATH = Path(OMEGA_ROOT) / "schema" / "episode_continuity" / "signature_manifest.json"

TYPED_ERRORS = (
    ContinuityContractError,
    EventIdentityConflict,
    EpisodeOwnershipError,
    EpisodeCaptureUnavailable,
    CanonicalEpisodeUnavailable,
    HistoricalSnapshotUnavailable,
    WindowMembershipError,
    WindowCapacityExceeded,
    TokenAccountingUnavailable,
    WindowReferenceError,
    WindowOutputError,
    StaleControlContext,
    MigrationConflict,
)


# ── error ownership ──────────────────────────────────────────────────────────


def test_every_typed_error_subclasses_the_shared_base():
    """CONTRACTS.md section 8: one base, defined in schema/continuity_json.py."""
    assert ContinuityContractError.__module__ == "schema.continuity_json"
    for error in TYPED_ERRORS:
        assert issubclass(error, ContinuityContractError), error.__name__


def test_all_thirteen_declared_errors_exist_and_are_distinct():
    assert len(TYPED_ERRORS) == 13
    assert len({error.__name__ for error in TYPED_ERRORS}) == 13


def test_an_error_is_not_a_valid_empty_record():
    """AGENTS.md section 2.1: 'could not run' and 'ran, found nothing' never share a value."""
    for error in TYPED_ERRORS:
        instance = error("something went wrong") if error is not WindowCapacityExceeded else error(
            "over budget", member_ids=("ep-1",)
        )
        assert not isinstance(instance, (dict, list, tuple))
        assert str(instance)


def test_a_boundary_error_preserves_its_original_cause():
    try:
        try:
            raise ValueError("the substrate said no")
        except ValueError as cause:
            raise EpisodeCaptureUnavailable("intake refused the event") from cause
    except EpisodeCaptureUnavailable as exc:
        assert isinstance(exc.__cause__, ValueError)
        assert "substrate" in str(exc.__cause__)


def test_window_capacity_exceeded_carries_the_sealed_cohort():
    exc = WindowCapacityExceeded("mandatory input does not fit", member_ids=("a", "b"))
    assert exc.member_count == 2 and tuple(exc.member_ids) == ("a", "b")


# ── implemented signatures ───────────────────────────────────────────────────


def _signature(target) -> str:
    """The rendered signature, with PEP 563's string annotations unquoted.

    Every contract module uses `from __future__ import annotations`, so `inspect.signature`
    renders each annotation as a quoted string. The lock is about the shape, not the quoting.
    """
    return str(inspect.signature(target)).replace("'", "")


def test_canonical_bytes_and_parse_json_object_signatures_are_locked():
    assert _signature(canonical_bytes) == "(value: object) -> bytes"
    assert _signature(parse_json_object) == "(raw: str) -> dict[str, object]"


def test_observation_exposes_exactly_state_value_reason():
    assert tuple(f.name for f in Observation.__dataclass_fields__.values()) == ("state", "value", "reason")


def test_canonical_ref_exposes_its_three_required_fields_plus_the_optional_locator():
    assert tuple(CanonicalRef.__dataclass_fields__) == (
        "record_id",
        "record_version",
        "content_sha256",
        "relative_path",
    )


def test_source_ref_cannot_be_mistaken_for_a_canonical_ref():
    assert "record_version" not in SourceRef.__dataclass_fields__
    assert "content_sha256" not in SourceRef.__dataclass_fields__


@pytest.mark.parametrize(
    "module_name,expected",
    [
        (
            "schema.episode_event",
            ("EPISODE_PHASES", "EVIDENCE_CLASSES", "EVENT_STATUSES", "EpisodeEvent"),
        ),
        (
            "schema.episode_record",
            (
                "ACTION_STATUSES",
                "CASCADE_STATUSES",
                "EPISODE_RECORD_BLOCKS",
                "EpisodeOutcome",
                "EpisodeRecord",
                "FEEDBACK_STATUSES",
                "MEMORY_COMMIT_STATUSES",
                "TASK_OUTCOMES",
                "VERIFICATION_STATUSES",
                "cascade_and_task_from_outcome_pack",
            ),
        ),
        (
            "schema.window_evidence",
            (
                "WINDOW_PACKET_BLOCKS",
                "HistoryBundle",
                "HistoryLimits",
                "SnapshotRef",
                "WindowEvidencePacket",
                "WindowManifest",
                "WindowMetrics",
            ),
        ),
        (
            "schema.window_reflection",
            (
                "COGNITIVE_DELTA_KEYS",
                "REFLECTION_FIELDS",
                "REFLECTION_LIMITS",
                "TRAJECTORY_OPERATIONS",
                "WINDOW_RESPONSE_KEYS",
                "ValidatedWindowResponse",
                "validate_window_response_payload",
            ),
        ),
    ],
)
def test_each_contract_module_exports_the_names_later_orders_import(module_name, expected):
    module = importlib.import_module(module_name)
    for name in expected:
        assert hasattr(module, name), f"{module_name}.{name}"


def test_every_contract_module_declares_the_same_schema_version():
    for module_name in (
        "schema.episode_event",
        "schema.episode_record",
        "schema.window_evidence",
        "schema.window_reflection",
    ):
        module = importlib.import_module(module_name)
        assert module.SCHEMA_VERSION.startswith(CONTINUITY_SCHEMA_VERSION), module_name


def test_the_schema_version_is_the_declared_vocabulary():
    assert CONTINUITY_SCHEMA_VERSION == "omega.episode_continuity.v2"


def test_the_shared_control_object_is_imported_not_redesigned():
    """A second control vocabulary is the thing CONTRACTS.md section 6 forbids outright."""
    import schema.window_reflection as window_reflection

    source = Path(inspect.getsourcefile(window_reflection)).read_text(encoding="utf-8")
    assert "from schema.omega_control_decision import" in source
    for forbidden in ('"DONE"', '"PRESSURE"', '"ASK_OPERATOR"'):
        assert forbidden not in source, "control decisions must come from the shared validator"


def test_the_pressure_intents_are_not_widened_by_this_order():
    from schema.omega_control_decision import PRESSURE_INTENTS

    assert PRESSURE_INTENTS == frozenset({"BROWSE", "USE_SKILL", "USE_TOOL", "LEARN", "ASK_OPERATOR"})
    assert "PURSUE_OUTCOME" not in PRESSURE_INTENTS


# ── the future-API manifest ──────────────────────────────────────────────────


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_the_signature_manifest_exists_and_declares_its_own_version():
    manifest = _manifest()
    assert manifest["schema_version"] == "omega.episode_continuity.signatures.v1"
    assert manifest["contract_version"] == CONTINUITY_SCHEMA_VERSION


def test_every_manifest_entry_names_its_owner_order_module_and_signature():
    for entry in _manifest()["future_apis"]:
        assert entry["owner_order"].startswith("WO-")
        assert entry["module"].count(".") >= 1
        assert entry["name"] and entry["signature"].startswith("(")
        assert entry["purpose"]


def test_the_manifest_covers_every_planned_api_contracts_md_names():
    manifest = _manifest()
    names = {entry["name"] for entry in manifest["future_apis"]}
    names |= {entry["name"] for entry in manifest.get("implemented_by_wo_03", ())}
    names |= {entry["name"] for entry in manifest.get("implemented_by_wo_07", ())}
    names |= {entry["name"] for entry in manifest.get("implemented_by_wo_08", ())}
    names |= {entry["name"] for entry in manifest.get("implemented_by_wo_09", ())}
    names |= {entry["name"] for entry in manifest.get("implemented_by_wo_10", ())}
    names |= {entry["name"] for entry in manifest.get("implemented_by_wo_11", ())}
    for planned in (
        "record_event",
        "read_episode_events",
        "seal_episode_record",
        "resolve_episode_commit",
        "migrate_episode_document",
        "seal_window",
        "hydrate_window",
        "collect_history",
        "build_window_metrics",
        "assemble_window_packet",
        "build_budgeted_window_request",
        "validate_window_response",
        "stage_window_findings",
        "evaluate_learning_links",
        "validate_window_control",
        "stage_window_control",
    ):
        assert planned in names, planned


def test_no_future_api_has_been_faked_as_a_production_stub():
    """The order's own rule. An empty-returning stub is the silent-empty defect class."""
    for entry in _manifest()["future_apis"]:
        try:
            module = importlib.import_module(entry["module"])
        except ModuleNotFoundError:
            continue  # the honest state before its owner order runs
        assert not hasattr(module, entry["name"]), (
            f"{entry['module']}.{entry['name']} exists before {entry['owner_order']} implemented it"
        )


def test_the_manifest_owners_match_the_contract_ownership_table():
    owners = {entry["name"]: entry["owner_order"] for entry in _manifest()["future_apis"]}
    assert "record_event" not in owners, "WO-03 implemented it; it is no longer pending"
    assert "seal_episode_record" not in owners, "WO-07 implemented it; it is no longer pending"
    assert "resolve_episode_commit" not in owners, "WO-07 implemented it; it is no longer pending"
    assert "migrate_episode_document" not in owners, "WO-08 implemented it; it is no longer pending"
    assert "seal_window" not in owners, "WO-09 implemented it; it is no longer pending"
    assert "hydrate_window" not in owners, "WO-09 implemented it; it is no longer pending"
    assert "collect_history" not in owners, "WO-10 implemented it; it is no longer pending"
    assert "build_window_metrics" not in owners, "WO-11 implemented it; it is no longer pending"
    assert owners["assemble_window_packet"] == "WO-12"
    assert owners["build_budgeted_window_request"] == "WO-12"
    assert owners["validate_window_response"] == "WO-13"
    assert owners["stage_window_findings"] == "WO-13"
    assert owners["evaluate_learning_links"] == "WO-14"
    assert owners["validate_window_control"] == "WO-15"
    assert owners["stage_window_control"] == "WO-15"


# ── the reused authorities are reused, not re-implemented ─────────────────────


def test_this_order_adds_no_second_clock_store_or_reconciler():
    sources = [
        Path(inspect.getsourcefile(importlib.import_module(name))).read_text(encoding="utf-8")
        for name in (
            "schema.continuity_json",
            "schema.episode_event",
            "schema.episode_record",
            "schema.window_evidence",
            "schema.window_reflection",
        )
    ]
    for source in sources:
        for forbidden in (
            "def finish_line_stamp",
            "def reconcile_at_boot",
            "def write_memory",
            "def intake_memory",
            "threading.Thread",
            "sqlite3",
        ):
            assert forbidden not in source, forbidden


def test_the_contract_modules_perform_no_io_at_import():
    """A schema module that reads the filesystem at import cannot be a pure contract."""
    for source in (
        Path(inspect.getsourcefile(continuity_json)).read_text(encoding="utf-8"),
        Path(inspect.getsourcefile(importlib.import_module("schema.episode_record"))).read_text(encoding="utf-8"),
    ):
        assert "open(" not in source
        assert "read_text(" not in source


def test_the_fixture_corpus_names_every_case_the_order_requires():
    fixtures = Path(OMEGA_ROOT) / "tests" / "continuity" / "fixtures" / "contracts"
    present = {path.stem for path in fixtures.glob("*.json")}
    for required in (
        "valid_event",
        "valid_episode",
        "ten_member_packet",
        "valid_window_result",
        "late_feedback_event",
        "unknown_coverage_historical_conversion",
        "conflicting_id_event",
    ):
        assert required in present, required


def test_every_fixture_validates_through_production_schema_code():
    """A fixture the reader happens to accept proves nothing; it must pass the real schema."""
    from tests.helpers.continuity_records import load_contract_fixture

    for name in (
        "valid_event",
        "valid_episode",
        "ten_member_packet",
        "valid_window_result",
        "late_feedback_event",
        "unknown_coverage_historical_conversion",
    ):
        assert load_contract_fixture(name) is not None


def test_the_conflicting_id_fixture_is_rejected_by_production_code():
    from tests.helpers.continuity_records import load_contract_fixture

    with pytest.raises(EventIdentityConflict):
        load_contract_fixture("conflicting_id_event")


# ── the lifecycle of the absence guard itself (WO-02A obligation 2) ────────────


def test_the_manifest_states_how_an_absence_guard_retires():
    """No-stub protection is correct only while an API is unimplemented.

    Without this, the guard reads as a permanent ban and the first order to implement its API
    would be tempted to delete the whole interface lock to get green.
    """
    lifecycle = _manifest()["lifecycle"]
    assert "no-stub protection" in lifecycle["note"]
    assert "REPLACES" in lifecycle["on_implementation"]
    assert "still-pending" in lifecycle["on_implementation"]


def test_the_next_transition_is_always_a_concrete_owned_obligation():
    """Order-agnostic: whichever order is next, the obligation must be specific.

    The WO-03-specific form of this test went stale the moment WO-03 retired its guards, which
    is the point — the transition advances. The order-specific assertion lives in its own test
    below, so this one keeps guarding the SHAPE without having to be rewritten every order.
    """
    transition = _manifest()["lifecycle"]["next_transition"]
    assert transition["owner_order"].startswith("WO-")
    assert transition["names"], "a transition must name the APIs it retires"
    action = transition["required_action"]
    assert "executable signature" in action and "behavioural" in action
    assert "keep their absence guard" in action or "keep its guard" in action


def test_every_named_transition_api_is_still_a_pending_future_entry():
    """The transition list may only name APIs that have not been implemented yet."""
    manifest = _manifest()
    pending = {entry["name"] for entry in manifest["future_apis"]}
    for name in manifest["lifecycle"]["next_transition"]["names"]:
        assert name in pending, f"{name} is no longer pending; retire its guard per the lifecycle"


# ── WO-03: the two retired absence guards, now executable checks ───────────────


def test_the_two_wo_03_apis_exist_with_their_locked_signatures():
    """The transition the lifecycle demanded: absence guard OUT, real checks IN."""
    from memory.episode_events import read_episode_events, record_event

    # Amended 2026-09-22 (known-issues pass): the producer states the event's evidence_class,
    # the binding WO-05 left unbuilt because this signature was locked.
    assert _signature(record_event) == (
        "(episode_id: str, *, operation_id: str, phase: str, event_kind: str, payload: dict, "
        "evidence_refs: tuple[str, ...] = (), occurred_at: str | None = None, "
        "evidence_class: str = host_observation) -> EpisodeEvent"
    )
    assert _signature(read_episode_events) == "(episode_id: str) -> tuple[EpisodeEvent, ...]"


def test_the_manifest_records_the_wo_03_transition_as_completed():
    manifest = _manifest()
    implemented = {entry["name"] for entry in manifest.get("implemented_by_wo_03", ())}
    assert implemented == {"record_event", "read_episode_events"}
    pending = {entry["name"] for entry in manifest["future_apis"]}
    assert not (implemented & pending), "an implemented API must not also be guarded as pending"
    completed = manifest["lifecycle"]["completed_transitions"]
    assert any(row["owner_order"] == "WO-03" for row in completed)


def test_every_other_pending_guard_is_still_in_place():
    """Two guards retired at WO-03, two at WO-07, one at WO-08, two at WO-09, one at WO-10, one at
    WO-11. The lock was not deleted."""
    manifest = _manifest()
    pending = {entry["name"] for entry in manifest["future_apis"]}
    assert len(pending) == 8, f"expected 8 still-pending APIs, got {len(pending)}"
    assert not pending & {"seal_episode_record", "resolve_episode_commit",
                          "migrate_episode_document", "seal_window", "hydrate_window",
                          "collect_history", "build_window_metrics"}
    for name in (
        "assemble_window_packet",
        "build_budgeted_window_request",
        "validate_window_response",
        "stage_window_findings",
        "evaluate_learning_links",
        "validate_window_control",
        "stage_window_control",
    ):
        assert name in pending, name


def test_the_next_transition_now_names_wo_12():
    transition = _manifest()["lifecycle"]["next_transition"]
    assert transition["owner_order"] == "WO-12"
    assert tuple(transition["names"]) == ("assemble_window_packet",
                                          "build_budgeted_window_request")


# ── WO-10: the retired absence guard, now an executable check ──────────────────


def test_the_wo_10_api_exists_with_its_locked_signature():
    from omega_loop.window.history import collect_history

    assert _signature(collect_history) == (
        "(manifest: WindowManifest, episodes: tuple[EpisodeRecord, ...], *, "
        "limits: HistoryLimits) -> HistoryBundle"
    )


def test_the_manifest_records_the_wo_10_transition_as_completed():
    manifest = _manifest()
    implemented = {entry["name"] for entry in manifest.get("implemented_by_wo_10", ())}
    assert implemented == {"collect_history"}
    assert not implemented & {entry["name"] for entry in manifest["future_apis"]}
    assert any(row["owner_order"] == "WO-10" for row in manifest["lifecycle"]["completed_transitions"])


def test_the_history_behaviour_is_pinned_not_just_its_signature():
    """A foreign retrieval policy is refused, and so is an incomplete cohort — before any read."""
    from omega_loop.window.history import HISTORY_POLICY_REVISION, collect_history
    from schema.continuity_json import WindowMembershipError
    from schema.window_evidence import HistoryLimits
    from tests.helpers.continuity_records import make_window_manifest

    incomplete = make_window_manifest(complete=False)
    with pytest.raises(ContinuityContractError, match="policy"):
        collect_history(incomplete, (), limits=HistoryLimits(
            record_cap=4, token_candidate_cap=64, retrieval_policy_revision="someone.else/9"))
    with pytest.raises(WindowMembershipError, match="complete"):
        collect_history(incomplete, (), limits=HistoryLimits(
            record_cap=4, token_candidate_cap=64,
            retrieval_policy_revision=HISTORY_POLICY_REVISION))


def test_the_history_module_adds_no_writer_clock_or_store():
    """History reads the existing snapshot authority; it writes nothing and owns no store."""
    import omega_loop.window.history as history

    source = Path(inspect.getsourcefile(history)).read_text(encoding="utf-8")
    for banned in ("write_memory", "intake_memory", "write_text", "atomic_replace", "sqlite3",
                   "def finish_line_stamp", "record_event(", "threading.Thread",
                   "_load_subdomain_metas", "read_trajectory("):
        assert banned not in source, banned


# ── WO-11: the retired absence guard, now an executable check ──────────────────


def test_the_wo_11_api_exists_with_its_locked_signature():
    from omega_loop.window.metrics import build_window_metrics

    assert _signature(build_window_metrics) == (
        "(episodes: tuple[EpisodeRecord, ...], *, history: HistoryBundle) -> WindowMetrics"
    )


def test_the_manifest_records_the_wo_11_transition_as_completed():
    manifest = _manifest()
    implemented = {entry["name"] for entry in manifest.get("implemented_by_wo_11", ())}
    assert implemented == {"build_window_metrics"}
    assert not implemented & {entry["name"] for entry in manifest["future_apis"]}
    assert any(row["owner_order"] == "WO-11"
               for row in manifest["lifecycle"]["completed_transitions"])


def test_the_metric_behaviour_is_pinned_not_just_its_signature():
    """Every row carries its populations and version; a rate over nothing is undefined, never a
    zero; and neither input may be something other than what the contract names."""
    from omega_loop.window.metrics import METRICS_COMPUTATION_VERSION, build_window_metrics
    from tests.helpers import continuity_metrics as cm

    with pytest.raises(ContinuityContractError, match="HistoryBundle"):
        build_window_metrics((), history={})
    with pytest.raises(ContinuityContractError, match="EpisodeRecord"):
        build_window_metrics(({"episode_id": "ep-iface-11"},), history=cm.history())

    metrics = build_window_metrics(cm.seed_records(), history=cm.history())
    assert metrics.computation_version == METRICS_COMPUTATION_VERSION
    for row in metrics.rows:
        assert row["computation_version"] == METRICS_COMPUTATION_VERSION
        assert row["observation"] in ("measured", "undefined")
        if row["observation"] == "measured":
            assert row["denominator"] >= 1
            assert len(row["numerator_refs"]) == row["numerator"]
        else:
            assert (row["numerator"], row["denominator"]) == (0, 0)
            assert row["reason"]
    verified = cm.only(metrics, "verified_success")
    cascade = cm.only(metrics, "cascade_completed")
    assert cascade["numerator"] > verified["numerator"], (
        "a completed cascade was counted as a verified success"
    )


def test_the_metrics_module_reads_no_clock_store_or_configuration():
    """A measurement that reaches for a clock or a store is not reproducible."""
    import omega_loop.window.metrics as metrics

    source = Path(inspect.getsourcefile(metrics)).read_text(encoding="utf-8")
    for banned in ("write_memory", "intake_memory", "write_text", "sqlite3", "record_event(",
                   "threading.Thread", "datetime.now", "time.time", "load_config",
                   "read_trajectory(", "open("):
        assert banned not in source, banned


# ── WO-09: the two retired absence guards, now executable checks ───────────────


def test_the_two_wo_09_apis_exist_with_their_locked_signatures():
    from omega_loop.window.membership import hydrate_window, seal_window

    assert _signature(seal_window) == "(window, *, snapshot: SnapshotRef) -> WindowManifest"
    assert _signature(hydrate_window) == (
        "(manifest: WindowManifest) -> tuple[EpisodeRecord, ...]"
    )


def test_the_manifest_records_the_wo_09_transition_as_completed():
    manifest = _manifest()
    implemented = {entry["name"] for entry in manifest.get("implemented_by_wo_09", ())}
    assert implemented == {"seal_window", "hydrate_window"}
    assert not implemented & {entry["name"] for entry in manifest["future_apis"]}
    assert any(row["owner_order"] == "WO-09" for row in manifest["lifecycle"]["completed_transitions"])


def test_the_membership_behaviour_is_pinned_not_just_its_signature():
    """A collecting window is never sealed, and an incomplete manifest is never hydrated."""
    from omega_loop.window.membership import hydrate_window, seal_window
    from pressure_loop.reflection_window import open_reflection_window
    from schema.continuity_json import WindowMembershipError
    from tests.helpers.continuity_records import make_snapshot_ref, make_window_manifest

    collecting = open_reflection_window("ep-iface-09", trajectory_snapshot={})
    with pytest.raises(WindowMembershipError, match="collecting"):
        seal_window(collecting, snapshot=make_snapshot_ref())
    with pytest.raises(WindowMembershipError, match="not complete"):
        hydrate_window(make_window_manifest(complete=False))


def test_the_membership_module_adds_no_writer_clock_or_store():
    """Membership reads the existing checkpoint, stamp, commit view and configuration."""
    import omega_loop.window.membership as membership

    source = Path(inspect.getsourcefile(membership)).read_text(encoding="utf-8")
    for banned in ("write_memory", "intake_memory", "write_text", "atomic_replace", "sqlite3",
                   "def finish_line_stamp", "record_event(", "threading.Thread",
                   "save_staged_windows", "save_reflection_window", "_load_subdomain_metas"):
        assert banned not in source, banned


# ── WO-08: the retired absence guard, now an executable check ──────────────────


def test_the_wo_08_api_exists_with_its_locked_signature():
    from scripts.migrate_episode_continuity import migrate_episode_document

    assert _signature(migrate_episode_document) == "(document: dict) -> MigrationResult"


def test_the_manifest_records_the_wo_08_transition_as_completed():
    manifest = _manifest()
    implemented = {entry["name"] for entry in manifest.get("implemented_by_wo_08", ())}
    assert implemented == {"migrate_episode_document"}
    assert not implemented & {entry["name"] for entry in manifest["future_apis"]}
    assert any(row["owner_order"] == "WO-08" for row in manifest["lifecycle"]["completed_transitions"])


def test_the_converter_behaviour_is_pinned_not_just_its_signature():
    """Pure, and absence stays absence: a record with no thread converts to not_captured."""
    import json

    from scripts.migrate_episode_continuity import migrate_episode_document

    body = {"episode_id": "ep-iface-08", "cascade_status": "completed",
            "outcome": {"outcome": {"status": "completed"}}}
    document = {"id": "9.01.00000008", "coord": [9, 1, 0], "type": "episode",
                "text": json.dumps(body), "enrichment": {"closed_at": ""}}
    first = migrate_episode_document(document)
    assert first.status == "converted"
    assert first.record.identity["thread_id"].state == "not_captured"
    assert migrate_episode_document(document).converted_document == first.converted_document


# ── WO-07: the two retired absence guards, now executable checks ───────────────


def test_the_two_wo_07_apis_exist_with_their_locked_signatures():
    from memory.episode_commit_view import resolve_episode_commit
    from memory.episode_record_seal import seal_episode_record

    assert _signature(seal_episode_record) == (
        "(episode, *, events: tuple[EpisodeEvent, ...], effects: tuple[dict, ...]) "
        "-> EpisodeRecord"
    )
    assert _signature(resolve_episode_commit) == (
        "(record_ref: CanonicalRef, *, snapshot: SnapshotRef) -> EpisodeCommitView"
    )


def test_the_manifest_records_the_wo_07_transition_as_completed():
    manifest = _manifest()
    implemented = {entry["name"] for entry in manifest.get("implemented_by_wo_07", ())}
    assert implemented == {"seal_episode_record", "resolve_episode_commit"}
    pending = {entry["name"] for entry in manifest["future_apis"]}
    assert not (implemented & pending), "an implemented API must not also be guarded as pending"
    completed = manifest["lifecycle"]["completed_transitions"]
    assert any(row["owner_order"] == "WO-07" for row in completed)


def test_the_seal_module_adds_no_writer_clock_or_store():
    """Sealing is pure content assembly: no write path, no clock of its own, no store."""
    import ast

    import memory.episode_record_seal as seal

    source = Path(inspect.getsourcefile(seal)).read_text(encoding="utf-8")
    for banned in ("def finish_line_stamp", "write_memory", "intake_memory", "sqlite3",
                   "open(", "record_event(", "threading.Thread"):
        assert banned not in source, banned
    names = {node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef)}
    assert "seal_episode_record" in names


def test_the_commit_view_is_a_read_only_join():
    """The view joins existing authorities; it never writes, and it holds no store of its own."""
    import memory.episode_commit_view as view

    source = Path(inspect.getsourcefile(view)).read_text(encoding="utf-8")
    for banned in ("write_memory", "intake_memory", "mark_committed", "mark_rejected",
                   "mark_consumed", "write_text", "sqlite3", "atomic_replace", "record_event("):
        assert banned not in source, banned


def test_the_seal_behaviour_is_pinned_not_just_its_signature():
    """A carrier without a finish-line stamp has no place in memory; sealing it is refused."""
    from types import SimpleNamespace

    from memory.episode_record_seal import seal_episode_record

    with pytest.raises(ContinuityContractError):
        seal_episode_record(SimpleNamespace(episode_id="ep-x", timekeeper={}), events=(),
                            effects=())


def test_the_capture_module_adds_no_second_substrate():
    """WO-03 reuses the journal and SRAM; it introduces no store, clock or scheduler."""
    import inspect

    import memory.episode_events as capture

    source = Path(inspect.getsourcefile(capture)).read_text(encoding="utf-8")
    for banned in (
        "def finish_line_stamp",
        "def write_memory",
        "sqlite3",
        "threading.Thread",
        "open(",
        "jsonl",
    ):
        assert banned not in source, banned

    # Durability and working state come from the existing owners, by name — asserted on the
    # parsed imports rather than on one exact import LINE, which only pinned the formatting of a
    # single call site and broke when WO-03A moved the ownership check into its own helper.
    import ast

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "memory":
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("memory."):
            imported.add(str(node.module))
        elif isinstance(node, ast.Import):
            imported.update(
                alias.name for alias in node.names if alias.name.split(".")[0] == "memory"
            )
    assert {"episode_sram", "intake_journal"} <= imported, imported
    assert imported <= {"episode_sram", "intake_journal"}, (
        f"capture reaches a memory module outside its two declared owners: {imported}"
    )
    assert "owner_for_intake" in source
    assert "bound_episode_id" in source, "ownership is authorized against the caller's binding"


def test_capture_behaviour_is_pinned_not_just_its_signature():
    """The lifecycle requires BEHAVIOURAL checks, not only a signature match."""
    from memory import episode_sram
    from memory.episode_events import read_episode_events, record_event

    episode_sram.reset()
    episode_sram.open_bucket("ep-iface-0001", pipeline_id=0)
    try:
        first = record_event(
            "ep-iface-0001", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1}
        )
        again = record_event(
            "ep-iface-0001", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 1}
        )
        assert (first.event_id, first.event_seq) == (again.event_id, again.event_seq)
        with pytest.raises(EventIdentityConflict):
            record_event(
                "ep-iface-0001", operation_id="op-1", phase="auto", event_kind="k", payload={"v": 2}
            )
        assert len(read_episode_events("ep-iface-0001")) == 1
    finally:
        episode_sram.reset()
