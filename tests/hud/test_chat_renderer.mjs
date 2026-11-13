import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/chat-renderer.js'), 'utf8');

const document = { addEventListener() {} };
const context = { document, navigator: {}, console, setTimeout() {} };
context.window = context;
vm.createContext(context);
vm.runInContext(source, context);

const rendered = context._renderMarkdown([
  'Basic skeleton:',
  '',
  '- Use Node.js + TypeScript.',
  '- Put the entry point in `todo.ts`.',
  '- Keep **one command** for `npm_test`.',
].join('\n'));

assert.match(rendered, /<code>todo\.ts<\/code>/);
assert.match(rendered, /<strong>one command<\/strong>/);
assert.match(rendered, /<code>npm_test<\/code>/);
assert.doesNotMatch(rendered, /OMEGA_INLINE|OMEGA<em>/);

// Diffs (operator 2026-09-05): a ```diff fence and a tool-result diff render
// one classed line per row, escaped, with adds/removes/hunks told apart.
const diffText = '--- a/hello.py\n+++ b/hello.py\n@@ -1,2 +1,2 @@\n-print("old <b>")\n+print("new")\n context';
const diffHtml = context._renderDiff(diffText);
const diffLines = diffHtml.split('\n');
assert.equal(diffLines.length, 6);
assert.match(diffLines[0], /^<span class="diff-line diff-meta">--- a\/hello\.py<\/span>$/);
assert.match(diffLines[2], /diff-hunk/);
assert.match(diffLines[3], /diff-del">-print\(&quot;old &lt;b&gt;&quot;\)/, 'removed lines are escaped');
assert.match(diffLines[4], /diff-add">\+print/);
assert.match(diffLines[5], /diff-ctx"> context/);

const fenced = context._renderMarkdown('Change:\n\n```diff\n-a\n+b\n```\n');
assert.match(fenced, /chat-code-block is-diff/);
assert.match(fenced, /diff-line diff-del">-a/);
assert.match(fenced, /diff-line diff-add">\+b/);
assert.doesNotMatch(fenced, /syn-kw/, 'a diff is not keyword-highlighted');
const block = context._renderCodeBlock('-a\n+b', 'diff');
assert.match(block, /chat-code-lang">diff</);


// ── an escaped apostrophe is not a comment ─────────────────────────────────
// Operator transcript 2026-09-20: a build's tool output rendered every apostrophe as
// a literal `&#39;`. _highlightCode paints the ESCAPED html, and its comment rule
// (`#.*$`) matched from the `#` INSIDE `&#39;` to the end of the line — splitting the
// entity, so the browser saw a bare `&` followed by the text `#39;`. The number rule
// was taught to step over entities (2026-09-15); every other pass must too.
const apostrophe = context._renderCodeBlock("it's the operator's file # note", 'text');
assert.match(apostrophe, /&#39;/, 'the escaped apostrophe survives whole');
assert.doesNotMatch(apostrophe, /&<span/, 'no pass may split an entity from its ampersand');
assert.doesNotMatch(apostrophe, /&amp;#39;/, 'and it is never escaped twice');
// The same for the other escapes a tool output carries.
for (const [text, entity] of [['a < b', '&lt;'], ['a > b', '&gt;'], ['say "hi"', '&quot;'], ['a & b', '&amp;']]) {
  const html = context._renderCodeBlock(text, 'text');
  assert.ok(html.includes(entity), text + ' keeps ' + entity);
  assert.doesNotMatch(html, /&<span/, text + ' must not split its entity');
}
// A real comment is still highlighted.
assert.match(context._renderCodeBlock('# a plain comment', 'python'), /syn-cm/);

console.log('chat renderer tests passed');
