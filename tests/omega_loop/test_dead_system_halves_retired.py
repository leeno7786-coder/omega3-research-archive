"""The dead system halves of Omega's episode prompt builders are retired.

Operator, 2026-09-21: "retire the rest of the dead system halves too".

Every live lane pack builds its own system prompt (``organ/promptorgan/packs/meta_omega/*``). The
agent and chat packs took only the USER half of ``_build_knowledge``, ``_build_apply``,
``_build_present_result`` and ``_build_chat_reflection``, and ``_build_response`` had no
production caller at all. Those system halves, and every helper and constant that existed only to
fill them, were prompt text no model ever read. The builders now return the user message alone,
and the live prompts are unchanged byte for byte (digests in
``docs/superpowers/evidence/2026-09-21-retire-dead-system-halves/``).

The retired handoff advisory was the only reader of the Step 5 plateau projection, so that goes
too: ``learning_progress.plateau_signal``, the projection onto ``MasteryView`` in
``packet._prior_mastery_for_domain``, and the ``OMEGA_HANDOFF_ENABLED`` flag. DeepSleep's mastery
pass keeps its own plateau bookkeeping through ``plateau_from``, ``append_delta`` and
``emit_plateau``, which are untouched.

``MasteryView``'s five plateau fields went with it (operator 2026-09-21: "drop the vestigial plateau
fields too"). A persisted packet is rebuilt with ``MasteryView(**pm)``, so a packet saved before
the change with those keys now fails to load — loudly, with the offending field named, which is
pinned below. That was the operator's call over keeping fields nothing sets or reads.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from omega_loop.episode import prompts
from tests.omega_loop.test_foldback_prompt_dispatch import packet_fixture

RETIRED = (
    # the four tuple builders whose system half no live lane sent, and the wholly dead one
    "_build_knowledge",
    "_build_apply",
    "_build_present_result",
    "_build_chat_reflection",
    "_build_response",
    "_parse_response",
    # helpers and constants that only ever filled those system halves
    "_richer_schema_note",
    "_handoff_advisory",
    "_pressure_lifecycle_schema_note",
    "_mind_primacy_advisory",
    "_foldback_monitor_system",
    "_FOLDBACK_MONITOR_SUFFIX",
    "_FOLDBACK_CONTROL_RED",
    "_FOLDBACK_CONTROL_RED_STALLED",
    "_FOLDBACK_CONTROL_GREEN",
    "_FOLDBACK_CONTROL_PASSOFF",
    "_CHAT_REFLECTION_SYSTEM",
)


@pytest.mark.parametrize("name", RETIRED)
def test_the_dead_prompt_text_is_gone(name):
    assert not hasattr(prompts, name), f"{name} is still defined; no live lane sends it"


@pytest.mark.parametrize(
    ("name", "packet"),
    [
        ("_knowledge_user", packet_fixture("agent", response_text=None)),
        ("_apply_user", packet_fixture("agent")),
        ("_present_result_user", packet_fixture("chat", response_text="1) 0.5")),
        ("_chat_reflection_user", packet_fixture("chat", response_text="Here is my answer.")),
    ],
)
def test_each_builder_returns_only_the_user_message(name, packet):
    message = getattr(prompts, name)(packet)

    assert isinstance(message, str) and message.strip(), (
        f"{name} must return the user message alone, as a non-empty string"
    )


def test_the_handoff_chain_that_fed_only_a_dead_advisory_is_gone():
    from pressure_loop import learning_progress

    assert not hasattr(learning_progress, "OMEGA_HANDOFF_ENABLED"), (
        "the flag's only reader was the retired _handoff_advisory; a flag that gates nothing "
        "tells the next reader something false"
    )
    assert not hasattr(learning_progress, "plateau_signal"), (
        "its only caller projected plateau facts onto MasteryView for the retired advisory"
    )
    # DeepSleep's own plateau bookkeeping stays.
    for name in ("plateau_from", "append_delta", "emit_plateau"):
        assert callable(getattr(learning_progress, name)), f"{name} must stay"


def test_masteryview_carries_only_the_mastery_numbers(monkeypatch):
    from deepsleep_loop._deepsleep_state import MasteryRecord
    from omega_loop.episode import packet
    from omega_loop.episode.types import MasteryView

    assert {f.name for f in dataclasses.fields(MasteryView)} == {
        "domain", "knowledge_mastery", "application_mastery", "target_mastery",
    }, "the plateau fields had no reader once the handoff advisory was retired"

    record = MasteryRecord(
        domain_id="ai", domain_name="AI", knowledge_mastery=0.7, target_mastery=0.7,
        evidence_count=4, recent_knowledge_deltas=(0.01, 0.005, 0.008),
    )
    monkeypatch.setattr(packet, "load_mastery", lambda *a, **k: {"ai": record})
    monkeypatch.setattr(packet, "normalize_domain_id", lambda d: "ai")

    view = packet._prior_mastery_for_domain("ai")

    assert view == MasteryView("AI", 0.7, 0.0, 0.7)


def test_a_packet_saved_with_the_retired_plateau_fields_fails_loudly():
    """The operator chose dropping the fields over keeping them for old packets. The failure must
    name the field, so it is never mistaken for a hollow or missing packet (AGENTS.md 2.1)."""
    from dataclasses import replace

    from omega_loop.episode.run import _packet_from_episode
    from omega_loop.episode.types import MasteryView

    saved = replace(packet_fixture("agent"), prior_mastery=MasteryView("python", 0.4, 0.1, 0.7)).to_dict()
    saved["prior_mastery"].update(plateau=True, sample_count=4)

    with pytest.raises(TypeError, match="plateau"):
        _packet_from_episode(SimpleNamespace(global_={"omega_packet": saved}))


def test_the_agenda_only_the_dead_advisory_rendered_is_gone():
    from omega_loop.episode import packet
    from omega_loop.episode.types import OmegaEpisodeInput

    assert "mastery_agenda" not in {f.name for f in dataclasses.fields(OmegaEpisodeInput)}
    assert not hasattr(packet, "_mastery_agenda_view"), (
        "the agenda read the mastery store on every episode for a renderer that no longer exists"
    )


def test_a_packet_saved_before_the_retirement_still_loads():
    """Removing a packet field must not break a packet persisted with it."""
    from omega_loop.episode.run import _packet_from_episode

    saved = packet_fixture("agent").to_dict()
    saved["mastery_agenda"] = [["python", 0.1, 0.7, "learning"]]

    rebuilt = _packet_from_episode(SimpleNamespace(global_={"omega_packet": saved}))

    assert rebuilt is not None and rebuilt.episode_id == "lane-test"
