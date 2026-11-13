// Browser Agent Extension - Shared Test Fixtures
// This module provides mock Chrome APIs, test utilities, and shared fixtures
// for the browser agent extension test suite.
// Uses Node.js native assert and does not require vitest/jest.

import assert from 'node:assert/strict';

// ============================================================================
// Mock Function Helper
// ============================================================================

/**
 * Record call args without non-cloneable platform objects.
 * AbortSignal breaks node:test worker structured-clone reporting.
 * Do not use `instanceof AbortSignal` — worker realms make that check fail
 * while the object is still an AbortSignal and still non-cloneable.
 */
function sanitizeValue(val) {
  if (val === null || val === undefined) return val;
  if (typeof val === 'function') return '[Function]';
  if (typeof val === 'symbol') return String(val);
  if (typeof val === 'object') {
    if ('signal' in val && val.signal != null) {
      const { signal, ...rest } = val;
      return sanitizeValue({ ...rest, signalPresent: true });
    }
    if (Array.isArray(val)) {
      return val.map(sanitizeValue);
    }
    // Plain objects or Error objects
    const cleanObj = {};
    for (const key of Object.keys(val)) {
      cleanObj[key] = sanitizeValue(val[key]);
    }
    return cleanObj;
  }
  return val;
}

function sanitizeArgsForRecording(args) {
  return args.map(sanitizeValue);
}

/**
 * Create a mock function that can track calls
 */
export function createMockFn(implementation = () => {}) {
  const calls = [];
  let returnValue = undefined;
  const originalImplementation = implementation;
  
  const fn = function(...args) {
    calls.push(sanitizeArgsForRecording(args));
    return implementation.apply(this, args);
  };
  
  fn.mockImplementation = (newImpl) => {
    implementation = newImpl;
    return fn;
  };

  fn.reset = () => {
    implementation = originalImplementation;
    calls.length = 0;
    return fn;
  };
  
  fn.mockReturnValue = (value) => {
    returnValue = value;
    implementation = () => value;
    return fn;
  };
  
  fn.mockResolvedValue = (value) => {
    returnValue = Promise.resolve(value);
    implementation = () => returnValue;
    return fn;
  };
  
  fn.mockRejectedValue = (error) => {
    returnValue = Promise.reject(error);
    implementation = () => returnValue;
    return fn;
  };
  
  fn.getCalls = () => [...calls];
  fn.lastCall = () => calls[calls.length - 1] || null;
  fn.clearCalls = () => { calls.length = 0; };
  fn.callCount = () => calls.length;
  
  return fn;
}

// ============================================================================
// Mock Chrome Extension APIs
// ============================================================================

/**
 * Create a fresh mock Chrome API object for each test
 * This prevents test pollution between test files
 */
export function createMockChrome() {
  const listeners = {
    runtime: { onMessage: new Set(), onConnect: new Set() },
    tabs: { onUpdated: new Set(), onRemoved: new Set(), onActivated: new Set() },
    storage: { onChanged: new Set() },
    webNavigation: { onCompleted: new Set() },
    action: { onClicked: new Set() },
  };

  const storage = new Map();
  const tabs = new Map();
  let lastTabId = 1;

  return {
    runtime: {
      lastError: null,
      id: 'test-extension-id',
      sendMessage: async function (message, callback) {
        // Dispatch to registered runtime.onMessage listeners the way Chrome
        // does: each listener receives (message, sender, sendResponse) and may
        // call sendResponse synchronously. The first response wins.
        let response = null;
        let responded = false;
        const sender = { id: this.id, url: 'chrome-extension://test/' };

        const sendResponse = (resp) => {
          if (responded) return;
          responded = true;
          response = resp;
        };

        for (const listener of listeners.runtime.onMessage) {
          try {
            listener(message, sender, sendResponse);
          } catch (e) {
            // A throwing listener should not abort the whole dispatch.
            if (!responded) {
              sendResponse({ success: false, error: e?.message || String(e) });
            }
          }
          if (responded) break;
        }

        if (!responded) {
          response = { success: false, error: 'No handler responded' };
        }

        if (callback) callback(response);
        return Promise.resolve(response);
      },
      onMessage: {
        listeners: listeners.runtime.onMessage,
        addListener: function(callback) {
          listeners.runtime.onMessage.add(callback);
          return true;
        },
        removeListener: function(callback) {
          listeners.runtime.onMessage.delete(callback);
          return true;
        },
        hasListener: function(callback) {
          return listeners.runtime.onMessage.has(callback);
        },
      },
      onConnect: {
        listeners: listeners.runtime.onConnect,
        addListener: function(callback) {
          listeners.runtime.onConnect.add(callback);
          return true;
        },
        removeListener: function(callback) {
          listeners.runtime.onConnect.delete(callback);
          return true;
        },
      },
      getURL: function(path) {
        return `chrome-extension://test/${path}`;
      },
      getManifest: async function() {
        return {
          name: 'Omega Browser Agent',
          version: '1.0.0',
          manifest_version: 3,
        };
      },
    },

    tabs: {
      TAB_ID_NONE: -1,
      lastTabId: 1,
      tabs: tabs,
      
      create: async function(options = {}) {
        const tabId = ++this.lastTabId;
        const tab = {
          id: tabId,
          windowId: 1,
          index: tabs.size,
          url: options?.url || 'about:blank',
          title: options?.title || '',
          active: options?.active || false,
          status: 'loading',
          pendingUrl: options?.url,
          ...options,
        };
        tabs.set(tabId, tab);
        
        // Simulate async status change
        setImmediate(() => {
          if (tabs.has(tabId)) {
            const t = tabs.get(tabId);
            t.status = 'complete';
            for (const listener of listeners.tabs.onUpdated) {
              try { listener(tabId, { status: 'complete' }, t); }
              catch (e) {}
            }
          }
        });
        
        return tab;
      },
      
      get: async function(tabId) {
        if (tabId === this.TAB_ID_NONE) {
          const error = new Error('Invalid tab ID');
          error.name = 'InvalidTabError';
          throw error;
        }
        const tab = tabs.get(tabId);
        if (!tab) {
          const error = new Error(`No tab with id: ${tabId}`);
          error.name = 'NoTabError';
          throw error;
        }
        return { ...tab };
      },
      
      getCurrent: async function() {
        const firstTab = tabs.values().next().value;
        return firstTab ? { ...firstTab, active: true } : null;
      },
      
      update: async function(tabId, updateProperties) {
        const tab = tabs.get(tabId);
        if (!tab) {
          const error = new Error(`No tab with id: ${tabId}`);
          error.name = 'NoTabError';
          throw error;
        }
        Object.assign(tab, updateProperties);
        if (updateProperties && updateProperties.active === true) {
          for (const other of tabs.values()) {
            if (other.id !== tabId) other.active = false;
          }
        }
        for (const listener of listeners.tabs.onUpdated) {
          try { listener(tabId, updateProperties, tab); }
          catch (e) {}
        }
        return tab;
      },
      
      remove: async function(tabId) {
        const tab = tabs.get(tabId);
        if (tab) {
          tabs.delete(tabId);
          for (const listener of listeners.tabs.onRemoved) {
            try { listener(tabId, { windowId: tab.windowId }); }
            catch (e) {}
          }
        }
      },
      
      query: async function(queryInfo = {}) {
        const allTabs = Array.from(tabs.values());
        let results = [...allTabs];
        if (queryInfo.active === true) {
          results = results.filter(t => t.active);
        }
        if (queryInfo.currentWindow === true) {
          results = results.filter(t => t.windowId === 1);
        }
        if (queryInfo.url) {
          const pattern = new RegExp(queryInfo.url.replace(/\*/g, '.*'));
          results = results.filter(t => pattern.test(t.url || t.pendingUrl || ''));
        }
        return results;
      },
      
      onUpdated: {
        listeners: listeners.tabs.onUpdated,
        addListener: function(callback) {
          listeners.tabs.onUpdated.add(callback);
          return true;
        },
        removeListener: function(callback) {
          listeners.tabs.onUpdated.delete(callback);
          return true;
        },
      },
      
      onRemoved: {
        listeners: listeners.tabs.onRemoved,
        addListener: function(callback) {
          listeners.tabs.onRemoved.add(callback);
          return true;
        },
        removeListener: function(callback) {
          listeners.tabs.onRemoved.delete(callback);
          return true;
        },
      },
      
      onActivated: {
        listeners: listeners.tabs.onActivated,
        addListener: function(callback) {
          listeners.tabs.onActivated.add(callback);
          return true;
        },
        removeListener: function(callback) {
          listeners.tabs.onActivated.delete(callback);
          return true;
        },
      },
    },

    offscreen: {
      hasDocument: async function() { return false; },
      createDocument: async function(options) { return { id: 'offscreen-doc' }; },
    },

    storage: {
      local: {
        storage: storage,
        get: async function(keys, callback) {
          const result = {};
          if (typeof keys === 'string') {
            result[keys] = storage.get(keys);
          } else if (Array.isArray(keys)) {
            for (const key of keys) result[key] = storage.get(key);
          } else if (keys == null) {
            for (const [key, value] of storage) result[key] = value;
          }
          if (callback) setImmediate(() => callback(result));
          return result;
        },
        set: async function(items, callback) {
          for (const [key, value] of Object.entries(items)) storage.set(key, value);
          for (const listener of listeners.storage.onChanged) {
            try { listener({ ...items }, 'local'); }
            catch (e) {}
          }
          if (callback) setImmediate(callback);
        },
        remove: async function(keys, callback) {
          if (typeof keys === 'string') storage.delete(keys);
          else if (Array.isArray(keys)) for (const key of keys) storage.delete(key);
          if (callback) setImmediate(callback);
        },
        clear: async function(callback) {
          storage.clear();
          if (callback) setImmediate(callback);
        },
        getBytesInUse: async function(keys, callback) {
          let total = 0;
          const getSize = (v) => JSON.stringify(v).length;
          if (typeof keys === 'string') total = getSize(storage.get(keys));
          else if (Array.isArray(keys)) for (const key of keys) total += getSize(storage.get(key));
          else for (const v of storage.values()) total += getSize(v);
          if (callback) setImmediate(() => callback(total));
          return total;
        },
        onChanged: {
          listeners: listeners.storage.onChanged,
          addListener: function(callback) {
            listeners.storage.onChanged.add(callback);
            return true;
          },
          removeListener: function(callback) {
            listeners.storage.onChanged.delete(callback);
            return true;
          },
        },
      },
      sync: {
        get: async function() { return {}; },
        set: async function() {},
        remove: async function() {},
        clear: async function() {},
      },
      onChanged: {
        addListener: function() { return true; },
        removeListener: function() { return true; },
      },
    },

    scripting: {
      executeScript: async function(options, callback) {
        const result = { result: null };
        if (callback) setImmediate(() => callback([result]));
        return [result];
      },
      insertCSS: async function() {},
      removeCSS: async function() {},
    },

    webNavigation: {
      onCompleted: {
        listeners: listeners.webNavigation.onCompleted,
        addListener: function(callback) {
          listeners.webNavigation.onCompleted.add(callback);
          return true;
        },
        removeListener: function(callback) {
          listeners.webNavigation.onCompleted.delete(callback);
          return true;
        },
      },
    },

    action: {
      disable: function() {},
      enable: function() {},
      onClicked: {
        listeners: listeners.action.onClicked,
        addListener: function(callback) {
          listeners.action.onClicked.add(callback);
          return true;
        },
        removeListener: function(callback) {
          listeners.action.onClicked.delete(callback);
          return true;
        },
      },
    },

    sidePanel: {
      open: function() {},
      getOptions: async function() { return {}; },
    },

    contextMenus: {
      create: function() {},
      remove: function() {},
      removeAll: function() {},
      update: function() {},
    },
  };
}

// Global mock chrome instance
export const mockChrome = createMockChrome();
global.chrome = mockChrome;

// ============================================================================
// Mock Fetch API
// ============================================================================

export function createMockFetch(defaultResponse = {}) {
  const calls = [];
  const mockFetch = async function(url, options = {}) {
    const { signal, ...restOptions } = options || {};
    calls.push({
      url,
      options: {
        ...restOptions,
        ...(signal !== undefined ? { signalPresent: !!signal } : {}),
      },
    });
    if (options?.mockResponse) return options.mockResponse;
    if (options?.mockError) throw options.mockError;
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: new Map([['content-type', 'application/json']]),
      json: async () => ({ ...defaultResponse }),
      text: async () => JSON.stringify(defaultResponse),
      blob: async () => new Blob([JSON.stringify(defaultResponse)]),
      arrayBuffer: async () => new TextEncoder().encode(JSON.stringify(defaultResponse)).buffer,
      clone: function() { return { ...this }; },
    };
  };
  mockFetch.getCalls = () => [...calls];
  mockFetch.clearCalls = () => { calls.length = 0; };
  mockFetch.lastCall = () => calls[calls.length - 1] || null;
  return mockFetch;
}

export const mockFetch = createMockFetch();
global.fetch = mockFetch;

// ============================================================================
// Test HTML Fixtures
// ============================================================================

export const testHtml = {
  simplePage: `<!DOCTYPE html><html><head><title>Test Page</title></head>
<body><h1>Welcome</h1><p>This is a test page.</p>
<button id="test-button">Click me</button>
<input id="test-input" type="text" value="">
<a href="/test" id="test-link">Test Link</a></body></html>`,

  searchResults: `<!DOCTYPE html><html><head><title>Search Results for Omega</title></head>
<body><div class="search-results">
<div class="result"><h2><a href="https://example.com/omega-docs">Omega Documentation</a></h2>
<p class="snippet">Official documentation for Omega AI</p><div class="url">https://example.com/omega-docs</div></div>
<div class="result"><h2><a href="https://example.com/omega-github">Omega on GitHub</a></h2>
<p class="snippet">GitHub repository for Omega project</p><div class="url">https://example.com/omega-github</div></div>
</div></body></html>`,

  articlePage: `<!DOCTYPE html><html><head><title>Understanding AI Agents</title>
<meta name="author" content="Jane Doe"><meta name="description" content="A comprehensive guide to AI agents"></head>
<body><article><header><h1>Understanding AI Agents</h1>
<p class="byline">By <span class="author">Jane Doe</span></p>
<time datetime="2024-01-15">January 15, 2024</time></header>
<div class="content"><p>Artificial Intelligence agents are transforming how we interact with computers.</p>
<p>In this article, we explore the capabilities and limitations of modern AI systems.</p>
<h2>How AI Agents Work</h2><p>AI agents use large language models to understand and generate human-like text.</p></div>
</article></body></html>`,

  productPage: `<!DOCTYPE html><html><head><title>Omega Pro</title></head>
<body><div class="product"><h1>Omega Pro - AI Assistant</h1>
<span class="price">$99.99</span><button class="add-to-cart">Add to Cart</button></div>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Omega Pro","price":"99.99"}</script>
</body></html>`,

  formPage: `<!DOCTYPE html><html><head><title>Login</title></head>
<body><form id="login-form">
<input type="text" name="username" id="username" placeholder="Enter username">
<input type="password" name="password" id="password" placeholder="Enter password">
<button type="submit">Login</button></form></body></html>`,

  linksPage: `<!DOCTYPE html><html><head><title>Navigation</title></head>
<body><nav id="main-nav"><a href="/home">Home</a><a href="/about">About</a>
<a href="/contact">Contact</a></nav></body></html>`,

  tablePage: `<!DOCTYPE html><html><head><title>Data</title></head>
<body><table><thead><tr><th>Name</th><th>Value</th></tr></thead>
<tbody><tr><td>Item 1</td><td>100</td></tr><tr><td>Item 2</td><td>200</td></tr></tbody></table></body></html>`,
};

// ============================================================================
// Assertion Utilities
// ============================================================================

export async function assertRejects(asyncFn, message) {
  let error;
  try {
    await asyncFn();
    assert.fail('Expected promise to reject');
  } catch (e) {
    error = e;
  }
  if (message && !String(error?.message || error).includes(message)) {
    assert.fail(`Expected error message to include "${message}", got: ${error?.message || error}`);
  }
  return error;
}

export function assertThrows(fn, message) {
  let error;
  try {
    fn();
    assert.fail('Expected function to throw');
  } catch (e) {
    error = e;
  }
  if (message && !String(error?.message || error).includes(message)) {
    assert.fail(`Expected error message to include "${message}", got: ${error?.message || error}`);
  }
  return error;
}

export async function waitFor(condition, { timeout = 1000, interval = 50 } = {}) {
  const startTime = Date.now();
  while (Date.now() - startTime < timeout) {
    try {
      if (await condition()) return true;
    } catch (e) {}
    await new Promise(resolve => setTimeout(resolve, interval));
  }
  return false;
}

export function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ============================================================================
// Test Setup/Teardown
// ============================================================================

export function setupTest() {
  const freshChrome = createMockChrome();
  const freshFetch = createMockFetch();
  global.chrome = freshChrome;
  global.fetch = freshFetch;
  return { chrome: freshChrome, fetch: freshFetch };
}

export function teardownTest() {
  delete global.chrome;
  delete global.fetch;
}

export default {
  createMockChrome,
  mockChrome,
  createMockFetch,
  mockFetch,
  testHtml,
  assertRejects,
  assertThrows,
  waitFor,
  delay,
  setupTest,
  teardownTest,
};
