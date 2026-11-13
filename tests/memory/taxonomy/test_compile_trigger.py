from unittest.mock import patch

import pytest

from core.required_substrate import RequiredSubstrateError
from memory.taxonomy import compile_trigger


def test_episode_rebuild_schedulers_are_retired():
    assert not hasattr(compile_trigger, "request_compile_on_close")
    assert not hasattr(compile_trigger, "request_reindex_after_deepsleep")
    assert not hasattr(compile_trigger, "_pending_timer")


def test_compile_on_boot_failure_is_attributed_and_propagates():
    with patch.object(compile_trigger, "reindex_and_compile", side_effect=RuntimeError("boom")):
        with pytest.raises(RequiredSubstrateError) as raised:
            compile_trigger.compile_on_boot()

        assert raised.value.component == "memory_taxonomy"
        assert raised.value.operation == "compile"
        assert isinstance(raised.value.__cause__, RuntimeError)


def test_compile_on_boot_rejects_an_absent_snapshot():
    with patch.object(compile_trigger, "reindex_and_compile", return_value=None):
        with pytest.raises(RequiredSubstrateError) as raised:
            compile_trigger.compile_on_boot()

    assert raised.value.component == "memory_taxonomy"
    assert raised.value.operation == "compile"
    assert raised.value.reason == "snapshot_missing"
