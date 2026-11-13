"""Phase 4 cut 9 — agent-lane episodes: the motor-handoff split.

THE live failure this cut fixes: a build task's episode ran motor planning AND
the whole build in-episode on the CHAT lane, so the next chat waited minutes
behind the build. These tests pin both halves of the split and the seams that
make it safe:

* a chat episode that plans a USE_SKILL build enqueues the durable agent-line
  row BEFORE it folds, answers with the dispatch acknowledgment, and never runs
  AUTO itself — while a USE_TOOL plan stays exactly where it was;
* the agent episode obeys the carried program verbatim (no second planner pass)
  with its identity rebound and its CONTRACT byte-identical;
* the operator's origin, verbatim request and completion survive the lane hop;
* the chain guard bounds how many episodes one ask may occupy.

Idioms follow tests/core/test_lane_parallel.py (the ``lines`` fixture pointing
the durable lines at a per-test directory) and tests/core/test_cognition_loop.py
(driving the real cascade functions with the model seams replaced).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cognition_chain as cl
from core.pipelines import CAUSAL_PARENT_KEY, work_kind_for_contributor
from pressure_loop import agent_lane
from pressure_loop.episode import EpisodeStore
from schema.motor_program import MotorProgram

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "motor_programs"
    / "derive_implement_verify.json"
)

_REQUEST = "build me a CSV importer"


@pytest.fixture
def lines(tmp_path, monkeypatch):
    """Point the durable operator lines at a per-test directory (cut 7/8).

    ``_queue_path`` anchors the whole per-lane directory, so redirecting that
    one function isolates chat, agent and meta together.
    """
    import pressure_loop.cognition_queue as queue_mod

    monkeypatch.setattr(queue_mod, "_queue_path", lambda: tmp_path / "operator_input_queue.json")
    queue_mod._reset_cache_for_tests()
    yield queue_mod
    queue_mod._reset_cache_for_tests()


@pytest.fixture
def messages():
    """A clean system-messages store (the completion receipt's rail)."""
    from pressure_loop import system_messages

    system_messages.clear_messages()
    yield system_messages
    system_messages.clear_messages()


def _program_dict(episode_id: str) -> dict:
    """One real, contract-valid MotorProgram bound to ``episode_id``."""
    from pressure_loop.motor_loop_guard import (
        material_pathway_fingerprint,
        material_pathway_signature,
    )

    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    program = MotorProgram.from_dict({**raw, "episode_id": episode_id})
    return {
        **program.to_dict(),
        "fingerprint": program.fingerprint(),
        "material_fingerprint": material_pathway_fingerprint(program),
        "material_signature": material_pathway_signature(program),
        "shadow": False,
    }


def _chat_episode(store: EpisodeStore, *, cycle_id: str = "c-1"):
    episode = store.open(pipeline_id=0, work_kind="operator_turn",
        opened_by="operator_command",
        pressure={
            "opened_by": "operator_command",
            "contributors": [
                {
                    "id": "operator_command",
                    "weight": 1.0,
                    "detail": {"message": _REQUEST, "cycle_id": cycle_id},
                }
            ],
        },
    )
    # WO-04: same as _agent_episode — the admission seam always opens this bucket before any
    # phase runs, and the contract handoff records its acceptance criteria into it.
    from memory import episode_sram

    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    episode.meta = {
        "execution_action_pack": {
            "episode_id": episode.episode_id,
            "action": "USE_SKILL",
            "execution_inputs": {"objective": _REQUEST},
        }
    }
    return episode


def _planned(store: EpisodeStore, *, action: str = "USE_SKILL", cycle_id: str = "c-1"):
    """A chat episode that has just had a program attached (the split seam)."""
    episode = _chat_episode(store, cycle_id=cycle_id)
    episode.meta["execution_action_pack"]["action"] = action
    episode.meta["motor_program"] = _program_dict(episode.episode_id)
    return episode


def _stub_planning(monkeypatch, episode, *, program: dict | None = None):
    """Replace the execution-contract projection and attach seams; return the attach log."""
    attached: list[MotorProgram] = []

    def _execution_contract_for(ep):
        return MotorProgram.from_dict(program or _program_dict(ep.episode_id))

    def _attach(ep, prog):
        attached.append(prog)
        meta = dict(getattr(ep, "meta", None) or {})
        meta["motor_program"] = {**prog.to_dict(), "shadow": False}
        ep.meta = meta
        return meta["motor_program"]

    monkeypatch.setattr(
        "meta_loop.execution_contract.execution_contract_for", _execution_contract_for
    )
    monkeypatch.setattr("omega_loop.motor.run.attach_motor_program", _attach)
    return attached


def _stub_cascade(monkeypatch):
    """Capture the two continuations out of the motor seam."""
    seen: dict[str, list] = {"auto": [], "fold": [], "reply": []}

    monkeypatch.setattr(cl, "_start_auto", lambda *a, **k: seen["auto"].append(True))
    monkeypatch.setattr(
        cl,
        "_start_meta_b_global_b",
        lambda store, ep, cascade_status="completed": seen["fold"].append(cascade_status),
    )
    monkeypatch.setattr(
        cl,
        "_deliver_chat_reply_at_auto",
        lambda ep: seen["reply"].append(
            str(dict(dict(getattr(ep, "auto", None) or {}).get("outputs") or {}).get(
                "response_text"
            ) or "")
        ),
    )
    return seen


_OBJECTIVE = "learn how gradient checks establish implementation correctness"


def _skill_pack(episode_id: str, *, skill_id: str = "builder", **overrides) -> dict:
    """A SkillExecutionPack dict shaped exactly as Meta A leaves one."""
    from schema.skill_execution_pack import build_skill_execution_pack

    decision = SimpleNamespace(
        selected=True,
        skill_id=skill_id,
        skill_name=skill_id.replace("_", " ").title(),
        skill_class="builder",
        execution_kind="build",
        source_path=f"skills/{skill_id}/SKILL.md",
        allowed_tools=("write_file", "run_command"),
        forbidden_tools=("rm",),
        safety_class="command_safe",
        requires_confirmation_for=(),
        limits={"max_steps": 40},
        evidence_required=("diff",),
        completion_criteria=("tests pass",),
        output_schema={"type": "object"},
    )
    for key, value in overrides.items():
        setattr(decision, key, value)
    return build_skill_execution_pack(
        episode_id=episode_id,
        objective=_OBJECTIVE,
        decision=decision,
        source_action_goal={"episode_id": episode_id, "opened_by": "operator_command"},
    ).to_dict()


def _dispatched_pack(parent_id: str, *, skill_id: str = "builder") -> dict:
    """The execution pack the CHAT episode authored and handed to the line."""
    return {
        "episode_id": parent_id,
        "action": "USE_SKILL",
        "source_action_goal": {"episode_id": parent_id, "opened_by": "operator_command"},
        "execution_inputs": {
            "objective": _OBJECTIVE,
            "skill_execution_pack": _skill_pack(parent_id, skill_id=skill_id),
        },
    }


def _envelope(parent_id: str = "chat-ep-1", *, chain_depth: int = 1) -> dict:
    return {
        CAUSAL_PARENT_KEY: parent_id,
        "causal_root_episode_id": parent_id,
        "objective": _OBJECTIVE,
        "original_request": _REQUEST,
        "origin_opened_by": "operator_command",
        "operator_cycle_id": "c-1",
        "motor_program": _program_dict(parent_id),
        "execution_action_pack": _dispatched_pack(parent_id),
        "prior_motor_attempts": [{"program_id": "mp-old", "progress": False}],
        "agent_chain_depth": chain_depth,
        "dispatched_at": "2026-08-10T00:00:00+00:00",
    }


def _agent_episode(store: EpisodeStore, envelope: dict | None = None):
    """The episode the agent line's row opens next tick, state restored."""
    envelope = envelope or _envelope()
    episode = store.open(pipeline_id=2, work_kind="agent_continuation",
        opened_by="agent_motor_dispatch",
        pressure={
            "opened_by": "agent_motor_dispatch",
            "contributors": [
                {
                    "id": "agent_motor_dispatch",
                    "weight": 1.0,
                    "detail": {
                        "message": _REQUEST,
                        "directive_intent": "USE_SKILL",
                        CAUSAL_PARENT_KEY: envelope[CAUSAL_PARENT_KEY],
                        agent_lane.DISPATCH_META_KEY: envelope,
                    },
                }
            ],
        },
    )
    # WO-04: the cascade's admission seam always opens this episode's SRAM bucket before any
    # phase runs, and  records its acceptance criteria into it. These
    # tests call that handoff directly, so they open the same owner rather than a state the
    # cascade cannot produce.
    from memory import episode_sram

    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    agent_lane.restore_dispatch(episode)
    meta = dict(episode.meta or {})
    # What Meta A leaves behind on THIS episode: a pack it re-derived itself.
    meta["execution_action_pack"] = {
        "episode_id": episode.episode_id,
        "action": "USE_SKILL",
        "source_action_goal": {"episode_id": episode.episode_id},
        "execution_inputs": {"objective": _REQUEST},
    }
    episode.meta = meta
    return episode


# ── the chat side ────────────────────────────────────────────────────────────


def test_cognition_has_no_chat_to_agent_acknowledgement_path():
    """A planned HUD action proceeds to AUTO; no handoff can answer with an ACK."""
    import inspect

    source = inspect.getsource(cl._bind_execution_contract)

    assert not hasattr(cl, "_dispatch_to_agent_lane")
    assert "should_hand_off" not in source
    assert "_start_auto(store, episode, cascade_status)" in source


def test_an_operator_chat_build_executes_inline_on_its_existing_lane(lines, monkeypatch):
    """Operator work keeps one HUD-to-AUTO lane and never mints an agent row."""
    store = EpisodeStore()
    episode = _chat_episode(store)
    _stub_planning(monkeypatch, episode)
    seen = _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    assert seen["auto"] == [True]
    assert seen["reply"] == []
    assert seen["fold"] == []
    assert lines.peek_inputs() == ()


def test_a_use_tool_plan_stays_inline(lines, monkeypatch):
    """A bounded tool call is the thing the answer is MADE of — handing it away
    would answer the operator with a promise instead of an answer."""
    store = EpisodeStore()
    episode = _chat_episode(store)
    episode.meta["execution_action_pack"]["action"] = "USE_TOOL"
    _stub_planning(monkeypatch, episode)
    seen = _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    assert seen["auto"] == [True]
    assert lines.peek_inputs() == ()


def test_an_autonomous_build_keeps_its_own_lane(lines, monkeypatch):
    """Only CHAT-lane work moves. A meta-lane practice build is already off the
    conversation's path; hopping it would buy nothing and cost a tick."""
    store = EpisodeStore()
    episode = store.open(pipeline_id=0, work_kind="autonomous", opened_by="execution_gap", pressure={"opened_by": "execution_gap"})
    # WO-04: the admission seam opens this bucket before any phase runs.
    from memory import episode_sram

    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    episode.meta = {
        "execution_action_pack": {
            "episode_id": episode.episode_id,
            "action": "USE_SKILL",
            "execution_inputs": {},
        }
    }
    _stub_planning(monkeypatch, episode)
    seen = _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    assert episode.work_kind != "operator_turn"
    assert seen["auto"] == [True]
    assert lines.peek_inputs() == ()


def test_kill_switch_runs_the_build_inline_exactly_as_before(lines, monkeypatch):
    """OMEGA_AGENT_LANE=0 → _start_auto, byte-for-byte the pre-cut-9 chat path."""
    monkeypatch.setenv("OMEGA_AGENT_LANE", "0")
    store = EpisodeStore()
    episode = _chat_episode(store)
    _stub_planning(monkeypatch, episode)
    seen = _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    assert seen["auto"] == [True]
    assert seen["fold"] == []
    assert lines.peek_inputs() == ()


def test_the_agent_line_dedups_on_the_causal_parent(lines):
    """One episode's planning produces at most one build. A row re-minted from
    the same parent MERGES with the one already waiting; a different episode's
    build is its own row, never folded into someone else's."""
    def row(env):
        return agent_lane.build_contributor(env, contributor_type="agent_motor_dispatch")

    first = _planned(EpisodeStore(), cycle_id="c-1")
    envelope = agent_lane.build_envelope(first, original_request=_REQUEST)

    assert lines.enqueue_input(row(envelope), operator_activity=False) is True
    assert lines.enqueue_input(row(envelope), operator_activity=False) is False  # merged
    assert len(lines.peek_inputs()) == 1

    second = _planned(EpisodeStore(), cycle_id="c-2")
    other = agent_lane.build_envelope(second, original_request="and a JSON exporter")
    assert lines.enqueue_input(row(other), operator_activity=False) is True
    assert len(lines.peek_inputs()) == 2


def test_the_dispatch_routes_to_the_agent_line_and_the_agent_carrier(lines, monkeypatch):
    """Both lane derivations must agree, or the cut-8 tick offers a lane whose
    ignition resolves onto another lane's carrier and holds the turn forever."""
    store = EpisodeStore()
    episode = _planned(store)
    agent_lane.hand_off_motor_program(episode, original_request=_REQUEST)

    row = lines.peek_inputs()[0]
    assert work_kind_for_contributor(row) == "agent_continuation"
    assert "line_lane" not in row.metadata


def test_the_build_and_the_next_chat_ignite_on_their_bound_lanes_in_the_same_tick(lines, monkeypatch):
    """THE acceptance shape (verification ladder step 9), hermetically: with the
    build dispatched to its own line, the operator's very next turn ignites the
    chat lane in the SAME tick that starts the build on the agent lane. Before
    this cut the build WAS the chat episode, so the next turn waited it out."""
    from core.shared_state import SharedState
    from pressure_loop.contributors import from_operator_message
    from pressure_loop.loop import _run_pressure_tick_body

    class _Orch:
        def __init__(self):
            self.shared_state = SharedState()
            self.episode_store = EpisodeStore()

    orch = _Orch()
    planned = _planned(EpisodeStore())
    agent_lane.hand_off_motor_program(planned, original_request=_REQUEST)
    lines.enqueue_input(
        from_operator_message({"message": "meanwhile, what time is it?", "cycle_id": "c-2"})
    , operator_activity=False)
    monkeypatch.setattr("pressure_loop.pressure.gather_contributors", lambda ss, **k: [])

    seen: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "pressure_loop.pipeline_dispatch.dispatch_cascade",
        lambda store, ignition, contributor, **k: seen.append(
            (ignition.pipeline_id, ignition.opened_by)
        ),
    )

    assert _run_pressure_tick_body(orch) is True
    # Chat is P0 and Coding Agent is P2; both dispatch in this pressure tick.
    assert seen == [
        (0, "operator_query"),
        (2, "agent_motor_dispatch"),
    ]


def test_operator_utterances_can_never_reach_the_agent_line(lines):
    """The ruling: the whole conversation rides chat; the agent line is fed only
    by machine-minted work. A mis-sourced row would land on CHAT and delay the
    very conversation this split exists to keep free."""
    from pressure_loop.contributors import make_contributor

    with pytest.raises(ValueError, match="operator_turn_cycle_required"):
        lines.enqueue_input(
            make_contributor("operator", "agent_motor_dispatch", 1.0, {CAUSAL_PARENT_KEY: "e1"})
        , operator_activity=False)
    assert not lines.enqueue_input(make_contributor("omega", "operator_command", 1.0, {}), operator_activity=False)


def test_a_queued_build_is_not_a_waiting_operator(lines, monkeypatch):
    """"Is anyone in line?" means a PERSON. The agent line holds the mind's own
    machine-minted work, so counting it there would raise operator pressure for
    a build nobody is waiting in line for — and stand the background-intake
    gates down for as long as the agent lane stays busy."""
    from core.shared_state import SharedState
    from pressure_loop.pressure_field import read_operator_message

    store = EpisodeStore()
    episode = _planned(store)
    agent_lane.hand_off_motor_program(episode, original_request=_REQUEST)

    assert lines.has_waiting_input() is not None  # the whole surface
    assert lines.has_waiting_input(operator_only=True) is None  # …but no person is waiting
    assert read_operator_message(SharedState()) == 0.0

    from pressure_loop.contributors import from_operator_message

    lines.enqueue_input(
        from_operator_message({"message": "and how is it going?", "cycle_id": "c-2"})
    , operator_activity=False)
    assert lines.has_waiting_input() is not None
    assert read_operator_message(SharedState()) > 0.0


def test_the_program_moves_it_is_not_copied(lines, monkeypatch):
    """THE defect this cut had to fix (live 2026-08-10): every reader that
    finds an attached program on a closed episode reads it as an ATTEMPT this
    episode made. The post-execution foldback validator walks program → pack →
    skill pack → executor RESULT and raised on the result that never exists in
    a dispatching episode ("post-execution foldback result episode_id
    mismatch"), crashing the fold. One honest move settles it for every reader:
    the motor-attempt close, the journal row, and that validator."""
    from omega_loop.episode.packet import refresh_omega_execution_foldback

    store = EpisodeStore()
    episode = _planned(store)
    program_id = episode.meta["motor_program"]["program_id"]
    episode.meta["execution_action_pack"]["execution_inputs"]["motor_program"] = dict(
        episode.meta["motor_program"]
    )
    handoff = agent_lane.hand_off_motor_program(episode, original_request=_REQUEST)

    assert "motor_program" not in episode.meta
    assert "motor_program" not in episode.meta["execution_action_pack"]["execution_inputs"]
    # The identity stays on the record — the journal must still say what this
    # episode authored, only not that it attempted it.
    assert handoff["program_id"] == program_id
    # The line row holds the only copy.
    queued = lines.peek_inputs()[0]
    assert queued.metadata[agent_lane.DISPATCH_META_KEY]["motor_program"]["program_id"] == program_id

    def _never(_episode):
        raise AssertionError("evidence must not be built for a handed-off program")

    monkeypatch.setattr("auto_loop.motor_adapter.execution_evidence_from_fold", _never)

    assert refresh_omega_execution_foldback(episode) is None
    cl._persist_motor_attempt_and_pressure(episode, SimpleNamespace())


def test_the_queued_row_is_never_mutated_after_it_is_durable(lines, tmp_path):
    """Once enqueued the envelope IS the durable row. The handoff pops the
    program out of the LIVE execution pack a moment later, and a shallow envelope
    copy made that the SAME dict — so the file written at enqueue and the row
    still held in memory disagreed about their own payload, and the next write to
    the agent line would have re-serialized the mutated one over it (review
    finding). The program rides at the top level exactly once."""
    store = EpisodeStore()
    episode = _planned(store)
    episode.meta["execution_action_pack"]["execution_inputs"]["motor_program"] = dict(
        episode.meta["motor_program"]
    )
    agent_lane.hand_off_motor_program(episode, original_request=_REQUEST)

    envelope = lines.peek_inputs()[0].metadata[agent_lane.DISPATCH_META_KEY]
    on_disk = json.loads(
        lines._queue_path().read_text(encoding="utf-8")
    )
    durable = on_disk["queue"][0]["metadata"][agent_lane.DISPATCH_META_KEY]

    # Compared through the serializer, because that is the only difference the
    # line itself introduces (frozen tuples land as JSON arrays).
    assert durable == json.loads(json.dumps(envelope))
    assert "motor_program" not in envelope["execution_action_pack"]["execution_inputs"]
    assert "motor_program" not in durable["execution_action_pack"]["execution_inputs"]
    assert envelope["motor_program"]["program_id"]


def test_the_journal_records_a_handoff_not_an_attempt(lines, monkeypatch):
    """Same hazard on the journal side: the per-episode row derives execution
    evidence from any attached program, so a dispatched episode would be
    journaled as a FAILED motor attempt."""
    from pressure_loop import episode_journal

    store = EpisodeStore()
    episode = _planned(store)
    agent_lane.hand_off_motor_program(episode, original_request=_REQUEST)
    episode.auto = {"status": "skipped", "outputs": {}, "telemetry": {"cycle_kind": "skipped"}}

    def _never(_episode):
        raise AssertionError("the journal must not build evidence for a handoff")

    monkeypatch.setattr("auto_loop.motor_adapter.execution_evidence_from_fold", _never)

    body = episode_journal.enriched_episode_from_episode(episode)

    assert body["motor_attempt_assessment"] is None
    assert body["motor_program"] is None
    handoff = body["execution"]["agent_lane_handoff"]
    assert handoff[CAUSAL_PARENT_KEY] == episode.episode_id
    assert handoff["objective"]


# ── the agent side ───────────────────────────────────────────────────────────


def test_the_agent_episode_obeys_the_carried_program_without_replanning(lines, monkeypatch):
    """Verbatim obedience. A second planner pass could author a DIFFERENT
    program for work the operator has already been told is under way."""
    store = EpisodeStore()
    episode = _agent_episode(store)
    attached = _stub_planning(monkeypatch, episode)
    monkeypatch.setattr(
        "meta_loop.execution_contract.execution_contract_for",
        lambda *a, **k: pytest.fail("the agent episode must never re-plan"),
    )
    seen = _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    assert seen["auto"] == [True]  # straight to execution
    assert len(attached) == 1
    bound = attached[0]
    carried = MotorProgram.from_dict(_envelope()["motor_program"])
    # Rebound to THIS episode (every downstream identity assertion requires it)…
    assert bound.episode_id == episode.episode_id
    # …while the CONTRACT itself is byte-identical: the fingerprint is semantic.
    assert bound.fingerprint() == carried.fingerprint()
    assert bound.program_id == carried.program_id
    assert bound.outcome_contract.to_dict() == carried.outcome_contract.to_dict()
    # It never hands its own work on again.
    assert lines.peek_inputs() == ()


# ── risk 9: the DISPATCHED pack wins over the re-derived one ─────────────────


def test_skill_selection_really_can_diverge_between_the_two_episodes():
    """The premise, proved rather than assumed. Selection weights
    capability_strength at 0.10, read from the strength index — which the
    dispatching chat episode's OWN terminus updates before the agent episode
    ever runs Meta A. Same objective, same catalog, moved memory, other skill."""
    from meta_loop.skill.run import select_skill_for_intent

    def _skill(skill_id: str, name: str):
        return SimpleNamespace(
            skill_id=skill_id,
            name=name,
            skill_class="builder",
            domains=("build",),
            execution_kinds=("build",),
            allowed_tools=("write_file",),
            forbidden_tools=(),
            safety_class="write_safe",
            requires_confirmation_for=(),
            limits={},
            evidence_required=(),
            completion_criteria=(),
            output_schema={},
            description="build things",
            selectable=True,
            source_path=f"skills/{skill_id}/SKILL.md",
        )

    catalog = [_skill("alpha_builder", "Alpha Builder"), _skill("beta_builder", "Beta Builder")]
    objective = "build and verify the importer"

    when_planned = select_skill_for_intent(
        objective=objective, catalog=catalog, strengths={"alpha_builder": 0.95}
    )
    # …the chat episode's terminus then credits the other capability.
    when_executed = select_skill_for_intent(
        objective=objective, catalog=catalog, strengths={"beta_builder": 0.95}
    )

    assert when_planned.selected and when_executed.selected
    assert when_planned.skill_id != when_executed.skill_id


def test_the_dispatched_execution_pack_wins_over_the_re_derived_one(lines, monkeypatch):
    """RISK 9. ``build_envelope`` has carried the pack since cut 9 and nothing
    read it, so the agent episode executed whatever Meta A re-selected here and
    now. The program's authority covers the CONTRACT, never the SELECTION — a
    different skill_id is different tools, a different safety envelope and
    different limits for work the operator was told was already under way."""
    store = EpisodeStore()
    episode = _agent_episode(store)
    # Meta A re-derived a DIFFERENT skill on this episode.
    episode.meta["execution_action_pack"]["execution_inputs"]["skill_execution_pack"] = (
        _skill_pack(episode.episode_id, skill_id="scratch_writer")
    )
    _stub_planning(monkeypatch, episode)
    _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    installed = episode.meta["execution_action_pack"]["execution_inputs"][
        "skill_execution_pack"
    ]
    dispatched = _dispatched_pack("chat-ep-1")["execution_inputs"]["skill_execution_pack"]
    assert installed["selected_skill"] == dispatched["selected_skill"]
    assert installed["allowed_tools"] == dispatched["allowed_tools"]
    assert installed["forbidden_tools"] == dispatched["forbidden_tools"]
    assert installed["safety"] == dispatched["safety"]
    assert installed["limits"] == dispatched["limits"]
    # The foldback mirror reads the same selection, never the discarded one.
    assert episode.meta["skill_execution_pack"]["selected_skill"] == dispatched["selected_skill"]
    # …and the divergence is RECORDED, not swallowed.
    record = episode.meta[agent_lane.PACK_REBIND_META_KEY]
    assert record["diverged"] is True
    assert record["carried_skill_id"] == "builder"
    assert record["rederived_skill_id"] == "scratch_writer"


def test_the_restored_pack_is_rebound_so_the_executor_accepts_it(lines, monkeypatch):
    """Identity only. ``auto_loop.intake`` compares FOUR episode identities and
    raises when any names the parent, so a verbatim restore would strand every
    dispatched build at the door — and ``dispatch_id`` must be recomputed
    because the foldback validators compare it against a result THIS episode's
    executor produces."""
    from auto_loop.intake.run import auto_input_from_episode

    store = EpisodeStore()
    episode = _agent_episode(store)
    _stub_planning(monkeypatch, episode)
    _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    pack = episode.meta["execution_action_pack"]
    skill_pack = pack["execution_inputs"]["skill_execution_pack"]
    ep_id = episode.episode_id
    assert pack["episode_id"] == ep_id
    assert pack["source_action_goal"]["episode_id"] == ep_id
    assert skill_pack["episode_id"] == ep_id
    assert skill_pack["source_action_goal"]["episode_id"] == ep_id
    dispatched = _dispatched_pack("chat-ep-1")["execution_inputs"]["skill_execution_pack"]
    assert skill_pack["dispatch_id"] != dispatched["dispatch_id"]
    assert (
        skill_pack["dispatch_id"]
        == _skill_pack(ep_id, skill_id="builder")["dispatch_id"]
    )
    # The origin that decides "is this the operator's work" is carried, not this
    # episode's own machine-minted provenance.
    assert skill_pack["source_action_goal"]["opened_by"] == "operator_command"

    episode.global_ = {"intent_decision": {"episode_id": ep_id, "intent": "USE_SKILL"}}
    assert auto_input_from_episode(episode) is not None


def test_the_program_still_synchronizes_its_contract_onto_the_carried_pack(lines, monkeypatch):
    """Order is the point: the carried pack is installed BEFORE the attach, so
    the program's objective / evidence / completion terms land on the DISPATCHED
    skill pack exactly as they did on the chat episode that authored both."""
    store = EpisodeStore()
    episode = _agent_episode(store)
    # The REAL attach runs here; only the continuation out of the seam is stubbed.
    monkeypatch.setattr(cl, "_start_auto", lambda *a, **k: None)

    cl._bind_execution_contract(store, episode)

    from schema.qwen_skill_dispatch import motor_authority

    program = episode.meta["motor_program"]
    objective, evidence, completion, _ = motor_authority(program)
    skill_pack = episode.meta["execution_action_pack"]["execution_inputs"][
        "skill_execution_pack"
    ]
    assert skill_pack["objective"] == objective
    assert skill_pack["evidence_required"] == list(evidence)
    assert skill_pack["completion_criteria"] == list(completion)
    # The SELECTION underneath is still the dispatched one.
    assert skill_pack["selected_skill"]["skill_id"] == "builder"


def test_an_ordinary_episode_keeps_the_pack_meta_a_derived(lines):
    """The seam is agent-lane only: nothing else has a dispatched pack to obey."""
    store = EpisodeStore()
    episode = _chat_episode(store)
    before = dict(episode.meta["execution_action_pack"])

    assert agent_lane.restore_carried_pack(episode) is None
    assert episode.meta["execution_action_pack"] == before
    assert agent_lane.PACK_REBIND_META_KEY not in episode.meta


def test_a_dispatch_minted_before_this_reader_existed_still_runs(lines, monkeypatch):
    """Envelopes already on the durable line carry no pack (or an empty one).
    They must fall through to the re-derived pack rather than raise — the row
    predates the field and the build is still owed."""
    envelope = _envelope()
    envelope.pop("execution_action_pack")
    store = EpisodeStore()
    episode = _agent_episode(store, envelope)
    _stub_planning(monkeypatch, episode)
    seen = _stub_cascade(monkeypatch)

    assert agent_lane.restore_carried_pack(episode) is None
    cl._bind_execution_contract(store, episode)

    assert seen["auto"] == [True]
    assert episode.meta["execution_action_pack"]["execution_inputs"]["objective"] == _REQUEST


def test_restore_carries_the_motor_loop_history_across_the_hop(lines):
    """The guard's attempt history lands under the key the planner and the
    journal already read, so the hop does not reset the loop detector."""
    store = EpisodeStore()
    episode = _agent_episode(store)

    assert episode.meta["prior_motor_attempts"] == [{"program_id": "mp-old", "progress": False}]
    assert agent_lane.carried_dispatch(episode)["objective"]


def test_the_operator_origin_survives_the_lane_hop(lines):
    """Two deterministic readers ask "is this the operator's work": the
    workspace port (operator directory vs _practice) and the build notices.
    Both would silently demote an agent-lane build without the carried origin."""
    store = EpisodeStore()
    episode = _agent_episode(store)

    assert episode.opened_by == "agent_motor_dispatch"
    assert cl._episode_origin_opened_by(episode) == "operator_command"
    assert cl._operator_request_text(episode) == _REQUEST


def test_an_ordinary_episode_keeps_its_own_provenance(lines):
    store = EpisodeStore()
    episode = _chat_episode(store)

    assert cl._episode_origin_opened_by(episode) == "operator_command"
    assert cl._operator_request_text(episode) == _REQUEST
    assert agent_lane.carried_dispatch(episode) is None
    assert agent_lane.carried_program(episode) is None


def test_the_agent_episode_authorizes_as_the_operators_own_work(lines):
    """``skill_dispatch_confirmed`` asks the skill pack's source_action_goal "was
    this authorized by the operator". With OMEGA_AUTONOMOUS_SKILL_EXEC off, a raw
    provenance read would make every operator build unauthorized the moment it
    left the chat lane."""
    from auto_loop.skill.run import skill_dispatch_confirmed
    from meta_loop.skill.run import _skill_source_action_goal

    sag = _skill_source_action_goal(_agent_episode(EpisodeStore()), {})
    assert sag["opened_by"] == "operator_command"
    assert skill_dispatch_confirmed(sag) is True

    autonomous = EpisodeStore().open(pipeline_id=0, work_kind="autonomous",
        opened_by="execution_gap", pressure={"opened_by": "execution_gap"}
    )
    assert _skill_source_action_goal(autonomous, {})["opened_by"] == "execution_gap"


# ── the chain ────────────────────────────────────────────────────────────────


def _continue_result(intent: str = "APPLY", reason: str = "the suite is still red"):
    return SimpleNamespace(
        intent_decision={"intent": intent, "reason": reason},
        build_accepted=False,
    )


def test_an_unfinished_build_continues_on_its_own_lane(lines):
    """The mind decided to continue (its own intent_decision); this cut only
    decides WHICH LANE that continuation rides. Before it, the continuation
    re-entered through generic pressure on the META lane — the mind's own
    stream — which is exactly the cross-traffic Phase 4 ends."""
    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.meta["motor_program"] = _program_dict(episode.episode_id)

    record = agent_lane.maybe_mint_continuation(
        episode, _continue_result(), in_episode_passes_exhausted=True
    )

    assert record is not None
    assert record["agent_chain_depth"] == 2
    assert record["causal_root_episode_id"] == "chat-ep-1"
    queued = lines.peek_inputs()
    assert [c.type for c in queued] == ["agent_continuation"]
    envelope = queued[0].metadata[agent_lane.DISPATCH_META_KEY]
    assert envelope["origin_opened_by"] == "operator_command"  # still the operator's
    assert envelope["causal_root_episode_id"] == "chat-ep-1"
    assert work_kind_for_contributor(queued[0]) == "agent_continuation"


def test_the_motor_loop_guard_history_spans_the_episode_chain(lines):
    """The guard's limits must count attempts across the CHAIN, not restart at
    every hop. The continuation carries the history BY VALUE, so it has to carry
    the list the close produced — the attempt this episode just made included.
    Reading the planner's pre-close ``prior_motor_attempts`` instead meant every
    pass re-arrived with the same history, the equivalent/distinct counters
    never advanced, and only MAX_CHAIN_PASSES stood between a build repeating
    the same failed pathway and the operator."""
    from pressure_loop.motor_loop_guard import ATTEMPTS_THROUGH_CLOSE_KEY

    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.meta["motor_program"] = _program_dict(episode.episode_id)
    # What the motor-attempt close wrote when it graded this episode's attempt.
    closed = [
        {"program_id": "mp-old", "progress": False},
        {"program_id": "mp-this", "program_fingerprint": "f-this", "loop_action": "invalidate"},
    ]
    episode.meta[ATTEMPTS_THROUGH_CLOSE_KEY] = closed

    agent_lane.maybe_mint_continuation(
        episode, _continue_result(), in_episode_passes_exhausted=True
    )

    envelope = lines.peek_inputs()[0].metadata[agent_lane.DISPATCH_META_KEY]
    assert envelope["prior_motor_attempts"] == closed
    # …and the next episode restores it under the key the guard already reads.
    successor = _agent_episode(EpisodeStore(), envelope)
    assert successor.meta["prior_motor_attempts"] == closed


def test_the_attempt_close_records_the_history_through_this_episode():
    """The other half of the same seam: the close is what produces that list."""
    from pressure_loop.motor_loop_guard import (
        ATTEMPTS_THROUGH_CLOSE_KEY,
        material_pathway_fingerprint,
    )
    from schema.execution_evidence import ExecutionEvidence

    program = MotorProgram.from_dict(_program_dict("ep-agent-1"))
    evidence = ExecutionEvidence.from_dict(
        {
            "program_id": program.program_id,
            "episode_id": "ep-agent-1",
            "status": "failure",
            "failure_kind": "verification_failure",
            "failure_reason": "verification_failure",
        }
    )
    prior = [{"program_id": "mp-old", "program_fingerprint": "old", "verified_progress": False}]
    episode = SimpleNamespace(episode_id="ep-agent-1", meta={})

    cl._apply_motor_loop_guard(
        program,
        evidence,
        prior,
        {"proposed_action": {"required_capabilities": ["shell"]}},
        episode_id="ep-agent-1",
        episode=episode,
    )

    through = episode.meta[ATTEMPTS_THROUGH_CLOSE_KEY]
    assert [row.get("program_fingerprint") for row in through] == [
        "old",
        program.fingerprint(),
    ]
    assert through[-1]["material_fingerprint"] == material_pathway_fingerprint(program)
    # The planner's own key is UNTOUCHED: the journal reads it as the attempts
    # that PRECEDED this one to form its RPE expectation.
    assert "prior_motor_attempts" not in episode.meta


def test_the_chain_is_uncapped_and_ends_when_omega_accepts(lines):
    """REVERSAL 2026-08-15: no pass cap, in the episode or across the chain.

    The old guard existed for a real reason — the in-episode cap reset on every
    hop, so a build that yielded every time could hop lanes forever. Removing the
    in-episode cap removes what it was guarding: a build that is still going
    stays in the episode it started in, folding AUTO -> Meta B -> Global B ->
    Omega -> AUTO for as long as Omega keeps returning APPLY.

    Operator: "3 build commands before a foldback is ridiculous, the model will
    never get anything done before it folds back". The terminator is Omega's
    judgement — ``build_accepted`` — not a number, which is what makes this an
    agentic loop rather than a stutter.

    Depth is still RECORDED (an operator watching a build across a restart wants
    to know it is on pass four), it just no longer gates.
    """
    store = EpisodeStore()
    deep = _agent_episode(store, _envelope(chain_depth=12))
    deep.meta["motor_program"] = _program_dict(deep.episode_id)

    minted = agent_lane.maybe_mint_continuation(
        deep, _continue_result(), in_episode_passes_exhausted=True
    )
    assert minted is not None, "depth alone must never stop a build Omega wants continued"
    assert int(minted.get("agent_chain_depth") or 0) == 13

    # And the thing that DOES stop it: Omega accepting the build.
    store_done = EpisodeStore()
    done = _agent_episode(store_done, _envelope(chain_depth=12))
    done.meta["motor_program"] = _program_dict(done.episode_id)
    accepted = _continue_result()
    accepted.build_accepted = True
    assert (
        agent_lane.maybe_mint_continuation(
            done, accepted, in_episode_passes_exhausted=True
        )
        is None
    )


@pytest.mark.parametrize(
    "result, exhausted",
    [
        (_continue_result(intent="IDLE"), True),  # the mind chose to stop
        (_continue_result(reason=""), True),  # a continuation with no reason is not one
        (_continue_result(), False),  # the cheaper in-episode pass owns this
    ],
)
def test_a_continuation_needs_the_minds_own_decision(lines, result, exhausted):
    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.meta["motor_program"] = _program_dict(episode.episode_id)

    assert (
        agent_lane.maybe_mint_continuation(
            episode, result, in_episode_passes_exhausted=exhausted
        )
        is None
    )
    assert lines.peek_inputs() == ()


def test_a_shadow_plan_carries_nothing_forward(lines):
    """Shadow plans are observability only. A continuation without a canonical
    program would be a promise with no program behind it — and building the
    envelope anyway would raise inside the fail-hard cascade."""
    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.meta["motor_program"] = {**_program_dict(episode.episode_id), "shadow": True}

    assert (
        agent_lane.maybe_mint_continuation(
            episode, _continue_result(), in_episode_passes_exhausted=True
        )
        is None
    )
    assert lines.peek_inputs() == ()


def test_an_ordinary_episode_never_mints_a_continuation(lines):
    store = EpisodeStore()
    episode = _planned(store)

    assert (
        agent_lane.maybe_mint_continuation(
            episode, _continue_result(), in_episode_passes_exhausted=True
        )
        is None
    )
    assert lines.peek_inputs() == ()


# ── the operator hears about it ──────────────────────────────────────────────


def test_the_completion_reaches_the_operator_exactly_once(lines, messages):
    """The chat bubble that asked was resolved seconds after the ask, so the
    ending needs a durable, badge-counted record of its own."""
    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.global_ = {"skill_outcome": {"objective_complete": True}}

    record = agent_lane.announce_completion(episode)

    assert record is not None
    assert record["kind"] == agent_lane.COMPLETION_KIND
    assert "Finished on the agent lane" in record["text"]
    assert record["opened_by"] == "operator_command"  # the build's ORIGIN
    assert record["meta"]["causal_root_episode_id"] == "chat-ep-1"
    assert record["meta"]["original_request"] == _REQUEST

    # Idempotent by causal ROOT: one ask, one ending, however many agent
    # episodes it took.
    second = _agent_episode(EpisodeStore())
    assert agent_lane.announce_completion(second) is None
    assert len([m for m in messages.list_messages() if m.get("kind") == agent_lane.COMPLETION_KIND]) == 1


def test_an_unfinished_ending_says_so(lines, messages):
    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.global_ = {
        "skill_outcome": {"objective_complete": False, "unresolved_blockers": ["pytest red"]}
    }

    record = agent_lane.announce_completion(episode)

    assert record is not None
    assert "Stopped short" in record["text"] and "pytest red" in record["text"]


def test_the_receipt_is_an_announcement_not_an_open_question(lines, messages, monkeypatch):
    """It lands in the panel and the badge — but it must NOT open a row in the
    open-questions journal (review finding). That journal enforces ONE open
    operator question at a time: ``pressure_field._selected_evidence_terminal``
    bypasses EVERY ask terminal while any row is open, and each Omega packet
    renders the open rows back to the mind. A receipt nobody has a reason to
    answer stays open forever, so one finished build would have silenced every
    question the mind ever wanted to ask the operator."""
    from pressure_loop import open_questions

    opened: list = []
    monkeypatch.setattr(
        open_questions, "ensure_open_question", lambda **k: opened.append(k) or "q-1"
    )
    asks: list = []
    monkeypatch.setattr(
        "pressure_loop.proactive_lifecycle.record_ask",
        lambda **k: asks.append(k),
    )

    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.global_ = {"skill_outcome": {"objective_complete": True}}
    record = agent_lane.announce_completion(episode)

    assert record is not None and record["status"] == "pending"  # panel + badge
    assert opened == []  # …but the ASK channel stays free
    assert asks == []
    assert "open_question_id" not in record
    assert "proactive_thread_id" not in record
    assert record["expects_answer"] is False  # durable, so a restart reads it too


def test_the_receipt_defers_to_the_build_lifecycle_notice(lines, messages):
    """The deterministic build notice is the richer message and wins; the
    receipt exists for the paths that notice leaves silent."""
    store = EpisodeStore()
    episode = _agent_episode(store)
    cl._mark_operator_notice_posted(episode)

    assert agent_lane.announce_completion(episode) is None
    assert messages.list_messages() == []


def test_a_continued_episode_is_not_finished(lines, messages):
    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.meta[agent_lane.CONTINUATION_META_KEY] = {"agent_chain_depth": 2}

    assert agent_lane.announce_completion(episode) is None
    assert messages.list_messages() == []


def test_an_ordinary_episode_files_no_receipt(lines, messages):
    store = EpisodeStore()
    episode = _chat_episode(store)

    assert agent_lane.announce_completion(episode) is None
    assert messages.list_messages() == []


# ── the switch is a drain valve, not a trap door (risk 5, ruling 2026-08-11) ──


def test_kill_switch_mints_no_new_agent_work(lines, messages, monkeypatch):
    """What the switch DOES turn off: every seam that puts a new row on the line."""
    monkeypatch.setenv("OMEGA_AGENT_LANE", "0")
    store = EpisodeStore()
    episode = _planned(store)

    assert agent_lane.agent_lane_enabled() is False
    assert agent_lane.should_hand_off(episode) is False
    assert lines.peek_inputs() == ()


def test_a_row_already_on_the_line_still_executes_its_carried_program(lines, monkeypatch):
    """RISK 5. The switch cannot un-enqueue a durable row: the tick still leases
    it and still opens an agent episode (core.lanes is not gated on this switch,
    and must not be). Before this fix every READER was gated, so that episode
    re-planned a DIFFERENT program for work the operator was already told was
    under way. Now it obeys the carried one, exactly as with the switch on."""
    monkeypatch.setenv("OMEGA_AGENT_LANE", "0")
    store = EpisodeStore()
    episode = _agent_episode(store)

    # The envelope was lifted onto the episode despite the switch.
    assert agent_lane.carried_dispatch(episode) is not None
    assert episode.meta["prior_motor_attempts"] == [{"program_id": "mp-old", "progress": False}]

    attached = _stub_planning(monkeypatch, episode)
    monkeypatch.setattr(
        "meta_loop.execution_contract.execution_contract_for",
        lambda *a, **k: pytest.fail("a dispatched episode must never re-plan, switch or not"),
    )
    seen = _stub_cascade(monkeypatch)

    cl._bind_execution_contract(store, episode)

    assert seen["auto"] == [True]
    carried = MotorProgram.from_dict(_envelope()["motor_program"])
    assert attached[0].fingerprint() == carried.fingerprint()
    assert attached[0].episode_id == episode.episode_id
    # And it hands nothing on: the switch still refuses to mint.
    assert lines.peek_inputs() == ()


def test_a_row_already_on_the_line_still_files_its_receipt(lines, messages, monkeypatch):
    """The receipt is the ONLY thing that closes the loop on a build whose chat
    bubble was resolved seconds after the ask. Gating it meant the build ran and
    its ending reached nobody."""
    monkeypatch.setenv("OMEGA_AGENT_LANE", "0")
    store = EpisodeStore()
    episode = _agent_episode(store)

    record = agent_lane.announce_completion(episode)

    assert record is not None
    assert record["kind"] == agent_lane.COMPLETION_KIND
    assert record["meta"]["causal_root_episode_id"] == "chat-ep-1"
    # The ORIGIN survives too — the workspace port files this as the operator's
    # work rather than demoting it to autonomous practice.
    assert agent_lane.carried_origin_opened_by(episode) == "operator_command"
    assert cl._episode_origin_opened_by(episode) == "operator_command"
    assert len(messages.list_messages()) == 1


def test_kill_switch_cuts_the_chain_loudly_and_the_operator_hears_why(
    lines, messages, monkeypatch
):
    """A continuation MINTS a new row, so the switch refuses it — but never in
    silence: a chain cut short and a build that finished must not look alike."""
    monkeypatch.setenv("OMEGA_AGENT_LANE", "0")
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        agent_lane, "_emit", lambda event, **fields: emitted.append((event, fields))
    )
    store = EpisodeStore()
    episode = _agent_episode(store)
    episode.meta["motor_program"] = _program_dict(episode.episode_id)

    assert (
        agent_lane.maybe_mint_continuation(
            episode, _continue_result(), in_episode_passes_exhausted=True
        )
        is None
    )
    assert lines.peek_inputs() == ()
    skips = [f for event, f in emitted if event == "agent_lane_continuation"]
    assert [f["status"] for f in skips] == ["skip"]
    assert "OMEGA_AGENT_LANE=0" in skips[0]["summary"]

    # No continuation was recorded, so the receipt fires and says it stopped short.
    record = agent_lane.announce_completion(episode, cascade_status="failed")
    assert record is not None
    assert record["text"].startswith("Stopped short of finishing")


# ── resuming a build: the dev lobe is what "where it paused" lives in ─────────


def _dev_candidate(memory_id: str, title: str, text: str):
    return SimpleNamespace(memory_id=memory_id, title=title, text=text, coord=[11, 1, 0])


def test_a_resuming_build_recalls_its_own_dev_history(monkeypatch):
    """The long-horizon seam. Every agent run already WRITES the dev lobe
    (objectives, artifacts, execution reports at coord [11, *, *]); nothing read
    them back, so each episode rediscovered the same directories."""
    captured = {}

    def _read(spec):
        captured["spec"] = spec
        return SimpleNamespace(
            candidates=[
                _dev_candidate("m-1", "parser build", "touched parser/lexer.py; round trip red"),
                _dev_candidate("m-2", "parser build", "added parser/tests/test_round_trip.py"),
            ]
        )

    monkeypatch.setattr("memory.api.read_memory", _read)
    store = EpisodeStore()
    episode = _agent_episode(store)

    context = agent_lane.recall_dev_context(episode)

    assert context is not None
    assert context["recalled"] == 2
    assert "lexer.py" in context["items"][0]["text"]
    # It survives on the episode for the executor's prompt.
    assert agent_lane.carried_resume_context(episode) == context


def test_the_resume_recall_asks_for_the_dev_lobe_explicitly(monkeypatch):
    """THE reason this was invisible: QuerySpec.denied_lobes defaults to (6, 11),
    so ordinary recall never returns dev memory. A resuming build has to name it,
    and must not widen the default for everyone else."""
    captured = {}

    def _read(spec):
        captured["spec"] = spec
        return SimpleNamespace(candidates=[])

    monkeypatch.setattr("memory.api.read_memory", _read)
    store = EpisodeStore()
    agent_lane.recall_dev_context(_agent_episode(store))

    spec = captured["spec"]
    assert spec.allowed_lobes == [11], "the dev lobe, and only the dev lobe"
    assert spec.denied_lobes == [], "the default deny would drop every result"
    assert spec.text == _OBJECTIVE, "the build's own objective is the query"
    assert spec.limit == agent_lane.RESUME_RECALL_LIMIT


def test_a_first_run_carries_no_context_and_does_not_pretend_to(monkeypatch):
    monkeypatch.setattr(
        "memory.api.read_memory", lambda spec: SimpleNamespace(candidates=[])
    )
    store = EpisodeStore()
    episode = _agent_episode(store)

    assert agent_lane.recall_dev_context(episode) is None
    assert agent_lane.carried_resume_context(episode) is None


def test_a_failed_recall_stops_the_build_with_the_original_cause(monkeypatch):
    """Unreadable history is a failed component, not an empty first run."""

    failure = RuntimeError("memory substrate down")

    def _boom(spec):
        raise failure

    monkeypatch.setattr("memory.api.read_memory", _boom)
    store = EpisodeStore()
    episode = _agent_episode(store)

    with pytest.raises(RuntimeError) as caught:
        agent_lane.recall_dev_context(episode)
    assert caught.value is failure
    assert agent_lane.carried_resume_context(episode) is None


def test_an_episode_carrying_no_dispatch_recalls_nothing(monkeypatch):
    """Only a build resumes. An ordinary episode has no work to look up."""
    called = []
    monkeypatch.setattr(
        "memory.api.read_memory",
        lambda spec: called.append(spec) or SimpleNamespace(candidates=[]),
    )
    store = EpisodeStore()
    plain = store.open(pipeline_id=0, work_kind="operator_turn", opened_by="operator_query", pressure={"aggregate": 1.0})

    assert agent_lane.recall_dev_context(plain) is None
    assert called == []
