/**
 * HUD upload-filename XSS guard (High #7).
 *
 * appendUpload() rendered the uploaded filename into innerHTML unescaped:
 *   span.innerHTML = `<span class="name">${item.filename}</span> ...`
 * A file named `<img src=x onerror=...>.png` (the filename is user-controlled, echoed by
 * /api/upload) executed on render — stored DOM-XSS. The filename (and kind) must be escaped.
 *
 * Drives the real window.OmegaUI.appendUpload on the served page.
 */
import { test, expect } from '@playwright/test';

test.describe('HUD upload-filename XSS guard', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/OMEGA|Portable Cognitive Substrate/);
  });

  test('a malicious uploaded filename is escaped, not executed', async ({ page }) => {
    const payload = '<img src=x onerror="window.__xss=true">.png';
    await page.evaluate((fn) => {
      (window as any).__xss = false;
      (window as any).OmegaUI.appendUpload({ filename: fn, kind: 'image', size: 2048, url: 'about:blank' });
    }, payload);

    // the onerror handler must NOT have fired
    expect(await page.evaluate(() => (window as any).__xss)).toBe(false);
    // no <img> was injected inside the filename label
    expect(await page.locator('.feed-upload .name img').count()).toBe(0);
    // the label shows the filename as literal text
    await expect(page.locator('.feed-upload .name').last()).toHaveText(payload);
  });
});
