"""
Dewey Map — coordinate system and lobe routing.

Defines the deterministic coordinate system (X, Y, Z) and the lobe
routing table. This is the single source of truth for how memories
are organized and routed.

Inspired by Omega3.0's dewey_map.py but simplified for standalone use.

Coordinate system:
  X = lobe (0-11): the broad category of memory
  Y = subdomain: fine-grained classification within a lobe
  Z = cluster: domain-specific clustering (for knowledge lobe)

Lobes:
  0  identity      — constitution, personality, self-model
  1  personas      — people the system knows
  2  knowledge     — facts, concepts, domain knowledge
  3  reasoning     — goals, decisions, constraints, heuristics
  4  situational   — 5W1H, outcomes, sentiment, self-awareness
  5  tools         — cognitive tools, dev tools
  6  journal       — episodic journal (DeepSleep writes)
  7  vision        — images, visual patterns
  8  coding        — code, programming knowledge
  9  temporal      — sessions, events, milestones, history
  10 workflows     — code authoring, debugging, learning templates
  11 dev           — development artifacts (isolated)

Scripted lobes (no learned routing): {3, 5, 6, 7, 9, 11}
Entity-bearing lobes (cross-lobe linking): {1, 4}
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

# =====================================================================
# Lobe definitions
# =====================================================================

LOBE_MAP: Dict[int, str] = {
    0: "identity",
    1: "personas",
    2: "knowledge",
    3: "reasoning",
    4: "situational",
    5: "tools",
    6: "journal",
    7: "vision",
    8: "coding",
    9: "temporal",
    10: "workflows",
    11: "dev",
}

# Lobe descriptions for documentation
LOBE_DESCRIPTIONS: Dict[int, str] = {
    0: "Constitution, personality, self-model, emergent traits",
    1: "People the system knows (operator, personas)",
    2: "Facts, concepts, domain knowledge (~56 subdomains)",
    3: "Goals, decisions, constraints, heuristics, alignment",
    4: "5W1H, outcomes, sentiment — system self-awareness",
    5: "Cognitive tools (5,0), dev tools (5,1)",
    6: "Episodic journal — DeepSleep writes only",
    7: "Images, face embeddings, scene descriptions",
    8: "Code, programming knowledge (50 subdomains)",
    9: "Sessions, events, milestones, history, temporal grounding",
    10: "Code authoring, debugging, review, testing, learning",
    11: "Development artifacts (isolated — never bleeds into cognitive recall)",
}

# =====================================================================
# Routing classification
# =====================================================================

# Lobes with deterministic grammar routing (helper-based)
DETERMINISTIC_LOBES: Set[int] = {0, 1, 2, 4, 8, 10}

# Lobes with scripted routing (source-tag dispatch, no learned routing)
SCRIPTED_LOBES: Set[int] = {3, 5, 6, 7, 9, 11}

# Lobes that carry entity-bearing data (cross-lobe linking)
ENTITY_BEARING_LOBES: Set[int] = {1, 4}

# Lobes that are structurally non-recallable through general engine
NON_RECALLABLE_LOBES: Set[int] = {6, 7}

# =====================================================================
# Subdomain maps
# =====================================================================

# Knowledge subdomains (Y values for lobe 2)
KNOWLEDGE_SUBDOMAINS: Dict[int, str] = {
    0: "general",
    1: "science",
    2: "mathematics",
    3: "history",
    4: "philosophy",
    5: "technology",
    6: "language",
    7: "art",
    8: "music",
    9: "sports",
    10: "politics",
    11: "economics",
    12: "psychology",
    13: "biology",
    14: "physics",
    15: "chemistry",
    16: "geography",
    17: "law",
    18: "medicine",
    19: "engineering",
}

# Coding subdomains (Y values for lobe 8)
CODING_SUBDOMAINS: Dict[int, str] = {
    0: "general",
    1: "python",
    2: "javascript",
    3: "typescript",
    4: "rust",
    5: "go",
    6: "java",
    7: "c_cpp",
    8: "sql",
    9: "shell",
    10: "markdown",
    11: "yaml",
    12: "html_css",
    13: "docker",
    14: "kubernetes",
    15: "aws",
    16: "azure",
    17: "gcp",
    18: "git",
    19: "testing",
}

# =====================================================================
# Coordinate resolution
# =====================================================================


def resolve_path(coord: List[int], base_path: str = "") -> str:
    """Resolve a (X, Y, Z) coordinate to a filesystem path.

    Args:
        coord: [X, Y, Z] coordinate list
        base_path: Base directory path (default: current directory)

    Returns:
        Filesystem path string
    """
    if len(coord) < 1:
        return base_path

    x = coord[0]
    y = coord[1] if len(coord) > 1 else 0
    z = coord[2] if len(coord) > 2 else 0

    # Get lobe directory name
    lobe_name = LOBE_MAP.get(x, f"lobe_{x}")
    path = f"{base_path}/{lobe_name}"

    # Add subdomain for knowledge and coding lobes
    if x == 2 and y in KNOWLEDGE_SUBDOMAINS:
        path += f"/{KNOWLEDGE_SUBDOMAINS[y]}"
    elif x == 8 and y in CODING_SUBDOMAINS:
        path += f"/{CODING_SUBDOMAINS[y]}"

    # Add Z cluster for knowledge lobe
    if x == 2 and z > 0:
        path += f"/cluster_{z}"

    return path


def coord_to_string(coord: List[int]) -> str:
    """Convert coordinate to string representation.

    Args:
        coord: [X, Y, Z] coordinate list

    Returns:
        String like "2.10.0"
    """
    parts = [str(c) for c in coord[:3]]
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts)


def string_to_coord(s: str) -> List[int]:
    """Parse coordinate string to list.

    Args:
        s: String like "2.10.0"

    Returns:
        [X, Y, Z] coordinate list
    """
    parts = s.split(".")
    coord = [int(p) for p in parts[:3]]
    while len(coord) < 3:
        coord.append(0)
    return coord[:3]


def is_deterministic_lobe(lobe: int) -> bool:
    """Check if a lobe uses deterministic grammar routing.

    Args:
        lobe: Lobe ID (X coordinate)

    Returns:
        True if the lobe uses deterministic routing
    """
    return lobe in DETERMINISTIC_LOBES


def is_scripted_lobe(lobe: int) -> bool:
    """Check if a lobe uses scripted routing.

    Args:
        lobe: Lobe ID (X coordinate)

    Returns:
        True if the lobe uses scripted routing
    """
    return lobe in SCRIPTED_LOBES


def is_entity_bearing_lobe(lobe: int) -> bool:
    """Check if a lobe carries entity-bearing data.

    Args:
        lobe: Lobe ID (X coordinate)

    Returns:
        True if the lobe is entity-bearing
    """
    return lobe in ENTITY_BEARING_LOBES


def is_recallable_lobe(lobe: int) -> bool:
    """Check if a lobe is recallable through the general engine.

    Args:
        lobe: Lobe ID (X coordinate)

    Returns:
        True if the lobe is recallable
    """
    return lobe not in NON_RECALLABLE_LOBES


def get_lobe_name(lobe: int) -> str:
    """Get the name of a lobe.

    Args:
        lobe: Lobe ID (X coordinate)

    Returns:
        Lobe name string
    """
    return LOBE_MAP.get(lobe, f"lobe_{lobe}")


def get_lobe_description(lobe: int) -> str:
    """Get the description of a lobe.

    Args:
        lobe: Lobe ID (X coordinate)

    Returns:
        Lobe description string
    """
    return LOBE_DESCRIPTIONS.get(lobe, "Unknown lobe")


def get_all_lobe_ids() -> List[int]:
    """Get all valid lobe IDs.

    Returns:
        List of lobe IDs [0, 1, 2, ..., 11]
    """
    return sorted(LOBE_MAP.keys())


def get_recallable_lobe_ids() -> List[int]:
    """Get all recallable lobe IDs.

    Returns:
        List of recallable lobe IDs
    """
    return [lobe for lobe in get_all_lobe_ids() if is_recallable_lobe(lobe)]


def get_deterministic_lobe_ids() -> List[int]:
    """Get all deterministic routing lobe IDs.

    Returns:
        List of deterministic lobe IDs
    """
    return sorted(DETERMINISTIC_LOBES)


def get_scripted_lobe_ids() -> List[int]:
    """Get all scripted routing lobe IDs.

    Returns:
        List of scripted lobe IDs
    """
    return sorted(SCRIPTED_LOBES)
