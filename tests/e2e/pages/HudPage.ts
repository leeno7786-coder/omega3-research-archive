/**
 * Page Object for Omega 3.0 HUD Interface
 * 
 * This class encapsulates selectors and actions for the HUD interface,
 * providing a clean API for E2E tests.
 */

import { Page, Locator, expect } from '@playwright/test';

export class HudPage {
  readonly page: Page;
  readonly canvas: Locator;
  readonly title: Locator;
  readonly favicon: Locator;
  readonly viewportMeta: Locator;
  readonly preconnectLinks: Locator;
  readonly googleFontsPreconnect: Locator;

  constructor(page: Page) {
    this.page = page;
    this.canvas = page.locator('#canvas.canvas');
    this.title = page.locator('title');
    this.favicon = page.locator('link[rel="icon"]');
    this.viewportMeta = page.locator('meta[name="viewport"]');
    this.preconnectLinks = page.locator('link[rel="preconnect"]');
    this.googleFontsPreconnect = page.locator('link[rel="preconnect"][href="https://fonts.googleapis.com"]');
  }

  /**
   * Navigate to the HUD homepage
   */
  async goto() {
    await this.page.goto('/');
  }

  /**
   * Wait for the page to fully load
   */
  async waitForLoad() {
    await expect(this.page).toHaveTitle(/OMEGA|Portable Cognitive Substrate/);
    await expect(this.canvas).toBeVisible();
  }

  /**
   * Verify the basic page structure is present
   */
  async verifyPageStructure() {
    await expect(this.title).toHaveCount(1);
    await expect(this.favicon).toHaveCount(1);
    await expect(this.viewportMeta).toHaveCount(1);
    await expect(this.canvas).toBeVisible();
  }

  /**
   * Get the page title
   */
  async getTitle() {
    return await this.page.title();
  }

  /**
   * Get the page content as HTML
   */
  async getContent() {
    return await this.page.content();
  }

  /**
   * Check if the canvas is rendered
   */
  async isCanvasVisible() {
    return await this.canvas.isVisible();
  }

  /**
   * Get canvas element count
   */
  async getCanvasCount() {
    return await this.canvas.count();
  }

  /**
   * Verify Google Fonts are preconnected
   */
  async verifyGoogleFontsPreconnected() {
    await expect(this.googleFontsPreconnect).toHaveCount(1);
    await expect(this.preconnectLinks).toHaveCount(2);
  }

  /**
   * Verify theme support exists in CSS
   */
  async verifyThemeSupport() {
    const html = await this.getContent();
    expect(html).toContain('data-theme');
    expect(html).toContain('--ink');
    expect(html).toContain('--bone');
    expect(html).toContain('--ember');
    expect(html).toContain('--patina');
  }

  /**
   * Check health endpoint
   */
  async checkHealth() {
    const response = await this.page.request.get('/healthz');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.status).toBe('ok');
    return body;
  }

  /**
   * Check API status endpoint
   */
  async checkApiStatus() {
    const response = await this.page.request.get('/api/status');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toHaveProperty('pressure_loop');
    return body;
  }

  /**
   * Check boot state endpoint
   */
  async checkBootState() {
    const response = await this.page.request.get('/api/boot/state');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toHaveProperty('phase');
    expect(body).toHaveProperty('states');
    return body;
  }

  /**
   * Check a specific panel endpoint
   */
  async checkPanel(panelName: string) {
    const response = await this.page.request.get(`/api/panel/${panelName}`);
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toBeTruthy();
    expect(typeof body).toBe('object');
    return body;
  }

  /**
   * Check all panel endpoints
   */
  async checkAllPanels() {
    const panels = [
      'brain',
      'memory',
      'omega',
      'meta',
      'global',
      'autoloop',
      'identity',
      'deepsleep',
      'settings',
    ];
    const results = {};
    
    for (const panel of panels) {
      results[panel] = await this.checkPanel(panel);
    }
    
    return results;
  }

  /**
   * Take a screenshot of the current page
   */
  async takeScreenshot(path?: string) {
    return await this.page.screenshot({ path, fullPage: true });
  }
}

export default HudPage;
