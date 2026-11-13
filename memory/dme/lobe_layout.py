"""DME v2 — Dewey lobe bitmap layout inside 262144-bit entries.

Uses Omega's actual Dewey lobes (0–11), not legacy 5W1H WHO/WHAT/… names.
Each lobe owns a fixed word span in the entry for page-0 hot coordinates.
Paged growth (32768 slots/page) is handled by the coordinate registry; in-entry
bitmaps cover the first PAGE0_SLOTS per lobe.

2026-07-04: widened to 262144 bits (LongMemEval shakedown: a real _S
haystack mints ~10k unique tokens in ONE lobe; the old 320 hot slots/lobe
left answer memories with NO bits — invisible to every matmul). 21824 hot
slots per lobe; the schema version on the SRAM meta gates the rebuild.
"""

from __future__ import annotations

from memory.dewey_map import LOBE_MAP

ENTRY_WORDS = 4096
ENTRY_BITS = ENTRY_WORDS * 64
HEADER_WORDS = 3
PAYLOAD_WORDS = ENTRY_WORDS - HEADER_WORDS
PAYLOAD_BITS = PAYLOAD_WORDS * 64
PAGE_SIZE = 32768
WORDS_PER_LOBE = (ENTRY_WORDS - HEADER_WORDS) // 12  # 341 words per lobe
PAGE0_SLOTS = WORDS_PER_LOBE * 64  # 21824 hot slots per lobe

DEWEY_LOBE_IDS = tuple(sorted(LOBE_MAP.keys()))
LOBE_NAME = dict(LOBE_MAP)

# Word offset for each Dewey lobe's page-0 bitmap region.
LOBE_WORD_OFFSET: dict[int, int] = {
    lobe_id: HEADER_WORDS + (idx * WORDS_PER_LOBE) for idx, lobe_id in enumerate(DEWEY_LOBE_IDS)
}


def addr_to_bit_position(lobe: int, page: int, slot: int) -> tuple[int, int, int]:
    """Map (lobe, page, slot) to word index and bit index within the entry."""
    if lobe not in LOBE_WORD_OFFSET:
        raise ValueError(f"Unknown Dewey lobe: {lobe}")
    if page != 0:
        raise ValueError("In-entry bitmap supports page 0 only; use registry pages for page > 0")
    if slot < 0 or slot >= PAGE0_SLOTS:
        raise ValueError(f"Slot {slot} out of page-0 range 0..{PAGE0_SLOTS - 1}")
    word_offset = LOBE_WORD_OFFSET[lobe]
    word_index = word_offset + (slot // 64)
    bit_index = slot % 64
    return lobe, word_index, bit_index


def coordinate_tuple(raw) -> tuple[int, int, int] | None:
    """Normalize coord input to (lobe, page, slot) using Dewey (X,Y,Z)."""
    if raw is None:
        return None
    if isinstance(raw, tuple):
        parts = raw
    elif isinstance(raw, list):
        parts = tuple(raw)
    else:
        return None
    if len(parts) < 1:
        return None
    try:
        lobe = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 else 0
        slot = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        return None
    return lobe, page, slot
