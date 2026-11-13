"""Review follow-up (I2) — over-broad synonyms must widen, not confidently misfile.

The honest-signals doctrine prefers widening (None → recoverable _unsorted) over a
confident wrong subdomain. Three synonyms added during vocab expansion were broad
enough to capture unrelated free-text into a real subdomain; they are removed.
"""

from __future__ import annotations

from auto_loop.mril_tools.subdomain_helpers import subdomain_for


def test_calm_under_load_is_not_sentiment():
    # "calm" as a system-state adjective must not route to situational sentiment
    assert subdomain_for("keep the system calm under heavy load", 4) is None


def test_software_package_is_not_an_artifact():
    # "package" as a dependency noun must not route to dev artifacts
    assert subdomain_for("the requests package is already installed", 11) is None


def test_bundle_assets_is_not_an_artifact():
    assert subdomain_for("bundle the assets together for the page", 11) is None


# ── REGRESSION: the specific, intended synonyms still route ───────────────
def test_deliverable_still_routes_to_artifacts():
    assert subdomain_for("the build generated a deliverable package", 11) == 0


def test_emotion_words_still_route_to_sentiment():
    assert subdomain_for("debugging left me annoyed and drained", 4) == 7
