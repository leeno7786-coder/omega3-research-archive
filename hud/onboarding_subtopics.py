"""Onboarding subtopic decomposition - fold the operator's onboarding topics through
the model so Omega breaks each into concrete, learnable subtopics.

When the operator fills out their profile, each chosen topic is a learning category Omega
has NO stored knowledge of yet. This module builds the prompt that tells the model exactly
that - "you have no knowledge here, break it into the smaller subtopics you must learn" -
runs it through the resident model, and returns ``{category: [subtopic, ...]}``. The caller
(``hud.operator_onboarding.seed_first_wake``) saves each subtopic as a CHILD subdomain
under its category (a mastery gap with ``parent`` set), which the existing autonomous
learning cascade then works through.

Failure-soft end to end: a model that is down, slow, or returns garbage yields ``{}`` so
onboarding still completes with the parent categories seeded.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("omega.hud.onboarding_subtopics")

_MAX_SUBTOPICS_PER_CATEGORY = 8

_SUBTOPIC_SYSTEM = (
    "You are Omega, a self-directed AI building your own learning map from the operator's "
    "profile. For every topic the operator lists you currently have NO stored knowledge or "
    "memory - each topic is a gap you must learn to fill. Break each topic into a handful of "
    "concrete, studyable subtopics: the specific things you must learn to reach real "
    "competence. Output STRICT JSON only - no prose, no markdown, no commentary."
)


def build_subtopic_prompt(occupation: str, categories: List[str]) -> tuple[str, str]:
    """Build the (system, user) onboarding decomposition prompt.

    Tells the model it has NO knowledge of these topics yet and must learn them, and to
    break each into concrete subtopics, returning a strict JSON ``{topic: [subtopic,...]}``.
    """
    occ = str(occupation or "").strip() or "(unspecified)"
    listed = "\n".join(
        f"{i}. {str(c or '').strip()}" for i, c in enumerate(categories or [], start=1)
    )
    user = (
        f"Operator occupation: {occ}\n\n"
        "Topics you must learn (you have NO knowledge of any of these yet - this is your "
        "learning agenda, build it out):\n"
        f"{listed}\n\n"
        "For EACH topic above, list 4 to 6 concrete subtopics you must learn to build real "
        "mastery in it. Each subtopic must be specific and studyable - a real concept or "
        'skill, never vague (good: "Big-O analysis", "Graph traversal"; bad: "basics", '
        '"fundamentals", "advanced").\n\n'
        "Return STRICT JSON mapping every topic to its subtopics, exactly this shape:\n"
        '{"<topic>": ["<subtopic>", "<subtopic>", ...], ...}'
    )
    return _SUBTOPIC_SYSTEM, user


def _invoke_model(system: str, user: str, runtime_client: Any) -> Optional[str]:
    """One model call -> raw text. Mirrors pressure_loop.fast_reply; emits + failure-soft."""
    if runtime_client is None:
        from runtime.client import get_runtime_client

        runtime_client = get_runtime_client()

    import time

    from runtime.socket import invoke_runtime_generate

    t0 = time.perf_counter()
    res = invoke_runtime_generate(
        operation="onboarding_subtopics",
        episode_id="onboarding-subtopics",
        client=runtime_client,
        prompt=user,
        system=system,
        max_tokens=900,
        temperature=0.3,
        top_p=0.9,
        enable_thinking=False,
        tools=None,
        stop=None,
        timeout=90,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    text = res.get("text") if isinstance(res, dict) else None
    ok = bool(text)
    try:
        from guardian.terminal_feedback import emit_inference

        emit_inference(
            "hud",
            "onboarding_subtopics",
            episode_id="onboarding-subtopics",
            latency_ms=latency_ms,
            ok=ok,
            model="gemma4-embedded",
            status="ok" if ok else "fail",
        )
    except Exception:
        pass
    return str(text) if text is not None else None


def _clean_subtopics(raw_list: Any) -> List[str]:
    """Normalize one category's subtopic list: strings only, stripped, deduped
    (case-insensitive), blanks dropped, capped."""
    out: List[str] = []
    seen: set[str] = set()
    for raw in raw_list or []:
        title = str(raw or "").strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        out.append(title)
        if len(out) >= _MAX_SUBTOPICS_PER_CATEGORY:
            break
    return out


def decompose_subtopics(
    occupation: str,
    categories: List[str],
    *,
    reason_fn: Optional[Callable[[str, str], Optional[str]]] = None,
    runtime_client: Any = None,
) -> Dict[str, List[str]]:
    """Fold the onboarding categories through the model -> ``{category: [subtopic, ...]}``.

    Restricted to the categories actually asked about (case-insensitive match to the
    model's keys), each list cleaned/deduped/capped. ``reason_fn(system, user) -> str``
    injects the model for tests. Returns ``{}`` on any failure (caller degrades to
    parents-only). Never raises.
    """
    cats = [str(c or "").strip() for c in (categories or []) if str(c or "").strip()]
    if not cats:
        return {}
    system, user = build_subtopic_prompt(occupation, cats)
    try:
        raw = (
            reason_fn(system, user)
            if reason_fn is not None
            else _invoke_model(system, user, runtime_client)
        )
    except Exception:
        log.warning("onboarding subtopic model call failed (failure-soft)", exc_info=True)
        return {}

    from omega_loop._json_extract import extract_json_object

    payload = extract_json_object(raw)
    if not isinstance(payload, dict):
        return {}

    # Match the model's returned keys back to the categories we asked about (the model may
    # echo a topic with different casing/spacing); only keep requested categories.
    by_lower = {c.lower(): c for c in cats}
    out: Dict[str, List[str]] = {}
    for key, value in payload.items():
        canonical = by_lower.get(str(key or "").strip().lower())
        if canonical is None:
            continue
        subs = _clean_subtopics(value)
        if subs:
            out[canonical] = subs
    return out
