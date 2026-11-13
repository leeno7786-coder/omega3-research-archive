"""Global arrivals survive migration, restart, and parallel producers."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from pressure_loop import cognition_queue as queue
from pressure_loop.contributors import from_operator_message


def _legacy(seq, cycle):
    return {"source": "operator", "type": "operator_query", "weight": 1,
            "metadata": {"message": "waiting turn", "cycle_id": cycle, "arrival_seq": seq}}


def test_existing_legacy_turn_precedes_new_producer_after_restart():
    path = queue._legacy_lines_dir() / "chat.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([_legacy(4, "old")]), encoding="utf-8")
    seq = queue.next_arrival_seq()
    assert seq == 5
    queue.enqueue_input(from_operator_message({"message": "new", "cycle_id": "new", "arrival_seq": seq}),
                        operator_activity=False)
    assert [c.metadata["cycle_id"] for c in queue.peek_inputs()] == ["old", "new"]
    assert path.with_name("chat.json.migrated").exists()
    queue._reset_cache_for_tests()
    assert queue.next_arrival_seq() == 6


def test_all_old_files_contribute_to_one_counter():
    for name, seq in [("chat", 2), ("agent", 41), ("meta", 7)]:
        path = queue._legacy_lines_dir() / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([_legacy(seq, name)]), encoding="utf-8")
    assert queue.next_arrival_seq() == 42
    assert [c.metadata["arrival_seq"] for c in queue.peek_inputs()] == [2, 7, 41]


def test_concurrent_stamps_are_unique_and_persisted_without_queued_rows():
    with ThreadPoolExecutor(max_workers=8) as pool:
        stamps = sorted(pool.map(lambda _: queue.next_arrival_seq(), range(64)))
    assert stamps == list(range(1, 65))
    queue._reset_cache_for_tests()
    assert queue.next_arrival_seq() == 65


def test_unreadable_legacy_source_fails_repeatedly_without_archival():
    path = queue._legacy_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("broken-json", encoding="utf-8")
    for _ in range(2):
        with pytest.raises(queue.CognitionQueueUnreadable):
            queue.next_arrival_seq()
    assert path.read_text(encoding="utf-8") == "broken-json"


def test_completed_screen_order_cannot_override_reserved_arrival():
    first = queue.reserve_operator_arrival("first")
    second = queue.reserve_operator_arrival("second")
    for seq, cycle in [(second, "second"), (first, "first")]:
        queue.enqueue_input(from_operator_message({"message": cycle, "cycle_id": cycle, "arrival_seq": seq}),
                            operator_activity=False)
    first_lease = queue.lease_next_input(0)
    assert first_lease is not None and first_lease.metadata["cycle_id"] == "first"
    assert queue.acknowledge_input(first_lease, 0)
    second_lease = queue.lease_next_input(0)
    assert second_lease is not None and second_lease.metadata["cycle_id"] == "second"
    assert queue.lease_next_input(1) is None
