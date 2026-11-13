"""Deterministic lobe 8 (coding) vs lobe 2 (knowledge) discrimination for ingest.

A learning topic is CODING only when it names a concrete programming language,
framework/library, runtime, or developer/infra tool (or carries literal code
syntax). Everything else — concepts, theory, methods, non-software domains — is
KNOWLEDGE, which is the DEFAULT. The occupation never forces the lobe: a Software
Engineer's "Algorithms" / "System Design" are knowledge; only the actual code
("Python") is coding.

Pure deterministic markers — NO classifier (those are retired). Boundary-aware so a
marker fires only as a whole token: "rust" never matches inside "frustration", "api"
never inside "apiary", "node" only via "node.js"/"nodejs" so "Lymph Nodes" stays
knowledge. Tuned against the onboarding subcategory hints across all occupations
(see tests/memory/test_coding_vs_knowledge_routing.py — the accuracy oracle).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

CODING_LOBE = 8
KNOWLEDGE_LOBE = 2

# Whole-title-only matches: single/short names too ambiguous to scan as substrings.
# Only fire when the ENTIRE (stripped, lowered) title equals one of these — so the D
# language matches a bare "D" topic but "Go-to-market" / "C-Suite" / "R&D" do not.
_CODING_EXACT_TITLES = frozenset({"c", "r", "go", "api", "apis"})

# Punctuated names that are unambiguous as substrings (the non-word chars make them
# safe — these strings essentially never occur outside a coding context).
_CODING_PUNCT_SUBSTRINGS = (
    "c++",
    "c#",
    "f#",
    ".net",
    "asp.net",
    "node.js",
    "next.js",
    "nuxt.js",
    "objective-c",
    "ci/cd-pipeline",
)

# Boundary-aware whole-token markers: concrete languages, frameworks/libraries,
# runtimes, and dev/infra tools. Curated to avoid common-English collisions — bare
# "node", "spring", "qt", "ml", "go", "c", "r" are deliberately EXCLUDED here (handled
# above as exact/punct, or dropped) so everyday words don't misroute to coding.
_CODING_TOKENS = (
    # languages
    "python",
    "javascript",
    "typescript",
    "java",
    "kotlin",
    "swift",
    "golang",
    "rust",
    "ruby",
    "php",
    "perl",
    "scala",
    "haskell",
    "elixir",
    "erlang",
    "clojure",
    "lua",
    "dart",
    "matlab",
    "sql",
    "nosql",
    "plsql",
    "tsql",
    "bash",
    "powershell",
    "html",
    "css",
    "sass",
    "scss",
    "cobol",
    "fortran",
    "solidity",
    "groovy",
    "julia",
    "assembly",
    "webassembly",
    "wasm",
    # frameworks / libraries / runtimes
    "react",
    "angular",
    "vue",
    "svelte",
    "nextjs",
    "nodejs",
    "express",
    "django",
    "flask",
    "fastapi",
    "rails",
    "sinatra",
    "laravel",
    "symfony",
    "spring boot",
    "spring framework",
    "dotnet",
    "blazor",
    "tensorflow",
    "pytorch",
    "keras",
    "pandas",
    "numpy",
    "scipy",
    "sklearn",
    "scikit",
    "jquery",
    "bootstrap",
    "tailwind",
    "redux",
    "unity",
    "unreal",
    "godot",
    "opengl",
    "vulkan",
    "directx",
    "webgl",
    "threejs",
    "flutter",
    "xamarin",
    # tools / infra
    "docker",
    "kubernetes",
    "k8s",
    "terraform",
    "ansible",
    "jenkins",
    "gitlab",
    "github actions",
    "git",
    "grpc",
    "graphql",
    "webpack",
    "vite",
    "rollup",
    "babel",
    "eslint",
    "redis",
    "postgres",
    "postgresql",
    "mysql",
    "mongodb",
    "sqlite",
    "kafka",
    "rabbitmq",
    "nginx",
    "cuda",
    # code-ish nouns / syntax
    "sdk",
    "cli",
    "regex",
    "crud",
    "orm",
    "coroutine",
    "decorator",
    "middleware",
    "webhook",
    "async def",
    "rest api",
    "graphql api",
)


def _compile(markers) -> list:
    """Boundary-aware matchers: a marker matches only as a whole token, never as a
    substring. ``(?<!\\w)...(?!\\w)`` brackets the (stripped) marker."""
    return [re.compile(r"(?<!\w)" + re.escape(m.strip()) + r"(?!\w)") for m in markers if m.strip()]


_CODING_TOKEN_RE = _compile(_CODING_TOKENS)


def classify_topic_lobe(title: str) -> int:
    """Discern a learning topic: coding (``CODING_LOBE`` = 8) vs knowledge
    (``KNOWLEDGE_LOBE`` = 2).

    CODING iff the title names a concrete language / framework / runtime / dev-or-infra
    tool (or carries code syntax). Everything else is KNOWLEDGE — the default. The
    occupation is NEVER consulted; the topic is judged on its own merits.
    """
    t = (title or "").strip().lower()
    if not t:
        return KNOWLEDGE_LOBE
    if t in _CODING_EXACT_TITLES:
        return CODING_LOBE
    for needle in _CODING_PUNCT_SUBSTRINGS:
        if needle in t:
            return CODING_LOBE
    for rx in _CODING_TOKEN_RE:
        if rx.search(t):
            return CODING_LOBE
    return KNOWLEDGE_LOBE


def memory_record_routing_text(record: Mapping[str, Any]) -> str:
    """Return the canonical routing input replayable from persisted memory fields."""
    if not isinstance(record, Mapping):
        return ""
    return " ".join(
        value
        for field in ("domain", "subtopic", "title", "summary")
        if (value := str(record.get(field) or "").strip())
    )


def classify_memory_record_lobe(record: Mapping[str, Any]) -> int:
    """Classify exactly the fields that integrity can replay after persistence."""
    return classify_topic_lobe(memory_record_routing_text(record))
