"""Operator chat always enters Chat before Global A routes specialist work."""

import json
from datetime import datetime, timezone

import pytest

from auto_loop.execute import job_control as jobs
from core.pipelines import lane_for_contributor
from global_loop import intersections as mail
from pressure_loop import cognition_queue as queue
from pressure_loop.operator_chat import (
    _named_job_thread,
    _screen_and_enqueue_operator_chat,
    _thread_for_job,
)

OBJECTIVE = "build a todo app"
THREAD = "t-build"
NOTE = "also add a delete button"


@pytest.fixture(autouse=True)
def _ordinary_chat_never_becomes_a_job_revision(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("ordinary chat must never become a job revision")

    monkeypatch.setattr(jobs, "request_instruction", refuse)


@pytest.fixture
def queued(monkeypatch):
    turns = []

    def enqueue(contributor, **_kwargs):
        turns.append(contributor)
        return True

    monkeypatch.setattr(queue, "enqueue_input", enqueue)
    return turns


def _open(job_id, thread_id):
    root = jobs.jobs_root().parent.parent.parent
    project = root / "projects" / job_id
    project.mkdir(parents=True, exist_ok=True)
    return jobs.open_job(job_id, f"cycle-{job_id}", OBJECTIVE, root,
                         project_root=project, thread_id=thread_id)


def _chat(text, **kwargs):
    kwargs.setdefault("cycle_id", "note-cycle")
    return _screen_and_enqueue_operator_chat(text, **kwargs)


def test_active_agent_does_not_bypass_the_chat_global_a_classifier(queued):
    _open("job-a", THREAD)

    result = _chat(NOTE)

    assert result["ok"] is True and result["queued"] is True
    assert len(queued) == 1
    assert lane_for_contributor(queued[0]) == "chat"
    assert queued[0].source == "operator"
    assert queued[0].metadata["message"] == NOTE
    assert "operator_route" not in queued[0].metadata
    assert "operator_route_pin" not in queued[0].metadata
    assert mail.all_mail() == [], "Global A creates specialist mail after classification"
    assert jobs.get_job("job-a").revisions == ()


def test_job_thread_lookup_uses_host_owned_job_identity(monkeypatch):
    _open("job-a", "t-a")
    monkeypatch.setattr("pressure_loop.pressure.staged_signals", lambda: ())

    assert _thread_for_job("job-a") == "t-a"
    assert _named_job_thread("update job-a to use sqlite") == "t-a"


def test_thread_lookup_keeps_unresolved_job_references_unresolved(monkeypatch):
    _open("job-a", "t-a")
    monkeypatch.setattr("pressure_loop.pressure.staged_signals", lambda: ())

    assert _named_job_thread("update job-missing to use sqlite") == ""
    assert _named_job_thread("nice job, what next?") is None


def test_a_job_records_the_thread_it_opened_in():
    _open("job-a", "t-a")

    assert jobs.get_job("job-a").thread_id == "t-a"


def test_reopening_a_job_under_another_thread_is_an_identity_conflict():
    job = _open("job-a", "t-a")

    with pytest.raises(ValueError, match="thread conflict"):
        jobs.open_job("job-a", job.cycle_id, OBJECTIVE, job.workspace_root,
                      thread_id="t-other")
    assert jobs.get_job("job-a").thread_id == "t-a"


def _v1_row(mail_id, **overrides):
    import time

    row = {
        "mail_id": mail_id,
        "effect_id": f"effect-{mail_id}",
        "generation": 0,
        "arrival_seq": 41,
        "thread_id": THREAD,
        "delivery_point": "omega_reason",
        "purpose": "operator_instruction",
        "requirement": "required",
        "target_lane": "",
        "target_job_id": "",
        "continuation_key": "",
        "source_actor": "operator",
        "source_episode_id": "ep-op",
        "payload": {"text": "use sqlite"},
        "provenance": [],
        "status": "ready",
        "lease_id": "",
        "lease_until": 0.0,
        "claimed_by": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ttl_s": 900.0,
    }
    row.update(overrides)
    if row["status"] == "leased":
        row.setdefault("lease_until", time.time() + 600)
    return row


def test_a_version_1_mail_store_migrates_without_expiring_admitted_mail():
    import time

    ready = _v1_row("m-ready")
    leased = _v1_row("m-leased", status="leased", lease_id="lease-1", claimed_by="ep-old",
                     generation=1, arrival_seq=42, lease_until=time.time() + 600)
    mail.mail_path().parent.mkdir(parents=True, exist_ok=True)
    mail.mail_path().write_text(json.dumps({"version": 1, "mail": {
        "m-ready": ready, "m-leased": leased}}), encoding="utf-8")
    mail.reset_cache_for_tests()

    rows = {row["mail_id"]: row for row in mail.all_mail()}

    expected_ready = {**ready, "sent_seq": 0, "origin": {}, "offered_to": []}
    expected_ready.pop("ttl_s")
    expected_leased = {**leased, "sent_seq": 0, "origin": {}, "offered_to": ["ep-old"]}
    expected_leased.pop("ttl_s")
    assert rows["m-ready"] == expected_ready
    assert rows["m-leased"] == expected_leased
    assert mail.acknowledge_mail([{"mail_id": "m-leased", "lease_id": "lease-1",
                                   "generation": 1}], episode_id="ep-old") == 1
    on_disk = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    assert on_disk["version"] == mail.MAIL_VERSION
    assert "ttl_s" not in on_disk["mail"]["m-ready"]


@pytest.mark.parametrize("payload", [
    {"version": 1, "mail": {"m": {**_v1_row("m"), "sent_seq": 3}}},
    {"version": 1, "mail": {"m": {key: value for key, value in _v1_row("m").items()
                                     if key != "ttl_s"}}},
    {"version": 99, "mail": {}},
], ids=["v1-with-v2-field", "v1-missing-field", "unknown-version"])
def test_an_invalid_or_unknown_mail_version_remains_unavailable(payload):
    mail.mail_path().parent.mkdir(parents=True, exist_ok=True)
    mail.mail_path().write_text(json.dumps(payload), encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(ValueError):
        mail.all_mail()
