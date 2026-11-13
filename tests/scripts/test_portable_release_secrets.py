import shutil
import subprocess
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT


def _predicate_script() -> str:
    source = (OMEGA_ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8")
    start = source.index("function Test-SkippedPart")
    predicate = source.index("function Test-ReleaseRelativePath", start)
    brace = source.index("{", predicate)
    depth = 0
    for index in range(brace, len(source)):
        depth += source[index] == "{"
        depth -= source[index] == "}"
        if depth == 0:
            if index >= brace and depth == 0:
                return source[start : index + 1]
    raise AssertionError("unbalanced predicate")


def _allowed(path: str) -> bool:
    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell is required")
    command = _predicate_script() + f"\nif (Test-ReleaseRelativePath '{path}') {{ exit 0 }} else {{ exit 1 }}"
    return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True).returncode == 0


@pytest.mark.parametrize("path", [
    "guardian/release-secrets/key.sealed", "guardian/state/mcp/authorization/oauth-vault.json",
    "guardian/state/mcp/authorization/token.json", "guardian/state/mcp/authorization/tokens.json",
    "guardian/state/mcp/authorization/refresh-token.json", "guardian/state/mcp/authorization/connections.json",
    "guardian/state/mcp/authorization/connection.json", "guardian/state/mcp/registry.json",
    "browser/session.json", "browser-agent/profile/session.json", "browser_agent_extension/sessions/cookies.json",
])
def test_predicate_rejects_forbidden_paths(path: str) -> None:
    assert not _allowed(path)


@pytest.mark.parametrize("path", [
    "plugin_platform/marketplace/catalog.json", "schema/mcp/package_manifest.schema.json",
    "plugin_platform/oauth/public_metadata.json", "plugin_platform/trust/public_keys.json",
    "browser_agent/bootstrap.py", "browser_agent/lifecycle.py",
])
def test_predicate_keeps_public_metadata(path: str) -> None:
    assert _allowed(path)


@pytest.mark.parametrize("relative", [
    "browser-agent/profile.json",
    "browser-agent/profile/nested/session.json",
    "browser_agent_extension/session.json",
])
def test_final_verifier_rejects_browser_artifacts(tmp_path: Path, relative: str) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell is required")
    source = (OMEGA_ROOT / "scripts" / "verify_portable.ps1").read_text(encoding="utf-8")
    start = source.index("function Assert-ReleaseSecrets")
    end = source.index("function Assert-Manifest", start)
    helper = source[source.index("function Get-PortableRelativePath") : source.index("function Assert-NoAbsolutePaths")]
    nested = tmp_path / "Omega" / relative
    nested.parent.mkdir(parents=True)
    nested.write_text("{}", encoding="utf-8")
    command = helper + source[start:end] + f"\nAssert-ReleaseSecrets '{tmp_path}'; exit 0"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True)
    assert result.returncode != 0


def test_final_verifier_allows_source_keys_but_rejects_json_secret(tmp_path: Path) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell is required")
    source = (OMEGA_ROOT / "scripts" / "verify_portable.ps1").read_text(encoding="utf-8")
    start = source.index("function Assert-ReleaseSecrets")
    end = source.index("function Assert-Manifest", start)
    helper = source[source.index("function Get-PortableRelativePath") : source.index("function Assert-NoAbsolutePaths")]
    for module in ("authorization.py", "oauth_broker.py", "oauth_protocol.py", "plugin_connections.py"):
        path = tmp_path / "Omega" / "mcp_platform" / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"access_token" == field\n"refresh_token": schema_key\n', encoding="utf-8")
    leak = tmp_path / "Omega" / "config" / "oauth.json"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text('{"access_token": "secret-value"}', encoding="utf-8")
    command = helper + source[start:end] + f"\nAssert-ReleaseSecrets '{tmp_path}'; exit 0"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True)
    assert result.returncode != 0
    leak.unlink()
    command = helper + source[start:end] + f"\nAssert-ReleaseSecrets '{tmp_path}'; exit 0"
    assert subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True).returncode == 0
