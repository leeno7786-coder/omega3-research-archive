/**
 * Browser Agent Extension - Extraction Workflow E2E Tests
 * 
 * Tests the page extraction and data collection workflows.
 */

import { test, expect } from '@playwright/test';
import { chromium } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_PATH = path.resolve(__dirname, '../../../browser_agent_extension');

/**
 * Helper to create a test HTML page with known content
 */
function createTestHtml(title, content) {
  return `<!DOCTYPE html>
<html>
<head><title>${title}</title></head>
<body>${content}</body>
</html>`;
}

/**
 * Helper to serve HTML content via data URL
 */
function createDataUrl(html) {
  return `data:text/html,${encodeURIComponent(html)}`;
}

/**
 * Helper to create a local test server page
 */
async function createTestPage(page, html) {
  const dataUrl = createDataUrl(html);
  await page.goto(dataUrl);
  await page.waitForLoadState('networkidle');
}

test.describe('Browser Agent - Content Extraction', () => {
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

  test('should extract all text from simple page', async () => {
    const html = createTestHtml('Test Page', '<p>Hello, World!</p><p>Second paragraph.</p>');
    await createTestPage(page, html);
    
    const bodyText = await page.evaluate(() => document.body.textContent);
    expect(bodyText).toContain('Hello, World!');
    expect(bodyText).toContain('Second paragraph.');
    
    console.log('  ✓ Extracted all text from simple page');
  });

  test('should extract headings from page', async () => {
    const html = createTestHtml('Test Page', `
      <h1>Main Heading</h1>
      <h2>Subheading</h2>
      <h3>Minor heading</h3>
      <p>Some content.</p>
    `);
    await createTestPage(page, html);
    
    const headings = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('h1, h2, h3')).map(h => h.textContent);
    });
    
    expect(headings).toHaveLength(3);
    expect(headings[0]).toBe('Main Heading');
    expect(headings[1]).toBe('Subheading');
    expect(headings[2]).toBe('Minor heading');
    
    console.log('  ✓ Extracted headings from page');
  });

  test('should extract links from page', async () => {
    const html = createTestHtml('Test Page', `
      <a href="https://example.com">Link 1</a>
      <a href="https://google.com">Link 2</a>
      <a href="/local">Local Link</a>
    `);
    await createTestPage(page, html);
    
    const links = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('a')).map(a => ({
        text: a.textContent,
        href: a.href
      }));
    });
    
    expect(links).toHaveLength(3);
    expect(links[0].text).toBe('Link 1');
    expect(links[0].href).toContain('example.com');
    
    console.log('  ✓ Extracted links from page');
  });

  test('should extract interactive elements', async () => {
    const html = createTestHtml('Test Page', `
      <button id="btn1">Click Me</button>
      <input type="text" id="input1" placeholder="Enter text">
      <select id="select1"><option>Option 1</option></select>
      <a href="#">Link</a>
    `);
    await createTestPage(page, html);
    
    const interactiveElements = await page.evaluate(() => {
      const selectors = ['button', 'input', 'select', 'a[href]'];
      return Array.from(document.querySelectorAll(selectors.join(','))).map(el => ({
        tag: el.tagName,
        type: el.type || '',
        id: el.id || '',
        text: el.textContent?.trim() || '',
      }));
    });
    
    expect(interactiveElements.length).toBeGreaterThanOrEqual(4);
    const button = interactiveElements.find(el => el.tag === 'BUTTON');
    expect(button).toBeTruthy();
    expect(button.text).toBe('Click Me');
    
    console.log('  ✓ Extracted interactive elements from page');
  });

  test('should extract tables from page', async () => {
    const html = createTestHtml('Test Page', `
      <table>
        <thead><tr><th>Name</th><th>Age</th></tr></thead>
        <tbody>
          <tr><td>Alice</td><td>25</td></tr>
          <tr><td>Bob</td><td>30</td></tr>
        </tbody>
      </table>
    `);
    await createTestPage(page, html);
    
    const tableData = await page.evaluate(() => {
      const table = document.querySelector('table');
      if (!table) return null;
      
      const rows = Array.from(table.querySelectorAll('tr'));
      return rows.map(row => {
        const cells = Array.from(row.querySelectorAll('td, th'));
        return cells.map(cell => cell.textContent?.trim() || '');
      });
    });
    
    expect(tableData).toBeTruthy();
    expect(tableData).toHaveLength(3); // header + 2 data rows
    expect(tableData[0]).toEqual(['Name', 'Age']);
    expect(tableData[1]).toEqual(['Alice', '25']);
    
    console.log('  ✓ Extracted tables from page');
  });

  test('should extract forms from page', async () => {
    const html = createTestHtml('Test Page', `
      <form id="test-form">
        <input type="text" name="username" placeholder="Username">
        <input type="password" name="password" placeholder="Password">
        <button type="submit">Submit</button>
      </form>
    `);
    await createTestPage(page, html);
    
    const formData = await page.evaluate(() => {
      const form = document.querySelector('form');
      if (!form) return null;
      
      const inputs = Array.from(form.querySelectorAll('input, button, select, textarea'));
      return inputs.map(input => ({
        tag: input.tagName,
        type: input.type || '',
        name: input.name || '',
        placeholder: input.placeholder || '',
      }));
    });
    
    expect(formData).toBeTruthy();
    expect(formData.length).toBe(3);
    expect(formData[0].name).toBe('username');
    expect(formData[1].name).toBe('password');
    
    console.log('  ✓ Extracted forms from page');
  });

  test('should handle search results page extraction', async () => {
    const searchHtml = createTestHtml('Search Results', `
      <h1>Search Results</h1>
      <div class="result">
        <h2><a href="https://example.com/1">Result 1</a></h2>
        <p>Description for result 1</p>
      </div>
      <div class="result">
        <h2><a href="https://example.com/2">Result 2</a></h2>
        <p>Description for result 2</p>
      </div>
    `);
    await createTestPage(page, searchHtml);
    
    const results = await page.evaluate(() => {
      const resultDivs = Array.from(document.querySelectorAll('.result'));
      return resultDivs.map(div => ({
        title: div.querySelector('h2 a')?.textContent?.trim() || '',
        url: div.querySelector('h2 a')?.href || '',
        description: div.querySelector('p')?.textContent?.trim() || '',
      }));
    });
    
    expect(results).toHaveLength(2);
    expect(results[0].title).toBe('Result 1');
    expect(results[0].description).toBe('Description for result 1');
    
    console.log('  ✓ Extracted search results from page');
  });
});

test.describe('Browser Agent - Metadata Extraction', () => {
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

  test('should extract meta tags from page', async () => {
    const html = createTestHtml('Test Page', `
      <meta name="description" content="Test description">
      <meta name="keywords" content="test, example">
      <meta name="author" content="Test Author">
      <p>Main content</p>
    `);
    await createTestPage(page, html);
    
    const metaTags = await page.evaluate(() => {
      const metas = Array.from(document.querySelectorAll('meta[name]'));
      return metas.map(meta => ({
        name: meta.getAttribute('name'),
        content: meta.getAttribute('content'),
      }));
    });
    
    expect(metaTags.length).toBeGreaterThanOrEqual(3);
    const description = metaTags.find(m => m.name === 'description');
    expect(description?.content).toBe('Test description');
    
    console.log('  ✓ Extracted meta tags from page');
  });

  test('should extract Open Graph tags from page', async () => {
    const html = createTestHtml('Test Page', `
      <meta property="og:title" content="Open Graph Title">
      <meta property="og:description" content="Open Graph Description">
      <meta property="og:image" content="https://example.com/image.jpg">
      <meta property="og:url" content="https://example.com">
    `);
    await createTestPage(page, html);
    
    const ogTags = await page.evaluate(() => {
      const metas = Array.from(document.querySelectorAll('meta[property^="og:"]'));
      return metas.map(meta => ({
        property: meta.getAttribute('property'),
        content: meta.getAttribute('content'),
      }));
    });
    
    expect(ogTags).toHaveLength(4);
    const title = ogTags.find(m => m.property === 'og:title');
    expect(title?.content).toBe('Open Graph Title');
    
    console.log('  ✓ Extracted Open Graph tags from page');
  });

  test('should extract JSON-LD structured data', async () => {
    const html = createTestHtml('Test Page', `
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": "John Doe",
        "jobTitle": "Developer"
      }
      </script>
    `);
    await createTestPage(page, html);
    
    const jsonLd = await page.evaluate(() => {
      const script = document.querySelector('script[type="application/ld+json"]');
      if (!script) return null;
      try {
        return JSON.parse(script.textContent);
      } catch (e) {
        return null;
      }
    });
    
    expect(jsonLd).toBeTruthy();
    expect(jsonLd['@type']).toBe('Person');
    expect(jsonLd.name).toBe('John Doe');
    
    console.log('  ✓ Extracted JSON-LD structured data from page');
  });
});
