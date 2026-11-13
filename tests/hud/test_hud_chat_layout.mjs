// The composer must stay on screen no matter how long the conversation gets.
//
// Operator, 2026-09-20: "the chat input ... is being pinned at the bottom causing the
// agent input to disappear off screen once enough chats are added in."
//
// The cause is a grid-track mismatch, not a scroll bug. `.shell` is `height: 100dvh`
// with `grid-template-rows: auto 1fr auto` — three tracks for what used to be three
// children. But #cycle and #clock ship with the `hidden` attribute, so they are not
// grid items at all, and the ONLY item left, `.stage`, auto-places into track 1: the
// `auto` one. An auto track is sized by its content, so the stage grew with every
// message past the 100dvh the shell reserved, and `body { overflow: hidden }` clipped
// the overflow — taking the composer with it. The feed's own `overflow-y: auto` never
// engaged because nothing above it was ever bounded.
//
// This harness asserts the chain that makes the feed the thing that scrolls:
// shell (exactly the viewport) → stage (a FLEXIBLE track) → canvas (bounded) →
// feed (scrolls) + composer (always in view).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const shellPath = path.resolve(here, '../../hud/ui-src/shell.html');
const shellHtml = fs.readFileSync(shellPath, 'utf8');

// The bare rule for a selector — never a descendant/density variant of it.
function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = shellHtml.match(new RegExp('^[ \\t]*' + escaped + '\\s*\\{([^}]*)\\}', 'm'));
  assert.ok(match, 'shell.html must carry a bare rule for ' + selector);
  return match[1];
}

function declaration(body, property) {
  // These rules carry prose between their declarations; a comment is not a separator.
  const clean = body.replace(/\/\*[\s\S]*?\*\//g, ' ');
  const match = clean.match(new RegExp('(?:^|;)\\s*' + property + '\\s*:\\s*([^;]+)'));
  return match ? match[1].trim() : null;
}

const shell = rule('.shell');
const stage = rule('.stage');
const canvas = rule('.canvas');
const feed = rule('.feed');

// ── the cause, still true of the markup ────────────────────────────────────
// Kept as an assertion so the next reader does not have to take the diagnosis on
// faith: the shell really does have exactly one laid-out child.
for (const id of ['cycle', 'clock']) {
  const tag = shellHtml.match(new RegExp('<span id="' + id + '"[^>]*>'));
  assert.ok(tag, '#' + id + ' must still exist in the shell');
  assert.match(tag[0], /\bhidden\b/,
    '#' + id + ' is hidden, so .stage is the shell\'s only laid-out child');
}

// ── the shell is exactly the viewport ──────────────────────────────────────
assert.equal(declaration(shell, 'height'), '100dvh',
  'the shell must be EXACTLY the viewport, so the track holding the feed is bounded');

// ── the stage sits in a FLEXIBLE track of it ───────────────────────────────
const display = declaration(shell, 'display');
if (display === 'grid') {
  const tracks = String(declaration(shell, 'grid-template-rows') || '');
  const flexible = /^\s*(minmax\(\s*0\s*,\s*1fr\s*\)|1fr)\s*$/.test(tracks);
  assert.ok(
    flexible || /grid-row\s*:/.test(stage),
    'a grid shell must either declare ONE flexible track or place .stage explicitly — '
    + 'with tracks "' + tracks + '" the stage auto-places into an auto track and grows '
    + 'past the viewport as messages arrive',
  );
} else {
  assert.equal(display, 'flex', 'the shell lays its children out as a grid or a flex column');
  assert.equal(declaration(shell, 'flex-direction'), 'column');
  const grow = String(declaration(stage, 'flex') || declaration(stage, 'flex-grow') || '');
  assert.match(grow, /^1\b/,
    'the stage must take the shell\'s remaining height, not its content height');
}

// ── and every box below it is bounded, so the FEED is what scrolls ─────────
for (const [name, body] of [['.stage', stage], ['.canvas', canvas], ['.feed', feed]]) {
  assert.equal(declaration(body, 'min-height'), '0',
    name + ' must declare min-height: 0, or it refuses to shrink below its content');
}
assert.equal(declaration(canvas, 'overflow'), 'hidden');
assert.equal(declaration(feed, 'overflow-y'), 'auto');

// ── the composer is a sibling AFTER the feed, inside the canvas ────────────
const canvasMarkup = shellHtml.slice(shellHtml.indexOf('<div class="canvas" id="canvas">'));
const feedAt = canvasMarkup.indexOf('<div class="feed" id="feed">');
const composerAt = canvasMarkup.indexOf('<div class="composer">');
assert.ok(feedAt >= 0 && composerAt > feedAt,
  'the composer must follow the feed inside the canvas, so a bounded canvas always shows it');

// ── a finished build's open trace is bounded ──────────────────────────
// A resolved turn that called tools keeps its trace on screen (chat-feed.js). Without a
// bound, eight tool calls carrying diffs push the answer a whole screen away from the
// work that produced it. A RUNNING trace stays unbounded on purpose.
{
  const resolved = shellHtml.match(
    /\.cockpit-shell \.thinking-block\.is-resolved[^{]*\.thinking-trace[^{]*\{([^}]*)\}/);
  assert.ok(resolved, 'a resolved block open trace must have its own rule');
  assert.match(resolved[1], /max-height:\s*\d+vh/, 'and a bound');
  assert.match(resolved[1], /overflow-y:\s*auto/, 'and its own scroll');
  const pendingBound = shellHtml.match(
    /\.cockpit-shell \.thinking-block\.is-pending[^{]*\.thinking-trace[^{]*\{([^}]*)\}/);
  assert.equal(pendingBound, null, 'a running trace is never bounded: it would hide the live step');
}

console.log('hud chat layout tests passed');
