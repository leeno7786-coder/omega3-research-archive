"""T5: the cross-lane mail store, with leases rather than claim-on-read.

Cross-lane cognitive integration spec, section 2. Mail is an addressed payload that
enters cognition at exactly ONE declared delivery point. The store is durable, the host
owns every identifier, and delivery is at-least-once:

  ready -> leased          a consumer claimed it, with a lease id and a deadline
  leased -> ready          the lease expired; a crash before reasoning loses nothing
  leased -> (deleted)      acknowledgement: the consuming step's outcome is durable,
                           so the row is removed. Honour lives on the binding.

Acknowledged is not fulfilled. Whether a requirement was HONOURED lives in the receipt
and causal record, never in a mail status.

Effect identity is minted at PIN time, not derived from the receiving episode, so a
redelivery after a lease expiry — possibly to a different episode on a different
pipeline — resolves to the same effect. Cognition may be replayed; an external action
may not be repeated.

Review 2026-09-16 reproduced six store-level defects. Each is pinned below under
"store integrity": failed writes leaving a dirty cache, returned rows aliasing store
internals, structurally corrupt rows reading as no mail, deadlines unenforced at
claim and acknowledgement, first-level-only payload validation, and a batch
acknowledgement API that could not acknowledge a normal multi-row claim.
"""

import json
from types import SimpleNamespace

import pytest

from global_loop import intersections as mail
from pressure_loop import cognition_queue as cq

OPERATOR = dict(thread_id="t-1", delivery_point="omega_reason", purpose="operator_instruction",
                requirement="required", source_actor="operator", source_episode_id="ep-op")
EVIDENCE = dict(thread_id="t-1", delivery_point="omega_reason", purpose="lane_evidence",
                requirement="preferred", source_actor="browser", source_episode_id="ep-br")


@pytest.fixture(autouse=True)
def _isolated_mail(monkeypatch, tmp_path):
    monkeypatch.setattr(mail, "mail_path", lambda: tmp_path / "cross_lane_mail.json")
    mail.clear_mail()
    yield
    mail.clear_mail()


def _pin(**overrides):
    row = dict(OPERATOR, payload={"text": "use sqlite"})
    row.update(overrides)
    return mail.pin_mail(**row)


def _claim(episode_id="ep-1", **overrides):
    args = dict(delivery_point="omega_reason", lane="chat", thread_id="t-1",
                episode_id=episode_id, lease_s=60)
    args.update(overrides)
    return mail.claim_mail(**args)


# ── identity is host-owned ────────────────────────────────────────────────────

def test_the_host_mints_every_identifier():
    pinned = _pin()

    assert pinned["mail_id"] and pinned["effect_id"]
    assert pinned["status"] == "ready"
    assert pinned["generation"] == 0
    assert pinned["lease_id"] == "" and pinned["claimed_by"] == ""


def test_arrival_order_comes_from_the_one_global_sequence():
    """Mail reuses the queue's sequence; a second counter could disagree with it."""
    first = _pin()
    second = _pin()

    assert second["arrival_seq"] > first["arrival_seq"]
    assert cq.next_arrival_seq() > second["arrival_seq"]


# ── addressing ────────────────────────────────────────────────────────────────

def test_a_claim_only_sees_mail_addressed_to_it():
    _pin(thread_id="t-1")
    _pin(thread_id="t-other")
    _pin(thread_id="t-1", delivery_point="lane_entry", target_lane="agent")

    claimed = _claim()

    assert [row["thread_id"] for row in claimed] == ["t-1"]
    assert all(row["delivery_point"] == "omega_reason" for row in claimed)


def test_lane_entry_mail_is_addressed_to_one_lane():
    _pin(delivery_point="lane_entry", target_lane="agent", purpose="omega_objective")

    assert mail.claim_mail(delivery_point="lane_entry", lane="browser", thread_id="t-1",
                           episode_id="ep-1", lease_s=60) == []
    assert len(mail.claim_mail(delivery_point="lane_entry", lane="agent", thread_id="t-1",
                               episode_id="ep-1", lease_s=60)) == 1


def test_mail_purpose_is_descriptive_and_any_lane_can_receive_it():
    pinned = _pin(delivery_point="lane_entry", target_lane="window",
                  purpose="operator_reflection_note", source_actor="browser_worker",
                  requirement="optional")

    claimed = mail.claim_mail(delivery_point="lane_entry", lane="window", thread_id="t-1",
                              episode_id="ep-window", lease_s=60)

    assert [row["mail_id"] for row in claimed] == [pinned["mail_id"]]
    assert claimed[0]["purpose"] == "operator_reflection_note"
    assert claimed[0]["source_actor"] == "browser_worker"


def test_a_job_constraint_is_honoured():
    _pin(target_job_id="job-7")

    assert _claim(job_id="job-9") == []
    assert len(_claim(job_id="job-7")) == 1


def test_lane_entry_can_be_claimed_by_explicit_mail_ids():
    """A continuation claims the exact mail its successor metadata names."""
    wanted = _pin(delivery_point="lane_entry", target_lane="agent", purpose="omega_objective")
    _pin(delivery_point="lane_entry", target_lane="agent", purpose="lane_evidence")

    claimed = mail.claim_mail(delivery_point="lane_entry", lane="agent", thread_id="t-1",
                              episode_id="ep-1", lease_s=60, mail_ids=[wanted["mail_id"]])

    assert [row["mail_id"] for row in claimed] == [wanted["mail_id"]]


# ── deterministic selection order ─────────────────────────────────────────────

def test_selection_is_first_come_first_served_across_labels():
    stop = _pin(purpose="stop_job", payload={"reason": "after the tests pass"})
    evidence = mail.pin_mail(**dict(EVIDENCE, payload={"text": "a page said x"}))
    optional = mail.pin_mail(**dict(EVIDENCE, requirement="optional", payload={"text": "aside"}))
    instruction = _pin()

    claimed = _claim()

    assert [row["mail_id"] for row in claimed] == [
        stop["mail_id"], evidence["mail_id"], optional["mail_id"], instruction["mail_id"]]


def test_mail_is_fifo_by_arrival_without_priority_classes():
    first = _pin(requirement="optional", purpose="future_label")
    second = mail.pin_mail(**dict(EVIDENCE, requirement="required", payload={"text": "later"}))

    claimed = _claim()

    assert [row["mail_id"] for row in claimed] == [first["mail_id"], second["mail_id"]]


# ── lease, not claim-on-read ──────────────────────────────────────────────────

def test_claiming_leases_rather_than_consuming():
    _pin()

    claimed = _claim()

    assert claimed[0]["status"] == "leased"
    assert claimed[0]["lease_id"] and claimed[0]["claimed_by"] == "ep-1"
    assert claimed[0]["generation"] == 1, "a status transition bumps the generation"
    assert _claim(episode_id="ep-2") == [], "not offered twice while the lease holds"


def test_an_expired_lease_returns_the_mail_to_ready():
    """A crash before reasoning must not lose an operator instruction."""
    _pin()
    _claim(lease_s=-1)

    assert mail.release_expired_leases() == 1
    assert [row["claimed_by"] for row in _claim(episode_id="ep-2")] == ["ep-2"]


def test_redelivery_keeps_the_same_effect_identity():
    """Effect identity is minted at PIN time, so a different episode resolves the same
    effect: cognition may be replayed, an external action may not be repeated."""
    pinned = _pin()
    first = _claim(lease_s=-1)
    mail.release_expired_leases()
    second = _claim(episode_id="ep-2")

    assert first[0]["effect_id"] == second[0]["effect_id"] == pinned["effect_id"]


# ── acknowledgement ───────────────────────────────────────────────────────────

def test_acknowledgement_is_terminal():
    _pin()
    claimed = _claim()

    assert mail.acknowledge_mail(claimed, episode_id="ep-1") == 1
    assert mail.pending_mail() == []


def test_acknowledgement_deletes_the_row_it_does_not_mark_it_acknowledged():
    """The lifecycle ends by removing the row. Durable honour lives on the binding."""
    pinned = _pin()
    claimed = _claim()

    assert mail.acknowledge_mail(claimed, episode_id="ep-1") == 1
    assert mail.all_mail() == []
    assert "acknowledged" not in mail.STATUSES
    assert pinned["mail_id"] not in {row["mail_id"] for row in mail.pending_mail()}


def test_a_multi_row_claim_can_be_acknowledged():
    """Every row is leased under its OWN id, so one lease id could not cover a claim."""
    _pin()
    mail.pin_mail(**dict(EVIDENCE, payload={"text": "a page said x"}))

    claimed = _claim()

    assert len(claimed) == 2
    assert claimed[0]["lease_id"] != claimed[1]["lease_id"]
    assert mail.acknowledge_mail(claimed, episode_id="ep-1") == 2
    assert mail.pending_mail() == []


@pytest.mark.parametrize("bad", ["lease", "generation", "episode"])
def test_acknowledgement_validates_the_lease_and_the_generation(bad):
    _pin()
    claimed = _claim()[0]
    if bad == "lease":
        claimed["lease_id"] = "not-the-lease"
    elif bad == "generation":
        claimed["generation"] = claimed["generation"] - 1

    with pytest.raises(ValueError):
        mail.acknowledge_mail(
            [claimed], episode_id="ep-someone-else" if bad == "episode" else "ep-1")


def test_acknowledging_mail_that_was_never_leased_is_an_error():
    pinned = _pin()

    with pytest.raises(ValueError):
        mail.acknowledge_mail([dict(pinned, lease_id="whatever")], episode_id="ep-1")


# ── budget: nothing is silently truncated ─────────────────────────────────────

def test_preferred_mail_over_budget_stays_ready_for_a_later_episode():
    _pin()
    mail.pin_mail(**dict(EVIDENCE, payload={"text": "x" * 4000}))

    claimed = _claim(payload_budget=200)

    assert [row["purpose"] for row in claimed] == ["operator_instruction"]
    assert [row["purpose"] for row in mail.pending_mail()] == ["lane_evidence"]


def test_required_mail_over_budget_stays_ready_rather_than_truncated():
    _pin(payload={"text": "y" * 4000})

    assert _claim(payload_budget=200) == []
    assert [row["status"] for row in mail.pending_mail()] == ["ready"]


# ── payload authority ─────────────────────────────────────────────────────────

def test_a_payload_may_name_an_artifact():
    """Artifact identity is legal: a continuation cannot be specified without it."""
    pinned = _pin(payload={"objective": "add retries", "path": "auto_loop/execute/run.py",
                           "job_id": "job-7", "url": "https://example.test/doc"})

    assert pinned["payload"]["path"] == "auto_loop/execute/run.py"


@pytest.mark.parametrize("payload", [
    {"tool": "run_tests"},
    {"command": "pytest -q"},
    {"argv": ["rm", "-rf", "/"]},
    {"shell": "bash -c ls"},
    {"execution": {"tool": "run_command", "argv": ["ls"]}},
    {"plan": {"steps": [{"cmd": "pytest -q"}]}},
])
def test_a_payload_may_never_carry_executable_authority_at_any_depth(payload):
    """The lane chooses its own tools; mail says what and why, never how to execute."""
    with pytest.raises(ValueError):
        _pin(payload=payload)


# ── store integrity (review 2026-09-16) ───────────────────────────────────────

def test_a_failed_pin_is_not_claimable():
    """Gap 1: mutating the cache before the write left a failed pin live in memory."""
    def explode(_state):
        raise OSError("disk full")

    real_write, mail._write = mail._write, explode
    try:
        with pytest.raises(OSError):
            _pin()
    finally:
        mail._write = real_write

    assert mail.pending_mail() == []


def test_a_failed_acknowledgement_does_not_destroy_the_row():
    """Gap 1, worst case: the rows were popped from cache, then a later successful
    write deleted them from disk although the acknowledgement had raised."""
    _pin()
    claimed = _claim()
    failing = {"on": True}
    real_write = mail._write

    def maybe(state):
        if failing["on"]:
            raise OSError("disk full")
        real_write(state)

    mail._write = maybe
    try:
        with pytest.raises(OSError):
            mail.acknowledge_mail(claimed, episode_id="ep-1")
        failing["on"] = False
        # The row survived, so the same receipt still acknowledges it.
        assert mail.acknowledge_mail(claimed, episode_id="ep-1") == 1
    finally:
        mail._write = real_write


def test_a_returned_row_cannot_reach_back_into_the_store():
    """Gap 2: shallow copies let a caller add a forbidden command to stored mail."""
    pinned = _pin()
    pinned["payload"]["text"] = "mutated"
    pinned["payload"]["command"] = "rm -rf /"
    pinned["provenance"].append("forged")

    stored = mail.pending_mail()[0]

    assert stored["payload"] == {"text": "use sqlite"}
    assert stored["provenance"] == []


def test_an_incoming_payload_is_copied_not_aliased():
    """Gap 2: nested input objects stayed aliased to the caller's dict."""
    payload = {"nested": {"text": "original"}}
    mail.pin_mail(**dict(OPERATOR, payload=payload))
    payload["nested"]["text"] = "mutated"

    assert mail.pending_mail()[0]["payload"]["nested"]["text"] == "original"


@pytest.mark.parametrize("corrupt", ["status", "missing_field", "timestamp", "wrong_id", "lease"])
def test_a_structurally_invalid_row_is_explicitly_unavailable(tmp_path, corrupt):
    """Gap 3: envelope-only validation let a broken row read as an empty store."""
    _pin()
    path = tmp_path / "cross_lane_mail.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    mail_id, row = next(iter(body["mail"].items()))
    if corrupt == "status":
        row["status"] = "readi"
    elif corrupt == "missing_field":
        row.pop("effect_id")
    elif corrupt == "timestamp":
        row["created_at"] = "not-a-time"
    elif corrupt == "wrong_id":
        body["mail"] = {"some-other-id": row}
    else:
        row["lease_id"] = "dangling-lease"
    path.write_text(json.dumps(body), encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(ValueError):
        mail.pending_mail()


def test_acknowledgement_refuses_an_expired_lease():
    """Gap 4: an expired lease authorises nothing — the mail may be re-leased."""
    _pin()
    claimed = _claim(lease_s=-1)

    with pytest.raises(ValueError):
        mail.acknowledge_mail(claimed, episode_id="ep-1")


# ── durability and failure-soft reads ─────────────────────────────────────────

def test_mail_survives_a_reload():
    pinned = _pin()
    mail.reset_cache_for_tests()

    assert [row["mail_id"] for row in mail.pending_mail()] == [pinned["mail_id"]]


def test_old_unacknowledged_mail_survives_reload_and_has_no_ttl(tmp_path):
    pinned = _pin()
    path = tmp_path / "cross_lane_mail.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["mail"][pinned["mail_id"]]["created_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(body), encoding="utf-8")
    mail.reset_cache_for_tests()

    [restored] = _claim()

    assert restored["mail_id"] == pinned["mail_id"]
    assert "ttl_s" not in restored


def test_an_absent_store_reads_as_no_mail():
    mail.reset_cache_for_tests()

    assert mail.pending_mail() == []


def test_a_corrupt_store_is_explicitly_unavailable(tmp_path):
    """Absent is normal; corrupt is reported, never silently treated as empty."""
    (tmp_path / "cross_lane_mail.json").write_text("{not json", encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(ValueError):
        mail.pending_mail()


def test_the_store_is_written_atomically_and_sorted(tmp_path):
    _pin()
    body = json.loads((tmp_path / "cross_lane_mail.json").read_text(encoding="utf-8"))

    assert isinstance(body, dict) and body["mail"]
    assert not list(tmp_path.glob(".cross_lane_mail.json.*.tmp")), "no temp file survives"


# ── deadlines: sampled under the lock, and finite (review 2026-09-16) ─────────

def test_deadlines_are_sampled_after_the_lock_is_held(monkeypatch):
    """A timestamp taken BEFORE the lock goes stale while contending for it.

    The clock advances during the candidate load, standing in for a real lock wait: if
    ``now`` were sampled first, this mail would still look live and be claimed.
    """
    _pin()
    base = mail.time.time()
    clock = {"now": base}
    monkeypatch.setattr(mail, "time", SimpleNamespace(time=lambda: clock["now"]))
    claimed = _claim(lease_s=5)
    clock["now"] = base
    real_candidate = mail._candidate

    def contended():
        clock["now"] = base + 10
        return real_candidate()

    monkeypatch.setattr(mail, "_candidate", contended)

    assert _claim(episode_id="ep-2")
    assert claimed[0]["mail_id"] == mail.all_mail()[0]["mail_id"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_lease_is_refused(bad):
    _pin()

    with pytest.raises(ValueError):
        _claim(lease_s=bad)


def test_a_persisted_non_finite_lease_is_explicitly_unavailable(tmp_path):
    """NaN round-trips through JSON and compares false, so such a lease never releases."""
    _pin()
    path = tmp_path / "cross_lane_mail.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    row = next(iter(body["mail"].values()))
    row["lease_until"] = float("nan")
    path.write_text(json.dumps(body), encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(ValueError):
        mail.pending_mail()


@pytest.mark.parametrize("payload", ["just a string", 42, {"blob": "z" * 20000}])
def test_a_persisted_payload_must_meet_the_pin_contract(tmp_path, payload):
    """Reload used only the recursive key check, so a non-object or oversized payload
    loaded successfully. Pinning and recovery share one contract."""
    _pin()
    path = tmp_path / "cross_lane_mail.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    row = next(iter(body["mail"].values()))
    row["payload"] = payload
    path.write_text(json.dumps(body), encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(ValueError):
        mail.pending_mail()


def test_old_mail_can_be_acknowledged_after_reload(tmp_path):
    pinned = _pin()
    path = tmp_path / "cross_lane_mail.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["mail"][pinned["mail_id"]]["created_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(body), encoding="utf-8")
    mail.reset_cache_for_tests()

    claimed = _claim(lease_s=60)

    assert mail.acknowledge_mail(claimed, episode_id="ep-1") == 1


def test_version_three_expired_row_migrates_back_to_ready_without_ttl(tmp_path):
    pinned = _pin()
    path = tmp_path / "cross_lane_mail.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["version"] = 3
    body["mail"][pinned["mail_id"]].update(
        status="expired", created_at="2000-01-01T00:00:00+00:00", ttl_s=1.0)
    path.write_text(json.dumps(body), encoding="utf-8")
    mail.reset_cache_for_tests()

    [restored] = mail.pending_mail()

    assert restored["status"] == "ready"
    assert "ttl_s" not in restored
    assert [row["mail_id"] for row in _claim()] == [pinned["mail_id"]]


# ── T6 support: cutoff and release ────────────────────────────────────────────

def test_the_high_water_mark_of_an_empty_store_is_zero():
    assert mail.high_water_seq() == 0


def test_a_cutoff_excludes_mail_pinned_after_it():
    """Membership is decided once: later mail belongs to a later episode."""
    early = _pin()
    cutoff = mail.high_water_seq()
    late = _pin()

    claimed = _claim(cutoff_seq=cutoff)

    assert [row["mail_id"] for row in claimed] == [early["mail_id"]]
    assert [row["mail_id"] for row in mail.pending_mail()] == [late["mail_id"]]


def test_reading_the_high_water_mark_consumes_no_sequence_number():
    _pin()
    before = cq.next_arrival_seq()
    mail.high_water_seq()
    mail.high_water_seq()

    assert cq.next_arrival_seq() == before + 1


def test_releasing_returns_owned_mail_to_ready_immediately():
    _pin()
    claimed = _claim()

    assert mail.release_mail(claimed, episode_id="ep-1") == 1
    assert [row["claimed_by"] for row in _claim(episode_id="ep-2")] == ["ep-2"]


def test_a_stale_release_never_takes_mail_another_episode_holds():
    """Release is lenient, but ownership is still per row."""
    _pin()
    stale = _claim(lease_s=-1)
    mail.release_expired_leases()
    retaken = _claim(episode_id="ep-2")

    assert mail.release_mail(stale, episode_id="ep-1") == 0
    assert mail.acknowledge_mail(retaken, episode_id="ep-2") == 1


def test_max_rows_is_a_selection_limit_that_leaves_the_rest_ready():
    for index in range(3):
        _pin(payload={"text": f"m{index}"})

    claimed = _claim(max_rows=2)

    assert len(claimed) == 2
    assert [row["status"] for row in mail.pending_mail()] == ["ready"]


def test_admit_decides_membership_and_refused_mail_stays_ready():
    small = _pin(payload={"text": "a"})
    big = _pin(payload={"text": "b" * 100})

    claimed = _claim(admit=lambda rows: sum(len(r["payload"]["text"]) for r in rows) <= 10)

    assert [row["mail_id"] for row in claimed] == [small["mail_id"]]
    assert [row["mail_id"] for row in mail.pending_mail()] == [big["mail_id"]]


def test_admit_receives_snapshots_and_cannot_reach_into_the_store():
    _pin()

    def meddle(rows):
        rows[-1]["payload"]["text"] = "tampered"
        return True

    claimed = _claim(admit=meddle)
    mail.release_mail(claimed, episode_id="ep-1")

    assert mail.pending_mail()[0]["payload"] == {"text": "use sqlite"}


def test_owned_mail_reports_only_what_this_episode_still_holds():
    _pin()
    claimed = _claim()

    assert len(mail.owned_mail(claimed, episode_id="ep-1")) == 1
    assert mail.owned_mail(claimed, episode_id="ep-2") == []


def test_owned_mail_excludes_an_expired_or_retaken_lease():
    _pin()
    stale = _claim(lease_s=-1)

    assert mail.owned_mail(stale, episode_id="ep-1") == [], "an expired lease holds nothing"
    mail.release_expired_leases()
    _claim(episode_id="ep-2")
    assert mail.owned_mail(stale, episode_id="ep-1") == []


def test_admit_cannot_mutate_an_earlier_accepted_row():
    """Earlier accepted rows are the very dicts returned to the caller."""
    _pin(payload={"text": "first"})
    _pin(payload={"text": "second"})
    calls = {"n": 0}

    def meddle(rows):
        calls["n"] += 1
        if len(rows) == 2:
            rows[0]["payload"]["text"] = "tampered"
            rows[0]["lease_id"] = "forged"
        return True

    claimed = _claim(admit=meddle)

    assert calls["n"] == 2, "the second call is the one that sees an earlier row"
    assert claimed[0]["payload"] == {"text": "first"}
    assert mail.acknowledge_mail(claimed, episode_id="ep-1") == 2, "receipts still valid"


def test_refusals_are_returned_with_the_claim():
    small = _pin(payload={"text": "a"})
    big = _pin(payload={"text": "b" * 100})

    claimed, refused = mail.claim_mail_detailed(
        delivery_point="omega_reason", lane="chat", thread_id="t-1", episode_id="ep-1",
        lease_s=60, admit=lambda rows: sum(len(r["payload"]["text"]) for r in rows) <= 10)

    assert [row["mail_id"] for row in claimed] == [small["mail_id"]]
    assert [(row["mail_id"], row["requirement"]) for row in refused] == [(big["mail_id"], "required")]
    assert refused[0]["reason"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_payload_value_is_refused_at_any_depth(bad):
    """NaN and Infinity are not JSON; the writer would refuse what the loader allowed."""
    with pytest.raises(ValueError):
        _pin(payload={"metrics": {"score": bad}})
    with pytest.raises(ValueError):
        _pin(payload={"samples": [1.0, bad]})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_persisted_non_finite_payload_value_is_explicitly_unavailable(tmp_path, bad):
    """It read as AVAILABLE mail and then failed the next claim mid-persist, which is
    the inverse of the corrupt-store guarantee."""
    _pin()
    path = tmp_path / "cross_lane_mail.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    row = next(iter(body["mail"].values()))
    row["payload"] = {"metrics": {"score": bad}, "samples": [1.0, bad]}
    # json.dumps emits the non-standard NaN/Infinity literals, and json.loads takes
    # them back — which is exactly how such a row reached the store unnoticed.
    path.write_text(json.dumps(body), encoding="utf-8")
    mail.reset_cache_for_tests()

    with pytest.raises(ValueError):
        mail.pending_mail()
