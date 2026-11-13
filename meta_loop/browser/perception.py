"""Meta A's read-only DevTools/OCR phase. Never executes browser actions."""
import base64
import hashlib
from dataclasses import dataclass

from browser_agent.controller import BrowserControllerError
from schema.browser_perception import BrowserPerceptionPack, public_url


@dataclass(frozen=True)
class BrowserPerceptionAttachment:
    """Live pixels, deliberately outside episode dataclass/serializable slots.

    AUTO consumes this attachment for its multimodal call; it is not memory.
    The evidence hash binds it to the serialized perception receipt.
    """
    screenshot: bytes
    screenshot_hash: str


def _controller():
    from auto_loop.execute.brain_skill_executor import _browser_controller

    return _browser_controller()


def read_ocr(image: bytes) -> str:
    """OCR over the screenshot, for text the page renders only as pixels (canvas, images).

    The readable text of the page itself comes from the DOM (``page_text``); this is the
    optional second channel and needs the rapidocr-onnxruntime engine installed in the brain
    environment. Without it OCR stays explicitly unavailable: no reasoning model stands in for
    it (no_inference_in_meta) and nothing is invented.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise NotImplementedError("ocr_tool is unavailable: rapidocr-onnxruntime is not installed") from exc
    import numpy
    from io import BytesIO
    from PIL import Image

    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()
    with Image.open(BytesIO(image)) as source:
        pixels = numpy.asarray(source.convert("RGB"))
    result, _elapsed = _OCR_ENGINE(pixels)
    return "\n".join(str(row[1]) for row in (result or ()) if len(row) > 1 and str(row[1]).strip())


_OCR_ENGINE = None


def _ensure_attached(controller) -> None:
    """Meta A must see a live page before AUTO acts, so the look secures the session first.

    Attaching connects to Omega's debugger-enabled Edge, setting it up when missing exactly
    as the first browser primitive does. A session that died with Edge or the driver is
    replaced, and a task tab the operator closed releases its lock. None of it performs a
    page action; without it a closed tab or restarted Edge left every later look failing.
    """
    if getattr(controller, "session_id", None) and controller.ensure_task_tab_alive() != "session_lost":
        return
    from browser_agent.lifecycle import HUD_DEBUGGER_ADDRESS

    controller.attach(HUD_DEBUGGER_ADDRESS, auto_setup=True)


def capture_browser_perception(controller, *, ocr=None):
    """Return factual evidence plus ephemeral pixels; failures stay explicit."""
    from browser_agent.agent_loop import is_running

    if is_running():
        # Another episode's agent run is driving the window (an operator note arriving
        # mid-run, say). Looking now would interleave with its actions; AUTO delivers the
        # note to that run instead.
        return BrowserPerceptionPack(status="busy"), None
    try:
        _ensure_attached(controller)
        if getattr(controller, "locked_window", None) is None:
            # Omega owns no task tab yet, so no page of this workflow exists to look at. The
            # current tab is whatever the operator has in front of them (usually the HUD); it
            # is never captured or sent to a model. AUTO opens Omega's own tab first.
            return BrowserPerceptionPack(session_id=str(controller.session_id or "")[:200],
                                         status="no_task_tab"), None
        observed = controller.read_snapshot()
        encoded = observed.get("screenshot") or ""
        if len(encoded) > 16_000_000:
            raise ValueError("screenshot exceeds perception limit")
        pixels = base64.b64decode(encoded, validate=True) if encoded else b""
    except (BrowserControllerError, OSError, ValueError) as exc:
        # Error codes/types preserve attribution without copying possible page secrets.
        code = getattr(exc, "code", type(exc).__name__)
        return BrowserPerceptionPack(error=str(code)[:200]), None
    digest = hashlib.sha256(pixels).hexdigest() if pixels else ""
    page_text = " ".join(str(observed.get("page_text") or "").split())[:6000]
    text, ocr_status, ocr_error = "", "no_screenshot", ""
    if pixels:
        try:
            text = (ocr or read_ocr)(pixels)
            if type(text) is not str:
                raise ValueError("OCR must return text")
            text = text[:4000]
            ocr_status = "success"
        except Exception as exc:
            ocr_status = "unavailable" if isinstance(exc, NotImplementedError) else "failed"
            ocr_error = type(exc).__name__
            text = ""
    pack = BrowserPerceptionPack(
        url=public_url(str(observed.get("url") or "")),
        title=str(observed.get("title") or "")[:500],
        session_id=str(observed.get("session_id") or "")[:200],
        locked_window=str(observed.get("locked_window") or "")[:200],
        ocr_text=text, page_text=page_text, text_status="success" if page_text else "unavailable",
        has_screenshot=bool(pixels), screenshot_hash=digest,
        status="success", ocr_status=ocr_status, ocr_error=ocr_error)
    return pack, BrowserPerceptionAttachment(pixels, digest) if pixels else None


def run(state, episode, *, controller=None, ocr=None):
    if state.intent != "BROWSE":
        return state
    pack, attachment = capture_browser_perception(
        controller if controller is not None else _controller(), ocr=ocr)
    state.execution_inputs["browser_perception"] = pack.to_dict()
    # Not a dataclass field: asdict/archive/journal serializers cannot persist it.
    episode._browser_perception_attachment = attachment
    return state
