---
name: devops-workflow
kind: execution_skill
version: 1
enabled: true
description: 'DevOps and deployment: Docker, CI/CD, infrastructure as code, monitoring,
  and incident response'
author: omega
tags:
- devops
- docker
- ci-cd
keywords: []
skill_class: devops_skill
intents:
- USE_SKILL
execution_kinds:
- MAINTAIN
- PLAN
domains:
- devops
- infrastructure
- ci-cd
- monitoring
allowed_tools:
- read_files
- search_repo
- run_command
- web_fetch
forbidden_tools:
- desktop_screenshot
- desktop_click
- browser_navigate
- browser_read
- patch_file
- write_file
- edit_file
safety_class: command_safe
requires_confirmation_for:
- run_command
max_steps: 30
max_duration_ms: 600000
max_files_changed: 0
evidence_required:
- pattern_applied
- workflow_documented
- config_validated
completion_criteria:
- devops_pattern_established
- infrastructure_code_verified
- monitoring_configured
failure_classes:
- tool_unavailable
- deployment_failed
- infrastructure_invalid
output_schema:
  type: object
  properties:
    pattern:
      type: string
      title: DevOps pattern applied (docker, ci-cd, iac, monitoring, incident-response)
    status:
      type: string
      enum:
      - configured
      - validated
      - deployed
      - documented
    recommendations:
      type: array
      items:
        type: string
      title: Next steps or improvements
    artifacts:
      type: array
      items:
        type: string
      title: Files referenced or created
  required:
  - pattern
  - status
long_description: 'Activates DevOps engineering patterns: multi-stage Docker builds
  with minimal images; GitHub Actions CI/CD with matrix builds and caching; Terraform/OpenTofu
  infrastructure as code with remote state and modules; Prometheus/Grafana monitoring
  with RED method and symptom-based alerting; and structured incident response with
  detect-respond-mitigate-resolve-learn workflow.'
---

# devops-workflow

Activates DevOps engineering patterns: multi-stage Docker builds with minimal images; GitHub Actions CI/CD with matrix builds and caching; Terraform/OpenTofu infrastructure as code with remote state and modules; Prometheus/Grafana monitoring with RED method and symptom-based alerting; and structured incident response with detect-respond-mitigate-resolve-learn workflow.

## Instructions
You are a DevOps engineer. Follow these patterns.

## Docker
- Use multi-stage builds to minimize image size:
```dockerfile
FROM python:3.12-slim AS builder
COPY requirements.txt .
RUN pip install --prefix=/opt/omega -r requirements.txt

FROM python:3.12-slim
COPY --from=builder /opt/omega /opt/omega
COPY src/ /app
WORKDIR /app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0"]
```
- Use `.dockerignore` to exclude `__pycache__`, `.git`, `venv`, `tests/`, `.env`.
- Tag images with commit SHA AND semver: `myapp:v1.2.3`, `myapp:abc123def`.
- Use `docker compose` for local dev. Define services: app, db, cache, queue.

## CI/CD (GitHub Actions)
```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements/dev.txt
      - run: ruff check .
      - run: pytest --cov=src --cov-report=term-missing
```
- Run linters and tests on every push. Run E2E tests on main merges.
- Cache dependencies: `actions/cache` for pip, npm, or apt.
- Use matrix builds for multiple Python/Node versions.
- Separate CI (verify) from CD (deploy). Require CI green for deploy.

## Infrastructure as Code
- Terraform for cloud resources. OpenTofu as open-source alternative.
- Keep state in remote backend (S3/GCS/Azure Storage with locking).
- Use modules for reusable infrastructure: `modules/vpc/`, `modules/eks/`.
- Use `terraform plan` in CI, require approval for `terraform apply`.
- Tag all resources: `{ "project": "myapp", "env": "production", "managed_by": "terraform" }`.

## Monitoring
- Metrics: use Prometheus for collection, Grafana for dashboards, RED method (Rate, Errors, Duration).
- Logs: structured JSON logs. `logging.basicConfig(format="%(message)s", stream=sys.stdout)`.
- Alerts: alert on symptoms not causes. "API error rate > 5%" not "Server CPU > 90%".
- On-call: runbooks for every alert. Playbook format: symptoms -> check -> fix -> verify.

## Incident response
1. **Detect**: monitor, alert, or user report. Severity: sev1 (outage), sev2 (degraded), sev3 (minor).
2. **Respond**: acknowledge the alert. Declare incident if sev1/sev2.
3. **Mitigate**: rollback, feature flag off, scale up. Stop the bleeding first.
4. **Resolve**: apply the permanent fix. Verify in staging, deploy to prod.
5. **Learn**: blameless postmortem. What went well? What went wrong? What will we change?

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
