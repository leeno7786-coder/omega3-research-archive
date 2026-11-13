---
name: web-api-design
kind: execution_skill
version: 1
enabled: true
description: 'REST API design: endpoints, error handling, versioning, OpenAPI, and
  FastAPI patterns'
author: omega
tags:
- api
- rest
- fastapi
keywords: []
skill_class: repair_skill
intents:
- USE_SKILL
execution_kinds:
- REVIEW
- PATCH
- REPAIR
domains:
- backend
- api_design
- rest_patterns
allowed_tools:
- read_files
- search_repo
- patch_file
- write_file
- run_command
forbidden_tools:
- desktop_screenshot
- browser_navigate
- browser_read
safety_class: write_safe
requires_confirmation_for:
- run_command
max_steps: 30
max_duration_ms: 600000
max_files_changed: 15
evidence_required:
- api_patterns_matched
- violations_found
- patches_applied
completion_criteria:
- api_reviewed_against_patterns
- recommendations_documented
- code_aligned_with_rest_standards
failure_classes:
- malformed_api_code
- unreadable_endpoints
- pattern_violations_unfixable
output_schema:
  type: object
  properties:
    summary:
      type: string
    violations_found:
      type: array
      items:
        type: object
        properties:
          endpoint:
            type: string
          pattern:
            type: string
          issue:
            type: string
        required:
        - endpoint
        - pattern
        - issue
    patches_applied:
      type: array
      items:
        type: string
    recommendations:
      type: array
      items:
        type: string
  required:
  - summary
  - violations_found
  - patches_applied
long_description: 'Activates REST API design patterns: noun-based URL design, HTTP
  method semantics, structured error responses with field-level details, FastAPI/Pydantic
  v2 endpoint patterns, URL-prefix versioning with deprecation headers, and cursor/offset
  pagination strategies.'
---

# web-api-design

Activates REST API design patterns: noun-based URL design, HTTP method semantics, structured error responses with field-level details, FastAPI/Pydantic v2 endpoint patterns, URL-prefix versioning with deprecation headers, and cursor/offset pagination strategies.

## Instructions
You are an API design specialist. Follow these patterns.

## RESTful URL design
- Nouns for resources: `/users`, `/orders/{id}`, `/users/{id}/orders`. No verbs in URLs.
- Plural nouns for collections: `/users` not `/user`.
- Nested resources max 2 levels deep: `/users/{id}/orders/{orderId}/items` is too deep - use `/items?orderId=123`.
- Query params for filtering, sorting, and pagination: `?status=active&sort=created_at&order=desc&limit=20&offset=0`.

## HTTP methods
| Method | Action | Status codes |
|--------|--------|-------------|
| GET | Read | 200, 404 |
| POST | Create | 201, 400, 409 |
| PUT | Full replace | 200, 204, 400, 404 |
| PATCH | Partial update | 200, 204, 400, 404, 422 |
| DELETE | Remove | 204, 404 |

## Structured error responses
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid input data",
    "details": [
      {"field": "email", "message": "must be a valid email address", "code": "invalid_format"}
    ],
    "request_id": "req_abc123"
  }
}
```
- Never leak stack traces, internal paths, or implementation details in error responses.
- Use standard HTTP status codes consistently. Don't return 200 for errors.
- Return 422 (Unprocessable Entity) for validation errors, 400 for malformed syntax.

## OpenAPI / FastAPI
- Use FastAPI for Python APIs. Define models with Pydantic v2.
- Use `response_model=` for response schemas. Use `status_code=` for different codes.
- Document with `summary`, `description`, and `tags` parameters.
- Use `Query(..., description="...")` and `Path(..., description="...")` for parameter docs.
- Example:
```python
@app.post("/users", response_model=UserOut, status_code=201, tags=["users"])
async def create_user(body: UserCreate) -> UserOut:
    """Create a new user. Validates email uniqueness."""
    ...
```

## Versioning
- URL prefix: `/v1/users`, `/v2/users`. Never accept-version headers (harder to discover).
- Backward-compatible changes (new fields, new endpoints) don't need a version bump.
- Breaking changes (field removal, type change, endpoint removal) need a new version.
- Deprecate with a `Sunset` header: `Sunset: Sat, 31 Dec 2026 23:59:59 GMT`.

## Pagination
- Cursor-based for real-time data: `?cursor=eyJpZCI6IDEyM30&limit=20`.
- Offset-based for stable data: `?offset=0&limit=20`.
- Return: `{ "items": [...], "total": 150, "limit": 20, "offset": 0 }` or `{ "items": [...], "next_cursor": "...", "has_more": true }`.

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
