from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import lmstudio_updater


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _archive(path: Path, files: dict[str, str]) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(archive: Path, digest: str, *, version: str = "0.4.22") -> dict[str, object]:
    return {
        "schema": 1,
        "product": "LM Studio",
        "platform": "windows-x64",
        "version": version,
        "artifact_url": archive.as_uri(),
        "artifact_type": "zip",
        "sha256": digest,
        "root": "LM Studio",
        "replace_paths": ["LM Studio.exe", "resources"],
        "launcher": "Launch Portable.cmd",
    }


def test_versions_compare_numeric_builds_and_prereleases() -> None:
    assert lmstudio_updater.compare_versions("0.4.9", "0.4.21+2") < 0
    assert lmstudio_updater.compare_versions("0.4.21+2", "0.4.21+2") == 0
    assert lmstudio_updater.compare_versions("0.4.22-beta", "0.4.22") < 0


def test_official_update_check_uses_the_portable_install_version_and_build(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "resources" / "app" / "package.json", '{"version":"0.4.21+2"}')

    result = lmstudio_updater.check_official_update(
        app,
        fetch_version=lambda url: (
            {
                "version": "0.4.24",
                "build": "1",
                "releaseNotes": "Fixes and improvements",
            }
            if url.endswith("/win32/x86/0.4.21")
            else pytest.fail(f"unexpected update URL: {url}")
        ),
    )

    assert result.current_version == "0.4.21+2"
    assert result.latest_version == "0.4.24+1"
    assert result.available is True
    assert result.installer_url == (
        "https://updates.lmstudio.ai/LM-Studio-0.4.24-1-win32-x64.exe"
    )


def test_official_update_apply_requires_a_valid_signature_and_relaunches_portable_root(
    tmp_path: Path,
) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "resources" / "app" / "package.json", '{"version":"0.4.21+2"}')
    _write(app / "Launch Portable.cmd", "@echo off\r\n")
    installer = tmp_path / "LM-Studio-0.4.24-1-win32-x64.exe"
    installer.write_bytes(b"signed installer")
    check = lmstudio_updater.OfficialUpdateCheck(
        current_version="0.4.21+2",
        latest_version="0.4.24+1",
        available=True,
        installer_url="https://updates.lmstudio.ai/LM-Studio-0.4.24-1-win32-x64.exe",
        release_notes="Fixes and improvements",
    )
    launched: list[Path] = []

    def run_installer(command, **_kwargs):
        assert len(command) == 3
        assert Path(command[0]) == installer
        assert command[1] == "/S"
        assert command[2].startswith("/D=")
        assert Path(command[2][3:]) == app.resolve()
        _write(app / "resources" / "app" / "package.json", '{"version":"0.4.24+1"}')
        return subprocess.CompletedProcess(command, 0)

    result = lmstudio_updater.apply_official_update(
        app,
        check,
        installer_path=installer,
        verify_signature=lambda _path: True,
        run_installer=run_installer,
        is_running=lambda: False,
        relauncher=launched.append,
    )

    assert result.version == "0.4.24+1"
    assert len(launched) == 1
    assert launched[0] == app / "Launch Portable.cmd"


def test_official_update_refuses_an_unsigned_installer(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "resources" / "app" / "package.json", '{"version":"0.4.21+2"}')
    installer = tmp_path / "LM-Studio-0.4.24-1-win32-x64.exe"
    installer.write_bytes(b"unsigned installer")
    check = lmstudio_updater.OfficialUpdateCheck(
        current_version="0.4.21+2",
        latest_version="0.4.24+1",
        available=True,
        installer_url="https://updates.lmstudio.ai/LM-Studio-0.4.24-1-win32-x64.exe",
        release_notes="",
    )

    with pytest.raises(lmstudio_updater.UpdateError, match="signature"):
        lmstudio_updater.apply_official_update(
            app,
            check,
            installer_path=installer,
            verify_signature=lambda _path: False,
            is_running=lambda: False,
            relauncher=lambda _path: pytest.fail("must not relaunch"),
        )


def test_cached_official_installer_requires_lm_studio_receipt_digest(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    pending = lmstudio_updater.official_pending_directory(app)
    installer = pending / "LM-Studio-0.4.24-1-win32-x64.exe"
    installer.parent.mkdir(parents=True)
    installer.write_bytes(b"signed installer")
    digest = base64.b64encode(hashlib.sha512(installer.read_bytes()).digest()).decode("ascii")
    _write(pending / "update-info.json", json.dumps({"fileName": installer.name, "sha512": digest}))
    check = lmstudio_updater.OfficialUpdateCheck(
        current_version="0.4.21+2",
        latest_version="0.4.24+1",
        available=True,
        installer_url="https://updates.lmstudio.ai/LM-Studio-0.4.24-1-win32-x64.exe",
        release_notes="",
    )

    assert lmstudio_updater.find_cached_official_installer(app, check) == installer.resolve()


def test_manifest_rejects_non_windows_or_non_archive_payload() -> None:
    with pytest.raises(lmstudio_updater.UpdateError, match="windows-x64"):
        lmstudio_updater.parse_manifest(
            {
                "schema": 1,
                "product": "LM Studio",
                "platform": "linux-x64",
                "version": "1",
                "artifact_url": "https://example.invalid/lm.zip",
            }
        )


def test_default_lm_studio_relauncher_is_windowless_and_streamless(tmp_path, monkeypatch) -> None:
    launcher = tmp_path / "Launch Portable.cmd"
    _write(launcher, "@echo off\n")
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    lmstudio_updater._default_relauncher(launcher)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == f'call "{launcher}"'
    assert kwargs["shell"] is True
    assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL

    with pytest.raises(lmstudio_updater.UpdateError, match="zip"):
        lmstudio_updater.parse_manifest(
            {
                "schema": 1,
                "platform": "windows-x64",
                "version": "1",
                "artifact_url": "https://example.invalid/lm.exe",
                "product": "LM Studio",
                "artifact_type": "installer",
            }
        )


def test_check_reads_portable_manifest_and_reports_update(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "resources" / "app" / "package.json", '{"version":"0.4.21+2"}')
    archive = tmp_path / "lmstudio.zip"
    digest = _archive(
        archive,
        {
            "LM Studio/LM Studio.exe": "new",
            "LM Studio/resources/app/package.json": '{"version":"0.4.22"}',
        },
    )
    manifest = _manifest(archive, digest)

    result = lmstudio_updater.check_for_update(
        app,
        manifest_url="https://updates.example.invalid/lmstudio.json",
        fetch_manifest=lambda _url: manifest,
    )

    assert result.current_version == "0.4.21+2"
    assert result.latest_version == "0.4.22"
    assert result.available is True


def test_apply_replaces_only_application_payload_and_relaunches(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "LM Studio.exe", "old")
    _write(app / "resources" / "app" / "package.json", '{"version":"0.4.21+2"}')
    _write(app / "Launch Portable.cmd", "@echo off\r\n")
    _write(app / "models" / "user-model.gguf", "keep")
    _write(app / "settings.json", '{"downloadsFolder":"portable"}')

    archive = tmp_path / "lmstudio.zip"
    digest = _archive(
        archive,
        {
            "LM Studio/LM Studio.exe": "new",
            "LM Studio/resources/app/package.json": '{"version":"0.4.22"}',
        },
    )
    manifest = lmstudio_updater.parse_manifest(_manifest(archive, digest))
    relaunched: list[Path] = []

    result = lmstudio_updater.apply_update(
        app,
        manifest,
        downloader=lambda _url, destination: shutil.copyfile(archive, destination),
        is_running=lambda: False,
        relauncher=relaunched.append,
    )

    assert result.version == "0.4.22"
    assert (app / "LM Studio.exe").read_text(encoding="utf-8") == "new"
    assert json.loads((app / "resources" / "app" / "package.json").read_text(encoding="utf-8"))["version"] == "0.4.22"
    assert (app / "models" / "user-model.gguf").read_text(encoding="utf-8") == "keep"
    assert (app / "settings.json").is_file()
    assert relaunched == [app / "Launch Portable.cmd"]


def test_normalize_portable_settings_rewrites_all_known_install_settings(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    legacy = r"C:\Users\builder\.lmstudio\models"
    _write(app / "settings.json", json.dumps({"downloadsFolder": legacy}))
    _write(
        app / "apps" / "bionic" / "settings.json",
        json.dumps({"downloadsFolder": legacy, "language": "en"}),
    )

    models = app / "models"
    touched = lmstudio_updater.normalize_portable_settings(app, model_root=models)

    assert touched == (app / "settings.json", app / "apps" / "bionic" / "settings.json")
    for path in touched:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["downloadsFolder"] == str(models.resolve())


def test_normalize_portable_settings_fails_on_corrupt_settings(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "apps" / "bionic" / "settings.json", "{broken")

    with pytest.raises(lmstudio_updater.UpdateError, match="settings"):
        lmstudio_updater.normalize_portable_settings(app)


def test_apply_rejects_path_traversal_before_touching_app(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "LM Studio.exe", "old")
    archive = tmp_path / "lmstudio.zip"
    digest = _archive(archive, {"LM Studio/../../outside.txt": "bad"})
    manifest = lmstudio_updater.parse_manifest(_manifest(archive, digest))

    with pytest.raises(lmstudio_updater.UpdateError, match="unsafe archive path"):
        lmstudio_updater.apply_update(
            app,
            manifest,
            downloader=lambda _url, destination: shutil.copyfile(archive, destination),
            is_running=lambda: False,
            relauncher=lambda _path: pytest.fail("must not relaunch"),
        )

    assert (app / "LM Studio.exe").read_text(encoding="utf-8") == "old"


def test_apply_refuses_to_touch_a_running_lm_studio(tmp_path: Path) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "LM Studio.exe", "old")
    archive = tmp_path / "lmstudio.zip"
    digest = _archive(archive, {"LM Studio/LM Studio.exe": "new"})
    manifest = lmstudio_updater.parse_manifest(_manifest(archive, digest))

    with pytest.raises(lmstudio_updater.UpdateError, match="still running"):
        lmstudio_updater.apply_update(
            app,
            manifest,
            downloader=lambda _url, destination: shutil.copyfile(archive, destination),
            is_running=lambda: True,
            relauncher=lambda _path: None,
        )

    assert (app / "LM Studio.exe").read_text(encoding="utf-8") == "old"


def test_apply_uses_the_windows_process_guard_by_default(tmp_path: Path, monkeypatch) -> None:
    app = tmp_path / "LM Studio"
    _write(app / "LM Studio.exe", "old")
    archive = tmp_path / "lmstudio.zip"
    digest = _archive(archive, {"LM Studio/LM Studio.exe": "new"})
    manifest = lmstudio_updater.parse_manifest(_manifest(archive, digest))
    monkeypatch.setattr(lmstudio_updater, "_default_is_running", lambda _root: True)

    with pytest.raises(lmstudio_updater.UpdateError, match="still running"):
        lmstudio_updater.apply_update(
            app,
            manifest,
            downloader=lambda _url, destination: shutil.copyfile(archive, destination),
            relauncher=lambda _path: pytest.fail("must not relaunch"),
        )
