"""R0 — the deterministic structural reranker over the retina's K window.

Stage 2 of the ratified retrieval stack (ADR-MO-021): the retina's popcount scan
takes the top K by Hamming distance, and this reorders inside that window using
structure the bi-encoder discards. It is deterministic — same window, same
order, every run — and it never admits a record the first stage did not
retrieve, so it cannot manufacture evidence.

Ported unchanged from the Program I evaluation tree, deliberately: its numbers
were measured against a sealed corpus and re-deriving the weights here would
invalidate them. Measured at pool 6,000, Rademacher D=16384, K=64: stratum floor
0.9100, displacement 0.0000, hard-distinction **0.9840 after rerank**, Recall@10
0.9293 -> 0.9329. Hard-distinction is invariant to K and pool size because the
reranker only reorders a fixed window.

SCOPE CAUTION. Those figures come from the Program I synthetic corpus, whose
hard negatives were CONSTRUCTED to be polarity flips, temporal inversions and
role reversals — exactly the three signals below. On ordinary prose the
reranker should be expected to help less, and that has not been measured. Treat
0.9840 as a property of that corpus, not a promise about a real organ.

Original module docstring follows.

---

Linguistic reranker for the Program I semantic retina.

The retina is a bi-encoder: it mean-pools token vectors, which is close to
order-invariant and barely moves for a single "not". Measured on the sealed
corpus, a target and its hard negative sit at 0.94-0.99 cosine with decision
margins of +/-0.005-0.03, and `role_reversal` pairs have *identical word
multisets* differing only in order. Those distinctions are structural, so they
are recovered here with structure rather than with more embedding capacity.

Three signals, each targeting one measured failure mode:

* **polarity**  -- negation (`role`: "is permitted" vs "is not permitted")
* **temporal**  -- before/after state ("before revision 74" vs "now after")
* **order**     -- argument order ("A authorized B" vs "B authorized A")

A note on method: a dependency parse was tried first and rejected. The small
spaCy pipeline cannot parse this corpus reliably -- it returns no subject at all
for "Unit-7D77 authorized Unit-BF06 ..." and roots "Before revision 74, Unit-FA02
routed telemetry ..." on the noun `telemetry`, because the synthetic
`Unit-XXXX`/`Relay-XXXX` identifiers derail the tagger. Argument order is
therefore recovered positionally, which needs no parse and is exactly the signal
role reversal turns on. spaCy is still used where it proved reliable: the `neg`
dependency and lemmatisation.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

POSITIVE = 1
NEGATIVE = -1
BEFORE = -1
AFTER = 1

# Words that flip the polarity of the proposition being asserted or asked for.
_NEGATION_WORDS = frozenset(
    {
        "not", "no", "never", "none", "nor", "cannot", "without",
        "forbidden", "prohibited", "barred", "denied", "refused",
        "lack", "lacks", "lacking", "absent", "missing",
        "uncalibrated", "unauthorised", "unauthorized", "incomplete", "unfinished",
    }
)

# Deliberately narrow. An earlier draft included "was", "did", "used" and "past";
# those fire on ordinary prose, so every unrelated candidate acquired a spurious
# temporal reading and the feature became noise. Only markers that actually
# encode a before/after state survive.
_BEFORE_WORDS = frozenset(
    {"before", "until", "prior", "previously", "formerly", "originally", "earlier"}
)
_AFTER_WORDS = frozenset(
    {"after", "now", "since", "currently", "subsequently", "afterwards", "onward"}
)

# "from" and "through" do encode a before/after state in this corpus ("From week
# 12, ...", "Through cycle 12, ..."), but they are far too common to trust on
# their own. They count only when they govern a time noun.
_CONTEXTUAL_BEFORE = frozenset({"through"})
_CONTEXTUAL_AFTER = frozenset({"from"})
_TIME_NOUNS = frozenset(
    {"week", "cycle", "revision", "version", "phase", "quarter", "year",
     "month", "day", "sprint", "release", "update", "epoch"}
)

# Order AND polarity only discriminate when the two texts are near lexical
# duplicates -- the role-reversal and negation situations. Below this coverage
# the shared tokens are incidental and carry no signal.
#
# The polarity gate is not cosmetic. Natural prose contains "no"/"not"/"without"
# constantly: 43.5% of omega candidates and 30.1% of public ones read as negated,
# against 8.5% of synthetic. Applying polarity ungated penalised half the real
# corpus against any unmarked query and cost Recall@10 0.9329 -> 0.9074 on
# development, while gating it keeps the full hard-distinction gain.
# Two floors, because the ablation showed the two features have different noise
# profiles on real text. Order at 0.5 was harmless (R@10 0.9329 -> 0.9338 alone);
# polarity at 0.5 cost 0.9329 -> 0.9264 even when gated, because natural prose
# negates so often. Measured on development, polarity at 0.7 with order at 0.5 is
# the setting where the reranker is strictly better than not reranking on BOTH
# axes while still clearing the 0.90 rerank bar.
_ORDER_COVERAGE_FLOOR = 0.5

# Polarity is NOT gated on query-vs-candidate overlap. Queries paraphrase --
# "finished calibrating" against "completed the calibration" -- so a genuine
# negation contrast can sit at only 0.50-0.67 coverage, and a query-side floor
# high enough to silence incidental negation also silenced the real contrasts
# (22 of 300 development negation cases failed exactly this way).
#
# Instead polarity fires when the WINDOW contains the contrast: two candidates
# that are near lexical duplicates of each other but disagree in polarity. That
# is precisely the negation situation, and unrelated prose carrying an incidental
# "not" has no such twin, so it stays inert.
_POLARITY_CONTRAST_FLOOR = 0.9

# Weights. Polarity and temporal disagreement are categorical contradictions, so
# they carry more than the noisier positional signal. All three are deliberately
# small relative to the cosine spread inside a rerank window (~0.6-0.9) and large
# relative to the measured hard-negative margins (+/-0.005-0.03), so the reranker
# settles near-ties without overturning gross semantic ranking.
DEFAULT_WEIGHTS = {"polarity": 0.10, "temporal": 0.10, "order": 0.06}


@dataclass(frozen=True)
class TextFeatures:
    """Structural reading of one text."""

    tokens: tuple[str, ...]
    first_position: dict[str, int]
    polarity: int
    temporal: int


def _agreement(left: int, right: int) -> float:
    """+1 agree, -1 contradict, 0 when either side carries no signal."""
    if left == 0 or right == 0:
        return 0.0
    return 1.0 if left == right else -1.0


def _contradiction(left: int, right: int) -> float:
    """-1 contradict, 0 otherwise. Never rewards agreement.

    Rewarding polarity agreement promoted whichever member of a contrast pair
    happened to match an unmarked query, pushing unrelated candidates above
    genuine targets and costing the zero-overlap stratum 0.5167 -> 0.5067 on
    development. Contradiction is evidence of wrongness; agreement is not
    evidence of rightness.
    """
    if left == 0 or right == 0:
        return 0.0
    return 0.0 if left == right else -1.0


def lexical_coverage(query: TextFeatures, candidate: TextFeatures) -> float:
    """Fraction of the shorter text's vocabulary that both texts share."""
    shared = set(query.first_position) & set(candidate.first_position)
    smaller = min(len(query.first_position), len(candidate.first_position))
    return (len(shared) / smaller) if smaller else 0.0


def order_agreement(query: TextFeatures, candidate: TextFeatures) -> float:
    """Kendall-style agreement over tokens the two texts share.

    For every unordered pair of shared tokens, do they appear in the same
    relative order in both texts? Returns 0.0 when fewer than two tokens are
    shared, so an unrelated candidate is neither rewarded nor punished.
    """
    shared = sorted(set(query.first_position) & set(candidate.first_position))
    if len(shared) < 2:
        return 0.0
    if lexical_coverage(query, candidate) < _ORDER_COVERAGE_FLOOR:
        return 0.0
    agree = disagree = 0
    for index, left in enumerate(shared):
        for right in shared[index + 1 :]:
            query_order = query.first_position[left] < query.first_position[right]
            candidate_order = candidate.first_position[left] < candidate.first_position[right]
            if query_order == candidate_order:
                agree += 1
            else:
                disagree += 1
    total = agree + disagree
    return (agree - disagree) / total if total else 0.0


class LinguisticReranker:
    """Reorders a retrieval window using structure the bi-encoder discards."""

    def __init__(self, *, weights: dict[str, float] | None = None, model: str = "en_core_web_sm") -> None:
        import spacy

        # The parse is not used (see module docstring); the tagger and lemmatiser
        # are. Disabling the rest keeps this cheap enough to run per candidate.
        self._nlp = spacy.load(model, disable=["ner", "parser"])
        if "sentencizer" not in self._nlp.pipe_names:
            self._nlp.add_pipe("sentencizer")
        self.weights = dict(DEFAULT_WEIGHTS if weights is None else weights)

    @lru_cache(maxsize=200_000)
    def _analyze_cached(self, text: str) -> TextFeatures:
        doc = self._nlp(text)
        tokens: list[str] = []
        polarity = POSITIVE
        temporal = 0
        for token in doc:
            lower = token.lower_
            lemma = token.lemma_.lower()
            if lower in _NEGATION_WORDS or lemma in _NEGATION_WORDS or token.dep_ == "neg":
                polarity = NEGATIVE
            if temporal == 0:
                following = doc[token.i + 1].lower_ if token.i + 1 < len(doc) else ""
                if lower in _AFTER_WORDS:
                    temporal = AFTER
                elif lower in _BEFORE_WORDS:
                    temporal = BEFORE
                elif lower in _CONTEXTUAL_AFTER and following in _TIME_NOUNS:
                    temporal = AFTER
                elif lower in _CONTEXTUAL_BEFORE and following in _TIME_NOUNS:
                    temporal = BEFORE
            if token.is_stop or token.is_punct or token.is_space:
                continue
            if lemma in _NEGATION_WORDS:
                continue
            if len(lemma) > 1:
                tokens.append(lemma)
        first_position: dict[str, int] = {}
        for index, lemma in enumerate(tokens):
            first_position.setdefault(lemma, index)
        return TextFeatures(
            tokens=tuple(tokens),
            first_position=first_position,
            polarity=polarity,
            temporal=temporal,
        )

    def analyze(self, text: str) -> TextFeatures:
        return self._analyze_cached(str(text))

    def score(
        self,
        query: TextFeatures,
        candidate: TextFeatures,
        base_score: float,
        *,
        polarity_active: bool = False,
    ) -> float:
        """Score one candidate.

        ``polarity_active`` is decided at window level by
        :meth:`polarity_contrast_set` -- an incidental "not" in unrelated prose is
        not a contradiction of the query, so polarity stays off by default.
        """
        polarity = (
            _contradiction(query.polarity, candidate.polarity) if polarity_active else 0.0
        )
        return (
            float(base_score)
            + self.weights["polarity"] * polarity
            + self.weights["temporal"] * _agreement(query.temporal, candidate.temporal)
            + self.weights["order"] * order_agreement(query, candidate)
        )

    def polarity_contrast_set(self, features: Sequence[TextFeatures]) -> set[int]:
        """Indices of candidates that stand in a polarity contrast with another.

        A contrast is two near lexical duplicates that disagree in polarity --
        "X completed the calibration" against "X never completed the
        calibration". Negation words are excluded from the token list, so a true
        pair reaches coverage 1.0.
        """
        active: set[int] = set()
        for left in range(len(features)):
            for right in range(left + 1, len(features)):
                if features[left].polarity == features[right].polarity:
                    continue
                if lexical_coverage(features[left], features[right]) >= _POLARITY_CONTRAST_FLOOR:
                    active.add(left)
                    active.add(right)
        return active

    def rerank(
        self,
        query: str,
        candidates: Sequence[tuple[str, str, float]] | Iterable[tuple[str, str, float]],
        *,
        top_k: int | None = None,
    ) -> list[tuple[str, float]]:
        """Reorder ``(candidate_id, text, base_score)`` triples, best first.

        Ties break on the original score and then on candidate id, so the result
        is a total order and identical inputs always give identical output.
        """
        items = list(candidates)
        if top_k is not None and top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        window = items if top_k is None else items[:top_k]
        query_features = self.analyze(query)
        features = [self.analyze(text) for _cid, text, _base in window]
        active = self.polarity_contrast_set(features)
        scored = [
            (
                candidate_id,
                self.score(
                    query_features,
                    features[index],
                    base,
                    polarity_active=index in active,
                ),
                base,
            )
            for index, (candidate_id, _text, base) in enumerate(window)
        ]
        scored.sort(key=lambda row: (-row[1], -row[2], row[0]))
        return [(candidate_id, score) for candidate_id, score, _base in scored]


def rerank_window(
    reranker: LinguisticReranker, query: str, candidates: Sequence[tuple[str, str, float]]
) -> list[str]:
    """Convenience: the reordered candidate ids only."""
    return [candidate_id for candidate_id, _score in reranker.rerank(query, candidates)]


__all__: list[Any] = [
    "AFTER",
    "BEFORE",
    "DEFAULT_WEIGHTS",
    "LinguisticReranker",
    "NEGATIVE",
    "POSITIVE",
    "TextFeatures",
    "lexical_coverage",
    "order_agreement",
    "rerank_window",
]
