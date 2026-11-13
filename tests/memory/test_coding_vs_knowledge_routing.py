"""Deterministic coding(8)-vs-knowledge(2) topic discrimination on ingest.

The onboarding router must judge each LEARNING TOPIC on its merits — a concrete
programming language / framework / runtime / dev-or-infra tool is CODING (lobe 8);
everything else (concepts, theory, methods, non-software domains) is KNOWLEDGE
(lobe 2), which is the DEFAULT. The occupation never forces the lobe: a Software
Engineer's "Algorithms" / "System Design" are KNOWLEDGE, only the actual code
("Python") is coding. No classifier — pure deterministic markers.
"""

from __future__ import annotations

import pytest

from memory.routing.coding_vs_knowledge import classify_topic_lobe

CODING = 8
KNOWLEDGE = 2


# The actual onboarding subcategory hints (hud/operator_onboarding.py), hand-labeled.
# Across ~55 canonical topics only the concrete languages/frameworks/tools are coding.
CANONICAL_HINTS = {
    # software_engineer
    "Python": CODING,
    "System Design": KNOWLEDGE,
    "Distributed Systems": KNOWLEDGE,
    "Algorithms": KNOWLEDGE,
    # data_scientist
    "Machine Learning": KNOWLEDGE,
    "Statistics": KNOWLEDGE,
    "Deep Learning": KNOWLEDGE,
    "Data Pipelines": KNOWLEDGE,
    # devops_sre
    "Kubernetes": CODING,
    "CI/CD": KNOWLEDGE,
    "Observability": KNOWLEDGE,
    "Cloud Infrastructure": KNOWLEDGE,
    # web_developer
    "JavaScript": CODING,
    "React": CODING,
    "APIs": CODING,
    "Accessibility": KNOWLEDGE,
    # security_engineer
    "AppSec": KNOWLEDGE,
    "Cryptography": KNOWLEDGE,
    "Threat Modeling": KNOWLEDGE,
    "Incident Response": KNOWLEDGE,
    # game_developer
    "Game Engines": KNOWLEDGE,
    "Graphics": KNOWLEDGE,
    "Physics": KNOWLEDGE,
    "Gameplay Systems": KNOWLEDGE,
    # healthcare
    "Cardiology": KNOWLEDGE,
    "Pharmacology": KNOWLEDGE,
    "Diagnostics": KNOWLEDGE,
    "Patient Care": KNOWLEDGE,
    # lawyer
    "Contract Law": KNOWLEDGE,
    "Litigation": KNOWLEDGE,
    "Compliance": KNOWLEDGE,
    "IP Law": KNOWLEDGE,
    # educator
    "Curriculum Design": KNOWLEDGE,
    "Pedagogy": KNOWLEDGE,
    "Assessment": KNOWLEDGE,
    "EdTech": KNOWLEDGE,
    # writer
    "Narrative Craft": KNOWLEDGE,
    "Editing": KNOWLEDGE,
    "Research": KNOWLEDGE,
    "Interviewing": KNOWLEDGE,
    # designer
    "UX Research": KNOWLEDGE,
    "Visual Design": KNOWLEDGE,
    "Typography": KNOWLEDGE,
    "Design Systems": KNOWLEDGE,
    # researcher
    "Methodology": KNOWLEDGE,
    "Literature Review": KNOWLEDGE,
    "Grant Writing": KNOWLEDGE,
    # business
    "Financial Modeling": KNOWLEDGE,
    "Strategy": KNOWLEDGE,
    "Operations": KNOWLEDGE,
    "Accounting": KNOWLEDGE,
    # marketing
    "SEO": KNOWLEDGE,
    "Content Strategy": KNOWLEDGE,
    "Analytics": KNOWLEDGE,
    "Brand": KNOWLEDGE,
    # student
    "Study Skills": KNOWLEDGE,
    "Note Taking": KNOWLEDGE,
    "Exam Prep": KNOWLEDGE,
}


def test_canonical_onboarding_hints_route_accurately():
    """Every canonical onboarding hint lands in the right lobe — the accuracy oracle."""
    wrong = {
        topic: (classify_topic_lobe(topic), expected)
        for topic, expected in CANONICAL_HINTS.items()
        if classify_topic_lobe(topic) != expected
    }
    assert not wrong, "misrouted topics (got, expected): " + ", ".join(
        f"{t}=({g}!={e})" for t, (g, e) in wrong.items()
    )


def test_software_engineer_set_is_not_all_coding():
    """The operator's exact complaint: a SWE's set is not all coding."""
    assert classify_topic_lobe("Python") == CODING
    assert classify_topic_lobe("System Design") == KNOWLEDGE
    assert classify_topic_lobe("Distributed Systems") == KNOWLEDGE
    assert classify_topic_lobe("Algorithms") == KNOWLEDGE


@pytest.mark.parametrize(
    "topic",
    [
        "Python",
        "python programming",
        "JavaScript",
        "TypeScript",
        "Rust",
        "Go",
        "C",
        "C++",
        "C#",
        "SQL",
        "Bash scripting",
        "PowerShell",
        "HTML/CSS",
        "React",
        "Angular",
        "Vue",
        "Django",
        "Flask",
        "FastAPI",
        "Spring Boot",
        "Node.js",
        "TensorFlow",
        "PyTorch",
        "Docker",
        "Kubernetes",
        "Terraform",
        "GraphQL",
        "REST API",
        ".NET",
        "Unity",
        "Unreal Engine",
    ],
)
def test_concrete_tech_is_coding(topic):
    assert classify_topic_lobe(topic) == CODING, topic


@pytest.mark.parametrize(
    "topic",
    [
        "Algorithms",
        "Data Structures",
        "System Design",
        "Machine Learning",
        "Deep Learning",
        "Statistics",
        "Linear Algebra",
        "Cryptography",
        "Distributed Systems",
        "Operating Systems",
        "Computer Networks",
        "API Design and Interface Contracts",
        "Cardiology",
        "Contract Law",
        "Pedagogy",
        "Macroeconomics",
        "Organic Chemistry",
        "Cognitive Psychology",
        "Project Management",
    ],
)
def test_concepts_and_domains_are_knowledge(topic):
    assert classify_topic_lobe(topic) == KNOWLEDGE, topic


@pytest.mark.parametrize(
    "topic",
    [
        "Lymph Nodes",  # must NOT match "node"
        "Frustration",  # must NOT match "rust"
        "Data Structures",  # must NOT match "struct"
        "Reaction Kinetics",  # must NOT match "react"
        "Apiary Management",  # must NOT match "api"
        "Go-to-market Strategy",  # bare "Go" only matches as a whole-title topic
        "C-Suite Leadership",  # bare "C" only matches as a whole-title topic
        "R&D Management",  # bare "R" only matches as a whole-title topic
        "Goal Setting",
    ],
)
def test_no_false_positive_coding_matches(topic):
    assert classify_topic_lobe(topic) == KNOWLEDGE, topic


def test_empty_or_blank_defaults_to_knowledge():
    assert classify_topic_lobe("") == KNOWLEDGE
    assert classify_topic_lobe("   ") == KNOWLEDGE
    assert classify_topic_lobe(None) == KNOWLEDGE  # type: ignore[arg-type]
