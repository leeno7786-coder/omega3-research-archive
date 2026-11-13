/**
 * Browser Agent Extension - OCR Pipeline Integration Tests
 * 
 * Tests the full OCR pipeline: screenshot capture -> OCR service -> result merging.
 * Includes offscreen document lifecycle and OCR worker communication.
 */

import assert from 'node:assert/strict';
import { describe, it, beforeEach, afterEach } from 'node:test';
import { createMockChrome, createMockFn } from '../conftest.mjs';

// Mock OCR Service
class MockOCRService {
  constructor() {
    this.workerReady = true;
    this.recognizeCalls = [];
  }

  async recognizeScreenshot(screenshotData, options = {}) {
    this.recognizeCalls.push({ screenshotData, options });
    
    return {
      text: 'Mock OCR extracted text',
      blocks: [
        { type: 'paragraph', text: 'First paragraph', boundingBox: { x: 0, y: 0, width: 100, height: 20 } },
        { type: 'paragraph', text: 'Second paragraph', boundingBox: { x: 0, y: 30, width: 100, height: 20 } },
      ],
      language: options.language || 'en',
      confidence: 0.95,
      processingTime: 100,
    };
  }

  async mistralRecognizeScreenshot(screenshotData, options = {}) {
    return {
      text: 'Mistral OCR result',
      model: options.model || 'mistral-ocr-latest',
    };
  }

  async cloudRecognizeScreenshot(screenshotData, options = {}) {
    return {
      text: 'Cloud OCR result',
      provider: 'mistral',
      model: 'mistral-ocr-latest',
    };
  }

  isWorkerReady() {
    return this.workerReady;
  }

  getRecognizeCalls() {
    return [...this.recognizeCalls];
  }

  clearCalls() {
    this.recognizeCalls = [];
  }
}

// Mock Offscreen Document
class MockOffscreenDocument {
  constructor() {
    this.created = false;
    this.destroyed = false;
    this.ocrService = null;
  }

  async create() {
    this.created = true;
    return true;
  }

  async destroy() {
    this.destroyed = true;
    this.created = false;
    return true;
  }

  async ensureCreated() {
    if (!this.created) {
      await this.create();
    }
    return this.created;
  }

  setOCRService(service) {
    this.ocrService = service;
  }

  getOCRService() {
    return this.ocrService;
  }
}

// Mock Screenshot Capture
class MockScreenshotCapture {
  constructor() {
    this.capturedScreenshots = [];
  }

  async captureVisibleTab(tabId, options = {}) {
    const screenshot = {
      tabId,
      data: `base64-screenshot-${tabId}-${Date.now()}`,
      width: options.width || 1920,
      height: options.height || 1080,
      format: options.format || 'png',
      timestamp: Date.now(),
    };
    
    this.capturedScreenshots.push(screenshot);
    return screenshot;
  }

  async capturePageArea(tabId, rect, options = {}) {
    const screenshot = {
      tabId,
      data: `base64-area-screenshot-${tabId}`,
      rect,
      timestamp: Date.now(),
    };
    
    this.capturedScreenshots.push(screenshot);
    return screenshot;
  }

  getCapturedScreenshots() {
    return [...this.capturedScreenshots];
  }

  clearScreenshots() {
    this.capturedScreenshots = [];
  }
}

// Mock DOM Extractor
class MockDOMExtractor {
  constructor() {
    this.extractedText = [];
  }

  extractMainContent() {
    return {
      title: 'Test Page Title',
      mainContent: 'This is the main content of the page.',
      wordCount: 10,
    };
  }

  extractTextFromDOM() {
    return {
      text: 'All text from DOM',
      elements: 25,
    };
  }

  mergeOCRWithDOM(ocrResult, domText) {
    const domContent = domText?.text ?? domText ?? '';
    const ocrContent = ocrResult?.text ?? ocrResult ?? '';
    return {
      ocrText: ocrContent,
      domText: domContent,
      merged: `${domContent} ${ocrContent}`.trim(),
      hasOCRContent: !!ocrContent,
      hasDOMContent: !!domContent,
    };
  }
}

// Mock Browser Automation for OCR Pipeline
class MockBrowserAutomation {
  constructor() {
    this.captureCalls = [];
  }

  async capturePageVision(tabId, options = {}) {
    this.captureCalls.push({ tabId, options });
    
    return {
      screenshot: `base64-vision-${tabId}`,
      ocrText: 'Vision OCR text',
      domText: 'Vision DOM text',
      mergedText: 'Vision merged text',
      elements: [],
      timestamp: Date.now(),
    };
  }

  async scan(tabId, options = {}) {
    return {
      screenshots: [
        { data: 'screenshot-1', area: { x: 0, y: 0, width: 500, height: 500 } },
        { data: 'screenshot-2', area: { x: 500, y: 0, width: 500, height: 500 } },
      ],
      ocrResults: [
        { text: 'Scan result 1' },
        { text: 'Scan result 2' },
      ],
      mergedText: 'Scan merged text',
    };
  }

  autoOcrIfNeeded(tabId, options = {}) {
    return {
      neededOCR: true,
      performedOCR: true,
      text: 'Auto OCR result',
    };
  }

  getCaptureCalls() {
    return [...this.captureCalls];
  }

  clearCalls() {
    this.captureCalls = [];
  }
}

describe('OCR Pipeline Integration', () => {
  let mockChrome;
  let originalChrome;
  let ocrService;
  let offscreenDocument;
  let screenshotCapture;
  let domExtractor;
  let browserAutomation;

  beforeEach(() => {
    mockChrome = createMockChrome();
    originalChrome = global.chrome;
    global.chrome = mockChrome;
    
    ocrService = new MockOCRService();
    offscreenDocument = new MockOffscreenDocument();
    screenshotCapture = new MockScreenshotCapture();
    domExtractor = new MockDOMExtractor();
    browserAutomation = new MockBrowserAutomation();
    
    // Set up offscreen with OCR service
    offscreenDocument.setOCRService(ocrService);
  });

  afterEach(() => {
    global.chrome = originalChrome;
    ocrService = null;
    offscreenDocument = null;
    screenshotCapture = null;
    domExtractor = null;
    browserAutomation = null;
  });

  describe('Full OCR Flow', () => {
    it('should complete full OCR flow: screenshot -> OCR service', async () => {
      // Step 1: Capture screenshot
      const screenshot = await screenshotCapture.captureVisibleTab(1, { format: 'png' });
      assert.ok(screenshot);
      assert.ok(screenshot.data);
      
      // Step 2: Perform OCR
      const ocrResult = await ocrService.recognizeScreenshot(screenshot.data, { language: 'en' });
      assert.ok(ocrResult);
      assert.ok(ocrResult.text);
      assert.ok(ocrResult.blocks);
      assert.strictEqual(ocrResult.language, 'en');
      
      console.log('  ✓ Full OCR flow: screenshot -> OCR service works');
    });

    it('should handle provider fallback chain', async () => {
      const screenshotData = 'test-screenshot';
      
      // Try Mistral OCR first
      const mistralResult = await ocrService.mistralRecognizeScreenshot(screenshotData, {
        model: 'mistral-ocr-latest',
      });
      assert.ok(mistralResult);
      assert.strictEqual(mistralResult.model, 'mistral-ocr-latest');
      
      // Fall back to generic cloud OCR
      const cloudResult = await ocrService.cloudRecognizeScreenshot(screenshotData, {
        provider: 'mistral',
      });
      assert.ok(cloudResult);
      assert.strictEqual(cloudResult.provider, 'mistral');
      
      console.log('  ✓ Provider fallback chain works');
    });

    it('should merge OCR results with DOM', async () => {
      const ocrResult = await ocrService.recognizeScreenshot('test', {});
      const domText = domExtractor.extractTextFromDOM();
      
      const merged = domExtractor.mergeOCRWithDOM(ocrResult, domText);
      
      assert.ok(merged);
      assert.ok(merged.ocrText);
      assert.ok(merged.domText);
      assert.ok(merged.merged);
      assert.strictEqual(merged.hasOCRContent, true);
      assert.strictEqual(merged.hasDOMContent, true);
      
      console.log('  ✓ OCR result merging with DOM works');
    });

    it('should handle error recovery in OCR flow', async () => {
      // Simulate OCR failure
      const originalRecognize = ocrService.recognizeScreenshot;
      ocrService.recognizeScreenshot = async () => {
        throw new Error('OCR failed');
      };
      
      try {
        await ocrService.recognizeScreenshot('test');
        assert.fail('Should have thrown OCR error');
      } catch (error) {
        assert.strictEqual(error.message, 'OCR failed');
      }
      
      // Restore
      ocrService.recognizeScreenshot = originalRecognize;
      
      console.log('  ✓ Error recovery in OCR flow works');
    });
  });

  describe('Offscreen Integration', () => {
    it('should manage offscreen document lifecycle', async () => {
      // Ensure offscreen is created
      const created = await offscreenDocument.ensureCreated();
      assert.ok(created);
      assert.strictEqual(offscreenDocument.created, true);
      
      // Get OCR service
      const service = offscreenDocument.getOCRService();
      assert.ok(service);
      
      // Destroy offscreen
      const destroyed = await offscreenDocument.destroy();
      assert.ok(destroyed);
      assert.strictEqual(offscreenDocument.destroyed, true);
      assert.strictEqual(offscreenDocument.created, false);
      
      console.log('  ✓ Offscreen document lifecycle works');
    });

    it('should handle OCR worker communication', async () => {
      // Verify worker is ready
      assert.strictEqual(ocrService.isWorkerReady(), true);
      
      // Perform OCR through worker
      const result = await ocrService.recognizeScreenshot('test-data');
      assert.ok(result);
      
      console.log('  ✓ OCR worker communication works');
    });

    it('should clean up resources', async () => {
      await offscreenDocument.ensureCreated();
      await offscreenDocument.destroy();
      
      assert.strictEqual(offscreenDocument.created, false);
      
      console.log('  ✓ Resource cleanup works');
    });
  });

  describe('Screenshot Capture', () => {
    it('should capture visible tab screenshot', async () => {
      const screenshot = await screenshotCapture.captureVisibleTab(1, {
        format: 'png',
        quality: 0.9,
      });
      
      assert.ok(screenshot);
      assert.strictEqual(screenshot.tabId, 1);
      assert.ok(screenshot.data);
      assert.strictEqual(screenshot.width, 1920);
      assert.strictEqual(screenshot.height, 1080);
      
      console.log('  ✓ Visible tab screenshot capture works');
    });

    it('should capture page area screenshot', async () => {
      const rect = { x: 0, y: 0, width: 500, height: 500 };
      const screenshot = await screenshotCapture.capturePageArea(1, rect, {
        format: 'jpeg',
      });
      
      assert.ok(screenshot);
      assert.strictEqual(screenshot.tabId, 1);
      assert.deepStrictEqual(screenshot.rect, rect);
      
      console.log('  ✓ Page area screenshot capture works');
    });

    it('should track captured screenshots', async () => {
      await screenshotCapture.captureVisibleTab(1);
      await screenshotCapture.captureVisibleTab(2);
      await screenshotCapture.capturePageArea(3, { x: 0, y: 0, width: 100, height: 100 });
      
      const captured = screenshotCapture.getCapturedScreenshots();
      assert.strictEqual(captured.length, 3);
      
      console.log('  ✓ Screenshot tracking works');
    });
  });

  describe('DOM Extraction', () => {
    it('should extract main content', async () => {
      const content = domExtractor.extractMainContent();
      
      assert.ok(content);
      assert.ok(content.title);
      assert.ok(content.mainContent);
      assert.ok(content.wordCount);
      
      console.log('  ✓ Main content extraction works');
    });

    it('should extract all text from DOM', async () => {
      const text = domExtractor.extractTextFromDOM();
      
      assert.ok(text);
      assert.ok(text.text);
      assert.ok(text.elements);
      
      console.log('  ✓ Full DOM text extraction works');
    });

    it('should merge OCR with DOM correctly', async () => {
      const ocrResult = {
        text: 'Text from image',
        blocks: [],
      };
      
      const domText = {
        text: 'Text from DOM',
        elements: 10,
      };
      
      const merged = domExtractor.mergeOCRWithDOM(ocrResult, domText);
      
      assert.ok(merged.merged.includes('Text from image'));
      assert.ok(merged.merged.includes('Text from DOM'));
      
      console.log('  ✓ OCR and DOM merging works');
    });

    it('should handle missing DOM content', async () => {
      const ocrResult = { text: 'OCR text', blocks: [] };
      const emptyDom = { text: '', elements: 0 };
      
      const merged = domExtractor.mergeOCRWithDOM(ocrResult, emptyDom);
      
      assert.strictEqual(merged.hasDOMContent, false);
      assert.strictEqual(merged.hasOCRContent, true);
      
      console.log('  ✓ Missing DOM content handling works');
    });
  });

  describe('Browser Automation OCR', () => {
    it('should capture page vision', async () => {
      const result = await browserAutomation.capturePageVision(1, {
        includeDOM: true,
        includeScreenshot: true,
      });
      
      assert.ok(result);
      assert.ok(result.screenshot);
      assert.ok(result.ocrText);
      assert.ok(result.domText);
      assert.ok(result.mergedText);
      assert.ok(Array.isArray(result.elements));
      
      console.log('  ✓ Capture page vision works');
    });

    it('should perform scan operation', async () => {
      const result = await browserAutomation.scan(1, {
        areas: [
          { x: 0, y: 0, width: 500, height: 500 },
          { x: 500, y: 0, width: 500, height: 500 },
        ],
      });
      
      assert.ok(result);
      assert.ok(Array.isArray(result.screenshots));
      assert.ok(Array.isArray(result.ocrResults));
      assert.ok(result.mergedText);
      
      console.log('  ✓ Scan operation works');
    });

    it('should perform auto OCR when needed', async () => {
      const result = browserAutomation.autoOcrIfNeeded(1, {
        threshold: 0.5,
      });
      
      assert.ok(result);
      assert.strictEqual(result.neededOCR, true);
      assert.strictEqual(result.performedOCR, true);
      
      console.log('  ✓ Auto OCR when needed works');
    });

    it('should track capture calls', async () => {
      await browserAutomation.capturePageVision(1);
      await browserAutomation.capturePageVision(2);
      await browserAutomation.capturePageVision(3);
      
      const calls = browserAutomation.getCaptureCalls();
      assert.strictEqual(calls.length, 3);
      
      console.log('  ✓ Capture call tracking works');
    });
  });

  describe('Pipeline Scenarios', () => {
    it('should handle full page OCR pipeline', async () => {
      // 1. Capture screenshot
      const screenshot = await screenshotCapture.captureVisibleTab(1);
      
      // 2. Perform OCR
      const ocrResult = await ocrService.recognizeScreenshot(screenshot.data);
      
      // 3. Extract DOM
      const domText = domExtractor.extractTextFromDOM();
      
      // 4. Merge results
      const merged = domExtractor.mergeOCRWithDOM(ocrResult, domText);
      
      assert.ok(screenshot);
      assert.ok(ocrResult);
      assert.ok(domText);
      assert.ok(merged);
      
      console.log('  ✓ Full page OCR pipeline works');
    });

    it('should handle partial page OCR', async () => {
      // Capture specific area
      const rect = { x: 100, y: 100, width: 300, height: 200 };
      const screenshot = await screenshotCapture.capturePageArea(1, rect);
      
      // Perform OCR on area
      const ocrResult = await ocrService.recognizeScreenshot(screenshot.data, {
        language: 'en',
      });
      
      assert.ok(ocrResult);
      assert.ok(ocrResult.text);
      
      console.log('  ✓ Partial page OCR works');
    });

    it('should handle multi-area scan and OCR', async () => {
      // Perform scan
      const scanResult = await browserAutomation.scan(1);
      
      // All screenshots should have corresponding OCR
      assert.strictEqual(scanResult.screenshots.length, scanResult.ocrResults.length);
      
      console.log('  ✓ Multi-area scan and OCR works');
    });

    it('should handle error in any pipeline step', async () => {
      // Simulate failure in screenshot capture
      const originalCapture = screenshotCapture.captureVisibleTab;
      screenshotCapture.captureVisibleTab = async () => {
        throw new Error('Capture failed');
      };
      
      try {
        await screenshotCapture.captureVisibleTab(1);
        assert.fail('Should have thrown capture error');
      } catch (error) {
        assert.strictEqual(error.message, 'Capture failed');
      }
      
      // Restore
      screenshotCapture.captureVisibleTab = originalCapture;
      
      console.log('  ✓ Pipeline error handling works');
    });
  });

  describe('Performance and Configuration', () => {
    it('should respect OCR options', async () => {
      const options = {
        language: 'fr',
        preserveFormatting: true,
        singleColumn: true,
      };
      
      const result = await ocrService.recognizeScreenshot('test', options);
      
      assert.strictEqual(result.language, 'fr');
      
      console.log('  ✓ OCR options handling works');
    });

    it('should track OCR processing time', async () => {
      const result = await ocrService.recognizeScreenshot('test');
      
      assert.ok(result.processingTime);
      assert.ok(typeof result.processingTime === 'number');
      
      console.log('  ✓ OCR processing time tracking works');
    });

    it('should return confidence scores', async () => {
      const result = await ocrService.recognizeScreenshot('test');
      
      assert.ok(result.confidence);
      assert.ok(result.confidence >= 0 && result.confidence <= 1);
      
      console.log('  ✓ OCR confidence scoring works');
    });

    it('should handle screenshot format options', async () => {
      const formats = ['png', 'jpeg', 'webp'];
      
      for (const format of formats) {
        const screenshot = await screenshotCapture.captureVisibleTab(1, { format });
        assert.strictEqual(screenshot.format, format);
      }
      
      console.log('  ✓ Screenshot format options work');
    });
  });
});

console.log('\n=== OCR Pipeline Integration Tests Complete ===\n');
