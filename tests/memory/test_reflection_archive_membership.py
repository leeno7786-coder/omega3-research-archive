"""Reflection-window envelopes are archives, not canonical memory records."""
import json

import pytest

from core.required_substrate import RequiredSubstrateError
from memory.engine import reindex
from memory.projection.build import MembershipError, scan_canonical_membership
from memory.projection.generation import current_view, projection_read_transaction


def _archive(root, directory="600_journal/reflection_windows"):
    path = root / "memory" / directory / "window_example.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"window": {}, "omega_outcome": {}}), encoding="utf-8")
    return path


def test_only_reflection_archives_do_not_block_either_canonical_scanner(tmp_path):
    _archive(tmp_path)
    assert list(reindex.find_json_files(root=tmp_path)) == []
    reindex.build_index(root=tmp_path)
    with projection_read_transaction(tmp_path):
        view = current_view()
        assert view is not None
        assert json.loads(view.read_bytes("coord_index.snapshot.json")) == {}
    assert scan_canonical_membership(tmp_path / "memory") == []


@pytest.mark.parametrize("directory", [
    "600_journal/deepsleep",
    "600_journal/reflection_windows_other",
    "600_journal/nested/reflection_windows",
    "400_situational/reflection_windows",
])
def test_invalid_records_outside_exact_archive_subtree_still_fail(tmp_path, directory):
    _archive(tmp_path)
    invalid = _archive(tmp_path, directory)
    assert list(reindex.find_json_files(root=tmp_path)) == [invalid]
    with pytest.raises(RequiredSubstrateError):
        reindex.build_index(root=tmp_path)
    with pytest.raises(MembershipError) as raised:
        scan_canonical_membership(tmp_path / "memory")
    assert raised.value.kind == "ambiguous_no_id"
    assert raised.value.path == invalid.relative_to(tmp_path).as_posix()
