"""T6: Global B binds omega_reason mail into the one packet Omega reads.

Cross-lane cognitive integration spec, section 3. These are BOUNDARY tests: they drive
the real mail store, the real ``attach_omega_packet``, the real ``_packet_from_episode``
readback, every real lane pack's ``build``, and the real ``EpisodeStore.close`` — not
stubs of them. Four boundaries are pinned:

1. claim -> packet -> shared rendering: bound mail reaches the ONE packet Omega reads,
   renders in every lane, and never exposes lease internals.
2. cutoff and replay stability: membership is decided at the first binding, so a
   re-entered fold reconciles exactly the inputs it started with.
3. concurrent ownership: two episodes of one thread cannot both bind the same mail.
4. failure before a durable outcome: mail is acknowledged only after the episode's
   journal row is actually archived; a fold with no valid control releases it, and a
   lease that runs out is redelivered rather than lost.

Scope limit, stated rather than tested around: the binding record lives on the carrier's
``global_``, and carriers are not restored across a restart, so replay stability holds
within a process. Across a crash leases expire and mail is claimed afresh; pinning the
bound set across a crash is T8's durable transaction record. Binding and rendering
perform no external effect, so effect deduplication is not T6's to prove.
"""

import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from global_loop import intersections as mail
from global_loop.assemble.run import global_b_input_from_episode
from omega_loop.episode import packet as packet_mod
from omega_loop.episode import prompts as prompts_mod
from omega_loop.episode.packet import (
    acknowledge_omega_mail,
    attach_omega_packet,
    bind_omega_mail,
    release_omega_mail,
)
from omega_loop.episode.run import _packet_from_episode
from organ.promptorgan.packs.meta_omega import agent, browser, chat, core, window

THREAD = "t-parser"


def _episode(*, lane="chat", thread_id=THREAD, episode_id="ep-1"):
    """The shape test_foldback_lane_packet drives through attach_omega_packet."""
    goal = {"episode_id": episode_id, "intent": "BROWSE", "omega_lane": lane}
    pack = {"episode_id": episode_id, "action": "BROWSE", "tool": "browser_agent",
            "source_action_goal": goal, "execution_inputs": {}}
    return SimpleNamespace(
        episode_id=episode_id, thread_id=thread_id, opened_by="operator_query",
        global_={"intent_decision": goal},
        pressure={"contributors": [{"type": "operator_query", "source": "operator",
                                    "metadata": {"message": "build a parser"}}]},
        meta={"execution_action_pack": pack},
        auto={"episode_id": episode_id, "execution_pack": pack,
              "source_action_goal": goal, "outputs": {}})


def _pin(text="use sqlite", *, thread_id=THREAD, **overrides):
    row = dict(thread_id=thread_id, delivery_point="omega_reason",
               purpose="operator_instruction", requirement="required",
               source_actor="operator", source_episode_id="ep-op",
               payload={"text": text})
    row.update(overrides)
    return mail.pin_mail(**row)


def _attach(episode):
    attach_omega_packet(episode, global_b_input_from_episode(episode, {}), {})
    return _packet_from_episode(episode)


def _record(episode):
    return dict(episode.global_.get("omega_mail") or {})


def _for_lane(packet, pack):
    """The packet as that lane's pack receives it.

    OmegaEpisodeInput enforces that a window-lane packet carries a named window_context
    and that no other lane does, so the window pack gets a real window packet rather
    than a chat packet relabelled.
    """
    lane = pack.__name__.rsplit(".", 1)[-1]
    context = {"window_id": "win-1", "episodes": []} if lane == "window" else None
    return replace(packet, omega_lane=lane, window_context=context)


# ── 1. claim -> packet -> shared rendering ────────────────────────────────────

def test_bound_mail_reaches_the_one_packet_omega_reads():
    """The serialized packet is the only one Omega ever sees, so binding must precede it."""
    pinned = _pin()
    episode = _episode()

    restored = _attach(episode)

    assert [row["mail_id"] for row in restored.cross_lane_mail] == [pinned["mail_id"]]
    assert restored.cross_lane_mail[0]["payload"] == {"text": "use sqlite"}


def test_the_packet_never_carries_lease_internals():
    """lease_id, generation and effect_id are host bookkeeping, kept on the carrier."""
    _pin()
    episode = _episode()

    restored = _attach(episode)

    row = restored.cross_lane_mail[0]
    for internal in ("lease_id", "generation", "claimed_by", "lease_until", "effect_id"):
        assert internal not in row
    assert _record(episode)["receipts"][0]["lease_id"], "the carrier keeps the receipt"


@pytest.mark.parametrize("pack", [chat, agent, browser, window], ids=lambda p: p.__name__)
def test_every_lane_renders_the_one_shared_block(pack):
    _pin()
    restored = _attach(_episode())

    _, user = pack.build(_for_lane(restored, pack))

    assert "CROSS-LANE MAIL" in user
    assert "use sqlite" in user


@pytest.mark.parametrize("pack", [chat, agent, browser, window], ids=lambda p: p.__name__)
def test_an_episode_with_no_mail_renders_byte_identically(pack):
    restored = _attach(_episode())
    lane_packet = _for_lane(restored, pack)

    assert restored.cross_lane_mail == ()
    assert core.cross_lane_block(lane_packet) == ""
    assert "CROSS-LANE MAIL" not in pack.build(lane_packet)[1]


# ── 2. cutoff and replay stability ────────────────────────────────────────────

def test_membership_is_decided_once_and_a_reentered_fold_rebinds_the_same_set():
    early = _pin("use sqlite")
    episode = _episode()
    first = bind_omega_mail(episode, lane="chat")
    late = _pin("actually use postgres")

    again = bind_omega_mail(episode, lane="chat")

    assert [row["mail_id"] for row in first] == [early["mail_id"]]
    assert again == first, "a re-entered fold reconciles exactly its original inputs"
    assert _record(episode)["cutoff_seq"] == early["arrival_seq"]
    assert [row["mail_id"] for row in mail.pending_mail()] == [late["mail_id"]]


def test_an_empty_binding_is_decided_once_too():
    """Even 'nothing was waiting' is a membership decision a replay must not revisit."""
    episode = _episode()
    assert bind_omega_mail(episode, lane="chat") == ()
    late = _pin()

    assert bind_omega_mail(episode, lane="chat") == ()
    assert _record(episode)["disposition"] == "none"
    assert [row["mail_id"] for row in mail.pending_mail()] == [late["mail_id"]]


def test_an_episode_with_no_thread_binds_nothing():
    _pin()

    assert bind_omega_mail(_episode(thread_id=""), lane="chat") == ()
    assert len(mail.pending_mail()) == 1


def test_chat_specialist_handoff_does_not_consume_mail_for_the_target_lane():
    pinned = _pin(purpose="lane_evidence", target_lane="agent")
    episode = _episode(lane="chat")
    episode.auto["outputs"]["specialist_handoff"] = {
        "lane": "agent", "handoff_id": "handoff-1",
    }

    restored = _attach(episode)

    assert restored.cross_lane_mail == ()
    assert _record(episode)["bound_mail_ids"] == []
    assert [row["mail_id"] for row in mail.pending_mail()] == [pinned["mail_id"]]


def test_chat_handoff_fold_claims_chat_mail_and_leaves_agent_mail_ready():
    chat_mail = _pin("chat update", purpose="lane_evidence", target_lane="chat")
    agent_mail = _pin("agent instruction", purpose="lane_evidence", target_lane="agent")
    broadcast_mail = _pin("shared browser evidence", purpose="lane_evidence")
    episode = _episode(lane="chat")
    episode.auto["outputs"]["specialist_handoff"] = {
        "lane": "agent", "handoff_id": "handoff-2",
    }

    restored = _attach(episode)

    assert [row["mail_id"] for row in restored.cross_lane_mail] == [chat_mail["mail_id"]]
    assert _record(episode)["bound_mail_ids"] == [chat_mail["mail_id"]]
    assert {row["mail_id"] for row in mail.pending_mail()} == {
        agent_mail["mail_id"], broadcast_mail["mail_id"],
    }


# ── 3. concurrent ownership ───────────────────────────────────────────────────

def test_two_episodes_of_one_thread_cannot_both_bind_the_same_mail():
    pinned = _pin()
    episodes = [_episode(episode_id="ep-a"), _episode(episode_id="ep-b")]
    start = threading.Barrier(2)
    results: dict[str, tuple] = {}

    def fold(episode):
        start.wait()
        results[episode.episode_id] = bind_omega_mail(episode, lane="chat")

    workers = [threading.Thread(target=fold, args=(episode,)) for episode in episodes]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)

    owners = [eid for eid, bound in results.items() if bound]
    assert len(owners) == 1, results
    assert [row["mail_id"] for row in results[owners[0]]] == [pinned["mail_id"]]


# ── 4. failure before a durable outcome ───────────────────────────────────────

def test_a_fold_with_no_valid_control_releases_its_mail_immediately():
    pinned = _pin()
    episode = _episode()
    bind_omega_mail(episode, lane="chat")

    assert release_omega_mail(episode) == 1
    ready = mail.pending_mail()
    assert [(row["mail_id"], row["status"]) for row in ready] == [(pinned["mail_id"], "ready")]
    assert ready[0]["effect_id"] == pinned["effect_id"], "redelivery resolves the same effect"


def test_released_mail_is_never_acknowledged_by_the_failed_episode():
    """A contained fold failure still archives; it must not consume what it never reasoned over."""
    _pin()
    episode = _episode()
    bind_omega_mail(episode, lane="chat")
    release_omega_mail(episode)

    assert acknowledge_omega_mail(episode) == 0
    assert [row["status"] for row in mail.pending_mail()] == ["ready"]


def test_a_lease_that_runs_out_mid_fold_is_redelivered_not_lost(monkeypatch):
    pinned = _pin()
    monkeypatch.setattr(packet_mod, "_OMEGA_MAIL_LEASE_S", -1.0)
    slow = _episode(episode_id="ep-slow")
    bind_omega_mail(slow, lane="chat")

    assert acknowledge_omega_mail(slow) == 0
    assert _record(slow)["disposition"] == "acknowledge_failed"
    monkeypatch.setattr(packet_mod, "_OMEGA_MAIL_LEASE_S", 1800.0)
    redelivered = bind_omega_mail(_episode(episode_id="ep-next"), lane="chat")
    assert [row["mail_id"] for row in redelivered] == [pinned["mail_id"]]


def test_a_corrupt_store_is_recorded_as_unavailable_not_as_empty():
    mail.mail_path().parent.mkdir(parents=True, exist_ok=True)
    mail.mail_path().write_text("{not json", encoding="utf-8")
    mail.reset_cache_for_tests()
    episode = _episode()

    assert bind_omega_mail(episode, lane="chat") == ()
    assert _record(episode)["disposition"] == "unavailable"


def test_required_mail_over_the_budget_is_recorded_as_omitted(monkeypatch):
    """Recorded for prerequisite enforcement — never silently dropped."""
    monkeypatch.setattr(prompts_mod, "CROSS_LANE_MAIL_BLOCK_CHARS", 50)
    big = _pin("z" * 500)
    episode = _episode()

    assert bind_omega_mail(episode, lane="chat") == ()
    assert _record(episode)["omitted_required_mail_ids"] == [big["mail_id"]]
    assert [row["status"] for row in mail.pending_mail()] == ["ready"]


def test_omitted_required_mail_is_surfaced_on_the_packet_omega_reads(monkeypatch):
    """Prerequisite enforcement reads the omission; Omega is not left to invent it."""
    monkeypatch.setattr(prompts_mod, "CROSS_LANE_MAIL_BLOCK_CHARS", 50)
    big = _pin("z" * 500)
    restored = _attach(_episode())

    assert restored.omitted_required_mail_ids == (big["mail_id"],)
    block = core.cross_lane_block(restored)
    assert "omitted" in block.lower()
    assert "ASK_OPERATOR" in block
    assert "never invent" in block.lower()


# ── review 2026-09-16: membership at selection; ownership on re-entry ─────────

def test_the_row_limit_applies_at_selection_so_every_claimed_message_is_in_the_packet():
    """Cutting rows after the claim once acknowledged a message the prompt never showed."""
    from omega_loop.episode.types import CROSS_LANE_MAIL_ROWS

    pinned = [_pin(f"m{index}") for index in range(CROSS_LANE_MAIL_ROWS + 1)]
    episode = _episode()

    restored = _attach(episode)
    record = _record(episode)

    assert len(restored.cross_lane_mail) == CROSS_LANE_MAIL_ROWS
    assert [row["mail_id"] for row in restored.cross_lane_mail] == record["bound_mail_ids"]
    assert len(record["receipts"]) == CROSS_LANE_MAIL_ROWS
    assert record["omitted_required_mail_ids"] == [pinned[-1]["mail_id"]]
    assert acknowledge_omega_mail(episode) == CROSS_LANE_MAIL_ROWS
    assert [row["mail_id"] for row in mail.pending_mail()] == [pinned[-1]["mail_id"]]


def test_an_escaped_block_over_budget_is_left_ready_rather_than_truncated(monkeypatch):
    """Escaping expands content, so the raw payload size is not the prompt size."""
    from guardian.external_ingress import harden
    from omega_loop.episode.types import bounded_mail

    pinned = _pin("&" * 3100 + " FINAL_INSTRUCTION_USE_SQLITE")
    body = json.dumps(list(bounded_mail([pinned])), ensure_ascii=False)
    assert len(harden(body)) > len(body) + 1000, "precondition: escaping expands this payload"
    rendered = prompts_mod.render_cross_lane_block(bounded_mail([pinned]))
    monkeypatch.setattr(prompts_mod, "CROSS_LANE_MAIL_BLOCK_CHARS", len(rendered) - 1)
    episode = _episode()

    assert bind_omega_mail(episode, lane="chat") == ()
    assert _record(episode)["omitted_required_mail_ids"] == [pinned["mail_id"]]
    assert [row["status"] for row in mail.pending_mail()] == ["ready"]


def test_an_accepted_message_is_rendered_in_full_including_its_last_instruction():
    pinned = _pin("&" * 400 + " FINAL_INSTRUCTION_USE_SQLITE")
    restored = _attach(_episode())

    assert [row["mail_id"] for row in restored.cross_lane_mail] == [pinned["mail_id"]]
    block = core.cross_lane_block(restored)
    assert "FINAL_INSTRUCTION_USE_SQLITE" in block
    assert len(block) <= prompts_mod.CROSS_LANE_MAIL_BLOCK_CHARS


def test_a_fold_that_released_its_mail_never_reuses_it_on_reentry():
    """A released; B took the mail; A re-entering must not render B's message."""
    pinned = _pin()
    first = _episode(episode_id="ep-a")
    bind_omega_mail(first, lane="chat")
    release_omega_mail(first)
    second = _episode(episode_id="ep-b")
    assert [row["mail_id"] for row in bind_omega_mail(second, lane="chat")] == [pinned["mail_id"]]

    assert bind_omega_mail(first, lane="chat") == ()
    assert _record(first)["disposition"] == "released"
    assert acknowledge_omega_mail(second) == 1


def test_a_fold_whose_lease_was_taken_drops_its_whole_binding(monkeypatch):
    pinned = _pin()
    monkeypatch.setattr(packet_mod, "_OMEGA_MAIL_LEASE_S", -1.0)
    first = _episode(episode_id="ep-a")
    bind_omega_mail(first, lane="chat")
    monkeypatch.setattr(packet_mod, "_OMEGA_MAIL_LEASE_S", 1800.0)
    second = _episode(episode_id="ep-b")
    assert [row["mail_id"] for row in bind_omega_mail(second, lane="chat")] == [pinned["mail_id"]]

    assert bind_omega_mail(first, lane="chat") == ()
    assert _record(first)["disposition"] == "ownership_lost"
    assert acknowledge_omega_mail(first) == 0
    assert acknowledge_omega_mail(second) == 1


def test_a_reentered_fold_that_still_holds_its_leases_keeps_its_binding():
    _pin()
    episode = _episode()
    first = bind_omega_mail(episode, lane="chat")

    assert bind_omega_mail(episode, lane="chat") == first
    assert _record(episode)["disposition"] == "bound"


# ── review 2026-09-16 (round 2): the PROMPT path, and atomic omissions ─────────

def test_the_prompt_readback_never_renders_mail_the_episode_released():
    """The binder is not what Omega reads — the readback is. Ownership is checked there."""
    pinned = _pin()
    first = _episode(episode_id="ep-a")
    _attach(first)
    release_omega_mail(first)
    second = _episode(episode_id="ep-b")

    restored_second = _attach(second)

    assert [row["mail_id"] for row in restored_second.cross_lane_mail] == [pinned["mail_id"]]
    assert _packet_from_episode(first).cross_lane_mail == ()
    assert "use sqlite" not in core.cross_lane_block(_packet_from_episode(first))


def test_the_prompt_readback_drops_mail_whose_lease_was_taken(monkeypatch):
    pinned = _pin()
    first = _episode(episode_id="ep-a")
    assert [row["mail_id"] for row in _attach(first).cross_lane_mail] == [pinned["mail_id"]]
    # The lease runs out and another episode takes the mail between two readbacks.
    from global_loop import intersections as store

    # Directly through the store, so the carrier still believes it is "bound" — exactly
    # the stale belief the readback must not trust.
    store.release_mail(_record(first)["receipts"], episode_id="ep-a")
    store.claim_mail(delivery_point="omega_reason", lane="chat", thread_id=THREAD,
                     episode_id="ep-b", lease_s=60)
    assert _record(first)["disposition"] == "bound"

    assert _packet_from_episode(first).cross_lane_mail == ()
    assert _record(first)["disposition"] == "ownership_lost"


def test_a_required_omission_survives_another_episode_taking_it_at_once(monkeypatch):
    """Omissions come from the claim's own refusals, never from a later store read."""
    monkeypatch.setattr(prompts_mod, "CROSS_LANE_MAIL_BLOCK_CHARS", 50)
    big = _pin("z" * 500)
    real = mail.claim_mail_detailed

    def claim_then_rival_takes_it(**kwargs):
        result = real(**kwargs)
        real(delivery_point="omega_reason", lane="chat", thread_id=THREAD,
             episode_id="ep-rival", lease_s=60)
        return result

    monkeypatch.setattr(mail, "claim_mail_detailed", claim_then_rival_takes_it)
    episode = _episode()

    assert bind_omega_mail(episode, lane="chat") == ()
    assert _record(episode)["omitted_required_mail_ids"] == [big["mail_id"]]
    assert mail.pending_mail() == [], "the rival holds it now"


def test_ownership_is_rechecked_before_every_retry():
    """The prompt is built once and reused across retries, so each attempt must re-check.

    Drives the real run_omega_for_episode: between its first and second attempt another
    episode takes the mail, and the retry must not send the instruction again.
    """
    from omega_loop.episode.run import EpisodeCognitionFoldError, run_omega_for_episode

    _pin()
    episode = _episode()
    _attach(episode)
    prompts_seen: list[str] = []

    def reason(_system, user):
        prompts_seen.append(user)
        if len(prompts_seen) == 1:
            mail.release_mail(_record(episode)["receipts"], episode_id=episode.episode_id)
            mail.claim_mail(delivery_point="omega_reason", lane="chat", thread_id=THREAD,
                            episode_id="ep-rival", lease_s=60)
        return None  # no output, so the loop retries

    with pytest.raises(EpisodeCognitionFoldError):
        run_omega_for_episode(episode, reason_fn=reason)

    assert len(prompts_seen) == 2
    assert "use sqlite" in prompts_seen[0]
    assert "use sqlite" not in prompts_seen[1]
    assert _record(episode)["disposition"] == "ownership_lost"


# ── the durable-outcome seam on the real EpisodeStore.close ───────────────────

def _open_real_episode():
    from pressure_loop.episode import EpisodeStore

    store = EpisodeStore()
    episode = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn")
    return store, episode


def test_mail_is_acknowledged_only_after_the_journal_row_is_archived():
    store, episode = _open_real_episode()
    _pin(thread_id=episode.thread_id)
    bind_omega_mail(episode, lane="chat")

    store.close(after_archive=acknowledge_omega_mail)

    assert _record(episode)["disposition"] == "acknowledged"
    assert mail.high_water_seq() == 0, "acknowledged mail leaves the store"


def test_a_failed_archive_never_triggers_acknowledgement(monkeypatch):
    """close() tolerates a failed archive; a caller must not read that as a durable outcome."""
    from pressure_loop import episode_journal

    def refuse(*_a, **_k):
        raise OSError("journal unwritable")

    monkeypatch.setattr(episode_journal, "archive_episode", refuse)
    store, episode = _open_real_episode()
    _pin(thread_id=episode.thread_id)
    bind_omega_mail(episode, lane="chat")
    calls: list = []

    closed = store.close(after_archive=lambda ep: calls.append(ep))

    assert closed is episode, "the close itself still completes"
    assert calls == []
    assert mail.high_water_seq() > 0 and mail.pending_mail() == [], "still leased, not acknowledged"


def test_an_after_archive_failure_never_fails_the_close():
    store, episode = _open_real_episode()

    def explode(_ep):
        raise RuntimeError("mail bookkeeping broke")

    closed = store.close(after_archive=explode)

    assert closed is episode
    assert episode.state == "complete"
