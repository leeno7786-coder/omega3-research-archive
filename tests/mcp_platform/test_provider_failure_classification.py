"""An opaque provider failure must still name a cause an operator can act on.

Google Calendar's MCP endpoint answered 403 to tools/list immediately after a
successful sign-in, and the log said only "reason=unclassified", which tells an
operator nothing. The envelope named its canonical status all along. That status
is a closed enum, so it can be logged under the same rule the message and the
details metadata are refused by: nothing provider-authored reaches the log.
"""

from __future__ import annotations

import logging
import re

import httpx2
import pytest

from mcp_platform.transports.streamable_http import _log_resource_http_failure


def _response(body: dict[str, object], status_code: int = 403) -> httpx2.Response:
    request = httpx2.Request(
        "POST",
        "https://apps.example.com/mcp",
        headers={"Authorization": "Bearer secret-header"},
        json={"method": "tools/list"},
    )
    return httpx2.Response(status_code, json=body, request=request)


def test_a_canonical_status_is_named_when_no_details_reason_is_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        _log_resource_http_failure(
            _response({"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "no"}})
        )

    assert "reason=PERMISSION_DENIED" in caplog.text
    assert "unclassified" not in caplog.text


def test_a_details_reason_still_wins_over_the_broader_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The specific cause is more useful than the class it belongs to."""
    body = {
        "error": {
            "status": "PERMISSION_DENIED",
            "message": "secret-provider-message",
            "details": [{"reason": "SERVICE_DISABLED"}],
        }
    }

    with caplog.at_level(logging.WARNING):
        _log_resource_http_failure(_response(body))

    assert "reason=SERVICE_DISABLED" in caplog.text
    assert "secret-provider-message" not in caplog.text


@pytest.mark.parametrize(
    "status",
    ["not a status", "permission_denied", "SOMETHING_INVENTED", 7, None, "A", "X" * 200],
)
def test_only_the_canonical_enum_is_admitted(
    status: object, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches provider prose reaching the log through the status field."""
    with caplog.at_level(logging.WARNING):
        _log_resource_http_failure(_response({"error": {"status": status, "message": "secret"}}))

    # Read the field itself rather than searching the whole line: a one-letter
    # status would match inside "WARNING" and prove nothing.
    reported = re.search(r"reason=(\S+)", caplog.text)
    assert reported is not None and reported.group(1) == "unclassified"
    assert "secret" not in caplog.text


def test_body_capture_is_off_unless_the_operator_asks(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The log stays free of provider text; the body goes to its own file, opt in."""
    monkeypatch.setattr("core.paths.OMEGA_ROOT", tmp_path)
    body = {"error": {"status": "PERMISSION_DENIED", "message": "secret-provider-message"}}

    monkeypatch.delenv("OMEGA_MCP_CAPTURE_FAILURES", raising=False)
    with caplog.at_level(logging.WARNING):
        _log_resource_http_failure(_response(body))
    assert "body_captured" not in caplog.text
    assert not list(tmp_path.glob("guardian/logs/mcp-failure-*.json"))

    caplog.clear()
    monkeypatch.setenv("OMEGA_MCP_CAPTURE_FAILURES", "1")
    with caplog.at_level(logging.WARNING):
        _log_resource_http_failure(_response(body))

    captured = list(tmp_path.glob("guardian/logs/mcp-failure-*.json"))
    assert len(captured) == 1
    assert "secret-provider-message" in captured[0].read_text(encoding="utf-8")
    # the body reaches the file, never the log, and the credential reaches neither
    assert "secret-provider-message" not in caplog.text
    assert "body_captured" in caplog.text
    assert "secret-header" not in captured[0].read_text(encoding="utf-8")


def test_the_challenge_header_names_the_failure_when_the_body_cannot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Google answers an auth failure with a challenge AND a successful body.

    Catches a classifier that only reads the body: the body says the request
    succeeded, so the reason lives in the RFC 6750 challenge or nowhere.
    """
    request = httpx2.Request(
        "POST",
        "https://calendarmcp.googleapis.com/mcp/v1",
        headers={"Authorization": "Bearer secret-header"},
        json={"method": "tools/list"},
    )
    response = httpx2.Response(
        403,
        headers={
            "WWW-Authenticate": (
                'Bearer realm="https://accounts.google.com/", error="insufficient_scope", '
                'scope="https://www.googleapis.com/auth/calendar"'
            )
        },
        json={"id": 3, "jsonrpc": "2.0", "result": {"tools": []}},
        request=request,
    )

    with caplog.at_level(logging.WARNING):
        _log_resource_http_failure(response)

    assert "reason=insufficient_scope" in caplog.text
    assert "secret-header" not in caplog.text


@pytest.mark.parametrize(
    "challenge",
    [
        'Bearer error="something_invented"',
        "Basic realm=x",
        'Bearer error="INVALID_TOKEN"',
        "",
    ],
)
def test_only_the_registered_bearer_errors_are_admitted(
    challenge: str, caplog: pytest.LogCaptureFixture
) -> None:
    request = httpx2.Request("POST", "https://apps.example.com/mcp", json={"method": "tools/list"})
    response = httpx2.Response(
        403, headers={"WWW-Authenticate": challenge}, json={"result": {}}, request=request
    )

    with caplog.at_level(logging.WARNING):
        _log_resource_http_failure(response)

    reported = re.search(r"reason=(\S+)", caplog.text)
    assert reported is not None and reported.group(1) == "unclassified"
