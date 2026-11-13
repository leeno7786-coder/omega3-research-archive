"""Decomposer enrichment modules. Each exports enrich(text) -> Optional[Dict].

Per `project_guardian_sole_writer_doctrine` (2026-05-08): each enricher
is named for its CURRENT canonical lobe — no backwards-compat aliases.
A function aliased to a different name is a lie about what it does.

Lobe-to-enricher map:
  0 identity     → enrich_identity
  1 personas     → enrich_personas        (was: enrich_personal)
  2 knowledge    → enrich_knowledge
  4 situational  → enrich_situational
  6 journal      → enrich_journal
  7 vision       → enrich_vision
  8 coding       → enrich_coding
  10 workflows   → enrich_workflow
"""

from memory.enrichment.identity import enrich as enrich_identity
from memory.enrichment.personas import enrich as enrich_personas
from memory.enrichment.knowledge import enrich as enrich_knowledge
from memory.enrichment.vision_enrichment import enrich as enrich_vision
from memory.enrichment.journal import enrich as enrich_journal
from memory.enrichment.situational import enrich as enrich_situational
from memory.enrichment.coding import enrich as enrich_coding
from memory.enrichment.workflow import enrich as enrich_workflow

ENRICHMENT_MAP = {
    0: enrich_identity,
    1: enrich_personas,
    2: enrich_knowledge,
    4: enrich_situational,
    6: enrich_journal,
    7: enrich_vision,
    8: enrich_coding,
    10: enrich_workflow,
}
