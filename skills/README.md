# Omega Agent Skills Catalog

This folder contains SKILL.md-format skills consumed by live chat and AUTO's bounded native executor.

Layout:

- `skills/<skill-name>/SKILL.md` (from the repository root)

## Core Catalog

- code-review-excellence
- debugging-methodology
- devops-workflow
- git-conventional-commits
- python-best-practices
- react-frontend
- skill-creator
- sql-database
- superpowers
- testing-strategy
- web-api-design
- pytest
- vastai

## Omega-Focused Skill Pack

- model-feedback-loop
- omega-planner-executor
- omega-refactor-surgeon
- omega-yolo-safe-edits
- runtime-provider-onboarding
- skill-system-maintainer
- slash-command-architect
- codex-grade-coding
- test-driven-development
- auditing-security
- brainstorming

## Notes

- Live chat loads skills exclusively from local SKILL.md files (YAML frontmatter + Markdown body); the legacy JSON format has been fully migrated out and is no longer read.
- SKILL.md name and description should be declared in frontmatter.
- Every skill now includes frontmatter metadata for:
  tools: tool selections for task execution
  command: optional custom slash token for the skill (defaults to /skill:<skill-name>). /skills and /status are live-chat commands, not per-skill frontmatter.
