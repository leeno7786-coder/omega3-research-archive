from __future__ import annotations

from types import SimpleNamespace

import cognition_chain
from omega_loop.episode.types import OmegaEpisodeResult


def test_cognition_foldback_materializes_onboarding_children_for_next_episode(monkeypatch):
    context = {
        "occupation": "software_engineer",
        "categories": [{"title": "Python", "lobe": 8, "y": 0}],
        "source_citation": {"source_id": "src-onboarding"},
    }
    episode = SimpleNamespace(
        pressure={
            "contributors": [
                {
                    "detail": {
                        "origin": "onboarding_bootstrap",
                        "onboarding_context": context,
                    }
                }
            ]
        },
        global_={"outcome": {"artifacts": {}}},
        auto={},
    )
    result = OmegaEpisodeResult(
        episode_id="ep-onboarding",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params={},
        response_text=None,
        synthesis=None,
        reflection=None,
        onboarding_subtopics=({"parent": "Python", "subtopics": ["asyncio", "Decorators"]},),
    )

    for name in (
        "_advance_proactive_lifecycle",
        "_ingest_fold_knowledge",
        "_persist_motor_attempt_and_pressure",
        "_apply_build_stash_decision",
        "_record_trajectory_influence",
        "_record_signal_omega",
    ):
        monkeypatch.setattr(
            cognition_chain,
            name,
            lambda *args, **kwargs: None,
            raising=False,
        )

    seeded = []
    monkeypatch.setattr(
        "hud.operator_onboarding.seed_foldback_subtopics",
        lambda received_context, rows: (
            seeded.append((received_context, rows))
            or [{"subtopic": "asyncio", "parent": "Python", "lobe": 8}]
        ),
        raising=False,
    )

    cognition_chain._apply_omega_episode_result(episode, result)

    assert seeded == [(context, result.onboarding_subtopics)]
