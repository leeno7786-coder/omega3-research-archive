"""Light keyword skill index — match a command to its most relevant skills.

Decoupled from the strict ExecutionSkill schema (Phase 6F selection): this scans
EVERY SKILL.md (name + description + triggers + body), builds a keyword index, and
returns the top-N skills whose guidance should be injected as CONTEXT for a command.
The chat brain (armed with read/search/edit/write/command tools) then uses that
guidance to do the task — like a coding agent pulling the right playbooks per task.

Operator doctrine 2026-06-25: "use the skill choice for every command sent to keyword
match the skills that go with the command to ensure the model does its job correctly
with good context." Lenient by design: a SKILL.md that fails the strict executable
contract still contributes its keywords + guidance here.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Skill roots scanned for SKILL.md (relative to OMEGA_ROOT). Live chat owns
# execution; this catalog is the only bundled skill surface.
_SKILL_DIRS = ("skills",)

_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "is",
    "it",
    "this",
    "that",
    "use",
    "using",
    "me",
    "my",
    "i",
    "you",
    "your",
    "we",
    "should",
    "when",
    "asks",
    "user",
    "users",
    "skill",
    "skills",
    "task",
    "tasks",
    "do",
    "does",
    "make",
    "want",
    "need",
    "get",
    "set",
    "can",
    "be",
    "as",
    "at",
    "by",
    "from",
    "into",
    "are",
    "was",
    "will",
    "would",
    "if",
    "then",
    "so",
    "but",
    "not",
    "no",
    "yes",
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.split(r"[^a-z0-9]+", str(text or "").lower())
        if t and len(t) > 1 and t not in _STOP
    }


def _omega_root() -> Path:
    from core.paths import OMEGA_ROOT

    return Path(OMEGA_ROOT)


def _parse_skill(path: Path) -> Dict[str, Any] | None:
    """Lenient parse: pull name + description + triggers + body, ignoring whether the
    frontmatter satisfies the strict ExecutionSkill schema."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = _FRONTMATTER_RE.match(raw)
    fm_text, body = (m.group(1), m.group(2)) if m else ("", raw)
    name = path.parent.name
    description = ""
    triggers: List[str] = []
    try:
        import yaml

        fm = yaml.safe_load(fm_text) if fm_text.strip() else None
        if isinstance(fm, dict):
            name = str(fm.get("name") or name).strip() or name
            description = str(fm.get("description") or "").strip()
            tg = fm.get("triggers") or fm.get("keywords") or []
            if isinstance(tg, (list, tuple)):
                triggers = [str(t).strip() for t in tg if str(t).strip()]
            elif isinstance(tg, str):
                triggers = [tg.strip()]
    except Exception:
        # malformed YAML — fall back to dir name + the raw body for keywords
        pass
    # The "Use this skill when …" line (common across the catalog) is a dense trigger
    # source — fold it into the trigger weight.
    when = ""
    wm = re.search(r"Use this skill when ([^\n]+)", body, re.IGNORECASE)
    if wm:
        when = wm.group(1)
    return {
        "name": name,
        "path": str(path),
        "description": description,
        "triggers": triggers,
        "body": body.strip(),
        "name_tokens": _tokens(name.replace("-", " ")),
        "trigger_tokens": _tokens(" ".join(triggers) + " " + when),
        "desc_tokens": _tokens(description),
        "body_tokens": _tokens(body[:1200]),
    }


@functools.lru_cache(maxsize=1)
def load_skill_index() -> Tuple[Dict[str, Any], ...]:
    """Scan + parse all SKILL.md once (cached). Order-stable by path."""
    skills: List[Dict[str, Any]] = []
    root = _omega_root()
    seen: set[str] = set()
    for d in _SKILL_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("SKILL.md")):
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            parsed = _parse_skill(p)
            if parsed:
                skills.append(parsed)
    return tuple(skills)


def _score(cmd_tokens: set[str], skill: Dict[str, Any]) -> float:
    """Weighted keyword overlap — triggers/name dominate, body is a weak signal."""
    return (
        3.0 * len(cmd_tokens & skill["trigger_tokens"])
        + 2.0 * len(cmd_tokens & skill["name_tokens"])
        + 1.5 * len(cmd_tokens & skill["desc_tokens"])
        + 0.5 * len(cmd_tokens & skill["body_tokens"])
    )


def match_skills(
    command_text: str, *, top_n: int = 4, min_score: float = 1.0
) -> List[Dict[str, Any]]:
    """The top-N skills whose keywords match the command, best first."""
    cmd = _tokens(command_text)
    if not cmd:
        return []
    # Dedupe by skill NAME — the same skill can live in several roots (skills/,
    # Keep the highest-scoring copy when a local catalog contains duplicates.
    best_by_name: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    for skill in load_skill_index():
        s = _score(cmd, skill)
        if s < min_score:
            continue
        name = skill["name"]
        if name not in best_by_name or s > best_by_name[name][0]:
            best_by_name[name] = (s, skill)
    scored = sorted(best_by_name.values(), key=lambda x: (-x[0], x[1]["name"]))
    return [dict(sk, score=round(sc, 2)) for sc, sk in scored[:top_n]]


def skill_context_for_command(command_text: str, *, top_n: int = 4, body_chars: int = 1400) -> str:
    """Bundle the matched skills' guidance into a context block for the executor's
    directive — empty string when nothing matches (the model then works tool-only)."""
    matched = match_skills(command_text, top_n=top_n)
    if not matched:
        return ""
    blocks = []
    for sk in matched:
        body = str(sk.get("body") or "")
        if len(body) > body_chars:
            body = body[:body_chars].rstrip() + " …"
        desc = str(sk.get("description") or "").strip()
        head = f"### Skill: {sk['name']}"
        blocks.append("\n".join(x for x in (head, desc, "", body) if x is not None))
    intro = (
        "Relevant skills matched to this command — use them as GUIDANCE (playbooks), "
        "applying the steps that fit. You have read/grep/edit/write/shell tools.\n"
    )
    return intro + "\n\n---\n\n".join(blocks)


def _reset_cache_for_tests() -> None:
    load_skill_index.cache_clear()
