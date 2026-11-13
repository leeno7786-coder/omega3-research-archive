"""Static architecture contracts for the provider-neutral plugin foundation."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from plugin_platform.catalog import TrustTier
from plugin_platform.local_trust import LocalTrustError, LocalTrustRoot
from plugin_platform.marketplace_importer import (
    _VERIFICATION_SEAL,
    NormalizedPluginCandidate,
    VerifiedNormalizedPluginCandidate,
)


_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugin_platform"
_EMBEDDED_CATALOG_ROOT = _PLUGIN_ROOT / "marketplace"
_EMBEDDED_PACKAGES_ROOT = _EMBEDDED_CATALOG_ROOT / "packages"
_CURRENT_PACKAGES = (
    "github.official",
    "google.workspace.calendar",
    "google.workspace.drive",
    "google.workspace.gmail",
    "slack.official",
)
_RETIRED_FOUNDATION_SYMBOLS = (
    "FeaturedPluginService",
    "get_default_featured_plugin_service",
    "_FEATURED_PACKAGES",
    "resolve_authoritative_connector",
    "GOOGLE_DRIVE_SCOPES",
    "featured_plugin_",
)
_ALLOWED_ASSET_KINDS = frozenset({"skill", "workflow", "icon", "capability_authority"})
_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "anthropic",
        "authlib",
        "asyncssh",
        "asyncio",
        "auto_loop",
        "builtins",
        "chat",
        "connectors",
        "ftplib",
        "global_loop",
        "google",
        "grpc",
        "httpx",
        "httpx2",
        "hud",
        "imaplib",
        "importlib",
        "keyring",
        "langchain",
        "litellm",
        "marshal",
        "meta_loop",
        "multiprocessing",
        "oauthlib",
        "ollama",
        "omega_loop",
        "openai",
        "pexpect",
        "pickle",
        "poplib",
        "pressure_loop",
        "pty",
        "requests",
        "runpy",
        "runtime",
        "smtplib",
        "socket",
        "subprocess",
        "telnetlib",
        "torch",
        "transformers",
        "urllib3",
        "websocket",
        "websockets",
    }
)
_FOUNDATION_FILESYSTEM_COMPAT_MODULE = "runtime.pinned_filesystem"
_FORBIDDEN_IMPORT_PARTS = frozenset(
    {
        "chat",
        "credential",
        "credentials",
        "http_client",
        "model_client",
        "oauth",
        "provider_connector",
        "vault",
    }
)
_FORBIDDEN_DIRECT_CALLS = frozenset({"__import__", "compile", "eval", "exec"})
_FORBIDDEN_IMPORT_PATHS = frozenset({"http.client", "urllib.request"})
_FORBIDDEN_CALL_ROOTS = frozenset(
    {
        "aiohttp",
        "http",
        "httpx",
        "importlib",
        "multiprocessing",
        "requests",
        "socket",
        "subprocess",
        "urllib3",
    }
)
_NETWORK_CALL_ROOTS = frozenset(
    {
        "aiohttp",
        "asyncio",
        "grpc",
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "urllib3",
        "websocket",
        "websockets",
    }
)
_CROSS_LAYER_ROOTS = frozenset({"core", "mcp_platform"})
_CROSS_LAYER_ALLOWED_MODULES = frozenset({"core.paths", "mcp_platform.canonical"})
_BUILTINS_MAPPINGS = frozenset({"__builtins__", "builtins.__dict__"})
_DYNAMIC_ATTRIBUTE = "<dynamic>"
_AliasBindings = dict[str, frozenset[str]]


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    if not (
        isinstance(node, ast.Call)
        and not node.keywords
        and len(node.args) == 1
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        return None
    separator = _constant_string(node.func.value)
    items = tuple(_constant_string(item) for item in node.args[0].elts)
    if separator is None or any(item is None for item in items):
        return None
    return separator.join(item for item in items if item is not None)


def _resolved_symbol_names(node: ast.AST | None, aliases: _AliasBindings) -> frozenset[str]:
    if node is None:
        return frozenset()
    dotted = _dotted_name(node)
    if dotted is not None:
        head, separator, tail = dotted.partition(".")
        canonical = aliases.get(head, frozenset({head}))
        suffix = separator + tail if separator else ""
        return frozenset(name + suffix for name in canonical)
    if isinstance(node, ast.Subscript):
        owners = _resolved_symbol_names(node.value, aliases)
        if not owners.intersection(_BUILTINS_MAPPINGS):
            return frozenset()
        attribute = _constant_string(node.slice)
        return frozenset({f"builtins.{attribute or _DYNAMIC_ATTRIBUTE}"})
    if not isinstance(node, ast.Call):
        return frozenset()
    getters = _resolved_symbol_names(node.func, aliases)
    if getters.intersection(f"{mapping}.get" for mapping in _BUILTINS_MAPPINGS) and node.args:
        attribute = _constant_string(node.args[0])
        return frozenset({f"builtins.{attribute or _DYNAMIC_ATTRIBUTE}"})
    if len(node.args) not in {2, 3} or node.keywords:
        return frozenset()
    if not getters.intersection({"getattr", "builtins.getattr"}):
        return frozenset()
    owners = _resolved_symbol_names(node.args[0], aliases)
    if not owners:
        return frozenset()
    attribute = _constant_string(node.args[1]) or _DYNAMIC_ATTRIBUTE
    return frozenset(f"{owner}.{attribute}" for owner in owners)


def _forbidden_import(name: str) -> bool:
    parts = tuple(part.casefold() for part in name.split("."))
    return bool(parts) and (
        parts[0] in _FORBIDDEN_IMPORT_ROOTS
        or ".".join(parts) in _FORBIDDEN_IMPORT_PATHS
        or any(part in _FORBIDDEN_IMPORT_PARTS for part in parts)
    )


def _cross_layer_import_allowed(module: str, imported_name: str | None = None) -> bool:
    normalized_module = module.casefold()
    if imported_name is None:
        return normalized_module in _CROSS_LAYER_ALLOWED_MODULES
    if normalized_module in _CROSS_LAYER_ALLOWED_MODULES:
        return True
    return f"{normalized_module}.{imported_name.casefold()}" in _CROSS_LAYER_ALLOWED_MODULES


def _forbidden_call(name: str) -> bool:
    parts = tuple(part.casefold() for part in name.split("."))
    leaf = parts[-1]
    root = parts[0]
    if name in _FORBIDDEN_DIRECT_CALLS:
        return True
    if root == "builtins" and leaf in {*_FORBIDDEN_DIRECT_CALLS, _DYNAMIC_ATTRIBUTE}:
        return True
    if (
        leaf in {"popen", "startfile", "system"}
        or leaf.startswith(("createprocess", "fork", "posix_spawn", "spawn"))
        or leaf == "exec"
        or leaf.startswith("exec_")
        or (leaf.startswith("exec") and leaf[4:] and set(leaf[4:]).issubset({"e", "l", "p", "v"}))
        or "subprocess" in leaf
    ):
        return True
    if root in _FORBIDDEN_CALL_ROOTS:
        return True
    return root in _NETWORK_CALL_ROOTS and (
        leaf in {"accept", "bind", "connect", "connect_ex", "listen", "serve"}
        or "connection" in leaf
        or leaf == "server"
        or leaf.endswith("_server")
        or leaf.startswith("server_")
    )


def _has_reachable_bounded_exit(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, (ast.Break, ast.Raise, ast.Return)):
            return True
        if isinstance(statement, ast.Continue):
            return False
        if isinstance(statement, ast.If) and statement.orelse:
            if _has_reachable_bounded_exit(statement.body) and _has_reachable_bounded_exit(
                statement.orelse
            ):
                return True
    return False


def _is_deadline_expired_test(node: ast.AST, aliases: _AliasBindings) -> bool:
    return bool(
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.GtE)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == "deadline"
        and isinstance(node.left, ast.Call)
        and _resolved_symbol_names(node.left.func, aliases) == frozenset({"time.monotonic"})
    )


def _has_loop_level_continue(node: ast.AST) -> bool:
    if isinstance(node, ast.Continue):
        return True
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(_has_loop_level_continue(child) for child in ast.iter_child_nodes(node))


def _deadline_guarded(loop: ast.While, aliases: _AliasBindings) -> bool:
    for candidate in loop.body:
        if isinstance(candidate, (ast.Break, ast.Continue, ast.Raise, ast.Return)):
            return False
        if _has_loop_level_continue(candidate):
            return False
        if not isinstance(candidate, ast.If):
            continue
        if _is_deadline_expired_test(candidate.test, aliases) and _has_reachable_bounded_exit(
            candidate.body
        ):
            return True
    return False


def _asset_kind_contracts(
    tree: ast.Module,
) -> tuple[tuple[frozenset[str], bool, bool], ...]:
    contracts: list[tuple[frozenset[str], bool, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if any(isinstance(target, ast.Name) and target.id == "AssetKind" for target in targets):
                contracts.append((frozenset(), True, False))
            continue
        if not isinstance(node, ast.ClassDef) or node.name != "AssetKind":
            continue
        values: set[str] = set()
        has_nonliteral_member = False
        for statement in node.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                values.add(statement.value.value)
            else:
                has_nonliteral_member = True
        inherits_str_enum = any(
            (base_name := _dotted_name(base)) is not None
            and base_name.rsplit(".", 1)[-1] == "StrEnum"
            for base in node.bases
        )
        contracts.append((frozenset(values), has_nonliteral_member, inherits_str_enum))
    return tuple(contracts)


def _target_names(node: ast.AST | None) -> frozenset[str]:
    if isinstance(node, ast.Name):
        return frozenset({node.id})
    if isinstance(node, (ast.List, ast.Tuple)):
        return frozenset().union(*(_target_names(item) for item in node.elts))
    return frozenset()


class _ArchitectureScanner(ast.NodeVisitor):
    def __init__(self, *, filename: str = "") -> None:
        self.filename = filename
        self.aliases: _AliasBindings = {}
        self.violations: list[str] = []

    def _visit_scoped(
        self,
        nodes: list[ast.stmt],
        *,
        unbound_names: frozenset[str] = frozenset(),
        initial_aliases: _AliasBindings | None = None,
    ) -> _AliasBindings:
        outer_aliases = self.aliases
        starting_aliases = self.aliases if initial_aliases is None else initial_aliases
        self.aliases = starting_aliases.copy()
        for name in unbound_names:
            self.aliases.pop(name, None)
        try:
            for node in nodes:
                self.visit(node)
            return self.aliases.copy()
        finally:
            self.aliases = outer_aliases

    @staticmethod
    def _possible_binding_union(*states: _AliasBindings) -> _AliasBindings:
        merged: _AliasBindings = {}
        for state in states:
            for name, bindings in state.items():
                merged[name] = merged.get(name, frozenset()).union(bindings)
        return merged

    def _bind(self, target: ast.AST, value: ast.AST | None) -> None:
        resolved = _resolved_symbol_names(value, self.aliases)
        for name in _target_names(target):
            if not resolved:
                self.aliases.pop(name, None)
            else:
                self.aliases[name] = resolved

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.partition(".")[0].casefold()
            compatibility_import = (
                alias.name == _FOUNDATION_FILESYSTEM_COMPAT_MODULE
                and self.filename.replace("\\", "/").endswith("plugin_platform/filesystem.py")
            )
            if (
                (not compatibility_import and _forbidden_import(alias.name))
                or (
                    alias.name == "plugin_platform"
                    or root == "plugin_platform"
                    and alias.name == "plugin_platform"
                )
                or (root in _CROSS_LAYER_ROOTS and not _cross_layer_import_allowed(alias.name))
            ):
                self.violations.append(f"forbidden import {alias.name}")
            self.aliases[alias.asname or alias.name.split(".", 1)[0]] = frozenset({alias.name})

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.partition(".")[0].casefold()
        targets = tuple(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
        compatibility_import = (
            module == "runtime"
            and len(node.names) == 1
            and node.names[0].name == "pinned_filesystem"
            and self.filename.replace("\\", "/").endswith("plugin_platform/filesystem.py")
        )
        if compatibility_import:
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = frozenset({f"{module}.{alias.name}"})
            return
        if root == "plugin_platform" and module != "plugin_platform":
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = frozenset(
                    {f"{module}.{alias.name}" if module else alias.name}
                )
            return
        if root in _CROSS_LAYER_ROOTS:
            for alias, target in zip(node.names, targets, strict=True):
                if not _cross_layer_import_allowed(module, alias.name):
                    self.violations.append(f"forbidden import {target}")
        else:
            for target in (module, *targets):
                if _forbidden_import(target):
                    self.violations.append(f"forbidden import {target}")
                elif _forbidden_call(target):
                    self.violations.append(f"forbidden call import {target}")
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = frozenset(
                {f"{module}.{alias.name}" if module else alias.name}
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self._bind(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._bind(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind(node.target, node.value)

    def visit_Call(self, node: ast.Call) -> None:
        for call_name in _resolved_symbol_names(node.func, self.aliases):
            if _forbidden_call(call_name):
                self.violations.append(f"forbidden call {call_name}")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        if (
            isinstance(node.test, ast.Constant)
            and node.test.value is True
            and not _deadline_guarded(node, self.aliases)
        ):
            self.violations.append("unbounded while True")
        self.visit(node.test)
        self._visit_scoped(node.body)
        self._visit_scoped(node.orelse)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        initial_aliases = self.aliases.copy()
        body_aliases = self._visit_scoped(node.body, initial_aliases=initial_aliases)
        orelse_aliases = self._visit_scoped(node.orelse, initial_aliases=initial_aliases)
        self.aliases = self._possible_binding_union(body_aliases, orelse_aliases)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._visit_scoped(node.body, unbound_names=_target_names(node.target))
        self._visit_scoped(node.orelse)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        unbound_names: frozenset[str] = frozenset()
        for item in node.items:
            self.visit(item.context_expr)
            unbound_names |= _target_names(item.optional_vars)
        self._visit_scoped(node.body, unbound_names=unbound_names)

    visit_AsyncWith = visit_With

    def visit_Try(self, node: ast.Try) -> None:
        initial_aliases = self.aliases.copy()
        body_aliases = self._visit_scoped(node.body, initial_aliases=initial_aliases)
        normal_aliases = self._visit_scoped(node.orelse, initial_aliases=body_aliases)
        possible_states = [normal_aliases]
        for handler in node.handlers:
            if handler.type is not None:
                self.visit(handler.type)
            unbound = frozenset({handler.name}) if handler.name is not None else frozenset()
            possible_states.append(
                self._visit_scoped(
                    handler.body,
                    unbound_names=unbound,
                    initial_aliases=initial_aliases,
                )
            )
        self.aliases = self._possible_binding_union(*possible_states)
        for statement in node.finalbody:
            self.visit(statement)

    visit_TryStar = visit_Try

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        argument_names = frozenset(
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        )
        if node.args.vararg is not None:
            argument_names |= {node.args.vararg.arg}
        if node.args.kwarg is not None:
            argument_names |= {node.args.kwarg.arg}
        self._visit_scoped(node.body, unbound_names=argument_names)
        self.aliases.pop(node.name, None)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._visit_scoped(node.body)
        self.aliases.pop(node.name, None)


def _architecture_violations(source: str, *, filename: str) -> tuple[str, ...]:
    tree = ast.parse(source, filename=filename)
    scanner = _ArchitectureScanner(filename=filename)
    scanner.visit(tree)
    for asset_kinds, has_nonliteral_member, inherits_str_enum in _asset_kind_contracts(tree):
        if has_nonliteral_member or not inherits_str_enum or asset_kinds != _ALLOWED_ASSET_KINDS:
            scanner.violations.append(f"executable or unknown asset kinds {sorted(asset_kinds)}")
    return tuple(scanner.violations)


def _has_canonical_asset_kind(source: str, *, filename: str) -> bool:
    tree = ast.parse(source, filename=filename)
    return (
        sum(
            not has_nonliteral_member and inherits_str_enum and asset_kinds == _ALLOWED_ASSET_KINDS
            for asset_kinds, has_nonliteral_member, inherits_str_enum in _asset_kind_contracts(tree)
        )
        == 1
    )


def test_plugin_foundation_has_no_execution_or_product_layer_backdoors() -> None:
    """Catches a production import, call, asset, or loop that crosses foundation scope."""
    modules = tuple(sorted(path for path in _PLUGIN_ROOT.rglob("*.py") if path.is_file()))
    assert modules
    sources = {path: path.read_text(encoding="utf-8") for path in modules}
    violations = {
        path.relative_to(_PLUGIN_ROOT).as_posix(): findings
        for path, source in sources.items()
        if (
            findings := _architecture_violations(
                source,
                filename=str(path),
            )
        )
    }
    canonical_asset_kind_modules = tuple(
        path.relative_to(_PLUGIN_ROOT).as_posix()
        for path, source in sources.items()
        if _has_canonical_asset_kind(source, filename=str(path))
    )
    if canonical_asset_kind_modules != ("contracts.py",):
        violations["<asset-kind-contract>"] = (
            f"canonical AssetKind definitions {canonical_asset_kind_modules}",
        )
    assert violations == {}


@pytest.mark.parametrize(
    "mutation",
    [
        "import hud\n",
        "from pressure_loop import loop\n",
        "import plugin_platform\n",
        "import runtime.model_client\n",
        "import requests\nrequests.get('https://example.test')\n",
        pytest.param(
            "import httpx2\n",
            id="forbidden-httpx2-import",
        ),
        pytest.param(
            "from httpx2 import AsyncClient\nAsyncClient()\n",
            id="forbidden-httpx2-async-client-import",
        ),
        "from urllib import request\nrequest.urlopen('https://example.test')\n",
        "import subprocess\nsubprocess.Popen(['plugin'])\n",
        "import oauthlib\n",
        "eval('plugin')\n",
        "import builtins\nbuiltins.eval('plugin')\n",
        "from builtins import eval as evaluate\nevaluate('plugin')\n",
        "import os\nos.system('plugin')\n",
        "import os as host\nhost.system('plugin')\n",
        "from os import system as launch\nlaunch('plugin')\n",
        pytest.param(
            "import builtins\ngetattr(builtins, 'eval')('plugin')\n",
            id="constant-getattr-builtins-eval",
        ),
        pytest.param(
            "import builtins as safe\nevaluate = getattr(safe, 'eval')\nevaluate('plugin')\n",
            id="aliased-constant-getattr-builtins-eval",
        ),
        pytest.param(
            "import asyncio as event_loop\nevent_loop.open_connection('example.test', 443)\n",
            id="aliased-asyncio-open-connection",
        ),
        pytest.param(
            "from asyncio import open_connection as connect\nconnect('example.test', 443)\n",
            id="imported-asyncio-open-connection",
        ),
        pytest.param(
            "import os as host\nhost.posix_spawn('plugin', ['plugin'], {})\n",
            id="aliased-os-posix-spawn",
        ),
        pytest.param(
            "from os import posix_spawn as launch\nlaunch('plugin', ['plugin'], {})\n",
            id="imported-os-posix-spawn",
        ),
        pytest.param(
            "import ctypes as native\nnative.windll.kernel32.CreateProcessW()\n",
            id="aliased-ctypes-create-process-w",
        ),
        pytest.param(
            "from ctypes import windll as native\nnative.kernel32.CreateProcessW()\n",
            id="imported-ctypes-create-process-w",
        ),
        pytest.param(
            "import ctypes\nkernel32 = ctypes.WinDLL('kernel32')\nkernel32.CreateProcessW()\n",
            id="stored-ctypes-windll-create-process-w",
        ),
        pytest.param(
            "import ctypes\nctypes.windll.kernel32.CreateProcessA()\n",
            id="ctypes-create-process-a-family",
        ),
        pytest.param(
            "import os\nos.posix_spawnp('plugin', ['plugin'], {})\n",
            id="os-posix-spawnp-family",
        ),
        pytest.param(
            "import asyncio\nasyncio.start_server(handler, '127.0.0.1', 0)\n",
            id="asyncio-start-server-family",
        ),
        pytest.param(
            "import asyncio as transport\n"
            "transport.open_connection('example.test', 443)\n"
            "import safe_transport as transport\n",
            id="earlier-call-before-later-alias-rebinding",
        ),
        pytest.param(
            "if enabled:\n"
            "    import asyncio as transport\n"
            "transport.open_connection('example.test', 443)\n",
            id="conditional-import-alias-used-after-branch",
        ),
        pytest.param(
            "try:\n"
            "    import asyncio as transport\n"
            "except ImportError:\n"
            "    pass\n"
            "transport.open_connection('example.test', 443)\n",
            id="try-import-alias-used-after-branch",
        ),
        pytest.param(
            "for item in items:\n"
            "    import asyncio as transport\n"
            "transport.open_connection('example.test', 443)\n",
            id="for-import-alias-used-after-loop",
        ),
        pytest.param(
            "with manager:\n"
            "    import asyncio as transport\n"
            "transport.open_connection('example.test', 443)\n",
            id="with-import-alias-used-after-context",
        ),
        pytest.param(
            "while running:\n"
            "    import asyncio as transport\n"
            "transport.open_connection('example.test', 443)\n",
            id="nonconstant-while-import-alias-used-after-loop",
        ),
        pytest.param(
            "match value:\n"
            "    case 0:\n"
            "        import asyncio as transport\n"
            "    case _:\n"
            "        import safe_transport as transport\n"
            "transport.open_connection('example.test', 443)\n",
            id="match-import-alias-overwritten-by-later-case",
        ),
        "while True:\n    pass\n",
        pytest.param(
            "while True:\n"
            "    continue\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise TimeoutError\n",
            id="deadline-guard-unreachable-after-continue",
        ),
        pytest.param(
            "while True:\n"
            "    if time.monotonic() >= deadline:\n"
            "        continue\n"
            "        raise TimeoutError\n",
            id="deadline-exit-unreachable-after-continue",
        ),
        pytest.param(
            "while True:\n"
            "    def unused_guard():\n"
            "        if time.monotonic() >= deadline:\n"
            "            raise TimeoutError\n"
            "    work()\n",
            id="deadline-guard-only-in-uncalled-nested-function",
        ),
        pytest.param(
            "while True:\n    if time.monotonic() < deadline:\n        raise TimeoutError\n",
            id="nonexpiring-less-than-deadline-guard",
        ),
        pytest.param(
            "while True:\n"
            "    if time.monotonic() >= deadline and False:\n"
            "        raise TimeoutError\n",
            id="unsatisfiable-deadline-guard",
        ),
        pytest.param(
            "while True:\n"
            "    if True:\n"
            "        continue\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise TimeoutError\n",
            id="unconditional-branch-continue-before-deadline-guard",
        ),
        "from enum import StrEnum\nclass AssetKind(StrEnum):\n    SKILL = 'skill'\n"
        "    WORKFLOW = 'workflow'\n    ICON = 'icon'\n    SCRIPT = 'script'\n",
        pytest.param(
            "from enum import StrEnum\n"
            "class AssetKind(StrEnum):\n"
            "    SKILL = 'skill'\n"
            "    WORKFLOW = 'workflow'\n"
            "    ICON = 'icon'\n"
            "    EXECUTABLE = 'exec' + 'utable'\n",
            id="nonliteral-executable-asset-kind",
        ),
        pytest.param(
            "import builtins\ngetattr(builtins, ''.join(['e', 'val']))('plugin')\n",
            id="computed-getattr-builtins-eval",
        ),
        pytest.param(
            "import builtins\nbuiltins.__dict__['eval']('plugin')\n",
            id="builtins-dict-eval",
        ),
        pytest.param(
            "builtins.__dict__.get('eval')('plugin')\n",
            id="builtins-dict-get-eval",
        ),
        pytest.param(
            "__builtins__['eval']('plugin')\n",
            id="dunder-builtins-subscript-eval",
        ),
        pytest.param(
            "__builtins__.get('eval')('plugin')\n",
            id="dunder-builtins-get-eval",
        ),
        pytest.param(
            "import builtins\n",
            id="standalone-builtins-import-removal-mutant",
        ),
        pytest.param(
            "from enum import StrEnum\n"
            "AssetKind = StrEnum(\n"
            "    'AssetKind',\n"
            "    {'SKILL': 'skill', 'WORKFLOW': 'workflow', "
            "'ICON': 'icon', 'EXECUTABLE': 'executable'},\n"
            ")\n",
            id="functional-executable-asset-kind",
        ),
        pytest.param(
            "from mcp_platform.transport import connect\nconnect()\n",
            id="forbidden-mcp-platform-wrapper",
        ),
        pytest.param(
            "from core.process import launch\nlaunch()\n",
            id="forbidden-core-wrapper",
        ),
    ],
)
def test_architecture_scanner_rejects_deliberate_forbidden_mutations(mutation: str) -> None:
    """Catches a scanner weakened so a known scope-escape mutation survives."""
    assert _architecture_violations(mutation, filename="mutation.py")


def test_architecture_scanner_allows_only_the_existing_bounded_lock_loop() -> None:
    """Catches treating any syntactic break as proof that a forever loop is bounded."""
    bounded = """
deadline = time.monotonic() + timeout
while True:
    if locked:
        break
    if time.monotonic() >= deadline:
        raise TimeoutError
"""
    assert _architecture_violations(bounded, filename="bounded.py") == ()


def test_plugin_foundation_gate_recurses_into_nested_subpackages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches replacing recursive module discovery with a top-level-only glob."""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "escape.py").write_text("import hud\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_PLUGIN_ROOT", tmp_path)

    with pytest.raises(AssertionError) as exc_info:
        test_plugin_foundation_has_no_execution_or_product_layer_backdoors()

    assert "nested/escape.py" in str(exc_info.value).replace("\\", "/")


def test_plugin_foundation_gate_requires_one_canonical_asset_kind_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches deleting the canonical class while leaving no enum to validate."""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "contracts.py").write_text(
        "from enum import StrEnum\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_PLUGIN_ROOT", tmp_path)

    with pytest.raises(AssertionError) as exc_info:
        test_plugin_foundation_has_no_execution_or_product_layer_backdoors()

    assert "canonical AssetKind" in str(exc_info.value)


def test_signed_embedded_catalog_names_exactly_the_current_packages() -> None:
    """Catches a hardcoded Featured package list surviving beside the catalog."""

    catalog = json.loads(
        (_EMBEDDED_CATALOG_ROOT / "catalog.json").read_text(encoding="utf-8")
    )
    entries = tuple(sorted(entry["plugin_id"] for entry in catalog["entries"]))
    directories = tuple(
        sorted(item.name for item in _EMBEDDED_PACKAGES_ROOT.iterdir() if item.is_dir())
    )

    # The invariant is that the catalog and the package tree agree -- not that
    # either matches a list written here. This test exists to catch a hardcoded
    # package list surviving beside the catalog, and it carried one: adding a
    # plugin failed it, on the very thing it was written to prevent.
    assert directories == entries
    assert entries, "the shipped catalog names no packages"


def _retired_foundation_offenders() -> tuple[str, ...]:
    offenders: list[str] = []
    for path in sorted(_PLUGIN_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for symbol in _RETIRED_FOUNDATION_SYMBOLS:
            if symbol in text:
                relative = path.relative_to(_PLUGIN_ROOT).as_posix()
                offenders.append(f"{relative}: {symbol}")
    return tuple(offenders)


def test_plugin_foundation_has_no_retired_featured_service_symbols() -> None:
    """Catches the retired Featured composition root returning to the foundation."""

    assert _retired_foundation_offenders() == ()


@pytest.mark.parametrize("symbol", _RETIRED_FOUNDATION_SYMBOLS)
def test_retired_featured_symbol_gate_rejects_a_planted_module(
    symbol: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a scan narrowed so a reintroduced Featured symbol survives."""

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "escape.py").write_text(f"VALUE = {symbol!r}\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_PLUGIN_ROOT", tmp_path)

    with pytest.raises(AssertionError):
        test_plugin_foundation_has_no_retired_featured_service_symbols()


def test_local_trust_seal_never_produces_a_tier_above_publisher_verified() -> None:
    """Catches ``LocalTrustRoot`` trusting a candidate's own claimed OMEGA_VERIFIED tier.

    ``MarketplaceImporter.verify`` already refuses a candidate that claims
    ``OMEGA_VERIFIED`` between ``normalize`` and ``verify`` (see
    ``tests/plugin_platform/test_marketplace_importer.py``'s
    ``test_verification_refuses_a_candidate_that_claims_omega_verification``).
    That is a *different* gate. This test reaches ``LocalTrustRoot`` directly
    -- using the importer's private verification seal to build a
    ``VerifiedNormalizedPluginCandidate`` the importer's own gate never
    reviewed -- so a defect in the importer's gate alone could never widen
    what the local trust seal is willing to sign. Every other candidate field
    is left unset: both entry points must reject on trust tier alone, before
    ever touching the manifest.
    """

    forged = VerifiedNormalizedPluginCandidate(
        NormalizedPluginCandidate(
            manifest=None,
            trust_tier=TrustTier.OMEGA_VERIFIED,
            distribution=None,
            source_evidence=None,
            artifact=None,
        ),
        _VERIFICATION_SEAL,
    )
    trust_root = LocalTrustRoot.from_vault_key(b"k" * 32)

    with pytest.raises(LocalTrustError) as seal_error:
        trust_root.seal_package(forged)
    assert str(seal_error.value) == "candidate_trust_tier_unsupported"

    with pytest.raises(LocalTrustError) as materialize_error:
        trust_root.materialize_sealed_package(forged, envelope=None, destination=Path("unused"))
    assert str(materialize_error.value) == "candidate_trust_tier_unsupported"
