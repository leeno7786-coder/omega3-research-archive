"""
test_hud_settings.py - HUD settings profile and theme routing

Direct script test for the HUD settings surface. It avoids TestClient so
it can run in the repo venv without pytest/httpx.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

import core.paths as _core_paths  # noqa: E402

# Plain-script runner, live tree: everything this script writes goes to a scratch directory.
# The redirect precedes the hud imports because guardian/state is resolved as soon as the app
# is composed; with it inside main() the plain-script matrix still saw the operator's live
# cognition queue rewritten (found 2026-09-15).
_SCRATCH = Path(tempfile.mkdtemp(prefix="omega-hud-settings-"))
_core_paths.GUARDIAN_STATE_ROOT = _SCRATCH / "guardian" / "state"

from core.paths import OMEGA_ROOT  # noqa: E402
from hud import settings as hud_settings  # noqa: E402
from hud.server import create_app  # noqa: E402


log = logging.getLogger("omega.tests.hud.test_hud_settings")


_PASSED = 0
_FAILED = 0
_FAILURES: list[str] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  PASS  {name}")
    else:
        _FAILED += 1
        msg = f"  FAIL  {name}" + (f" -- {detail}" if detail else "")
        _FAILURES.append(msg)
        print(msg)


def _fake_receipt(envelope):
    return SimpleNamespace(
        cycle_id=envelope.cycle_id,
        validation_status="staged",
        staging_ref="stage:operator-profile",
        path="memory/100_personas/person_stub/operator/profile.json",
        message="queued for guardian approval",
    )


def _route(app, path: str, method: str | None = None):
    for route in app.routes:
        if getattr(route, "path", "") != path:
            continue
        if method is None or method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method or '*'} {path}")


def main() -> int:
    print("=" * 60)
    print("test_hud_settings -- HUD settings profile/theme")
    print("=" * 60)
    print()

    # Onboarding is one-shot/locked; this plain-script test re-saves the profile to exercise
    # the save mechanics, so neutralize the lock here. The lock itself is covered by the
    # pytest suite (tests/hud/test_operator_profile_lock.py).
    import hud.operator_identity as _oi

    _oi.is_onboarded = lambda: False

    # clear_inputs() below used to wipe the operator's real cognition queue, the profile saves
    # enqueued a test "Noah" profile into it, and final_path_for_meta created folders under the
    # live memory tree (found 2026-09-15). Queue and memory root live in the scratch directory.
    from memory.engine import validate_write as _validate_write
    from pressure_loop import cognition_queue as _cognition_queue

    _cognition_queue._queue_path = lambda: _SCRATCH / "guardian" / "state" / "cognition_queue.json"
    _cognition_queue._reset_cache_for_tests()
    _validate_write.MEMORY_ROOT = _SCRATCH / "memory"

    funcs = hud_settings.settings_functions()
    _check("S1.settings function shelf is non-empty", len(funcs) >= 5)
    _check(
        "S1.operator profile is active",
        funcs[0].get("id") == "operator_profile" and funcs[0].get("status") == "active",
    )
    _check(
        "S1.themes function is active",
        any(
            function_record.get("id") == "themes" and function_record.get("status") == "active"
            for function_record in funcs
        ),
    )
    _check(
        "S1.voice function is active",
        any(
            function_record.get("id") == "voice" and function_record.get("status") == "active"
            for function_record in funcs
        ),
    )

    normalized = hud_settings.normalize_operator_profile(
        {
            "operator_name": "Noah",
            "bday": "Jan 1",
            "anything_else": "Likes direct, practical answers.",
            "unknown": "ignored but reported",
        }
    )
    _check(
        "S2.aliases normalize into canonical profile keys",
        normalized["profile"].get("name") == "Noah"
        and normalized["profile"].get("birthday") == "Jan 1"
        and normalized["profile"].get("notes"),
    )
    _check(
        "S2.unknown keys are reported but not persisted as fields",
        normalized["ignored_fields"] == ["unknown"],
    )
    _check(
        "S2.onboarding tags include name and anything_else",
        {"onboarding:name", "onboarding:anything_else"}.issubset(
            set(normalized["onboarding_tags"])
        ),
    )

    try:
        hud_settings.normalize_operator_profile({"name": "  "})
        empty_rejected = False
    except ValueError:
        empty_rejected = True
    _check("S2.empty profile payload is rejected", empty_rejected)

    envelope, built = hud_settings.build_operator_profile_envelope(
        {
            "name": "Noah",
            "preferred_name": "Lee",
            "birthday": "Jan 1",
            "role": "operator",
        }
    )
    payload = envelope.payload
    enrichment = payload.get("enrichment", {}).get("operator_profile", {})
    _check(
        "S3.envelope routes to persona/operator lobe",
        envelope.coord == [1, 0, 0]
        and payload.get("domain") == "operator"
        and payload.get("subtopic") == "operator_profile",
    )
    from memory.engine.validate_write import final_path_for_meta

    operator_profile_path = final_path_for_meta(
        {
            "id": "user_profile.settings.test",
            "coord": envelope.coord,
            "domain": payload.get("domain"),
            "subtopic": payload.get("subtopic"),
        }
    )
    _check(
        "S3.operator profile writes under operator/type memory",
        operator_profile_path.parent.name == "operator_profile"
        and operator_profile_path.parent.parent.name == "operator"
        and operator_profile_path.parent.parent.parent.name == "100_personas",
        str(operator_profile_path),
    )
    person_stub_path = final_path_for_meta(
        {
            "id": "person.profile.test",
            "coord": [1, 1, 0],
            "domain": "alice-smith",
        }
    )
    _check(
        "S3.person stubs keep per-person memory subdirectory",
        person_stub_path.parent.name == "alice-smith"
        and person_stub_path.parent.parent.name == "100_personas",
        str(person_stub_path),
    )
    _check(
        "S3.envelope preserves structured profile enrichment",
        enrichment.get("person_id") == "operator"
        and enrichment.get("fields", {}).get("preferred_name") == "Lee",
    )
    _check(
        "S3.envelope carries completeness tags",
        {"onboarding:name", "onboarding:preferred_name", "onboarding:relationship"}.issubset(
            set(payload.get("tags", []))
        ),
    )
    _check(
        "S3.envelope text includes personal fields",
        "birthday: Jan 1" in payload.get("text", "")
        and built["accepted_fields"] == ["name", "preferred_name", "birthday", "relationship"],
    )

    from pressure_loop import cognition_queue

    cognition_queue.clear_inputs()
    try:
        result = hud_settings.save_operator_profile({"name": "Noah", "tone": "warm"})
    finally:
        cognition_queue.clear_inputs()
    _check(
        "S4.save_operator_profile queues screened memory intake",
        result.get("ok") is True and result.get("memory", {}).get("status") == "queued",
    )
    _check(
        "S4.save_operator_profile returns queued profile fields",
        result.get("ok") is True
        and result.get("target", "").endswith("operator"),
    )

    theme = hud_settings.normalize_hud_theme_settings(
        {
            "theme": "paper",
            "density": "compact",
            "motion": "reduced",
            "glow": "quiet",
            "panel_depth": "flat",
            "unknown": "ignored",
        }
    )
    _check(
        "S5.theme settings normalize valid choices",
        theme["settings"].get("theme") == "paper" and theme["settings"].get("density") == "compact",
    )
    _check("S5.theme settings report ignored fields", theme["ignored_fields"] == ["unknown"])
    try:
        hud_settings.normalize_hud_theme_settings({"theme": "laser_rainbow"})
        invalid_theme_rejected = False
    except ValueError:
        invalid_theme_rejected = True
    _check("S5.invalid theme choice is rejected", invalid_theme_rejected)

    with tempfile.TemporaryDirectory() as temp_dir:
        old_theme_path = hud_settings._HUD_THEME_SETTINGS_PATH
        old_voice_path = hud_settings._HUD_VOICE_SETTINGS_PATH
        hud_settings._HUD_THEME_SETTINGS_PATH = Path(temp_dir) / "theme_settings.json"
        hud_settings._HUD_VOICE_SETTINGS_PATH = Path(temp_dir) / "voice_settings.json"
        try:
            saved_theme = hud_settings.save_hud_theme_settings(
                {
                    "theme": "night_garden",
                    "density": "spacious",
                    "motion": "full",
                    "glow": "vivid",
                    "panel_depth": "floating",
                }
            )
            loaded_theme = hud_settings.load_hud_theme_settings()
            _check(
                "S5.theme settings persist to Guardian state path",
                saved_theme.get("ok") is True
                and hud_settings._HUD_THEME_SETTINGS_PATH.exists()
                and loaded_theme.get("settings", {}).get("theme") == "night_garden",
            )

            app = create_app()
            route_paths = {getattr(route, "path", "") for route in app.routes}
            _check(
                "S6.settings functions route registered", "/api/settings/functions" in route_paths
            )
            _check(
                "S6.operator profile route registered",
                "/api/settings/operator-profile" in route_paths,
            )
            _check("S6.hud theme routes registered", "/api/settings/hud-theme" in route_paths)

            cognition_queue.clear_inputs()
            try:
                endpoint = _route(app, "/api/settings/operator-profile")
                response = asyncio.run(endpoint({"name": "Noah", "occupation": "Omega HUD"}))
                body = json.loads(response.body.decode("utf-8"))
                route_ok = body.get("ok") is True and "occupation" in body.get(
                    "accepted_fields", []
                )
            finally:
                cognition_queue.clear_inputs()
            _check("S6.operator profile endpoint returns success JSON", route_ok)

            try:
                asyncio.run(endpoint({}))
                rejected_empty = False
            except HTTPException as exc:
                rejected_empty = exc.status_code == 400
            _check("S6.operator profile endpoint rejects empty payload", rejected_empty)

            theme_body = json.loads(
                asyncio.run(_route(app, "/api/settings/hud-theme", "GET")()).body.decode("utf-8")
            )
            _check(
                "S6.theme GET returns settings and options",
                theme_body.get("settings", {}).get("theme") == "night_garden"
                and isinstance(theme_body.get("options", {}).get("theme"), list),
            )

            theme_post = _route(app, "/api/settings/hud-theme", "POST")
            theme_response = asyncio.run(
                theme_post({"theme": "high_contrast", "density": "compact"})
            )
            theme_saved = json.loads(theme_response.body.decode("utf-8"))
            _check(
                "S6.theme POST saves operator HUD preferences",
                theme_saved.get("settings", {}).get("theme") == "high_contrast"
                and theme_saved.get("settings", {}).get("density") == "compact",
            )
            try:
                asyncio.run(theme_post({"glow": "blinding"}))
                invalid_route_rejected = False
            except HTTPException as exc:
                invalid_route_rejected = exc.status_code == 400
            _check("S6.theme POST rejects invalid choices", invalid_route_rejected)

            # Voice settings tests
            voice = hud_settings.normalize_voice_settings(
                {
                    "tts_enabled": True,
                    "tts_voice": "Aiden",
                    "tts_speed": 1.0,
                    "tts_pitch": 1.0,
                    "stt_enabled": True,
                    "stt_language": "auto",
                    "stt_task": "transcribe",
                    "stt_model": "parakeet_tdt_06b_v3",
                    "wake_word_enabled": False,
                    "wake_word": "hey omega",
                    "auto_speak": True,
                    "voice_activity_detection": True,
                    "noise_cancellation": True,
                }
            )
            _check(
                "S6.voice settings normalize valid choices",
                voice["settings"].get("tts_voice") == "Aiden"
                and voice["settings"].get("stt_language") == "auto"
                and voice["settings"].get("stt_task") == "transcribe",
            )
            _check(
                "S6.voice settings include native Qwen option",
                "Ryan" in [opt["id"] for opt in hud_settings.HUD_VOICE_OPTIONS["tts_voice"]],
            )
            _check(
                "S6.voice defaults use native Qwen voice",
                hud_settings.HUD_VOICE_DEFAULTS.get("tts_voice") == "Aiden",
            )
            try:
                hud_settings.normalize_voice_settings({"tts_voice": "jarvis"})
                retired_voice_rejected = False
            except ValueError:
                retired_voice_rejected = True
            _check(
                "S6.retired voice aliases are rejected",
                retired_voice_rejected,
            )
            try:
                hud_settings.normalize_voice_settings({"tts_voice": "unknown_voice"})
                invalid_voice_rejected = False
            except ValueError:
                invalid_voice_rejected = True
            _check("S6.invalid voice choice is rejected", invalid_voice_rejected)

            saved_voice = hud_settings.save_voice_settings(
                {
                    "tts_voice": "Uncle_Fu",
                    "tts_speed": 1.15,
                }
            )
            loaded_voice = hud_settings.load_voice_settings()
            _check(
                "S6.voice settings persist to Guardian state path",
                saved_voice.get("ok") is True
                and hud_settings._HUD_VOICE_SETTINGS_PATH.exists()
                and loaded_voice.get("settings", {}).get("tts_voice") == "Uncle_Fu",
            )

            _check("S6.voice routes registered", "/api/settings/voice" in route_paths)

            voice_get = json.loads(
                asyncio.run(_route(app, "/api/settings/voice", "GET")()).body.decode("utf-8")
            )
            _check(
                "S6.voice GET returns settings and options",
                voice_get.get("settings", {}).get("tts_voice") == "Uncle_Fu"
                and isinstance(voice_get.get("options", {}).get("tts_voice"), list),
            )

            voice_post = _route(app, "/api/settings/voice", "POST")
            voice_response = asyncio.run(
                voice_post(
                    {
                        "tts_voice": "Dylan",
                        "stt_language": "es",
                        "stt_task": "translate",
                    }
                )
            )
            voice_saved = json.loads(voice_response.body.decode("utf-8"))
            _check(
                "S6.voice POST saves operator voice preferences",
                voice_saved.get("settings", {}).get("tts_voice") == "Dylan"
                and voice_saved.get("settings", {}).get("stt_language") == "es"
                and voice_saved.get("settings", {}).get("stt_task") == "translate",
            )

        finally:
            hud_settings._HUD_THEME_SETTINGS_PATH = old_theme_path
            hud_settings._HUD_VOICE_SETTINGS_PATH = old_voice_path

    html = (OMEGA_ROOT / "hud" / "omega_interface.html").read_text(encoding="utf-8")
    _check("S7.html has settings pill", 'data-panel="settings"' in html)
    _check(
        "S7.html wires panelSettings and profile endpoint",
        "function panelSettings" in html and "/api/settings/operator-profile" in html,
    )
    _check(
        "S7.html includes operator profile fields",
        all(marker in html for marker in ("op-name", "op-birthday", "op-occupation", "op-notes")),
    )
    _check(
        "S7.html includes HUD theme controls",
        all(
            marker in html
            for marker in (
                "hud-theme-form",
                "/api/settings/hud-theme",
                "night_garden",
                "high_contrast",
                "applyHudThemeSettings",
            )
        ),
    )
    _check(
        "S7.html includes voice settings form",
        all(
            marker in html
            for marker in (
                "hud-voice-form",
                "/api/settings/voice",
                "Aiden",
                "tts_voice",
                "stt_language",
                "stt_task",
                "parakeet_tdt_06b_v3",
                "applyVoiceSettings",
            )
        ),
    )
    _check(
        "S7.html includes new theme options",
        all(
            marker in html
            for marker in ("ocean_deep", "sunset_amber", "arctic_mist", "midnight_violet")
        ),
    )
    _check(
        "S7.html includes face memory enrollment under operator profile",
        all(
            marker in html
            for marker in (
                "operator-face-memory-card",
                "/api/vision/upload-face",
                "operator-face-memory-upload",
                "save face memory",
            )
        ),
    )
    _check(
        "S7.html wires chat webcam to face memory flyout",
        "openFaceMemoryFromWebcam" in html and "face-memory-flyout" in html,
    )
    _check(
        "S7.html removed legacy OmegaEnrollment overlay",
        "OmegaEnrollment" not in html and "op-enroll-prompt" not in html,
    )
    _check(
        "S7.html includes first-boot operator profile onboarding chat",
        "appendFirstBootOperatorProfileChat" in html and "operator profile" in html,
    )
    _check(
        "S7.html onboarding is gated on operator profile only (no localStorage latch)",
        "FIRST_BOOT_CHAT_KEY" not in html and "firstBootOperatorProfileChat" not in html,
    )
    _check(
        "S7.html removed standalone face-identify settings panel",
        "operator-face-identify" not in html and "face-identify-file" not in html,
    )

    print()
    print(f"PASSED: {_PASSED}")
    print(f"FAILED: {_FAILED}")
    if _FAILURES:
        print()
        print("Failures:")
        for failure in _FAILURES:
            print(failure)
    return 0 if _FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
