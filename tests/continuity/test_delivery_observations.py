"""WO-05 Step 6 — proposed, queued, attempted, confirmed, and honestly uncertain.

The order: *"Distinguish message/artifact proposal, queueing, delivery attempt and
provider-confirmed delivery. For interrupted external effects, retain uncertain status;
instrumentation must not retry the action."*

Acceptance case "Side effects": *a timed-out delivery is uncertain until a real receipt
resolves it, never automatically resent.*

The distinction that carries the risk is **failed versus uncertain**. A send that provably
never left is safe to retry. A send that timed out mid-flight may already have arrived, and
retrying it delivers the message twice — to a real person, in the operator's name. Collapsing
the two into one "it didn't work" is how a double-send happens, so `uncertain` is a state of its
own and nothing here is allowed to advance it without a provider receipt.

There is also a rule about this module rather than its data: instrumentation observes, it does
not act. A capture layer that can resend is a capture layer that will, on some retry path
nobody meant to arm.
"""

from __future__ import annotations

import pytest

from memory import auto_evidence


def test_the_four_delivery_stages_are_distinct():
    states = auto_evidence.DELIVERY_STATES

    for stage in ("proposed", "queued", "attempted", "confirmed"):
        assert stage in states, f"{stage} has no state, so it cannot be told from the others"
    assert "uncertain" in states, "an interrupted effect needs a state that is not failure"
    assert "failed" in states
    assert len(set(states)) == len(states), "a duplicated state cannot be reasoned about"


def test_a_proposal_is_not_a_delivery():
    """A drafted message is not a sent one, and the record must not blur them."""
    proposed = auto_evidence.delivery_receipt(target="operator:chat", state="proposed")

    assert proposed["state"] == "proposed"
    assert proposed["delivered"] is False, (
        "a proposal reported itself as delivered; nothing has left the process yet"
    )
    assert not proposed["provider_receipt"]


def test_confirmed_delivery_requires_a_real_provider_receipt():
    """"Provider-confirmed" is a claim about the provider, not about our hopes.

    Without a receipt there is no evidence the message arrived, so the state cannot be claimed.
    This is the one state that asserts something about the outside world.
    """
    with pytest.raises(ValueError) as caught:
        auto_evidence.delivery_receipt(target="email:someone", state="confirmed")

    assert "receipt" in str(caught.value).lower()

    ok = auto_evidence.delivery_receipt(
        target="email:someone", state="confirmed", provider_receipt="provider-msg-id-4417"
    )
    assert ok["delivered"] is True
    assert ok["provider_receipt"] == "provider-msg-id-4417"


def test_a_timed_out_delivery_is_uncertain_and_not_failed():
    """The acceptance case. The distinction that prevents a double-send."""
    timed_out = auto_evidence.delivery_receipt(
        target="email:someone",
        state="uncertain",
        reason="the provider connection timed out after the request was written",
    )

    assert timed_out["state"] == "uncertain"
    assert timed_out["delivered"] is False, "an uncertain send must not claim delivery"
    assert timed_out["safe_to_retry"] is False, (
        "an uncertain send was marked safe to retry; it may already have arrived, and resending "
        "delivers the message twice under the operator's name"
    )
    assert timed_out["reason"], "an uncertain state without its cause cannot be resolved later"


def test_a_provably_failed_delivery_is_retryable_but_an_uncertain_one_is_not():
    """The asymmetry, asserted side by side so neither can drift into the other."""
    failed = auto_evidence.delivery_receipt(
        target="email:someone", state="failed", reason="the provider refused the recipient"
    )
    uncertain = auto_evidence.delivery_receipt(
        target="email:someone", state="uncertain", reason="connection dropped mid-write"
    )

    assert failed["safe_to_retry"] is True
    assert uncertain["safe_to_retry"] is False
    assert failed["safe_to_retry"] != uncertain["safe_to_retry"], (
        "a refusal and an interrupted send report the same retry safety, so 'can I send this "
        "again?' has no answer"
    )


def test_an_uncertain_delivery_is_resolved_only_by_a_real_receipt():
    uncertain = auto_evidence.delivery_receipt(
        target="email:someone", state="uncertain", reason="connection dropped"
    )

    with pytest.raises(ValueError):
        auto_evidence.resolve_delivery(uncertain, provider_receipt="")

    resolved = auto_evidence.resolve_delivery(uncertain, provider_receipt="provider-msg-id-99")
    assert resolved["state"] == "confirmed"
    assert resolved["delivered"] is True
    assert resolved["provider_receipt"] == "provider-msg-id-99"
    # The original is untouched: a resolution is a new fact, not an edit of the old one.
    assert uncertain["state"] == "uncertain"


def test_resolution_cannot_turn_an_uncertain_send_into_a_failure_without_evidence():
    """Resolving downward is a claim too.

    "It never arrived" needs provider evidence exactly as much as "it did". Otherwise an
    uncertain send could be quietly written off and then legitimately resent.
    """
    uncertain = auto_evidence.delivery_receipt(
        target="email:someone", state="uncertain", reason="connection dropped"
    )

    with pytest.raises(ValueError):
        auto_evidence.resolve_delivery(uncertain, provider_receipt="", failed_reason="")


def test_the_instrumentation_exposes_no_way_to_resend():
    """A rule about this module, not its data.

    Instrumentation observes; it does not act. If a resend lived here it would eventually be
    called from some retry path nobody meant to arm, and the thing being resent is a message to
    a real person.
    """
    forbidden = [
        name
        for name in dir(auto_evidence)
        if not name.startswith("_")
        and any(word in name.lower() for word in ("resend", "retry", "send_again", "deliver_now"))
    ]

    assert forbidden == [], (
        f"memory/auto_evidence.py exposes {forbidden}; a capture layer that can perform the "
        "action will eventually be asked to"
    )
