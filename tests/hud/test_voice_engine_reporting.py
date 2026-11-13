"""Every voice surface names the engine the runtime reports, never a hardcoded one.

Kokoro was retired (its ids now migrate to Qwen in hud/settings.py and are never
offered). The launcher's setup copy was corrected on 2026-09-19 (90b90fc1) because a
shipped surface promising a retired engine is a claim the files do not support. The same
claim survived in three operator-facing telemetry payloads, each with the literal
``"kokoro"`` spelled into it while ``runtime.tts.status()`` was already reporting the
live engine one field away.

A hardcoded engine name cannot be wrong about a swap it never reads, so these assert the
value is DERIVED: a status reporting an arbitrary engine must surface that engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hud import telemetry

ROOT = Path(__file__).resolve().parents[2]

# Every retired speech engine. A surface naming one of these is describing a product
# Omega does not ship.
RETIRED_SPEECH_ENGINES = ("kokoro", "piper", "jarvis", "whisper")


@pytest.fixture
def reported_engine(monkeypatch):
    """Point every voice surface at a status whose engine the test chooses."""

    def _use(engine: str) -> None:
        import runtime.stt as stt_module
        import runtime.tts as tts_module

        monkeypatch.setattr(
            tts_module,
            "status",
            lambda: {
                "available": True,
                "engine": engine,
                "expected_engine": engine,
                "voice": "Aiden",
                "base": "http://127.0.0.1:11434",
                "device": "Vulkan0",
                "execution_provider": "vulkan",
                "model_file": "models\\qwen3-tts\\model.gguf",
                "voice_resident": True,
                "sample_rate": 24000,
            },
        )
        monkeypatch.setattr(
            stt_module,
            "status",
            lambda: {"available": True, "engine": "parakeet", "base": "http://127.0.0.1:11434"},
        )

    return _use


@pytest.mark.parametrize("engine", ["qwen3-tts", "a-later-engine"])
def test_the_system_subtree_names_the_reported_engine(reported_engine, engine) -> None:
    reported_engine(engine)
    voice = telemetry._telemetry_system()["voice"]
    assert voice["tts"]["engine"] == engine, (
        "the system subtree must report the engine the runtime is running"
    )
    assert voice["stt"]["engine"] == "parakeet"


@pytest.mark.parametrize("engine", ["qwen3-tts", "a-later-engine"])
def test_the_identity_panel_voice_posture_names_the_reported_engine(
    reported_engine, engine
) -> None:
    reported_engine(engine)
    assert telemetry.panel_identity()["voice"]["engine"] == engine


@pytest.mark.parametrize("engine", ["qwen3-tts", "a-later-engine"])
def test_the_voice_pill_detail_names_the_reported_engine(reported_engine, engine) -> None:
    reported_engine(engine)
    detail = telemetry._probe_voice()["detail"]
    assert f"{engine}:ready" in detail, detail
    assert "parakeet:ready" in detail, detail


def test_a_tts_status_that_cannot_run_names_the_capability_not_an_engine(monkeypatch) -> None:
    """No engine is known when the probe itself failed, so none is invented."""
    import runtime.tts as tts_module

    def _raise():
        raise RuntimeError("runtime unreachable")

    monkeypatch.setattr(tts_module, "status", _raise)
    detail = telemetry._probe_voice()["detail"]
    assert "tts:missing" in detail, detail
    for retired in RETIRED_SPEECH_ENGINES:
        assert retired not in detail.casefold(), detail


@pytest.mark.parametrize(
    "path",
    ["hud/telemetry.py", "runtime/local_services.py", "runtime/client.py", "runtime/supervisor.py"],
)
def test_no_live_speech_surface_names_a_retired_engine(path: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8").casefold()
    for retired in RETIRED_SPEECH_ENGINES:
        assert retired not in source, f"{path} still names the retired {retired} engine"
