import os

from core.paths import OMEGA_ROOT
from vastai.utils import VERSION

_APIKEY_SENTINEL = object()


def _resolve_api_key(api_key: object) -> str:
    """
    Resolves the API key from (in order):
      1. Explicit argument
      2. VAST_API_KEY environment variable
      3. The SSD-owned Omega state file:
         guardian/state/vastai/config/vast_api_key
    """
    if api_key is not _APIKEY_SENTINEL:
        return api_key

    env = os.getenv("VAST_API_KEY")
    if env:
        return env

    xdg_path = OMEGA_ROOT / "guardian" / "state" / "vastai" / "config" / "vast_api_key"
    if xdg_path.exists():
        return xdg_path.read_text(encoding="utf-8").strip()

    raise RuntimeError(
        f"No API key found. Pass api_key=, set VAST_API_KEY, or save your key to {xdg_path}"
    )


class _BaseClient:
    def __init__(
        self, api_key: object = _APIKEY_SENTINEL, vast_server: str = "https://console.vast.ai"
    ):
        self._api_key = _resolve_api_key(api_key)
        self._vast_server = vast_server.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "User-Agent": f"vastai-sdk/{VERSION}"}

    def _url(self, path: str) -> str:
        return f"{self._vast_server}{path}"
