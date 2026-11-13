/**
 * HUD Smoke Tests - Basic connectivity and page load verification
 * 
 * These tests verify that the Omega 3.0 HUD interface loads correctly
 * and responds to basic interactions.
 */

import { test, expect } from '@playwright/test';

// Base test suite for HUD interface
test.describe('HUD Interface', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to the HUD homepage before each test
    await page.goto('/');
    
    // Wait for the page to load - check for title
    await expect(page).toHaveTitle(/OMEGA|Portable Cognitive Substrate/);
  });

  test('homepage loads successfully', async ({ page }) => {
    // Verify page title
    await expect(page).toHaveTitle(/OMEGA|Portable Cognitive Substrate/);
    
    // Verify the main HUD stage exists.
    const canvas = page.locator('#canvas.canvas');
    await expect(canvas).toBeVisible();
  });

  test('health check endpoint responds', async ({ request }) => {
    // Test the health endpoint directly
    const response = await request.get('/healthz');
    expect(response.status()).toBe(200);
    
    const body = await response.json();
    expect(body.status).toBe('ok');
    expect(body.ts).toBeDefined();
  });

  test('API status endpoint responds', async ({ request }) => {
    // Test the status API endpoint
    const response = await request.get('/api/status');
    expect(response.status()).toBe(200);
    
    const body = await response.json();
    // Should return keyed subsystem status pills.
    expect(body).toHaveProperty('pressure_loop');
    expect(body.pressure_loop).toHaveProperty('state');
  });

  test('page has expected canvas element', async ({ page }) => {
    // The HUD interface uses a div-based canvas/stage.
    const canvas = page.locator('#canvas.canvas');
    await expect(canvas).toBeVisible();
    await expect(canvas).toHaveCount(1);
  });

  test('page has expected meta tags', async ({ page }) => {
    // Verify favicon is present
    const favicon = page.locator('link[rel="icon"]');
    await expect(favicon).toHaveCount(1);
    
    // Verify viewport meta tag
    const viewport = page.locator('meta[name="viewport"]');
    await expect(viewport).toHaveCount(1);
  });

  test('Google Fonts are preconnected', async ({ page }) => {
    // The HUD preconnects to Google Fonts for performance
    const preconnectLinks = page.locator('link[rel="preconnect"]');
    await expect(preconnectLinks).toHaveCount(2);
    
    // Check for Google Fonts URLs
    const fontsPreconnect = page.locator('link[rel="preconnect"][href="https://fonts.googleapis.com"]');
    await expect(fontsPreconnect).toHaveCount(1);
  });
});

// Additional test suite for API endpoints
test.describe('API Endpoints', () => {
  test('boot state endpoint', async ({ request }) => {
    const response = await request.get('/api/boot/state');
    expect(response.status()).toBe(200);
    
    const body = await response.json();
    expect(body).toHaveProperty('phase');
    expect(body).toHaveProperty('states');
  });

  test('panel endpoints structure', async ({ request }) => {
    // Test that panel endpoints exist and return valid structure
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
    
    for (const panel of panels) {
      const response = await request.get(`/api/panel/${panel}`);
      expect(response.status()).toBe(200), `Panel ${panel} should return 200`;
      
      const body = await response.json();
      expect(body && typeof body).toBe('object'), `Panel ${panel} should return an object`;
    }
  });
});

// Test suite for static assets
test.describe('Static Assets', () => {
  test('CSS files are accessible', async ({ request }) => {
    // The HUD bundles CSS inline, but we can test if any external CSS would work
    // This is more of a placeholder for when external assets are added
    const response = await request.get('/');
    expect(response.status()).toBe(200);
    
    const html = await response.text();
    expect(html).toContain('style');
  });
});

// Test suite for theme support
test.describe('Theme Support', () => {
  test('page has theme support in CSS', async ({ page }) => {
    await page.goto('/');
    // The HUD supports multiple themes via data-theme attribute
    const html = await page.content();
    expect(html).toContain('data-theme');
  });

  test('multiple theme definitions exist', async ({ page }) => {
    await page.goto('/');
    const html = await page.content();
    
    // Check for theme-related CSS variables
    expect(html).toContain('--ink');
    expect(html).toContain('--bone');
    expect(html).toContain('--ember');
    expect(html).toContain('--patina');
  });
});
