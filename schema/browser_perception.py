"""Bounded browser evidence; image pixels belong only to the live attachment."""
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def bounded_text(value: Any, name: str, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be non-empty text of at most {limit} characters")
    return value


def strict_keys(value: Any, keys: set[str], name: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{name} requires exactly {sorted(keys)}")
    return value


def public_url(value: str) -> str:
    """Exclude embedded authentication, query tokens and fragment secrets."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "about"}:
            return ""
        host = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:2000]
    except ValueError:
        return ""


@dataclass(frozen=True)
class BrowserPerceptionPack:
    url: str = ""
    title: str = ""
    session_id: str = ""
    locked_window: str = ""
    ocr_text: str = ""
    # The page's readable text as the browser renders it (document.body.innerText),
    # read through DevTools; exact where OCR would guess.
    page_text: str = ""
    text_status: str = "unavailable"
    has_screenshot: bool = False
    screenshot_hash: str = ""
    status: str = "unavailable"
    ocr_status: str = "unavailable"
    error: str = ""
    ocr_error: str = ""

    def __post_init__(self):
        for name, limit in (("url", 2000), ("title", 500), ("session_id", 200),
                            ("locked_window", 200), ("ocr_text", 4000), ("page_text", 6000),
                            ("error", 200), ("ocr_error", 200), ("screenshot_hash", 64)):
            value = getattr(self, name)
            if type(value) is not str or len(value) > limit:
                raise ValueError(f"invalid browser perception {name}")
        # no_task_tab: the session is live but Omega owns no tab yet, so nothing was captured.
        # busy: another run is driving the window right now, so this episode does not look.
        if self.status not in {"success", "unavailable", "no_task_tab", "busy"}:
            raise ValueError("invalid perception status")
        if self.ocr_status not in {"success", "failed", "unavailable", "no_screenshot"}:
            raise ValueError("invalid OCR status")
        if self.text_status not in {"success", "unavailable"}:
            raise ValueError("invalid page text status")
        if self.text_status == "success" and not self.page_text:
            raise ValueError("page text success requires text")
        if type(self.has_screenshot) is not bool:
            raise ValueError("has_screenshot must be boolean")
        if self.has_screenshot != bool(self.screenshot_hash):
            raise ValueError("screenshot requires a hash")
        if self.screenshot_hash and (len(self.screenshot_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.screenshot_hash
        )):
            raise ValueError("invalid screenshot hash")
        if self.status != "success" and self.has_screenshot:
            raise ValueError("unavailable perception cannot carry pixels")
        if self.status == "no_task_tab" and not self.session_id:
            raise ValueError("no_task_tab perception names the live session")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid browser perception fields")
        return cls(**value)
