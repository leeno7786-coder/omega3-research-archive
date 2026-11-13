"""Capture a connected provider's real tool surface, then weave it into a package.

A package ships the provider's tool contracts as SIGNED capability authority --
Omega will not open a connection to a surface its own key has not vouched for.
That authority has to come from the provider itself, and providers such as Slack
answer ``tools/list`` with 401 until a token exists. So the order is forced:
authorize once, capture with that token, weave, sign, release.

Three subcommands, deliberately split so the network step never touches the
marketplace signing key:

``scaffold``
    Write every unsigned artefact a new provider needs -- manifest, skill,
    curated listing, provenance record, review promotion, catalog entry -- from
    one reviewed spec. Six documents have to agree before a package can be
    signed, and a disagreement surfaces as an opaque signing failure naming
    none of them. Offline, no key access. ``--template`` prints a spec.

``capture``
    Read the operator's own stored token out of the OAuth vault, run the MCP
    handshake against the provider's endpoint and write the discovery document
    (the same shape as ``tests/fixtures/google_mcp/*_discovery.json``).
    Needs OMEGA_SEAL_PASSPHRASE. No signing key access.

``weave``
    Turn a captured discovery document into ``tools/authority.json`` plus the
    matching ``capabilities`` block in ``plugin.json``. Offline, deterministic,
    no key access. Sign afterwards with ``scripts/sign_marketplace_catalog.py``.

The access token is used and discarded. It is never written to a file, a log or
this script's output.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_platform.canonical import canonical_bytes  # noqa: E402
from plugin_platform.marketplace_provenance import (  # noqa: E402
    scaffold_provenance_record,
)
from mcp_platform.provider_capture import (  # noqa: E402
    ProviderCaptureError,
    capture_provider_surface,
)

PACKAGES = ROOT / "plugin_platform" / "marketplace" / "packages"
CAPTURES = ROOT / "tests" / "fixtures"

# Result policy defaults. Every shipped package uses these; a provider needing
# something else is a review decision, not a capture decision.
DEFAULT_MAX_RESULT_BYTES = 65536
#: What a tool result may carry. `structuredContent` counts as
#: ``application/json`` and a `text` content block as ``text/plain``, and MCP
#: servers conventionally return BOTH -- the text block is a serialization of
#: the structured payload. Signing only the JSON half made
#: `_enforce_signed_tool_result` refuse every such result as `result_invalid`:
#: 425 of 431 shipped tools could be routed, planned, authorized and executed
#: and then had their answer thrown away at the last gate. Verified against
#: Google Calendar on 2026-09-08, whose `structuredContent` validated against
#: its own signed output schema while the accompanying text block failed this.
DEFAULT_MIME_TYPES = ("application/json", "text/plain")

# What a result schema means when the provider states none. Matches the shape
# GitHub's shipped package already carries, and admits any well-formed object.
OPEN_RESULT_SCHEMA = {"additionalProperties": True, "type": "object"}

# The manifest must DECLARE the authority for it to be signed and verified.
AUTHORITY_ASSET = {
    "kind": "capability_authority",
    "media_type": "application/json",
    "path": "tools/authority.json",
}


# ---------------------------------------------------------------- token lookup


def _access_token(plugin_id: str) -> str:
    """The operator's own token for this plugin, from the sealed vault.

    Prefers a committed grant. Falls back to a prepared transaction, which is
    what survives when the token exchange succeeded but the connection was
    refused afterwards -- exactly the state a package with no signed authority
    leaves behind, and the state this script exists to get out of.
    """
    import os

    from mcp_platform.oauth_vault import OAuthCredentialVault

    # Either spelling opens the vault -- `sealed_secrets` reads the file variant
    # too, and that is the one this installation actually boots with, so a guard
    # naming only the inline variable turned a working shell into a refusal.
    if not (
        os.environ.get("OMEGA_SEAL_PASSPHRASE")
        or os.environ.get("OMEGA_SEAL_PASSPHRASE_FILE")
    ):
        raise SystemExit(
            "The OAuth vault is sealed and no seal passphrase is set.\n"
            "Set OMEGA_SEAL_PASSPHRASE, or OMEGA_SEAL_PASSPHRASE_FILE pointing at the\n"
            "file Omega boots with, in this shell and run again.\n"
            "The passphrase is read from the environment and never printed or stored."
        )

    vault = OAuthCredentialVault()
    try:
        grants = vault.committed_grant_receipts()
    except ValueError as exc:
        if str(exc) != "oauth_vault_locked":
            raise
        raise SystemExit(
            "OMEGA_SEAL_PASSPHRASE did not open the vault. Check it is the passphrase "
            "this installation's sealed key was written with."
        ) from None

    for grant in grants:
        if grant.plugin_id != plugin_id:
            continue
        partition = vault.load_token_partition(grant.token_partition_key)
        token = partition.token.access_token
        if token:
            print(f"  token source: committed grant ({grant.app_id})")
            return token

    for transaction in vault.recover_prepared_transactions():
        if transaction.plugin_id != plugin_id or transaction.prepared_token is None:
            continue
        token = transaction.prepared_token.access_token
        if token:
            expires = transaction.prepared_token.expires_at
            if expires is not None and expires <= time.time():
                print("  prepared token has expired; authorize again")
                continue
            print(f"  token source: prepared transaction ({transaction.app_id})")
            return token

    raise SystemExit(
        f"No usable token for {plugin_id} in the vault.\n"
        "Authorize the plugin once in the HUD, then run this again. A sign-in "
        "that failed at the capability check still leaves a prepared token, "
        "which is enough."
    )


def _declared_wire_version(plugin_id: str) -> str | None:
    """The protocol version this package's runtime client will negotiate.

    A capture that offers a DIFFERENT version gets a different answer, and the
    difference reads as drift for the rest of its life. Google's Calendar server
    answers the same nine tools with different output schemas on 2025-11-25 and
    on 2026-07-28; capturing on the newer one produced a package whose every
    schema digest disagreed with what the client -- pinned to the version the
    package declares -- actually sees.
    """
    manifest_path = PACKAGES / plugin_id / "plugin.json"
    if not manifest_path.is_file():
        return None
    authority_path = PACKAGES / plugin_id / "tools" / "authority.json"
    if not authority_path.is_file():
        return None
    apps = json.loads(authority_path.read_bytes()).get("apps") or {}
    for app in apps.values():
        compatibility = (app or {}).get("runtime_compatibility")
        if isinstance(compatibility, dict):
            version = compatibility.get("wire_protocol_version")
            if isinstance(version, str) and version:
                return version
    return None


def capture(plugin_id: str, endpoint: str, out: Path) -> int:
    from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION

    token = _access_token(plugin_id)
    declared = _declared_wire_version(plugin_id)
    if declared is not None and declared != MCP_PROTOCOL_VERSION:
        print(f"  offering {declared}, the wire version this package declares")
    print(f"  initialize -> {endpoint}")
    try:
        document = capture_provider_surface(
            endpoint,
            access_token=token,
            **({} if declared is None else {"offered_version": declared}),
        )
    except ProviderCaptureError as exc:
        raise SystemExit(str(exc)) from None
    print(f"  server: {document['server_info']} | protocol {document['protocol_version']}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps(document, indent=2, sort_keys=True).encode() + b"\n")
    print(f"\n  captured {len(document['tools'])} tools -> {_display(out)}")
    for tool in document["tools"]:
        risk = _propose_risk(tool.get("annotations") or {})
        print(f"    {str(tool.get('name')):34} {risk}")
    return 0


# ----------------------------------------------------------------------- weave


def _resource_group(
    entries: Any, *, field: str
) -> tuple[dict[str, Any], set[str]]:
    """Turn a captured resource list into its signed policy group.

    The signed policy is ``{uri | uri_template, mime_type, max_bytes}``. Only
    the NAME takes part in reconciliation, so a provider that omits its MIME
    type does not become unconnectable over a field nothing compares.
    """
    group: dict[str, Any] = {}
    names: set[str] = set()
    for entry in entries:
        name = str(entry.get("name") or "")
        uri = str(entry.get("uri") or entry.get("uriTemplate") or "")
        if not name or not uri:
            raise SystemExit(f"a captured resource is missing a name or {field}")
        # The provider's own value, or None. Inventing one guarantees drift:
        # the receipt carries exactly what the server sent, so a default that
        # the server never stated can never match it.
        stated_mime = entry.get("mimeType")
        group[name] = {
            field: uri,
            "mime_type": str(stated_mime) if stated_mime else None,
            "max_bytes": DEFAULT_MAX_RESULT_BYTES,
        }
        names.add(name)
    return group, names


def _prompt_group(entries: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Turn a captured prompt list into its signed policy group.

    A prompt's schema digest covers its argument list exactly, and the contract
    recomputes it, so it is derived here rather than declared.
    """
    from mcp_platform.canonical import canonical_bytes as _canonical

    group: dict[str, Any] = {}
    declared: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        if not name:
            raise SystemExit("a captured prompt is missing a name")
        arguments = sorted(
            (
                {"name": str(argument.get("name") or ""), "required": bool(argument.get("required"))}
                for argument in (entry.get("arguments") or ())
            ),
            key=lambda argument: argument["name"],
        )
        digest = sha256(_canonical({"arguments": arguments})).hexdigest()
        group[name] = {
            "title": str(entry.get("title") or entry.get("description") or name),
            "arguments": arguments,
            "schema_sha256": digest,
            "max_result_bytes": DEFAULT_MAX_RESULT_BYTES,
        }
        declared.append(
            {"kind": "prompt", "name": name, "schema_sha256": digest, "version": None}
        )
    return group, declared




# ``mcp_platform.client_contracts`` enforces this map when it parses a signed
# authority, so the effect is never an independent choice: name the risk class
# and the effect follows.
_EFFECT_FOR_RISK = {
    "read_only": "read",
    "additive_write": "create",
    "mutating_write": "update",
    "destructive": "delete",
    "send_publish": "send",
    "cross_provider_transfer": "transfer",
}


def _propose_risk(annotations: dict[str, Any]) -> str:
    """A STARTING risk class read off the provider's own hints.

    This is a proposal for a reviewer, not a verdict. The hints are coarse and
    the shipped Gmail package overrides four of them in both directions: a draft
    that can be sent was raised to ``send_publish``, removing a label was lowered
    off ``destructive``, and replacing a message's whole label set was raised
    onto it. Nothing in an annotation could have produced those calls, so weave
    takes overrides and prints every classification for review before signing.
    """
    if annotations.get("readOnlyHint") is True:
        return "read_only"
    if annotations.get("destructiveHint") is True:
        return "destructive"
    if annotations.get("idempotentHint") is True:
        return "mutating_write"
    return "additive_write"


def _title(name: str, annotations: dict[str, Any]) -> str:
    stated = str(annotations.get("title") or "").strip().rstrip(".")
    return stated or name.replace("_", " ").strip().capitalize()


def _display(path: Path) -> str:
    """Repo-relative when it is in the repo, absolute otherwise (tests, forks)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _digest(schema: Any) -> str:
    return sha256(canonical_bytes(schema)).hexdigest()


def weave(plugin_id: str, capture_path: Path, overrides_path: Path | None) -> int:
    package = PACKAGES / plugin_id
    manifest_path = package / "plugin.json"
    manifest = json.loads(manifest_path.read_bytes())
    document = json.loads(capture_path.read_bytes())
    overrides: dict[str, Any] = (
        json.loads(overrides_path.read_bytes()) if overrides_path is not None else {}
    )
    unknown = set(overrides) - {str(tool["name"]) for tool in document["tools"]}
    if unknown:
        # An override naming a tool the provider does not offer is a stale
        # review, and silently ignoring it would leave a real tool unreviewed.
        raise SystemExit("overrides name tools the capture does not have: " + ", ".join(sorted(unknown)))

    # What this package already decided. A risk class is a review decision, not
    # an observation, so a fresh capture of the same tool must not silently
    # replace one -- only an explicit override may.
    reviewed: dict[str, Any] = {}
    authority_file = package / "tools" / "authority.json"
    if authority_file.exists():
        existing = json.loads(authority_file.read_bytes())
        for app_authority in (existing.get("apps") or {}).values():
            reviewed.update(app_authority.get("tools") or {})

    apps = manifest["required_apps"]
    if len(apps) != 1:
        raise SystemExit("weave supports a single-app package; this one has %d" % len(apps))
    app = apps[0]
    if document["endpoint"] != app["remote_origin"]:
        raise SystemExit(
            f"capture endpoint {document['endpoint']} is not the app's remote origin "
            f"{app['remote_origin']}"
        )

    tools: dict[str, Any] = {}
    capabilities: list[dict[str, Any]] = []
    unplannable: list[str] = []
    review: list[tuple[str, str, str, bool]] = []
    for tool in document["tools"]:
        name = str(tool["name"])
        annotations = tool.get("annotations") or {}
        override = overrides.get(name) or {}
        already = reviewed.get(name) or {}
        proposed = _propose_risk(annotations)
        # Explicit review first, then this package's standing review, then the
        # provider's hint -- which only decides a tool nobody has reviewed.
        risk = str(override.get("risk_class") or already.get("risk_class") or proposed)
        if risk not in _EFFECT_FOR_RISK:
            raise SystemExit(f"{name}: unknown risk class {risk!r}")
        effect = _EFFECT_FOR_RISK[risk]
        title = str(
            override.get("title") or already.get("title") or _title(name, annotations)
        )
        source = "override" if override else ("package" if already else "provider")
        review.append((name, risk, title, source, proposed))
        input_schema = tool["inputSchema"]
        # A provider is not obliged to describe its results. Slack's server
        # declares none at all, while Google's declares one per tool. The signed
        # policy still needs a mapping -- ``client_contracts`` refuses a null
        # output schema -- so an undeclared result is recorded as the open
        # object, which claims nothing about a shape nobody stated. This is safe
        # to sign because the result schema is deliberately outside the
        # capability revision: ``_revision_document`` hashes only name, input
        # digest and version, so declaring one here cannot drift a live server
        # that declares none.
        output_schema = tool.get("outputSchema") or OPEN_RESULT_SCHEMA
        tools[name] = {
            "allowed_mime_types": list(DEFAULT_MIME_TYPES),
            "effect": effect,
            "input_schema": input_schema,
            "input_schema_sha256": _digest(input_schema),
            "max_result_bytes": DEFAULT_MAX_RESULT_BYTES,
            "output_schema": output_schema,
            "output_schema_sha256": _digest(output_schema),
            "requires_revision": False,
            "risk_class": risk,
            "title": title,
        }
        capabilities.append(
            {
                "kind": "tool",
                "name": name,
                "schema_sha256": _digest(input_schema),
                "version": None,
            }
        )
        if not _plannable(input_schema, output_schema):
            unplannable.append(name)

    if unplannable:
        # Named, never silent: the chat route cannot reach these, so the
        # operator has to know before signing. They still ship -- an
        # unplannable tool is an authorized capability the planner happens not
        # to be able to fill, which is what github.official has always
        # released, and refusing the package instead would have cost Netlify
        # all nine tools over six discriminated unions.
        print(
            f"  WARNING: the planner cannot fill {len(unplannable)} of {len(tools)} tools;"
            " chat will not reach these:"
        )
        for name in unplannable:
            print(f"    {name}")

    from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

    # Reported, not declared. Omega enumerates a capability group only when the
    # package declares something of that kind, so declaring one costs a
    # reconciliation surface and today buys nothing -- the planner routes tools.
    # Hugging Face makes the cost concrete: it serves its docs as ``skill://``
    # resources and answered 155 in one capture and 149 in the next, minutes
    # apart, so a package declaring them drifts whenever a doc is edited.
    served = {
        "resource": len(document.get("resources") or ()),
        "resource template": len(document.get("resource_templates") or ()),
        "prompt": len(document.get("prompts") or ()),
    }
    if any(served.values()):
        summary = ", ".join(f"{count} {name}(s)" for name, count in served.items() if count)
        print(f"  the provider also serves {summary}; not declared (see weave's note)")

    app_authority: dict[str, Any] = {
        "prompts": {},
        "resource_templates": {},
        "resources": {},
        "tools": tools,
    }
    # A server speaking Omega's own wire version needs no compatibility block --
    # GitHub's signed package carries none. Declaring one there would assert a
    # legacy handshake against a server that never asked for it. The Google
    # packages carry one because their endpoint answers 2025-11-25 and reports
    # its identity through the handshake rather than the metadata document.
    if document["protocol_version"] != MCP_PROTOCOL_VERSION:
        server = document["server_info"]
        identity = (str(server.get("name") or ""), str(server.get("version") or ""))
        if document["protocol_version"] not in HANDSHAKE_PROTOCOL_VERSIONS:
            raise SystemExit(
                f"the provider speaks {document['protocol_version']}, which is neither "
                f"Omega's own version nor a handshake-era one the SDK knows"
            )
        if not all(identity):
            raise SystemExit(
                "the provider speaks the legacy handshake but named no server "
                "identity, and the compatibility block cannot be invented"
            )
        app_authority["runtime_compatibility"] = {
            "handshake_mode": "legacy",
            "missing_metadata": {"server_id": identity[0], "server_version": identity[1]},
            # The version THIS server speaks, as observed. `_discover_receipt`
            # compares the live negotiated version against this one exactly, so
            # it is the drift check for the wire: a server that moves off it
            # stops connecting until the package is re-released. It must be a
            # member of the SDK's handshake-era set, which is what the package
            # verifier admits.
            "wire_protocol_version": document["protocol_version"],
        }
        print(
            f"  provider speaks {document['protocol_version']} (Omega speaks "
            f"{MCP_PROTOCOL_VERSION}) -- declaring the legacy handshake"
        )

    authority = {
        "apps": {app["app_id"]: app_authority},
        "plugin_id": manifest["plugin_id"],
        "schema_version": 2,
    }

    app["capabilities"] = sorted(capabilities, key=lambda item: item["name"])
    # The authority is only signed if the manifest declares it as an asset. The
    # signing script recomputes each declared asset's digest but never adds one,
    # so a package that never had an authority would otherwise sign cleanly and
    # ship without it. The digest is real, not a placeholder: the manifest must
    # verify on its own, and signing only recomputes what is already declared.
    # Signed assets are stored as canonical bytes and verified byte-for-byte
    # (`canonical_bytes(document) != payload` is an asset_invalid). Pretty
    # printing here would sign a package that cannot be opened.
    authority_bytes = canonical_bytes(authority)
    assets = [a for a in manifest["assets"] if a.get("path") != AUTHORITY_ASSET["path"]]
    manifest["assets"] = sorted(
        [*assets, {**AUTHORITY_ASSET, "sha256": sha256(authority_bytes).hexdigest()}],
        key=lambda item: item["path"],
    )
    # The signature the manifest carries is now stale, and deliberately left in
    # place. A manifest is an exact-fields document, so dropping the pair makes
    # it unparseable and the signing script cannot even read it. Stale is not a
    # risk here: the signed bytes changed, so `verify_plugin_package` refuses
    # this manifest until it is re-signed. It fails closed either way, and only
    # one of the two can actually be signed afterwards.

    authority_path = package / "tools" / "authority.json"
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_bytes(authority_bytes)
    manifest_path.write_bytes(canonical_bytes(manifest))

    print(f"  wrote {_display(authority_path)} ({len(tools)} tools)")
    print(f"  wrote {_display(manifest_path)} (capabilities + authority asset)")
    print()
    print("  REVIEW -- a risk class decides what Omega may do without asking again.")
    print(f"  {'tool':34} {'risk class':24} title")
    marks = {"override": "*", "package": "=", "provider": " "}
    for name, risk, title, source, _proposed in review:
        print(f"{marks[source]} {name:34} {risk:24} {title}")
    legend = [
        (mark, meaning)
        for mark, meaning in (
            ("*", "pinned by the overrides file"),
            ("=", "kept from this package's existing review"),
        )
        if any(marks[item[3]] == mark for item in review)
    ]
    for mark, meaning in legend:
        print(f"  ({mark} = {meaning} rather than the provider's hints)")
    # Keeping the reviewed value is the safe default, and it would hide a
    # provider that has started proposing something stronger. So say so: this
    # is the one thing a re-capture can tell a maintainer that nothing else can.
    contested = [
        (name, risk, proposed)
        for name, risk, _title, source, proposed in review
        if source == "package" and proposed != risk
    ]
    if contested:
        print()
        print(
            f"  NOTE: the provider's hints now propose a different class for "
            f"{len(contested)} reviewed tool(s); the package's review was kept:"
        )
        for name, risk, proposed in contested:
            print(f"    {name:34} reviewed {risk:24} provider proposes {proposed}")
    print()
    print("  Then sign:")
    print("    .venv-brain\\Scripts\\python.exe scripts\\sign_marketplace_catalog.py")
    return 0


def _plannable(input_schema: Any, output_schema: Any) -> bool:
    from mcp_platform import plugin_planning

    try:
        plugin_planning._schema(input_schema)
        plugin_planning._result_schema(output_schema)
    except Exception:  # noqa: BLE001 - reported, never raised
        return False
    return True


# --------------------------------------------------------------------- scaffold

#: Everything a provider needs that cannot be read off the wire. The capture
#: supplies the tool surface; this supplies identity, listing copy and consent
#: scopes -- the parts a person decides.
SPEC_TEMPLATE = {
    "plugin_id": "vendor.official",
    "title": "Vendor",
    "summary": "Official Vendor MCP access for the things it does.",
    "publisher_display_name": "Vendor",
    "app_publisher": "com.vendor",
    "provider_id": "com.vendor.mcp",
    "remote_origin": "https://mcp.vendor.com/mcp",
    "authorization_issuer": "https://mcp.vendor.com",
    "authoritative_source_url": "https://docs.vendor.com/mcp",
    "source_url": "https://vendor.com",
    "scopes": ["read", "write"],
    "categories": ["productivity"],
    "search_terms": ["vendor"],
    "skill_slug": "vendor",
}
_SPEC_FIELDS = frozenset(SPEC_TEMPLATE)

BUILD_ROOT = ROOT / "plugin_platform" / "marketplace_build"
CATALOG_PATH = ROOT / "plugin_platform" / "marketplace" / "catalog.json"
PROVENANCE_PATH = ROOT / "plugin_platform" / "marketplace_provenance.json"
REVIEW_POLICY_PATH = BUILD_ROOT / "review_policy.json"
PACKAGE_URI_PREFIX = "https://plugins.omega2ai.com/marketplace/packages"
_PLACEHOLDER_DIGEST = "0" * 64
#: A syntactically valid Ed25519 signature that cannot verify against anything.
_UNSIGNED_SIGNATURE = base64.b64encode(bytes(64)).decode("ascii")


def _write_json(path: Path, document: Any, *, canonical: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if canonical:
        path.write_bytes(canonical_bytes(document))
    else:
        path.write_bytes(json.dumps(document, indent=2, sort_keys=True).encode() + b"\n")


def scaffold(spec_path: Path) -> int:
    """Write every unsigned artefact a new provider needs, ready to capture.

    Six files have to agree before a package can be signed, and getting any one
    of them wrong surfaces as an opaque signing failure rather than a message
    naming what is missing. Hand-authoring them is what made the first Slack
    release a day's work. This writes them together from one reviewed spec,
    leaves every digest as a placeholder the signing step fills in, and stops
    before anything needing the key or the network.
    """
    from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION

    spec = json.loads(spec_path.read_bytes())
    missing = _SPEC_FIELDS - set(spec)
    unknown = set(spec) - _SPEC_FIELDS
    if missing or unknown:
        raise SystemExit(
            "spec fields are wrong"
            + (f"\n  missing: {sorted(missing)}" if missing else "")
            + (f"\n  unknown: {sorted(unknown)}" if unknown else "")
        )
    plugin_id = str(spec["plugin_id"])
    app_id = f"{plugin_id}.mcp"
    package = PACKAGES / plugin_id
    if package.exists():
        raise SystemExit(f"{_display(package)} already exists; scaffold will not overwrite it")

    # Built FIRST because it is the strictest reader of the spec: it is what
    # rejects a malformed publisher, origin, issuer or documentation URL. Doing
    # it after the writes left half-packages on disk whose only symptom, on the
    # rerun, was "already exists; scaffold will not overwrite it".
    # The trust tier is minted by the module that owns the provenance format,
    # never named here. A maintainer command must not be able to grant a tier;
    # the grant is the review policy naming this record's digest, checked again
    # at signing time.
    record = scaffold_provenance_record(
        plugin_id=plugin_id,
        provider_publisher_id=spec["app_publisher"],
        publisher_display_name=spec["publisher_display_name"],
        provider_id=spec["provider_id"],
        remote_origin=spec["remote_origin"],
        authorization_issuer=spec["authorization_issuer"],
        authoritative_source_url=spec["authoritative_source_url"],
    )

    skill_path = f"skills/{spec['skill_slug']}/SKILL.md"
    skill_body = (
        f"# {spec['title']}\n\n{spec['summary']}\n\n"
        "Connect the plugin, then ask for what you need in plain language.\n"
    )
    (package / skill_path).parent.mkdir(parents=True, exist_ok=True)
    (package / skill_path).write_bytes(skill_body.encode())

    manifest = {
        "assets": [
            {
                "kind": "skill",
                "media_type": "text/markdown",
                "path": skill_path,
                "sha256": sha256(skill_body.encode()).hexdigest(),
            }
        ],
        "description": spec["summary"],
        "enterprise_roles": [],
        "entry_points": [
            {
                "entry_point_id": "get_started",
                "required_app_ids": [app_id],
                "skill_path": skill_path,
                "title": f"Get started with {spec['title']}",
            }
        ],
        "kind": "mcp_backed",
        "manifest_version": 2,
        "minimum_omega_version": "3.0.0",
        "optional_apps": [],
        "plugin_id": plugin_id,
        "plugin_version": "1.0.0",
        "proactive_event_rules": [],
        "publisher_id": "omega.first_party",
        "required_apps": [
            {
                "app_id": app_id,
                "authentication": {
                    "kind": "oauth_authorization_code_pkce",
                    "scopes": sorted(spec["scopes"]),
                    "secret_fields": [],
                },
                "authorization_issuer": spec["authorization_issuer"],
                # Filled by ``weave`` from the captured surface. A package
                # cannot declare a tool before anyone was allowed to look.
                "capabilities": [],
                "deployment": "remote",
                "protocol_version": MCP_PROTOCOL_VERSION,
                "provider_id": spec["provider_id"],
                "publisher_id": spec["app_publisher"],
                "remote_origin": spec["remote_origin"],
                "transports": ["streamable_http"],
            }
        ],
        "schedule_template_paths": [],
        # Placeholders the signing step replaces. They are shaped like the real
        # thing rather than left empty because a manifest is an exact-fields
        # document that refuses an empty identifier -- and shaped is safe: the
        # signature cannot verify, so an unsigned scaffold fails closed exactly
        # as it should until someone signs it.
        "signature": _UNSIGNED_SIGNATURE,
        "signing_key_id": json.loads(CATALOG_PATH.read_bytes())["signing_key_id"],
        "starter_requests": [],
        "title": spec["title"],
    }
    _write_json(package / "plugin.json", manifest, canonical=True)

    listing = {
        "categories": list(spec["categories"]),
        "distribution": {"kind": "remote_package"},
        "icon_asset_path": None,
        "offline_capable": False,
        "pricing": "free",
        "release_history": [],
        "released_at": time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime()),
        "search_terms": sorted(set(spec["search_terms"])),
        "source_url": spec["source_url"],
        "support_url": None,
        "update_notes_sha256": None,
    }
    _write_json(
        BUILD_ROOT / "metadata" / plugin_id / "catalog_metadata.json", listing, canonical=False
    )

    provenance = json.loads(PROVENANCE_PATH.read_bytes())
    records = [item for item in provenance["records"] if item["plugin_id"] != plugin_id]
    records.append(record)
    provenance["records"] = sorted(records, key=lambda item: item["plugin_id"])
    provenance["sequence"] = int(provenance["sequence"]) + 1
    _write_json(PROVENANCE_PATH, provenance, canonical=True)

    # An omega_verified listing is granted only to a plugin an explicit review
    # policy names TOGETHER with the digest of the provenance it will carry, so
    # a package can never nominate its own trust tier.
    policy = json.loads(REVIEW_POLICY_PATH.read_bytes())
    promotions = [item for item in policy["promotions"] if item["plugin_id"] != plugin_id]
    promotions.append(
        {"plugin_id": plugin_id, "provenance_sha256": sha256(canonical_bytes(record)).hexdigest()}
    )
    policy["promotions"] = sorted(promotions, key=lambda item: item["plugin_id"])
    _write_json(REVIEW_POLICY_PATH, policy, canonical=False)

    catalog = json.loads(CATALOG_PATH.read_bytes())
    entries = [item for item in catalog["entries"] if item["plugin_id"] != plugin_id]
    entries.append(
        {
            **listing,
            # Every digest is a placeholder on purpose: only signing can know
            # them, and it recomputes all three from the real bytes.
            "manifest_sha256": _PLACEHOLDER_DIGEST,
            "package_sha256": _PLACEHOLDER_DIGEST,
            "provenance_sha256": _PLACEHOLDER_DIGEST,
            "package_uri": f"{PACKAGE_URI_PREFIX}/{plugin_id}/1.0.0",
            "plugin_id": plugin_id,
            "plugin_version": "1.0.0",
            "publisher_id": "omega.first_party",
            "summary": spec["summary"],
            "title": spec["title"],
            # Mirrors the provenance record rather than asserting a tier of its
            # own -- and the entry only survives release if the policy digest
            # and that record still agree.
            "trust_tier": record["trust_tier"],
        }
    )
    catalog["entries"] = sorted(entries, key=lambda item: item["plugin_id"])
    catalog["sequence"] = int(catalog["sequence"]) + 1
    _write_json(CATALOG_PATH, catalog, canonical=True)

    print(f"  scaffolded {plugin_id} (app {app_id})")
    for path in (
        package / "plugin.json",
        package / skill_path,
        BUILD_ROOT / "metadata" / plugin_id / "catalog_metadata.json",
        PROVENANCE_PATH,
        REVIEW_POLICY_PATH,
        CATALOG_PATH,
    ):
        print(f"    {_display(path)}")
    print()
    print("  Next, in order:")
    print(f"    1. provision the OAuth profile for {spec['authorization_issuer']}")
    print("       scripts/provision_oauth_profiles.py --help")
    print("    2. install the plugin in the HUD and authorize it once")
    print(f"    3. capture {plugin_id} --endpoint {spec['remote_origin']} --out <capture.json>")
    print(f"    4. weave {plugin_id} --capture <capture.json> [--overrides <reviewed risks>]")
    print(
        "       (a later re-capture keeps the risk classes and titles already in"
    )
    print(
        "        the package; --overrides is for changing one, not for holding it.)"
    )
    print(
        f"    5. scripts/sign_marketplace_catalog.py --plugin-id {plugin_id} "
        "--seed-sealed-path"
    )
    print(
        "       (--plugin-id matters: without it every package is re-signed onto"
    )
    print(
        "        the current key, which rotates packages nobody meant to touch.)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    grab = sub.add_parser("capture", help="read the live tool surface with the stored token")
    grab.add_argument("plugin_id")
    grab.add_argument("--endpoint", required=True)
    grab.add_argument("--out", type=Path, required=True)

    plan = sub.add_parser("scaffold", help="write the unsigned artefacts a new provider needs")
    plan.add_argument("--spec", type=Path, help="the reviewed provider spec")
    plan.add_argument(
        "--template", action="store_true", help="print a spec template and exit"
    )

    build = sub.add_parser("weave", help="turn a capture into signed-ready package files")
    build.add_argument("plugin_id")
    build.add_argument("--capture", type=Path, required=True)
    build.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help='reviewed risk classes and titles: {"tool_name": {"risk_class": ..., "title": ...}}',
    )

    args = parser.parse_args(argv)
    if args.command == "scaffold":
        if args.template:
            print(json.dumps(SPEC_TEMPLATE, indent=2, sort_keys=True))
            return 0
        if args.spec is None:
            raise SystemExit("scaffold needs --spec, or --template to print one")
        return scaffold(args.spec)
    if args.command == "capture":
        return capture(args.plugin_id, args.endpoint, args.out)
    return weave(args.plugin_id, args.capture, args.overrides)


if __name__ == "__main__":
    raise SystemExit(main())
