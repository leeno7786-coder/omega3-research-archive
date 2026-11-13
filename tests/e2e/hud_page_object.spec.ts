/**
 * HUD Page Object Tests - Using the HudPage page object
 * 
 * These tests demonstrate the page object pattern for cleaner, more maintainable tests.
 */

import { test, expect } from '@playwright/test';
import { HudPage } from './pages/HudPage';

test.describe('HUD with Page Object', () => {
  let hudPage: HudPage;

  test.beforeEach(async ({ page }) => {
    hudPage = new HudPage(page);
    await hudPage.goto();
    await hudPage.waitForLoad();
  });

  test('page loads with correct title', async () => {
    const title = await hudPage.getTitle();
    expect(title).toMatch(/OMEGA|Portable Cognitive Substrate/);
  });

  test('page structure is valid', async () => {
    await hudPage.verifyPageStructure();
  });

  test('canvas is visible and singular', async () => {
    await expect(await hudPage.isCanvasVisible()).toBeTruthy();
    expect(await hudPage.getCanvasCount()).toBe(1);
  });

  test('Google Fonts are preconnected', async () => {
    await hudPage.verifyGoogleFontsPreconnected();
  });

  test('theme support is present', async () => {
    await hudPage.verifyThemeSupport();
  });

  test('health endpoint responds correctly', async () => {
    const health = await hudPage.checkHealth();
    expect(health.status).toBe('ok');
    expect(health.ts).toBeDefined();
  });

  test('API status endpoint returns subsystem status object', async () => {
    const status = await hudPage.checkApiStatus();
    expect(status).toHaveProperty('pressure_loop');
    expect(status.pressure_loop).toHaveProperty('state');
  });

  test('boot state endpoint has phase and subsystem states', async () => {
    const bootState = await hudPage.checkBootState();
    expect(bootState).toHaveProperty('phase');
    expect(bootState).toHaveProperty('states');
  });

  test('all panel endpoints respond', async () => {
    const panels = await hudPage.checkAllPanels();
    
    // Verify all expected panels are present
    expect(panels).toHaveProperty('brain');
    expect(panels).toHaveProperty('memory');
    expect(panels).toHaveProperty('omega');
    expect(panels).toHaveProperty('meta');
    expect(panels).toHaveProperty('global');
    expect(panels).toHaveProperty('autoloop');
    expect(panels).toHaveProperty('identity');
    expect(panels).toHaveProperty('deepsleep');
    expect(panels).toHaveProperty('settings');
    
    // Verify each panel returns a structured data object.
    for (const [panelName, panelData] of Object.entries(panels)) {
      expect(panelData && typeof panelData).toBe('object');
    }
  });

  test('individual panel endpoints work', async () => {
    const brainPanel = await hudPage.checkPanel('brain');
    expect(brainPanel).toHaveProperty('cycle');
    
    const memoryPanel = await hudPage.checkPanel('memory');
    expect(memoryPanel).toBeTruthy();
    
    const omegaPanel = await hudPage.checkPanel('omega');
    expect(omegaPanel).toBeTruthy();
  });
});

// Test suite for visual regression (placeholder for future implementation)
test.describe('Visual Regression Placeholders', () => {
  test('screenshot can be captured', async ({ page }) => {
    const hudPage = new HudPage(page);
    await hudPage.goto();
    await hudPage.waitForLoad();
    
    // Take a screenshot (this will be saved to test-results)
    const screenshot = await hudPage.takeScreenshot();
    expect(screenshot).toBeTruthy();
  });
});
