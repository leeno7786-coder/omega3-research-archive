---
name: react-frontend
kind: execution_skill
version: 1
enabled: true
description: 'React/TypeScript development: component patterns, state management,
  hooks, and performance'
author: omega
tags:
- react
- typescript
- frontend
keywords: []
skill_class: repair_skill
intents:
- USE_SKILL
execution_kinds:
- WRITE
- PATCH
- REPAIR
domains:
- frontend
- react
- typescript
- web-development
allowed_tools:
- read_files
- search_repo
- patch_file
- write_file
- edit_file
- run_command
- run_tests
forbidden_tools:
- browser_navigate
- browser_read
- desktop_screenshot
- desktop_click
- web_fetch
safety_class: write_safe
requires_confirmation_for:
- run_command
max_steps: 35
max_duration_ms: 600000
max_files_changed: 15
evidence_required:
- pattern_adherence
- code_compiles
- tests_pass
- types_valid
completion_criteria:
- components_follow_structure
- hooks_implemented_correctly
- state_management_patterns_applied
- performance_optimizations_included
- tests_present_and_passing
failure_classes:
- syntax_error
- type_error
- test_failure
- pattern_violation
- missing_export
output_schema:
  type: object
  properties:
    summary:
      type: string
      title: Summary of React components created or modified
    components:
      type: array
      items:
        type: object
        properties:
          name:
            type: string
          pattern:
            type: string
            enum:
            - function_component
            - custom_hook
            - context_provider
            - page_component
          location:
            type: string
        required:
        - name
        - pattern
        - location
      title: List of components affected
    patterns_applied:
      type: array
      items:
        type: string
      title: React/TypeScript patterns applied (e.g., discriminated unions, useReducer,
        React.memo)
    tests_updated:
      type: boolean
    performance_notes:
      type: string
  required:
  - summary
  - components
  - patterns_applied
  - tests_updated
long_description: 'Activates React/TypeScript frontend development patterns: component
  structure with co-located tests and stories, discriminated union state patterns,
  custom hooks, useReducer for complex state, performance optimization with React.memo
  and virtualization, and testing with @testing-library/react and MSW.'
---

# react-frontend

Activates React/TypeScript frontend development patterns: component structure with co-located tests and stories, discriminated union state patterns, custom hooks, useReducer for complex state, performance optimization with React.memo and virtualization, and testing with @testing-library/react and MSW.

## Instructions
You are a senior frontend engineer. Follow these React/TypeScript patterns.

## Component structure
- One component per file. Name the file after the component: `UserProfile.tsx`.
- Prefer function components with hooks. No class components unless required (error boundaries).
- Export components as named exports. Default exports are allowed for page-level components.
- Co-locate styles, tests, and stories: `Button/Button.tsx`, `Button/Button.test.tsx`, `Button/Button.stories.tsx`.

## Types
- Define props inline with the component using `interface` (preferred) or `type`.
- Use `React.FC<Props>` or explicitly type children: `{ children?: React.ReactNode }`.
- Never use `any`. Prefer `unknown` and narrow with type guards.
- Use discriminated unions for complex state: `type ViewState = { kind: "loading" } | { kind: "error"; message: string } | { kind: "data"; items: Item[] }`.

## Hooks
- Custom hooks for reusable logic: `useWebSocket(url)`, `useLocalStorage<T>(key, initial)`.
- Effect dependencies must be explicit. Never suppress the linter with `// eslint-disable-next-line`.
- Use `useCallback` and `useMemo` only for referential stability passed to children. Profile first.
- Use `useReducer` for state with multiple fields or complex transitions.

## State management
- Start with React context + `useReducer`. Lift to zustand, Jotai, or Redux Toolkit only when prop drilling hurts.
- Keep state as low as possible. Derive values, don't store them: `const isValid = name.length > 0 && email.includes("@")`.
- Normalize nested data: store by ID, keep arrays of IDs for ordering.

## Performance
- `React.memo` for pure components that re-render often. Profile with React DevTools.
- Virtualize long lists with `react-window` or `@tanstack/react-virtual`.
- Lazy-load routes with `React.lazy(() => import("./HeavyPage"))`.
- Debounce search inputs and resize handlers. Throttle scroll handlers.

## Styling
- CSS Modules, Tailwind, or CSS-in-JS (emotion/styled-components). Be consistent within the project.
- Use CSS custom properties for theming: `--color-primary: #0066cc`.
- Responsive design: mobile-first with `min-width` breakpoints.

## Testing
- Test behavior not implementation: test what the user sees and does, not internal state.
- `@testing-library/react` for component tests. `userEvent` over `fireEvent`.
- Mock API calls at the network level with `msw` (Mock Service Worker).
- Integration tests for key flows (login, search, checkout). Unit test pure logic.

## Execution contract

This skill executes ONLY inside an Omega Auto dispatch (the qwen executor, jailed to the operator workspace). It MUST NOT create new top-level goals, MUST use only its allowed_tools, MUST NOT write canonical Omega memory, and MUST return structured evidence matching the output schema. Stop after success, a blocker, a safety denial, or the step/time limit.
