from __future__ import annotations

import ast
from pathlib import Path

from mcp_platform.inference import PreparedMcpInference

ROOT = Path(__file__).resolve().parents[2]
INFERENCE_ROOT = ROOT / "mcp_platform" / "inference"
FORBIDDEN_IMPORT_PREFIXES = (
    "runtime.client",
    "runtime.model_control",
    "core.pipelines",
    "openai",
    "google.genai",
)
FORBIDDEN_SUBMISSION_CALLS = {
    "submit",
    "generate",
    "generate_content",
    "chat",
    "complete",
    "completion",
    "infer",
    "run_lane",
    "enqueue_lane",
    "call_model",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _called_names(tree: ast.AST) -> set[str]:
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    return called


def test_mcp_inference_package_has_no_model_or_lane_submission_imports() -> None:
    for path in INFERENCE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        assert not any(
            name.startswith(FORBIDDEN_IMPORT_PREFIXES) for name in imported
        ), path


def test_mcp_inference_package_has_no_model_or_lane_submission_calls() -> None:
    for path in INFERENCE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert FORBIDDEN_SUBMISSION_CALLS.isdisjoint(_called_names(tree)), path
        assert not any(isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)) for node in ast.walk(tree)), path


def test_prepared_contract_has_no_submission_or_credential_fields() -> None:
    forbidden = {
        "model",
        "endpoint",
        "future",
        "ticket",
        "timeout_url",
        "api_key",
        "token",
        "client_secret",
        "vault_key",
        "provider_credential",
        "lane_client",
        "submission_id",
    }
    assert forbidden.isdisjoint(PreparedMcpInference.__dataclass_fields__)
