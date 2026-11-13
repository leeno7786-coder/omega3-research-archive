"""DME v2 packer — fixed-width Dewey-lobe coordinate entries."""

from __future__ import annotations

import hashlib
import struct
from typing import Iterable

import numpy as np

from memory.dme.lobe_layout import (
    ENTRY_WORDS,
    HEADER_WORDS as _HEADER_WORDS,
    addr_to_bit_position,
    coordinate_tuple,
)

HEADER_WORDS = _HEADER_WORDS

SOURCE_AUTHORITY = {
    "user": 100,
    "identity": 95,
    "gate": 90,
    "operator_chat": 85,
    "web": 60,
    "autonomous": 50,
    "system": 30,
}


def _source_hash(source: str) -> int:
    return int.from_bytes(hashlib.sha256(source.encode()).digest()[:4], "big")


def _authority_for(source: str) -> int:
    if source in SOURCE_AUTHORITY:
        return SOURCE_AUTHORITY[source]
    for prefix, weight in SOURCE_AUTHORITY.items():
        if source.startswith(prefix):
            return weight
    return 30


def pack_entry(
    fingerprint: int,
    timestamp: float,
    source: str,
    coordinates: Iterable[tuple[int, int, int] | list[int]],
) -> np.ndarray:
    entry = np.zeros(ENTRY_WORDS, dtype=np.uint64)
    entry[0] = np.uint64(fingerprint & 0xFFFFFFFFFFFFFFFF)
    entry[1] = np.frombuffer(struct.pack("d", timestamp), dtype=np.uint64)[0]
    src_hash = _source_hash(source) & 0xFFFFFFFF
    authority = _authority_for(source) & 0xFFFF
    entry[2] = np.uint64((src_hash << 32) | (authority << 16))
    for raw in coordinates:
        coord = coordinate_tuple(raw)
        if coord is None:
            continue
        set_coordinate(entry, coord)
    return entry


def set_coordinate(entry: np.ndarray, coord: tuple[int, int, int]) -> None:
    lobe, page, slot = coord
    if page != 0:
        return
    _, word_index, bit_index = addr_to_bit_position(lobe, page, slot)
    entry[word_index] |= np.uint64(1 << bit_index)


def has_coordinate(entry: np.ndarray, coord: tuple[int, int, int]) -> bool:
    lobe, page, slot = coord
    if page != 0:
        return False
    try:
        _, word_index, bit_index = addr_to_bit_position(lobe, page, slot)
    except ValueError:
        return False
    return bool(int(entry[word_index]) & (1 << bit_index))


def unpack_fingerprint(entry: np.ndarray) -> int:
    return int(entry[0])


def unpack_timestamp(entry: np.ndarray) -> float:
    return struct.unpack("d", entry[1].tobytes())[0]


def unpack_source_authority(entry: np.ndarray) -> int:
    return (int(entry[2]) >> 16) & 0xFFFF
