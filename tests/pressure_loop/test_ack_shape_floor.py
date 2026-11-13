"""An ``ack`` label never reaches the command rail (operator 2026-09-05).

History: the first post-day-zero boot (2026-07-19) classified the operator's
26-word self-introduction ``ack``, rode the command rail, and dispatched a
workspace build job about the introduction itself. A work-order-shape floor
then decided which ack-labelled text could ride that rail.

The rail is gone. Imperative work and go-aheads are CONVERSATION for the chat
brain, which calls its tools on demand inside AUTO; only an explicit learning
ask and an explicit "remember" are deterministic commands. So no ack label —
short, long, work-order shaped or declarative — can route a turn to a build,
and the self-introduction misroute cannot recur by construction.
"""

from __future__ import annotations

import pytest

from pressure_loop.operator_chat import _chat_ingress_class

_INTRO = (
    "I'm Noah, your new operator. I own a business called Omega AI LLC "
    "and I also created Omega3.0(you). I have a son named Abel too."
)


@pytest.mark.parametrize(
    "text",
    [
        "yes do it",
        "go ahead",
        _INTRO,
        "Build me a dashboard for the sensory sync counters with a live graph per capability.",
        "I want you to take the persona graph and integrate the face registry.",
        "Omega, create a summary document of everything we shipped today.",
        "My company builds AI systems. We created three products last year.",
    ],
)
def test_an_ack_label_never_routes_to_the_command_rail(text):
    assert _chat_ingress_class(text, intent="ack") != "operator_command", text


def test_only_explicit_learn_and_remember_are_commands():
    assert _chat_ingress_class("learn about CRDTs", intent="ack") == "operator_command"
    assert _chat_ingress_class("remember I use vim", intent="freeform") == "operator_command"
    assert _chat_ingress_class("build a todo app", intent="freeform") == "phatic"
    assert _chat_ingress_class("can you build a todo app?", intent="question") == (
        "epistemic_request"
    )
