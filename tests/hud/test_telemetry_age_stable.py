"""HUD live-feed stabilization: relative-age fields must not churn the
panel hash every poll.

Panels hash their telemetry slice to decide whether to re-render. The
sub-minute relative-age helpers used to return a per-SECOND value
("5s ago" -> "6s ago" / "5S" -> "6S"), so a recent timestamp changed the
hash on every 2s poll and forced a destructive card rebuild. These tests
pin the coarsened, stable sub-minute bucket so cards stop resetting.
"""

from datetime import datetime, timezone, timedelta

from hud.telemetry import _relative_age, _age_short


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_relative_age_sub_minute_is_stable_not_per_second():
    # Two timestamps 1s and 30s old both fall in the sub-minute bucket
    # and MUST render identically (no per-second churn).
    assert _relative_age(_iso(1)) == _relative_age(_iso(30)) == "just now"


def test_age_short_sub_minute_is_stable_not_per_second():
    assert _age_short(_iso(1)) == _age_short(_iso(30)) == "now"


def test_minute_and_hour_buckets_still_format():
    assert _relative_age(_iso(120)) == "2m ago"
    assert _relative_age(_iso(7200)) == "2h ago"
    assert _age_short(_iso(120)) == "2M"
    assert _age_short(_iso(7200)) == "2H"


def test_empty_and_bad_input_safe():
    assert _relative_age("") == "—"
    assert _age_short("") == "—"
    assert _relative_age("not-a-date") == "—"
    assert _age_short("not-a-date") == "—"


def test_no_per_second_token_in_sub_minute_output():
    # Guard against regression to a per-second format like "Ns ago" / "NS".
    out = _relative_age(_iso(5))
    assert "s ago" not in out, f"sub-minute relative-age churns per second: {out!r}"
    out_short = _age_short(_iso(5))
    assert not out_short.endswith("S"), f"sub-minute age_short churns per second: {out_short!r}"
