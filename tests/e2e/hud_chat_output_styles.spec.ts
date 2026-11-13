import { test, expect } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HUD_BUNDLE = 'file://' + path.resolve(HERE, '../../hud/omega_interface.html');

const sample = [
  '## Build plan',
  '',
  '- Parse the request',
  '- Render the result',
  '',
  '```python',
  'print("hello")',
  '```',
  '',
  '```mermaid',
  'flowchart TD',
  '  A[Start] --> B[Done]',
  '```',
].join('\n');

test.describe('HUD rich chat output', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(HUD_BUNDLE, { waitUntil: 'domcontentloaded' });
    await expect(page).toHaveTitle(/OMEGA|Portable Cognitive Substrate/);
  });

  test('renders structured Markdown, code, Mermaid, and inert HTML', async ({ page }) => {
    // Ensure the local Mermaid renderer is initialized before we render,
    // otherwise the mermaid block falls back to its source view.
    await page.waitForFunction(() => (window as any).OmegaMermaid && typeof (window as any).OmegaMermaid.render === 'function');
    const lineHtml = await page.evaluate((text) => {
      const line = (window as any).appendLine('assistant', text, true);
      return line.outerHTML;
    }, sample + '\n\n<script>window.__chatXss = true</script>');

    expect(lineHtml).not.toContain('## Build plan');
    await expect(page.locator('.line.assistant').last().locator('h2')).toHaveText('Build plan');
    // The sample has a python code block plus a Mermaid block whose source
    // fallback is itself rendered as a code block, so two code blocks appear.
    await expect(page.locator('.line.assistant').last().locator('.chat-code-block')).toHaveCount(2);
    await expect(page.locator('.line.assistant').last().locator('.chat-code-lang').first()).toHaveText('python');
    await expect(page.locator('.line.assistant').last().locator('.chat-mermaid-diagram')).toHaveCount(1);
    expect(await page.evaluate(() => (window as any).__chatXss)).toBeUndefined();
    await expect(page.locator('.line.assistant').last().locator('script')).toHaveCount(0);
  });

  test('opens Mermaid source when the diagram is invalid', async ({ page }) => {
    await page.evaluate(() => {
      (window as any).appendLine(
        'assistant',
        '```mermaid\nnot-a-supported-diagram\n```',
        true,
      );
    });

    const line = page.locator('.line.assistant').last();
    await expect(line.locator('.chat-mermaid-fallback[open]')).toHaveCount(1);
    await expect(line.locator('.chat-mermaid-fallback code')).toContainText('not-a-supported-diagram');
  });

  test('accepts spaced Mermaid node labels', async ({ page }) => {
    const html = await page.evaluate(() => (window as any)._renderMarkdown(
      '```mermaid\nflowchart TD\n  A [Start] --> B [Done]\n```',
    ));
    expect(html).toContain('chat-mermaid-diagram');
    expect(html).toContain('>Done<');
  });
});
