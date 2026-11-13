"""LEARN -> knowledge_extractor wiring (spec §8 learn loop)."""

from auto_loop.execute.run import _maybe_extract_knowledge
from pressure_loop.policy import clear_policy_cache, knowledge_learn_integration_wired


def test_learn_intent_extracts_when_wired(monkeypatch):
    clear_policy_cache()
    monkeypatch.setattr(
        "pressure_loop.policy.knowledge_learn_integration_wired",
        lambda policy=None: True,
    )
    outputs = {
        "tool_result": "Recursion is a technique where a function calls itself. "
        "A base case stops the recursion to avoid infinite loops."
    }
    warnings: list = []
    _maybe_extract_knowledge(
        {"source_action_goal": {"intent": "LEARN"}},
        outputs,
        {"query": "python recursion"},
        "coding",
        "web_fetch",
        warnings,
    )
    assert outputs.get("knowledge_artifact_id"), "LEARN should produce a KnowledgeArtifact id"
    assert outputs.get("knowledge_domain")
    assert not warnings


def test_learn_extracts_when_wired_from_config():
    clear_policy_cache()
    assert knowledge_learn_integration_wired() is True
    outputs = {"tool_result": "Recursion is a technique where a function calls itself."}
    warnings: list = []
    _maybe_extract_knowledge(
        {"source_action_goal": {"intent": "LEARN"}},
        outputs,
        {"query": "python recursion"},
        "coding",
        "web_fetch",
        warnings,
    )
    assert outputs.get("knowledge_artifact_id")
    assert not warnings


def test_learn_skips_when_not_wired(monkeypatch):
    clear_policy_cache()
    monkeypatch.setattr(
        "pressure_loop.policy.knowledge_learn_integration_wired",
        lambda policy=None: False,
    )
    outputs = {"tool_result": "Recursion is a technique where a function calls itself."}
    warnings: list = []
    _maybe_extract_knowledge(
        {"source_action_goal": {"intent": "LEARN"}},
        outputs,
        {"query": "python recursion"},
        "coding",
        "web_fetch",
        warnings,
    )
    assert "knowledge_artifact_id" not in outputs
    assert not warnings


def test_web_fetch_extracts_outside_learn():
    """Chat and USE_TOOL fetches file knowledge the same way LEARN does."""
    outputs = {
        "tool_result": {
            "query": "asyncio",
            "results": [
                {
                    "title": "asyncio",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "content": "asyncio is a library to write concurrent code using async/await.",
                }
            ],
        }
    }
    warnings: list = []
    _maybe_extract_knowledge(
        {"source_action_goal": {"intent": "RESPOND"}},
        outputs,
        {"query": "what is asyncio", "web_research": True},
        "python",
        "web_fetch",
        warnings,
    )
    assert outputs.get("knowledge_artifact_id")
    assert not warnings


def test_missing_or_empty_intent_skips_non_web_tools():
    outputs = {"tool_result": "x"}
    warnings: list = []
    _maybe_extract_knowledge({}, outputs, {}, "", "file_reader", warnings)
    assert "knowledge_artifact_id" not in outputs
    assert not warnings
