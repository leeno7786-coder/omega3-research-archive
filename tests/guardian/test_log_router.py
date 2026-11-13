"""Per-component log router — every logger gets its own file in guardian/logs."""

from __future__ import annotations

import logging

import pytest

from guardian.log_router import (
    ComponentLogRouter,
    _sanitize_component,
    install_component_logging,
    uninstall_for_tests,
)


@pytest.fixture()
def router(tmp_path):
    r = ComponentLogRouter(tmp_path)
    yield r
    r.close()


def _record(name: str, msg: str, level=logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_each_logger_gets_own_file(router, tmp_path):
    router.emit(_record("omega.runtime.brain_cloud_config", "cloud leg ok"))
    router.emit(_record("omega.guardian.decision", "gate passed"))
    router.emit(_record("auto_loop.execute.run", "cycle complete"))
    assert (tmp_path / "omega.runtime.brain_cloud_config.log").read_text(encoding="utf-8").count(
        "cloud leg ok"
    ) == 1
    assert (tmp_path / "omega.guardian.decision.log").exists()
    assert (tmp_path / "auto_loop.execute.run.log").exists()
    # no cross-contamination — each part is individually pinpointable
    assert "gate passed" not in (tmp_path / "omega.runtime.brain_cloud_config.log").read_text(
        encoding="utf-8"
    )


def test_repeated_emits_append(router, tmp_path):
    for i in range(3):
        router.emit(_record("omega.memory.api", f"write {i}"))
    content = (tmp_path / "omega.memory.api.log").read_text(encoding="utf-8")
    assert [f"write {i}" in content for i in range(3)] == [True, True, True]


def test_sanitize_component_names():
    assert _sanitize_component("omega.core.pressure") == "omega.core.pressure"
    assert _sanitize_component("weird:name/with\\chars") == "weird_name_with_chars"
    assert _sanitize_component("") == "root"
    assert _sanitize_component("CON.device") == "_CON.device"  # windows reserved stem


def test_emit_never_raises_on_bad_dir(tmp_path):
    router = ComponentLogRouter(tmp_path / "missing" / "\0bad")
    router.emit(_record("omega.x", "must not raise"))
    router.close()


def test_install_is_idempotent_and_uninstalls(tmp_path):
    # Other suites may raise the root level or logging.disable — neutralize so
    # this test is order-independent in the full run. They may ALSO have left a
    # ROUTER installed (boot.py/hud.standalone call install_component_logging
    # against the real logs dir and never uninstall): the singleton would make
    # install() return THAT router, silently routing this test's emit to the
    # wrong directory. Clear it first — same order-independence contract.
    uninstall_for_tests()
    logging.disable(logging.NOTSET)
    root = logging.getLogger()
    prior_level = root.level
    root.setLevel(logging.INFO)
    try:
        first = install_component_logging(tmp_path)
        second = install_component_logging(tmp_path)
        assert first is second
        assert sum(1 for h in root.handlers if isinstance(h, ComponentLogRouter)) == 1
        logging.getLogger("omega.test.router_install").error("hello routed world")
        assert (tmp_path / "omega.test.router_install.log").exists()
    finally:
        uninstall_for_tests()
        root.setLevel(prior_level)
    assert not any(
        isinstance(h, logging.Handler) and isinstance(h, ComponentLogRouter)
        for h in logging.getLogger().handlers
    )


def test_disable_env_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_COMPONENT_LOGS", "0")
    try:
        assert install_component_logging(tmp_path) is None
    finally:
        uninstall_for_tests()


def test_telemetry_writer_targets_logs_dir():
    """The single-directory directive: jsonl telemetry lands in the Guardian logs root.

    conftest monkeypatches the live constant to tmp for isolation, so pin the
    SOURCE default rather than the runtime value. The default resolves through
    core.paths.guardian_logs_root(), so OMEGA_GUARDIAN_LOGS_DIR redirects it (a live
    boot leaves it unset: guardian/logs).
    """
    import inspect

    from guardian.telemetry import writer

    src = inspect.getsource(writer)
    assert "TELEMETRY_DIR = guardian_logs_root()" in src
    assert 'FAILURE_LOG = guardian_logs_root() / "failures.jsonl"' in src
    assert '"guardian" / "telemetry"' not in src.replace(
        "guardian/telemetry/ holds package code only", ""
    )


def test_guardian_logs_root_defaults_to_guardian_logs(monkeypatch):
    from core.paths import OMEGA_ROOT, guardian_logs_root

    monkeypatch.delenv("OMEGA_GUARDIAN_LOGS_DIR", raising=False)
    assert guardian_logs_root() == OMEGA_ROOT / "guardian" / "logs"


def test_terminal_and_infra_default_into_logs_dir(monkeypatch, tmp_path):
    from guardian import terminal_console as tc

    monkeypatch.delenv("OMEGA_TERMINAL_TELEMETRY_FILE", raising=False)
    monkeypatch.setenv("OMEGA_GUARDIAN_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(tc, "TERMINAL_LOG_PATH", None)
    monkeypatch.setattr(tc, "INFRA_LOG_PATH", None)
    assert tc._resolve_terminal_log_path() == tmp_path / "logs" / "terminal.log"
    assert tc._resolve_infra_log_path() == tmp_path / "logs" / "infra.log"
