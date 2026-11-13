from __future__ import annotations

import importlib
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


REQUEST_ID = "a" * 48
REQUEST_REVISION = "b" * 64
PLAN_ID = "c" * 64
PLAN_DIGEST = "e" * 64
PRIOR_EPISODE_ID = "episode-before-permission"
TASK_ID = "logical-plugin-task"


@pytest.fixture(autouse=True)
def _isolated_plugin_queue(tmp_path, monkeypatch):
    """Per-test intake so writes to the legacy path still migrate from a blank line."""
    import pressure_loop.cognition_queue as queue
    from memory import episode_sram

    monkeypatch.setattr(queue, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    queue._reset_cache_for_tests()
    episode_sram.reset()
    yield
    # Restore fault-injected queue primitives before resetting the cache. Some
    # cases deliberately raise a BaseException from os.replace; leaving that
    # patch active makes fixture teardown fail and contaminates later cases.
    monkeypatch.undo()
    episode_sram.reset()
    queue._reset_cache_for_tests()


def _queued_input(contributor):
    from pressure_loop.cognition_queue import contributor_dedup_key, peek_inputs
    return next(c for c in peek_inputs()
                if contributor_dedup_key(c) == contributor_dedup_key(contributor))


def _durable_queue_payload(queue) -> list:
    payload = json.loads(queue._queue_path().read_text(encoding="utf-8"))
    return payload["queue"] + [payload["permission_state"]]


def _unload_lines_keep_abandonments(queue) -> None:
    queue._clear_cached_state()
    queue._STATE.loaded = False


def _write_durable_queue_payload(queue, payload) -> None:
    contributors = [row for row in payload if "_operator_queue_state" not in row]
    state_rows = [row for row in payload if "_operator_queue_state" in row]
    target = queue._queue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"schema_version": 1, "queue": contributors,
                "permission_state": state_rows[-1] if state_rows else queue._state_row(),
                "uninterpreted": {}}
    target.write_text(json.dumps(snapshot), encoding="utf-8")


def _install_durable_queue_payload(queue, payload) -> None:
    _write_durable_queue_payload(queue, payload)
    queue._reset_cache_for_tests()


def test_waiting_chat_turn_uses_the_originating_cycle_for_permission_continuation() -> None:
    from pressure_loop.operator_chat_responses import waiting_chat_turn

    turn = waiting_chat_turn(
        {
            "contributors": [
                {
                    "id": "plugin_permission_decision",
                    "detail": {
                        "request_id": REQUEST_ID,
                        "cycle_id": f"plugin-permission:{REQUEST_ID}",
                        "task_id": "chat-cycle-original",
                    },
                }
            ]
        }
    )

    assert turn == {"cycle_id": "chat-cycle-original", "chat_bubble_waiting": True}


def test_cognition_terminus_settles_permission_owner(monkeypatch) -> None:
    import cognition_chain

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.settle_plugin_permission_decision",
        lambda request_id, *, expected_revision: seen.append(
            (request_id, expected_revision)
        )
        or True,
    )
    episode = SimpleNamespace(
        pressure={
            "contributors": [
                {
                    "id": "plugin_permission_decision",
                    "metadata": {
                        "request_id": REQUEST_ID,
                        "expected_revision": REQUEST_REVISION,
                    },
                }
            ]
        }
    )

    cognition_chain._settle_plugin_permission_decision(episode)

    assert seen == [(REQUEST_ID, REQUEST_REVISION)]


class FatalQueueBoundary(BaseException):
    """Nonordinary intake failure used to prove exact BaseException ownership."""


def _invoke_permission_intake(module, entrypoint: str, *, arrival_seq: int) -> object:
    if entrypoint == "coordinator":
        return module.enqueue_plugin_permission_decision(
            _decision(module),
            arrival_seq=arrival_seq,
        )
    import pressure_loop.operator_chat as operator_chat

    return operator_chat.handle_plugin_permission_decision(
        request_id=REQUEST_ID,
        expected_revision=REQUEST_REVISION,
        decision="allow",
        arrival_seq=arrival_seq,
    )


def _fail_reservation_and_every_cleanup_write(
    module,
    entrypoint: str,
    failure_type,
    monkeypatch,
    *,
    arrival_seq: int,
):
    import pressure_loop.cognition_queue as queue

    real_replace = queue.os.replace
    primary = failure_type("reservation committed before intake failed")
    publications = 0

    def interrupted_replace(source, target):
        nonlocal publications
        publications += 1
        if publications == 1:
            real_replace(source, target)
            raise primary
        raise OSError(f"cleanup write {publications - 1} unavailable")

    monkeypatch.setattr(queue.os, "replace", interrupted_replace)
    with pytest.raises(failure_type) as caught:
        _invoke_permission_intake(module, entrypoint, arrival_seq=arrival_seq)
    assert caught.value is primary
    return primary, real_replace


def _continuations():
    spec = importlib.util.find_spec("pressure_loop.plugin_continuations")
    assert spec is not None, "the plugin permission continuation coordinator is missing"
    return importlib.import_module("pressure_loop.plugin_continuations")


def _lineage(module, *, plan_id: str = PLAN_ID):
    return module.PluginPermissionLineage(
        prior_episode_id=PRIOR_EPISODE_ID,
        task_id=TASK_ID,
        plan_id=plan_id,
        plan_digest=PLAN_DIGEST,
    )


def _configure(module, *, plan_id: str = PLAN_ID):
    calls: list[tuple[str, str]] = []

    def resolve(request_id: str, expected_revision: str):
        calls.append((request_id, expected_revision))
        return _lineage(module, plan_id=plan_id)

    module.configure_plugin_permission_lineage_resolver(resolve)
    return calls


def _decision(module, value: str = "allow"):
    return module.PluginPermissionDecision(
        request_id=REQUEST_ID,
        expected_revision=REQUEST_REVISION,
        decision=value,
    )


def test_enqueue_persists_one_content_free_decision_without_authority_or_provider_call(
    tmp_path, monkeypatch
) -> None:
    """Catches a HUD/transport shortcut that approves, cancels, or dispatches directly."""
    module = _continuations()
    resolver_calls = _configure(module)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("decision enqueue must not mutate authority or call a provider")

    from mcp_platform.plugin_permissions import PluginPermissionStore
    from mcp_platform.plugin_runtime import PluginRuntime

    for name in ("approve", "cancel", "consume"):
        monkeypatch.setattr(PluginPermissionStore, name, forbidden)
    monkeypatch.setattr(PluginRuntime, "invoke_tool", forbidden)
    monkeypatch.setattr("cognition_chain.ignite_episode", forbidden)

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=17) is True

    assert resolver_calls == [(REQUEST_ID, REQUEST_REVISION)]
    import pressure_loop.cognition_queue as queue
    payload = _durable_queue_payload(queue)
    assert payload[:-1] == [
        {
            "queue_id": payload[0]["queue_id"],
            "lane": "chat",
            "work_kind": "operator_turn",
            "source": "operator",
            "type": "plugin_permission_decision",
            "weight": 1.0,
            "metadata": {
                "request_id": REQUEST_ID,
                "expected_revision": REQUEST_REVISION,
                "decision": "allow",
                "cycle_id": f"plugin-permission:{REQUEST_ID}",
                "prior_episode_id": PRIOR_EPISODE_ID,
                "task_id": TASK_ID,
                "plan_id": PLAN_ID,
                "plan_digest": PLAN_DIGEST,
                "arrival_seq": 17,
                "directive_intent": "PLUGIN_OPERATION",
                "ingress_class": "authority_request",
            },
        }
    ]
    assert payload[-1]["_operator_queue_state"]["plugin_permission_owners"] == [
        {
            "request_id": REQUEST_ID,
            "expected_revision": REQUEST_REVISION,
            "arrival_seq": 17,
            "state": "queued",
        }
    ]
    serialized = json.dumps(payload)
    for forbidden_key in (
        "account_key_hash",
        "arguments",
        "authority_digest",
        "grant_id",
        "nonce",
        "provider",
        "secret",
        "token",
    ):
        assert forbidden_key not in serialized


def test_restart_reloads_exact_permission_and_logical_task_lineage() -> None:
    """Catches process-local continuation state or queue serialization field loss."""
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=5)
    queue._reset_cache_for_tests()

    (reloaded,) = queue.peek_inputs()
    continuation = module.plugin_permission_continuation_from_contributor(reloaded)
    assert continuation.decision == _decision(module)
    assert continuation.lineage == _lineage(module)
    assert reloaded.metadata["expected_revision"] == REQUEST_REVISION
    assert reloaded.metadata["arrival_seq"] == 5


def test_allow_cancel_race_has_one_durable_request_winner() -> None:
    """Catches decision-sensitive deduplication that queues both Allow and Cancel."""
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    barrier = __import__("threading").Barrier(2)

    def enqueue(value: str) -> bool:
        barrier.wait()
        return module.enqueue_plugin_permission_decision(
            _decision(module, value),
            arrival_seq=31 if value == "allow" else 32,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(enqueue, ("allow", "cancel")))

    assert outcomes[0] is True
    (winner,) = queue.peek_inputs()
    assert winner.metadata["request_id"] == REQUEST_ID
    assert winner.metadata["decision"] == "allow"
    assert winner.metadata["arrival_seq"] == 31
    on_disk = _durable_queue_payload(queue)
    assert len(on_disk[:-1]) == 1


def test_queue_arrival_order_is_shared_with_chat_and_external_events() -> None:
    """Catches a plugin-only side queue or append-order race."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    from pressure_loop.cognition_queue import enqueue_input, peek_inputs

    later_chat = from_operator_message(
        {
            "message": "ordinary chat",
            "ingress_class": "phatic",
            "cycle_id": "chat-later",
            "arrival_seq": 30,
        }
    )
    first_chat = from_operator_message(
        {
            "message": "ordinary chat first",
            "ingress_class": "phatic",
            "cycle_id": "chat-first",
            "arrival_seq": 10,
        }
    )

    assert enqueue_input(later_chat, operator_activity=False)
    later_chat = _queued_input(later_chat)
    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=20)
    assert enqueue_input(first_chat, operator_activity=False)
    first_chat = _queued_input(first_chat)

    assert [item.metadata["arrival_seq"] for item in peek_inputs()] == [10, 20, 30]
    assert [item.type for item in peek_inputs()] == [
        "operator_query",
        "plugin_permission_decision",
        "operator_query",
    ]


def test_pressure_alone_ignites_one_plugin_continuation_without_dispatch(monkeypatch) -> None:
    """Catches transport-driven cognition or provider execution before AUTO."""
    module = _continuations()
    _configure(module)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pressure has not handed the decision to AUTO")

    from mcp_platform.plugin_permissions import PluginPermissionStore
    from mcp_platform.plugin_runtime import PluginRuntime
    from pressure_loop.pressure import evaluate

    for name in ("approve", "cancel", "consume"):
        monkeypatch.setattr(PluginPermissionStore, name, forbidden)
    monkeypatch.setattr(PluginRuntime, "invoke_tool", forbidden)

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=1)
    from pressure_loop.cognition_queue import lease_next_input
    ignition = evaluate([lease_next_input(0)], active_episode=False, neuromod={})

    assert ignition is not None
    assert ignition.opened_by == "plugin_permission_decision"
    assert ignition.dominant_contributor.metadata["directive_intent"] == "PLUGIN_OPERATION"
    assert ignition.dominant_contributor.metadata["prior_episode_id"] == PRIOR_EPISODE_ID
    assert ignition.dominant_contributor.metadata["task_id"] == TASK_ID
    assert ignition.dominant_contributor.metadata["plan_id"] == PLAN_ID


def test_cognition_records_content_free_sensory_and_sram_receipts() -> None:
    """Catches synthetic chat text or provider payload entering the continuation episode."""
    module = _continuations()
    _configure(module)
    import cognition_chain as chain
    from memory import episode_sram
    from pressure_loop.contributors import EpisodeIgnition
    from pressure_loop.cognition_queue import peek_inputs

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=9)
    contributor = peek_inputs()[0]
    episode = SimpleNamespace(
        pipeline_id=0, work_kind="operator_turn",
        episode_id="continuation-episode",
        pressure={
            "contributors": [
                {"id": contributor.type, "weight": 1.0, "detail": dict(contributor.metadata)}
            ]
        },
    )
    ignition = EpisodeIgnition(
        episode_id="pressure-ignition",
        aggregate_pressure=1.0,
        opened_by=contributor.type,
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )
    store = MagicMock()

    chain._open_episode_sram(episode)
    chain._record_operator_ingress(store, episode, ignition)

    sensory = store.append_sensory.call_args.args[0]
    assert sensory["kind"] == "plugin_permission_decision"
    assert sensory["source"] == "user"
    assert sensory["payload"] == {
        "request_id": REQUEST_ID,
        "expected_revision": REQUEST_REVISION,
        "decision": "allow",
        "prior_episode_id": PRIOR_EPISODE_ID,
        "task_id": TASK_ID,
        "plan_id": PLAN_ID,
        "plan_digest": PLAN_DIGEST,
    }
    (fresh,) = episode_sram.pending("continuation-episode")
    assert fresh.kind == "memory"
    assert fresh.envelope.type == "plugin_permission_decision"
    assert fresh.envelope.payload["enrichment"] == {
        "event_kind": "plugin_permission_decision",
        "episode_id": "continuation-episode",
        "request_id": REQUEST_ID,
        "expected_revision": REQUEST_REVISION,
        "decision": "allow",
        "prior_episode_id": PRIOR_EPISODE_ID,
        "task_id": TASK_ID,
        "plan_id": PLAN_ID,
        "plan_digest": PLAN_DIGEST,
    }
    receipt_json = json.dumps(fresh.envelope.payload, sort_keys=True)
    for forbidden_key in ("arguments", "authority_digest", "nonce", "provider", "token"):
        assert forbidden_key not in receipt_json


def test_permission_required_auto_outcome_still_enters_mandatory_foldback(monkeypatch) -> None:
    """Catches treating permission suspension as a terminal-response shortcut."""
    import cognition_chain as chain

    episode = SimpleNamespace(
        pipeline_id=0, work_kind="operator_turn",
        episode_id=PRIOR_EPISODE_ID,
        meta={},
        auto={
            "status": "pending_input",
            "outputs": {"plugin_permission_request": {"request_id": REQUEST_ID}},
        },
        global_={},
        pressure={"contributors": []},
    )
    store = MagicMock()
    # WO-06: the typed population; an empty one yields the labelled 0.5 prior.
    from pressure_loop.episode import OutcomePopulation

    store.recent_outcome_population.return_value = OutcomePopulation()

    monkeypatch.setattr("auto_loop.auto.run_auto_front", lambda *_a, **_k: object())
    monkeypatch.setattr(
        "autonomic.symphony_neuromod.compute_episode_neuromod", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        "autonomic.symphony_neuromod.set_current_memory_salience", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "autonomic.neuromod_reinforce.reinforce_recalled_memories", lambda *_a, **_k: None
    )
    monkeypatch.setattr(chain, "_deliver_chat_reply_at_auto", lambda *_a, **_k: None)
    monkeypatch.setattr(chain, "_comprehend_and_ingest_fetched", lambda *_a, **_k: None)
    folded: list[str] = []
    monkeypatch.setattr(
        chain,
        "_start_meta_b_global_b",
        lambda *_a, **_k: folded.extend(("meta_b", "global_b", "omega")),
    )

    chain._start_auto(store, episode)

    assert folded == ["meta_b", "global_b", "omega"]
    assert episode.auto["status"] == "pending_input"


def test_continuation_uses_new_physical_episode_and_one_deepsleep_terminus(
    tmp_path, monkeypatch
) -> None:
    """Run the production fold/terminus routers; replace only model and heavy I/O leaves."""
    module = _continuations()
    _configure(module)
    import cognition_chain as chain
    import deepsleep_loop.loop as deepsleep
    import global_loop.loop as global_loop
    import meta_loop.meta_b as meta_b
    import omega_loop.episode.run as omega_episode
    from pressure_loop.episode import EpisodeStore
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.pressure import evaluate

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=1)
    from dataclasses import replace
    from pressure_loop.cognition_queue import lease_next_input
    ignition = replace(evaluate([lease_next_input(0)], active_episode=False, neuromod={}),
                       pipeline_id=0, work_kind="operator_turn")
    assert ignition is not None
    assert len(peek_inputs()) == 1

    stage_order: list[str] = []

    def meta_a_leaf(episode):
        episode.meta = dict(episode.meta or {})
        episode.meta["execution_action_pack"] = {
            "episode_id": episode.episode_id,
            "action": "RESPONSE",
            "tool": None,
            "execution_intent": "respond",
            "execution_inputs": {},
        }
        return episode.meta["execution_action_pack"]

    def permission_required_leaf(episode):
        pack = dict(episode.meta["execution_action_pack"])
        global_ = episode.global_ or {}
        episode.auto = {
            "episode_id": episode.episode_id,
            # Real AUTO's fold echoes Global A's full source goal.
            "source_action_goal": dict(
                global_.get("intent_decision") or global_.get("action_goal") or {}
            ),
            "execution_pack": pack,
            "status": "pending_input",
            "cycle_kind": "skipped",
            "outputs": {"plugin_permission_request": {"request_id": REQUEST_ID}},
            "memory": {"memory_status": "skipped"},
            "telemetry": {"cycle_kind": "skipped", "runtime_ms": 0, "warnings": []},
            "prompt_pack": {},
        }
        return episode.auto

    original_meta_b = meta_b.run_meta_b_front
    original_global_b = global_loop.run_global_b
    original_omega = omega_episode.run_omega_for_episode
    original_deepsleep = deepsleep.run_deepsleep_for_episode

    def traced_meta_b(episode):
        stage_order.append("meta_b")
        return original_meta_b(episode)

    def traced_global_b(episode, reflection):
        stage_order.append("global_b")
        return original_global_b(episode, reflection)

    omega_json = json.dumps(
        {
            "knowledge_to_ingest": [],
            "mastery": {
                "knowledge_mastery": 0.0,
                "application_mastery": 0.0,
                "rationale": "permission continuation is awaiting authority handling",
            },
            "pressure_params": {},
            "response_text": "Permission decision received.",
        }
    )

    def traced_omega(episode, **_kwargs):
        stage_order.append("omega")
        return original_omega(episode, reason_fn=lambda _system, _user: omega_json)

    def lightweight_deepsleep_core(_store, context_id, _intake_builder, **_kwargs):
        from memory import episode_sram

        episode_sram.flush(
            context_id,
            consolidate=lambda _items: {
                "enabled": True,
                "ingested": [],
                "turns": [],
                "recalls": [],
                "memories": ["content-free-permission-receipt"],
                "errors": [],
                "skipped": [],
            },
        )
        return SimpleNamespace(episode_id=context_id, journal_entry={})

    def traced_deepsleep(store, *, cascade_status="completed"):
        stage_order.append("deepsleep")
        return original_deepsleep(store, cascade_status=cascade_status)

    monkeypatch.setattr("meta_loop.execution_contract.motor_programs_enabled", lambda policy=None: False)
    monkeypatch.setattr("meta_loop.loop.run_meta_a", meta_a_leaf)
    monkeypatch.setattr("auto_loop.auto.run_auto_front", permission_required_leaf)
    monkeypatch.setattr(meta_b, "run_meta_b_front", traced_meta_b)
    monkeypatch.setattr(global_loop, "run_global_b", traced_global_b)
    monkeypatch.setattr(omega_episode, "run_omega_for_episode", traced_omega)
    monkeypatch.setattr(deepsleep, "_run_deepsleep_episode_core", lightweight_deepsleep_core)
    monkeypatch.setattr(deepsleep, "run_deepsleep_for_episode", traced_deepsleep)
    monkeypatch.setattr(chain, "_record_operator_outcome", lambda *_a, **_k: None)
    monkeypatch.setattr(chain, "_record_system_message", lambda *_a, **_k: None)
    monkeypatch.setattr(chain, "_record_proactive_chat", lambda *_a, **_k: None)

    store = EpisodeStore(
        reflection_window_path=tmp_path / "reflection.json",
        trajectory_path=tmp_path / "trajectory.json",
    )
    episode = chain.ignite_episode(store, ignition)

    assert episode.episode_id != PRIOR_EPISODE_ID
    assert episode.opened_by == "plugin_permission_decision"
    assert episode.pressure["contributors"][0]["detail"]["prior_episode_id"] == PRIOR_EPISODE_ID
    assert episode.pressure["contributors"][0]["detail"]["task_id"] == TASK_ID
    assert episode.pressure["contributors"][0]["detail"]["plan_id"] == PLAN_ID
    assert episode.auto["status"] == "pending_input"
    assert stage_order == ["meta_b", "global_b", "omega", "deepsleep"]
    assert peek_inputs() == ()


def test_transport_handler_only_stamps_arrival_and_enqueues(monkeypatch) -> None:
    """Catches a future HTTP/WebSocket handler calling cognition or plugin runtime."""
    module = _continuations()
    _configure(module)
    import pressure_loop.operator_chat as operator_chat
    from pressure_loop.cognition_queue import peek_inputs

    def forbidden(*_args, **_kwargs):
        raise AssertionError("transport must not ignite cognition")

    real_reserve = operator_chat.reserve_plugin_permission_arrival

    def reserve(request_id, expected_revision, arrival_seq):
        assert arrival_seq == 73
        return real_reserve(request_id, expected_revision, arrival_seq)

    monkeypatch.setattr(operator_chat, "reserve_plugin_permission_arrival", reserve)
    monkeypatch.setattr("cognition_chain.ignite_episode", forbidden)

    response = operator_chat.handle_plugin_permission_decision(
        request_id=REQUEST_ID,
        expected_revision=REQUEST_REVISION,
        decision="cancel",
        arrival_seq=73,
    )

    assert response == {"ok": True, "queued": True, "request_id": REQUEST_ID}
    (queued,) = peek_inputs()
    assert queued.metadata["arrival_seq"] == 73
    assert queued.metadata["decision"] == "cancel"


@pytest.mark.parametrize(
    "failure",
    (
        "plugin_permission_request_not_found",
        "plugin_permission_expired",
        "plugin_permission_replayed",
        "plugin_permission_revision_conflict",
        "plugin_permission_invalidated",
    ),
)
def test_auto_handoff_revalidates_wrong_expired_replayed_or_drifted_decisions_before_dispatch(
    failure: str, monkeypatch
) -> None:
    """Catches trusting durable queue metadata as current execution authority."""
    module = _continuations()
    _configure(module)
    from mcp_platform.plugin_runtime import PluginRuntime
    from pressure_loop.cognition_queue import peek_inputs

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=1)
    contributor = peek_inputs()[0]

    def reject(_request_id: str, _expected_revision: str):
        raise ValueError(failure)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid continuation reached provider dispatch")

    monkeypatch.setattr(PluginRuntime, "invoke_tool", forbidden)
    with pytest.raises(ValueError, match=failure):
        module.plugin_permission_continuation_from_contributor(
            contributor,
            lineage_resolver=reject,
        )


def test_lineage_drift_is_rejected_before_auto_can_consume_permission() -> None:
    """Catches matching only request/revision while the plan lineage changed."""
    module = _continuations()
    _configure(module)
    from pressure_loop.cognition_queue import peek_inputs

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=1)
    contributor = peek_inputs()[0]

    with pytest.raises(ValueError, match="plugin_permission_lineage_conflict"):
        module.plugin_permission_continuation_from_contributor(
            contributor,
            lineage_resolver=lambda *_a: _lineage(module, plan_id="d" * 64),
        )


def test_clean_process_transport_lazily_uses_real_permission_store(tmp_path, monkeypatch) -> None:
    """No test callback or prior cognition import is required by the HUD transport."""
    module = _continuations()
    reset = getattr(module, "_reset_lineage_resolver_for_tests", None)
    assert callable(reset), "the continuation resolver has no clean-process composition seam"

    from mcp_platform.plugin_permissions import PluginPermissionStore
    import mcp_platform.plugin_permissions as permission_module
    import pressure_loop.operator_chat as operator_chat

    fake_store = MagicMock(spec=PluginPermissionStore)
    fake_store.resolve_continuation_lineage.return_value = SimpleNamespace(
        prior_episode_id=PRIOR_EPISODE_ID,
        task_id=TASK_ID,
        plan_id=PLAN_ID,
        plan_digest="e" * 64,
        revision=REQUEST_REVISION,
        status="pending",
    )
    monkeypatch.setattr(
        permission_module, "get_default_plugin_permission_store", lambda: fake_store
    )
    reset()

    response = operator_chat.handle_plugin_permission_decision(
        request_id=REQUEST_ID,
        expected_revision=REQUEST_REVISION,
        decision="allow",
        arrival_seq=1,
    )

    assert response["queued"] is True
    fake_store.resolve_continuation_lineage.assert_called_once_with(
        REQUEST_ID,
        REQUEST_REVISION,
    )
    reset()


def test_restart_sequence_continues_after_durable_maximum() -> None:
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=50)
    queue._reset_cache_for_tests()

    assert queue.next_arrival_seq() == 51


def test_slow_earlier_permission_click_replaces_later_lock_winner() -> None:
    module = _continuations()
    _configure(module)
    from pressure_loop.cognition_queue import peek_inputs

    later_arrived = threading.Event()
    release_earlier = threading.Event()

    def enqueue_earlier() -> bool:
        later_arrived.wait(timeout=2)
        release_earlier.wait(timeout=2)
        return module.enqueue_plugin_permission_decision(
            _decision(module, "allow"),
            arrival_seq=10,
        )

    def enqueue_later() -> bool:
        outcome = module.enqueue_plugin_permission_decision(
            _decision(module, "cancel"),
            arrival_seq=11,
        )
        later_arrived.set()
        release_earlier.set()
        return outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        earlier = pool.submit(enqueue_earlier)
        later = pool.submit(enqueue_later)
        assert later.result() is True
        assert earlier.result() is True

    (winner,) = peek_inputs()
    assert winner.metadata["arrival_seq"] == 10
    assert winner.metadata["decision"] == "allow"


def test_acknowledged_permission_request_remains_deduped_across_restart() -> None:
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=1)
    (head,) = queue.peek_inputs()
    assert queue.lease_next_input(pipeline_id=0) == head
    assert queue.acknowledge_input(head, pipeline_id=0) is True
    queue._reset_cache_for_tests()

    assert (
        module.enqueue_plugin_permission_decision(_decision(module, "cancel"), arrival_seq=2)
        is False
    )
    settle = getattr(queue, "settle_plugin_permission_decision", None)
    assert callable(settle), "Task 6 has no exact terminal dedupe release seam"
    assert settle(REQUEST_ID, expected_revision=REQUEST_REVISION) is True


def test_settled_permission_owners_are_bounded_without_pruning_active_owners(
    monkeypatch,
) -> None:
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    monkeypatch.setattr(queue, "_MAX_PLUGIN_PERMISSION_OWNERS", 2)
    for index in range(3):
        request_id = f"{index + 1:048x}"
        decision = module.PluginPermissionDecision(
            request_id=request_id,
            expected_revision=REQUEST_REVISION,
            decision="allow",
        )
        assert module.enqueue_plugin_permission_decision(decision, arrival_seq=index + 1)
        (head,) = queue.peek_inputs()
        assert queue.lease_next_input(pipeline_id=0) == head
        assert queue.acknowledge_input(head, pipeline_id=0)
        assert queue.settle_plugin_permission_decision(
            request_id,
            expected_revision=REQUEST_REVISION,
        )

    queue._reset_cache_for_tests()
    queue.peek_inputs()
    assert len(queue._PLUGIN_PERMISSION_OWNERS) == 2
    assert "1".zfill(48) not in queue._PLUGIN_PERMISSION_OWNERS


@pytest.mark.parametrize(
    "failure_type",
    (OSError, FatalQueueBoundary, __import__("asyncio").CancelledError),
)
@pytest.mark.parametrize("published", (False, True))
def test_persistence_failure_reconciles_exact_disk_and_preserves_primary_identity(
    failure_type, published: bool, monkeypatch
) -> None:
    import pressure_loop.cognition_queue as queue
    from pressure_loop.contributors import from_operator_message

    real_replace = queue.os.replace
    failure = failure_type("publish interrupted")

    def interrupted_replace(source, target):
        if published:
            real_replace(source, target)
        raise failure

    monkeypatch.setattr(queue.os, "replace", interrupted_replace)
    contributor = from_operator_message(
        {
            "message": "durable or absent",
            "ingress_class": "epistemic_request",
            "cycle_id": "persist-reconcile",
            "arrival_seq": 7,
        }
    )
    with pytest.raises(failure_type) as caught:
        queue.enqueue_input(contributor, operator_activity=False)

    assert caught.value is failure
    note = "\n".join(getattr(failure, "__notes__", ()))
    assert f"operator_queue_persistence={'committed' if published else 'not_committed'}" in note
    queue._reset_cache_for_tests()
    assert bool(queue.peek_inputs()) is published


def test_corrupt_queue_load_is_all_or_nothing(tmp_path) -> None:
    import pressure_loop.cognition_queue as queue

    path = queue._queue_path()
    path.write_text(
        json.dumps(
            [
                {
                    "source": "operator",
                    "type": "operator_query",
                    "weight": 1.0,
                    "metadata": {"message": "must not leak", "cycle_id": "valid-first"},
                },
                "corrupt-row",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(queue.CognitionQueueUnreadable):
        queue.peek_inputs()
    assert not queue._STATE.loaded and not queue._STATE.queue


def test_earlier_reserved_click_blocks_later_winner_until_exact_enqueue_and_ack() -> None:
    """The stamped click, not whichever thread reaches enqueue first, owns the request."""
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    earlier_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, earlier_seq)
    later_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, later_seq)
    assert earlier_seq < later_seq

    assert module.enqueue_plugin_permission_decision(
        _decision(module, "cancel"), arrival_seq=later_seq
    )
    (later,) = queue.peek_inputs()
    assert queue.lease_next_input(pipeline_id=0) is None

    assert module.enqueue_plugin_permission_decision(
        _decision(module, "allow"), arrival_seq=earlier_seq
    )
    (winner,) = queue.peek_inputs()
    assert winner.metadata["arrival_seq"] == earlier_seq
    assert winner.metadata["decision"] == "allow"
    assert queue.lease_next_input(pipeline_id=0) == winner
    assert queue.acknowledge_input(later, pipeline_id=0) is False
    assert queue.owns_input_lease(winner, pipeline_id=0) is True
    assert queue.acknowledge_input(winner, pipeline_id=0) is True


def test_concurrent_thread_hop_reversal_never_leases_later_permission_click() -> None:
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    earlier_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, earlier_seq)
    later_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, later_seq)
    later_enqueued = threading.Event()
    release_earlier = threading.Event()

    def earlier() -> bool:
        later_enqueued.wait(timeout=2)
        release_earlier.wait(timeout=2)
        return module.enqueue_plugin_permission_decision(
            _decision(module, "allow"), arrival_seq=earlier_seq
        )

    def later() -> bool:
        queued = module.enqueue_plugin_permission_decision(
            _decision(module, "cancel"), arrival_seq=later_seq
        )
        later_enqueued.set()
        assert queue.lease_next_input(pipeline_id=0) is None
        release_earlier.set()
        return queued

    with ThreadPoolExecutor(max_workers=2) as pool:
        early_result = pool.submit(earlier)
        late_result = pool.submit(later)
        assert late_result.result() is True
        assert early_result.result() is True

    (winner,) = queue.peek_inputs()
    assert winner.metadata["arrival_seq"] == earlier_seq


def test_pending_arrival_reservation_survives_restart_and_blocks_later_lease() -> None:
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    earlier_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, earlier_seq)
    later_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, later_seq)
    assert module.enqueue_plugin_permission_decision(
        _decision(module, "cancel"), arrival_seq=later_seq
    )

    queue._reset_cache_for_tests()
    assert queue.lease_next_input(pipeline_id=0) is None
    assert module.enqueue_plugin_permission_decision(
        _decision(module, "allow"), arrival_seq=earlier_seq
    )
    assert queue.lease_next_input(pipeline_id=0).metadata["arrival_seq"] == earlier_seq


def test_abandoned_reservation_expires_after_restart_without_stalling_queue(
    monkeypatch,
) -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    clock = [1_000.0]
    monkeypatch.setattr(queue, "_wall_time", lambda: clock[0])
    abandoned = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, abandoned)
    later = from_operator_message(
        {
            "message": "later durable chat",
            "ingress_class": "epistemic_request",
            "cycle_id": "later-after-abandon",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) is None

    clock[0] += queue._ARRIVAL_RESERVATION_TTL_SECONDS + 1.0
    queue._reset_cache_for_tests()
    assert queue.lease_next_input(pipeline_id=0) == later
    assert abandoned < later.metadata["arrival_seq"]


def test_failed_permission_resolution_releases_exact_arrival_reservation() -> None:
    module = _continuations()
    import pressure_loop.cognition_queue as queue

    module.configure_plugin_permission_lineage_resolver(
        lambda *_args: (_ for _ in ()).throw(ValueError("plugin_permission_expired"))
    )
    with pytest.raises(ValueError, match="plugin_permission_expired"):
        module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=1)

    assert queue._ARRIVAL_RESERVATIONS == {}
    assert queue.peek_inputs() == ()


def test_chat_and_external_arrivals_share_reservation_order() -> None:
    from pressure_loop.contributors import from_operator_message, make_contributor
    import pressure_loop.cognition_queue as queue

    chat_seq = queue.next_arrival_seq()
    external_seq = queue.next_arrival_seq()
    chat = from_operator_message(
        {
            "message": "first chat",
            "ingress_class": "epistemic_request",
            "cycle_id": "chat-adjacent",
            "arrival_seq": chat_seq,
        }
    )
    external = make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {"lane": "chat", "cycle_id": "external-adjacent", "arrival_seq": external_seq},
    )

    assert queue.enqueue_input(external, operator_activity=False)
    external = _queued_input(external)
    assert queue.enqueue_input(chat, operator_activity=False)
    chat = _queued_input(chat)
    assert queue.peek_inputs() == (chat, external)


@pytest.mark.parametrize(
    "failure_type",
    (TypeError, FatalQueueBoundary, __import__("asyncio").CancelledError),
)
def test_encoding_failure_reconciles_exact_disk_and_preserves_identity(
    failure_type, monkeypatch
) -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    failure = failure_type("encoding interrupted")
    monkeypatch.setattr(queue, "_encoded_rows", lambda: (_ for _ in ()).throw(failure))
    contributor = from_operator_message(
        {
            "message": "must not remain volatile",
            "ingress_class": "epistemic_request",
            "cycle_id": "encode-reconcile",
            "arrival_seq": 7,
        }
    )
    with pytest.raises(failure_type) as caught:
        queue.enqueue_input(contributor, operator_activity=False)

    assert caught.value is failure
    assert "operator_queue_persistence=not_committed" in "\n".join(
        getattr(failure, "__notes__", ())
    )
    assert queue.peek_inputs() == ()


def test_cyclic_metadata_encoding_failure_leaves_no_volatile_queue_item() -> None:
    from pressure_loop.contributors import Contributor
    import pressure_loop.cognition_queue as queue

    metadata: dict[str, object] = {
        "cycle_id": "cyclic-metadata",
        "arrival_seq": 9,
    }
    metadata["cycle"] = metadata
    contributor = Contributor(
        source="operator",
        type="operator_query",
        weight=1.0,
        metadata=metadata,
    )

    with pytest.raises(ValueError, match="Circular reference detected") as caught:
        queue.enqueue_input(contributor, operator_activity=False)
    assert "operator_queue_persistence=not_committed" in "\n".join(
        getattr(caught.value, "__notes__", ())
    )
    assert queue.peek_inputs() == ()


def test_nonserializable_metadata_encoding_failure_leaves_no_volatile_queue_item() -> None:
    from pressure_loop.contributors import Contributor
    import pressure_loop.cognition_queue as queue

    contributor = Contributor(
        source="operator",
        type="operator_query",
        weight=1.0,
        metadata={
            "cycle_id": "nonserializable-metadata",
            "arrival_seq": 10,
            "unsupported": {"set-value"},
        },
    )

    with pytest.raises(TypeError, match="JSON serializable") as caught:
        queue.enqueue_input(contributor, operator_activity=False)
    assert "operator_queue_persistence=not_committed" in "\n".join(
        getattr(caught.value, "__notes__", ())
    )
    assert queue.peek_inputs() == ()


@pytest.mark.parametrize(
    ("request_id", "expected_revision"),
    (("not-opaque", REQUEST_REVISION), (REQUEST_ID, "not-a-digest")),
)
@pytest.mark.parametrize("owner_state", ("inflight", "settled"))
def test_malformed_durable_owner_identity_rejects_whole_document_repeatedly(
    request_id, expected_revision, owner_state
) -> None:
    import pressure_loop.cognition_queue as queue

    path = queue._queue_path()
    durable_state = {
        "schema_version": queue._QUEUE_STATE_SCHEMA,
        "plugin_permission_owners": [
            {
                "request_id": request_id,
                "expected_revision": expected_revision,
                "arrival_seq": 1,
                "state": owner_state,
            }
        ],
    }
    if queue._QUEUE_STATE_SCHEMA >= 2:
        durable_state["arrival_reservations"] = []
    path.write_text(
        json.dumps(
            [
                {
                    "source": "operator",
                    "type": "operator_query",
                    "weight": 1.0,
                    "metadata": {
                        "message": "valid prefix must not leak",
                        "cycle_id": "valid-prefix",
                    },
                },
                {"_operator_queue_state": durable_state},
            ]
        ),
        encoding="utf-8",
    )

    for _ in range(2):
        with pytest.raises(queue.CognitionQueueUnreadable):
            queue.peek_inputs()
        assert not queue._STATE.loaded and not queue._STATE.queue
        assert queue._PLUGIN_PERMISSION_OWNERS == {}
        queue._reset_cache_for_tests()


@pytest.mark.parametrize("rejection", ("source", "withhold", "quarantine"))
def test_rejected_chat_stamp_never_blocks_the_next_queue_head(rejection, monkeypatch) -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.operator_chat as operator_chat
    import pressure_loop.cognition_queue as queue

    if rejection == "source":
        monkeypatch.setattr(
            "memory.source_ledger.store.record_operator_chat_source",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ledger unavailable")),
        )
    else:
        monkeypatch.setattr(
            "memory.source_ledger.store.record_operator_chat_source",
            lambda *_args, **_kwargs: {"source_id": "screened"},
        )
        monkeypatch.setattr(
            "guardian.external_ingress.screen_text",
            lambda text, **_kwargs: (text, rejection),
        )

    response = operator_chat.handle_operator_chat("screen this chat")
    assert response["queued"] is False
    later_seq = queue.next_arrival_seq()
    later = from_operator_message(
        {
            "message": "later valid chat",
            "ingress_class": "epistemic_request",
            "cycle_id": "later-after-rejection",
            "arrival_seq": later_seq,
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) == later


def test_hud_like_sequence_allocation_without_enqueue_never_blocks_queue() -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    unused = queue.next_arrival_seq()
    later = from_operator_message(
        {
            "message": "later after abandoned HUD allocation",
            "ingress_class": "epistemic_request",
            "cycle_id": "later-hud-like",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert unused < later.metadata["arrival_seq"]
    assert queue.lease_next_input(pipeline_id=0) == later


def test_schema_two_generic_reservation_migrates_only_high_watermark() -> None:
    import pressure_loop.cognition_queue as queue

    queue._legacy_queue_path().write_text(
        json.dumps(
            [
                {
                    "_operator_queue_state": {
                        "schema_version": 2,
                        "plugin_permission_owners": [],
                        "arrival_reservations": [
                            {
                                "arrival_seq": 50,
                                "request_id": None,
                                "expected_revision": None,
                                "expires_at": 9_999_999_999.0,
                            }
                        ],
                    }
                }
            ]
        ),
        encoding="utf-8",
    )

    assert queue.next_arrival_seq() == 51
    assert queue._ARRIVAL_RESERVATIONS == {}


def test_unstamped_external_is_atomically_ordered_after_pending_plugin_reservation() -> None:
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import make_contributor
    import pressure_loop.cognition_queue as queue

    plugin_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, plugin_seq)
    external = make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {"lane": "chat", "cycle_id": "unstamped-external"},
    )
    assert queue.enqueue_input(external, operator_activity=False)
    external = _queued_input(external)
    (stamped_external,) = queue.peek_inputs()
    assert stamped_external.metadata["arrival_seq"] > plugin_seq
    assert queue.lease_next_input(pipeline_id=0) is None

    assert module.enqueue_plugin_permission_decision(_decision(module), arrival_seq=plugin_seq)
    assert [item.type for item in queue.peek_inputs()] == [
        "plugin_permission_decision",
        "sensory_observation",
    ]


def test_live_process_does_not_expire_slow_legitimate_plugin_reservation(monkeypatch) -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    clock = [1_000.0]
    monkeypatch.setattr(queue, "_wall_time", lambda: clock[0])
    plugin_seq = queue.next_arrival_seq()
    queue.reserve_plugin_permission_arrival(REQUEST_ID, REQUEST_REVISION, plugin_seq)
    clock[0] += queue._ARRIVAL_RESERVATION_TTL_SECONDS * 10
    later = from_operator_message(
        {
            "message": "later while plugin intake is still alive",
            "ingress_class": "epistemic_request",
            "cycle_id": "later-slow-plugin",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) is None


@pytest.mark.parametrize("published", (False, True))
def test_ack_persistence_reconciles_exact_lease_with_durable_outcome(
    published, monkeypatch
) -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    first = from_operator_message(
        {
            "message": "first",
            "ingress_class": "epistemic_request",
            "cycle_id": "ack-first",
            "arrival_seq": 1,
        }
    )
    second = from_operator_message(
        {
            "message": "second",
            "ingress_class": "epistemic_request",
            "cycle_id": "ack-second",
            "arrival_seq": 2,
        }
    )
    assert queue.enqueue_input(first, operator_activity=False)
    first = _queued_input(first)
    assert queue.enqueue_input(second, operator_activity=False)
    second = _queued_input(second)
    assert queue.lease_next_input(pipeline_id=0) == first
    real_replace = queue.os.replace
    failure = OSError("ack publication interrupted")

    def interrupted_replace(source, target):
        if published:
            real_replace(source, target)
        raise failure

    monkeypatch.setattr(queue.os, "replace", interrupted_replace)
    with pytest.raises(OSError) as caught:
        queue.acknowledge_input(first, pipeline_id=0)
    assert caught.value is failure
    note = "\n".join(failure.__notes__)
    assert f"operator_queue_persistence={'committed' if published else 'not_committed'}" in note
    if published:
        assert queue.owns_input_lease(first, pipeline_id=0) is False
        assert queue.lease_next_input(pipeline_id=0) == second
    else:
        assert queue.owns_input_lease(first, pipeline_id=0) is True
        assert queue.lease_next_input(pipeline_id=0) is None


def test_transient_first_reconcile_read_still_reports_committed(monkeypatch) -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    contributor = from_operator_message(
        {
            "message": "published despite raised replace",
            "ingress_class": "epistemic_request",
            "cycle_id": "transient-read",
            "arrival_seq": 1,
        }
    )
    real_replace = queue.os.replace
    real_read_text = queue.Path.read_text
    failure = OSError("publisher raised after replace")
    reads = 0

    def publish_then_raise(source, target):
        real_replace(source, target)
        raise failure

    def transient_read(path, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("transient reread")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(queue.os, "replace", publish_then_raise)
    queue.peek_inputs()
    monkeypatch.setattr(queue.Path, "read_text", transient_read)
    with pytest.raises(OSError) as caught:
        queue.enqueue_input(contributor, operator_activity=False)

    assert caught.value is failure
    assert "operator_queue_persistence=committed" in "\n".join(failure.__notes__)
    contributor = _queued_input(contributor)
    assert queue.peek_inputs() == (contributor,)


def test_unreadable_reconcile_state_is_explicitly_unknown(monkeypatch) -> None:
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    baseline = from_operator_message(
        {
            "message": "baseline",
            "ingress_class": "epistemic_request",
            "cycle_id": "unknown-baseline",
            "arrival_seq": 1,
        }
    )
    attempted = from_operator_message(
        {
            "message": "unknown durability",
            "ingress_class": "epistemic_request",
            "cycle_id": "unknown-attempt",
            "arrival_seq": 2,
        }
    )
    assert queue.enqueue_input(baseline, operator_activity=False)
    baseline = _queued_input(baseline)
    failure = OSError("publication unavailable")
    monkeypatch.setattr(queue.os, "replace", lambda *_args: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(
        queue.Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("reread unavailable")),
    )

    with pytest.raises(OSError) as caught:
        queue.enqueue_input(attempted, operator_activity=False)
    assert caught.value is failure
    note = "\n".join(failure.__notes__)
    assert "operator_queue_persistence=unknown" in note
    assert "operator_queue_persistence=not_committed" not in note


@pytest.mark.parametrize("schema_version", (True, 1.0, 2.0, "2"))
def test_queue_state_schema_rejects_non_integer_discriminator(schema_version) -> None:
    import pressure_loop.cognition_queue as queue

    state = {
        "schema_version": schema_version,
        "plugin_permission_owners": [],
    }
    if schema_version == queue._QUEUE_STATE_SCHEMA:
        state["arrival_reservations"] = []
    queue._queue_path().write_text(
        json.dumps([{"_operator_queue_state": state}]),
        encoding="utf-8",
    )

    with pytest.raises(queue.CognitionQueueUnreadable):
        queue.peek_inputs()


@pytest.mark.parametrize("entrypoint", ("transport", "coordinator"))
@pytest.mark.parametrize(
    "failure_type",
    (RuntimeError, __import__("asyncio").CancelledError, FatalQueueBoundary),
)
@pytest.mark.parametrize("published", (False, True))
def test_failed_reservation_acquisition_never_leaves_a_live_process_blocker(
    entrypoint, failure_type, published, monkeypatch
) -> None:
    """Catches reservation acquisition outside the caller's cleanup ownership."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    real_replace = queue.os.replace
    failure = failure_type("reservation publication interrupted")

    def interrupted_replace(source, target):
        if published:
            real_replace(source, target)
        raise failure

    monkeypatch.setattr(queue.os, "replace", interrupted_replace)
    with pytest.raises(failure_type) as caught:
        _invoke_permission_intake(module, entrypoint, arrival_seq=41)
    assert caught.value is failure

    monkeypatch.setattr(queue.os, "replace", real_replace)
    assert queue.peek_inputs() == ()
    assert queue._ARRIVAL_RESERVATIONS == {}
    later = from_operator_message(
        {
            "message": "later after failed reservation",
            "ingress_class": "epistemic_request",
            "cycle_id": f"later-{entrypoint}-{published}",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) == later


@pytest.mark.parametrize("entrypoint", ("transport", "coordinator"))
@pytest.mark.parametrize("cleanup_published", (False, True))
def test_failed_reservation_cleanup_retries_without_replacing_primary_error(
    entrypoint, cleanup_published, monkeypatch
) -> None:
    """Catches a one-shot cleanup that strands a post-publication reservation."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    real_replace = queue.os.replace
    primary = RuntimeError("reservation committed before failure")
    cleanup = OSError("first cleanup publication interrupted")
    publications = 0

    def interrupted_replace(source, target):
        nonlocal publications
        publications += 1
        if publications == 1:
            real_replace(source, target)
            raise primary
        if publications == 2:
            if cleanup_published:
                real_replace(source, target)
            raise cleanup
        return real_replace(source, target)

    monkeypatch.setattr(queue.os, "replace", interrupted_replace)
    with pytest.raises(RuntimeError) as caught:
        _invoke_permission_intake(module, entrypoint, arrival_seq=51)
    assert caught.value is primary

    monkeypatch.setattr(queue.os, "replace", real_replace)
    assert queue.peek_inputs() == ()
    assert queue._ARRIVAL_RESERVATIONS == {}
    later = from_operator_message(
        {
            "message": "later after cleanup retry",
            "ingress_class": "epistemic_request",
            "cycle_id": f"later-cleanup-{entrypoint}-{cleanup_published}",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) == later


@pytest.mark.parametrize("entrypoint", ("transport", "coordinator"))
def test_unknown_reservation_publication_is_reloaded_and_abandoned(entrypoint, monkeypatch) -> None:
    """Catches unknown publication state being mistaken for an absent reservation."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    real_replace = queue.os.replace
    real_read_text = queue.Path.read_text
    primary = FatalQueueBoundary("reservation publication outcome unknown")
    reads = 0
    publications = 0

    def publish_then_raise(source, target):
        nonlocal publications
        publications += 1
        real_replace(source, target)
        if publications == 1:
            raise primary

    def temporarily_unreadable(path, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads <= 2:
            raise OSError("reservation reconciliation temporarily unreadable")
        return real_read_text(path, *args, **kwargs)

    queue.peek_inputs()
    monkeypatch.setattr(queue.os, "replace", publish_then_raise)
    monkeypatch.setattr(queue.Path, "read_text", temporarily_unreadable)
    with pytest.raises(FatalQueueBoundary) as caught:
        _invoke_permission_intake(module, entrypoint, arrival_seq=61)
    assert caught.value is primary

    assert queue.peek_inputs() == ()
    assert queue._ARRIVAL_RESERVATIONS == {}
    later = from_operator_message(
        {
            "message": "later after unknown reservation",
            "ingress_class": "epistemic_request",
            "cycle_id": f"later-unknown-{entrypoint}",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) == later


@pytest.mark.parametrize("published", (False, True))
def test_unknown_ack_reloads_exact_durable_lease_outcome(published, monkeypatch) -> None:
    """Catches reload retaining a lease for a durably removed queue head."""
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    first = from_operator_message(
        {
            "message": "unknown ack first",
            "ingress_class": "epistemic_request",
            "cycle_id": "unknown-ack-first",
            "arrival_seq": 1,
        }
    )
    second = from_operator_message(
        {
            "message": "unknown ack second",
            "ingress_class": "epistemic_request",
            "cycle_id": "unknown-ack-second",
            "arrival_seq": 2,
        }
    )
    assert queue.enqueue_input(first, operator_activity=False)
    first = _queued_input(first)
    assert queue.enqueue_input(second, operator_activity=False)
    second = _queued_input(second)
    assert queue.lease_next_input(pipeline_id=0) == first
    real_replace = queue.os.replace
    real_read_text = queue.Path.read_text
    primary = __import__("asyncio").CancelledError("ack outcome unknown")
    reads = 0

    def interrupted_replace(source, target):
        if published:
            real_replace(source, target)
        raise primary

    def temporarily_unreadable(path, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads <= 2:
            raise OSError("ack reconciliation temporarily unreadable")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(queue.os, "replace", interrupted_replace)
    monkeypatch.setattr(queue.Path, "read_text", temporarily_unreadable)
    with pytest.raises(__import__("asyncio").CancelledError) as caught:
        queue.acknowledge_input(first, pipeline_id=0)
    assert caught.value is primary
    assert "operator_queue_persistence=unknown" in "\n".join(primary.__notes__)

    monkeypatch.setattr(queue.os, "replace", real_replace)
    durable = queue.peek_inputs()
    if published:
        assert durable == (second,)
        assert queue.owns_input_lease(first, pipeline_id=0) is False
        assert queue.lease_next_input(pipeline_id=0) == second
    else:
        assert durable == (first, second)
        assert queue.owns_input_lease(first, pipeline_id=0) is True
        assert queue.lease_next_input(pipeline_id=0) is None
        assert queue.acknowledge_input(first, pipeline_id=0)
        assert queue.lease_next_input(pipeline_id=0) == second


def test_unknown_ack_reload_clears_only_the_exact_replaced_lease_identity(monkeypatch) -> None:
    """Catches request-level lease recovery that mistakes another decision for the leased one."""
    module = _continuations()
    _configure(module)
    import pressure_loop.cognition_queue as queue

    assert module.enqueue_plugin_permission_decision(_decision(module, "allow"), arrival_seq=1)
    (leased,) = queue.peek_inputs()
    assert queue.lease_next_input(pipeline_id=0) == leased
    real_read_text = queue.Path.read_text
    primary = FatalQueueBoundary("permission ack outcome unknown")
    reads = 0

    monkeypatch.setattr(queue.os, "replace", lambda *_args: (_ for _ in ()).throw(primary))

    def temporarily_unreadable(path, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads <= 2:
            raise OSError("permission ack reconciliation temporarily unreadable")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(queue.Path, "read_text", temporarily_unreadable)
    with pytest.raises(FatalQueueBoundary) as caught:
        queue.acknowledge_input(leased, pipeline_id=0)
    assert caught.value is primary

    payload = _durable_queue_payload(queue)
    payload[0]["metadata"]["decision"] = "cancel"
    _install_durable_queue_payload(queue, payload)
    (replacement,) = queue.peek_inputs()
    assert replacement.metadata["decision"] == "cancel"
    assert queue.owns_input_lease(leased, pipeline_id=0) is False
    assert queue.lease_next_input(pipeline_id=0) == replacement


def test_repeated_unknown_ack_cancellation_preserves_retryable_exact_lease(monkeypatch) -> None:
    """Catches cancellation recovery that loses a provably durable first head."""
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    first = from_operator_message(
        {
            "message": "retry cancellation first",
            "ingress_class": "epistemic_request",
            "cycle_id": "retry-cancel-first",
            "arrival_seq": 1,
        }
    )
    second = from_operator_message(
        {
            "message": "retry cancellation second",
            "ingress_class": "epistemic_request",
            "cycle_id": "retry-cancel-second",
            "arrival_seq": 2,
        }
    )
    assert queue.enqueue_input(first, operator_activity=False)
    first = _queued_input(first)
    assert queue.enqueue_input(second, operator_activity=False)
    second = _queued_input(second)
    assert queue.lease_next_input(pipeline_id=0) == first
    real_replace = queue.os.replace
    real_read_text = queue.Path.read_text

    for attempt in range(2):
        primary = __import__("asyncio").CancelledError(f"ack cancellation {attempt}")
        reads = 0

        def cancel_before_publication(*_args):
            raise primary

        def temporarily_unreadable(path, *args, **kwargs):
            nonlocal reads
            reads += 1
            if reads <= 2:
                raise OSError("ack reconciliation temporarily unreadable")
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(queue.os, "replace", cancel_before_publication)
        monkeypatch.setattr(queue.Path, "read_text", temporarily_unreadable)
        with pytest.raises(__import__("asyncio").CancelledError) as caught:
            queue.acknowledge_input(first, pipeline_id=0)
        assert caught.value is primary
        assert queue.peek_inputs() == (first, second)
        assert queue.owns_input_lease(first, pipeline_id=0) is True

    monkeypatch.setattr(queue.os, "replace", real_replace)
    assert queue.acknowledge_input(first, pipeline_id=0)
    assert queue.lease_next_input(pipeline_id=0) == second


@pytest.mark.parametrize("entrypoint", ("transport", "coordinator"))
@pytest.mark.parametrize(
    "failure_type",
    (RuntimeError, __import__("asyncio").CancelledError, FatalQueueBoundary),
)
def test_exhausted_cleanup_writes_retire_only_the_failed_local_reservation(
    entrypoint, failure_type, monkeypatch
) -> None:
    """Catches final cleanup exhaustion leaving a current-process FCFS blocker."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    primary, real_replace = _fail_reservation_and_every_cleanup_write(
        module,
        entrypoint,
        failure_type,
        monkeypatch,
        arrival_seq=71,
    )
    assert "plugin_permission_arrival_cleanup_failed=" in "\n".join(primary.__notes__)

    monkeypatch.setattr(queue.os, "replace", real_replace)
    later = from_operator_message(
        {
            "message": "later after cleanup exhaustion",
            "ingress_class": "epistemic_request",
            "cycle_id": f"later-exhausted-{entrypoint}",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) == later


def test_local_abandonment_survives_same_process_reload_and_next_write_prunes_disk(
    monkeypatch,
) -> None:
    """Catches a volatile-only skip that is lost on authoritative same-process reload."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    _primary, real_replace = _fail_reservation_and_every_cleanup_write(
        module,
        "coordinator",
        FatalQueueBoundary,
        monkeypatch,
        arrival_seq=81,
    )
    durable_before = _durable_queue_payload(queue)
    assert (
        durable_before[-1]["_operator_queue_state"]["arrival_reservations"][0]["arrival_seq"] == 81
    )

    _unload_lines_keep_abandonments(queue)
    assert queue.peek_inputs() == ()
    assert queue._ARRIVAL_RESERVATIONS == {}

    monkeypatch.setattr(queue.os, "replace", real_replace)
    later = from_operator_message(
        {
            "message": "canonical prune mutation",
            "ingress_class": "epistemic_request",
            "cycle_id": "canonical-prune-mutation",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue._LOCAL_ABANDONED_RESERVATIONS == {}
    durable_after = _durable_queue_payload(queue)
    assert durable_after[-1]["_operator_queue_state"]["arrival_reservations"] == []

    queue._reset_cache_for_tests()
    assert queue.lease_next_input(pipeline_id=0) == later


def test_local_abandonment_does_not_unblock_another_live_reservation(monkeypatch) -> None:
    """Catches request-agnostic suppression that weakens lower-sequence FCFS."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    other_request = "d" * 48
    other_revision = "f" * 64
    queue.reserve_plugin_permission_arrival(other_request, other_revision, 72)
    _primary, real_replace = _fail_reservation_and_every_cleanup_write(
        module,
        "transport",
        RuntimeError,
        monkeypatch,
        arrival_seq=71,
    )

    monkeypatch.setattr(queue.os, "replace", real_replace)
    later = from_operator_message(
        {
            "message": "must remain behind other live owner",
            "ingress_class": "epistemic_request",
            "cycle_id": "other-owner-still-live",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) is None
    assert queue.abandon_arrival_reservation(
        72,
        request_id=other_request,
        expected_revision=other_revision,
    )
    assert queue.lease_next_input(pipeline_id=0) == later


@pytest.mark.parametrize("changed_field", ("request_id", "process_epoch"))
def test_local_abandonment_matches_exact_identity_and_process_epoch(
    changed_field, monkeypatch
) -> None:
    """Catches a local marker suppressing a different reservation owner."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    _primary, real_replace = _fail_reservation_and_every_cleanup_write(
        module,
        "coordinator",
        FatalQueueBoundary,
        monkeypatch,
        arrival_seq=91,
    )
    payload = _durable_queue_payload(queue)
    reservation = payload[-1]["_operator_queue_state"]["arrival_reservations"][0]
    reservation[changed_field] = "c" * (48 if changed_field == "request_id" else 32)
    _write_durable_queue_payload(queue, payload)
    _unload_lines_keep_abandonments(queue)
    monkeypatch.setattr(queue.os, "replace", real_replace)
    later = from_operator_message(
        {
            "message": "must remain behind changed owner",
            "ingress_class": "epistemic_request",
            "cycle_id": f"changed-owner-{changed_field}",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) is None


def test_process_restart_restores_fail_closed_expiry_for_local_abandonment(monkeypatch) -> None:
    """Catches a local marker being persisted or inherited across process epochs."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    clock = [1_000.0]
    monkeypatch.setattr(queue, "_wall_time", lambda: clock[0])
    _primary, real_replace = _fail_reservation_and_every_cleanup_write(
        module,
        "coordinator",
        __import__("asyncio").CancelledError,
        monkeypatch,
        arrival_seq=101,
    )
    queue._reset_cache_for_tests()
    monkeypatch.setattr(queue.os, "replace", real_replace)
    later = from_operator_message(
        {
            "message": "restart remains fail closed",
            "ingress_class": "epistemic_request",
            "cycle_id": "restart-fail-closed",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) is None

    clock[0] += queue._ARRIVAL_RESERVATION_TTL_SECONDS + 1.0
    queue._reset_cache_for_tests()
    assert queue.lease_next_input(pipeline_id=0) == later


def test_local_abandonment_capacity_is_reclaimed_by_successful_canonical_write(
    monkeypatch,
) -> None:
    """Catches an unbounded or unreclaimable local abandonment registry."""
    module = _continuations()
    _configure(module)
    from pressure_loop.contributors import from_operator_message
    import pressure_loop.cognition_queue as queue

    monkeypatch.setattr(queue, "_MAX_LOCAL_ABANDONED_RESERVATIONS", 1, raising=False)
    for index in range(2):
        primary, real_replace = _fail_reservation_and_every_cleanup_write(
            module,
            "coordinator",
            FatalQueueBoundary,
            monkeypatch,
            arrival_seq=111 + index,
        )
        assert primary.args
        monkeypatch.setattr(queue.os, "replace", real_replace)
        assert queue.next_arrival_seq() > 111 + index

    later = from_operator_message(
        {
            "message": "bounded markers reclaimed",
            "ingress_class": "epistemic_request",
            "cycle_id": "bounded-marker-reclaim",
            "arrival_seq": queue.next_arrival_seq(),
        }
    )
    assert queue.enqueue_input(later, operator_activity=False)
    later = _queued_input(later)
    assert queue.lease_next_input(pipeline_id=0) == later


def test_local_abandonment_cannot_delete_a_prior_process_reservation(monkeypatch) -> None:
    """Catches delayed cleanup deleting a reservation owned by an older epoch."""
    import pressure_loop.cognition_queue as queue

    request_id = "f" * 48
    expected_revision = "1" * 64
    arrival_seq = 131
    queue.reserve_plugin_permission_arrival(request_id, expected_revision, arrival_seq)
    prior_epoch = queue._PROCESS_EPOCH
    monkeypatch.setattr(queue, "_PROCESS_EPOCH", "2" * 32)

    assert (
        queue.abandon_arrival_reservation(
            arrival_seq,
            request_id=request_id,
            expected_revision=expected_revision,
        )
        is False
    )
    assert queue._ARRIVAL_RESERVATIONS[arrival_seq]["process_epoch"] == prior_epoch
    assert arrival_seq in {
        item["arrival_seq"] for item in queue._read_disk_snapshot_locked()["permission_state"]["_operator_queue_state"]["arrival_reservations"]
    }
