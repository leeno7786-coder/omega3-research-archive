/**
 * Browser Agent Extension E2E Test Utilities
 * 
 * Common helpers for testing the Chrome extension with Playwright.
 */

import { chromium } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Path to the browser agent extension directory
 */
export const EXTENSION_PATH = path.resolve(__dirname, '../../../browser_agent_extension');

/**
 * Extension ID - this may need to be dynamic based on the loaded extension
 * For development, Chrome assigns a temporary ID like 'abcdefghijklmnopqrstuvwxyz'
 */
export const EXTENSION_ID = 'TBD';

/**
 * Chrome extension URLs
 */
export const EXTENSION_URLS = {
  popup: `chrome-extension://${EXTENSION_ID}/sidepanel.html`,
  options: `chrome-extension://${EXTENSION_ID}/sidepanel.html`,
  background: `chrome-extension://${EXTENSION_ID}/background.html`,
};

/**
 * Launch a browser with the extension loaded
 */
export async function launchBrowserWithExtension(headless = true, slowMo = 0) {
  return chromium.launch({
    headless,
    slowMo,
    args: [
      `--disable-extensions-except=${EXTENSION_PATH}`,
      `--load-extension=${EXTENSION_PATH}`,
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-web-security',
      '--allow-running-insecure-content',
    ],
  });
}

/**
 * Get the extension ID from a browser context
 */
export async function getExtensionId(page) {
  // Navigate to chrome://extensions and extract the extension ID
  const extensionsPage = await page.context().newPage();
  await extensionsPage.goto('chrome://extensions');
  
  // Wait for extensions to load
  await extensionsPage.waitForSelector('extensions-manager');
  
  // Get all extension cards
  const extensionCards = await extensionsPage.$$('extensions-card');
  
  for (const card of extensionCards) {
    const name = await card.$eval('.name', el => el.textContent);
    if (name && name.includes('Omega') || name.includes('Browser Agent')) {
      const id = await card.getAttribute('id');
      await extensionsPage.close();
      return id;
    }
  }
  
  await extensionsPage.close();
  return null;
}

/**
 * Open the extension popup
 */
export async function openExtensionPopup(page, extensionId) {
  // Click on the extensions toolbar button
  const extensionButton = page.locator(`#toolbar >> text="Browser Agent"`);
  await extensionButton.click();
  
  // Wait for popup to appear
  const popup = page.locator('role=dialog');
  await popup.waitFor({ state: 'visible' });
  
  return popup;
}

/**
 * Open the extension sidepanel
 */
export async function openExtensionSidepanel(page, extensionId) {
  // Open sidepanel via keyboard shortcut or button
  await page.keyboard.press('Control+Shift+Y');
  
  // Wait for sidepanel to appear
  await page.waitForSelector('#sidepanel', { state: 'visible' });
  
  return page.locator('#sidepanel');
}

/**
 * Navigate to a test page
 */
export async function navigateToTestPage(page, url = 'http://example.com') {
  await page.goto(url);
  await page.waitForLoadState('networkidle');
  return page;
}

/**
 * Wait for content script to be injected
 */
export async function waitForContentScript(page, timeout = 10000) {
  return page.evaluate(async () => {
    let retries = 0;
    const maxRetries = 20;
    
    while (retries < maxRetries) {
      if (window.OmegaBrowserAgent) {
        return true;
      }
      await new Promise(resolve => setTimeout(resolve, 100));
      retries++;
    }
    return false;
  }, { timeout });
}

/**
 * Send a message to the background script
 */
export async function sendMessageToBackground(page, message) {
  return page.evaluate(async (msg) => {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(msg, resolve);
    });
  }, message);
}

/**
 * Get the current tab ID
 */
export async function getCurrentTabId(page) {
  return page.evaluate(() => {
    return new Promise((resolve) => {
      chrome.tabs.getCurrent(tab => {
        resolve(tab.id);
      });
    });
  });
}

/**
 * Execute code in a tab's context
 */
export async function executeInTab(page, tabId, code) {
  return page.evaluate(async ({ tabId, code }) => {
    return new Promise((resolve) => {
      chrome.tabs.executeScript(tabId, { code }, (results) => {
        resolve(results ? results[0] : null);
      });
    });
  }, { tabId, code });
}

/**
 * Get extension storage
 */
export async function getExtensionStorage(page, key) {
  const worker = await extensionServiceWorker(page);
  if (!worker) {
    return page.evaluate((key) => {
      const raw = localStorage.getItem(`omega_agent_${key}`);
      return raw === null ? undefined : JSON.parse(raw);
    }, key);
  }
  return worker.evaluate(async (key) => {
    return new Promise((resolve) => {
      chrome.storage.local.get(key, (result) => {
        resolve(result[key]);
      });
    });
  }, key);
}

/**
 * Set extension storage
 */
export async function setExtensionStorage(page, key, value) {
  const worker = await extensionServiceWorker(page);
  if (!worker) {
    return page.evaluate(({ key, value }) => {
      localStorage.setItem(`omega_agent_${key}`, JSON.stringify(value));
      return true;
    }, { key, value });
  }
  return worker.evaluate(async ({ key, value }) => {
    return new Promise((resolve) => {
      chrome.storage.local.set({ [key]: value }, () => {
        resolve(true);
      });
    });
  }, { key, value });
}

async function extensionServiceWorker(page) {
  const context = page.context();
  const existing = context.serviceWorkers().find((worker) =>
    worker.url().includes('/src/background/background.js'),
  );
  return existing || null;
}

/**
 * Helper to click an element with retry logic
 */
export async function clickWithRetry(page, selector, retries = 3, delay = 500) {
  for (let i = 0; i < retries; i++) {
    try {
      const element = page.locator(selector);
      await element.click();
      return true;
    } catch (error) {
      if (i === retries - 1) throw error;
      await page.waitForTimeout(delay);
    }
  }
}

/**
 * Helper to type text with retry logic
 */
export async function typeWithRetry(page, selector, text, retries = 3, delay = 500) {
  for (let i = 0; i < retries; i++) {
    try {
      const element = page.locator(selector);
      await element.fill(text);
      return true;
    } catch (error) {
      if (i === retries - 1) throw error;
      await page.waitForTimeout(delay);
    }
  }
}

/**
 * Wait for an element to exist and be visible
 */
export async function waitForElement(page, selector, timeout = 10000) {
  const element = page.locator(selector);
  await element.waitFor({ state: 'visible', timeout });
  return element;
}

/**
 * Wait for an element to disappear
 */
export async function waitForElementDisappear(page, selector, timeout = 10000) {
  const element = page.locator(selector);
  await element.waitFor({ state: 'hidden', timeout });
}

/**
 * Take a screenshot for documentation
 */
export async function takeScreenshot(page, name) {
  const screenshotPath = `../../../docs/validation/e2e-browser-agent-${name}.png`;
  await page.screenshot({ path: screenshotPath, fullPage: true });
  console.log(`Screenshot saved: ${screenshotPath}`);
}
