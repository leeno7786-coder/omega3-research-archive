"""Cognition's own read model of the local SKILL.md catalog (seam 9).

The HUD maintains a separate SKILL.md inventory, but it is operator-console code,
so the cognition cascade must NOT depend on it. This module is the brain-side,
HUD-free, READ-ONLY view of the product catalog: it scans local skill dirs,
parses simple SKILL.md frontmatter, and honors live-chat state without a bridge.

It is deliberately read-only: skill authoring/toggle/delete are operator concerns.
``memory.capability_catalog`` ingests this catalog into lobe 5 so the executor's
skills become Omega's applied capabilities.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.paths import GUARDIAN_STATE_ROOT, OMEGA_ROOT

# Omega's product skill catalog is local to the checkout. Later roots override
# earlier ones on a name collision; host-level agent libraries never enter it.
_SKILL_DIRS: tuple[Path, ...] = (
    OMEGA_ROOT / "skills",
)
_SKILL_CONFIG_PATH = GUARDIAN_STATE_ROOT / "live-chat" / "skill-config.json"

# Omega's SHIPPED executor skills — the repo-bundled dirs only. This is the
# curated set that becomes Omega's APPLIED capabilities (the design's "22"),
# deliberately excluding all host-level agent libraries. Live chat owns execution
# and the capability catalog stays on the local product tree.
BUNDLED_SKILL_DIRS: tuple[Path, ...] = (
    OMEGA_ROOT / "skills",
)


@dataclass(frozen=True)
class QwenSkill:
    name: str
    description: str = ""
    long_description: str = ""
    prompt: str = ""
    command: str = ""
    enabled: bool = True
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)

    @property
    def slash_command(self) -> str:
        return f"/{self.command or f'skill:{self.name}'}"


def normalize_skill_name(name: str) -> str:
    """Same normalization the bridge uses: lowercase, non-[a-z0-9-_] -> '-', squeezed."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-_]+", "-", str(name or "").strip().lower())).strip(
        "-"
    )


def _split_inline_list(raw: str) -> List[str]:
    src = str(raw or "").strip()
    if src.startswith("[") and src.endswith("]"):
        return [p.strip().strip("'\"") for p in src[1:-1].split(",") if p.strip()]
    return []


def _parse_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Parse the leading ---\\n...\\n--- block (key: value, inline [a,b], block lists).

    Returns (attrs, body). Keys are kept as-authored; callers lowercase on read.
    """
    src = str(text or "")
    match = re.match(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n?", src)
    if not match:
        return {}, src.strip()
    attrs: Dict[str, Any] = {}
    active_list_key = ""
    for line in (match.group(1) or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        list_item = re.match(r"^\s*-\s+(.+)$", line)
        if active_list_key and list_item:
            attrs.setdefault(active_list_key, []).append(list_item.group(1).strip().strip("'\""))
            continue
        active_list_key = ""
        kv = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not kv:
            continue
        key, value = kv.group(1).strip(), kv.group(2).strip()
        if not value:
            attrs[key] = []
            active_list_key = key
            continue
        inline = _split_inline_list(value)
        attrs[key] = inline if inline else value.strip("'\"")
    return attrs, src[match.end() :].strip()


def _attr_str(attrs: Dict[str, Any], *keys: str) -> str:
    lowered = {str(k).lower(): v for k, v in attrs.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str):
            return value
    return ""


def _attr_list(attrs: Dict[str, Any], key: str) -> List[str]:
    lowered = {str(k).lower(): v for k, v in attrs.items()}
    value = lowered.get(key.lower())
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        parsed = _split_inline_list(value)
        return parsed or [p.strip() for p in re.split(r"[|,]", value) if p.strip()]
    return []


def load_skill_config(path: Optional[Path] = None) -> Dict[str, bool]:
    """Enable/disable map from skill-config.json. Missing/garbage -> {} (all enabled).

    Accepts the bridge's two shapes: an object {name: bool} or an array of enabled names.
    """
    cfg_path = path or _SKILL_CONFIG_PATH
    try:
        if not cfg_path.is_file():
            return {}
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(raw, list):
        return {str(name): True for name in raw}
    if isinstance(raw, dict):
        return {str(k): bool(v) for k, v in raw.items()}
    return {}


def _read_skill_file(skill_file: Path, config: Dict[str, bool]) -> Optional[QwenSkill]:
    try:
        attrs, body = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    except OSError:
        return None
    name = normalize_skill_name(_attr_str(attrs, "name") or skill_file.parent.name)
    if not name:
        return None
    title_match = re.search(r"^#\s+(.+)$", body or "", flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else ""
    description = _attr_str(attrs, "description") or title or name
    return QwenSkill(
        name=name,
        description=description,
        long_description=_attr_str(attrs, "long_description", "longdescription") or description,
        prompt=body,
        command=(_attr_str(attrs, "command") or f"skill:{name}").lstrip("/"),
        enabled=config.get(name, True),
        tags=_attr_list(attrs, "tags"),
        keywords=_attr_list(attrs, "keywords"),
    )


def load_skills(
    roots: Optional[List[Path]] = None,
    config: Optional[Dict[str, bool]] = None,
) -> Dict[str, QwenSkill]:
    """Load the executor skill catalog as {name: QwenSkill}.

    Scans ``roots`` (default: the standard skill dirs) for ``<root>/<skill>/SKILL.md``;
    later roots override earlier on a name collision. ``config`` (default: read from
    skill-config.json) supplies the enabled state. Pure filesystem; no bridge required.
    """
    search = list(roots) if roots is not None else list(_SKILL_DIRS)
    prefs = config if config is not None else load_skill_config()
    skills: Dict[str, QwenSkill] = {}
    for root in search:
        try:
            if not Path(root).is_dir():
                continue
            children = sorted(Path(root).iterdir())
        except OSError:
            continue
        for child in children:
            skill_file = child / "SKILL.md"
            if not child.is_dir() or not skill_file.is_file():
                continue
            skill = _read_skill_file(skill_file, prefs)
            if skill is not None:
                skills[skill.name] = skill
    return skills
