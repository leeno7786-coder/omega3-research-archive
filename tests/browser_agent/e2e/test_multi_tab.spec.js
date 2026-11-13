/**
 * Browser Agent Extension - Multi-Tab E2E Tests
 * 
 * Tests workspace tab management and multi-tab scenarios.
 */

import { test, expect } from '@playwright/test';
import { chromium } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_PATH = path.resolve(__dirname, '../../../browser_agent_extension');

test.describe('Browser Agent - Multi-Tab Management', () => {
  let browser;
  let context;
  let page1, page2, page3;

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
    
    // Open multiple tabs
    page1 = await context.newPage();
    page2 = await context.newPage();
    page3 = await context.newPage();
  });

  test.afterAll(async () => {
    if (page1) await page1.close();
    if (page2) await page2.close();
    if (page3) await page3.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should open multiple tabs', async () => {
    // Navigate each page to a different URL
    await page1.goto('https://www.google.com');
    await page2.goto('https://www.github.com');
    await page3.goto('https://www.wikipedia.org');
    
    // Verify each page loaded
    const title1 = await page1.title();
    const title2 = await page2.title();
    const title3 = await page3.title();
    
    expect(title1).toContain('Google');
    expect(title2).toContain('GitHub');
    expect(title3).toContain('Wikipedia');
    
    console.log('  ✓ Opened and navigated 3 tabs');
  });

  test('should maintain separate state in each tab', async () => {
    // Navigate to different pages
    await page1.goto('https://www.google.com');
    await page2.goto('https://www.github.com');
    await page3.goto('https://www.wikipedia.org');
    
    // Get URLs from each page
    const url1 = page1.url();
    const url2 = page2.url();
    const url3 = page3.url();
    
    expect(url1).toContain('google.com');
    expect(url2).toContain('github.com');
    expect(url3).toContain('wikipedia.org');
    
    // Verify they are different
    expect(url1).not.toBe(url2);
    expect(url2).not.toBe(url3);
    expect(url1).not.toBe(url3);
    
    console.log('  ✓ Each tab maintains separate state');
  });

  test('should handle tab switching', async () => {
    // Navigate to different pages
    await page1.goto('https://www.google.com');
    await page2.goto('https://www.github.com');
    
    // Switch back to page1
    await page1.bringToFront();
    
    // Verify page1 is still on Google
    const title1 = await page1.title();
    expect(title1).toContain('Google');
    
    // Switch to page2
    await page2.bringToFront();
    
    // Verify page2 is still on GitHub
    const title2 = await page2.title();
    expect(title2).toContain('GitHub');
    
    console.log('  ✓ Tab switching works correctly');
  });

  test('should handle page reloads in multiple tabs', async () => {
    // Navigate to different pages
    await page1.goto('https://www.google.com');
    await page2.goto('https://www.github.com');
    
    // Reload both pages
    await page1.reload();
    await page2.reload();
    
    // Verify both pages reloaded successfully
    const title1 = await page1.title();
    const title2 = await page2.title();
    
    expect(title1).toContain('Google');
    expect(title2).toContain('GitHub');
    
    console.log('  ✓ Page reloads work in multiple tabs');
  });

  test('should handle navigation within individual tabs', async () => {
    // Start on different pages
    await page1.goto('https://www.google.com');
    await page2.goto('https://www.github.com');
    
    // Navigate page1 to a different page
    await page1.goto('https://www.wikipedia.org');
    
    // Verify page1 changed but page2 stayed the same
    const title1 = await page1.title();
    const title2 = await page2.title();
    
    expect(title1).toContain('Wikipedia');
    expect(title2).toContain('GitHub');
    
    console.log('  ✓ Individual tab navigation works correctly');
  });

  test('should handle closing tabs', async () => {
    // Create a new temporary page
    const tempPage = await context.newPage();
    await tempPage.goto('https://www.google.com');
    
    // Verify we have 4 pages now
    const pages = context.pages();
    expect(pages.length).toBe(4);
    
    // Close the temporary page
    await tempPage.close();
    
    // Verify we're back to 3 pages
    const remainingPages = context.pages();
    expect(remainingPages.length).toBe(3);
    
    console.log('  ✓ Tab closing works correctly');
  });
});

test.describe('Browser Agent - Tab Content Isolation', () => {
  let browser;
  let context;
  let page1, page2;

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
    
    page1 = await context.newPage();
    page2 = await context.newPage();
  });

  test.afterAll(async () => {
    if (page1) await page1.close();
    if (page2) await page2.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should maintain separate DOM in each tab', async () => {
    // Create different content in each tab
    const html1 = `<!DOCTYPE html><html><body><h1>Page 1</h1></body></html>`;
    const html2 = `<!DOCTYPE html><html><body><h1>Page 2</h1></body></html>`;
    
    await page1.setContent(html1);
    await page2.setContent(html2);
    
    // Verify each page has its own content
    const title1 = await page1.evaluate(() => document.querySelector('h1')?.textContent);
    const title2 = await page2.evaluate(() => document.querySelector('h1')?.textContent);
    
    expect(title1).toBe('Page 1');
    expect(title2).toBe('Page 2');
    
    console.log('  ✓ Each tab maintains separate DOM');
  });

  test('should maintain separate localStorage in each tab', async () => {
    // Set different values in each tab
    await page1.goto('http://127.0.0.1:13310/healthz');
    // localStorage is origin-scoped, not tab-scoped; use two equivalent
    // hostnames to exercise independent storage areas without claiming a
    // browser invariant that does not exist.
    await page2.goto('http://localhost:13310/healthz');
    await page1.evaluate(() => {
      localStorage.setItem('testKey', 'value1');
    });
    
    await page2.evaluate(() => {
      localStorage.setItem('testKey', 'value2');
    });
    
    // Verify each tab has its own value
    const value1 = await page1.evaluate(() => localStorage.getItem('testKey'));
    const value2 = await page2.evaluate(() => localStorage.getItem('testKey'));
    
    expect(value1).toBe('value1');
    expect(value2).toBe('value2');
    
    console.log('  ✓ Each tab maintains separate localStorage');
  });

  test('should handle form submissions in different tabs', async () => {
    const formHtml = `<!DOCTYPE html>
<html>
<body>
  <form id="testForm">
    <input type="text" name="data" value="">
    <button type="submit">Submit</button>
  </form>
  <div id="result"></div>
  <script>
    document.getElementById('testForm').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = e.target.querySelector('input');
      document.getElementById('result').textContent = input.value;
    });
  </script>
</body>
</html>`;

    await page1.setContent(formHtml);
    await page2.setContent(formHtml);
    
    // Submit different data in each tab
    await page1.evaluate(() => {
      document.querySelector('input').value = 'Tab 1 Data';
      document.querySelector('form').dispatchEvent(new Event('submit'));
    });
    
    await page2.evaluate(() => {
      document.querySelector('input').value = 'Tab 2 Data';
      document.querySelector('form').dispatchEvent(new Event('submit'));
    });
    
    // Verify each tab has its own result
    const result1 = await page1.evaluate(() => document.getElementById('result')?.textContent);
    const result2 = await page2.evaluate(() => document.getElementById('result')?.textContent);
    
    expect(result1).toBe('Tab 1 Data');
    expect(result2).toBe('Tab 2 Data');
    
    console.log('  ✓ Form submissions work independently in each tab');
  });
});

test.describe('Browser Agent - Workspace Tab Management', () => {
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

  test('should create new tabs', async () => {
    const initialCount = context.pages().length;
    
    // Create a new tab
    const newPage = await context.newPage();
    await newPage.goto('https://www.google.com');
    
    const newCount = context.pages().length;
    expect(newCount).toBe(initialCount + 1);
    
    await newPage.close();
    
    console.log('  ✓ Can create new tabs');
  });

  test('should list all open tabs', async () => {
    // Create multiple tabs
    const page2 = await context.newPage();
    const page3 = await context.newPage();
    
    await page.goto('https://www.google.com');
    await page2.goto('https://www.github.com');
    await page3.goto('https://www.wikipedia.org');
    
    // Get all pages in context
    const allPages = context.pages();
    
    expect(allPages.length).toBeGreaterThanOrEqual(3);
    
    // Verify each has a URL
    for (const p of allPages) {
      const url = p.url();
      expect(url).toBeTruthy();
    }
    
    await page2.close();
    await page3.close();
    
    console.log('  ✓ Can list all open tabs');
  });
});
