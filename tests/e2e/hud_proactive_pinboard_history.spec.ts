import { test, expect } from '@playwright/test';

test('proactive pinboard resolves active items and preserves read-only history', async ({ page }) => {
  let questionPending = true;
  let noticesPinned = true;
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];

  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.route('**/api/system-messages', async route => {
    const messages = questionPending ? [{
      id: 'question-1',
      ts: '2026-07-13T12:00:00Z',
      text: 'Should I continue with Binomial versus Poisson boundary cases?',
      seed_question: 'Scenario classification remains uncertain.',
      status: 'pending',
      response: null,
    }] : [];
    await route.fulfill({ json: { messages, unread: messages.length } });
  });
  await page.route('**/api/system-messages/question-1/respond', async route => {
    questionPending = false;
    await route.fulfill({
      json: {
        ok: true,
        message: { id: 'question-1', status: 'responded' },
      },
    });
  });
  await page.route('**/api/proactive/notices', async route => {
    const messages = noticesPinned ? [{
      id: 'notice-1',
      ts: '2026-07-13T12:01:00Z',
      message: 'The ten-scenario application pass is complete.',
      status: 'delivered',
      episode_id: 'episode-1',
      delivery: { text: 'The ten-scenario application pass is complete.' },
    }] : [];
    await route.fulfill({ json: { messages, count: messages.length } });
  });
  await page.route('**/api/proactive/notices/clear', async route => {
    noticesPinned = false;
    await route.fulfill({ json: { ok: true, cleared: 1 } });
  });
  await page.route('**/api/proactive/history', async route => {
    await route.fulfill({
      json: {
        count: 3,
        entries: [
          {
            id: 'question-1', type: 'question', status: 'responded',
            ts: '2026-07-13T12:00:00Z',
            text: 'Should I continue with Binomial versus Poisson boundary cases?',
            response: { text: 'Yes, focus on the boundary cases.' },
            episode_id: 'episode-1', thread_id: 'thread-1', result_id: '',
          },
          {
            id: 'result-1', type: 'result', status: 'responded',
            ts: '2026-07-13T12:02:00Z',
            text: 'I classified all ten scenarios and explained each choice.',
            response: { text: 'Good application. Continue.' },
            episode_id: 'episode-2', thread_id: 'thread-1', result_id: 'result-1',
          },
          {
            id: 'notice-1', type: 'notice', status: 'cleared',
            ts: '2026-07-13T12:01:00Z',
            text: 'The ten-scenario application pass is complete.',
            response: null,
            episode_id: 'episode-1', thread_id: '', result_id: '',
          },
        ],
      },
    });
  });

  await page.goto('/');
  await expect(page).toHaveTitle(/OMEGA|Portable Cognitive Substrate/);
  const rightDrawerToggle = page.locator('#side-drawer-right-toggle');
  if (await rightDrawerToggle.isVisible()) {
    await rightDrawerToggle.click({ force: true });
  }
  await page.evaluate(async () => {
    await (window as typeof window & {
      _refreshProactivePinboard: () => Promise<unknown>;
    })._refreshProactivePinboard();
  });

  await expect(page.locator('#system-messages-badge')).toHaveCount(0);
  await expect(page.getByText('Should I continue with Binomial versus Poisson boundary cases?')).toBeAttached();
  await expect(page.getByText('The ten-scenario application pass is complete.')).toBeAttached();

  await page.locator('.sysmsg-respond-input[data-msg-id="question-1"]').fill('Yes, focus on the boundary cases.');
  await page.locator('.sysmsg-send[data-msg-id="question-1"]').click();
  await expect(page.getByText('Should I continue with Binomial versus Poisson boundary cases?')).toHaveCount(0);
  await expect(page.getByText('The ten-scenario application pass is complete.')).toBeAttached();

  await page.locator('[data-proactive-clear]').click();
  await expect(page.getByText('The ten-scenario application pass is complete.')).toHaveCount(0);

  const leftDrawerToggle = page.locator('#side-drawer-toggle');
  if (await leftDrawerToggle.isVisible()) {
    await leftDrawerToggle.click({ force: true });
  }
  await page.locator('[data-panel="settings"]').click();
  await page.locator('[data-settings-view="proactive_history"]').click();
  const history = page.locator('[data-settings-view-panel="proactive_history"]');
  await expect(history.locator('.proactive-history-card')).toHaveCount(3);
  await expect(history.getByText('Question', { exact: true })).toBeVisible();
  await expect(history.getByText('Result', { exact: true })).toBeVisible();
  await expect(history.getByText('Notice', { exact: true })).toBeVisible();
  await expect(history.getByText('Good application. Continue.')).toBeVisible();
  await expect(history.getByText('Cleared', { exact: true })).toBeVisible();
  await expect(history.locator('textarea, .sysmsg-send, .sysmsg-delete')).toHaveCount(0);

  expect(pageErrors).toEqual([]);
  const unexpectedConsoleErrors = consoleErrors.filter(message => (
    !message.includes("ws://127.0.0.1:8092/ws/cli")
    && !message.includes("ws://localhost:8092/ws/cli")
  ));
  expect(unexpectedConsoleErrors).toEqual([]);
});
