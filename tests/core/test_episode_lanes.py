"""Descriptive provenance never chooses or changes physical episode ownership."""

import pytest

from core.pipelines import work_kind_for_contributor
from pressure_loop.contributors import make_contributor
from pressure_loop.episode import EpisodeStore


@pytest.mark.parametrize("pipeline_id", [0, 1, 2, 3])
@pytest.mark.parametrize("source,kind,expected", [
    ("operator", "operator_query", "operator_turn"),
    ("omega", "agent_motor_dispatch", "agent_continuation"),
    ("omega", "window_reflection_ready", "reflection"),
    ("omega", "operator_priority", "autonomous"),
])
def test_provenance_opens_on_the_explicit_free_pipeline(pipeline_id, source, kind, expected):
    contributor = make_contributor(source, kind, 1, {})
    store = EpisodeStore()
    episode = store.open(pipeline_id=pipeline_id,
                         work_kind=work_kind_for_contributor(contributor), opened_by=kind)
    assert store.current(pipeline_id=pipeline_id) is episode
    assert episode.pipeline_id == pipeline_id
    assert episode.work_kind == expected
    assert episode.opened_by == kind
