"""SKILL.md-backed local skill inventory for the HUD live-chat surface."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.paths import GUARDIAN_STATE_ROOT, OMEGA_ROOT

WORKSPACE_SKILL_DIR = OMEGA_ROOT / "skills"
USER_SKILL_DIR = GUARDIAN_STATE_ROOT / "live-chat" / "skills"
SKILL_CONFIG_PATH = GUARDIAN_STATE_ROOT / "live-chat" / "skill-config.json"

SEARCH_DIRS = [
    WORKSPACE_SKILL_DIR,
    USER_SKILL_DIR,
]

_BUILT_IN_COMMANDS = (
    {
        "name": "/help",
        "description": "Show the live chat, skills, plugins, and permission controls",
        "full_description": "/help - show the live chat surface and its current controls",
    },
    {
        "name": "/skills",
        "description": "List installed local skills",
        "full_description": "/skills - refresh the installed SKILL.md command list",
    },
    {
        "name": "/mode",
        "description": "Show the current chat permission mode",
        "full_description": "/mode - show read-only, allow-edits, or always-allow status",
    },
    {
        "name": "/read-only",
        "description": "Allow inspection only; block edits, commands, and plugins",
        "full_description": "/read-only - set the chat agent to observation-only mode",
    },
    {
        "name": "/allow-edits",
        "description": "Allow workspace edits; gate mutation-shaped commands",
        "full_description": "/allow-edits - allow bounded file edits in the workspace",
    },
    {
        "name": "/always-allow",
        "description": "Allow declared chat tools without this mode gate",
        "full_description": "/always-allow - explicitly enable the highest chat tool mode",
    },
    {
        "name": "/trust",
        "description": "Grant a directory to Omega's workspace tools",
        "full_description": "/trust <directory> - grant and activate a workspace directory",
    },
    {
        "name": "/cd",
        "description": "Select a previously trusted directory",
        "full_description": "/cd <directory> - select the active trusted directory",
    },
    {
        "name": "/trusted",
        "description": "List trusted directories and the active root",
        "full_description": "/trusted - list workspace grants and the active directory",
    },
    {
        "name": "/untrust",
        "description": "Remove a trusted directory",
        "full_description": "/untrust <directory> - remove a workspace grant",
    },
)


@dataclass(frozen=True)
class QwenSkill:
    name: str
    description: str = ""
    prompt: str = ""
    tools: list[str] = field(default_factory=list)
    enabled: bool = True
    command: str | None = None
    long_description: str = ""
    version: str = "1.0.0"
    author: str = "user"
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    welcome_message: str = ""
    scripts: list[str] = field(default_factory=list)

    @property
    def slash_command(self) -> str:
        return f"/{self.command or f'skill:{self.name}'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "longDescription": self.long_description or self.description,
            "long_description": self.long_description or self.description,
            "prompt": self.prompt,
            "command": self.command or f"skill:{self.name}",
            "tools": list(self.tools),
            "scripts": list(self.scripts),
            "enabled": self.enabled,
            "welcomeMessage": self.welcome_message,
            "welcome_message": self.welcome_message,
            "tags": list(self.tags),
            "keywords": list(self.keywords),
            "version": self.version,
            "author": self.author,
        }


@dataclass(frozen=True)
class QwenSkillCommand:
    name: str
    description: str
    full_description: str
    skill_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "fullDescription": self.full_description,
            "full_description": self.full_description,
            "skillName": self.skill_name,
            "skill_name": self.skill_name,
        }


def normalize_skill_name(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-_]+", "-", str(name or "").strip().lower())).strip(
        "-"
    )


def _split_list_value(raw: str) -> list[str]:
    src = str(raw or "").strip()
    if src.startswith("[") and src.endswith("]"):
        return [p.strip().strip("'\"") for p in src[1:-1].split(",") if p.strip()]
    return []


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    src = str(text or "")
    match = re.match(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n?", src)
    if not match:
        return {}, src.strip()

    attrs: dict[str, Any] = {}
    active_list_key = ""
    for line in (match.group(1) or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        list_match = re.match(r"^\s*-\s+(.+)$", line)
        if active_list_key and list_match:
            attrs.setdefault(active_list_key, []).append(list_match.group(1).strip().strip("'\""))
            continue

        active_list_key = ""
        kv = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not kv:
            continue
        key = kv.group(1).strip()
        value = kv.group(2).strip()
        if not value:
            attrs[key] = []
            active_list_key = key
            continue
        list_value = _split_list_value(value)
        attrs[key] = list_value if list_value else value.strip("'\"")

    return attrs, src[match.end() :].strip()


def _attr_string(attrs: dict[str, Any], *keys: str) -> str:
    lowered = {str(k).lower(): v for k, v in attrs.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str):
            return value
    return ""


def _attr_list(attrs: dict[str, Any], key: str) -> list[str]:
    lowered = {str(k).lower(): v for k, v in attrs.items()}
    value = lowered.get(key.lower())
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        parsed = _split_list_value(value)
        if parsed:
            return parsed
        return [p.strip() for p in re.split(r"[|,]", value) if p.strip()]
    return []


def _title_from_body(body: str) -> str:
    match = re.search(r"^#\s+(.+)$", body or "", flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def load_skill_config() -> dict[str, bool]:
    if not SKILL_CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(SKILL_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, list):
        return {str(name): True for name in raw}
    if isinstance(raw, dict):
        return {str(k): bool(v) for k, v in raw.items()}
    return {}


def read_skill_file(
    path: Path, prefs: dict[str, bool] | None = None, *, template: bool = False
) -> QwenSkill | None:
    try:
        attrs, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return None

    raw_name = _attr_string(attrs, "name") or path.parent.name
    name = normalize_skill_name(raw_name)
    if not name:
        return None

    title = _title_from_body(body)
    description = _attr_string(attrs, "description") or title or name
    long_description = _attr_string(attrs, "long_description", "longDescription") or description
    command = (_attr_string(attrs, "command") or f"skill:{name}").lstrip("/")
    config = prefs if prefs is not None else load_skill_config()

    return QwenSkill(
        name=name,
        description=description,
        prompt=body,
        tools=_attr_list(attrs, "tools"),
        enabled=False if template else config.get(name, True),
        command=command,
        long_description=long_description,
        version=_attr_string(attrs, "version") or "1.0.0",
        author=_attr_string(attrs, "author") or "user",
        tags=_attr_list(attrs, "tags"),
        keywords=_attr_list(attrs, "keywords"),
        welcome_message=_attr_string(attrs, "welcome_message", "welcomeMessage"),
        scripts=_attr_list(attrs, "scripts"),
    )


def load_skills() -> dict[str, QwenSkill]:
    prefs = load_skill_config()
    skills: dict[str, QwenSkill] = {}
    for root in SEARCH_DIRS:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if not skill_file.exists():
                continue
            skill = read_skill_file(skill_file, prefs)
            if skill is not None:
                skills[skill.name] = skill
    return skills


def get_skill_commands(skills: dict[str, QwenSkill] | None = None) -> list[QwenSkillCommand]:
    skills = skills if skills is not None else load_skills()
    commands: list[QwenSkillCommand] = []
    for name, skill in skills.items():
        if not skill.enabled:
            continue
        desc = skill.long_description or skill.description or ""
        commands.append(
            QwenSkillCommand(
                name=skill.slash_command,
                description=desc if len(desc) <= 80 else f"{desc[:77]}...",
                full_description=desc,
                skill_name=name,
            )
        )
    commands.sort(key=lambda c: c.name)
    return commands


def resolve_skill_command(
    text: str, skills: dict[str, QwenSkill] | None = None
) -> dict[str, Any] | None:
    """Resolve a leading skill slash command, or None when the text does not start with one.

    ``/skill:<name> <task>`` (or a skill's own frontmatter ``command``) is the operator
    choosing that skill for the task. Built-in commands and ``/plugin:`` selectors are not
    skills and return None. A rejected selection carries the reply to show the operator.
    """
    clean = str(text or "").strip()
    if not clean.startswith("/"):
        return None
    parts = clean.split(None, 1)
    head = parts[0]
    token = head[1:]
    if not token or token.lower().startswith("plugin:"):
        return None
    catalog = skills if skills is not None else load_skills()
    skill = {s.slash_command.lower(): s for s in catalog.values()}.get(head.lower())
    if skill is None:
        if token.lower().startswith("skill:"):
            name = normalize_skill_name(token[len("skill:"):]) or token[len("skill:"):]
            return {"ok": False, "message": f"No installed skill named {name!r}. Type / to see installed skills."}
        return None
    if not skill.enabled:
        return {"ok": False, "message": f"The {skill.name} skill is disabled. Enable it before using {skill.slash_command}."}
    objective = parts[1].strip() if len(parts) > 1 else ""
    if not objective:
        return {"ok": False, "message": f"Add the task after {skill.slash_command}, for example: {skill.slash_command} <what to do>"}
    return {"ok": True, "skill": skill.name, "command": skill.slash_command, "objective": objective}


def built_in_commands() -> list[dict[str, str]]:
    return [dict(item) for item in _BUILT_IN_COMMANDS]


def chat_help_message() -> str:
    """Return the deterministic help card painted in the live chat feed."""
    lines = [
        "Omega chat commands",
        "",
        "/help — show this command list",
        "/skills — list installed local skills",
        "/skill:<name> <task> — hand the task to that skill in the agent lane",
        "/mode — show the current chat permission mode",
        "/read-only, /allow-edits, /always-allow — change chat permissions",
        "/trust <directory> — grant and activate a workspace directory",
        "/cd <directory> — select a previously trusted directory",
        "/trusted — list trusted directories and the active root",
        "/untrust <directory> — remove a workspace grant",
        "",
        "Workspace tools stay unavailable until a trusted directory is active. "
        "Use Settings → Workspace or /trust, then /cd to switch roots.",
    ]
    return "\n".join(lines)


def installed_plugin_commands(entries: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, str]]:
    """Return slash commands for installed plugins only.

    Catalog candidates deliberately never reach this list. Installation is the
    authority boundary for exposing a plugin in the operator's chat surface.
    """
    commands: list[dict[str, str]] = []
    for entry in entries:
        plugin_id = str(entry.get("plugin_id") or "").strip()
        if not plugin_id or entry.get("installed") is not True:
            continue
        title = str(entry.get("title") or plugin_id).strip()
        description = str(entry.get("description") or entry.get("summary") or "").strip()
        commands.append(
            {
                "name": f"/plugin:{plugin_id}",
                "description": description[:100] or f"Use installed {title}",
                "full_description": f"/plugin:{plugin_id} - use the installed {title} plugin",
                "plugin_id": plugin_id,
            }
        )
    return sorted(commands, key=lambda item: item["name"])


def skill_payload(*, plugins: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    skills = load_skills()
    return {
        "ok": True,
        "available": [s.to_dict() for s in skills.values()],
        "commands": [c.to_dict() for c in get_skill_commands(skills)],
        "builtins": built_in_commands(),
        "plugins": installed_plugin_commands(plugins),
        "active": [],
    }
