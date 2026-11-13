from __future__ import annotations

import base64
import asyncio
import json
import os
from pathlib import Path

import pytest

from runtime import local_services, qwen_tts


def _qwen_files(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "qwen3-tts"
    root.mkdir()
    paths = {
        "root": root,
        "binary": root / "tts-server.exe",
        "model": root / "qwen-talker-0.6b-customvoice-Q8_0.gguf",
        "codec": root / "qwen-tokenizer-12hz-Q8_0.gguf",
    }
    for key in ("binary", "model", "codec"):
        paths[key].write_bytes(b"payload")
    # The shipped payload carries the sidecar for both platforms.
    (root / "tts-server").write_bytes(b"payload")
    return paths


def test_qwen_paths_are_rooted_and_payload_is_all_or_nothing(tmp_path) -> None:
    files = _qwen_files(tmp_path)
    cfg = {"qwen": {"root": str(files["root"]), "binary": str(files["binary"])} }

    resolved = qwen_tts.paths(cfg, platform="nt")

    assert resolved["model"] == files["model"].resolve()
    assert resolved["codec"] == files["codec"].resolve()
    assert resolved["binary"] == files["binary"].resolve()
    assert qwen_tts.payload_available(cfg) is True

    files["codec"].unlink()
    assert qwen_tts.payload_available(cfg) is False


def test_posix_runs_the_configured_sidecar_without_the_windows_suffix(tmp_path, monkeypatch) -> None:
    """config/tts.yaml names the Windows executable (the release builder stages it
    verbatim). On Linux the same payload directory carries the ELF build of that
    sidecar under the same name without ``.exe``."""
    monkeypatch.delenv("OMEGA_QWEN_TTS_BIN", raising=False)
    files = _qwen_files(tmp_path)
    cfg = {"qwen": {"root": str(files["root"]), "binary": str(files["binary"])}}

    assert qwen_tts.paths(cfg, platform="posix")["binary"] == files["root"].resolve() / "tts-server"
    assert qwen_tts.paths(cfg, platform="nt")["binary"] == files["binary"].resolve()


def test_explicit_binary_override_is_used_verbatim_on_every_platform(tmp_path, monkeypatch) -> None:
    override = tmp_path / "custom-tts.exe"
    monkeypatch.setenv("OMEGA_QWEN_TTS_BIN", str(override))

    assert qwen_tts.paths({"qwen": {}}, platform="posix")["binary"] == override.resolve()


def test_modality_manifest_matches_the_qwen_runtime_contract(monkeypatch) -> None:
    from config.loader import load_subsystem_bundle

    monkeypatch.delenv("OMEGA_QWEN_TTS_ROOT", raising=False)
    monkeypatch.delenv("OMEGA_QWEN_TTS_BIN", raising=False)
    manifest_path = Path(__file__).resolve().parents[2] / "models" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    qwen = manifest["models"]["qwen3_tts"]
    config = load_subsystem_bundle("tts")["config"]["tts"]
    qwen_config = config["qwen"]
    payload_root = (manifest_path.parent / qwen["payload_root"]).resolve()
    runtime_paths = qwen_tts.paths(config, platform="nt")

    assert "kokoro_tts" not in manifest["models"]
    assert payload_root == runtime_paths["binary"].parent
    assert qwen["device"] == qwen_config["device"] == "Vulkan0"
    assert qwen["provider"] == qwen_config["provider"] == "vulkan"
    assert qwen["runtime"]["path"].endswith("/tts-server.exe")
    assert qwen["variants"]["customvoice_q8_0"]["path"].endswith(
        "/" + qwen_config["model"]
    )
    assert qwen["variants"]["codec_12hz_q8_0"]["path"].endswith(
        "/" + qwen_config["codec"]
    )


def test_qwen_synthesis_preserves_openai_audio_contract() -> None:
    class Response:
        status_code = 200
        content = b"RIFF-qwen"
        text = ""

        def json(self):
            return {}

    class Client:
        def __init__(self):
            self.seen = None

        def post(self, url, *, json):
            self.seen = (url, json)
            return Response()

        def close(self):
            return None

    client = Client()
    engine = qwen_tts.QwenTTSProcess.__new__(qwen_tts.QwenTTSProcess)
    engine.base_url = "http://127.0.0.1:35802"
    engine.config = {"speaker": "Aiden"}
    engine._client = client

    result = engine.synthesise("hello from omega", "", 1.0)

    assert result["ok"] is True
    assert base64.b64decode(result["audio_b64"]) == b"RIFF-qwen"
    assert client.seen == (
        "http://127.0.0.1:35802/v1/audio/speech",
        {
            "input": "hello from omega",
            "voice": "Aiden",
            "response_format": "wav",
            "speed": 1.0,
        },
    )


def test_qwen_preserves_explicit_speaker_instead_of_remapping() -> None:
    class Response:
        status_code = 200
        content = b"RIFF-qwen"
        text = ""

        def json(self):
            return {}

    class Client:
        def __init__(self):
            self.seen = None

        def post(self, url, *, json):
            self.seen = (url, json)
            return Response()

    client = Client()
    engine = qwen_tts.QwenTTSProcess.__new__(qwen_tts.QwenTTSProcess)
    engine.base_url = "http://127.0.0.1:35802"
    engine.config = {"speaker": "Aiden"}
    engine._client = client

    engine.synthesise("hello", "Ryan", 1.0)

    assert client.seen[1]["voice"] == "Ryan"


def test_qwen_stream_requests_raw_pcm_and_yields_chunks() -> None:
    class Response:
        status_code = 200
        text = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_bytes(self):
            yield b"pcm-a"
            yield b"pcm-b"

    class Client:
        def __init__(self):
            self.seen = None

        def stream(self, method, url, *, json):
            self.seen = (method, url, json)
            return Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    client = Client()
    engine = qwen_tts.QwenTTSProcess.__new__(qwen_tts.QwenTTSProcess)
    engine.base_url = "http://127.0.0.1:35802"
    engine.config = {"speaker": "Aiden"}
    engine._client = client

    engine._async_client_factory = lambda: client

    async def collect():
        return [chunk async for chunk in engine.synthesise_stream("stream this", "", 1.1)]

    assert asyncio.run(collect()) == [b"pcm-a", b"pcm-b"]
    assert client.seen == (
        "POST",
        "http://127.0.0.1:35802/v1/audio/speech",
        {
            "input": "stream this",
            "voice": "Aiden",
            "response_format": "pcm",
            "speed": 1.1,
        },
    )


def test_qwen_process_starts_with_model_and_codec_and_closes(tmp_path) -> None:
    files = _qwen_files(tmp_path)
    calls: list[tuple[str, object]] = []

    class Process:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            calls.append(("terminate", None))

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    class Client:
        def get(self, url):
            calls.append(("get", url))
            return type("Response", (), {"status_code": 200, "json": lambda self: {}})()

        def close(self):
            calls.append(("client_close", None))

    spawned: list[list[str]] = []
    environments = []

    def popen(command, **_kwargs):
        spawned.append(command)
        environments.append(_kwargs["env"])
        # The current Qwen runtime requires the child's announced backend, not
        # just a healthy HTTP response. Simulate the real sidecar's startup log.
        _kwargs["stdout"].write("[Load] Talker backend: Vulkan0 (CPU threads: 8)\n")
        _kwargs["stdout"].flush()
        return Process()

    cfg = {
        "qwen": {
            "root": str(files["root"]),
            "binary": str(files["binary"]),
            "start_timeout_seconds": 5,
            "device": "Vulkan0",
            "provider": "vulkan",
        }
    }
    engine = qwen_tts.QwenTTSProcess(cfg, popen=popen, client=Client())

    sidecar = files["binary"] if os.name == "nt" else files["root"] / "tts-server"
    assert spawned and spawned[0][0] == str(sidecar.resolve())
    assert "--model" in spawned[0] and str(files["model"].resolve()) in spawned[0]
    assert "--codec" in spawned[0] and str(files["codec"].resolve()) in spawned[0]
    assert engine.health()["ok"] is True
    assert environments[0]["GGML_BACKEND"] == "Vulkan0"
    engine.close()
    assert ("terminate", None) in calls
    assert ("client_close", None) in calls


def test_selected_tts_is_qwen_vulkan_from_portable_sibling():
    from config.loader import load_subsystem_bundle

    cfg = load_subsystem_bundle("tts")["config"]["tts"]
    assert cfg["engine"] == "qwen"
    assert cfg["qwen"]["provider"] == "vulkan"
    assert cfg["qwen"]["device"] == "Vulkan0"
    assert cfg["qwen"]["root"] == "../models/qwen3-tts"


def test_runtime_default_speaker_is_delegated_to_selected_engine():
    assert local_services.SynthesiseRequest(text="Hello").voice == ""


def test_auto_selector_fails_closed_until_qwen_payload_is_staged(monkeypatch) -> None:
    monkeypatch.setattr(
        "config.loader.load_subsystem_bundle",
        lambda _name: {"config": {"tts": {"engine": "auto"}}},
    )
    monkeypatch.setattr(qwen_tts, "payload_available", lambda _cfg: False)

    with pytest.raises(FileNotFoundError, match="only supported speech engine"):
        local_services.create_tts_service()


def test_explicit_qwen_selection_fails_closed_without_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        "config.loader.load_subsystem_bundle",
        lambda _name: {"config": {"tts": {"engine": "qwen"}}},
    )
    monkeypatch.setattr(qwen_tts, "payload_available", lambda _cfg: False)

    with pytest.raises(FileNotFoundError, match="Qwen TTS is selected"):
        local_services.create_tts_service()
