import json

import pytest

from pressure_loop.contributors import from_operator_message, make_contributor


@pytest.fixture
def queue(tmp_path, monkeypatch):
    import pressure_loop.cognition_queue as module

    monkeypatch.setattr(module, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    module._reset_cache_for_tests()
    yield module
    module._reset_cache_for_tests()


def turn(seq):
    return from_operator_message(
        {"message": f"turn {seq}", "cycle_id": f"cycle-{seq}", "arrival_seq": seq, "weight": 1.0}
    )


def lane_work(seq, lane):
    return make_contributor("omega", "lane_work_dispatch", 1.0, {
        "lane": lane,
        "cycle_id": f"cycle-{seq}",
        "arrival_seq": seq,
    })


def test_legacy_restore_registers_once_and_preserves_interpretation(queue):
    c = queue.deserialize_input(queue.normalize_legacy_input(queue.serialize_input(turn(7))))
    queue.enqueue_input(turn(10), operator_activity=False)
    assert queue.enqueue_legacy_restore(queue.serialize_input(c))
    assert not queue.earlier_operator_turns_interpreted(10)
    assert queue.mark_operator_turn_interpreted(7, "cycle-7", outcome="answered")
    assert queue.acknowledge_input(queue.lease_next_input(0), 0)
    queue._reset_cache_for_tests()
    assert queue.enqueue_legacy_restore(queue.serialize_input(c))
    assert queue.earlier_operator_turns_interpreted(10)
    assert not queue.enqueue_legacy_restore(queue.serialize_input(c))


@pytest.mark.parametrize("after_replace", [False, True])
def test_legacy_registration_and_queue_publish_atomically(queue, monkeypatch, after_replace):
    queue.enqueue_input(turn(10), operator_activity=False)
    c = queue.deserialize_input(queue.normalize_legacy_input(queue.serialize_input(turn(7))))
    write = queue._write_encoded
    def fail(path, encoded):
        if after_replace:
            write(path, encoded)
        raise OSError("migration publication failed")
    monkeypatch.setattr(queue, "_write_encoded", fail)
    with pytest.raises(OSError, match="migration publication failed"):
        queue.enqueue_legacy_restore(queue.serialize_input(c))
    queue._reset_cache_for_tests()
    assert queue.earlier_operator_turns_interpreted(10) is (not after_replace)
    assert queue.queue_depth() == (2 if after_replace else 1)
    monkeypatch.setattr(queue, "_write_encoded", write)
    queue.enqueue_legacy_restore(queue.serialize_input(c))
    assert not queue.earlier_operator_turns_interpreted(10)


@pytest.mark.parametrize("invalid", [None, [], {"": {}}, {"id": {"arrival_seq": True, "cycle_id": "cycle"}}, {"id": {"arrival_seq": 1, "cycle_id": ""}}])
def test_migration_registration_receipts_validate_on_load(queue, invalid):
    queue.enqueue_input(turn(10), operator_activity=False)
    payload = json.loads(queue._queue_path().read_text(encoding="utf-8"))
    payload["legacy_registered_turns"] = invalid
    queue._queue_path().write_text(json.dumps(payload), encoding="utf-8")
    queue._reset_cache_for_tests()
    with pytest.raises(queue.CognitionQueueUnreadable):
        queue.peek_inputs()


def test_ordinary_restore_does_not_grow_migration_history_or_reopen_turn(queue):
    queue.enqueue_input(turn(7), operator_activity=False)
    c = queue.lease_next_input(0)
    queue.acknowledge_input(c, 0)
    queue.mark_operator_turn_interpreted(7, "cycle-7", outcome="answered")
    payload = json.loads(queue._queue_path().read_text(encoding="utf-8"))
    assert payload.pop("legacy_registered_turns") == {}
    # Previously written queue snapshots have no migration receipt field.
    queue._queue_path().write_text(json.dumps(payload), encoding="utf-8")
    queue._reset_cache_for_tests()
    assert queue.enqueue_restore(c)
    assert queue.earlier_operator_turns_interpreted(10)
    assert json.loads(queue._queue_path().read_text(encoding="utf-8"))["legacy_registered_turns"] == {}


def test_pending_row_migration_receipt_preserves_interpreted_envelope_replay(queue):
    row = queue.serialize_input(turn(7))
    row["metadata"]["line_lane"] = "chat"
    path = queue._legacy_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([row]), encoding="utf-8")
    c = queue.lease_next_input(0)
    assert queue.mark_operator_turn_interpreted(7, "cycle-7", outcome="answered")
    queue.acknowledge_input(c, 0)
    queue._reset_cache_for_tests()
    assert queue.enqueue_legacy_restore(queue.serialize_input(c))
    assert queue.earlier_operator_turns_interpreted(10)


def test_legacy_serialized_row_missing_arrival_uses_atomic_queue_admission(queue):
    row = {"source": "operator", "type": "operator_query", "weight": 1.0,
           "metadata": {"line_lane": "chat", "cycle_id": "old-missing"}}
    assert queue.enqueue_legacy_restore(row)
    c = queue.peek_inputs()[0]
    assert c.metadata["arrival_seq"] == 1
    assert not queue.earlier_operator_turns_interpreted(2)
    assert "arrival_seq" not in row["metadata"]


def test_four_lanes_lease_their_own_work_and_ack_exact_items(queue):
    contributors = (
        turn(4), turn(2), lane_work(5, "browser"),
        lane_work(1, "agent"), lane_work(3, "window"),
    )
    for contributor in contributors:
        assert queue.enqueue_input(contributor, operator_activity=False)
    leases = [queue.lease_next_input(pid) for pid in range(4)]
    assert [c.metadata["arrival_seq"] for c in leases] == [2, 5, 1, 3]
    assert queue.lease_next_input(0) is None
    assert not queue.acknowledge_input(leases[0], 1)
    assert queue.acknowledge_input(leases[2], 2)
    assert queue.acknowledge_input(leases[1], 1)
    assert queue.acknowledge_input(leases[3], 3)
    assert queue.acknowledge_input(leases[0], 0)
    assert queue.lease_next_input(0).metadata["arrival_seq"] == 4
    assert [c.metadata["arrival_seq"] for c in queue.peek_inputs()] == [4]


@pytest.mark.parametrize("pid", [-1, 4, True, "0", None])
def test_invalid_pipeline(queue, pid):
    with pytest.raises(ValueError):
        queue.lease_next_input(pid)


def test_restart_release_restore_and_global_sequence(queue):
    queue.enqueue_input(turn(12), operator_activity=False)
    leased = queue.lease_next_input(0)
    assert not queue.enqueue_restore(leased)
    queue._reset_cache_for_tests()
    restored = queue.lease_next_input(0)
    assert restored.metadata["queue_id"] == leased.metadata["queue_id"]
    assert queue.release_input_lease(0)
    assert queue.queue_depth() == 1
    assert queue.lease_next_input(0).metadata["queue_id"] == leased.metadata["queue_id"]
    item = lane_work(13, "browser")
    queue.enqueue_input(item, operator_activity=False)
    assert queue.peek_inputs()[-1].metadata["arrival_seq"] == 13
    assert queue.next_arrival_seq() == 14


def test_interpretation_order_survives_ack_and_restart(queue):
    for seq in (12, 13):
        queue.enqueue_input(turn(seq), operator_activity=False)
        assert queue.acknowledge_input(queue.lease_next_input(0), 0)
    assert queue.mark_operator_turn_interpreted(13, "cycle-13", outcome="answered")
    queue._reset_cache_for_tests()
    assert not queue.earlier_operator_turns_interpreted(13)
    assert not queue.mark_operator_turn_interpreted(12, "wrong", outcome="answered")
    assert queue.mark_operator_turn_interpreted(12, "cycle-12", outcome="answered")
    assert queue.earlier_operator_turns_interpreted(13)


def test_migration_is_sorted_and_repeatable(queue):
    paths = [
        queue._legacy_queue_path(),
        queue._legacy_lines_dir() / "chat.json",
        queue._legacy_lines_dir() / "agent.json",
        queue._legacy_lines_dir() / "meta.json",
    ]
    for seq, path in zip((4, 1, 3, 2), paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        c = turn(seq)
        path.write_text(
            json.dumps(
                [{"source": c.source, "type": c.type, "weight": c.weight, "metadata": c.metadata}]
            )
        )
    assert [c.metadata["arrival_seq"] for c in queue.peek_inputs()] == [1, 2, 3, 4]
    ids = [c.metadata["queue_id"] for c in queue.peek_inputs()]
    assert all(path.with_name(path.name + ".migrated").exists() for path in paths)
    queue._reset_cache_for_tests()
    assert [c.metadata["queue_id"] for c in queue.peek_inputs()] == ids


@pytest.mark.parametrize("after_replace", [False, True])
def test_atomic_write_failure_reconciles_committed_disk(queue, monkeypatch, after_replace):
    queue.enqueue_input(turn(1), operator_activity=False)
    original = queue.os.replace

    def fail(source, target):
        if after_replace:
            original(source, target)
        raise OSError("injected persistence boundary")

    monkeypatch.setattr(queue.os, "replace", fail)
    with pytest.raises(OSError):
        queue.enqueue_input(turn(2), operator_activity=False)
    assert queue.queue_depth() == (2 if after_replace else 1)


def test_corruption_never_becomes_empty(queue):
    queue._queue_path().write_text("broken")
    with pytest.raises(queue.CognitionQueueUnreadable):
        queue.peek_inputs()


def permission(seq=1):
    from pressure_loop.contributors import make_plugin_permission_decision_contributor

    return make_plugin_permission_decision_contributor(
        {
            "request_id": "a" * 48,
            "expected_revision": "b" * 64,
            "decision": "allow",
            "cycle_id": "plugin-permission:" + "a" * 48,
            "prior_episode_id": "prior",
            "task_id": "task",
            "plan_id": "c" * 64,
            "plan_digest": "d" * 64,
            "arrival_seq": seq,
            "directive_intent": "PLUGIN_OPERATION",
            "ingress_class": "authority_request",
        }
    )


def test_permission_roundtrip_reservation_barrier_and_owner(queue):
    from pressure_loop.plugin_continuations import (
        queued_plugin_permission_continuation_from_contributor,
    )

    queue.reserve_plugin_permission_arrival("a" * 48, "b" * 64, 1)
    queue.enqueue_input(turn(2), operator_activity=False)
    assert queue.lease_next_input(0) is None
    assert queue.enqueue_input(permission(), operator_activity=False)
    original = queue.lease_next_input(0)
    assert queued_plugin_permission_continuation_from_contributor(original).arrival_seq == 1
    assert "queue_id" not in original.metadata
    queue._reset_cache_for_tests()
    restored = queue.lease_next_input(0)
    assert (
        queue.serialize_input(restored)["queue_id"] == queue.serialize_input(original)["queue_id"]
    )
    assert queue.acknowledge_input(restored, 0)
    assert queue.settle_plugin_permission_decision("a" * 48, expected_revision="b" * 64)
    queue._reset_cache_for_tests()
    queue.reserve_plugin_permission_arrival("a" * 48, "b" * 64, 3)
    assert not queue.enqueue_input(permission(3), operator_activity=False)
    assert queue.lease_next_input(0).metadata["arrival_seq"] == 2


def test_permission_unreserved_and_conflicting_click_are_rejected(queue):
    with pytest.raises(ValueError, match="unreserved"):
        queue.enqueue_input(permission(), operator_activity=False)
    queue.reserve_plugin_permission_arrival("e" * 48, "b" * 64, 1)
    with pytest.raises(ValueError, match="conflict"):
        queue.enqueue_input(permission(), operator_activity=False)
    assert (
        queue.abandon_arrival_reservation(1, request_id="a" * 48, expected_revision="b" * 64)
        is False
    )
    assert queue.abandon_arrival_reservation(1, request_id="e" * 48, expected_revision="b" * 64)


def test_agent_dedup_uses_original_causal_parent_contract(queue):
    for seq in (1, 2):
        item = make_contributor(
            "system",
            "agent_continuation",
            1,
            {"causal_parent_episode_id": "parent", "cycle_id": str(seq)},
        )
        assert queue.enqueue_input(item, operator_activity=False) is (seq == 1)


def test_rejected_duplicate_publication_keeps_disk_reservation_consumption(queue, monkeypatch):
    queue.reserve_plugin_permission_arrival("a" * 48, "b" * 64, 1)
    queue.enqueue_input(permission(), operator_activity=False)
    queue.reserve_plugin_permission_arrival("a" * 48, "b" * 64, 3)
    original = queue.os.replace

    def publish_then_fail(source, target):
        original(source, target)
        raise OSError("committed")

    monkeypatch.setattr(queue.os, "replace", publish_then_fail)
    with pytest.raises(OSError):
        queue.enqueue_input(permission(3), operator_activity=False)
    assert 3 not in queue._ARRIVAL_RESERVATIONS


@pytest.mark.parametrize("published", [False, True])
def test_failed_ack_preserves_only_retryable_exact_lease(queue, monkeypatch, published):
    queue.enqueue_input(turn(1), operator_activity=False)
    leased = queue.lease_next_input(0)
    original = queue.os.replace

    def fail(source, target):
        if published:
            original(source, target)
        raise OSError("ack boundary")

    monkeypatch.setattr(queue.os, "replace", fail)
    with pytest.raises(OSError):
        queue.acknowledge_input(leased, 0)
    assert queue.owns_input_lease(leased, 0) is (not published)
    assert queue.queue_depth() == (0 if published else 1)


def test_reservation_expiry_only_releases_previous_process(queue, monkeypatch):
    monkeypatch.setattr(queue, "_wall_time", lambda: 100.0)
    queue.reserve_plugin_permission_arrival("a" * 48, "b" * 64, 1)
    queue.enqueue_input(turn(2), operator_activity=False)
    monkeypatch.setattr(queue, "_wall_time", lambda: 200.0)
    assert queue.lease_next_input(0) is None
    queue._reset_cache_for_tests()
    assert queue.lease_next_input(0).metadata["arrival_seq"] == 2


def test_restore_does_not_reopen_interpreted_turn(queue):
    queue.enqueue_input(turn(12), operator_activity=False)
    leased = queue.lease_next_input(0)
    queue.acknowledge_input(leased, 0)
    queue.mark_operator_turn_interpreted(12, "cycle-12", outcome="job_opened")
    queue._reset_cache_for_tests()
    assert queue.enqueue_restore(leased)
    assert queue.earlier_operator_turns_interpreted(13)


def test_admission_wakes_pressure_after_durable_publication(queue, monkeypatch):
    import pressure_loop.loop as pressure

    observed = []
    monkeypatch.setattr(
        pressure,
        "notify_work",
        lambda: observed.append(
            json.loads(queue._queue_path().read_text())["queue"][0]["metadata"]["arrival_seq"]
        ),
    )
    assert queue.enqueue_input(turn(1), operator_activity=False)
    assert observed == [1]


def test_migration_keeps_permission_state_and_collision_safe_archives(queue):
    state = {
        "_operator_queue_state": {
            "schema_version": 3,
            "plugin_permission_owners": [],
            "next_arrival_seq": 10,
            "arrival_reservations": [
                {
                    "arrival_seq": 8,
                    "request_id": "a" * 48,
                    "expected_revision": "b" * 64,
                    "expires_at": 9999999999.0,
                    "process_epoch": "c" * 32,
                }
            ],
        }
    }
    path = queue._legacy_state_path()
    path.write_text(json.dumps([state]))
    prior = path.with_name(path.name + ".migrated")
    prior.write_text("prior archive")
    assert queue.next_arrival_seq() == 10
    assert prior.read_text() == "prior archive"
    assert path.with_name(path.name + ".migrated.2").exists()
    queue._reset_cache_for_tests()
    queue.enqueue_input(turn(11), operator_activity=False)
    assert queue.lease_next_input(0) is None
    assert queue._ARRIVAL_RESERVATIONS[8]["request_id"] == "a" * 48


def test_concurrent_admission_stamps_one_global_sequence(queue):
    from concurrent.futures import ThreadPoolExecutor

    def enqueue(index):
        return queue.enqueue_input(
            make_contributor(
                "omega", "idle_wake", 1, {"lane": "agent", "cycle_id": f"internal-{index}"}
            ),
            operator_activity=False,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(enqueue, range(20)))
    assert [item.metadata["arrival_seq"] for item in queue.peek_inputs()] == list(range(1, 21))


def migrate_owners(queue, owners, rows=()):
    queue._legacy_state_path().write_text(
        json.dumps(
            [
                {
                    "_operator_queue_state": {
                        "schema_version": 3,
                        "plugin_permission_owners": owners,
                        "arrival_reservations": [],
                        "next_arrival_seq": 300,
                    }
                }
            ]
        )
    )
    if rows:
        queue._legacy_queue_path().write_text(json.dumps(list(rows)))
    queue.peek_inputs()


def permission_owner(state="queued", request="a" * 48):
    return {"request_id": request, "expected_revision": "b" * 64, "arrival_seq": 1, "state": state}


@pytest.mark.parametrize("state", ["queued", "inflight", "settled"])
def test_migrate_permission_owner_states_then_restore(queue, state):
    row = queue.serialize_input(permission())
    row["queue_id"] = "f" * 32
    migrate_owners(queue, [permission_owner(state)], [row] if state == "queued" else [])
    queue._reset_cache_for_tests()
    queue.peek_inputs()
    assert queue._PLUGIN_PERMISSION_OWNERS["a" * 48]["state"] == state
    item = queue.deserialize_input(row)
    assert queue.enqueue_restore(item) is (state == "inflight")
    if state == "settled":
        assert queue.lease_next_input(0) is None
        assert queue._PLUGIN_PERMISSION_OWNERS["a" * 48]["state"] == "settled"
    else:
        assert queue.acknowledge_input(queue.lease_next_input(0), 0)
        assert queue.settle_plugin_permission_decision("a" * 48, expected_revision="b" * 64)
    queue._reset_cache_for_tests()
    assert queue.queue_depth() == 0
    assert queue._PLUGIN_PERMISSION_OWNERS["a" * 48]["state"] == "settled"


@pytest.mark.parametrize("field,value", [("expected_revision", "e" * 64), ("arrival_seq", 2)])
def test_permission_restore_rejects_conflicting_owner_without_publication(queue, field, value):
    owner = permission_owner("inflight")
    owner[field] = value
    migrate_owners(queue, [owner])
    prior = queue._queue_path().read_bytes()
    row = queue.serialize_input(permission())
    row["queue_id"] = "f" * 32
    with pytest.raises(ValueError, match="conflict"):
        queue.enqueue_restore(queue.deserialize_input(row))
    assert queue._queue_path().read_bytes() == prior
    assert queue._PLUGIN_PERMISSION_OWNERS["a" * 48] == owner


@pytest.mark.parametrize("prunable", [False, True])
def test_permission_restore_enforces_durable_owner_capacity(queue, prunable):
    owners = [
        permission_owner("settled" if prunable and i == 0 else "inflight", f"{i:048x}")
        for i in range(256)
    ]
    migrate_owners(queue, owners)
    prior = queue._queue_path().read_bytes()
    row = queue.serialize_input(permission())
    row["queue_id"] = "f" * 32
    item = queue.deserialize_input(row)
    if prunable:
        assert queue.enqueue_restore(item)
    else:
        with pytest.raises(ValueError, match="limit_exceeded"):
            queue.enqueue_restore(item)
        assert queue._queue_path().read_bytes() == prior
    queue._reset_cache_for_tests()
    assert queue.queue_depth() == (1 if prunable else 0)
    assert len(queue._PLUGIN_PERMISSION_OWNERS) == 256


@pytest.mark.parametrize("message", ["hello Omega", "write a Python script", "read README"])
def test_operator_words_do_not_choose_pipeline(message):
    from core.pipelines import work_kind_for_contributor

    assert work_kind_for_contributor(from_operator_message({"message": message})) == "operator_turn"
