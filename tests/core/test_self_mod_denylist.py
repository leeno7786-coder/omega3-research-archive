"""§8.8 — the self-mod denylist. An APPLY may write real source (that is the
whole point), but it must NEVER modify its OWN safety system or its mind state:
the staged-approval gate, the guard/dispatcher/checkpoint code, or any of the
gitignored mind-state dirs. The entire reversibility + staged-graduation model is
defeated if a run can edit ``skill_execution_guards.py`` or self-grant by writing the gate
file. So classification fails SAFE — an unclassifiable or out-of-jail touched path
counts as a violation (block), because a false positive only refuses an APPLY
while a false negative defeats the guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto_loop.execute.self_mod_denylist import (  # noqa: E402
    PROTECTED_CODE_FILES,
    is_protected_path,
    self_mod_violations,
)


def _abs(root, rel):
    return str(Path(root) / rel)


# --------------------------------------------------------------------------
# mind-state dirs are protected (reused from core.mind_state_surface)
# --------------------------------------------------------------------------


def test_gate_file_is_protected(tmp_path):
    assert is_protected_path(
        _abs(tmp_path, "guardian/state/apply_authorization.json"), omega_root=tmp_path
    )


def test_memory_lobe_is_protected(tmp_path):
    assert is_protected_path(_abs(tmp_path, "memory/800_coding/x.json"), omega_root=tmp_path)


def test_memory_indexes_is_protected(tmp_path):
    assert is_protected_path(_abs(tmp_path, "memory/indexes/coord_index.json"), omega_root=tmp_path)


def test_pressure_state_is_protected(tmp_path):
    assert is_protected_path(_abs(tmp_path, "pressure_loop/state/p.json"), omega_root=tmp_path)


def test_deepsleep_state_is_protected(tmp_path):
    assert is_protected_path(_abs(tmp_path, "deepsleep_loop/state/d.json"), omega_root=tmp_path)


# --------------------------------------------------------------------------
# safety-critical CODE files are protected
# --------------------------------------------------------------------------


def test_skill_execution_guards_module_is_protected(tmp_path):
    assert is_protected_path(
        _abs(tmp_path, "auto_loop/execute/skill_execution_guards.py"), omega_root=tmp_path
    )


def test_run_dispatcher_is_protected(tmp_path):
    assert is_protected_path(_abs(tmp_path, "auto_loop/execute/run.py"), omega_root=tmp_path)


def test_mind_checkpoint_is_protected(tmp_path):
    assert is_protected_path(_abs(tmp_path, "guardian/mind_checkpoint.py"), omega_root=tmp_path)


def test_mind_state_surface_is_protected(tmp_path):
    assert is_protected_path(_abs(tmp_path, "core/mind_state_surface.py"), omega_root=tmp_path)


def test_live_chat_executor_is_protected(tmp_path):
    # The live-chat tool boundary — a run must not weaken its own executor or
    # turn the bounded native tool surface into an unguarded one.
    assert is_protected_path(
        _abs(tmp_path, "auto_loop/execute/brain_skill_executor.py"), omega_root=tmp_path
    )


def test_denylist_module_protects_itself(tmp_path):
    assert is_protected_path(
        _abs(tmp_path, "auto_loop/execute/self_mod_denylist.py"), omega_root=tmp_path
    )
    assert "auto_loop/execute/self_mod_denylist.py" in PROTECTED_CODE_FILES


def test_model_control_is_protected(tmp_path):
    # The §11 dev-pill GPU hotswap the guard ITSELF invokes (prepare/release_dev_pill_
    # runtime). An APPLY editing it could break the release path and starve cognition
    # of the GPU — it is part of the guard's own call surface.
    assert is_protected_path(_abs(tmp_path, "runtime/model_control.py"), omega_root=tmp_path)


def test_capability_catalog_is_protected(tmp_path):
    # Seam-9 capability SELECTION + STEP-4 reinforcement. An APPLY editing it could rig
    # how its own capabilities are chosen and credited — self-dealing, never real work.
    assert is_protected_path(_abs(tmp_path, "memory/capability_catalog.py"), omega_root=tmp_path)


def test_qwen_skills_is_protected(tmp_path):
    # The capability INVENTORY the executor draws from — an APPLY editing it could
    # inject or alter its own capabilities.
    assert is_protected_path(_abs(tmp_path, "runtime/qwen_skills.py"), omega_root=tmp_path)


# --------------------------------------------------------------------------
# ordinary code is NOT protected — an APPLY must still be able to do real work
# --------------------------------------------------------------------------


def test_ordinary_memory_source_is_not_protected(tmp_path):
    # memory/<lobe> + memory/indexes are mind state; memory/*.py is tracked CODE.
    assert not is_protected_path(_abs(tmp_path, "memory/symphony_strength.py"), omega_root=tmp_path)


def test_pressure_field_source_is_not_protected(tmp_path):
    # pressure_loop/state is mind state; pressure_loop/pressure_field.py is code.
    assert not is_protected_path(
        _abs(tmp_path, "pressure_loop/pressure_field.py"), omega_root=tmp_path
    )


def test_new_feature_file_is_not_protected(tmp_path):
    assert not is_protected_path(_abs(tmp_path, "features/new_thing.py"), omega_root=tmp_path)


def test_test_file_is_not_protected(tmp_path):
    assert not is_protected_path(_abs(tmp_path, "tests/core/test_new.py"), omega_root=tmp_path)


# --------------------------------------------------------------------------
# normalization — relative, backslash, absolute all classify the same
# --------------------------------------------------------------------------


def test_relative_path_is_classified(tmp_path):
    assert is_protected_path("guardian/state/apply_authorization.json", omega_root=tmp_path)


def test_backslash_path_is_classified(tmp_path):
    assert is_protected_path("auto_loop\\execute\\skill_execution_guards.py", omega_root=tmp_path)


# --------------------------------------------------------------------------
# fail-safe — out-of-jail / unparseable paths are violations
# --------------------------------------------------------------------------


def test_path_outside_jail_is_a_violation(tmp_path):
    outside = Path(tmp_path).parent / "elsewhere" / "secret.py"
    assert is_protected_path(str(outside), omega_root=tmp_path)


def test_empty_path_fails_safe(tmp_path):
    assert is_protected_path("", omega_root=tmp_path)
    assert is_protected_path("   ", omega_root=tmp_path)


# --------------------------------------------------------------------------
# self_mod_violations over an APPLY outcome ledger
# --------------------------------------------------------------------------


def test_violations_returns_only_protected_paths(tmp_path):
    outcome = {
        "ledger": {
            "touched_files": [
                "features/new_thing.py",  # ok
                "auto_loop/execute/skill_execution_guards.py",  # protected (code)
                "guardian/state/apply_authorization.json",  # protected (gate)
            ]
        }
    }
    assert set(self_mod_violations(outcome, omega_root=tmp_path)) == {
        "auto_loop/execute/skill_execution_guards.py",
        "guardian/state/apply_authorization.json",
    }


def test_bridge_shaped_touched_file_objects_are_scanned(tmp_path):
    # Executor ledger entries carry structured file objects, not bare strings. The
    # guard must scan entry["path"] or a structured executor report bypasses the denylist.
    outcome = {
        "ledger": {
            "touched_files": [
                {
                    "path": "features/new_thing.py",
                    "change": "modified",
                    "verdict": "pending",
                    "diff_summary": "",
                    "git_status": "M",
                },
                {
                    "path": "auto_loop/execute/skill_execution_guards.py",
                    "change": "modified",
                    "verdict": "pending",
                    "diff_summary": "",
                    "git_status": "M",
                },
            ]
        }
    }
    assert self_mod_violations(outcome, omega_root=tmp_path) == [
        "auto_loop/execute/skill_execution_guards.py"
    ]


def test_malformed_file_list_entry_fails_safe(tmp_path):
    outcome = {"ledger": {"touched_files": [{"change": "modified"}]}}
    assert self_mod_violations(outcome, omega_root=tmp_path) == ["<malformed-ledger-file-entry>"]


def test_clean_ledger_has_no_violations(tmp_path):
    outcome = {"ledger": {"touched_files": ["features/a.py", "docs/readme.md"]}}
    assert self_mod_violations(outcome, omega_root=tmp_path) == []


def test_non_list_ledger_value_is_ignored(tmp_path):
    # The success invoker reports ledger={"kept": True}; a bool must not crash or
    # be treated as a path.
    assert self_mod_violations({"ledger": {"kept": True}}, omega_root=tmp_path) == []


def test_missing_or_garbage_ledger_is_no_violation(tmp_path):
    assert self_mod_violations({}, omega_root=tmp_path) == []
    assert self_mod_violations({"ledger": None}, omega_root=tmp_path) == []
    assert self_mod_violations(None, omega_root=tmp_path) == []


def test_violations_scan_kept_and_rolled_back_lists(tmp_path):
    outcome = {
        "ledger": {
            "kept": ["guardian/mind_checkpoint.py"],  # protected
            "rolled_back": ["features/x.py"],  # ok
        }
    }
    assert self_mod_violations(outcome, omega_root=tmp_path) == ["guardian/mind_checkpoint.py"]
