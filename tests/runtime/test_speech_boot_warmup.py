"""Boot must exercise both speech models before publishing readiness."""
import base64
import io
from threading import Lock
from types import SimpleNamespace
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from runtime import local_services, qwen_tts, tts
from tests.runtime.test_local_services import _FakeDme, _FakePackedDme, _FakeSpeech


def test_gateway_warms_both_speech_models_before_serving_and_keeps_them_owned():
    warmed = []

    class Speech(_FakeSpeech):
        def warmup(self):
            assert not self.closed
            warmed.append(self)

    voice, ears = Speech(), Speech()
    app = local_services.create_app(dme=_FakeDme(), packed_dme=_FakePackedDme(),
                                    tts=voice, stt=ears)
    assert warmed == [voice, ears]
    with TestClient(app) as client:
        for _ in range(2):
            assert client.get('/health').status_code == 200
        assert not voice.closed and not ears.closed
        assert warmed == [voice, ears]
    assert voice.closed and ears.closed


def test_failed_speech_warmup_prevents_readiness_and_releases_models():
    class Broken(_FakeSpeech):
        def warmup(self):
            raise RuntimeError('codec warmup failed')

    dme, packed, voice, ears = _FakeDme(), _FakePackedDme(), Broken(), _FakeSpeech()
    with pytest.raises(RuntimeError, match='codec warmup failed'):
        local_services.create_app(dme=dme, packed_dme=packed, tts=voice, stt=ears)
    assert all(service.closed for service in [dme, packed, voice, ears])


@pytest.mark.parametrize('audio', [b'\x00\x01' * 100, b'', b'\x00'], ids=['pcm', 'empty', 'partial'])
def test_qwen_warmup_exercises_pcm_once_and_rejects_empty_or_partial_audio(audio):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=audio)

    engine = qwen_tts.QwenTTSProcess.__new__(qwen_tts.QwenTTSProcess)
    engine.base_url = 'http://localhost:35802'
    engine.config = {'speaker': 'Ryan'}
    engine._warmed = False
    engine._warmup_lock = Lock()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        engine._client = client
        if not audio or len(audio) % 2:
            with pytest.raises(RuntimeError, match='PCM'):
                engine.warmup()
            assert not engine._warmed
        else:
            engine.warmup()
            engine.warmup()
            assert engine._warmed
            assert len(calls) == 1
            import json
            payload = json.loads(calls[0].content)
            assert payload['response_format'] == 'pcm'
            assert payload['voice'] == 'Ryan'


def test_parakeet_warmup_runs_real_transcribe_contract_once(monkeypatch):
    engine = local_services.ParakeetEngine.__new__(local_services.ParakeetEngine)
    engine._warmed = False
    engine._warmup_lock = Lock()
    requests = []

    def transcribe(body):
        requests.append(body)
        with wave.open(io.BytesIO(base64.b64decode(body['audio_b64']))) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
            assert wav.getnframes() >= 16000
        return {'ok': True, 'text': ''}

    monkeypatch.setattr(engine, 'transcribe', transcribe)
    engine.warmup()
    engine.warmup()
    assert engine._warmed and len(requests) == 1


@pytest.mark.parametrize('warmed', [True, False])
def test_hud_warmup_checks_resident_readiness_without_generating(monkeypatch, warmed):
    client = SimpleNamespace(health_body=lambda: {
        'capability_health': {'tts': {'ok': True, 'warmed': warmed, 'resident': True}}})
    monkeypatch.setattr(tts, 'get_runtime_client', lambda: client)
    monkeypatch.setattr(tts, '_tts_config', lambda: {'warmup_on_boot': True})
    monkeypatch.setattr(tts, 'synthesize', lambda *a, **k: pytest.fail('readiness generated speech'))
    if warmed:
        assert tts.warmup() is True
    else:
        with pytest.raises(RuntimeError, match='warm'):
            tts.warmup()


@pytest.mark.parametrize('ready', [True, False])
def test_tts_status_reports_speech_readiness_not_just_gateway_health(monkeypatch, ready):
    client = SimpleNamespace(base_url='http://localhost', health=lambda: True,
        health_body=lambda: {'capability_health': {'tts': {
            'engine': 'qwen3-tts', 'ok': ready, 'warmed': ready, 'resident': ready}}})
    monkeypatch.setattr(tts, 'get_runtime_client', lambda: client)
    result = tts.status()
    assert result['available'] is ready
    assert result['warmed'] is ready
    assert result['resident'] is ready
    assert result['engine'] == 'qwen3-tts'
