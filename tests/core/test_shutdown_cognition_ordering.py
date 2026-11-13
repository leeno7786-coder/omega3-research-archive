"""RISK 4(b): stop the cognition threads BEFORE sweeping the workers they use.

Measured on this appliance 2026-08-11. SIGTERM at 14:14:22; the gateway was
gone by 14:14:30; the agent lane's cascade was still folding back at 14:15:42 —
72 seconds of model passes, broker checks, deepsleep phases and embed backfill
issuing loopback requests at processes that no longer existed. systemd held the
unit open for all of it and the journal filled with URLError.

Teardown's reverse-of-boot order could not fix this by itself: the lane
executors are not a boot component (they are created on first dispatch), so the
reverse walk never had anything to reverse. These pin the three claims the fix
rests on — the order, the bound, and the retry envelope that made the tail so
long — plus the guarantee that has to survive all of it: a hung cascade can
never wedge the shutdown.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import boot_lifecycle as bl  # noqa: E402
from core import cognition_halt  # noqa: E402
from pressure_loop import pipeline_dispatch  # noqa: E402
from pressure_loop.runtime import PressureRuntime  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pipeline_dispatch.shutdown_for_tests()
    bl.reset_lifecycle_state()
    monkeypatch.setattr(bl, "BOOT_MANIFEST_PATH", tmp_path / "boot.manifest.json")
    monkeypatch.setattr(bl, "SUBSTRATE_SNAPSHOT_PATH", tmp_path / "substrate_snapshot.json")
    monkeypatch.setattr(
        "guardian.state_persistence.SAVE_PATH", tmp_path / "save_state.json"
    )
    yield
    pipeline_dispatch.shutdown_for_tests()
    cognition_halt.reset_for_tests()


def test_cognition_threads_stop_before_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE ordering claim. Nothing that kills a worker may run first."""
    order: list[str] = []

    monkeypatch.setattr(
        pipeline_dispatch,
        "shutdown_pipelines",
        lambda timeout_s=0.0: order.append("lanes_stopped") or {},
    )
    monkeypatch.setattr(
        "runtime.supervisor.shutdown_runtime_process",
        lambda: order.append("gateway_stopped") or {},
    )
    bl.record_started("boot_lock")
    bl.record_started("runtime_process")
    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert order.index("lanes_stopped") < order.index("gateway_stopped")


def test_teardown_halt_is_set_before_the_gateway_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The halt flag has to be up while the gateway shutdown runs.

    A cascade still winding down during gateway shutdown is exactly the caller that
    must not enter a 33.5s embed retry envelope.
    """
    halted_during_shutdown: list[bool] = []

    monkeypatch.setattr(
        "runtime.supervisor.shutdown_runtime_process",
        lambda: halted_during_shutdown.append(cognition_halt.halt_requested()) or {},
    )
    bl.record_started("boot_lock")
    bl.record_started("runtime_process")
    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert halted_during_shutdown == [True]
    assert cognition_halt.halt_reason() == "runtime_teardown"


def test_shutdown_waits_for_a_cascade_that_is_still_landing() -> None:
    """A lane finishing durable work is given real time, not cut off."""
    landed = threading.Event()
    release = threading.Event()

    def _cascade() -> None:
        release.wait(5.0)
        landed.set()

    pool = pipeline_dispatch._executor_for(0)
    pool.submit(lambda: pipeline_dispatch._THREADS.__setitem__(
        0, threading.current_thread()
    ) or _cascade())
    time.sleep(0.05)  # let the worker thread claim the job and register itself

    release.set()
    report = pipeline_dispatch.shutdown_pipelines(timeout_s=5.0)

    assert landed.is_set(), "teardown must let an in-flight cascade finish"
    assert report["still_running"] == []


def test_a_hung_cascade_cannot_wedge_the_shutdown() -> None:
    """The bound. Past the deadline teardown proceeds and says who is stuck.

    This is the same guarantee the runtime unload path has always carried: a
    failed or hung dependant never leaves the brain waiting forever.
    """
    stuck = threading.Event()
    try:
        pool = pipeline_dispatch._executor_for(1)
        pool.submit(lambda: pipeline_dispatch._THREADS.__setitem__(
            1, threading.current_thread()
        ) or stuck.wait(30.0))
        time.sleep(0.05)

        started = time.monotonic()
        report = pipeline_dispatch.shutdown_pipelines(timeout_s=0.3)
        elapsed = time.monotonic() - started

        assert report["still_running"] == [1]
        assert elapsed < 5.0, "teardown must not wait on a cascade that will not land"
    finally:
        stuck.set()


def test_halt_clears_on_the_next_boot() -> None:
    """One-way within a boot, not across one — run_continuous resets first."""
    cognition_halt.request_halt("runtime_teardown")
    assert cognition_halt.halt_requested() is True

    bl.reset_lifecycle_state()

    assert cognition_halt.halt_requested() is False
    assert cognition_halt.halt_reason() == ""
