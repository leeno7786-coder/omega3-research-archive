"""Dewey 3D Cortex — Omega-native taxonomy.

Single source of truth for the memory coordinate system.
Every lobe, subdomain, and knowledge domain maps here.

Coordinate system: (X, Y, Z)
  X = lobe (0-9)
  Y = subdomain (0-N per lobe)
  Z = domain-specific (for knowledge lobe, maps to autonomous_knowledge.json domains)

Usage:
    from memory.dewey_map import LOBE_MAP, SUBDOMAIN_MAP, KNOWLEDGE_DOMAINS
"""

import logging

log = logging.getLogger("omega.memory.dewey_map")


# ── Lobe directories (X coordinate) ──
# 2026-05-06 restructure (per `project_memory_lobe_restructure`):
# 1 personal → personas (other people; persona_router auto-allocates Y).
# 4 context  → situational (5W1H + outcome + sentiment for SYSTEM
#              self-awareness — every meta choice / auto attempt /
#              global outcome lands here as full event recollection).
# 8 (NEW)    coding (Y=language, Z=concern; parallel to KNOWLEDGE_DOMAINS).
# 10 (NEW)   workflows (CLI integration — the system codes + teaches itself).
LOBE_MAP = {
    0: "000_identity",
    1: "100_personas",
    2: "200_knowledge",
    3: "300_reasoning",
    4: "400_situational",
    5: "500_tools",
    6: "600_journal",
    7: "700_vision",
    8: "800_coding",
    9: "900_temporal",
    10: "1000_workflows",
    11: "1100_dev",
}

# ── Non-recallable lobes (single source of truth) ──
# THE RECALL-LANE RULE (operator, established at lane inception; re-affirmed +
# made STRUCTURAL 2026-07-18): some lobes are outside the query-recall lane
# entirely — no caller-supplied allowed/denied lobe set can reach them. This is
# a boundary, not a default: the old DENIED_LOBES_DEFAULT was overridable by an
# explicit allowed_lobes, which let the replay harness (and any future caller)
# read what should never surface on query.
#   6 (600_journal): the DeepSleep journal is a METACOGNITIVE STATE, not a
#     memory. Its consumers (DeepSleep housekeeping, loop telemetry cascade)
#     read the filesystem directly (FS=TRUTH) — never the recall organs.
#   7 (700_vision): visual artifacts + face models recall through the DEDICATED
#     visual pipeline (operator 2026-07-18; pipeline pending — until it lands,
#     lobe-7 content is stored but not query-recallable). Ingest is unchanged.
# Enforced at every recall chokepoint: recall_graph.load_canonical_candidates,
# recall_graph.expand_via_graph, dme.adapter.recall_candidates, and the graph
# compiler's projection set. Import this constant; never hardcode {6, 7}.
NON_RECALLABLE_LOBES: frozenset = frozenset({6, 7})

# ── Entity-bearing lobes (single source of truth) ──
# Lobes whose records carry proper-noun anchors that warrant cross-lobe
# linking + supersedes-chain tracking + brain-side recall always-allow.
# Personas (1) hold "X is my dad" / "X is my cat" anchors; situational
# (4) holds "X likes Y" / "X did Y" assertions about the same entities.
# Adding a lobe here propagates to: entity_linker._LINKABLE_LOBES,
# supersedes._CHAIN_LOBES, brain.phases.retrieve._ENTITY_BEARING_LOBES,
# memory.api write_memory post-commit gate, and gates/base.py post-write
# hook — all of which import this constant. DO NOT hardcode `{1, 4}`
# anywhere downstream; import ENTITY_BEARING_LOBES instead.
ENTITY_BEARING_LOBES: frozenset = frozenset({1, 4})

# ── Scripted-I/O lobes (X coordinates that bypass the SetFit) ──
# Per operator (2026-05-08): lobes whose ingest is fully deterministic
# don't need a learned classifier. Routing happens via source-tag
# dispatch (gate-side) and the directory tree still exists; only the
# SetFit's stage-1 + stage-2b outputs exclude these lobes' label space.
#
# Current scripted-I/O lobes (per operator 2026-05-08):
#   3 reasoning — REPLACED by situational 5W1H. Reasoning is "why
#                 X happened"; situational.who/what/why/outcome
#                 captures that in event-recollection form. Lobe
#                 directory may stay for legacy reads but no SetFit.
#   5 skills    — to be RENAMED to "tools" (follow-up). Guardian is
#                 the sole writer (tool registry to disk, fixed
#                 schema). Meta is the sole reader (dynamic
#                 directory scan). No semantic routing needed.
#   6 journal   — deepsleep is sole writer, omega is sole reader.
#                 Every entry is a layered shared-state snapshot at
#                 a known schema. Source-tag {meta, global_loop,
#                 omega, deep_sleep, observations} routes directly
#                 to (6, Y).
#   7 vision    — image blobs. Ingest carries explicit modality
#                 metadata (image/face/scene/pattern); routing is
#                 deterministic by source.
#   9 temporal  — time writes (sessions/events/milestones/history)
#                 link to everything else for grounding. Always
#                 source-tagged with timestamp + event_type.
#   11 dev      — build artifacts, progress notes, dev context, dev
#                 vision. All system-internal writes from the build
#                 pipeline; never operator-typed.
#
# Routable lobes (SetFit classifies): 0 identity, 1 personas,
# 2 knowledge, 4 situational, 8 coding, 10 workflows.
#
# Adding a lobe here: ALSO ensure the gate's MemoryGate.ingest path
# has a source-tag bypass for that lobe BEFORE classify_within_lobe
# is called, otherwise writes will fail to route.
SCRIPTED_LOBES: set = {3, 5, 6, 7, 9, 11}

# ── Subdomain directories (X, Y) → directory name ──
# Reserved holding subdomain for static lobes when grammar cannot decide Y.
# Phase 5: never pollutes a real category like constitution/goals/who.
UNSORTED_SUBDOMAIN_Y = 127

SUBDOMAIN_MAP = {
    # 000_identity — operator-facing subcats: constitution + traits (2026-07-04).
    # Other Y slots remain for guardian/omega/self_inquiry but are excluded from
    # freeform + question chat identity recall (see memory.identity.subdomains).
    (0, 0): "constitution",
    (0, 1): "traits",
    (0, 2): "emergent_traits",  # legacy alias — reads still scan; new trait writes use Y=1
    (0, 3): "evolution",
    (0, 4): "guardian",
    # 2026-05-09: queries Omega emits to the operator (follow_up_question,
    # tension_response, ask_user). Each emission writes a record with
    # status=open + a cached embedding. When the operator replies, the
    # binder embeds the reply, scores against open records, and flips
    # status=answered with a bound_score + reply text. DeepSleep's
    # epoch reflection step walks open records to escalate stale ones
    # (project_self_inquiry_thread_tracking).
    (0, 5): "self_inquiry",
    # 2026-05-14: Omega's own open questions / curiosity entries land
    # here (was memory/600_journal/open_questions). Lives under identity
    # because the question stream IS Omega — what it's chasing, what it
    # noticed, what it deferred. Read by curiosity_engine + autonomous
    # loop + DeepSleep consolidation.
    (0, 6): "omega",
    # 100_personas — people the system knows.
    #   (1, 0) operator: THE principal persona — Omega's operator. One
    #     person, stable person_id="operator". Records land directly
    #     at memory/100_personas/operator/<id>.json (no 3rd-tier dir
    #     because there's exactly one operator).
    #   (1, 1) person_stub: SetFit anchor for "this text is about a
    #     person other than the operator." Per-person 3rd-tier dirs
    #     allocated by `core.persona_router` →
    #     memory/100_personas/person_stub/<person_id>/<id>.json
    (1, 0): "operator",
    (1, 1): "person_stub",
    # 200_knowledge (Y = broad knowledge type, Z = dynamic content cluster)
    #
    # Restructured 2026-05-08 from 10-subdomain grouping (science /
    # mathematics / etc) to ~50 fine-grained types per the operator's
    # original Dewey-decimal design. Each Y is a distinct knowledge
    # discipline the SetFit was meant to discriminate; the third
    # level (Z) is created DYNAMICALLY by `memory.knowledge_router`
    # on first write — no hardcoded second taxonomy. Empty subdomains
    # at boot are doctrine: the system reflecting on its own gaps is
    # the founding act of curiosity.
    #
    # Sciences
    (2, 0): "astronomy",
    (2, 1): "physics",
    (2, 2): "quantum_physics",
    (2, 3): "chemistry",
    (2, 4): "biology",
    (2, 5): "genetics",
    (2, 6): "neuroscience",
    (2, 7): "ecology",
    (2, 8): "geology",
    (2, 9): "meteorology",
    (2, 10): "oceanography",
    (2, 11): "environmental_science",
    # Math / Logic
    (2, 12): "mathematics",
    (2, 13): "statistics",
    (2, 14): "probability",
    (2, 15): "logic",
    (2, 16): "game_theory",
    # Engineering
    (2, 17): "mechanical_engineering",
    (2, 18): "electrical_engineering",
    (2, 19): "civil_engineering",
    (2, 20): "materials_engineering",
    (2, 21): "aerospace_engineering",
    (2, 22): "software_engineering",
    (2, 23): "robotics",
    (2, 24): "ai",
    (2, 25): "cybersecurity",
    (2, 26): "cryptography",
    # Medicine
    (2, 27): "medicine",
    (2, 28): "pharmacology",
    (2, 29): "epidemiology",
    (2, 30): "anatomy",
    # Social
    (2, 31): "psychology",
    (2, 32): "sociology",
    (2, 33): "anthropology",
    (2, 34): "archaeology",
    (2, 35): "economics",
    (2, 36): "political_science",
    # Humanities
    (2, 37): "philosophy",
    (2, 38): "ethics",
    (2, 39): "religion",
    (2, 40): "history",
    (2, 41): "geography",
    (2, 42): "linguistics",
    (2, 43): "literature",
    # Arts
    (2, 44): "music",
    (2, 45): "visual_arts",
    (2, 46): "performing_arts",
    (2, 47): "architecture",
    (2, 48): "film",
    # Practical
    (2, 49): "culinary",
    (2, 50): "agriculture",
    (2, 51): "sports",
    (2, 52): "education",
    (2, 53): "law",
    (2, 54): "business",
    (2, 55): "finance",
    # 300_reasoning
    (3, 0): "goals",
    (3, 1): "decisions",
    (3, 2): "constraints",
    (3, 3): "heuristics",
    (3, 4): "alignment",
    # 400_situational — 5W1H + outcome + sentiment. Primary use is
    # SYSTEM self-awareness: every meta choice, auto attempt, global
    # observation writes a situational entry through `enrich_situational`
    # with the full event recollection schema.
    (4, 0): "who",
    (4, 1): "what",
    (4, 2): "when",
    (4, 3): "where",
    (4, 4): "why",
    (4, 5): "how",
    (4, 6): "outcome",
    (4, 7): "sentiment",
    # 500_tools — RENAMED 2026-05-08 from 500_skills per operator
    # ("change the name to tools, store all the tools there, script
    # it"). Tools are scripted-I/O — guardian writes (registration),
    # voice_box reads (dispatch). Two scopes:
    #   (5, 0) cognitive: tools voice_box may invoke for the operator
    #                     (web_fetch is the day-one entry).
    #   (5, 1) dev:       dev-pipeline tools (coding_project_*,
    #                     dev_edit_file, dev_git_*, etc.) — invoked
    #                     by the dev pipeline only, NOT by meta or
    #                     voice_box's cognitive planner.
    (5, 0): "cognitive",
    (5, 1): "dev",
    # 600_journal — DeepSleep is the SOLE writer per operator directive
    # (2026-05-14). meta/global_loop/omega/observations subdomains were
    # retired: meta + global + omega each have their own homes (meta in
    # shared_state, global in temporal+identity, omega curiosity now
    # under (0,6)). Only DeepSleep produces the consolidation cascade
    # journal records that belong here. test_deepsleep_journal_ownership
    # enforces this — only `metacognition/deep_sleep/phases/
    # phase_4_persistence/` is permitted to call JournalGate.ingest().
    (6, 0): "deepsleep",
    # 700_vision
    (7, 0): "images",
    (7, 1): "face_embeddings",
    (7, 2): "scene_descriptions",
    (7, 3): "visual_patterns",
    # 800_coding — Dewey-style flat taxonomy (per operator
    # 2026-05-08): "set up coding subdomains the same way as
    # knowledge, give the set more exacting coords and labels."
    # 50 fine-grained subdomains spanning languages × cross-language
    # topics. Z (third tier) is content-driven, allocated per cluster
    # by a future coding_router (mirrors knowledge_router pattern).
    (8, 0): "python_general",
    (8, 1): "python_async",
    (8, 2): "python_packaging",
    (8, 3): "python_typing",
    (8, 4): "python_testing",
    (8, 5): "python_web",
    (8, 6): "python_ml",
    (8, 7): "python_data",
    (8, 8): "typescript_general",
    (8, 9): "typescript_react",
    (8, 10): "typescript_node",
    (8, 11): "typescript_types",
    (8, 12): "rust_general",
    (8, 13): "rust_systems",
    (8, 14): "rust_async",
    (8, 15): "go_general",
    (8, 16): "go_concurrency",
    (8, 17): "c_general",
    (8, 18): "c_embedded",
    (8, 19): "cpp_general",
    (8, 20): "cpp_templates",
    (8, 21): "cpp_performance",
    (8, 22): "sql_queries",
    (8, 23): "sql_performance",
    (8, 24): "sql_modeling",
    (8, 25): "shell_bash",
    (8, 26): "shell_powershell",
    (8, 27): "html_css",
    (8, 28): "algorithms",
    (8, 29): "data_structures",
    (8, 30): "design_patterns",
    (8, 31): "architecture",
    (8, 32): "concurrency",
    (8, 33): "networking",
    (8, 34): "protocols_http",
    (8, 35): "protocols_grpc",
    (8, 36): "databases_general",
    (8, 37): "caching",
    (8, 38): "testing_general",
    (8, 39): "debugging_general",
    (8, 40): "devops_docker",
    (8, 41): "devops_kubernetes",
    (8, 42): "devops_cicd",
    (8, 43): "security_code",
    (8, 44): "cryptography_code",
    (8, 45): "performance",
    (8, 46): "memory_management",
    (8, 47): "file_io",
    (8, 48): "build_systems",
    (8, 49): "version_control",
    # 900_temporal
    (9, 0): "sessions",
    (9, 1): "events",
    (9, 2): "milestones",
    (9, 3): "history",
    (9, 4): "capability",
    # 1000_workflows — operator-recorded sequences + autonomously
    # detected patterns + tool-chain templates. Feeds the CLI
    # integration where the system codes/teaches itself about code.
    (10, 0): "code_authoring",
    (10, 1): "code_debugging",
    (10, 2): "code_review",
    (10, 3): "code_testing",
    (10, 4): "code_refactoring",
    (10, 5): "learn_concept",
    (10, 6): "operator_recorded",
    (10, 7): "tool_chain_template",
    # 1100_dev (isolated — serverless CLI + HUD dev tools)
    (11, 0): "artifacts",
    (11, 1): "progress",
    (11, 2): "context",
    (11, 3): "vision",
    # Native agent observations.  These remain separate from builder artifacts:
    # an artifact is a file snapshot, while these records are the verified
    # workspace/project evidence that explains what the agent saw and touched.
    (11, 4): "projects",
    (11, 5): "full_tree",
    (11, 6): "strings",
    (11, 7): "operations",
}

# Per-lobe _unsorted holding buckets (static lobes only; 2/8 use dynamic-Y allocator).
for _lobe in (0, 1, 3, 4, 5, 6, 7, 9, 10, 11):
    SUBDOMAIN_MAP.setdefault((_lobe, UNSORTED_SUBDOMAIN_Y), "_unsorted")

# ── Knowledge domain → (Y_category, domain_dir) mapping ──
# Maps each autonomous_knowledge.json domain to its category (Y coord)
# and subdirectory within 200_knowledge/
KNOWLEDGE_DOMAINS = {
    # science (Y=0)
    "neuroscience": (0, "neuroscience"),
    "cognitive_neuroscience": (0, "cognitive_neuroscience"),
    "cognitive_science": (0, "cognitive_science"),
    "biology": (0, "biology"),
    "cell_biology": (0, "cell_biology"),
    "molecular_biology": (0, "molecular_biology"),
    "developmental_biology": (0, "developmental_biology"),
    "evolutionary_biology": (0, "evolutionary_biology"),
    "genetics": (0, "genetics"),
    "epigenetics": (0, "epigenetics"),
    "microbiology": (0, "microbiology"),
    "biochemistry": (0, "biochemistry"),
    "biophysics": (0, "biophysics"),
    "chemistry": (0, "chemistry"),
    "physics": (0, "physics"),
    "quantum_physics": (0, "quantum_physics"),
    "classical_mechanics": (0, "classical_mechanics"),
    "nuclear_physics": (0, "nuclear_physics"),
    "particle_physics": (0, "particle_physics"),
    "astroparticle_physics": (0, "astroparticle_physics"),
    "optics": (0, "optics"),
    "photonics": (0, "photonics"),
    "acoustics": (0, "acoustics"),
    "fluid_dynamics": (0, "fluid_dynamics"),
    "astronomy": (0, "astronomy"),
    "astrophysics": (0, "astrophysics"),
    "cosmology": (0, "cosmology"),
    "astrobiology": (0, "astrobiology"),
    "ecology": (0, "ecology"),
    "environmental_science": (0, "environmental_science"),
    "climate_science": (0, "climate_science"),
    "oceanography": (0, "oceanography"),
    "geology": (0, "geology"),
    "paleontology": (0, "paleontology"),
    "neurology": (0, "neurology"),
    "complexity_theory": (0, "complexity_theory"),
    "chaos_theory": (0, "chaos_theory"),
    "swarm_intelligence": (0, "swarm_intelligence"),
    "forensic_science": (0, "forensic_science"),
    "nutrition": (0, "nutrition"),
    "veterinary_science": (0, "veterinary_science"),
    # mathematics (Y=1)
    "mathematics": (1, "mathematics"),
    "algebra": (1, "algebra"),
    "geometry": (1, "geometry"),
    "calculus": (1, "calculus"),
    "topology": (1, "topology"),
    "statistics": (1, "statistics"),
    "probability_theory": (1, "probability_theory"),
    "logic": (1, "logic"),
    "number_theory": (1, "number_theory"),
    "graph_theory": (1, "graph_theory"),
    "algorithms": (1, "algorithms"),
    # philosophy (Y=2)
    "philosophy": (2, "philosophy"),
    "epistemology": (2, "epistemology"),
    "ethics": (2, "ethics"),
    "existentialism": (2, "existentialism"),
    "metaphysics": (2, "metaphysics"),
    "philosophy_of_mind": (2, "philosophy_of_mind"),
    "philosophy_of_science": (2, "philosophy_of_science"),
    "aesthetics": (2, "aesthetics"),
    "bioethics": (2, "bioethics"),
    "theology": (2, "theology"),
    # technology (Y=3)
    "computer_science": (3, "computer_science"),
    "artificial_intelligence": (3, "artificial_intelligence"),
    "machine_learning": (3, "machine_learning"),
    "cryptography": (3, "cryptography"),
    "information_theory": (3, "information_theory"),
    "network_theory": (3, "network_theory"),
    "networking": (3, "networking"),
    "cybersecurity": (3, "cybersecurity"),
    "data_science": (3, "data_science"),
    "quantum_computing": (3, "quantum_computing"),
    "robotics": (3, "robotics"),
    "software_engineering": (3, "software_engineering"),
    "computer_architecture": (3, "computer_architecture"),
    "database_systems": (3, "database_systems"),
    "distributed_systems": (3, "distributed_systems"),
    "operating_systems": (3, "operating_systems"),
    "neuromorphic_computing": (3, "neuromorphic_computing"),
    # engineering (Y=4)
    "engineering": (4, "engineering"),
    "electrical_engineering": (4, "electrical_engineering"),
    "mechanical_engineering": (4, "mechanical_engineering"),
    "aerospace_engineering": (4, "aerospace_engineering"),
    "civil_engineering": (4, "civil_engineering"),
    "chemical_engineering": (4, "chemical_engineering"),
    "materials_science": (4, "materials_science"),
    "signal_processing": (4, "signal_processing"),
    "control_theory": (4, "control_theory"),
    "electromagnetism": (4, "electromagnetism"),
    "thermodynamics": (4, "thermodynamics"),
    "renewable_energy": (4, "renewable_energy"),
    # humanities (Y=5)
    "linguistics": (5, "linguistics"),
    "literature": (5, "literature"),
    "music_theory": (5, "music_theory"),
    "art_history": (5, "art_history"),
    "creative_writing": (5, "creative_writing"),
    "film_studies": (5, "film_studies"),
    "photography": (5, "photography"),
    "rhetoric": (5, "rhetoric"),
    "journalism": (5, "journalism"),
    "architecture": (5, "architecture"),
    # social_science (Y=6)
    "psychology": (6, "psychology"),
    "social_psychology": (6, "social_psychology"),
    "evolutionary_psychology": (6, "evolutionary_psychology"),
    "sociology": (6, "sociology"),
    "economics": (6, "economics"),
    "behavioral_economics": (6, "behavioral_economics"),
    "anthropology": (6, "anthropology"),
    "political_science": (6, "political_science"),
    "game_theory": (6, "game_theory"),
    "decision_theory": (6, "decision_theory"),
    "pedagogy": (6, "pedagogy"),
    # medicine (Y=7)
    "medicine": (7, "medicine"),
    "pharmacology": (7, "pharmacology"),
    "immunology": (7, "immunology"),
    "epidemiology": (7, "epidemiology"),
    # world (Y=8)
    "history": (8, "history"),
    "history_of_science": (8, "history_of_science"),
    "agriculture": (8, "agriculture"),
    "law": (8, "law"),
    "library_science": (8, "library_science"),
    "cartography": (8, "cartography"),
    "urban_planning": (8, "urban_planning"),
    "transportation": (8, "transportation"),
    # P3-S44 (2026-05-28): lobe-2 directory subdomains (SUBDOMAIN_MAP X=2)
    # that were absent from this catalog, so `category_for_domain` could
    # not route them to a coarse category for credible-source selection.
    # Added with their coarse-category Y so every dispatchable knowledge
    # rotation subdomain resolves to a real category + curated source.
    "meteorology": (0, "meteorology"),  # science
    "probability": (1, "probability"),  # mathematics
    "ai": (3, "ai"),  # technology
    "materials_engineering": (4, "materials_engineering"),  # engineering
    "music": (5, "music"),  # humanities
    "visual_arts": (5, "visual_arts"),  # humanities
    "performing_arts": (5, "performing_arts"),  # humanities
    "film": (5, "film"),  # humanities
    "culinary": (5, "culinary"),  # humanities
    "education": (6, "education"),  # social_science
    "business": (6, "business"),  # social_science
    "finance": (6, "finance"),  # social_science
    "anatomy": (7, "anatomy"),  # medicine
    "religion": (2, "religion"),  # philosophy (adjacent to theology)
    "archaeology": (8, "archaeology"),  # world (adjacent to history)
    "geography": (8, "geography"),  # world
    "sports": (8, "sports"),  # world
}


def knowledge_coord(domain_name: str) -> tuple:
    """Return (X=2, Y=category, domain_dir) for a knowledge domain.

    If unknown, returns (2, 0, domain_name) — defaults to science.
    """
    entry = KNOWLEDGE_DOMAINS.get(domain_name)
    if entry:
        return (2, entry[0], entry[1])
    return (2, 0, domain_name)


# Coding domain -> (Y_subdomain, Z_cluster) mapping
# Lobe 8 now uses the flat SUBDOMAIN_MAP taxonomy above. Keep the older
# `<language>_<concern>` lookup keys as aliases, but resolve them to the
# current integer subdomain ids so canonical envelopes remain schema-valid.
_CODING_LANGUAGE_DEFAULT_Y = {
    "python": 0,
    "typescript": 8,
    "rust": 12,
    "go": 15,
    "c": 17,
    "cpp": 19,
    "sql": 22,
    "shell": 25,
    "html_css": 27,
}

CODING_DOMAINS = {
    # Exact current lobe-8 subdomain labels.
    "python_general": (0, 0),
    "python_async": (1, 0),
    "python_packaging": (2, 0),
    "python_typing": (3, 0),
    "python_testing": (4, 0),
    "python_web": (5, 0),
    "python_ml": (6, 0),
    "python_data": (7, 0),
    "typescript_general": (8, 0),
    "typescript_react": (9, 0),
    "typescript_node": (10, 0),
    "typescript_types": (11, 0),
    "rust_general": (12, 0),
    "rust_systems": (13, 0),
    "rust_async": (14, 0),
    "go_general": (15, 0),
    "go_concurrency": (16, 0),
    "c_general": (17, 0),
    "c_embedded": (18, 0),
    "cpp_general": (19, 0),
    "cpp_templates": (20, 0),
    "cpp_performance": (21, 0),
    "sql_queries": (22, 0),
    "sql_performance": (23, 0),
    "sql_modeling": (24, 0),
    "shell_bash": (25, 0),
    "shell_powershell": (26, 0),
    "html_css": (27, 0),
    "algorithms": (28, 0),
    "data_structures": (29, 0),
    "design_patterns": (30, 0),
    "architecture": (31, 0),
    "concurrency": (32, 0),
    "networking": (33, 0),
    "protocols_http": (34, 0),
    "protocols_grpc": (35, 0),
    "databases_general": (36, 0),
    "caching": (37, 0),
    "testing_general": (38, 0),
    "debugging_general": (39, 0),
    "devops_docker": (40, 0),
    "devops_kubernetes": (41, 0),
    "devops_cicd": (42, 0),
    "security_code": (43, 0),
    "cryptography_code": (44, 0),
    "performance": (45, 0),
    "memory_management": (46, 0),
    "file_io": (47, 0),
    "build_systems": (48, 0),
    "version_control": (49, 0),
    # Back-compat concern aliases used by coding enrichment/project helpers.
    "python_cli": (0, 0),
    "python_systems": (0, 0),
    "python_devops": (42, 0),
    "typescript_web": (9, 0),
    "typescript_cli": (8, 0),
    "typescript_systems": (8, 0),
    "typescript_testing": (38, 0),
    "rust_web": (12, 0),
    "rust_cli": (12, 0),
    "rust_embedded": (12, 0),
    "go_web": (15, 0),
    "go_cli": (15, 0),
    "go_devops": (15, 0),
    "go_systems": (15, 0),
    "c_systems": (17, 0),
    "c_security": (43, 0),
    "cpp_systems": (19, 0),
    "cpp_ml": (19, 0),
    "cpp_embedded": (19, 0),
    "sql_data": (22, 0),
    "sql_admin": (24, 0),
    "sql_systems": (22, 0),
    "shell_devops": (25, 0),
    "shell_cli": (25, 0),
    "shell_systems": (25, 0),
    "html_css_web": (27, 0),
    "html_css_systems": (27, 0),
}


def coding_coord(domain_name: str) -> tuple:
    """Return (X=8, Y=subdomain, Z=cluster) for a coding domain.

    Unknown language-qualified aliases fall back to that language's
    general subdomain. Fully unknown names fall back to python_general.
    """
    entry = CODING_DOMAINS.get(domain_name)
    if entry:
        return (8, entry[0], entry[1])

    for language, default_y in sorted(
        _CODING_LANGUAGE_DEFAULT_Y.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if domain_name == language or domain_name.startswith(f"{language}_"):
            return (8, default_y, 0)
    return (8, 0, 0)


# ── Coarse knowledge categories (P3-S44) ──
# The lobe-2 categories are the code-owned coarse vocabulary. Their order is
# the canonical Y-coordinate mapping used when a fine subdomain resolves to a
# category and its curated source URL.
KNOWLEDGE_CATEGORY_KEYS = (
    "science",  # 0
    "mathematics",  # 1
    "philosophy",  # 2
    "technology",  # 3
    "engineering",  # 4
    "humanities",  # 5
    "social_science",  # 6
    "medicine",  # 7
    "world",  # 8
    "synthesis",  # 9
)

# Coding subdomain prefix -> config/web_sources.yaml catalog domain.
# Most-specific prefixes first so 'typescript_*' is not shadowed by a
# shorter prefix. Language prefix wins over concern (python_ml -> python).
_CODING_CATALOG_BY_PREFIX = (
    ("typescript", "typescript"),
    ("nodejs", "nodejs"),
    ("python", "python"),
    ("rust", "rust"),
    ("go", "go"),
    ("devops", "devops"),
    ("html_css", "web"),
)


def _coding_catalog_domain(name: str) -> str:
    """Map a coding subdomain to its web_sources.yaml catalog domain.
    Language-grouped subdomains route to that language's docs domain;
    everything else falls to the general 'coding' community tier."""
    n = (name or "").lower()
    for prefix, catalog in _CODING_CATALOG_BY_PREFIX:
        if n == prefix or n.startswith(f"{prefix}_"):
            return catalog
    return "coding"


def category_for_domain(name, *, coding: bool = False):
    """Map a fine dewey subdomain to the coarse category the search +
    source catalog is keyed on. Returns ``None`` for unknown names.

    Knowledge subdomains resolve to one of ``KNOWLEDGE_CATEGORY_KEYS`` via the
    code-owned coarse category mapping. A coarse category passed in is returned
    unchanged.

    Coding subdomains (``coding=True``) resolve to the
    ``web_sources.yaml`` catalog domain for their language/concern group.

    The dispatched subdomain itself stays the wiki search term + mastery
    key; this only supplies the credible-source routing category.
    """
    if not name:
        return None
    if coding:
        if name in CODING_DOMAINS:
            return _coding_catalog_domain(name)
        # Bare language or language-qualified alias (e.g. 'python_web').
        return _coding_catalog_domain(name)
    if name in KNOWLEDGE_CATEGORY_KEYS:
        return name
    entry = KNOWLEDGE_DOMAINS.get(name)
    if entry:
        y = entry[0]
        if 0 <= y < len(KNOWLEDGE_CATEGORY_KEYS):
            return KNOWLEDGE_CATEGORY_KEYS[y]
    return None


# ── Situational facets (lobe 4) — full event recollection schema ──
# Order matches SUBDOMAIN_MAP[(4, 0..7)]. Used by enrich_situational
# to validate the facet field on incoming envelopes.
SITUATIONAL_FACETS = (
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "outcome",
    "sentiment",
)


# ── Workflow kinds (lobe 10) ──
# Order matches SUBDOMAIN_MAP[(10, 0..7)]. Used by enrich_workflow
# to validate the kind field on incoming envelopes.
WORKFLOW_KINDS = (
    "code_authoring",
    "code_debugging",
    "code_review",
    "code_testing",
    "code_refactoring",
    "learn_concept",
    "operator_recorded",
    "tool_chain_template",
)


def resolve_path(coord, domain=None):
    """Return the relative directory path for a coordinate.

    For knowledge lobe (X=2), includes domain subdirectory.
    """
    X = coord[0]
    Y = coord[1] if len(coord) > 1 else 0

    lobe = LOBE_MAP.get(X, f"{X}xx")
    subdomain = SUBDOMAIN_MAP.get((X, Y), f"{Y:02d}")

    if X == 2 and domain:
        # Three-tier: lobe/subdomain/domain/
        entry = KNOWLEDGE_DOMAINS.get(domain)
        if entry:
            return f"{lobe}/{subdomain}/{entry[1]}"
        return f"{lobe}/{subdomain}/{domain}"

    path = f"{lobe}/{subdomain}"
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "resolve_path",
            summary=path,
            coord=list(coord) if coord else [],
            domain=domain,
            path=path,
        )
    except Exception:
        pass
    return path


# ── SetFit label definitions (for retraining) ──
SETFIT_LABELS = []
for (x, y), name in sorted(SUBDOMAIN_MAP.items()):
    lobe_name = LOBE_MAP.get(x, f"{x}xx").split("_", 1)[-1]
    label_id = f"{x}.{y:02d}"
    SETFIT_LABELS.append(
        {
            "label_id": label_id,
            "coord": [x, y],
            "name": f"{lobe_name}.{name}",
        }
    )
