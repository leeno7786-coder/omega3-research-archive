"""Operator-provided learning SOURCE URL — the exact page Omega should fetch.

Operator directive 2026-07-22: "make any url i give it plausible, it searched
for the top ten but got stuck using wikipedia." When the operator hands Omega a
URL — in a directive, or answering Omega's own ASK for a source — that URL IS the
authoritative fetch target. Stashed here (one-shot, process-local) and consumed
by the LEARN topic resolver, which returns the URL as the topic so web_fetch
DIRECT-FETCHES it (web_fetch handles URL-form queries) instead of fuzzy-resolving
the topic phrase to a Wikipedia article via opensearch.

The stash carries an optional domain hint; when empty the resolver keeps whatever
mastery domain the normal cascade resolved (the topic the URL informs), so
mastery crediting is unchanged — only the fetch target is overridden.

One pending URL at a time: a fresh set replaces a stale one; the next LEARN that
resolves a topic consumes it (mirrors pressure_loop.learn_topic_choice).
"""

from __future__ import annotations

import re
from typing import Optional

# Permissive http(s) matcher: scheme + everything up to whitespace or a quote/
# angle bracket. Trailing sentence punctuation a human types AFTER a URL — the
# closing paren/bracket/brace, comma, period, etc. — is trimmed so
# "(see https://x.org/a)." resolves to the bare URL.
_URL_RX = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRAILING_TRIM = ")]}>.,;:!?'\""

_pending: Optional[dict] = None


def extract_first_url(text: str) -> str:
    """Return the first http(s) URL in text (trailing punctuation trimmed), or
    '' when the text carries none."""
    match = _URL_RX.search(str(text or ""))
    if not match:
        return ""
    return match.group(0).rstrip(_TRAILING_TRIM)


def set_source_url(url: str, domain: str = "") -> None:
    """Record an operator-provided source URL. No-op on a value that is not an
    http(s) URL, so a bare topic phrase never poses as a fetch target."""
    global _pending
    u = str(url or "").strip()
    low = u.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return
    _pending = {"url": u, "domain": str(domain or "").strip()}


def capture_from_text(text: str, domain: str = "") -> bool:
    """Extract the first URL from operator text and stash it. Returns True when a
    URL was captured; a no-op returning False when the text carries none."""
    url = extract_first_url(text)
    if not url:
        return False
    set_source_url(url, domain)
    return True


def peek_source_url() -> Optional[dict]:
    return dict(_pending) if _pending else None


def take_source_url() -> Optional[dict]:
    """Return and clear the pending URL (one-shot)."""
    global _pending
    value, _pending = _pending, None
    return value


def _reset_for_tests() -> None:
    global _pending
    _pending = None
