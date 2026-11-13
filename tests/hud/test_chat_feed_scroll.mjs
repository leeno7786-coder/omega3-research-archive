// The chat feed must pin to the newest message even when the HUD tab is not
// the foreground tab. Operator report 2026-08-01: "the chat window isn't
// scrolling properly, it's stuck at the top of the window."
//
// Measured in the live HUD with a hidden tab and 13 lines (scrollHeight 1442,
// clientHeight 571):
//   scrollTop = scrollHeight            -> 0      (never moved, even after 800ms)
//   scroll-behavior:auto + same assign  -> 871.2
//   scrollTo({behavior:'auto'})         -> 871.2
//
// Two causes, both tied to visibility:
//   1. .feed carries `scroll-behavior: smooth`, so an ordinary scroll request
//      is an ANIMATION, and animations do not advance in a hidden tab.
//   2. _scrollFeedToBottom was built on nested requestAnimationFrame, and rAF
//      does not fire in a hidden tab either.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/core/chat-feed.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function makeFeed() {
  return {
    scrollHeight: 1442,
    clientHeight: 571,
    style: { scrollBehavior: 'smooth' },
    _scrollTop: 0,
    scrolls: [],
    get scrollTop() { return this._scrollTop; },
    // Model the shipped CSS: scrolling while smooth is an animation, which a
    // hidden tab never advances. The helper temporarily requests auto.
    set scrollTop(v) { if (!this.hiddenTab || this.style.scrollBehavior === 'auto') this._scrollTop = v; },
    scrollTo(opts) {
      this.scrolls.push(opts);
      if (opts && opts.behavior === 'auto' && this.style.scrollBehavior === 'auto') this._scrollTop = opts.top;
      else if (!this.hiddenTab) this._scrollTop = opts.top;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild() {},
    hiddenTab: true,
  };
}

function load(feed, { rafFires }) {
  const timers = [];
  const ctx = {
    console,
    feed,
    document: { hidden: true, visibilityState: 'hidden' },
    requestAnimationFrame: (fn) => { if (rafFires) fn(); },
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    setInterval: () => 0,
    clearTimeout: () => {},
    fetch: () => Promise.resolve({ ok: false }),
    Date,
    Math,
    JSON,
    appendLine: () => null,
    _clipChatText: (t) => t,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return { ctx, drainTimers: () => { while (timers.length) timers.shift()(); } };
}

// ── the regression: hidden tab, rAF never fires ────────────────────────────
{
  const feed = makeFeed();
  const { ctx, drainTimers } = load(feed, { rafFires: false });
  ctx._scrollFeedToBottom();
  drainTimers();
  assert.equal(
    feed.scrollTop,
    871,
    'a reply that lands while the tab is hidden must still pin the feed to the bottom',
  );
}

// ── the ordinary path still pins ───────────────────────────────────────────
{
  const feed = makeFeed();
  feed.hiddenTab = false;
  const { ctx, drainTimers } = load(feed, { rafFires: true });
  ctx._scrollFeedToBottom();
  drainTimers();
  assert.equal(feed.scrollTop, 871, 'a visible tab must pin to the bottom too');
}

// ── it must not depend on rAF alone ────────────────────────────────────────
{
  const feed = makeFeed();
  const { ctx } = load(feed, { rafFires: false });
  ctx._scrollFeedToBottom();
  assert.ok(
    feed.scrollTop > 0,
    'the first pin must be synchronous — rAF is not guaranteed to ever run',
  );
  assert.ok(
    feed.scrolls.some(s => s && s.behavior === 'auto'),
    'the pin must request an immediate auto scroll; smooth never advances while hidden',
  );
}

// ── no module may pin the feed with a bare assignment ──────────────────────
// appendLine (boot.js) is the function EVERY chat line goes through, and it
// pinned with a scroll request that leaves the feed's smooth presentation
// behavior in force. That is an animation, so it silently did nothing in a
// hidden tab — which is why every writer must use the shared synchronous form.
// omega-ui.js also writes to the SAME #feed (it calls getElementById('feed')),
// so it carried the identical bug.
{
  const feedWriters = [
    'core/boot.js', 'core/chat-feed.js', 'core/chat-wiring.js',
    'core/live-wiring.js', 'core/omega-ui.js',
  ];
  for (const name of feedWriters) {
    const p = path.resolve(here, '../../hud/ui-src', name);
    if (!fs.existsSync(p)) continue;
    // Strip line comments — the canonical helper documents the forbidden
    // pattern in prose, and that must not trip its own guard.
    const text = fs.readFileSync(p, 'utf8')
      .split('\n')
      .filter(line => !line.trim().startsWith('//'))
      .join('\n');
    // Generic: ANY `x.scrollTop = x.scrollHeight` on the same object.
    const bare = text.match(/\b([A-Za-z_$][\w$]*)\.scrollTop\s*=\s*\1\.scrollHeight/);
    assert.equal(
      bare,
      null,
      `${name} must pin synchronously, not a bare scrollTop assignment `
      + `(found "${bare && bare[0]}" — smooth scroll never advances in a hidden tab)`,
    );
  }
}

console.log('chat feed scroll pinning tests passed');
