import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/core/speech-text.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const context = { console };
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: sourcePath });

const speech = context.OmegaSpeechText;
assert.ok(speech, 'speech text helpers must be exported globally');

const markdown = [
  '# **Distribution guide**',
  '* Binomial: fixed `n` trials.',
  '* [Poisson](https://example.com): counts over time.',
  '* Normal: continuous measurements.',
  '',
  '```python',
  'print("do not speak source code")',
  '```',
  'Use Binomial → Poisson when the setup changes.',
].join('\n');

const normalized = speech.normalize(markdown);
assert.equal(normalized.includes('*'), false);
assert.equal(normalized.includes('`'), false);
assert.equal(normalized.includes('https://'), false);
assert.equal(normalized.includes('print('), false);
assert.match(normalized, /Distribution guide/);
assert.match(normalized, /Binomial: fixed n trials/);
assert.match(normalized, /Poisson: counts over time/);
assert.match(normalized, /Code block omitted/);
assert.match(normalized, /Binomial, then Poisson/);

const softWrapped = [
  'The repair keeps the original request',
  'while preserving its intent and downstream lineage.',
].join('\n');
assert.equal(
  speech.normalize(softWrapped),
  'The repair keeps the original request while preserving its intent and downstream lineage.',
  'a visual line wrap must not manufacture a sentence ending',
);

assert.equal(
  speech.normalize('# Results\n- First item is complete'),
  'Results. First item is complete.',
  'speech prose must finish with terminal punctuation even when Markdown does not',
);

assert.equal(
  speech.normalize('Dr. Smith checked the U.S. route, e.g., the API response.'),
  'Doctor Smith checked the U S route, for example, the API response.',
  'speech prose must expand punctuation that phonemizers mistake for sentence endings',
);

const coherentSentence = [
  'This is a deliberately long sentence that continues through several related clauses',
  'because the synthesizer needs the entire grammatical structure to choose the right',
  'prosody instead of stopping in the middle and restarting abruptly at an arbitrary boundary.',
].join(' ');
assert.deepEqual(
  Array.from(speech.chunks(coherentSentence)),
  [coherentSentence],
  'normal response-length sentences should stay in one synthesis request',
);

const titledPerson = [
  'This response explains the architecture clearly and preserves the operator request while',
  'validating every downstream contract including Dr. Smith recommendations and all subsequent stages.',
].join(' ');
const titledChunks = Array.from(speech.chunks(titledPerson, { maxChars: 140 }));
assert.ok(
  titledChunks.every(chunk => !/\b(?:Dr|Mr|Mrs|Ms|Prof)\.$/i.test(chunk)),
  'a chunk must not mistake a title abbreviation for the end of a sentence',
);

const clauseFree = [
  'This deliberately long utterance keeps flowing without punctuation because the speech system',
  'still needs to communicate continuation instead of applying final sentence cadence at an',
  'arbitrary character boundary before the thought is actually complete.',
].join(' ');
const clauseFreeChunks = Array.from(speech.chunks(clauseFree, { maxChars: 140 }));
assert.ok(clauseFreeChunks.length > 1);
assert.match(
  clauseFreeChunks[0],
  /,$/,
  'an unavoidable synthetic split must carry continuation punctuation',
);

const naturalClauses = [
  'This long sentence keeps enough context for natural delivery, while the next related clause',
  'continues the same thought without forcing the voice to stop as if the response were already',
  'finished, and the final clause completes the meaning cleanly.',
].join(' ');
const naturalClauseChunks = Array.from(speech.chunks(naturalClauses, { maxChars: 140 }));
assert.equal(
  naturalClauseChunks[0],
  'This long sentence keeps enough context for natural delivery,',
  'a real clause pause is better than a later arbitrary length cut',
);

const punctuationAtLimit = `${'a'.repeat(80)}, and then the thought continues.`;
assert.ok(
  Array.from(speech.chunks(punctuationAtLimit, { maxChars: 80 }))
    .every(chunk => chunk.length <= 80),
  'continuation punctuation must not make a synthesis chunk exceed its bound',
);

const longText = Array.from(
  { length: 8 },
  (_, i) => `Scenario ${i + 1} uses a fixed number of trials and needs one concise explanation.`
).join(' ');
const chunks = speech.chunks(longText, { maxChars: 140 });
assert.ok(chunks.length >= 4, 'long responses must become multiple synthesis requests');
assert.ok(chunks.every(chunk => chunk.length <= 140));
assert.equal(chunks.join(' '), speech.normalize(longText));

// ── time-to-first-audio ────────────────────────────────────────────────────
// Kokoro synthesises at ~1.2x realtime (measured 2026-08-01: 10.6s of audio in
// ~8s), so a full-size first request means the operator watches the text land
// and then sits in silence for seconds. Chunks therefore RAMP: a small opening
// request gets speech started, and each later one may grow because it is
// synthesised behind the previous chunk's playback.
const multiSentence = [
  'Hello there.',
  'I am Omega, your autonomous cognitive partner.',
  'I was built to remember and reason locally, entirely on your own hardware.',
].join(' ');
const ramped = Array.from(speech.chunks(multiSentence));
assert.ok(ramped.length >= 3, 'a multi-sentence reply must not collapse into one request');
assert.equal(
  ramped[0],
  'Hello there.',
  'the first synthesis request must be the opening sentence alone',
);
assert.ok(
  ramped[0].length < ramped[ramped.length - 1].length,
  'chunks must ramp: small first request, larger ones behind playback',
);
assert.equal(ramped.join(' '), speech.normalize(multiSentence), 'ramping must not lose prose');

// Raising the opening budget to the full bound restores the old single-request
// behaviour — the seam/latency trade-off stays the operator's to make.
const tunedFirst = Array.from(speech.chunks(multiSentence, { firstChunkMaxChars: 300 }));
assert.deepEqual(
  tunedFirst,
  [speech.normalize(multiSentence)],
  'firstChunkMaxChars must be tunable for operators who prefer fewer seams',
);

// A single long sentence still cannot be started early — there is no earlier
// boundary to cut on — so the ramp must never fragment one below its own bound.
const oneLongSentence = Array.from(speech.chunks(coherentSentence));
assert.deepEqual(
  oneLongSentence,
  [coherentSentence],
  'the ramp must not split a lone sentence that packing would have kept whole',
);

console.log('speech text normalization and chunking tests passed');
