# E2E Tests (Playwright)

This directory contains end-to-end tests for the Omega 3.0 HUD interface using [Playwright](https://playwright.dev/).

## Structure

```
tests/e2e/
├── README.md                    # This file
├── playwright.config.ts         # Playwright configuration
├── hud_smoke.spec.ts            # Basic smoke tests for HUD
├── hud_page_object.spec.ts      # Tests using page object pattern
└── pages/
    └── HudPage.ts               # Page object for HUD interface
```

## Prerequisites

- Node.js (v18+ recommended)
- npm or yarn
- Python (for the HUD server)

## Setup

Playwright is already installed as a dev dependency:

```bash
npm install
```

This will install:
- `@playwright/test` - The Playwright test runner
- `dotenv` - For environment variable loading

## Running Tests

### Basic Commands

```bash
# Run all E2E tests
npm run test:e2e

# Run with UI mode (interactive)
npm run test:e2e:ui

# Run with headed browsers (visible)
npm run test:e2e:headed

# Run for a specific browser
npm run test:e2e:chromium
npm run test:e2e:firefox
npm run test:e2e:webkit

# Run with debug mode
npm run test:e2e:debug
```

### Playwright CLI

You can also use the Playwright CLI directly:

```bash
# Run tests
npx playwright test

# Run specific test file
npx playwright test tests/e2e/hud_smoke.spec.ts

# Run with headed mode
npx playwright test --headed

# Run specific project (browser)
npx playwright test --project=chromium

# Generate tests interactively
npx playwright codegen http://localhost:13310

# Show test report
npx playwright show-report playwright-report
```

## Configuration

The Playwright configuration is in `playwright.config.ts`:

- **Test Directory**: `./tests/e2e`
- **Base URL**: `http://127.0.0.1:13310` (HUD default port)
- **Web Server**: Automatically starts `.\\.venv-brain\\Scripts\\python.exe -m hud.standalone --no-boot` for local testing
- **Browsers**: Chromium, Firefox, WebKit, Mobile Chrome, Mobile Safari, Microsoft Edge
- **Retries**: 2 retries on CI, 0 locally
- **Artifacts**: HTML report, JSON results, screenshots on failure, traces on retry

### Environment Variables

You can override the base URL:

```bash
HUD_BASE_URL=http://localhost:13311 npm run test:e2e
```

In CI, the webServer is disabled and you should start the HUD server manually.

## Writing Tests

### Using Page Object Pattern (Recommended)

```typescript
import { test } from '@playwright/test';
import { HudPage } from './pages/HudPage';

test('page loads correctly', async ({ page }) => {
  const hudPage = new HudPage(page);
  await hudPage.goto();
  await hudPage.waitForLoad();
  
  const title = await hudPage.getTitle();
  expect(title).toMatch(/OMEGA/);
});
```

### Direct Selectors

```typescript
import { test, expect } from '@playwright/test';

test('homepage loads', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/OMEGA/);
  
  const canvas = page.locator('canvas');
  await expect(canvas).toBeVisible();
});
```

## Page Objects

The `pages/` directory contains reusable page objects that encapsulate selectors and actions:

- **HudPage**: Main HUD interface page object with methods for:
  - Navigation
  - Loading verification
  - Structure verification
  - API endpoint checks
  - Theme verification
  - Screenshot capture

## Test Organization

### Test Suites

1. **hud_smoke.spec.ts** - Basic smoke tests
   - Homepage loading
   - Health check endpoint
   - API endpoints
   - Page structure

2. **hud_page_object.spec.ts** - Page object pattern tests
   - Demonstrates using the HudPage class
   - All panel endpoints
   - Screenshot capture

### Adding New Tests

1. Create a new `.spec.ts` file in the `tests/e2e/` directory
2. Import `@playwright/test` and any page objects
3. Write tests using the `test()` function
4. Use `expect()` for assertions

## Best Practices

### Selectors

- Use `data-testid` attributes for elements you want to test
- Avoid CSS classes that might change
- Use semantic selectors when possible

```typescript
// Good - stable selector
page.locator('[data-testid="submit-button"]')

// Avoid - brittle selector
page.locator('.btn-primary')
```

### Test Isolation

- Each test should be independent
- Use `test.beforeEach` for setup
- Use `test.afterEach` for cleanup

### Timeouts

- Use appropriate timeouts for async operations
- Prefer Playwright's auto-waiting

```typescript
// Good - Playwright auto-waits
await expect(page.locator('.loaded')).toBeVisible();

// Avoid - hardcoded waits
await page.waitForTimeout(5000);
```

## CI Integration

For GitHub Actions, add a step like:

```yaml
- name: Install Playwright browsers
  run: npx playwright install --with-deps

- name: Run E2E tests
  run: npm run test:e2e
  env:
    CI: true
    HUD_BASE_URL: http://localhost:13310
```

## Troubleshooting

### Browser Installation

If browsers aren't installed:

```bash
npx playwright install
```

### Port Conflicts

If port 13310 is in use, override it:

```bash
HUD_BASE_URL=http://localhost:13311 npm run test:e2e
```

Or update the webServer configuration in `playwright.config.ts`.

### Debugging Tests

```bash
# Run with debug mode
npx playwright test --debug

# Run with headed mode to see the browser
npx playwright test --headed

# Open the HTML report
npx playwright show-report playwright-report
```

## Artifacts

Test artifacts are saved in:

- `test-results/` - Test results (JSON, traces)
- `playwright-report/` - HTML report
- `playwright-videos/` - Video recordings (if enabled)

These are all gitignored and won't be committed.

## Resources

- [Playwright Documentation](https://playwright.dev/docs/intro)
- [Playwright Test API](https://playwright.dev/docs/test-api)
- [Page Object Pattern](https://playwright.dev/docs/pom)
