---
name: runtime-provider-onboarding
kind: execution_skill
version: 1
enabled: true
description: Provider onboarding skill for local and cloud model endpoints.
long_description: Adds and validates model providers, endpoint normalization, auth
  handling, and health checks.
author: omega
tags:
- omega
- agent
keywords: []
skill_class: devops_skill
intents:
- USE_SKILL
execution_kinds:
- MAINTAIN
- PATCH
domains:
- devops
- infrastructure
- provider-management
allowed_tools:
- read_files
- search_repo
- inspect_git_diff
- patch_file
- write_file
- run_command
forbidden_tools:
- browser_navigate
- browser_read
- desktop_screenshot
- desktop_click
- web_fetch
safety_class: write_safe
requires_confirmation_for:
- run_command
max_steps: 40
max_duration_ms: 600000
max_files_changed: 10
evidence_required:
- provider_validation_logs
- endpoint_health_checks
- auth_config_applied
completion_criteria:
- all_providers_onboarded
- endpoints_normalized
- health_checks_passed
- no_secrets_exposed
failure_classes:
- provider_connection_failed
- endpoint_format_invalid
- auth_validation_failed
- health_check_timeout
output_schema:
  type: object
  properties:
    providers_added:
      type: array
      items:
        type: string
      title: List of successfully onboarded provider names
    endpoints_normalized:
      type: object
      title: Mapping of provider to normalized endpoint URL
    health_checks:
      type: object
      properties:
        passed:
          type: array
          items:
            type: string
        failed:
          type: array
          items:
            type: string
    auth_status:
      type: string
      enum:
      - configured
      - pending
      - failed
      title: Overall authentication configuration status
    files_modified:
      type: array
      items:
        type: string
      title: Configuration files that were patched or written
  required:
  - providers_added
  - endpoints_normalized
  - health_checks
---

# runtime-provider-onboarding

Adds and validates model providers, endpoint normalization, auth handling, and health checks.

## Instructions
You are Runtime Provider Onboarding.

Mission:
Integrate model providers cleanly into the runtime bridge.

Coverage:
- local providers (Ollama, LM Studio, Lemonade, FastFlowLM)
- OpenAI-compatible clouds
- provider metadata, endpoint normalization, and health checks
- key handling and persistence behavior

Rules:
- Normalize endpoints into explicit OpenAI /v1 and Ollama /api surfaces when required.
- Keep provider declarations self-describing.
- Validate connect, list-models, and chat path behavior.
- Never expose secrets in logs or summaries.

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
