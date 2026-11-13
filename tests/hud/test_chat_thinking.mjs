import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { Element } from './plugin_dom_shim.mjs';

Element.prototype.remove = function remove() {
  if (!this.parent) return;
  const index = this.parent.children.indexOf(this);
  if (index >= 0) this.parent.children.splice(index, 1);
};
Object.defineProperty(Element.prototype, 'style', {value: {}, writable: true});

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/chat-feed.js'), 'utf8');

const feed = new Element('div');
feed.scrollHeight = 0;
feed.scrollTo = () => {};

const context = {
  console,
  Date,
  Math,
  JSON,
  document: {
    hidden: false,
    addEventListener() {},
    createElement(tag) {
      const element = new Element(tag);
      element.style = {};
      return element;
    },
  },
  feed,
  requestAnimationFrame(fn) { fn(); },
  setTimeout() { return 0; },
  setInterval() { return 0; },
  fetch() { return Promise.resolve({ok: false}); },
  _clipChatText(value) { return String(value || ''); },
  _escapeHtml(value) { return String(value || ''); },
  _thinkingFromResult(result) {
    const memory = (result.thinking && result.thinking.memory) || {};
    return {
      status: 'memory_enriched',
      steps: [],
      memory: {
        status: memory.status || 'success',
        count: memory.count || 0,
        scope: memory.scope || [],
        entries: memory.entries || [],
        snippets: memory.snippets || [],
      },
    };
  },
};
context.window = context;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, {filename: 'chat-feed.js'});

const block = context.appendThinkingBlock('Hello Omega');
context.updateThinkingBlock(block, {
  ok: true,
  thinking: {
    memory: {
      status: 'success',
      count: 1,
      scope: ['identity'],
      entries: [{label: 'Creator', preview: 'Built Omega 3.0 by hand.'}],
    },
  },
}, 'resolved');

const memoryDetails = block.querySelector('.thinking-details');
const memoryList = block.querySelector('.thinking-memories');
assert.equal(memoryDetails.style.display, 'block');
assert.match(memoryDetails.textContent, /Memory hits used/);
assert.match(memoryList.textContent, /Creator/);
assert.match(memoryList.textContent, /Built Omega 3\.0 by hand\./);
assert.doesNotMatch(block.textContent, /Reviewed the request/);
assert.doesNotMatch(block.textContent, /Reasoning breakdown/);

console.log('chat thinking memory rendering tests passed');
