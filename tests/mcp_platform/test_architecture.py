"""AST architecture rails for the provider-neutral MCP organ."""

from __future__ import annotations

import ast
import hashlib
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MCP_ROOT = ROOT / "mcp_platform"
TEST_ROOT = ROOT / "tests" / "mcp_platform"
# Build output is not source: portable staging trees intentionally contain a
# second copy of the product and would make repo-wide ownership scans report
# duplicate implementations.  The staged release has its own verifier tests.
# The bundled Linux interpreter carries its own site-packages (third-party
# code, like .venv-brain's), so it is not Omega source either.
_REPO_SKIPPED_DIRECTORIES = frozenset(
    {"tests", "docs", "node_modules", "__pycache__", "dist", "python-3.13-linux"}
)
_RETIRED_PROTOCOL = date(2025, 11, 25).isoformat()
_UNRESOLVED_DYNAMIC_IMPORT = "<unresolved>"
_WORK_GENERATING_CALLS = frozenset(
    {
        "call_tool",
        "read_resource",
        "enqueue_input",
        "ensure_session",
        "ignite_episode",
        "poll",
        "prepare_mcp_inference",
        "reconcile_package",
        "sync",
    }
)
_REMOTE_MCP_MODULES = (
    "mcp.client.sse",
    "mcp.client.websocket",
    "mcp.server.sse",
)
_PROVIDER_MODULE_PREFIXES = (
    "connectors.google",
    "connectors.microsoft",
    "azure",
    "google",
    "googleapiclient",
    "microsoft",
    "msal",
    "msgraph",
    "office365",
    "provider",
    "providers",
)
_FORBIDDEN_MCP_FEATURE_CALLS = frozenset(
    {
        "create_message",
        "initialize",
        "initialized",
        "list_roots",
        "list_tasks",
        "set_logging_level",
        "set_roots",
    }
)
_DIRECT_SDK_OPERATION_CALLS = frozenset(
    {
        "call_tool",
        "get_prompt",
        "list_prompts",
        "list_resource_templates",
        "list_resources",
        "list_tools",
        "listen",
        "read_resource",
        "send_request",
    }
)
_SDK_CLIENT_OWNER_PATHS = frozenset(
    {
        MCP_ROOT / "client.py",
        MCP_ROOT / "transports" / "stdio.py",
        MCP_ROOT / "transports" / "streamable_http.py",
    }
)
_PROCESS_SPAWN_APIS = frozenset(
    {
        "anyio.open_process",
        "anyio.run_process",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "mcp.client.stdio.stdio_client",
        "mcp.os.win32.utilities.create_windows_process",
        "os.fork",
        "os.forkpty",
        "os.popen",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.startfile",
        "os.system",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "subprocess.run",
        "trio.lowlevel.open_process",
        "trio.open_process",
        "trio.run_process",
    }
)
_NETWORK_APIS = frozenset(
    {
        "aiohttp.ClientSession",
        "httpx.AsyncClient",
        "httpx.Client",
        "httpx2.AsyncClient",
        "httpx2.Client",
        "mcp.client.streamable_http.streamable_http_client",
        "requests.Session",
        "requests.delete",
        "requests.get",
        "requests.head",
        "requests.patch",
        "requests.post",
        "requests.put",
        "socket.create_connection",
        "socket.socket",
        "urllib.request.urlopen",
    }
)
_ALTERNATE_NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "curl_cffi",
        "http.client",
        "httpcore",
        "httpx",
        "pycurl",
        "requests",
        "socket",
        "urllib.request",
        "urllib3",
        "websockets",
    }
)
_CLIENT_PROVISIONER_PATHS = frozenset(
    {
        MCP_ROOT / "authorization.py",
        MCP_ROOT / "client.py",
        MCP_ROOT / "provisioner.py",
        MCP_ROOT / "transports" / "stdio.py",
        MCP_ROOT / "transports" / "streamable_http.py",
    }
)
_FORBIDDEN_COUPLING_ROOTS = frozenset(
    {
        "auto_loop",
        "chat",
        "connectors",
        "deepsleep_loop",
        "global_loop",
        "guardian",
        "hud",
        "memory",
        "meta_loop",
        "model",
        "models",
        "omega_loop",
        "pressure_loop",
        "proactive",
        "runtime",
        "schedule",
        "scheduling",
    }
)


@cache
def _modules() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(MCP_ROOT.rglob("*.py"))
    ]


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _import_bindings(tree: ast.AST) -> dict[str, str]:
    """Map local import bindings to their canonical module or symbol names."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    root = alias.name.partition(".")[0]
                    bindings[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _resolved_import_name(node: ast.AST, bindings: dict[str, str]) -> str:
    """Resolve the root of a dotted call through Python import aliases."""
    name = _dotted_name(node)
    root, separator, remainder = name.partition(".")
    imported = bindings.get(root)
    if imported is None:
        return name
    return f"{imported}.{remainder}" if separator else imported


def _merge_values(
    current: frozenset[str] | None,
    incoming: frozenset[str] | None,
) -> frozenset[str] | None:
    if current is None or incoming is None:
        return None
    merged = current | incoming
    return merged if len(merged) <= 64 else None


def _merge_scalar_values(
    current: frozenset[object] | None,
    incoming: frozenset[object] | None,
) -> frozenset[object] | None:
    if current is None or incoming is None:
        return None
    merged = current | incoming
    return merged if len(merged) <= 64 else None


def _closed_scalar_values(
    node: ast.AST,
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
    scalars: dict[str, frozenset[object] | None] | None = None,
) -> frozenset[object] | None:
    if isinstance(node, ast.NamedExpr):
        return _closed_scalar_values(node.value, texts, sequences, scalars)
    if isinstance(node, ast.Constant) and type(node.value) in {str, int, float, bool}:
        return frozenset({node.value})
    if isinstance(node, ast.Name):
        if scalars is not None and node.id in scalars:
            return scalars[node.id]
        values = texts.get(node.id)
        return None if values is None else frozenset(values)
    if isinstance(node, ast.IfExp):
        if isinstance(node.test, ast.Constant):
            branch = node.body if bool(node.test.value) else node.orelse
            return _closed_scalar_values(branch, texts, sequences, scalars)
        return _merge_scalar_values(
            _closed_scalar_values(node.body, texts, sequences, scalars),
            _closed_scalar_values(node.orelse, texts, sequences, scalars),
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        values = _closed_scalar_values(node.operand, texts, sequences, scalars)
        if values is None or any(type(value) not in {int, float} for value in values):
            return None
        operation = (
            (lambda value: +value) if isinstance(node.op, ast.UAdd) else lambda value: -value
        )
        return frozenset(operation(value) for value in values)  # type: ignore[operator]
    return None


def _text_values(
    node: ast.AST,
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
    scalars: dict[str, frozenset[object] | None] | None = None,
) -> frozenset[str] | None:
    if isinstance(node, ast.NamedExpr):
        return _text_values(node.value, texts, sequences, scalars)
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return frozenset({node.value})
    if isinstance(node, ast.Name):
        return texts.get(node.id)
    if isinstance(node, ast.IfExp):
        if isinstance(node.test, ast.Constant):
            branch = node.body if bool(node.test.value) else node.orelse
            return _text_values(branch, texts, sequences, scalars)
        body = _text_values(node.body, texts, sequences, scalars)
        other = _text_values(node.orelse, texts, sequences, scalars)
        return _merge_values(body, other)
    if isinstance(node, ast.JoinedStr):
        return _joined_string_values(node, texts, sequences, scalars)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _text_values(node.left, texts, sequences, scalars)
        right = _text_values(node.right, texts, sequences, scalars)
        if left is None or right is None or len(left) * len(right) > 64:
            return None
        return frozenset(first + second for first in left for second in right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _percent_string_values(node, texts, sequences, scalars)
    if isinstance(node, ast.Subscript):
        values = _sequence_values(node.value, texts, sequences, scalars)
        if values is None:
            return None
        indexes = _closed_scalar_values(node.slice, texts, sequences, scalars)
        selected: tuple[frozenset[str] | None, ...]
        if indexes is not None and all(type(index) is int for index in indexes):
            try:
                selected = tuple(values[index] for index in indexes)  # type: ignore[index]
            except IndexError:
                return None
        else:
            selected = values
        if any(value is None for value in selected):
            return None
        return frozenset(item for value in selected if value is not None for item in value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
    ):
        separators = _text_values(node.func.value, texts, sequences, scalars)
        values = _sequence_values(node.args[0], texts, sequences, scalars)
        if separators is None or values is None or any(value is None for value in values):
            return None
        results = {""}
        for value in values:
            assert value is not None
            if len(results) * len(value) > 64:
                return None
            results = {
                (prefix + "\0" + item) if prefix else item for prefix in results for item in value
            }
        return frozenset(
            separator.join(result.split("\0")) for separator in separators for result in results
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return _format_method_values(node, texts, sequences, scalars)
    return None


def _sequence_values(
    node: ast.AST,
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
    scalars: dict[str, frozenset[object] | None] | None = None,
) -> tuple[frozenset[str] | None, ...] | None:
    if isinstance(node, ast.NamedExpr):
        return _sequence_values(node.value, texts, sequences, scalars)
    if isinstance(node, ast.Name):
        return sequences.get(node.id)
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values: list[frozenset[str] | None] = []
    for item in node.elts:
        if isinstance(item, ast.Starred):
            expanded = _sequence_values(item.value, texts, sequences, scalars)
            if expanded is None:
                return None
            values.extend(expanded)
        else:
            values.append(_text_values(item, texts, sequences, scalars))
    return tuple(values)


def _bounded_product(groups: list[frozenset[object]]) -> list[tuple[object, ...]] | None:
    products: list[tuple[object, ...]] = [()]
    for group in groups:
        if len(products) * len(group) > 64:
            return None
        products = [prefix + (value,) for prefix in products for value in group]
    return products


def _joined_string_values(
    node: ast.JoinedStr,
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
    scalars: dict[str, frozenset[object] | None] | None,
) -> frozenset[str] | None:
    parts: list[frozenset[str]] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and type(part.value) is str:
            parts.append(frozenset({part.value}))
            continue
        if not isinstance(part, ast.FormattedValue):
            return None
        values = _closed_scalar_values(part.value, texts, sequences, scalars)
        specs = (
            frozenset({""})
            if part.format_spec is None
            else _text_values(part.format_spec, texts, sequences, scalars)
        )
        if values is None or specs is None or len(values) * len(specs) > 64:
            return None
        rendered: set[str] = set()
        for value in values:
            converted = repr(value) if part.conversion == 114 else value
            converted = str(value) if part.conversion == 115 else converted
            converted = ascii(value) if part.conversion == 97 else converted
            for spec in specs:
                try:
                    rendered.add(format(converted, spec))
                except (TypeError, ValueError):
                    return None
        parts.append(frozenset(rendered))
    products = _bounded_product([frozenset(part) for part in parts])
    return None if products is None else frozenset("".join(product) for product in products)


def _format_method_values(
    node: ast.Call,
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
    scalars: dict[str, frozenset[object] | None] | None,
) -> frozenset[str] | None:
    assert isinstance(node.func, ast.Attribute)
    templates = _text_values(node.func.value, texts, sequences, scalars)
    positional = [
        _closed_scalar_values(argument, texts, sequences, scalars) for argument in node.args
    ]
    if templates is None or any(values is None for values in positional):
        return None
    if any(keyword.arg is None for keyword in node.keywords):
        return None
    keyword_names = [keyword.arg for keyword in node.keywords]
    keyword_values = [
        _closed_scalar_values(keyword.value, texts, sequences, scalars) for keyword in node.keywords
    ]
    if any(values is None for values in keyword_values):
        return None
    groups = [
        *(values for values in positional if values is not None),
        *(values for values in keyword_values if values is not None),
    ]
    products = _bounded_product(groups)
    if products is None or len(templates) * len(products) > 64:
        return None
    rendered: set[str] = set()
    positional_count = len(positional)
    for template in templates:
        for product in products:
            keywords = dict(zip(keyword_names, product[positional_count:], strict=True))
            try:
                rendered.add(template.format(*product[:positional_count], **keywords))
            except (IndexError, KeyError, TypeError, ValueError):
                return None
    return frozenset(rendered)


def _percent_string_values(
    node: ast.BinOp,
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
    scalars: dict[str, frozenset[object] | None] | None,
) -> frozenset[str] | None:
    templates = _text_values(node.left, texts, sequences, scalars)
    if templates is None:
        return None
    if isinstance(node.right, ast.Tuple):
        groups = [
            _closed_scalar_values(item, texts, sequences, scalars) for item in node.right.elts
        ]
        if any(values is None for values in groups):
            return None
        products = _bounded_product([values for values in groups if values is not None])
        operands: list[object] | None = None if products is None else list(products)
    else:
        values = _closed_scalar_values(node.right, texts, sequences, scalars)
        operands = None if values is None else list(values)
    if operands is None or len(templates) * len(operands) > 64:
        return None
    rendered: set[str] = set()
    for template in templates:
        for operand in operands:
            try:
                rendered.add(template % operand)
            except (TypeError, ValueError):
                return None
    return frozenset(rendered)


def _static_text(node: ast.AST) -> str | None:
    """Fold literal text compositions used by imports, getattr, and headers."""
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text(node.left)
        right = _static_text(node.right)
        return None if left is None or right is None else left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
    ):
        separator = _static_text(node.func.value)
        values = node.args[0]
        if separator is None or not isinstance(values, (ast.List, ast.Tuple)):
            return None
        parts = tuple(_static_text(item) for item in values.elts)
        if any(part is None for part in parts):
            return None
        return separator.join(part for part in parts if part is not None)
    return None


def _symbolic_callable_values(
    node: ast.AST,
    bindings: dict[str, frozenset[str] | None],
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
) -> frozenset[str] | None:
    if isinstance(node, ast.NamedExpr):
        return _symbolic_callable_values(node.value, bindings, texts, sequences)
    if isinstance(node, ast.Name):
        return bindings.get(node.id, frozenset({node.id}))
    if isinstance(node, ast.Attribute):
        owners = _symbolic_callable_values(node.value, bindings, texts, sequences)
        if owners is None:
            return None
        return frozenset(f"{owner}.{node.attr}" for owner in owners)
    if isinstance(node, ast.IfExp):
        first = _symbolic_callable_values(node.body, bindings, texts, sequences)
        second = _symbolic_callable_values(node.orelse, bindings, texts, sequences)
        return _merge_values(first, second)
    if isinstance(node, ast.Call):
        callees = _symbolic_callable_values(node.func, bindings, texts, sequences)
        if callees is None:
            return None
        if callees & {"getattr", "builtins.getattr"} and len(node.args) >= 2:
            owners = _symbolic_callable_values(node.args[0], bindings, texts, sequences)
            attributes = _text_values(node.args[1], texts, sequences)
            if owners is None or attributes is None or len(owners) * len(attributes) > 64:
                return None
            return frozenset(f"{owner}.{attribute}" for owner in owners for attribute in attributes)
        if callees & {"__import__", "builtins.__import__", "importlib.import_module"} and node.args:
            return _text_values(node.args[0], texts, sequences)
    name = _dotted_name(node)
    return frozenset({name}) if name else None


@dataclass
class _SymbolicState:
    texts: dict[str, frozenset[str] | None]
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None]
    scalars: dict[str, frozenset[object] | None]
    callables: dict[str, frozenset[str] | None]
    module_evidence: dict[str, bool]
    loader_evidence: dict[str, bool]

    def clone(self) -> _SymbolicState:
        return _SymbolicState(
            texts=dict(self.texts),
            sequences=dict(self.sequences),
            scalars=dict(self.scalars),
            callables=dict(self.callables),
            module_evidence=dict(self.module_evidence),
            loader_evidence=dict(self.loader_evidence),
        )


@dataclass
class _SymbolicFlow:
    normal: _SymbolicState | None = None
    breaks: _SymbolicState | None = None
    continues: _SymbolicState | None = None
    returns: _SymbolicState | None = None
    raises: _SymbolicState | None = None
    exceptions: _SymbolicState | None = None


@cache
def _symbolic_flow(tree: ast.AST) -> tuple[_SymbolicState, dict[int, _SymbolicState]]:
    state = _SymbolicState(
        texts={},
        sequences={},
        scalars={},
        callables={},
        module_evidence={},
        loader_evidence={},
    )
    snapshots: dict[int, _SymbolicState] = {}
    body = (
        tree.body if isinstance(tree, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)) else []
    )
    flow = _flow_symbolic_statements(body, state, snapshots)
    terminal = _merge_optional_symbolic_states(
        flow.normal,
        flow.breaks,
        flow.continues,
        flow.returns,
        flow.raises,
    )
    return (state if terminal is None else terminal), snapshots


@pytest.fixture(autouse=True)
def _clear_symbolic_flow_cache_after_each_test():
    """Keep repo-wide AST analysis from retaining an entire test corpus."""
    yield
    _symbolic_flow.cache_clear()


def _flow_symbolic_statements(
    statements: list[ast.stmt],
    state: _SymbolicState,
    snapshots: dict[int, _SymbolicState],
) -> _SymbolicFlow:
    flow = _SymbolicFlow(normal=state.clone())
    for statement in statements:
        if flow.normal is None:
            break
        entry = flow.normal
        flow.normal = None
        flow = _merge_symbolic_flows(
            flow,
            _flow_symbolic_statement(statement, entry, snapshots),
        )
    return flow


def _flow_symbolic_statement(
    statement: ast.stmt,
    state: _SymbolicState,
    snapshots: dict[int, _SymbolicState],
) -> _SymbolicFlow:
    current = _record_symbolic_calls(statement, state.clone(), snapshots)
    possible_exception = state if current == state else _merge_symbolic_states(state, current)
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        _bind_symbolic_import(statement, current)
    elif isinstance(statement, ast.Assign):
        for target in statement.targets:
            _assign_symbolic_target(current, target, statement.value)
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        _assign_symbolic_target(current, statement.target, statement.value)
    elif isinstance(statement, ast.AugAssign):
        _forget_symbolic_target(current, statement.target)
    elif isinstance(statement, ast.Delete):
        for target in statement.targets:
            _forget_symbolic_target(current, target)
    elif isinstance(statement, ast.If):
        if isinstance(statement.test, ast.Constant):
            branch = statement.body if bool(statement.test.value) else statement.orelse
            branch_flow = _flow_symbolic_statements(branch, current, snapshots)
        else:
            branch_flow = _merge_symbolic_flows(
                _flow_symbolic_statements(statement.body, current, snapshots),
                _flow_symbolic_statements(statement.orelse, current, snapshots),
            )
        branch_flow.exceptions = _merge_optional_symbolic_states(
            possible_exception,
            branch_flow.exceptions,
        )
        return branch_flow
    elif isinstance(statement, (ast.Try, ast.TryStar)):
        return _flow_symbolic_try(statement, current, possible_exception, snapshots)
    elif isinstance(statement, ast.Match):
        alternatives = [_SymbolicFlow(normal=current.clone())]
        for case in statement.cases:
            case_state = _bind_match_pattern(current.clone(), case.pattern, statement.subject)
            if case.guard is not None:
                case_state = _record_expression_calls(case.guard, case_state, snapshots)
            alternatives.append(_flow_symbolic_statements(case.body, case_state, snapshots))
        match_flow = _merge_symbolic_flows(*alternatives)
        match_flow.exceptions = _merge_optional_symbolic_states(
            possible_exception,
            match_flow.exceptions,
        )
        return match_flow
    elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        loop_flow = _flow_symbolic_loop(statement, current, snapshots)
        loop_flow.exceptions = _merge_optional_symbolic_states(
            possible_exception,
            loop_flow.exceptions,
        )
        return loop_flow
    elif isinstance(statement, (ast.With, ast.AsyncWith)):
        for item in statement.items:
            if item.optional_vars is not None:
                _forget_symbolic_target(current, item.optional_vars)
        body_flow = _flow_symbolic_statements(statement.body, current, snapshots)
        body_flow.exceptions = _merge_optional_symbolic_states(
            possible_exception,
            body_flow.exceptions,
        )
        return body_flow
    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        current.callables[statement.name] = frozenset({statement.name})
        current.loader_evidence[statement.name] = False
        current.module_evidence[statement.name] = False
        function_state = current.clone()
        for argument in (
            *statement.args.posonlyargs,
            *statement.args.args,
            *statement.args.kwonlyargs,
        ):
            _replace_symbolic_name(
                function_state,
                argument.arg,
                None,
                None,
                None,
                frozenset({argument.arg}),
            )
        for argument in (statement.args.vararg, statement.args.kwarg):
            if argument is not None:
                _replace_symbolic_name(
                    function_state,
                    argument.arg,
                    None,
                    None,
                    None,
                    frozenset({argument.arg}),
                )
        _flow_symbolic_statements(statement.body, function_state, snapshots)
    elif isinstance(statement, ast.ClassDef):
        current.callables[statement.name] = frozenset({statement.name})
        current.loader_evidence[statement.name] = False
        current.module_evidence[statement.name] = False
        _flow_symbolic_statements(statement.body, current, snapshots)
    if isinstance(statement, ast.Break):
        return _SymbolicFlow(breaks=current, exceptions=possible_exception)
    if isinstance(statement, ast.Continue):
        return _SymbolicFlow(continues=current, exceptions=possible_exception)
    if isinstance(statement, ast.Return):
        return _SymbolicFlow(returns=current, exceptions=possible_exception)
    if isinstance(statement, ast.Raise):
        return _SymbolicFlow(raises=current, exceptions=possible_exception)
    return _SymbolicFlow(normal=current, exceptions=possible_exception)


def _flow_symbolic_try(
    statement: ast.Try | ast.TryStar,
    state: _SymbolicState,
    possible_exception: _SymbolicState,
    snapshots: dict[int, _SymbolicState],
) -> _SymbolicFlow:
    body_flow = _flow_symbolic_statements(statement.body, state, snapshots)
    else_flow = (
        _SymbolicFlow()
        if body_flow.normal is None
        else _flow_symbolic_statements(statement.orelse, body_flow.normal, snapshots)
    )
    combined = _SymbolicFlow(
        normal=else_flow.normal,
        breaks=_merge_optional_symbolic_states(body_flow.breaks, else_flow.breaks),
        continues=_merge_optional_symbolic_states(
            body_flow.continues,
            else_flow.continues,
        ),
        returns=_merge_optional_symbolic_states(body_flow.returns, else_flow.returns),
        raises=_merge_optional_symbolic_states(body_flow.raises, else_flow.raises),
        exceptions=_merge_optional_symbolic_states(
            possible_exception,
            body_flow.exceptions,
            else_flow.exceptions,
        ),
    )
    handler_entry = _merge_optional_symbolic_states(body_flow.exceptions, body_flow.raises)
    if handler_entry is not None:
        for handler in statement.handlers:
            handler_state = handler_entry.clone()
            if handler.name:
                _replace_symbolic_name(handler_state, handler.name, None, None, None, None)
            combined = _merge_symbolic_flows(
                combined,
                _flow_symbolic_statements(handler.body, handler_state, snapshots),
            )
    if statement.finalbody:
        combined = _apply_symbolic_finally(combined, statement.finalbody, snapshots)
    return combined


def _flow_symbolic_loop(
    statement: ast.For | ast.AsyncFor | ast.While,
    state: _SymbolicState,
    snapshots: dict[int, _SymbolicState],
) -> _SymbolicFlow:
    carried = state.clone()
    iteration_flow = _SymbolicFlow(normal=carried)
    while True:
        iteration = carried.clone()
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            _forget_symbolic_target(iteration, statement.target)
        iteration_flow = _flow_symbolic_statements(statement.body, iteration, snapshots)
        backedge = _merge_optional_symbolic_states(
            iteration_flow.normal,
            iteration_flow.continues,
        )
        if backedge is not None and isinstance(statement, ast.While):
            backedge = _record_expression_calls(statement.test, backedge, snapshots)
        merged = carried if backedge is None else _merge_symbolic_states(carried, backedge)
        if merged == carried:
            break
        carried = merged
    else_flow = _flow_symbolic_statements(statement.orelse, carried, snapshots)
    return _SymbolicFlow(
        normal=_merge_optional_symbolic_states(else_flow.normal, iteration_flow.breaks),
        breaks=else_flow.breaks,
        continues=else_flow.continues,
        returns=_merge_optional_symbolic_states(iteration_flow.returns, else_flow.returns),
        raises=_merge_optional_symbolic_states(iteration_flow.raises, else_flow.raises),
        exceptions=_merge_optional_symbolic_states(
            iteration_flow.exceptions,
            else_flow.exceptions,
        ),
    )


def _apply_symbolic_finally(
    flow: _SymbolicFlow,
    statements: list[ast.stmt],
    snapshots: dict[int, _SymbolicState],
) -> _SymbolicFlow:
    result = _SymbolicFlow()
    for status in ("normal", "breaks", "continues", "returns", "raises"):
        state = getattr(flow, status)
        if state is None:
            continue
        final_flow = _flow_symbolic_statements(statements, state, snapshots)
        if final_flow.normal is not None:
            setattr(
                result,
                status,
                _merge_optional_symbolic_states(getattr(result, status), final_flow.normal),
            )
        final_flow.normal = None
        result = _merge_symbolic_flows(result, final_flow)
    result.exceptions = _merge_optional_symbolic_states(result.exceptions, flow.exceptions)
    return result


def _merge_optional_symbolic_states(
    *states: _SymbolicState | None,
) -> _SymbolicState | None:
    present = [state for state in states if state is not None]
    if not present:
        return None
    return present[0] if len(present) == 1 else _merge_many_symbolic_states(present)


def _merge_symbolic_flows(*flows: _SymbolicFlow) -> _SymbolicFlow:
    return _SymbolicFlow(
        normal=_merge_optional_symbolic_states(*(flow.normal for flow in flows)),
        breaks=_merge_optional_symbolic_states(*(flow.breaks for flow in flows)),
        continues=_merge_optional_symbolic_states(*(flow.continues for flow in flows)),
        returns=_merge_optional_symbolic_states(*(flow.returns for flow in flows)),
        raises=_merge_optional_symbolic_states(*(flow.raises for flow in flows)),
        exceptions=_merge_optional_symbolic_states(*(flow.exceptions for flow in flows)),
    )


def _record_symbolic_calls(
    statement: ast.stmt,
    state: _SymbolicState,
    snapshots: dict[int, _SymbolicState],
) -> _SymbolicState:
    statement_state = state.clone()
    for child in ast.iter_child_nodes(statement):
        if isinstance(child, (ast.stmt, ast.match_case, ast.ExceptHandler)):
            continue
        statement_state = _record_expression_calls(child, statement_state, snapshots)
    return statement_state


def _record_expression_calls(
    node: ast.AST,
    state: _SymbolicState,
    snapshots: dict[int, _SymbolicState],
) -> _SymbolicState:
    prior = snapshots.get(id(node))
    snapshot = state.clone()
    snapshots[id(node)] = snapshot if prior is None else _merge_symbolic_states(prior, snapshot)
    if isinstance(node, ast.Lambda):
        return state
    if isinstance(node, ast.NamedExpr):
        current = _record_expression_calls(node.value, state, snapshots)
        _assign_symbolic_target(current, node.target, node.value)
        return current
    if isinstance(node, ast.IfExp):
        tested = _record_expression_calls(node.test, state, snapshots)
        if isinstance(node.test, ast.Constant):
            branch = node.body if bool(node.test.value) else node.orelse
            return _record_expression_calls(branch, tested, snapshots)
        return _merge_symbolic_states(
            _record_expression_calls(node.body, tested.clone(), snapshots),
            _record_expression_calls(node.orelse, tested.clone(), snapshots),
        )
    if isinstance(node, (ast.BoolOp, ast.Compare)):
        children = [
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.stmt, ast.match_case, ast.ExceptHandler))
        ]
        if not children:
            return state
        current = _record_expression_calls(children[0], state, snapshots)
        outcomes = [current.clone()]
        for child in children[1:]:
            current = _record_expression_calls(child, current, snapshots)
            outcomes.append(current.clone())
        return _merge_many_symbolic_states(outcomes)
    current = state
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.stmt, ast.match_case, ast.ExceptHandler)):
            continue
        current = _record_expression_calls(child, current, snapshots)
    return current


@dataclass(frozen=True)
class _AbstractValue:
    expression: ast.AST | None = None
    text: frozenset[str] | None = None
    sequence: tuple[frozenset[str] | None, ...] | None = None
    scalar: frozenset[object] | None = None
    callable_values: frozenset[str] | None = None
    module_evidence: bool = False
    loader_evidence: bool = False


def _bind_match_pattern(
    state: _SymbolicState,
    pattern: ast.pattern,
    subject: ast.expr,
) -> _SymbolicState:
    subject_value = _abstract_expression_value(subject, state)
    for name in _match_capture_names(pattern):
        _replace_symbolic_name(state, name, None, None, None, None)
    return _bind_nested_match_pattern(
        state,
        pattern,
        subject_value,
    )


def _abstract_expression_value(node: ast.AST, state: _SymbolicState) -> _AbstractValue:
    return _AbstractValue(
        expression=node,
        text=_text_values(node, state.texts, state.sequences, state.scalars),
        sequence=_sequence_values(node, state.texts, state.sequences, state.scalars),
        scalar=_closed_scalar_values(node, state.texts, state.sequences, state.scalars),
        callable_values=_symbolic_callable_values(
            node,
            state.callables,
            state.texts,
            state.sequences,
        ),
        module_evidence=_expression_module_evidence(node, state),
        loader_evidence=_expression_loader_evidence(node, state),
    )


def _bind_nested_match_pattern(
    state: _SymbolicState,
    pattern: ast.pattern,
    value: _AbstractValue,
) -> _SymbolicState:
    if isinstance(pattern, ast.MatchAs):
        if pattern.pattern is not None:
            state = _bind_nested_match_pattern(state, pattern.pattern, value)
        if pattern.name is not None:
            _assign_abstract_symbolic_name(state, pattern.name, value)
        return state
    if isinstance(pattern, ast.MatchStar):
        if pattern.name is not None:
            _assign_abstract_symbolic_name(state, pattern.name, value)
        return state
    if isinstance(pattern, ast.MatchOr):
        return _merge_many_symbolic_states(
            [
                _bind_nested_match_pattern(state.clone(), alternative, value)
                for alternative in pattern.patterns
            ]
        )
    if isinstance(pattern, ast.MatchSequence):
        return _bind_match_sequence(state, pattern, value)
    if isinstance(pattern, ast.MatchMapping):
        return _bind_match_mapping(state, pattern, value)
    if isinstance(pattern, ast.MatchClass):
        return _bind_match_class(state, pattern, value)
    return state


def _assign_abstract_symbolic_name(
    state: _SymbolicState,
    name: str,
    value: _AbstractValue,
) -> None:
    _replace_symbolic_name(
        state,
        name,
        value.text,
        value.sequence,
        value.scalar,
        value.callable_values,
        module_evidence=value.module_evidence,
        loader_evidence=value.loader_evidence,
    )


def _bind_match_sequence(
    state: _SymbolicState,
    pattern: ast.MatchSequence,
    value: _AbstractValue,
) -> _SymbolicState:
    elements = _abstract_sequence_elements(value, state)
    star_indexes = [
        index for index, nested in enumerate(pattern.patterns) if isinstance(nested, ast.MatchStar)
    ]
    if elements is None or len(star_indexes) > 1:
        elements = [_AbstractValue()] * len(pattern.patterns)
    if not star_indexes:
        if len(elements) != len(pattern.patterns):
            elements = [_AbstractValue()] * len(pattern.patterns)
        for nested, element in zip(pattern.patterns, elements, strict=True):
            state = _bind_nested_match_pattern(state, nested, element)
        return state
    star_index = star_indexes[0]
    trailing = len(pattern.patterns) - star_index - 1
    if len(elements) < len(pattern.patterns) - 1:
        elements = [_AbstractValue()] * len(pattern.patterns)
        star_values = [_AbstractValue()]
    else:
        star_values = elements[star_index : len(elements) - trailing if trailing else None]
    for index, nested in enumerate(pattern.patterns):
        if index < star_index:
            nested_value = elements[index]
        elif index == star_index:
            nested_value = _abstract_sequence_slice(star_values)
        else:
            nested_value = elements[len(elements) - (len(pattern.patterns) - index)]
        state = _bind_nested_match_pattern(state, nested, nested_value)
    return state


def _abstract_sequence_elements(
    value: _AbstractValue,
    state: _SymbolicState,
) -> list[_AbstractValue] | None:
    if isinstance(value.expression, (ast.Tuple, ast.List)):
        return [_abstract_expression_value(element, state) for element in value.expression.elts]
    if value.sequence is None:
        return None
    return [
        _AbstractValue(
            text=text,
            scalar=None if text is None else frozenset(text),
            module_evidence=text is not None
            and any(_forbidden_dynamic_module_evidence(item) for item in text),
        )
        for text in value.sequence
    ]


def _abstract_sequence_slice(values: list[_AbstractValue]) -> _AbstractValue:
    texts = tuple(value.text for value in values)
    return _AbstractValue(
        sequence=texts,
        module_evidence=any(value.module_evidence for value in values),
        loader_evidence=any(value.loader_evidence for value in values),
    )


def _bind_match_mapping(
    state: _SymbolicState,
    pattern: ast.MatchMapping,
    value: _AbstractValue,
) -> _SymbolicState:
    entries: dict[object, _AbstractValue] = {}
    if isinstance(value.expression, ast.Dict):
        for key, item in zip(value.expression.keys, value.expression.values, strict=True):
            if key is None:
                continue
            keys = _closed_scalar_values(key, state.texts, state.sequences, state.scalars)
            if keys is not None and len(keys) == 1:
                entries[next(iter(keys))] = _abstract_expression_value(item, state)
    for key, nested in zip(pattern.keys, pattern.patterns, strict=True):
        keys = _closed_scalar_values(key, state.texts, state.sequences, state.scalars)
        nested_value = (
            entries.get(next(iter(keys)), _AbstractValue())
            if keys is not None and len(keys) == 1
            else _AbstractValue()
        )
        state = _bind_nested_match_pattern(state, nested, nested_value)
    if pattern.rest is not None:
        _assign_abstract_symbolic_name(state, pattern.rest, _AbstractValue())
    return state


def _bind_match_class(
    state: _SymbolicState,
    pattern: ast.MatchClass,
    value: _AbstractValue,
) -> _SymbolicState:
    subject = value.expression
    compatible = (
        isinstance(subject, ast.Call)
        and _dotted_name(subject.func).rsplit(".", 1)[-1]
        == _dotted_name(pattern.cls).rsplit(".", 1)[-1]
    )
    positional = list(subject.args) if compatible and isinstance(subject, ast.Call) else []
    keywords = (
        {keyword.arg: keyword.value for keyword in subject.keywords if keyword.arg is not None}
        if compatible and isinstance(subject, ast.Call)
        else {}
    )
    for index, nested in enumerate(pattern.patterns):
        nested_value = (
            _abstract_expression_value(positional[index], state)
            if index < len(positional)
            else _AbstractValue()
        )
        state = _bind_nested_match_pattern(state, nested, nested_value)
    for attribute, nested in zip(pattern.kwd_attrs, pattern.kwd_patterns, strict=True):
        nested_value = (
            _abstract_expression_value(keywords[attribute], state)
            if attribute in keywords
            else _AbstractValue()
        )
        state = _bind_nested_match_pattern(state, nested, nested_value)
    return state


def _match_capture_names(pattern: ast.pattern) -> set[str]:
    names = {
        node.name
        for node in ast.walk(pattern)
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None
    }
    names.update(
        node.rest
        for node in ast.walk(pattern)
        if isinstance(node, ast.MatchMapping) and node.rest is not None
    )
    return names


def _forbidden_dynamic_module_evidence(value: str) -> bool:
    return (
        value.startswith(_REMOTE_MCP_MODULES)
        or "experimental.tasks" in value
        or value.startswith(("mcp._", "mcp_types._"))
        or "._" in value
        and value.startswith(("mcp.", "mcp_types."))
        or value.startswith(("plugin_platform.registry", "plugin_platform.package_registry"))
        or value in {"anyio", "asyncio", "os", "subprocess", "trio"}
        or value.startswith("mcp.client.stdio")
        or value
        in {
            "aiohttp",
            "http.client",
            "httpcore",
            "httpx",
            "requests",
            "socket",
            "urllib",
            "urllib3",
        }
    )


def _expression_module_evidence(node: ast.AST, state: _SymbolicState) -> bool:
    if isinstance(node, ast.Name) and state.module_evidence.get(node.id, False):
        return True
    values = _text_values(node, state.texts, state.sequences, state.scalars)
    if values is not None and any(_forbidden_dynamic_module_evidence(value) for value in values):
        return True
    return any(
        _expression_module_evidence(child, state)
        for child in ast.iter_child_nodes(node)
        if isinstance(child, ast.expr)
    )


def _expression_loader_evidence(node: ast.AST, state: _SymbolicState) -> bool:
    if isinstance(node, ast.Name) and state.loader_evidence.get(node.id, False):
        return True
    names = _symbolic_callable_values(
        node,
        state.callables,
        state.texts,
        state.sequences,
    )
    if names is not None and names & {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
    }:
        return True
    children = (
        [*node.args, *(keyword.value for keyword in node.keywords)]
        if isinstance(node, ast.Call)
        else [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)]
    )
    return any(_expression_loader_evidence(child, state) for child in children)


def _bind_symbolic_import(node: ast.Import | ast.ImportFrom, state: _SymbolicState) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.partition(".")[0]
            canonical = alias.name if alias.asname else alias.name.partition(".")[0]
            _replace_symbolic_name(
                state,
                local,
                None,
                None,
                None,
                frozenset({canonical}),
                loader_evidence=canonical == "importlib",
            )
        return
    if not node.module:
        return
    for alias in node.names:
        if alias.name == "*":
            continue
        _replace_symbolic_name(
            state,
            alias.asname or alias.name,
            None,
            None,
            None,
            frozenset({f"{node.module}.{alias.name}"}),
            loader_evidence=f"{node.module}.{alias.name}"
            in {"builtins.__import__", "importlib.import_module"},
        )


def _assign_symbolic_target(state: _SymbolicState, target: ast.AST, value: ast.AST) -> None:
    if isinstance(target, ast.Name):
        _replace_symbolic_name(
            state,
            target.id,
            _text_values(value, state.texts, state.sequences, state.scalars),
            _sequence_values(value, state.texts, state.sequences, state.scalars),
            _closed_scalar_values(value, state.texts, state.sequences, state.scalars),
            _symbolic_callable_values(value, state.callables, state.texts, state.sequences),
            module_evidence=_expression_module_evidence(value, state),
            loader_evidence=_expression_loader_evidence(value, state),
        )
        return
    if not isinstance(target, (ast.Tuple, ast.List)):
        return
    if isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
        for nested_target, nested_value in zip(target.elts, value.elts, strict=True):
            _assign_symbolic_target(state, nested_target, nested_value)
        return
    values = _sequence_values(value, state.texts, state.sequences, state.scalars)
    if values is not None and len(target.elts) == len(values):
        for nested_target, text in zip(target.elts, values, strict=True):
            if isinstance(nested_target, ast.Name):
                scalar = None if text is None else frozenset(text)
                _replace_symbolic_name(
                    state,
                    nested_target.id,
                    text,
                    None,
                    scalar,
                    None,
                    module_evidence=text is not None
                    and any(_forbidden_dynamic_module_evidence(item) for item in text),
                )
            else:
                _forget_symbolic_target(state, nested_target)
        return
    _forget_symbolic_target(state, target)


def _forget_symbolic_target(state: _SymbolicState, target: ast.AST) -> None:
    if isinstance(target, ast.Name):
        _replace_symbolic_name(state, target.id, None, None, None, None)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for nested in target.elts:
            _forget_symbolic_target(state, nested)


def _replace_symbolic_name(
    state: _SymbolicState,
    name: str,
    text: frozenset[str] | None,
    sequence: tuple[frozenset[str] | None, ...] | None,
    scalar: frozenset[object] | None,
    callable_values: frozenset[str] | None,
    *,
    module_evidence: bool = False,
    loader_evidence: bool = False,
) -> None:
    state.texts[name] = text
    state.sequences[name] = sequence
    state.scalars[name] = scalar
    state.callables[name] = callable_values
    state.module_evidence[name] = module_evidence
    state.loader_evidence[name] = loader_evidence


def _merge_many_symbolic_states(states: list[_SymbolicState]) -> _SymbolicState:
    assert states
    merged = states[0].clone()
    for state in states[1:]:
        merged = _merge_symbolic_states(merged, state)
    return merged


def _merge_symbolic_states(first: _SymbolicState, second: _SymbolicState) -> _SymbolicState:
    return _SymbolicState(
        texts=_merge_symbolic_maps(first.texts, second.texts, _merge_values),
        sequences=_merge_symbolic_maps(first.sequences, second.sequences, _merge_sequence_values),
        scalars=_merge_symbolic_maps(first.scalars, second.scalars, _merge_scalar_values),
        callables=_merge_symbolic_maps(first.callables, second.callables, _merge_values),
        module_evidence=_merge_symbolic_evidence(
            first.module_evidence,
            second.module_evidence,
        ),
        loader_evidence=_merge_symbolic_evidence(
            first.loader_evidence,
            second.loader_evidence,
        ),
    )


def _merge_symbolic_evidence(first: dict[str, bool], second: dict[str, bool]) -> dict[str, bool]:
    return {
        name: first.get(name, False) or second.get(name, False)
        for name in first.keys() | second.keys()
    }


def _merge_symbolic_maps(
    first: dict[str, object],
    second: dict[str, object],
    merger: object,
) -> dict[str, object]:
    merged: dict[str, object] = {}
    for name in first.keys() | second.keys():
        if name not in first or name not in second:
            merged[name] = None
        else:
            merged[name] = merger(first[name], second[name])  # type: ignore[operator]
    return merged


def _merge_sequence_values(
    first: tuple[frozenset[str] | None, ...] | None,
    second: tuple[frozenset[str] | None, ...] | None,
) -> tuple[frozenset[str] | None, ...] | None:
    if first is None or second is None or len(first) != len(second):
        return None
    return tuple(_merge_values(left, right) for left, right in zip(first, second, strict=True))


def _symbolic_call_names(
    node: ast.AST,
    bindings: dict[str, frozenset[str] | None],
    texts: dict[str, frozenset[str] | None],
    sequences: dict[str, tuple[frozenset[str] | None, ...] | None],
) -> frozenset[str] | None:
    return _symbolic_callable_values(node, bindings, texts, sequences)


def _resolved_callable_name(node: ast.AST, bindings: dict[str, str]) -> str:
    """Resolve direct, aliased, assigned, and constant-getattr call targets."""
    if (
        isinstance(node, ast.Call)
        and _resolved_import_name(node.func, bindings) in {"builtins.getattr", "getattr"}
        and len(node.args) >= 2
    ):
        owner = _resolved_import_name(node.args[0], bindings)
        attribute = _static_text(node.args[1])
        return f"{owner}.{attribute}" if owner and attribute is not None else ""
    return _resolved_import_name(node, bindings)


def _call_bindings(tree: ast.AST) -> dict[str, str]:
    """Include simple assignment aliases so a forbidden call cannot hide behind a name."""
    imported = _import_bindings(tree)
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    ]
    values: dict[str, frozenset[str] | None] = {
        name: frozenset({canonical}) for name, canonical in imported.items()
    }
    for node in assignments:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                values.setdefault(target.id, frozenset())
    while True:
        changed = False
        for node in assignments:
            resolved = _symbolic_callable_values(node.value, values, {}, {})
            if resolved is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                merged = _merge_values(values[target.id], resolved)
                if merged != values[target.id]:
                    values[target.id] = merged
                    changed = True
        if not changed:
            break
    return {
        name: next(iter(canonical))
        for name, canonical in values.items()
        if canonical is not None and len(canonical) == 1
    }


def _dynamic_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Inspect one tree and release its symbolic-flow graph immediately."""
    try:
        return _dynamic_imports_uncached(tree)
    finally:
        _symbolic_flow.cache_clear()


def _dynamic_imports_uncached(tree: ast.AST) -> list[tuple[int, str]]:
    final_state, snapshots = _symbolic_flow(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        state = snapshots.get(id(node), final_state)
        names = _symbolic_call_names(node.func, state.callables, state.texts, state.sequences)
        canonical_loaders = {"__import__", "builtins.__import__", "importlib.import_module"}
        canonical_loader = names is not None and bool(names.intersection(canonical_loaders))
        targets = [*node.args, *(keyword.value for keyword in node.keywords)]
        resolved_targets: list[tuple[ast.expr, frozenset[str]]] = []
        for target in targets:
            target_state = snapshots.get(id(target), state)
            values = _text_values(
                target,
                target_state.texts,
                target_state.sequences,
                target_state.scalars,
            )
            if values is not None:
                resolved_targets.append((target, values))
        if not canonical_loader and not resolved_targets:
            if _expression_loader_evidence(
                node.func,
                state,
            ) or any(
                _expression_module_evidence(
                    target,
                    snapshots.get(id(target), state),
                )
                for target in targets
            ):
                found.append((node.lineno, _UNRESOLVED_DYNAMIC_IMPORT))
            continue
        if canonical_loader and not resolved_targets:
            found.append((node.lineno, _UNRESOLVED_DYNAMIC_IMPORT))
            continue
        found.extend(
            (node.lineno, module)
            for target, modules in resolved_targets
            for module in sorted(modules)
            if not _approved_non_loader_text_target(node, names, target, module)
        )
    return found


def _approved_non_loader_text_target(
    call: ast.Call,
    names: frozenset[str] | None,
    target: ast.expr,
    value: str,
) -> bool:
    """Exclude one exact data-field literal that is not a dynamic module target."""
    return (
        names == frozenset({"mcp_platform.inference.contracts.PreparedMcpInference"})
        and value == "mcp"
        and any(keyword.arg == "lane_hint" and keyword.value is target for keyword in call.keywords)
    )


def _string_constants(tree: ast.AST, value: str) -> list[int]:
    """Find constants in one tree without retaining its analysis graph."""
    try:
        return _string_constants_uncached(tree, value)
    finally:
        _symbolic_flow.cache_clear()


def _string_constants_uncached(tree: ast.AST, value: str) -> list[int]:
    final_state, snapshots = _symbolic_flow(tree)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.expr)
            and (
                values := _text_values(
                    node,
                    snapshots.get(id(node), final_state).texts,
                    snapshots.get(id(node), final_state).sequences,
                    snapshots.get(id(node), final_state).scalars,
                )
            )
            is not None
            and value in values
        }
    )


def _private_sdk_import_violations(tree: ast.AST) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    bindings = _call_bindings(tree)

    def sdk_module(module: str) -> bool:
        return module == "mcp" or module.startswith("mcp.") or module.startswith("mcp_types")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if sdk_module(alias.name) and (
                    alias.name.startswith(("mcp._", "mcp_types._")) or "._" in alias.name
                ):
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if sdk_module(node.module) and (
                node.module.startswith(("mcp._", "mcp_types._")) or "._" in node.module
            ):
                violations.append((node.lineno, node.module))
            for alias in node.names:
                if sdk_module(node.module) and alias.name.startswith("_"):
                    violations.append((node.lineno, f"{node.module}.{alias.name}"))
        elif isinstance(node, ast.Attribute):
            name = _resolved_import_name(node, bindings)
            if sdk_module(name) and any(part.startswith("_") for part in name.split(".")[1:]):
                violations.append((node.lineno, name))
        elif isinstance(node, ast.Call):
            name = _resolved_callable_name(node, bindings) or _resolved_callable_name(
                node.func, bindings
            )
            if sdk_module(name) and any(part.startswith("_") for part in name.split(".")[1:]):
                violations.append((node.lineno, name))
    violations.extend(
        (line, module)
        for line, module in _dynamic_imports(tree)
        if module == _UNRESOLVED_DYNAMIC_IMPORT
        or sdk_module(module)
        and (module.startswith(("mcp._", "mcp_types._")) or "._" in module)
    )
    return violations


def _private_plugin_registry_violations(tree: ast.AST) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    bindings = _call_bindings(tree)
    registry_modules = ("plugin_platform.registry", "plugin_platform.package_registry")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                if module.startswith(registry_modules):
                    violations.append((node.lineno, module))
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("plugin_platform")
            ):
                violations.extend(
                    (node.lineno, f"{node.module}.{alias.name}")
                    for alias in node.names
                    if alias.name.startswith("_")
                )
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            owner = _resolved_import_name(node.value, bindings)
            leaf = _dotted_name(node.value).rsplit(".", 1)[-1]
            if owner.startswith(registry_modules) or leaf in {
                "app_registry",
                "package_registry",
                "plugin_registry",
            }:
                violations.append((node.lineno, f"{owner}.{node.attr}"))
        elif isinstance(node, ast.Call):
            name = _resolved_callable_name(node, bindings) or _resolved_callable_name(
                node.func, bindings
            )
            owner, _separator, attribute = name.rpartition(".")
            if attribute.startswith("_") and owner.startswith(registry_modules):
                violations.append((node.lineno, name))
    violations.extend(
        (line, module)
        for line, module in _dynamic_imports(tree)
        if module == _UNRESOLVED_DYNAMIC_IMPORT or module.startswith(registry_modules)
    )
    return violations


def _literal_work_loop_violations(tree: ast.AST, *, path: Path | None = None) -> list[int]:
    """Reject loops without reachable exit, progress, or a finite iterator owner."""
    violations: list[int] = []
    for loop in (node for node in ast.walk(tree) if isinstance(node, ast.While)):
        called = {
            _dotted_name(node.func).rsplit(".", 1)[-1]
            for node in _reachable_loop_nodes(loop.body)
            if isinstance(node, ast.Call)
        }
        finite_iterator = "asend" in called or "anext" in called
        literal_forever = isinstance(loop.test, ast.Constant) and loop.test.value is True
        if called & _WORK_GENERATING_CALLS or not (
            literal_forever
            and _loop_has_unconditional_exit(loop)
            or finite_iterator
            or _loop_has_finite_counter_bound(loop)
            or _loop_has_explicit_deadline_bound(loop)
            or _exact_finite_production_loop_owner(path, tree, loop)
            or not literal_forever
            and _conditional_loop_progresses(loop)
        ):
            violations.append(loop.lineno)
    return violations


def _reachable_loop_nodes(statements: list[ast.stmt]) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes.append(statement)
            continue
        nodes.append(statement)
        for child in ast.iter_child_nodes(statement):
            if not isinstance(child, ast.stmt):
                nodes.extend(ast.walk(child))
        if isinstance(statement, ast.If):
            if isinstance(statement.test, ast.Constant):
                branch = statement.body if bool(statement.test.value) else statement.orelse
                nodes.extend(_reachable_loop_nodes(branch))
            else:
                nodes.extend(_reachable_loop_nodes(statement.body))
                nodes.extend(_reachable_loop_nodes(statement.orelse))
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            nodes.extend(_reachable_loop_nodes(statement.body))
            for handler in statement.handlers:
                nodes.append(handler)
                nodes.extend(_reachable_loop_nodes(handler.body))
            nodes.extend(_reachable_loop_nodes(statement.orelse))
            nodes.extend(_reachable_loop_nodes(statement.finalbody))
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            nodes.extend(_reachable_loop_nodes(statement.body))
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            nodes.extend(
                node
                for node in _reachable_loop_nodes(statement.body)
                if isinstance(node, (ast.Return, ast.Raise))
            )
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                nodes.extend(_reachable_loop_nodes(case.body))
        if isinstance(statement, (ast.Break, ast.Continue, ast.Return, ast.Raise)):
            break
    return nodes


def _loop_has_unconditional_exit(loop: ast.While) -> bool:
    """Accept only when every reachable first iteration terminates the loop."""
    outcomes = _condition_progress_outcomes(loop.body, set(), False)
    return bool(outcomes) and all(status == "exit" for status, _mutated in outcomes)


def _loop_event_reaches_every_backedge(loop: ast.While, event_ids: set[int]) -> bool:
    outcomes = _loop_event_outcomes(loop.body, event_ids, False)
    return bool(outcomes) and all(status == "exit" or saw_event for status, saw_event in outcomes)


def _conditional_loop_progresses(loop: ast.While) -> bool:
    condition_references = {
        reference
        for node in ast.walk(loop.test)
        if isinstance(node, (ast.Name, ast.Attribute)) and (reference := _dotted_name(node))
    }
    outcomes = _condition_progress_outcomes(loop.body, condition_references, False)
    return bool(outcomes) and all(status == "exit" or mutated for status, mutated in outcomes)


def _condition_progress_outcomes(
    statements: list[ast.stmt],
    condition_references: set[str],
    mutated: bool,
) -> set[tuple[str, bool]]:
    outcomes: set[tuple[str, bool]] = {("active", mutated)}
    for statement in statements:
        next_outcomes: set[tuple[str, bool]] = set()
        for status, prior_mutation in outcomes:
            if status != "active":
                next_outcomes.add((status, prior_mutation))
                continue
            next_outcomes.update(
                _condition_statement_outcomes(statement, condition_references, prior_mutation)
            )
        outcomes = next_outcomes
    return outcomes


def _condition_statement_outcomes(
    statement: ast.stmt,
    condition_references: set[str],
    mutated: bool,
) -> set[tuple[str, bool]]:
    if isinstance(statement, ast.If):
        branches = (
            (statement.body if bool(statement.test.value) else statement.orelse,)
            if isinstance(statement.test, ast.Constant)
            else (statement.body, statement.orelse)
        )
        return {
            outcome
            for branch in branches
            for outcome in _condition_progress_outcomes(branch, condition_references, mutated)
        }
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _condition_progress_outcomes(statement.body, condition_references, mutated)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        body_outcomes = _condition_progress_outcomes(statement.body, condition_references, mutated)
        if statement.orelse:
            body_outcomes = _continue_active_outcomes(
                body_outcomes, statement.orelse, condition_references
            )
        outcomes = body_outcomes | {
            outcome
            for handler in statement.handlers
            for outcome in _condition_progress_outcomes(handler.body, condition_references, mutated)
        }
        if statement.finalbody:
            outcomes = _continue_all_outcomes(outcomes, statement.finalbody, condition_references)
        return outcomes
    if isinstance(statement, ast.Match):
        return {("active", mutated)} | {
            outcome
            for case in statement.cases
            for outcome in _condition_progress_outcomes(
                case.body,
                condition_references,
                mutated,
            )
        }
    if isinstance(statement, ast.Continue):
        return {("iterate", mutated)}
    if isinstance(statement, (ast.Break, ast.Return, ast.Raise)):
        return {("exit", mutated)}
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {("active", mutated)}
    return {
        (
            "active",
            mutated or _statement_mutates_condition(statement, condition_references),
        )
    }


def _continue_active_outcomes(
    outcomes: set[tuple[str, bool]],
    statements: list[ast.stmt],
    condition_references: set[str],
) -> set[tuple[str, bool]]:
    continued = {
        outcome
        for status, mutated in outcomes
        if status == "active"
        for outcome in _condition_progress_outcomes(statements, condition_references, mutated)
    }
    return continued | {outcome for outcome in outcomes if outcome[0] != "active"}


def _continue_all_outcomes(
    outcomes: set[tuple[str, bool]],
    statements: list[ast.stmt],
    condition_references: set[str],
) -> set[tuple[str, bool]]:
    continued: set[tuple[str, bool]] = set()
    for prior_status, mutated in outcomes:
        for status, final_mutation in _condition_progress_outcomes(
            statements, condition_references, mutated
        ):
            continued.add((prior_status if status == "active" else status, final_mutation))
    return continued


def _statement_mutates_condition(statement: ast.stmt, condition_references: set[str]) -> bool:
    if isinstance(statement, ast.AugAssign):
        return _dotted_name(statement.target) in condition_references and not (
            isinstance(statement.value, ast.Constant) and statement.value.value == 0
        )
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if statement.value is not None and any(
            _dotted_name(target) in condition_references
            and _dotted_name(statement.value) != _dotted_name(target)
            for target in targets
        ):
            return True
    for call in (node for node in _reachable_loop_nodes([statement]) if isinstance(node, ast.Call)):
        name = _dotted_name(call.func)
        receiver, _separator, leaf = name.rpartition(".")
        if leaf in {"pop", "popleft", "remove"} and receiver in condition_references:
            return True
    return False


def _loop_event_outcomes(
    statements: list[ast.stmt], event_ids: set[int], saw_event: bool
) -> set[tuple[str, bool]]:
    outcomes: set[tuple[str, bool]] = {("active", saw_event)}
    for statement in statements:
        next_outcomes: set[tuple[str, bool]] = set()
        for status, prior_event in outcomes:
            if status != "active":
                next_outcomes.add((status, prior_event))
                continue
            next_outcomes.update(_loop_event_statement_outcomes(statement, event_ids, prior_event))
        outcomes = next_outcomes
    return outcomes


def _loop_event_statement_outcomes(
    statement: ast.stmt, event_ids: set[int], saw_event: bool
) -> set[tuple[str, bool]]:
    direct_event = (
        saw_event
        or id(statement) in event_ids
        or any(
            id(node) in event_ids
            for node in ast.iter_child_nodes(statement)
            if not isinstance(node, ast.stmt)
            for node in ast.walk(node)
            if isinstance(node, ast.Call)
        )
    )
    if isinstance(statement, ast.If):
        branches = (
            (statement.body if bool(statement.test.value) else statement.orelse,)
            if isinstance(statement.test, ast.Constant)
            else (statement.body, statement.orelse)
        )
        return {
            outcome
            for branch in branches
            for outcome in _loop_event_outcomes(branch, event_ids, direct_event)
        }
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _loop_event_outcomes(statement.body, event_ids, direct_event)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        body_outcomes = _loop_event_outcomes(statement.body, event_ids, direct_event)
        if statement.orelse:
            body_outcomes = _continue_loop_event_outcomes(
                body_outcomes, statement.orelse, event_ids, active_only=True
            )
        outcomes = body_outcomes | {
            outcome
            for handler in statement.handlers
            for outcome in _loop_event_outcomes(handler.body, event_ids, direct_event)
        }
        if statement.finalbody:
            outcomes = _continue_loop_event_outcomes(
                outcomes, statement.finalbody, event_ids, active_only=False
            )
        return outcomes
    if isinstance(statement, ast.Match):
        return {("active", direct_event)} | {
            outcome
            for case in statement.cases
            for outcome in _loop_event_outcomes(case.body, event_ids, direct_event)
        }
    if isinstance(statement, ast.Continue):
        return {("iterate", direct_event)}
    if isinstance(statement, (ast.Break, ast.Return, ast.Raise)):
        return {("exit", direct_event)}
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {("active", saw_event)}
    return {("active", direct_event)}


def _continue_loop_event_outcomes(
    outcomes: set[tuple[str, bool]],
    statements: list[ast.stmt],
    event_ids: set[int],
    *,
    active_only: bool,
) -> set[tuple[str, bool]]:
    continued: set[tuple[str, bool]] = set()
    for prior_status, saw_event in outcomes:
        if active_only and prior_status != "active":
            continued.add((prior_status, saw_event))
            continue
        for status, final_event in _loop_event_outcomes(statements, event_ids, saw_event):
            continued.add((prior_status if status == "active" else status, final_event))
    return continued


def _loop_has_finite_counter_bound(loop: ast.While) -> bool:
    reachable = _reachable_loop_nodes(loop.body)
    candidates: dict[tuple[str, str], set[int]] = {}
    for node in reachable:
        if not (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.op, (ast.Add, ast.Sub))
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) in {int, float}
            and node.value.value > 0
        ):
            continue
        direction = "increment" if isinstance(node.op, ast.Add) else "decrement"
        candidates.setdefault((node.target.id, direction), set()).add(id(node))
    for (counter, direction), progress_ids in candidates.items():
        if any(
            _writes_name(node, counter)
            and not (isinstance(node, ast.AugAssign) and id(node) in progress_ids)
            for node in reachable
        ):
            continue
        guard_ids = {
            id(node)
            for node in reachable
            if isinstance(node, ast.If) and _counter_guard_exits(node, counter, direction)
        }
        if (
            guard_ids
            and _loop_event_reaches_every_backedge(loop, progress_ids)
            and _loop_event_reaches_every_backedge(loop, guard_ids)
        ):
            return True
    return False


def _writes_name(node: ast.AST, name: str) -> bool:
    return name in _statement_write_references(node)


def _statement_write_references(node: ast.AST) -> set[str]:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets.extend(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        targets.append(node.target)
    elif isinstance(node, ast.Delete):
        targets.extend(node.targets)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        targets.append(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets.extend(item.optional_vars for item in node.items if item.optional_vars is not None)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    elif isinstance(node, ast.Import):
        return {alias.asname or alias.name.partition(".")[0] for alias in node.names}
    elif isinstance(node, ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names if alias.name != "*"}
    elif isinstance(node, ast.Match):
        return {name for case in node.cases for name in _match_capture_names(case.pattern)}
    elif isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    return {reference for target in targets for reference in _target_write_references(target)}


def _target_write_references(target: ast.AST) -> set[str]:
    if isinstance(target, (ast.Name, ast.Attribute, ast.Subscript)):
        return {ast.unparse(target)}
    if isinstance(target, ast.Starred):
        return _target_write_references(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {
            reference for nested in target.elts for reference in _target_write_references(nested)
        }
    return set()


def _counter_guard_exits(node: ast.If, counter: str, direction: str) -> bool:
    outcomes = _condition_progress_outcomes(node.body, set(), False)
    if not outcomes or not all(status == "exit" for status, _mutated in outcomes):
        return False
    return _counter_limit_guarantees_test(node.test, counter, direction)


def _counter_limit_guarantees_test(test: ast.AST, counter: str, direction: str) -> bool:
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.BoolOp):
        proofs = (
            _counter_limit_guarantees_test(value, counter, direction) for value in test.values
        )
        return all(proofs) if isinstance(test.op, ast.And) else any(proofs)
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    left = test.left
    operator = test.ops[0]
    right = test.comparators[0]
    if direction == "decrement":
        return _ordered_counter_limit(left, operator, right, counter, (ast.Lt, ast.LtE)) or (
            _ordered_counter_limit(right, operator, left, counter, (ast.Gt, ast.GtE))
        )
    return _ordered_counter_limit(left, operator, right, counter, (ast.Gt, ast.GtE)) or (
        _ordered_counter_limit(right, operator, left, counter, (ast.Lt, ast.LtE))
    )


def _ordered_counter_limit(
    counter_node: ast.AST,
    operator: ast.cmpop,
    limit_node: ast.AST,
    counter: str,
    operators: tuple[type[ast.cmpop], ...],
) -> bool:
    return (
        isinstance(counter_node, ast.Name)
        and counter_node.id == counter
        and isinstance(operator, operators)
        and (
            isinstance(limit_node, (ast.Name, ast.Attribute))
            or isinstance(limit_node, ast.Constant)
            and type(limit_node.value) in {int, float}
        )
        and not any(
            isinstance(node, ast.Name) and node.id == counter for node in ast.walk(limit_node)
        )
    )


def _loop_has_explicit_deadline_bound(loop: ast.While) -> bool:
    """Require a canonical deadline check before every reachable backedge."""
    reachable = _reachable_loop_nodes(loop.body)
    if _loop_has_derived_deadline_bound(loop, reachable):
        return True
    guards: dict[str, set[int]] = {}
    for node in reachable:
        if not isinstance(node, ast.If):
            continue
        outcomes = _condition_progress_outcomes(node.body, set(), False)
        if not outcomes or not all(status == "exit" for status, _mutated in outcomes):
            continue
        for deadline in _deadline_guard_names(node.test):
            guards.setdefault(deadline, set()).add(id(node))
    return any(
        not any(_writes_name(node, deadline) for node in reachable)
        and _loop_event_reaches_every_backedge(loop, guard_ids)
        for deadline, guard_ids in guards.items()
    )


def _loop_has_derived_deadline_bound(loop: ast.While, reachable: list[ast.AST]) -> bool:
    clocks = {"anyio.current_time", "time.monotonic"}
    assignments: dict[tuple[str, str], set[int]] = {}
    for node in reachable:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not (
            len(targets) == 1
            and isinstance(targets[0], ast.Name)
            and isinstance(node.value, ast.BinOp)
            and isinstance(node.value.op, ast.Sub)
            and isinstance(node.value.left, ast.Name)
            and "deadline" in node.value.left.id.casefold()
            and isinstance(node.value.right, ast.Call)
            and _dotted_name(node.value.right.func) in clocks
        ):
            continue
        assignments.setdefault(
            (targets[0].id, node.value.left.id),
            set(),
        ).add(id(node))

    for (remaining, deadline), assignment_ids in assignments.items():
        guard_ids = {
            id(node)
            for node in reachable
            if isinstance(node, ast.If) and _counter_guard_exits(node, remaining, "decrement")
        }
        if (
            guard_ids
            and not any(
                _writes_name(node, remaining) and id(node) not in assignment_ids
                for node in reachable
            )
            and not any(_writes_name(node, deadline) for node in reachable)
            and _loop_event_reaches_every_backedge(loop, assignment_ids)
            and _loop_event_reaches_every_backedge(loop, guard_ids)
        ):
            return True
    return False


def _deadline_guard_names(test: ast.AST) -> set[str]:
    proofs = _deadline_guard_proofs(test)
    return set() if proofs is None else proofs


def _deadline_guard_proofs(test: ast.AST) -> set[str] | None:
    if isinstance(test, ast.Constant):
        return None if bool(test.value) else set()
    if isinstance(test, ast.BoolOp):
        child_proofs = [_deadline_guard_proofs(value) for value in test.values]
        if isinstance(test.op, ast.Or):
            if any(proof is None for proof in child_proofs):
                return None
            return set().union(*(proof for proof in child_proofs if proof is not None))
        candidates: set[str] | None = None
        for proof in child_proofs:
            if proof is None:
                continue
            if not proof:
                return set()
            candidates = set(proof) if candidates is None else candidates & proof
        return set() if candidates is None else candidates
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return set()

    deadlines: set[str] = set()
    clocks = {"anyio.current_time", "time.monotonic"}
    left = test.left
    operator = test.ops[0]
    right = test.comparators[0]
    if (
        isinstance(left, ast.Call)
        and _dotted_name(left.func) in clocks
        and isinstance(operator, (ast.Gt, ast.GtE))
        and isinstance(right, ast.Name)
        and "deadline" in right.id.casefold()
    ):
        deadlines.add(right.id)
    if (
        isinstance(left, ast.Name)
        and "deadline" in left.id.casefold()
        and isinstance(operator, (ast.Lt, ast.LtE))
        and isinstance(right, ast.Call)
        and _dotted_name(right.func) in clocks
    ):
        deadlines.add(left.id)
    return deadlines


def _exact_finite_production_loop_owner(path: Path | None, tree: ast.AST, loop: ast.While) -> bool:
    """Permit only source-traced loops with an exact owner and backedge shape."""
    if path is None:
        return False
    owner = _qualified_enclosing_name(tree, loop)
    if not owner:
        return False
    key = (path, owner, ast.unparse(loop.test))
    owners: dict[tuple[Path, str, str], str] = {
        (MCP_ROOT / "artifact_guard.py", "_identity_and_digest", "True"): (
            "74d53205dbb705ede808874ce4b5c0f808cb4899c7c4d2a0824db72d83bdbd3e"
        ),
        (
            MCP_ROOT / "artifact_guard.py",
            "ArtifactLaunchGuard.acquire",
            "artifact_is_contained",
        ): ("63f94a81622a0eacac356c2310ef311eee95720a97880db89180da5aa54f72ae"),
        (MCP_ROOT / "authority.py", "McpAuthorityGateway.wait_for_cleanup_sync", "True"): (
            "9ec84fe0f338a731b6ea72d00fe31b48966e41c16d44d5a548cbb6c992e01ec6"
        ),
        (
            MCP_ROOT / "authorization.py",
            "_McpAuthorizationPort.receive_authorization_request",
            "True",
        ): ("8a9b9a0b5825e4b8d06bee9efe5101827c1a42d5e8bc419dcd631be08298542a"),
        (MCP_ROOT / "authorization.py", "_bounded_flow_close", "True"): (
            "70d2640e24449cf38f47fd2cf693993e3538aa3b0643e5b48d170a7bc6408374"
        ),
        (MCP_ROOT / "client.py", "_collect_pages", "True"): (
            "736d953cf81d175d5dce5807a563f348f80f2c17b175a7d39c993ef1fd83e4f6"
        ),
        (MCP_ROOT / "client.py", "_operation_pages", "True"): (
            "20bfd55ea5301d151f02cd85a2a2d641e479b24fa7f9c9596f04b5ddf185b8c7"
        ),
        (MCP_ROOT / "client.py", "_discovery_pages", "True"): (
            "150302c6af74d879717b55fcbebed351c5c5445c5dd102adca0a992c6ff1e99c"
        ),
        (MCP_ROOT / "client.py", "CurrentMcpClient._recover_created_task", "True"): (
            "c387b75604a3280b4359fb6d3a551efe265a467bb55308bde3537b70c6b3f2c9"
        ),
        (MCP_ROOT / "client.py", "CurrentMcpClient._join_operation_cleanup", "True"): (
            "6a3c8d6574cafad2cf850aee80d513b62e1b540dd71ad6c2f4c091f5ac89181d"
        ),
        (MCP_ROOT / "client.py", "CurrentMcpClient._join_owner", "True"): (
            "d955ac9dd72deaa509b9791c7860d3d7d5593fbb9fc2ca0ac9398263c8fb207d"
        ),
        (
            MCP_ROOT / "client.py",
            "CurrentMcpClient._recover_created_task",
            "loop.time() < deadline and (not cleanup.done())",
        ): ("fe9739030af41b85ec7aeab154c5913f384f97787b3c46cc2131faa6f3c42581"),
        (MCP_ROOT / "client.py", "CurrentMcpClient._recover_created_task", "not cleanup.done()"): (
            "69bda410733036a51ac30c33cbb3390448277719ca4d48bf326a7252079c339c"
        ),
        (
            MCP_ROOT / "client.py",
            "CurrentMcpClient._join_owned_task",
            "loop.time() < deadline and (not owned.done())",
        ): ("326ee7154eca6b3d874245d5e3b993e3cdd87e1652148542c99d436a947f84d3"),
        (MCP_ROOT / "package_verifier.py", "_read_bounded_file", "True"): (
            "11fd337930b25c461fbcc109ff28705f026a6afdfb2d32165d7ab8972b69d71d"
        ),
        (MCP_ROOT / "package_verifier.py", "verify_package", "True"): (
            "d5d074d1d557b69ebfb2d71b2d3d0400ac04bffa8f45a186bcfda352d8a6de69"
        ),
        (MCP_ROOT / "external_switch.py", "_validate_parent_chain", "True"): (
            "093100626df69e95b9eb026f378f2a500aeaf82402190fbd84d3b028b3124f06"
        ),
        (MCP_ROOT / "redaction.py", "RedactingStderrCapture._drain", "read_fd >= 0"): (
            "3f239c5a28bf0e6e49fd807c5791f168f3e2405e8fe856934db24b8cdc9d7055"
        ),
        (
            MCP_ROOT / "service.py",
            "start_default_mcp_host",
            "_default_state in {'starting', 'stopping'}",
        ): ("85f07384d455a711715c1790305caf5cff4967e1a2d3db8539d97e5c6031c9a7"),
        (
            MCP_ROOT / "service.py",
            "shutdown_default_mcp_host",
            "_default_state == 'starting'",
        ): ("e3302920264d2a4c04bee4e2f8602f13efcd65a1b96e6ea81f78d5d54e58e004"),
        (
            MCP_ROOT / "service.py",
            "shutdown_default_mcp_host",
            "_default_state == 'stopping' and _default_stopping_owner == stopping_owner",
        ): ("f90d4e6d584fe511d83e260b2b32971d650335e38f83d575141c4a5d61d75db5"),
        (
            MCP_ROOT / "transports" / "streamable_http.py",
            "_BoundedResponseStream.aclose",
            "True",
        ): ("b6c0bec83ff4c01fb9a055a16d0e4bbf51f821b7c16eb671a2f4dd98208fbe80"),
        (
            MCP_ROOT / "transports" / "streamable_http.py",
            "_BoundedResponseStream.aclose",
            "loop.time() < deadline and (not close_task.done())",
        ): ("6fdd901c163264723055143565449ecf9e3d49734cbf6159a504d8aedbf843d1"),
    }
    expected_digest = owners.get(key)
    if expected_digest is None:
        return False
    normalized = ast.dump(loop, include_attributes=False).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest() == expected_digest


def _qualified_enclosing_name(tree: ast.AST, node: ast.AST) -> str:
    enclosing = [
        owner
        for owner in ast.walk(tree)
        if isinstance(owner, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and owner.lineno <= node.lineno <= (owner.end_lineno or owner.lineno)
    ]
    enclosing.sort(
        key=lambda owner: (
            owner.lineno,
            -((owner.end_lineno or owner.lineno) - owner.lineno),
        )
    )
    return ".".join(owner.name for owner in enclosing)


def _run_forever_violations(path: Path, tree: ast.AST) -> list[int]:
    violations: list[int] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        for call in ast.walk(function):
            if not (
                isinstance(call, ast.Call) and _dotted_name(call.func).endswith(".run_forever")
            ):
                continue
            if path != MCP_ROOT / "supervisor.py" or function.name != "_run_transport_loop":
                violations.append(call.lineno)
    return violations


def _process_spawn_violations(
    path: Path,
    tree: ast.AST,
) -> list[tuple[int, str]]:
    """Return process creation calls outside the current stdio adapter."""
    if path == MCP_ROOT / "transports" / "stdio.py":
        return []

    bindings = _call_bindings(tree)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _resolved_callable_name(node.func, bindings)
        if name in _PROCESS_SPAWN_APIS:
            violations.append((node.lineno, name))
    for line, module in _dynamic_imports(tree):
        if (
            module == _UNRESOLVED_DYNAMIC_IMPORT
            or module in {"anyio", "asyncio", "os", "subprocess", "trio"}
            or module.startswith("mcp.client.stdio")
        ):
            violations.append((line, f"dynamic:{module}"))
    return violations


def _network_client_violations(path: Path, tree: ast.AST) -> list[tuple[int, str]]:
    """Return networking clients outside the one current HTTP adapter."""
    if path == MCP_ROOT / "transports" / "streamable_http.py":
        return []

    bindings = _call_bindings(tree)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                if module == "httpx2" or module.startswith("httpx2."):
                    continue
                if any(
                    module == forbidden or module.startswith(f"{forbidden}.")
                    for forbidden in _ALTERNATE_NETWORK_MODULES
                ):
                    violations.append((node.lineno, module))
        elif isinstance(node, ast.Call):
            name = _resolved_callable_name(node.func, bindings)
            if name in _NETWORK_APIS:
                violations.append((node.lineno, name))
    for line, module in _dynamic_imports(tree):
        if (
            module == _UNRESOLVED_DYNAMIC_IMPORT
            or module == "httpx2"
            or module.startswith("httpx2.")
            or any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in _ALTERNATE_NETWORK_MODULES
            )
        ):
            violations.append((line, f"dynamic:{module}"))
    return violations


def test_loop_rail_rejects_work_generation_but_allows_event_loop_lifecycle() -> None:
    forbidden = ast.parse(
        "async def rogue(gateway):\n"
        "    while True:\n"
        "        await gateway.call_tool('server', 'tool', {})\n"
    )
    lifecycle = ast.parse("def _run_transport_loop(loop):\n    loop.run_forever()\n")
    arbitrary = ast.parse(
        "async def rogue(worker):\n    while True:\n        await worker.tick()\n"
    )

    assert _literal_work_loop_violations(forbidden) == [2]
    assert _literal_work_loop_violations(arbitrary) == [2]
    assert _literal_work_loop_violations(lifecycle) == []
    assert _run_forever_violations(MCP_ROOT / "supervisor.py", lifecycle) == []
    assert _run_forever_violations(Path("second_scheduler.py"), lifecycle) == [2]
    wrong_supervisor_function = ast.parse("def second_scheduler(loop):\n    loop.run_forever()\n")
    assert _run_forever_violations(MCP_ROOT / "supervisor.py", wrong_supervisor_function) == [2]


def test_loop_rail_rejects_conditional_and_unreachable_exit_bypasses() -> None:
    conditional = ast.parse(
        "async def rogue(worker, keep_running):\n"
        "    while keep_running:\n"
        "        await worker.tick()\n"
    )
    dead_exit = ast.parse(
        "async def rogue(worker):\n"
        "    while True:\n"
        "        if False:\n"
        "            break\n"
        "        await worker.tick()\n"
    )
    continue_before_exit = ast.parse(
        "async def rogue(worker):\n"
        "    while True:\n"
        "        continue\n"
        "        return await worker.tick()\n"
    )
    uncalled_nested_exit = ast.parse(
        "async def rogue(worker):\n"
        "    while True:\n"
        "        def never_called():\n"
        "            return\n"
        "        await worker.tick()\n"
    )

    assert _literal_work_loop_violations(conditional) == [2]
    assert _literal_work_loop_violations(dead_exit) == [2]
    assert _literal_work_loop_violations(continue_before_exit) == [2]
    assert _literal_work_loop_violations(uncalled_nested_exit) == [2]


def test_wait_loops_require_a_structural_bound_or_every_path_condition_progress() -> None:
    bare_event_wait = ast.parse(
        "async def rogue(signal, keep_running):\n"
        "    while keep_running:\n"
        "        await signal.wait()\n"
    )
    bare_condition_wait = ast.parse(
        "def rogue(condition, keep_running):\n    while keep_running:\n        condition.wait()\n"
    )
    aliased_wait = ast.parse(
        "async def rogue(signal, keep_running):\n"
        "    pause = signal.wait\n"
        "    while keep_running:\n"
        "        await pause()\n"
    )
    unrelated_assignment = ast.parse(
        "async def rogue(signal, keep_running):\n"
        "    while keep_running:\n"
        "        await signal.wait()\n"
        "        unrelated = False\n"
    )
    dead_mutation = ast.parse(
        "async def rogue(signal, keep_running):\n"
        "    while keep_running:\n"
        "        await signal.wait()\n"
        "        if False:\n"
        "            keep_running = False\n"
    )
    no_op_mutation = ast.parse(
        "def rogue(keep_running):\n    while keep_running:\n        keep_running = keep_running\n"
    )
    annotation_only = ast.parse(
        "def rogue(keep_running):\n    while keep_running:\n        keep_running: bool\n"
    )
    zero_augmented_mutation = ast.parse(
        "def rogue(remaining):\n    while remaining:\n        remaining += 0\n"
    )
    continue_before_mutation = ast.parse(
        "async def rogue(signal, keep_running):\n"
        "    while keep_running:\n"
        "        await signal.wait()\n"
        "        continue\n"
        "        keep_running = False\n"
    )
    partial_condition_mutation = ast.parse(
        "async def rogue(signal, keep_running, change_condition):\n"
        "    while keep_running:\n"
        "        if change_condition:\n"
        "            keep_running = False\n"
        "        else:\n"
        "            await signal.wait()\n"
    )
    mixed_bounded_wait = ast.parse(
        "async def rogue(signal, keep_running, bypass_timeout):\n"
        "    while keep_running:\n"
        "        if bypass_timeout:\n"
        "            await signal.wait()\n"
        "        else:\n"
        "            with anyio.fail_after(1):\n"
        "                await signal.wait()\n"
    )
    partial_wait_bound = ast.parse(
        "async def rogue(signal, keep_running, skip_wait):\n"
        "    while keep_running:\n"
        "        if skip_wait:\n"
        "            unrelated = True\n"
        "        else:\n"
        "            with anyio.fail_after(1):\n"
        "                await signal.wait()\n"
    )
    finite_timeout = ast.parse(
        "async def bounded(signal, keep_running):\n"
        "    while keep_running:\n"
        "        with anyio.fail_after(1):\n"
        "            await signal.wait()\n"
    )
    finite_deadline = ast.parse(
        "async def bounded(process, deadline):\n"
        "    while process.returncode is None:\n"
        "        if anyio.current_time() >= deadline:\n"
        "            return False\n"
        "        await anyio.sleep(0.01)\n"
    )
    every_iteration_mutates_condition = ast.parse(
        "async def bounded(signal, keep_running):\n"
        "    while keep_running:\n"
        "        await signal.wait()\n"
        "        keep_running = False\n"
    )
    exact_supervisor = ast.parse("def _run_transport_loop(loop):\n    loop.run_forever()\n")

    for mutation in (
        bare_event_wait,
        bare_condition_wait,
        unrelated_assignment,
        dead_mutation,
        no_op_mutation,
        annotation_only,
        zero_augmented_mutation,
        continue_before_mutation,
        partial_condition_mutation,
        mixed_bounded_wait,
        partial_wait_bound,
        finite_timeout,
    ):
        assert _literal_work_loop_violations(mutation) == [2]
    assert _literal_work_loop_violations(aliased_wait) == [3]
    assert _literal_work_loop_violations(finite_deadline) == []
    assert _literal_work_loop_violations(every_iteration_mutates_condition) == []
    assert _run_forever_violations(MCP_ROOT / "supervisor.py", exact_supervisor) == []


def test_literal_loop_exit_requires_unconditional_or_mandatory_finite_progress() -> None:
    nonliteral_conditional_break = ast.parse(
        "async def rogue(signal, keep_running, should_stop):\n"
        "    while keep_running:\n"
        "        await signal.wait()\n"
        "        if should_stop:\n"
        "            break\n"
        "        continue\n"
    )
    conditional_break = ast.parse(
        "async def rogue(signal, should_stop):\n"
        "    while True:\n"
        "        await signal.wait()\n"
        "        if should_stop:\n"
        "            break\n"
        "        continue\n"
    )
    exception_break = ast.parse(
        "async def rogue(signal):\n"
        "    while True:\n"
        "        try:\n"
        "            await signal.wait()\n"
        "        except RuntimeError:\n"
        "            break\n"
        "        continue\n"
    )
    continue_before_counter = ast.parse(
        "def rogue(remaining, skip_progress):\n"
        "    while True:\n"
        "        if skip_progress:\n"
        "            continue\n"
        "        remaining -= 1\n"
        "        if remaining <= 0:\n"
        "            break\n"
    )
    continue_before_deadline = ast.parse(
        "async def rogue(deadline, skip_check):\n"
        "    while True:\n"
        "        if skip_check:\n"
        "            continue\n"
        "        if anyio.current_time() >= deadline:\n"
        "            break\n"
        "        await anyio.sleep(0.01)\n"
    )
    nested_conditional_return = ast.parse(
        "def rogue(should_stop, may_return):\n"
        "    while True:\n"
        "        if should_stop:\n"
        "            if may_return:\n"
        "                return\n"
        "        continue\n"
    )
    unconditional_break = ast.parse("def bounded():\n    while True:\n        break\n")
    finite_counter = ast.parse(
        "def bounded(remaining):\n"
        "    while True:\n"
        "        remaining -= 1\n"
        "        if remaining <= 0:\n"
        "            break\n"
        "        continue\n"
    )
    finite_deadline = ast.parse(
        "async def bounded(deadline):\n"
        "    while True:\n"
        "        if anyio.current_time() >= deadline:\n"
        "            break\n"
        "        await anyio.sleep(0.01)\n"
    )
    spoofed_canonical_owner = ast.parse(
        "def _identity_and_digest(digest):\n"
        "    while True:\n"
        "        def dead(read, buffer, handle):\n"
        "            read_file(handle, buffer)\n"
        "            if read.value == 0:\n"
        "                return\n"
        "            raise ArtifactGuardError\n"
        "        digest.update(b'unbounded')\n"
    )

    for mutation in (
        nonliteral_conditional_break,
        conditional_break,
        exception_break,
        continue_before_counter,
        continue_before_deadline,
        nested_conditional_return,
    ):
        assert _literal_work_loop_violations(mutation) == [2]
    assert _literal_work_loop_violations(unconditional_break) == []
    assert _literal_work_loop_violations(finite_counter) == []
    assert _literal_work_loop_violations(finite_deadline) == []
    assert _literal_work_loop_violations(
        spoofed_canonical_owner, path=MCP_ROOT / "artifact_guard.py"
    ) == [2]


@pytest.mark.parametrize(
    "source",
    (
        "async def rogue(signal, should_stop):\n"
        "    while True:\n"
        "        await signal.wait(timeout=5)\n"
        "        if should_stop:\n"
        "            break\n"
        "        continue\n",
        "async def rogue(signal, should_stop):\n"
        "    while True:\n"
        "        with anyio.fail_after(5):\n"
        "            await signal.wait()\n"
        "        if should_stop:\n"
        "            break\n",
        "def rogue(remaining, should_stop):\n"
        "    while True:\n"
        "        remaining -= 1\n"
        "        if remaining <= 0 and should_stop:\n"
        "            break\n",
        "async def rogue(deadline, should_stop):\n"
        "    while True:\n"
        "        if anyio.current_time() >= deadline and should_stop:\n"
        "            break\n"
        "        await anyio.sleep(0.01)\n",
        "def rogue(condition, deadline, should_stop):\n"
        "    while condition():\n"
        "        remaining = deadline - time.monotonic()\n"
        "        if remaining <= 0 and should_stop:\n"
        "            break\n"
        "        condition.wait(timeout=remaining)\n",
    ),
    ids=(
        "timeout-argument",
        "timeout-context",
        "conditional-counter",
        "conditional-deadline",
        "conditional-derived-deadline",
    ),
)
def test_per_iteration_timeouts_and_conditional_guards_do_not_prove_total_bound(
    source: str,
) -> None:
    assert _literal_work_loop_violations(ast.parse(source)) == [2]


def test_total_loop_bound_adjacency_preserves_guaranteed_measure_and_exact_owner() -> None:
    unconditional_decrement = ast.parse(
        "def bounded(remaining):\n"
        "    while True:\n"
        "        remaining -= 1\n"
        "        if remaining <= 0:\n"
        "            break\n"
    )
    counter_implies_exit = ast.parse(
        "def bounded(remaining, should_stop):\n"
        "    while True:\n"
        "        remaining -= 1\n"
        "        if remaining <= 0 or should_stop:\n"
        "            break\n"
    )
    derived_deadline = ast.parse(
        "def bounded(condition, deadline):\n"
        "    while condition():\n"
        "        remaining = deadline - time.monotonic()\n"
        "        if remaining <= 0:\n"
        "            break\n"
        "        condition.wait(timeout=remaining)\n"
    )
    canonical_wait = ast.parse(
        "def start_default_mcp_host():\n"
        "    while _default_state in {'starting', 'stopping'}:\n"
        "        _default_condition.wait()\n"
    )
    canonical_wait_spoof = ast.parse(
        "def start_default_mcp_host(should_stop):\n"
        "    while _default_state in {'starting', 'stopping'}:\n"
        "        _default_condition.wait()\n"
        "        if should_stop:\n"
        "            continue\n"
    )

    assert _literal_work_loop_violations(unconditional_decrement) == []
    assert _literal_work_loop_violations(counter_implies_exit) == []
    assert _literal_work_loop_violations(derived_deadline) == []
    assert (
        _literal_work_loop_violations(
            canonical_wait,
            path=MCP_ROOT / "service.py",
        )
        == []
    )
    assert _literal_work_loop_violations(
        canonical_wait_spoof,
        path=MCP_ROOT / "service.py",
    ) == [2]


@pytest.mark.parametrize(
    "replacement",
    (
        "remaining, other = reset()",
        "[remaining, other] = reset()",
        "head, *remaining = reset()",
        "(other, [remaining]) = reset()",
        "del remaining",
        "if remaining := reset():\n            observe(remaining)",
        "remaining += reset()",
        "for remaining in values:\n            observe(remaining)",
        "with reset() as remaining:\n            observe(remaining)",
        "try:\n            observe()\n        except Exception as remaining:\n"
        "            observe(remaining)",
        "match value:\n            case remaining:\n                observe(remaining)",
    ),
    ids=(
        "tuple",
        "list",
        "starred",
        "nested",
        "delete",
        "walrus",
        "augassign",
        "for-target",
        "with-target",
        "except-target",
        "match-target",
    ),
)
def test_counter_bound_rejects_every_recursive_binding_replacement(replacement: str) -> None:
    source = (
        "def rogue(remaining, values):\n"
        "    while True:\n"
        "        remaining -= 1\n"
        "        if remaining <= 0:\n"
        "            break\n"
        f"        {replacement}\n"
    )

    assert _literal_work_loop_violations(ast.parse(source)) == [2]


@pytest.mark.parametrize(
    "replacement",
    (
        "deadline, other = reset()",
        "[deadline, other] = reset()",
        "head, *deadline = reset()",
        "(other, [deadline]) = reset()",
        "del deadline",
        "if deadline := reset():\n            observe(deadline)",
        "deadline += reset()",
        "try:\n            observe()\n        except Exception as deadline:\n"
        "            observe(deadline)",
        "match value:\n            case deadline:\n                observe(deadline)",
    ),
    ids=(
        "tuple",
        "list",
        "starred",
        "nested",
        "delete",
        "walrus",
        "augassign",
        "except-target",
        "match-target",
    ),
)
def test_deadline_bound_rejects_every_recursive_binding_replacement(replacement: str) -> None:
    source = (
        "def rogue(deadline):\n"
        "    while True:\n"
        "        if time.monotonic() >= deadline:\n"
        "            break\n"
        f"        {replacement}\n"
    )

    assert _literal_work_loop_violations(ast.parse(source)) == [2]


def test_bound_replacement_on_one_reachable_path_and_early_match_continue_are_rejected() -> None:
    conditional_replacement = ast.parse(
        "def rogue(remaining, should_reset):\n"
        "    while True:\n"
        "        remaining -= 1\n"
        "        if remaining <= 0:\n"
        "            break\n"
        "        if should_reset:\n"
        "            remaining, other = reset()\n"
    )
    match_continue = ast.parse(
        "def rogue(remaining, mode):\n"
        "    while True:\n"
        "        match mode:\n"
        "            case 'skip':\n"
        "                continue\n"
        "            case _:\n"
        "                observe()\n"
        "        remaining -= 1\n"
        "        if remaining <= 0:\n"
        "            break\n"
    )
    nested_location_reads = ast.parse(
        "def bounded(remaining, holder):\n"
        "    while True:\n"
        "        holder.value = remaining\n"
        "        holder[remaining] = observe()\n"
        "        remaining -= 1\n"
        "        if remaining <= 0:\n"
        "            break\n"
    )

    assert _literal_work_loop_violations(conditional_replacement) == [2]
    assert _literal_work_loop_violations(match_continue) == [2]
    assert _literal_work_loop_violations(nested_location_reads) == []


def test_artifact_traversal_allowance_requires_exact_qualified_owner_and_loop_digest() -> None:
    exact_loop = (
        "            while artifact_is_contained:\n"
        "                directories.append(directory)\n"
        "                if directory == root:\n"
        "                    break\n"
        "                parent = directory.parent\n"
        "                if parent == directory:\n"
        "                    raise ArtifactGuardError('artifact_identity_mismatch')\n"
        "                directory = parent\n"
    )
    canonical = ast.parse(
        "class ArtifactLaunchGuard:\n    @classmethod\n    def acquire(cls):\n" + exact_loop
    )
    conditional_backedge = ast.parse(
        "class ArtifactLaunchGuard:\n"
        "    @classmethod\n"
        "    def acquire(cls, should_skip):\n"
        "            while artifact_is_contained:\n"
        "                directories.append(directory)\n"
        "                if directory == root:\n"
        "                    break\n"
        "                parent = directory.parent\n"
        "                if parent == directory:\n"
        "                    raise ArtifactGuardError('artifact_identity_mismatch')\n"
        "                if should_skip:\n"
        "                    continue\n"
        "                directory = parent\n"
    )
    wrong_qualified_owner = ast.parse(
        "def acquire():\n" + exact_loop.replace("            ", "    ")
    )

    assert (
        _literal_work_loop_violations(
            canonical,
            path=MCP_ROOT / "artifact_guard.py",
        )
        == []
    )
    assert _literal_work_loop_violations(
        conditional_backedge,
        path=MCP_ROOT / "artifact_guard.py",
    ) == [4]
    assert _literal_work_loop_violations(
        wrong_qualified_owner,
        path=MCP_ROOT / "artifact_guard.py",
    ) == [2]


def test_supervisor_loop_owner_is_the_exact_canonical_module() -> None:
    lifecycle = ast.parse("def _run_transport_loop(loop):\n    loop.run_forever()\n")

    assert _run_forever_violations(MCP_ROOT / "supervisor.py", lifecycle) == []
    assert _run_forever_violations(
        MCP_ROOT / "nested" / "supervisor.py",
        lifecycle,
    ) == [2]


def test_mcp_platform_contains_no_second_forever_work_loop() -> None:
    violations = [
        (path.relative_to(ROOT).as_posix(), line)
        for path, tree in _modules()
        for line in (
            *_literal_work_loop_violations(tree, path=path),
            *_run_forever_violations(path, tree),
        )
    ]

    assert violations == []


def _sdk_client_violations(path: Path, tree: ast.Module) -> list[tuple[int, str]]:
    """Scan one module and release its symbolic-flow graph immediately."""
    try:
        return _sdk_client_violations_uncached(path, tree)
    finally:
        _symbolic_flow.cache_clear()


def _sdk_client_violations_uncached(path: Path, tree: ast.Module) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    final_state, snapshots = _symbolic_flow(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "mcp":
            for alias in node.names:
                if alias.name == "ClientSession":
                    violations.append((node.lineno, "mcp.ClientSession"))
                elif alias.name == "Client" and path not in _SDK_CLIENT_OWNER_PATHS:
                    violations.append((node.lineno, "mcp.Client"))
        if not isinstance(node, ast.Call):
            continue
        state = snapshots.get(id(node), final_state)
        names = _symbolic_call_names(node.func, state.callables, state.texts, state.sequences)
        if names is None:
            direct_name = _dotted_name(node.func)
            names = frozenset({direct_name}) if direct_name else frozenset()
        for name in sorted(names):
            leaf = name.rsplit(".", 1)[-1]
            if name == "mcp.Client" and path not in _SDK_CLIENT_OWNER_PATHS:
                violations.append((node.lineno, name))
            if (
                path != MCP_ROOT / "client.py"
                and leaf in _DIRECT_SDK_OPERATION_CALLS
                and not _known_non_sdk_operation(path, name)
            ):
                violations.append((node.lineno, name))

    for node in (item for item in ast.walk(tree) if isinstance(item, ast.ClassDef)):
        methods = {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        bases = {_dotted_name(base).rsplit(".", 1)[-1] for base in node.bases}
        if (
            (node.name != "CurrentMcpClient" or path != MCP_ROOT / "client.py")
            and "Protocol" not in bases
            and {"start", "close"} <= methods
            and methods & {"call_tool", "get_prompt", "read_resource"}
        ):
            violations.append((node.lineno, node.name))
    if path not in _SDK_CLIENT_OWNER_PATHS:
        violations.extend(
            (line, f"dynamic:{module}")
            for line, module in _dynamic_imports(tree)
            if module == _UNRESOLVED_DYNAMIC_IMPORT
            or module in {"mcp", "mcp.client", "mcp.client.session"}
        )
    return violations


def _known_non_sdk_operation(path: Path, name: str) -> bool:
    try:
        if path.is_relative_to(MCP_ROOT / "sdk"):
            return True
    except ValueError:
        return False
    return (path == MCP_ROOT / "authority.py" and name.startswith("self._supervisor.")) or (
        path == MCP_ROOT / "supervisor.py" and name.startswith("entry.session.")
    )


def test_only_current_client_owns_direct_mcp_sdk_operations() -> None:
    violations: list[tuple[str, int, str]] = []
    client_operations: list[int] = []
    for path, tree in _modules():
        violations.extend(
            (path.relative_to(ROOT).as_posix(), line, name)
            for line, name in _sdk_client_violations(path, tree)
        )
        if path != MCP_ROOT / "client.py":
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _dotted_name(node.func).rsplit(".", 1)[-1] in _DIRECT_SDK_OPERATION_CALLS:
                client_operations.append(node.lineno)

    assert violations == []
    assert client_operations


def _discovery_only_client_for_scan():
    """One discovery-only CurrentMcpClient -- no real transport needed: the
    guard every operation method funnels through fires before anything ever
    touches the SDK client factory, so a placeholder object stands in fine.
    """
    from mcp_platform.client import CurrentMcpClient
    from mcp_platform.client_contracts import (
        McpClientDeployment,
        McpClientLimits,
        McpConnectionDescriptor,
        McpTransport,
    )
    from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
    from plugin_platform.contracts import PluginAuthentication

    descriptor = McpConnectionDescriptor(
        app_id="vendor.weather",
        publisher_id="vendor",
        deployment=McpClientDeployment.REMOTE,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin="https://mcp.vendor.example.com",
        authorization_issuer="https://mcp.vendor.example.com",
        authentication=PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        declared_capabilities={},
        extension_versions={},
        capability_revision="0" * 64,
        limits=McpClientLimits(),
    )
    return CurrentMcpClient.for_restricted_discovery(descriptor, object())


def test_discovery_only_current_mcp_client_cannot_reach_execution_operations() -> None:
    """A not-yet-reviewed publisher-verified/personal connection's discovery
    client (``CurrentMcpClient.for_restricted_discovery``) is structurally
    incapable of ``tools/call``, ``prompts/get``, ``resources/read``, or a
    subscription -- every one of them fails closed with
    ``McpClientErrorReason.REVIEW_REQUIRED`` before the SDK client factory is
    ever touched, confirmed here for the four public operations most
    directly exercised (`list_tools`/`call_tool`/`get_prompt`/`read_resource`);
    ``tests/mcp_platform/test_client_discovery.py`` proves the same guard for
    every other operation method (`list_prompts`, `list_resources`,
    `list_resource_templates`, `listen`) against a real server."""
    import asyncio

    from mcp_platform.client_contracts import (
        McpClientError,
        McpClientErrorReason,
        McpOperationKind,
        McpOperationRequest,
    )

    client = _discovery_only_client_for_scan()

    def request(operation: McpOperationKind, capability_id: str) -> McpOperationRequest:
        return McpOperationRequest(
            request_id="architecture-scan-1",
            app_id="vendor.weather",
            operation=operation,
            capability_id=capability_id,
            arguments={},
            capability_revision="0" * 64,
            deadline_monotonic=1.0,
            cancellation_id="cancel-1",
            task_id=None,
            input_request_id=None,
            request_state=None,
        )

    async def exercise() -> list[McpClientErrorReason]:
        reasons = []
        for coroutine in (
            client.list_tools(request(McpOperationKind.TOOLS_LIST, "tools/list")),
            client.call_tool(request(McpOperationKind.TOOL_CALL, "tool:forecast")),
            client.get_prompt(request(McpOperationKind.PROMPT_GET, "prompt:summarize")),
            client.read_resource(request(McpOperationKind.RESOURCE_READ, "resource:almanac")),
        ):
            try:
                await coroutine
                raise AssertionError("must fail closed")
            except McpClientError as error:
                reasons.append(error.reason)
        return reasons

    reasons = asyncio.run(exercise())
    assert reasons == [McpClientErrorReason.REVIEW_REQUIRED] * 4


def test_discovery_only_current_mcp_client_cannot_start_an_ordinary_session() -> None:
    import asyncio

    from mcp_platform.client_contracts import McpClientError, McpClientErrorReason

    client = _discovery_only_client_for_scan()
    with pytest.raises(McpClientError) as raised:
        asyncio.run(client.start())
    assert raised.value.reason is McpClientErrorReason.REVIEW_REQUIRED


def test_current_client_scanner_rejects_imported_and_injected_second_sessions() -> None:
    imported = ast.parse(
        "from mcp import ClientSession\n"
        "async def invoke(sdk_session):\n"
        "    return await sdk_session.call_tool('tool', {})\n"
    )
    duplicated = ast.parse(
        "class AlternateClient:\n"
        "    async def start(self): pass\n"
        "    async def call_tool(self): pass\n"
        "    async def close(self): pass\n"
    )
    constructed = ast.parse(
        "import mcp\nPeer = getattr(mcp, 'Cli' + 'ent')\nalternate = Peer(lambda: None)\n"
    )
    dynamic = ast.parse(
        "from importlib import import_module\nprotocol = import_module('mcp.' + 'client')\n"
    )

    assert _sdk_client_violations(Path("rogue.py"), imported)
    assert _sdk_client_violations(Path("rogue.py"), duplicated) == [(1, "AlternateClient")]
    assert _sdk_client_violations(Path("rogue.py"), constructed)
    assert _sdk_client_violations(Path("rogue.py"), dynamic) == [(2, "dynamic:mcp.client")]


def test_current_client_owner_is_canonical_and_sdk_receivers_cannot_be_renamed() -> None:
    nested_owner_collision = ast.parse(
        "from mcp import Client\n"
        "async def invoke(transport):\n"
        "    return await transport.call_tool('tool', {})\n"
    )
    arbitrary_receiver = ast.parse(
        "async def invoke(protocol_peer):\n"
        "    return await protocol_peer.read_resource('omega://private')\n"
    )

    nested_violations = _sdk_client_violations(
        MCP_ROOT / "nested" / "client.py",
        nested_owner_collision,
    )
    assert (1, "mcp.Client") in nested_violations
    assert any(line == 3 and name.endswith("call_tool") for line, name in nested_violations)
    assert _sdk_client_violations(MCP_ROOT / "rogue.py", arbitrary_receiver) == [
        (2, "protocol_peer.read_resource")
    ]


def test_legacy_session_files_and_retired_protocol_literals_are_absent() -> None:
    assert (MCP_ROOT / "session.py").exists() is False
    assert (TEST_ROOT / "test_session.py").exists() is False

    violations = [
        (path.relative_to(ROOT).as_posix(), line)
        for root in (MCP_ROOT, TEST_ROOT)
        for path in sorted(root.rglob("*.py"))
        for line in _string_constants(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
            _RETIRED_PROTOCOL,
        )
    ]
    assert violations == []
    mutation = ast.parse(f"PROTOCOL = {_RETIRED_PROTOCOL!r}\n")
    assert _string_constants(mutation, _RETIRED_PROTOCOL) == [1]


def test_retired_protocol_scanner_folds_statement_bound_names_and_collections() -> None:
    mutation = ast.parse(
        "year = '2025'\n"
        "middle = '11' if enabled else '10'\n"
        "parts = [year, middle, '25']\n"
        "PROTOCOL = '-'.join(parts)\n"
    )

    assert _string_constants(mutation, _RETIRED_PROTOCOL) == [4]


def test_retired_protocol_scanner_folds_fstring_format_percent_and_join_fragments() -> None:
    mutations = (
        ast.parse("protocol = f'{2025:04d}-{11:02d}-{25:02d}'\n"),
        ast.parse("protocol = '{}-{}-{}'.format('2025', '11', '25')\n"),
        ast.parse("protocol = '%04d-%02d-%02d' % (2025, 11, 25)\n"),
        ast.parse(
            "year = 2025\n"
            "month = 11 if current else 10\n"
            "day = 25\n"
            "protocol = f'{year:04d}-{month:02d}-{day:02d}'\n"
        ),
        ast.parse(
            "year = '2025'\n"
            "month = '11' if current else '10'\n"
            "parts = [year, month, '25']\n"
            "protocol = '-'.join(parts)\n"
        ),
        ast.parse(
            "def hidden(current):\n"
            "    year = '2025'\n"
            "    month = '11' if current else '10'\n"
            "    protocol = '-'.join((year, month, '25'))\n"
        ),
    )

    assert [_string_constants(tree, _RETIRED_PROTOCOL) for tree in mutations] == [
        [1],
        [1],
        [1],
        [4],
        [4],
        [4],
    ]


def test_private_sdk_and_plugin_registry_seams_are_rejected() -> None:
    private_sdk = [
        (path.relative_to(ROOT).as_posix(), line, name)
        for path, tree in _modules()
        for line, name in _private_sdk_import_violations(tree)
    ]
    private_registry = [
        (path.relative_to(ROOT).as_posix(), line, name)
        for path, tree in _modules()
        for line, name in _private_plugin_registry_violations(tree)
    ]

    assert private_sdk == []
    assert private_registry == []


def test_private_boundary_scanners_catch_alias_dynamic_and_nested_bypasses() -> None:
    sdk_cases = (
        "from mcp.client import _transport\n",
        "import mcp._internal.transport\n",
        "from importlib import import_module as load\nload('mcp._internal.transport')\n",
        "import importlib\nimportlib.import_module('mcp.' + '_internal.transport')\n",
        "import mcp as protocol\nprotocol._internal.open()\n",
        "import mcp as protocol\ngetattr(protocol, '_' + 'internal').open()\n",
    )
    registry_cases = (
        "from plugin_platform.registry import _load\n",
        "import plugin_platform.registry.internal\n",
        "import importlib\nimportlib.import_module('plugin_platform.registry.internal')\n",
        ("import importlib\nimportlib.import_module('plugin_platform.' + 'registry.internal')\n"),
        "import plugin_platform.registry as store\nstore._read_document()\n",
        "from plugin_platform import registry as store\ngetattr(store, '_' + 'load')()\n",
        "app_registry._read_document()\n",
    )

    assert all(_private_sdk_import_violations(ast.parse(source)) for source in sdk_cases)
    assert all(_private_plugin_registry_violations(ast.parse(source)) for source in registry_cases)


def test_current_client_and_provisioner_import_no_product_cognition_or_provider_modules() -> None:
    violations: list[tuple[str, int, str]] = []
    for path, tree in _modules():
        if path not in _CLIENT_PROVISIONER_PATHS:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for module in modules:
                    normalized = module.casefold()
                    root = normalized.partition(".")[0]
                    if root in _FORBIDDEN_COUPLING_ROOTS or any(
                        normalized == prefix or normalized.startswith(f"{prefix}.")
                        for prefix in _PROVIDER_MODULE_PREFIXES
                    ):
                        violations.append((path.name, node.lineno, module))
        for line, module in _dynamic_imports(tree):
            normalized = module.casefold()
            if (
                module == _UNRESOLVED_DYNAMIC_IMPORT
                or normalized.partition(".")[0] in _FORBIDDEN_COUPLING_ROOTS
                or any(
                    normalized == prefix or normalized.startswith(f"{prefix}.")
                    for prefix in _PROVIDER_MODULE_PREFIXES
                )
            ):
                violations.append((path.name, line, module))

    assert violations == []


def test_only_ingress_bridge_admits_external_queue_input() -> None:
    imports: list[Path] = []
    calls: list[Path] = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "pressure_loop.cognition_queue"
                and any(alias.name == "enqueue_input" for alias in node.names)
            ):
                imports.append(path)
            elif (
                isinstance(node, ast.Call)
                and _dotted_name(node.func).rsplit(".", 1)[-1] == "enqueue_input"
            ):
                calls.append(path)

    assert [path.name for path in imports] == ["ingress.py"]
    assert [path.name for path in calls] == ["ingress.py"]


def test_mcp_platform_has_no_direct_canonical_memory_write() -> None:
    violations: list[tuple[str, int, str]] = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted_name(node.func)
            leaf = name.rsplit(".", 1)[-1]
            if leaf in {"write_memory", "intake_memory"} or name.endswith("episode_sram.hold"):
                violations.append((path.name, node.lineno, name))

    assert violations == []


def test_current_runtime_routes_exact_stdio_process_creation_only_through_adapter() -> None:
    provider_imports: list[tuple[str, str]] = []
    remote_imports: list[tuple[str, str]] = []
    stdio_owners: list[str] = []
    for path, tree in _modules():
        for module in _imported_modules(tree):
            normalized = module.casefold()
            if any(
                normalized == prefix or normalized.startswith(prefix + ".")
                for prefix in _PROVIDER_MODULE_PREFIXES
            ):
                provider_imports.append((path.name, module))
            if module.startswith(_REMOTE_MCP_MODULES):
                remote_imports.append((path.name, module))
        if any(
            isinstance(node, ast.AsyncFunctionDef) and node.name == "stdio_client"
            for node in ast.walk(tree)
        ):
            stdio_owners.append(path.name)

    assert provider_imports == []
    assert remote_imports == []
    assert stdio_owners == ["stdio.py"]

    supervisor = ast.parse(
        (MCP_ROOT / "supervisor.py").read_text(encoding="utf-8"),
        filename=str(MCP_ROOT / "supervisor.py"),
    )
    assert "mcp_platform.session" not in _imported_modules(supervisor)
    assert "mcp_platform.transports.stdio" in _imported_modules(supervisor)

    current_creation_calls: list[tuple[str, int, str]] = []
    for path, tree in _modules():
        bindings = _call_bindings(tree)
        current_creation_calls.extend(
            (path.name, node.lineno, _resolved_callable_name(node.func, bindings))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _resolved_callable_name(node.func, bindings)
            in {
                "anyio.open_process",
                "mcp.os.win32.utilities.create_windows_process",
            }
        )
    assert current_creation_calls
    assert {name for name, _line, _call in current_creation_calls} == {"stdio.py"}
    assert {call for _name, _line, call in current_creation_calls} == {
        "anyio.open_process",
        "mcp.os.win32.utilities.create_windows_process",
    }

    stdio_tree = ast.parse(
        (MCP_ROOT / "transports" / "stdio.py").read_text(encoding="utf-8"),
        filename=str(MCP_ROOT / "transports" / "stdio.py"),
    )
    assert all(
        not any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        for node in ast.walk(stdio_tree)
        if isinstance(node, ast.Call)
    )


def test_process_spawn_rail_resolves_direct_and_imported_alias_calls() -> None:
    forbidden = (
        ("import subprocess\nsubprocess.Popen(['server'])\n", "subprocess.Popen"),
        ("import subprocess as sp\nsp.run(['server'])\n", "subprocess.run"),
        (
            "from subprocess import check_output as launch\nlaunch(['server'])\n",
            "subprocess.check_output",
        ),
        ("import os\nos.posix_spawn('server', ['server'], {})\n", "os.posix_spawn"),
        ("from os import spawnv as launch\nlaunch(0, 'server', [])\n", "os.spawnv"),
        (
            "import asyncio\nasyncio.create_subprocess_exec('server')\n",
            "asyncio.create_subprocess_exec",
        ),
        (
            "from asyncio import create_subprocess_shell as launch\nlaunch('server')\n",
            "asyncio.create_subprocess_shell",
        ),
        ("import anyio\nanyio.open_process(['server'])\n", "anyio.open_process"),
        (
            "from anyio import run_process as launch\nlaunch(['server'])\n",
            "anyio.run_process",
        ),
        ("import trio\ntrio.run_process(['server'])\n", "trio.run_process"),
        (
            "from trio import run_process as launch\nlaunch(['server'])\n",
            "trio.run_process",
        ),
        (
            "import subprocess\nlaunch = subprocess.Popen\nlaunch(['server'])\n",
            "subprocess.Popen",
        ),
        (
            "import subprocess\nlaunch = getattr(subprocess, 'Popen')\nlaunch(['server'])\n",
            "subprocess.Popen",
        ),
    )

    for source, canonical_name in forbidden:
        assert _process_spawn_violations(
            Path("rogue.py"),
            ast.parse(source),
        ) == [
            (
                3 if "launch =" in source else 2,
                canonical_name,
            )
        ]

    dynamic = ast.parse("import importlib\nimportlib.import_module('subprocess')\n")
    assert _process_spawn_violations(Path("rogue.py"), dynamic) == [(2, "dynamic:subprocess")]


def test_process_spawn_rail_allows_stdio_owner_and_finite_lifecycle_helpers() -> None:
    authorized_stdio = ast.parse(
        "from anyio import open_process as launch\n"
        "async def open_server():\n"
        "    return await launch(['server'])\n"
    )
    finite_lifecycle = ast.parse(
        "async def close_server(process):\n    process.terminate()\n    await process.wait()\n"
    )
    basename_collision = ast.parse(
        "import anyio as runtime\n"
        "async def open_server():\n"
        "    return await runtime.open_process(['server'])\n"
    )

    assert _process_spawn_violations(MCP_ROOT / "transports" / "stdio.py", authorized_stdio) == []
    assert _process_spawn_violations(Path("lifecycle.py"), finite_lifecycle) == []
    assert _process_spawn_violations(
        MCP_ROOT / "rogue" / "session.py",
        basename_collision,
    ) == [(3, "anyio.open_process")]


def test_dynamic_process_creation_cannot_escape_the_stdio_adapter() -> None:
    process_calls = [
        (path.name, line, name)
        for path, tree in _modules()
        for line, name in _process_spawn_violations(path, tree)
    ]

    assert process_calls == []


def test_network_rail_rejects_direct_and_aliased_alternate_clients() -> None:
    forbidden = (
        ("import requests\nrequests.get('https://example.com')\n", "requests.get"),
        (
            "from aiohttp import ClientSession as Session\nSession()\n",
            "aiohttp.ClientSession",
        ),
        ("import httpx as hx\nhx.AsyncClient()\n", "httpx.AsyncClient"),
        (
            "from urllib.request import urlopen as fetch\nfetch('https://example.com')\n",
            "urllib.request.urlopen",
        ),
        ("import urllib3 as pool\npool.PoolManager()\n", "urllib3"),
        (
            "from http.client import HTTPSConnection as Connect\nConnect('example.com')\n",
            "http.client",
        ),
        ("import socket as net\nnet.socket()\n", "socket"),
        (
            "import httpx as hx\nopen_client = hx.AsyncClient\nopen_client()\n",
            "httpx.AsyncClient",
        ),
    )

    for source, canonical_name in forbidden:
        violations = _network_client_violations(Path("rogue.py"), ast.parse(source))
        assert any(name == canonical_name for _line, name in violations)

    dynamic = ast.parse("from importlib import import_module\nimport_module('urllib3')\n")
    assert _network_client_violations(Path("rogue.py"), dynamic) == [(2, "dynamic:urllib3")]


def test_only_streamable_http_adapter_owns_network_client_creation() -> None:
    violations = [
        (path.relative_to(ROOT).as_posix(), line, name)
        for path, tree in _modules()
        for line, name in _network_client_violations(path, tree)
    ]
    assert violations == []

    owners: list[str] = []
    for path, tree in _modules():
        bindings = _call_bindings(tree)
        if any(
            isinstance(node, ast.Call)
            and _resolved_callable_name(node.func, bindings)
            in {"httpx2.AsyncClient", "httpx2.Client"}
            for node in ast.walk(tree)
        ):
            owners.append(path.name)
    assert owners == ["streamable_http.py"]


# --------------------------------------------------------------------------
# The two checks above walk `_modules()` (`mcp_platform/` only), so a raw
# httpx2 client construction planted anywhere ELSE in the repository --
# scripts/, plugin_platform/, hud/ -- would previously pass unnoticed. These
# companions are deliberately lightweight (a plain AST walk, not the full
# symbolic-flow engine `_modules()`'s other consumers need) and cover the
# WHOLE production tree.
# --------------------------------------------------------------------------

#: Owns httpx2 client construction. Nothing else may call
#: ``httpx2.AsyncClient``/``httpx2.Client`` anywhere in the repository.
_HTTPX2_CLIENT_OWNER = MCP_ROOT / "transports" / "streamable_http.py"
#: Every production file allowed to import httpx2 at all -- the owner plus
#: every direct caller of its factories (`catalog_fetch.py`/`registry_fetch.py`
#: via `build_bounded_sync_http_client`, `oauth_protocol.py` via
#: `build_bounded_http_client`) or receiver of the httpx2 `Request`/`Response`/
#: `Auth` objects the owner hands it without ever constructing a client itself
#: (`authorization.py`). Importing httpx2 anywhere else is a regression this
#: gate must catch, even where nothing is ever constructed.
_HTTPX2_IMPORT_ALLOWED_PATHS = frozenset(
    {
        _HTTPX2_CLIENT_OWNER,
        MCP_ROOT / "catalog_fetch.py",
        MCP_ROOT / "registry_fetch.py",
        MCP_ROOT / "oauth_protocol.py",
        MCP_ROOT / "authorization.py",
        # The maintainer capture path. It reaches a provider with the
        # operator's own token to read a first tool surface, which the
        # ordinary client cannot do: that client refuses a surface Omega
        # has not signed, and restricted discovery is unauthenticated by
        # construction. It lives here rather than under scripts/ so this
        # rail keeps holding -- every outbound provider request stays in
        # one auditable package.
        MCP_ROOT / "provider_capture.py",
        # Package artifact downloads use the bounded sync client's transport,
        # timeout, and exception types; they do not construct a client here.
        MCP_ROOT / "package_fetch.py",
    }
)


def _repo_python_modules(root: Path) -> list[tuple[Path, ast.Module]]:
    """Parse every production (non test/doc/cache) .py file in the whole repository."""
    collected: list[tuple[Path, ast.Module]] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if not name.startswith(".") and name not in _REPO_SKIPPED_DIRECTORIES
        )
        parent = Path(directory)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = parent / filename
            try:
                collected.append(
                    (
                        path,
                        ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path)),
                    )
                )
            except SyntaxError:
                continue
    return collected


def _repo_wide_httpx2_offenders(
    *,
    root: Path | None = None,
    owner: Path | None = None,
    import_allowed: frozenset[Path] | None = None,
) -> tuple[tuple[str, int, str], ...]:
    base = ROOT if root is None else root
    owner_path = _HTTPX2_CLIENT_OWNER if owner is None else owner
    allowed = _HTTPX2_IMPORT_ALLOWED_PATHS if import_allowed is None else import_allowed
    offenders: list[tuple[str, int, str]] = []
    for path, tree in _repo_python_modules(base):
        relative = path.relative_to(base).as_posix()
        bindings = _call_bindings(tree)
        if path != owner_path:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _resolved_callable_name(node.func, bindings)
                if name in {"httpx2.AsyncClient", "httpx2.Client"}:
                    offenders.append((relative, node.lineno, name))
        if path not in allowed:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for module in modules:
                    if module == "httpx2" or module.startswith("httpx2."):
                        offenders.append((relative, node.lineno, f"import:{module}"))
    return tuple(offenders)


def test_httpx2_client_construction_is_confined_to_the_streamable_http_owner_repo_wide() -> None:
    """Catches a raw httpx2 client construction planted anywhere outside mcp_platform/.

    ``test_only_streamable_http_adapter_owns_network_client_creation`` already
    proves this within ``mcp_platform/`` using the full symbolic-flow engine;
    a construction under ``scripts/``, ``plugin_platform/``, or anywhere else
    in the tree previously passed unnoticed because `_modules()` only walks
    ``mcp_platform/``.
    """

    offenders = tuple(
        item for item in _repo_wide_httpx2_offenders() if item[2] in {"httpx2.AsyncClient", "httpx2.Client"}
    )
    assert offenders == ()


def test_httpx2_import_is_confined_to_its_owner_and_direct_callers_repo_wide() -> None:
    """Catches an httpx2 import planted outside the closed, evidence-based allowlist."""

    offenders = tuple(item for item in _repo_wide_httpx2_offenders() if item[2].startswith("import:"))
    assert offenders == ()


def test_repo_wide_httpx2_gate_rejects_a_planted_construction_under_scripts(
    tmp_path: Path,
) -> None:
    """Catches the repo-wide scan missing a client built outside mcp_platform/."""

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "rogue_fetch.py").write_text(
        "import httpx2\n\nclient = httpx2.AsyncClient()\n",
        encoding="utf-8",
    )

    offenders = _repo_wide_httpx2_offenders(
        root=tmp_path,
        owner=tmp_path / "___no_owner___.py",
        import_allowed=frozenset(),
    )

    found = {(item[0], item[2]) for item in offenders}
    assert ("scripts/rogue_fetch.py", "httpx2.AsyncClient") in found
    assert ("scripts/rogue_fetch.py", "import:httpx2") in found


def _unapproved_protocol_violations(tree: ast.Module) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    final_state, snapshots = _symbolic_flow(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            state = snapshots.get(id(node), final_state)
            names = _symbolic_call_names(node.func, state.callables, state.texts, state.sequences)
            if names is None:
                direct_name = _dotted_name(node.func)
                names = frozenset({direct_name}) if direct_name else frozenset()
            forbidden = sorted(
                name for name in names if name.rsplit(".", 1)[-1] in _FORBIDDEN_MCP_FEATURE_CALLS
            )
            if forbidden:
                violations.append((node.lineno, forbidden[0]))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            violations.extend(
                (node.lineno, module)
                for module in modules
                if "experimental.tasks" in module or module.startswith(_REMOTE_MCP_MODULES)
            )
    violations.extend(
        (line, f"dynamic:{module}")
        for line, module in _dynamic_imports(tree)
        if module == _UNRESOLVED_DYNAMIC_IMPORT
        or "experimental.tasks" in module
        or module.startswith(_REMOTE_MCP_MODULES)
    )
    return violations


def test_unapproved_protocol_features_do_not_escape_the_current_client() -> None:
    violations = [
        (path.name, line, name)
        for path, tree in _modules()
        for line, name in _unapproved_protocol_violations(tree)
    ]

    assert violations == []

    session_header_uses = [
        (path.name, line)
        for path, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and type(node.value) is str
        and node.value.casefold() == "mcp-session-id"
        for line in (node.lineno,)
    ]
    # The sole occurrence cryptographically binds any injected legacy header so
    # redirects cannot add or change it; it is not read as session authority.
    assert len(session_header_uses) == 1
    assert session_header_uses[0][0] == "authorization.py"

    aliased_call = ast.parse(
        "async def legacy(sdk):\n"
        "    operation = getattr(sdk, 'init' + 'ialize')\n"
        "    await operation()\n"
    )
    dynamic_sse = ast.parse(
        "from importlib import import_module\n"
        "transport = import_module('.'.join(('mcp', 'client', 'sse')))\n"
    )
    assert _unapproved_protocol_violations(aliased_call)
    assert _unapproved_protocol_violations(dynamic_sse) == [(2, "dynamic:mcp.client.sse")]


def test_protocol_scanner_folds_bound_aliases_collections_and_dynamic_imports() -> None:
    assembled_initialize = ast.parse(
        "prefix = 'init'\n"
        "suffix = 'ialize' if enabled else 'ialized'\n"
        "method_name = ''.join([prefix, suffix])\n"
        "async def legacy(protocol_peer):\n"
        "    operation = getattr(protocol_peer, method_name)\n"
        "    return await operation()\n"
    )
    assembled_sse = ast.parse(
        "from importlib import import_module as load\n"
        "root = 'mcp'\n"
        "segments = [root, 'client', 'sse']\n"
        "module_name = '.'.join(segments)\n"
        "loader = load\n"
        "transport_module = loader(module_name)\n"
        "open_transport = transport_module.sse_client\n"
        "open_transport()\n"
    )
    unresolved_dynamic = ast.parse(
        "from importlib import import_module\n"
        "module_name = choose_transport()\n"
        "import_module(module_name)\n"
    )

    assert _unapproved_protocol_violations(assembled_initialize)
    assert _unapproved_protocol_violations(assembled_sse) == [(6, "dynamic:mcp.client.sse")]
    assert _unapproved_protocol_violations(unresolved_dynamic) == [(3, "dynamic:<unresolved>")]


def test_protocol_scanner_fails_closed_for_selected_loaders_and_collection_targets() -> None:
    function_selected = ast.parse("loader = choose_loader()\nloader('mcp.' + 'client.sse')\n")
    attribute_selected = ast.parse(
        "owner = choose_loader_owner()\nowner.load('mcp.client.' + 'sse')\n"
    )
    injected_loader = ast.parse("def invoke(loader):\n    return loader('mcp.client.sse')\n")
    injected_attribute_loader = ast.parse(
        "def invoke(owner):\n    return owner.load('mcp.client.sse')\n"
    )
    lambda_wrapper = ast.parse(
        "from importlib import import_module\n"
        "loader = lambda target: import_module(target)\n"
        "loader('mcp.client.sse')\n"
    )
    collection_target = ast.parse(
        "targets = ['json', 'mcp.client.sse']\n"
        "loader = choose_loader()\n"
        "loader(targets[selected])\n"
    )
    keyword_target = ast.parse(
        "loader = choose_loader() if enabled else choose_other_loader()\n"
        "loader(module='mcp.client.sse')\n"
    )
    importlib_alias = ast.parse(
        "from importlib import import_module as load\nload('mcp.client.sse')\n"
    )
    unrelated_dynamic = ast.parse(
        "renderer = choose_renderer()\nrenderer('ordinary display text')\n"
    )
    safe_nonforbidden_module = ast.parse("loader = choose_loader()\nloader('json')\n")
    safe_canonical_loader = ast.parse(
        "from importlib import import_module\nimport_module('json')\n"
    )

    for mutation in (
        function_selected,
        attribute_selected,
        injected_loader,
        injected_attribute_loader,
        lambda_wrapper,
        collection_target,
        keyword_target,
        importlib_alias,
    ):
        assert _unapproved_protocol_violations(mutation)
    assert _unapproved_protocol_violations(unrelated_dynamic) == []
    assert _unapproved_protocol_violations(safe_nonforbidden_module) == []
    assert _unapproved_protocol_violations(safe_canonical_loader) == []


def test_symbolic_bindings_overwrite_sequentially_and_merge_control_flow_paths() -> None:
    unknown_then_known = ast.parse(
        "target = choose_target()\n"
        "target = 'mcp.client.sse'\n"
        "loader = choose_loader()\n"
        "loader(target)\n"
    )
    known_then_unknown = ast.parse(
        "from importlib import import_module\n"
        "target = 'mcp.client.sse'\n"
        "target = choose_target()\n"
        "import_module(target)\n"
    )
    conditional_unknown = ast.parse(
        "from importlib import import_module\n"
        "target = 'mcp.client.sse'\n"
        "if enabled:\n"
        "    target = choose_target()\n"
        "import_module(target)\n"
    )
    finally_rebind = ast.parse(
        "target = choose_target()\n"
        "try:\n"
        "    inspect_target()\n"
        "finally:\n"
        "    target = 'mcp.client.sse'\n"
        "loader = choose_loader()\n"
        "loader(target)\n"
    )
    loop_carried_unknown = ast.parse(
        "from importlib import import_module\n"
        "target = 'mcp.client.sse'\n"
        "while enabled:\n"
        "    target = choose_target()\n"
        "import_module(target)\n"
    )
    tuple_destructuring = ast.parse(
        "targets = ('json', 'mcp.client.sse')\n"
        "safe_target, target = targets\n"
        "loader = choose_loader()\n"
        "loader(target)\n"
    )
    subscript_target = ast.parse(
        "targets = ('json', 'mcp.client.sse')\n"
        "target = targets[selected]\n"
        "loader = choose_loader()\n"
        "loader(target)\n"
    )
    loader_rebind = ast.parse(
        "from importlib import import_module\n"
        "loader = choose_loader()\n"
        "loader = import_module\n"
        "loader('mcp.client.sse')\n"
    )
    safe_call_before_forbidden_rebind = ast.parse(
        "from importlib import import_module\n"
        "target = 'json'\n"
        "import_module(target)\n"
        "target = 'mcp.client.sse'\n"
    )
    forbidden_call_before_safe_rebind = ast.parse(
        "from importlib import import_module\n"
        "target = 'mcp.client.sse'\n"
        "import_module(target)\n"
        "target = 'json'\n"
    )
    safe_unrelated = ast.parse(
        "target = choose_target()\n"
        "target = 'ordinary display text'\n"
        "renderer = choose_renderer()\n"
        "renderer(target)\n"
    )
    unknown_then_known_operation = ast.parse(
        "async def invoke(peer):\n"
        "    operation = choose_operation()\n"
        "    operation = peer.initialize\n"
        "    await operation()\n"
    )
    safe_operation_before_forbidden_rebind = ast.parse(
        "async def invoke(peer):\n"
        "    operation = peer.render\n"
        "    await operation()\n"
        "    operation = peer.initialize\n"
    )
    safe_sdk_call_before_forbidden_rebind = ast.parse(
        "async def invoke(peer):\n"
        "    operation = peer.render\n"
        "    await operation()\n"
        "    operation = peer.call_tool\n"
    )

    assert _unapproved_protocol_violations(unknown_then_known) == [(4, "dynamic:mcp.client.sse")]
    assert _unapproved_protocol_violations(known_then_unknown) == [(4, "dynamic:<unresolved>")]
    assert _unapproved_protocol_violations(conditional_unknown) == [(5, "dynamic:<unresolved>")]
    assert _unapproved_protocol_violations(finally_rebind) == [(7, "dynamic:mcp.client.sse")]
    assert _unapproved_protocol_violations(loop_carried_unknown) == [(5, "dynamic:<unresolved>")]
    assert _unapproved_protocol_violations(tuple_destructuring) == [(4, "dynamic:mcp.client.sse")]
    assert _unapproved_protocol_violations(subscript_target) == [(4, "dynamic:mcp.client.sse")]
    assert _unapproved_protocol_violations(loader_rebind) == [(4, "dynamic:mcp.client.sse")]
    assert _unapproved_protocol_violations(safe_call_before_forbidden_rebind) == []
    assert _unapproved_protocol_violations(forbidden_call_before_safe_rebind) == [
        (3, "dynamic:mcp.client.sse")
    ]
    assert _unapproved_protocol_violations(safe_unrelated) == []
    assert _unapproved_protocol_violations(unknown_then_known_operation) == [(4, "peer.initialize")]
    assert _unapproved_protocol_violations(safe_operation_before_forbidden_rebind) == []
    assert (
        _sdk_client_violations(MCP_ROOT / "rogue.py", safe_sdk_call_before_forbidden_rebind) == []
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "if target := 'mcp.client.sse':\n"
            "    import_module(target)\n",
            [(4, "dynamic:mcp.client.sse")],
        ),
        (
            "loader = choose_loader()\nloader(target := 'mcp.client.sse')\n",
            [(2, "dynamic:mcp.client.sse")],
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while target := 'mcp.client.sse':\n"
            "    import_module(target)\n"
            "    break\n",
            [(4, "dynamic:mcp.client.sse")],
        ),
        (
            "async def invoke(peer):\n    (operation := peer.initialize)()\n",
            [(2, "peer.initialize")],
        ),
    ),
    ids=("condition", "call-argument", "loop-condition", "callable"),
)
def test_named_expressions_update_symbolic_state_at_the_evaluation_point(
    source: str,
    expected: list[tuple[int, str]],
) -> None:
    assert _unapproved_protocol_violations(ast.parse(source)) == expected


def test_safe_unrelated_named_expression_is_not_a_dynamic_import() -> None:
    safe_unrelated_rebind = ast.parse(
        "renderer = choose_renderer()\nrenderer(target := 'ordinary display text')\n"
    )

    assert _unapproved_protocol_violations(safe_unrelated_rebind) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "from importlib import import_module\n"
            "value = 'mcp.client.sse'\n"
            "target = 'json'\n"
            "match value:\n"
            "    case target:\n"
            "        import_module(target)\n",
            [(6, "dynamic:mcp.client.sse")],
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "match value:\n"
            "    case target:\n"
            "        import_module(target)\n",
            [(5, "dynamic:<unresolved>")],
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "match value:\n"
            "    case [*target]:\n"
            "        import_module(target)\n",
            [(5, "dynamic:<unresolved>")],
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "match value:\n"
            "    case {'module': target, **rest}:\n"
            "        import_module(target)\n",
            [(5, "dynamic:<unresolved>")],
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "match value:\n"
            "    case Descriptor(module=target):\n"
            "        import_module(target)\n",
            [(5, "dynamic:<unresolved>")],
        ),
    ),
    ids=("direct", "unknown", "star", "mapping", "class"),
)
def test_match_capture_patterns_replace_prior_symbolic_bindings(
    source: str,
    expected: list[tuple[int, str]],
) -> None:
    assert _unapproved_protocol_violations(ast.parse(source)) == expected


def test_safe_unrelated_match_capture_is_not_a_dynamic_import() -> None:
    safe_unrelated_capture = ast.parse(
        "target = 'ordinary display text'\n"
        "match value:\n"
        "    case {'label': target}:\n"
        "        renderer(target)\n"
    )

    assert _unapproved_protocol_violations(safe_unrelated_capture) == []


def test_match_capture_replaces_from_the_pre_capture_subject_state() -> None:
    same_named_subject = ast.parse(
        "from importlib import import_module\n"
        "target = 'json'\n"
        "match target:\n"
        "    case target:\n"
        "        import_module(target)\n"
    )

    assert _unapproved_protocol_violations(same_named_subject) == []


@pytest.mark.parametrize(("depth", "expected_line"), ((4, 12), (8, 20), (16, 36)))
def test_loop_symbolic_flow_reaches_fixpoint_beyond_three_transfers(
    depth: int,
    expected_line: int,
) -> None:
    source = [
        "from importlib import import_module",
        "slot_0 = 'mcp.client.sse'",
        *(f"slot_{index} = 'json'" for index in range(1, depth + 1)),
        "while enabled:",
        *(f"    slot_{index} = slot_{index - 1}" for index in range(depth, 0, -1)),
        f"import_module(slot_{depth})",
    ]

    assert _unapproved_protocol_violations(ast.parse("\n".join(source))) == [
        (expected_line, "dynamic:mcp.client.sse")
    ]


def test_callable_alias_flow_reaches_a_finite_fixpoint_and_widens_rebinds() -> None:
    chain = ast.parse(
        "\n".join(
            [
                "from importlib import import_module",
                *(f"slot_{index} = slot_{index + 1}" for index in range(16)),
                "slot_16 = import_module",
            ]
        )
    )
    ambiguous = ast.parse(
        "from importlib import import_module\n"
        "import json\n"
        "loader = import_module\n"
        "loader = json.loads\n"
    )

    assert _call_bindings(chain)["slot_0"] == "importlib.import_module"
    assert "loader" not in _call_bindings(ambiguous)


def test_symbolic_branch_unions_fail_closed_without_flagging_safe_dynamic_calls() -> None:
    known_union = ast.parse(
        "from importlib import import_module\n"
        "target = 'json'\n"
        "if enabled:\n"
        "    target = 'mcp.client.sse'\n"
        "import_module(target)\n"
    )
    unknown_known_union = ast.parse(
        "from importlib import import_module\n"
        "if enabled:\n"
        "    target = choose_target()\n"
        "else:\n"
        "    target = 'mcp.client.sse'\n"
        "import_module(target)\n"
    )
    safe_canonical_union = ast.parse(
        "from importlib import import_module\n"
        "target = 'json' if enabled else 'pathlib'\n"
        "import_module(target)\n"
    )
    safe_unrelated_union = ast.parse(
        "target = 'ordinary display text' if enabled else 'another label'\n"
        "renderer = choose_renderer()\n"
        "renderer(target)\n"
    )

    assert _unapproved_protocol_violations(known_union) == [(5, "dynamic:mcp.client.sse")]
    assert _unapproved_protocol_violations(unknown_known_union) == [(6, "dynamic:<unresolved>")]
    assert _unapproved_protocol_violations(safe_canonical_union) == []
    assert _unapproved_protocol_violations(safe_unrelated_union) == []


@pytest.mark.parametrize(
    ("source", "expected_line"),
    (
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    if should_stop:\n"
            "        target = 'mcp.client.sse'\n"
            "        continue\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            8,
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    if should_stop:\n"
            "        target = 'mcp.client.sse'\n"
            "        break\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            8,
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    try:\n"
            "        target = 'mcp.client.sse'\n"
            "        continue\n"
            "    finally:\n"
            "        observe()\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            10,
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    try:\n"
            "        target = 'mcp.client.sse'\n"
            "        break\n"
            "    finally:\n"
            "        observe()\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            10,
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    try:\n"
            "        target = 'mcp.client.sse'\n"
            "    finally:\n"
            "        if should_stop:\n"
            "            continue\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            10,
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    try:\n"
            "        target = 'mcp.client.sse'\n"
            "    finally:\n"
            "        if should_stop:\n"
            "            break\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            10,
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    match mode:\n"
            "        case 'stop':\n"
            "            target = 'mcp.client.sse'\n"
            "            continue\n"
            "        case _:\n"
            "            observe()\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            11,
        ),
        (
            "from importlib import import_module\n"
            "target = 'json'\n"
            "while enabled:\n"
            "    match mode:\n"
            "        case 'stop':\n"
            "            target = 'mcp.client.sse'\n"
            "            break\n"
            "        case _:\n"
            "            observe()\n"
            "    target = 'json'\n"
            "import_module(target)\n",
            11,
        ),
    ),
    ids=(
        "if-continue",
        "if-break",
        "try-continue",
        "try-break",
        "finally-continue",
        "finally-break",
        "match-continue",
        "match-break",
    ),
)
def test_nested_loop_control_outcomes_preserve_point_in_time_state(
    source: str,
    expected_line: int,
) -> None:
    assert _unapproved_protocol_violations(ast.parse(source)) == [
        (expected_line, "dynamic:mcp.client.sse")
    ]


def test_exception_handlers_join_every_try_statement_prefix() -> None:
    intermediate_forbidden_target = ast.parse(
        "from importlib import import_module\n"
        "target = 'json'\n"
        "try:\n"
        "    target = 'mcp.client.sse'\n"
        "    first_risk()\n"
        "    target = 'pathlib'\n"
        "    second_risk()\n"
        "    target = 'json'\n"
        "except RuntimeError:\n"
        "    import_module(target)\n"
    )

    assert _unapproved_protocol_violations(intermediate_forbidden_target) == [
        (10, "dynamic:mcp.client.sse")
    ]


@pytest.mark.parametrize(
    "source",
    (
        "from importlib import import_module\n"
        "match (('mcp.client.sse',),):\n"
        "    case ((target,),):\n"
        "        import_module(target)\n",
        "from importlib import import_module\n"
        "match {'outer': {'module': 'mcp.client.sse'}}:\n"
        "    case {'outer': {'module': target}}:\n"
        "        import_module(target)\n",
        "from importlib import import_module\n"
        "match Envelope(payload=Descriptor(module='mcp.client.sse')):\n"
        "    case Envelope(payload=Descriptor(module=target)):\n"
        "        import_module(target)\n",
        "from importlib import import_module\n"
        "match ('json', 'mcp.client.sse'):\n"
        "    case [_, *targets]:\n"
        "        import_module(targets[0])\n",
        "from importlib import import_module\n"
        "match ('mcp.client.sse',):\n"
        "    case (target,) | [target]:\n"
        "        import_module(target)\n",
    ),
    ids=("sequence", "mapping", "class", "star", "or"),
)
def test_recursive_match_patterns_transfer_nested_capture_values(source: str) -> None:
    assert (4, "dynamic:mcp.client.sse") in _unapproved_protocol_violations(ast.parse(source))


@pytest.mark.parametrize(
    ("source", "expected_line"),
    (
        (
            "loader = choose_loader()\ntarget = choose_target('mcp.client.sse')\nloader(target)\n",
            3,
        ),
        (
            "from importlib import import_module\n"
            "loader = choose_loader(import_module)\n"
            "target = choose_target()\n"
            "loader(target)\n",
            4,
        ),
        (
            "import importlib\n"
            "choose_loader(importlib.import_module)(choose_target('mcp.client.sse'))\n",
            2,
        ),
    ),
    ids=("forbidden-target-evidence", "loader-evidence", "both-inline"),
)
def test_unknown_loader_and_target_fail_closed_with_dynamic_import_evidence(
    source: str,
    expected_line: int,
) -> None:
    assert (expected_line, "dynamic:<unresolved>") in _unapproved_protocol_violations(
        ast.parse(source)
    )


def test_unknown_loader_and_target_without_forbidden_evidence_remain_safe() -> None:
    unrelated_unknown = ast.parse(
        "loader = choose_loader()\ntarget = choose_target()\nloader(target)\n"
    )
    unrelated_literal = ast.parse(
        "renderer = choose_renderer()\n"
        "payload = choose_payload('ordinary display text')\n"
        "renderer(payload)\n"
    )
    overwritten_evidence = ast.parse(
        "renderer = choose_renderer()\n"
        "target = 'mcp.client.sse'\n"
        "target = choose_target()\n"
        "renderer(target)\n"
    )

    assert _unapproved_protocol_violations(unrelated_unknown) == []
    assert _unapproved_protocol_violations(unrelated_literal) == []
    assert _unapproved_protocol_violations(overwritten_evidence) == []


def test_inference_boundary_imports_no_model_or_runtime_client() -> None:
    forbidden_roots = {
        "auto_loop",
        "llama_cpp",
        "anthropic",
        "deepsleep_loop",
        "global_loop",
        "memory",
        "meta_loop",
        "ollama",
        "openai",
        "omega_loop",
        "pressure_loop",
        "runtime",
        "torch",
        "transformers",
    }
    violations: list[tuple[str, str]] = []
    for path in sorted((MCP_ROOT / "inference").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module.partition(".")[0] in forbidden_roots:
                violations.append((path.name, module))

    assert violations == []


def test_legacy_oauth_paths_are_absent() -> None:
    sources = tuple(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "mcp_platform" / "plugin_connections.py",
            ROOT / "mcp_platform" / "authorization.py",
            ROOT / "hud" / "server.py",
            ROOT / "hud" / "ui-src" / "panels" / "settings-plugins.js",
        )
    )
    forbidden = (
        "OMEGA_GOOGLE_OAUTH_CLIENT_CONFIG",
        "OMEGA_GOOGLE_OAUTH_CLIENT_ID",
        "OMEGA_GOOGLE_OAUTH_REDIRECT_URI",
        "OMEGA_GITHUB_OAUTH_CLIENT_CONFIG",
        "OMEGA_GITHUB_OAUTH_CLIENT_ID",
        "OMEGA_GITHUB_OAUTH_REDIRECT_URI",
        "is_google_account_issuer",
        "is_github_account_issuer",
        "/oauth/google/callback",
        "/oauth/github/callback",
        "google_oauth_client_config_invalid",
        "google_oauth_client_id_unconfigured",
        "github_oauth_client_config_invalid",
        "github_oauth_client_id_unconfigured",
    )

    assert not any(value in source for source in sources for value in forbidden)
    assert not (ROOT / "mcp_platform" / "google_account_partition.py").exists()
    assert not (ROOT / "config" / "plugins" / "google_workspace.yaml").exists()
    assert not (ROOT / "config" / "plugins" / "README.md").exists()
    assert not (TEST_ROOT / "test_google_account_grants.py").exists()
    assert not (TEST_ROOT / "test_google_workspace_configuration.py").exists()
    assert (TEST_ROOT / "test_shared_oauth_grants.py").is_file()


def test_lifecycle_runtime_has_no_legacy_signing_trust_path() -> None:
    source = (MCP_ROOT / "plugin_lifecycle.py").read_text(encoding="utf-8")

    forbidden = (
        "_LEGACY_FEATURED_KEY_RING",
        "_FEATURED_SAME_VERSION_REPAIRS",
        "_repair_same_version_release",
        "featured-repair-",
    )

    assert not any(value in source for value in forbidden)


_COGNITION_PRODUCTION_ROOTS = (
    "core",
    "pressure_loop",
    "auto_loop",
    "omega_loop",
    "runtime",
    "hud",
    "memory",
    "mcp_platform",
    "deepsleep_loop",
)
_RETIRED_SCHEDULER_MODULES = frozenset(
    {
        "pressure_loop.operator_queue",
        "pressure_loop.lane_dispatch",
        "pressure_loop.lane_pressure",
        "core.lane_state",
        "core.lanes",
    }
)
_RETIRED_AFFINITY_NAMES = frozenset({"LANE_SEAT", "LANE_SLOTS"})
_ALLOWED_LINE_LANE_DECODERS = frozenset(
    {
        (Path("core/pipeline_state.py"), "_recover_old_envelopes"),
        (Path("pressure_loop/cognition_queue.py"), "normalize_legacy_input"),
    }
)


def _cognition_production_modules() -> Iterator[tuple[Path, ast.Module]]:
    for package in _COGNITION_PRODUCTION_ROOTS:
        for path in sorted((ROOT / package).rglob("*.py")):
            yield (
                path,
                ast.parse(
                    path.read_text(encoding="utf-8", errors="replace"),
                    filename=str(path),
                ),
            )
    for name in ("boot.py", "cognition_chain.py"):
        path = ROOT / name
        yield (
            path,
            ast.parse(
                path.read_text(encoding="utf-8", errors="replace"),
                filename=str(path),
            ),
        )


def _retired_topology_violations(path: Path, tree: ast.Module) -> list[tuple[int, str]]:
    relative = path.relative_to(ROOT)
    violations: list[tuple[int, str]] = []
    decoder_candidates = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (relative, node.name) in _ALLOWED_LINE_LANE_DECODERS
    ]
    allowed_decoder = decoder_candidates[0] if len(decoder_candidates) == 1 else None

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.in_allowed_decoder_body = False

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous = self.in_allowed_decoder_body

            # Defaults, annotations, decorators, and type parameters are evaluated
            # outside the function body and never inherit the migration exemption.
            self.in_allowed_decoder_body = False
            for decorator in node.decorator_list:
                self.visit(decorator)
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
            for type_parameter in getattr(node, "type_params", ()):
                self.visit(type_parameter)

            # Only statements owned directly by the one exact top-level decoder
            # node receive the legacy-field allowance. A nested function resets it.
            self.in_allowed_decoder_body = node is allowed_decoder
            for statement in node.body:
                self.visit(statement)
            self.in_allowed_decoder_body = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            previous = self.in_allowed_decoder_body
            self.in_allowed_decoder_body = False
            self.generic_visit(node)
            self.in_allowed_decoder_body = previous

        def visit_Lambda(self, node: ast.Lambda) -> None:
            previous = self.in_allowed_decoder_body
            self.in_allowed_decoder_body = False
            self.generic_visit(node)
            self.in_allowed_decoder_body = previous

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name in _RETIRED_SCHEDULER_MODULES:
                    violations.append((node.lineno, alias.name))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            if module in _RETIRED_SCHEDULER_MODULES:
                violations.append((node.lineno, module))
            for alias in node.names:
                qualified = f"{module}.{alias.name}" if module else alias.name
                if qualified in _RETIRED_SCHEDULER_MODULES:
                    violations.append((node.lineno, qualified))

        def visit_Name(self, node: ast.Name) -> None:
            if node.id in _RETIRED_AFFINITY_NAMES:
                violations.append((node.lineno, node.id))

        def visit_Constant(self, node: ast.Constant) -> None:
            if node.value in _RETIRED_AFFINITY_NAMES:
                violations.append((node.lineno, str(node.value)))
            if node.value == "line_lane" and not self.in_allowed_decoder_body:
                violations.append((node.lineno, "line_lane"))

        def visit_Dict(self, node: ast.Dict) -> None:
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in {"chat", "agent", "meta", "window"}
                    and isinstance(value, ast.Constant)
                    and type(value.value) is int
                    and value.value in range(4)
                ):
                    violations.append((node.lineno, f"{key.value}->{value.value}"))
            self.generic_visit(node)

    Visitor().visit(tree)
    violations.extend(
        (line, module)
        for line, module in _dynamic_imports(tree)
        if module in _RETIRED_SCHEDULER_MODULES
    )
    return violations


def _law_section(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def _scan_retired_topology_modules(
    modules: Iterable[tuple[Path, ast.Module]],
    *,
    scanner: Callable[[Path, ast.Module], list[tuple[int, str]]] = (
        _retired_topology_violations
    ),
) -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    _symbolic_flow.cache_clear()
    try:
        for path, tree in modules:
            try:
                violations.extend(
                    (path.relative_to(ROOT).as_posix(), line, name)
                    for line, name in scanner(path, tree)
                )
            finally:
                # _symbolic_flow caches the AST key plus a cloned state snapshot
                # per expression. A repo-wide scan must own at most one module's
                # flow graph at a time, including when its scanner raises.
                _symbolic_flow.cache_clear()
    finally:
        _symbolic_flow.cache_clear()
    return violations


def test_cognition_production_module_source_is_not_process_cached() -> None:
    assert not hasattr(_cognition_production_modules, "cache_info")


def test_retired_topology_module_scan_bounds_and_releases_symbolic_cache() -> None:
    modules = (
        (ROOT / "first.py", ast.parse("import importlib\nname = 'safe'\n")),
        (ROOT / "second.py", ast.parse("value = 'ordinary'\n")),
    )
    observed_cache_sizes: list[int] = []

    def recording_scanner(path: Path, tree: ast.Module) -> list[tuple[int, str]]:
        del path
        observed_cache_sizes.append(_symbolic_flow.cache_info().currsize)
        _symbolic_flow(tree)
        return []

    def raising_scanner(path: Path, tree: ast.Module) -> list[tuple[int, str]]:
        del path
        _symbolic_flow(tree)
        raise RuntimeError("planted scanner failure")

    _symbolic_flow.cache_clear()
    _symbolic_flow(ast.parse("seed = 'must be released'\n"))
    try:
        assert _scan_retired_topology_modules(modules, scanner=recording_scanner) == []
        assert observed_cache_sizes == [0, 0]
        assert _symbolic_flow.cache_info().currsize == 0

        with pytest.raises(RuntimeError, match="planted scanner failure"):
            _scan_retired_topology_modules(modules[:1], scanner=raising_scanner)
        assert _symbolic_flow.cache_info().currsize == 0
    finally:
        _symbolic_flow.cache_clear()


def test_production_has_one_queue_and_no_semantic_pipeline_affinity() -> None:
    violations = _scan_retired_topology_modules(_cognition_production_modules())

    assert violations == []
    for retired in (
        "pressure_loop/operator_queue.py",
        "pressure_loop/lane_dispatch.py",
        "pressure_loop/lane_pressure.py",
        "core/lane_state.py",
        "core/lanes.py",
    ):
        assert not (ROOT / retired).exists()
    assert (ROOT / "pressure_loop" / "cognition_queue.py").is_file()


def test_retired_topology_scanner_is_structural_and_decoder_allowance_is_narrow() -> None:
    planted = ast.parse(
        "import pressure_loop.operator_queue\n"
        "from importlib import import_module\n"
        "import_module('pressure_loop.lane_dispatch')\n"
        "LANE_SEAT = {'chat': 0}\n"
        "def live_intake(metadata):\n"
        "    return metadata.get('line_lane')\n"
    )
    allowed_decoder = ast.parse(
        "def normalize_legacy_input(metadata):\n"
        "    metadata.pop('line_lane', None)\n"
    )
    assert _retired_topology_violations(ROOT / "boot.py", planted) == [
        (1, "pressure_loop.operator_queue"),
        (4, "LANE_SEAT"),
        (4, "chat->0"),
        (6, "line_lane"),
        (3, "pressure_loop.lane_dispatch"),
    ]
    assert (
        _retired_topology_violations(
            ROOT / "pressure_loop" / "cognition_queue.py",
            allowed_decoder,
        )
        == []
    )


def test_retired_topology_scanner_rejects_package_member_imports() -> None:
    package_members = ast.parse(
        "from pressure_loop import operator_queue as retired_queue\n"
        "from core import lane_state as retired_state\n"
    )

    assert _retired_topology_violations(ROOT / "boot.py", package_members) == [
        (1, "pressure_loop.operator_queue"),
        (2, "core.lane_state"),
    ]


def test_line_lane_decoder_allowance_is_exact_top_level_body() -> None:
    scoped_bypasses = ast.parse(
        "@marker('line_lane')\n"
        "def normalize_legacy_input(metadata='line_lane'):\n"
        "    class normalize_legacy_input:\n"
        "        value = 'line_lane'\n"
        "    def normalize_legacy_input():\n"
        "        return 'line_lane'\n"
        "    return metadata.pop('line_lane', None)\n"
        "class Decoder:\n"
        "    def normalize_legacy_input(self):\n"
        "        return 'line_lane'\n"
    )

    assert sorted(
        _retired_topology_violations(
            ROOT / "pressure_loop" / "cognition_queue.py",
            scoped_bypasses,
        )
    ) == [
        (1, "line_lane"),
        (2, "line_lane"),
        (4, "line_lane"),
        (6, "line_lane"),
        (10, "line_lane"),
    ]


def test_governing_law_requires_four_dedicated_semantic_fifos() -> None:
    law = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    queue_law = _law_section(law, "### 4.0", "### 4.1")
    chat_law = _law_section(law, "### 4.1", "## 5.")
    execution_law = _law_section(law, "### 7.1", "## 8.")
    normalized_queue_law = " ".join(queue_law.casefold().split())
    normalized_chat_law = " ".join(chat_law.casefold().split())
    normalized_execution_law = " ".join(execution_law.casefold().split())

    for required in (
        "four semantic lanes are exactly `chat`, `browser`, `agent` (coding agent), and `window`",
        "physical pipeline IDs `0`, `1`, `2`, and `3` are permanently bound to those lanes",
        "Each lane admits one active episode and preserves FCFS order within its own line",
        "arrival_seq",
        "A busy lane does not block admission or execution in another lane",
        "Up to four predictions may run at once, one per lane",
        "No work preempts a running lane episode",
        "exact `queue_id`",
    ):
        assert " ".join(required.casefold().split()) in normalized_queue_law
    for required in (
        "Every HUD turn enters the durable Chat FIFO",
        "Global A classifies the intent inside its pressure-opened Chat episode",
        "correlated `chat.reply`",
        "correlated `chat.update`",
    ):
        assert " ".join(required.casefold().split()) in normalized_chat_law
    for required in (
        "The configured model/runtime serves Chat, Browser, Coding Agent, and 10-Window Reflection",
        "four permanent semantic pipeline bindings",
        "execution control state",
        "not canonical memory",
        "execution report",
        "SRAM",
        "DeepSleep",
    ):
        assert " ".join(required.casefold().split()) in normalized_execution_law
