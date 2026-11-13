"""Omega speaks on boot — the residency proof you can hear (operator 2026-09-22).

The operator's test for "are the speech models actually resident from boot?": make Omega SPEAK at
boot. Before onboarding is complete it always speaks the onboarding phrase; after, it always greets
the operator by name, with variety. If you hear it, the whole chain is alive — weights resident on
the GPU, synthesis, PCM over HTTP, scheduled audio, playback. If you do not, something is broken and
you know before you have said a word to it.

Two deliberate exceptions, both stated rather than assumed:

* **The voice pill boots OFF, and the greeting speaks anyway.** A default-off toggle is not a
  considered choice, and this utterance is the boot probe.
* **An explicit ``tts_enabled: false`` is still honoured.** That one IS a considered choice, and
  §0.1 says a boundary the operator set is a boundary Omega keeps.

And two things it must never do (AGENTS.md §0: no person-specific defaults):

* never invent, guess, or infer a name — a name comes from the operator's own recorded profile,
  and until one exists Omega says "operator", which is a role, not an identity;
* never assume a timezone, which rules out "good morning".
"""

from __future__ import annotations

import pytest

from pressure_loop import boot_greeting as bg
from pressure_loop import proactive_chat_messages as pm


@pytest.fixture(autouse=True)
def _isolate_proactive_store(tmp_path, monkeypatch):
    """Keep this suite out of the operator's live notices.

    ``proactive_chat_messages._store_path`` resolves ``OMEGA_ROOT/guardian/state`` directly, so the
    session's ``redirect_memory_to_tmp`` does not reach it — the same gap the continuity conftest
    records for ``reflection_window``. Measured the hard way on 2026-09-22: an earlier version of
    this suite wrote three "Hello, Noah." records into the live store.
    """
    monkeypatch.setattr(pm, "_store_path", lambda: tmp_path / "proactive_chat_messages.json")
    pm._reset_cache_for_tests()
    bg.reset_for_tests()
    yield
    pm._reset_cache_for_tests()
    bg.reset_for_tests()


def test_before_onboarding_it_always_speaks_the_onboarding_phrase():
    phrase = bg.greeting_text(setup_completed=False, name="Noah")

    assert phrase == bg.ONBOARDING_PHRASE
    assert "Noah" not in phrase, (
        "onboarding comes first; a name recorded before setup completed is not a greeting"
    )


def test_after_onboarding_it_greets_the_operator_by_name():
    phrase = bg.greeting_text(setup_completed=True, name="Noah", pick=lambda seq: seq[0])

    assert "Noah" in phrase
    assert phrase == bg.NAMED_GREETINGS[0].format(name="Noah")


def test_the_greeting_varies():
    """"a variety of greeting on boot" — more than one, and every one of them usable."""
    assert len(bg.NAMED_GREETINGS) >= 5
    rendered = {template.format(name="Noah") for template in bg.NAMED_GREETINGS}
    assert len(rendered) == len(bg.NAMED_GREETINGS), "no duplicate greetings"
    for text in rendered:
        assert "Noah" in text
        assert text.strip() == text and text.endswith((".", "!", "?"))

    seen = {bg.greeting_text(setup_completed=True, name="Noah",
                             pick=lambda seq, i=index: seq[i % len(seq)])
            for index in range(len(bg.NAMED_GREETINGS))}
    assert len(seen) == len(bg.NAMED_GREETINGS), "each template is reachable"


def test_no_greeting_assumes_a_time_of_day():
    for template in bg.NAMED_GREETINGS + (bg.ONBOARDING_PHRASE,):
        lowered = template.lower()
        for forbidden in ("morning", "afternoon", "evening", "tonight", "today"):
            assert forbidden not in lowered, f"{template!r} assumes a timezone (§0)"


def test_an_unrecorded_name_is_addressed_as_operator_never_invented():
    """Operator 2026-09-22: "either the operator's name, or simply operator if there is none."

    A role noun is not a person-specific default: it assumes nothing about who is listening, which
    is exactly what §0 asks for. What it must never do is reach for the machine's owner, a login,
    or a guess."""
    for absent in (None, "", "   "):
        phrase = bg.greeting_text(setup_completed=True, name=absent, pick=lambda seq: seq[0])
        assert phrase == bg.NAMED_GREETINGS[0].format(name=bg.FALLBACK_NAME)
        assert "operator" in phrase.lower()
        assert "{" not in phrase and "None" not in phrase


def test_a_name_is_used_as_recorded_and_bounded():
    assert "Ada" in bg.greeting_text(setup_completed=True, name="  Ada  ",
                                     pick=lambda seq: seq[0])
    long_name = "N" * 400
    phrase = bg.greeting_text(setup_completed=True, name=long_name, pick=lambda seq: seq[0])
    assert len(phrase) < 200, "a pathological profile value cannot become a pathological utterance"


# ── what it reads, and what it refuses to read ────────────────────────────────


def test_the_name_comes_from_the_operators_own_profile_memory(monkeypatch):
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Noah")
    monkeypatch.setattr(bg, "_setup_completed", lambda: True)

    assert "Noah" in bg.boot_greeting()


def test_a_profile_read_failure_is_nameless_rather_than_a_guess(monkeypatch):
    def _explode():
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(bg, "_recall_operator_name", _explode)
    monkeypatch.setattr(bg, "_setup_completed", lambda: True)

    phrase = bg.boot_greeting()
    assert "operator" in phrase.lower(), (
        "a failed recall must not become a name, and must not silence the boot probe"
    )
    assert phrase != bg.ONBOARDING_PHRASE, "setup is complete; this is a greeting, not onboarding"


def test_an_unreadable_setup_state_speaks_onboarding(monkeypatch):
    def _explode():
        raise RuntimeError("state unreadable")

    monkeypatch.setattr(bg, "_setup_completed", _explode)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Noah")

    assert bg.boot_greeting() == bg.ONBOARDING_PHRASE, (
        "if we cannot tell whether setup finished, onboarding is the safe thing to say"
    )


def test_operator_profile_controls_onboarding_even_when_launcher_setup_is_complete(
    monkeypatch, tmp_path
):
    """The backend setup wizard is not the operator identity onboarding source."""
    import core.paths
    import hud.operator_identity

    (tmp_path / "startup_setup.json").write_text(
        '{"setup_completed": true}', encoding="utf-8"
    )
    monkeypatch.setattr(core.paths, "GUARDIAN_STATE_ROOT", tmp_path)
    monkeypatch.setattr(hud.operator_identity, "is_onboarded", lambda: False)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Ada")

    assert bg._setup_completed() is False
    assert bg.boot_greeting() == bg.ONBOARDING_PHRASE


def test_operator_profile_completion_wins_when_launcher_setup_is_incomplete(
    monkeypatch, tmp_path
):
    """A saved operator identity remains onboarded regardless of launcher wizard state."""
    import core.paths
    import hud.operator_identity

    (tmp_path / "startup_setup.json").write_text(
        '{"setup_completed": false}', encoding="utf-8"
    )
    monkeypatch.setattr(core.paths, "GUARDIAN_STATE_ROOT", tmp_path)
    monkeypatch.setattr(hud.operator_identity, "is_onboarded", lambda: True)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Ada")

    assert bg._setup_completed() is True
    assert "Ada" in bg.boot_greeting()


# ── publication: once per boot, spoken even with the pill off ─────────────────


def test_publication_marks_the_notice_as_always_spoken(monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr(bg, "_append_notice", lambda **kw: published.append(dict(kw)) or True)
    monkeypatch.setattr(bg, "_setup_completed", lambda: True)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Noah")
    bg.reset_for_tests()

    assert bg.publish_boot_greeting() is True
    assert len(published) == 1
    notice = published[0]
    assert "Noah" in notice["text"]
    assert notice["speak_always"] is True, "the pill boots off; this utterance still speaks"
    assert notice["kind"] == bg.NOTICE_KIND


def test_a_second_call_in_the_same_boot_does_not_greet_twice(monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr(bg, "_append_notice", lambda **kw: published.append(dict(kw)) or True)
    monkeypatch.setattr(bg, "_setup_completed", lambda: True)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Noah")
    bg.reset_for_tests()

    assert bg.publish_boot_greeting() is True
    assert bg.publish_boot_greeting() is False, "one greeting per boot, not one per HUD refresh"
    assert len(published) == 1


def test_a_publication_failure_is_reported_not_swallowed(monkeypatch):
    def _explode(**_kw):
        raise RuntimeError("no rail")

    monkeypatch.setattr(bg, "_append_notice", _explode)
    monkeypatch.setattr(bg, "_setup_completed", lambda: True)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Noah")
    bg.reset_for_tests()

    assert bg.publish_boot_greeting() is False
    assert bg.last_failure() is not None and "no rail" in bg.last_failure()
    assert bg.publish_boot_greeting() is False, "a failed greeting may retry, not double-speak"


def test_the_greeting_is_not_a_chat_reply(monkeypatch):
    """§4.1: only the actual action result resolves a turn. A greeting answers nothing."""
    import inspect

    source = inspect.getsource(bg)
    # A CALL, not the word: the module's own docstring promises it never touches these, and that
    # promise should be allowed to say so.
    assert "append_response(" not in source
    assert "publish_reply(" not in source
    assert '"chat.reply"' not in source


def test_the_same_greeting_may_be_said_again_on_a_later_boot(monkeypatch):
    """The proactive rail dedupes identical text against its whole history, because an autonomous
    nudge repeated forty times is spam. A boot greeting is the opposite: saying it again is the
    point — you greet every time you wake up — and with a handful of templates a collision across
    boots is certain. So this kind is exempt, and its once-per-boot guarantee comes from its own
    state rather than from the rail's spam filter."""
    monkeypatch.setattr(bg, "_setup_completed", lambda: True)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Noah")
    monkeypatch.setattr(bg, "greeting_text", lambda **_kw: "Hello, Noah.")

    bg.reset_for_tests()
    assert bg.publish_boot_greeting() is True, "first boot greets"

    bg.reset_for_tests()  # a later boot of the same process-lifetime story
    assert bg.publish_boot_greeting() is True, (
        "a second boot must greet again even though the words are identical"
    )
    greetings = [row for row in pm.list_pending() if row.get("kind") == bg.NOTICE_KIND]
    assert len(greetings) == 2, greetings


def test_the_speak_flag_survives_the_transport_to_the_hud(monkeypatch):
    """The round trip §2.1 demands: a flag written by the producer must still be there when the
    consumer parses it.

    This failed live on 2026-09-22 — the greeting was published with speak_always, the proactive
    read route projected only id/message/episode_id/opened_by/intent/ts/delivery, and the HUD saw a
    plain notice that the voice pill then silenced. Published, delivered, and never heard.
    """
    from fastapi.testclient import TestClient
    from hud.server import create_app

    monkeypatch.setattr(bg, "_setup_completed", lambda: True)
    monkeypatch.setattr(bg, "_recall_operator_name", lambda: "Noah")
    assert bg.publish_boot_greeting() is True

    body = TestClient(create_app()).get("/api/proactive/notices").json()
    greetings = [row for row in body["messages"] if row.get("opened_by") == "boot_greeting"]
    assert len(greetings) == 1, body
    payload = greetings[0].get("payload") or {}
    assert payload.get("speak_always") is True, (
        "the flag that lets the greeting speak past the pill did not survive the transport"
    )
    assert "Noah" in greetings[0]["message"]
