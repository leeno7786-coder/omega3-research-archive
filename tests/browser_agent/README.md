# Browser agent tests

Two different browser agents are tested from here, and their suites run differently.

## Omega's brain-side browser agent (Python, pytest)

The browser lane Omega itself drives: the DevTools controller, the bounded agent loop,
Edge bootstrap and HUD lifecycle.

| File | Covers |
|------|--------|
| `test_agent_loop.py` | `browser_agent/agent_loop.py`: one tool per turn, a screenshot frame after every action, budgets, notes mid-run |
| `test_browser_controller.py` | `browser_agent/controller.py`: the background task tab over CDP, input, page state, release |
| `test_bootstrap.py` | `browser_agent/bootstrap.py`: the portable Edge profile and debugger launch |
| `test_lifecycle.py` | `browser_agent/lifecycle.py`: HUD window startup |
| `test_js_harnesses.py` | runs the two JavaScript suites below inside the pytest sweep |

```powershell
.\.venv-brain\Scripts\python.exe -m pytest tests/browser_agent -q
```

## The Omega Browser Agent extension (JavaScript)

### Unit tests — `browser_agent_extension/tests/**/*.test.js` (bun)

These import the extension's real source (`src/shared/...`): the agent loop's action
parsing, the action validator, mouse actions, the vision flow, page extractors and the
manifest.

```powershell
cd browser_agent_extension
bun test tests
```

### Integration harness — `tests/browser_agent/integration/*.mjs` (node)

`node:test` suites built on the mock Chrome APIs in `conftest.mjs`. They exercise the
harness's mock contracts for the agent workflow, backend switching, content/background
messaging, the OCR pipeline and tab management. They do **not** import extension source,
so a change inside `browser_agent_extension/src` cannot fail them; the bun unit tests above
are what cover the extension's code.

```powershell
node tests/browser_agent/run_all.mjs
```

`run_all.mjs` runs every `*.mjs` it finds here and in `integration/` with `node <file>` and
treats exit 0 as a pass. It skips `conftest.mjs`, and it skips Playwright specs unless
`--e2e` is given.

### End-to-end — `tests/browser_agent/e2e/*.spec.js` (Playwright)

Launches Chromium with the built extension. Requires Playwright and its Chromium build.

```powershell
npm run test:browser-agent:e2e
```

## History

The unit-test `.mjs` files that once lived at the top of this directory
(`test_agent_loop.mjs`, `test_cdp.mjs`, `test_browser_automation.mjs` and 13 more) were
emptied in commit `f691dabc` and removed on 2026-09-15: `run_all.mjs` counted each empty
file as a passing test. Their former contents are in git history. `TEST_SUITE_PLAN.md` is
the original plan for that suite and is kept for reference only.

## Mock Chrome APIs (`conftest.mjs`)

```javascript
import { createMockChrome, createMockFetch } from './conftest.mjs';

const chrome = createMockChrome();
const fetch = createMockFetch();
global.chrome = chrome;
global.fetch = fetch;

chrome.tabs.create.mockImplementation(async (options) => ({ id: 1, url: options.url, active: false }));
```

`conftest.mjs` also provides `assertRejects`, `assertThrows`, `waitFor`, `delay` and HTML
fixtures under `testHtml` (`simplePage`, `searchResults`, `articlePage`, `productPage`,
`formPage`, `linksPage`, `tablePage`).
