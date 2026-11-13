"""End-to-end acceptance seams for the signed MCP foundation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import psutil
import pytest

import mcp_platform.ingress as ingress
from mcp_platform import action_ledger
from mcp_platform.app_registry import McpAppRegistry, McpAppRegistrySnapshot
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.external_switch import ExternalMcpSwitch
from mcp_platform.host import McpHost
from mcp_platform.inference import PreparedMcpInference, prepare_mcp_inference
from mcp_platform.operation_contracts import McpExternalDisableOutcome
from mcp_platform.package_contracts import (
    RiskClass,
)
from mcp_platform.package_verifier import VerifiedPackage
from mcp_platform.registry import InstalledPackageRecord
from mcp_platform.source_contracts import McpSourceContext
from mcp_platform.supervisor import McpProcessSupervisor
from tests.mcp_platform.test_authority import Harness, _replace_proposal
from tests.mcp_platform.test_current_client_acceptance import (
    _add_python_runtime,
    _signed_python_package,
)
from tests.mcp_platform.test_inference_prepare import _input
from tests.mcp_platform.test_ingress import valid_structured_result
from tests.mcp_platform.test_supervisor import FakeClock, FakeSessionFactory

pytest_plugins = ("tests.mcp_platform.test_supervisor",)


def test_phase_handoff_exposes_only_the_immutable_app_snapshot(tmp_path: Path) -> None:
    root_import = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import mcp_platform;"
                "from mcp_platform.app_registry import McpAppRegistrySnapshot;"
                "assert mcp_platform.McpAppRegistrySnapshot is McpAppRegistrySnapshot"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    snapshot = McpAppRegistry(state_root=tmp_path / "apps").snapshot()

    assert root_import.returncode == 0, root_import.stderr
    assert type(snapshot) is McpAppRegistrySnapshot
    assert snapshot.records == ()
    assert not any(
        hasattr(snapshot, mutator) for mutator in ("install", "enable", "disable", "uninstall")
    )


class _DrainAuthority:
    def __init__(
        self,
        switch: ExternalMcpSwitch,
        supervisor: McpProcessSupervisor,
    ) -> None:
        self.switch = switch
        self.supervisor = supervisor

    async def disable_external(
        self,
        expected_revision: str,
    ) -> McpExternalDisableOutcome:
        self.switch.set_enabled(False, expected_revision=expected_revision)
        stopped = self.supervisor.stop_external_sync()
        return McpExternalDisableOutcome(
            drained_calls=0,
            uncertain_writes=0,
            failed_reads=0,
            sessions_closed=stopped["sessions_closed"],
            sessions_forced=stopped["sessions_forced"],
            errors=stopped["errors"],
        )

    @staticmethod
    def wait_for_cleanup_sync(_timeout_s: float) -> bool:
        return True


def test_switch_off_on_and_off_preserves_only_local_sessions(
    installed_packages,
) -> None:
    registry, switch, _calls = installed_packages
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    authority = _DrainAuthority(switch, supervisor)
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=authority,
    )
    started = host.start()
    try:
        assert [session.server_id for session in started] == ["omega.first_party.local"]

        off = switch.load()
        enabled = anyio.run(host.set_external_enabled, True, off.revision)
        assert enabled["reconciliation"] == {
            "started": 1,
            "blocked": 0,
            "errors": 0,
        }
        assert {launch.verified_package.manifest.server_id for launch in factory.started} == {
            "omega.first_party.local",
            "omega.first_party.external",
        }

        on = switch.load()
        disabled = anyio.run(host.set_external_enabled, False, on.revision)
        packages = {package["server_id"]: package for package in disabled["status"]["packages"]}
        assert packages["omega.first_party.external"]["session_state"] == "stopped"
        assert packages["omega.first_party.local"]["session_state"] == "ready"
    finally:
        host.shutdown()


def test_direct_and_standing_authority_execute_and_direct_authority_is_one_shot(
    tmp_path: Path,
) -> None:
    direct_root = tmp_path / "direct"
    direct_root.mkdir()
    direct = Harness(direct_root)
    first = anyio.run(direct.gateway.execute, direct.proposal)
    replay = anyio.run(direct.gateway.execute, direct.proposal)

    assert first.status == "confirmed"
    assert first.admission is not None
    assert first.admission.admitted_count == 1
    assert first.admission.deduplicated is False
    assert replay.status == "blocked"
    assert replay.failed_check == "authority_validity"
    assert replay.deduplicated is False
    assert direct.supervisor.call_count == 1
    assert direct.ingress_calls == 1
    record = action_ledger.get_action(first.action_id, path=direct.ledger_path)
    assert record is not None and record["state"] == "confirmed"

    standing_root = tmp_path / "standing"
    standing_root.mkdir()
    standing = Harness(standing_root)
    standing.authority = dataclasses.replace(
        standing.authority,
        authority_kind="standing_grant",
        authority_id="grant-1",
        authority_revision="f" * 64,
        job_id="job-1",
        job_revision="0" * 64,
        direct_execution=False,
        standing_execution=True,
    )
    proposal = _replace_proposal(
        standing.proposal,
        authority_kind="standing_grant",
        authority_id="grant-1",
        authority_revision="f" * 64,
    )
    outcome = anyio.run(standing.gateway.execute, proposal)
    assert outcome.status == "confirmed"
    assert standing.supervisor.call_count == 1


@pytest.mark.parametrize(
    ("risk", "flag"),
    [
        (RiskClass.DESTRUCTIVE, "allow_destructive"),
        (RiskClass.SEND_PUBLISH, "allow_send_publish"),
        (RiskClass.CROSS_PROVIDER_TRANSFER, "allow_cross_provider"),
    ],
)
def test_high_risk_operations_without_exact_authority_never_dispatch(
    tmp_path: Path,
    risk: RiskClass,
    flag: str,
) -> None:
    harness = Harness(tmp_path, risk)
    harness.authority = dataclasses.replace(harness.authority, **{flag: False})
    proposal = harness.proposal
    if risk is RiskClass.CROSS_PROVIDER_TRANSFER:
        source = {"provider": "provider.fake", "folders": ["inbox"]}
        destination = {"provider": "provider.other", "folder": "archive"}
        harness.connection = dataclasses.replace(
            harness.connection,
            source_constraints=source,
            destination_constraints=destination,
        )
        harness.authority = dataclasses.replace(
            harness.authority,
            source_constraints=source,
            destination_constraints=destination,
        )
        proposal = _replace_proposal(
            harness.proposal,
            source_constraints=source,
            destination_constraints=destination,
        )

    outcome = anyio.run(harness.gateway.execute, proposal)

    assert outcome.status == "blocked"
    assert outcome.failed_check == "risk_specific_authority"
    assert harness.supervisor.call_count == 0


def test_stale_provider_revision_never_reaches_dispatch(tmp_path: Path) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    proposal = _replace_proposal(
        harness.proposal,
        expected_provider_revisions={"object-1": "revision-0"},
        arguments={
            "query": "inbox",
            "object_id": "object-1",
            "expected_provider_revisions": {"object-1": "stale-revision"},
        },
    )
    harness.authority = dataclasses.replace(
        harness.authority,
        arguments_sha256=sha256_hex(canonical_bytes(proposal.arguments_dict())),
    )

    outcome = anyio.run(harness.gateway.execute, proposal)

    assert outcome.status == "blocked"
    assert outcome.failed_check == "provider_revision_precondition"
    assert harness.supervisor.call_count == 0


def test_source_ledger_guardian_queue_order_is_the_only_admission_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    context = McpSourceContext(
        server_id="omega.first_party.fake",
        server_version="1.0.0",
        artifact_sha256="a" * 64,
        provider_id="provider.fake",
        account_ref="account-ref",
        connection_revision="b" * 64,
        tool_name="omega.fake.read",
        tool_schema_sha256="c" * 64,
        call_timestamp="2026-08-11T15:00:01Z",
        omega_action_lineage_id="act_0123456789abcdef",
        max_result_bytes=8192,
        allowed_mime_types=("application/json",),
    )
    document = valid_structured_result()
    encoded = canonical_bytes(document)

    def record(data: bytes, **_kwargs):
        calls.append("source_ledger")
        return SimpleNamespace(
            source_id="src_" + "1" * 24,
            content_hash=hashlib.sha256(data).hexdigest(),
        )

    real_screen = ingress.screen_receipt

    def screen(content, **kwargs):
        calls.append("guardian")
        return real_screen(content, **kwargs)

    def enqueue(_contributor, *, operator_activity):
        assert operator_activity is False
        calls.append("queue")
        return True

    monkeypatch.setattr(ingress, "record_source_bytes", record)
    monkeypatch.setattr(ingress, "screen_receipt", screen)
    monkeypatch.setattr(ingress, "enqueue_input", enqueue)

    receipt = ingress.admit_mcp_result(context, document, source_root=tmp_path)

    assert calls == ["source_ledger", "guardian", "queue"]
    assert receipt.enqueued is True
    assert receipt.result_digest == sha256_hex(encoded)


def test_prepared_inference_is_stable_and_stops_before_submission() -> None:
    source = _input()

    first = prepare_mcp_inference(source)
    second = prepare_mcp_inference(source)

    assert type(first) is PreparedMcpInference
    assert first == second
    assert first.request_digest == second.request_digest
    assert first.lane_hint == "mcp"


class _SinglePackageRegistry:
    def __init__(self, package: VerifiedPackage) -> None:
        self.package = package
        self.record = dataclasses.replace(
            InstalledPackageRecord.from_verified(package),
            enabled=True,
        )

    def get(self, server_id: str) -> InstalledPackageRecord:
        assert server_id == self.record.server_id
        return self.record

    def list_records(self) -> tuple[InstalledPackageRecord, ...]:
        return (self.record,)

    def snapshot(self):
        return SimpleNamespace(revision="d" * 64, records=(self.record,))

    def package_path(self, _record: InstalledPackageRecord) -> Path:
        return self.package.tool_catalog_path.parent

    def reverify_installed(self, server_id: str) -> VerifiedPackage:
        assert server_id == self.record.server_id
        return self.package

    def block(self, _server_id: str, reason: str) -> InstalledPackageRecord:
        self.record = dataclasses.replace(self.record, blocker=reason)
        return self.record


def _real_stdio_package(
    tmp_path: Path,
    peer_cache_root: Path,
) -> tuple[VerifiedPackage, Path]:
    package_root, package, _ring, _secret = _signed_python_package(
        tmp_path / "real-stdio-package",
        peer_cache_root,
    )
    return package, _add_python_runtime(package_root)


@pytest.mark.timeout(30)
def test_host_shutdown_reaps_the_real_fake_stdio_process(
    tmp_path: Path,
    mcp_compiled_peer_cache: Path,
) -> None:
    package, runtime_pointer = _real_stdio_package(tmp_path, mcp_compiled_peer_cache)
    registry = _SinglePackageRegistry(package)
    switch = ExternalMcpSwitch(tmp_path / "external-switch.json")
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=lambda _path: package,
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_DrainAuthority(switch, supervisor),
    )
    parent = psutil.Process()
    before = {child.pid for child in parent.children(recursive=True)}
    owned: list[psutil.Process] = []
    try:
        started = host.start()
        assert [snapshot.server_id for snapshot in started] == [package.manifest.server_id]
        owned = [child for child in parent.children(recursive=True) if child.pid not in before]
        assert owned

        result = host.shutdown()
        gone, alive = psutil.wait_procs(owned, timeout=5.0)

        assert result["errors"] == 0
        assert gone
        assert alive == []
    finally:
        if supervisor.thread_alive():
            host.shutdown()
        for child in owned:
            if child.is_running():
                child.kill()
                child.wait(timeout=5.0)
        runtime_pointer.unlink(missing_ok=True)
        (runtime_pointer.parents[2] / "pyvenv.cfg").unlink(missing_ok=True)
