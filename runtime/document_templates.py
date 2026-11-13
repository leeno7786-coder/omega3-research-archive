"""Load shipped document template skeletons + fill instructions."""

from __future__ import annotations


from core.paths import OMEGA_ROOT

_TEMPLATES_ROOT = OMEGA_ROOT / "config" / "document_templates"


def load_template_parts(name: str = "generic") -> tuple[str, str]:
    """Return (skeleton, instructions) for a named template."""
    tmpl_dir = _TEMPLATES_ROOT / (name or "generic")
    skeleton_path = tmpl_dir / "skeleton.md"
    instructions_path = tmpl_dir / "instructions.md"
    skeleton = skeleton_path.read_text(encoding="utf-8")
    instructions = instructions_path.read_text(encoding="utf-8")
    return skeleton.strip(), instructions.strip()


def build_fill_prompt(
    *,
    template: str,
    user_prompt: str,
    fmt: str,
) -> str:
    skeleton, instructions = load_template_parts(template)
    return (
        f"{instructions}\n\n"
        f"Template skeleton (fill in the content sections):\n{skeleton}\n\n"
        f"Operator input:\n{user_prompt.strip()}\n\n"
        f"Output format: Markdown only (will be rendered to {fmt})."
    )
