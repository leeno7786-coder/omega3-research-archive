"""Brain-owned CPU face recognition using SCRFD + ArcFace ONNX models.

The face pipeline is deliberately separate from DME. It uses only
``CPUExecutionProvider`` and keeps the persistent identity data in the brain's
``AnchorStore``. The runtime service is not required for face inference.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from core.paths import RUNTIME_MODELS_ROOT
from runtime.face.anchor_store import get_anchor_store

log = logging.getLogger("omega.runtime.face.recognizer")

_MODEL_FILES = {
    "detector": "det_10g.onnx",
    "embedding": "w600k_r50.onnx",
    "landmarks_2d": "2d106det.onnx",
    "landmarks_3d": "1k3d68.onnx",
    "age_gender": "genderage.onnx",
}
_MODEL_IDS = {
    "detector": "face_det_10g",
    "embedding": "face_w600k_r50",
    "landmarks_2d": "face_2d106det",
    "landmarks_3d": "face_1k3d68",
    "age_gender": "face_genderage",
}
_DET_INPUT_SIZE = 640
_DET_STRIDES = (8, 16, 32)
_DET_NUM_ANCHORS = 2
_DET_SCORE_THRESH = 0.45
_DET_NMS_IOU = 0.4
_ARCFACE_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


class FaceModelUnavailable(RuntimeError):
    """The local face model bundle cannot be loaded."""


def face_model_root() -> Path:
    """Return the face asset directory shipped inside the portable product."""
    return RUNTIME_MODELS_ROOT / "onnxs" / "face"


class FaceRecognizer:
    """Lazy, thread-safe CPU ONNX face detector and ArcFace embedder."""

    provider = "CPUExecutionProvider"

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else face_model_root()
        self._lock = threading.RLock()
        self._det_session: Any = None
        self._rec_session: Any = None
        self._det_input_name = ""
        self._rec_input_name = ""

    @property
    def loaded(self) -> bool:
        return self._det_session is not None and self._rec_session is not None

    def _model_path(self, role: str) -> Path:
        path = (self.root / _MODEL_FILES[role]).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise FaceModelUnavailable(f"face model path escaped root: {role}") from exc
        if not path.is_file():
            raise FaceModelUnavailable(f"face model missing: {path}")
        return path

    def _load_session(self, role: str) -> tuple[Any, str]:
        try:
            import onnxruntime as ort
        except Exception as exc:  # pragma: no cover - dependency contract
            raise FaceModelUnavailable("onnxruntime is unavailable") from exc
        if self.provider not in ort.get_available_providers():
            raise FaceModelUnavailable(f"{self.provider} is unavailable")
        path = self._model_path(role)
        try:
            session = ort.InferenceSession(str(path), providers=[self.provider])
        except Exception as exc:
            raise FaceModelUnavailable(f"face model failed to load: {path.name}") from exc
        inputs = session.get_inputs()
        if not inputs:
            raise FaceModelUnavailable(f"face model has no input: {path.name}")
        return session, str(inputs[0].name)

    def load(self) -> dict[str, Any]:
        """Load detector and embedding sessions, leaving auxiliary models lazy."""
        self._ensure_sessions()
        return self.status()

    def unload(self) -> None:
        """Release CPU session references so the next request reloads cleanly."""
        with self._lock:
            self._det_session = None
            self._rec_session = None
            self._det_input_name = ""
            self._rec_input_name = ""

    def _ensure_sessions(self) -> None:
        if self.loaded:
            return
        with self._lock:
            if self._det_session is None:
                self._det_session, self._det_input_name = self._load_session("detector")
            if self._rec_session is None:
                try:
                    self._rec_session, self._rec_input_name = self._load_session("embedding")
                except Exception:
                    self._det_session = None
                    self._det_input_name = ""
                    raise

    def status(self) -> dict[str, Any]:
        missing = [
            role for role in ("detector", "embedding")
            if not (self.root / _MODEL_FILES[role]).is_file()
        ]
        return {
            "ok": not missing,
            "face_ready": not missing,
            "models_loaded": self.loaded,
            "lazy_load": True,
            "provider": self.provider,
            "runtime": "brain-local-cpu",
            "runtime_reachable": True,
            "model_root": str(self.root),
            "models": [
                {"id": _MODEL_IDS[role], "role": role, "loaded": self.loaded}
                for role in _MODEL_FILES
            ],
            "missing": missing,
            "known_names": get_anchor_store().names(),
        }

    @staticmethod
    def _decode(image_bytes: bytes) -> np.ndarray:
        import cv2

        raw = np.frombuffer(image_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError("image decode failed")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def detect(self, image_bytes: bytes) -> list[dict[str, Any]]:
        self._ensure_sessions()
        return self._detect_array(self._decode(image_bytes))

    def _detect_array(self, rgb: np.ndarray) -> list[dict[str, Any]]:
        self._ensure_sessions()
        import cv2

        height, width = rgb.shape[:2]
        scale = min(_DET_INPUT_SIZE / width, _DET_INPUT_SIZE / height)
        new_width = max(1, int(width * scale))
        new_height = max(1, int(height * scale))
        resized = cv2.resize(rgb, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((_DET_INPUT_SIZE, _DET_INPUT_SIZE, 3), dtype=np.uint8)
        canvas[:new_height, :new_width] = resized
        array = canvas.astype(np.float32)
        array = (array[:, :, ::-1] - 127.5) / 128.0
        array = array.transpose(2, 0, 1)[None, ...]

        outputs = self._det_session.run(None, {self._det_input_name: array})
        scores = outputs[0:3]
        boxes = outputs[3:6]
        keypoints = outputs[6:9]
        faces: list[dict[str, Any]] = []
        for stride, score_blob, box_blob, keypoint_blob in zip(
            _DET_STRIDES, scores, boxes, keypoints
        ):
            grid = _DET_INPUT_SIZE // stride
            centers = _make_anchor_centers(grid, stride, _DET_NUM_ANCHORS)
            flat_scores = np.asarray(score_blob).reshape(-1)
            keep = flat_scores >= _DET_SCORE_THRESH
            if not keep.any():
                continue
            selected_centers = centers[keep]
            selected_scores = flat_scores[keep]
            selected_boxes = np.asarray(box_blob).reshape(-1, 4)[keep]
            selected_keypoints = np.asarray(keypoint_blob).reshape(-1, 5, 2)[keep]
            x1 = selected_centers[:, 0] - selected_boxes[:, 0] * stride
            y1 = selected_centers[:, 1] - selected_boxes[:, 1] * stride
            x2 = selected_centers[:, 0] + selected_boxes[:, 2] * stride
            y2 = selected_centers[:, 1] + selected_boxes[:, 3] * stride
            selected_keypoints = selected_keypoints * stride
            selected_keypoints[:, :, 0] += selected_centers[:, 0:1]
            selected_keypoints[:, :, 1] += selected_centers[:, 1:2]
            for index, score in enumerate(selected_scores):
                faces.append(
                    {
                        "bbox": [
                            float(x1[index]),
                            float(y1[index]),
                            float(x2[index]),
                            float(y2[index]),
                        ],
                        "score": float(score),
                        "kps": selected_keypoints[index].tolist(),
                    }
                )
        if not faces:
            return []
        faces = _nms(faces, _DET_NMS_IOU)
        for face in faces:
            face["bbox"] = [float(value / scale) for value in face["bbox"]]
            face["kps"] = [[float(x / scale), float(y / scale)] for x, y in face["kps"]]
        faces.sort(
            key=lambda face: (face["bbox"][2] - face["bbox"][0])
            * (face["bbox"][3] - face["bbox"][1]),
            reverse=True,
        )
        return faces

    def embed(self, rgb: np.ndarray, keypoints: list[list[float]]) -> np.ndarray:
        self._ensure_sessions()
        import cv2

        transform = _similarity_transform(np.asarray(keypoints, dtype=np.float32), _ARCFACE_TEMPLATE)
        warped = cv2.warpAffine(rgb, transform, (112, 112), flags=cv2.INTER_LINEAR)
        array = warped[:, :, ::-1].astype(np.float32)
        array = ((array - 127.5) / 127.5).transpose(2, 0, 1)[None, ...]
        embedding = self._rec_session.run(None, {self._rec_input_name: array})[0][0]
        embedding = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if embedding.shape[0] != 512:
            raise ValueError(f"ArcFace embedding must be 512-dim, got {embedding.shape[0]}")
        return embedding / (np.linalg.norm(embedding) + 1e-9)

    def identify(self, image_bytes: bytes) -> dict[str, Any]:
        try:
            rgb = self._decode(image_bytes)
            faces = self._detect_array(rgb)
        except FaceModelUnavailable:
            raise
        except Exception as exc:
            return {
                "ok": False,
                "degraded": False,
                "error": f"face preprocessing failed: {exc}",
                "face_count": 0,
                "recognized_name": None,
            }
        if not faces:
            return {
                "ok": True,
                "degraded": False,
                "face_count": 0,
                "recognized_name": None,
                "confidence": 0.0,
                "reason": "no_face_detected",
            }
        face = faces[0]
        try:
            embedding = self.embed(rgb, face["kps"])
        except Exception as exc:
            return {
                "ok": False,
                "degraded": False,
                "error": f"face embedding failed: {exc}",
                "face_count": len(faces),
                "recognized_name": None,
            }
        match = get_anchor_store().identify(embedding)
        return {
            "ok": True,
            "degraded": False,
            "face_count": len(faces),
            "bbox": face["bbox"],
            "score": face["score"],
            "embedding": embedding.tolist(),
            "recognized_name": match["recognized_name"],
            "name": match["recognized_name"],
            "confidence": match["confidence"],
            "candidates": match["candidates"],
            "threshold": match.get("threshold"),
        }


def _make_anchor_centers(grid: int, stride: int, count: int) -> np.ndarray:
    ys, xs = np.mgrid[0:grid, 0:grid]
    centers = np.stack([xs.reshape(-1), ys.reshape(-1)], axis=-1).astype(np.float32) * stride
    return np.repeat(centers, count, axis=0)


def _nms(faces: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    ordered = sorted(faces, key=lambda face: face["score"], reverse=True)
    kept: list[dict[str, Any]] = []
    while ordered:
        current = ordered.pop(0)
        kept.append(current)
        ordered = [
            face for face in ordered if _iou(current["bbox"], face["bbox"]) < threshold
        ]
    return kept


def _iou(first: list[float], second: list[float]) -> float:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def _similarity_transform(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    if source.shape != target.shape or source.shape != (5, 2):
        raise ValueError("face alignment requires five 2D keypoints")
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    covariance = (target_centered.T @ source_centered) / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    direction = np.ones(2, dtype=np.float32)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        direction[-1] = -1
    rotation = u @ np.diag(direction) @ vt
    variance = float((source_centered**2).sum() / len(source))
    scale = float((singular * direction).sum() / max(variance, 1e-12))
    translation = target_mean - scale * (rotation @ source_mean)
    transform = np.zeros((2, 3), dtype=np.float32)
    transform[:, :2] = scale * rotation
    transform[:, 2] = translation
    return transform


_singleton: Optional[FaceRecognizer] = None
_singleton_lock = threading.Lock()


def get_recognizer() -> FaceRecognizer:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = FaceRecognizer()
    return _singleton


def reset_recognizer_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.unload()
        _singleton = None
