"""Seam 9 — the brain's own (hud-free) read model of the qwen executor's skills.

The HUD has its own SKILL.md inventory, but it is being rebuilt and cognition must
not depend on it. This loader is cognition's decoupled view of the executor skill
catalog: parse SKILL.md frontmatter from the skill dirs, honor the enable/disable
config, expose a read-only QwenSkill. No writes, no HUD imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.qwen_skills import QwenSkill, load_skills, normalize_skill_name  # noqa: E402


def _write_skill(root: Path, dirname: str, frontmatter: str, body: str = "Body text.") -> None:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_loads_skill_from_skill_md(tmp_path):
    _write_skill(
        tmp_path,
        "pytest",
        'name: pytest\ndescription: "Run and fix pytest suites"\ntags: [testing, python]\ncommand: skill:pytest',
        body="# pytest\nUse pytest.",
    )
    skills = load_skills(roots=[tmp_path], config={})
    assert "pytest" in skills
    sk = skills["pytest"]
    assert isinstance(sk, QwenSkill)
    assert sk.description == "Run and fix pytest suites"
    assert sk.tags == ["testing", "python"]
    assert sk.command == "skill:pytest"
    assert sk.enabled is True
    assert "Use pytest." in sk.prompt


def test_disabled_in_config_marks_skill_disabled(tmp_path):
    _write_skill(tmp_path, "azure-ai", "name: azure-ai\ndescription: Azure helper")
    skills = load_skills(roots=[tmp_path], config={"azure-ai": False})
    assert skills["azure-ai"].enabled is False


def test_dir_without_skill_md_is_skipped(tmp_path):
    (tmp_path / "not-a-skill").mkdir()
    _write_skill(tmp_path, "python", "name: python\ndescription: Python expert")
    skills = load_skills(roots=[tmp_path], config={})
    assert set(skills) == {"python"}


def test_name_falls_back_to_dir_and_is_normalized(tmp_path):
    # No name in frontmatter -> normalized directory name.
    _write_skill(tmp_path, "Code Review", "description: reviews code")
    skills = load_skills(roots=[tmp_path], config={})
    assert "code-review" in skills
    assert normalize_skill_name("Code Review") == "code-review"


def test_bundled_dirs_are_repo_only_not_the_user_global_library():
    # Omega's applied-capability catalog is its SHIPPED skills, never the user's
    # global ~/.agents / ~/.qwen-agent-tui library (that would flood lobe 5).
    # Structural check, not a home-substring check: on Linux the repo itself
    # lives under $HOME, so "home not in path" would reject every valid dir.
    from runtime.qwen_skills import BUNDLED_SKILL_DIRS

    repo = Path(ROOT).resolve()
    global_libs = (Path.home() / ".agents", Path.home() / ".qwen-agent-tui")
    for d in BUNDLED_SKILL_DIRS:
        resolved = Path(d).resolve()
        assert resolved.is_relative_to(repo)  # under the repo…
        for lib in global_libs:  # …and never the user's global library
            assert not resolved.is_relative_to(lib)


def test_later_root_overrides_earlier_on_name_collision(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_skill(a, "python", "name: python\ndescription: from-a")
    _write_skill(b, "python", "name: python\ndescription: from-b")
    skills = load_skills(roots=[a, b], config={})
    assert skills["python"].description == "from-b"  # later root wins
