"""The Vulkan loader: the backend Omega asked for is the backend it got (operator 2026-09-22).

`config/tts.yaml` pins `device: Vulkan0`, and the live log proves qwentts.cpp honours it — all 28
talker layers plus the codec, quantizer, upsampler and DAC load into the Vulkan backend on the
860M. The binary has no `--n-gpu-layers` flag at all (`tts-server.exe --help`): placement follows
the ggml backend it selects, which Omega selects through `GGML_BACKEND`.

What was missing is the *verification*. Omega asked for Vulkan and then believed it. If the Vulkan
device is absent, the driver fails, or the DLL is missing, qwentts.cpp loads on CPU and reports
itself perfectly healthy — a voice roughly an order of magnitude slower, with nothing anywhere
saying the request was not honoured. That is silent degradation, which this substrate does not do
(AGENTS.md section 2): a component that cannot do its job fails in a way its caller cannot mistake
for success.

So the loader reads the server's OWN startup banner — `[Load] Talker backend: Vulkan0` — and holds
it to the request.
"""

from __future__ import annotations

import pytest

from runtime import qwen_tts

VULKAN_BANNER = """[Qwen] qwentts.cpp e46682f (2026-09-13)
ggml_vulkan: Found 1 Vulkan devices:
ggml_vulkan: 0 = AMD Radeon(TM) 860M Graphics (AMD proprietary driver) | uma: 1 | fp16: 1
[Load] Talker backend: Vulkan0 (CPU threads: 8)
[WeightCtx] Loaded 316 tensors, 774.6 MB into backend
[Talker] Loaded: 28 layers, hidden 1024, heads 16/8
[Pipeline] Ready: hop 1920 samples @ 24000 Hz mono
"""

CPU_BANNER = """[Qwen] qwentts.cpp e46682f (2026-09-13)
[Load] Talker backend: CPU (CPU threads: 8)
[WeightCtx] Loaded 316 tensors, 774.6 MB into backend
[Pipeline] Ready: hop 1920 samples @ 24000 Hz mono
"""


def test_the_banner_reports_the_backend_that_actually_loaded(tmp_path):
    log = tmp_path / "qwen-tts.log"
    log.write_text(VULKAN_BANNER, encoding="utf-8")

    assert qwen_tts.loaded_backend(log) == "Vulkan0"


def test_a_vulkan_request_served_on_cpu_is_a_failure_not_a_downgrade(tmp_path):
    log = tmp_path / "qwen-tts.log"
    log.write_text(CPU_BANNER, encoding="utf-8")

    with pytest.raises(qwen_tts.BackendNotHonoured) as info:
        qwen_tts.require_backend(log, requested="Vulkan0")
    message = str(info.value)
    assert "Vulkan0" in message and "CPU" in message, message
    assert "cpu" in message.lower()


def test_a_vulkan_request_served_on_vulkan_passes_and_reports_it(tmp_path):
    log = tmp_path / "qwen-tts.log"
    log.write_text(VULKAN_BANNER, encoding="utf-8")

    assert qwen_tts.require_backend(log, requested="Vulkan0") == "Vulkan0"


def test_auto_accepts_whatever_loaded_but_still_reports_it(tmp_path):
    log = tmp_path / "qwen-tts.log"
    log.write_text(CPU_BANNER, encoding="utf-8")

    assert qwen_tts.require_backend(log, requested="auto") == "CPU"


def test_a_banner_that_never_appeared_is_unknown_rather_than_assumed(tmp_path):
    log = tmp_path / "qwen-tts.log"
    log.write_text("[Qwen] qwentts.cpp e46682f\n", encoding="utf-8")

    assert qwen_tts.loaded_backend(log) is None
    with pytest.raises(qwen_tts.BackendNotHonoured, match="did not report"):
        qwen_tts.require_backend(log, requested="Vulkan0")


def test_an_absent_log_is_unknown_and_never_claims_success(tmp_path):
    assert qwen_tts.loaded_backend(tmp_path / "nope.log") is None
    with pytest.raises(qwen_tts.BackendNotHonoured):
        qwen_tts.require_backend(tmp_path / "nope.log", requested="Vulkan0")


def test_the_newest_banner_wins_when_the_log_holds_several_runs(tmp_path):
    """The log is appended across restarts; a stale CPU run must not condemn a Vulkan one."""
    log = tmp_path / "qwen-tts.log"
    log.write_text(CPU_BANNER + VULKAN_BANNER, encoding="utf-8")

    assert qwen_tts.loaded_backend(log) == "Vulkan0"
    assert qwen_tts.require_backend(log, requested="Vulkan0") == "Vulkan0"


def test_the_talker_variant_is_selectable_and_defaults_to_the_voice_in_use():
    r"""Two talkers now sit beside each other in J:\models\qwen3-tts — customvoice (in use) and
    base. The default must not change because a second payload arrived."""
    resolved = qwen_tts.paths({"qwen": {
        "root": "../models/qwen3-tts",
        "binary": "../models/qwen3-tts/tts-server.exe",
        "codec": "qwen-tokenizer-12hz-Q8_0.gguf",
    }})
    assert resolved["model"].name == "qwen-talker-0.6b-customvoice-Q8_0.gguf"

    chosen = qwen_tts.paths({"qwen": {
        "root": "../models/qwen3-tts",
        "binary": "../models/qwen3-tts/tts-server.exe",
        "model": "qwen-talker-0.6b-base-Q8_0.gguf",
        "codec": "qwen-tokenizer-12hz-Q8_0.gguf",
    }})
    assert chosen["model"].name == "qwen-talker-0.6b-base-Q8_0.gguf"


def test_startup_refuses_a_worker_that_loaded_on_the_wrong_device(tmp_path, monkeypatch):
    """The whole point: a healthy worker on the wrong device is a failure, not a voice."""
    import runtime.qwen_tts as qt

    monkeypatch.setattr(qt, "guardian_logs_root", lambda: tmp_path)
    (tmp_path / "qwen-tts.log").write_text(CPU_BANNER, encoding="utf-8")
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in ("tts-server.exe", "tts-server", "talker.gguf", "codec.gguf"):
        (payload / name).write_bytes(b"x")

    class _Proc:
        returncode = None

        def poll(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

    class _Resp:
        status_code = 200

    class _Client:
        def get(self, _url):
            return _Resp()

        def close(self):
            return None

    with pytest.raises(qt.BackendNotHonoured, match="Vulkan0"):
        qt.QwenTTSProcess(
            {"qwen": {"binary": str(payload / "tts-server.exe"),
                      "model": str(payload / "talker.gguf"),
                      "codec": str(payload / "codec.gguf"),
                      "device": "Vulkan0", "provider": "vulkan", "port": 39999}},
            popen=lambda *a, **k: _Proc(),
            client=_Client(),
        )
