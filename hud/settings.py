"""HUD settings command helpers.

The HUD remains a thin command layer: settings actions normalize the
operator payload, then hand durable writes to memory.api/Guardian.
"""

from __future__ import annotations

import logging
import json
import math
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Tuple

from core.paths import GUARDIAN_STATE_ROOT

log = logging.getLogger("omega.hud.settings")


def _emit_fail(
    operation: str, *, status: str = "fail", failure_class: str, summary: str, **fields: Any
) -> None:
    """Remote-diagnosable failure record for HUD settings persistence.
    Metadata only — never the setting values. Lazy-import; its own
    failure handler is log.debug only (no recursion)."""
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "hud",
            operation,
            status=status,
            summary=summary,
            component="hud.settings",
            failure_class=failure_class,
            **fields,
        )
    except Exception as _e:  # noqa: BLE001 - never recurse into emit
        log.debug("settings emit_phase failed (%s): %s", operation, _e)


_HUD_THEME_SETTINGS_PATH = GUARDIAN_STATE_ROOT / "hud" / "theme_settings.json"

HUD_THEME_DEFAULTS: Dict[str, str] = {
    "theme": "industrial",
    "density": "comfortable",
    "motion": "full",
    "glow": "balanced",
    "panel_depth": "standard",
}

HUD_THEME_OPTIONS: Dict[str, Tuple[Dict[str, str], ...]] = {
    "theme": (
        {"id": "industrial", "label": "industrial", "detail": "ink, bone, copper"},
        {"id": "paper", "label": "paper", "detail": "light desk surface"},
        {"id": "night_garden", "label": "night garden", "detail": "deep green and teal"},
        {"id": "high_contrast", "label": "high contrast", "detail": "black, white, signal gold"},
        {
            "id": "ocean_deep",
            "label": "ocean deep",
            "detail": "navy depths with bioluminescent cyan",
        },
        {"id": "sunset_amber", "label": "sunset amber", "detail": "warm amber and dusky rose"},
        {
            "id": "arctic_mist",
            "label": "arctic mist",
            "detail": "cool blue-white with silver frost",
        },
        {
            "id": "midnight_violet",
            "label": "midnight violet",
            "detail": "deep purple with electric accents",
        },
    ),
    "density": (
        {"id": "comfortable", "label": "comfortable", "detail": "current spacing"},
        {"id": "compact", "label": "compact", "detail": "denser repeated work"},
        {"id": "spacious", "label": "spacious", "detail": "more room to breathe"},
    ),
    "motion": (
        {"id": "full", "label": "full", "detail": "normal animation"},
        {"id": "reduced", "label": "reduced", "detail": "minimal motion"},
    ),
    "glow": (
        {"id": "balanced", "label": "balanced", "detail": "default atmosphere"},
        {"id": "quiet", "label": "quiet", "detail": "lower bloom"},
        {"id": "vivid", "label": "vivid", "detail": "stronger presence"},
    ),
    "panel_depth": (
        {"id": "standard", "label": "standard", "detail": "blurred depth reveal"},
        {"id": "flat", "label": "flat", "detail": "less overlay depth"},
        {"id": "floating", "label": "floating", "detail": "stronger framed panel"},
    ),
}


@dataclass(frozen=True)
class OperatorProfileField:
    key: str
    label: str
    aliases: Tuple[str, ...]
    max_length: int
    onboarding_key: str = ""


OPERATOR_PROFILE_FIELDS: Tuple[OperatorProfileField, ...] = (
    OperatorProfileField("name", "name", ("name", "operator_name"), 80, "name"),
    OperatorProfileField(
        "preferred_name",
        "preferred name",
        ("preferred_name", "preferredName", "nickname"),
        80,
        "preferred_name",
    ),
    OperatorProfileField(
        "birthday", "birthday", ("birthday", "birthdate", "birth_date", "bday"), 40
    ),
    OperatorProfileField("pronouns", "pronouns", ("pronouns",), 40),
    OperatorProfileField("location", "location", ("location", "home", "city"), 120),
    OperatorProfileField("timezone", "timezone", ("timezone", "time_zone"), 80),
    OperatorProfileField(
        "occupation", "occupation", ("occupation", "work", "job", "profession"), 160, "occupation"
    ),
    OperatorProfileField(
        "relationship", "relationship", ("relationship", "role", "relation"), 120, "relationship"
    ),
    OperatorProfileField(
        "notes", "notes", ("notes", "anything_else", "extra"), 1200, "anything_else"
    ),
    OperatorProfileField(
        "picture_path",
        "picture path",
        ("picture_path", "profile_picture", "avatar"),
        500,
        "picture_path",
    ),
)


def settings_functions() -> List[Dict[str, str]]:
    """Return the HUD settings function shelf.

    Operator profile, HUD themes, and voice & speech are active.
    Reserved entries make the panel an extensible settings surface
    without implying that those settings are already wired.
    """
    return [
        {
            "id": "operator_profile",
            "label": "operator profile",
            "status": "active",
            "target": "memory/100_personas/operator",
        },
        {
            "id": "themes",
            "label": "themes",
            "status": "active",
            "target": "guardian/state/hud/theme_settings.json",
        },
        {
            "id": "voice",
            "label": "voice & speech",
            "status": "active",
            "target": "guardian/state/hud/voice_settings.json",
        },
        {
            "id": "workspace",
            "label": "workspace access",
            "status": "active",
            "target": "guardian/state/trusted_folders.json",
        },
        {
            "id": "terminal",
            "label": "terminal",
            "status": "active",
            "target": "guardian/state/mind_backend.json",
        },
        {"id": "memory", "label": "memory", "status": "reserved", "target": "recall posture prefs"},
    ]


# The backend-neutral mind connector owns provider selection, endpoint, model, and
# encrypted credentials. Live chat is the sole agent surface; runtime owns modalities.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, *, max_length: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    cleaned = " ".join(value.replace("\r", "\n").split())
    return cleaned[:max_length].strip()


def hud_theme_options() -> Dict[str, Any]:
    return {
        "defaults": dict(HUD_THEME_DEFAULTS),
        "options": {
            key: [dict(item) for item in values] for key, values in HUD_THEME_OPTIONS.items()
        },
    }


def _allowed_theme_value(key: str, value: str) -> bool:
    return value in {item["id"] for item in HUD_THEME_OPTIONS[key]}


def normalize_hud_theme_settings(
    payload: Dict[str, Any],
    *,
    base: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """Validate and normalize HUD appearance settings."""
    if not isinstance(payload, dict):
        raise ValueError("HUD theme payload must be an object")

    settings = dict(HUD_THEME_DEFAULTS)
    if base:
        for key, value in base.items():
            if key in settings and isinstance(value, str) and _allowed_theme_value(key, value):
                settings[key] = value

    accepted: List[str] = []
    for key in HUD_THEME_DEFAULTS:
        if key not in payload:
            continue
        value = _clean_text(payload.get(key), max_length=80)
        if not value:
            continue
        if not _allowed_theme_value(key, value):
            allowed = ", ".join(item["id"] for item in HUD_THEME_OPTIONS[key])
            raise ValueError(f"invalid {key}: {value}; allowed: {allowed}")
        settings[key] = value
        accepted.append(key)

    ignored = sorted(
        key
        for key, value in payload.items()
        if key not in HUD_THEME_DEFAULTS and value not in (None, "")
    )
    return {
        "settings": settings,
        "accepted_fields": accepted,
        "ignored_fields": ignored,
    }


def load_hud_theme_settings() -> Dict[str, Any]:
    """Load persisted HUD theme settings, falling back to defaults.

    ``stored`` says whether a settings FILE exists, which is not the same question as
    what the settings are: after a factory reset (``scripts/day_zero_reset.py`` clears
    guardian/state) there is no file, and the HUD client uses that to drop the previous
    operator's cached appearance from the browser — state no wipe on the SSD can reach.
    A file that exists but cannot be read still counts as stored: the operator's prefs
    are already reported degraded, and a client cache may hold the only copy left.
    """
    stored = True
    try:
        raw = json.loads(_HUD_THEME_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("theme settings root is not an object")
        normalized = normalize_hud_theme_settings(raw)
        settings = normalized["settings"]
    except FileNotFoundError:
        stored = False
        settings = dict(HUD_THEME_DEFAULTS)
    except Exception as exc:
        log.warning("failed to load HUD theme settings; using defaults: %s", exc)
        _emit_fail(
            "hud_theme_load",
            status="degraded",
            failure_class="theme_settings_corrupt",
            summary="HUD theme settings unreadable — reverted to defaults, operator prefs lost",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        settings = dict(HUD_THEME_DEFAULTS)
    return {
        "ok": True,
        "stored": stored,
        "settings": settings,
        "path": str(_HUD_THEME_SETTINGS_PATH),
        **hud_theme_options(),
    }


def _write_hud_theme_settings(settings: Dict[str, str]) -> None:
    _HUD_THEME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _HUD_THEME_SETTINGS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(_HUD_THEME_SETTINGS_PATH)


def save_hud_theme_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist HUD appearance settings under Guardian state."""
    current = load_hud_theme_settings()["settings"]
    normalized = normalize_hud_theme_settings(payload, base=current)
    settings = normalized["settings"]
    _write_hud_theme_settings(settings)
    log.info(
        "settings HUD theme saved fields=%s theme=%s",
        normalized["accepted_fields"],
        settings.get("theme"),
    )
    return {
        "ok": True,
        "settings": settings,
        "accepted_fields": normalized["accepted_fields"],
        "ignored_fields": normalized["ignored_fields"],
        "path": str(_HUD_THEME_SETTINGS_PATH),
        **hud_theme_options(),
    }


# =================================================================
# Voice & Speech settings
# =================================================================

_HUD_VOICE_SETTINGS_PATH = GUARDIAN_STATE_ROOT / "hud" / "voice_settings.json"
_VOICE_SETTINGS_LOCK = threading.RLock()

HUD_VOICE_DEFAULTS: Dict[str, Any] = {
    "tts_enabled": True,
    "tts_voice": "Aiden",
    "tts_speed": 1.0,
    "tts_pitch": 1.0,
    "stt_enabled": True,
    "stt_language": "auto",
    "stt_task": "transcribe",
    # The shipped speech recognizer is Parakeet served by CrispASR.
    "stt_model": "parakeet_tdt_06b_v3",
    "wake_word_enabled": False,
    "wake_word": "hey omega",
    "auto_speak": True,
    "voice_activity_detection": True,
    "noise_cancellation": True,
    # Live conversation (2026-09-05): continuous listening with endpointing per
    # utterance, sentence-streamed Qwen, and barge-in. Off = the fixed-window
    # capture that preceded it.
    "live_mode": True,
}

# Upgrade-only migration from the removed Kokoro roster. These IDs are never
# offered to operators and never select a Kokoro engine; they are converted to
# the closest Qwen CustomVoice speaker when an existing settings file is read.
LEGACY_TTS_VOICE_MIGRATIONS: Dict[str, str] = {
    "af_bella": "Aiden",
    "af_alloy": "Aiden",
    "af_nova": "Aiden",
    "af_sky": "Aiden",
    "am_adam": "Ryan",
    "am_echo": "Ryan",
    "am_onyx": "Ryan",
    "bf_alice": "Aiden",
    "bm_fable": "Ryan",
    "jarvis": "Ryan",
    "alloy": "Aiden",
    "echo": "Ryan",
    "fable": "Ryan",
    "onyx": "Ryan",
    "nova": "Aiden",
    "shimmer": "Aiden",
}

HUD_VOICE_OPTIONS: Dict[str, Tuple[Dict[str, str], ...]] = {
    "tts_voice": (
        {"id": "Aiden", "label": "Aiden", "detail": "bright American English"},
        {"id": "Ryan", "label": "Ryan", "detail": "dynamic English"},
        {"id": "Vivian", "label": "Vivian", "detail": "bright Chinese"},
        {"id": "Serena", "label": "Serena", "detail": "gentle Chinese"},
        {"id": "Uncle_Fu", "label": "Uncle Fu", "detail": "mellow Chinese"},
        {"id": "Dylan", "label": "Dylan", "detail": "Beijing Chinese"},
        {"id": "Eric", "label": "Eric", "detail": "Sichuan Chinese"},
        {"id": "Ono_Anna", "label": "Ono Anna", "detail": "playful Japanese"},
        {"id": "Sohee", "label": "Sohee", "detail": "warm Korean"},
    ),
    "stt_language": (
        {"id": "auto", "label": "auto-detect", "detail": "automatic language detection"},
        {"id": "en", "label": "english", "detail": "english"},
        {"id": "es", "label": "spanish", "detail": "espanol"},
        {"id": "fr", "label": "french", "detail": "francais"},
        {"id": "de", "label": "german", "detail": "deutsch"},
        {"id": "ja", "label": "japanese", "detail": "nihongo"},
        {"id": "zh", "label": "chinese", "detail": "zhongwen"},
        {"id": "ko", "label": "korean", "detail": "hangugeo"},
        {"id": "pt", "label": "portuguese", "detail": "portugues"},
        {"id": "it", "label": "italian", "detail": "italiano"},
        {"id": "hi", "label": "hindi", "detail": "hindi"},
    ),
    "stt_task": (
        {"id": "transcribe", "label": "transcribe", "detail": "original language"},
        {"id": "translate", "label": "translate", "detail": "translate to english"},
    ),
    "stt_model": (
        {
            "id": "parakeet_tdt_06b_v3",
            "label": "parakeet-tdt-0.6b-v3",
            "detail": "CrispASR portable speech recognition",
        },
    ),
    "tts_speed": (
        {"id": "0.75", "label": "0.75x", "detail": "slow"},
        {"id": "0.9", "label": "0.9x", "detail": "relaxed"},
        {"id": "1.0", "label": "1.0x", "detail": "normal"},
        {"id": "1.15", "label": "1.15x", "detail": "brisk"},
        {"id": "1.3", "label": "1.3x", "detail": "fast"},
    ),
    "tts_pitch": (
        {"id": "0.85", "label": "0.85x", "detail": "lower"},
        {"id": "0.95", "label": "0.95x", "detail": "slightly lower"},
        {"id": "1.0", "label": "1.0x", "detail": "natural"},
        {"id": "1.05", "label": "1.05x", "detail": "slightly higher"},
        {"id": "1.15", "label": "1.15x", "detail": "higher"},
    ),
}


def hud_voice_options() -> Dict[str, Any]:
    """Return voice settings defaults and allowed options."""
    return {
        "defaults": dict(HUD_VOICE_DEFAULTS),
        "options": {
            key: [dict(item) for item in values] for key, values in HUD_VOICE_OPTIONS.items()
        },
    }


def _allowed_voice_value(key: str, value: Any) -> bool:
    if key not in HUD_VOICE_OPTIONS:
        return True  # boolean / numeric fields — accept any sane value
    allowed_ids = {item["id"] for item in HUD_VOICE_OPTIONS[key]}
    return str(value) in allowed_ids


def _coerce_voice_bool(key: str, raw: Any) -> bool:
    """Parse explicit JSON/form booleans without treating "false" as truthy."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if math.isfinite(float(raw)) and float(raw) in (0.0, 1.0):
            return bool(raw)
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned in {"true", "1", "on", "yes"}:
            return True
        if cleaned in {"false", "0", "off", "no"}:
            return False
    raise ValueError(f"invalid {key}: expected true or false")


def normalize_voice_settings(
    payload: Dict[str, Any],
    *,
    base: Dict[str, Any] | None = None,
    strict: bool = True,
) -> Dict[str, Any]:
    """Validate and normalize voice & speech settings."""
    if not isinstance(payload, dict):
        raise ValueError("voice settings payload must be an object")

    settings: Dict[str, Any] = dict(HUD_VOICE_DEFAULTS)
    if base:
        for key, value in base.items():
            if key in settings:
                settings[key] = value

    accepted: List[str] = []
    migrated_fields: List[str] = []
    for key in HUD_VOICE_DEFAULTS:
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw is None:
            continue

        # Boolean fields
        if isinstance(settings[key], bool):
            try:
                settings[key] = _coerce_voice_bool(key, raw)
            except ValueError:
                if strict:
                    raise
                continue
            accepted.append(key)
            continue

        # Numeric fields (speed, pitch)
        if isinstance(settings[key], (int, float)):
            try:
                num = float(raw)
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"invalid {key}: {raw}") from None
                continue
            if not math.isfinite(num) or not _allowed_voice_value(key, num):
                if not strict:
                    continue
                allowed = ", ".join(item["id"] for item in HUD_VOICE_OPTIONS[key])
                raise ValueError(f"invalid {key}: {raw}; allowed: {allowed}")
            settings[key] = num
            accepted.append(key)
            continue

        # String / enum fields
        cleaned = _clean_text(raw, max_length=80)
        if not cleaned:
            continue
        if key == "tts_voice":
            migrated = LEGACY_TTS_VOICE_MIGRATIONS.get(cleaned.casefold())
            if migrated is not None:
                cleaned = migrated
                migrated_fields.append(key)
        if key in HUD_VOICE_OPTIONS and not _allowed_voice_value(key, cleaned):
            if not strict:
                continue
            allowed = ", ".join(item["id"] for item in HUD_VOICE_OPTIONS[key])
            raise ValueError(f"invalid {key}: {cleaned}; allowed: {allowed}")
        settings[key] = cleaned
        accepted.append(key)

    ignored = sorted(
        key
        for key, value in payload.items()
        if key not in HUD_VOICE_DEFAULTS and value not in (None, "")
    )
    return {
        "settings": settings,
        "accepted_fields": accepted,
        "migrated_fields": migrated_fields,
        "ignored_fields": ignored,
    }


def load_voice_settings() -> Dict[str, Any]:
    """Load persisted voice settings, falling back to defaults.

    ``stored`` carries the same meaning as in ``load_hud_theme_settings``.
    """
    stored = True
    with _VOICE_SETTINGS_LOCK:
        try:
            raw = json.loads(_HUD_VOICE_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("voice settings root is not an object")
            # Existing installations could contain older free-range numeric
            # values. Default only invalid fields during load; new saves remain
            # strict so one legacy field cannot erase every valid preference.
            normalized = normalize_voice_settings(raw, strict=False)
            settings = normalized["settings"]
            migrated_fields = normalized["migrated_fields"]
        except FileNotFoundError:
            stored = False
            settings = dict(HUD_VOICE_DEFAULTS)
            migrated_fields = []
        except Exception as exc:
            log.warning("failed to load voice settings; using defaults: %s", exc)
            _emit_fail(
                "voice_settings_load",
                status="degraded",
                failure_class="voice_settings_corrupt",
                summary="voice settings unreadable — reverted to defaults",
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )
            settings = dict(HUD_VOICE_DEFAULTS)
            migrated_fields = []
        if migrated_fields:
            _write_voice_settings(settings)
            log.info("migrated legacy voice settings to Qwen: %s", migrated_fields)
    return {
        "ok": True,
        "stored": stored,
        "settings": settings,
        "migrated_fields": migrated_fields,
        "path": str(_HUD_VOICE_SETTINGS_PATH),
        **hud_voice_options(),
    }


def _write_voice_settings(settings: Dict[str, Any]) -> None:
    _HUD_VOICE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _HUD_VOICE_SETTINGS_PATH.with_name(
        f".{_HUD_VOICE_SETTINGS_PATH.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp_path.write_text(
            json.dumps(settings, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(_HUD_VOICE_SETTINGS_PATH)
    finally:
        tmp_path.unlink(missing_ok=True)


def save_voice_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist voice & speech settings under Guardian state."""
    with _VOICE_SETTINGS_LOCK:
        current = load_voice_settings()["settings"]
        normalized = normalize_voice_settings(payload, base=current)
        settings = normalized["settings"]
        _write_voice_settings(settings)
    log.info(
        "settings voice saved fields=%s tts_voice=%s",
        normalized["accepted_fields"],
        settings.get("tts_voice"),
    )
    return {
        "ok": True,
        "settings": settings,
        "accepted_fields": normalized["accepted_fields"],
        "migrated_fields": normalized["migrated_fields"],
        "ignored_fields": normalized["ignored_fields"],
        "path": str(_HUD_VOICE_SETTINGS_PATH),
        **hud_voice_options(),
    }


def normalize_operator_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a settings operator-profile payload."""
    if not isinstance(payload, dict):
        raise ValueError("operator profile payload must be an object")

    known_keys = {alias for field in OPERATOR_PROFILE_FIELDS for alias in field.aliases}
    known_keys.update({field.key for field in OPERATOR_PROFILE_FIELDS})
    known_keys.update({"cycle_id", "subcategories", "subcategory", "subtopics"})

    profile: Dict[str, str] = {}
    accepted: List[str] = []
    onboarding_tags: List[str] = []

    for field in OPERATOR_PROFILE_FIELDS:
        raw = None
        for alias in field.aliases:
            if alias in payload:
                raw = payload.get(alias)
                break
        cleaned = _clean_text(raw, max_length=field.max_length)
        if not cleaned:
            continue
        profile[field.key] = cleaned
        accepted.append(field.key)
        if field.onboarding_key:
            onboarding_tags.append(f"onboarding:{field.onboarding_key}")

    if not profile:
        raise ValueError("at least one operator profile field is required")

    ignored = sorted(
        key for key, value in payload.items() if key not in known_keys and value not in (None, "")
    )

    return {
        "profile": profile,
        "accepted_fields": accepted,
        "ignored_fields": ignored,
        "onboarding_tags": list(dict.fromkeys(onboarding_tags)),
    }


def _profile_lines(profile: Dict[str, str]) -> List[str]:
    labels = {field.key: field.label for field in OPERATOR_PROFILE_FIELDS}
    return [f"- {labels.get(key, key)}: {value}" for key, value in profile.items()]


def _source_citation_fields(
    source_citation: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    keys = (
        "source_id",
        "source_type",
        "span_start",
        "span_end",
        "span_hash",
        "source_content_hash",
        "source_session_id",
        "source_session_timestamp",
    )
    return {
        key: source_citation[key]
        for key in keys
        if source_citation and source_citation.get(key) is not None
    }


def build_operator_profile_envelope(
    payload: Dict[str, Any],
    *,
    source_citation: Mapping[str, Any] | None = None,
):
    """Build the CanonicalEnvelope for an operator settings write."""
    from memory.api import CanonicalEnvelope

    normalized = normalize_operator_profile(payload)
    profile = normalized["profile"]
    accepted = normalized["accepted_fields"]
    onboarding_tags = normalized["onboarding_tags"]
    now = _now_iso()
    cycle_id = (
        _clean_text(payload.get("cycle_id"), max_length=120)
        or f"hud-settings-{uuid.uuid4().hex[:8]}"
    )
    display_name = profile.get("preferred_name") or profile.get("name") or "operator"
    lines = _profile_lines(profile)
    text = "Operator profile settings update for {}.\n{}".format(
        display_name,
        "\n".join(lines),
    )
    summary_bits = []
    if profile.get("name"):
        summary_bits.append(f"name={profile['name']}")
    if profile.get("preferred_name"):
        summary_bits.append(f"preferred={profile['preferred_name']}")
    if profile.get("birthday"):
        summary_bits.append("birthday set")
    if profile.get("occupation"):
        summary_bits.append(f"occupation={profile['occupation'][:48]}")
    summary = "; ".join(summary_bits) or f"{len(accepted)} operator profile fields set"

    tags = list(
        dict.fromkeys(
            [
                "hud_settings",
                "operator_profile",
                "user_profile",
                "operator_identity",
                "persona",
                *onboarding_tags,
                *[f"profile:{field}" for field in accepted],
            ]
        )
    )

    envelope = CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=[1, 0, 0],
        type="user_profile",
        payload={
            "title": f"Operator profile settings: {display_name}",
            "summary": summary[:240],
            "text": text[:4000],
            "tags": tags,
            "domain": "operator",
            "subtopic": "operator_profile",
            "private": True,
            "enrichment": {
                "operator_profile": {
                    "person_id": "operator",
                    "profile_owner": "operator",
                    "fields": profile,
                    "accepted_fields": accepted,
                    "updated_at": now,
                    "source": "hud.settings",
                },
            },
            **_source_citation_fields(source_citation),
        },
        source=f"hud.settings:{now}",
        confidence=0.95,
        provenance=[
            "hud.settings.operator_profile",
            *(
                [f"source_id:{source_citation['source_id']}"]
                if source_citation and source_citation.get("source_id")
                else []
            ),
        ],
        id=f"user_profile.settings.{uuid.uuid4().hex[:12]}",
        timestamp_created=now,
        version=1,
        strength_index=0.75,
    )
    return envelope, normalized


def build_operator_name_anchor_envelope(
    name: str,
    *,
    source_citation: Mapping[str, Any] | None = None,
):
    """A prominent, RECALLABLE anchor for the operator's name — placed above all
    (strength 1.0, NON-private) so "who is <name>" surfaces it first, and it
    bridges <name> == the operator so relational memories ("the operator's son")
    resolve to <name>. The full profile blob stays private; this is the public
    who-is-the-operator fact."""
    from memory.api import CanonicalEnvelope

    now = _now_iso()
    return CanonicalEnvelope(
        cycle_id=f"operator-name-anchor-{uuid.uuid4().hex[:8]}",
        coord=[1, 0, 0],
        type="identity_anchor",
        payload={
            "title": "Operator name",
            "summary": f"The operator is {name}.",
            "text": (
                f"The operator I work with is {name}. {name} is my operator — "
                f"when a memory refers to 'the operator' (for example 'the "
                f"operator's son'), it means {name}."
            ),
            "tags": ["operator_identity", "operator_name", "operator", "persona", "anchor"],
            "domain": "operator",
            "subtopic": "operator_name",
            "private": False,
            # Structured name so memory.persona_ingest._resolve_operator_name can
            # make relations name-bearing ("Noah Lee's son is Abel") without
            # parsing the summary.
            "enrichment": {"operator_name": name},
            **_source_citation_fields(source_citation),
        },
        source=f"hud.settings.operator_name:{now}",
        confidence=1.0,
        provenance=[
            "hud.settings.operator_name_anchor",
            *(
                [f"source_id:{source_citation['source_id']}"]
                if source_citation and source_citation.get("source_id")
                else []
            ),
        ],
        id="1.00.operator_name",
        timestamp_created=now,
        version=1,
        strength_index=1.0,
    )


def _coerce_subcategories(payload: Dict[str, Any]) -> List[str]:
    """Onboarding subcategories may arrive as a list or comma-separated string."""
    raw = payload.get("subcategories") or payload.get("subcategory") or payload.get("subtopics")
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def _gate_operator_profile_ingress(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Quarantine the raw onboarding artifact, then recursively Guardian-screen it.

    The immutable source is recorded before screening so rejected submissions remain
    auditable without ever entering canonical memory. Only a wholly allowed receipt
    may project into persona, knowledge/coding, graph, or cognition.
    """
    if not isinstance(payload, dict):
        raise ValueError("operator profile payload must be an object")

    from guardian.external_ingress import screen_receipt
    from memory.source_ledger.store import record_source
    from memory.taxonomy.normalize import canonical_json

    cycle_id = (
        _clean_text(payload.get("cycle_id"), max_length=120)
        or f"hud-onboarding-{uuid.uuid4().hex[:12]}"
    )
    raw = dict(payload)
    intake_timestamp = _now_iso()
    artifact = record_source(
        canonical_json(raw),
        source_type="operator_onboarding",
        origin_id=cycle_id,
        metadata={
            "source_session_id": cycle_id,
            "source_session_timestamp": intake_timestamp,
        },
    )
    screened = screen_receipt(raw, source="operator_onboarding")
    guardian = dict(screened.pop("guardian", {}) or {})
    decisions = {
        str(key): str(value) for key, value in dict(guardian.get("decisions") or {}).items()
    }
    if int(guardian.get("quarantined") or 0) > 0 or "quarantine" in decisions.values():
        decision = "quarantine"
    elif decisions:
        decision = "flag"
    else:
        decision = "allow"
    return {
        "decision": decision,
        "payload": dict(screened),
        "citation": artifact.citation(),
        "guardian": guardian,
    }


# Serializes the one-shot onboarding guard. The is_onboarded() check and the
# durable write below are a check-then-act: without this mutex two concurrent
# saves (a double-click, an API replay) can BOTH observe "not onboarded" and
# both write a profile. Held across the complete deterministic memory fan-out;
# model-driven category decomposition is deferred to Omega foldback, so this
# critical section never waits on inference. RLock mirrors the voice-settings lock.
_OPERATOR_PROFILE_LOCK = threading.RLock()


def save_operator_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Screen onboarding and place it on the durable cognition input queue.

    An existing profile locks onboarding permanently. While the first profile is
    queued or leased, the queue's fixed identity rejects duplicate submissions. The
    screened payload reaches memory only after pressure opens its episode SRAM bucket.
    """
    from dataclasses import asdict

    from hud.operator_identity import is_onboarded
    from pressure_loop.contributors import make_contributor
    from pressure_loop.cognition_queue import enqueue_input

    with _OPERATOR_PROFILE_LOCK:
        if is_onboarded():
            _emit_fail(
                "operator_profile_locked",
                failure_class="onboarding_locked",
                summary="operator profile re-save rejected — onboarding already complete (locked)",
            )
            return {
                "ok": False,
                "locked": True,
                "error": (
                    "Onboarding already complete — the operator profile is locked. "
                    "Run a day-zero reset to start over."
                ),
            }

        ingress = _gate_operator_profile_ingress(payload)
        decision = str(ingress.get("decision") or "quarantine")
        guardian = {"decision": decision, **dict(ingress.get("guardian") or {})}
        if decision != "allow":
            _emit_fail(
                "operator_profile_ingress",
                failure_class="guardian_ingress_withheld",
                summary=f"operator onboarding withheld by Guardian ({decision})",
            )
            return {
                "ok": False,
                "locked": False,
                "error": "Onboarding was withheld by Guardian and was not added to memory.",
                "guardian": guardian,
            }

        screened_payload = dict(ingress.get("payload") or {})
        source_citation = dict(ingress.get("citation") or {})
        envelope, normalized = build_operator_profile_envelope(
            screened_payload,
            source_citation=source_citation,
        )
        memory_envelopes = [asdict(envelope)]
        operator_name = normalized["profile"].get("preferred_name") or normalized["profile"].get(
            "name"
        )
        if operator_name:
            memory_envelopes.append(
                asdict(
                    build_operator_name_anchor_envelope(
                        operator_name,
                        source_citation=source_citation,
                    )
                )
            )
        contributor = make_contributor(
            "external",
            "sensory_observation",
            1.0,
            {
                "lane": "chat",
                "cycle_id": "operator-onboarding",
                "message": "Screened operator onboarding is ready for episode intake.",
                "ingress_class": "operator_onboarding",
                "directive_intent": "LEARN",
                "memory_envelopes": memory_envelopes,
                "onboarding_profile": {
                    "profile": dict(normalized["profile"]),
                    "subcategories": _coerce_subcategories(screened_payload),
                    "source_citation": source_citation,
                },
            },
        )
        if not enqueue_input(contributor, operator_activity=False):
            return {
                "ok": False,
                "locked": True,
                "error": "Onboarding is already queued or in progress.",
                "guardian": guardian,
            }

    log.info("settings operator profile queued fields=%s", normalized["accepted_fields"])
    return {
        "ok": True,
        "persona_id": "operator",
        "coord": envelope.coord,
        "accepted_fields": normalized["accepted_fields"],
        "ignored_fields": normalized["ignored_fields"],
        "target": "memory/100_personas/operator",
        "onboarding_seed": None,
        "persona_records": 0,
        "acknowledgement": (
            "Onboarding received and queued. It will be written to memory when its "
            "cognition episode closes."
        ),
        "guardian": guardian,
        "memory": {
            "cycle_id": envelope.cycle_id,
            "status": "queued",
            "staging_ref": None,
            "path": None,
            "message": "durably queued for episode SRAM intake",
        },
    }
