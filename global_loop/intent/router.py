"""Global A's intent router: ModernBERT-base with primary, multi-intent and certainty heads.

Trained dev-side on the ROCm GPU (``scripts/intent_router/train.py``) and shipped as a
hash-pinned ONNX bundle the brain runs on CPU with onnxruntime and tokenizers. The router
routes only when it is sure. A message whose calibrated certainty is not ``known``, whose
top route is below that route's threshold, or whose lead over the runner-up is below the
margin is escalated to Global A's reasoning model instead. Explicit ``/skill`` and plugin
selections never reach it.

One resident ONNX Runtime session serves every physical pipeline. ``InferenceSession.run``
is safe to call concurrently, so parallel episodes never queue behind one another, and the
router shares no seat with the Ettin retina.

``router_input`` and ``decide`` are the single definition of the input template and the
accept-or-escalate rule; training imports them from here so the two cannot drift.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

ROUTES = ("FREEFORM_CHAT", "CODING_AGENT", "BROWSER_AGENT", "DOCUMENT_CREATION")
CERTAINTY = ("known", "ambiguous", "out_of_domain")
ESCALATE = len(ROUTES)
BUNDLE_FILES = ("router.onnx", "router.json", "tokenizer.json", "tokenizer_config.json",
                "special_tokens_map.json")
PIN_FILENAME = "intent_router.pin.json"
BUNDLE_ENV = "OMEGA_INTENT_ROUTER_DIR"
MODELS_DIR_ENV = "OMEGA_MODELS_DIR"
PIN_ENV = "OMEGA_INTENT_ROUTER_PIN"
_OUTPUTS = ("primary_logits", "intent_logits", "certainty_logits")
_READ_CHUNK = 1 << 20


class IntentRouterUnavailable(RuntimeError):
    """No promoted router pin, or no bundle anywhere the seat knows to look."""


class IntentRouterArtifactError(RuntimeError):
    """A bundle or its configuration is not the pinned, promoted router."""


# ── the shared contract ───────────────────────────────────────────────────────


def router_input(text: str, context: str = "") -> str:
    """The exact string the encoder sees, in training and in the brain."""
    message = f"[CURRENT USER MESSAGE] {str(text or '').strip()}"
    previous = str(context or "").strip()
    return f"[PREVIOUS ASSISTANT ACTION] {previous} {message}" if previous else message


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


def sigmoid(logits: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-logits))


def decide(primary_probs: np.ndarray, certainty_probs: np.ndarray, thresholds: np.ndarray,
           margin: float, known_threshold: float) -> np.ndarray:
    """Route index per row, or ``ESCALATE`` when the router is not sure enough to decide."""
    order = np.argsort(-primary_probs, axis=1)
    index = np.arange(len(primary_probs))
    top1 = order[:, 0]
    p1 = primary_probs[index, top1]
    p2 = primary_probs[index, order[:, 1]]
    accept = ((certainty_probs[:, CERTAINTY.index("known")] >= known_threshold)
              & (p1 >= thresholds[top1]) & ((p1 - p2) >= margin))
    return np.where(accept, top1, ESCALATE)


@dataclass(frozen=True)
class RouteDecision:
    route: str | None
    reason: str
    confidence: float
    margin: float
    primary: Mapping[str, float]
    intents: tuple[str, ...]
    intent_scores: Mapping[str, float]
    certainty: Mapping[str, float]

    @property
    def escalated(self) -> bool:
        return self.route is None


# ── the pinned bundle ─────────────────────────────────────────────────────────


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def pin_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    if source.get(PIN_ENV):
        return Path(source[PIN_ENV])
    from core.paths import CONFIG_ROOT

    return CONFIG_ROOT / PIN_FILENAME


def load_pin(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise IntentRouterUnavailable(f"no promoted intent router: {path} does not exist")
    pin = json.loads(path.read_text(encoding="utf-8"))
    files = pin.get("files") if isinstance(pin, dict) else None
    if not isinstance(files, dict) or set(files) != set(BUNDLE_FILES) or not all(
            isinstance(value, str) and len(value) == 64 for value in files.values()):
        raise IntentRouterArtifactError(f"{path} does not pin exactly {list(BUNDLE_FILES)} by sha256")
    return pin


def candidate_bundles(env: Mapping[str, str] | None = None) -> list[Path]:
    """Every place the seat looks, in priority order; touches no filesystem."""
    source = os.environ if env is None else env
    candidates: list[Path] = []
    if source.get(BUNDLE_ENV):
        candidates.append(Path(source[BUNDLE_ENV]))
    if source.get(MODELS_DIR_ENV):
        candidates.append(Path(source[MODELS_DIR_ENV]) / "intent" / "router")
    from core.paths import RUNTIME_MODELS_ROOT

    candidates.append(RUNTIME_MODELS_ROOT / "intent" / "router")
    return list(dict.fromkeys(candidates))


def verify_bundle(bundle: Path, pin: Mapping[str, Any]) -> None:
    faults: list[str] = []
    for name in BUNDLE_FILES:
        path = Path(bundle) / name
        if not path.is_file():
            faults.append(f"{name}: missing")
        elif _digest(path) != pin["files"][name]:
            faults.append(f"{name}: sha256 does not match the promoted pin")
    if faults:
        raise IntentRouterArtifactError(f"intent router bundle {bundle} is not the promoted router "
                                        f"{pin.get('run', '?')}:\n  " + "\n  ".join(faults))


def select_bundle(env: Mapping[str, str] | None = None) -> Path:
    """An explicitly named bundle is used verbatim; otherwise the first complete candidate."""
    source = os.environ if env is None else env
    candidates = candidate_bundles(source)
    if source.get(BUNDLE_ENV):
        return candidates[0]
    for candidate in candidates:
        if all((candidate / name).is_file() for name in BUNDLE_FILES):
            return candidate
    raise IntentRouterUnavailable("no intent router bundle found. Looked in:\n  "
                                  + "\n  ".join(str(candidate) for candidate in candidates))


# ── the router ────────────────────────────────────────────────────────────────


class IntentRouter:
    """A verified bundle loaded once: tokenizer, CPU ONNX session, calibrated decision rule."""

    def __init__(self, bundle: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.bundle = Path(bundle)
        config = json.loads((self.bundle / "router.json").read_text(encoding="utf-8"))
        if tuple(config.get("routes") or ()) != ROUTES or tuple(config.get("certainty") or ()) != CERTAINTY:
            raise IntentRouterArtifactError("router.json route or certainty order differs from the brain's contract")
        template = config.get("input_template") or {}
        probe = ("probe text", "CODING_AGENT: probe action")
        if (template.get("with_context", "").format(context=probe[1], text=probe[0]) != router_input(*probe)
                or template.get("without_context", "").format(text=probe[0]) != router_input(probe[0])):
            raise IntentRouterArtifactError("router.json input template differs from router_input")
        self.max_length = int(config["max_length"])
        temperatures = config["temperatures"]
        self._t_primary = float(temperatures["primary_temperature"])
        self._t_intent = float(temperatures["intent_temperature"])
        self._t_certainty = float(temperatures["certainty_temperature"])
        self._thresholds = np.array([float(config["thresholds"][route]) for route in ROUTES])
        self._margin = float(config["margin"])
        self._known_threshold = float(config["known_threshold"])
        self._intent_thresholds = np.array([float(config["intent_thresholds"][route]) for route in ROUTES])

        tokenizer = Tokenizer.from_file(str(self.bundle / "tokenizer.json"))
        tokenizer.no_padding()
        tokenizer.enable_truncation(max_length=self.max_length)
        self._tokenizer = tokenizer
        self._session = ort.InferenceSession(str(self.bundle / "router.onnx"),
                                             providers=["CPUExecutionProvider"])

    def logits(self, text: str, context: str = "") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        encoding = self._tokenizer.encode(router_input(text, context))
        feeds = {"input_ids": np.array([encoding.ids], dtype=np.int64),
                 "attention_mask": np.array([encoding.attention_mask], dtype=np.int64)}
        primary, intents, certainty = self._session.run(list(_OUTPUTS), feeds)
        return (np.asarray(primary, dtype=np.float64), np.asarray(intents, dtype=np.float64),
                np.asarray(certainty, dtype=np.float64))

    def route(self, text: str, context: str = "") -> RouteDecision:
        primary_logits, intent_logits, certainty_logits = self.logits(text, context)
        primary = softmax(primary_logits / self._t_primary)
        certainty = softmax(certainty_logits / self._t_certainty)
        intents = sigmoid(intent_logits / self._t_intent)[0]
        choice = int(decide(primary, certainty, self._thresholds, self._margin, self._known_threshold)[0])
        order = np.argsort(-primary[0])
        p1, p2 = float(primary[0, order[0]]), float(primary[0, order[1]])
        if certainty[0, CERTAINTY.index("known")] < self._known_threshold:
            reason = f"certainty_{CERTAINTY[int(certainty[0].argmax())]}"
        elif p1 < self._thresholds[order[0]]:
            reason = "below_threshold"
        elif p1 - p2 < self._margin:
            reason = "below_margin"
        else:
            reason = "accepted"
        return RouteDecision(
            route=None if choice == ESCALATE else ROUTES[choice],
            reason="accepted" if choice != ESCALATE else (reason if reason != "accepted" else "escalated"),
            confidence=p1,
            margin=p1 - p2,
            primary={route: float(primary[0, i]) for i, route in enumerate(ROUTES)},
            intents=tuple(route for i, route in enumerate(ROUTES) if intents[i] >= self._intent_thresholds[i]),
            intent_scores={route: float(intents[i]) for i, route in enumerate(ROUTES)},
            certainty={label: float(certainty[0, i]) for i, label in enumerate(CERTAINTY)},
        )


# ── the seat ──────────────────────────────────────────────────────────────────

_SEAT: dict[tuple[str, str], IntentRouter] = {}
_SEAT_LOCK = threading.Lock()


def get_router(*, env: Mapping[str, str] | None = None) -> IntentRouter:
    """Resolve, verify and load the promoted router once per process; reuse it everywhere."""
    source = os.environ if env is None else env
    pin = load_pin(pin_path(source))
    bundle = select_bundle(source)
    key = (str(bundle), json.dumps(pin["files"], sort_keys=True))
    router = _SEAT.get(key)
    if router is not None:
        return router
    with _SEAT_LOCK:
        router = _SEAT.get(key)
        if router is None:
            verify_bundle(bundle, pin)
            router = IntentRouter(bundle)
            _SEAT[key] = router
    return router


def reset_router_cache() -> None:
    with _SEAT_LOCK:
        _SEAT.clear()


__all__ = [
    "BUNDLE_FILES", "CERTAINTY", "ESCALATE", "IntentRouter", "IntentRouterArtifactError",
    "IntentRouterUnavailable", "PIN_FILENAME", "ROUTES", "RouteDecision", "candidate_bundles",
    "decide", "get_router", "load_pin", "pin_path", "reset_router_cache", "router_input",
    "select_bundle", "sigmoid", "softmax", "verify_bundle",
]
