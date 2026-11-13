from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from runtime import local_services


def test_parakeet_audio_staging_is_product_owned(monkeypatch, tmp_path) -> None:
    staging = tmp_path / "guardian" / "state" / "staging"
    monkeypatch.setattr(local_services, "STAGING_ROOT", staging)

    path = local_services._temporary_audio_path(".wav")

    assert path.parent == staging / "audio"
    assert path.suffix == ".wav"
    assert path.is_relative_to(staging)


def test_speech_device_policy_follows_the_selected_mind_seat() -> None:
    assert local_services.select_speech_execution("local", ["DmlExecutionProvider", "CPUExecutionProvider"]) == {
        "device": "cpu",
        "provider": "CPUExecutionProvider",
        "requested": "cpu",
    }
    assert local_services.select_speech_execution("cloud", ["DmlExecutionProvider", "CPUExecutionProvider"]) == {
        "device": "gpu",
        "provider": "DmlExecutionProvider",
        "requested": "gpu",
    }


def test_cloud_speech_reports_cpu_when_no_gpu_provider_is_shipped() -> None:
    assert local_services.select_speech_execution("cloud", ["CPUExecutionProvider"]) == {
        "device": "cpu",
        "provider": "CPUExecutionProvider",
        "requested": "gpu",
    }


@pytest.mark.parametrize(
    ("mind_seat", "device", "provider"),
    [("local", "CPU", "cpu"), ("cloud", "Vulkan0", "vulkan")],
)
def test_qwen_placement_follows_the_selected_mind_seat(
    monkeypatch, mind_seat, device, provider
) -> None:
    captured = []
    config = {
        "engine": "qwen",
        "qwen": {"device": "Vulkan0", "provider": "vulkan"},
    }

    class Qwen:
        def __init__(self, cfg):
            captured.append(cfg)

    monkeypatch.setattr(local_services, "selected_mind_seat", lambda: mind_seat)
    monkeypatch.setattr(
        "config.loader.load_subsystem_bundle",
        lambda _name: {"config": {"tts": config}},
    )
    monkeypatch.setattr("runtime.qwen_tts.payload_available", lambda _cfg: True)
    monkeypatch.setattr("runtime.qwen_tts.QwenTTSProcess", Qwen)

    local_services.create_tts_service()

    assert captured[0]["qwen"]["device"] == device
    assert captured[0]["qwen"]["provider"] == provider
    assert config["qwen"] == {"device": "Vulkan0", "provider": "vulkan"}


def test_parakeet_runs_on_the_cpu_so_the_voice_owns_the_gpu() -> None:
    """Operator 2026-09-22: "parakeet gets the cpu, qwen gets the gpu."

    The device is a shared-memory iGPU (AMD 860M, uma: 1, ~8 GB total), and qwen TTS holds about
    1.2 GB of weights plus 912 MB of KV cache on it. ASR competing for the same memory and compute
    costs the latency-critical path — speech has to START fast — while parakeet is quick enough on
    a 16-thread Ryzen AI 7. So placement is no longer a function of the mind seat: the voice owns
    the GPU, recognition owns the CPU.

    Superseded: `test_parakeet_backend_is_cpu_for_local_and_gpu_for_cloud`, which gave a cloud seat
    the Vulkan backend and put both engines on the same iGPU.
    """
    binary = local_services.Path("vulkan") / "crispasr.exe"

    assert local_services.select_parakeet_backend("local", binary) == "cpu"
    assert local_services.select_parakeet_backend("cloud", binary) == "cpu", (
        "a cloud seat must not put recognition on the GPU the voice needs"
    )


def test_parakeet_cpu_placement_does_not_depend_on_which_payload_shipped() -> None:
    """The Vulkan payload is the only one we ship, and it runs CPU perfectly well with
    --gpu-backend cpu. Placement must not flip because of the folder a binary sits in."""
    for folder in ("vulkan", "cpu", "somewhere-else"):
        binary = local_services.Path(folder) / "crispasr.exe"
        assert local_services.select_parakeet_backend("cloud", binary) == "cpu", folder


def test_dme_uses_the_vitis_child_instead_of_registering_it_in_the_gateway(
    monkeypatch,
) -> None:
    import sys
    from types import ModuleType

    monkeypatch.setenv("OMEGA_NATIVE_ORT", "1")
    registrations = []
    ort = ModuleType("onnxruntime")
    ort.register_execution_provider_library = lambda *args: registrations.append(args)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setattr(
        local_services,
        "_onnx_providers",
        lambda: ["VitisAIExecutionProvider", "CPUExecutionProvider"],
    )

    engine = local_services.DmeEngine()

    assert engine.provider == "CPUExecutionProvider"
    assert engine.device == "cpu"
    assert registrations == []


def test_dme_uses_cpu_when_npu_provider_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        local_services.DmeEngine,
        "_available_providers",
        staticmethod(lambda: ["CPUExecutionProvider"]),
    )

    engine = local_services.DmeEngine()

    assert engine.provider == "CPUExecutionProvider"
    assert engine.device == "cpu"


def test_dme_boot_is_native_onnx_free_and_scores_portably(monkeypatch) -> None:
    """A portable boot must not import the crash-prone native ORT extension."""
    monkeypatch.delenv("OMEGA_NATIVE_ORT", raising=False)

    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "onnxruntime":
            raise AssertionError("portable DME boot imported onnxruntime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    engine = local_services.DmeEngine()
    result = engine.overlap([1.0] * 3904, [[1.0] * 3904])

    assert engine.backend == "numpy"
    assert result == {
        "ok": True,
        "overlaps": [3904],
        "backend": "CPUExecutionProvider",
        "engine": "numpy",
    }


def test_dme_native_probe_failure_keeps_numpy_capability(monkeypatch) -> None:
    monkeypatch.setenv("OMEGA_NATIVE_ORT", "1")

    def fail_probe(self):
        self.provider_probe_error = "native ONNX Runtime unavailable: test failure"
        return ["CPUExecutionProvider"]

    monkeypatch.setattr(local_services.DmeEngine, "_available_providers", fail_probe)
    engine = local_services.DmeEngine()

    assert engine.backend == "numpy"
    assert engine.health()["ok"] is True
    assert "native ONNX Runtime unavailable" in engine.health()["provider_probe_error"]


def test_gateway_boot_keeps_optional_native_lanes_explicitly_unavailable(monkeypatch) -> None:
    class _Broken:
        def __init__(self):
            raise ImportError("native DLL mismatch")

    monkeypatch.setattr(local_services, "DmeEngine", _Broken)
    monkeypatch.setattr(local_services, "PackedDmeEngine", _Broken)
    monkeypatch.setattr(local_services, "ParakeetEngine", _Broken)
    # Keep the test deterministic even when the optional Qwen payload is
    # present on the developer SSD.  This exercises the unavailable-lane
    # contract without starting a real sidecar process.
    monkeypatch.setattr(local_services, "create_tts_service", _Broken)

    app = local_services.create_app(warmup=False)
    with TestClient(app) as client:
        health = client.get("/health").json()
        overlap = client.post(
            "/dme/overlap", json={"query": [0.0] * 3904, "memory": []}
        ).json()

    assert health["status"] == "ok"
    assert health["capability_health"]["dme"]["ok"] is False
    assert health["capability_health"]["tts"]["reason"] == "tts_unavailable"
    assert overlap["ok"] is False
    assert overlap["reason"] == "dme_unavailable"


class _FakeDme:
    provider = "CPUExecutionProvider"
    device = "cpu"
    provider_probe_error = ""

    def __init__(self) -> None:
        self.closed = False

    def warmup(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeSpeech:
    execution = {"device": "cpu", "provider": "CPUExecutionProvider"}
    device = "cpu"
    backend = "cpu"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StreamingSpeech(_FakeSpeech):
    engine = "qwen3-tts"

    def health(self) -> dict[str, object]:
        return {
            "ok": True,
            "engine": self.engine,
            "device": self.execution["device"],
            "provider": self.execution["provider"],
        }

    async def synthesise_stream(self, text: str, voice: str, speed: float):
        assert (text, voice, speed) == ("hello", "af_bella", 1.0)
        yield b"pcm-a"
        yield b"pcm-b"


class _FakePackedDme:
    provider = "VitisAIExecutionProvider"
    device = "npu"
    loaded = None

    def __init__(self) -> None:
        self.closed = False

    def health(self) -> dict[str, str | bool]:
        return {"ok": True, "device": self.device, "provider": self.provider}

    def load_plane(self, path: str, rows: int) -> dict[str, object]:
        self.loaded = (path, rows)
        return {"ok": True, "device": self.device, "provider": self.provider}

    def overlap(self, queries_b64: str) -> dict[str, object]:
        return {
            "ok": True,
            "overlaps": [[7, 3]],
            "device": self.device,
            "provider": self.provider,
            "queries_b64": queries_b64,
        }

    def close(self) -> None:
        self.closed = True


def test_packed_dme_routes_expose_the_npu_service_contract() -> None:
    dme = _FakeDme()
    tts = _FakeSpeech()
    stt = _FakeSpeech()
    packed = _FakePackedDme()
    app = local_services.create_app(
        dme=dme,
        tts=tts,
        stt=stt,
        packed_dme=packed,
        warmup=False,
    )

    with TestClient(app) as client:
        assert client.get("/dme/packed/health").json() == {
            "ok": True,
            "device": "npu",
            "provider": "VitisAIExecutionProvider",
        }
        assert client.post(
            "/dme/packed/plane",
            json={"path": "I:/Omega3.0/memory/dme/sram.bin", "rows": 2},
        ).json()["ok"] is True
        result = client.post(
            "/dme/packed/overlap", json={"queries_b64": "AQ=="}
        ).json()
        assert result["overlaps"] == [[7, 3]]
        assert result["device"] == "npu"

    assert dme.closed is True
    assert packed.closed is True
    assert tts.closed is True
    assert stt.closed is True


class _FakeVitisClient:
    """Stands in for the cp312 child: the parent's wiring is under test here.

    ``open_tiles`` opens the parent-baked manifest with the real backend on
    the CPU provider, so overlap exactness is checked end to end through the
    same manifest contract the child uses.
    """

    def __init__(self, providers=("VitisAIExecutionProvider", "CPUExecutionProvider"),
                 *, proven: bool = True, start_error: Exception | None = None) -> None:
        self.providers = list(providers)
        self.proven = proven
        self.start_error = start_error
        self.requests: list[tuple[str, dict]] = []
        self.session = None
        self.closed = False
        self.alive = False

    def start(self) -> dict:
        if self.start_error is not None:
            raise self.start_error
        self.alive = True
        return {
            "python": "3.12.10",
            "executable": "python.exe",
            "onnxruntime": "1.23.2",
            "providers": self.providers,
        }

    def request(self, op: str, params: dict, *, timeout_s: float | None = None) -> dict:
        from memory.dme import npu_backend
        from runtime import vitis_worker

        self.requests.append((op, params))
        if op == "open_tiles":
            self.session = npu_backend.ShardedOverlapSession.from_manifest(
                params["manifest"], cache_root=Path(params["cache_root"]),
            )
            return {
                "tiles": len(params["manifest"]["tiles"]),
                "providers": self.providers,
                "engagement": [
                    {"proven": self.proven} for _ in params["manifest"]["tiles"]
                ],
            }
        if op == "overlap":
            bits = vitis_worker.decode_bits(params["bits"])
            return {"overlaps": self.session.overlap(bits).tolist()}
        raise AssertionError(op)

    def close(self) -> None:
        self.closed = True
        self.alive = False


def _packed_plane(root: Path, rows: int) -> tuple[Path, np.ndarray]:
    from memory.dme.lobe_layout import ENTRY_WORDS
    from memory.dme.packer import HEADER_WORDS

    rng = np.random.default_rng(11)
    bits = np.zeros((rows, ENTRY_WORDS * 64), dtype=np.uint8)
    payload = bits[:, HEADER_WORDS * 64:]
    payload[:] = (rng.random(payload.shape) < 0.0005).astype(np.uint8)
    words = np.packbits(bits, axis=1, bitorder="little").view(np.uint64)
    path = root / "memory" / "dme" / "sram.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    words.tofile(path)
    return path, words


def _packed_engine(monkeypatch, tmp_path: Path, client: _FakeVitisClient):
    from memory.dme import npu_backend

    monkeypatch.setattr(local_services, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(npu_backend, "default_cache_root", lambda: tmp_path / "tiles")
    return local_services.PackedDmeEngine(client_factory=lambda: client)


def test_packed_dme_names_a_missing_vitis_interpreter(tmp_path) -> None:
    missing = tmp_path / ".venv-vitis" / "Scripts" / "python.exe"

    engine = local_services.PackedDmeEngine(
        client_factory=lambda: local_services.vitis_lane.default_client(tmp_path),
    )

    health = engine.health()
    assert health["ok"] is False
    assert health["device"] == "unavailable"
    assert "Vitis lane interpreter is missing" in health["reason"]
    assert str(missing) in health["reason"]
    with pytest.raises(RuntimeError, match="Vitis lane interpreter is missing"):
        engine.load_plane(str(tmp_path / "memory" / "dme" / "sram.bin"), 2)


def test_packed_dme_is_unavailable_when_the_child_has_no_vitis_provider(
    monkeypatch, tmp_path
) -> None:
    client = _FakeVitisClient(providers=("CPUExecutionProvider",))

    engine = _packed_engine(monkeypatch, tmp_path, client)

    health = engine.health()
    assert health["ok"] is False
    assert "VitisAIExecutionProvider" in health["reason"]
    assert "CPUExecutionProvider" in health["reason"]
    assert client.closed is True


def test_packed_dme_reports_a_child_that_could_not_start(monkeypatch, tmp_path) -> None:
    client = _FakeVitisClient(
        start_error=local_services.vitis_lane.VitisWorkerExited(
            3, "ImportError: DLL load failed while importing onnxruntime"
        )
    )

    engine = _packed_engine(monkeypatch, tmp_path, client)

    health = engine.health()
    assert health["ok"] is False
    assert "DLL load failed" in health["reason"]


def test_packed_dme_health_names_the_child_runtime(monkeypatch, tmp_path) -> None:
    engine = _packed_engine(monkeypatch, tmp_path, _FakeVitisClient())

    health = engine.health()
    assert health["ok"] is True
    assert health["device"] == "npu"
    assert health["provider"] == "VitisAIExecutionProvider"
    assert health["loaded"] is False
    assert health["worker"] == {"python": "3.12.10", "onnxruntime": "1.23.2"}


def test_packed_dme_bakes_in_the_parent_and_scores_exactly_in_the_child(
    monkeypatch, tmp_path
) -> None:
    import base64

    from memory.dme.packer import HEADER_WORDS

    client = _FakeVitisClient()
    engine = _packed_engine(monkeypatch, tmp_path, client)
    path, words = _packed_plane(tmp_path, rows=2)

    loaded = engine.load_plane(str(path), 2)
    query_words = words[:1].copy()
    result = engine.overlap(base64.b64encode(query_words.tobytes()).decode("ascii"))

    op, params = client.requests[0]
    assert op == "open_tiles"
    assert params["verify_npu"] is True
    assert params["cache_root"] == str(tmp_path / "tiles")
    assert all(
        (tmp_path / "tiles" / f"{tile['key']}.onnx").is_file()
        for tile in params["manifest"]["tiles"]
    )
    assert loaded["loaded"] is True
    bits = np.unpackbits(words.view(np.uint8), axis=1, bitorder="little")[:, HEADER_WORDS * 64:]
    assert result["overlaps"] == (bits[:1].astype(np.int64) @ bits.T.astype(np.int64)).tolist()
    assert result["device"] == "npu"


def test_packed_dme_refuses_a_plane_whose_placement_was_not_proven(
    monkeypatch, tmp_path
) -> None:
    engine = _packed_engine(monkeypatch, tmp_path, _FakeVitisClient(proven=False))
    path, _ = _packed_plane(tmp_path, rows=2)

    with pytest.raises(RuntimeError, match="not proven for every tile"):
        engine.load_plane(str(path), 2)

    assert engine.health()["loaded"] is False


def test_packed_dme_refuses_a_plane_outside_the_omega_tree(monkeypatch, tmp_path) -> None:
    engine = _packed_engine(monkeypatch, tmp_path / "omega", _FakeVitisClient())
    path, _ = _packed_plane(tmp_path / "elsewhere", rows=2)

    with pytest.raises(ValueError, match="outside the Omega tree"):
        engine.load_plane(str(path), 2)


def test_gateway_builds_packed_dme_on_the_vitis_lane(monkeypatch) -> None:
    client = _FakeVitisClient()
    roots: list[Path] = []

    def default_client(root):
        roots.append(root)
        return client

    monkeypatch.setattr(local_services.vitis_lane, "default_client", default_client)
    app = local_services.create_app(
        dme=_FakeDme(), tts=_FakeSpeech(), stt=_FakeSpeech(), warmup=False,
    )

    with TestClient(app) as http:
        assert http.get("/dme/packed/health").json()["worker"]["python"] == "3.12.10"

    assert roots == [local_services.OMEGA_ROOT]
    assert client.closed is True


def test_packed_dme_close_releases_the_child(monkeypatch, tmp_path) -> None:
    client = _FakeVitisClient()
    engine = _packed_engine(monkeypatch, tmp_path, client)

    engine.close()

    assert client.closed is True
    assert engine.health()["ok"] is False


def test_tts_stream_route_exposes_pcm_contract_and_engine_headers() -> None:
    dme = _FakeDme()
    tts = _StreamingSpeech()
    stt = _FakeSpeech()
    packed = _FakePackedDme()
    app = local_services.create_app(
        dme=dme,
        tts=tts,
        stt=stt,
        packed_dme=packed,
        warmup=False,
    )

    with TestClient(app) as client:
        response = client.post(
            "/synthesise/stream",
            json={"text": "hello", "voice": "af_bella", "length_scale": 1.0},
        )

    assert response.status_code == 200
    assert response.content == b"pcm-apcm-b"
    assert response.headers["content-type"] == "audio/pcm"
    assert response.headers["x-omega-tts-sample-rate"] == "24000"
    assert response.headers["x-omega-tts-channels"] == "1"
    assert response.headers["x-omega-tts-sample-width"] == "2"
    assert response.headers["x-omega-tts-engine"] == "qwen3-tts"


def test_tts_stream_route_reports_unavailable_service_before_streaming() -> None:
    class _DownSpeech(_StreamingSpeech):
        def health(self) -> dict[str, object]:
            return {
                "ok": False,
                "engine": self.engine,
                "reason": "qwen_tts_unavailable",
            }

    app = local_services.create_app(
        dme=_FakeDme(),
        tts=_DownSpeech(),
        stt=_FakeSpeech(),
        packed_dme=_FakePackedDme(),
        warmup=False,
    )

    with TestClient(app) as client:
        response = client.post("/synthesise/stream", json={"text": "hello"})

    assert response.status_code == 503
    assert response.json()["reason"] == "qwen_tts_unavailable"


def test_local_service_shutdown_endpoint_releases_every_service() -> None:
    dme = _FakeDme()
    tts = _FakeSpeech()
    stt = _FakeSpeech()
    packed = _FakePackedDme()
    app = local_services.create_app(
        dme=dme,
        tts=tts,
        stt=stt,
        packed_dme=packed,
        warmup=False,
    )

    with TestClient(app) as client:
        response = client.post("/admin/orchestration/shutdown")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "shutdown": "services_closed"}
    assert dme.closed is True
    assert packed.closed is True
    assert tts.closed is True
    assert stt.closed is True


def test_warmup_failure_releases_every_constructed_service() -> None:
    class _FailingDme(_FakeDme):
        def warmup(self) -> None:
            raise RuntimeError("DME warm-up failed")

    dme = _FailingDme()
    tts = _FakeSpeech()
    stt = _FakeSpeech()
    packed = _FakePackedDme()

    with pytest.raises(RuntimeError, match="DME warm-up failed"):
        local_services.create_app(
            dme=dme,
            tts=tts,
            stt=stt,
            packed_dme=packed,
            warmup=True,
        )

    assert dme.closed is True
    assert packed.closed is True
    assert tts.closed is True
    assert stt.closed is True


def test_shutdown_endpoint_requests_server_exit_even_when_release_fails() -> None:
    class _FailingSpeech(_FakeSpeech):
        def close(self) -> None:
            self.closed = True
            raise RuntimeError("TTS release failed")

    dme = _FakeDme()
    tts = _FailingSpeech()
    stt = _FakeSpeech()
    packed = _FakePackedDme()
    app = local_services.create_app(
        dme=dme,
        tts=tts,
        stt=stt,
        packed_dme=packed,
        warmup=False,
    )
    server = SimpleNamespace(should_exit=False)
    app.state.server = server
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/admin/orchestration/shutdown"
    )

    with pytest.raises(ExceptionGroup, match="local service release failed"):
        endpoint()

    assert server.should_exit is True
    assert dme.closed is True
    assert packed.closed is True
    assert tts.closed is True
    assert stt.closed is True


def test_parakeet_start_failure_closes_worker_and_http_client(monkeypatch, tmp_path) -> None:
    model = tmp_path / "parakeet.gguf"
    model.write_bytes(b"model")
    events: list[str] = []

    class _Process:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")

    class _Client:
        def get(self, _url):
            raise local_services.httpx.ConnectError("worker not ready")

        def close(self):
            events.append("client_close")

    clock = iter((0.0, 181.0))
    monkeypatch.setattr(local_services.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(local_services.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(local_services.subprocess, "Popen", lambda *_a, **_k: _Process())
    monkeypatch.setattr(
        local_services.ParakeetEngine,
        "_binary",
        lambda _self: tmp_path / "cpu" / "crispasr.exe",
    )

    engine = local_services.ParakeetEngine.__new__(local_services.ParakeetEngine)
    engine.model_path = model
    engine.process = None
    engine.base_url = ""
    engine.mind_seat = "local"
    engine.backend = ""
    engine.device = ""
    engine._client = _Client()

    with pytest.raises(TimeoutError, match="did not become healthy"):
        engine._start()

    assert events == ["client_close", "terminate", "wait:10"]
    assert engine.process is None
    assert engine.base_url == ""


def test_generate_stream_route_forwards_tool_call_and_reasoning_frames(monkeypatch) -> None:
    """Live 2026-09-15: the proxy streamed only tokens, so a reply that called web_search
    reached the brain as an empty completion."""
    from runtime import brain_local

    monkeypatch.setattr(brain_local, "generate_stream_events", lambda body, **_: iter([
        ("reasoning", "look it up"),
        ("tool_call", {"index": 0, "id": "call-1", "name": "web_search", "arguments": "{\"query\": \"Qwen\"}"}),
        ("token", "Searching."),
        ("done", {"finish_reason": "tool_calls", "model_id": "mistral-medium"}),
    ]))
    app = local_services.create_app(dme=_FakeDme(), tts=_FakeSpeech(), stt=_FakeSpeech(),
                                    packed_dme=_FakePackedDme(), warmup=False)

    response = TestClient(app).post("/generate/stream", json={"prompt": "Tell me about Qwen3.5 2B"})

    assert response.status_code == 200
    body = response.text
    assert 'event: reasoning\ndata: {"text": "look it up"}' in body
    assert 'event: tool_call\ndata: {"index": 0, "id": "call-1", "name": "web_search"' in body
    assert 'event: token\ndata: {"token": "Searching."}' in body
    assert 'event: done\ndata: {"finish_reason": "tool_calls", "model_id": "mistral-medium"}' in body


def test_gateway_stays_up_while_its_brain_lives_and_leaves_with_it() -> None:
    """POSIX counterpart of the Windows kill-on-close job: once the gateway is
    re-parented its brain is gone, and its whole process group (TTS + STT
    sidecars included) is terminated instead of being orphaned."""
    import signal as _signal

    from runtime import local_services

    parents = iter([4242, 4242, 1])
    sleeps: list[float] = []
    killed: list[tuple[int, int]] = []

    local_services.exit_with_parent(
        4242,
        getppid=lambda: next(parents),
        getpgrp=lambda: 777,
        killpg=lambda pgrp, sig: killed.append((pgrp, sig)),
        sleep=sleeps.append,
        interval_s=0.5,
    )

    assert sleeps == [0.5, 0.5]
    assert killed == [(777, _signal.SIGTERM)]


def test_gateway_leaves_at_once_when_its_brain_is_already_gone() -> None:
    from runtime import local_services

    killed: list[int] = []
    local_services.exit_with_parent(
        4242,
        getppid=lambda: 1,
        getpgrp=lambda: 777,
        killpg=lambda pgrp, sig: killed.append(pgrp),
        sleep=lambda _s: None,
    )

    assert killed == [777]
