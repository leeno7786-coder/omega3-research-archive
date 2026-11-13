# F:/Omega3.0/tests/memory/taxonomy/test_canonical_capture.py
import json
from unittest.mock import patch, MagicMock
from memory.taxonomy.canonical_capture import capture_episode_to_canonical


def test_canonical_capture_routing_and_enrichment():
    episode = {
        "episode_id": "ep-test-123",
        "closed_at": "2026-06-29T10:00:00+00:00",
        "cascade_status": "completed",
        "auto": {
            "status": "completed",
            "action": "execute",
            "failure_kind": "",
            "failure_reason": "",
        },
        "meta": {
            "skill_result_snapshot": {
                "skill_name": "example_skill",
                "success": True,
                "linked_persons": ["operator"],
                "linked_coding": ["file.py"],
            }
        },
    }

    calls = []

    def fake_intake(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        calls.append(
            {
                "coord": env.coord,
                "payload": env.payload,
                "propose": propose,
                "bypass_guardian": bypass_guardian,
                "skip_embed": skip_embed,
            }
        )

    with (
        patch("memory.api.intake_memory", fake_intake),
        patch("memory.taxonomy.canonical_capture.write_episode_to_temporal") as mock_write_ep,
    ):
        report = capture_episode_to_canonical(episode)

        # 1. Verify report signature
        assert report["episode_id"] == "ep-test-123"
        assert report["events_written"] > 0
        assert not report["errors"]

        # 2. Verify write_episode_to_temporal was called
        mock_write_ep.assert_called_once_with(episode)

        # 3. Verify situational writes to Lobe 4
        # We expect qwen_skill_result (how -> [4, 5, 0]) and auto_outcome (outcome -> [4, 6, 0])
        coords = [c["coord"] for c in calls]
        assert [4, 5, 0] in coords  # how
        assert [4, 6, 0] in coords  # outcome

        # 4. Verify properties of calls (Guardian-free, embed enabled)
        for call in calls:
            assert call["propose"] is False
            assert call["bypass_guardian"] is True
            assert call["skip_embed"] is False

            enrich = call["payload"]["enrichment"]
            assert enrich["canonical"] is True
            assert enrich["episode_id"] == "ep-test-123"
            assert "event_kind" in enrich


def test_episodic_capture_is_guardian_free():
    episode = {
        "episode_id": "ep-test-guardian-free",
        "closed_at": "2026-06-29T10:00:00+00:00",
        "cascade_status": "completed",
    }

    calls = []

    def fake_intake(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        calls.append({"propose": propose, "bypass_guardian": bypass_guardian})

    with (
        patch("memory.api.intake_memory", fake_intake),
        patch("memory.taxonomy.canonical_capture.write_episode_to_temporal") as mock_write_ep,
    ):
        capture_episode_to_canonical(episode)

        # All writes staged from the capture loop must be direct (Guardian bypassed)
        for call in calls:
            assert call["propose"] is False
            assert call["bypass_guardian"] is True


def test_pressure_ignition_is_log_only_and_never_mints_a_situational_record():
    """Live 2026-09-05: every operator turn minted an 'Event pressure_ignition'
    lobe-4 record (50 in a day) that nothing recalled. The ignition is pipeline
    plumbing — it already rides inside the lobe-9 episode record — so the
    capture skips it and reports the skip instead of writing it."""
    episode = {
        "episode_id": "ep-ignite-1",
        "closed_at": "2026-09-05T10:00:00+00:00",
        "cascade_status": "completed",
        "pressure": {"pressure": 0.9, "ignition_reason": "operator_input", "source": "operator"},
        "auto": {"status": "completed", "action": "respond", "failure_kind": "", "failure_reason": ""},
    }
    calls = []

    def fake_intake(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        calls.append(env)

    with (
        patch("memory.api.intake_memory", fake_intake),
        patch("memory.taxonomy.canonical_capture.write_episode_to_temporal"),
    ):
        report = capture_episode_to_canonical(episode)

    assert not report["errors"]
    assert report["events_skipped"] == ["pressure_ignition"]
    kinds = [env.payload["enrichment"]["event_kind"] for env in calls]
    assert "pressure_ignition" not in kinds
    assert all(env.coord != [4, 1, 0] for env in calls)  # no 'when' facet record
    assert "pressure_ignition" not in " ".join(env.payload["title"] for env in calls)
