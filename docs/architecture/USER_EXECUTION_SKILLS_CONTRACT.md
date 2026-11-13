# User-Created Execution Skills — Authoring Contract (Phase 6N)

This contract defines how a **user** may author an execution skill that Omega's qwen executor
can run. It is the authoring foundation only — full public UX comes later. The governing rule:
**no unsafe user skill executes by default.** User skills share the same `ExecutionSkill` schema
as bundled skills, but are **disabled until explicitly trusted**.

> Sandbox invariant: qwen + its tools are jailed to the **operator's workspace**. Omega's own
> source/architecture is invisible to skill execution. A user skill cannot read or write Omega
> itself; it operates only inside the bounded dispatch Auto pushes to qwen.

---

## 1. Skill directory layout

```
<user-skill-root>/
  <skill-name>/
    SKILL.md          # required — YAML frontmatter + markdown body (the contract qwen receives)
    (optional assets) # reference files the body points to
```

A user-skill root is a directory containing one subdirectory per skill. Each subdirectory's
name is the skill's slug source. Bundled skills live under `omega_cli_ts/skills/`; user skills
live under a separate, operator-designated root and are loaded via
`runtime.user_execution_skills.load_user_execution_skills`.

## 2. Required `SKILL.md` frontmatter

Frontmatter is real YAML between `---` fences. Required keys (same as bundled skills):

| key | type | notes |
|---|---|---|
| `name` | str | unique slug source → `skill_id` |
| `kind` | str | must be `execution_skill` |
| `version` | int | ≥ 1 |
| `enabled` | bool | authored intent; **forced `false` for untrusted user skills** |
| `description` | str | one line, used for selection matching |
| `skill_class` | str | one of the 15 skill classes |
| `intents` | list | must include `USE_SKILL` |
| `execution_kinds` | list | subset of the 21 execution kinds |
| `domains` | list | matching domains |
| `allowed_tools` | list | the ONLY tools qwen may use |
| `forbidden_tools` | list | tools explicitly denied |
| `safety_class` | str | one of the 7 safety classes (§3) |
| `requires_confirmation_for` | list | actions needing operator confirmation |
| `max_steps`, `max_duration_ms`, `max_files_changed` | int | hard limits |
| `evidence_required` | list | evidence the result must carry |
| `completion_criteria` | list | done conditions |
| `failure_classes` | list | declared failure modes |
| `output_schema` | object | JSON-schema shape of the structured result |

Validation is performed by `schema.execution_skill.validate_execution_skill_frontmatter`
(14 cross-field rules).

## 3. Allowed safety classes

`read_only`, `network_read`, `write_safe`, `command_safe`, `destructive`, `desktop_control`,
`self_modification`. A **user skill may NOT declare `self_modification`** — it is rejected at
validation (no special policy grants it yet).

## 4. Allowed tool declarations

- `allowed_tools` must be the minimal set the skill needs; raw world-mutating tools
  (`write_file`, `execute_command`, …) are qwen-**internal** and only usable when the skill's
  `safety_class` permits writes/commands.
- `forbidden_tools` must be declared and must not overlap `allowed_tools`.
- Cross-field rules: `read_only` skills may declare no write/command tools and
  `max_files_changed == 0`; `destructive`/`desktop_control` require confirmation.

## 5. Validation process

1. Parse frontmatter as YAML (malformed YAML → rejected, diagnostic only).
2. `validate_execution_skill_frontmatter(..., allow_self_modification=False)` — a user skill is
   never granted self-modification.
3. On any validation error the skill is **not loaded** — it appears only as a
   `SkillLoadDiagnostic`. Loading is fail-soft per skill (one bad skill never blocks the rest).

## 6. Disabled-by-default policy for user skills

- **Bundled** skills may be enabled by default (`enabled: true` is honored).
- **User** skills are forced **disabled** (`enabled → false`) unless explicitly trusted, so an
  untrusted user skill is **not selectable** and can never be dispatched to qwen.

## 7. Trust / graduation policy

A user skill becomes selectable only when explicitly trusted, via either:
- the per-skill `trust_approved` allow-list (operator names the skill), or
- the global `OMEGA_TRUST_USER_SKILLS` flag (default **OFF**).

Graduation (raising a trusted skill's capability strength) follows the same reinforcement path
as bundled skills (§10) — trust gates *selectability*; graduation tracks *reliability*.

## 8. How skills become capabilities

`memory.capability_catalog.sync_capabilities` ingests **enabled** skills into lobe 5 as Omega
capabilities. Trusted user skills can be included in this sync later; untrusted ones are not.

## 9. How capability IDs are assigned

Deterministically: `capability_id_for_name(name, scope="dev")` → `5.01.qwenskill-<slug>`. The id
is stable across reloads and identical for bundled and user skills — so a re-sync overwrites in
place rather than duplicating.

## 10. How skill success reinforces capability

A **grounded** skill success calls `reinforce_capability(name, …)` (+0.05 write_strength,
clamped). A partial that materially advanced the objective earns a smaller delta. Pending
approval is neutral (no reinforce, no punish). See Phase 6L consolidation.

## 11. How failures are recorded

- failure / timeout / invalid → **failure memory** (and may create repair pressure);
- blocked by safety → **safety memory** (not a capability failure);
- malformed / bridge-unavailable → **bridge reliability** weakened (not the skill);
- qwen forbidden-tool attempt → the skill's capability is **weakened**.

## 12. How unsafe skills are rejected

- `self_modification` safety class → rejected at validation.
- Malformed frontmatter → rejected, diagnostic only.
- A user skill **cannot bypass guards** (the Phase 6I execution guard applies to every dispatch),
  **cannot route around the skill layer** (Phase 6M), and **cannot become a pressure contributor
  directly** (pressure is owned by the cognition cascade, never by qwen or a skill).

---

## Policy summary

- bundled skills may be enabled by default;
- user-created skills default disabled or require explicit trust approval;
- user skills must pass schema validation;
- user skills must declare `safety_class`, `allowed_tools`, and `forbidden_tools`;
- user skills cannot request `self_modification` unless a special policy allows it (none does yet);
- user skills cannot bypass guards;
- user skills cannot become pressure contributors directly.
