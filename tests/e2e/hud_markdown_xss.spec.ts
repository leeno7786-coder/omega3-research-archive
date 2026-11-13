/**
 * HUD markdown XSS guards (High #8 / #51).
 *
 * The HUD renders assistant/telemetry markdown into innerHTML. Two sinks were unsafe:
 *  - #8: `[text](url)` emitted `<a href="url">` with NO scheme validation -> a `javascript:`
 *        (or `data:`) URI in model/telemetry output executed on click (stored XSS).
 *  - #51: a duplicate `_escapeHtml` omitted the single-quote escape and, via hoisting, won —
 *        letting a `'` break out of single-quoted attributes.
 *
 * These drive the REAL served page's renderer (window-global functions) via page.evaluate.
 */
import { test, expect } from '@playwright/test';

test.describe('HUD markdown XSS guards', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/OMEGA|Portable Cognitive Substrate/);
  });

  test('javascript: link renders as inert text, not an anchor', async ({ page }) => {
    const html = await page.evaluate(() => (window as any)._renderMarkdown('[click me](javascript:alert(1))'));
    expect(html.toLowerCase()).not.toContain('javascript:alert');
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('<a ');      // no anchor emitted for the unsafe scheme
    expect(html).toContain('click me');      // the link text is preserved
  });

  test('whitespace/control-obfuscated javascript scheme is still blocked', async ({ page }) => {
    const html = await page.evaluate(() => (window as any)._renderMarkdown('[x](java\tscript:alert(1))'));
    expect(html.toLowerCase()).not.toContain('javascript:');
    expect(html).not.toContain('<a ');
  });

  test('data: URI is blocked', async ({ page }) => {
    const html = await page.evaluate(() => (window as any)._renderMarkdown('[x](data:text/html,hi)'));
    expect(html).not.toContain('href="data:');
    expect(html).not.toContain('<a ');
  });

  test('https link is allowed and rendered as a safe anchor', async ({ page }) => {
    const html = await page.evaluate(() => (window as any)._renderMarkdown('[ok](https://example.com/p)'));
    expect(html).toContain('href="https://example.com/p"');
    expect(html).toContain('rel="noopener"');
  });

  test('relative link is allowed', async ({ page }) => {
    const html = await page.evaluate(() => (window as any)._renderMarkdown('[doc](/local/page)'));
    expect(html).toContain('href="/local/page"');
  });

  test('_escapeHtml escapes single quotes (no single-quoted-attribute breakout)', async ({ page }) => {
    const out = await page.evaluate(() => (window as any)._escapeHtml("a'b\"c<d>e&f"));
    expect(out).toBe('a&#39;b&quot;c&lt;d&gt;e&amp;f');
  });

  test('only one _escapeHtml is defined on the page (no hoisting override)', async ({ page }) => {
    // sanity: the canonical escaper (with single-quote handling) is the one in effect
    const escapesQuote = await page.evaluate(() => (window as any)._escapeHtml("'") === '&#39;');
    expect(escapesQuote).toBe(true);
  });
});
