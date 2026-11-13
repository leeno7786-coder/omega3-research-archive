"""Paged coordinate registry keyed by Dewey lobe."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

from memory.dme.lobe_layout import PAGE0_SLOTS, PAGE_SIZE

DEFAULT_REGISTRY_NAME = "coordinate_registry.json"

# Task 1 instrumentation (projection-speed plan): realized-work counter, not
# a configured-value receipt -- counts ``_save()`` invocations that actually
# ran. Every new key rewrites the WHOLE registry via the pure-Python
# ``json.dumps(..., indent=2)`` path, so this is the counter that shows
# whether that cost was paid once or thousands of times.
_save_call_count = 0


def registry_save_count() -> int:
    """Cumulative process-wide count of ``CoordinateRegistry._save()`` calls."""
    return _save_call_count

# Below this length a single edit reaches unrelated words, so fuzzy resolution
# is refused outright rather than risking a lobe-wide union match.
MIN_FUZZY_LENGTH = 5


def _restricted_edit_distance(left: str, right: str, max_distance: int) -> int:
    """Optimal string alignment distance, giving up once it exceeds the bound.

    Adjacent transpositions count as one edit, so the common "teh"/"the" class
    of typo is recovered as cheaply as an insertion.
    """

    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    previous: list[int] = list(range(len(right) + 1))
    before_previous: list[int] = []
    for i, left_char in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        best = current[0]
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            if (
                i > 1
                and j > 1
                and left_char == right[j - 2]
                and left[i - 2] == right_char
            ):
                value = min(value, before_previous[j - 2] + 1)
            current[j] = value
            best = min(best, value)
        if best > max_distance:
            return max_distance + 1
        before_previous, previous = previous, current
    return previous[-1]


class CoordinateRegistry:
    """Allocate stable (lobe, page, slot) addresses for canonical tokens.

    Cut 13: ``readonly=True`` (a registry captured from a published generation) refuses
    every registration — published generations are immutable; only the builder writes
    a registry, into a candidate (``strict=True`` ⇒ malformed JSON raises instead of
    yielding an empty registry)."""

    def __init__(self, path: Path, *, readonly: bool = False, strict: bool = False):
        self._path = path
        self._readonly = bool(readonly)
        self._strict = bool(strict)
        self._lock = threading.Lock()
        self._defer_saves = False
        self._addresses: dict[str, tuple[int, int, int]] = {}
        self._counters: dict[int, int] = {lobe: 0 for lobe in range(12)}
        self._page_counters: dict[int, int] = {lobe: 0 for lobe in range(12)}
        # Lazy (first char, length) buckets for fuzzy query resolution. Dropped
        # whenever the vocabulary grows so a stale snapshot cannot hide a key.
        self._fuzzy_buckets: dict[tuple[str, int], list[str]] | None = None
        self._load()

    def lookup(self, key: str) -> tuple[int, int, int] | None:
        return self._addresses.get(key.lower().strip())

    def register(self, key: str, lobe: int) -> tuple[int, int, int]:
        canonical = key.lower().strip()
        if not canonical:
            raise ValueError("empty coordinate key")
        with self._lock:
            existing = self._addresses.get(canonical)
            if existing is not None:
                return existing
            if self._readonly:
                from memory.projection.generation import GenerationIntegrityError

                raise GenerationIntegrityError(
                    f"registry {self._path.name} belongs to a published generation and is read-only (key {canonical!r} not registered)")
            page, slot = self._allocate(lobe)
            addr = (lobe, page, slot)
            self._addresses[canonical] = addr
            self._fuzzy_buckets = None
            if not self._defer_saves:
                self._save()
            return addr

    @contextmanager
    def bulk_registration(self):
        """Defer the per-key JSON save during bulk seating — a fresh eval
        store registers tens of thousands of tokens, and rewriting the whole
        registry file per token was ~170s/instance. One save at exit."""
        self._defer_saves = True
        try:
            yield self
        finally:
            self._defer_saves = False
            with self._lock:
                self._save()

    def register_or_lookup(self, key: str, lobe: int) -> tuple[int, int, int]:
        found = self.lookup(key)
        if found is not None:
            return found
        return self.register(key, lobe)

    def near_matches(
        self,
        key: str,
        *,
        max_distance: int = 1,
        min_length: int = MIN_FUZZY_LENGTH,
        limit: int = 3,
    ) -> list[tuple[int, int, int]]:
        """Addresses of registered keys within a small edit distance of ``key``.

        A source typo mints its own coordinate, so a correctly spelled query
        resolves to a different address and an otherwise-perfect record never
        scores. This widens resolution on the QUERY side only — no coordinate is
        minted — so seat geometry and the stopword fabrication guard are
        untouched.

        Short keys are refused: at length 3 a single edit reaches genuinely
        unrelated words ("cat"/"car"/"cab"), which would union-match the lobe.
        Candidates are bucketed by first character, so a typo in the leading
        character is not recovered; that is the deliberate cost of keeping this
        cheap enough to run per query token.
        """

        canonical = key.lower().strip()
        if len(canonical) < min_length or max_distance < 1:
            return []
        buckets = self._fuzzy_buckets_snapshot()
        head = canonical[0]
        scored: list[tuple[int, str]] = []
        for length in range(len(canonical) - max_distance, len(canonical) + max_distance + 1):
            for candidate in buckets.get((head, length), ()):
                if candidate == canonical:
                    continue
                distance = _restricted_edit_distance(canonical, candidate, max_distance)
                if distance <= max_distance:
                    scored.append((distance, candidate))
        scored.sort()
        out: list[tuple[int, int, int]] = []
        for _distance, candidate in scored[:limit]:
            address = self._addresses.get(candidate)
            if address is not None:
                out.append(address)
        return out

    def _fuzzy_buckets_snapshot(self) -> dict[tuple[str, int], list[str]]:
        with self._lock:
            if self._fuzzy_buckets is None:
                buckets: dict[tuple[str, int], list[str]] = {}
                for candidate in self._addresses:
                    if len(candidate) < MIN_FUZZY_LENGTH:
                        continue
                    buckets.setdefault((candidate[0], len(candidate)), []).append(candidate)
                self._fuzzy_buckets = buckets
            return self._fuzzy_buckets

    def _allocate(self, lobe: int) -> tuple[int, int]:
        count = self._counters.get(lobe, 0)
        if count < PAGE0_SLOTS:
            self._counters[lobe] = count + 1
            return 0, count
        page = self._page_counters.get(lobe, 0) + 1
        self._page_counters[lobe] = page
        slot = count - PAGE0_SLOTS
        if slot >= PAGE_SIZE:
            raise ValueError(f"DME lobe {lobe} coordinate page exhausted")
        return page, slot

    def _load(self) -> None:
        if not self._path.exists():
            if self._strict:
                from memory.projection.generation import GenerationIntegrityError

                raise GenerationIntegrityError(f"registry missing: {self._path}")
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            if self._strict:
                from memory.projection.generation import GenerationIntegrityError

                raise GenerationIntegrityError(f"registry malformed (never 'empty' under generation mode): {self._path}")
            return
        if self._strict and not isinstance(data, dict):
            from memory.projection.generation import GenerationIntegrityError

            raise GenerationIntegrityError(f"registry malformed (not an object): {self._path}")
        for key, raw in dict(data.get("addresses") or {}).items():
            if isinstance(raw, (list, tuple)) and len(raw) == 3:
                self._addresses[key] = (int(raw[0]), int(raw[1]), int(raw[2]))
        self._counters = {int(k): int(v) for k, v in dict(data.get("counters") or {}).items()}
        self._page_counters = {
            int(k): int(v) for k, v in dict(data.get("page_counters") or {}).items()
        }

    def _save(self) -> None:
        global _save_call_count
        if self._readonly:
            from memory.projection.generation import GenerationIntegrityError

            raise GenerationIntegrityError("read-only registry (published generation) cannot be saved")
        _save_call_count += 1
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "addresses": {k: list(v) for k, v in sorted(self._addresses.items())},
            "counters": self._counters,
            "page_counters": self._page_counters,
        }
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _generation_view(root: Path | str | None):
    from memory.projection.generation import resolve_view_or_legacy

    mode, view = resolve_view_or_legacy(root)
    return view if mode == "generation" else None


def legacy_registry_path(root: Path | str | None = None) -> Path:
    if root is None:
        from core.paths import MEMORY_ROOT

        base = MEMORY_ROOT
    else:
        base = Path(root)
        if base.name != "memory":
            base = base / "memory"
    return base / "dme" / DEFAULT_REGISTRY_NAME


def registry_path(root: Path | str | None = None) -> Path:
    """Legacy: today's fixed ``dme/coordinate_registry.json``. Generation mode (Cut 13):
    the ambient view's immutable registry snapshot; a manifest without a view raises
    GenerationViewRequired."""
    view = _generation_view(root)
    if view is not None:
        return view.coordinate_registry
    return legacy_registry_path(root)


# mtime/size-keyed instance cache: recall paths call load_registry per query,
# and re-parsing the JSON each time was a visible slice of Mode-C latency. A
# register() saves to disk, so the stamp moves and the next load reparses —
# coherent by construction, deterministic either way.
_REGISTRY_CACHE: dict[str, tuple[tuple, CoordinateRegistry]] = {}


_VIEW_REGISTRY_CACHE: dict[str, CoordinateRegistry] = {}


def load_registry(root: Path | str | None = None) -> CoordinateRegistry:
    view = _generation_view(root)
    if view is not None:
        # Cut 13: STRICT, read-only, verified bytes, cached per generation (immutable)
        cached = _VIEW_REGISTRY_CACHE.get(view.generation_id)
        if cached is not None:
            return cached
        view.read_bytes("coordinate_registry.json")  # verify against the manifest (fail loud)
        registry = CoordinateRegistry(view.coordinate_registry, readonly=True, strict=True)
        _VIEW_REGISTRY_CACHE[view.generation_id] = registry
        return registry
    path = registry_path(root)
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    key = str(path)
    hit = _REGISTRY_CACHE.get(key)
    if hit is not None and stamp is not None and hit[0] == stamp:
        return hit[1]
    registry = CoordinateRegistry(path)
    if stamp is not None:
        _REGISTRY_CACHE[key] = (stamp, registry)
    return registry
