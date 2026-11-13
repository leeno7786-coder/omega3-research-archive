"""Deterministic query-type router → forced remember_* tool sequence.

Routing tiers (first match wins):
  1. Regex intents (identity, persona, code project, **how to / how do we do**, time, …)
  2. ``suggest_code_workflow_lobe`` marker disambiguation (lobe 8 vs 10)
  3. Default ``remember_knowledge`` (lobe 2)

**Procedure vs workflow split**
  - ``how to`` / ``how do we do`` → ``remember_procedure`` (lobe **5** tools/cognitive
    procedures — voice_box invokable skills, operator how-to on *using* tools)
  - Process-heavy text *without* that prefix → ``remember_workflow`` (lobe **10**)
    when code/workflow markers favor process over artifact

Gate ingest/recall symmetry uses the gate contract lobe + ``subdomain_for``; MRIL
recall-tool lobe match is informational only (see Phase 7 harness).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from auto_loop.mril_tools.registry import REMEMBER_TOOL_REGISTRY
from auto_loop.mril_tools.subdomain_helpers import subdomain_for

_RELATIONSHIP_WORDS = (
    "mother",
    "father",
    "mom",
    "dad",
    "brother",
    "sister",
    "son",
    "daughter",
    "sons",
    "daughters",
    "child",
    "children",
    "kid",
    "kids",
    "wife",
    "wives",
    "husband",
    "husbands",
    "married",
    "marital",
    "partner",
    "girlfriend",
    "boyfriend",
    "fiance",
    "spouse",
    "spouses",
    "friend",
    "colleague",
    "coworker",
    "boss",
    "mentor",
    "uncle",
    "aunt",
    "cousin",
    "grandma",
    "grandmother",
    "grandpa",
    "grandfather",
    "roommate",
    "neighbor",
    "classmate",
)
_RELATIONSHIP_RE = re.compile(r"\b(?:" + "|".join(_RELATIONSHIP_WORDS) + r")\b", re.I)
_PERSON_NAME_QUERY_RE = re.compile(
    r"\bwho\s+(?:is|was)\s+([A-Z][a-z]{1,30}(?:\s+[A-Z][a-z]{1,30})?)\b"
)
# Recall verbs that mean "retrieve what you hold about this person". Live
# finding 2026-07-30: "what do you remember about Noah?" returned 8 persona
# records and "what do you recall about Noah?" returned zero, because this
# alternation listed only remember|know -- "recall" missed every persona tier
# and fell through to the knowledge catch-all (lobe 2), where the operator's
# persona records do not live. One constant, used by both alternations below,
# so the two lists cannot drift apart again.
_PERSON_MEMORY_VERBS = r"(?:remember|recall|recollect|know)"
_PERSON_MEMORY_QUERY_RE = re.compile(
    r"\b(?i:(?:what\s+do\s+you\s+(?:\w+\s+){0,2}" + _PERSON_MEMORY_VERBS + r"\s+about|"
    r"tell\s+me\s+(?:(?:everything|all)\s+you\s+" + _PERSON_MEMORY_VERBS + r"\s+about|about)))\s+"
    r"([A-Z][a-z]{1,30}(?:\s+[A-Z][a-z]{1,30})?)\b",
)
_PERSON_ORG_ROLE_QUERY_RE = re.compile(
    r"(?i:^\s*what\s+do\s+)"
    r"(?:[A-Z][a-z]{1,30}|I|me)(?:\s+[A-Z][a-z]{1,30})?"
    r"(?i:\s+and\s+)"
    r"(?:[A-Z][a-z]{1,30}|I|me)(?:\s+[A-Z][a-z]{1,30})?"
    r"(?i:\s+(?:both\s+)?do\s+at\b)",
)

# High-precision journal/vision INTENT regexes — shared by route_memory_tool's
# dispatch tiers below and by route_names_non_recallable_lobe (the recall
# verdict's region-refusal authority). The greedy grammar-probe fallback tier
# must NEVER feed that refusal: a broad match is harmless for tool dispatch
# (it just picks a recoverable lobe) but single generic words ("patterns",
# "screen", "photo", "display") probe to lobe 7 and would veto unrelated
# recalls outright (review finding 2026-07-18).
# The operator asking about THEIR OWN world — own name / birthday, a
# preference, a pet, a relation's name. Live 2026-09-05: "what is my favorite
# editor?" and "what is my dog's name?" fell through every branch to the
# knowledge catch-all (lobe 2); the recall's allowed_lobes then excluded the
# lobe-1 persona records that held both facts and Omega denied knowing them.
_OPERATOR_PERSONAL_RE = re.compile(
    r"\bmy (?:favou?rite|preferred)\b"
    r"|\bmy (?:\w+ )?\w+'s name\b"
    r"|\bmy (?:own )?(?:name|birthday|birth date|age|hometown|address|email|phone number)\b"
    # deliberately NO bare "what is my ..." catch-all: "what are my open
    # questions" / "my defining characteristics" / "my traits" are identity
    # (lobe 0) and "my goals" is reasoning; only the nouns above are persona.
    r"|\bmy (?:dogs?|cats?|pets?|kids?|children|family|partner)\b"
    # "which editor do I like best?" / "what do I prefer?" -- the operator's
    # own tastes asked without the word "favorite" (live 2026-09-06: routed to
    # knowledge, recall came back empty, and the model invented an answer)
    r"|\bdo i (?:like|prefer|love|enjoy)\b"
    r"|\bi (?:like|prefer|love|enjoy) (?:best|most)\b",
    re.I,
)
_VISION_FACE_RE = re.compile(r"\b(who is this (face|person)|recognize this face)\b")
_VISION_SCENES_RE = re.compile(r"\b(what|which) scenes?\b")
_JOURNAL_RE = re.compile(r"\bdeepsleep\b|\b(what|which) (journal|consolidation)\b")

# Native agent observations have a typed recall surface.  Keep this before
# the broad dev grammar so "what was in the directory?" cannot fall through to
# knowledge(2) or be answered from the cloud model's still-hot transcript.
_DEV_OBSERVATION_QUERY_RE = re.compile(
    r"\b(?:full\s+)?(?:tree|directory(?:\s+tree)?|folder(?:\s+tree)?|paths?)\b"
    r"|\bwhat (?:was|were) in (?:the )?(?:directory|folder|workspace)\b"
    r"|\b(?:files?|entries)\s+(?:(?:are|were|listed|stored)\s+)?(?:in|under|inside)\b"
    r"|\b(?:strings?|contents?|text|lines?)\b"
    r"|\bwhat (?:did|has) (?:we|you|i) (?:see|read|find)\b"
    r"|\b(?:touch(?:ed)?|change(?:d)?|modif(?:y|ied)|edit(?:ed)?|operations?|commands?|actions?)\b"
    r"|\bwhat (?:did|have) (?:we|you|i) (?:touch|change|modify|edit|run)\b"
    r"|\b(?:project|projects|repository|repo|workspace|drive|roots?)\b",
    re.I,
)


def _dev_observation_subdomain(text: str) -> int | None:
    """Return the typed dev-observation bucket requested by *text*."""
    lowered = str(text or "").lower()
    if re.search(
        r"\b(?:full\s+)?(?:tree|directory(?:\s+tree)?|folder(?:\s+tree)?|paths?)\b"
        r"|\bwhat (?:was|were) in (?:the )?(?:directory|folder|workspace)\b"
        r"|\b(?:files?|entries)\s+(?:(?:are|were|listed|stored)\s+)?(?:in|under|inside)\b",
        lowered,
    ):
        return 5
    if re.search(
        r"\b(?:strings?|contents?|text|lines?)\b"
        r"|\bwhat (?:did|has) (?:we|you|i) (?:see|read|find)\b",
        lowered,
    ):
        return 6
    if re.search(
        r"\b(?:touch(?:ed)?|change(?:d)?|modif(?:y|ied)|edit(?:ed)?|operations?|commands?|actions?)\b"
        r"|\bwhat (?:did|have) (?:we|you|i) (?:touch|change|modify|edit|run)\b",
        lowered,
    ):
        return 7
    if re.search(r"\b(?:project|projects|repository|repo|workspace|drive|roots?)\b", lowered):
        return 4
    return None


def _tool_entry(
    tool: str,
    query: str,
    *,
    scope_subdomain: bool = True,
    limit: int | None = None,
    allowed_subdomains: list[int] | None = None,
) -> Dict[str, Any]:
    """Build a forced remember_* tool entry.

    ``scope_subdomain`` (default True) narrows recall to one deterministic subdomain via
    ``subdomain_for`` — right for query-shaped recall (knowledge), where the operator's wording
    picks the subdirectory. Set it FALSE for whole-lobe grounding (identity/persona): a prompt-type
    recall must hit EVERY subdirectory of its lobe so the full set surfaces and nothing is dropped
    by a wording heuristic (e.g. "who is your creator?" must not be narrowed to the omega subdomain
    and miss the creator seed in the constitution subdomain). The tool stays scoped to its lobe
    (``allowed_lobes``), so it never routes outside the prompt type. ``limit`` overrides the recall
    cap for that entry (used to pull a small lobe exhaustively)."""
    lane, lobe, memory_type = REMEMBER_TOOL_REGISTRY[tool]
    entry: Dict[str, Any] = {
        "tool": tool,
        "query": query,
        "allowed_lobes": [lobe],
        "memory_type": memory_type,
        "lane": lane,
        "lobe": lobe,
    }
    if allowed_subdomains is not None:
        entry["allowed_subdomains"] = [int(y) for y in allowed_subdomains]
    elif scope_subdomain:
        subdomain = subdomain_for(query, lobe)
        if subdomain is not None:
            entry["subdomain"] = subdomain
    if limit is not None:
        entry["limit"] = int(limit)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "auto",
            "route_memory_tool",
            summary=f"tool={tool} lobe={lobe}",
            tool=tool,
            lobe=lobe,
            subdomain=entry.get("subdomain"),
        )
    except Exception:
        pass
    return entry


def route_memory_tool(query: str) -> Dict[str, Any]:
    """Classify operator query into exactly one Meta-routed remember_* tool."""
    text = str(query or "").strip()
    lowered = text.lower()

    if re.search(r"\b(who am i|what are my traits|my traits)\b", lowered):
        return _tool_entry("remember_identity", text)

    if re.search(
        r"\b(?:what are you|what is your purpose|what is your name|what is omega(?!\s+ai\b)|"
        r"tell me about yourself|describe yourself|what are your traits)\b",
        lowered,
    ):
        return _tool_entry("remember_identity", text, scope_subdomain=False)

    if re.search(
        r"\b(?:who (?:is|was) your creator|who (?:created|built) you|your creator)\b",
        lowered,
    ):
        return _tool_entry("remember_identity", text, scope_subdomain=False)

    if re.search(r"\bwhat do you (?:\w+ ){0,2}remember about me\b", lowered):
        return _tool_entry("remember_persona", text)

    if re.search(r"\bwhy did we decide\b", lowered):
        return _tool_entry("remember_reasoning", text)

    if re.search(
        # intervening words allowed: "what do you remember about the
        # implementation in this codebase" must still reach the code branch
        r"\b(what do you remember about .{0,40}\b(project|code|codebase|repo(?:sitory)?)\b|python project)\b",
        lowered,
    ):
        if re.search(r"\bdev\b|\bimplementation\b|\bagent work\b", lowered):
            return _tool_entry("remember_dev", text)
        return _tool_entry("remember_code", text)

    # Operator how-to on using tools → lobe 5 cognitive procedures (not lobe 10 workflows).
    # General interrogative form: "how do i/we/you <verb>" is a procedure ask.
    if re.search(r"\bhow do we do\b|\bhow to\b|\bhow do (?:i|we|you)\b", lowered):
        return _tool_entry("remember_procedure", text)

    # Operator-personal facts live in lobe 1 (persona). Sits after the
    # how-to / project / code branches (their precedence is kept) and before
    # the temporal branch so "when is my birthday" stays a persona lookup.
    if _OPERATOR_PERSONAL_RE.search(lowered) and not re.search(
        r"\b(?:goals?|objectives?|constraints?|trajectory|traits|roadmap|artifacts?|"
        r"occupation|job|workplace|employer|project|code|codebase|repo)\b",
        lowered,
    ):
        # whole-lobe grounding: the fact may sit under the operator subdomain
        # (1.00 relation_dog) or the named person (1.01 biscuit); a wording
        # heuristic must not drop either.
        return _tool_entry("remember_persona", text, scope_subdomain=False)
    # "when did/do/was..." questions are temporal recall (battery-1000:
    # 'when did we deliver the first honey batch' fell to code/workflow).
    if re.search(r"^\s*when (?:did|do|does|was|were|is|will)\b", lowered):
        return _tool_entry("remember_time", text)

    from memory.plugin_memory import is_plugin_memory_recall_query

    if is_plugin_memory_recall_query(text):
        # Plugin-result events are routed to the outcome facet (Y=6), not the
        # generic 5W1H "what" facet (Y=1).
        return _tool_entry("remember_situation", text, allowed_subdomains=(6,))

    # Dev-observation recall is deterministic and must stay in lobe 11.  The
    # explicit subdomain is the user's requested view (project/tree/strings/
    # operations), not a semantic guess made by the answer model.
    dev_observation_y = _dev_observation_subdomain(text)
    if dev_observation_y is not None and _DEV_OBSERVATION_QUERY_RE.search(text):
        return _tool_entry(
            "remember_dev",
            text,
            allowed_subdomains=(dev_observation_y,),
        )

    # "who <action-verb>..." is situational (who-did-it), not persona lookup.
    if re.search(r"^\s*who (?:left|opened|closed|broke|approved|forgot|spilled|caused)\b", lowered):
        return _tool_entry("remember_situation", text)

    if re.search(
        r"\b(where were we|what happened last time|last time we)\b",
        lowered,
    ):
        return _tool_entry("remember_time", text)

    if _VISION_FACE_RE.search(lowered):
        return _tool_entry("remember_vision", text)

    if re.search(r"\bopen questions\b", lowered):
        # self_inquiry lives at identity (0,5), not reasoning.
        return _tool_entry("remember_identity", text)

    if re.search(r"\b(mastery|progress) in\b", lowered):
        return _tool_entry("remember_knowledge", text)

    if re.search(r"\bomega'?s trajectory\b|\bwhat is my trajectory\b", lowered):
        return _tool_entry("remember_time", text)

    # ── Category recall intents — cover the lobes that previously fell through
    # to knowledge(2). Anchors are ADJACENT ("what goals", not "what ... goals"),
    # and this block sits BEFORE the knowledge catch-all so a leading category
    # question is not hijacked by a trailing "what do you know about ..." clause. ──
    if (
        re.search(r"\b(what|which) (goals?|objectives?|constraints?)\b", lowered)
        or re.search(
            r"\bwhat (is|are|was|were) (our|the|my) "
            r"(goals?|objectives?|constraints?|aims?)\b",
            lowered,
        )
        or re.search(r"\bwhat did we (decide|choose|settle)\b", lowered)
    ):
        return _tool_entry("remember_reasoning", text)

    if re.search(r"\b(what|which) (cognitive )?(tools?|pipelines?)\b", lowered):
        return _tool_entry("remember_procedure", text)

    if re.search(r"\bhow (did|do) (i|we|you) feel\b", lowered) or re.search(
        r"\bwhat (was|were) the (outcome|result|consequence)\b", lowered
    ):
        return _tool_entry("remember_situation", text)

    if re.search(r"\b(what|which) (milestones?|events?)\b", lowered):
        return _tool_entry("remember_time", text)

    if (
        re.search(r"\b(what|which) artifacts?\b", lowered)
        or re.search(r"\bwhat('?s| is) the roadmap\b", lowered)
        # the operator asking about work THEY (or we) built: lobe 11 holds the
        # builder artifacts (live 2026-09-06: "do you remember what I built
        # yesterday?" swept lobes 9/4/2/8, never 11, and answered from DeepSleep
        # trait-promotion episodes that it "built emergent traits")
        or re.search(
            r"\bwhat (?:did|have|had) (?:i|we|you) (?:build|built|make|made|write|wrote|create|created|code|coded|implement|implemented)\b"
            r"|\bwhat (?:i|we) (?:built|made|wrote|created|coded|implemented)\b",
            lowered,
        )
        or re.search(r"\bwhat did the build (produce|output|generate)\b", lowered)
    ):
        return _tool_entry("remember_dev", text)

    if _VISION_SCENES_RE.search(lowered):
        return _tool_entry("remember_vision", text)

    if _JOURNAL_RE.search(lowered):
        return _tool_entry("remember_journal", text)

    if re.search(r"\bwho do i know\b", lowered) or re.search(
        r"\bwho (?:is|was)\b.{0,40}\bnamed\b", lowered
    ) or re.search(
        r"^\s*who (?:are|were)\b.{1,80}\b(?:i|we) know\b", lowered
    ):
        return _tool_entry("remember_persona", text, scope_subdomain=False)

    if re.search(r"\bwho\s+(?:is|was)\s+(?:my|our)\b", lowered) and _RELATIONSHIP_RE.search(
        lowered
    ):
        return _tool_entry("remember_persona", text, scope_subdomain=False)

    if _RELATIONSHIP_RE.search(lowered) and (
        re.search(r"^\s*who\s+(?:has|is|was)\b", lowered)
        or re.search(r"^\s*(?:does|did)\s+.{1,60}\s+have\b", lowered)
        or re.search(
            r"^\s*what\s+(?:is|was)\s+.{1,60}\s+(?:relationship|marital status)\b",
            lowered,
        )
    ):
        return _tool_entry("remember_persona", text, scope_subdomain=False)

    if _PERSON_NAME_QUERY_RE.search(text):
        return _tool_entry("remember_persona", text, scope_subdomain=False)

    if _PERSON_MEMORY_QUERY_RE.search(text):
        return _tool_entry("remember_persona", text, scope_subdomain=False)

    if _PERSON_ORG_ROLE_QUERY_RE.search(text):
        return _tool_entry("remember_persona", text, scope_subdomain=False)

    if re.search(
        r"\b(where do i work|where am i employed|who do i work for|"
        r"what is my (?:occupation|job|workplace|employer))\b",
        lowered,
    ):
        return _tool_entry("remember_persona", text)

    # Knowledge catch-all — AFTER the specific category intents so a leading
    # "what <category>" question wins over a trailing "what do you know about".
    # "what did I ask you earlier?" / "what were we discussing?" — the
    # conversation itself: lobe 9 episode transcripts (live 2026-09-06: fell
    # to the knowledge catch-all and answered "the data does not contain it")
    if re.search(
        r"\bwhat (?:did|have) (?:i|we|you) (?:ask|tell|say|said|mention|discuss)\b"
        r"|\bwhat (?:was|were) (?:the last thing|we) (?:i |you )?(?:said|saying|talking|discussing|discussed)\b"
        r"|\bwhat (?:did|have) we (?:talk|chat)(?:ed)? about\b",
        lowered,
    ):
        return _tool_entry("remember_time", text, scope_subdomain=False)
    if re.search(r"\bwhat do you know about\b", lowered):
        return _tool_entry("remember_knowledge", text)

    # ── Grammar-probe disambiguation (quality slice 2, 2026-07-04) ──
    # Ambiguous free-form queries previously defaulted straight to
    # knowledge(2) — the L2-default gravity the stress battery flagged
    # (plausible-lobe landing 34.6%). Before defaulting, probe whether a
    # lobe's own deterministic grammar RECOGNIZES the text; the first
    # recognizing lobe wins. Acceptance filters keep weak signals from
    # hijacking: identity only via guardian-rule hits (pronouns are
    # everywhere), situational only via outcome/sentiment (5W1H
    # interrogatives fire on any question), reasoning excludes bare
    # goal-words ('want/goal' too generic to claim the lobe).
    probe = _grammar_probe(lowered, _GRAMMAR_PROBES_PRE)
    if probe is not None:
        return _tool_entry(probe, text)

    from memory.routing.code_workflow_disambiguation import suggest_code_workflow_lobe

    cw = suggest_code_workflow_lobe(text)
    if cw == 8:
        return _tool_entry("remember_code", text)
    # Process markers without how-to prefix → lobe 10 workflows (not remember_procedure/5).
    if cw == 10:
        return _tool_entry("remember_workflow", text)

    # Softer probes AFTER the code/workflow disambiguator: dev-tool words
    # ("pipeline", "deploy") appear inside process/workflow narratives, so cw
    # must get first refusal on those before the tools/reasoning grammars.
    probe = _grammar_probe(lowered, _GRAMMAR_PROBES_POST)
    if probe is not None:
        return _tool_entry(probe, text)

    return _tool_entry("remember_knowledge", text)


_IMPERATIVE_SIGNAL = re.compile(
    # possessive/question rule-contexts only — "its lifetime rules" (a rust
    # concept) must not trigger the guardian probe (redteam pin)
    r"\b(you|your|never|must|always|forbidden|non-negotiable"
    r"|(?:my|our|hard|house|ground) rules?|what (?:are|is) the (?:hard )?rules?)\b"
)

# (lobe, tool, accepted subdomains or None=any). PRE tier = strong topical
# vocabularies that outrank code/workflow disambiguation; POST tier = softer
# grammars that only claim what cw declined. Acceptance filters keep weak
# signals from hijacking: identity only via guardian-rule hits AND an
# imperative/second-person signal ("lifetime rules" is not a rule for Omega),
# situational only via outcome/sentiment (5W1H fires on any question),
# reasoning excludes bare goal-words ('want/goal' too generic).
_GRAMMAR_PROBES_PRE = (
    # identity: guardian rules (4) gated on the imperative signal below;
    # traits/evolution/self-inquiry (2,3,5) are strong content vocabularies
    # ("what are my defining characteristics") accepted without it.
    (0, "remember_identity", frozenset({2, 3, 4, 5})),
    (9, "remember_time", None),
    (7, "remember_vision", None),
    (11, "remember_dev", None),
    (4, "remember_situation", frozenset({6, 7})),  # outcome/sentiment only
)
_GRAMMAR_PROBES_POST = (
    (5, "remember_procedure", None),
    (3, "remember_reasoning", frozenset({1, 2, 3, 4})),  # no bare goal-words
    # NO lobe-8 probe: part-word term-matching the coding taxonomy is too
    # greedy for free-form queries ("...how neurons encode memory" would match
    # a memory_* subdomain). Code routing stays with suggest_code_workflow_lobe.
)


def _grammar_probe(lowered: str, probes) -> str | None:
    """First lobe whose grammar recognizes the text (with acceptance filter)."""
    for lobe, tool, accept in probes:
        y = subdomain_for(lowered, lobe)
        if y is None:
            continue
        if accept is not None and y not in accept:
            continue
        if lobe == 0 and y == 4 and not _IMPERATIVE_SIGNAL.search(lowered):
            continue  # guardian-rule hits need an imperative/rule signal
        return tool
    return None


def route_memory_tools(query: str) -> List[Dict[str, Any]]:
    """Back-compat: Meta routes a single tool; returns a one-element list."""
    return [route_memory_tool(query)]


def route_names_non_recallable_lobe(query: str) -> int | None:
    """Lobe 6/7 ONLY when a high-precision intent regex names it — never the
    greedy grammar-probe fallback tier. This is the recall verdict's region-
    refusal authority (memory/recall_verifier._non_recallable_region_reason):
    an unrecoverable whole-verdict veto needs dispatch-grade intent evidence.
    The probe tier stays dispatch-only, where a broad match is recoverable."""
    lowered = str(query or "").strip().lower()
    if _JOURNAL_RE.search(lowered):
        return 6
    if _VISION_FACE_RE.search(lowered) or _VISION_SCENES_RE.search(lowered):
        return 7
    return None
