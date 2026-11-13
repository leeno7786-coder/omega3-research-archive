"""
Provenance Schema
=================

Origin record attached to every persistent object. Required by the
Schema Contract — no freeform persistence is permitted, and every
object must declare where it came from, who authored it, when it was
created, and how it can be traced back.

The system enforces this at runtime: a persistence operation that
arrives without provenance is REJECTED by gate_policy. This is what
makes the contract a runtime invariant rather than a developer
guideline.

Used by:
    - memory_object.py        — every memory record
    - reflection.py           — every metacognitive reflection
    - policy_proposal.py      — every proposal Meta/Omega emits
    - guardian_intervention.py — every guardian decision
    - tool_output.py          — every tool result
    - cycle_record.py         — every cycle persisted
    - any future persistent type

ARCHITECTURAL RULES:
    - No persistence without provenance.
    - origin_id must be stable across system restarts.
    - ingested_at and created_at are distinct: created_at is when the
      object first existed; ingested_at is when this system saw it.
    - This schema MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import logging

log = logging.getLogger("omega.schema.provenance")


@dataclass(frozen=True)
class Provenance:
    """Where a persistent object came from."""

    # Stable identifier of what produced this object.
    # Examples:
    #   "lobe:knowledge"            — gate-tagged ingest
    #   "phase_3_inference"         — autonomous loop phase
    #   "meta_loop.synthesis"       — meta layer decision
    #   "omega.policy_designer"     — omega proposal
    #   "tool:web_fetch"            — tool output
    #   "user:noah"                 — direct user input
    origin_id: str

    # What kind of source this was. Drives downstream policy decisions.
    #   "ingest"     — raw input from outside the system
    #   "inference"  — model output
    #   "reflection" — metacognitive output
    #   "policy"     — policy/proposal emission
    #   "tool"       — tool execution result
    #   "guardian"   — guardian decision
    #   "verify"     — verification result
    #   "synthesis"  — system-synthesized (e.g. Deep Sleep tool creation)
    #   "user"       — direct user input
    origin_type: str

    # The author of the object. For inference: model name + version.
    # For phases: phase id. For tools: tool name + version. For users:
    # user identifier.
    author: str

    # ISO-8601 UTC. When the object was first created at its source.
    created_at: str

    # ISO-8601 UTC. When this system persisted the object. May equal
    # created_at for system-internal objects.
    ingested_at: str

    # Free-form parent reference for chained provenance. Lets one
    # persistent object cite another — e.g. a reflection cites the
    # cycle record it reflected on; a tool synthesized by Deep Sleep
    # cites the reflection that proposed it.
    parent_id: Optional[str] = None

    # Free-form session/cycle/epoch ref, if applicable.
    cycle_id: Optional[str] = None
