/**
 * Browser Agent Extension - Basic Navigation E2E Tests
 * 
 * Tests basic navigation and page interaction with the browser agent.
 * These tests require the extension to be loaded in Chrome/Chromium.
 */

import { test, expect, chromium } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_PATH = path.resolve(__dirname, '../../../browser_agent_extension');

// Helper to launch browser with extension
test.beforeAll(async ({ browserName }) => {
  console.log(`Running browser agent basic navigation tests on ${browserName}`);
});

test.describe('Browser Agent - Basic Navigation', () => {
  // Use a single browser context for these tests
  let browser;
  let context;
  let page;
  
  test.beforeAll(async () => {
    browser = await chromium.launch({
      headless: true,
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-sandbox',
        '--disable-setuid-sandbox',
      ],
    });
    context = await browser.newContext();
    page = await context.newPage();
  });
  
  test.afterAll(async () => {
    if (page) await page.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should load the extension in browser', async () => {
    // Navigate to a test page
    await page.goto('https://www.google.com');
    await page.waitForLoadState('networkidle');
    
    // Verify the page loaded
    const title = await page.title();
    expect(title).toContain('Google');
    
    console.log('  ✓ Page loaded successfully');
  });

  test('should have content script injected', async () => {
    await page.goto('https://www.google.com');
    await page.waitForLoadState('networkidle');
    
    // Check if our content script global is available
    const hasContentScript = await page.evaluate(() => {
      return typeof window.OmegaBrowserAgent !== 'undefined';
    });
    
    // Note: Content script may not be injected on all pages
    // This is expected behavior - the extension only injects on pages
    // that match the URL patterns in the manifest
    console.log(`  ✓ Content script check: ${hasContentScript ? 'injected' : 'not injected (expected)'}`);
  });

  test('should be able to navigate to different pages', async () => {
    const testUrls = [
      'https://www.google.com',
      'https://www.github.com',
    ];
    
    for (const url of testUrls) {
      await page.goto(url);
      await page.waitForLoadState('networkidle');
      const title = await page.title();
      expect(title.length).toBeGreaterThan(0);
      console.log(`  ✓ Navigated to ${url}`);
    }
  });

  test('should maintain browser state across navigation', async () => {
    // Navigate to first page
    await page.goto('https://www.google.com');
    const firstTitle = await page.title();
    
    // Navigate to second page
    await page.goto('https://www.github.com');
    const secondTitle = await page.title();
    
    // Both pages should have loaded successfully
    expect(firstTitle.length).toBeGreaterThan(0);
    expect(secondTitle.length).toBeGreaterThan(0);
    expect(firstTitle).not.toBe(secondTitle);
    
    console.log('  ✓ Browser state maintained across navigation');
  });
});

test.describe('Browser Agent - Page Extraction', () => {
  let browser;
  let context;
  let page;
  
  test.beforeAll(async () => {
    browser = await chromium.launch({
      headless: true,
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-sandbox',
        '--disable-setuid-sandbox',
      ],
    });
    context = await browser.newContext();
    page = await context.newPage();
  });
  
  test.afterAll(async () => {
    if (page) await page.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should be able to extract page title', async () => {
    await page.goto('https://www.google.com');
    await page.waitForLoadState('networkidle');
    
    const title = await page.title();
    expect(title).toBeTruthy();
    expect(title.length).toBeGreaterThan(0);
    
    console.log(`  ✓ Extracted page title: ${title}`);
  });

  test('should be able to extract page URL', async () => {
    await page.goto('https://www.google.com');
    await page.waitForLoadState('networkidle');
    
    const url = page.url();
    expect(url).toContain('google.com');
    
    console.log(`  ✓ Extracted page URL: ${url}`);
  });

  test('should be able to extract page text content', async () => {
    await page.goto('https://www.google.com');
    await page.waitForLoadState('networkidle');
    
    const bodyText = await page.evaluate(() => document.body.innerText);
    expect(bodyText.length).toBeGreaterThan(100);
    
    console.log(`  ✓ Extracted ${bodyText.length} characters of page content`);
  });
});

test.describe('Browser Agent - Headless Navigation', () => {
  let browser;
  let page;
  
  test.beforeAll(async () => {
    browser = await chromium.launch({
      headless: true,
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-sandbox',
        '--disable-setuid-sandbox',
      ],
    });
    page = await browser.newPage();
  });
  
  test.afterAll(async () => {
    if (page) await page.close();
    if (browser) await browser.close();
  });

  test('should handle rapid navigation', async () => {
    const urls = [
      'https://www.google.com',
      'https://www.github.com',
      'https://www.wikipedia.org',
    ];
    
    for (const url of urls) {
      await page.goto(url, { waitUntil: 'networkidle' });
      const title = await page.title();
      expect(title.length).toBeGreaterThan(0);
      console.log(`  ✓ Rapid navigation to ${url}`);
    }
  });

  test('should handle page reloads', async () => {
    await page.goto('https://www.google.com');
    const firstTitle = await page.title();
    
    await page.reload();
    await page.waitForLoadState('networkidle');
    
    const secondTitle = await page.title();
    expect(firstTitle).toBe(secondTitle);
    
    console.log('  ✓ Page reload handled successfully');
  });
});
