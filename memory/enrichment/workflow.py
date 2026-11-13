"""Workflow enrichment (lobe 10) — detect workflow kind + step payload.

Per `project_memory_lobe_restructure` (2026-05-06): lobe 10 stores
workflows that feed the CLI integration where the system codes and
teaches itself about code. Three workflow sources count:
  1. Operator-recorded sequences (kind=operator_recorded)
  2. Autonomously detected patterns (any of the code_* kinds)
  3. Tool-chain templates (kind=tool_chain_template)

This decomposer assigns the kind from cues and bundles the step
payload (a list of operations) so downstream consumers can replay it.

Pattern-based, deterministic, no LLM.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from memory.dewey_map import WORKFLOW_KINDS

log = logging.getLogger("omega.memory.enrichment.workflow")


# ── Kind detection cues ──
# Order matters: more-specific cues first so e.g. "operator recorded"
# wins over the generic "code authoring" pattern.
_KIND_CUES: List[tuple] = [
    (r"\boperator[- ]recorded\b|\btaught by user\b|\buser workflow\b", "operator_recorded"),
    (
        r"\btool[- ]chain template\b|\bcomposite plan\b|\bmulti[- ]tool sequence\b",
        "tool_chain_template",
    ),
    (r"\bdebug(?:ging)?\b|\bfix\b|\bbug\b|\btraceback\b|\berror\b", "code_debugging"),
    (r"\bauthor(?:ing)?\b|\bwrite (?:new )?code\b|\bcreate (?:a )?function\b", "code_authoring"),
    (r"\breview\b|\bread (?:through )?(?:the )?code\b|\baudit\b", "code_review"),
    (r"\btests?\b|\btesting\b|\bunit tests?\b|\bpytest\b|\bjest\b|\bvitest\b", "code_testing"),
    (r"\brefactor(?:ing)?\b|\brestructure\b|\bcleanup\b", "code_refactoring"),
    (
        r"\blearn (?:about )?(?:a )?(?:concept|topic)\b|\bstudy\b|\bunderstand how\b",
        "learn_concept",
    ),
]


def _detect_kind(text_lo: str, hint: Optional[str]) -> Optional[str]:
    """Pick a workflow kind. Caller hint wins if valid."""
    if hint and hint in WORKFLOW_KINDS:
        return hint
    for pattern, kind in _KIND_CUES:
        if re.search(pattern, text_lo):
            return kind
    return None


def _extract_steps(text: str, hint: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Pull a list of step descriptions from the text. Caller hint
    wins (e.g. operator-recorded workflows arrive with a structured
    steps payload). Otherwise scan for numbered/bulleted lines."""
    if hint:
        return [s if isinstance(s, dict) else {"description": str(s)} for s in hint]
    steps: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(?:\d+\.|[-*])\s+(.+)$", line)
        if m:
            steps.append({"description": m.group(1).strip()})
    return steps


def enrich(text: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Decompose a workflow write into kind + steps."""
    if not text or not isinstance(text, str):
        return None
    text_lo = text.lower()
    kind = _detect_kind(text_lo, kwargs.get("kind"))
    if not kind:
        log.debug("workflow: no kind signal in %r", text[:60])
        return None
    steps = _extract_steps(text, kwargs.get("steps"))
    return {
        "kind": kind,
        "step_count": len(steps),
        "steps": steps,
        "operator_recorded": kind == "operator_recorded",
        "summary": text[:240],
    }
