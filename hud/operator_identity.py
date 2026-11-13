"""Read operator display context from the HUD settings persona store."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from core.paths import MEMORY_ROOT

log = logging.getLogger("omega.hud.operator_identity")

_OPERATOR_PERSONA_DIR = MEMORY_ROOT / "100_personas" / "operator"


class OperatorProfileUnreadable(RuntimeError):
    """The operator profile exists but cannot be read as canonical JSON."""


def operator_context() -> dict[str, str]:
    """Return operator display fields for the HUD.

    Reads the same persona namespace written by hud.settings.save_operator_profile
    (lobe 1, coord [1,0,0], person_id="operator"). Returns empty strings when unset.
    """
    fields = latest_operator_profile_fields()
    if not fields:
        return {}
    name = (fields.get("preferred_name") or fields.get("name") or "").strip()
    relationship = (fields.get("relationship") or fields.get("role") or "").strip()
    return {"name": name, "relationship": relationship}


def is_onboarded() -> bool:
    """True once an operator profile has been saved (it has a name).

    Onboarding is one-shot: after the first save the profile is LOCKED — the HUD
    disables the form and ``hud.settings.save_operator_profile`` rejects re-saves so a
    re-submit can't overwrite the profile or re-run the subtopic decomposition.
    Releasing the lock = a day-zero reset (which wipes the operator persona).
    """
    return bool((operator_context() or {}).get("name"))


def latest_operator_profile_fields() -> dict[str, Any]:
    """Return the full operator profile fields from the latest persona record.

    Reads the same persona namespace written by hud.settings.save_operator_profile
    (lobe 1, coord [1,0,0], person_id="operator"). Returns empty dict when unset.
    """
    try:
        root_stat = _OPERATOR_PERSONA_DIR.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise OperatorProfileUnreadable("operator_profile_directory_unreadable") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise OperatorProfileUnreadable("operator_profile_path_is_not_a_directory")

    def _raise_walk_error(exc: OSError) -> None:
        raise OperatorProfileUnreadable("operator_profile_directory_unreadable") from exc

    try:
        files = []
        for directory, _subdirectories, names in os.walk(
            _OPERATOR_PERSONA_DIR, onerror=_raise_walk_error, followlinks=False
        ):
            files.extend(
                Path(directory) / name for name in names if name.lower().endswith(".json")
            )
    except OperatorProfileUnreadable:
        raise
    except OSError as exc:
        raise OperatorProfileUnreadable("operator_profile_directory_unreadable") from exc

    latest_fields: dict[str, Any] = {}
    latest_ts = ""

    for path in sorted(files):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorProfileUnreadable(
                f"operator_profile_record_unreadable:{path.name}"
            ) from exc
        if not isinstance(record, dict):
            raise OperatorProfileUnreadable(f"operator_profile_record_invalid:{path.name}")
        enrichment = record.get("enrichment") or {}
        if not isinstance(enrichment, dict):
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            enrichment = payload.get("enrichment") or {}
        profile = enrichment.get("operator_profile") or {}
        fields = profile.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            continue
        ts = str(
            record.get("timestamp_created")
            or (record.get("provenance") or {}).get("ingested_at")
            or profile.get("updated_at")
            or path.name
        )
        if ts >= latest_ts:
            latest_ts = ts
            latest_fields = fields
    return latest_fields
