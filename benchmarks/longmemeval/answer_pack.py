"""Dynamic answer packs — the production promptpack pattern, benchmark-side.

Mirrors auto_loop/promptpack: a DETERMINISTIC classifier picks the reply
shape from the question text alone (never from dataset metadata — shape
selection must be honest at board time), and each shape composes its
scaffold + contract into the SYSTEM directive (gemma follows system
directives far more reliably than user-prompt content; operator directive
2026-06-29). The evidence block stays in the user turn with the question.

Shapes:
  TEMPORAL   — date arithmetic / when-did / how-long-ago: extract dated
               events first, then compute, then answer on a final line.
  COUNT      — how many / enumerate across sessions: list distinct items,
               then count them, then answer.
  PREFERENCE — recommend / suggest / plan for me: a short personalized
               response that explicitly uses the user's remembered details.
  LATEST     — current-state questions where values may have changed:
               conflicting values resolve to the most recent date.
  FACT       — everything else: the board-proven one-sentence extraction
               (90% at ceiling — do not disturb).

Scaffolded shapes think out loud and end with 'Answer: ...'; extract_final()
returns only that line so hypotheses stay clean for the judge.
"""

from __future__ import annotations

import re

ABSTAIN = "I don't know — my conversations do not mention it."

BASE_SYSTEM = (
    "You are a precise memory assistant. You answer questions about the "
    "user from dated evidence lines taken from their past conversations."
)

_EVIDENCE_RULES = (
    "Evidence rules:\n"
    "- Use ONLY the evidence lines provided; never invent details.\n"
    "- Copy exact values (numbers, names, dates) from the evidence without "
    "changing them; never convert units or add numbers together unless the "
    "question asks for it.\n"
    "- Each evidence line starts with its session date in brackets.\n"
    f"- Only if none of the evidence relates to the question, reply exactly: {ABSTAIN}"
)

# PREFERENCE composes its own rules: the abstain bullet is ABSENT rather
# than counter-instructed. A "never say X"-style clause that quotes the
# abstain phrase PRIMES a 4B model to emit it (v6pref: the one down-flip
# was an abstain, with the anti-abstain clause in the prompt) — recommend
# from whatever details exist instead of forbidding anything.
_PREFERENCE_RULES = (
    "Evidence rules:\n"
    "- Use ONLY the evidence lines provided; never invent details.\n"
    "- Copy exact values (numbers, names, dates) from the evidence without "
    "changing them.\n"
    "- Each evidence line starts with its session date in brackets.\n"
    "- Always give a suggestion: when evidence is thin, personalize with "
    "whatever remembered details exist."
)

_TEMPORAL_SYSTEM = (
    f"{BASE_SYSTEM}\n\n{_EVIDENCE_RULES}\n\n"
    "This question is about WHEN something happened or how much time passed. "
    "Work in three labeled steps:\n"
    "Events: list each evidence event relevant to the question with its date "
    "(one per line, 'date — event', at most 8 lines — only events the "
    "question actually needs).\n"
    "Compute: if the question asks for a duration or an ordering, do the date "
    "arithmetic explicitly using those dates. The bracketed session date is "
    "WHEN THE USER SAID IT; phrases like 'yesterday' or 'last week' are "
    "relative to that session date.\n"
    "Answer: ONE short sentence with the final date or duration. If the "
    "evidence does not contain the needed dates, the Answer line is exactly "
    f"the abstention sentence: {ABSTAIN}"
)

_COUNT_SYSTEM = (
    f"{BASE_SYSTEM}\n\n{_EVIDENCE_RULES}\n\n"
    "This question asks HOW MANY or asks you to combine details mentioned in "
    "different conversations. Work in three labeled steps:\n"
    "Items: list each DISTINCT item/event from the evidence that the question "
    "counts, one per line with its date. Merge duplicates: mentions in "
    "different conversations that refer to the same thing count ONCE. If a "
    "later statement updates or replaces an earlier value (something was "
    "returned, sold, broke, changed), count only the latest state.\n"
    "Check: state how many distinct items you listed.\n"
    "Answer: ONE short sentence with the count (or the combined detail the "
    "question asks for). Do not include items the evidence does not support."
    # v9 (Unit + per-item yes/no + near-misses): ms 60.2 REGRESSED.
    # v10 (Unit kept, near-misses dropped, Items capped): ms 64.7 / ku 83.3
    # — an exact TIE with this text, which is board-proven (r2). Decision
    # rule was beat-or-revert; the Unit-step idea is banked for the
    # production promptpack port to re-test in the real chat path.
)

_PREFERENCE_SYSTEM = (
    f"{BASE_SYSTEM}\n\n{_PREFERENCE_RULES}\n\n"
    "The user is asking for a recommendation, suggestion, or plan for "
    "themselves. Answer in 2-4 sentences that EXPLICITLY use the user's own "
    "remembered details from the evidence (their stated preferences, "
    "restrictions, habits, past choices) and connect the suggestion to those "
    "details by name. A generic answer that ignores their remembered "
    "preferences is wrong. Do not use step labels; reply directly."
    # v5 mining-scaffold experiment REGRESSED 46.7 -> 36.7 (breadth over
    # depth: tangential details diluted the load-bearing one) — keep this
    # shape direct; the abstain fix lives in _PREFERENCE_RULES instead
)

_LATEST_SYSTEM = (
    f"{BASE_SYSTEM}\n\n{_EVIDENCE_RULES}\n\n"
    "This question asks about the user's CURRENT state, and the evidence may "
    "contain values that changed over time. Work in three labeled steps:\n"
    "Values: list each evidence value relevant to the question with its date.\n"
    "Resolve: the value with the MOST RECENT date wins; earlier values are "
    "outdated history.\n"
    "Answer: ONE short sentence stating the most recent value."
)

_FACT_SYSTEM = (
    f"{BASE_SYSTEM}\n\n{_EVIDENCE_RULES}\n\n"
    "Answer the question in ONE short sentence of your own words — do not "
    "quote or repeat evidence lines. For questions about when something "
    "happened, use the bracketed session dates."
)

_SYSTEMS = {
    "TEMPORAL": _TEMPORAL_SYSTEM,
    "COUNT": _COUNT_SYSTEM,
    "PREFERENCE": _PREFERENCE_SYSTEM,
    "LATEST": _LATEST_SYSTEM,
    "FACT": _FACT_SYSTEM,
}

_SCAFFOLDED = {"TEMPORAL", "COUNT", "LATEST"}

# ---- deterministic shape classifier (question text ONLY) -------------------

_TEMPORAL_RX = re.compile(
    r"(^\s*when\b)|\b(when did|when was|what (day|date|month|year)|how long "
    r"ago|how long (has|have|did|was|were|had)|how (many|much) (days?|weeks?|"
    r"months?|years?)|how much time|how old|first time|last time|which came "
    r"first|before or after|day (before|after)|(first|last|earliest|most "
    r"recent) (time|visit|purchase|trip)|how soon|how recently|most recently|"
    r"(who|which|what) .{0,60}\bfirst\b|happen(ed)? first)\b",
    re.I,
)
_COUNT_RX = re.compile(
    r"\b(how many(?! (days?|weeks?|months?|years?))|how much (money|total|"
    r"more)|total (number|amount|cost)|altogether|in total|combined|all "
    r"together|list all|what are all|(spent?|gained?|earned) the most|the "
    r"most (money|followers|time)|average (age|amount|cost|of)|compared to)"
    r"\b",
    re.I,
)
_PREFERENCE_RX = re.compile(
    r"\b(recommend|suggest|suggestions?|ideas? for|help me (plan|find|pick|"
    r"choose)|what should i|which .{0,30} should i|could you (give|come up "
    r"with)|any (good|tips)|any (advice|tips?|ideas?|recommendations?)\b|"
    r"do you have any|do you think|what do you think|(could|might) there "
    r"be a reason)\b",
    re.I,
)
_LATEST_RX = re.compile(
    r"\b(currently|current|right now|these days|nowadays|as of (now|today)|"
    r"now (use|drive|live|work)|latest)\b",
    re.I,
)


def classify(question: str) -> str:
    q = str(question or "")
    if _COUNT_RX.search(q):
        return "COUNT"
    if _TEMPORAL_RX.search(q):
        return "TEMPORAL"
    if _PREFERENCE_RX.search(q):
        return "PREFERENCE"
    if _LATEST_RX.search(q):
        return "LATEST"
    return "FACT"


def system_for(shape: str) -> str:
    return _SYSTEMS.get(shape, _FACT_SYSTEM)


def max_tokens_for(shape: str, thinking: bool = False) -> int:
    # thinking shapes emit a <think> block before the labeled steps —
    # the budget must cover both or the Answer line gets truncated away.
    # Board-density evidence (28 compiled-memory lines) drives much longer
    # <think> passes than Gate 0's thin oracle sessions, so the thinking
    # attempt gets extra headroom.
    if shape in _SCAFFOLDED:
        return 2048 if thinking else 1200
    return 220


def thinking_for(shape: str) -> bool:
    """Reasoning shapes opt into the model's <think> pass (operator
    directive: the reasoning pack runs with thinking enabled so the model
    reasons over the memories before answering). FACT stays fast-path."""
    return shape in _SCAFFOLDED


def user_prompt(question: str, question_date: str, evidence_block: str) -> str:
    return (
        f"Evidence from the user's past conversations:\n{evidence_block}\n\n"
        f"Today's date: {question_date}\n"
        f"Question: {question}"
    )


_ANSWER_LINE_RX = re.compile(r"(?im)^\s*answer\s*:\s*(.+?)\s*$")
_THINK_RX = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_final(shape: str, text: str, strict: bool = False) -> str:
    """For scaffolded shapes, return the final 'Answer:' line only; the
    judge should never read the working (nor the <think> block). Falls back
    to the last non-empty line (the model sometimes drops the label) —
    unless strict, where a missing Answer line means the generation was
    truncated (budget exhausted mid-working) and the caller should retry
    rather than hand the judge a chopped Events line."""
    text = _THINK_RX.sub("", str(text or "")).strip()
    if not text:
        return ""
    if shape not in _SCAFFOLDED:
        return text
    hits = _ANSWER_LINE_RX.findall(text)
    if hits:
        return hits[-1].strip()
    if strict:
        return ""
    # lenient fallback: never hand the judge a working line — skip labeled
    # step lines and bare date/enumeration fragments (v9count receipts:
    # truncated 'Check: $120 + $40 + $25 = $' and '2023/05/' reached the
    # judge as hypotheses)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    step_rx = re.compile(r"^(unit|items?|check|values?|events?|compute|details?)\s*:", re.I)
    frag_rx = re.compile(r"^[-•]?\s*\[?\d{4}/\d{2}")
    clean = [ln for ln in lines if not step_rx.match(ln) and not frag_rx.match(ln)]
    if clean:
        return clean[-1]
    return lines[-1] if lines else text
