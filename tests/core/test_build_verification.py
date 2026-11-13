"""Deterministic build verification — the battery behind the fold-back's main meta question."""

from core.build_verification import verify_build_against_request


def _mk(tmp_path, name, content="x = 1\n"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


GREEN_TESTS = {
    "passed": 4,
    "failed": 0,
    "errors": 0,
    "skipped": 0,
    "failures": [],
    "summary": "4 passed, 0 failed, 0 error(s)",
    "all_passed": True,
    "ran": True,
}
RED_TESTS = {
    "passed": 2,
    "failed": 1,
    "errors": 0,
    "skipped": 0,
    "failures": ["FAILED test_x.py::test_header"],
    "summary": "2 passed, 1 failed, 0 error(s)",
    "all_passed": False,
    "ran": True,
}


def test_all_green_when_everything_satisfies_the_request(tmp_path):
    files = [
        _mk(tmp_path, "csv_stats.py", "if __name__ == '__main__':\n    pass\n"),
        _mk(tmp_path, "tests/test_csv_stats.py", "def test_a():\n    assert True\n"),
        _mk(tmp_path, "README.md", "# csv_stats\nusage...\n"),
    ]
    out = verify_build_against_request(
        original_request="Build a CLI tool with a pytest test file with at least 4 tests and a README.",
        files=files,
        test_report=GREEN_TESTS,
    )
    assert out["all_green"] is True
    names = {c["name"] for c in out["checks"]}
    assert {"deliverables_exist", "code_sound", "tests_green", "request_coverage"} <= names


def test_missing_deliverable_is_red(tmp_path):
    files = [_mk(tmp_path, "a.py"), str(tmp_path / "never_written.py")]
    out = verify_build_against_request(original_request="", files=files, test_report=GREEN_TESTS)
    assert out["all_green"] is False
    by = {c["name"]: c for c in out["checks"]}
    assert by["deliverables_exist"]["ok"] is False
    assert "never_written.py" in by["deliverables_exist"]["detail"]


def test_syntax_error_is_red(tmp_path):
    files = [_mk(tmp_path, "bad.py", "def broken(:\n")]
    out = verify_build_against_request(original_request="", files=files, test_report=GREEN_TESTS)
    by = {c["name"]: c for c in out["checks"]}
    assert by["code_sound"]["ok"] is False and "bad.py" in by["code_sound"]["detail"]


def test_failing_tests_are_red(tmp_path):
    files = [_mk(tmp_path, "a.py")]
    out = verify_build_against_request(original_request="", files=files, test_report=RED_TESTS)
    by = {c["name"]: c for c in out["checks"]}
    assert by["tests_green"]["ok"] is False
    assert out["all_green"] is False


def test_request_coverage_readme_and_min_tests(tmp_path):
    # request names a README + at least 4 tests; only 3 ran and no README exists -> red w/ detail
    files = [_mk(tmp_path, "tool.py"), _mk(tmp_path, "test_tool.py", "def test_a(): pass\n")]
    report = dict(GREEN_TESTS, passed=3)
    out = verify_build_against_request(
        original_request="build a tool with a README and at least 4 tests",
        files=files,
        test_report=report,
    )
    by = {c["name"]: c for c in out["checks"]}
    assert by["request_coverage"]["ok"] is False
    d = by["request_coverage"]["detail"]
    assert "README" in d and "at least 4 tests" in d


def test_unrecognizable_request_omits_coverage_check(tmp_path):
    files = [_mk(tmp_path, "a.py")]
    out = verify_build_against_request(
        original_request="do the thing we discussed",
        files=files,
        test_report=GREEN_TESTS,
    )
    assert "request_coverage" not in {c["name"] for c in out["checks"]}


def test_no_files_at_all_is_red():
    out = verify_build_against_request(original_request="build x", files=[], test_report=None)
    assert out["all_green"] is False


# ── presence checks look at the PROJECT, not just this dispatch's diff ────────────────────
# Live finding 2026-07-02 (episode 384e9e9c): the build was complete on disk (csv_stats.py,
# README.md, test_csv_stats.py all present) but the fold's touched-file list carried ONE file
# (the bridge only tracks write_file calls; the executor wrote via shell), so request_coverage
# said "README missing" and completion was structurally impossible. tests_green already runs
# pytest across the whole project dir — presence checks must see the same project.


def test_request_coverage_sees_project_readme_beyond_touched_files(tmp_path):
    _mk(tmp_path, "csv_stats.py", "if __name__ == '__main__':\n    pass\n")
    _mk(tmp_path, "README.md", "# csv_stats\nusage...\n")
    _mk(tmp_path, "test_csv_stats.py", "def test_a():\n    assert True\n")
    touched = [_mk(tmp_path, "test_example.py", "def test_b():\n    assert True\n")]
    out = verify_build_against_request(
        original_request="Rebuild the csv_stats project: csv_stats.py, a pytest test file "
        "with at least 4 tests, and a README with usage examples.",
        files=touched,
        test_report=GREEN_TESTS,
    )
    by = {c["name"]: c for c in out["checks"]}
    assert by["request_coverage"]["ok"] is True, by["request_coverage"]["detail"]
    assert out["all_green"] is True


def test_request_coverage_sees_project_test_files_beyond_touched_files(tmp_path):
    _mk(tmp_path, "test_tool.py", "def test_a():\n    assert True\n")
    touched = [_mk(tmp_path, "tool.py")]
    out = verify_build_against_request(
        original_request="build a tool with pytest tests",
        files=touched,
        test_report=GREEN_TESTS,
    )
    by = {c["name"]: c for c in out["checks"]}
    assert by["request_coverage"]["ok"] is True, by["request_coverage"]["detail"]


def test_request_coverage_still_red_when_project_truly_lacks_the_readme(tmp_path):
    # dir-awareness must not manufacture green: nothing in the project dir is a README.
    touched = [_mk(tmp_path, "tool.py"), _mk(tmp_path, "test_tool.py", "def test_a(): pass\n")]
    out = verify_build_against_request(
        original_request="build a tool with a README",
        files=touched,
        test_report=GREEN_TESTS,
    )
    by = {c["name"]: c for c in out["checks"]}
    assert by["request_coverage"]["ok"] is False
    assert "README" in by["request_coverage"]["detail"]


def test_request_coverage_cli_entry_found_in_project_dir(tmp_path):
    _mk(tmp_path, "pyproject.toml", "[project]\nname='tool'\n")
    touched = [_mk(tmp_path, "helper.py")]
    out = verify_build_against_request(
        original_request="build a cli tool",
        files=touched,
        test_report=GREEN_TESTS,
    )
    by = {c["name"]: c for c in out["checks"]}
    assert by["request_coverage"]["ok"] is True, by["request_coverage"]["detail"]
