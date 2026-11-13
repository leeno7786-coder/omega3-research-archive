import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/core/chat-feed.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const imageCalls = [];
const documentCalls = [];
const errors = [];
const context = {
  console,
  document: { hidden: false },
  window: { _lastStreamedLine: null },
  setInterval: () => 0,
  requestAnimationFrame: callback => callback(),
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  encodeURIComponent,
  appendImageMessage: result => {
    imageCalls.push(result);
    return { kind: 'image-card' };
  },
  appendDocumentMessage: result => {
    documentCalls.push(result);
    return { kind: 'document-card' };
  },
  appendLine: () => ({ classList: { add() {} } }),
  appendChatError: message => errors.push(message),
  _renderMarkdown: text => text,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: sourcePath });

const imageResult = context.renderChatResult(
  { ok: true, image: { url: '/images/generated.png' } },
  null,
);
assert.equal(imageCalls.length, 1);
assert.equal(imageResult.artifact, true);
assert.equal(imageResult.ok, true);

const documentResult = context.renderChatResult(
  { ok: true, document: { url: '/documents/generated.md' } },
  null,
);
assert.equal(documentCalls.length, 1);
assert.equal(documentResult.artifact, true);
assert.equal(documentResult.ok, true);
assert.deepEqual(errors, []);

console.log('queued chat artifact rendering tests passed');
