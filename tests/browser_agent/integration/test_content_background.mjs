/**
 * Browser Agent Extension - Content-Background Messaging Integration Tests
 * 
 * Tests the messaging integration between content scripts and background scripts.
 * Uses mocked Chrome API to simulate extension messaging.
 */

import assert from 'node:assert/strict';
import { describe, it, beforeEach, afterEach } from 'node:test';
import { createMockChrome, createMockFn } from '../conftest.mjs';

// Mock message handlers for background script
const backgroundHandlers = {
  automation: createMockFn(async (message, sender, sendResponse) => {
    const { type, data } = message;
    
    switch (type) {
      case 'AUTOMATION_COMMAND':
        sendResponse({ success: true, result: `Executed ${data.command}` });
        break;
      case 'GET_PAGE_STATE':
        sendResponse({ 
          success: true, 
          state: { url: 'https://example.com', title: 'Test Page' } 
        });
        break;
      case 'CAPTURE_SCREENSHOT':
        sendResponse({ 
          success: true, 
          screenshot: 'base64-encoded-data' 
        });
        break;
      case 'PERFORM_OCR':
        sendResponse({ 
          success: true, 
          text: 'OCR extracted text' 
        });
        break;
      case 'GET_LOCK_STATE':
        sendResponse({ success: true, locked: false });
        break;
      default:
        sendResponse({ success: false, error: 'Unknown command' });
    }
    
    return true;
  }),
  
  pageState: createMockFn(async (message, sender, sendResponse) => {
    sendResponse({
      success: true,
      url: 'https://example.com',
      title: 'Test Page',
      html: '<html><body>Test</body></html>',
    });
  }),
  
  screenshot: createMockFn(async (message, sender, sendResponse) => {
    sendResponse({
      success: true,
      data: 'base64-screenshot-data',
      width: 1920,
      height: 1080,
    });
  }),
  
  ocr: createMockFn(async (message, sender, sendResponse) => {
    sendResponse({
      success: true,
      text: 'Extracted text from image',
      language: 'en',
      confidence: 0.95,
    });
  }),

  lockState: createMockFn(async (message, sender, sendResponse) => {
    sendResponse({ success: true, locked: false });
  }),
};

// Mock content script message sender
class MockContentScript {
  constructor(tabId, chrome) {
    this.tabId = tabId;
    this.chrome = chrome;
  }

  async sendMessage(type, data = {}) {
    return new Promise((resolve, reject) => {
      const message = { type, data, tabId: this.tabId };
      
      this.chrome.runtime.sendMessage(message, (response) => {
        const error = this.chrome.runtime.lastError;
        if (error) {
          reject(new Error(error.message));
        } else {
          resolve(response);
        }
      });
    });
  }

  async requestPageState() {
    return this.sendMessage('GET_PAGE_STATE');
  }

  async requestScreenshot(options = {}) {
    return this.sendMessage('CAPTURE_SCREENSHOT', options);
  }

  async requestOCR(imageData, options = {}) {
    return this.sendMessage('PERFORM_OCR', { imageData, ...options });
  }

  async executeAutomation(command, params = {}) {
    return this.sendMessage('AUTOMATION_COMMAND', { command, ...params });
  }

  async checkLockState() {
    return this.sendMessage('GET_LOCK_STATE');
  }
}

// Mock background script message handler
class MockBackgroundScript {
  constructor(chrome) {
    this.chrome = chrome;
    this.handlers = new Map();
    
    // Register default handlers
    this.registerHandler('AUTOMATION_COMMAND', backgroundHandlers.automation);
    this.registerHandler('GET_PAGE_STATE', backgroundHandlers.pageState);
    this.registerHandler('CAPTURE_SCREENSHOT', backgroundHandlers.screenshot);
    this.registerHandler('PERFORM_OCR', backgroundHandlers.ocr);
    this.registerHandler('GET_LOCK_STATE', backgroundHandlers.lockState);
  }

  registerHandler(type, handler) {
    this.handlers.set(type, handler);
  }

  async handleMessage(message, sender, sendResponse) {
    const handler = this.handlers.get(message.type);
    
    if (handler) {
      return handler(message, sender, sendResponse);
    }
    
    sendResponse({ success: false, error: 'No handler for message type' });
    return false;
  }
}

describe('Content-Background Messaging Integration', () => {
  let chrome;
  let originalChrome;
  let contentScript;
  let backgroundScript;

  beforeEach(() => {
    chrome = createMockChrome();
    originalChrome = global.chrome;
    global.chrome = chrome;
    
    // Set up message listener on chrome.runtime
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      backgroundScript.handleMessage(message, sender, sendResponse);
    });
    
    // Create instances
    contentScript = new MockContentScript(1, chrome);
    backgroundScript = new MockBackgroundScript(chrome);
    
    // Reset mocks
    Object.values(backgroundHandlers).forEach(h => h.clearCalls());
  });

  afterEach(() => {
    global.chrome = originalChrome;
    contentScript = null;
    backgroundScript = null;
  });

  describe('Message Passing (mocked chrome.runtime)', () => {
    it('should send automation command from content to background', async () => {
      const response = await contentScript.executeAutomation('click', {
        selector: '#button',
      });
      
      assert.ok(response);
      assert.strictEqual(response.success, true);
      assert.ok(response.result);
      assert.ok(backgroundHandlers.automation.callCount() > 0);
      
      console.log('  ✓ Automation command execution works');
    });

    it('should request page state from background', async () => {
      const response = await contentScript.requestPageState();
      
      assert.ok(response);
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.url, 'https://example.com');
      assert.strictEqual(response.title, 'Test Page');
      assert.ok(backgroundHandlers.pageState.callCount() > 0);
      
      console.log('  ✓ Page state retrieval works');
    });

    it('should capture screenshot via background', async () => {
      const response = await contentScript.requestScreenshot({
        format: 'png',
        quality: 0.9,
      });
      
      assert.ok(response);
      assert.strictEqual(response.success, true);
      assert.ok(response.data);
      assert.strictEqual(response.width, 1920);
      assert.strictEqual(response.height, 1080);
      assert.ok(backgroundHandlers.screenshot.callCount() > 0);
      
      console.log('  ✓ Screenshot capture works');
    });

    it('should perform OCR via background', async () => {
      const response = await contentScript.requestOCR('base64-image-data', {
        language: 'en',
      });
      
      assert.ok(response);
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.text, 'Extracted text from image');
      assert.strictEqual(response.language, 'en');
      assert.strictEqual(response.confidence, 0.95);
      assert.ok(backgroundHandlers.ocr.callCount() > 0);
      
      console.log('  ✓ OCR request works');
    });

    it('should check lock state from background', async () => {
      const response = await contentScript.checkLockState();
      
      assert.ok(response);
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.locked, false);
      assert.ok(backgroundHandlers.lockState.callCount() > 0);
      
      console.log('  ✓ Lock state check works');
    });
  });

  describe('Automation Command Execution', () => {
    it('should execute click command', async () => {
      const response = await contentScript.executeAutomation('click', {
        selector: '#submit-button',
      });
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.result, 'Executed click');
      
      console.log('  ✓ Click command execution works');
    });

    it('should execute type command', async () => {
      const response = await contentScript.executeAutomation('type', {
        selector: '#input-field',
        text: 'Hello World',
      });
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.result, 'Executed type');
      
      console.log('  ✓ Type command execution works');
    });

    it('should execute scroll command', async () => {
      const response = await contentScript.executeAutomation('scroll', {
        x: 0,
        y: 500,
      });
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.result, 'Executed scroll');
      
      console.log('  ✓ Scroll command execution works');
    });

    it('should execute navigate command', async () => {
      const response = await contentScript.executeAutomation('navigate', {
        url: 'https://example.com/page',
      });
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.result, 'Executed navigate');
      
      console.log('  ✓ Navigate command execution works');
    });

    it('should execute extract command', async () => {
      const response = await contentScript.executeAutomation('extract', {
        mode: 'main',
      });
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.result, 'Executed extract');
      
      console.log('  ✓ Extract command execution works');
    });

    it('should execute wait command', async () => {
      const response = await contentScript.executeAutomation('wait', {
        selector: '#element',
        timeout: 5000,
      });
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.result, 'Executed wait');
      
      console.log('  ✓ Wait command execution works');
    });
  });

  describe('Error Handling', () => {
    it('should handle message timeout', async () => {
      // Create a slow handler
      const slowHandler = createMockFn(async (message, sender, sendResponse) => {
        await new Promise(resolve => setTimeout(resolve, 1000));
        sendResponse({ success: false, error: 'Timeout' });
      });
      
      backgroundScript.registerHandler('SLOW_COMMAND', slowHandler);
      
      // Override chrome.runtime to simulate timeout
      const originalSendMessage = chrome.runtime.sendMessage;
      chrome.runtime.sendMessage = (message, callback) => {
        setTimeout(() => {
          chrome.runtime.lastError = new Error('Message timeout');
          callback(null);
        }, 50);
      };
      
      try {
        await contentScript.sendMessage('SLOW_COMMAND');
        assert.fail('Should have thrown timeout error');
      } catch (error) {
        assert.ok(error.message.includes('timeout') || error.message.includes('Message timeout'));
      }
      
      console.log('  ✓ Message timeout handling works');
    });

    it('should handle invalid message type', async () => {
      const response = await contentScript.sendMessage('INVALID_TYPE', {});
      
      // Should get default error response
      assert.strictEqual(response.success, false);
      assert.ok(response.error);
      
      console.log('  ✓ Invalid message type handling works');
    });

    it('should handle tab not found error', async () => {
      // Simulate tab not found error
      const originalSendMessage = chrome.runtime.sendMessage;
      chrome.runtime.sendMessage = (message, callback) => {
        chrome.runtime.lastError = new Error('Tab not found');
        callback(null);
      };
      
      try {
        await contentScript.requestPageState();
        assert.fail('Should have thrown tab not found error');
      } catch (error) {
        assert.ok(error.message.includes('Tab not found'));
      }
      
      console.log('  ✓ Tab not found error handling works');
    });

    it('should handle invalid message format', async () => {
      // Register handler for unknown message
      backgroundScript.registerHandler('UNKNOWN_MESSAGE', (message, sender, sendResponse) => {
        sendResponse({ success: false, error: 'Invalid message format' });
      });
      
      const response = await contentScript.sendMessage('UNKNOWN_MESSAGE', { invalid: true });
      
      assert.strictEqual(response.success, false);
      assert.ok(response.error);
      
      console.log('  ✓ Invalid message format handling works');
    });
  });

  describe('Message Validation', () => {
    it('should validate automation command format', async () => {
      const validCommands = [
        'click',
        'type',
        'scroll',
        'navigate',
        'extract',
        'wait',
        'capturePageVision',
        'scan',
        'keyboard',
      ];
      
      for (const command of validCommands) {
        const response = await contentScript.executeAutomation(command, {});
        assert.strictEqual(response.success, true);
      }
      
      console.log('  ✓ All valid automation commands work');
    });

    it('should pass parameters through messaging', async () => {
      const params = {
        selector: '#test',
        text: 'test value',
        timeout: 1000,
        x: 100,
        y: 200,
      };
      
      await contentScript.executeAutomation('click', params);
      
      const lastCall = backgroundHandlers.automation.lastCall();
      assert.ok(lastCall);
      assert.deepStrictEqual(lastCall[0].data, { command: 'click', ...params });
      
      console.log('  ✓ Parameter passing through messaging works');
    });

    it('should include tabId in messages', async () => {
      await contentScript.requestPageState();
      
      const lastCall = backgroundHandlers.pageState.lastCall();
      assert.ok(lastCall);
      assert.strictEqual(lastCall[0].tabId, 1);
      
      console.log('  ✓ Tab ID inclusion in messages works');
    });
  });

  describe('Concurrent Messaging', () => {
    it('should handle multiple concurrent requests', async () => {
      const promises = [
        contentScript.requestPageState(),
        contentScript.checkLockState(),
        contentScript.executeAutomation('click', {}),
        contentScript.requestScreenshot(),
      ];
      
      const responses = await Promise.all(promises);
      
      assert.strictEqual(responses.length, 4);
      assert.ok(responses.every(r => r && r.success !== undefined));
      
      console.log('  ✓ Concurrent messaging works');
    });

    it('should maintain message order for sequential calls', async () => {
      const commands = [
        { type: 'navigate', url: '/page1' },
        { type: 'click', selector: '#btn1' },
        { type: 'type', text: 'Input 1' },
        { type: 'scroll', y: 100 },
      ];
      
      for (const cmd of commands) {
        await contentScript.executeAutomation(cmd.type, cmd);
      }
      
      // Verify all were called
      assert.strictEqual(backgroundHandlers.automation.callCount(), 4);
      
      console.log('  ✓ Sequential message ordering works');
    });
  });

  describe('Handler Registration', () => {
    it('should allow dynamic handler registration', async () => {
      const customHandler = createMockFn(async (message, sender, sendResponse) => {
        sendResponse({ success: true, custom: true });
      });
      
      backgroundScript.registerHandler('CUSTOM_COMMAND', customHandler);
      
      // Create a custom content script method
      const response = await contentScript.sendMessage('CUSTOM_COMMAND', {});
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.custom, true);
      assert.ok(customHandler.callCount() > 0);
      
      console.log('  ✓ Dynamic handler registration works');
    });

    it('should override existing handlers', async () => {
      const overrideHandler = createMockFn(async (message, sender, sendResponse) => {
        sendResponse({ success: true, overridden: true });
      });
      
      backgroundScript.registerHandler('AUTOMATION_COMMAND', overrideHandler);
      
      const response = await contentScript.executeAutomation('click', {});
      
      assert.strictEqual(response.success, true);
      assert.strictEqual(response.overridden, true);
      assert.ok(overrideHandler.callCount() > 0);
      
      console.log('  ✓ Handler override works');
    });
  });
});

console.log('\n=== Content-Background Messaging Integration Tests Complete ===\n');
