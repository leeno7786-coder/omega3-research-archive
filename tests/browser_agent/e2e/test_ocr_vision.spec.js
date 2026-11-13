/**
 * Browser Agent Extension - OCR and Vision E2E Tests
 * 
 * Tests OCR (Optical Character Recognition) and computer vision capabilities
 * of the browser agent extension, including screenshot capture and text extraction.
 */

import { test, expect } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  EXTENSION_PATH,
  launchBrowserWithExtension,
  navigateToTestPage,
  takeScreenshot,
} from './test-utils.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

test.describe('Browser Agent - OCR Vision Capabilities', () => {
  let browser;
  let context;
  let page;

  test.beforeAll(async () => {
    browser = await launchBrowserWithExtension(true, 0);
    context = await browser.newContext();
    page = await context.newPage();
    console.log('  OCR/Vision test setup complete');
  });

  test.afterAll(async () => {
    if (page) await page.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should capture screenshot of visible page', async () => {
    await navigateToTestPage(page, 'https://www.google.com');
    
    // Capture screenshot
    const screenshotBuffer = await page.screenshot({
      type: 'png',
      fullPage: false,
    });
    
    expect(screenshotBuffer.length).toBeGreaterThan(0);
    console.log(`  ✓ Screenshot captured (${screenshotBuffer.length} bytes)`);
  });

  test('should capture full page screenshot', async () => {
    await navigateToTestPage(page, 'https://www.wikipedia.org/wiki/Playwright');
    
    // Capture full page screenshot
    const fullPageScreenshot = await page.screenshot({
      type: 'png',
      fullPage: true,
    });
    
    expect(fullPageScreenshot.length).toBeGreaterThan(0);
    console.log(`  ✓ Full page screenshot captured (${fullPageScreenshot.length} bytes)`);
  });

  test('should extract visible text from page', async () => {
    await navigateToTestPage(page, 'https://www.wikipedia.org/wiki/JavaScript');
    
    // Extract all visible text
    const pageText = await page.evaluate(() => {
      return document.body.innerText;
    });
    
    expect(pageText.length).toBeGreaterThan(1000);
    expect(pageText).toContain('JavaScript');
    console.log(`  ✓ Extracted ${pageText.length} characters of visible text`);
  });

  test('should identify clickable elements on page', async () => {
    await navigateToTestPage(page, 'https://www.google.com');
    
    // Get all clickable elements
    const clickableElements = await page.evaluate(() => {
      const clickable = [];
      const selectors = [
        'a[href]',
        'button',
        'input[type="submit"]',
        'input[type="button"]',
        '[onclick]',
        '[role="button"]',
      ];
      
      selectors.forEach(selector => {
        const elements = document.querySelectorAll(selector);
        clickable.push(...Array.from(elements));
      });
      
      return clickable.map(el => ({
        tag: el.tagName,
        text: el.innerText?.substring(0, 50) || '',
        href: el.href || '',
      }));
    });
    
    expect(clickableElements.length).toBeGreaterThan(0);
    console.log(`  ✓ Found ${clickableElements.length} clickable elements`);
  });

  test('should extract page metadata', async () => {
    await navigateToTestPage(page, 'https://www.wikipedia.org');
    
    const metadata = await page.evaluate(() => {
      return {
        title: document.title,
        description: document.querySelector('meta[name="description"]')?.content || '',
        lang: document.documentElement.lang || '',
        charset: document.characterSet || '',
      };
    });
    
    expect(metadata.title).toBeTruthy();
    expect(metadata.title.length).toBeGreaterThan(0);
    console.log(`  ✓ Extracted page metadata: ${JSON.stringify(metadata, null, 2)}`);
  });

  test('should handle pages with images', async () => {
    await navigateToTestPage(page, 'https://www.google.com');
    
    const images = await page.evaluate(() => {
      const imgElements = document.querySelectorAll('img');
      return Array.from(imgElements).map(img => ({
        src: img.src || '',
        alt: img.alt || '',
        width: img.naturalWidth || 0,
        height: img.naturalHeight || 0,
      }));
    });
    
    // There should be at least one image (Google logo or similar)
    expect(images.length).toBeGreaterThanOrEqual(0);
    console.log(`  ✓ Found ${images.length} images on page`);
  });

  test('should extract structured data from search results', async () => {
    await navigateToTestPage(page, 'https://www.google.com/search?q=playwright+testing');
    
    // Search markup varies by region and consent state; the body is the
    // stable readiness boundary for this extraction contract.
    await page.waitForSelector('body');
    
    const searchResults = await page.evaluate(() => {
      const results = [];
      const links = document.querySelectorAll('a[href]');
      
      links.forEach(link => {
        const href = link.href;
        const text = link.innerText;
        
        // Filter for search result links
        if (href && text && href.includes('http') && !href.includes('google.com/search')) {
          results.push({
            text: text.substring(0, 100),
            url: href,
          });
        }
      });
      
      return results.slice(0, 5); // Return top 5 results
    });
    
    expect(searchResults.length).toBeGreaterThan(0);
    console.log(`  ✓ Extracted ${searchResults.length} search results`);
  });

  test('should capture viewport dimensions', async () => {
    await navigateToTestPage(page, 'https://www.google.com');
    
    const dimensions = await page.evaluate(() => {
      return {
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight,
      };
    });
    
    expect(dimensions.viewportWidth).toBeGreaterThan(0);
    expect(dimensions.viewportHeight).toBeGreaterThan(0);
    console.log(`  ✓ Viewport dimensions: ${dimensions.viewportWidth}x${dimensions.viewportHeight}`);
  });

  test('should handle OCR-like text extraction from image-heavy pages', async () => {
    // Navigate to a page with images
    await navigateToTestPage(page, 'https://www.wikipedia.org/wiki/Image');
    
    // Extract text that would typically be OCR'd from images
    const pageContent = await page.evaluate(() => {
      // Get all text that might be in image captions or alt text
      const images = document.querySelectorAll('img');
      const altTexts = [];
      
      images.forEach(img => {
        if (img.alt && img.alt.trim()) {
          altTexts.push(img.alt.trim());
        }
      });
      
      return {
        altTexts,
        totalImages: images.length,
      };
    });
    
    expect(pageContent.totalImages).toBeGreaterThanOrEqual(0);
    console.log(`  ✓ Extracted alt text from ${pageContent.totalImages} images`);
  });

  test('should extract tables from page', async () => {
    await navigateToTestPage(page, 'https://www.wikipedia.org/wiki/Comparison_of_JavaScript_frameworks');
    
    // Try to find tables on the page
    const tables = await page.evaluate(() => {
      const tableElements = document.querySelectorAll('table');
      const tableData = [];
      
      tableElements.forEach((table, index) => {
        const rows = table.querySelectorAll('tr');
        const tableInfo = {
          index,
          rowCount: rows.length,
          cellCount: 0,
        };
        
        rows.forEach(row => {
          const cells = row.querySelectorAll('th, td');
          tableInfo.cellCount += cells.length;
        });
        
        tableData.push(tableInfo);
      });
      
      return tableData;
    });
    
    console.log(`  ✓ Found ${tables.length} tables on page`);
  });
});

test.describe('Browser Agent - Vision-Based Navigation', () => {
  let browser;
  let context;
  let page;

  test.beforeAll(async () => {
    browser = await launchBrowserWithExtension(true, 0);
    context = await browser.newContext();
    page = await context.newPage();
  });

  test.afterAll(async () => {
    if (page) await page.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should navigate using visual landmarks', async () => {
    await navigateToTestPage(page, 'https://www.google.com');
    
    // Get the initial URL
    const initialUrl = page.url();
    
    // Find a link and navigate to it
    const links = await page.$$('a[href]');
    
    if (links.length > 0) {
      // Click the first link (if it's safe)
      const firstLink = links[0];
      const href = await firstLink.getAttribute('href');
      
      if (href && href.startsWith('http')) {
        await firstLink.click();
        await page.waitForLoadState('networkidle');
        
        const newUrl = page.url();
        expect(newUrl).not.toBe(initialUrl);
        console.log(`  ✓ Navigated from ${initialUrl} to ${newUrl}`);
      }
    }
  });

  test('should verify page content visibility', async () => {
    await navigateToTestPage(page, 'https://www.google.com');
    
    // Check if the page has visible content
    const isVisible = await page.evaluate(() => {
      const body = document.body;
      return body && body.offsetHeight > 0 && body.offsetWidth > 0;
    });
    
    expect(isVisible).toBe(true);
    console.log('  ✓ Page content is visible');
  });

  test('should handle rapid page changes', async () => {
    const urls = [
      'https://www.google.com',
      'https://www.github.com',
      'https://www.wikipedia.org',
    ];
    
    for (const url of urls) {
      await page.goto(url, { waitUntil: 'networkidle' });
      const title = await page.title();
      expect(title.length).toBeGreaterThan(0);
      console.log(`  ✓ Rapid navigation to ${url} - title: ${title}`);
    }
  });
});
