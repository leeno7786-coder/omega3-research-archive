"""Task #12 — pre-verifier anchor filter + widen ladder (RATIFIED, 2026-07-04).

Anchor basis (verbatim): a candidate may reach the verifier only with a
lexical anchor OR a strong edge OR hop <= 1 OR a compiler-proven alias/
session bridge. The filter only REMOVES candidates — an emptied list flows
to UNKNOWN exactly like an empty recall, so abstention is never blocked.

Widen ladder (verbatim): scoped-Y → +_unsorted → whole-lobe before UNKNOWN.
Grammar-scoped recalls only; explicit allowed_subdomains contracts never
widen.
"""

from __future__ import annotations

from typing import Any, Dict, List


class TestAnchorFilter:
    def _run(self, query: str, candidates):
        from memory.recall_anchor import filter_anchor_basis

        return filter_anchor_basis(query, candidates)

    @staticmethod
    def _dme(cid: str, summary: str, hop: int, edge_class: str) -> Dict[str, Any]:
        return {
            "id": cid,
            "title": "",
            "summary": summary,
            "score": 0.2,
            "provenance": {"candidate_source": "dme", "dme_hop": hop, "dme_edge_class": edge_class},
        }

    def test_weak_hop2_without_lexical_anchor_is_dropped(self):
        kept, dropped = self._run(
            "where is the depth atlas",
            [self._dme("9.00.aaa", "the ferry launched on the solstice", 2, "weak_hop2")],
        )
        assert kept == []
        assert len(dropped) == 1

    def test_strong_hop1_without_lexical_anchor_survives(self):
        # THE alias case: entity-bridged memory shares no query token
        kept, dropped = self._run(
            "what gadget balances the ballast",
            [self._dme("5.00.bbb", "the Aegir-9 pump controller regulates flow", 1, "strong_hop1")],
        )
        assert len(kept) == 1 and not dropped

    def test_weak_hop1_without_lexical_anchor_is_dropped(self):
        # Gate-1 live finding (2026-07-04): weak hop-1 arrivals with zero
        # lexical tie were the fabrication vector (theme-cosine rescued
        # same-subdomain neighbors on isolated-alias queries). Weak placement
        # reach needs a lexical anchor at ANY depth.
        kept, dropped = self._run(
            "what gadget balances the ballast",
            [self._dme("5.00.ccc", "the tide chart tool predicts windows", 1, "weak_hop1")],
        )
        assert kept == []
        assert len(dropped) == 1

    def test_weak_hop1_with_lexical_anchor_survives(self):
        kept, dropped = self._run(
            "which utility forecasts when the harbor is open",
            [
                self._dme(
                    "5.00.ccc", "the tide chart tool predicts the harbor windows", 1, "weak_hop1"
                )
            ],
        )
        assert len(kept) == 1 and not dropped

    def test_classless_hop1_keeps_hop_basis(self):
        # pre-split DME rows carry no edge class — the hop<=1 basis stands
        candidate = {
            "id": "5.00.old",
            "title": "",
            "summary": "no shared tokens here",
            "provenance": {"candidate_source": "dme", "dme_hop": 1},
        }
        kept, dropped = self._run("what gadget balances the ballast", [candidate])
        assert len(kept) == 1 and not dropped

    def test_weak_hop2_with_lexical_anchor_survives(self):
        kept, dropped = self._run(
            "where is the depth atlas",
            [
                self._dme(
                    "11.00.ddd", "the chart build produced a signed depth atlas", 2, "weak_hop2"
                )
            ],
        )
        assert len(kept) == 1 and not dropped

    def test_non_dme_candidates_are_lane_anchored(self):
        kept, dropped = self._run(
            "where is the depth atlas",
            [
                {
                    "id": "4.00.eee",
                    "title": "",
                    "summary": "unrelated walker hit",
                    "provenance": {"candidate_source": "walker"},
                }
            ],
        )
        assert len(kept) == 1 and not dropped

    def test_empty_candidates_stay_empty(self):
        kept, dropped = self._run("anything at all", [])
        assert kept == [] and dropped == []


class TestWidenLadder:
    """The remember tool's recall widen: scoped-Y → +_unsorted → whole-lobe."""

    @staticmethod
    def _memory_recorder(specs: List[Any], hit_on: str):
        """Return a memory callable that records QuerySpecs and yields a
        candidate only when the widen rung matches ``hit_on``."""

        def _verdict(candidates):
            class _V:
                status = "verified" if candidates else "empty"
                reason = "fake"
                answer_payload = {"candidates": candidates}

            return _V()

        def _memory(spec):
            specs.append(spec)
            subs = spec.allowed_subdomains
            rung = "lobe" if subs is None else "unsorted" if 127 in (subs or []) else "scoped"
            if rung == hit_on:
                return _verdict(
                    [
                        {
                            "coord": [4, 3, 0],
                            "memory_id": "4.03.zzz",
                            "score": 0.8,
                            "title": "t",
                            "summary": "the night dive succeeded",
                        }
                    ]
                )
            return _verdict([])

        return _memory

    def _invoke(self, memory, monkeypatch):
        from auto_loop.mril_tools import remember as rem

        return rem.invoke_remember_tool(
            {
                "tool": "remember_situation",
                "query": "what happened on the night dive",
                "allowed_lobes": [4],
                "memory_type": "situational",
                "lane": "situational",
                "lobe": 4,
                "subdomain": 6,
            },
            "ep-widen",
            memory=memory,
        )

    def test_whole_lobe_rung_recovers(self, monkeypatch):
        specs: List[Any] = []
        out = self._invoke(self._memory_recorder(specs, hit_on="lobe"), monkeypatch)
        assert len(specs) == 3, "ladder = scoped → +_unsorted → whole-lobe"
        assert specs[0].allowed_subdomains == [6]
        assert 127 in specs[1].allowed_subdomains
        assert specs[2].allowed_subdomains is None
        assert out["evidence"], "whole-lobe rung must recover the memory"
        assert out["widened_unsorted"] is True
        assert out["widened_lobe"] is True

    def test_unsorted_rung_stops_ladder(self, monkeypatch):
        specs: List[Any] = []
        out = self._invoke(self._memory_recorder(specs, hit_on="unsorted"), monkeypatch)
        assert len(specs) == 2
        assert out["evidence"]
        assert out["widened_unsorted"] is True
        assert out["widened_lobe"] is False

    def test_scoped_hit_never_widens(self, monkeypatch):
        specs: List[Any] = []
        out = self._invoke(self._memory_recorder(specs, hit_on="scoped"), monkeypatch)
        assert len(specs) == 1
        assert out["evidence"]
        assert out["widened_unsorted"] is False
        assert out["widened_lobe"] is False

    def test_explicit_contract_never_widens(self, monkeypatch):
        from auto_loop.mril_tools import remember as rem

        specs: List[Any] = []
        out = rem.invoke_remember_tool(
            {
                "tool": "remember_situation",
                "query": "what happened on the night dive",
                "allowed_lobes": [4],
                "memory_type": "situational",
                "lane": "situational",
                "lobe": 4,
                "allowed_subdomains": [6],
            },
            "ep-contract",
            memory=self._memory_recorder(specs, hit_on="lobe"),
        )
        assert len(specs) == 1, "explicit allowed_subdomains = exact contract"
        assert out["evidence"] == []
        assert out["widened_unsorted"] is False
        assert out["widened_lobe"] is False
