"""The queue is the ONLY gate into the mind — RED-first (AGENTS.md §4.0).

Operator, 2026-07-31: *"The queue owns the pressure lane. Every outside input
goes through the queue — no matter if we had 12 different pipelines, they all
pay toll at the queue. The queue is the converging point for everything to enter
the mind."* And: *"It directs traffic on a first come, first serve basis, in the
order it arrived."*

Each test asserts the CONFIGURED VALUE and the FAIL-MODE, both directions
(doctrine 2026-06-09). Findings audited in docs/CASCADE_AUDIT_2026-07-31.md.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.contributors import from_operator_message


@pytest.fixture(autouse=True)
def _isolated_box(tmp_path, monkeypatch):
    """Per-test queue file; reset the module cache both sides of the test."""
    import pressure_loop.cognition_queue as queue_mod

    monkeypatch.setattr(
        queue_mod, "_queue_path", lambda: tmp_path / "operator_input_queue.json"
    )
    queue_mod._reset_cache_for_tests()
    yield
    queue_mod._reset_cache_for_tests()


def _turn(text: str, cycle_id: str, **extra):
    return from_operator_message(
        {
            "message": text,
            "ingress_class": extra.pop("ingress_class", "epistemic_request"),
            "weight": 1.0,
            "cycle_id": cycle_id,
            **extra,
        }
    )


def test_enqueue_failure_does_not_claim_a_durable_place(monkeypatch):
    """A failed atomic replace must reach the caller and leave no queued turn."""
    import pressure_loop.cognition_queue as queue_mod

    def _replace_fails(*_args, **_kwargs):
        raise OSError("operator queue disk unavailable")

    monkeypatch.setattr(queue_mod.os, "replace", _replace_fails)

    with pytest.raises(OSError, match="operator queue disk unavailable"):
        queue_mod.enqueue_input(_turn("do not acknowledge me", "persist-1"), operator_activity=False)

    assert not queue_mod.peek_inputs()


def test_acknowledge_failure_restores_the_owned_head(monkeypatch):
    """A failed durable acknowledgement must not consume or release the head."""
    import pressure_loop.cognition_queue as queue_mod

    turn = _turn("do not consume me", "persist-ack-1")
    assert queue_mod.enqueue_input(turn, operator_activity=False)
    turn = queue_mod.peek_inputs()[0]
    assert queue_mod.lease_next_input(pipeline_id=0) is turn

    def _replace_fails(*_args, **_kwargs):
        raise OSError("operator queue disk unavailable")

    monkeypatch.setattr(queue_mod.os, "replace", _replace_fails)

    with pytest.raises(OSError, match="operator queue disk unavailable"):
        queue_mod.acknowledge_input(turn, pipeline_id=0)

    assert queue_mod.peek_inputs() == (turn,)
    assert queue_mod.lease_next_input(pipeline_id=0) is None


# ── §4.0 the mirror is gone: the queue is the only road to ignition ───────────


def test_no_operator_contributor_is_gathered_from_shared_state():
    """gather_contributors must not synthesise an operator turn from SharedState.

    A retired SharedState mirror was a second gate running newest-wins against
    the queue's oldest-first (audit §1.2). The queue is now the only source of
    operator pressure.
    """
    from core.shared_state import SharedState
    from pressure_loop.pressure import gather_contributors

    ss = SharedState()
    gathered = gather_contributors(ss)

    operator_rows = [c for c in gathered if c.source == "operator"]
    assert operator_rows == [], (
        "an operator turn reached the contributor set without passing the queue: "
        f"{[c.metadata.get('message') for c in operator_rows]}"
    )


def test_operator_pressure_comes_from_the_queue_not_a_slot():
    """The LINE is the pressure — non-empty queue means pressure, empty means none.

    No TTL, no decay: a line self-clears because the head walks through the gate
    (audit §1.13, §1.6). FAIL-MODE today: five silent `return 0.0` branches make
    "the operator said something" and "said nothing" identical.
    """
    from core.shared_state import SharedState
    from pressure_loop.cognition_queue import enqueue_input
    from pressure_loop.pressure_field import read_operator_message

    ss = SharedState()
    assert read_operator_message(ss) == 0.0, "an empty line must carry no pressure"

    assert enqueue_input(_turn("someone is waiting", "c-1"), operator_activity=False)
    assert read_operator_message(ss) > 0.0, (
        "a turn waiting in line must carry operator pressure without any "
        "SharedState record and without a TTL clock"
    )


def test_operator_pressure_does_not_decay_with_wall_clock():
    """A waiting turn must not evaporate because an episode ran long.

    The mirror's 60s TTL meant a turn still in line reported as no operator
    input at all, and made metadata survival a function of episode duration
    (audit §1.6) — which silently confounds every measurement over the chat path.
    """
    from core.shared_state import SharedState
    from pressure_loop.cognition_queue import enqueue_input
    from pressure_loop.pressure_field import read_operator_message

    ss = SharedState()
    assert enqueue_input(_turn("still waiting", "c-1"), operator_activity=False)
    first = read_operator_message(ss)

    import core.symphony_time as st

    real_now = st.now_iso

    # Two hours later — far beyond any TTL that ever existed.
    monkey = getattr(st, "now_iso")
    try:
        st.now_iso = lambda: "2026-07-31T02:00:00+00:00"  # type: ignore[assignment]
        later = read_operator_message(ss)
    finally:
        st.now_iso = monkey if monkey is not None else real_now  # type: ignore[assignment]

    assert later == first, (
        "operator pressure changed with wall-clock time; the line must hold its "
        "place regardless of how long the previous episode took"
    )


# ── §4.0 first come, first serve, in the order it ARRIVED ────────────────────


def test_arrival_order_survives_variable_latency_before_enqueue():
    """Order is SEND order, not enqueue-completion order.

    Each chat does a ledger write, a Guardian screen and a classifier call with
    a 15s timeout before it reaches the queue, on concurrent threadpool threads
    (audit §1.4). VALUE: the queue orders on an arrival stamp taken before any
    variable-latency work. FAIL-MODE: a slow-classifying earlier message lands
    behind a fast later one.
    """
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    # A arrived first but finished its pre-enqueue work last.
    a = _turn("sent first", "c-a", arrival_seq=1)
    b = _turn("sent second", "c-b", arrival_seq=2)
    c = _turn("sent third", "c-c", arrival_seq=3)

    assert enqueue_input(c, operator_activity=False)
    assert enqueue_input(a, operator_activity=False)
    assert enqueue_input(b, operator_activity=False)

    order = [str(x.metadata.get("message")) for x in peek_inputs()]
    assert order == ["sent first", "sent second", "sent third"], (
        f"the line re-ordered itself around pre-enqueue latency: {order}"
    )


def test_repeated_text_holds_two_distinct_places_in_line():
    """"yes" twice is two turns, not one.

    _dedup_key keyed on message; two distinct cycle_ids collapsed to one key,
    so the line jumped and a turn was answered twice (audit §1.3). VALUE:
    cycle_id is the authoritative identity.
    """
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    assert enqueue_input(_turn("yes", "c-1", arrival_seq=1), operator_activity=False)
    assert enqueue_input(_turn("yes", "c-2", arrival_seq=2), operator_activity=False)

    queued = peek_inputs()
    assert len(queued) == 2, "two separate sends collapsed into one place in line"
    assert [x.metadata.get("cycle_id") for x in queued] == ["c-1", "c-2"]


def test_one_dedup_key_is_authoritative():
    """Pin selection and queue admission must not use different identities.

    Two functions ranked the same fields in opposite order — message-first vs
    cycle_id/system_query_id-first — which is the mechanical root of the
    double-fired episode (audit §1.3). Any cure that leaves both alive silently
    re-breaks it.
    """
    import pressure_loop.cognition_queue as queue_mod
    import pressure_loop.pressure as pressure_mod

    answer = _turn(
        "the answer",
        "c-1",
        ingress_class="answer_to_question",
        system_query_id="q-1",
    )
    box_key = queue_mod._box_dedup_key(answer)

    pin_key_fn = getattr(pressure_mod, "_dedup_key", None)
    if pin_key_fn is None:
        return  # deleted outright — one key by construction
    assert pin_key_fn(answer) == box_key, (
        "pressure and the queue disagree about which turn this is; "
        f"pressure={pin_key_fn(answer)} queue={box_key}"
    )


def test_system_query_answer_ignites_exactly_once():
    """An answer to a system question must run cognition ONCE.

    Deterministic double-fire (audit §1.3): the mirror won the pin, acknowledge
    compared the wrong key, returned False, that boolean was discarded, and the
    head ignited again on the next tick.
    """
    from pressure_loop.cognition_queue import (
        acknowledge_input,
        enqueue_input,
        lease_next_input,
        peek_inputs,
    )

    answer = _turn(
        "it was the second one",
        "c-1",
        ingress_class="answer_to_question",
        system_query_id="q-1",
        arrival_seq=1,
    )
    assert enqueue_input(answer, operator_activity=False)

    leased = lease_next_input(pipeline_id=0)
    assert leased is not None
    assert acknowledge_input(leased, pipeline_id=0) is True, (
        "the turn that opened the episode could not be acknowledged off the "
        "queue — it will ignite a second time on the next tick"
    )
    assert peek_inputs() == (), "the answered turn is still in line"


# ── §4.0 nothing is lost ──────────────────────────────────────────────────────


def test_paused_substrate_holds_the_line(monkeypatch):
    """A paused substrate means the line HOLDS, not that the turn evaporates.

    Chat was rejected after its SourceLedger citation was already written, so
    the operator had to retype (audit §1.10).
    """
    import pressure_loop.operator_chat as oc
    import pressure_loop.cognition_queue as oq

    monkeypatch.setattr(
        "pressure_loop.pressure_loop_pause.is_pressure_loop_paused", lambda: True
    )
    monkeypatch.setattr(
        "auto_loop.promptpack.intent_classifier.classify_chat_intent",
        lambda *a, **k: None,
    )

    result = oc.handle_operator_chat("hold this for me", runtime=None)

    assert result.get("queued") is True, (
        "a paused substrate discarded the operator's turn instead of holding "
        f"its place in line: {result.get('error')}"
    )
    assert len(oq.peek_inputs()) == 1, "the turn never reached the line"


def test_scheduling_lease_does_not_consume_head_before_carrier_opens():
    """A scheduling decision alone cannot remove a turn from durable intake."""
    from pressure_loop.cognition_queue import (
        enqueue_input,
        lease_next_input,
        peek_inputs,
    )

    assert enqueue_input(_turn("do not lose me", "c-1", arrival_seq=1), operator_activity=False)
    leased = lease_next_input(pipeline_id=0)
    assert leased is not None

    # A lease is a scheduling decision only: the turn is still durably in line.
    assert len(peek_inputs()) == 1, (
        "the turn was consumed by a scheduling decision; a crash now loses it"
    )


def test_corrupt_queue_file_is_never_silently_emptied(tmp_path, monkeypatch):
    """A truncated queue file must not read as "nobody is waiting".

    _ensure_loaded set _LOADED=True before the read, so a corrupt file yielded
    an empty in-memory queue that the next enqueue wrote back over the file —
    losing the whole backlog with a telemetry breadcrumb (audit §1.9). §2.1:
    "could not read" and "read, found nothing" must never share a value.
    """
    import pressure_loop.cognition_queue as queue_mod

    path = tmp_path / "operator_input_queue.json"
    path.write_text('[{"source": "operator", "type": "operator_qu', encoding="utf-8")
    monkeypatch.setattr(queue_mod, "_queue_path", lambda: path)
    queue_mod._reset_cache_for_tests()

    with pytest.raises(Exception):
        queue_mod.peek_inputs()


def test_persist_is_atomic(tmp_path, monkeypatch):
    """The queue file is replaced atomically, never truncated in place.

    A crash mid-write must leave the previous backlog intact (audit §1.9).
    """
    import pressure_loop.cognition_queue as queue_mod

    path = tmp_path / "operator_input_queue.json"
    monkeypatch.setattr(queue_mod, "_queue_path", lambda: path)
    queue_mod._reset_cache_for_tests()
    assert queue_mod.enqueue_input(_turn("first", "c-1", arrival_seq=1), operator_activity=False)
    # Cut 7: the durable line for an operator turn is the chat line's file.
    path = queue_mod._queue_path()

    written: list[str] = []
    real_replace = __import__("os").replace

    def _tracking_replace(src, dst):
        written.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", _tracking_replace)
    assert queue_mod.enqueue_input(_turn("second", "c-2", arrival_seq=2), operator_activity=False)

    assert written, (
        "the queue was written in place; a crash mid-write loses the backlog"
    )
    assert json.loads(path.read_text(encoding="utf-8")), "the queue file is unreadable"


# ── §4.0 the gate: an episode is running → the line HOLDS ────────────────────


def test_a_second_lease_is_refused_while_an_episode_owns_the_line():
    """THE GATE. One turn in flight at a time, enforced by the queue itself.

    Today the line holds only because the pressure loop is single-threaded and
    ignite_episode runs the whole cascade inline, so the loop thread physically
    cannot lease again — the queue is an INBOX, not a gate. evaluate's
    `active_episode` branch (pressure.py:932-938), the code that literally
    implements this rule, is unreachable: `active` is read from store.current()
    on the very thread that is blocked (audit §1.1).

    A lease is a CLAIM. While one is outstanding the line holds, whatever the
    threading model does.
    """
    from pressure_loop.cognition_queue import (
        enqueue_input,
        lease_next_input,
    )

    assert enqueue_input(_turn("first", "c-1", arrival_seq=1), operator_activity=False)
    assert enqueue_input(_turn("second", "c-2", arrival_seq=2), operator_activity=False)

    first = lease_next_input(pipeline_id=0)
    assert first is not None
    assert first.metadata.get("cycle_id") == "c-1"

    assert lease_next_input(pipeline_id=0) is None, (
        "a second turn was leased while an episode already owned the line — "
        "the queue is an inbox, not a gate"
    )


def test_acknowledging_admitted_head_exposes_the_next_turn():
    """Carrier admission removes its head; the next turn remains queued.

    Active-episode ownership prevents that next turn from igniting mid-episode.
    The queue itself must expose it as the new durable head.
    """
    from pressure_loop.cognition_queue import (
        acknowledge_input,
        enqueue_input,
        lease_next_input,
        peek_inputs,
    )

    assert enqueue_input(_turn("first", "c-1", arrival_seq=1), operator_activity=False)
    assert enqueue_input(_turn("second", "c-2", arrival_seq=2), operator_activity=False)

    head = lease_next_input(pipeline_id=0)
    assert head is not None
    assert lease_next_input(pipeline_id=0) is None

    assert acknowledge_input(head, pipeline_id=0) is True
    assert len(peek_inputs()) == 1

    nxt = lease_next_input(pipeline_id=0)
    assert nxt is not None, "the gate never reopened for the next in line"
    assert nxt.metadata.get("cycle_id") == "c-2"


def test_failed_scheduling_before_carrier_open_releases_the_lease():
    """Pre-admission failure leaves the durable head available for retry."""
    from pressure_loop.cognition_queue import (
        enqueue_input,
        lease_next_input,
        peek_inputs,
        release_input_lease,
    )

    assert enqueue_input(_turn("do not lose me", "c-1", arrival_seq=1), operator_activity=False)
    head = lease_next_input(pipeline_id=0)
    assert head is not None

    release_input_lease(pipeline_id=0)  # no carrier opened, so acknowledgement is forbidden

    assert len(peek_inputs()) == 1, "failed scheduling ate the turn"
    again = lease_next_input(pipeline_id=0)
    assert again is not None
    assert again.metadata.get("cycle_id") == "c-1"


def test_a_restart_clears_a_stranded_lease(tmp_path, monkeypatch):
    """The line is durable; the claim on it is not.

    A lease that outlived its process would block every future operator turn
    forever — strictly worse than the accidental gate it replaces. The queue
    persists; the lease is in-memory only, so a restart always reopens the gate.
    """
    import pressure_loop.cognition_queue as queue_mod

    monkeypatch.setattr(queue_mod, "_queue_path", lambda: tmp_path / "q.json")
    queue_mod._reset_cache_for_tests()
    assert queue_mod.enqueue_input(_turn("survivor", "c-1", arrival_seq=1), operator_activity=False)
    assert queue_mod.lease_next_input(pipeline_id=0) is not None
    assert queue_mod.lease_next_input(pipeline_id=0) is None

    queue_mod._reset_cache_for_tests()  # process restart

    revived = queue_mod.lease_next_input(pipeline_id=0)
    assert revived is not None, "a stranded lease survived a restart and bricked the line"
    assert revived.metadata.get("cycle_id") == "c-1"
    queue_mod._reset_cache_for_tests()


def test_an_ignition_that_opens_no_episode_reopens_the_line(monkeypatch):
    """A claim must never outlive the attempt that took it.

    `ignite_episode` can return None (cognition disabled, episode open refused).
    That path never reaches the close finally, so without an explicit release
    the claim would strand and block EVERY later operator turn — the gate
    failing shut is worse than the accidental gate it replaces.
    """
    import cognition_chain
    import pressure_loop.loop as loop_mod
    from pressure_loop.cognition_queue import (
        enqueue_input,
        lease_next_input,
        peek_inputs,
    )

    assert enqueue_input(_turn("nobody answered me", "c-1", arrival_seq=1), operator_activity=False)
    monkeypatch.setattr(cognition_chain, "ignite_episode", lambda *_a, **_k: None)
    monkeypatch.setattr(loop_mod, "ignite_episode", lambda *_a, **_k: None, raising=False)

    leased = lease_next_input(pipeline_id=0)
    assert leased is not None
    # Simulate the loop's ignite-returned-None path.
    from pressure_loop.cognition_queue import release_input_lease

    assert release_input_lease(pipeline_id=0) is True

    assert len(peek_inputs()) == 1, "the unanswered turn left the line"
    assert lease_next_input(pipeline_id=0) is not None, (
        "the gate failed SHUT — every later operator turn is blocked"
    )


def test_reading_operator_pressure_never_claims_the_line():
    """Asking "is anyone in line?" must not take their place.

    The pressure reader runs every tick. If it claimed, it would either brick
    the gate or hand ignition a turn nobody released.
    """
    from core.shared_state import SharedState
    from pressure_loop.cognition_queue import (
        enqueue_input,
        lease_next_input,
    )
    from pressure_loop.pressure_field import read_operator_message

    assert enqueue_input(_turn("waiting", "c-1", arrival_seq=1), operator_activity=False)
    ss = SharedState()
    assert read_operator_message(ss) > 0.0
    assert read_operator_message(ss) > 0.0  # idempotent

    assert lease_next_input(pipeline_id=0) is not None, (
        "the pressure read claimed the line, so ignition could not lease it"
    )


def test_only_operator_authored_turns_join_the_operator_line():
    """The operator's line cannot be joined by someone who is not the operator.

    _enqueue admitted on `type` alone with no `source` check, so an Omega- or
    model-authored contributor typed operator_query could enter and be held in
    SRAM as speaker="operator" (audit §1.11). omega_levels.py:133 already has
    this guard.
    """
    from pressure_loop.contributors import make_contributor
    from pressure_loop.cognition_queue import (
        enqueue_input,
        peek_inputs,
    )

    impostor = make_contributor(
        "omega",
        "operator_query",
        1.0,
        {"message": "a sentence the operator never said", "cycle_id": "x-1"},
    )
    assert enqueue_input(impostor, operator_activity=False) is False, (
        "a non-operator contributor joined the operator queue; it would be "
        'persisted as something the operator said (speaker="operator")'
    )
    assert peek_inputs() == ()


def test_screened_external_observation_pays_toll_at_the_same_queue():
    from pressure_loop.contributors import make_contributor
    from pressure_loop.cognition_queue import enqueue_input, peek_inputs

    observation = make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {
            "cycle_id": "sensory-calendar-ev-1",
            "lane": "chat",
            "memory_envelopes": [{"cycle_id": "sensory-calendar-ev-1"}],
        },
    )

    assert enqueue_input(observation, operator_activity=False)
    queued = peek_inputs()[0]
    assert queued.source == observation.source and queued.type == observation.type
    assert all(queued.metadata[key] == value for key, value in observation.metadata.items())


def test_mcp_result_reaches_cognition_only_as_an_external_queue_item(tmp_path):
    """Removing MCP ingress's queue call would leave no cognition stimulus."""
    from mcp_platform.ingress import admit_mcp_result
    from mcp_platform.source_contracts import McpSourceContext
    from pressure_loop.cognition_queue import peek_inputs

    context = McpSourceContext(
        server_id="omega.first_party.google",
        server_version="1.0.0",
        artifact_sha256="a" * 64,
        provider_id="google",
        account_ref="account-ref-1",
        connection_revision="b" * 64,
        tool_name="omega.google.gmail.read",
        tool_schema_sha256="c" * 64,
        call_timestamp="2026-08-11T15:00:01Z",
        omega_action_lineage_id="act_0123456789abcdef",
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )
    result = {
        "result_kind": "observation",
        "provider_object_id": "message-1",
        "provider_container_id": "inbox",
        "provider_revision": "etag-1",
        "provider_change_token": "change-4",
        "event_kind": "current",
        "observed_at": "2026-08-11T15:00:00Z",
        "status": "current",
        "mime_type": "application/json",
        "content": {"subject": "Quarterly plan"},
    }

    receipt = admit_mcp_result(context, result, source_root=tmp_path)

    [queued] = peek_inputs()
    assert (queued.source, queued.type, queued.weight) == (
        receipt.contributor.source,
        receipt.contributor.type,
        receipt.contributor.weight,
    )
    assert {
        key: value
        for key, value in queued.metadata.items()
        if key not in {"arrival_seq", "queue_id"}
    } == receipt.contributor.metadata
    assert queued.metadata["lane"] == "chat"
    assert type(queued.metadata["arrival_seq"]) is int
    assert queued.metadata["arrival_seq"] > 0
    assert "arrival_seq" not in receipt.contributor.metadata
    assert receipt.contributor.metadata["lane"] == "chat"
    assert receipt.contributor.source == "external"
    assert receipt.contributor.type == "sensory_observation"
