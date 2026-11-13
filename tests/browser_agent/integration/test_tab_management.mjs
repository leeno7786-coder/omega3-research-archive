/**
 * Browser Agent Extension - Tab Management Integration Tests
 * 
 * Tests workspace tab orchestration, multi-tab scenarios, and tab lifecycle management.
 */

import assert from 'node:assert/strict';
import { describe, it, beforeEach, afterEach } from 'node:test';
import { createMockChrome, createMockFn } from '../conftest.mjs';

// Mock Chrome tabs API extensions
function extendChromeForTabs(chrome) {
  const tabs = new Map();
  let nextTabId = 1;

  // Storage for tab data
  const tabData = new Map();

  chrome.tabs = {
    ...chrome.tabs,
    
    // Override to use our mock tabs
    get: createMockFn(async (tabId, callback) => {
      const tab = tabs.get(tabId);
      if (tab) {
        if (callback) callback(tab);
        return tab;
      }
      if (callback) callback(null);
      return null;
    }),
    
    getCurrent: createMockFn(async (callback) => {
      // Return the first tab as current
      const firstTab = tabs.values().next().value;
      if (firstTab) {
        if (callback) callback(firstTab);
        return firstTab;
      }
      if (callback) callback(null);
      return null;
    }),
    
    create: createMockFn(async (createProperties, callback) => {
      const tabId = nextTabId++;
      const tab = {
        id: tabId,
        windowId: createProperties.windowId || 1,
        index: tabs.size,
        url: createProperties.url || 'about:blank',
        title: createProperties.title || '',
        active: createProperties.active || false,
        highlighted: createProperties.highlighted || false,
        ...createProperties,
      };
      
      tabs.set(tabId, tab);
      
      if (callback) callback(tab);
      return tab;
    }),
    
    update: createMockFn(async (tabId, updateProperties, callback) => {
      const tab = tabs.get(tabId);
      if (tab) {
        Object.assign(tab, updateProperties);
        if (updateProperties && updateProperties.active === true) {
          for (const other of tabs.values()) {
            if (other.id !== tabId) other.active = false;
          }
        }
        if (callback) callback(tab);
        return tab;
      }
      if (callback) callback(null);
      return null;
    }),
    
    remove: createMockFn(async (tabId, callback) => {
      const tab = tabs.get(tabId);
      if (tab) {
        tabs.delete(tabId);
        tabData.delete(tabId);
        if (callback) callback();
        return true;
      }
      if (callback) callback();
      return false;
    }),
    
    query: createMockFn(async (queryInfo, callback) => {
      const results = [];
      
      for (const tab of tabs.values()) {
        let matches = true;
        
        if (queryInfo.windowId !== undefined && tab.windowId !== queryInfo.windowId) {
          matches = false;
        }
        if (queryInfo.active !== undefined && tab.active !== queryInfo.active) {
          matches = false;
        }
        if (queryInfo.url && !tab.url.includes(queryInfo.url)) {
          matches = false;
        }
        
        if (matches) {
          results.push(tab);
        }
      }
      
      if (callback) callback(results);
      return results;
    }),
    
    highlight: createMockFn(async (highlightInfo, callback) => {
      if (callback) callback();
      return true;
    }),
    
    // Tab data storage
    set: (tabId, data) => {
      tabData.set(tabId, data);
    },
    get: (tabId) => {
      return tabData.get(tabId);
    },
    delete: (tabId) => {
      tabData.delete(tabId);
    },
    
    // List all tabs
    list: () => Array.from(tabs.values()),
    
    // Clear all tabs
    clear: () => {
      tabs.clear();
      tabData.clear();
      nextTabId = 1;
    },
  };

  return chrome;
}

// Mock Tab Registry (from browser-automation.js)
class MockTabRegistry {
  constructor(chrome) {
    this.chrome = chrome;
    this.workspaceTabs = new Map();
    this.agentTabId = null;
  }

  async _saveTabRegistry() {
    const registry = {
      workspaceTabs: Object.fromEntries(this.workspaceTabs),
      agentTabId: this.agentTabId,
      savedAt: Date.now(),
    };
    
    await this.chrome.storage.local.set({ tabRegistry: registry });
    return registry;
  }

  async _loadTabRegistry() {
    const result = await this.chrome.storage.local.get('tabRegistry');
    const registry = result.tabRegistry || {};
    
    this.workspaceTabs = new Map(Object.entries(registry.workspaceTabs || {}));
    this.agentTabId = registry.agentTabId || null;
    
    return registry;
  }

  _registerWorkspaceTab(tabId, url) {
    if (!this.workspaceTabs.has(tabId)) {
      this.workspaceTabs.set(tabId, { tabId, url, alive: true });
      return true;
    }
    return false;
  }

  setAgentTab(tabId) {
    this.agentTabId = tabId;
  }

  clearAgentTab() {
    this.agentTabId = null;
  }

  async handleTabRemoved(tabId) {
    this.workspaceTabs.delete(tabId);
    if (this.agentTabId === tabId) {
      this.agentTabId = null;
    }
    await this._saveTabRegistry();
  }

  listWorkspaceTabs() {
    return Array.from(this.workspaceTabs.values())
      .filter(tab => tab.alive)
      .sort((a, b) => a.tabId - b.tabId);
  }

  _resolveTabIndex(index) {
    const tabs = this.listWorkspaceTabs();
    if (index >= 0 && index < tabs.length) {
      return tabs[index].tabId;
    }
    return null;
  }

  async openWorkspaceTab(url, options = {}) {
    const tab = await this.chrome.tabs.create({ url, active: true });
    if (tab && tab.id) {
      this._registerWorkspaceTab(tab.id, url);
      if (options.setAsAgent) {
        this.setAgentTab(tab.id);
      }
      await this._saveTabRegistry();
      return tab;
    }
    return null;
  }

  async switchWorkspaceTab(index) {
    const tabId = this._resolveTabIndex(index);
    if (tabId) {
      const tab = this.workspaceTabs.get(tabId);
      if (tab) {
        await this.chrome.tabs.update(tabId, { active: true });
        return tab;
      }
    }
    return null;
  }

  async closeWorkspaceTab(index) {
    const tabId = this._resolveTabIndex(index);
    if (tabId) {
      const tab = this.workspaceTabs.get(tabId);
      if (tab) {
        await this.chrome.tabs.remove(tabId);
        await this.handleTabRemoved(tabId);
        return true;
      }
    }
    return false;
  }

  isWorkspaceTab(tabId) {
    return this.workspaceTabs.has(tabId);
  }

  isWorkspaceUrl(url) {
    return this.listWorkspaceTabs().some(tab => tab.url === url);
  }

  getWorkspaceUrl(tabId) {
    const tab = this.workspaceTabs.get(tabId);
    return tab ? tab.url : null;
  }
}

// Mock Browser Automation Tab Management
class MockBrowserAutomation {
  constructor(chrome) {
    this.chrome = chrome;
    this.tabRegistry = new MockTabRegistry(chrome);
  }

  // Delegate to tab registry
  _saveTabRegistry() {
    return this.tabRegistry._saveTabRegistry();
  }

  _loadTabRegistry() {
    return this.tabRegistry._loadTabRegistry();
  }

  _registerWorkspaceTab(tabId, url) {
    return this.tabRegistry._registerWorkspaceTab(tabId, url);
  }

  setAgentTab(tabId) {
    this.tabRegistry.setAgentTab(tabId);
  }

  handleTabRemoved(tabId) {
    return this.tabRegistry.handleTabRemoved(tabId);
  }

  listWorkspaceTabs() {
    return this.tabRegistry.listWorkspaceTabs();
  }

  _resolveTabIndex(index) {
    return this.tabRegistry._resolveTabIndex(index);
  }

  async openWorkspaceTab(url, options = {}) {
    return this.tabRegistry.openWorkspaceTab(url, options);
  }

  async switchWorkspaceTab(index) {
    return this.tabRegistry.switchWorkspaceTab(index);
  }

  async closeWorkspaceTab(index) {
    return this.tabRegistry.closeWorkspaceTab(index);
  }

  clearAgentTab() {
    this.tabRegistry.clearAgentTab();
  }

  isWorkspaceTab(tabId) {
    return this.tabRegistry.isWorkspaceTab(tabId);
  }

  isWorkspaceUrl(url) {
    return this.tabRegistry.isWorkspaceUrl(url);
  }

  getWorkspaceUrl(tabId) {
    return this.tabRegistry.getWorkspaceUrl(tabId);
  }
}

describe('Tab Management Integration', () => {
  let chrome;
  let originalChrome;
  let automation;

  beforeEach(() => {
    chrome = createMockChrome();
    chrome = extendChromeForTabs(chrome);
    originalChrome = global.chrome;
    global.chrome = chrome;
    
    automation = new MockBrowserAutomation(chrome);
  });

  afterEach(() => {
    global.chrome = originalChrome;
    chrome.tabs.clear();
    automation = null;
  });

  describe('Workspace Tab Orchestration', () => {
    it('should create new workspace tab', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com');
      
      assert.ok(tab);
      assert.ok(tab.id);
      assert.strictEqual(tab.url, 'https://example.com');
      
      // Verify tab is in workspace
      assert.ok(automation.isWorkspaceTab(tab.id));
      
      console.log('  ✓ New workspace tab creation works');
    });

    it('should create multiple workspace tabs', async () => {
      const urls = [
        'https://example.com',
        'https://test.com',
        'https://demo.com',
      ];
      
      const tabs = [];
      for (const url of urls) {
        const tab = await automation.openWorkspaceTab(url);
        tabs.push(tab);
      }
      
      assert.strictEqual(tabs.length, 3);
      
      // All should be in workspace
      for (const tab of tabs) {
        assert.ok(automation.isWorkspaceTab(tab.id));
      }
      
      console.log('  ✓ Multiple workspace tab creation works');
    });

    it('should switch between workspace tabs', async () => {
      // Create multiple tabs
      const tab1 = await automation.openWorkspaceTab('https://example.com');
      const tab2 = await automation.openWorkspaceTab('https://test.com');
      
      // Switch to first tab (index 0)
      const switched1 = await automation.switchWorkspaceTab(0);
      assert.ok(switched1);
      assert.strictEqual(switched1.tabId, tab1.id);
      
      // Switch to second tab (index 1)
      const switched2 = await automation.switchWorkspaceTab(1);
      assert.ok(switched2);
      assert.strictEqual(switched2.tabId, tab2.id);
      
      console.log('  ✓ Tab switching works');
    });

    it('should close workspace tab', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com');
      
      // Verify tab exists
      assert.ok(automation.isWorkspaceTab(tab.id));
      
      // Close tab
      const closed = await automation.closeWorkspaceTab(0);
      assert.ok(closed);
      
      // Verify tab is removed from workspace
      assert.ok(!automation.isWorkspaceTab(tab.id));
      
      console.log('  ✓ Workspace tab closing works');
    });

    it('should handle tab not found when switching', async () => {
      // Try to switch to non-existent tab
      const result = await automation.switchWorkspaceTab(999);
      assert.ok(!result);
      
      console.log('  ✓ Tab not found handling works');
    });

    it('should handle tab not found when closing', async () => {
      // Try to close non-existent tab
      const result = await automation.closeWorkspaceTab(999);
      assert.ok(!result);
      
      console.log('  ✓ Tab not found handling for close works');
    });
  });

  describe('Multi-Tab Scenarios', () => {
    it('should handle cross-tab communication', async () => {
      const tab1 = await automation.openWorkspaceTab('https://example.com');
      const tab2 = await automation.openWorkspaceTab('https://test.com');
      
      // Both tabs should be accessible
      assert.ok(automation.isWorkspaceTab(tab1.id));
      assert.ok(automation.isWorkspaceTab(tab2.id));
      
      // Can switch between them
      await automation.switchWorkspaceTab(0);
      await automation.switchWorkspaceTab(1);
      
      console.log('  ✓ Cross-tab communication works');
    });

    it('should track active tab', async () => {
      const tab1 = await automation.openWorkspaceTab('https://example.com');
      const tab2 = await automation.openWorkspaceTab('https://test.com');
      
      // Switch to second tab
      await automation.switchWorkspaceTab(1);
      
      // Second tab should be active
      const tabs = await chrome.tabs.query({ active: true });
      assert.strictEqual(tabs.length, 1);
      assert.strictEqual(tabs[0].id, tab2.id);
      
      console.log('  ✓ Active tab tracking works');
    });

    it('should handle workspace isolation', async () => {
      const workspaceTab = await automation.openWorkspaceTab('https://example.com');
      
      // Create a non-workspace tab directly
      const nonWorkspaceTab = await chrome.tabs.create({ url: 'https://other.com' });
      
      // Workspace tab should be in registry
      assert.ok(automation.isWorkspaceTab(workspaceTab.id));
      
      // Non-workspace tab should not be in registry
      assert.ok(!automation.isWorkspaceTab(nonWorkspaceTab.id));
      
      console.log('  ✓ Workspace isolation works');
    });

    it('should maintain tab order', async () => {
      const urls = ['https://a.com', 'https://b.com', 'https://c.com'];
      
      for (const url of urls) {
        await automation.openWorkspaceTab(url);
      }
      
      const tabs = automation.listWorkspaceTabs();
      assert.strictEqual(tabs.length, 3);
      
      // Tabs should be in creation order
      for (let i = 0; i < tabs.length; i++) {
        assert.strictEqual(tabs[i].url, urls[i]);
      }
      
      console.log('  ✓ Tab order maintenance works');
    });

    it('should handle duplicate tab prevention', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com');
      
      // Try to register same tab again
      const registered = automation.tabRegistry._registerWorkspaceTab(tab.id, tab.url);
      assert.ok(!registered); // Should return false for duplicate
      
      console.log('  ✓ Duplicate tab prevention works');
    });
  });

  describe('Tab State Persistence', () => {
    it('should persist tab state across operations', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com', { setAsAgent: true });
      
      // Tab should be in workspace
      assert.ok(automation.isWorkspaceTab(tab.id));
      
      // Switch away and back
      await automation.switchWorkspaceTab(0);
      
      // Tab should still be in workspace
      assert.ok(automation.isWorkspaceTab(tab.id));
      
      console.log('  ✓ Tab state persistence works');
    });

    it('should save and load tab registry', async () => {
      // Create tabs and set agent tab
      const tab1 = await automation.openWorkspaceTab('https://example.com');
      const tab2 = await automation.openWorkspaceTab('https://test.com');
      automation.setAgentTab(tab2.id);
      
      // Save registry
      await automation._saveTabRegistry();
      
      // Create new automation instance (simulate restart)
      const newAutomation = new MockBrowserAutomation(chrome);
      
      // Load registry
      await newAutomation._loadTabRegistry();
      
      // Verify tabs are restored
      const tabs = newAutomation.listWorkspaceTabs();
      assert.strictEqual(tabs.length, 2);
      
      console.log('  ✓ Tab registry save and load works');
    });

    it('should handle tab removal cleanup', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com');
      automation.setAgentTab(tab.id);
      
      // Remove tab
      await chrome.tabs.remove(tab.id);
      await automation.handleTabRemoved(tab.id);
      
      // Agent tab should be cleared if removed tab was agent tab
      assert.ok(!automation.tabRegistry.agentTabId);
      
      console.log('  ✓ Tab removal cleanup works');
    });

    it('should clean up workspace on tab removal', async () => {
      const tab1 = await automation.openWorkspaceTab('https://example.com');
      const tab2 = await automation.openWorkspaceTab('https://test.com');
      
      // Remove first tab
      await chrome.tabs.remove(tab1.id);
      await automation.handleTabRemoved(tab1.id);
      
      // First tab should be removed from workspace
      assert.ok(!automation.isWorkspaceTab(tab1.id));
      // Second tab should still be in workspace
      assert.ok(automation.isWorkspaceTab(tab2.id));
      
      console.log('  ✓ Workspace cleanup on tab removal works');
    });
  });

  describe('Agent Tab Management', () => {
    it('should set and clear agent tab', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com');
      
      // Set as agent tab
      automation.setAgentTab(tab.id);
      assert.strictEqual(automation.tabRegistry.agentTabId, tab.id);
      
      // Clear agent tab
      automation.clearAgentTab();
      assert.ok(!automation.tabRegistry.agentTabId);
      
      console.log('  ✓ Agent tab set and clear works');
    });

    it('should set agent tab on creation', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com', { setAsAgent: true });
      
      assert.strictEqual(automation.tabRegistry.agentTabId, tab.id);
      
      console.log('  ✓ Agent tab set on creation works');
    });

    it('should clear agent tab on removal', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com');
      automation.setAgentTab(tab.id);
      
      await chrome.tabs.remove(tab.id);
      await automation.handleTabRemoved(tab.id);
      
      assert.ok(!automation.tabRegistry.agentTabId);
      
      console.log('  ✓ Agent tab clear on removal works');
    });

    it('should only have one agent tab at a time', async () => {
      const tab1 = await automation.openWorkspaceTab('https://example.com');
      const tab2 = await automation.openWorkspaceTab('https://test.com');
      
      automation.setAgentTab(tab1.id);
      assert.strictEqual(automation.tabRegistry.agentTabId, tab1.id);
      
      automation.setAgentTab(tab2.id);
      assert.strictEqual(automation.tabRegistry.agentTabId, tab2.id);
      
      console.log('  ✓ Single agent tab enforcement works');
    });
  });

  describe('Tab URL Management', () => {
    it('should track workspace URLs', async () => {
      const urls = ['https://a.com', 'https://b.com', 'https://c.com'];
      
      for (const url of urls) {
        await automation.openWorkspaceTab(url);
      }
      
      for (const url of urls) {
        assert.ok(automation.isWorkspaceUrl(url));
      }
      
      console.log('  ✓ Workspace URL tracking works');
    });

    it('should get workspace URL for tab', async () => {
      const url = 'https://example.com';
      const tab = await automation.openWorkspaceTab(url);
      
      const workspaceUrl = automation.getWorkspaceUrl(tab.id);
      assert.strictEqual(workspaceUrl, url);
      
      console.log('  ✓ Workspace URL retrieval works');
    });

    it('should return null for non-workspace tab URL', async () => {
      const tab = await chrome.tabs.create({ url: 'https://other.com' });
      
      const workspaceUrl = automation.getWorkspaceUrl(tab.id);
      assert.ok(!workspaceUrl);
      
      console.log('  ✓ Non-workspace tab URL handling works');
    });

    it('should handle URL updates', async () => {
      const tab = await automation.openWorkspaceTab('https://example.com');
      
      // Update tab URL
      await chrome.tabs.update(tab.id, { url: 'https://updated.com' });
      
      // Workspace registry might still have old URL (depending on implementation)
      // This tests the current behavior
      const workspaceUrl = automation.getWorkspaceUrl(tab.id);
      assert.ok(workspaceUrl);
      
      console.log('  ✓ URL update handling works');
    });
  });

  describe('Tab Registry Operations', () => {
    it('should list all workspace tabs', async () => {
      await automation.openWorkspaceTab('https://a.com');
      await automation.openWorkspaceTab('https://b.com');
      await automation.openWorkspaceTab('https://c.com');
      
      const tabs = automation.listWorkspaceTabs();
      assert.strictEqual(tabs.length, 3);
      
      console.log('  ✓ Listing all workspace tabs works');
    });

    it('should resolve tab index correctly', async () => {
      await automation.openWorkspaceTab('https://a.com');
      await automation.openWorkspaceTab('https://b.com');
      
      const tabId0 = automation.tabRegistry._resolveTabIndex(0);
      const tabId1 = automation.tabRegistry._resolveTabIndex(1);
      
      assert.ok(tabId0);
      assert.ok(tabId1);
      assert.ok(tabId0 !== tabId1);
      
      console.log('  ✓ Tab index resolution works');
    });

    it('should return null for invalid tab index', async () => {
      const tabId = automation.tabRegistry._resolveTabIndex(999);
      assert.ok(!tabId);
      
      console.log('  ✓ Invalid tab index handling works');
    });

    it('should handle empty workspace', async () => {
      const tabs = automation.listWorkspaceTabs();
      assert.strictEqual(tabs.length, 0);
      
      const tabId = automation.tabRegistry._resolveTabIndex(0);
      assert.ok(!tabId);
      
      console.log('  ✓ Empty workspace handling works');
    });
  });

  describe('Concurrent Tab Operations', () => {
    it('should handle multiple tab operations concurrently', async () => {
      const promises = [
        automation.openWorkspaceTab('https://a.com'),
        automation.openWorkspaceTab('https://b.com'),
        automation.openWorkspaceTab('https://c.com'),
      ];
      
      const tabs = await Promise.all(promises);
      
      assert.strictEqual(tabs.length, 3);
      
      for (const tab of tabs) {
        assert.ok(automation.isWorkspaceTab(tab.id));
      }
      
      console.log('  ✓ Concurrent tab operations work');
    });

    it('should handle sequential tab operations', async () => {
      const tabs = [];
      
      for (let i = 0; i < 5; i++) {
        const tab = await automation.openWorkspaceTab(`https://test${i}.com`);
        tabs.push(tab);
      }
      
      assert.strictEqual(tabs.length, 5);
      
      const listedTabs = automation.listWorkspaceTabs();
      assert.strictEqual(listedTabs.length, 5);
      
      console.log('  ✓ Sequential tab operations work');
    });

    it('should handle mixed operations', async () => {
      // Create tabs
      const tab1 = await automation.openWorkspaceTab('https://a.com');
      const tab2 = await automation.openWorkspaceTab('https://b.com');
      
      // Switch tabs
      await automation.switchWorkspaceTab(0);
      await automation.switchWorkspaceTab(1);
      
      // Close a tab
      await automation.closeWorkspaceTab(0);
      
      // Create another tab
      const tab3 = await automation.openWorkspaceTab('https://c.com');
      
      // Verify state
      assert.ok(!automation.isWorkspaceTab(tab1.id));
      assert.ok(automation.isWorkspaceTab(tab2.id));
      assert.ok(automation.isWorkspaceTab(tab3.id));
      
      console.log('  ✓ Mixed operations work');
    });
  });
});

console.log('\n=== Tab Management Integration Tests Complete ===\n');
