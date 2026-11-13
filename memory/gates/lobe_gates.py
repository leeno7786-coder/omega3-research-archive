"""Per-lobe memory gates — one gate per Dewey lobe.

Each gate scopes ingest/recall to a single lobe and its subdomains.
Cognitive systems compose these through the memory API layer above.

Lobe map (2026-05-06 restructure — see project_memory_lobe_restructure):
  0  identity    — constitution, personality, emergent_traits, evolution, guardian
  1  personas    — OTHER people; persona_router auto-allocates Y per person
  2  knowledge   — science, math, philosophy, tech, engineering, humanities, social_science, medicine, world, synthesis
  3  reasoning   — goals, decisions, constraints, heuristics, alignment
  4  situational — 5W1H + outcome + sentiment (system self-awareness)
  5  tools       — cognitive (web_fetch + voice_box-invokable) + dev (pipeline)
  6  journal     — meta, global_loop, omega, deep_sleep, observations
  7  vision      — images, face_embeddings, scene_descriptions, visual_patterns
  8  coding      — python, typescript, rust, go, c, cpp, sql, shell, html_css
  9  temporal    — sessions, events, milestones, history
  10 workflows   — code_authoring, code_debugging, code_review, code_testing,
                   code_refactoring, learn_concept, operator_recorded, tool_chain_template
  11 dev         — artifacts, progress, context, vision
"""

from memory.gates.base import MemoryGate
from memory.enrichment import (
    enrich_identity,
    enrich_personas,
    enrich_knowledge,
    enrich_vision,
    enrich_journal,
    enrich_situational,
    enrich_coding,
    enrich_workflow,
)
import logging

log = logging.getLogger("omega.memory.gates.lobe_gates")


class IdentityGate(MemoryGate):
    """Lobe 0 — constitution, personality, emergent traits, evolution, guardian."""

    READABLE_LOBES = [0]
    WRITABLE_LOBES = [0]
    DECOMPOSERS = {0: enrich_identity}
    SOURCE_TAG = "lobe:identity"


class PersonasGate(MemoryGate):
    """Lobe 1 — OTHER people the system has met. Persona records are
    keyed by (1, 0, person_id) where the third tier is the per-person
    namespace allocated by `core.persona_router` on first encounter."""

    READABLE_LOBES = [1]
    WRITABLE_LOBES = [1]
    DECOMPOSERS = {1: enrich_personas}
    SOURCE_TAG = "lobe:personas"


class KnowledgeGate(MemoryGate):
    """Lobe 2 — all knowledge subdomains + 3-tier domain routing."""

    READABLE_LOBES = [2]
    WRITABLE_LOBES = [2]
    DECOMPOSERS = {2: enrich_knowledge}
    SOURCE_TAG = "lobe:knowledge"


class ReasoningGate(MemoryGate):
    """Lobe 3 — goals, decisions, constraints, heuristics, alignment."""

    READABLE_LOBES = [3]
    WRITABLE_LOBES = [3]
    DECOMPOSERS = {}
    SOURCE_TAG = "lobe:reasoning"


class SituationalGate(MemoryGate):
    """Lobe 4 — 5W1H + outcome + sentiment. SYSTEM self-awareness:
    every meta choice / auto attempt / global outcome lands here as
    a full event-recollection record (success/failure_reason/
    sentiment/intensity/linked_persons/linked_coding)."""

    READABLE_LOBES = [4]
    WRITABLE_LOBES = [4]
    DECOMPOSERS = {4: enrich_situational}
    SOURCE_TAG = "lobe:situational"


class ToolsGate(MemoryGate):
    """Lobe 5 — tools registry. RENAMED 2026-05-08 from SkillsGate.

    Two scopes (per dewey_map SUBDOMAIN_MAP):
      (5, 0) cognitive: voice_box-invokable tools (web_fetch + future
                        operator-callable synthesized tools).
      (5, 1) dev:       dev-pipeline tools (coding_project_*,
                        dev_edit_file, dev_git_*, etc.).

    Scripted-I/O lobe — no SetFit at this level. Source-tag dispatch:
    guardian writes registrations, voice_box / dev pipeline read.

    Override note (2026-05-11): ingest() honors metadata['registry_scope']
    to pick Y deterministically. The base classifier path mis-routes
    every dev tool to (5,0) cognitive because the runtime classifier
    sees "tool schema catalog entry" text and picks Y=0 regardless of
    the explicit dev/cognitive declaration. Per
    project_guardian_tool_registrar: "discerns subdomain from manifest
    scope ('cognitive'→(5,0), 'dev'→(5,1))" — the manifest is the
    authority, not the SetFit's text guess.
    """

    READABLE_LOBES = [5]
    WRITABLE_LOBES = [5]
    DECOMPOSERS = {}
    SOURCE_TAG = "lobe:tools"

    # Subdomain Y per dewey_map SUBDOMAIN_MAP for lobe 5.
    _SCOPE_TO_Y = {"cognitive": 0, "dev": 1}

    def ingest(self, text, metadata=None):
        # If caller declared a registry_scope, honor it deterministically
        # — bypass the runtime classifier (which doesn't know about
        # registry_scope and otherwise routes everything to Y=0).
        scope = None
        if metadata:
            scope = metadata.get("registry_scope") or (metadata.get("constraints") or {}).get(
                "registry_scope"
            )
        if scope not in self._SCOPE_TO_Y:
            # No declaration → fall back to base classifier path
            # (preserves behavior for any future caller that doesn't set
            # registry_scope explicitly).
            return super().ingest(text, metadata)

        y = self._SCOPE_TO_Y[scope]
        coord = [5, y, 0]

        # Mirror the base ingest pipeline minus the classify step:
        # Guardian raw-text check, enrichment, atomic write.
        if not self._guardian_check(text, source=self.SOURCE_TAG):
            log.info("Guardian blocked write from %s", self.SOURCE_TAG)
            return None
        enrichment = self._enrich(text, lobe=5)
        path = self._write(text, coord, enrichment, metadata)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "gate_ingest",
                summary=f"scope={scope} coord={coord}",
                gate="tools",
                registry_scope=scope,
                coord=coord,
                ok=path is not None,
            )
        except Exception:
            pass
        return path


class JournalGate(MemoryGate):
    """Lobe 6 — meta, global_loop, omega, deep_sleep, observations."""

    READABLE_LOBES = [6]
    WRITABLE_LOBES = [6]
    DECOMPOSERS = {6: enrich_journal}
    SOURCE_TAG = "lobe:journal"


class VisionGate(MemoryGate):
    """Lobe 7 — images, face embeddings, scene descriptions, visual patterns."""

    READABLE_LOBES = [7]
    WRITABLE_LOBES = [7]
    DECOMPOSERS = {7: enrich_vision}
    SOURCE_TAG = "lobe:vision"


class CodingGate(MemoryGate):
    """Lobe 8 — coding knowledge keyed by language (Y) and concern (Z).
    2-tier shape parallel to KnowledgeGate; see CODING_DOMAINS in
    memory.dewey_map for the language→concern catalog."""

    READABLE_LOBES = [8]
    WRITABLE_LOBES = [8]
    DECOMPOSERS = {8: enrich_coding}
    SOURCE_TAG = "lobe:coding"


class TemporalGate(MemoryGate):
    """Lobe 9 — sessions, events, milestones, history."""

    READABLE_LOBES = [9]
    WRITABLE_LOBES = [9]
    DECOMPOSERS = {}
    SOURCE_TAG = "lobe:temporal"


class WorkflowsGate(MemoryGate):
    """Lobe 10 — operator-recorded sequences + autonomously detected
    patterns + tool-chain templates. Feeds the CLI integration where
    the system codes + teaches itself about code."""

    READABLE_LOBES = [10]
    WRITABLE_LOBES = [10]
    DECOMPOSERS = {10: enrich_workflow}
    SOURCE_TAG = "lobe:workflows"


class DevGate(MemoryGate):
    """Lobe 11 — dev artifacts, progress, context, vision. Isolated from brain."""

    READABLE_LOBES = [11]
    WRITABLE_LOBES = [11]
    DECOMPOSERS = {}
    SOURCE_TAG = "lobe:dev"
