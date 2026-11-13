# Browser Agent Extension - Comprehensive Test Suite Plan

## Progress Summary (2026-07-16)

**✅ COMPLETED:**
- Phase 1: Core Unit Tests
  - `test_storage_manager.mjs` - 14 tests for storage abstraction
  - `test_base_backend.mjs` - 17 tests for base backend
  - `test_inference_engine.mjs` - 26 tests for inference engine
  - `test_model_manager.mjs` - 24 tests for model manager
  - `test_agent_loop.mjs` - 22 tests for agent loop
- Phase 2: Backend-Specific Tests
  - `test_backends/test_lmstudio_backend.mjs` - 18 tests for LM Studio backend
  - `test_backends/test_omega_portable_backend.mjs` - 18 tests for Omega Portable backend
- Phase 3: Browser Automation Tests
  - `test_browser_automation.mjs` - 16 tests for browser automation
- Phase 4: Integration Tests
  - `integration/test_agent_workflow.mjs` - AgentLoop + BrowserAutomation integration
  - `integration/test_backend_integration.mjs` - Model Manager + Backend integration
  - `integration/test_content_background.mjs` - Content-Background messaging
  - `integration/test_ocr_pipeline.mjs` - OCR pipeline with screenshot capture
  - `integration/test_tab_management.mjs` - Workspace tab orchestration
- Phase 5: Additional Unit Tests
  - `test_cloud_vision.mjs` - Cloud OCR and vision capabilities
  - `test_fetch_proxy.mjs` - Fetch abstraction layer
- Phase 6: E2E Tests with Playwright
  - `e2e/test_basic_navigation.spec.js` - Basic navigation and page interaction
  - `e2e/test_extraction_workflow.spec.js` - Page content extraction workflows
  - `e2e/test_multi_tab.spec.js` - Multi-tab navigation scenarios
  - `e2e/test_settings_persistence.spec.js` - Settings persistence across sessions
  - `e2e/test_ocr_vision.spec.js` - OCR and vision capabilities

**📊 Total New Tests Created:** ~250+ tests across 19 new test files

**✅ FIXED:**
- `test_agent_loop_response_action.mjs` - Added missing `isModelLoaded` mock method
- `test_base_backend.mjs` - Fixed async/await issues with `getAvailableModels` and `getConnectionInfo`
- `test_inference_engine.mjs` - Fixed assertions for `isModelLoaded` and `updateModelParams`
- `test_model_manager.mjs` - Fixed assertions for `setEngineConfig` and `migrateSettings`
- `test_agent_loop.mjs` - Fixed const reassignment, async tests, and assertion expectations

**⚠️ KNOWN ISSUES:**
- `test_openrouter_backend.mjs` - ResponseValidationError fallback not properly implemented/handled

**✅ ALL PLANNED TESTS COMPLETED**

All test files from the original plan have been created:
- 8 Unit test files (including 2 new: cloud_vision, fetch_proxy)
- 5 Integration test files
- 5 E2E test files
- Infrastructure: conftest.mjs, run_all.mjs, README.md, TEST_SUITE_PLAN.md

---

## Overview

This document outlines a comprehensive test suite for the Omega Browser Agent extension. The suite covers unit tests, integration tests, and end-to-end tests across all components of the browser agent.

## Test Categories

### 1. Unit Tests (Node.js MJS)
- **Location**: `tests/browser_agent/`
- **Runner**: Node.js with native test runner or mocha
- **Focus**: Individual functions and modules in isolation

### 2. Integration Tests (Node.js MJS)
- **Location**: `tests/browser_agent/integration/`
- **Focus**: Module interactions, messaging between components

### 3. End-to-End Tests (Playwright)
- **Location**: `tests/browser_agent/e2e/`
- **Focus**: Full extension behavior in a real browser environment

---

## Test Suite Structure

```
tests/browser_agent/
├── README.md                    # Test suite documentation
├── TEST_SUITE_PLAN.md           # This file
├── conftest.mjs                # Shared test fixtures and setup
├── run_all.mjs                 # Test runner script
│
├── # Unit Tests
├── test_action_validator.mjs   # ✅ EXISTS (70 lines, good coverage)
├── test_agent_loop.mjs         # ✅ NEW: Core agent loop tests (22 tests)
├── test_agent_loop_prompt_guidance.mjs  # ✅ EXISTS
├── test_agent_loop_response_action.mjs  # ✅ EXISTS (fixed mock)
├── test_agent_loop_vision.mjs  # ✅ EXISTS
├── test_backends/
│   ├── test_base_backend.mjs   # ✅ NEW: Base backend abstract class (17 tests)
│   ├── test_lmstudio_backend.mjs # ✅ NEW: LM Studio backend (18 tests)
│   ├── test_openrouter_backend.mjs # ⚠️ EXISTS (needs investigation)
│   └── test_omega_portable_backend.mjs # ✅ NEW: Omega Portable backend (18 tests)
│
├── test_browser_automation.mjs # ✅ NEW: Core automation logic (16 tests)
├── test_cloud_vision.mjs        # ⏳ NEW: Cloud vision/OCR (not yet created)
├── test_content_click_hints.mjs # ✅ EXISTS
├── test_fetch_proxy.mjs        # ⏳ NEW: Fetch proxy layer (not yet created)
├── test_inference_engine.mjs   # ✅ NEW: Inference engine (26 tests)
├── test_model_manager.mjs      # ✅ NEW: Model lifecycle (24 tests)
├── test_ocr_service.mjs         # ✅ EXISTS
├── test_page_extractors.mjs    # ✅ EXISTS
├── test_storage_manager.mjs    # ✅ NEW: Storage abstraction (14 tests)
│
├── # Integration Tests
├── integration/
│   ├── test_agent_workflow.mjs # NEW: Agent loop + automation flow
│   ├── test_backend_integration.mjs # NEW: Backend + model manager
│   ├── test_content_background.mjs # NEW: Content <-> Background messaging
│   ├── test_ocr_pipeline.mjs   # NEW: OCR service + offscreen
│   └── test_tab_management.mjs # NEW: Workspace tab orchestration
│
└── # End-to-End Tests
    └── e2e/
        ├── test_basic_navigation.spec.js # Playwright E2E
        ├── test_extraction_workflow.spec.js
        ├── test_multi_tab.spec.js
        ├── test_settings_persistence.spec.js
        └── test_ocr_vision.spec.js
```

---

## Detailed Test Specifications

### 1. Unit Tests

#### 1.1 Action Validator (`test_action_validator.mjs`)
**Status**: ✅ EXISTS (70 lines, good coverage)

**Additional tests needed**:
- [ ] Edge cases for URL validation (international domains, ports, special chars)
- [ ] ASK action with mixed question formats
- [ ] All confirmation action types
- [ ] Invalid action type handling
- [ ] Max length boundaries for all string fields

#### 1.2 Agent Loop (`test_agent_loop.mjs`)
**Status**: ❌ MISSING

**Test Cases**:
```javascript
// Core functionality
- parseAction: JSON in fenced blocks
- parseAction: JSON inline in prose
- parseAction: Partial/truncated JSON recovery
- parseAction: Multiple actions (take first)
- parseAction: No action found
- parsePartialAction: Incomplete JSON extraction
- containsAction: Detection in various formats

// Action execution
- executeAction: All 21 action types
- executeAction: Action validation failures
- executeAction: Confirmation flow
- executeAction: ASK user interaction

// Planning
- buildTaskPlan: Clear task -> stages
- buildTaskPlan: Ambiguous task -> questions + stages
- buildTaskPlan: User answers integration
- buildTaskPlan: Plan failure fallback

// Loop control
- runTask: Step counting and limits
- runTask: Abort handling
- runTask: Error recovery
- runTask: Image pruning in history
- runTask: Token counting warnings
- runTask: History trimming

// System prompt
- buildSystemPrompt: Page state integration
- buildSystemPrompt: All extraction modes
- buildSystemPrompt: Interactive elements listing

// Utilities
- inferNavigateFromTask: URL detection
- inferNavigateFromTask: Domain shortcuts
- inferNavigateFromTask: No navigation needed
- formatActionResultContext: All action types
- normalizeAction: Case normalization
```

**File**: `tests/browser_agent/test_agent_loop.mjs` (NEW)

#### 1.3 Backends

**Base Backend (`test_base_backend.mjs`)**
**Status**: ❌ MISSING

```javascript
// BaseBackend abstract class
- Constructor: Default config
- Constructor: Custom config override
- getType: Abstract method enforcement
- getName: Abstract method enforcement
- applyConfig: Partial config updates
- loadModel: Abstract method
- chatCompletion: Abstract method
- isModelLoaded: Default implementation
- getConnectionInfo: Default structure
```

**OpenRouter Backend (`test_openrouter_backend.mjs`)**
**Status**: ✅ EXISTS (109 lines, good coverage)

**Additional tests needed**:
- [ ] extractOpenRouterModelIds: Edge cases (null, empty, non-array)
- [ ] resolveOpenRouterModelId: Partial matches
- [ ] resolveOpenRouterModelId: Case sensitivity
- [ ] formatOpenRouterError: All error types
- [ ] isRateLimitError: All rate limit indicators
- [ ] retryAfterMs: Header parsing (seconds, date)
- [ ] withOpenRouterRetries: Retry logic
- [ ] normalizeBaseUrl: Various URL formats
- [ ] normalizeMessageContent: All content types

**LM Studio Backend (`test_lmstudio_backend.mjs`)**
**Status**: ❌ MISSING

```javascript
// LMStudioBackend class
- Constructor: Default config (localhost:1234)
- getType: Returns 'lmstudio'
- getName: Returns 'LM Studio'
- checkConnection: Success path
- checkConnection: Failure path (timeout, refused)
- loadModel: Model loading flow
- chatCompletion: Stream handling
- chatCompletion: Error recovery
- fetchModels: Model list retrieval
- applyConfig: Server URL updates
```

**Omega Portable Backend (`test_omega_portable_backend.mjs`)**
**Status**: ❌ MISSING

```javascript
// OmegaPortableBackend class
- Constructor: Default config (localhost:11434)
- getType: Returns 'omega-portable'
- getName: Returns 'Omega-Portable'
- checkConnection: Success path
- checkConnection: Failure path
- loadModel: Model loading with polling
- chatCompletion: Streaming and non-streaming
- getAvailableModels: Model discovery
- applyConfig: Server configuration
```

#### 1.4 Browser Automation (`test_browser_automation.mjs`)
**Status**: ❌ MISSING

```javascript
// Tab management
- _saveTabRegistry: Persistence
- _loadTabRegistry: Loading from storage
- _registerWorkspaceTab: Duplicate prevention
- setAgentTab: Agent tab tracking
- handleTabRemoved: Cleanup logic
- listWorkspaceTabs: Alive tab filtering
- _resolveTabIndex: Validation
- openWorkspaceTab: New tab creation
- switchWorkspaceTab: Tab switching
- closeWorkspaceTab: Tab removal
- clearAgentTab: Reset state

// Navigation
- navigate: URL navigation
- _waitForTabComplete: Completion detection
- getWorkspaceUrl: URL resolution
- isWorkspaceUrl: URL matching
- isWorkspaceTab: Tab classification

// Lock management
- setLock: Lock state
- isLocked: Lock checking

// Automation execution
- click: Element clicking
- clickXY: Coordinate clicking
- type: Text input
- scroll: Page scrolling
- extract: Content extraction
- capturePageVision: Screenshot + OCR
- scan: Multi-screenshot capture
- zoom: Zoom level
- wait: Element waiting
- keyboard: Key sending
- listWorkspaceTabs: Tab listing
- openWorkspaceTab: Tab opening
- switchWorkspaceTab: Tab switching
- closeWorkspaceTab: Tab closing

// State management
- getPageState: Current page info
- getDOMTree: DOM structure
- ensureContentScriptReady: Injection check
- autoOcrIfNeeded: Auto OCR trigger
```

#### 1.5 Cloud Vision (`test_cloud_vision.mjs`)
**Status**: ❌ MISSING

```javascript
// Cloud OCR
- cloudRecognizeScreenshot: Mistral OCR
- cloudRecognizeScreenshot: Generic cloud OCR
- DEFAULT_CLOUD_OCR_MODEL: Constant
- DEFAULT_MISTRAL_OCR_MODEL: Constant

// Vision utilities
- mistralRecognizeScreenshot: API call
- mistralRecognizeScreenshot: Error handling
```

#### 1.6 Fetch Proxy (`test_fetch_proxy.mjs`)
**Status**: ❌ MISSING

```javascript
// Fetch abstraction
- createFetchProxy: Proxy creation
- fetchWithTimeout: Timeout handling
- fetchWithRetry: Retry logic
- handleFetchError: Error formatting
```

#### 1.7 Inference Engine (`test_inference_engine.mjs`)
**Status**: ❌ MISSING

```javascript
// Engine configuration
- PRESETS: Default configurations
- createInferenceEngine: Engine creation
- Engine chat: Completion generation
- Engine stream: Streaming generation
- Engine loadModel: Model loading
- Engine unloadModel: Model unloading

// Model parameters
- applyModelParams: Parameter merging
- validateModelParams: Parameter validation
```

#### 1.8 Model Manager (`test_model_manager.mjs`)
**Status**: ❌ MISSING

```javascript
// Model lifecycle
- ModelManager constructor: Default state
- loadModel: Loading flow
- unloadModel: Unloading flow
- switchModel: Model switching
- reloadModel: Reconnection
- getModel: Current model state
- isModelLoaded: Loaded check
- isLoading: Loading check

// Backend management
- setBackend: Backend switching
- getBackend: Current backend
- getAvailableBackends: Backend listing

// Error handling
- handleModelError: Error formatting
- handleConnectionError: Connection issues
```

#### 1.9 OCR Service (`test_ocr_service.mjs`)
**Status**: ✅ EXISTS (basic tests)

**Additional tests needed**:
- [ ] recognizeScreenshot: Provider chain (auto -> mistral -> cloud -> local)
- [ ] localRecognizeScreenshot: Offscreen OCR
- [ ] ensureOcrOffscreen: Document creation
- [ ] mergeVisionText: DOM + OCR merging
- [ ] mergeVisionText: Conflict resolution

#### 1.10 Page Extractors (`test_page_extractors.mjs`)
**Status**: ✅ EXISTS (44 lines, basic coverage)

**Additional tests needed**:
- [ ] detectPageKind: All page types (search, article, product, etc.)
- [ ] formatExtractionForPrompt: All extraction modes
- [ ] extractMeta: Metadata extraction
- [ ] extractSearchResults: Results parsing
- [ ] extractMainContent: Article extraction
- [ ] extractTables: Table parsing
- [ ] extractPriorityLinks: Link extraction
- [ ] runPageExtraction: Full extraction pipeline

#### 1.11 Storage Manager (`test_storage_manager.mjs`)
**Status**: ❌ MISSING

```javascript
// Storage abstraction
- StorageManager constructor: Default options
- get: Value retrieval
- get: Default values
- set: Value storage
- set: Error handling
- remove: Value deletion
- clear: Full clearance
- getAll: All keys retrieval
- onChanged: Change events (mocked)
```

### 2. Integration Tests

#### 2.1 Agent Workflow (`integration/test_agent_workflow.mjs`)
**Status**: ❌ MISSING

```javascript
// Full agent loop
- AgentLoop + BrowserAutomation integration
- Action execution -> State update flow
- Planning -> Execution -> Result cycle
- Error propagation through layers
- History management across steps

// Multi-step scenarios
- Navigate + Extract workflow
- Click + Wait + Verify workflow
- ASK + Continue workflow
- Tab management workflow
```

#### 2.2 Backend Integration (`integration/test_backend_integration.mjs`)
**Status**: ❌ MISSING

```javascript
// Model Manager + Backend
- Model loading through backend
- Chat completion flow
- Error handling across layers
- Configuration propagation

// Backend switching
- OpenRouter -> LM Studio switch
- Local -> Cloud switch
- Error recovery during switch
```

#### 2.3 Content-Background Messaging (`integration/test_content_background.mjs`)
**Status**: ❌ MISSING

**Note**: Requires Chrome API mocking

```javascript
// Message passing (mocked chrome.runtime)
- Automation command execution
- Page state retrieval
- Screenshot capture
- OCR requests
- Lock state synchronization

// Error handling
- Message timeout handling
- Invalid message handling
- Tab not found handling
```

#### 2.4 OCR Pipeline (`integration/test_ocr_pipeline.mjs`)
**Status**: ❌ MISSING

```javascript
// Full OCR flow
- Screenshot capture -> OCR service
- Provider fallback chain
- Result merging with DOM
- Error recovery

// Offscreen integration
- Offscreen document lifecycle
- OCR worker communication
- Resource cleanup
```

#### 2.5 Tab Management (`integration/test_tab_management.mjs`)
**Status**: ❌ MISSING

```javascript
// Workspace tab orchestration
- New tab creation
- Tab switching
- Tab closing
- Tab state persistence
- Tab removal handling

// Multi-tab scenarios
- Cross-tab communication
- Active tab tracking
- Workspace isolation
```

### 3. End-to-End Tests (Playwright)

#### 3.1 Basic Navigation (`e2e/test_basic_navigation.spec.js`)
**Status**: ❌ MISSING

```javascript
// Extension loading
- Extension loads in Edge
- Side panel opens
- Settings panel accessible

// Basic navigation
- NAVIGATE action executes
- URL changes in workspace tab
- Page loads successfully
- Title updates

// Error handling
- Invalid URL handling
- Navigation timeout
- Connection refused
```

#### 3.2 Extraction Workflow (`e2e/test_extraction_workflow.spec.js`)
**Status**: ❌ MISSING

```javascript
// Page content extraction
- EXTRACT action with mode: main
- EXTRACT action with mode: searchResults
- EXTRACT action with mode: article
- EXTRACT action with mode: meta
- EXTRACT action with mode: table
- EXTRACT action with mode: links
- EXTRACT action with selector

// Result verification
- Extracted content accuracy
- Structured data parsing
- Error handling for invalid selectors
```

#### 3.3 Multi-Tab Workflow (`e2e/test_multi_tab.spec.js`)
**Status**: ❌ MISSING

```javascript
// Tab operations
- TAB_NEW opens new workspace tab
- TAB_LIST returns correct tabs
- TAB_SWITCH changes active tab
- TAB_CLOSE closes tab
- TAB_CLOSE prevents closing last tab

// Cross-tab scenarios
- Independent navigation in tabs
- State isolation between tabs
- Switch and continue workflow
```

#### 3.4 Settings Persistence (`e2e/test_settings_persistence.spec.js`)
**Status**: ❌ MISSING

```javascript
// Settings changes
- Backend type switch
- Server URL update
- Model identifier update
- API key storage
- Confirmation action toggle

// Persistence verification
- Settings survive page reload
- Settings survive extension restart
- Settings validation
```

#### 3.5 OCR Vision (`e2e/test_ocr_vision.spec.js`)
**Status**: ❌ MISSING

```javascript
// Vision capabilities
- SS action captures screenshot
- OCR action extracts text
- SCAN action captures multiple screenshots
- Auto OCR on page load

// Result verification
- OCR text accuracy
- Screenshot data URL format
- UI element map generation
- Vision fallback chain
```

---

## Test Infrastructure

### 1. Test Runner (`run_all.mjs`)

```javascript
// Run all browser agent tests
import { runTests } from './test_runner.mjs';

const suites = [
  // Unit tests
  './test_action_validator.mjs',
  './test_agent_loop.mjs',
  './test_backends/test_base_backend.mjs',
  './test_backends/test_lmstudio_backend.mjs',
  './test_backends/test_openrouter_backend.mjs',
  './test_backends/test_omega_portable_backend.mjs',
  './test_browser_automation.mjs',
  './test_cloud_vision.mjs',
  './test_content_click_hints.mjs',
  './test_fetch_proxy.mjs',
  './test_inference_engine.mjs',
  './test_model_manager.mjs',
  './test_ocr_service.mjs',
  './test_page_extractors.mjs',
  './test_storage_manager.mjs',
  
  // Integration tests
  './integration/test_agent_workflow.mjs',
  './integration/test_backend_integration.mjs',
  './integration/test_content_background.mjs',
  './integration/test_ocr_pipeline.mjs',
  './integration/test_tab_management.mjs',
];

runTests(suites);
```

### 2. Shared Fixtures (`conftest.mjs`)

```javascript
// Mock Chrome APIs
export const mockChrome = {
  runtime: {
    sendMessage: vi.fn(),
    onMessage: {
      addListener: vi.fn(),
      removeListener: vi.fn(),
    },
    getURL: vi.fn((path) => path),
  },
  tabs: {
    create: vi.fn(),
    get: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    onUpdated: {
      addListener: vi.fn(),
      removeListener: vi.fn(),
    },
    onRemoved: {
      addListener: vi.fn(),
      removeListener: vi.fn(),
    },
  },
  offscreen: {
    createDocument: vi.fn(),
    hasDocument: vi.fn(),
  },
  storage: {
    local: {
      get: vi.fn(),
      set: vi.fn(),
      remove: vi.fn(),
      clear: vi.fn(),
      onChanged: {
        addListener: vi.fn(),
      },
    },
  },
  scripting: {
    executeScript: vi.fn(),
  },
};

// Mock fetch
export const mockFetch = vi.fn();

// Test HTML fixtures
export const testHtml = {
  searchResults: `<html>...</html>`,
  article: `<html>...</html>`,
  productPage: `<html>...</html>`,
};

// Before each test
export function setup() {
  global.chrome = mockChrome;
  global.fetch = mockFetch;
  
  // Reset all mocks
  vi.resetAllMocks();
}

// After each test
export function teardown() {
  delete global.chrome;
  delete global.fetch;
}
```

### 3. Chrome API Mocking Strategy

For tests that require Chrome extension APIs, we'll use a mocking approach:

```javascript
// In test files that need Chrome APIs
import { mockChrome } from './conftest.mjs';

// Setup before tests
beforeEach(() => {
  global.chrome = { ...mockChrome };
});

afterEach(() => {
  delete global.chrome;
});
```

For more complex scenarios, consider using:
- **sinon-chrome**: A library specifically for mocking Chrome APIs
- **jest-environment-node**: For Node.js test environment
- **Custom mock implementations**: For specific Chrome API behaviors

---

## Test Execution Strategy

### Local Development

```bash
# Run all browser agent tests
node tests/browser_agent/run_all.mjs

# Run specific test
node tests/browser_agent/test_agent_loop.mjs

# Run with Node.js test runner (if available)
node --test tests/browser_agent/
```

### CI/CD Integration

```yaml
# .github/workflows/browser-agent-tests.yml
name: Browser Agent Tests

on:
  push:
    paths:
      - 'browser_agent_extension/**'
      - 'tests/browser_agent/**'
  pull_request:
    paths:
      - 'browser_agent_extension/**'
      - 'tests/browser_agent/**'

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - run: npm ci
      - run: node tests/browser_agent/run_all.mjs

  e2e-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - uses: microsoft/playwright-github-action@v1
      - run: npm install -D playwright
      - run: npx playwright install
      - run: npx playwright test tests/browser_agent/e2e/
```

---

## Test Coverage Goals

| Component | Unit Tests | Integration Tests | E2E Tests | Current Coverage |
|-----------|------------|------------------|------------|------------------|
| action-validator.js | ✅ 70% | ❌ 0% | ❌ 0% | 70% |
| agent-loop.js | ❌ 0% | ❌ 0% | ❌ 0% | 0% |
| browser-automation.js | ❌ 0% | ❌ 0% | ❌ 0% | 0% |
| backends/ | ✅ 50% | ❌ 0% | ❌ 0% | 25% |
| page-extractors.js | ✅ 30% | ❌ 0% | ❌ 0% | 30% |
| ocr-service.js | ✅ 20% | ❌ 0% | ❌ 0% | 20% |
| content.js | ❌ 0% | ❌ 0% | ❌ 0% | 0% |
| background.js | ❌ 0% | ❌ 0% | ❌ 0% | 0% |
| model-manager.js | ❌ 0% | ❌ 0% | ❌ 0% | 0% |
| storage-manager.js | ❌ 0% | ❌ 0% | ❌ 0% | 0% |
| inference-engine.js | ❌ 0% | ❌ 0% | ❌ 0% | 0% |
| sidepanel/ | ❌ 0% | ❌ 0% | ❌ 0% | 0% |

**Target Coverage**:
- Unit Tests: 80%+ of all functions
- Integration Tests: All major workflows
- E2E Tests: All user-facing features

---

## Priority Implementation Order

### Phase 1: Core Unit Tests (High Priority)
1. `test_agent_loop.mjs` - Core agent logic
2. `test_browser_automation.mjs` - Automation foundation
3. `test_model_manager.mjs` - Model lifecycle
4. `test_storage_manager.mjs` - Storage abstraction

### Phase 2: Backend Unit Tests (High Priority)
5. `test_base_backend.mjs`
6. `test_lmstudio_backend.mjs`
7. `test_omega_portable_backend.mjs`

### Phase 3: Remaining Unit Tests (Medium Priority)
8. `test_cloud_vision.mjs`
9. `test_fetch_proxy.mjs`
10. `test_inference_engine.mjs`

### Phase 4: Integration Tests (Medium Priority)
11. `integration/test_agent_workflow.mjs`
12. `integration/test_backend_integration.mjs`
13. `integration/test_tab_management.mjs`

### Phase 5: E2E Tests (Medium Priority)
14. `e2e/test_basic_navigation.spec.js`
15. `e2e/test_extraction_workflow.spec.js`
16. `e2e/test_settings_persistence.spec.js`

---

## Success Criteria

1. **All existing tests pass** - No regressions
2. **New tests add value** - Each test covers untested code or edge cases
3. **Tests are maintainable** - Clear structure, good naming, minimal duplication
4. **Tests run quickly** - Unit tests < 1s each, full suite < 30s
5. **High coverage** - 80%+ line coverage for all modules

---

## Files to Create

### Immediate (Phase 1)
- [ ] `tests/browser_agent/conftest.mjs`
- [ ] `tests/browser_agent/run_all.mjs`
- [ ] `tests/browser_agent/test_agent_loop.mjs`
- [ ] `tests/browser_agent/test_browser_automation.mjs`
- [ ] `tests/browser_agent/test_model_manager.mjs`
- [ ] `tests/browser_agent/test_storage_manager.mjs`

### Phase 2
- [ ] `tests/browser_agent/test_backends/test_base_backend.mjs`
- [ ] `tests/browser_agent/test_backends/test_lmstudio_backend.mjs`
- [ ] `tests/browser_agent/test_backends/test_omega_portable_backend.mjs`

### Phase 3
- [ ] Remaining unit tests
- [ ] Integration tests
- [ ] E2E test infrastructure

---

## Notes

1. **Chrome API Dependency**: Many modules depend on Chrome extension APIs (chrome.runtime, chrome.tabs, etc.). Tests requiring these APIs must either:
   - Mock the APIs (for unit tests)
   - Use a real browser environment (for E2E tests)
   - Be skipped in Node.js environment

2. **ES Modules**: All browser agent code uses ES modules. Tests must also use ES modules (.mjs extension) and use `import/export` syntax.

3. **Node.js Compatibility**: Tests run in Node.js but test browser code. Use `node:assert` or testing libraries that work with ES modules.

4. **Build Process**: The extension has a build process (`build-edge-extension.ps1`). Tests should run against source files, not built files.

5. **Dependencies**: The extension uses Tesseract.js for OCR. Tests may need access to the vendor directory or mock OCR functionality.
