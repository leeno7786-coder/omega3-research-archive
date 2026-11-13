"""AUTO's own evidence about what it actually did (WO-05).

Thin and deterministic, like ``memory/cognition_capture.py`` is for the planning phases. It
records nothing it was not given and infers nothing from content: the host names the tool, the
attempt and the outcome, and this turns them into owner-bound events through the accepted WO-03
capture API. No second store, no second writer, no reasoning archive.

Two contracts carry the weight here.

**Attempt identity comes from the invocation.** Each real attempt mints its own token and its
result is recorded under the matching one, so an attempt and its outcome correlate while two
attempts stay two occurrences. This is the lesson WO-04A closure r3 landed for phase failures,
applied to tools: two genuine attempts can fail with identical text, so identity must never be
derived from the text.

**Effect certainty is not a courtesy field.** ``run_operator_tool`` refuses on eight distinct
paths before a handler ever runs, and returns the same ``ToolResult(False, error=...)`` for all
of them as for a handler that raised mid-write. Those are not the same fact. A refusal proves
nothing happened; a crash proves nothing either way. Collapsing them tells a reader deciding
whether to retry exactly the thing it must not be told.
"""

from __future__ import annotations

from typing import Any

from memory.episode_events import capture_context, record_event

#: How much of the world moved. ``partial_effect`` is for a handler that reports having done
#: some of its work; ``unknown_effect`` is for one that cannot say. Neither collapses into
#: ``no_effect``, which is a positive claim that the world is untouched.
EFFECT_STATES: tuple[str, ...] = (
    "applied",
    "no_effect",
    "partial_effect",
    "unknown_effect",
)

#: Every refusal path of ``auto_loop.execute.brain_skill_executor.run_operator_tool``, plus the
#: handler crash. Read off that function's actual early returns rather than imagined, so a
#: branch added there without a kind here is a test failure and not a silent generic label.
TOOL_ERROR_KINDS: tuple[str, ...] = (
    "arguments_unparseable",
    "unknown_tool",
    "not_in_lane",
    "workspace_unavailable",
    "command_refused",
    "permission_required",
    "execution_ceiling",
    "budget_exhausted",
    "cancelled",
    "handler_raised",
    "tool_reported_failure",
    # The agent exchange's own refusal of a mutating call made before any task plan.
    "plan_required",
    # The browser lane's refusals, each raised BEFORE the action reaches the browser.
    "gate_refused",
    "arguments_invalid",
    "browser_unavailable",
)

#: Which refusals prove the world did not move. Everything above the handler call in
#: ``run_operator_tool`` returns before the tool runs, so the claim is provable rather than
#: optimistic. ``handler_raised`` is the one kind that cannot make the claim.
_EFFECT_BY_ERROR_KIND: dict[str, str] = {
    "arguments_unparseable": "no_effect",
    "unknown_tool": "no_effect",
    "not_in_lane": "no_effect",
    "workspace_unavailable": "no_effect",
    "command_refused": "no_effect",
    "permission_required": "no_effect",
    "execution_ceiling": "no_effect",
    "budget_exhausted": "no_effect",
    # A cancellation can land after the handler has started, so it is not a no-effect claim.
    "cancelled": "unknown_effect",
    "handler_raised": "unknown_effect",
    # The handler ran and reported its own failure. It may have validated its input and done
    # nothing, or written half a file first; this layer cannot tell, and guessing no_effect
    # would invite a double-apply on retry.
    "tool_reported_failure": "unknown_effect",
    # auto_loop.execute.run: refused by the exchange before run_operator_tool was reached.
    "plan_required": "no_effect",
    # browser_agent.agent_loop: the host's delivery gate refused the action, its arguments
    # failed validation, or ``_act`` found no task tab / no focused field — all before any
    # controller call, so the page provably did not move.
    "gate_refused": "no_effect",
    "arguments_invalid": "no_effect",
    "browser_unavailable": "no_effect",
}

TOOL_ATTEMPT = "auto.tool_attempt"
TOOL_RESULT = "auto.tool_result"


class UnmappedToolErrorKind(Exception):
    """An error kind with no effect mapping.

    Raised rather than defaulted. Law section 2.1: an unrecognised refusal must not quietly
    become the safest-sounding answer, because "nothing happened" is a claim a reader will act
    on and a wrong one invites a double-apply.
    """


def effect_for_error_kind(error_kind: str) -> str:
    """The effect certainty a given refusal kind proves, or a loud failure."""
    kind = str(error_kind or "").strip()
    try:
        return _EFFECT_BY_ERROR_KIND[kind]
    except KeyError:
        raise UnmappedToolErrorKind(
            f"tool error kind {kind!r} has no declared effect certainty; map it in "
            "memory/auto_evidence.py rather than assuming the world did not move"
        ) from None


def new_attempt_token() -> str:
    """A fresh identity for ONE real attempt at a tool.

    Host bookkeeping, deliberately not derived from the tool name, its arguments or its error
    text: two genuine attempts can be identical in all three, and merging them would report a
    retry as a replay.
    """
    import uuid

    return uuid.uuid4().hex


def _text(value: Any) -> str:
    return str(value or "").strip()


def _episode_id(episode: Any) -> str:
    return _text(getattr(episode, "episode_id", "") or "")


def _host_ids(episode: Any) -> dict[str, Any]:
    """The host's own identities a capture context may carry (an ExecutionContext has them).

    Only what the host set: an absent thread still roots at the episode inside ``record_event``,
    rather than being guessed here.
    """
    ids: dict[str, Any] = {}
    for name in ("thread_id", "job_id"):
        value = _text(getattr(episode, name, "") or "")
        if value:
            ids[name] = value
    arrival = getattr(episode, "arrival_seq", 0)
    if isinstance(arrival, int) and not isinstance(arrival, bool) and arrival > 0:
        ids["arrival_seq"] = arrival
    return ids


def capture_tool_attempt(
    episode: Any,
    *,
    tool: str,
    attempt_token: str,
    arguments_digest: str = "",
    lane: str = "",
) -> Any:
    """Record that an attempt was MADE, before its outcome is known.

    Written first and on its own so an attempt that never returns — a hang, a kill, a crashed
    process — is still on record. An attempt captured only alongside its result cannot describe
    the case where there is no result.
    """
    episode_id = _episode_id(episode)
    token = _text(attempt_token) or new_attempt_token()
    payload = {
        "episode_id": episode_id,
        "tool": _text(tool),
        "attempt_token": token,
        "lane": _text(lane),
        # A digest, not the arguments: the arguments can carry operator content and secrets,
        # which step 5 governs. The digest proves which arguments without retaining them.
        "arguments_digest": _text(arguments_digest),
    }
    with capture_context(attempt_id=token, **_host_ids(episode)):
        return record_event(
            episode_id,
            operation_id=f"tool-attempt:{episode_id}:{token}",
            phase="auto",
            event_kind=TOOL_ATTEMPT,
            payload=payload,
        )


def capture_tool_result(
    episode: Any,
    *,
    tool: str,
    attempt_token: str,
    ok: bool,
    error_kind: str = "",
    error_detail: str = "",
    effect: str = "",
) -> Any:
    """Record the outcome of ONE attempt, under the matching token.

    ``effect`` is derived from ``error_kind`` when not supplied, so a caller cannot forget it
    and leave the certainty blank. A supplied value wins only where the handler genuinely knows
    better than the mapping — a handler reporting ``partial_effect`` about its own work.
    """
    episode_id = _episode_id(episode)
    token = _text(attempt_token)
    if not token:
        raise ValueError(
            "a tool result requires the token of the attempt it belongs to; without it the "
            "outcome cannot be correlated with the attempt that produced it"
        )
    kind = _text(error_kind)
    if ok:
        resolved_effect = _text(effect) or "applied"
    else:
        resolved_effect = _text(effect) or effect_for_error_kind(kind)
    if resolved_effect not in EFFECT_STATES:
        raise ValueError(
            f"effect {resolved_effect!r} is not one of {EFFECT_STATES}; an undeclared "
            "certainty cannot be reasoned about by a reader deciding whether to retry"
        )
    payload = {
        "episode_id": episode_id,
        "tool": _text(tool),
        "attempt_token": token,
        "ok": bool(ok),
        "error_kind": kind,
        # Step 5: the detail quotes whatever the tool reported, which routinely includes the
        # request that failed. Redacted HERE, before it can become an event payload.
        "error_detail": safe_detail(error_detail),
        "effect": resolved_effect,
    }
    with capture_context(attempt_id=token, **_host_ids(episode)):
        return record_event(
            episode_id,
            operation_id=f"tool-result:{episode_id}:{token}",
            phase="auto",
            event_kind=TOOL_RESULT,
            payload=payload,
        )


class AttemptRecorder:
    """Attempt/result capture for a lane that does not dispatch through ``run_operator_tool``.

    The browser loop and the plugin step runner hold no ``ExecutionContext``; the host builds one
    of these for the episode it owns and hands it down, so each action is recorded under a fresh
    token BEFORE it runs and its result under the same token after.
    """

    def __init__(self, episode_id: str, *, lane: str) -> None:
        self.episode_id = str(episode_id)
        self.lane = str(lane or "")

    def attempt(self, tool: str, *, arguments_digest: str = "") -> str:
        token = new_attempt_token()
        capture_tool_attempt(self, tool=tool, attempt_token=token, lane=self.lane,
                             arguments_digest=arguments_digest)
        return token

    def result(self, token: str, tool: str, *, ok: bool, error_kind: str = "",
               error_detail: str = "", effect: str = "") -> Any:
        return capture_tool_result(self, tool=tool, attempt_token=token, ok=ok,
                                   error_kind=error_kind, error_detail=error_detail, effect=effect)


def attempt_recorder(episode_id: str, *, lane: str) -> "AttemptRecorder | None":
    """A recorder for *episode_id*, or None when this thread owns no episode at all.

    The same rule as ``run_operator_tool``: a thread owning nothing records nothing (there is no
    episode to attribute the action to), while a thread bound to a DIFFERENT episode still gets
    a recorder, and ``record_event`` refuses it loudly naming both episodes.
    """
    wanted = _text(episode_id)
    if not wanted:
        return None
    from memory import episode_sram

    if episode_sram.bound_episode_id() is None:
        return None
    return AttemptRecorder(wanted, lane=lane)


# ── Step 4: artifacts, verification and incidental discoveries ────────────────

#: What happened to an artifact. ``rolled_back`` is separate from ``changed`` deliberately: an
#: undo that reads as an ordinary edit turns a retreat into apparent progress, which is exactly
#: the misreading the window trial found Omega making about its own build.
ARTIFACT_CHANGE_KINDS: tuple[str, ...] = ("created", "changed", "rolled_back", "deleted")

ARTIFACT_REVISION = "auto.artifact_revision"
VERIFICATION_RESULT = "auto.verification_result"
INCIDENTAL_OBSERVATION = "auto.incidental_observation"


def artifact_revision_receipt(
    *, path: str, revision: int, content_sha256: str, change_kind: str
) -> dict[str, Any]:
    """One artifact at one revision, with the bytes that made it.

    The content digest is the point. ``ExecutionReport.files_changed`` carries bare paths, so
    "app.py changed" cannot be compared with anything: two different builds of the same file are
    the same sentence. A digest makes a revision identifiable, which is what lets a verification
    below be shown to belong to it — or not.
    """
    kind = _text(change_kind)
    if kind not in ARTIFACT_CHANGE_KINDS:
        raise ValueError(
            f"artifact change kind {kind!r} is not one of {ARTIFACT_CHANGE_KINDS}; an "
            "unrecognised change cannot be told apart from a rollback"
        )
    digest = _text(content_sha256)
    if not digest and kind != "deleted":
        raise ValueError(
            f"artifact {path!r} at revision {revision} has no content digest; without it the "
            "revision cannot be distinguished from any other version of the same path"
        )
    return {
        "path": _text(path),
        "revision": int(revision),
        "content_sha256": digest,
        "change_kind": kind,
    }


def verification_receipt(
    *, criterion: str, path: str, revision: int, content_sha256: str, passed: bool
) -> dict[str, Any]:
    """A check, WHAT it asked, and the exact bytes it asked it of.

    ``ExecutionReport.verification`` is a free-form mapping with nothing tying it to a revision,
    so "the tests passed" survives a later edit to the very file that was tested. A reader —
    Omega, deciding whether its pass gate is met — then cannot tell a verified artifact from a
    changed one that merely used to be verified. That is not staleness; it is a false claim
    about the present.
    """
    digest = _text(content_sha256)
    if not digest:
        raise ValueError(
            "a verification result requires the content digest it checked; without it the "
            "result cannot be shown to belong to any particular revision"
        )
    if not _text(criterion):
        raise ValueError(
            "a verification result requires its criterion; 'it passed' without saying what was "
            "asked is not a check anyone can re-run or trust"
        )
    return {
        "criterion": _text(criterion),
        "path": _text(path),
        "revision": int(revision),
        "content_sha256": digest,
        "passed": bool(passed),
    }


def verification_covers(receipt: Any, *, content_sha256: str) -> bool:
    """Is the artifact at ``content_sha256`` verified BY this receipt?

    False whenever the bytes differ, and false for a receipt whose own result was a failure.
    The second case matters as much as the first: this answers "is it verified?", so a recorded
    failure must never be found as coverage and read as evidence of success.
    """
    if not isinstance(receipt, dict):
        return False
    if not bool(receipt.get("passed")):
        return False
    checked = _text(receipt.get("content_sha256"))
    return bool(checked) and checked == _text(content_sha256)


def incidental_observation_receipt(
    *,
    observation: str,
    source: str,
    attributed_to: str,
    evidence_class: str = "external_content",
) -> dict[str, Any]:
    """Something material AUTO noticed that the objective did not ask about.

    Worth recording: the operator's build is often blocked by a thing nobody asked to look for.
    But it is recorded WITH its attribution, because a discovery read out of external output is
    the external source speaking, not the host asserting a fact. Host-only classes are refused
    here rather than downstream: ``schema/episode_event.py`` gates them on producer authority,
    and external content asking for one is the forgery that gate exists to stop.
    """
    from schema.episode_event import EVIDENCE_CLASSES, _HOST_ONLY_CLASSES

    klass = _text(evidence_class)
    attribution = _text(attributed_to)
    if klass not in EVIDENCE_CLASSES:
        raise ValueError(f"evidence class {klass!r} is not one of {EVIDENCE_CLASSES}")
    if klass in _HOST_ONLY_CLASSES and attribution != "host":
        raise ValueError(
            f"evidence class {klass!r} is host-only, and this observation is attributed to "
            f"{attribution!r}; external content cannot assert a host receipt about itself"
        )
    if not _text(observation):
        raise ValueError("an incidental observation with no content is not an observation")
    return {
        "observation": _text(observation),
        "source": _text(source),
        "attributed_to": attribution,
        "evidence_class": klass,
    }


def capture_artifact_revision(episode: Any, *, path: str, revision: int, content_sha256: str,
                              change_kind: str) -> Any:
    """Record one artifact revision as an event."""
    episode_id = _episode_id(episode)
    receipt = artifact_revision_receipt(
        path=path, revision=revision, content_sha256=content_sha256, change_kind=change_kind
    )
    return record_event(
        episode_id,
        operation_id=f"artifact:{episode_id}:{receipt['path']}:{receipt['revision']}",
        phase="auto",
        event_kind=ARTIFACT_REVISION,
        payload={"episode_id": episode_id, **receipt},
    )


def workspace_relative(path: Any, root: Any) -> str:
    """One stable spelling for a workspace file: POSIX, relative to the workspace root.

    Artifact and verification receipts are matched by path, so the writer (which reports an
    absolute path) and a later check must name a file the same way.
    """
    from pathlib import Path

    candidate = Path(str(path))
    base = Path(str(root))
    try:
        return candidate.resolve().relative_to(base.resolve()).as_posix()
    except (OSError, ValueError):
        return candidate.as_posix()


def current_artifact_revisions(episode_id: str) -> dict[str, tuple[int, str]]:
    """The latest recorded revision and digest per path for this episode."""
    from memory.episode_events import read_episode_events

    latest: dict[str, tuple[int, str]] = {}
    for event in read_episode_events(str(episode_id or "")):
        if event.event_kind != ARTIFACT_REVISION:
            continue
        payload = event.payload
        path = str(payload.get("path") or "")
        revision = int(payload.get("revision") or 0)
        if path and revision > latest.get(path, (0, ""))[0]:
            latest[path] = (revision, str(payload.get("content_sha256") or ""))
    return latest


def capture_workspace_revisions(episode: Any, paths: Any, *, root: Any, created: Any = (),
                                rolled_back: bool = False) -> list[Any]:
    """Record the revision each touched workspace file now stands at (WO-05 step 4).

    Called by the real writers after a tool ran, from what is on disk rather than what the tool
    said. A file that no longer exists is ``deleted``; a file whose bytes equal its latest
    recorded revision is not a new revision (an identical rewrite changed nothing); a restore
    from memory is ``rolled_back``, never an ordinary edit. ``created`` names the paths the
    writer knows did not exist before its call.
    """
    import hashlib
    from pathlib import Path

    episode_id = _episode_id(episode)
    latest = current_artifact_revisions(episode_id)
    new_paths = {workspace_relative(item, root) for item in created or ()}
    events: list[Any] = []
    for relative in sorted({workspace_relative(item, root) for item in paths or ()}):
        file = Path(str(root)) / relative
        held_revision, held_digest = latest.get(relative, (0, ""))
        if file.is_file():
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            if held_revision and digest == held_digest:
                continue
            if rolled_back:
                kind = "rolled_back"
            elif relative in new_paths and not held_revision:
                kind = "created"
            else:
                kind = "changed"
        else:
            if held_revision and not held_digest:
                continue  # already recorded as deleted
            digest, kind = "", "deleted"
        events.append(capture_artifact_revision(
            episode, path=relative, revision=held_revision + 1, content_sha256=digest,
            change_kind=kind,
        ))
        latest[relative] = (held_revision + 1, digest)
    return events


def capture_verification(episode: Any, *, criterion: str, path: str, revision: int,
                         content_sha256: str, passed: bool) -> Any:
    """Record one verification result, bound to the revision it checked."""
    episode_id = _episode_id(episode)
    receipt = verification_receipt(
        criterion=criterion, path=path, revision=revision,
        content_sha256=content_sha256, passed=passed,
    )
    # The operation token includes the digest, so verifying a CHANGED revision is a new
    # occurrence rather than a replay of the earlier pass.
    return record_event(
        episode_id,
        operation_id=(
            f"verification:{episode_id}:{receipt['path']}:{receipt['content_sha256'][:16]}"
            f":{content_hash_of_criterion(receipt['criterion'])}"
        ),
        phase="auto",
        event_kind=VERIFICATION_RESULT,
        payload={"episode_id": episode_id, **receipt},
        evidence_class="verification_result",
    )


def content_hash_of_criterion(criterion: str) -> str:
    """A short stable token for a criterion, so two criteria on one revision stay distinct."""
    import hashlib

    return hashlib.sha256(_text(criterion).encode("utf-8")).hexdigest()[:16]


# ── Step 5: nothing sensitive becomes cognitive memory ────────────────────────

#: Response fields that are safe to carry into cognitive evidence. A whitelist, not a
#: blacklist: a provider that adds a new field tomorrow must not have it persisted by default,
#: and ``reasoning`` is exactly the kind of field that arrives unannounced.
_SAFE_INFERENCE_FIELDS: tuple[str, ...] = (
    "request_digest",
    "model",
    "backend",
    "finish_reason",
    "tokens",
    "latency_ms",
    "ok",
    "reason",
)


def safe_detail(text: Any) -> str:
    """An error detail with credential values removed, via the EXISTING treatment.

    Tool failures quote the request that failed, and requests carry credentials, so this is one
    of the likeliest routes for a bearer token to reach cognitive memory.
    """
    from memory.dev_observation_intake import redact_secrets

    return redact_secrets(text)


def fenced_external(text: Any) -> str:
    """External content composed as data, never instructions, via the EXISTING treatment.

    ``harden`` escapes the structural delimiters, so content cannot reproduce its own closing
    fence and speak outside it (AGENTS.md section 6).
    """
    from guardian.external_ingress import harden

    return harden(str(text or ""))


def redaction_receipt(*, source: str, content_sha256: str, reason: str) -> dict[str, Any]:
    """Proof that something was withheld, and why.

    The order is explicit that *a hash alone is not a promise the sensitive source is safe to
    retain*. A receipt carrying only a digest lets a reader conclude nothing was withheld, so
    the reason is required and the source attribution is required: the reason makes the omission
    honest, and the attribution is what lets someone entitled to the real material find it.
    """
    detail = _text(reason)
    if not detail:
        raise ValueError(
            "a redaction receipt requires its reason; a digest with no reason reads as if "
            "nothing was withheld, which is the opposite of what a redaction records"
        )
    attribution = _text(source)
    if not attribution:
        raise ValueError(
            "a redaction receipt requires its source; a bare digest points nowhere and cannot "
            "be traced back by anyone entitled to the original"
        )
    return {
        "source": attribution,
        "content_sha256": _text(content_sha256),
        "reason": detail,
        "redacted": True,
    }


INFERENCE_REQUEST = "auto.inference_request"


def capture_inference(episode: Any, *, operation: str, body: Any, phase: str = "auto") -> Any:
    """Record ONE model call with the identity of the request the transport actually sent.

    Only the whitelisted safe fields of the completion body are kept (``inference_evidence``),
    so the private reasoning trace never becomes an event. A body the transport returned
    without a digest is recorded with the digest's ABSENCE stated, never with an invented one.
    Each call gets its own token: two identical requests are two calls, not a replay.
    """
    episode_id = _episode_id(episode)
    evidence = inference_evidence(body)
    digest = _text(evidence.get("request_digest"))
    payload = {
        "episode_id": episode_id,
        "operation": _text(operation),
        **evidence,
        "request_digest": digest,
        "request_digest_state": "observed" if digest else "not_captured",
    }
    return record_event(
        episode_id,
        operation_id=f"inference:{episode_id}:{new_attempt_token()}",
        phase=phase,
        event_kind=INFERENCE_REQUEST,
        payload=payload,
    )


def inference_evidence(body: Any) -> dict[str, Any]:
    """The safe receipt identity from a completion body — and nothing else.

    Whitelisted rather than filtered, so a field nobody has considered is absent by default.
    ``reasoning`` is the field this exists for: it is genuinely useful live, for telemetry and
    for diagnosing a truncated answer, and it is exactly the "full private reasoning archive"
    the order puts outside its scope. It can also quote whatever the model was handed.
    """
    if not isinstance(body, dict):
        return {}
    evidence: dict[str, Any] = {}
    for field in _SAFE_INFERENCE_FIELDS:
        if field in body and body[field] is not None:
            value = body[field]
            evidence[field] = safe_detail(value) if isinstance(value, str) else value
    return evidence


# ── WO-10: what a recall actually supplied ────────────────────────────────────

INPUT_EXPOSURE = "auto.input_exposure"


def _exposure_identity(row: Any) -> str:
    """A candidate's identity: its memory id, or its content digest when it has none."""
    memory_id = _text(row.get("memory_id"))
    return memory_id or f"content:{_text(row.get('content_sha256'))}"


def capture_input_exposure(episode_id: str, exposure: Any) -> Any:
    """Record what a recall RETRIEVED and what it ADMITTED, in the episode that ran it.

    WO-05 made the input-admission owner (``candidate_hygiene``) report retrieved / admitted /
    excluded-with-reason, but nothing recorded it, so no reader could establish what an episode
    was actually supplied. WO-10 reads exactly this event: only an admitted input establishes
    known exposure (``admitted_from`` / ``read_from``); a record retrieved and then excluded was
    never supplied, and committing beside another episode proves nothing either.

    Identities and digests only: the query is kept as a digest and no candidate content is
    copied. The same rule as ``attempt_recorder``: a thread that owns no episode records nothing
    (there is no episode to attribute the recall to), while a thread bound to a DIFFERENT episode
    is refused loudly by ``record_event``.
    """
    import hashlib

    from memory import episode_sram

    wanted = _text(episode_id)
    if not wanted or episode_sram.bound_episode_id() is None:
        return None
    report = dict(exposure or {})
    retrieved = [
        {
            "memory_id": _exposure_identity(row),
            "content_sha256": _text(row.get("content_sha256")),
            "tool": _text(row.get("tool")),
            "lobe": row.get("lobe"),
            "retrieval_order": row.get("retrieval_order"),
        }
        for row in report.get("retrieved") or ()
    ]
    admitted = [_exposure_identity(row) for row in report.get("admitted") or ()]
    excluded = [
        {"memory_id": _exposure_identity(row), "content_sha256": _text(row.get("content_sha256")),
         "reason": _text(row.get("reason"))}
        for row in report.get("excluded") or ()
    ]
    query = _text(report.get("query"))
    payload = {
        "episode_id": wanted,
        "query_digest": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "candidate_cap": report.get("candidate_cap"),
        "retrieved": retrieved,
        "admitted": admitted,
        "excluded": excluded,
    }
    return record_event(
        wanted,
        operation_id=f"input-exposure:{wanted}:{new_attempt_token()}",
        phase="auto",
        event_kind=INPUT_EXPOSURE,
        payload=payload,
    )


# ── Step 6: what actually left, and what we cannot know ───────────────────────

#: The stages of an outward effect. ``uncertain`` is not a shade of ``failed``: a send that
#: provably never left is safe to retry, while one interrupted mid-flight may already have
#: arrived, and retrying that delivers the message twice — to a real person, in the operator's
#: name. Collapsing them is how a double-send happens.
DELIVERY_STATES: tuple[str, ...] = (
    "proposed",
    "queued",
    "attempted",
    "confirmed",
    "uncertain",
    "failed",
)

DELIVERY_OBSERVATION = "auto.delivery_observation"

#: Only a provider receipt proves arrival, so only this state may claim it.
_DELIVERED_STATES = frozenset({"confirmed"})

#: A retry is safe only where nothing can have left. ``attempted`` is excluded deliberately:
#: an attempt with no outcome yet is not known to have failed.
_RETRYABLE_STATES = frozenset({"proposed", "queued", "failed"})


def delivery_receipt(
    *, target: str, state: str, provider_receipt: str = "", reason: str = ""
) -> dict[str, Any]:
    """One outward effect at one stage, with the retry question already answered.

    ``delivered`` and ``safe_to_retry`` are derived here rather than left to each caller,
    because both are the kind of judgement that gets made differently in two places and then
    disagrees. The rules are narrow on purpose: ``confirmed`` requires a provider receipt, and
    ``uncertain`` requires a reason, because an uncertain state with no cause can never be
    resolved by anyone later.
    """
    stage = _text(state)
    if stage not in DELIVERY_STATES:
        raise ValueError(
            f"delivery state {stage!r} is not one of {DELIVERY_STATES}; an unrecognised stage "
            "cannot answer whether anything left or whether it is safe to send again"
        )
    receipt_id = _text(provider_receipt)
    detail = _text(reason)
    if stage in _DELIVERED_STATES and not receipt_id:
        raise ValueError(
            "a confirmed delivery requires the provider receipt that confirms it; without one "
            "'confirmed' is a claim about the outside world with no evidence behind it"
        )
    if stage in {"uncertain", "failed"} and not detail:
        raise ValueError(
            f"a {stage!r} delivery requires its reason; an uncertain effect with no recorded "
            "cause cannot be resolved later, and a failure with none cannot be judged"
        )
    return {
        "target": _text(target),
        "state": stage,
        "provider_receipt": receipt_id,
        "reason": detail,
        "delivered": stage in _DELIVERED_STATES,
        "safe_to_retry": stage in _RETRYABLE_STATES,
    }


def resolve_delivery(
    receipt: Any, *, provider_receipt: str = "", failed_reason: str = ""
) -> dict[str, Any]:
    """Resolve an uncertain effect with EVIDENCE, in either direction.

    Returns a new receipt; the original is left alone, because a resolution is a later fact
    about the same effect and not a correction of what was true before.

    Resolving downward needs evidence just as much as upward. "It never arrived" licenses a
    resend, so accepting it without provider evidence would let an uncertain send be quietly
    written off and then legitimately delivered twice — the exact outcome ``uncertain`` exists
    to prevent. This function performs no send of any kind.
    """
    if not isinstance(receipt, dict):
        raise ValueError("resolve_delivery needs the delivery receipt it is resolving")
    confirmed_id = _text(provider_receipt)
    failure = _text(failed_reason)
    if confirmed_id and failure:
        raise ValueError(
            "a resolution cannot be both confirmed and failed; one effect has one outcome"
        )
    if confirmed_id:
        return delivery_receipt(
            target=receipt.get("target", ""), state="confirmed", provider_receipt=confirmed_id
        )
    if failure:
        return delivery_receipt(
            target=receipt.get("target", ""), state="failed", reason=failure
        )
    raise ValueError(
        "resolving an uncertain delivery requires provider evidence — a receipt that it "
        "arrived, or a stated provider reason that it did not. Neither may be assumed, "
        "because assuming failure licenses a resend of a message that may already have landed"
    )


def capture_delivery_observation(
    episode: Any, *, target: str, state: str, provider_receipt: str = "", reason: str = ""
) -> Any:
    """Record one delivery stage as an event. Observes only; never sends."""
    episode_id = _episode_id(episode)
    receipt = delivery_receipt(
        target=target, state=state, provider_receipt=provider_receipt, reason=reason
    )
    return record_event(
        episode_id,
        operation_id=f"delivery:{episode_id}:{receipt['target']}:{receipt['state']}",
        phase="auto",
        event_kind=DELIVERY_OBSERVATION,
        payload={"episode_id": episode_id, **receipt},
    )


__all__ = [
    "ARTIFACT_CHANGE_KINDS",
    "AttemptRecorder",
    "INFERENCE_REQUEST",
    "INPUT_EXPOSURE",
    "capture_input_exposure",
    "attempt_recorder",
    "capture_inference",
    "capture_workspace_revisions",
    "current_artifact_revisions",
    "workspace_relative",
    "DELIVERY_OBSERVATION",
    "DELIVERY_STATES",
    "capture_delivery_observation",
    "delivery_receipt",
    "resolve_delivery",
    "fenced_external",
    "inference_evidence",
    "redaction_receipt",
    "safe_detail",
    "ARTIFACT_REVISION",
    "EFFECT_STATES",
    "INCIDENTAL_OBSERVATION",
    "VERIFICATION_RESULT",
    "artifact_revision_receipt",
    "capture_artifact_revision",
    "capture_verification",
    "incidental_observation_receipt",
    "verification_covers",
    "verification_receipt",
    "TOOL_ATTEMPT",
    "TOOL_ERROR_KINDS",
    "TOOL_RESULT",
    "UnmappedToolErrorKind",
    "capture_tool_attempt",
    "capture_tool_result",
    "effect_for_error_kind",
    "new_attempt_token",
]
