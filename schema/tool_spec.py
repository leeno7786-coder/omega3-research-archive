"""
Tool Spec Schema
================

Canonical definition of a tool. Required by the Tool Contract — every
callable in the system (whether human-authored or synthesized by Deep
Sleep) declares itself through this schema before it can be registered
in the tool registry.

The Tool Contract requires every tool to be:
    - bounded: clear inputs, outputs, side effects
    - explainable: a human-readable description
    - schema-validated: parameters match a JSON Schema
    - versioned: every revision gets a new version
    - safe under Omega constraints: declared safety class

ARCHITECTURAL RULES:
    - Tools that fail schema validation cannot be registered
      (tool_policy enforces this).
    - Synthesized tools (origin_type="synthesis") undergo an extra
      verification step before registration.
    - Tool revision creates a new ToolSpec; the old one is retired
      via a PolicyProposal of kind="tool_retirement".
    - This schema MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from schema.provenance import Provenance
import logging

log = logging.getLogger("omega.schema.tool_spec")


SafetyClass = Literal[
    "safe",  # read-only, no side effects (e.g. recall, grep)
    "read_only",  # may read external data (e.g. web_fetch)
    "mutating",  # writes to memory or filesystem
    "destructive",  # deletes or overwrites
    "dangerous",  # arbitrary code execution
]


ToolOriginType = Literal[
    "human_authored",  # written by an operator
    "synthesized",  # created by Deep Sleep tool synthesis
    "imported",  # pulled from an external library
    "user_provided",  # provided by user at runtime
]


@dataclass(frozen=True)
class ToolParameter:
    """One parameter on a tool's call signature."""

    name: str
    type: str  # JSON Schema type: "string", "integer", "object", etc.
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[List[Any]] = None  # allowed values, if constrained


@dataclass(frozen=True)
class ToolSpec:
    """Canonical tool definition."""

    # Identity + version
    name: str
    version: str  # SemVer: "1.0.0", "2.1.3"
    tool_id: str  # stable id: name@version

    # Description (used by the model + by tool_policy for selection)
    description: str
    summary: str = ""  # one-line summary for model menus

    # Provenance + safety
    provenance: Provenance = None  # how this tool came to exist
    origin_type: ToolOriginType = "human_authored"
    safety: SafetyClass = "safe"

    # Call signature
    parameters: List[ToolParameter] = field(default_factory=list)
    returns_schema: Dict[str, Any] = field(default_factory=dict)  # JSON Schema for output

    # Implementation pointer — module path that holds the callable.
    # tool_policy resolves this to a real callable when registering.
    implementation_module: str = ""  # e.g. "tools.web_fetch"
    implementation_function: str = ""  # e.g. "web_fetch"
    implementation_source: str = (
        ""  # generated module source (omega synth); "" = points at an existing module
    )

    # Lifecycle
    deprecated: bool = False
    superseded_by: Optional[str] = None  # tool_id of replacement
    retired_at: Optional[str] = None  # ISO-8601 if retired

    # Verification chain — every synthesized tool must verify before
    # registration. Records the verification + intervention IDs.
    verification_id: Optional[str] = None
    intervention_id: Optional[str] = None

    # Constraints — bounds on tool usage. tool_policy may decline to
    # invoke a tool if a constraint would be violated.
    constraints: Dict[str, Any] = field(default_factory=dict)
    # Examples:
    #   {"rate_limit_per_minute": 30}
    #   {"max_payload_chars": 100000}
    #   {"allowed_protocols": ["https"]}

    # Tags — for selection / filtering / search
    tags: List[str] = field(default_factory=list)

    # =================================================================
    # Runtime conversion helpers
    # =================================================================
    #
    # These methods translate the canonical ToolSpec into the shapes
    # the runtime + the model need. The schema dataclass is the single
    # source of truth; consumers (autonomous Phase 2, brain dispatch,
    # any future caller) get one consistent way to ask "give me the
    # OpenAI tool dict for this spec." No alias zoo.


# =====================================================================
# Module-level helpers (work with raw schema files, not just dataclass
# instances)
# =====================================================================


def load_spec_from_schema_file(schema_path) -> Optional[ToolSpec]:
    """Load a ToolSpec from its on-disk JSON schema. Returns None if
    the file doesn't exist or doesn't conform to the canonical shape.

    Schema files at `schema/tools/<name>.json` round-trip with
    this loader: dump via `dataclasses.asdict()` (with `parameters`
    serialised as a list of ToolParameter dicts), reload via this
    function. The canonical web_fetch.json is the reference example.
    """
    import json as _json
    from pathlib import Path as _Path

    p = _Path(schema_path)
    if not p.exists():
        return None
    try:
        raw = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None
    # Drop metadata fields the dataclass doesn't carry (`_schema`,
    # `_schema_version`).
    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    try:
        params = data.get("parameters") or []
        data["parameters"] = [ToolParameter(**pp) if isinstance(pp, dict) else pp for pp in params]
        return ToolSpec(**data)
    except (TypeError, ValueError):
        return None
