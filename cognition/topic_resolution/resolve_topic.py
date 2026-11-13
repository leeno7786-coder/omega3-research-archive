"""Topic Resolution Engine — deterministic priority-cascade topic selection."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.symphony_time import now_iso
from deepsleep_loop.mastery_store import load_mastery
from cognition.topic_resolution.domain_key import canonical_domain_key, extract_topic_from_text
from knowledge_extractor.derive.run import _slug
from deepsleep_loop._deepsleep_state import FailurePattern
from deepsleep_loop.failure_store import load_failures
from pressure_loop.open_questions import STATUS_OPEN, OpenQuestion, list_open
from pressure_loop.symphony_learning_state import build_episode_learning_state
from pressure_loop.policy import load_policy
from pressure_loop.temporal_store import read_trajectory

log = logging.getLogger("omega.cognition.topic_resolution")

NO_TOPIC_AVAILABLE = "NO_TOPIC_AVAILABLE"
SEVERE_MASTERY_GAP_THRESHOLD = 0.5

_FORBIDDEN_TOPICS = frozenset(
    {
        "learning material",
        "general knowledge",
        "random",
        "unknown",
        "general",
    }
)


@dataclass(frozen=True)
class ResolvedTopic:
    topic: str
    domain: str
    source: str
    confidence: float
    fold_question_id: str = ""
    fold_question_text: str = ""


def _normalize_topic(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _extract_topic_from_text(text: str) -> str:
    return extract_topic_from_text(text)


def _is_concrete_topic(topic: str) -> bool:
    normalized = _normalize_topic(topic)
    return bool(normalized) and normalized not in _FORBIDDEN_TOPICS


def _resolved(
    topic: str,
    source: str,
    confidence: float = 1.0,
    *,
    domain: str | None = None,
    fold_question_id: str = "",
    fold_question_text: str = "",
) -> ResolvedTopic:
    return ResolvedTopic(
        topic=topic,
        domain=domain if domain is not None else canonical_domain_key(topic),
        source=source,
        confidence=float(confidence),
        fold_question_id=str(fold_question_id or ""),
        fold_question_text=str(fold_question_text or ""),
    )


def _no_topic() -> ResolvedTopic:
    return ResolvedTopic(
        topic=NO_TOPIC_AVAILABLE,
        domain="",
        source="none",
        confidence=0.0,
    )


def _parse_iso_seconds(iso: str) -> float | None:
    raw = str(iso or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def _open_question_topic_key(question: OpenQuestion) -> str:
    return _normalize_topic(_extract_topic_from_text(question.question_text))


def _score_open_question(question: OpenQuestion, *, now_ts: float, recurrence: int) -> float:
    confidence = float(question.urgency) if question.urgency is not None else 0.5
    asked_ts = _parse_iso_seconds(question.asked_at)
    age_seconds = max(0.0, now_ts - asked_ts) if asked_ts is not None else 0.0
    return confidence * age_seconds * max(1, int(recurrence))


_OPERATOR_CONTRIBUTOR_TYPES = frozenset(
    {"operator_query", "operator_command", "operator_intent", "operator_response"}
)


def _is_operator_origin(state) -> bool:
    meta = dict(getattr(state, "meta", None) or {})
    if str(meta.get("directive_intent") or "").strip():
        return True
    mi = getattr(state, "mi", None)
    if mi is None:
        return False
    pressure = dict(getattr(mi, "pressure", None) or {})
    reason = str(pressure.get("reason") or "").strip()
    if reason in _OPERATOR_CONTRIBUTOR_TYPES:
        return True
    for row in pressure.get("contributors") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("source") or "").strip().lower() == "operator":
            return True
        contrib_type = str(row.get("id") or row.get("type") or "").strip()
        if contrib_type in _OPERATOR_CONTRIBUTOR_TYPES:
            return True
    return False


def _resolve_operator_query(state) -> ResolvedTopic | None:
    if not _is_operator_origin(state):
        return None
    for raw in (getattr(state, "target_str", ""), getattr(state, "text", "")):
        text = str(raw or "").strip()
        if not text:
            continue
        topic = _extract_topic_from_text(text)
        if _is_concrete_topic(topic):
            return _resolved(topic, "operator_query")
    return None


def _resolve_omega_chosen_topic(_state=None) -> ResolvedTopic | None:
    """Omega's OWN specific learning topic (operator 2026-07-08) — from ANY
    mind-driven LEARN decision, not just one path: the idle awakening sets it
    directly, and every per-episode intent_decision that names a concrete topic
    stashes it too (stage_omega_successor). That specific choice is the
    LEARN topic, above the trajectory/mastery-gap resolvers, so study advances
    instead of re-fetching the domain overview. One-shot: consumed when this
    fires, so a residual re-ignition with no fresh choice falls through to the
    domain-based resolvers. Operator-directed LEARN keeps its own resolver
    (above this) — the user's words are already the specific topic. If the
    chosen topic merely echoes the domain, yield to the normal cascade."""
    try:
        from pressure_loop.learn_topic_choice import take_chosen_topic

        choice = take_chosen_topic()
        if not choice:
            return None
        topic = str(choice.get("topic") or "").strip()
        domain = str(choice.get("domain") or "").strip()
        if not topic or not _is_concrete_topic(topic):
            return None
        # A bare-domain-echo choice adds nothing over the domain resolvers.
        if topic.strip().lower() == domain.strip().lower():
            return None
        return _resolved(
            topic, "omega_choice", confidence=0.95, domain=canonical_domain_key(domain or topic)
        )
    except Exception:
        return None


def _resolve_open_question(_state=None) -> ResolvedTopic | None:
    try:
        questions = list_open()
    except Exception:
        return None
    if not questions:
        return None

    now_ts = _parse_iso_seconds(now_iso()) or 0.0
    recurrence: dict[str, int] = {}
    for question in questions:
        key = _open_question_topic_key(question)
        recurrence[key] = recurrence.get(key, 0) + 1

    best: OpenQuestion | None = None
    best_score = -1.0
    for question in questions:
        key = _open_question_topic_key(question)
        score = _score_open_question(
            question,
            now_ts=now_ts,
            recurrence=recurrence.get(key, 1),
        )
        if score > best_score:
            best_score = score
            best = question

    if best is None:
        return None
    topic = str(best.question_text or "").strip()
    if not _is_concrete_topic(topic):
        topic = _extract_topic_from_text(topic)
    if _is_concrete_topic(topic):
        return _resolved(topic, "open_question", confidence=0.5)
    return None


def _severe_mastery_gap_threshold() -> float:
    try:
        policy = load_policy()
        cognition = policy.get("cognition") if isinstance(policy, dict) else None
        if isinstance(cognition, dict):
            topic_resolution = cognition.get("topic_resolution")
            if isinstance(topic_resolution, dict):
                raw = topic_resolution.get("severe_mastery_gap_threshold")
                if raw is not None:
                    return float(raw)
    except Exception:
        pass
    return SEVERE_MASTERY_GAP_THRESHOLD


def _resolve_mastery_gap(_state=None) -> ResolvedTopic | None:
    try:
        records = load_mastery()
    except Exception:
        return None
    if not records:
        return None

    threshold = _severe_mastery_gap_threshold()
    best_name = ""
    best_gap = -1.0
    for record in records.values():
        if str(record.status or "").lower() == "mastered":
            continue
        target = float(record.target_mastery)
        knowledge_gap = target - float(record.knowledge_mastery)
        application_gap = target - float(record.application_mastery)
        gap = max(knowledge_gap, application_gap)
        if gap > best_gap:
            best_gap = gap
            best_name = str(record.domain_name or record.domain_id or "").strip()

    if best_gap < threshold:
        return None
    if _is_concrete_topic(best_name):
        return _resolved(best_name, "mastery_gap", confidence=min(1.0, best_gap))
    return None


def _learning_state_envelope(state) -> dict[str, Any]:
    mi = getattr(state, "mi", None)
    pressure = dict(getattr(mi, "pressure", None) or {})
    envelope = pressure.get("learning_state")
    if isinstance(envelope, dict) and envelope:
        return envelope
    try:
        return build_episode_learning_state()
    except Exception:
        return {}


def _has_learning_debt(envelope: dict[str, Any]) -> bool:
    flags = envelope.get("learning_debt_flags") or {}
    if not isinstance(flags, dict):
        return False
    return any(bool(flags.get(key)) for key in flags)


def _resolve_learning_debt(state) -> ResolvedTopic | None:
    envelope = _learning_state_envelope(state)
    if not _has_learning_debt(envelope):
        return None

    mastery = envelope.get("mastery") or {}
    top_weak = mastery.get("top_weak") if isinstance(mastery, dict) else []
    if not isinstance(top_weak, list) or not top_weak:
        return None

    weakest = min(
        (entry for entry in top_weak if isinstance(entry, dict)),
        key=lambda entry: float(entry.get("mastery", 1.0) or 1.0),
        default=None,
    )
    if not weakest:
        return None
    topic = str(weakest.get("domain") or "").strip()
    if _is_concrete_topic(topic):
        return _resolved(topic, "learning_debt", confidence=0.5)
    return None


def _critical_open_question_thresholds() -> dict[str, float]:
    defaults = {
        "persistence_threshold": 3.0,
        "relevance_threshold": 0.6,
        "blockage_threshold": 0.8,
        "blockage_recency_decay": 0.85,
        "blockage_recency_window": 20.0,
    }
    try:
        policy = load_policy()
        cognition = policy.get("cognition") if isinstance(policy, dict) else None
        if isinstance(cognition, dict):
            tr = cognition.get("topic_resolution")
            if isinstance(tr, dict):
                cq = tr.get("critical_open_question")
                if isinstance(cq, dict):
                    for key in defaults:
                        raw = cq.get(key)
                        if raw is not None:
                            try:
                                defaults[key] = float(raw)
                            except (TypeError, ValueError):
                                pass
    except Exception:
        pass
    return defaults


def _question_trajectory_relevance(
    question: OpenQuestion,
    trajectory: dict[str, Any],
    *,
    trajectory_text: str | None = None,
) -> float:
    """Ettin-retina similarity between a question and trajectory domain."""
    from memory.retina import similarity

    question_text = str(question.question_text or "").strip()
    target_domain = str(trajectory.get("target_domain") or "").strip()
    if not question_text or not target_domain:
        return 0.0
    try:
        score = similarity(question_text, trajectory_text or target_domain)
    except Exception:  # noqa: BLE001 — relevance degrades to neutral
        log.warning(
            "question/trajectory relevance: Ettin retina unavailable; "
            "scoring this question neutral",
            exc_info=True,
        )
        return 0.0
    return float(score) if score is not None else 0.0


def _blockage_recency_weights(
    *,
    window: int,
    decay: float,
) -> dict[str, float]:
    """Map episode_id -> recency weight (1.0 = newest in journal window)."""
    from pressure_loop.episode_journal import journal_window

    if window <= 0:
        return {}
    episodes = journal_window(n=window)
    if not episodes:
        return {}
    weights: dict[str, float] = {}
    n = len(episodes)
    floor = decay ** (n + 1)
    for i, ep in enumerate(episodes):
        eid = str(ep.episode_id or "").strip()
        if not eid:
            continue
        age = n - 1 - i
        weights[eid] = decay**age
    weights["__floor__"] = floor
    return weights


def _pattern_recency_weight(
    last_episode: str,
    *,
    recency_weights: dict[str, float],
) -> float:
    if not recency_weights:
        return 1.0
    eid = str(last_episode or "").strip()
    if not eid:
        return float(recency_weights.get("__floor__", 0.5))
    if eid in recency_weights and eid != "__floor__":
        return float(recency_weights[eid])
    return float(recency_weights.get("__floor__", 0.0))


def _question_execution_blockage(
    question: OpenQuestion,
    failures: dict[str, FailurePattern],
    *,
    recency_weights: dict[str, float] | None = None,
) -> float:
    """0.0–1.0 blockage score from failure frequency on the question's domain."""
    try:
        q_domain = canonical_domain_key(question.question_text)
        if not q_domain:
            return 0.0
        thresholds = _critical_open_question_thresholds()
        if recency_weights is None:
            window = int(thresholds.get("blockage_recency_window", 20) or 20)
            decay = float(thresholds.get("blockage_recency_decay", 0.85) or 0.85)
            if decay <= 0.0 or decay > 1.0:
                decay = 0.85
            recency_weights = _blockage_recency_weights(window=window, decay=decay)
        score = 0.0
        for pattern in (failures or {}).values():
            pattern_domain = pattern.domain_id or canonical_domain_key(pattern.context)
            if pattern_domain == q_domain:
                freq_term = min(float(pattern.frequency or 0) / 5.0, 0.5)
                recency = _pattern_recency_weight(
                    pattern.last_episode,
                    recency_weights=recency_weights,
                )
                score += freq_term * recency
        return min(score, 1.0)
    except Exception:
        return 0.0


def _publish_topic_fold(
    *,
    target_domain: str,
    question: OpenQuestion,
    scoped_topic: str,
) -> None:
    payload = {
        "active": True,
        "target_domain": target_domain,
        "question_id": str(question.question_id or ""),
        "question_text": str(question.question_text or ""),
        "scoped_topic": scoped_topic,
        "at": now_iso(),
    }
    try:
        from pressure_loop.runtime import try_get_default_runtime

        rt = try_get_default_runtime()
        if rt is not None:
            rt.shared_state.write("last_topic_fold", payload)
    except Exception:
        pass
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "cognition",
            "topic_fold",
            summary=f"{target_domain}: {question.question_text}"[:120],
            target_domain=target_domain,
            question_id=question.question_id,
        )
    except Exception:
        pass


def _pick_critical_open_question(trajectory: dict[str, Any]) -> OpenQuestion | None:
    """Return the highest-scored OPEN question that is CRITICAL and path-relevant."""
    try:
        questions = list_open()
    except Exception:
        return None
    if not questions:
        return None

    thresholds = _critical_open_question_thresholds()
    persistence_threshold = thresholds["persistence_threshold"]
    relevance_threshold = thresholds["relevance_threshold"]
    blockage_threshold = thresholds["blockage_threshold"]

    try:
        failures = load_failures()
    except Exception:
        failures = {}

    recency_weights: dict[str, float] = {}
    if questions:
        window = int(thresholds.get("blockage_recency_window", 20) or 20)
        decay = float(thresholds.get("blockage_recency_decay", 0.85) or 0.85)
        if decay <= 0.0 or decay > 1.0:
            decay = 0.85
        recency_weights = _blockage_recency_weights(window=window, decay=decay)

    target_domain = str(trajectory.get("target_domain") or "").strip()
    trajectory_text = target_domain or None

    candidates: list[tuple[float, OpenQuestion]] = []
    for q in questions:
        if q.status != STATUS_OPEN:
            continue
        persistence = int(q.windows_survived or 0)
        relevance = _question_trajectory_relevance(q, trajectory, trajectory_text=trajectory_text)
        blockage = _question_execution_blockage(q, failures, recency_weights=recency_weights)

        if relevance < relevance_threshold:
            continue

        is_critical = (persistence >= persistence_threshold) or (blockage >= blockage_threshold)
        if not is_critical:
            continue

        score = float(persistence) * relevance * (1.0 + blockage)
        candidates.append((score, q))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def mastery_phase(target_domain: str) -> dict | None:
    """Where a trajectory's domain sits on the goal-directed arc (operator 2026-07-08): the
    trajectory is a future APPLICATION goal, not a permanent LEARN lock.

      learn    — knowledge below the apply bar: still being learned.
      apply    — learned enough to apply, application still below target.
      achieved — application at or above target: the goal is met.

    Returns the phase with the numbers it was read from, or None when the domain has no mastery
    record — a domain never touched, which is not the same as one learned to zero.

    The ONE reading of the arc. The topic cascade's stopping rule
    (``_trajectory_goal_still_learning``) and Omega's per-episode CONDUCT guidance both read it,
    so the prompt can never name a phase the cascade is not acting on. A mastery store that
    cannot be read raises: that is not a domain with no record.
    """
    from pressure_loop.practice_mint import MIND_DRIVEN_KNOWLEDGE_BAR as bar

    # module-level load_mastery (patched by tests, single source with the
    # rest of the cascade); _slug is the same key normalizer the store uses
    records = load_mastery()
    rec = records.get(_slug(target_domain)) or records.get(target_domain)
    if rec is None:
        return None
    knowledge = float(rec.knowledge_mastery)
    application = float(rec.application_mastery)
    target = float(rec.target_mastery)
    if application >= target:
        phase = "achieved"
    elif knowledge >= float(bar):
        phase = "apply"
    else:
        phase = "learn"
    return {
        "phase": phase,
        "knowledge": knowledge,
        "application": application,
        "target_mastery": target,
        "apply_bar": float(bar),
    }


def _trajectory_goal_still_learning(target_domain: str) -> bool | None:
    """Whether the trajectory should keep driving LEARN, read from ``mastery_phase``:
      True  — still in the LEARN phase (knowledge below the apply bar): the
              trajectory SHOULD keep driving LEARN (convergence holds).
      False — learned-enough OR the application goal is met: the trajectory
              STOPS driving LEARN — the apply path takes the domain and LEARN
              focus rotates to what is not yet learned.
      None  — unknown domain / no record: caller keeps prior behavior.
    """
    try:
        view = mastery_phase(target_domain)
    except Exception:
        return None
    if view is None:
        return None
    return view["phase"] == "learn"


def _resolve_trajectory(_state=None) -> ResolvedTopic | None:
    try:
        trajectory = read_trajectory()
    except Exception:
        return None
    if not isinstance(trajectory, dict):
        return None
    target_domain = str(trajectory.get("target_domain") or "").strip()
    if not _is_concrete_topic(target_domain):
        return None

    # Learn-until-ready: only drive LEARN while the goal domain is still being
    # learned. Once learned-enough (or the application goal is met), yield so
    # the apply path takes over and LEARN rotates to unlearned domains.
    if _trajectory_goal_still_learning(target_domain) is False:
        return None

    critical_question = _pick_critical_open_question(trajectory)
    if critical_question is not None:
        scoped = f"{target_domain}: {critical_question.question_text}"
        _publish_topic_fold(
            target_domain=target_domain,
            question=critical_question,
            scoped_topic=scoped,
        )
        return _resolved(
            scoped,
            "trajectory_fold",
            confidence=0.90,
            domain=_slug(target_domain),
            fold_question_id=critical_question.question_id,
            fold_question_text=critical_question.question_text,
        )

    return _resolved(target_domain, "trajectory", confidence=0.85)


def _resolve_pressure_meta(state) -> ResolvedTopic | None:
    meta = dict(getattr(state, "meta", None) or {})
    for key in ("domain", "topic"):
        topic = str(meta.get(key) or "").strip()
        if _is_concrete_topic(topic):
            return _resolved(topic, "pressure", confidence=0.5)
    return None


def _domain_from_url(url: str) -> str:
    """Fallback mastery domain from a bare URL host ('https://owasp.org/..' ->
    'owasp'). Used only when the cascade resolved no topic for the operator's URL
    to inherit a domain from — so a context-free "here's a URL" still credits a
    sane domain instead of the whole URL slugged."""
    try:
        from urllib.parse import urlparse

        host = urlparse(str(url or "")).netloc.lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        label = host.split(".")[0] if host else ""
        return _slug(label) if label else ""
    except Exception:
        return ""


def _apply_operator_url_override(resolved: ResolvedTopic) -> ResolvedTopic:
    """Operator-provided URL override (operator 2026-07-22: "make any url i give
    it plausible").

    When a source URL is pending (the operator handed one over, in a directive or
    answering Omega's own ASK), that URL becomes the LEARN fetch target — web_fetch
    direct-fetches a URL-form query, bypassing the Wikipedia opensearch
    fuzzy-resolve that otherwise strands the mind on the encyclopedia. The mastery
    DOMAIN is inherited from whatever the cascade resolved (the topic the URL
    informs), so crediting is unchanged; a stashed hint or the URL host is the
    fallback only when the cascade found no topic. One-shot: consumed here, so a
    later residual LEARN falls back to the normal cascade."""
    try:
        from pressure_loop.learn_source_url import take_source_url

        pending = take_source_url()
    except Exception:
        pending = None
    if not pending:
        return resolved
    url = str(pending.get("url") or "").strip()
    if not url:
        return resolved
    domain = ""
    if resolved.topic != NO_TOPIC_AVAILABLE:
        domain = str(resolved.domain or "").strip()
    if not domain:
        domain = str(pending.get("domain") or "").strip()
    if not domain:
        domain = _domain_from_url(url)
    return ResolvedTopic(
        topic=url,
        domain=domain,
        source="operator_url",
        confidence=1.0,
    )


def resolve_learning_topic(state) -> ResolvedTopic:
    """Resolve a concrete learning topic from the six-source priority cascade.

    The MIND's TRAJECTORY (the specific subtopic it set itself to pursue for the
    operator's agenda) takes priority over the deterministic mastery-gap rescan, so the
    LEARN focus stays STABLE on the mind's chosen subtopic until mastery instead of
    hopping to whatever has the largest gap each tick (the convergence fix). The
    mastery-gap rescan is now a BOOTSTRAP-only fallback: it fires when the mind has set
    NO trajectory yet (cold start), picking from the operator-seeded onboarding mastery
    topics to get the first learning going — after which the mind owns the focus."""
    resolvers = (
        _resolve_operator_query,
        _resolve_omega_chosen_topic,  # Omega's SPECIFIC awakening topic — the exact sub-aspect it chose
        _resolve_trajectory,  # the mind's goal arc — drives LEARN until ready-to-apply
        _resolve_mastery_gap,  # BOOTSTRAP fallback (cold start: no choice, no trajectory)
        _resolve_open_question,
        _resolve_learning_debt,
        _resolve_pressure_meta,
    )
    resolved = _no_topic()
    for resolver in resolvers:
        try:
            candidate = resolver(state)
        except Exception:
            candidate = None
        if candidate is not None and _is_concrete_topic(candidate.topic):
            resolved = candidate
            break
    # An operator-provided URL, if pending, overrides the fetch TARGET (keeps the
    # resolved domain) so web_fetch direct-fetches it instead of fuzzy-resolving
    # the topic to Wikipedia. No pending URL -> identity (byte-for-byte the same
    # ResolvedTopic the cascade produced). One-shot: consumed on this LEARN.
    return _apply_operator_url_override(resolved)
