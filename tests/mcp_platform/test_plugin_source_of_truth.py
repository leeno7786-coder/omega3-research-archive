"""Migration gate: one universal lifecycle, no retired Featured runtime path."""

from __future__ import annotations

import ast
import os
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.plugin_authority import PluginExecutionPolicy
from mcp_platform.plugin_catalog import compile_plugin_tool_bindings
from mcp_platform.plugin_connections import (
    AuthorizationStart,
    ConnectionStatus,
    PluginConnectionCoordinator,
)
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.plugin_state import PluginLifecycleState
from plugin_platform.contracts import PluginAuthenticationKind
from tests.helpers.plugin_consent import full_permission_settings


ROOT = Path(__file__).resolve().parents[2]

#: Every symbol a retired Featured runtime path would have to reintroduce.
RETIRED = (
    "FeaturedPluginService",
    "get_default_featured_plugin_service",
    "_FEATURED_PACKAGES",
    "resolve_authoritative_connector",
    "GOOGLE_DRIVE_SCOPES",
    "/api/plugins/featured",
    "featured_plugin_",
)

#: The packages the current signed embedded catalog ships.
CURRENT_PACKAGES = (
    "github.official",
    "google.workspace.calendar",
    "google.workspace.drive",
    "google.workspace.gmail",
)

_ACCOUNT_KEY = "operator-account-key"
_SOURCE_SUFFIXES = frozenset({".py", ".js", ".html"})
# Generated release staging is validated by the portable-release verifier, not
# treated as a second production source tree by singleton/source-of-truth scans.
# The bundled Linux interpreter carries its own site-packages (third-party
# code, like .venv-brain's), so it is not Omega source either.
_SKIPPED_DIRECTORIES = frozenset(
    {"tests", "docs", "node_modules", "__pycache__", "dist", "python-3.13-linux"}
)
_OAUTH_FLOWS = {
    PluginAuthenticationKind.OAUTH_AUTHORIZATION_CODE_PKCE: "authorization_code_pkce",
    PluginAuthenticationKind.OAUTH_DEVICE_CODE: "device_code",
    PluginAuthenticationKind.OAUTH_CLIENT_METADATA_DOCUMENT: "authorization_code_pkce",
}


def production_source_paths(root: Path | None = None) -> tuple[Path, ...]:
    """Return every shipped Python/HUD source file, excluding tests and tooling."""

    base = ROOT if root is None else root
    collected: list[Path] = []
    for directory, subdirectories, filenames in os.walk(base):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if not name.startswith(".") and name not in _SKIPPED_DIRECTORIES
        )
        parent = Path(directory)
        for filename in sorted(filenames):
            if Path(filename).suffix in _SOURCE_SUFFIXES:
                collected.append(parent / filename)
    return tuple(collected)


def production_source_text(root: Path | None = None) -> str:
    """Concatenate production source so one scan covers server and HUD alike."""

    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in production_source_paths(root)
    )


def test_native_connector_and_workspace_sources_are_absent() -> None:
    assert not (ROOT / "connectors").exists()
    assert not (ROOT / "auto_loop" / "execute" / "connector_tool.py").exists()
    assert not (ROOT / "auto_loop" / "execute" / "workspace_alignment.py").exists()
    assert not (ROOT / "schema" / "connector_action.py").exists()
    assert not (ROOT / "schema" / "workspace_alignment.py").exists()


def test_production_python_has_no_native_connector_imports() -> None:
    offenders: list[str] = []
    for path in production_source_paths():
        if path.suffix != ".py":
            continue
        relative = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        if "from connectors" in text or "import connectors" in text:
            offenders.append(str(relative))
    assert offenders == []


def test_current_action_vocabulary_has_no_retired_workspace_actions() -> None:
    for relative in (
        "meta_loop/action/run.py",
        "auto_loop/execute/run.py",
        "schema/meta.schema.yaml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "CONNECTOR_ACTION" not in text
        assert "ALIGN_WORKSPACE" not in text


def test_featured_runtime_paths_are_absent_from_production() -> None:
    text = production_source_text()
    for symbol in RETIRED:
        assert symbol not in text


@pytest.mark.parametrize("symbol", RETIRED)
def test_retired_path_gate_fails_on_a_planted_production_symbol(
    symbol: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a scan narrowed so a reintroduced Featured symbol survives."""

    package = tmp_path / "mcp_platform"
    package.mkdir()
    (package / "plugin_lifecycle.py").write_text(f"VALUE = {symbol!r}\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)

    with pytest.raises(AssertionError):
        test_featured_runtime_paths_are_absent_from_production()


def test_retired_path_gate_reads_the_hud_bundle_and_ui_sources() -> None:
    """Catches a scan restricted to Python while the HUD keeps a retired route."""

    scanned = {path.relative_to(ROOT).as_posix() for path in production_source_paths()}

    assert "hud/omega_interface.html" in scanned
    assert "hud/ui-src/panels/settings-plugins.js" in scanned
    assert "mcp_platform/plugin_lifecycle.py" in scanned
    assert not any(item.startswith("tests/") for item in scanned)


# --------------------------------------------------------------------------
# Marketplace integrity: exactly one implementation of each singleton
# component the plan introduced (catalog schema, snapshot store, lifecycle
# service), so a competing/duplicate implementation can never quietly diverge
# from the reviewed one.
# --------------------------------------------------------------------------


def _symbol_definition_files(symbol: str, *, root: Path | None = None) -> tuple[str, ...]:
    """Return production files defining ``symbol`` as a top-level class/function/constant.

    Only module-level statements are considered (never a nested attribute or
    method of the same name), so this names exactly the files that could
    compete to be *the* implementation of a singleton component.
    """

    base = ROOT if root is None else root
    found: list[str] = []
    for path in production_source_paths(root):
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError:
            continue
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == symbol
            ) or (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == symbol
                    for target in node.targets
                )
            ):
                found.append(path.relative_to(base).as_posix())
                break
    return tuple(found)


def test_exactly_one_catalog_parser_version_is_defined() -> None:
    """Catches a second, competing marketplace catalog schema constant/parser."""

    assert _symbol_definition_files("CATALOG_SCHEMA_VERSION") == ("plugin_platform/catalog.py",)


def test_exactly_one_marketplace_snapshot_store_is_defined() -> None:
    """Catches a second on-device catalog snapshot store bypassing the reviewed one."""

    assert _symbol_definition_files("CatalogStore") == ("plugin_platform/catalog_store.py",)


def test_exactly_one_plugin_lifecycle_service_is_defined() -> None:
    """Catches a second plugin lifecycle service competing with the universal one."""

    assert _symbol_definition_files("PluginLifecycleService") == (
        "mcp_platform/plugin_lifecycle.py",
    )


@pytest.mark.parametrize(
    ("symbol", "canonical_relative", "mutant_relative", "mutant_source"),
    [
        pytest.param(
            "CATALOG_SCHEMA_VERSION",
            "plugin_platform/catalog.py",
            "plugin_platform/legacy_catalog.py",
            "CATALOG_SCHEMA_VERSION = 2\n",
            id="second-catalog-schema-version",
        ),
        pytest.param(
            "CatalogStore",
            "plugin_platform/catalog_store.py",
            "plugin_platform/legacy_catalog_store.py",
            "class CatalogStore:\n    pass\n",
            id="second-snapshot-store",
        ),
        pytest.param(
            "PluginLifecycleService",
            "mcp_platform/plugin_lifecycle.py",
            "mcp_platform/legacy_plugin_lifecycle.py",
            "class PluginLifecycleService:\n    pass\n",
            id="second-lifecycle-service",
        ),
    ],
)
def test_singleton_symbol_gate_rejects_a_second_definition(
    symbol: str,
    canonical_relative: str,
    mutant_relative: str,
    mutant_source: str,
    tmp_path: Path,
) -> None:
    """Catches a competing implementation planted beside the canonical one."""

    canonical_path = tmp_path / canonical_relative
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(mutant_source, encoding="utf-8")
    mutant_path = tmp_path / mutant_relative
    mutant_path.parent.mkdir(parents=True, exist_ok=True)
    mutant_path.write_text(mutant_source, encoding="utf-8")

    found = _symbol_definition_files(symbol, root=tmp_path)

    assert set(found) == {canonical_relative, mutant_relative}


# --------------------------------------------------------------------------
# Marketplace integrity: the HUD never reaches past the local backend to the
# upstream Official MCP Registry, and no plugin install entry point accepts
# a raw, unimported registry listing.
# --------------------------------------------------------------------------

_OFFICIAL_REGISTRY_HOST = "registry.modelcontextprotocol.io"


def _hud_official_registry_offenders(root: Path | None = None) -> tuple[str, ...]:
    base = ROOT if root is None else root
    offenders: list[str] = []
    for path in production_source_paths(root):
        relative = path.relative_to(base).as_posix()
        if not relative.startswith("hud/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _OFFICIAL_REGISTRY_HOST in text:
            offenders.append(relative)
    return tuple(offenders)


def test_hud_never_calls_the_official_registry_directly() -> None:
    """Catches a HUD panel bypassing the local backend to call the upstream registry."""

    assert _hud_official_registry_offenders() == ()


def test_hud_official_registry_gate_rejects_a_planted_direct_call(tmp_path: Path) -> None:
    """Catches a scan that stops noticing a hardcoded upstream registry URL in HUD."""

    (tmp_path / "hud").mkdir()
    (tmp_path / "hud" / "rogue.js").write_text(
        "fetch('https://registry.modelcontextprotocol.io/v0/servers')\n",
        encoding="utf-8",
    )

    assert _hud_official_registry_offenders(tmp_path) == ("hud/rogue.js",)


#: Parameter names that would smuggle a raw, unimported registry listing
#: straight into a plugin install entry point, bypassing
#: ``MarketplaceImporter``'s normalize/verify pipeline entirely.
_RAW_REGISTRY_INSTALL_PARAM_NAMES = frozenset(
    {"server", "raw", "listing", "registry_entry", "registry_server"}
)
#: Bare mapping/dict annotations are just as much a raw-registry smuggling
#: shape as ``McpRegistryServer`` itself -- a listing could travel as an
#: unparsed dict just as easily as the typed registry object.
_RAW_REGISTRY_ANNOTATION_HINTS = ("McpRegistryServer", "dict", "Dict", "Mapping", "MutableMapping")

#: The installer's own file/class. Every PUBLIC method here is scanned
#: regardless of name -- not just ones literally spelled ``install`` -- so a
#: differently named overload accepting the same raw shape cannot hide behind
#: a name-based scan. ``accept_normalized`` is documented as the installer's
#: only marketplace door; the type-guard checks below hold it to that.
_PLUGIN_INSTALLER_FILE = "plugin_platform/installer.py"
_PLUGIN_INSTALLER_CLASS = "PluginInstaller"


def _has_raw_registry_shape(parameter: ast.arg) -> bool:
    annotation = ast.unparse(parameter.annotation) if parameter.annotation is not None else ""
    return (
        any(hint in annotation for hint in _RAW_REGISTRY_ANNOTATION_HINTS)
        or parameter.arg in _RAW_REGISTRY_INSTALL_PARAM_NAMES
    )


def _method_raw_registry_offenders(
    method: ast.FunctionDef | ast.AsyncFunctionDef, relative: str
) -> list[str]:
    arguments = method.args
    if not arguments.args or arguments.args[0].arg != "self":
        return []
    return [
        f"{relative}:{method.lineno}:{parameter.arg}"
        for parameter in (*arguments.args[1:], *arguments.kwonlyargs)
        if _has_raw_registry_shape(parameter)
    ]


def _raw_registry_install_offenders(root: Path | None = None) -> tuple[str, ...]:
    """Every plugin-install entry point that could accept a raw registry listing.

    Covers (a) any method literally named ``install`` anywhere in production
    (the lifecycle's install entry points), and (b) every PUBLIC method of
    ``PluginInstaller`` regardless of name, since ``accept_normalized`` -- not
    a name convention -- is what actually makes it the marketplace's only
    door into the installer (see
    ``test_accept_normalized_is_the_marketplace_installer_entry_and_checks_type_first``).
    """

    base = ROOT if root is None else root
    offenders: list[str] = []
    for path in production_source_paths(root):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(base).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError:
            continue
        is_installer_file = relative == _PLUGIN_INSTALLER_FILE
        for node in ast.walk(tree):
            if (
                is_installer_file
                and isinstance(node, ast.ClassDef)
                and node.name == _PLUGIN_INSTALLER_CLASS
            ):
                for item in node.body:
                    if isinstance(
                        item, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and not item.name.startswith("_"):
                        offenders.extend(_method_raw_registry_offenders(item, relative))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "install":
                offenders.extend(_method_raw_registry_offenders(node, relative))
    return tuple(offenders)


def _find_class_method(
    tree: ast.Module, class_name: str, method_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    return item
    return None


def _is_type_identity_guard(statement: ast.stmt, *, param_name: str, expected: str) -> bool:
    """True for exactly ``if type(<param_name>) is not <expected>: raise ...``."""

    if not isinstance(statement, ast.If) or len(statement.body) != 1:
        return False
    if not isinstance(statement.body[0], ast.Raise):
        return False
    test = statement.test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and len(test.comparators) == 1
    ):
        return False
    left = test.left
    comparator = test.comparators[0]
    return bool(
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "type"
        and len(left.args) == 1
        and isinstance(left.args[0], ast.Name)
        and left.args[0].id == param_name
        and isinstance(comparator, ast.Name)
        and comparator.id == expected
    )


def _guards_type_before_any_other_statement(
    body: list[ast.stmt], *, param_name: str, expected: str
) -> bool:
    """True only when the type guard is the first non-docstring statement.

    Anything else preceding it -- a call, a `with`, another check -- means the
    method could act on an unverified candidate before ever proving its type,
    which is exactly the "before any registry call" property the review asked
    for.
    """

    for statement in body:
        if _is_type_identity_guard(statement, param_name=param_name, expected=expected):
            return True
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue  # a leading docstring is not a side effect
        return False
    return False


def _accept_normalized_enforces_verified_candidate_type(root: Path | None = None) -> bool:
    base = ROOT if root is None else root
    target = base / _PLUGIN_INSTALLER_FILE
    if not target.exists():
        return False
    try:
        tree = ast.parse(target.read_text(encoding="utf-8", errors="replace"), filename=str(target))
    except SyntaxError:
        return False
    method = _find_class_method(tree, _PLUGIN_INSTALLER_CLASS, "accept_normalized")
    if method is None:
        return False
    first_parameter = next(
        (argument.arg for argument in method.args.args if argument.arg != "self"), None
    )
    if first_parameter is None:
        return False
    return _guards_type_before_any_other_statement(
        method.body,
        param_name=first_parameter,
        expected="VerifiedNormalizedPluginCandidate",
    )


def test_no_plugin_install_overload_accepts_a_raw_registry_listing() -> None:
    """Catches an install entry point that skips normalize/verify for a raw listing."""

    assert _raw_registry_install_offenders() == ()


def test_raw_registry_install_gate_rejects_a_planted_overload(tmp_path: Path) -> None:
    """Catches a scan too narrow to notice a raw-registry install overload."""

    (tmp_path / "mcp_platform").mkdir()
    (tmp_path / "mcp_platform" / "rogue_lifecycle.py").write_text(
        "class RogueService:\n"
        "    def install(self, server, *, confirmation):\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )

    assert _raw_registry_install_offenders(tmp_path) == (
        "mcp_platform/rogue_lifecycle.py:2:server",
    )


def test_raw_registry_install_gate_catches_a_differently_named_installer_overload(
    tmp_path: Path,
) -> None:
    """Catches a raw-registry overload on PluginInstaller under any other name.

    A scan that only matches the literal name ``install`` would miss a new
    public method -- named anything else -- accepting the same raw shape;
    this is the blind spot the review found in the name-only scan above.
    """

    (tmp_path / "plugin_platform").mkdir()
    (tmp_path / "plugin_platform" / "installer.py").write_text(
        "class PluginInstaller:\n"
        "    def import_listing(self, server: McpRegistryServer) -> None:\n"
        "        raise NotImplementedError\n"
        "    def accept_normalized(self, candidate: object) -> None:\n"
        "        if type(candidate) is not VerifiedNormalizedPluginCandidate:\n"
        "            raise ValueError('plugin_install_candidate_unverified')\n",
        encoding="utf-8",
    )

    assert _raw_registry_install_offenders(tmp_path) == (
        "plugin_platform/installer.py:2:server",
    )


def test_accept_normalized_is_the_marketplace_installer_entry_and_checks_type_first() -> None:
    """Catches accept_normalized's exact-type guard weakening or moving later."""

    assert _accept_normalized_enforces_verified_candidate_type() is True


def test_accept_normalized_type_guard_gate_rejects_an_isinstance_weakening(
    tmp_path: Path,
) -> None:
    """Catches the exact `type(...) is not X` guard being loosened to isinstance.

    ``isinstance`` would also accept a subclass -- something a look-alike
    carrying the same attributes but constructed outside
    ``MarketplaceImporter`` could be crafted to satisfy -- exactly what an
    exact type test forecloses.
    """

    (tmp_path / "plugin_platform").mkdir()
    (tmp_path / "plugin_platform" / "installer.py").write_text(
        "class PluginInstaller:\n"
        "    def accept_normalized(self, candidate: object) -> None:\n"
        "        if not isinstance(candidate, VerifiedNormalizedPluginCandidate):\n"
        "            raise ValueError('plugin_install_candidate_unverified')\n",
        encoding="utf-8",
    )

    assert _accept_normalized_enforces_verified_candidate_type(tmp_path) is False


# --------------------------------------------------------------------------
# Marketplace integrity: no code path assigns the highest trust tier
# (``omega_verified``) from data an untrusted publisher, registry, or catalog
# entry supplies. Every legitimate site is closed and signature-gated:
# ``plugin_platform/catalog.py`` (a signed catalog entry, exposed only after
# ``verify_signed_bytes`` succeeds), ``plugin_platform/package_verifier.py``
# (the marketplace Ed25519-signed package default),
# ``plugin_platform/marketplace_provenance.py`` (a signed provenance record,
# likewise exposed only after signature verification), and
# ``scripts/build_marketplace_catalog.py`` (the one human-reviewed,
# closed-list build-time promotion gate). Anywhere else, constructing a
# ``trust_tier`` of ``omega_verified``/``TrustTier.OMEGA_VERIFIED`` is a
# regression this gate must catch.
# --------------------------------------------------------------------------

_OMEGA_VERIFIED_SANCTIONED_FILES = frozenset(
    {
        "plugin_platform/catalog.py",
        "plugin_platform/package_verifier.py",
        "plugin_platform/marketplace_provenance.py",
        "scripts/build_marketplace_catalog.py",
    }
)


def _is_omega_verified_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value == "omega_verified":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "OMEGA_VERIFIED"


def _is_trust_tier_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "trust_tier"


def _assignment_targets_trust_tier(target: ast.AST) -> bool:
    """True for `<x>.trust_tier = ...` or `<x>["trust_tier"] = ...` targets."""

    if isinstance(target, ast.Attribute):
        return target.attr == "trust_tier"
    if isinstance(target, ast.Subscript):
        # Python 3.9+: `node.slice` is the index expression directly.
        return _is_trust_tier_key(target.slice)
    return False


def _omega_verified_assignment_offenders(root: Path | None = None) -> tuple[str, ...]:
    """Every production site that constructs a `trust_tier` of `omega_verified`.

    Covers a keyword argument, a dict literal entry, an annotated-assignment
    default, a plain attribute assignment (`self.trust_tier = ...`), a
    subscript assignment (`entry["trust_tier"] = ...`), an augmented
    assignment to either target shape, and a positional call argument
    (`setattr(obj, "trust_tier", "omega_verified")` /
    `object.__setattr__(self, "trust_tier", "omega_verified")` -- the shape
    already used, with a validated variable rather than a literal, at
    `plugin_platform/local_trust.py`'s own `object.__setattr__` calls).
    """

    base = ROOT if root is None else root
    offenders: list[str] = []
    for path in production_source_paths(root):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(base).as_posix()
        if relative in _OMEGA_VERIFIED_SANCTIONED_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "trust_tier"
                and _is_omega_verified_literal(node.value)
            ):
                offenders.append(f"{relative}:{node.value.lineno}")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "trust_tier"
                        and _is_omega_verified_literal(value)
                    ):
                        offenders.append(f"{relative}:{value.lineno}")
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "trust_tier"
                and node.value is not None
                and _is_omega_verified_literal(node.value)
            ):
                offenders.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Assign):
                if any(
                    _assignment_targets_trust_tier(target) for target in node.targets
                ) and _is_omega_verified_literal(node.value):
                    offenders.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.AugAssign):
                if _assignment_targets_trust_tier(node.target) and _is_omega_verified_literal(
                    node.value
                ):
                    offenders.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Call):
                positional = node.args
                for index, argument in enumerate(positional):
                    if _is_trust_tier_key(argument) and any(
                        _is_omega_verified_literal(later) for later in positional[index + 1 :]
                    ):
                        offenders.append(f"{relative}:{node.lineno}")
                        break
    return tuple(offenders)


def test_no_code_path_assigns_omega_verified_from_publisher_metadata() -> None:
    """Catches a trust_tier construction outside the closed, signature-gated site set."""

    assert _omega_verified_assignment_offenders() == ()


@pytest.mark.parametrize(
    ("filename", "source", "expected_line"),
    [
        pytest.param(
            "rogue_keyword.py",
            "def build():\n    return Entry(trust_tier='omega_verified')\n",
            2,
            id="keyword",
        ),
        pytest.param(
            "rogue_dict.py",
            "def build():\n    return {'trust_tier': 'omega_verified'}\n",
            2,
            id="dict",
        ),
        pytest.param(
            "rogue_annassign.py",
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\n"
            "class RogueEntry:\n"
            "    trust_tier: str = 'omega_verified'\n",
            6,
            id="annassign-dataclass-default",
        ),
        pytest.param(
            "rogue_subscript.py",
            "def widen(entry):\n    entry['trust_tier'] = 'omega_verified'\n",
            2,
            id="subscript-assign",
        ),
        pytest.param(
            "rogue_attribute.py",
            "class Rogue:\n    def widen(self):\n        self.trust_tier = 'omega_verified'\n",
            3,
            id="attribute-assign",
        ),
        pytest.param(
            "rogue_setattr.py",
            "def widen(entry):\n    object.__setattr__(entry, 'trust_tier', 'omega_verified')\n",
            2,
            id="positional-setattr",
        ),
    ],
)
def test_omega_verified_assignment_gate_rejects_every_planted_shape(
    filename: str,
    source: str,
    expected_line: int,
    tmp_path: Path,
) -> None:
    """Catches a scan that misses any one of the ways to construct omega_verified."""

    (tmp_path / "mcp_platform").mkdir()
    (tmp_path / "mcp_platform" / filename).write_text(source, encoding="utf-8")

    offenders = _omega_verified_assignment_offenders(tmp_path)

    assert offenders == (f"mcp_platform/{filename}:{expected_line}",)


# --------------------------------------------------------------------------
# The registry/publisher-facing modules themselves get a stricter, whole-file
# scan: ANY omega_verified reference at all (a literal string, or a
# `TrustTier.OMEGA_VERIFIED` attribute reference), not just the assignment
# shapes above -- these are the modules a publisher-authored registry listing
# actually flows through before a plugin is ever installed, so even a
# comparison-shaped reference deserves an explicit, reasoned entry rather
# than silent trust.
# --------------------------------------------------------------------------

_STRICT_OMEGA_VERIFIED_FILES = frozenset(
    {
        "plugin_platform/local_trust.py",
        "plugin_platform/marketplace_importer.py",
        "plugin_platform/mcp_registry.py",
        "mcp_platform/plugin_discovery.py",
    }
)

#: Every exact, reasoned exception to the strict per-file scan below. Each is
#: a defensive READ-time check that a value can never equal OMEGA_VERIFIED --
#: never a construction, assignment, or grant of that tier.
_STRICT_OMEGA_VERIFIED_SANCTIONED_OCCURRENCES = frozenset(
    {
        # `mcp_registry.py`'s `McpRegistryServer.__post_init__`:
        #   if self.trust_tier is TrustTier.OMEGA_VERIFIED:
        #       raise McpRegistryError(_RECORD_INVALID)
        # Refuses (never grants) a registry listing that claims Omega's own
        # first-party tier. See the module docstring: "Nothing here may
        # therefore reach TrustTier.OMEGA_VERIFIED".
        ("plugin_platform/mcp_registry.py", 304),
    }
)


def _strict_omega_verified_offenders(root: Path | None = None) -> tuple[str, ...]:
    base = ROOT if root is None else root
    offenders: list[str] = []
    for path in production_source_paths(root):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(base).as_posix()
        if relative not in _STRICT_OMEGA_VERIFIED_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "omega_verified":
                lineno = node.lineno
            elif isinstance(node, ast.Attribute) and node.attr == "OMEGA_VERIFIED":
                lineno = node.lineno
            else:
                continue
            if (relative, lineno) not in _STRICT_OMEGA_VERIFIED_SANCTIONED_OCCURRENCES:
                offenders.append(f"{relative}:{lineno}")
    return tuple(offenders)


def test_no_unreasoned_omega_verified_reference_in_registry_facing_modules() -> None:
    """Catches ANY unreasoned omega_verified reference in the registry-facing modules.

    Broader than the assignment-shape scan above: this flags every literal
    and every `TrustTier.OMEGA_VERIFIED` reference in these four files,
    exempting only the exact, individually reasoned occurrences enumerated
    above.
    """

    assert _strict_omega_verified_offenders() == ()


def test_strict_omega_verified_gate_rejects_an_unreasoned_occurrence(tmp_path: Path) -> None:
    """Catches the strict per-file scan missing a fresh, unreasoned reference."""

    (tmp_path / "plugin_platform").mkdir()
    (tmp_path / "plugin_platform" / "mcp_registry.py").write_text(
        "TIER = 'omega_verified'\n",
        encoding="utf-8",
    )

    assert _strict_omega_verified_offenders(tmp_path) == ("plugin_platform/mcp_registry.py:1",)


def test_strict_omega_verified_gate_still_allows_the_one_enumerated_exception(
    tmp_path: Path,
) -> None:
    """Catches the enumerated exception itself becoming unreachable/unmatched."""

    (tmp_path / "plugin_platform").mkdir()
    (tmp_path / "plugin_platform" / "mcp_registry.py").write_text(
        "\n" * 303 + "if x is TrustTier.OMEGA_VERIFIED:\n    raise ValueError('rejected')\n",
        encoding="utf-8",
    )

    assert _strict_omega_verified_offenders(tmp_path) == ()


class _FakeAuthority:
    def __init__(
        self, *, connection_revision: str, revision: str, capability_ceiling: tuple[str, ...]
    ) -> None:
        self.connection_revision = connection_revision
        self.revision = revision
        # A fully consented connection: every signed tool left selected.
        self.capability_ceiling = capability_ceiling

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _FakeAuthority)
            and other.connection_revision == self.connection_revision
            and other.revision == self.revision
        )

    def __hash__(self) -> int:
        return hash((self.connection_revision, self.revision))


class _FakeActivation:
    def __init__(
        self,
        *,
        plugin_id: str,
        app_id: str,
        account_key_sha256: str,
        capability_revision: str,
        descriptor_revision: str,
        package_revision: str,
        connection_revision: str,
        capability_ceiling: tuple[str, ...],
    ) -> None:
        self.plugin_id = plugin_id
        self.app_id = app_id
        self.account_key_sha256 = account_key_sha256
        self.capability_revision = capability_revision
        self.descriptor_revision = descriptor_revision
        self.package_revision = package_revision
        self._authority = _FakeAuthority(
            connection_revision=connection_revision,
            revision=connection_revision,
            capability_ceiling=capability_ceiling,
        )

    def authority(self) -> _FakeAuthority:
        return self._authority


class _FakeGrant:
    """One durable connection grant, provider-neutral and account partitioned."""

    def __init__(self, activation: _FakeActivation) -> None:
        self.activation = activation
        self.authority = activation.authority()


class _FakeConnections:
    """A provider-neutral connection service that never touches a network."""

    def __init__(self, service: PluginLifecycleService) -> None:
        self._service = service
        self._grants: dict[tuple[str, str], _FakeGrant] = {}
        self._requests: dict[str, tuple[str, str]] = {}
        self.revoked: list[tuple[str, str]] = []
        self.invalidated: list[str] = []

    # -- exact signed inputs -------------------------------------------------
    def requirement(self, plugin_id: str, app_id: str) -> Any:
        manifest = self._service.plugin_registry.reverify_installed(plugin_id).manifest
        for app in (*manifest.required_apps, *manifest.optional_apps):
            if app.app_id == app_id:
                return app
        raise ValueError("plugin_app_not_installed")

    @staticmethod
    def _account_digest(account_key: str) -> str:
        return sha256(account_key.encode("utf-8")).hexdigest()

    def seat_connection(self, plugin_id: str, app_id: str, account_key_sha256: str) -> None:
        descriptor = self._service.descriptor_for_plugin_app(plugin_id, app_id)
        descriptor_revision = sha256_hex(canonical_bytes(descriptor.to_dict()))
        verified = self._service.plugin_registry.reverify_installed(plugin_id)
        connection_revision = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.test.fake-connection.v1",
                    "plugin_id": plugin_id,
                    "app_id": app_id,
                    "account_key_sha256": account_key_sha256,
                    "descriptor_revision": descriptor_revision,
                }
            )
        )
        self._grants[(plugin_id, app_id)] = _FakeGrant(
            _FakeActivation(
                plugin_id=plugin_id,
                app_id=app_id,
                account_key_sha256=account_key_sha256,
                capability_revision=descriptor.capability_revision,
                descriptor_revision=descriptor_revision,
                package_revision=verified.package_sha256,
                connection_revision=connection_revision,
                capability_ceiling=tuple(sorted(
                    f"tool:{name}"
                    for name in (descriptor.signed_capability_authority or {}).get("tools", {})
                )),
            )
        )

    # -- PluginGrantService surface used by planning authority ---------------
    def current(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> _FakeGrant | None:
        grant = self._grants.get((plugin_id, app_id))
        if grant is None or grant.activation.account_key_sha256 != account_key_sha256:
            return None
        return grant

    def lifecycle_revision(self) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.test.fake-grants.v1",
                    "connected": sorted(
                        f"{plugin_id}:{app_id}" for plugin_id, app_id in self._grants
                    ),
                }
            )
        )

    # -- PluginConnectionService surface -------------------------------------
    async def status(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
    ) -> ConnectionStatus:
        account = self._account_digest(account_key)
        grant = self.current(plugin_id, app_id=app_id, account_key_sha256=account)
        return ConnectionStatus(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=account,
            status="installed_disconnected" if grant is None else "connected",
            authorization_context_revision="b" * 64,
            token_revision=None if grant is None else 0,
            provider_scopes=self.requirement(plugin_id, app_id).authentication.scopes,
            execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
            authority_revision="" if grant is None else grant.authority.revision,
        )

    async def start(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        expected_registry_revision: str,
        scopes: tuple[str, ...] | None = None,
        capability_ceiling: tuple[str, ...] | None = None,
        **_unused: object,
    ) -> AuthorizationStart:
        del expected_registry_revision
        # Like the real service, a connection starts only under the operator's
        # explicit selection, and reports exactly that selection back.
        if scopes is None or capability_ceiling is None:
            raise ValueError("plugin_scopes_invalid")
        authentication = self.requirement(plugin_id, app_id).authentication
        flow = _OAUTH_FLOWS[authentication.kind]
        request_id = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.test.fake-request.v1",
                    "plugin_id": plugin_id,
                    "app_id": app_id,
                }
            )
        )
        self._requests[request_id] = (plugin_id, app_id)
        return AuthorizationStart(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=self._account_digest(account_key),
            status="authorizing",
            request_id=request_id,
            authorization_url=(
                None if flow == "device_code" else "https://authorize.invalid/authorize"
            ),
            expires_at=None,
            authorization_context_revision="b" * 64,
            provider_scopes=tuple(scopes),
            execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
            capability_ceiling=tuple(capability_ceiling),
            authorization_flow=flow,
            user_code=None if flow != "device_code" else "ABCD-EFGH",
            verification_uri=(
                None if flow != "device_code" else "https://verify.invalid/device"
            ),
            poll_interval_seconds=None if flow != "device_code" else 5,
        )

    def callback_request_id(self, callback_uri: str) -> str:
        return callback_uri.rsplit("state=", 1)[1]

    async def complete_callback(self, callback_uri: str) -> ConnectionStatus:
        return await self._complete(self.callback_request_id(callback_uri))

    async def complete_device(self, request_id: str) -> ConnectionStatus:
        return await self._complete(request_id)

    async def _complete(self, request_id: str) -> ConnectionStatus:
        plugin_id, app_id = self._requests[request_id]
        account = self._account_digest(_ACCOUNT_KEY)
        self.seat_connection(plugin_id, app_id, account)
        return await self.status(plugin_id, app_id=app_id, account_key=_ACCOUNT_KEY)

    async def revoke(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        expected_registry_revision: str,
    ) -> None:
        del account_key, expected_registry_revision
        if self._grants.pop((plugin_id, app_id), None) is not None:
            self.revoked.append((plugin_id, app_id))

    async def invalidate_execution(self, plugin_id: str, **_unused: object) -> None:
        self.invalidated.append(plugin_id)


def _attach_fake_connections(service: PluginLifecycleService) -> _FakeConnections:
    """Seat one provider-neutral connection layer under the real coordinator."""

    connections = _FakeConnections(service)
    service._connection_service = connections
    service._connection_coordinator = PluginConnectionCoordinator(
        plugin_registry=service.plugin_registry,
        connection_service=connections,
    )
    service.plugin_grants = connections
    return connections


def seat_connected_plugin(
    service: PluginLifecycleService,
    plugin_id: str,
) -> _FakeConnections:
    """Move one installed plugin to a connected, planner-eligible state.

    Only installed *and currently authorized* plugins reach the runtime catalog,
    so a fixture that merely enables an installed record has no planning
    authority and compiles no candidate.
    """

    connections = _attach_fake_connections(service)
    account = sha256(service.operator_account_key().encode("utf-8")).hexdigest()
    for app_id in service.app_ids_for_plugin(plugin_id):
        connections.seat_connection(plugin_id, app_id, account)
    service._set_enabled(plugin_id, True)
    return connections


def _plugin_bindings(service: PluginLifecycleService, plugin_id: str) -> tuple[Any, ...]:
    return tuple(
        item
        for item in compile_plugin_tool_bindings(
            plugin_snapshot=service.plugin_registry.snapshot(),
            app_snapshot=service.app_registry.snapshot(),
            descriptor_for_app=service.descriptor_for_plugin_app,
            account_key_hash=sha256(_ACCOUNT_KEY.encode("utf-8")).hexdigest(),
            app_authority_provider=service.planning_app_authority,
        )
        if item.plugin_id == plugin_id
    )


@pytest.mark.anyio
@pytest.mark.parametrize("plugin_id", CURRENT_PACKAGES)
async def test_every_current_package_uses_only_the_universal_lifecycle(
    plugin_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install, set up, connect, compile, try in chat, disconnect, uninstall."""

    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", _ACCOUNT_KEY)
    service = PluginLifecycleService(state_root=tmp_path)
    try:
        catalog = {item.plugin_id for item in service.catalog_source().snapshot().plugins}
        assert set(CURRENT_PACKAGES) <= catalog

        review = service.review(plugin_id)
        installed = service.install(plugin_id, confirmation=review.sha256)
        assert installed.record.plugin_id == plugin_id
        assert installed.review_sha256 == review.sha256
        version = service.plugin_registry.get(plugin_id).selected_version

        connections = _attach_fake_connections(service)
        app_ids = service.app_ids_for_plugin(plugin_id)
        assert len(app_ids) == 1
        app_id = app_ids[0]

        assert (
            await service.catalog_state(plugin_id, catalog_version=version)
            == PluginLifecycleState.SETUP_REQUIRED.value
        )

        selection = await service.permissions(plugin_id)
        started = await service.start_connection(
            plugin_id,
            settings=full_permission_settings(selection),
        )
        assert started.status == "connecting"
        assert started.request_id is not None
        authentication = connections.requirement(plugin_id, app_id).authentication
        assert started.authorization_flow == _OAUTH_FLOWS[authentication.kind]

        if started.authorization_flow == "device_code":
            snapshot = await service.poll_connection(started.request_id)
        else:
            snapshot = await service.complete_connection(
                f"https://omega2ai.com/oauth/callback?code=x&state={started.request_id}"
            )
        assert snapshot.state is PluginLifecycleState.CONNECTED
        assert service.plugin_registry.get(plugin_id).enabled is True
        assert connections.invalidated == []

        bindings = _plugin_bindings(service, plugin_id)
        assert bindings
        assert {item.app_id for item in bindings} == {app_id}

        handoff = service.try_in_chat(plugin_id)
        assert handoff.plugin_id == plugin_id
        assert handoff.title
        assert handoff.intro_text.startswith(f"{handoff.title} is connected.")
        introduced = {
            line[2:].rstrip(".")
            for line in handoff.intro_text.splitlines()
            if line.startswith("- ")
        }
        assert {item.title.rstrip(".") for item in bindings} <= introduced

        disconnected = await service.disconnect(plugin_id)
        assert disconnected.state is PluginLifecycleState.SETUP_REQUIRED
        assert connections.revoked == [(plugin_id, app_id)]
        assert service.plugin_registry.get(plugin_id).enabled is False
        assert _plugin_bindings(service, plugin_id) == ()

        await service.uninstall(plugin_id)
        assert service.app_ids_for_plugin(plugin_id) == ()
        assert (
            await service.catalog_state(plugin_id, catalog_version=version)
            == PluginLifecycleState.AVAILABLE.value
        )
    finally:
        service.close()
