"""I4 — the autonomic dopamine baseline must read live episode records.

write_episode_to_temporal writes lobe-9 records as type="episode" with the body
(flat neuromod) JSON in meta["text"] and NO score block. recent_cycles previously
filtered for score.kind=="cycle_event" — a shape no live writer emits — so it
returned [], _dopamine_avg_by_domain returned {}, d_avg defaulted to 0.0, and the
dopamine_plateau autonomic drive was permanently satisfied on a fabricated reading.
recent_cycles now synthesizes the structured score block from the episode body.
"""

from __future__ import annotations


def _write_episode(domain: str, *, dopamine: float, eid: str, plasticity=0.4, stress=0.1):
    from memory.episode_sram import close_bucket, open_bucket
    from pressure_loop.temporal_store import write_episode_to_temporal

    open_bucket(eid, pipeline_id=0)
    write_episode_to_temporal(
        {
            "episode_id": eid,
            "closed_at": f"2026-06-18T00:00:0{eid[-1]}+00:00",
            "cascade_status": "completed",
            "neuromod": {
                "dopamine": dopamine,
                "plasticity": plasticity,
                "stress": stress,
                "cycle_kind": "success",
            },
            "topic_history": {"domain": domain},
        }
    )
    close_bucket(eid)


def test_recent_cycles_synthesizes_score_from_episode_record(tmp_path):
    from autonomic import _history

    _write_episode("quantum_physics", dopamine=0.8, eid="ep-i4-1")
    cycles = _history.recent_cycles(limit=10)

    assert cycles, "an episode record must surface as a cycle"
    c = cycles[0]
    assert c.get("domain") == "quantum_physics"
    assert c["score"]["dopamine"]["score"] == 0.8
    assert c["score"]["stress"]["level"] == 0.1
    assert c["score"]["plasticity"]["strength"] == 0.4
    assert c["score"]["cycle_kind"] == "success"


def test_extract_dopamine_outcomes_reads_episode_dopamine(tmp_path):
    from autonomic import _history

    _write_episode("topology", dopamine=0.7, eid="ep-i4-2")
    outcomes = _history.extract_dopamine_outcomes(_history.recent_cycles(limit=10), {})
    assert 0.7 in outcomes


def test_dopamine_avg_by_domain_is_not_stuck_at_zero(tmp_path):
    from autonomic.cognitive_churn import _dopamine_avg_by_domain

    _write_episode("genetics", dopamine=0.6, eid="ep-i4-3")
    by_dom = _dopamine_avg_by_domain(cycle_limit=10)
    assert by_dom.get("genetics") == 0.6
