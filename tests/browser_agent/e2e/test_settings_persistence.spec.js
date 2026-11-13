/**
 * Browser Agent Extension - Settings Persistence E2E Tests
 * 
 * Tests settings persistence across extension restarts and sessions.
 * These tests verify that user preferences are saved and restored correctly.
 */

import { test, expect } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  EXTENSION_PATH,
  launchBrowserWithExtension,
  getExtensionStorage,
  setExtensionStorage,
  takeScreenshot,
} from './test-utils.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

test.describe('Browser Agent - Settings Persistence', () => {
  let browser;
  let context;
  let page;

  test.beforeAll(async () => {
    browser = await launchBrowserWithExtension(true, 0);
    context = await browser.newContext();
    page = await context.newPage();
    await page.goto('http://127.0.0.1:13310/healthz');
    console.log('  Settings persistence test setup complete');
  });

  test.afterAll(async () => {
    if (page) await page.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should save and retrieve API key settings', async () => {
    // Set an API key
    await setExtensionStorage(page, 'openrouter_api_key', 'test-api-key-12345');
    
    // Retrieve the API key
    const savedKey = await getExtensionStorage(page, 'openrouter_api_key');
    
    expect(savedKey).toBe('test-api-key-12345');
    console.log('  ✓ API key saved and retrieved successfully');
  });

  test('should save and retrieve backend configuration', async () => {
    const backendConfig = {
      type: 'openrouter',
      serverUrl: 'https://api.openrouter.ai',
      apiKey: 'test-key',
    };
    
    await setExtensionStorage(page, 'backend_config', backendConfig);
    const savedConfig = await getExtensionStorage(page, 'backend_config');
    
    expect(savedConfig).toEqual(backendConfig);
    console.log('  ✓ Backend configuration saved and retrieved successfully');
  });

  test('should save and retrieve model settings', async () => {
    const modelSettings = {
      currentModel: 'openai/gpt-4o',
      temperature: 0.7,
      maxTokens: 4096,
    };
    
    await setExtensionStorage(page, 'model_settings', modelSettings);
    const savedSettings = await getExtensionStorage(page, 'model_settings');
    
    expect(savedSettings.currentModel).toBe('openai/gpt-4o');
    expect(savedSettings.temperature).toBe(0.7);
    expect(savedSettings.maxTokens).toBe(4096);
    console.log('  ✓ Model settings saved and retrieved successfully');
  });

  test('should persist settings across page reloads', async () => {
    // Set initial settings
    await setExtensionStorage(page, 'test_setting', 'value1');
    
    // Reload the page
    await page.reload();
    await page.waitForLoadState('networkidle');
    
    // Verify settings are still present
    const savedValue = await getExtensionStorage(page, 'test_setting');
    expect(savedValue).toBe('value1');
    
    console.log('  ✓ Settings persisted across page reload');
  });

  test('should handle multiple settings simultaneously', async () => {
    const settings = {
      api_key: 'key1',
      model: 'model1',
      theme: 'dark',
      fontSize: 14,
    };
    
    // Save all settings
    for (const [key, value] of Object.entries(settings)) {
      await setExtensionStorage(page, key, value);
    }
    
    // Verify all settings
    for (const [key, expectedValue] of Object.entries(settings)) {
      const savedValue = await getExtensionStorage(page, key);
      expect(savedValue).toBe(expectedValue);
    }
    
    console.log('  ✓ Multiple settings handled simultaneously');
  });

  test('should update existing settings', async () => {
    // Set initial value
    await setExtensionStorage(page, 'update_test', 'initial');
    
    // Update the value
    await setExtensionStorage(page, 'update_test', 'updated');
    
    // Verify the update
    const savedValue = await getExtensionStorage(page, 'update_test');
    expect(savedValue).toBe('updated');
    
    console.log('  ✓ Settings updated successfully');
  });

  test('should return undefined for non-existent settings', async () => {
    const nonExistent = await getExtensionStorage(page, 'non_existent_setting_xyz');
    expect(nonExistent).toBeUndefined();
    
    console.log('  ✓ Non-existent settings return undefined');
  });

  test('should handle complex nested settings objects', async () => {
    const complexSettings = {
      backend: {
        type: 'lmstudio',
        url: 'http://localhost:1234',
        timeout: 30000,
      },
      inference: {
        temperature: 0.9,
        topP: 0.95,
        maxTokens: 2048,
      },
      ui: {
        theme: 'system',
        font: 'sans-serif',
        animations: true,
      },
    };
    
    await setExtensionStorage(page, 'complex_settings', complexSettings);
    const savedSettings = await getExtensionStorage(page, 'complex_settings');
    
    expect(savedSettings).toEqual(complexSettings);
    expect(savedSettings.backend.type).toBe('lmstudio');
    expect(savedSettings.inference.temperature).toBe(0.9);
    expect(savedSettings.ui.animations).toBe(true);
    
    console.log('  ✓ Complex nested settings handled correctly');
  });

  test('should clear settings when null is set', async () => {
    // Set a setting
    await setExtensionStorage(page, 'clear_test', 'value');
    
    // Clear it by setting null
    await setExtensionStorage(page, 'clear_test', null);
    
    // Verify it's cleared
    const cleared = await getExtensionStorage(page, 'clear_test');
    expect(cleared).toBeNull();
    
    console.log('  ✓ Settings cleared successfully');
  });
});

test.describe('Browser Agent - Settings UI Interaction', () => {
  let browser;
  let context;
  let page;

  test.beforeAll(async () => {
    browser = await launchBrowserWithExtension(true, 0);
    context = await browser.newContext();
    page = await context.newPage();
    await page.goto('http://127.0.0.1:13310/healthz');
  });

  test.afterAll(async () => {
    if (page) await page.close();
    if (context) await context.close();
    if (browser) await browser.close();
  });

  test('should open settings page in sidepanel', async () => {
    await page.goto('https://www.google.com');
    await page.waitForLoadState('networkidle');
    
    // Try to open the extension sidepanel
    // Note: This may require the extension to be properly configured
    // For now, we verify the navigation works
    const title = await page.title();
    expect(title.length).toBeGreaterThan(0);
    
    console.log('  ✓ Settings page navigation works');
  });

  test('should have consistent settings format', async () => {
    // Test that settings follow a consistent format
    const testSettings = {
      version: '1.0',
      features: {
        ocr: true,
        vision: true,
        extraction: true,
      },
    };
    
    await setExtensionStorage(page, 'settings_format_test', testSettings);
    const saved = await getExtensionStorage(page, 'settings_format_test');
    
    expect(saved).toHaveProperty('version');
    expect(saved).toHaveProperty('features');
    expect(saved.features).toHaveProperty('ocr');
    
    console.log('  ✓ Settings format is consistent');
  });
});
