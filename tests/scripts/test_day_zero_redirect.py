"""Medium #23 — day_zero silently ignored a redirected knowledge-artifact store.

_resolve_policy_path(path: Path) called path.is_absolute(), but the policy accessor may return a
str. _resolve_policy_path(str) raised AttributeError, which wipe_knowledge_extractor_state's broad
`except Exception` swallowed -> it fell back to the hardcoded default and wiped the WRONG (or no)
store, leaving the redirected store untouched. Fix: coerce str->Path; narrow the except.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import scripts.day_zero_reset as dz
from scripts.day_zero_reset import OMEGA_ROOT, _resolve_policy_path


def test_resolve_policy_path_accepts_str():
    # a str (as some policy accessors return) must resolve, not raise (str-vs-Path bug)
    p = _resolve_policy_path("knowledge_extractor/state/index.json")
    assert isinstance(p, Path)
    assert p == OMEGA_ROOT / "knowledge_extractor/state/index.json"


def test_redirected_store_is_honored_not_silently_ignored(tmp_path, monkeypatch):
    # redirect OMEGA_ROOT into tmp so even the fallback default is harmless
    monkeypatch.setattr(dz, "OMEGA_ROOT", tmp_path)
    import pressure_loop.policy as policy

    redirected = tmp_path / "redir_store"
    redirected.mkdir()
    (redirected / "stale.json").write_text("{}", encoding="utf-8")

    # the accessor returns a STR pointing at the redirected store (the bug case)
    monkeypatch.setattr(
        policy, "knowledge_artifact_store_path", lambda: str(redirected / "index.json")
    )

    result = dz.wipe_knowledge_extractor_state()
    assert not (redirected / "stale.json").exists()  # the redirected store was actually wiped
    assert result["removed"] >= 1


def test_resolve_policy_path_rejects_path_outside_portable_root(tmp_path, monkeypatch):
    """A stale policy cannot make factory reset delete an external workspace."""
    portable_root = tmp_path / "omega"
    external_root = tmp_path / "operator-workspace"
    portable_root.mkdir()
    external_root.mkdir()
    monkeypatch.setattr(dz, "OMEGA_ROOT", portable_root)

    with pytest.raises(dz.DayZeroResetError, match="outside portable Omega root"):
        _resolve_policy_path(external_root / "episode_journal.jsonl")
