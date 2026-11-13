/**
 * Playwright Configuration for Browser Agent Extension E2E Tests
 * 
 * This config is optimized for testing Chrome extensions.
 * Tests the extension's UI, popup, sidepanel, and content scripts.
 */

import { defineConfig, devices } from '@playwright/test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Browser Agent Extension path
 * Update this path if the extension directory structure changes
 */
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const EXTENSION_PATH = resolve(ROOT, 'browser_agent_extension');

/**
 * HUD server for testing the full Omega system
 */
const HUD_BASE_URL = 'http://127.0.0.1:13310';

/**
 * Test URL for browser agent testing
 */
const TEST_URL = 'http://example.com';

export default defineConfig({
  // Test directory for browser agent E2E tests
  testDir: resolve(ROOT, 'tests', 'browser_agent', 'e2e'),
  
  // Run tests in files in parallel
  fullyParallel: false,
  
  // Fail the build on CI if test.only is left in the source code
  forbidOnly: !!process.env.CI,
  
  // Retry on CI only
  retries: process.env.CI ? 2 : 0,
  
  // Opt out of parallel tests on CI
  workers: process.env.CI ? 1 : undefined,
  
  // Reporter configuration
  reporter: [
    ['list'],
    ['html', { outputFolder: '../../../../playwright-report/browser-agent', open: 'never' }],
    ['json', { outputFile: '../../../../test-results/browser-agent-results.json' }],
  ],
  
  // Global timeout settings
  timeout: 60000, // 60 seconds per test
  expect: {
    timeout: 10000, // 10 seconds for expect assertions
  },
  
  // Shared settings for all projects
  use: {
    // Base URL for navigation
    baseURL: HUD_BASE_URL,
    
    // Collect trace when retrying failed tests
    trace: 'on-first-retry',
    
    // Capture screenshot after each test failure
    screenshot: 'only-on-failure',
    
    // Record video only when retrying
    video: 'retain-on-failure',
    
    // Headless mode (set to false for debugging)
    headless: true,
    
    // Slow down operations for debugging (ms)
    // slowMo: 100,
    
    // Viewport size
    viewport: { width: 1280, height: 800 },
    
    // Chrome-specific settings for extension testing
    launchOptions: {
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-web-security',
        '--allow-running-insecure-content',
      ],
    },
  },
  
  // Configure projects for different browser contexts
  projects: [
    {
      name: 'chromium-extension',
      use: {
        ...devices['Desktop Chromium'],
        // Chrome extension specific launch options
        launchOptions: {
          args: [
            `--disable-extensions-except=${EXTENSION_PATH}`,
            `--load-extension=${EXTENSION_PATH}`,
            '--no-sandbox',
            '--disable-setuid-sandbox',
          ],
        },
      },
    },
    {
      name: 'chromium-regular',
      use: {
        ...devices['Desktop Chromium'],
        // Regular browser without extension for comparison
      },
    },
  ],
  
  // Web server configuration
  webServer: process.env.CI 
    ? undefined
    : {
        command: `"${resolve(ROOT, '.venv-brain', 'Scripts', 'python.exe')}" -m hud.standalone --no-boot`,
        url: 'http://127.0.0.1:13310/healthz',
        timeout: 120 * 1000, // 120 seconds timeout for server startup
        reuseExistingServer: !process.env.CI,
      },
  
  // Global setup (for things like installing the extension)
});
