// The chat feed is a FIFO with a cap, not an unbounded log.
//
// Operator, 2026-09-20: "they need to append to the hud as a fifo setup." Nothing
// ever left the feed, so a long session accumulated hundreds of DOM entries; with
// the layout fix the feed scrolls, but an unbounded feed still grows the scroll
// container without limit and drags every repaint with it.
//
// The rules this pins:
//   1. every writer appends through ONE helper, so there is one place a cap can live;
//   2. the OLDEST entries leave first;
//   3. a turn still waiting for its answer is never evicted — its block is the paint
//      target both the reply rail and the response poll hold;
//   4. eviction is visible. A feed that silently drops history looks like a feed that
//      lost it; one line at the top says what left and that memory still has it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { Element } from './plugin_dom_shim.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/core/chat-feed.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function load() {
  const feed = new Element('div');
  feed.scrollHeight = 0;
  feed.clientHeight = 0;
  feed.style = {};
  feed.scrollTo = () => {};
  const context = {
    console, Date, Math, JSON, Number, String, Array, Object, Boolean, Map, Set,
    document: {
      hidden: false,
      addEventListener() {},
      createElement(tag) { const el = new Element(tag); el.style = {}; return el; },
    },
    feed,
    requestAnimationFrame(fn) { fn(); },
    setTimeout() { return 0; },
    setInterval() { return 0; },
    fetch() { return Promise.resolve({ ok: false }); },
    _clipChatText(value) { return String(value || ''); },
    _escapeHtml(value) { return String(value || ''); },
    _renderMarkdown(value) { return String(value || ''); },
    appendLine: () => null,
    _thinkingFromResult() {
      return { status: 'resolved', steps: [], memory: { status: 'skipped', count: 0, scope: [], entries: [], snippets: [] } };
    },
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context, { filename: 'chat-feed.js' });
  return { context, feed };
}

function line(context, text) {
  const el = context.document.createElement('div');
  el.className = 'line assistant';
  el.textContent = text;
  return el;
}

// Entries the operator can see, ignoring the trim notice.
function entries(feed) {
  return feed.children.filter(child => child.getAttribute('data-feed-notice') !== 'true');
}

// ── the cap, and the order it drops in ─────────────────────────────────────
{
  const { context, feed } = load();
  const cap = context.CHAT_FEED_MAX_ENTRIES;
  assert.ok(Number.isInteger(cap) && cap >= 20 && cap <= 400,
    'the feed declares a cap on entries: ' + String(cap));

  for (let i = 0; i < cap + 25; i += 1) context.appendToFeed(line(context, 'turn ' + i));
  assert.equal(entries(feed).length, cap, 'the feed holds at most its cap');
  assert.equal(entries(feed)[0].textContent, 'turn 25', 'the OLDEST entries leave first');
  assert.equal(entries(feed)[cap - 1].textContent, 'turn ' + String(cap + 24),
    'the newest entry is always kept');
}

// ── a live turn is never evicted ───────────────────────────────────────────
{
  const { context, feed } = load();
  const cap = context.CHAT_FEED_MAX_ENTRIES;
  const pending = context.appendThinkingBlock('the turn still being answered');
  assert.ok(String(pending.className).includes('is-pending'), 'the block starts pending');
  for (let i = 0; i < cap + 40; i += 1) context.appendToFeed(line(context, 'turn ' + i));
  assert.ok(entries(feed).includes(pending),
    'the block a reply is still going to paint into must survive the cap');
  assert.equal(entries(feed).length, cap);
}

// ── eviction says so, once, at the top ─────────────────────────────────────
{
  const { context, feed } = load();
  const cap = context.CHAT_FEED_MAX_ENTRIES;
  for (let i = 0; i < cap; i += 1) context.appendToFeed(line(context, 'turn ' + i));
  assert.equal(feed.children.filter(c => c.getAttribute('data-feed-notice') === 'true').length, 0,
    'a feed inside the cap carries no notice');

  context.appendToFeed(line(context, 'one more'));
  const notices = feed.children.filter(c => c.getAttribute('data-feed-notice') === 'true');
  assert.equal(notices.length, 1, 'exactly one notice');
  assert.equal(feed.children[0], notices[0], 'the notice sits at the top of the feed');
  assert.match(notices[0].textContent, /1\b/, 'the notice counts what left');

  for (let i = 0; i < 9; i += 1) context.appendToFeed(line(context, 'more ' + i));
  const after = feed.children.filter(c => c.getAttribute('data-feed-notice') === 'true');
  assert.equal(after.length, 1, 'the notice is updated, never duplicated');
  assert.match(after[0].textContent, /\b10\b/, 'the count accumulates across trims');
  assert.equal(entries(feed).length, cap, 'the notice does not consume a slot');
}

// ── one door: no module appends to the feed behind the cap ─────────────────
{
  const writers = [
    'core/boot.js', 'core/chat-feed.js', 'core/chat-wiring.js',
    'core/live-wiring.js', 'core/omega-ui.js', 'core/plugin-permissions.js',
  ];
  for (const name of writers) {
    const file = path.resolve(here, '../../hud/ui-src', name);
    if (!fs.existsSync(file)) continue;
    const text = fs.readFileSync(file, 'utf8')
      .split('\n')
      .filter(l => !l.trim().startsWith('//'))
      .join('\n');
    // The canonical helper is the one place allowed to touch the feed directly.
    const bare = text.match(/\b(feed|f)\.appendChild\s*\(/g) || [];
    const allowed = name === 'core/chat-feed.js' ? 1 : 0;
    assert.ok(
      bare.length <= allowed,
      name + ' must append through appendToFeed, not ' + bare.length
      + ' bare feed.appendChild call(s) — a second door means the cap does not hold',
    );
  }
}

console.log('hud chat feed FIFO tests passed');
