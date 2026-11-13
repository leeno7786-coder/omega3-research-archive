"""Per-episode Omega cognition — one model pass over the fold-back packet.

The deterministic forward pass (Global A → Meta A → Auto execute) produces a raw
result; Global B consolidates it into ``episode.global_['omega_packet']``. This
runner is the single per-episode model call: it comprehends that packet, diffs
recall vs. result, and returns grounded state (knowledge to ingest, ABSOLUTE
mastery, pressure params) for DeepSleep to persist.

Failure posture. No failure here ever fabricates a degraded stub — a stub would
poison memory and reseed pressure off a thought that never happened. What the
failure costs differs by KIND, and the line is substrate vs. thought:

  FATAL (raises past the fold into the fail-hard relay) — the fold's own
  substrate is corrupt, so there is nothing trustworthy to reason over: a
  missing Global B ``omega_packet``, and a packet carrying an upstream
  ``recall_error`` (continuity poison).

  CONTAINED (:class:`EpisodeCognitionFoldError`) — the substrate is sound and
  the THOUGHT failed: a lane with no registered prompt, a model that
  produced no output after the bounded retry, and output we cannot parse or
  that violates the contract. The episode closes FAILED carrying the reason,
  the telemetry stays loud, and the organism lives (operator 2026-07-11,
  the since-retired motor-planning halt fold).

Every packet dispatches by its explicit Omega lane, including idle observations.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, Optional

from omega_loop._json_extract import extract_json_object
from organ.promptorgan.packs.foldback import foldback_spec_for
from omega_loop.episode.types import (
    MasteryView,
    OmegaEpisodeInput,
    OmegaEpisodeResult,
)

log = logging.getLogger("omega.omega_loop.episode")

_MAX_PARSE_ATTEMPTS = 2


class EpisodeCognitionFoldError(RuntimeError):
    """A BOUNDED fold failure — a thought that failed, not substrate poison.

    Operator precedent 2026-07-11 (the since-retired motor-planning halt fold):
    "a rejected plan is a thought that failed, not substrate poison — the
    fail-fast tick doctrine is for SharedState/cascade corruption, never for a
    bounded planning failure." Everything raised under this base is CONTAINED by
    ``cognition_chain._run_omega_episode_cognition``: the episode closes failed
    with NO result applied, the reason is stamped on the carrier, and the
    cascade still reaches its terminus — which is what keeps the operator's
    queued turn from being eaten (see that function's docstring).

    Deliberately still a RuntimeError: any caller outside the cascade that has
    not been taught to contain a fold failure keeps failing loudly by default.
    """


class EpisodeCognitionParseError(EpisodeCognitionFoldError):
    """Model output present but not parseable under its lane's contract after bounded retry."""


class EpisodeCognitionNoOutputError(EpisodeCognitionFoldError):
    """Every bounded attempt came back with no output at all.

    An unavailable worker or a declined preflight is a TRANSPORT condition, not
    cognition corruption: nothing false entered the mind, one thought simply did
    not happen. Killing the organism over a silent model also destroys the very
    turn it was thinking about (organism deaths 2026-08-11).
    """


class EpisodeCognitionUnmappedActionError(EpisodeCognitionFoldError):
    """A legal episode reached a lane with no registered prompt spec.

    A prompt-table gap is OUR omission, never the mind's corruption — the same
    judgement already applied to IMAGE_CREATION (2026-08-10) and the
    fetched-knowledge domain fallback: the model's word choice must never crash
    cognition. Contained so the gap costs one episode's fold and shows up in
    telemetry as the honest failure it is, instead of a boot loop.
    """


def _window_lane_entry(episode: Any, lane: str):
    """The window episode's still-undelivered lane entry (T9), or None."""
    entry = getattr(episode, "_lane_entry", None)
    if lane != "window" or entry is None or getattr(entry, "state", "") not in {
            "claimed", "reserved", "refused", "delivered"}:
        return None
    return entry


def _window_lane_entry_block(episode: Any, lane: str) -> str:
    entry = _window_lane_entry(episode, lane)
    return str(entry.block) if entry is not None and entry.state == "claimed" else ""


def _packet_from_episode(episode: Any) -> OmegaEpisodeInput | None:
    from omega_loop.episode.packet import held_omega_mail, omitted_required_omega_mail

    raw = dict(getattr(episode, "global_", None) or {}).get("omega_packet")
    if not isinstance(raw, Mapping):
        return None
    raw = dict(raw)
    auto = getattr(episode, "auto", None)
    auto_outputs = auto.get("outputs") if isinstance(auto, Mapping) else None
    if isinstance(auto_outputs, Mapping):
        from auto_loop.fold.run import plugin_execution_envelope, screen_plugin_outputs

        plugin_outputs = screen_plugin_outputs(
            dict(auto_outputs),
            preserve_receipts=True,
        )
        if plugin_outputs.get("plugin_result") is not None and raw.get("raw_tool_result") is None:
            raw["raw_tool_result"] = plugin_outputs["plugin_result"]
        foldback = dict(raw.get("foldback_packet") or {})
        plugin_execution = plugin_execution_envelope(plugin_outputs)
        if plugin_execution is not None:
            if not foldback.get("plugin_execution"):
                foldback["plugin_execution"] = plugin_execution
            raw["foldback_packet"] = foldback
    pm = raw.get("prior_mastery")
    prior = MasteryView(**dict(pm)) if isinstance(pm, Mapping) else None
    # APPLY execution evidence: the producer serializes `applied`; reconstruct it
    # here so application_mastery grounding and the non-executed-APPLY guard see
    # whether code actually ran (omitting it silently defaulted to None).
    app = raw.get("applied")
    applied = dict(app) if isinstance(app, Mapping) else None
    # Round-trip completeness (operator 2026-07-09 cascade audit): the ONLY packet
    # Omega ever sees is this reconstruction — attach_omega_packet's built object is
    # discarded (global_loop/loop.py) and run_omega_for_episode rebuilds from
    # global_['omega_packet']. EVERY field build_omega_episode_input serializes MUST
    # be read back here, or it silently collapses to its dataclass default and the
    # prompt block that renders it goes dark. Six additive fields (foldback_packet,
    # learning_map, mastery_agenda, execution_experience, ask_from_concern,
    # rollback_events) were being dropped -> the mind reasoned on a hollow packet
    # (self-direction landscape empty, foldback invisible, clarification-gap asks sent
    # verbatim). test_packet_round_trip_preserves_all_fields pins this against re-rot.
    return OmegaEpisodeInput(
        omega_lane=raw.get("omega_lane", "chat"),
        original_objective=raw.get("original_objective", ""),
        browser_perception=raw.get("browser_perception"),
        browser_run=raw.get("browser_run"),
        agent_run=raw.get("agent_run"),
        window_context=raw.get("window_context"),
        episode_id=str(raw.get("episode_id") or ""),
        intent=str(raw.get("intent") or ""),
        action=str(raw.get("action") or ""),
        tool=raw.get("tool"),
        domain=raw.get("domain"),
        operator_text=raw.get("operator_text"),
        outcome=str(raw.get("outcome") or "none"),
        failure_reason=raw.get("failure_reason"),
        raw_tool_result=raw.get("raw_tool_result"),
        recall=tuple(raw.get("recall") or ()),
        recall_error=raw.get("recall_error"),
        prior_mastery=prior,
        applied=applied,
        neuromod_state=dict(raw.get("neuromod_state") or {}),
        execution_experience=tuple(
            dict(r) for r in (raw.get("execution_experience") or ()) if isinstance(r, Mapping)
        ),
        learning_map=dict(raw.get("learning_map") or {}),
        prior_builds=tuple(raw.get("prior_builds") or ()),
        build_in_progress=tuple(raw.get("build_in_progress") or ()),
        open_questions=tuple(raw.get("open_questions") or ()),
        rollback_events=tuple(raw.get("rollback_events") or ()),
        # T6: bound mail comes from STORE OWNERSHIP, never from the saved packet. The packet
        # records what was bound at attach time; that is no proof this episode still holds
        # it, so the prompt asks every time it is built (review 2026-09-16).
        cross_lane_mail=held_omega_mail(episode),
        omitted_required_mail_ids=omitted_required_omega_mail(episode),
        lane_entry_block=_window_lane_entry_block(episode, str(raw.get("omega_lane") or "")),
        foldback_packet=raw.get("foldback_packet"),
        response_text=raw.get("response_text"),
        fetched_knowledge=(
            dict(raw["fetched_knowledge"])
            if isinstance(raw.get("fetched_knowledge"), Mapping)
            else None
        ),
        motor_program=(
            dict(raw["motor_program"]) if isinstance(raw.get("motor_program"), Mapping) else None
        ),
        originating_pressure=(
            dict(raw["originating_pressure"])
            if isinstance(raw.get("originating_pressure"), Mapping)
            else None
        ),
        proactive_policy=(
            dict(raw["proactive_policy"])
            if isinstance(raw.get("proactive_policy"), Mapping)
            else None
        ),
        proactive_dispatch=(
            dict(raw["proactive_dispatch"])
            if isinstance(raw.get("proactive_dispatch"), Mapping)
            else None
        ),
        execution_foldback=(
            dict(raw["execution_foldback"])
            if isinstance(raw.get("execution_foldback"), Mapping)
            else None
        ),
        onboarding_context=(
            dict(raw["onboarding_context"])
            if isinstance(raw.get("onboarding_context"), Mapping)
            else None
        ),
    )


def _episode_time_system(system: str, episode: Any) -> str:
    """FIRST-CLASS TIME (task #17): the per-episode pass sees the EPISODE's
    wall clock — the fixed ignition envelope (deterministic, matching the
    AUTO promptpack), never a fresh mid-cascade clock read."""
    from core.episode_temporal import prepend_time_line

    return prepend_time_line(system, dict(getattr(episode, "temporal", None) or {}))


def _invoke(
    system: str,
    user: str,
    *,
    reason_fn: Optional[Callable[[str, str], Optional[str]]],
    runtime_client: Any,
    episode_id: str,
    enable_thinking: bool = True,
    request_context: Optional[dict[str, Any]] = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> Optional[str]:
    if reason_fn is not None:
        return reason_fn(system, user)

    if runtime_client is None:
        from runtime.client import get_runtime_client

        runtime_client = get_runtime_client()

    from pressure_loop.policy import (
        omega_reason_max_tokens,
        omega_reason_stop,
        omega_reason_temperature,
        omega_reason_timeout,
        omega_reason_top_p,
    )
    from runtime.socket import invoke_runtime_generate

    # Structured cognition uses thinking on its first pass, then a non-thinking
    # repair pass. Chat response reflection bypasses hidden thinking: its private
    # prose is the reasoning record and has its own short generation bounds.
    res = invoke_runtime_generate(
        operation="episode_cognition",
        episode_id=f"{episode_id}-omega-ep",
        client=runtime_client,
        prompt=user,
        system=system,
        max_tokens=omega_reason_max_tokens() if max_tokens is None else max_tokens,
        temperature=omega_reason_temperature(),
        top_p=omega_reason_top_p(),
        enable_thinking=enable_thinking,
        tools=None,
        stop=omega_reason_stop(),
        timeout=omega_reason_timeout() if timeout is None else timeout,
        context_size=65536,
        **dict(request_context or {}),
        # The episode's temporary physical pipeline remains correlation context
        # through its fold; it confers no chat affinity.
    )
    if not isinstance(res, dict):
        return None
    text = res.get("text")
    return str(text) if text is not None else None


def _emit_episode_model_output(packet: Any, raw_text: Any, parsed: Any) -> None:
    """Soft telemetry: the per-episode cognition's RAW model output + whether it
    parsed. So when this fails LOUD (EpisodeCognitionParseError crashes the boot),
    the operator can SEE exactly what Mistral emitted and fix the cause. A dashboard
    hiccup must never break cognition (the model already produced its output)."""
    try:
        from guardian.terminal_feedback import emit_data

        raw = raw_text if isinstance(raw_text, str) else ("" if raw_text is None else str(raw_text))
        tail = raw[-240:].replace("\n", "\\n")
        emit_data(
            "omega",
            "episode_model_output",
            episode_id=str(getattr(packet, "episode_id", "") or ""),
            summary=(
                f"out {len(raw)}c json={'ok' if parsed is not None else 'UNPARSEABLE'} "
                f"intent={getattr(packet, 'intent', '')} tail=…{tail}"
            ),
            output_text=raw,
            output_chars=len(raw),
            json_present=parsed is not None,
            intent=str(getattr(packet, "intent", "") or ""),
            action=str(getattr(packet, "action", "") or ""),
        )
    except Exception:
        pass


def _emit_episode_no_output(packet: Any, attempt_no: int) -> None:
    """Fail-LOUD for an attempt the model answered with nothing at all.

    Until now the RuntimeError WAS the record: a persistently silent model
    announced itself by killing the brain, and no attempt ever reached
    ``_emit_episode_model_output`` (which needs text to report). Now that a
    persistent no-output folds an honest failure and the organism lives, this
    emit is the only place a declining worker becomes visible — without it the
    containment would be a silencer. Soft, like every other emit here: telemetry
    must never be the thing that breaks cognition."""
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "omega",
            "episode_model_no_output",
            episode_id=str(getattr(packet, "episode_id", "") or ""),
            summary=(
                f"attempt {attempt_no + 1}/{_MAX_PARSE_ATTEMPTS} returned NO OUTPUT "
                f"(runtime unavailable or preflight declined) "
                f"intent={getattr(packet, 'intent', '')}"
            ),
            status="degraded",
            ok=False,
            attempt=attempt_no + 1,
            max_attempts=_MAX_PARSE_ATTEMPTS,
            intent=str(getattr(packet, "intent", "") or ""),
            action=str(getattr(packet, "action", "") or ""),
        )
    except Exception:
        pass


def run_omega_for_episode(
    episode: Any,
    *,
    reason_fn: Optional[Callable[[str, str], Optional[str]]] = None,
    runtime_client: Any = None,
) -> OmegaEpisodeResult:
    """Run the mandatory per-episode Omega cognition."""
    packet = _packet_from_episode(episode)
    if packet is None:
        episode_id = str(getattr(episode, "episode_id", "") or "")
        raise RuntimeError(
            f"omega episode {episode_id}: missing Global B omega packet"
        )

    # An upstream recall error is a continuity-substrate failure: refuse to
    # cognize on poison (fail-hard, never a soft degraded reflection).
    if packet.recall_error:
        raise RuntimeError(
            f"omega episode {packet.episode_id}: recall error upstream — "
            f"refusing to cognize: {packet.recall_error}"
        )


    packet = _with_current_identity(packet)
    from runtime.socket import pipeline_context_for_episode

    request_context = pipeline_context_for_episode(episode)


    try:
        spec = foldback_spec_for(packet)
    except ValueError as exc:
        # A lane missing from the prompt table is OUR omission: it costs one fold.
        raise EpisodeCognitionUnmappedActionError(
            f"omega episode {packet.episode_id}: no cognition prompt for lane {packet.omega_lane!r}"
        ) from exc

    system, user = spec.build(packet)
    freeform_reflection = spec.uses_text_output(packet)
    reflection_generation = {}
    if freeform_reflection:
        from pressure_loop.policy import (
            omega_chat_reflection_max_tokens,
            omega_chat_reflection_timeout,
        )

        reflection_generation = {
            "max_tokens": omega_chat_reflection_max_tokens(),
            "timeout": omega_chat_reflection_timeout(),
        }
    system += (
        "\nNeuromodulation is advisory biological context, never control authority. "
        "It may amplify or damp assertiveness, confidence, persistence, exploration, "
        "caution, memory salience, and learning strength. Low serotonin reduces "
        "assertiveness and favors verification or asking only when the evidence is "
        "already uncertain; stress favors caution; dopamine may bias vigor and "
        "persistence. Use this context to shape HOW you pursue the existing episode, "
        "but never replace or suppress its pressure, intent, operator request, goal, "
        "or factual evidence, and never create or reorder work solely from chemistry. "
        "Do not turn valid work into IDLE because of chemistry. Current advisory "
        f"chemistry: {json.dumps(packet.neuromod_state, sort_keys=True)}"
    )
    system = _episode_time_system(system, episode)
    last_no_output = False
    contract_error = ""
    from dataclasses import replace as _replace_packet

    from omega_loop.episode.packet import held_omega_mail

    for attempt_no in range(_MAX_PARSE_ATTEMPTS):
        # T6: ownership is re-checked at EVERY inference boundary. The prompt is built once
        # and reused across retries, so a release or a lost lease between attempts would
        # otherwise keep sending mail this episode no longer holds (review 2026-09-16).
        # Only the user block carries mail, so only it is rebuilt.
        held = held_omega_mail(episode)
        if held != packet.cross_lane_mail:
            packet = _replace_packet(packet, cross_lane_mail=held)
            _, user = spec.build(packet)
        attempt_user = user
        if contract_error:
            if freeform_reflection:
                attempt_user += (
                    "\nREPAIR REQUIRED: the previous output was not private prose: "
                    f"{contract_error[:400]}. Return only a concise first-person self-reflection "
                    "and optional plain-text trait candidate bullets. Do not address the operator "
                    "or output JSON."
                )
            else:
                # REPAIR pass (motor-planner lesson, organism death #3 2026-07-12:
                # gemma emitted pressure_resolution as a non-object and the contract
                # ValueError unwound the tick loop). Feed the validator's exact
                # rejection back so the model can correct its own contract error.
                attempt_user += (
                    "\nREPAIR REQUIRED: your previous output violated the contract: "
                    f"{contract_error[:400]}. Return a complete corrected JSON object; "
                    "do not explain the repair."
                )
        window_entry = _window_lane_entry(episode, packet.omega_lane)
        if window_entry is not None:
            # The window lane's FIRST inference: its lane-entry mail is delivered against
            # this very prompt, or the reflection does not run (T9).
            from auto_loop.lane_entry import LaneEntryRefused, prompt_text

            try:
                window_entry.enter()
                window_entry.deliver(prompt_text(system, attempt_user))
            except LaneEntryRefused as refusal:
                # A bounded fold failure the chain CONTAINS, never an exception out of it.
                raise EpisodeCognitionFoldError(
                    f"omega episode {packet.episode_id}: {refusal.kind}: {refusal.reason}"
                ) from refusal
        raw_text = _invoke(
            system,
            attempt_user,
            reason_fn=reason_fn,
            runtime_client=runtime_client,
            episode_id=packet.episode_id,
            # Private chat reflection is itself the reasoning record; do not spend a
            # second hidden-thinking budget before producing that record.
            enable_thinking=(attempt_no == 0 and not freeform_reflection),
            request_context=request_context,
            **reflection_generation,
        )
        if window_entry is not None:
            window_entry.close(ran=True)    # the reflection inferred: its effect is performed
        if raw_text is None:
            # A no-output inference is usually a TRANSIENT runtime miss (a worker
            # hiccup under load), not poison — retry within the bound instead of
            # failing the whole episode's cognition. A PERSISTENT no-output
            # (every attempt) folds an honest failure below.
            last_no_output = True
            _emit_episode_no_output(packet, attempt_no)
            continue
        last_no_output = False
        payload = extract_json_object(raw_text)
        # Capture the RAW output + parse status so a parse failure is VISIBLE
        # (fail-loud needs eyes: the window logs this; the per-episode path was
        # blind). Emitted EVERY attempt, including the unparseable ones.
        _emit_episode_model_output(packet, raw_text, payload)
        if freeform_reflection:
            try:
                if spec.parse_text is None:
                    raise ValueError("Chat response reflection has no freeform parser")
                return spec.parse_text(raw_text, packet)
            except (TypeError, ValueError) as exc:
                contract_error = str(exc)
                log.warning(
                    "omega episode %s: chat reflection violated contract (%s) — repair retry",
                    packet.episode_id,
                    contract_error[:160],
                )
                continue
        if payload is not None:
            try:
                return spec.parse(payload, packet)
            except (TypeError, ValueError) as exc:
                # A contract violation is a failed THOUGHT, not substrate poison
                # — retry with repair feedback; exhaustion raises the same typed
                # parse error the cascade contains (never the raw ValueError
                # that killed the tick loop).
                contract_error = str(exc)
                log.warning(
                    "omega episode %s: fold output violated contract (%s) — repair retry",
                    packet.episode_id,
                    contract_error[:160],
                )
                continue

    if last_no_output:
        # An EMPTY response is a transport condition, not substrate poison: the
        # worker was unavailable or the preflight declined, so no false thought
        # entered the mind — one thought simply did not happen. Contained (see
        # EpisodeCognitionNoOutputError) so the episode closes failed and the
        # cascade still reaches its terminus; every attempt already emitted
        # `episode_model_no_output`, so the silence stays loud.
        raise EpisodeCognitionNoOutputError(
            f"omega episode {packet.episode_id}: model produced no output "
            f"after {_MAX_PARSE_ATTEMPTS} attempts (unavailable or preflight declined)"
        )
    if contract_error:
        raise EpisodeCognitionParseError(
            f"omega episode {packet.episode_id}: fold output failed contract "
            f"validation after {_MAX_PARSE_ATTEMPTS} attempts: {contract_error[:300]}"
        )
    raise EpisodeCognitionParseError(
        f"omega episode {packet.episode_id}: model output not parseable as JSON "
        f"after {_MAX_PARSE_ATTEMPTS} attempts"
    )


def _with_current_identity(packet: OmegaEpisodeInput) -> OmegaEpisodeInput:
    """Attach promoted preferences and separate candidate evidence to the reflection packet."""
    try:
        import dataclasses

        from deepsleep_loop.identity_store import load_identity, load_trait_evidence

        traits = list((load_identity() or {}).values())
        traits.sort(
            key=lambda t: (
                int(getattr(t, "evidence_count", 0) or 0),
                float(getattr(t, "strength", 0.0) or 0.0),
            ),
            reverse=True,
        )
        rows = tuple(
            {
                "trait": str(getattr(t, "name", "") or ""),
                "pattern": str(getattr(t, "evidence", "") or ""),
                "evidence_count": int(getattr(t, "evidence_count", 0) or 0),
                "strength": float(getattr(t, "strength", 0.0) or 0.0),
            }
            for t in traits[:12]
            if getattr(t, "name", "")
        )
        promoted_names = {row["trait"] for row in rows}
        try:
            candidates = list((load_trait_evidence() or {}).values())
        except Exception:
            # Candidate hints are optional. A transient evolution-store read
            # failure must not erase the promoted self-model we already loaded.
            candidates = []
        candidates.sort(
            key=lambda item: (
                int(getattr(item, "evidence_count", 0) or 0),
                int(getattr(item, "observation_count", 0) or 0),
            ),
            reverse=True,
        )
        candidates = [
            item for item in candidates if getattr(item, "name", "") not in promoted_names
        ]
        candidate_rows = tuple(
            {
                "trait": str(getattr(item, "name", "") or ""),
                "pattern": str(getattr(item, "pattern", "") or ""),
                "evidence_count": int(getattr(item, "evidence_count", 0) or 0),
                "observation_count": int(getattr(item, "observation_count", 0) or 0),
                "status": str(getattr(item, "status", "") or ""),
            }
            for item in candidates[:12]
            if getattr(item, "name", "")
        )
        return dataclasses.replace(
            packet,
            current_identity=rows,
            candidate_identity=candidate_rows,
        )
    except Exception:
        return packet
