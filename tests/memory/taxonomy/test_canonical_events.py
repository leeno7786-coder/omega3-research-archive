# F:/Omega3.0/tests/memory/taxonomy/test_canonical_events.py
import json
from pathlib import Path
from memory.taxonomy.canonical_events import read_events_from_canonical


def test_read_events_from_canonical(tmp_path):
    # Set up a mock memory hierarchy under tmp_path/memory
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(exist_ok=True)

    indexes_dir = mem_dir / "indexes"
    indexes_dir.mkdir(exist_ok=True)

    # 1. Create coord_index.json
    coord_index = {
        "id1": {"coord": [4, 0, 0], "path": "memory/lobe4_who.json"},
        "id2": {"coord": [9, 1, 0], "path": "memory/lobe9_ep.json"},
        "id3": {"coord": [9, 3, 0], "path": "memory/lobe9_cons.json"},
        "id4": {"coord": [2, 0, 0], "path": "memory/lobe2_ignored.json"},  # should be ignored
    }

    with open(indexes_dir / "coord_index.json", "w", encoding="utf-8") as f:
        json.dump(coord_index, f)

    # 2. Create the mock JSON memory files
    # Lobe 4 Situational record
    lobe4_rec = {
        "id": "id1",
        "coord": [4, 0, 0],
        "type": "situational",
        "enrichment": {
            "canonical": True,
            "event_kind": "operator_input",
            "episode_id": "ep-1",
            "closed_at": "2026-06-29T10:00:00Z",
            "event_id": "evt-who-1",
            "event_source": "operator_agent",
            "event_payload": {"message": "hello"},
        },
    }
    with open(mem_dir / "lobe4_who.json", "w", encoding="utf-8") as f:
        json.dump(lobe4_rec, f)

    # Lobe 9 episode record [9, 1, 0]
    lobe9_ep = {
        "id": "id2",
        "coord": [9, 1, 0],
        "text": json.dumps({"tool_calls": [{"name": "read"}]}),
        "enrichment": {
            "canonical": True,
            "event_kind": "auto_outcome",
            "episode_id": "ep-1",
            "closed_at": "2026-06-29T10:01:00Z",
            "event_id": "evt-ep-1",
        },
    }
    with open(mem_dir / "lobe9_ep.json", "w", encoding="utf-8") as f:
        json.dump(lobe9_ep, f)

    # Lobe 9 consolidation record [9, 3, 0]
    lobe9_cons = {
        "id": "id3",
        "coord": [9, 3, 0],
        "text": json.dumps(
            {
                "event_id": "evt-cons-1",
                "event_kind": "deepsleep_summary",
                "source": "deepsleep",
                "payload": {"consolidated": True},
            }
        ),
        "enrichment": {
            "canonical": True,
            "event_kind": "deepsleep_summary",
            "episode_id": "ep-1",
            "closed_at": "2026-06-29T10:02:00Z",
        },
    }
    with open(mem_dir / "lobe9_cons.json", "w", encoding="utf-8") as f:
        json.dump(lobe9_cons, f)

    # 3. Read events from canonical and assert properties
    events = read_events_from_canonical(root=tmp_path)

    assert len(events) == 3

    # Assert ordering: chronologically
    assert events[0].event_id == "evt-who-1"
    assert events[0].event_kind == "operator_input"
    assert events[0].payload == {"message": "hello"}

    assert events[1].event_id == "evt-ep-1"
    assert events[1].event_kind == "auto_outcome"
    assert events[1].payload == {"tool_calls": [{"name": "read"}]}

    assert events[2].event_id == "evt-cons-1"
    assert events[2].event_kind == "deepsleep_summary"
    assert events[2].payload == {"consolidated": True}

    # Assert filtering by event_kind
    filtered = read_events_from_canonical(event_kind="operator_input", root=tmp_path)
    assert len(filtered) == 1
    assert filtered[0].event_id == "evt-who-1"

    # Assert filtering by episode_id
    filtered_ep = read_events_from_canonical(episode_id="ep-1", root=tmp_path)
    assert len(filtered_ep) == 3
