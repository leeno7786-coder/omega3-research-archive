from __future__ import annotations

import json
import inspect
import os

import pytest

from core.paths import OMEGA_ROOT, portable_python_path
from scripts import omega_launcher


def _path_identity(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(value)))


def test_startup_configuration_round_trips_without_plaintext_api_key(tmp_path) -> None:
    payload = {
        "backend": "cloud",
        "provider": "openrouter",
        "model": "openai/gpt-4.1-mini",
        "base_url": "https://openrouter.ai/api/v1",
    }

    state = omega_launcher.save_startup_configuration(
        tmp_path,
        payload,
        api_key="test-secret-key",
    )

    assert state["setup_completed"] is True
    assert state["backend"] == "cloud"
    assert state["provider"] == "openrouter"
    assert state["model"] == payload["model"]

    state_file = omega_launcher.startup_state_path(tmp_path)
    state_text = state_file.read_text(encoding="utf-8")
    assert "test-secret-key" not in state_text
    assert "api_key" not in json.loads(state_text)
    assert omega_launcher.load_startup_state(tmp_path) == state

    vault = tmp_path / "guardian" / "state" / "connectors" / "omega-connect.secrets.json"
    assert vault.is_file()
    assert "test-secret-key" not in vault.read_text(encoding="utf-8")


def test_cloud_configuration_requires_a_key_on_first_setup(tmp_path) -> None:
    payload = {
        "backend": "cloud",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "base_url": "https://api.openai.com/v1",
    }

    with pytest.raises(RuntimeError, match="API key is required"):
        omega_launcher.save_startup_configuration(tmp_path, payload)


def test_local_configuration_does_not_require_cloud_credentials(tmp_path) -> None:
    payload = {
        "backend": "local",
        "provider": "lmstudio",
        "model": "local-model",
        "base_url": "http://127.0.0.1:1234/v1",
    }

    state = omega_launcher.save_startup_configuration(tmp_path, payload)

    assert state["backend"] == "local"
    assert state["local_model"] == "local-model"
    assert not (
        tmp_path / "guardian" / "state" / "connectors" / "omega-connect.secrets.json"
    ).exists()


def test_lm_studio_launch_is_windowless_and_does_not_inherit_console_streams(
    tmp_path, monkeypatch
) -> None:
    launcher = tmp_path / "LM Studio" / "Launch Portable.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\n", encoding="utf-8")
    calls = []

    class _Process:
        pid = 123

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(omega_launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    omega_launcher.launch_lm_studio(tmp_path, popen=fake_popen)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert _path_identity(command) == _path_identity(f'call "{launcher}"')
    assert kwargs["shell"] is True
    assert kwargs["creationflags"] & omega_launcher.subprocess.CREATE_NO_WINDOW
    assert kwargs["stdin"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stdout"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stderr"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["env"][omega_launcher.OMEGA_BROKER_AUTHORIZED_ENV] == "1"


def test_hud_launch_uses_only_the_portable_runtime_and_is_windowless(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "Omega"
    python = portable_python_path(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"portable-python")
    hud = root / "hud" / "standalone.py"
    hud.parent.mkdir(parents=True)
    hud.write_text("", encoding="utf-8")
    calls = []

    class _Process:
        pid = 654

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(omega_launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    omega_launcher.launch_hud(root, ("--port", "13311", "--no-boot"), popen=fake_popen)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert len(command) == 5
    assert _path_identity(command[0]) == _path_identity(python)
    assert _path_identity(command[1]) == _path_identity(hud)
    assert command[2:] == ["--port", "13311", "--no-boot"]
    assert _path_identity(kwargs["cwd"]) == _path_identity(root)
    assert _path_identity(kwargs["env"]["OMEGA_ROOT"]) == _path_identity(root.resolve())
    assert kwargs["env"][omega_launcher.OMEGA_HUD_OPEN_BROWSER_ENV] == "1"
    assert kwargs["creationflags"] & omega_launcher.subprocess.CREATE_NO_WINDOW
    assert kwargs["stdin"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stdout"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stderr"] is omega_launcher.subprocess.DEVNULL


def test_main_dispatches_broker_commands_without_opening_setup_ui(
    tmp_path, monkeypatch
) -> None:
    seen = []
    monkeypatch.setattr(omega_launcher, "product_root", lambda: tmp_path / "Omega")
    monkeypatch.setattr(
        omega_launcher,
        "launch_hud",
        lambda product, arguments: seen.append(("hud", product, tuple(arguments))),
    )
    monkeypatch.setattr(
        omega_launcher,
        "launch_lm_studio",
        lambda ssd: seen.append(("lm", ssd)),
    )
    monkeypatch.setattr(
        omega_launcher,
        "run_setup_wizard",
        lambda *_args: (_ for _ in ()).throw(AssertionError("setup UI opened")),
    )

    omega_launcher.main(["--launch-hud", "--port", "13311"])
    omega_launcher.main(["--launch-lm-studio"])

    assert seen == [
        ("hud", (tmp_path / "Omega").resolve(), ("--port", "13311")),
        ("lm", tmp_path.resolve()),
    ]


def test_omega_update_targets_only_the_ssd_portable_lm_studio_root(tmp_path) -> None:
    launcher = tmp_path / "LM Studio" / "Launch Portable.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\n", encoding="utf-8")
    seen = []

    def fake_updater(app_root):
        seen.append(app_root)
        return "updated"

    result = omega_launcher.update_lm_studio(tmp_path, updater=fake_updater)

    assert result == "updated"
    assert seen == [(tmp_path / "LM Studio").resolve()]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only launcher branch")
def test_linux_runtime_launch_is_windowless_and_runs_the_portable_command(
    tmp_path, monkeypatch
) -> None:
    launcher = tmp_path / "LM-Studio-Linux" / "run-ai.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    class _Process:
        pid = 789

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(omega_launcher.shutil, "which", lambda name: "wsl.exe")
    monkeypatch.setattr(omega_launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    omega_launcher.launch_linux_runtime(tmp_path, popen=fake_popen)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ["wsl.exe", "--cd"]
    expected_directory = omega_launcher._windows_directory_to_wsl(launcher.parent)
    assert _path_identity(command[2]) == _path_identity(expected_directory)
    assert command[3:] == ["sh", "-lc", omega_launcher.LINUX_STARTUP_COMMAND]
    assert _path_identity(kwargs["cwd"]) == _path_identity(launcher.parent)
    assert kwargs["creationflags"] & omega_launcher.subprocess.CREATE_NO_WINDOW
    assert kwargs["stdin"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stdout"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stderr"] is omega_launcher.subprocess.DEVNULL


@pytest.mark.skipif(os.name != "nt", reason="Windows-only launcher branch")
def test_linux_runtime_launch_fails_when_script_or_wsl_is_missing(tmp_path, monkeypatch) -> None:
    with pytest.raises(FileNotFoundError, match="run-ai.sh"):
        omega_launcher.launch_linux_runtime(tmp_path, popen=lambda *_args, **_kwargs: None)

    launcher = tmp_path / "LM-Studio-Linux" / "run-ai.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(omega_launcher.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="WSL"):
        omega_launcher.launch_linux_runtime(tmp_path, popen=lambda *_args, **_kwargs: None)


def test_omega_launch_is_windowless_and_does_not_inherit_console_streams(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "Omega"
    root.mkdir()
    python = portable_python_path(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"portable-python")
    (root / "boot.py").write_text("", encoding="utf-8")
    calls = []

    class _Process:
        pid = 456

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(omega_launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    omega_launcher.launch_omega(root, popen=fake_popen)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert len(command) == 2
    assert _path_identity(command[0]) == _path_identity(python)
    assert _path_identity(command[1]) == _path_identity(root / "boot.py")
    assert kwargs["creationflags"] & omega_launcher.subprocess.CREATE_NO_WINDOW
    assert kwargs["stdin"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stdout"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stderr"] is omega_launcher.subprocess.DEVNULL


def test_factory_reset_uses_only_the_bundled_script_and_is_windowless(tmp_path, monkeypatch) -> None:
    product = tmp_path / "Omega"
    product.mkdir()
    python = portable_python_path(product)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"portable-python")
    script = product / "scripts" / "day_zero_reset.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(omega_launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    omega_launcher.factory_reset(product, runner=fake_run)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert len(command) == 2
    assert _path_identity(command[0]) == _path_identity(python)
    assert _path_identity(command[1]) == _path_identity(script)
    assert _path_identity(kwargs["cwd"]) == _path_identity(product)
    assert _path_identity(kwargs["env"]["OMEGA_ROOT"]) == _path_identity(product.resolve())
    assert kwargs["check"] is True
    assert kwargs["stdin"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stdout"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["stderr"] is omega_launcher.subprocess.DEVNULL
    assert kwargs["creationflags"] & omega_launcher.subprocess.CREATE_NO_WINDOW


@pytest.mark.skipif(os.name != "nt", reason="Windows-only launcher branch")
def test_portable_runtime_folders_are_marked_hidden(tmp_path) -> None:
    product = tmp_path / "Omega"
    lm_studio = tmp_path / "LM Studio"
    linux_lm_studio = tmp_path / "LM-Studio-Linux"
    product.mkdir()
    lm_studio.mkdir()
    linux_lm_studio.mkdir()
    marked = []

    def set_attributes(path, attributes):
        marked.append((path, attributes))
        return 1

    hidden = omega_launcher.hide_portable_runtime(
        product,
        set_attributes=set_attributes,
    )

    assert hidden == (product.resolve(), lm_studio.resolve(), linux_lm_studio.resolve())
    assert [path for path, _attributes in marked] == [str(path) for path in hidden]
    assert all(attributes & omega_launcher.FILE_ATTRIBUTE_HIDDEN for _path, attributes in marked)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only launcher branch")
def test_exposed_launcher_directory_is_never_marked_hidden(tmp_path) -> None:
    product = tmp_path / "Omega3.0"
    lm_studio = tmp_path / "LM Studio"
    linux_lm_studio = tmp_path / "LM-Studio-Linux"
    product.mkdir()
    (product / "Omega.exe").write_bytes(b"public launcher")
    lm_studio.mkdir()
    linux_lm_studio.mkdir()
    marked = []

    def set_attributes(path, attributes):
        marked.append((path, attributes))
        return 1

    hidden = omega_launcher.hide_portable_runtime(
        product,
        set_attributes=set_attributes,
    )

    assert hidden == (lm_studio.resolve(), linux_lm_studio.resolve())
    assert str(product.resolve()) not in [path for path, _attributes in marked]


def test_source_tree_uses_the_drive_root_when_portable_runtimes_are_siblings(tmp_path) -> None:
    product = tmp_path / "Omega3.0"
    product.mkdir()
    (tmp_path / "LM Studio").mkdir()
    (tmp_path / "LM-Studio-Linux").mkdir()

    assert omega_launcher.ssd_root(product) == tmp_path.resolve()


def test_startup_state_rejects_unknown_backend(tmp_path) -> None:
    with pytest.raises(ValueError, match="backend"):
        omega_launcher.save_startup_configuration(
            tmp_path,
            {"backend": "legacy", "model": "model"},
        )


def test_product_metadata_exposes_verified_public_identity() -> None:
    info = omega_launcher.load_product_info(OMEGA_ROOT)

    assert info["website"] == "https://www.omega2ai.com/"
    assert info["privacy_policy"]["url"] == "https://www.omega2ai.com/privacy"
    assert info["contacts"] == ["noahlee@omega2ai.com", "mitchellray@omega2ai.com"]
    assert info["hero_image"] == "config/portable_hero.png"
    assert (OMEGA_ROOT / info["hero_image"]).is_file()
    assert info["intellectual_property"]["application_number"] == "63/965,475"
    assert info["intellectual_property"]["filed_date"] == "2026-01-22"


def test_bundled_privacy_policy_is_available_to_the_setup_help_view() -> None:
    policy = omega_launcher.load_privacy_policy(OMEGA_ROOT)

    assert "Omega 3.0 portable privacy notice" in policy
    assert "cloud provider" in policy
    assert "noahlee@omega2ai.com" in policy


def test_open_external_url_delegates_only_valid_http_urls() -> None:
    opened = []

    omega_launcher.open_external_url(
        "https://www.omega2ai.com/privacy",
        opener=opened.append,
    )

    assert opened == ["https://www.omega2ai.com/privacy"]

    with pytest.raises(ValueError, match="HTTP"):
        omega_launcher.open_external_url("file:///C:/secrets.txt", opener=opened.append)


def test_launch_password_is_hashed_and_verifies(tmp_path) -> None:
    omega_launcher.save_launch_password(tmp_path, "correct horse battery")

    lock_path = tmp_path / "guardian" / "state" / "launcher_lock.json"
    lock_text = lock_path.read_text(encoding="utf-8")
    assert "correct horse battery" not in lock_text
    assert omega_launcher.load_launch_lock(tmp_path)["enabled"] is True
    assert omega_launcher.verify_launch_password(tmp_path, "correct horse battery") is True
    assert omega_launcher.verify_launch_password(tmp_path, "wrong password") is False


def test_launch_password_requires_a_minimum_length(tmp_path) -> None:
    with pytest.raises(ValueError, match="8 characters"):
        omega_launcher.save_launch_password(tmp_path, "short")


def test_clear_launch_password_removes_the_lock(tmp_path) -> None:
    omega_launcher.save_launch_password(tmp_path, "correct horse battery")
    omega_launcher.clear_launch_password(tmp_path)

    assert omega_launcher.load_launch_lock(tmp_path)["enabled"] is False
    assert omega_launcher.verify_launch_password(tmp_path, "correct horse battery") is False


def test_startup_help_explains_simple_vision_requirements() -> None:
    guide = omega_launcher.load_help_guide(OMEGA_ROOT)

    for marker in (
        "## Vision",
        "models/onnxs/face/",
        "vision-capable multimodal model",
        "Face vision does not use the DME NPU",
        "attach",
        "face memory",
    ):
        assert marker in guide


def test_native_setup_window_sizes_outer_frame_from_the_required_client_area() -> None:
    source = inspect.getsource(omega_launcher.SetupWizard._run_native)

    assert "AdjustWindowRectEx" in source
    assert "SETUP_CLIENT_HEIGHT" in source
    assert omega_launcher.SETUP_CLIENT_HEIGHT >= 670


def test_native_cloud_setup_exposes_explicit_encrypted_key_save_action() -> None:
    source = inspect.getsource(omega_launcher)

    assert "_BTN_SAVE_KEY" in source
    assert 'self.controls["save_key"]' in source
    assert '"Save key securely"' in source
    assert "_save_key" in source
    assert "_save_api_key(self.product" in source


def test_native_paint_declares_all_draw_text_alignment_flags() -> None:
    source = inspect.getsource(omega_launcher)

    assert "_DT_RIGHT =" in source


def test_boot_progress_exposes_staged_feedback_for_a_slow_startup() -> None:
    early = omega_launcher.boot_progress(0.0)
    warm = omega_launcher.boot_progress(30.0)
    late = omega_launcher.boot_progress(90.0)

    assert early.phase == "starting"
    assert early.percent < warm.percent <= late.percent < 100
    assert late.phase == "warming"
    assert "still" in late.detail.casefold()
    assert "background" in late.hint.casefold()


def test_boot_progress_reports_a_failed_child_process() -> None:
    failed = omega_launcher.boot_progress(4.0, process_running=False, returncode=1)

    assert failed.phase == "failed"
    assert failed.percent == 100
    assert "failed" in failed.detail.casefold()


def test_native_boot_monitor_closes_only_after_hud_reports_ready() -> None:
    source = inspect.getsource(omega_launcher.SetupWizard)

    assert "_WM_TIMER" in source
    assert "read_boot_status(self.product)" in source
    assert "interpret_boot_status(status, elapsed)" in source
    assert 'if action == "close":' in source
    assert "self._user32.DestroyWindow(self.hwnd)" in source
    # Both wizards read boot.py's status through one interpretation.
    assert 'if phase == "ready":' in inspect.getsource(omega_launcher.interpret_boot_status)
    tk_source = inspect.getsource(omega_launcher.TkSetupWizard)
    assert "interpret_boot_status(status, elapsed)" in tk_source
    assert "read_boot_status(self.product)" in tk_source


def test_the_launcher_voice_labels_follow_runtime_tts_status(monkeypatch) -> None:
    import runtime.tts as tts_module

    monkeypatch.setattr(
        tts_module,
        "status",
        lambda: {"available": True, "engine": "a-later-engine"},
    )

    surfaces = omega_launcher.launcher_voice_surfaces()
    assert surfaces.hero_tts == "a-later-engine"
    assert surfaces.summary_tts == "a-later-engine"


def test_the_launcher_voice_labels_name_the_capability_when_tts_status_fails(monkeypatch) -> None:
    import runtime.tts as tts_module

    monkeypatch.setattr(
        tts_module,
        "status",
        lambda: (_ for _ in ()).throw(RuntimeError("runtime unreachable")),
    )

    surfaces = omega_launcher.launcher_voice_surfaces()
    assert surfaces.hero_tts == "TTS capability"
    assert surfaces.summary_tts == "TTS capability"


@pytest.mark.skipif(os.name == "nt", reason="native Linux launch branch")
def test_native_linux_runtime_starts_run_ai_from_its_own_folder(tmp_path) -> None:
    launcher = tmp_path / "LM-Studio-Linux" / "run-ai.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []
    omega_launcher.launch_linux_runtime(
        tmp_path, popen=lambda command, **kwargs: calls.append((command, kwargs))
    )

    assert calls == [
        (["sh", "-lc", omega_launcher.LINUX_STARTUP_COMMAND], {"cwd": str(launcher.parent)})
    ]


@pytest.mark.skipif(os.name == "nt", reason="native Linux launch branch")
def test_native_linux_runtime_requires_run_ai(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="run-ai.sh"):
        omega_launcher.launch_linux_runtime(tmp_path, popen=lambda *_args, **_kwargs: None)


@pytest.mark.skipif(os.name == "nt", reason="Windows hides folders; Linux has no hidden attribute")
def test_linux_never_hides_portable_folders(tmp_path) -> None:
    (tmp_path / "Omega").mkdir()
    (tmp_path / "LM-Studio-Linux").mkdir()
    assert omega_launcher.hide_portable_runtime(tmp_path / "Omega") == ()
