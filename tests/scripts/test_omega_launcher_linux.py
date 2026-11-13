"""The Linux counterpart of Omega.exe: same wizard flow, same state, same launches."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT
from scripts import omega_launcher


def _product(tmp_path: Path, name: str = "Omega3.0") -> Path:
    product = tmp_path / name
    (product / "config").mkdir(parents=True)
    (product / "config" / "portable_product.json").write_text("{}", encoding="utf-8")
    return product


def test_frozen_linux_launcher_finds_the_product_beside_it(tmp_path, monkeypatch) -> None:
    """A frozen launcher has no ``.exe`` suffix off Windows; it still sits at the SSD root."""
    product = _product(tmp_path)
    launcher = tmp_path / "Omega-Linux"
    launcher.write_bytes(b"\x7fELF")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(launcher))

    assert omega_launcher.product_root() == product.resolve()


def test_setup_wizard_toolkit_follows_the_platform() -> None:
    assert omega_launcher.setup_wizard_class("nt") is omega_launcher.SetupWizard
    assert omega_launcher.setup_wizard_class("posix") is omega_launcher.TkSetupWizard


def test_launch_password_fields_are_validated_once_for_both_wizards() -> None:
    omega_launcher.validate_launch_password_fields("", "")
    omega_launcher.validate_launch_password_fields("long-enough", "long-enough")
    with pytest.raises(ValueError, match="at least"):
        omega_launcher.validate_launch_password_fields("short", "short")
    with pytest.raises(ValueError, match="do not match"):
        omega_launcher.validate_launch_password_fields("long-enough", "different!")


def test_local_summary_names_the_model_and_the_fixed_endpoint(tmp_path) -> None:
    text = omega_launcher.setup_summary_text(
        tmp_path, {"backend": "local", "model": "meta/muse-glimmer"}, entered_key=""
    )
    assert "meta/muse-glimmer" in text
    assert omega_launcher.LOCAL_BASE_URL in text


def test_cloud_summary_reports_an_unsaved_key(tmp_path) -> None:
    provider = omega_launcher.public_cloud_providers()[0]
    form = {"backend": "cloud", "provider": provider["id"], "base_url": "", "model": "m"}
    assert "Missing" in omega_launcher.setup_summary_text(tmp_path, form, entered_key="")
    assert "Save key securely" in omega_launcher.setup_summary_text(
        tmp_path, form, entered_key="sk-typed"
    )


def test_boot_status_ready_closes_failed_stops_and_stages_continue() -> None:
    ready, action = omega_launcher.interpret_boot_status({"phase": "ready"}, 12.0)
    assert (ready.phase, action) == ("ready", "close")

    failed, action = omega_launcher.interpret_boot_status(
        {"phase": "failed", "detail": "runtime down"}, 12.0
    )
    assert (failed.phase, failed.detail, action) == ("failed", "runtime down", "stop")

    staged, action = omega_launcher.interpret_boot_status(
        {"phase": "memory", "percent": 140, "detail": "Loading memory"}, 12.0
    )
    assert (staged.phase, staged.percent, action) == ("memory", 99, "continue")

    unknown, action = omega_launcher.interpret_boot_status(None, 20.0)
    assert (unknown, action) == (omega_launcher.boot_progress(20.0), "continue")


@pytest.mark.skipif(os.name == "nt", reason="POSIX session handling")
def test_linux_boot_is_detached_from_the_launcher(tmp_path) -> None:
    """The launcher window closes once the HUD is ready; boot must not share its session."""
    root = tmp_path / "Omega"
    python = omega_launcher.portable_python_path(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"portable-python")
    (root / "boot.py").write_text("", encoding="utf-8")
    calls = []

    omega_launcher.launch_omega(root, popen=lambda command, **kwargs: calls.append(kwargs))

    assert calls[0]["start_new_session"] is True


@pytest.mark.skipif(os.name == "nt", reason="Windows uses MessageBoxW")
def test_linux_launch_errors_are_shown_in_a_dialog() -> None:
    shown = []
    omega_launcher.show_launcher_error("boom", dialog=lambda title, text: shown.append((title, text)))
    assert shown == [("Omega", "boom")]


def test_linux_launcher_build_uses_only_the_ssd_interpreter() -> None:
    script = (OMEGA_ROOT / "scripts" / "build_launcher_linux.sh").read_text(encoding="utf-8")
    assert "python-3.13-linux/bin/python3" in script
    assert "-m PyInstaller" in script
    assert "--onefile" in script
    assert "--name Omega-Linux" in script
    for hidden in (
        "runtime.cloud_providers",
        "scripts.lmstudio_updater",
        "cryptography.hazmat.primitives.ciphers.aead",
    ):
        assert f"--hidden-import {hidden}" in script
    assert "scripts/omega_launcher.py" in script
    # _tkinter finds libtcl9/libtcl9tk9 only through the interpreter's own RPATH;
    # PyInstaller must be told where they are or the frozen wizard cannot start.
    assert 'LD_LIBRARY_PATH="$SOURCE_ROOT/python-3.13-linux/lib' in script
    # The wizard loads no ML stack at run time (only requests/jsonschema/cryptography/
    # tkinter), but PyInstaller follows function-level imports into it: keep it out.
    for heavy in ("torch", "transformers", "scipy", "numpy", "spacy", "onnxruntime", "cv2"):
        assert f"--exclude-module {heavy}" in script
    # The wizard's look is drawn with Pillow; it must ship.
    assert "--exclude-module PIL" not in script
    assert "--hidden-import scripts.omega_launcher_art" in script


@pytest.mark.skipif(
    os.name == "nt" or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="needs a Linux display",
)
def test_tk_wizard_builds_every_page_on_the_real_product() -> None:
    wizard = omega_launcher.TkSetupWizard(OMEGA_ROOT)
    try:
        wizard.build()
        for page in range(8):
            wizard.show_page(page)
            wizard.root.update_idletasks()
            assert wizard.page == page
        assert wizard.state["title"] == "Starting Omega 3.0"
        # Page 3's live inputs are placed exactly where the Win32 controls sit.
        wizard.show_page(3)
        wizard.root.update_idletasks()
        assert wizard.canvas.coords(wizard._field_items["endpoint"]) == [52.0, 368.0]
        assert wizard.canvas.itemcget(wizard._field_items["endpoint"], "state") == "normal"
        assert wizard.canvas.itemcget(wizard._field_items["local_model"], "state") == "hidden"
    finally:
        wizard.root.destroy()


def test_the_wizard_fonts_ship_with_the_product() -> None:
    from scripts import omega_launcher_art as art

    art.FontSet(OMEGA_ROOT)  # raises on any missing face
    fonts = OMEGA_ROOT / "config" / "fonts"
    assert (fonts / "LICENSE-Noto.txt").is_file()
    assert (fonts / "LICENSE-DejaVu.txt").is_file()


def test_every_page_renders_at_the_native_client_size_in_the_shared_palette() -> None:
    from scripts import omega_launcher_art as art

    fonts = art.FontSet(OMEGA_ROOT)
    for page in range(8):
        frame = art.render_page(page, fonts, {"title": "t", "subtitle": "s"})
        assert frame.size == (omega_launcher.SETUP_CLIENT_WIDTH, omega_launcher.SETUP_CLIENT_HEIGHT)
        assert frame.getpixel((5, 5)) == omega_launcher.OMEGA_PALETTE["header"]
        assert frame.getpixel((5, 660)) == omega_launcher.OMEGA_PALETTE["window"]


def test_painted_buttons_are_clickable_where_the_win32_buttons_sit() -> None:
    from scripts import omega_launcher_art as art

    assert art.button_at(0, 850, 625).action == "next"
    assert art.button_at(0, 60, 625) is None  # no Back on the welcome page
    assert art.button_at(4, 700, 480).action == "boot"
    assert art.button_at(4, 60, 625).action == "back"
    assert art.button_at(7, 850, 625) is None


def test_linux_local_page_launches_the_portable_linux_lm_studio() -> None:
    from scripts import omega_launcher_art as art

    assert [button.action for button in art.PAGES[2].buttons] == ["launch_lm_studio"]
