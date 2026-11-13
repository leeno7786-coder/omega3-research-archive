// @module core/speech-text.js
// Speech-only normalization and grammar-aware utterance chunking. The visible
// chat keeps its Markdown; Kokoro receives coherent prose whose request
// boundaries carry the sentence/clause prosody needed for natural playback.
(function (root) {
  'use strict';

  const PROTECTED_DOT = '\uE000';
  const TITLE_ABBREVIATION = /\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|St)\.$/i;

  function normalize(value) {
    let text = String(value == null ? '' : value);
    if (!text.trim()) return '';
    text = text
      .replace(/```[\s\S]*?```/g, ' Code block omitted. ')
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, ' $1 ')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/<https?:\/\/[^>]+>/gi, ' ')
      .replace(/https?:\/\/\S+/gi, ' ')
      .replace(/`([^`]+)`/g, '$1')
      // eSpeak preserves abbreviation periods as prosodic stops. Expand the
      // common forms before chunking so they cannot end a thought early.
      .replace(/\be\.g\./gi, 'for example')
      .replace(/\bi\.e\./gi, 'that is')
      .replace(/\bvs\./gi, 'versus')
      .replace(/\betc\./gi, 'and so on')
      .replace(/\bDr\./gi, 'Doctor')
      .replace(/\bMr\./gi, 'Mister')
      .replace(/\bMrs\./gi, 'Missus')
      .replace(/\bMs\./gi, 'Miz')
      .replace(/\bProf\./gi, 'Professor')
      .replace(/\bSr\./gi, 'Senior')
      .replace(/\bJr\./gi, 'Junior')
      .replace(/\b(?:[A-Za-z]\.){2,}/g, match => (
        (match.match(/[A-Za-z]/g) || []).join(' ')
      ))
      // Markdown structure is a real pause; ordinary visual wrapping is not.
      .replace(/^\s{0,3}#{1,6}\s*/gm, '. ')
      .replace(/^\s*>\s?/gm, '')
      .replace(/^\s*[-+*]\s+/gm, '. ')
      .replace(/^\s*\d+[.)]\s+/gm, '. ')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/__([^_]+)__/g, '$1')
      .replace(/\*([^*\n]+)\*/g, '$1')
      .replace(/_([^_\n]+)_/g, '$1')
      .replace(/[~*_]+/g, '')
      .replace(/(?:→|=>)/g, ', then ')
      .replace(/[•▪◦]/g, '. ')
      .replace(/&/g, ' and ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\r?\n[ \t]*\r?\n+/g, '. ')
      .replace(/\r?\n/g, ' ')
      .replace(/\s+([,.!?;:])/g, '$1')
      .replace(/\.\s*\./g, '.')
      .replace(/\s+/g, ' ')
      .replace(/^\.\s*/, '')
      .trim();
    if (text && !/[.!?]$/.test(text)) {
      text = text.replace(/[,;:]+$/, '') + '.';
    }
    return text;
  }

  function _protectSentenceDots(value) {
    return value
      .replace(/(\d)\.(?=\d)/g, `$1${PROTECTED_DOT}`)
      .replace(/\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|St)\./gi, match => (
        match.slice(0, -1) + PROTECTED_DOT
      ))
      .replace(/\b(?:[A-Za-z]\.){2,}/g, (match, offset, source) => {
        const following = source.slice(offset + match.length).match(/^\s*([A-Za-z])/);
        const continues = !!following && following[1] === following[1].toLowerCase();
        const stem = continues ? match : match.slice(0, -1);
        const protectedStem = stem.replace(/\./g, PROTECTED_DOT);
        return continues ? protectedStem : `${protectedStem}.`;
      });
  }

  function _sentences(value) {
    return _protectSentenceDots(value)
      .split(/(?<=[.!?])\s+/)
      .filter(Boolean)
      .map(sentence => sentence.replaceAll(PROTECTED_DOT, '.'));
  }

  function _artificialCut(rest, maxChars) {
    const minCut = Math.floor(maxChars * 0.55);
    const minNaturalCut = Math.min(minCut, 40);
    const search = rest.slice(0, maxChars + 1);
    let naturalCut = -1;
    const clauseBoundary = /[,;:—–-](?=\s)/g;
    let match;
    while ((match = clauseBoundary.exec(search)) !== null) {
      const candidate = match.index + 1;
      if (candidate >= minNaturalCut) naturalCut = candidate;
    }
    if (naturalCut >= 0) return { cut: naturalCut, continuation: false };

    // Reserve one character for a continuation comma. Without it, Kokoro
    // applies final-sentence cadence to an arbitrary length boundary.
    let cut = rest.lastIndexOf(' ', maxChars - 1);
    if (cut < minCut) cut = Math.max(1, maxChars - 1);
    while (cut > minCut && TITLE_ABBREVIATION.test(rest.slice(0, cut).trim())) {
      cut = rest.lastIndexOf(' ', cut - 1);
    }
    return { cut, continuation: true };
  }

  function _splitLong(segment, maxChars) {
    const out = [];
    let rest = segment;
    while (rest.length > maxChars) {
      const split = _artificialCut(rest, maxChars);
      const cut = split.cut;
      let head = rest.slice(0, cut).trim();
      if (split.continuation && !/[,;:—–-]$/.test(head)) head += ',';
      out.push(head);
      rest = rest.slice(cut).trim();
    }
    if (rest) out.push(rest);
    return out;
  }

  function chunks(value, opts = {}) {
    const text = normalize(value);
    if (!text) return [];
    const rawMax = Number(opts.maxChars || 280);
    const maxChars = Math.max(80, Math.min(360, Number.isFinite(rawMax) ? rawMax : 280));
    // TIME-TO-FIRST-AUDIO. Kokoro synthesises at ~1.2x realtime (measured
    // 2026-08-01: 10.6s of audio in ~8s on this CPU), and KokoroTTS._drain
    // prefetches exactly one chunk ahead. So packing a whole reply into one
    // 280-char request buys nothing but silence — the operator reads the text
    // and waits seconds for a voice. Instead the budget RAMPS: a small opening
    // request starts speech almost immediately, and every later chunk is
    // synthesised behind the previous chunk's playback, so it can afford to be
    // bigger. Growth stays modest because playback of chunk N only covers
    // synthesis of chunk N+1 while N+1 is not much longer than N.
    const rawFirst = Number(opts.firstChunkMaxChars || 48);
    const firstMax = Math.max(
      24,
      Math.min(maxChars, Number.isFinite(rawFirst) ? rawFirst : 48),
    );
    const GROWTH = 1.6;
    const sentences = _sentences(text);
    const pieces = sentences.flatMap(sentence => _splitLong(sentence, maxChars));
    const out = [];
    let current = '';
    let budget = firstMax;
    for (const piece of pieces) {
      const candidate = current ? `${current} ${piece}` : piece;
      // An empty `current` always accepts the piece: a lone sentence longer than
      // the budget has no earlier boundary to start on, and fragmenting it would
      // only add seams without starting speech any sooner.
      if (current && candidate.length > budget) {
        out.push(current);
        current = piece;
        budget = Math.min(maxChars, Math.ceil(budget * GROWTH));
      } else {
        current = candidate;
      }
    }
    if (current) out.push(current);
    return out;
  }

  root.OmegaSpeechText = Object.freeze({ normalize, chunks });
})(typeof globalThis !== 'undefined' ? globalThis : window);
