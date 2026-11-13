"""The LEARN grounding recall must search the DOMAIN's own lobe — the subcategory Omega chose
to learn decides where it recalls. A coding subcat ("python", lobe 8) recalls via remember_code;
a knowledge subcat via remember_knowledge. The old fixed remember_knowledge search missed coding
facts entirely, so the mind was blind to what it already knew about a coding topic."""

from __future__ import annotations

from types import SimpleNamespace

from meta_loop.memory.run import _apply_learn_knowledge_recall, _learn_recall_tool_for_domain


def test_coding_subcategory_recalls_from_code_lobe():
    assert _learn_recall_tool_for_domain("python") == "remember_code"
    assert _learn_recall_tool_for_domain("Python") == "remember_code"
    assert _learn_recall_tool_for_domain("JavaScript") == "remember_code"
    assert _learn_recall_tool_for_domain("React") == "remember_code"


def test_knowledge_subcategory_recalls_from_knowledge_lobe():
    assert _learn_recall_tool_for_domain("algorithms") == "remember_knowledge"
    assert _learn_recall_tool_for_domain("system design") == "remember_knowledge"
    assert _learn_recall_tool_for_domain("distributed systems") == "remember_knowledge"
    assert _learn_recall_tool_for_domain("") == "remember_knowledge"


def test_apply_learn_recall_routes_python_to_remember_code():
    state = SimpleNamespace(execution_inputs={"domain": "python"}, memory_scope=())
    _apply_learn_knowledge_recall(state, "python programming language")
    assert state.memory_tools[0]["tool"] == "remember_code"
    assert state.memory_tools[0]["domain"] == "python"


def test_apply_learn_recall_routes_algorithms_to_remember_knowledge():
    state = SimpleNamespace(execution_inputs={"domain": "algorithms"}, memory_scope=())
    _apply_learn_knowledge_recall(state, "sorting and searching")
    assert state.memory_tools[0]["tool"] == "remember_knowledge"
    assert state.memory_tools[0]["domain"] == "algorithms"
