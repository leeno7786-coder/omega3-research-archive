from __future__ import annotations

import socket

from schema.tools import web_fetch as wf


_PUBLIC_ADDRINFO = [
    (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("93.184.216.34", 443),
    )
]


class _Response:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: bytes = b"<html>public article</html>",
        headers: dict[str, str] | None = None,
        url: str = "https://example.com/article",
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.url = url
        self.closed = False

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size: int = 8192):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise wf.requests.HTTPError(f"status={self.status_code}")

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.call_kwargs: list[dict] = []
        self.mounts: list[tuple[str, object]] = []
        self.trust_env = True

    def mount(self, prefix: str, adapter: object) -> None:
        self.mounts.append((prefix, adapter))

    def get(self, url: str, **kwargs):
        self.calls.append(url)
        self.call_kwargs.append(kwargs)
        return self.responses.pop(0)

    def close(self) -> None:
        return None


def _fail_legacy_get(*_args, **_kwargs):
    raise AssertionError("direct fetch must use the validated streaming session")


def test_direct_fetch_rejects_loopback_before_network(monkeypatch):
    monkeypatch.setattr(
        wf.requests,
        "get",
        lambda *_a, **_k: _Response(url="http://127.0.0.1/private"),
    )

    result = wf.web_fetch("http://127.0.0.1/private")

    assert result["results"] == []


def test_web_fetch_rejects_non_http_url_schemes():
    result = wf.web_fetch("file:///etc/passwd")

    assert result["results"] == []
    assert "only http" in result["error"]


def test_direct_fetch_preserves_public_text_retrieval(monkeypatch):
    session = _Session([_Response()])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: _PUBLIC_ADDRINFO)
    monkeypatch.setattr(wf.requests, "Session", lambda: session)
    monkeypatch.setattr(wf.requests, "get", _fail_legacy_get)

    result = wf.web_fetch("https://example.com/article")

    assert result["results"] == [
        {
            "url": "https://example.com/article",
            "title": "https://example.com/article",
            "content": "public article",
            "source": "direct",
        }
    ]
    assert session.trust_env is False

    assert session.calls == ["https://93.184.216.34/article"]
    assert session.call_kwargs[0]["headers"]["Host"] == "example.com"
    assert session.mounts and session.mounts[0][0] == "https://"
    pool_kwargs = session.mounts[0][1].poolmanager.connection_pool_kw
    assert pool_kwargs["server_hostname"] == "example.com"
    assert pool_kwargs["assert_hostname"] == "example.com"


def test_direct_fetch_revalidates_redirect_targets(monkeypatch):
    session = _Session(
        [
            _Response(
                status_code=302,
                headers={
                    "Content-Type": "text/html",
                    "Location": "http://127.0.0.1/admin",
                },
                url="https://example.com/start",
            )
        ]
    )
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: _PUBLIC_ADDRINFO)
    monkeypatch.setattr(wf.requests, "Session", lambda: session)
    monkeypatch.setattr(wf.requests, "get", lambda *_a, **_k: session.responses[0])

    result = wf.web_fetch("https://example.com/start")

    assert result["results"] == []
    assert session.calls == ["https://93.184.216.34/start"]


def test_direct_fetch_rejects_decompressed_body_over_transport_cap(monkeypatch):
    limit = getattr(wf, "_MAX_RESPONSE_BYTES", 256 * 1024)
    oversized = _Response(body=b"x" * (limit + 1))
    session = _Session([oversized])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: _PUBLIC_ADDRINFO)
    monkeypatch.setattr(wf.requests, "Session", lambda: session)
    monkeypatch.setattr(wf.requests, "get", lambda *_a, **_k: oversized)

    result = wf.web_fetch("https://example.com/oversized")

    assert result["results"] == []
    assert oversized.closed is True


def test_a_failed_direct_fetch_is_an_error_not_an_empty_success(monkeypatch):
    """A dead URL must not share the 'ran, found nothing' return shape."""

    def _boom(url: str) -> dict:
        raise ConnectionError("timed out connecting to example.invalid")

    monkeypatch.setattr(wf, "_safe_direct_fetch", _boom)
    result = wf.web_fetch("https://example.invalid/offline")

    assert result["results"] == []
    assert result["total_results"] == 0
    assert result.get("error"), result
    assert "timed out" in str(result["error"])
