"""The operator account key partitions every plugin surface, so it must exist.

Requiring an environment variable no production path ever set meant every
plugin answered "The Omega operator account is not configured for plugins",
which is what the HUD showed on a real install. The key is an identifier, not a
credential: it is hashed to partition the runtime catalog, the permission store
and every stored connection, so it has to be stable for the life of an
installation and travel with a portable one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_platform.plugin_lifecycle import (
    PluginLifecycleService,
    _durable_operator_account_key,
)


@pytest.fixture(autouse=True)
def _no_pinned_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMEGA_OPERATOR_ACCOUNT_KEY", raising=False)


def test_the_key_is_born_once_and_read_back_afterwards(tmp_path: Path) -> None:
    """A key that changed between launches would orphan every stored grant."""
    first = _durable_operator_account_key(tmp_path)
    second = _durable_operator_account_key(tmp_path)

    assert first == second
    assert len(first) == 64
    document = json.loads((tmp_path / "operator-account.json").read_bytes())
    assert document == {"schema_version": 1, "account_key": first}


def test_two_installations_do_not_share_a_partition(tmp_path: Path) -> None:
    assert _durable_operator_account_key(tmp_path / "one") != _durable_operator_account_key(
        tmp_path / "two"
    )


def test_a_pinned_environment_key_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests and operators pinning a partition keep the old behaviour."""
    monkeypatch.setattr("core.paths.MCP_AUTHORIZATION_ROOT", tmp_path)
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "  pinned-account  ")

    assert PluginLifecycleService.operator_account_key() == "pinned-account"
    assert not (tmp_path / "operator-account.json").exists()


def test_an_unset_environment_no_longer_leaves_plugins_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a regression back to 'operator account not configured'."""
    monkeypatch.setattr("core.paths.MCP_AUTHORIZATION_ROOT", tmp_path)

    key = PluginLifecycleService.operator_account_key()

    assert key and key == PluginLifecycleService.operator_account_key()
    assert (tmp_path / "operator-account.json").is_file()


def test_a_corrupt_account_file_fails_closed_rather_than_repartitioning(
    tmp_path: Path,
) -> None:
    """Silently minting a second key would strand the credentials under the first."""
    (tmp_path / "operator-account.json").write_bytes(b"{ not json")

    with pytest.raises(ValueError, match="^plugin_account_unconfigured$"):
        _durable_operator_account_key(tmp_path)

    (tmp_path / "operator-account.json").write_bytes(json.dumps({"account_key": ""}).encode())
    with pytest.raises(ValueError, match="^plugin_account_unconfigured$"):
        _durable_operator_account_key(tmp_path)
