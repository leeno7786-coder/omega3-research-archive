"""Global A's intent router: shared contract, pinned bundle, and the accept-or-escalate rule."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from global_loop.intent import router as R

REPO = Path(__file__).resolve().parents[2]
CONFIG = {
    "routes": list(R.ROUTES),
    "certainty": list(R.CERTAINTY),
    "input_template": {"with_context": "[PREVIOUS ASSISTANT ACTION] {context} [CURRENT USER MESSAGE] {text}",
                       "without_context": "[CURRENT USER MESSAGE] {text}"},
    "max_length": 128,
    "temperatures": {"primary_temperature": 1.0, "intent_temperature": 1.0, "certainty_temperature": 1.0},
    "thresholds": {"FREEFORM_CHAT": 0.9, "CODING_AGENT": 0.95, "BROWSER_AGENT": 0.95, "DOCUMENT_CREATION": 0.95},
    "margin": 0.3,
    "known_threshold": 0.6,
    "intent_thresholds": {route: 0.5 for route in R.ROUTES},
}


def _logits(probs) -> np.ndarray:
    return np.log(np.asarray([probs], dtype=np.float64))


# message -> (primary probs, intent probs, certainty probs)
SCRIPT = {
    "run the tests in my repo": ([0.01, 0.97, 0.01, 0.01], [0.02, 0.98, 0.01, 0.01], [0.97, 0.02, 0.01]),
    "click it": ([0.20, 0.38, 0.40, 0.02], [0.3, 0.5, 0.6, 0.1], [0.90, 0.09, 0.01]),
    "asdfgh": ([0.97, 0.01, 0.01, 0.01], [0.0, 0.0, 0.0, 0.0], [0.05, 0.10, 0.85]),
    "look up cuda and update my doc": ([0.93, 0.02, 0.02, 0.03], [0.95, 0.05, 0.1, 0.9], [0.95, 0.04, 0.01]),
}


class FakeTokenizer:
    @classmethod
    def from_file(cls, path):
        return cls()

    def no_padding(self):
        return None

    def enable_truncation(self, max_length):
        self.max_length = max_length

    def encode(self, text):
        message = text.split("[CURRENT USER MESSAGE] ", 1)[1]
        return SimpleNamespace(ids=[list(SCRIPT).index(message) + 1], attention_mask=[1])


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, path, providers=None):
        self.providers = providers
        self.calls = 0
        FakeSession.instances.append(self)

    def run(self, names, feeds):
        self.calls += 1
        primary, intents, certainty = list(SCRIPT.values())[int(feeds["input_ids"][0][0]) - 1]
        return _logits(primary), np.log(np.asarray([intents]) + 1e-9) - np.log(1 - np.asarray([intents]) + 1e-9), \
            _logits(certainty)


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    import onnxruntime
    import tokenizers

    FakeSession.instances.clear()
    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeSession)
    monkeypatch.setattr(tokenizers, "Tokenizer", FakeTokenizer)
    root = tmp_path / "models" / "intent" / "router"
    root.mkdir(parents=True)
    for name in R.BUNDLE_FILES:
        payload = json.dumps(CONFIG) if name == "router.json" else f"fake {name}"
        (root / name).write_text(payload, encoding="utf-8")
    pin = {"run": "test-run", "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                        for name in R.BUNDLE_FILES}}
    pin_file = tmp_path / "intent_router.pin.json"
    pin_file.write_text(json.dumps(pin), encoding="utf-8")
    R.reset_router_cache()
    yield SimpleNamespace(root=root, pin=pin_file, env={R.PIN_ENV: str(pin_file), R.BUNDLE_ENV: str(root)})
    R.reset_router_cache()


def test_training_and_brain_share_one_input_template():
    sys.path.insert(0, str(REPO / "scripts" / "intent_router"))
    import dataset

    assert dataset.router_input is R.router_input
    assert R.router_input("now turn that into a pdf", "FREEFORM_CHAT: explained moon phases") == (
        "[PREVIOUS ASSISTANT ACTION] FREEFORM_CHAT: explained moon phases [CURRENT USER MESSAGE] now turn that into a pdf")
    assert R.router_input("  hi  ") == "[CURRENT USER MESSAGE] hi"


def test_decide_accepts_only_confident_known_wide_margin_routes():
    thresholds = np.array([0.9, 0.95, 0.95, 0.95])
    primary = np.array([[0.01, 0.97, 0.01, 0.01],   # confident code
                        [0.38, 0.34, 0.24, 0.04],   # the operator's example: must escalate
                        [0.50, 0.49, 0.005, 0.005], # below threshold and margin
                        [0.97, 0.01, 0.01, 0.01]])  # confident but out of domain
    certainty = np.array([[0.97, 0.02, 0.01], [0.9, 0.09, 0.01], [0.9, 0.09, 0.01], [0.05, 0.1, 0.85]])
    assert R.decide(primary, certainty, thresholds, 0.3, 0.6).tolist() == [1, R.ESCALATE, R.ESCALATE, R.ESCALATE]


def test_router_routes_confident_messages_and_escalates_the_rest(bundle):
    router = R.get_router(env=bundle.env)

    code = router.route("run the tests in my repo")
    assert (code.route, code.reason, code.escalated) == ("CODING_AGENT", "accepted", False)
    assert code.intents == ("CODING_AGENT",)

    split = router.route("click it")
    assert split.escalated and split.reason == "below_threshold"

    noise = router.route("asdfgh")
    assert noise.escalated and noise.reason == "certainty_out_of_domain"

    mixed = router.route("look up cuda and update my doc")
    assert mixed.route == "FREEFORM_CHAT" and mixed.intents == ("FREEFORM_CHAT", "DOCUMENT_CREATION")


def test_parallel_pipelines_share_one_session_without_blocking(bundle):
    results: list[str | None] = []
    lock = threading.Lock()

    def worker():
        router = R.get_router(env=bundle.env)
        decision = router.route("run the tests in my repo")
        with lock:
            results.append(decision.route)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["CODING_AGENT"] * 16
    assert len(FakeSession.instances) == 1 and FakeSession.instances[0].providers == ["CPUExecutionProvider"]


def test_a_tampered_bundle_file_fails_closed(bundle):
    (bundle.root / "router.onnx").write_text("swapped weights", encoding="utf-8")
    with pytest.raises(R.IntentRouterArtifactError, match="router.onnx: sha256"):
        R.get_router(env=bundle.env)


def test_missing_pin_or_bundle_is_unavailable(bundle, tmp_path, monkeypatch):
    with pytest.raises(R.IntentRouterUnavailable, match="no promoted intent router"):
        R.get_router(env={R.PIN_ENV: str(tmp_path / "absent.json"), R.BUNDLE_ENV: str(bundle.root)})
    # The runtime models root is always a candidate. Point it at an empty folder: the check used
    # to skip itself whenever the seat had a promoted bundle installed.
    import core.paths

    monkeypatch.setattr(core.paths, "RUNTIME_MODELS_ROOT", tmp_path / "no-runtime-models")
    with pytest.raises(R.IntentRouterUnavailable, match="no intent router bundle found"):
        R.select_bundle({R.MODELS_DIR_ENV: str(tmp_path / "empty")})


def test_a_config_that_disagrees_with_the_contract_is_refused(bundle):
    bad = dict(CONFIG, routes=list(reversed(R.ROUTES)))
    (bundle.root / "router.json").write_text(json.dumps(bad), encoding="utf-8")
    pin = json.loads(bundle.pin.read_text(encoding="utf-8"))
    pin["files"]["router.json"] = hashlib.sha256((bundle.root / "router.json").read_bytes()).hexdigest()
    bundle.pin.write_text(json.dumps(pin), encoding="utf-8")
    with pytest.raises(R.IntentRouterArtifactError, match="route or certainty order"):
        R.get_router(env=bundle.env)
