"""Weaving a captured tool surface must reproduce what was actually signed.

The capture tool exists because a provider like Slack answers ``tools/list``
with 401 until a token exists, so a package cannot ship signed capability
authority until someone has authorized once and read the real surface back.

That makes the weave step the thing standing between a provider's raw answer
and an authority Omega's own key vouches for. It is pinned here against the
three Google packages already signed and shipped: same schema digests, same
risk classes, same effect for each risk. If the derivation drifts, a future
capture would sign a surface that no longer matches the reviewed one.

The Gmail overrides are the point of the overrides file. Four of its tools were
classified by a human against the provider's own hints, in both directions, and
no annotation could have produced those calls.
"""

from __future__ import annotations

import importlib.util
import json
from hashlib import sha256
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.trust import load_embedded_key_ring

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "capture_provider_tools", ROOT / "scripts" / "capture_provider_tools.py"
)
capture_tool = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(capture_tool)

# The reviewed calls the shipped Gmail package carries that its provider's
# annotations do not imply. Reproduced here so the test proves the overrides
# file is what closes the gap, not a looser derivation.
GMAIL_OVERRIDES = {
    "create_draft": "send_publish",
    "unlabel_thread": "mutating_write",
    "unlabel_message": "mutating_write",
    "update_message_labels": "destructive",
}

PROVIDERS = ("calendar", "drive", "gmail")


def _capture(provider: str) -> dict:
    return json.loads(
        (ROOT / f"tests/fixtures/google_mcp/{provider}_discovery.json").read_bytes()
    )


def _shipped_app(provider: str) -> dict:
    path = (
        ROOT
        / "plugin_platform/marketplace/packages"
        / f"google.workspace.{provider}"
        / "tools/authority.json"
    )
    return next(iter(json.loads(path.read_bytes())["apps"].values()))


@pytest.mark.parametrize("provider", PROVIDERS)
def test_schema_digests_match_the_signed_package(provider: str) -> None:
    """A digest that drifts would sign a contract the provider never offered."""
    app = _shipped_app(provider)

    for tool in _capture(provider)["tools"]:
        policy = app["tools"][tool["name"]]
        assert capture_tool._digest(tool["inputSchema"]) == policy["input_schema_sha256"]
        assert capture_tool._digest(tool["outputSchema"]) == policy["output_schema_sha256"]
        assert policy["input_schema_sha256"] == sha256(
            canonical_bytes(tool["inputSchema"])
        ).hexdigest()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_risk_classes_match_once_the_reviewed_overrides_are_applied(provider: str) -> None:
    app = _shipped_app(provider)
    overrides = GMAIL_OVERRIDES if provider == "gmail" else {}

    for tool in _capture(provider)["tools"]:
        name = tool["name"]
        proposed = capture_tool._propose_risk(tool.get("annotations") or {})
        risk = overrides.get(name, proposed)
        assert risk == app["tools"][name]["risk_class"], name
        assert capture_tool._EFFECT_FOR_RISK[risk] == app["tools"][name]["effect"], name


def test_the_gmail_overrides_are_genuinely_not_derivable() -> None:
    """Guards the reason the overrides file exists.

    If a future derivation happened to produce these four on its own, the
    override entries would become dead weight that hides a real behaviour
    change. This fails when that day comes, so the file gets revisited.
    """
    proposals = {
        tool["name"]: capture_tool._propose_risk(tool.get("annotations") or {})
        for tool in _capture("gmail")["tools"]
    }

    for name, reviewed in GMAIL_OVERRIDES.items():
        assert proposals[name] != reviewed, name


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_shipped_schema_is_plannable(provider: str) -> None:
    """A tool the planner refuses is one a connected plugin can never call."""
    for tool in _capture(provider)["tools"]:
        assert capture_tool._plannable(tool["inputSchema"], tool["outputSchema"]), tool["name"]


@pytest.mark.parametrize(
    ("package", "legacy"),
    [
        ("google.workspace.calendar", True),
        ("google.workspace.drive", True),
        ("google.workspace.gmail", True),
        ("slack.official", True),
        ("github.official", False),
    ],
)
def test_the_legacy_handshake_is_declared_only_on_an_older_provider(
    package: str, legacy: bool
) -> None:
    """Declaring it against a current server asserts a handshake nobody asked for.

    GitHub speaks Omega's own wire version and its signed package carries no
    compatibility block at all. The others answer an older version and carry one.
    """
    path = (
        ROOT / "plugin_platform/marketplace/packages" / package / "tools/authority.json"
    )
    app = next(iter(json.loads(path.read_bytes())["apps"].values()))

    assert ("runtime_compatibility" in app) is legacy
    if legacy:
        assert app["runtime_compatibility"]["handshake_mode"] == "legacy"


@pytest.mark.parametrize(
    ("package", "capture_path"),
    [
        ("google.workspace.calendar", "tests/fixtures/google_mcp/calendar_discovery.json"),
        ("slack.official", "tests/fixtures/slack_mcp/slack_discovery.json"),
    ],
)
def test_the_handshake_version_is_the_one_the_server_actually_speaks(
    package: str, capture_path: str
) -> None:
    """`wire_protocol_version` is an observation, and it took two failures to settle.

    The verifier first pinned it to LATEST_HANDSHAKE_VERSION, so writing what a
    provider answered was an `asset_invalid` at signing time -- and Google's
    endpoint answers exactly that constant, which hid the disagreement. Then
    Slack connected and `_discover_receipt` compared the LIVE negotiated version
    against this field and refused with protocol_mismatch, because the constant
    is not what Slack speaks.

    Both cannot be right. The field is the wire's drift check, so it names this
    server's version; the verifier now admits any handshake-era one.
    """
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

    path = (
        ROOT / "plugin_platform/marketplace/packages" / package / "tools/authority.json"
    )
    app = next(iter(json.loads(path.read_bytes())["apps"].values()))
    observed = json.loads((ROOT / capture_path).read_bytes())["protocol_version"]

    assert app["runtime_compatibility"]["wire_protocol_version"] == observed
    assert observed in HANDSHAKE_PROTOCOL_VERSIONS


def test_the_slack_package_matches_its_captured_surface() -> None:
    """Slack is the first package built entirely by this tool, end to end."""
    package = ROOT / "plugin_platform/marketplace/packages/slack.official"
    verified = verify_plugin_package(package, load_embedded_key_ring(), "3.0.0")
    app = verified.manifest.required_apps[0]
    capture = json.loads((ROOT / "tests/fixtures/slack_mcp/slack_discovery.json").read_bytes())
    authority = json.loads(verified.asset_bytes["tools/authority.json"])["apps"][app.app_id]

    assert capture["endpoint"] == app.remote_origin
    observed = {tool["name"]: tool for tool in capture["tools"]}
    assert set(authority["tools"]) == set(observed) == {cap.name for cap in app.capabilities}

    for capability in app.capabilities:
        tool = observed[capability.name]
        policy = authority["tools"][capability.name]
        assert policy["input_schema"] == tool["inputSchema"], capability.name
        assert capability.schema_sha256 == sha256(
            canonical_bytes(tool["inputSchema"])
        ).hexdigest()
        # Slack declares no result shapes at all, so every policy carries the
        # open object rather than a contract the provider never stated.
        assert "outputSchema" not in tool
        assert policy["output_schema"] == capture_tool.OPEN_RESULT_SCHEMA


def test_sending_a_slack_message_is_not_filed_as_an_ordinary_write() -> None:
    """Slack's hints call a send an ordinary write. Reaching people is not.

    `slack_send_message` posts into a channel or DM, `slack_schedule_message`
    does the same on a timer, and a draft lands in the user's Slack ready to
    go. All three carry only `readOnlyHint: false` with no destructive or
    idempotent hint, which derives `additive_write` -- the same class as
    creating a private record. The risk class is what decides whether Omega
    may act without asking, so all three are pinned to `send_publish`.
    """
    package = ROOT / "plugin_platform/marketplace/packages/slack.official"
    verified = verify_plugin_package(package, load_embedded_key_ring(), "3.0.0")
    app = verified.manifest.required_apps[0]
    authority = json.loads(verified.asset_bytes["tools/authority.json"])["apps"][app.app_id]
    capture = json.loads((ROOT / "tests/fixtures/slack_mcp/slack_discovery.json").read_bytes())
    hints = {tool["name"]: (tool.get("annotations") or {}) for tool in capture["tools"]}

    for name in ("slack_send_message", "slack_schedule_message", "slack_send_message_draft"):
        assert authority["tools"][name]["risk_class"] == "send_publish", name
        assert authority["tools"][name]["effect"] == "send", name
        assert capture_tool._propose_risk(hints[name]) == "additive_write", name

    for name in ("slack_read_channel", "slack_read_thread", "slack_search_public",
                 "slack_search_channels"):
        assert authority["tools"][name]["risk_class"] == "read_only", name


def test_weaving_a_capture_reproduces_the_shipped_package_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole path, on a package whose signed answer is already known.

    Strips Calendar back to the state Slack is in -- no ``tools/authority.json``
    and an empty ``capabilities`` list -- then rebuilds it from the captured
    discovery document and the reviewed titles. Anything the weave gets wrong
    shows up as a difference against bytes the operator's key already signed.
    """
    source = ROOT / "plugin_platform/marketplace/packages/google.workspace.calendar"
    shipped_authority = json.loads((source / "tools/authority.json").read_bytes())
    shipped_manifest = json.loads((source / "plugin.json").read_bytes())
    shipped_app = next(iter(shipped_authority["apps"].values()))

    package = tmp_path / "google.workspace.calendar"
    package.mkdir(parents=True)
    stripped = json.loads(json.dumps(shipped_manifest))
    stripped["required_apps"][0]["capabilities"] = []
    (package / "plugin.json").write_bytes(json.dumps(stripped).encode())
    monkeypatch.setattr(capture_tool, "PACKAGES", tmp_path)

    overrides = tmp_path / "overrides.json"
    overrides.write_bytes(
        json.dumps(
            {
                name: {"title": policy["title"], "risk_class": policy["risk_class"]}
                for name, policy in shipped_app["tools"].items()
            }
        ).encode()
    )

    assert (
        capture_tool.weave(
            "google.workspace.calendar",
            ROOT / "tests/fixtures/google_mcp/calendar_discovery.json",
            overrides,
        )
        == 0
    )
    capsys.readouterr()

    rebuilt = json.loads((package / "tools/authority.json").read_bytes())
    assert rebuilt == shipped_authority

    manifest = json.loads((package / "plugin.json").read_bytes())
    assert manifest["required_apps"][0]["capabilities"] == (
        shipped_manifest["required_apps"][0]["capabilities"]
    )
    # The stale signature stays: a manifest is an exact-fields document and
    # dropping the pair makes it unparseable by the signing script. It fails
    # closed anyway, because the bytes it covers have changed.
    assert manifest["signature"] == shipped_manifest["signature"]


def test_a_stale_override_is_refused_rather_than_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override naming a retired tool means the review is out of date.

    Ignoring it would leave whichever tool replaced it classified by the
    provider's hints alone, with nobody aware the review had lapsed.
    """
    source = ROOT / "plugin_platform/marketplace/packages/google.workspace.calendar"
    package = tmp_path / "google.workspace.calendar"
    package.mkdir(parents=True)
    (package / "plugin.json").write_bytes((source / "plugin.json").read_bytes())
    monkeypatch.setattr(capture_tool, "PACKAGES", tmp_path)

    overrides = tmp_path / "overrides.json"
    overrides.write_bytes(json.dumps({"tool_that_retired": {"risk_class": "read_only"}}).encode())

    with pytest.raises(SystemExit, match="tool_that_retired"):
        capture_tool.weave(
            "google.workspace.calendar",
            ROOT / "tests/fixtures/google_mcp/calendar_discovery.json",
            overrides,
        )


def test_effect_is_never_an_independent_choice() -> None:
    """`client_contracts` rejects an authority whose effect and risk disagree."""
    from mcp_platform.client_contracts import UNREVIEWED_EFFECT
    from mcp_platform.package_contracts import RiskClass

    reviewable = {risk for risk in RiskClass if risk is not RiskClass.EXTERNAL_UNREVIEWED}
    assert {RiskClass(name) for name in capture_tool._EFFECT_FOR_RISK} == reviewable
    assert UNREVIEWED_EFFECT not in capture_tool._EFFECT_FOR_RISK.values()


def test_re_weaving_keeps_the_packages_own_review_without_an_overrides_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A maintainer changing nothing must not widen what a provider may do.

    `weave` used to take every risk class and title from the provider's
    annotation hints unless an overrides file said otherwise. Re-capturing Gmail
    on 2026-09-07 -- for a release whose entire content was one sentence Google
    added to a description -- therefore rewrote all 23 titles and moved four
    risk classes onto the provider's proposal, including `create_draft` from
    `send_publish` down to `additive_write`. A risk class decides what Omega may
    do without asking again, so that is a signed release quietly granting a
    provider more than the review gave it.

    The package's existing authority is now the default. Nothing is passed here:
    the point is that the reviewed answer survives on its own.
    """
    source = ROOT / "plugin_platform/marketplace/packages/google.workspace.gmail"
    shipped_authority = json.loads((source / "tools/authority.json").read_bytes())
    shipped_app = next(iter(shipped_authority["apps"].values()))

    package = tmp_path / "google.workspace.gmail"
    (package / "tools").mkdir(parents=True)
    (package / "plugin.json").write_bytes((source / "plugin.json").read_bytes())
    (package / "tools/authority.json").write_bytes((source / "tools/authority.json").read_bytes())
    monkeypatch.setattr(capture_tool, "PACKAGES", tmp_path)

    assert (
        capture_tool.weave(
            "google.workspace.gmail",
            ROOT / "tests/fixtures/google_mcp/gmail_discovery.json",
            None,
        )
        == 0
    )
    printed = capsys.readouterr().out

    rebuilt = next(iter(json.loads((package / "tools/authority.json").read_bytes())["apps"].values()))
    assert rebuilt == shipped_app
    for name, reviewed in GMAIL_OVERRIDES.items():
        assert rebuilt["tools"][name]["risk_class"] == reviewed, name

    # Keeping the reviewed value is the safe default and would otherwise hide a
    # provider that has begun proposing something else, so the disagreement is
    # reported. These four are exactly the ones the overrides file exists for.
    assert "the package's review was kept" in printed
    for name in GMAIL_OVERRIDES:
        assert name in printed


def test_an_overrides_entry_still_beats_the_packages_standing_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keeping a review by default must not make one impossible to change."""
    source = ROOT / "plugin_platform/marketplace/packages/google.workspace.gmail"
    package = tmp_path / "google.workspace.gmail"
    (package / "tools").mkdir(parents=True)
    (package / "plugin.json").write_bytes((source / "plugin.json").read_bytes())
    (package / "tools/authority.json").write_bytes((source / "tools/authority.json").read_bytes())
    monkeypatch.setattr(capture_tool, "PACKAGES", tmp_path)

    overrides = tmp_path / "overrides.json"
    overrides.write_bytes(
        json.dumps({"create_draft": {"risk_class": "destructive", "title": "Reviewed again"}}).encode()
    )

    assert (
        capture_tool.weave(
            "google.workspace.gmail",
            ROOT / "tests/fixtures/google_mcp/gmail_discovery.json",
            overrides,
        )
        == 0
    )
    capsys.readouterr()

    rebuilt = next(iter(json.loads((package / "tools/authority.json").read_bytes())["apps"].values()))
    assert rebuilt["tools"]["create_draft"]["risk_class"] == "destructive"
    assert rebuilt["tools"]["create_draft"]["title"] == "Reviewed again"
    assert (
        rebuilt["tools"]["create_draft"]["effect"]
        == capture_tool._EFFECT_FOR_RISK["destructive"]
    )
    # Every tool the override does not name still holds its reviewed value.
    for name, reviewed in GMAIL_OVERRIDES.items():
        if name != "create_draft":
            assert rebuilt["tools"][name]["risk_class"] == reviewed, name
