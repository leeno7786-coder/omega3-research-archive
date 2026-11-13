"""Agent-lane metacognition over acquisition or execution evidence and the build battery.

The lane, not the action, selects this prompt class. Inside it the evidence kind
picks the grounding: a read/fetch acquisition is comprehended into grounded
knowledge, applied work grounds application mastery, and a paused build keeps the
deterministic red/green battery authoritative.
"""

from collections.abc import Mapping

from .core import OPERATOR_DELIVERY, cross_lane_block, metacognitive_core, parse_lane


ACQUISITION_GROUNDING = '''ACQUIRED INFORMATION: the object level ran a read or fetch tool on a learning target.
Comprehend the RESULT and diff it against what you already RECALLED.
Optional lane payload knowledge_to_ingest: [{"domain":str,"statement":str,"confidence":number}]
holds only statements the result supports. Optional mastery holds knowledge_mastery and
application_mastery as ABSOLUTE [0,1] estimates of how much of the TARGET competency you can now
demonstrably cover (recall plus new result), not how much you learned today, with a rationale.
If nothing new was acquired keep the prior value; coverage may DROP when knowledge proves shallow.
When more learning is owed, PRESSURE intent=LEARN names topic as the SPECIFIC next sub-aspect,
advancing beyond what you already know so study does not repeat.
'''

TERMINAL_AGENT = '''TERMINAL AGENT: an operator's agent task runs one tool batch per episode. When the
batch's results show more tool work is needed, decide PRESSURE with intent=USE_TOOL and name the next
step in reason; the next episode runs that batch with these results. Decide DONE when the operator's
request is satisfied.
'''

APPLY_GROUNDING = '''APPLIED WORK: the object level used an executor to DO work in this domain.
Optional mastery grounds application_mastery as an ABSOLUTE [0,1] estimate of how much of the
TARGET competency you can now demonstrably APPLY: a clean successful execution moves it toward
target, a failed or empty run leaves or lowers it. knowledge_mastery usually HOLDS because applying
is not learning. Work that never executed (pending approval, blocked, no dispatch) changes no mastery.
'''


AGENT_POLICY = '''TERMINAL AGENT RUN: the object level ran ONE batch of the operator's agent tools in
their trusted folder (start_job, reading and searching files, writing and editing files, running
commands and tests, git inspection, web lookups). agent_run is the host's receipt: status, the batch
ledger with each call's result, touched_files, the files the project holds now, and the agent's own
summary or question. The operator's task normally needs several batches; each batch ends here, and
your decision is what runs next.
Decide from agent_run.status:
- job_claimed: the batch only claimed the project. PRESSURE intent=USE_TOOL, and reason IS the next
  concrete step: the files to write, or the command to run.
- batch_ran: calls ran. DONE only when the ledger and project_files show the operator's request is
  satisfied; otherwise PRESSURE intent=USE_TOOL with the next concrete step and what is still missing.
- done_claimed: the agent made no calls and reports the work finished. DONE only when project_files
  and the ledger show it; when the claim outruns the evidence, PRESSURE intent=USE_TOOL naming exactly
  what is missing.
- ask_operator: ASK_OPERATOR; synthesis is the agent's question, put to the operator plainly.
  agent_run.options are its numbered choices; the task is paused and their answer resumes it.
- no_action: nothing ran and nothing was asked. PRESSURE intent=USE_TOOL once with the next step
  stated plainly; when an earlier step in this task also produced no action, ASK_OPERATOR with the
  concrete blocker instead.
- blocked: every call failed. Never PRESSURE intent=USE_TOOL. DONE with a plain account of what
  failed, or ASK_OPERATOR when only the operator can unblock it.
agent_run.operator_notes are words the operator sent WHILE the batch ran, already handed to it:
they are instructions from them, and your decision answers the task as they have now stated it.
A successful call proves that call only; the request is proven by the files. reason is work for the
next batch, never a question addressed to the model.
'''


def _battery_control(wip: Mapping) -> str:
    verdict = wip.get("verification")
    if not (isinstance(verdict, Mapping) and verdict.get("all_green") is True):
        control = ("The deterministic battery is RED. CONTINUE with PRESSURE, intent=USE_SKILL, "
                   "execution_debt_delta grounded in red checks, or ASK_OPERATOR for a concrete blocker. "
                   "Completion is not available while any check is red or absent.\n")
        if wip.get("verification_stalled"):
            control += ("ESCALATION IS OPEN: red checks are UNCHANGED. Ask about the precise blocker "
                        "or continue only with a genuinely NEW approach.\n")
        return control
    control = ("The deterministic battery is GREEN. COMPLETE (the default) means DONE. "
               "Continuation is no longer on the table without a concrete defect the checks cannot see.\n")
    if wip.get("completed_awaiting_passoff"):
        control += ("PASS-OFF: work exists in the workspace. Accept with build_accepted=true and DONE; "
                    "a specific evidenced refinement permits build_accepted=false and PRESSURE "
                    "on existing files, never a rebuild.\n")
    return control


def build(packet):
    from omega_loop.episode import prompts

    system = metacognitive_core("agent") + OPERATOR_DELIVERY
    evidence = {"objective": packet.original_objective, "foldback_packet": packet.foldback_packet,
                "failure_reason": packet.failure_reason}
    if prompts.acquires_knowledge(packet):
        # The acquisition user block already renders the tool result and fetched knowledge.
        user = prompts._knowledge_user(packet)
        system += ACQUISITION_GROUNDING + prompts._onboarding_foldback_blocks(packet)[0]
        system += ("Optional lane payload: knowledge_to_ingest, mastery, onboarding_subtopics. "
                   "Evidence grounds every assessment.\n")
    else:
        user = prompts._apply_user(packet)
        system += prompts._FOLDBACK_MONITOR_PREFIX
        system += AGENT_POLICY if packet.agent_run else TERMINAL_AGENT
        system += "Optional lane payload: mastery, build_accepted. Evidence grounds every assessment.\n"
        system += _battery_control(packet.build_in_progress[0]) if packet.build_in_progress else APPLY_GROUNDING
        evidence.update(raw_tool_result=packet.raw_tool_result, fetched_knowledge=packet.fetched_knowledge,
                        agent_run=packet.agent_run)
    system += (prompts._self_report_note() + prompts._learning_map_block(packet)
               + prompts._execution_experience_block(packet))
    user += "\nOriginal objective and factual result:\n" + prompts._json_data_block(evidence, 24000)
    return system, user + cross_lane_block(packet)


def parse(payload, packet):
    from omega_loop.episode import prompts

    grounding = prompts._parse_knowledge if prompts.acquires_knowledge(packet) else prompts._parse_apply
    return parse_lane(payload, packet, grounding)
