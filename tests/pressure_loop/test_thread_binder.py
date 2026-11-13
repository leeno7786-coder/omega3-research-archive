"""T3: the ingress binder decides which thread an operator turn belongs to.

Cross-lane cognitive integration spec, section 1. T2 made ``thread_id`` a stable
invariant — created once by the host, immutable, persisted on the episode and the
journal row, inherited by successors. T3 is therefore a BINDER only, never an
identity generator: it answers continue / modify / begin by SELECTING an existing
thread_id or returning "" so ``EpisodeStore.open`` roots a new one.

  named running or unfinished job    -> that job's thread
  answer to a pending agent question -> the thread recorded ON the question
  explicit referential follow-up     -> the thread the recent context DESCRIBES
  ambiguous or unrelated input       -> a new thread

Review 2026-09-16 found five correctness gaps against the spec's
"ambiguity -> new thread" contract. Each is pinned here:

1. Recent-route context carried no identity, so a follow-up bound to whichever
   journal row happened to be newest — possibly another lane's thread entirely,
   since four lanes run at once. Context now names the thread it describes.
2. Any text containing a pronoun was treated as a resolved reference, so
   "what time is it?" and "forget that, start something new" both inherited a
   thread. A live context plus a matching word is not a referent.
3. An agent question was assumed to be journaled or to have a staged successor,
   but ``ask_operator`` publishes during AUTO before either is guaranteed, and
   successor admission keeps only a digest. The thread now rides the question
   record, and an unresolved explicit answer must NOT fall through to reference
   matching.
4. Job ids were matched as substrings and disambiguated by length, so "job-777"
   matched "job-7" and "compare job-7 and job-88" silently picked one.
5. The paused ingress path enqueued without ever calling the binder.

The recent-route context is driven through the REAL ``remember_action`` here
rather than by stubbing its accessors: the identity linkage in gap 1 is the thing
under test, so simulating it would test nothing.
"""

from types import SimpleNamespace

import pytest

from global_loop.intent import context as route_context
from pressure_loop import episode_journal, operator_chat, pressure, system_messages
from pressure_loop.contributors import from_operator_message, make_contributor
from pressure_loop.episode_journal import PriorEpisode

ANSWERED = "CODING_AGENT"


def _row(episode_id, thread_id):
    return PriorEpisode(episode_id, "operator_query", None, None, None, None,
                        thread_id=thread_id)


def _successor(job_id, thread_id):
    return make_contributor("omega", "omega_successor", 1.0,
                            {"agent_job_id": job_id, "thread_id": thread_id})


def _question(thread_id, *, episode_id="asking-ep", job_id="job-7"):
    """A pending agent question as ``current_outreach`` reports it."""
    return {"kind": "agent_question", "episode_id": episode_id, "question_id": "q1",
            "thread_id": thread_id, "meta": {"job_id": job_id}}


def _context(thread_id):
    """Record a real recent-route context describing ``thread_id``."""
    route_context.remember_action(ANSWERED, "wrote a parser",
                                  thread_id=thread_id, episode_id="ep-answered")


@pytest.fixture(autouse=True)
def _no_ambient_sources(monkeypatch, tmp_path):
    """Every source is empty unless a test supplies it."""
    monkeypatch.setattr(system_messages, "_store_path", lambda: tmp_path / "system_messages.json")
    system_messages._reset_cache_for_tests()
    monkeypatch.setattr(episode_journal, "journal_episodes_by_ids", lambda ids, **_k: [])
    monkeypatch.setattr(episode_journal, "journal_window", lambda *_a, **_k: [])
    monkeypatch.setattr(pressure, "staged_signals", lambda: ())
    route_context.reset()

    from auto_loop.execute import job_control

    monkeypatch.setattr(job_control, "active_jobs", lambda: ())
    yield
    system_messages._reset_cache_for_tests()
    route_context.reset()


# ── the recent-route context must identify what it describes (gap 1) ──────────

def test_route_context_records_the_thread_it_describes():
    _context("t-agent")

    assert route_context.previous_thread() == "t-agent"
    assert route_context.previous_episode() == "ep-answered"
    assert ANSWERED in route_context.previous_action()


def test_route_context_thread_expires_with_its_line():
    """One TTL governs both: an expired line cannot anchor a referent."""
    route_context.remember_action(ANSWERED, "wrote a parser", thread_id="t-agent",
                                  episode_id="ep-answered", now=0.0)
    later = route_context.CONTEXT_TTL_SECONDS + 1

    assert route_context.previous_action(now=later) == ""
    assert route_context.previous_thread(now=later) == ""


# ── answer to a pending agent question (gap 3) ────────────────────────────────

def test_an_answer_to_an_agent_question_continues_the_thread_on_the_question():
    """Authoritative for the whole lifecycle: no journal or ledger dependency."""
    assert operator_chat._bind_thread("use sqlite", _question("t-agent"), {}) == "t-agent"


def test_an_unresolved_answer_does_not_fall_through_to_reference_matching():
    """ask_operator publishes during AUTO; if the thread is missing, bind nothing.

    Falling through was worse than binding nothing: with a live context, "use that"
    bound the answer to an unrelated thread.
    """
    _context("t-unrelated")

    assert operator_chat._bind_thread("use that", _question(""), {}) == ""


def test_an_answer_falls_back_to_the_job_ledger_only_within_the_agent_question(monkeypatch):
    monkeypatch.setattr(pressure, "staged_signals", lambda: (_successor("job-7", "t-agent"),))

    assert operator_chat._bind_thread("use sqlite", _question("", job_id="job-7"),
                                      {"job_id": "job-7"}) == "t-agent"


# ── a turn that names a job (gap 4) ───────────────────────────────────────────

def test_a_turn_naming_an_unfinished_job_continues_its_thread(monkeypatch):
    monkeypatch.setattr(pressure, "staged_signals", lambda: (_successor("job-7", "t-build"),))

    assert operator_chat._bind_thread("what happened with job-7?", None, {}) == "t-build"


def test_a_turn_naming_a_running_job_continues_its_thread(monkeypatch):
    """Ordinary mid-build input is intercepted before the binder and held as a job
    instruction; this covers the paths that still reach binding."""
    from auto_loop.execute import job_control

    monkeypatch.setattr(job_control, "active_jobs",
                        lambda: (SimpleNamespace(job_id="job-9", status="running"),))
    monkeypatch.setattr(pressure, "staged_signals", lambda: (_successor("job-9", "t-running"),))

    assert operator_chat._bind_thread("job-9 status", None, {}) == "t-running"


def test_a_longer_job_id_is_not_matched_by_a_shorter_one(monkeypatch):
    """job-777 is not job-7: ids match as complete identifiers or not at all."""
    monkeypatch.setattr(pressure, "staged_signals", lambda: (_successor("job-7", "t-build"),))

    assert operator_chat._bind_thread("check job-777 please", None, {}) == ""


def test_two_named_jobs_on_different_threads_are_ambiguous(monkeypatch):
    monkeypatch.setattr(pressure, "staged_signals",
                        lambda: (_successor("job-7", "t-one"), _successor("job-88", "t-two")))

    assert operator_chat._bind_thread("compare job-7 and job-88", None, {}) == ""


def test_two_named_jobs_on_one_thread_still_resolve(monkeypatch):
    """Ambiguity is about the resolved THREAD, not the number of ids named."""
    monkeypatch.setattr(pressure, "staged_signals",
                        lambda: (_successor("job-7", "t-same"), _successor("job-88", "t-same")))

    assert operator_chat._bind_thread("compare job-7 and job-88", None, {}) == "t-same"


# ── explicit referential follow-up (gaps 1 and 2) ─────────────────────────────

def test_a_referential_follow_up_continues_the_thread_the_context_describes(monkeypatch):
    _context("t-described")
    # A different lane archived more recently; the newest row must not win.
    monkeypatch.setattr(episode_journal, "journal_window",
                        lambda *_a, **_k: [_row("newest", "t-other-lane")])

    assert operator_chat._bind_thread("build that", None, {}) == "t-described"


def test_a_follow_up_never_binds_to_an_unrelated_newest_journal_row(monkeypatch):
    """Context and the journal have no identity linkage; only context decides."""
    _context("")
    monkeypatch.setattr(episode_journal, "journal_window",
                        lambda *_a, **_k: [_row("autonomous-ep", "t-autonomous")])

    assert operator_chat._bind_thread("build that", None, {}) == ""


def test_an_interrogative_containing_a_pronoun_is_not_a_follow_up():
    _context("t-described")

    assert operator_chat._bind_thread("what time is it?", None, {}) == ""


def test_an_explicit_new_topic_is_not_a_follow_up():
    _context("t-described")

    for text in ("Forget that. Start a new unrelated project.",
                 "do that instead on a different repo",
                 "never mind that, start over"):
        assert operator_chat._bind_thread(text, None, {}) == "", text


def test_a_referential_turn_with_no_recent_context_starts_a_new_thread():
    assert operator_chat._bind_thread("build that", None, {}) == ""


def test_an_unrelated_turn_starts_a_new_thread():
    _context("t-described")

    assert operator_chat._bind_thread("what is the capital of France?", None, {}) == ""


# ── invariants ────────────────────────────────────────────────────────────────

def test_the_binder_never_authors_an_identifier(monkeypatch):
    """Whatever it returns is an id that already existed, or nothing at all."""
    existing = {"t-described", "t-build"}
    _context("t-described")
    monkeypatch.setattr(pressure, "staged_signals", lambda: (_successor("job-7", "t-build"),))

    for text in ("build that", "job-7 again", "hello", "", "start a brand new thing",
                 "what time is it?", "compare job-7 and job-777"):
        bound = operator_chat._bind_thread(text, None, {})
        assert bound == "" or bound in existing, text


def test_the_resolved_thread_reaches_the_contributor_metadata():
    """The binding rides the ordinary ingress metadata channel, unfiltered."""
    contributor = from_operator_message(
        {"message": "build that", "ingress_class": "operator_query", "thread_id": "t-prev"})

    assert contributor.metadata["thread_id"] == "t-prev"


# ── paused ingress must bind too (gap 5) ──────────────────────────────────────

def test_a_turn_queued_while_paused_still_carries_its_thread(monkeypatch):
    """A pause holds the line; it must not strip the turn's thread on the way in."""
    from pressure_loop import cognition_queue, pressure_loop_pause

    queued: list = []
    monkeypatch.setattr(pressure_loop_pause, "is_pressure_loop_paused", lambda: True)
    monkeypatch.setattr(cognition_queue, "enqueue_input",
                        lambda contributor, **_k: queued.append(contributor) or True)
    _context("t-described")

    operator_chat._screen_and_enqueue_operator_chat("build that", cycle_id="paused-1",
                                                    arrival_seq=1)

    assert [c.metadata.get("thread_id") for c in queued] == ["t-described"]


def test_a_paused_answer_keeps_its_own_questions_thread(monkeypatch, tmp_path):
    """A question on thread A must not become thread B just because a pause held it."""
    from pressure_loop import cognition_queue, pressure_loop_pause, system_messages

    queued: list = []
    monkeypatch.setattr(pressure_loop_pause, "is_pressure_loop_paused", lambda: True)
    monkeypatch.setattr(cognition_queue, "enqueue_input",
                        lambda contributor, **_k: queued.append(contributor) or True)
    from auto_loop.execute import job_control

    job_control.open_job("job-7", "asking-cycle", "build the parser", tmp_path,
                         thread_id="t-question")
    system_messages.append_system_query(
        episode_id="asking-ep", text="Which storage?", kind="agent_question", opened_by="operator_query",
        thread_id="t-question", meta={"job_id": "job-7"})
    _context("t-context")

    operator_chat._screen_and_enqueue_operator_chat("use that", cycle_id="paused-2",
                                                    arrival_seq=2)

    assert [c.metadata.get("thread_id") for c in queued] == ["t-question"]


# ── the producer path: a question must actually CARRY its episode's thread ─────

def test_a_question_asked_mid_batch_records_its_episode_thread(tmp_path):
    """The whole point of gap 3: the record has to be written WITH a thread.

    A populated record was previously supplied by the test itself, which hid a
    producer that always wrote "" (review 2026-09-16). This drives the real tool,
    reloads the durable store, and then binds an answer from it.
    """
    from auto_loop.execute import brain_skill_executor as executor
    from pressure_loop import system_messages

    ctx = executor.ExecutionContext(tmp_path, "build a parser", 30.0,
                                    episode_id="ep-live", thread_id="t-live", job_id="job-7")

    result = executor._ask_operator(
        {"question": "sqlite or postgres?", "options": ["sqlite", "postgres"]}, ctx)

    assert result.ok
    assert system_messages.current_outreach()["thread_id"] == "t-live"

    # Durable, not just in memory: an answer may arrive after a restart.
    system_messages._reset_cache_for_tests()
    reloaded = system_messages.current_outreach()
    assert reloaded["thread_id"] == "t-live"
    assert operator_chat._bind_thread("sqlite", reloaded, {}) == "t-live"


# ── ambiguity must terminate binding, not fall through (gap 2) ─────────────────

def test_an_ambiguous_job_reference_does_not_inherit_the_context_thread(monkeypatch):
    _context("t-parser")
    monkeypatch.setattr(pressure, "staged_signals",
                        lambda: (_successor("job-7", "t-one"), _successor("job-88", "t-two")))

    assert operator_chat._bind_thread(
        "update job-7 and job-88 to use the same compiler", None, {}) == ""


def test_a_named_job_with_an_unknown_thread_is_unresolved(monkeypatch):
    """One resolved job must not win while another NAMED job's thread is unknown."""
    from auto_loop.execute import job_control

    _context("t-parser")
    monkeypatch.setattr(job_control, "active_jobs",
                        lambda: (SimpleNamespace(job_id="job-5", status="running"),))
    monkeypatch.setattr(pressure, "staged_signals", lambda: (_successor("job-7", "t-known"),))

    assert operator_chat._bind_thread("update job-5 and job-7 together", None, {}) == ""


# ── the follow-up grammar must reject new requests (gap 3 of review 2) ────────

def test_a_new_request_that_merely_contains_a_pronoun_is_not_a_follow_up():
    """Arbitrary text between verb and pronoun accepted these as follow-ups."""
    _context("t-parser")

    for text in ("build a clock that displays UTC",
                 "run this SQL: SELECT 1",
                 "make a tool that converts it"):
        assert operator_chat._bind_thread(text, None, {}) == "", text


def test_a_complement_follow_up_still_binds():
    """The conservative grammar must still admit the documented follow-up shape."""
    _context("t-parser")

    for text in ("now make it a pdf", "fix it", "run it again", "use the same"):
        assert operator_chat._bind_thread(text, None, {}) == "t-parser", text


def test_a_self_contained_request_does_not_inherit_context():
    """The pronoun points FORWARD at content the message supplies, not back at a turn."""
    _context("t-parser")

    for text in ("run this: SELECT 1",
                 "make this a PDF: Hello world",
                 "fix it\nSELECT 1"):
        assert operator_chat._bind_thread(text, None, {}) == "", text


# ── a dotted job id is its own identifier (gap 3 of review 3) ────────────────

def test_a_dotted_job_id_matches_itself_not_its_prefix(monkeypatch):
    """job_control._ID permits dots, so job-7 must not match inside job-7.backup."""
    monkeypatch.setattr(pressure, "staged_signals",
                        lambda: (_successor("job-7", "t-short"),
                                 _successor("job-7.backup", "t-dotted")))

    assert operator_chat._bind_thread("check job-7.backup", None, {}) == "t-dotted"


# ── paused answer-eligibility (gap 2 of review 3) ────────────────────────────

def test_a_paused_directive_is_not_an_answer(monkeypatch):
    """Normal ingress excludes directives from answering; the paused path must too."""
    from pressure_loop import cognition_queue, pressure_loop_pause, system_messages

    queued: list = []
    monkeypatch.setattr(pressure_loop_pause, "is_pressure_loop_paused", lambda: True)
    monkeypatch.setattr(cognition_queue, "enqueue_input",
                        lambda contributor, **_k: queued.append(contributor) or True)
    monkeypatch.setattr(system_messages, "current_outreach", lambda: _question("t-question"))

    operator_chat._screen_and_enqueue_operator_chat(
        "draw a mountain", cycle_id="paused-3", arrival_seq=3,
        directive_intent="IMAGE_CREATE")

    assert [c.metadata.get("thread_id") for c in queued] == [None]


# ── paused/unpaused parity comes from ONE decision (gap 1 of review 4) ───────

@pytest.mark.parametrize("text,eligible", [
    ("learn about volcanoes", False),
    ("remember that my bicycle is blue", False),
    ("sqlite", True),
    ("yes", True),
])
def test_answer_eligibility_is_a_single_shared_decision(text, eligible):
    """Both paths call this; parity is structural, not two matching expressions."""
    assert operator_chat._ingress_decision(text, None)[3] is eligible


@pytest.mark.parametrize("text,expected", [
    ("learn about volcanoes", None),
    ("remember that my bicycle is blue", None),
    ("sqlite", "t-question"),
])
def test_a_paused_turn_binds_exactly_as_it_would_awake(monkeypatch, tmp_path, text, expected):
    """Deterministic commands must not inherit a question's thread while paused."""
    from pressure_loop import cognition_queue, pressure_loop_pause, system_messages

    queued: list = []
    monkeypatch.setattr(pressure_loop_pause, "is_pressure_loop_paused", lambda: True)
    monkeypatch.setattr(cognition_queue, "enqueue_input",
                        lambda contributor, **_k: queued.append(contributor) or True)
    from auto_loop.execute import job_control

    job_control.open_job("job-7", "asking-cycle", "build the parser", tmp_path,
                         thread_id="t-question")
    system_messages.append_system_query(
        episode_id="asking-ep", text="Which storage?", kind="agent_question", opened_by="operator_query",
        thread_id="t-question", meta={"job_id": "job-7"})

    operator_chat._screen_and_enqueue_operator_chat(text, cycle_id=f"paused-{abs(hash(text)) % 997}",
                                                    arrival_seq=9)

    assert queued, text
    assert queued[-1].metadata.get("thread_id") == expected, text
