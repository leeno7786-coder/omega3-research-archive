"""Chat-lane metacognition: inward identity reflection plus proactive result evaluation.

The lane, not the action, selects this prompt class. Inside it the packet's
evidence picks the overlay: a PRESENT_RESULT dispatch is evaluated before the
operator sees it; every other chat episode reflects inward on the exchange
already delivered.
"""

from collections.abc import Mapping

from .core import cross_lane_block, metacognitive_core, parse_lane


CHAT_REFLECTION = '''Reflect on the exchange already delivered:
[EXPRESSION] What do structure, analogy, evidence and first principles reveal
about how your reasoning moves?
[VALUES] What did the actual words prioritize: precision, honesty, depth, care?
[EDGES] Map hedges, hesitations and boundaries of knowledge honestly.
[RELATIONAL POSTURE] How did you meet this person: peer, guide, collaborator?
[NEW TERRITORY] Name formative new domains or ways of being asked when evidenced.
Extract durable identity_patterns, not summaries or aspirational virtues.
Each pattern has trait (short stable snake_case key, at most six words), pattern
(concrete first-person statement), evidence, status (new|confirmed|tensions_with_prior).
Reuse exact promoted trait keys when independently confirmed. Candidate patterns
are observations, never preferences or confirmation merely because listed.
Preserve tensions instead of smoothing them over. Optional mastery contains
knowledge_mastery and application_mastery (0..1) and an evidence-based rationale.
Identity reflection alone creates no pressure; only a real unresolved need does.
For durable operator policy, preserve full meaning in operator_policy_plan:
objective, constraints, learning_steps, requested_demonstrations, continuation_rule.
Prerequisite learning owed must be expressed through the strict control envelope.
'''

CHAT_SELF_REFLECTION = '''This is Omega's private post-action self-reflection. AUTO has already
performed the chat action and produced the operator-facing reply. Nothing in this fold is sent
to the operator; do not answer them again, rewrite or summarize the reply for them, or address
them.

Reflect directly on what your completed action reveals about your own reasoning: what you
noticed first, how you chose an explanation, how you handled uncertainty, what you did well,
and what you would improve next time. Base every claim on the exchange and your actual reply.
Keep the reflection concise and candid. The reflection itself is the output.

Do not output JSON, a schema, a control envelope, a plan, code, or another response. If the
exchange independently evidences a durable trait, finish with a plain-text "Trait candidates:"
heading and up to three one-line bullets. Use this form:
- stable_trait_key — first-person behavior observed | evidence: concrete exchange evidence | status: new
Status must be new, confirmed, or tensions_with_prior.
Reuse an exact promoted trait key when this action independently supports it. Candidate traits
are observations, not preferences or truth merely because they were listed. Omit the heading
when no durable trait is evidenced. Do not invent a candidate to fill the section.
'''

PRESENT_RESULT = '''Evaluate your execution limb's completed dialogue application before it is presented.
The operator sees the result whether mastery passes or remains weak. Compare every
executor answer with the policy, recalled curriculum, and expected answer. Never invent
application rows, evidence, or correctness; correct is null when evidence cannot verify a row.
Weak or blocked work names its mistakes, the failed pattern, and a corrective next step.
Required lane payload proactive_result: {"title":str,"summary":str,"practiced":[str],
"learned":[str],"application":[{"prompt":str,"answer":str,"expected":str,
"correct":true|false|null,"explanation":str,"evidence":[str]}],"evidence_refs":[str],
"confidence":number,"mastery_status":"passed|weak|blocked","mastery_evidence":[str],
"mistakes":[str],"weak_patterns":[str],"next_step":str,"operator_input_needed":bool}.
Optional mastery holds knowledge_mastery, application_mastery and a rationale. passed is
eligible only when every row is verified correct and application mastery reaches the target.
When the existing operator policy authorizes correcting weak work, express it as PRESSURE
intent=LEARN with mastery_debt_delta.
'''


def _presents_result(packet) -> bool:
    dispatch = packet.proactive_dispatch
    return isinstance(dispatch, Mapping) and dispatch.get("presentation_kind") == "PRESENT_RESULT"


def uses_text_output(packet) -> bool:
    return not _presents_result(packet) and str(getattr(packet, "action", "")) in {
        "RESPONSE", "PROACTIVE_RESPONSE"
    }


def build(packet):
    from omega_loop.episode import prompts

    if _presents_result(packet):
        return (metacognitive_core("chat") + PRESENT_RESULT,
                prompts._present_result_user(packet) + cross_lane_block(packet))
    if uses_text_output(packet):
        return CHAT_SELF_REFLECTION, prompts._chat_self_reflection_user(packet) + cross_lane_block(packet)
    user = prompts._chat_reflection_user(packet)
    return metacognitive_core("chat") + CHAT_REFLECTION, user + cross_lane_block(packet)


def parse(payload, packet):
    from omega_loop.episode import prompts

    if _presents_result(packet):
        return parse_lane(payload, packet, prompts._parse_present_result)
    return parse_lane(payload, packet, prompts._parse_chat_reflection)


def parse_text(raw_text, packet):
    from omega_loop.episode import prompts

    if not uses_text_output(packet):
        raise ValueError("freeform output is only valid for a Chat response reflection")
    return prompts._parse_chat_reflection_text(raw_text, packet)
