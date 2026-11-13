from __future__ import annotations

from runtime.client import TIMEOUTS


def test_synthesise_timeout_reads_tts_yaml():
    assert TIMEOUTS["synthesise"] >= 180
