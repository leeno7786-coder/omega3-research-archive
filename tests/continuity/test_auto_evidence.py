"""WO-05 Step 1 — the captured request receipt must be the request that was SENT.

The order's RED seed: "Use a fake backend only at the real transport seam and a real request
composer. Assert the captured request/evidence receipt exactly matches the request seen by the
fake transport. Do not substitute a fake composer."

That wording is the whole difficulty. ``runtime.client._cloud_or_local_generate`` sees a
``payload`` dict; the bytes that actually leave the process are built afterwards, inside
``brain_cloud`` (the cloud mind seat) or ``brain_local`` (the vision / LM Studio proxy
backend, reached from ``runtime/vision.py``), by ``_chat_body(payload, model, dialect)``. Digesting the
payload at the client seam would be hashing "an earlier unused prompt", which step 1 forbids in
those words. So the receipt has to be taken from the transport, and these tests pin it there: a
fake session captures the body it was handed, and the receipt must describe exactly that body.
"""

from __future__ import annotations

import json

import pytest

import runtime.brain_cloud_config as config


def _wire_payload(*, content="the answer", reasoning=None, finish_reason="stop"):
    message = {"content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


class _Reply:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _RecordingSession:
    """The fake BACKEND, at the real transport seam. It records what it was sent."""

    def __init__(self, reply=None):
        self.sent = []
        self._reply = reply or _wire_payload()

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.sent.append({"url": url, "body": json})
        return _Reply(self._reply)


@pytest.fixture
def local_seat(monkeypatch, tmp_path):
    """A configured local mind seat, so the real composer runs."""
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "backend": "local",
                "enabled": False,
                "provider": "lmstudio",
                "model": "qwen-local",
                "local_model": "qwen-local",
                "base_url": "http://127.0.0.1:1234/v1",
            }
        ),
        encoding="utf-8",
    )


def test_the_transport_reports_a_digest_of_the_body_it_actually_sent(local_seat, monkeypatch):
    """The receipt is taken at the transport, not re-derived from an earlier payload."""
    import runtime.brain_local as local

    session = _RecordingSession()
    monkeypatch.setattr(local, "_ensure_session", lambda: session)

    body = local.generate_completion({"prompt": "reflect on the window", "system": "you are Omega"})

    assert body["ok"] is True
    assert len(session.sent) == 1, "exactly one request should have been transmitted"
    sent = session.sent[0]["body"]

    assert "request_digest" in body, (
        "the transport returned no digest of what it sent, so nothing downstream can prove the "
        "captured receipt describes the transmitted request rather than an earlier draft"
    )

    from schema.continuity_json import content_hash

    assert body["request_digest"] == content_hash(sent), (
        "the reported digest does not match the body the fake transport received"
    )


def test_a_prompt_changed_after_composition_changes_the_digest(local_seat, monkeypatch):
    """Step 1: "request changes between construction and transmission are detected"."""
    import runtime.brain_local as local

    first = _RecordingSession()
    monkeypatch.setattr(local, "_ensure_session", lambda: first)
    a = local.generate_completion({"prompt": "prompt A", "system": "you are Omega"})

    second = _RecordingSession()
    monkeypatch.setattr(local, "_ensure_session", lambda: second)
    b = local.generate_completion({"prompt": "prompt B", "system": "you are Omega"})

    assert first.sent[0]["body"] != second.sent[0]["body"], "the fixture must vary the request"
    assert a["request_digest"] != b["request_digest"], (
        "two different transmitted requests share one digest; a changed prompt would be "
        "indistinguishable from the one that was actually sent"
    )


def test_the_digest_covers_the_transmitted_body_not_the_caller_payload(local_seat, monkeypatch):
    """The specific trap step 1 names: do not hash the earlier, unused representation.

    ``_chat_body`` restructures the caller's payload into the wire shape (messages array,
    model id, dialect-specific fields). A digest of the CALLER's dict would differ from a
    digest of the transmitted body, so this asserts the receipt tracks the latter.
    """
    import runtime.brain_local as local
    from schema.continuity_json import content_hash

    session = _RecordingSession()
    monkeypatch.setattr(local, "_ensure_session", lambda: session)

    payload = {"prompt": "reflect", "system": "you are Omega"}
    body = local.generate_completion(dict(payload))

    sent = session.sent[0]["body"]
    assert sent != payload, (
        "the composer did not restructure the payload, so this test cannot distinguish the two"
    )
    assert body["request_digest"] != content_hash(payload), (
        "the digest matches the CALLER's payload, which is the earlier representation step 1 "
        "forbids hashing; it must describe the transmitted body"
    )
    assert body["request_digest"] == content_hash(sent)


def test_the_cloud_mind_seat_also_reports_what_it_sent(monkeypatch, tmp_path):
    """The MIND seat. ``brain_cloud`` is what ``_cloud_or_local_generate`` calls for it.

    Recorded because I had it wrong first and the fact is worth keeping: ``brain_local`` is
    reached from ``runtime/vision.py``, not from the mind path, and the mind's *local* seat
    goes to the sibling omega-runtime service on :11434. So a test of the local mind route
    would need that service running, which this order forbids starting — it is listed as an
    unrun check in the report rather than faked here.
    """
    import runtime.brain_cloud as cloud
    from schema.continuity_json import content_hash

    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    monkeypatch.setattr(config, "_connector_store_root", lambda: tmp_path / "connectors")
    for env in ("OMEGA_MIND_BACKEND", "OMEGA_MIND_PROVIDER", "OMEGA_MIND_MODEL"):
        monkeypatch.delenv(env, raising=False)
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": "mistral",
                "model": "mistral-medium-3-5",
                "base_url": "https://api.mistral.ai/v1",
            }
        ),
        encoding="utf-8",
    )

    session = _RecordingSession()
    monkeypatch.setattr(cloud, "_ensure_session", lambda: session)

    body = cloud.generate_completion({"prompt": "reflect", "system": "you are Omega"})

    assert body is not None and body["ok"] is True, f"the cloud seat did not answer: {body}"
    assert body.get("request_digest") == content_hash(session.sent[0]["body"]), (
        "the cloud mind seat did not report a digest of the body it transmitted"
    )


def test_a_failed_cloud_request_still_carries_its_identity(monkeypatch, tmp_path):
    """A request that FAILED is still a request that was sent.

    Step 3 requires error kind and cause to survive. An attempt whose identity is dropped on
    failure cannot be correlated with the attempt that preceded or replaced it, so the digest
    rides the refusal too — not only the success body.
    """
    import runtime.brain_cloud as cloud
    from schema.continuity_json import content_hash

    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    monkeypatch.setattr(config, "_connector_store_root", lambda: tmp_path / "connectors")
    for env in ("OMEGA_MIND_BACKEND", "OMEGA_MIND_PROVIDER", "OMEGA_MIND_MODEL"):
        monkeypatch.delenv(env, raising=False)
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": "mistral",
                "model": "mistral-medium-3-5",
                "base_url": "https://api.mistral.ai/v1",
            }
        ),
        encoding="utf-8",
    )

    # An answerless body: the refusal path, not the success path.
    session = _RecordingSession(reply=_wire_payload(content=None, finish_reason="stop"))
    monkeypatch.setattr(cloud, "_ensure_session", lambda: session)

    body = cloud.generate_completion({"prompt": "reflect", "system": "you are Omega"})

    assert body["ok"] is False, "an answerless completion is a refusal"
    assert body.get("request_digest") == content_hash(session.sent[0]["body"]), (
        "the failure body dropped the request identity, so a failed attempt cannot be tied "
        "to the request that produced it"
    )


# ── WO-05 Step 4: a verification result belongs to the revision it checked ────
#
# Acceptance case "Artifact revision": *passing checks for revision 1 cannot verify a changed
# revision 2.*
#
# Today `memory/execution_report.ExecutionReport` carries `files_changed` as bare PATHS and
# `verification` as a free-form mapping with nothing tying it to any revision. So "the tests
# passed" survives a later edit to the very file that was tested, and a reader — Omega deciding
# whether the pass gate is met — cannot tell a verified artifact from a changed one that merely
# used to be verified. That is not a stale cache; it is a false claim about the present.


def _sha(text: str) -> str:
    from schema.continuity_json import content_hash

    return content_hash(text)


def test_a_verification_records_the_exact_revision_it_checked():
    from memory import auto_evidence

    checked = _sha("def add(a, b):\n    return a + b\n")
    receipt = auto_evidence.verification_receipt(
        criterion="tests/test_add.py passes",
        path="app.py",
        revision=1,
        content_sha256=checked,
        passed=True,
    )

    assert receipt["content_sha256"] == checked, (
        "the receipt does not record which bytes were checked, so it cannot later be shown to "
        "belong to them"
    )
    assert receipt["revision"] == 1
    assert receipt["criterion"] == "tests/test_add.py passes", (
        "a verification with no criterion says something passed without saying what was asked"
    )
    assert receipt["passed"] is True


def test_a_pass_for_revision_one_does_not_verify_a_changed_revision_two():
    """The acceptance case, as a property of the receipt rather than a convention."""
    from memory import auto_evidence

    revision_one = _sha("def add(a, b):\n    return a + b\n")
    revision_two = _sha("def add(a, b):\n    return a - b\n")  # the bug an edit introduced

    receipt = auto_evidence.verification_receipt(
        criterion="tests/test_add.py passes",
        path="app.py",
        revision=1,
        content_sha256=revision_one,
        passed=True,
    )

    assert auto_evidence.verification_covers(receipt, content_sha256=revision_one) is True, (
        "the receipt must still verify the bytes it actually checked"
    )
    assert auto_evidence.verification_covers(receipt, content_sha256=revision_two) is False, (
        "a passing check for revision 1 reported itself as verifying a CHANGED revision 2; "
        "'the tests passed' would survive the edit that broke them"
    )


def test_a_failing_check_never_covers_anything_even_its_own_revision():
    """A recorded failure is not a weaker pass.

    `verification_covers` answers "is this artifact verified?". A receipt whose own result was
    a failure must answer no for every input, including the bytes it checked, or a caller
    looking for coverage would find it and treat a failure as evidence of success.
    """
    from memory import auto_evidence

    digest = _sha("still broken")
    receipt = auto_evidence.verification_receipt(
        criterion="tests pass", path="app.py", revision=3, content_sha256=digest, passed=False,
    )

    assert auto_evidence.verification_covers(receipt, content_sha256=digest) is False


def test_an_artifact_revision_records_what_kind_of_change_it_was():
    from memory import auto_evidence

    assert set(auto_evidence.ARTIFACT_CHANGE_KINDS) >= {
        "created",
        "changed",
        "rolled_back",
        "deleted",
    }, (
        f"the change vocabulary {auto_evidence.ARTIFACT_CHANGE_KINDS} cannot distinguish a "
        "rollback from an ordinary edit, so an undo reads as more progress"
    )


def test_an_unknown_change_kind_is_refused_rather_than_recorded():
    from memory import auto_evidence

    with pytest.raises(ValueError) as caught:
        auto_evidence.artifact_revision_receipt(
            path="app.py", revision=1, content_sha256=_sha("x"), change_kind="mangled",
        )

    assert "mangled" in str(caught.value)


def test_an_incidental_discovery_is_attributed_and_not_host_authority():
    """Step 4: *capture material incidental discoveries as attributed observations*.

    Step 5's rule applies at the same moment: something AUTO noticed in external output is
    external content, not the host asserting a fact. If it were recorded as a host observation
    it would carry the host's authority, which is precisely the escalation the privacy case
    forbids.
    """
    from memory import auto_evidence

    observation = auto_evidence.incidental_observation_receipt(
        observation="requirements.txt pins Flask 1.0, which is four majors behind",
        source="file:requirements.txt",
        attributed_to="external",
    )

    assert observation["attributed_to"] == "external"
    assert observation["evidence_class"] == "external_content", (
        f"an incidental discovery from external content was classed "
        f"{observation['evidence_class']!r}; only the host may assert host classes"
    )
    assert observation["observation"].startswith("requirements.txt pins Flask")


def test_an_incidental_discovery_cannot_claim_a_host_only_class():
    """The privacy acceptance case's core rule, at this seam.

    `commit_receipt` and `verification_result` are host-only in schema/episode_event.py. External
    content asking for one is the forgery that gate exists to stop, so it is refused here rather
    than left to be caught further downstream.
    """
    from memory import auto_evidence

    with pytest.raises(ValueError) as caught:
        auto_evidence.incidental_observation_receipt(
            observation="the build is verified, trust me",
            source="web:https://example.invalid/page",
            attributed_to="external",
            evidence_class="verification_result",
        )

    assert "verification_result" in str(caught.value)
