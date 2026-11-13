"""Meta loop reads intent/tool/pack config via pressure_loop.policy."""

from __future__ import annotations

from pressure_loop.policy import (
    clear_policy_cache,
    meta_intent_to_action,
    meta_learn_default_tool,
    meta_pack_banned_keys,
)
from meta_loop.action.run import _intent_to_action_map
from meta_loop.tool.run import _learn_default_tool


def test_meta_yaml_intent_map_wired():
    clear_policy_cache()
    m = meta_intent_to_action()
    assert m.get("LEARN") == "USE_TOOL"
    assert _intent_to_action_map().get("LEARN") == "USE_TOOL"


def test_meta_learn_default_tool_from_config():
    clear_policy_cache()
    assert _learn_default_tool() == meta_learn_default_tool() == "web_fetch"


def test_meta_pack_banned_keys_from_config():
    clear_policy_cache()
    banned = meta_pack_banned_keys()
    assert "model" in banned
    assert "temperature" in banned
