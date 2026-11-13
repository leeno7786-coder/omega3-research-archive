"""USE_SKILL remains available for autonomous work, not operator chat routing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

import global_loop.intent.run  # noqa: F401 — ensure the submodule is in sys.modules
from global_loop.intent.run import _map_opened_by_to_intent
from pressure_loop.contributors import ALLOWED_INTENTS

# the package __init__ re-exports run() as global_loop.intent.run, shadowing the module
# attribute — fetch the real module object from sys.modules to toggle its flag global.
_INTENT_MODULE = sys.modules["global_loop.intent.run"]


@pytest.fixture
def skill_routing_on(monkeypatch):
    monkeypatch.delenv("OMEGA_USE_SKILL_ROUTING", raising=False)
    monkeypatch.setattr(_INTENT_MODULE, "OMEGA_USE_SKILL_ROUTING", True)


@pytest.fixture
def skill_routing_off(monkeypatch):
    # the flag is now FLIPPED LIVE (default ON); the kill-switch must be set explicitly
    monkeypatch.delenv("OMEGA_USE_SKILL_ROUTING", raising=False)
    monkeypatch.setattr(_INTENT_MODULE, "OMEGA_USE_SKILL_ROUTING", False)


# ── the allowed intent set ────────────────────────────────────────────────────
def test_use_skill_in_allowed_intents():
    assert "USE_SKILL" in ALLOWED_INTENTS


def test_use_skill_in_schema_enum():
    schema = yaml.safe_load(Path("schema/global_intent.schema.yaml").read_text(encoding="utf-8"))
    enum = schema["definitions"]["allowed_intent"]["enum"]
    assert "USE_SKILL" in enum


# ── operator work always reaches the one RESPONSE interpreter ─────────────────
@pytest.mark.parametrize(
    "msg", ["fix this bug", "inspect this repo", "run the build", "review this code"]
)
def test_flag_off_keeps_operator_work_on_response(skill_routing_off, msg):
    assert _map_opened_by_to_intent("operator_command", msg) == "RESPOND"


# The retired routing flag cannot recreate a second operator execution lane.
@pytest.mark.parametrize(
    "msg",
    [
        "inspect this repo",
        "fix this bug",
        "run tests and diagnose",
        "review this code",
        "implement the dispatch schema",
        "refactor this module",
    ],
)
def test_flag_on_keeps_operator_work_on_response(skill_routing_on, msg):
    intent = _map_opened_by_to_intent("operator_command", msg)
    assert intent == "RESPOND"
    # Global emits an intent NAME only — never a skill or tool selection.
    assert isinstance(intent, str) and intent in ALLOWED_INTENTS


# ── non-agentic routes are unchanged regardless of the flag ───────────────────
def test_pure_answer_routes_to_respond(skill_routing_on):
    assert _map_opened_by_to_intent("operator_query", "what is the architecture?") == "RESPOND"


def test_learning_request_routes_to_learn_not_qwen(skill_routing_on):
    # a memory/learn ask must NOT route to the qwen skill path
    assert _map_opened_by_to_intent("operator_command", "learn python async") == "LEARN"


def test_identity_gap_routes_to_idle(skill_routing_on):
    # SELF_INQUIRY was removed; identity_gap now collapses to IDLE.
    assert _map_opened_by_to_intent("identity_gap", "") == "IDLE"


def test_idle_unchanged(skill_routing_on):
    assert _map_opened_by_to_intent("idle_probe_aggregate", "") == "IDLE"


# ── Global never names a skill/tool (structural) ──────────────────────────────
def test_global_output_is_an_intent_string_no_skill_or_tool(skill_routing_on):
    intent = _map_opened_by_to_intent("operator_command", "inspect this repo")
    assert isinstance(intent, str)  # an intent name, not a {selected_skill/selected_tool} object
