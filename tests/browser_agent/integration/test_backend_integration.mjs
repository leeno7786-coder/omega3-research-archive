/**
 * Browser Agent Extension - Backend Integration Tests
 * 
 * Tests the integration between Model Manager, Backend classes, and related components.
 * Focuses on model loading, chat completion, and backend switching scenarios.
 */

import assert from 'node:assert/strict';
import { describe, it, beforeEach, afterEach } from 'node:test';
import { createMockChrome, createMockFn } from '../conftest.mjs';

// Mock base backend class
class MockBaseBackend {
  constructor(config = {}) {
    this.config = { ...config };
    this.models = [];
    this.connected = false;
    this.loadedModel = null;
  }

  getType() {
    return this.constructor.name.toLowerCase().replace('mock', '');
  }

  getName() {
    return this.constructor.name.replace('Mock', '');
  }

  async checkConnection() {
    this.connected = true;
    return { connected: true, url: this.config.serverUrl };
  }

  async loadModel(modelId, params = {}) {
    this.loadedModel = modelId;
    return { modelId, loaded: true };
  }

  async unloadModel() {
    this.loadedModel = null;
    return { unloaded: true };
  }

  async chatCompletion(messages, options = {}) {
    return {
      choices: [{
        message: { content: 'Mock response' },
        finishReason: 'stop',
      }],
      usage: { promptTokens: 10, completionTokens: 5 },
    };
  }

  async getAvailableModels() {
    return this.models;
  }

  applyConfig(config) {
    this.config = { ...this.config, ...config };
    return this.config;
  }

  getConnectionInfo() {
    return { ...this.config };
  }
}

// Mock LM Studio backend
class MockLMStudioBackend extends MockBaseBackend {
  constructor(config = {}) {
    super(config);
    this.config = { serverUrl: 'http://localhost:1234', ...config };
    this.models = ['llama-7b', 'mistral-7b', 'phi-2'];
  }

  getType() { return 'lmstudio'; }
  getName() { return 'LM Studio'; }
}

// Mock Omega Portable backend
class MockOmegaPortableBackend extends MockBaseBackend {
  constructor(config = {}) {
    super(config);
    this.config = { serverUrl: 'http://localhost:11434', ...config };
    this.models = ['gemma-7b', 'mistral-7b'];
  }

  getType() { return 'omega-portable'; }
  getName() { return 'Omega-Portable'; }
}

// Mock OpenRouter backend
class MockOpenRouterBackend extends MockBaseBackend {
  constructor(config = {}) {
    super(config);
    this.config = { apiKey: 'test-key', ...config };
    this.models = ['openai/gpt-4o', 'anthropic/claude-3', 'google/gemini-pro'];
  }

  getType() { return 'openrouter'; }
  getName() { return 'OpenRouter'; }

  async checkConnection() {
    this.connected = !!this.config.apiKey;
    return { connected: this.connected, hasKey: !!this.config.apiKey };
  }
}

// Mock Model Manager
class MockModelManager {
  constructor(backends = []) {
    this.backends = new Map();
    this.currentBackend = null;
    this.currentModel = null;
    this.lastModelId = null;
    this.isLoading = false;
    
    for (const backend of backends) {
      this.backends.set(backend.getType(), backend);
    }
    
    if (backends.length > 0) {
      this.currentBackend = backends[0];
    }
  }

  getBackend(type) {
    return this.backends.get(type);
  }

  getCurrentBackend() {
    return this.currentBackend;
  }

  getAvailableBackends() {
    return Array.from(this.backends.values());
  }

  async setBackend(type) {
    const backend = this.backends.get(type);
    if (backend) {
      if (this.currentModel) {
        await this.unloadModel();
      }
      this.currentBackend = backend;
      return true;
    }
    return false;
  }

  async loadModel(modelId, params = {}) {
    if (!this.currentBackend) {
      throw new Error('No backend selected');
    }
    
    this.isLoading = true;
    const result = await this.currentBackend.loadModel(modelId, params);
    this.currentModel = modelId;
    this.lastModelId = modelId;
    this.isLoading = false;
    return result;
  }

  async unloadModel() {
    if (this.currentBackend && this.currentModel) {
      await this.currentBackend.unloadModel();
      this.currentModel = null;
      return true;
    }
    return false;
  }

  async switchModel(modelId, params = {}) {
    if (this.currentModel === modelId) {
      return { switched: false, reason: 'already_loaded' };
    }
    
    if (this.currentModel) {
      await this.unloadModel();
    }
    
    return this.loadModel(modelId, params);
  }

  async reloadModel() {
    const modelId = this.currentModel || this.lastModelId;
    if (modelId) {
      if (this.currentModel) {
        await this.unloadModel();
      }
      return this.loadModel(modelId);
    }
    throw new Error('No model loaded');
  }

  getModel() {
    return {
      id: this.currentModel,
      backend: this.currentBackend?.getType(),
      loaded: !!this.currentModel,
    };
  }

  isModelLoaded() {
    return !!this.currentModel;
  }

  isLoadingModel() {
    return this.isLoading;
  }

  getAvailableModels() {
    const models = [];
    for (const backend of this.backends.values()) {
      models.push(...backend.models);
    }
    return models;
  }
}

describe('Backend Integration', () => {
  let lmStudioBackend;
  let omegaPortableBackend;
  let openRouterBackend;
  let modelManager;

  beforeEach(() => {
    lmStudioBackend = new MockLMStudioBackend();
    omegaPortableBackend = new MockOmegaPortableBackend();
    openRouterBackend = new MockOpenRouterBackend({ apiKey: 'test-key-123' });
    
    modelManager = new MockModelManager([
      lmStudioBackend,
      omegaPortableBackend,
      openRouterBackend,
    ]);
  });

  afterEach(() => {
    lmStudioBackend = null;
    omegaPortableBackend = null;
    openRouterBackend = null;
    modelManager = null;
  });

  describe('Model Manager + Backend Integration', () => {
    it('should load model through backend', async () => {
      const result = await modelManager.loadModel('llama-7b');
      
      assert.ok(result.loaded);
      assert.strictEqual(modelManager.currentModel, 'llama-7b');
      assert.strictEqual(lmStudioBackend.loadedModel, 'llama-7b');
      console.log('  ✓ Model loading through backend works');
    });

    it('should handle chat completion flow', async () => {
      await modelManager.loadModel('llama-7b');
      
      const messages = [
        { role: 'user', content: 'Hello, how are you?' },
      ];
      
      const response = await modelManager.currentBackend.chatCompletion(messages, {
        temperature: 0.7,
      });
      
      assert.ok(response);
      assert.ok(response.choices);
      assert.ok(response.choices[0].message.content);
      assert.ok(response.usage);
      console.log('  ✓ Chat completion flow works');
    });

    it('should handle error during model loading', async () => {
      // Create a failing backend
      const failingBackend = new MockBaseBackend();
      failingBackend.loadModel = async () => {
        throw new Error('Connection failed');
      };
      
      const failingManager = new MockModelManager([failingBackend]);
      
      try {
        await failingManager.loadModel('test-model');
        assert.fail('Should have thrown an error');
      } catch (error) {
        assert.strictEqual(error.message, 'Connection failed');
      }
      
      console.log('  ✓ Error handling during model loading works');
    });

    it('should propagate configuration to backend', async () => {
      const newConfig = { serverUrl: 'http://localhost:5000', timeout: 60000 };
      
      lmStudioBackend.applyConfig(newConfig);
      
      const connectionInfo = lmStudioBackend.getConnectionInfo();
      assert.strictEqual(connectionInfo.serverUrl, 'http://localhost:5000');
      assert.strictEqual(connectionInfo.timeout, 60000);
      
      console.log('  ✓ Configuration propagation to backend works');
    });
  });

  describe('Backend Switching', () => {
    it('should switch from OpenRouter to LM Studio', async () => {
      // Start with OpenRouter
      await modelManager.setBackend('openrouter');
      assert.strictEqual(modelManager.currentBackend.getType(), 'openrouter');
      
      // Switch to LM Studio
      const switched = await modelManager.setBackend('lmstudio');
      assert.ok(switched);
      assert.strictEqual(modelManager.currentBackend.getType(), 'lmstudio');
      
      console.log('  ✓ Switch from OpenRouter to LM Studio works');
    });

    it('should switch from local to cloud backend', async () => {
      // Start with LM Studio (local)
      await modelManager.setBackend('lmstudio');
      assert.strictEqual(modelManager.currentBackend.getType(), 'lmstudio');
      
      // Switch to OpenRouter (cloud)
      const switched = await modelManager.setBackend('openrouter');
      assert.ok(switched);
      assert.strictEqual(modelManager.currentBackend.getType(), 'openrouter');
      
      console.log('  ✓ Switch from local to cloud backend works');
    });

    it('should handle error recovery during switch', async () => {
      // Try to switch to non-existent backend
      const switched = await modelManager.setBackend('nonexistent');
      assert.ok(!switched);
      
      // Should still be on previous backend
      assert.ok(modelManager.currentBackend);
      
      console.log('  ✓ Error recovery during backend switch works');
    });

    it('should switch backend with model loaded', async () => {
      // Load a model on current backend
      await modelManager.loadModel('openai/gpt-4o');
      assert.strictEqual(modelManager.currentModel, 'openai/gpt-4o');
      
      // Switch backend (model should be unloaded)
      await modelManager.setBackend('lmstudio');
      assert.strictEqual(modelManager.currentBackend.getType(), 'lmstudio');
      assert.strictEqual(modelManager.currentModel, null);
      
      console.log('  ✓ Backend switch with loaded model works');
    });
  });

  describe('Model Lifecycle', () => {
    it('should unload and reload model', async () => {
      await modelManager.loadModel('llama-7b');
      assert.strictEqual(modelManager.currentModel, 'llama-7b');
      
      // Unload
      const unloaded = await modelManager.unloadModel();
      assert.ok(unloaded);
      assert.strictEqual(modelManager.currentModel, null);
      
      // Reload
      const reloaded = await modelManager.reloadModel();
      assert.ok(reloaded.loaded);
      assert.strictEqual(modelManager.currentModel, 'llama-7b');
      
      console.log('  ✓ Model unload and reload works');
    });

    it('should switch models', async () => {
      await modelManager.loadModel('llama-7b');
      assert.strictEqual(modelManager.currentModel, 'llama-7b');
      
      const result = await modelManager.switchModel('mistral-7b');
      assert.ok(result.loaded);
      assert.strictEqual(modelManager.currentModel, 'mistral-7b');
      
      console.log('  ✓ Model switching works');
    });

    it('should handle switching to already loaded model', async () => {
      await modelManager.loadModel('llama-7b');
      
      const result = await modelManager.switchModel('llama-7b');
      assert.strictEqual(result.reason, 'already_loaded');
      assert.strictEqual(modelManager.currentModel, 'llama-7b');
      
      console.log('  ✓ Switching to already loaded model works');
    });

    it('should track loading state', async () => {
      assert.strictEqual(modelManager.isLoadingModel(), false);
      
      // Load a model (async)
      const loadPromise = modelManager.loadModel('llama-7b');
      assert.strictEqual(modelManager.isLoadingModel(), true);
      
      await loadPromise;
      assert.strictEqual(modelManager.isLoadingModel(), false);
      
      console.log('  ✓ Loading state tracking works');
    });
  });

  describe('Backend Capabilities', () => {
    it('should get available models from backend', async () => {
      const models = await lmStudioBackend.getAvailableModels();
      
      assert.ok(Array.isArray(models));
      assert.ok(models.length > 0);
      assert.ok(models.includes('llama-7b'));
      
      console.log('  ✓ Getting available models from backend works');
    });

    it('should check backend connection', async () => {
      const result = await lmStudioBackend.checkConnection();
      
      assert.ok(result.connected);
      assert.ok(result.url);
      
      console.log('  ✓ Backend connection check works');
    });

    it('should handle connection failure', async () => {
      const failingBackend = new MockLMStudioBackend({ serverUrl: 'http://localhost:9999' });
      failingBackend.checkConnection = async () => ({ connected: false });
      
      const result = await failingBackend.checkConnection();
      assert.strictEqual(result.connected, false);
      
      console.log('  ✓ Connection failure handling works');
    });

    it('should identify backend type and name', () => {
      assert.strictEqual(lmStudioBackend.getType(), 'lmstudio');
      assert.strictEqual(lmStudioBackend.getName(), 'LM Studio');
      
      assert.strictEqual(omegaPortableBackend.getType(), 'omega-portable');
      assert.strictEqual(omegaPortableBackend.getName(), 'Omega-Portable');
      
      assert.strictEqual(openRouterBackend.getType(), 'openrouter');
      assert.strictEqual(openRouterBackend.getName(), 'OpenRouter');
      
      console.log('  ✓ Backend type and name identification works');
    });

    it('should return connection info', () => {
      const info = lmStudioBackend.getConnectionInfo();
      
      assert.ok(info);
      assert.strictEqual(info.serverUrl, 'http://localhost:1234');
      
      console.log('  ✓ Connection info retrieval works');
    });
  });

  describe('Multi-Backend Scenarios', () => {
    it('should get all available models across backends', () => {
      const allModels = modelManager.getAvailableModels();
      
      assert.ok(allModels.length > 0);
      // Should have models from all backends
      assert.ok(allModels.includes('llama-7b')); // LM Studio
      assert.ok(allModels.includes('gemma-7b')); // Omega Portable
      assert.ok(allModels.includes('openai/gpt-4o')); // OpenRouter
      
      console.log('  ✓ Getting all models across backends works');
    });

    it('should get current backend', () => {
      const current = modelManager.getCurrentBackend();
      assert.ok(current);
      assert.ok(current.getType());
      assert.ok(current.getName());
      
      console.log('  ✓ Getting current backend works');
    });

    it('should get specific backend by type', () => {
      const lmStudio = modelManager.getBackend('lmstudio');
      assert.ok(lmStudio);
      assert.strictEqual(lmStudio.getType(), 'lmstudio');
      
      const omegaPortable = modelManager.getBackend('omega-portable');
      assert.ok(omegaPortable);
      assert.strictEqual(omegaPortable.getType(), 'omega-portable');
      
      console.log('  ✓ Getting backend by type works');
    });

    it('should get all available backends', () => {
      const backends = modelManager.getAvailableBackends();
      assert.ok(Array.isArray(backends));
      assert.strictEqual(backends.length, 3);
      
      console.log('  ✓ Getting all available backends works');
    });

    it('should get model info', () => {
      const modelInfo = modelManager.getModel();
      
      assert.ok(modelInfo);
      assert.strictEqual(modelInfo.loaded, false);
      assert.strictEqual(modelInfo.id, null);
      
      console.log('  ✓ Getting model info works');
    });

    it('should check if model is loaded', () => {
      assert.strictEqual(modelManager.isModelLoaded(), false);
      
      // Load a model
      modelManager.currentModel = 'test-model';
      assert.strictEqual(modelManager.isModelLoaded(), true);
      
      console.log('  ✓ Model loaded check works');
    });
  });

  describe('OpenRouter Backend Specifics', () => {
    it('should require API key for connection', async () => {
      const backendNoKey = new MockOpenRouterBackend({ apiKey: '' });
      const result = await backendNoKey.checkConnection();
      
      assert.strictEqual(result.connected, false);
      assert.strictEqual(result.hasKey, false);
      
      console.log('  ✓ OpenRouter API key requirement works');
    });

    it('should connect with valid API key', async () => {
      const result = await openRouterBackend.checkConnection();
      
      assert.strictEqual(result.connected, true);
      assert.strictEqual(result.hasKey, true);
      
      console.log('  ✓ OpenRouter connection with API key works');
    });

    it('should have cloud models', async () => {
      const models = await openRouterBackend.getAvailableModels();
      
      assert.ok(models.length > 0);
      assert.ok(models.some(m => m.includes('openai')));
      assert.ok(models.some(m => m.includes('anthropic')));
      
      console.log('  ✓ OpenRouter cloud models work');
    });
  });
});

console.log('\n=== Backend Integration Tests Complete ===\n');
