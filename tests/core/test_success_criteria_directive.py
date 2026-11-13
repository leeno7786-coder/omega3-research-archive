"""The MIND'S BAR reaches the agent — and the agent's own voice survives a fold-back.

Operator 2026-07-31, after live episode b06b85ac (a WRITE run failed as
``skill_no_evidence`` after 138s, nothing banked):

    "take the deterministic gate out and add to the model's output for the agent
     what success looks like, that makes it a real learning process from
     knowledge it gained, it sets a bar for itself and then tries to reach it"

    "it needs to also carry the agent's last few messages to get it in context
     of exactly what it was doing before the foldback"

    "skills should be gated by read/write and they should become
     modifiers/enhancers not law for a build, they should never affect a build's
     outcome"

Omega ALREADY authors a per-objective bar — ``outcome_contract.acceptance_criteria``
is a model-authored field of the motor program (omega_loop/motor/prompts.py).
It just never reached the agent AS a bar: it rendered as a bare comma-joined
``## Completion criteria`` line, below Allowed tools / Forbidden tools / Safety /
Limits, indistinguishable from a SKILL's boilerplate completion criteria. These
tests pin the three things that were missing.
"""

from __future__ import annotations

from schema.qwen_skill_dispatch import QwenSkillExecutionDispatch, build_directive


def _motor_program(acceptance=("every test in tests/ passes", "README documents the CLI")) -> dict:
    return {
        "program_id": "mp1",
        "parent_program_id": "",
        "outcome_id": "out1",
        "episode_id": "ep1",
        "attempt_number": 1,
        "created_at": "2026-07-31T12:00:00Z",
        "outcome_contract": {
            "objective": "build the todo app",
            "acceptance_criteria": list(acceptance),
            "constraints": ["do not change the objective"],
            "priorities": ["correctness"],
        },
        "pathway": {
            "pathway_id": "derive-implement-verify",
            "revision": 1,
            "name": "derive implement verify",
            "method": "write tests, implement, and verify",
            "operational_rationale": "the result needs executable proof",
            "failed_predecessor_id": "",
            "change_evidence": [],
        },
        "execution_plan": {
            "capability_class": "development",
            "required_capabilities": [],
            "operations": ["run the focused pytest suite"],
            "permitted_local_discretion": ["choose test flags"],
            "prohibited_deviations": ["change the requested outcome"],
            "return_to_mind_conditions": ["work is complete"],
        },
        "evidence_contract": {
            "required_artifacts": ["files_changed"],
            "required_source_proof": [],
            "required_checks": ["tests_run"],
            "completion_signals": ["pytest passes"],
            "disqualifying_failures": ["fabricated evidence"],
        },
    }


def _dispatch(
    *,
    motor_program: dict | None = None,
    completion_criteria: tuple = (),
    execution_kind: str = "WRITE",
    objective: str = "build the todo app",
) -> QwenSkillExecutionDispatch:
    return QwenSkillExecutionDispatch(
        episode_id="ep1",
        dispatch_id="d1",
        skill_name="python-build",
        skill_id="python-build",
        skill_class="development",
        execution_kind=execution_kind,
        objective=objective,
        skill_markdown="# Python build\nUse tests first.",
        allowed_tools=("read_file", "write_file"),
        forbidden_tools=(),
        safety={},
        limits={"max_steps": 20, "max_files_changed": 10},
        evidence_required=(),
        completion_criteria=completion_criteria,
        expected_output_schema={},
        source_action_goal={},
        source_skill_execution_pack={},
        motor_program=dict(motor_program or {}),
    )


# ── 1. the mind's bar reaches the agent, stated as a bar ──────────────────────


def test_mind_authored_acceptance_criteria_are_handed_to_the_agent_as_a_bar():
    """The whole point of retiring the deterministic gate: the agent is TOLD the
    target up front, in the mind's own words, instead of being graded on a
    constant it never saw."""
    text = build_directive(_dispatch(motor_program=_motor_program()))
    assert "SUCCESS CRITERIA" in text.upper()
    assert "every test in tests/ passes" in text
    assert "README documents the CLI" in text


def test_the_bar_leads_the_directive_instead_of_trailing_it():
    """b06b85ac's directive DID carry the criteria — as a comma-joined tail line
    under Limits, after four envelope sections. A 4B anchors on what leads. The
    bar must sit with the Objective, ahead of the tool/safety envelope and the
    skill body."""
    text = build_directive(_dispatch(motor_program=_motor_program()))
    bar = text.upper().index("SUCCESS CRITERIA")
    assert bar < text.index("## Allowed tools")
    assert bar < text.index("## Skill (SKILL.md body)")


def test_the_agent_is_told_to_self_assess_against_the_bar():
    """Shortfall must come back as a REPORT the mind decides on — retry, change
    approach, or stop — so the loop is not a hardcoded failure at the execute
    seam nor an unbounded retry. The agent has to know to say so."""
    text = build_directive(_dispatch(motor_program=_motor_program())).lower()
    assert "fall short" in text or "fell short" in text or "not met" in text
    assert "which criteria" in text or "say which" in text or "state which" in text


def test_the_shortfall_names_the_field_that_actually_reaches_the_mind():
    """Prose alone is advisory. A shortfall only becomes a DECISION if it lands in
    ``unresolved_blockers``, which is load-bearing all the way up:

      agent output -> QwenSkillExecutionResult.unresolved_blockers
                   -> foldback verdict blockers (omega_loop/episode/packet.py:926)
                   -> satisfied=False / more_work_needed=True
                   -> the mind's pressure_resolution (continue|revise|supersede|retire)

    and ``GlobalBSkillUpdate`` structurally forbids intent_satisfied while any
    blocker stands (schema/global_b_skill_update.py:79). Naming the field is what
    turns "tell me if you fell short" into the loop the operator asked for —
    report, then the mind chooses — instead of an unbounded retry."""
    text = build_directive(_dispatch(motor_program=_motor_program()))
    assert "unresolved_blockers" in text


def test_the_marker_the_directive_teaches_is_the_marker_the_bridge_parses():
    """A CONTRACT between two files that must move together.

    ``auto_loop/execute/success_criteria.py`` teaches the agent a line form;
    ``auto_loop/execute/brain_skill_executor.py`` lifts that form out of the
    current brain response into
    ``unresolved_blockers``. If either side drifts, the agent writes its shortfall
    in a shape nothing reads and it dies silently at the bridge — with success
    reported on both sides. That is the exact defect class this whole arc has been
    about, so it gets a test rather than a convention."""
    from auto_loop.execute.brain_skill_executor import _declared_shortfall

    text = build_directive(_dispatch(motor_program=_motor_program()))
    example = next(
        line.strip() for line in text.splitlines() if line.strip().startswith("UNMET:")
    )
    filled = example.replace("<the criterion, copied>", "README documents the CLI").replace(
        "<what actually blocked it>", "ran out of turns"
    )
    assert _declared_shortfall(filled) == ["README documents the CLI - ran out of turns"]


# ── 2. a SKILL's criteria are a modifier, never the bar ───────────────────────


def test_skill_completion_criteria_are_labelled_guidance_not_the_mind_s_bar():
    """Operator: skills "should become modifiers/enhancers not law for a build".
    With no motor program there is no mind-authored bar, so the skill's own
    completion criteria must NOT be dressed up as one."""
    text = build_directive(_dispatch(completion_criteria=("skill says: lint clean",)))
    assert "skill says: lint clean" in text  # still offered as guidance
    upper = text.upper()
    if "SUCCESS CRITERIA" in upper:
        head = upper.index("SUCCESS CRITERIA")
        section = text[head : head + 400]
        assert "skill says: lint clean" not in section


def test_a_write_run_with_no_authored_bar_still_gets_the_thin_default():
    """An agent is never dispatched with no notion of done at all — but the
    default is a prompt-side hint, not a gate."""
    text = build_directive(_dispatch(completion_criteria=())).lower()
    assert "success criteria" in text
    assert "describing a change is not making it" in text


def test_read_only_route_is_not_given_a_write_bar():
    """A read-only dispatch's evidence is findings, not files. The default bar
    must not tell it to write."""
    d = _dispatch(execution_kind="INSPECT")
    object.__setattr__(d, "execution_route", "read_only")
    text = build_directive(d).lower()
    assert "write your changes to disk" not in text


# ── 3. the agent's own narration survives the fold-back ───────────────────────


def test_recent_messages_thread_into_the_resume_context(monkeypatch, tmp_path):
    """The digest says what was DONE (files, tools). ``recent_messages`` says
    what the agent was DOING, in its own voice, when the fold-back cut it off."""
    from memory import episode_sram
    from memory.execution_report import ExecutionReport, hold_execution_report

    # The report's relative paths resolve against the selected workspace.
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: tmp_path)
    episode_sram.open_bucket("ep-resume-narration", pipeline_id=0)
    hold_execution_report(
        ExecutionReport(
        episode_id="ep-resume-narration",
        build_task="build the todo app",
        pass_gate=("the requested todo app works",),
        did=("changed main.py",),
        did_not=("delete command unfinished",),
        succeeded=(),
        failed=(),
        progress_digest="wrote main.py; next: the delete command",
        files_changed=["main.py"],
        recent_messages=[
            "Now adding the delete subcommand to the argparse parser.",
            "The test for delete is failing on an off-by-one in the index.",
        ],
        )
    )
    try:
        text = build_directive(_dispatch())
        assert "off-by-one in the index" in text
        assert "delete subcommand" in text
    finally:
        episode_sram.reset()


def test_resume_without_narration_is_unchanged(monkeypatch, tmp_path):
    """A report without narration still carries its verified progress digest."""
    from memory import episode_sram
    from memory.execution_report import ExecutionReport, hold_execution_report

    # The report's relative paths resolve against the selected workspace.
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: tmp_path)
    episode_sram.open_bucket("ep-resume-no-narration", pipeline_id=0)
    hold_execution_report(
        ExecutionReport(
        episode_id="ep-resume-no-narration",
        build_task="build the todo app",
        pass_gate=("the requested todo app works",),
        did=("changed main.py",),
        did_not=("delete command unfinished",),
        succeeded=(),
        failed=(),
        progress_digest="wrote main.py; next: the delete command",
        files_changed=["main.py"],
        )
    )
    try:
        text = build_directive(_dispatch())
        assert "wrote main.py" in text
        assert "WAS DOING" not in text.upper()
    finally:
        episode_sram.reset()
