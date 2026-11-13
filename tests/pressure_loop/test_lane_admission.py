"""Each fixed semantic lane leases and advances its own durable FIFO.

Capacity and dispatch are faked in admission tests so they assert the routing
contract rather than running a model cascade. The real pressure tick and durable
queue still run, including lane-local ingress barriers and cancellation checks.
"""

import dataclasses
import inspect
from types import SimpleNamespace

import pytest

from auto_loop.execute import job_control as jobs
from core.shared_state import SharedState
from pressure_loop import cognition_queue as cq, loop, pipeline_dispatch, pressure
from pressure_loop.contributors import make_contributor
from pressure_loop.omega_successor import stage_omega_successor
from schema.omega_control_decision import OmegaControlDecision


@dataclasses.dataclass
class _Ignition:
    """Minimal stand-in: admission only ``replace``s these two fields."""

    dominant_contributor: object = None
    pipeline_id: int | None = None
    work_kind: str = ""


def _autonomous(seq):
    return make_contributor("autonomous", "curiosity", 1.0,
                            {"arrival_seq": seq, "cycle_id": f"auto-{seq}", "lane": "chat"})


def _operator(seq):
    return make_contributor("operator", "operator_query", 1.0,
                            {"arrival_seq": seq, "cycle_id": f"op-{seq}"})


def _agent_continuation(seq):
    return make_contributor("autonomous", "agent_continuation", 1.0,
                            {"arrival_seq": seq, "cycle_id": f"cont-{seq}"})


def _browser(seq):
    return make_contributor("omega", "browser_work", 1.0,
                            {"arrival_seq": seq, "cycle_id": f"browser-{seq}",
                             "lane": "browser"})


def _window(seq):
    return make_contributor("omega", "window_reflection_ready", 1.0,
                            {"arrival_seq": seq, "cycle_id": f"window-{seq}"})


@pytest.fixture
def admissions(monkeypatch):
    """Record what admission dispatched, without running a cascade."""
    recorded: list[tuple[int, str, str]] = []
    monkeypatch.setattr(pressure, "gather_contributors", lambda *_a, **_k: [])
    monkeypatch.setattr(pressure, "evaluate",
                        lambda rows, **_k: _Ignition(dominant_contributor=rows[0]))
    monkeypatch.setattr(pipeline_dispatch, "raise_pending_fatal", lambda: None)

    def dispatch(store, ignition, contributor, *, pipeline_id, work_kind, on_not_admitted):
        recorded.append((pipeline_id, work_kind, contributor.metadata["cycle_id"]))

    monkeypatch.setattr(pipeline_dispatch, "dispatch_cascade", dispatch)
    return recorded


def _free(monkeypatch, ids):
    monkeypatch.setattr(pipeline_dispatch, "free_pipeline_ids", lambda: tuple(ids))


def _tick(store=None):
    return loop._run_pressure_tick_body(
        SimpleNamespace(episode_store=store, shared_state=SharedState()))


def _queued_cycle_ids():
    return [row.metadata["cycle_id"] for row in cq.peek_inputs()]


def _leased(row) -> bool:
    """Whether any physical pipeline currently holds this row.

    Dispatch does NOT consume the queue row: acknowledgement happens at the episode's own
    boundary, so an admitted item stays durable and LEASED until then. "Still queued" therefore
    does not mean "not admitted" — these tests distinguish the two.
    """
    return any(cq.owns_input_lease(row, pid) for pid in range(4))


def _unleased_cycle_ids():
    return [row.metadata["cycle_id"] for row in cq.peek_inputs() if not _leased(row)]


# ── isolation sentinel: no durable write may touch the appliance's own state ───


def test_the_durable_queue_under_test_is_a_temporary_file(tmp_path):
    """The root conftest's autouse `_isolate_pipeline_envelopes` redirects `_queue_path`.

    Asserted rather than assumed: every test below enqueues to the DURABLE queue, so if that
    redirect ever stopped applying to this suite these tests would be writing the operator's
    real cognition queue — fabricated turns the next real boot would faithfully act on.
    """
    cq.enqueue_input(_autonomous(1), operator_activity=False)
    path = cq._queue_path()
    assert path.exists(), "the enqueue really was durable"
    assert tmp_path in path.parents, f"durable queue escaped the temporary root: {path}"
    assert "Omega3.0\\guardian" not in str(path)


# ── the acceptance matrix ─────────────────────────────────────────────────────


def test_four_different_lanes_can_be_admitted_in_one_tick(admissions, monkeypatch):
    """Four permanent lane bindings can all advance during the same pressure pass."""
    _free(monkeypatch, (0, 1, 2, 3))
    for item in (_operator(1), _browser(2), _agent_continuation(3), _window(4)):
        cq.enqueue_input(item, operator_activity=False)

    assert _tick() is True
    assert [row[2] for row in admissions] == ["op-1", "browser-2", "cont-3", "window-4"]
    assert {row[0] for row in admissions} == {0, 1, 2, 3}
    assert _unleased_cycle_ids() == [], "every queued item is held by a pipeline"


def test_chat_fifo_serializes_two_chat_turns(admissions, monkeypatch):
    """The next Chat turn waits for Chat to release, in Chat arrival order."""
    _free(monkeypatch, (0,))
    earlier = _autonomous(1)
    cq.enqueue_input(earlier, operator_activity=False)
    cq.enqueue_input(_operator(2), operator_activity=False)

    assert _tick() is True
    assert [row[2] for row in admissions] == [earlier.metadata["cycle_id"]]
    assert admissions[0][0] == 0
    assert _queued_cycle_ids() == [earlier.metadata["cycle_id"], "op-2"], \
        "the second Chat turn stayed in its own FIFO"
    assert _unleased_cycle_ids() == ["op-2"], "and it was not admitted"


def test_agent_continuation_and_chat_turn_can_run_concurrently(admissions, monkeypatch):
    _free(monkeypatch, (0, 2))
    cq.enqueue_input(_agent_continuation(1), operator_activity=False)
    cq.enqueue_input(_operator(2), operator_activity=False)

    assert _tick() is True
    assert {(row[0], row[2]) for row in admissions} == {(0, "op-2"), (2, "cont-1")}


def test_browser_work_uses_its_fixed_pipeline(admissions, monkeypatch):
    _free(monkeypatch, (1,))
    cq.enqueue_input(_browser(1), operator_activity=False)

    assert _tick() is True
    assert admissions == [(1, "autonomous", "browser-1")]


def test_window_reflection_uses_its_fixed_pipeline(admissions, monkeypatch):
    _free(monkeypatch, (3,))
    cq.enqueue_input(_window(1), operator_activity=False)

    assert _tick() is True
    assert admissions == [(3, "reflection", "window-1")]
    assert _unleased_cycle_ids() == []


def test_a_fifth_item_waits_and_admits_on_a_later_free_slot(admissions, monkeypatch):
    """Four lanes run; a second item for a busy lane waits in that lane's FIFO."""
    _free(monkeypatch, (0, 1, 2, 3))
    rows = (_operator(1), _browser(2), _agent_continuation(3), _window(4), _browser(5))
    for row in rows:
        cq.enqueue_input(row, operator_activity=False)

    assert _tick() is True
    assert [row[2] for row in admissions] == ["op-1", "browser-2", "cont-3", "window-4"]
    assert _unleased_cycle_ids() == ["browser-5"], "durable and not acknowledged"
    assert [row.metadata["arrival_seq"] for row in cq.peek_inputs()] == [1, 2, 3, 4, 5]

    # Browser's current episode reaches its own acknowledgement boundary.
    browser = next(row for row in cq.peek_inputs() if row.metadata["cycle_id"] == "browser-2")
    assert cq.acknowledge_input(browser, 1) is True
    admissions.clear()
    _free(monkeypatch, (1,))
    assert _tick() is True
    assert admissions == [(1, "autonomous", "browser-5")]


def test_completion_order_does_not_change_admission_order(monkeypatch):
    """Matrix row 5. Acknowledging out of order renumbers nothing; the next slot takes the next.

    Driven through the queue's own lease/acknowledge surface, because that is where arrival
    identity lives — a faked selector could not show it.
    """
    for item in (_operator(1), _browser(2), _agent_continuation(3), _window(4),
                 _agent_continuation(5)):
        cq.enqueue_input(item, operator_activity=False)
    leased = {pid: cq.lease_next_input(pid) for pid in range(4)}
    assert [leased[pid].metadata["cycle_id"] for pid in range(4)] == [
        "op-1", "browser-2", "cont-3", "window-4",
    ]
    assert cq.lease_next_input(0) is None, "a pipeline holding a lease takes nothing else"

    # Pipeline 2 finishes FIRST, out of admission order.
    assert cq.acknowledge_input(leased[2], 2) is True
    assert _queued_cycle_ids() == ["op-1", "browser-2", "window-4", "cont-5"]
    assert [row.metadata["arrival_seq"] for row in cq.peek_inputs()] == [1, 2, 4, 5], \
        "arrival order is never renumbered or reassigned"

    # Its now-free slot consumes the next UNLEASED item, not the oldest still-leased one.
    assert cq.lease_next_input(2).metadata["cycle_id"] == "cont-5"


def test_an_incomplete_chat_arrival_does_not_hold_other_lanes(admissions, monkeypatch):
    """An ingress barrier preserves Chat order without stalling Browser."""
    _free(monkeypatch, (0, 1, 2, 3))
    held = cq.reserve_operator_arrival("still-screening")
    cq.enqueue_input(_operator(held + 1), operator_activity=False)
    cq.enqueue_input(_browser(held + 2), operator_activity=False)

    assert _tick() is True
    assert admissions == [(1, "autonomous", f"browser-{held + 2}")]
    assert len(cq.peek_inputs()) == 2, "both later arrivals are intact and still waiting"


def test_chat_advances_its_fifo_after_each_exact_ack(admissions, monkeypatch):
    _free(monkeypatch, (0, 1, 2, 3))
    for item in (_autonomous(1), _operator(2)):
        cq.enqueue_input(item, operator_activity=False)

    assert _tick() is True
    assert admissions == [(0, "autonomous", "auto-1")]
    first = next(row for row in cq.peek_inputs() if row.metadata["cycle_id"] == "auto-1")
    assert cq.acknowledge_input(first, 0) is True
    admissions.clear()
    assert _tick() is True
    assert admissions == [(0, "operator_turn", "op-2")]


# ── matrix rows 7 and 8: cancellation, through the real producers ─────────────


JOB = "fifo-job"
JOB_CYCLE = "fifo-bubble"
THREAD = "t-fifo"
OBJECTIVE = "build the thing"


@pytest.fixture
def successors(tmp_path, monkeypatch):
    """The real successor ledger and job store, on temporary paths."""
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})
    pressure._UNRESOLVED_STOPS.clear()
    yield
    pressure._UNRESOLVED_STOPS.clear()
    cq.clear_inputs()


def _control():
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "fact")
    assessment.update(gap="unfinished", evidence=[])
    return OmegaControlDecision.from_dict(dict(
        assessment=assessment, decision="PRESSURE", synthesis="summary",
        pressure=dict(intent="USE_TOOL", confidence=0.5, reason="finish", setpoints={},
                      desired_state="complete", observed_state="partial",
                      resolution_conditions=["verified"])))


def _agent_episode():
    return SimpleNamespace(
        episode_id="parent", thread_id=THREAD, opened_by="operator_query",
        global_={"omega_packet": {"original_objective": OBJECTIVE, "omega_lane": "agent"}},
        pressure={"contributors": [{"id": "operator_query", "detail": {"cycle_id": JOB_CYCLE}}]},
        auto={"outputs": {"agent_run": {"status": "batch_ran", "steps": 1, "job_id": JOB}}})


def _open_job():
    root = jobs.jobs_root().parent.parent.parent
    project = root / "projects" / JOB
    project.mkdir(parents=True, exist_ok=True)
    return jobs.open_job(JOB, JOB_CYCLE, OBJECTIVE, root, project_root=project,
                         thread_id=THREAD)


def _unreadable_ledger(monkeypatch):
    """Switchable, never undone: `monkeypatch.undo()` would revert the autouse isolation too."""
    real_read = pressure._read_successors
    broken = {"on": True}

    def read():
        if broken["on"]:
            raise OSError("ledger unreadable")
        return real_read()

    monkeypatch.setattr(pressure, "_read_successors", read)
    return broken


def test_an_unknown_cancellation_head_is_not_consumed_and_is_not_bypassed(
    admissions, monkeypatch, successors
):
    """An unknown job stop holds Agent, while the independent Chat lane advances."""
    _open_job()
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None
    assert cq.enqueue_input(row, operator_activity=False)
    assert pressure.release_staged(row), "queued, and no longer staged"
    cq.enqueue_input(_operator(99), operator_activity=False)
    broken = _unreadable_ledger(monkeypatch)
    _free(monkeypatch, (0, 1, 2, 3))

    assert _tick() is True
    assert admissions == [(0, "operator_turn", "op-99")]
    still_queued = [r.metadata.get("successor_id") or r.metadata["cycle_id"]
                    for r in cq.peek_inputs()]
    assert still_queued == [row.metadata["successor_id"], "op-99"], \
        "both rows keep their original positions"

    # The next tick judges the agent row again after its stop authority is readable.
    broken["on"] = False
    assert _tick() is True
    assert admissions[-1][0] == 2
    assert len(admissions) == 2, "the deferred row first, then the operator"


def test_a_head_that_stays_unjudgeable_fails_loudly_instead_of_stalling_the_line(
    admissions, monkeypatch, successors
):
    """A persistently unreadable Agent cancellation fails with the row and cause intact."""
    from core.required_substrate import RequiredSubstrateError

    _open_job()
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None
    assert cq.enqueue_input(row, operator_activity=False)
    assert pressure.release_staged(row), "queued, and no longer staged"
    cq.enqueue_input(_operator(99), operator_activity=False)
    _unreadable_ledger(monkeypatch)
    _free(monkeypatch, (0, 1, 2, 3))
    now = {"t": 1000.0}
    monkeypatch.setattr(loop, "_clock", lambda: now["t"])

    assert _tick() is True, "Chat can advance while the Agent head is unjudgeable"
    assert admissions == [(0, "operator_turn", "op-99")]
    now["t"] += loop.UNJUDGEABLE_HEAD_LIMIT_S - 1
    assert _tick() is False, "still within the bound"
    now["t"] += 2
    with pytest.raises(RequiredSubstrateError, match="cancellation") as caught:
        _tick()
    assert row.metadata["successor_id"] in str(caught.value)
    assert "ledger unreadable" in str(caught.value)
    assert admissions == [(0, "operator_turn", "op-99")], \
        "Chat advanced once; the unjudgeable Agent row was never dispatched"
    assert [r.metadata.get("successor_id") or r.metadata["cycle_id"] for r in cq.peek_inputs()] == [
        row.metadata["successor_id"], "op-99"], "both rows keep their positions"
    assert not _leased(row), "the failing pass released its lease"


def test_a_confirmed_stop_retires_only_that_item_and_the_line_advances(
    admissions, monkeypatch, successors
):
    """Exactly the stopped Agent item is retired; the independent Chat turn proceeds."""
    _open_job()
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None
    assert cq.enqueue_input(row, operator_activity=False)
    cq.enqueue_input(_operator(99), operator_activity=False)
    jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)
    _free(monkeypatch, (0, 1, 2, 3))

    assert _tick() is True
    assert admissions == [(0, "operator_turn", "op-99")], \
        "the stopped item never reached dispatch; the next item advanced"
    assert _queued_cycle_ids() == ["op-99"], \
        "exactly the stopped copy was retired; the untouched item is still there"
    assert row.metadata["successor_id"] in pressure._read_successors()["admitted"], \
        "retired through the existing successor mechanism"


# ── matrix rows 9 and 10 ──────────────────────────────────────────────────────


def test_a_rejected_evaluation_releases_only_its_own_lease(admissions, monkeypatch):
    """Matrix row 9. The exact request is preserved; no other lease is disturbed."""
    _free(monkeypatch, (0, 1))
    cq.enqueue_input(_autonomous(1), operator_activity=False)
    cq.enqueue_input(_autonomous(2), operator_activity=False)
    monkeypatch.setattr(pressure, "evaluate", lambda rows, **_k: None)

    assert _tick() is False
    assert admissions == []
    assert _queued_cycle_ids() == ["auto-1", "auto-2"], "both requests are intact"
    assert cq.lease_next_input(0) is not None, "the released lease is available again"


def test_a_dispatch_that_does_not_admit_releases_only_its_own_lease(admissions, monkeypatch):
    """Matrix row 9, dispatch half: `on_not_admitted` frees that pipeline's lease alone."""
    _free(monkeypatch, (0, 1))
    cq.enqueue_input(_autonomous(1), operator_activity=False)
    cq.enqueue_input(_autonomous(2), operator_activity=False)

    def refuse(store, ignition, contributor, *, pipeline_id, work_kind, on_not_admitted):
        admissions.append((pipeline_id, work_kind, contributor.metadata["cycle_id"]))
        on_not_admitted()

    monkeypatch.setattr(pipeline_dispatch, "dispatch_cascade", refuse)

    assert _tick() is True
    # Releasing leaves the row at its own position, so the very next pipeline is offered
    # the SAME oldest request rather than skipping past it. That is the FIFO working, not
    # a retry loop: nothing was consumed and nothing was reordered.
    assert [row[2] for row in admissions] == ["auto-1"]
    assert _queued_cycle_ids() == ["auto-1", "auto-2"], "neither request was consumed"
    assert _unleased_cycle_ids() == ["auto-1", "auto-2"], "every lease was released"


def test_a_busy_pipeline_is_never_preempted_or_reassigned(admissions, monkeypatch):
    """A busy Browser lane holds only Browser work while other lanes proceed."""
    busy = {1: "running-episode"}
    store = SimpleNamespace(current=lambda *, pipeline_id: busy.get(pipeline_id))
    _free(monkeypatch, (0, 1, 2, 3))
    for item in (_operator(1), _browser(2), _agent_continuation(3), _window(4)):
        cq.enqueue_input(item, operator_activity=False)

    assert _tick(store) is True
    assert {row[0] for row in admissions} == {0, 2, 3}, "pipeline 1 was left alone"
    assert [row[2] for row in admissions] == ["op-1", "cont-3", "window-4"]
    assert busy == {1: "running-episode"}, "the running episode was not moved or replaced"
    assert _unleased_cycle_ids() == ["browser-2"], "only the busy lane's item waits"


# ── the removed selection route ───────────────────────────────────────────────


def _executable_source(function) -> str:
    """The function's code with its docstring stripped, so prose cannot satisfy an audit."""
    import ast
    import textwrap

    definition = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    body = definition.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


def test_the_queue_lease_has_no_selection_filter_left():
    """The argument is REMOVED, not defaulted off and not ignored."""
    assert list(inspect.signature(cq.lease_next_input).parameters) == ["pipeline_id"]
    assert list(inspect.signature(loop._lease_runnable_input).parameters) == ["pipeline_id"]
    assert "operator_only" not in _executable_source(cq.lease_next_input)


def test_no_priority_bearing_selection_route_remains_in_admission():
    """Audit the executable call graph, not a grep of the whole word.

    `has_waiting_input(operator_only=True)` legitimately survives: it is a non-claiming
    OBSERVATION that feeds the `operator_message` pressure component (§4.0, "the line IS the
    pressure"). It reorders nothing and consumes nothing. What must not exist is a
    priority-bearing filter on the LEASE/SELECTION path.
    """
    import ast
    import textwrap

    for function in (loop._run_pressure_tick_body, loop._lease_runnable_input):
        source = _executable_source(function)
        for node in ast.walk(ast.parse(textwrap.dedent(source))):
            if isinstance(node, ast.keyword) and node.arg == "operator_only":
                raise AssertionError(
                    f"a selection filter survives in {function.__name__}:\n{source}"
                )
        for banned in ("reserved", "free_remaining"):
            assert banned not in source, f"{banned!r} still shapes {function.__name__}"

    lease = _executable_source(cq.lease_next_input)
    assert "work_kind_for_contributor" not in lease, "selection must not read work_kind"
    assert "source" not in lease, "selection must not read the contributor's source"
