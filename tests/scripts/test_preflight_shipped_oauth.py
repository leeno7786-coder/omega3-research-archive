"""Pure helpers of scripts/preflight_shipped_oauth.py -- no network."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "preflight_shipped_oauth", ROOT / "scripts" / "preflight_shipped_oauth.py"
)
assert _spec is not None and _spec.loader is not None
preflight = importlib.util.module_from_spec(_spec)
sys.modules["preflight_shipped_oauth"] = preflight
_spec.loader.exec_module(preflight)


def test_scope_check_agrees_regardless_of_order() -> None:
    assert preflight.scope_check(("write", "read"), "read write") == "ok"


def test_scope_check_names_both_sides_of_a_disagreement() -> None:
    result = preflight.scope_check(("read", "admin"), "read write")
    assert result.startswith("FAIL")
    assert "['admin']" in result and "['write']" in result


def test_scope_check_rejects_an_empty_registration_scope() -> None:
    assert preflight.scope_check(("read",), "") == "FAIL registration.scope is empty"


def test_scope_check_is_not_applicable_without_a_registration() -> None:
    assert preflight.scope_check(("repo",), None) == "n/a"


def test_redirect_to_vendor_login_passes() -> None:
    passed, detail = preflight.classify_authorize(
        302, "https://huggingface.co/login?next=%2Foauth%2Fauthorize", ""
    )
    assert passed and "huggingface.co/login" in detail


def test_redirect_carrying_error_fails_with_the_provider_error() -> None:
    passed, detail = preflight.classify_authorize(
        302,
        "https://plugins.omega2ai.com/oauth/x/callback?error=invalid_scope"
        "&error_description=scope+mcp+is+not+allowed",
        "",
    )
    assert not passed
    assert "error=invalid_scope" in detail and "scope mcp is not allowed" in detail


def test_consent_page_passes_and_error_page_fails() -> None:
    ok, _ = preflight.classify_authorize(200, None, "<title>Authorize Omega</title>")
    assert ok
    bad, detail = preflight.classify_authorize(
        200, None, "<title>Error</title><p>The redirect_uri is not registered</p>"
    )
    assert not bad and "redirect_uri" in detail
    code, detail = preflight.classify_authorize(400, None, '{"error":"invalid_client"}')
    assert not code and "invalid_client" in detail


def test_pkce_challenge_is_s256_of_the_verifier() -> None:
    verifier, challenge = preflight.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()


def test_a_generic_200_error_page_is_not_a_pass() -> None:
    """A 200 is only a PASS with evidence of a login or consent page; an error page with no
    OAuth error code is not one, and a page with no evidence either way is unconfirmed."""
    error, detail = preflight.classify_authorize(
        200, None, "<title>Error</title><p>Application not found</p>"
    )
    assert not error and "error page" in detail
    unknown, detail = preflight.classify_authorize(200, None, "<title></title><div id=root></div>")
    assert not unknown and "unconfirmed" in detail


def test_a_200_with_sign_in_evidence_passes() -> None:
    ok, _ = preflight.classify_authorize(
        200, None, "<title>Slack</title><form><input type=email name=email>Sign in to Slack</form>"
    )
    assert ok


def test_a_redirect_to_a_generic_error_path_is_not_a_pass() -> None:
    passed, detail = preflight.classify_authorize(302, "https://vendor.example/oauth/error?id=7", "")
    assert not passed and "error" in detail


def _fetcher(pages):
    visited = []

    def fetch(url):
        visited.append(url)
        return pages[url]

    return fetch, visited


def test_a_redirect_chain_passes_only_on_login_evidence_at_its_end() -> None:
    fetch, visited = _fetcher({
        "https://idp.example/authorize?x=1": (302, "/login?next=1", ""),
        "https://idp.example/login?next=1": (200, None, "<title>Sign in</title><input type=password>"),
    })
    passed, detail = preflight.follow_authorize("https://idp.example/authorize?x=1", fetch)
    assert passed and visited[-1] == "https://idp.example/login?next=1"


def test_a_redirect_to_a_page_without_consent_evidence_is_not_a_pass() -> None:
    fetch, _ = _fetcher({
        "https://idp.example/authorize": (302, "https://idp.example/maintenance", ""),
        "https://idp.example/maintenance": (200, None, "<title>We'll be right back</title>"),
    })
    passed, detail = preflight.follow_authorize("https://idp.example/authorize", fetch)
    assert not passed and "unconfirmed" in detail


def test_a_redirect_loop_is_not_a_pass() -> None:
    fetch, _ = _fetcher({
        "https://idp.example/a": (302, "https://idp.example/b", ""),
        "https://idp.example/b": (302, "https://idp.example/a", ""),
    })
    passed, detail = preflight.follow_authorize("https://idp.example/a", fetch)
    assert not passed and "redirect" in detail


def test_an_error_anywhere_in_the_chain_fails() -> None:
    fetch, _ = _fetcher({
        "https://idp.example/authorize": (302, "https://idp.example/cb?error=invalid_scope", ""),
    })
    passed, detail = preflight.follow_authorize("https://idp.example/authorize", fetch)
    assert not passed and "invalid_scope" in detail
