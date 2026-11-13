"""Security boundary tests for signed declarative plugin package trees."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
import plugin_platform.filesystem as filesystem_module
import plugin_platform.package_verifier as package_verifier_module
from plugin_platform.package_verifier import (
    MAX_ICON_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_SKILL_BYTES,
    MAX_WORKFLOW_BYTES,
    PackageVerificationError,
    verify_plugin_package,
)
from plugin_platform.contracts import PluginManifest
from plugin_platform.filesystem import exact_regular_files, fsync_directory, fsync_file
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _manifest(package: Path) -> dict[str, Any]:
    value = json.loads((package / "plugin.json").read_bytes().decode("utf-8"))
    assert isinstance(value, dict)
    return value


def _write_signed_manifest(
    package: Path,
    private_key: Ed25519PrivateKey,
    document: dict[str, Any],
) -> None:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    (package / "plugin.json").write_bytes(canonical_bytes(document))


def _add_asset(
    package: Path,
    private_key: Ed25519PrivateKey,
    *,
    relative_path: str,
    kind: str,
    media_type: str,
    payload: bytes,
) -> Path:
    path = package.joinpath(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    document = _manifest(package)
    assets = document["assets"]
    assert isinstance(assets, list)
    assets.append(
        {
            "path": relative_path,
            "kind": kind,
            "media_type": media_type,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    _write_signed_manifest(package, private_key, document)
    return path


def _assert_rejected(
    package: Path,
    ring: TrustedPublisherKeyRing,
    reason: str,
    *,
    omega_version: str = "3.0.0",
) -> None:
    with pytest.raises(PackageVerificationError, match=rf"^{reason}$"):
        verify_plugin_package(package, ring, omega_version)


def _replace_directory_with_link(path: Path, target: Path) -> bool:
    original = path.with_name(f"{path.name}-before-swap")
    try:
        path.rename(original)
    except OSError:
        return False
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            original.rename(path)
            pytest.fail("junction creation unavailable during native race test")
    else:
        os.symlink(target, path, target_is_directory=True)
    return True


def _verify_in_thread(
    package: Path,
    ring: TrustedPublisherKeyRing,
) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["verified"] = verify_plugin_package(package, ring, "3.0.0")
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def _operation_in_thread(
    operation: Callable[[], None],
) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            operation()
            outcome["completed"] = True
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_valid_signed_package_returns_only_immutable_verified_state(tmp_path: Path) -> None:
    """Catches accepting bytes without returning their complete immutable identity."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    manifest_bytes = (package / "plugin.json").read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    expected_package_bytes = canonical_bytes(
        {
            "manifest_sha256": manifest_sha256,
            "assets": {
                "skills/workspace/SKILL.md": (
                    "a64e2f24df086339d710edcc464ae0722a83a6c21e296cf0d7758fd6425fc312"
                )
            },
        }
    )

    verified = verify_plugin_package(package, ring, "3.0.0")

    assert verified.manifest.plugin_id == "test.workspace"
    assert verified.package_root == package
    assert verified.manifest_sha256 == manifest_sha256
    assert verified.package_sha256 == hashlib.sha256(expected_package_bytes).hexdigest()
    assert verified.asset_paths == {"skills/workspace/SKILL.md": Path("skills/workspace/SKILL.md")}
    assert verified.asset_bytes == {
        "skills/workspace/SKILL.md": (b"# Workspace\n\nUse the connected workspace deliberately.\n")
    }
    assert isinstance(verified.asset_paths, MappingProxyType)
    assert isinstance(verified.asset_bytes, MappingProxyType)
    with pytest.raises(TypeError):
        verified.asset_paths["other"] = package  # type: ignore[index]
    with pytest.raises(TypeError):
        verified.asset_bytes["other"] = b"changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        verified.asset_bytes["skills/workspace/SKILL.md"][0] = 0  # type: ignore[index]
    assert len(manifest_bytes) + sum(map(len, verified.asset_bytes.values())) <= MAX_PACKAGE_BYTES


def test_only_successful_package_verification_issues_app_requirement_provenance(
    tmp_path: Path,
) -> None:
    """Catches nominal AppRequirement construction masquerading as signed authority."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    document = _manifest(package)
    requirement = {
        "app_id": "example.remote.workspace",
        "publisher_id": "example.publisher",
        "deployment": "remote",
        "provider_id": "workspace",
        "protocol_version": "2026-07-28",
        "transports": ["streamable_http"],
        "remote_origin": "https://workspace.example",
        "authorization_issuer": "https://auth.example/tenant",
        "authentication": {
            "kind": "oauth_authorization_code_pkce",
            "scopes": ["workspace.read"],
            "secret_fields": [],
        },
        "capabilities": [
            {
                "kind": "tool",
                "name": "list_items",
                "schema_sha256": "2" * 64,
                "version": None,
            }
        ],
    }
    document["kind"] = "mcp_backed"
    document["required_apps"] = [requirement]
    document["entry_points"][0]["required_app_ids"] = [requirement["app_id"]]
    _write_signed_manifest(package, private_key, document)
    nominal = PluginManifest.from_dict(document).required_apps[0]

    with pytest.raises(ValueError, match="^app_requirement_not_verified$"):
        package_verifier_module.validate_verified_app_requirement(nominal)

    verified = verify_plugin_package(package, ring, "3.0.0")
    issued = verified.manifest.required_apps[0]

    assert package_verifier_module.validate_verified_app_requirement(issued) is issued


def test_changed_manifest_rejects_against_its_original_signature(tmp_path: Path) -> None:
    """Catches trusting a canonical manifest changed after its publisher signed it."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    document = _manifest(package)
    document["title"] = "Changed after signing"
    (package / "plugin.json").write_bytes(canonical_bytes(document))

    _assert_rejected(package, ring, "invalid_signature")


def test_changed_asset_rejects_against_its_signed_digest(tmp_path: Path) -> None:
    """Catches admitting asset bytes changed after their digest was signed."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    (package / "skills" / "workspace" / "SKILL.md").write_bytes(b"# Changed\n")

    _assert_rejected(package, ring, "asset_digest_mismatch")


@pytest.mark.parametrize("change", ["added_file", "removed_asset", "empty_directory"])
def test_package_tree_must_equal_manifest_declarations(tmp_path: Path, change: str) -> None:
    """Catches undeclared files/directories or a declared file missing from the exact tree."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    if change == "added_file":
        (package / "undeclared.txt").write_text("undeclared", encoding="utf-8")
    elif change == "removed_asset":
        (package / "skills" / "workspace" / "SKILL.md").unlink()
    else:
        (package / "unused").mkdir()

    _assert_rejected(package, ring, "package_file_set_mismatch")


@pytest.mark.parametrize("level", ["root", "directory", "asset"])
def test_symlink_or_reparse_rejects_at_every_package_level(tmp_path: Path, level: str) -> None:
    """Catches following a reparse at the package root, an ancestor, or an asset leaf."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    candidate = package
    if level == "root":
        candidate = tmp_path / "package-alias"
        target = package
        target_is_directory = True
    elif level == "directory":
        target = tmp_path / "outside-workspace"
        target.mkdir()
        (target / "SKILL.md").write_bytes(
            (package / "skills" / "workspace" / "SKILL.md").read_bytes()
        )
        linked = package / "skills" / "workspace"
        linked.rename(package / "original-workspace")
        candidate = package
        target_is_directory = True
        candidate_link = linked
    else:
        target = tmp_path / "outside-skill.md"
        target.write_bytes((package / "skills" / "workspace" / "SKILL.md").read_bytes())
        linked = package / "skills" / "workspace" / "SKILL.md"
        linked.unlink()
        candidate = package
        target_is_directory = False
        candidate_link = linked
    link = candidate if level == "root" else candidate_link
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    _assert_rejected(candidate, ring, "package_tree_invalid")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_windows_junction_is_rejected_without_following_it(tmp_path: Path) -> None:
    """Catches treating a directory junction as an ordinary package directory."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "SKILL.md").write_bytes((package / "skills" / "workspace" / "SKILL.md").read_bytes())
    linked = package / "skills" / "workspace"
    linked.rename(package / "original-workspace")
    created = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(linked), str(target)],
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("junction creation unavailable")

    _assert_rejected(package, ring, "package_tree_invalid")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs unavailable on this platform")
def test_fifo_in_package_tree_is_rejected(tmp_path: Path) -> None:
    """Catches accepting a blocking non-regular filesystem object as package content."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    os.mkfifo(package / "package.pipe")

    _assert_rejected(package, ring, "package_tree_invalid")


def test_device_path_is_not_an_ordinary_tree_root() -> None:
    """Catches treating a device node as an ordinary filesystem tree."""
    with pytest.raises(ValueError, match="^package_tree_invalid$"):
        exact_regular_files(Path(os.devnull))


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_fsync_helpers_reject_reparse_targets(tmp_path: Path, kind: str) -> None:
    """Catches stable-storage helpers following a swapped file or directory reparse."""
    if kind == "file":
        target = tmp_path / "target.txt"
        target.write_text("target", encoding="utf-8")
        link = tmp_path / "file-link"
        operation = fsync_file
        target_is_directory = False
    else:
        target = tmp_path / "target-directory"
        target.mkdir()
        link = tmp_path / "directory-link"
        operation = fsync_directory
        target_is_directory = True
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="^package_tree_invalid$"):
        operation(link)


@pytest.mark.parametrize(
    "asset_path",
    [
        "../outside/SKILL.md",
        "skills/../../outside/SKILL.md",
        "/skills/workspace/SKILL.md",
        "C:/skills/workspace/SKILL.md",
    ],
)
def test_traversing_or_rooted_asset_declaration_rejects(tmp_path: Path, asset_path: str) -> None:
    """Catches a signed declaration escaping the package root on any host flavor."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    document = _manifest(package)
    document["assets"][0]["path"] = asset_path
    document["entry_points"][0]["skill_path"] = asset_path
    _write_signed_manifest(package, private_key, document)

    _assert_rejected(package, ring, "manifest_invalid")


@pytest.mark.parametrize(
    "asset_path",
    ["skills/NUL/SKILL.md", "skills/con/SKILL.md", "workflows/LpT9.json"],
)
def test_windows_reserved_alias_in_asset_declaration_rejects(
    tmp_path: Path, asset_path: str
) -> None:
    """Catches portable packages that map to Windows device aliases case-insensitively."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    document = _manifest(package)
    document["assets"][0]["path"] = asset_path
    document["entry_points"][0]["skill_path"] = asset_path
    _write_signed_manifest(package, private_key, document)

    _assert_rejected(package, ring, "manifest_invalid")


def test_casefolded_asset_aliases_cannot_name_one_portable_file(tmp_path: Path) -> None:
    """Catches two signed declarations collapsing to one file on Windows."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    document = _manifest(package)
    alias = dict(document["assets"][0])
    alias["path"] = "skills/WORKSPACE/SKILL.md"
    document["assets"].append(alias)
    _write_signed_manifest(package, private_key, document)

    _assert_rejected(package, ring, "manifest_invalid")


@pytest.mark.parametrize(
    "alias",
    [
        "skills//workspace/SKILL.md",
        "skills/workspace//SKILL.md",
        "skills/workspace/SKILL.md/",
    ],
)
def test_normalized_asset_aliases_cannot_name_one_portable_file(tmp_path: Path, alias: str) -> None:
    """Catches distinct signed strings normalizing to one physical package path."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    document = _manifest(package)
    aliased = dict(document["assets"][0])
    aliased["path"] = alias
    document["assets"].append(aliased)
    _write_signed_manifest(package, private_key, document)

    _assert_rejected(package, ring, "manifest_invalid")


def test_tree_entry_limit_rejects_the_first_entry_beyond_4096(tmp_path: Path) -> None:
    """Catches metadata-only trees exhausting memory before the package byte gate."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    filler = package / "filler"
    filler.mkdir()
    for number in range(4091):
        (filler / f"{number:04x}").touch()

    exact_regular_files(package)
    (filler / "overflow").touch()
    _assert_rejected(package, ring, "package_tree_too_large")


def test_tree_depth_limit_rejects_directory_level_33(tmp_path: Path) -> None:
    """Catches recursive-depth exhaustion and pins the 32-directory boundary."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    current = package
    for _ in range(32):
        current /= "d"
        current.mkdir()
    (current / "at-limit").touch()

    exact_regular_files(package)
    too_deep = current / "d"
    too_deep.mkdir()
    (too_deep / "beyond-limit").touch()
    _assert_rejected(package, ring, "package_tree_too_deep")


def test_concurrent_ancestor_swap_during_tree_traversal_never_admits_external_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches reopening a checked directory by path after a traversal-time swap."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    outside = tmp_path / "outside-skills"
    (outside / "workspace").mkdir(parents=True)
    (outside / "workspace" / "SKILL.md").write_bytes(b"# External bytes\n")
    reached = threading.Event()
    release = threading.Event()
    real_scandir = os.scandir
    calls = 0

    def pausing_scandir(path: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            reached.set()
            if not release.wait(10):
                raise RuntimeError("tree race rendezvous timed out")
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", pausing_scandir)
    thread, outcome = _verify_in_thread(package, ring)
    assert reached.wait(10)
    swapped = _replace_directory_with_link(package / "skills", outside)
    release.set()
    thread.join(10)

    assert not thread.is_alive()
    if swapped:
        assert str(outcome["error"]) == "package_tree_invalid"
        assert "verified" not in outcome
    else:
        assert os.name == "nt"
        assert "error" not in outcome
        assert outcome["verified"].manifest.plugin_id == "test.workspace"  # type: ignore[union-attr]


def test_concurrent_ancestor_swap_before_asset_read_never_admits_external_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches path-based asset opens after the verified ancestor was replaced."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(b"# External bytes\n")
    reached = threading.Event()
    release = threading.Event()
    real_verify = package_verifier_module.verify_signed_bytes

    def pausing_verify(*args: object, **kwargs: object) -> None:
        real_verify(*args, **kwargs)  # type: ignore[arg-type]
        reached.set()
        if not release.wait(10):
            raise RuntimeError("asset race rendezvous timed out")

    monkeypatch.setattr(package_verifier_module, "verify_signed_bytes", pausing_verify)
    thread, outcome = _verify_in_thread(package, ring)
    assert reached.wait(10)
    swapped = _replace_directory_with_link(package / "skills" / "workspace", outside)
    release.set()
    thread.join(10)

    assert not thread.is_alive()
    if swapped:
        assert str(outcome["error"]) == "package_tree_invalid"
        assert "verified" not in outcome
    else:
        assert os.name == "nt"
        assert "error" not in outcome
        assert outcome["verified"].manifest.plugin_id == "test.workspace"  # type: ignore[union-attr]


def test_post_validation_source_swap_cannot_rebind_returned_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches returning reopenable source paths after authenticated bytes are finalized."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    relative = Path("skills/workspace/SKILL.md")
    original_payload = b"# Workspace\n\nUse the connected workspace deliberately.\n"
    external_payload = b"# External bytes\n"
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(external_payload)
    reached = threading.Event()
    release = threading.Event()
    real_validate = filesystem_module.PinnedPackageTree.validate_bindings

    def pausing_validate(tree: filesystem_module.PinnedPackageTree) -> None:
        real_validate(tree)
        reached.set()
        if not release.wait(10):
            raise RuntimeError("post-validation race rendezvous timed out")

    monkeypatch.setattr(
        filesystem_module.PinnedPackageTree,
        "validate_bindings",
        pausing_validate,
    )
    thread, outcome = _verify_in_thread(package, ring)
    assert reached.wait(10)
    swapped = _replace_directory_with_link(package / "skills" / "workspace", outside)
    assert swapped is True
    release.set()
    thread.join(10)

    assert not thread.is_alive()
    assert "error" not in outcome
    verified = outcome["verified"]
    assert verified.asset_paths == {relative.as_posix(): relative}  # type: ignore[union-attr]
    assert verified.asset_bytes == {  # type: ignore[union-attr]
        relative.as_posix(): original_payload
    }
    returned_path = verified.asset_paths[relative.as_posix()]  # type: ignore[union-attr]
    assert returned_path.is_absolute() is False
    assert returned_path.resolve() != (outside / "SKILL.md").resolve()
    assert (package / returned_path).read_bytes() == external_payload
    (package / "skills" / "workspace-before-swap").rename(tmp_path / "released-post-validation")


@pytest.mark.parametrize("exit_kind", ["success", "failure", "cancellation"])
def test_pinned_handles_close_after_every_verifier_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_kind: str,
) -> None:
    """Catches leaked root, directory, or file handles after any verifier exit."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    real_open = filesystem_module._open_node
    real_close = filesystem_module._close_native
    live_handles: dict[int, str] = {}
    # The patch is module-wide, so a background thread left by an earlier test can open
    # pinned nodes meanwhile. The verifier runs on this thread; only its handles count.
    verifier_thread = threading.current_thread()

    def tracked_open(*args: object, **kwargs: object):
        handle, information = real_open(*args, **kwargs)  # type: ignore[arg-type]
        if threading.current_thread() is verifier_thread:
            assert handle not in live_handles
            live_handles[handle] = str(args[0])
        return handle, information

    def tracked_close(handle: int) -> None:
        if threading.current_thread() is verifier_thread:
            live_handles.pop(handle)
        real_close(handle)

    monkeypatch.setattr(filesystem_module, "_open_node", tracked_open)
    monkeypatch.setattr(filesystem_module, "_close_native", tracked_close)
    if exit_kind == "failure":
        (package / "skills" / "workspace" / "SKILL.md").write_bytes(b"changed")
        with pytest.raises(PackageVerificationError, match="^asset_digest_mismatch$"):
            verify_plugin_package(package, ring, "3.0.0")
    elif exit_kind == "cancellation":

        def cancel(*args: object, **kwargs: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(package_verifier_module, "verify_signed_bytes", cancel)
        with pytest.raises(KeyboardInterrupt):
            verify_plugin_package(package, ring, "3.0.0")
    else:
        verify_plugin_package(package, ring, "3.0.0")

    assert live_handles == {}, sorted(live_handles.values())
    package.rename(tmp_path / f"moved-after-{exit_kind}")


def test_verifier_entry_boundary_stays_within_a_deterministic_handle_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches retaining one native descriptor per entry below the 4,096-entry ceiling."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    filler = package / "filler"
    filler.mkdir()
    for number in range(4091):
        (filler / f"{number:04x}").touch()

    real_open = filesystem_module._open_node
    real_close = filesystem_module._close_native
    live_handles: set[int] = set()
    peak_live = 0

    def budgeted_open(*args: object, **kwargs: object):
        nonlocal peak_live
        handle, information = real_open(*args, **kwargs)  # type: ignore[arg-type]
        if len(live_handles) >= 64:
            real_close(handle)
            raise OSError("deterministic native handle budget exhausted")
        live_handles.add(handle)
        peak_live = max(peak_live, len(live_handles))
        return handle, information

    def tracked_close(handle: int) -> None:
        live_handles.remove(handle)
        real_close(handle)

    monkeypatch.setattr(filesystem_module, "_open_node", budgeted_open)
    monkeypatch.setattr(filesystem_module, "_close_native", tracked_close)

    _assert_rejected(package, ring, "package_file_set_mismatch")

    assert peak_live <= 64
    assert live_handles == set()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_fsync_higher_ancestor_swap_cannot_follow_external_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Catches opening an immediate parent absolutely before pinning its higher ancestor."""
    ancestor = tmp_path / "package" / "ancestor"
    parent = ancestor / "parent"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside_parent = outside / "parent"
    outside_parent.mkdir(parents=True)
    if kind == "file":
        candidate = parent / "payload"
        candidate.write_bytes(b"package")
        (outside_parent / candidate.name).write_bytes(b"external")
        operation = partial(fsync_file, candidate)
    else:
        candidate = parent / "metadata"
        candidate.mkdir()
        (outside_parent / candidate.name).mkdir()
        operation = partial(fsync_directory, candidate)

    reached = threading.Event()
    release = threading.Event()
    real_open = filesystem_module._open_node
    real_close = filesystem_module._close_native
    live_handles: set[int] = set()
    paused = False

    def pausing_open(path: Path, *args: object, **kwargs: object):
        nonlocal paused
        if Path(path) == candidate.parent and not paused:
            paused = True
            reached.set()
            if not release.wait(10):
                raise RuntimeError("fsync race rendezvous timed out")
        handle, information = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
        live_handles.add(handle)
        return handle, information

    def tracked_close(handle: int) -> None:
        live_handles.remove(handle)
        real_close(handle)

    monkeypatch.setattr(filesystem_module, "_open_node", pausing_open)
    monkeypatch.setattr(filesystem_module, "_close_native", tracked_close)
    thread, outcome = _operation_in_thread(operation)
    assert reached.wait(10)
    swapped = _replace_directory_with_link(ancestor, outside)
    release.set()
    thread.join(10)

    assert not thread.is_alive()
    if swapped:
        assert type(outcome.get("error")) is ValueError
        assert str(outcome["error"]) == "package_tree_invalid"
        original = ancestor.with_name("ancestor-before-swap")
    else:
        assert os.name == "nt"
        assert outcome == {"completed": True}
        original = ancestor
    assert live_handles == set()
    original.rename(tmp_path / f"released-fsync-{kind}")


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative binding behavior")
def test_posix_final_binding_never_opens_a_descendant_absolutely_after_ancestor_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches O_NOFOLLOW protecting only the final component of an absolute reopen."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    outside = tmp_path / "outside-skills"
    (outside / "workspace").mkdir(parents=True)
    (outside / "workspace" / "SKILL.md").write_bytes(b"# External bytes\n")
    reached = threading.Event()
    release = threading.Event()
    real_open = filesystem_module._open_node
    real_validate = filesystem_module.PinnedPackageTree.validate_bindings
    validating = threading.Event()
    paused = False
    swapped = False

    def phase_marked_validate(tree: filesystem_module.PinnedPackageTree) -> None:
        validating.set()
        try:
            real_validate(tree)
        finally:
            validating.clear()

    def guarded_open(path: Path, *args: object, **kwargs: object):
        nonlocal paused
        candidate = Path(path)
        if validating.is_set() and candidate == package / "skills" and not paused:
            paused = True
            result = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
            reached.set()
            if not release.wait(10):
                raise RuntimeError("binding race rendezvous timed out")
            return result
        if (
            swapped
            and candidate == package / "skills" / "workspace"
            and kwargs.get("parent_handle") is None
        ):
            raise AssertionError("unsafe absolute descendant reopen")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(filesystem_module, "_open_node", guarded_open)
    monkeypatch.setattr(
        filesystem_module.PinnedPackageTree,
        "validate_bindings",
        phase_marked_validate,
    )
    thread, outcome = _verify_in_thread(package, ring)
    assert reached.wait(10)
    swapped = _replace_directory_with_link(package / "skills", outside)
    assert swapped is True
    release.set()
    thread.join(10)

    assert not thread.is_alive()
    assert type(outcome.get("error")) is PackageVerificationError
    assert str(outcome["error"]) == "package_tree_invalid"
    assert "verified" not in outcome
    (package / "skills-before-swap").rename(tmp_path / "released-posix-binding")


def test_oversized_manifest_rejects_before_json_parsing(tmp_path: Path) -> None:
    """Catches parsing or retaining a plugin manifest beyond its closed byte boundary."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    (package / "plugin.json").write_bytes(b"{" + b"x" * MAX_MANIFEST_BYTES)

    _assert_rejected(package, ring, "manifest_too_large")


@pytest.mark.parametrize(
    ("relative_path", "kind", "media_type", "limit", "prefix"),
    [
        ("skills/large/SKILL.md", "skill", "text/markdown", MAX_SKILL_BYTES, b"#"),
        ("workflows/large.json", "workflow", "application/json", MAX_WORKFLOW_BYTES, b"{"),
        ("assets/large.png", "icon", "image/png", MAX_ICON_BYTES, _PNG_SIGNATURE),
    ],
)
def test_each_asset_kind_has_a_closed_byte_boundary(
    tmp_path: Path,
    relative_path: str,
    kind: str,
    media_type: str,
    limit: int,
    prefix: bytes,
) -> None:
    """Catches reading any declared asset beyond its kind-specific bound."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    payload = prefix + b"x" * limit
    _add_asset(
        package,
        private_key,
        relative_path=relative_path,
        kind=kind,
        media_type=media_type,
        payload=payload,
    )

    _assert_rejected(package, ring, "asset_too_large")


def test_total_package_bytes_have_a_closed_boundary(tmp_path: Path) -> None:
    """Catches a collection of individually valid assets exceeding the package ceiling."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    each_size = (MAX_PACKAGE_BYTES // 5) + 1
    for number in range(5):
        _add_asset(
            package,
            private_key,
            relative_path=f"assets/icon-{number}.png",
            kind="icon",
            media_type="image/png",
            payload=_PNG_SIGNATURE + b"x" * (each_size - len(_PNG_SIGNATURE)),
        )

    _assert_rejected(package, ring, "package_too_large")


def test_skill_markdown_requires_exact_utf8(tmp_path: Path) -> None:
    """Catches admitting signed skill bytes that are not exact UTF-8 Markdown text."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    skill = package / "skills" / "workspace" / "SKILL.md"
    payload = b"# Skill\n\xff"
    skill.write_bytes(payload)
    document = _manifest(package)
    document["assets"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    _write_signed_manifest(package, private_key, document)

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize(
    ("relative_path", "kind", "media_type", "payload", "reason"),
    [
        ("skills/workspace/guide.md", "skill", "text/markdown", b"# Guide\n", "asset_invalid"),
        ("workflows/job.yaml", "workflow", "application/json", b"{}", "asset_invalid"),
        (
            "assets/plugin.webp",
            "icon",
            "image/webp",
            b"RIFF-not-an-icon",
            "manifest_invalid",
        ),
    ],
)
def test_asset_kind_rejects_every_disallowed_file_type(
    tmp_path: Path,
    relative_path: str,
    kind: str,
    media_type: str,
    payload: bytes,
    reason: str,
) -> None:
    """Catches executable or alternate file formats entering a declarative asset lane."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    _add_asset(
        package,
        private_key,
        relative_path=relative_path,
        kind=kind,
        media_type=media_type,
        payload=payload,
    )

    _assert_rejected(package, ring, reason)


def test_skill_markdown_cannot_begin_with_a_shebang(tmp_path: Path) -> None:
    """Catches executable script content disguised behind the skill Markdown filename."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    skill = package / "skills" / "workspace" / "SKILL.md"
    payload = b"#!/usr/bin/env python\nprint('not declarative')\n"
    skill.write_bytes(payload)
    document = _manifest(package)
    document["assets"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    _write_signed_manifest(package, private_key, document)

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize(
    "payload",
    [b'{"steps":[],"steps":[1]}', b'{ "steps": [] }'],
)
def test_workflow_requires_duplicate_free_exact_canonical_json(
    tmp_path: Path, payload: bytes
) -> None:
    """Catches ambiguous duplicate keys or noncanonical JSON representations in workflows."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    _add_asset(
        package,
        private_key,
        relative_path="workflows/job.json",
        kind="workflow",
        media_type="application/json",
        payload=payload,
    )

    _assert_rejected(package, ring, "asset_invalid")


def test_icon_requires_the_png_file_signature(tmp_path: Path) -> None:
    """Catches arbitrary signed bytes disguised by a PNG filename and media type."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    _add_asset(
        package,
        private_key,
        relative_path="assets/plugin.png",
        kind="icon",
        media_type="image/png",
        payload=b"not-png-data",
    )

    _assert_rejected(package, ring, "asset_invalid")


@pytest.mark.parametrize("omega_version", ["2.9.9", "not-a-version"])
def test_incompatible_or_invalid_omega_version_rejects(tmp_path: Path, omega_version: str) -> None:
    """Catches activating a package outside its declared Omega compatibility range."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)

    _assert_rejected(package, ring, "omega_version_incompatible", omega_version=omega_version)


@pytest.mark.parametrize(
    "relative_path",
    [
        "workflows/client_secret.json",
        "workflows/provider-token.json",
        "assets/private_key.png",
    ],
)
def test_secret_bearing_asset_filename_rejects(tmp_path: Path, relative_path: str) -> None:
    """Catches credentials being smuggled into an otherwise allowed declarative filename."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    if relative_path.startswith("assets/"):
        kind, media_type, payload = "icon", "image/png", _PNG_SIGNATURE
    else:
        kind, media_type, payload = "workflow", "application/json", b"{}"
    _add_asset(
        package,
        private_key,
        relative_path=relative_path,
        kind=kind,
        media_type=media_type,
        payload=payload,
    )

    _assert_rejected(package, ring, "secret_material_forbidden")


def test_secret_bearing_manifest_field_rejects_without_disclosure(tmp_path: Path) -> None:
    """Catches an open manifest retaining credential material or leaking it on rejection."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    document = _manifest(package)
    sentinel = "opaque-provider-material-must-never-leak"
    document["credentials"] = sentinel
    _write_signed_manifest(package, private_key, document)

    with pytest.raises(PackageVerificationError) as caught:
        verify_plugin_package(package, ring, "3.0.0")

    assert str(caught.value) == "manifest_invalid"
    assert sentinel not in str(caught.value)
    assert caught.value.__cause__ is None


def test_manifest_bytes_reject_duplicate_keys_and_non_utf8(tmp_path: Path) -> None:
    """Catches ambiguous or non-text plugin manifests reaching contract parsing."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    manifest_path = package / "plugin.json"
    original = manifest_path.read_bytes()
    duplicate = original.replace(b'{"assets":', b'{"assets":[],"assets":', 1)
    manifest_path.write_bytes(duplicate)
    _assert_rejected(package, ring, "manifest_json_invalid")

    manifest_path.write_bytes(original + b"\xff")
    _assert_rejected(package, ring, "manifest_json_invalid")


def test_all_errors_are_content_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches package paths, signed content, signatures, or environment data in errors."""
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path / "private-package-location", private_key)
    document = _manifest(package)
    signature = document["signature"]
    assert isinstance(signature, str)
    environment_secret = "environment-material-must-not-leak"
    monkeypatch.setenv("OMEGA_PLUGIN_TEST_SECRET", environment_secret)
    document["title"] = "signed-content-must-not-leak"
    (package / "plugin.json").write_bytes(canonical_bytes(document))

    with pytest.raises(PackageVerificationError) as caught:
        verify_plugin_package(package, ring, "3.0.0")

    message = str(caught.value)
    for forbidden in (
        str(package),
        "signed-content-must-not-leak",
        signature,
        environment_secret,
    ):
        assert forbidden not in message
    assert caught.value.__cause__ is None
