"""WO-05 Step 3 — one attempt, one token; and no-effect is not unknown-effect.

The order: *"Capture each real tool attempt under a new operation token and its result under
the matching token. Preserve error kind/cause, budget exhaustion, cancellation, permission
change, retry and uncertainty. No-effect, partial-effect and unknown-effect are not
interchangeable."*

Acceptance case "Retry and recovery": *a failed tool attempt followed by a successful new
attempt retains both occurrences.*

Two things are missing today, both in `auto_loop/execute/brain_skill_executor.run_operator_tool`
— the single dispatch point for every operator tool:

1. **No attempt identity.** Nothing distinguishes the first attempt from the retry, so two
   real executions cannot both be retained. This is the same lesson WO-04A closure r3 landed
   for phase failures: occurrence identity must come from the invocation, never from the text,
   because two genuine attempts can fail identically.

2. **Effect certainty is collapsed.** Every refusal path returns `ToolResult(False, error=...)`,
   so a permission denial — where the tool provably never ran — reads exactly like a handler
   that raised half way through writing a file. The first is `no_effect`; the second is
   `unknown_effect` and must never be reported as the first, because a reader deciding whether
   to retry needs to know whether the world already moved.
"""

from __future__ import annotations

import pytest



def _auto_evidence():
    """The module under construction.

    NOT ``pytest.importorskip``: a skipped test is not a failing test, and law section 8
    requires watching this fail for the behavioural reason before the code exists. So the
    absence is an assertion with a message, and every test below goes red until it is real.
    """
    try:
        from memory import auto_evidence
    except ImportError as exc:  # pragma: no cover - the RED path
        pytest.fail(
            "memory/auto_evidence.py does not exist, so no tool attempt can be captured "
            f"under its own token and no effect certainty can be recorded ({exc})"
        )
    return auto_evidence


def test_an_attempt_token_is_fresh_per_attempt_and_shared_with_its_result():
    """Correlation without collision: same attempt, one token; new attempt, new token."""
    first = _auto_evidence().new_attempt_token()
    second = _auto_evidence().new_attempt_token()

    assert first and second
    assert first != second, (
        "two attempts share one token, so a retry would replay over the first attempt's "
        "occurrence instead of being retained beside it"
    )


@pytest.mark.parametrize(
    "error_kind,expected_effect",
    [
        # Every one of these is a real branch of run_operator_tool that returns BEFORE the
        # handler is reached, so the tool provably did not run.
        ("arguments_unparseable", "no_effect"),
        ("unknown_tool", "no_effect"),
        ("not_in_lane", "no_effect"),
        ("workspace_unavailable", "no_effect"),
        ("command_refused", "no_effect"),
        ("permission_required", "no_effect"),
        ("execution_ceiling", "no_effect"),
    ],
)
def test_a_refusal_before_the_handler_is_no_effect(error_kind, expected_effect):
    assert _auto_evidence().effect_for_error_kind(error_kind) == expected_effect, (
        f"{error_kind} is refused before the handler runs, so the world did not move; "
        "reporting anything but no_effect would make a safe retry look risky"
    )


def test_a_handler_that_raised_is_unknown_effect_not_no_effect():
    """The asymmetry that matters.

    A handler that raised may have written a file, sent a request or half-applied a patch
    before it failed. Nothing here knows which, and guessing in either direction is a lie:
    claiming no_effect invites a double-apply on retry, and claiming applied invents a result.
    """
    effect = _auto_evidence().effect_for_error_kind("handler_raised")

    assert effect == "unknown_effect", (
        f"a crashed handler reported {effect!r}; a reader cannot tell whether retrying would "
        "repeat work that already landed"
    )
    assert effect != _auto_evidence().effect_for_error_kind("permission_required"), (
        "a handler crash and a permission denial report the same effect, so 'did anything "
        "happen?' has no answer — the exact collapse this step exists to prevent"
    )


def test_the_effect_vocabulary_keeps_its_three_uncertain_states_distinct():
    vocabulary = set(_auto_evidence().EFFECT_STATES)

    assert {"no_effect", "partial_effect", "unknown_effect"} <= vocabulary, (
        f"the effect vocabulary {sorted(vocabulary)} cannot express all three uncertainty "
        "states the order requires to stay distinguishable"
    )
    assert "applied" in vocabulary, "a successful effect needs its own state too"


def test_every_refusal_branch_of_the_real_dispatcher_has_an_error_kind():
    """The vocabulary must cover the dispatcher that exists, not an idealised one.

    Read off `run_operator_tool`'s actual early returns. If a branch is added there without a
    kind here, the capture would fall back to a generic label and the cause of the refusal
    would be lost at the boundary.
    """
    kinds = set(_auto_evidence().TOOL_ERROR_KINDS)

    for required in (
        "arguments_unparseable",
        "unknown_tool",
        "not_in_lane",
        "workspace_unavailable",
        "command_refused",
        "permission_required",
        "execution_ceiling",
        "budget_exhausted",
        "handler_raised",
    ):
        assert required in kinds, f"{required} is a real refusal path with no declared kind"


def test_an_unknown_error_kind_fails_rather_than_defaulting_to_no_effect():
    """Law section 2.1 at this boundary.

    An unrecognised kind must not quietly become the safest-sounding answer. If a new refusal
    branch appears and nobody maps it, the honest outcome is a loud failure, not a confident
    'nothing happened' that could be wrong.
    """
    with pytest.raises(Exception) as caught:
        _auto_evidence().effect_for_error_kind("some_branch_added_later")

    assert "some_branch_added_later" in str(caught.value), (
        "the failure must name the unmapped kind so the gap is fixable"
    )


# ── the wiring: the real dispatcher must produce these events ────────────────


def _ctx(tmp_path, **fields):
    from auto_loop.execute.brain_skill_executor import ExecutionContext

    return ExecutionContext(tmp_path, "build a basic todo app", 30.0, **fields)


def _events(episode_id, kind=None):
    from memory.episode_events import read_episode_events

    rows = list(read_episode_events(episode_id))
    return [e for e in rows if kind is None or e.event_kind == kind]


def test_the_real_dispatcher_records_an_attempt_and_a_matching_result(tmp_path):
    """Driven through the real ``run_operator_tool``, not a stand-in for it."""
    from auto_loop.execute import brain_skill_executor as executor
    from tests.helpers.episode_owner import owned

    episode_id = "ep-tool-1"
    with owned(episode_id):
        result, _args = executor.run_operator_tool(
            "no_such_tool", "{}", _ctx(tmp_path, episode_id=episode_id)
        )

        assert result.ok is False, "an unknown tool is a refusal"
        attempts = _events(episode_id, _auto_evidence().TOOL_ATTEMPT)
        results = _events(episode_id, _auto_evidence().TOOL_RESULT)

        assert len(attempts) == 1, (
            f"the dispatcher recorded {len(attempts)} attempt event(s); every real attempt at a "
            "tool must be on record before its outcome is known"
        )
        assert len(results) == 1, f"expected one result event, got {len(results)}"

        token = attempts[0].payload["attempt_token"]
        assert results[0].payload["attempt_token"] == token, (
            "the result carries a different token from its attempt, so the two cannot be "
            "correlated"
        )
        assert results[0].payload["error_kind"] == "unknown_tool"
        assert results[0].payload["effect"] == "no_effect", (
            "an unknown tool never ran, so the record must positively state the world is "
            "untouched rather than leaving a retry to guess"
        )


def test_a_failed_attempt_and_a_later_one_retain_both_occurrences(tmp_path):
    """Acceptance case "Retry and recovery", at the event level.

    Two real attempts, identical in tool, arguments and error text. If occurrence identity
    came from any of those, the second would replay over the first and the retry would be
    invisible — the defect WO-04A closure r3 fixed for phase failures.
    """
    from auto_loop.execute import brain_skill_executor as executor
    from tests.helpers.episode_owner import owned

    episode_id = "ep-tool-retry"
    with owned(episode_id):
        ctx = _ctx(tmp_path, episode_id=episode_id)
        executor.run_operator_tool("no_such_tool", "{}", ctx)
        executor.run_operator_tool("no_such_tool", "{}", ctx)

        attempts = _events(episode_id, _auto_evidence().TOOL_ATTEMPT)
        results = _events(episode_id, _auto_evidence().TOOL_RESULT)

        assert len(attempts) == 2, (
            f"two identical attempts collapsed into {len(attempts)} occurrence(s); equal tool, "
            "arguments and error text are not evidence that the tool was not run again"
        )
        assert len(results) == 2, f"expected two result events, got {len(results)}"
        tokens = {row.payload["attempt_token"] for row in attempts}
        assert len(tokens) == 2, "both attempts share one token"
        assert attempts[0].event_seq < attempts[1].event_seq, "the order must be recorded"


def test_a_context_with_no_episode_records_nothing_and_still_runs(tmp_path):
    """Instrumentation must not invent an owner it does not have.

    Plenty of callers drive ``run_operator_tool`` with no episode at all. That is not a
    capture failure — there is nothing to attribute the attempt to — so the tool still runs
    and no event is written. A capture failure while an episode IS bound is a different
    matter and stays loud.
    """
    from auto_loop.execute import brain_skill_executor as executor

    result, _args = executor.run_operator_tool("no_such_tool", "{}", _ctx(tmp_path))

    assert result.ok is False, "the tool must still be dispatched and still refuse"


def test_an_episode_id_without_the_owner_binding_records_nothing_and_still_runs(tmp_path):
    """An episode id is not ownership.

    Found by the full tests/core sweep, not by my focused selection: 36 pre-existing tests
    drive this dispatcher with an ExecutionContext that names an episode but holds no SRAM
    bucket, and `record_event` correctly refuses a caller without the matching live owner
    binding (WO-03A R1). Gating capture on the id alone turned every one of them into an
    EpisodeOwnershipError.

    Forcing those tests to open buckets would restore exactly the broad SRAM requirement
    across unrelated unit tests that WO-04 was told to stop doing. So a thread that owns
    nothing records nothing — there is genuinely no episode to attribute the attempt to — and
    the tool still runs.
    """
    from auto_loop.execute import brain_skill_executor as executor
    from memory import episode_sram

    episode_sram.reset()
    assert episode_sram.bound_episode_id() is None, "this thread must own nothing here"

    result, _args = executor.run_operator_tool(
        "no_such_tool", "{}", _ctx(tmp_path, episode_id="ep-unowned")
    )

    assert result.ok is False, "the tool must still be dispatched and still refuse"
    assert _events("ep-unowned") == [], (
        "an attempt was attributed to an episode this thread does not own"
    )


def test_a_thread_bound_to_a_different_episode_fails_loudly(tmp_path):
    """The other half, and the one that keeps the skip above honest.

    Owning NOTHING is a legitimate no-capture. Owning a DIFFERENT episode than the context
    names is a real defect: silently attributing one episode's work to another, or quietly
    dropping it, is worse than crashing. So the mismatch is not filtered out — it is left to
    fail where it is named.
    """
    import pytest as _pytest

    from auto_loop.execute import brain_skill_executor as executor
    from tests.helpers.episode_owner import owned

    with owned("ep-owner-A"):
        with _pytest.raises(Exception) as caught:
            executor.run_operator_tool(
                "no_such_tool", "{}", _ctx(tmp_path, episode_id="ep-different-B")
            )

    message = str(caught.value)
    assert "ep-different-B" in message and "ep-owner-A" in message, (
        "the failure must name BOTH episodes, or nobody can tell which binding was wrong: "
        f"{message}"
    )
