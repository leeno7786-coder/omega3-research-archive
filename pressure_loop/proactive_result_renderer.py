"""Deterministic operator-facing rendering for structured proactive results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from schema.proactive_lifecycle import ProactiveResult


def render_proactive_result(value: ProactiveResult | Mapping[str, Any]) -> str:
    """Render evidence fields without exposing prompts or hidden reasoning."""
    result = value if isinstance(value, ProactiveResult) else ProactiveResult.from_dict(value)
    lines = [f"Omega result: {result.title}", "", result.summary]
    if result.practiced:
        lines.extend(["", "What I practiced:"])
        lines.extend(f"- {item}" for item in result.practiced)
    if result.learned:
        lines.extend(["", "What I learned:"])
        lines.extend(f"- {item}" for item in result.learned)
    lines.extend(["", "Application results:"])
    for index, row in enumerate(result.application, start=1):
        verdict = "correct" if row.correct is True else "incorrect"
        if row.correct is None:
            verdict = "not independently verified"
        expected = f" Expected: {row.expected}." if row.expected else ""
        lines.append(
            f"{index}. {row.prompt} → {row.answer} ({verdict}).{expected} Why: {row.explanation}"
        )

    lines.append("")
    if result.mastery_status == "passed":
        lines.extend(
            [
                "Mastery status: Passed",
                "Mastery evidence: "
                + "; ".join(result.mastery_evidence or ("Verified application passed.",)),
                "Next unlocked step: " + result.next_step,
            ]
        )
    elif result.mastery_status == "blocked":
        lines.extend(
            [
                "Mastery status: Blocked",
                f"Confidence: {result.confidence:.0%}",
                "Weakness: "
                + "; ".join(result.weak_patterns or ("Application evidence was incomplete.",)),
                "Next corrective step: " + result.next_step,
                "Operator input needed: " + ("Yes" if result.operator_input_needed else "No"),
            ]
        )
    else:
        lines.extend(
            [
                "Mastery status: Not yet passed",
                f"Confidence: {result.confidence:.0%}",
                "Mistakes: "
                + "; ".join(result.mistakes or ("No verified mistakes were returned.",)),
                "Weakness: " + "; ".join(result.weak_patterns or ("Evidence was incomplete.",)),
                "Next corrective step: " + result.next_step,
                "Operator input needed: " + ("Yes" if result.operator_input_needed else "No"),
            ]
        )
    return "\n".join(lines).strip()


__all__ = ["render_proactive_result"]
