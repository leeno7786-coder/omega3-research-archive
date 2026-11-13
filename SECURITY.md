# Security and responsible disclosure

Omega 3.0 is retired research code and is **not maintained as a security-supported service**. Do not deploy it with real credentials or expose its HTTP interfaces on the public internet without a fresh security review.

## Reporting vulnerabilities

Report a credential exposure, sensitive file, authentication defect or exploit privately to **nanoai@omega2ai.com**. Do not include secrets in public GitHub issues.

## Historical credentials and data

The release is prepared as a **fresh-source snapshot**, not a copy of the private Git history, because archived internal test logs and evidence contain values that resemble keys and private-machine references. Local datasets, memory profiles, OAuth token stores, models, binaries, .env files, and personal validation traces are omitted.

An automated pattern scan cannot prove the absence of every secret. The publisher should review file contents and commit history before any final public upload.

## Scope of permissions

The architecture implements declarative plugin/MCP grants, package verification, external-ingress screening, and process isolation. Their presence does **not** constitute a formal security certification. Use a disposable, isolated environment for experimentation.
