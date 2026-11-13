"""
test_face_recognizer.py — FACE-1 verification (brain-owned CPU architecture)

Face inference (detect/recognize/embed) is brain-owned CPU ONNX Runtime.
The DME NPU route is separate and is never selected by this pipeline. This
suite verifies the parts that are live + offline-testable:

    - AnchorStore: enroll, identify, forget, names, threshold logic
      (brain-side persistent data layer — real, offline)
    - Brain-local face status exposes CPU provider and model residency
    - FACE-1 roundtrip: enroll a synthetic 512-d embedding through the
      AnchorStore and identify it back
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import logging

log = logging.getLogger("omega.tests.hud.test_face_recognizer")


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_PASSED = 0
_FAILED = 0
_FAILURES: list[str] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  PASS  {name}")
    else:
        _FAILED += 1
        msg = f"  FAIL  {name}" + (f" -- {detail}" if detail else "")
        _FAILURES.append(msg)
        print(msg)


def _synthetic_face_bytes() -> bytes:
    """A doodle PNG used to exercise the local no-face contract."""
    img = Image.new("RGB", (640, 480), (96, 96, 96))
    d = ImageDraw.Draw(img)
    d.ellipse([220, 140, 420, 340], fill=(220, 180, 150))
    d.ellipse([265, 200, 295, 230], fill=(20, 20, 20))
    d.ellipse([350, 200, 380, 230], fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    print("=" * 60)
    print("test_face_recognizer -- FACE-1 (brain-local CPU arch)")
    print("=" * 60)
    print()

    # ============================================================
    # AnchorStore — pure unit tests with a temp file (live, offline)
    # ============================================================
    from runtime.face.anchor_store import AnchorStore, RECOGNITION_THRESHOLD

    with tempfile.TemporaryDirectory() as td:
        store = AnchorStore(path=Path(td) / "anchors.json")

        emb = np.random.RandomState(7).randn(512).astype(np.float32)
        result = store.identify(emb)
        _check(
            "AS.empty store -> no_anchors",
            result["recognized_name"] is None and result["reason"] == "no_anchors",
        )

        rng = np.random.RandomState(42)
        noah_emb = rng.randn(512).astype(np.float32)
        entry = store.enroll("noah", noah_emb)
        _check("AS.enroll returns entry with sample_count=1", entry["sample_count"] == 1)
        _check("AS.names lists 'noah'", store.names() == ["noah"])

        result = store.identify(noah_emb)
        _check(
            "AS.same embedding -> recognized as noah",
            result["recognized_name"] == "noah" and result["confidence"] > 0.99,
            detail=str(result),
        )

        other_emb = np.random.RandomState(99).randn(512).astype(np.float32)
        result = store.identify(other_emb)
        _check(
            "AS.different embedding -> not recognized",
            result["recognized_name"] is None and result["confidence"] < RECOGNITION_THRESHOLD,
            detail=f"sim={result['confidence']:.3f}",
        )

        noah_v2 = noah_emb + rng.randn(512).astype(np.float32) * 0.05
        entry = store.enroll("noah", noah_v2)
        _check("AS.re-enroll bumps sample_count to 2", entry["sample_count"] == 2)

        ok = store.forget("noah")
        _check("AS.forget returns True for known name", ok)
        _check("AS.names empty after forget", store.names() == [])
        _check("AS.forget unknown returns False", store.forget("ghost") is False)

        try:
            store.enroll("", noah_emb)
            raised = False
        except ValueError:
            raised = True
        _check("AS.enroll empty name raises", raised)
        try:
            store.enroll("x", np.zeros(64))
            raised = False
        except ValueError:
            raised = True
        _check("AS.enroll wrong-dim raises", raised)

    # ============================================================
    # Face recognition API — brain-local CPU contract
    # ============================================================
    # enroll() and the AnchorStore roundtrip below write the face anchor store; the live one
    # is the operator's real guardian/state/face_anchors.json (found 2026-09-15).
    import runtime.face.anchor_store as anchor_store_module

    anchor_store_module._singleton = anchor_store_module.AnchorStore(
        Path(tempfile.mkdtemp(prefix="omega-face-anchors-")) / "face_anchors.json"
    )
    from runtime.face import recognition as face_rec

    for fn in ("identify", "enroll", "known_names", "forget", "face_status"):
        _check(f"face_rec.{fn} callable", callable(getattr(face_rec, fn, None)))

    status = face_rec.face_status()
    _check(
        "face_status reports brain-local CPU + anchor_count",
        status.get("runtime") == "brain-local-cpu"
        and status.get("provider") == "CPUExecutionProvider"
        and "anchor_count" in status,
        detail=str(status),
    )
    _check("known_names returns list", isinstance(face_rec.known_names(), list))

    res = face_rec.identify(_synthetic_face_bytes())
    _check(
        "identify returns an honest local result",
        isinstance(res, dict)
        and res.get("degraded") is False
        and res.get("name") is None,
        detail=str(res),
    )
    res = face_rec.enroll(_synthetic_face_bytes(), "x")
    _check(
        "enroll -> {ok:<bool>, name:'x'}",
        isinstance(res, dict) and isinstance(res.get("ok"), bool) and res.get("name") == "x",
        detail=str(res),
    )

    # ============================================================
    # FACE-1 roundtrip — pure AnchorStore persistence contract
    # (synthetic deterministic 512-d embedding; AnchorStore contract)
    # ============================================================
    from runtime.face.anchor_store import get_anchor_store

    store = get_anchor_store()
    store.forget("test_operator")
    synth = np.random.RandomState(123).randn(512).astype(np.float32)
    res = store.enroll("test_operator", synth)
    _check("FACE-1 roundtrip: enroll succeeds", res["sample_count"] >= 1)
    match = store.identify(synth)
    _check(
        "FACE-1 roundtrip: identify same embedding -> recognized",
        match["recognized_name"] == "test_operator",
        detail=str(match),
    )
    store.forget("test_operator")

    print()
    print("=" * 60)
    print(f"  PASSED: {_PASSED}")
    print(f"  FAILED: {_FAILED}")
    if _FAILURES:
        print()
        print("FAILURES:")
        for f in _FAILURES:
            print(f)
    print("=" * 60)
    return 0 if _FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
