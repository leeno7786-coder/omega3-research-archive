"""A knowledge subdomain is named for the WHOLE topic Omega sought.

Live 2026-07-31: Omega autonomously worked down a machine-learning curriculum
and the on-disk taxonomy came out as single words --

    memory/200_knowledge/
      machine/      <- "Machine Learning"                        (Y=0)
      statistics/   <- "Statistics"                              (Y=1)
      deep/         <- "Deep Learning"                           (Y=2)
      statistical/  <- "Statistical Algorithms for ML"           (Y=5)
      activation/   <- "Activation Functions and Backpropagation"(Y=12)

``_derive_name`` took ``toks[0]`` and discarded the rest of the name. Same
defect class as "University of Phoenix" -> "phoenix": a name reduced to a
fragment.

Costs: the subdomain name is a lexical retrieval target, so "deep learning"
had to match a directory called ``deep``; related material scattered
(``machine`` and ``deep`` look like unrelated siblings when one is a subfield
of the other); and near-duplicates split (``statistical`` vs ``statistics``).

Operator ruling: "we need to adjust that so it stores as exactly what omega
sought."
"""

from __future__ import annotations

from memory.knowledge_router import _derive_name

MAX_DIRNAME = 64


def _name(title: str) -> str:
    return _derive_name("", title)


def test_multiword_topic_keeps_its_whole_name():
    assert _name("Machine Learning") == "machine_learning"
    assert _name("Deep Learning") == "deep_learning"


def test_the_live_taxonomy_stops_collapsing_to_head_tokens():
    """The exact titles Omega allocated tonight."""
    assert _name("Statistical Algorithms for ML") == "statistical_algorithms"
    assert _name("Activation Functions and Backpropagation") == (
        "activation_functions_backpropagation"
    )


def test_subfields_no_longer_look_like_unrelated_siblings():
    """'machine' vs 'deep' hid that one is a subfield of the other."""
    ml = _name("Machine Learning")
    dl = _name("Deep Learning")
    assert ml != dl
    assert "learning" in ml and "learning" in dl


def test_near_duplicates_stay_distinguishable():
    assert _name("Statistics") != _name("Statistical Algorithms for ML")


def test_the_topic_omega_is_learning_right_now():
    got = _name("Types of Machine Learning (Supervised, Unsupervised, Reinforcement)")
    assert "machine_learning" in got, got
    assert len(got) <= MAX_DIRNAME


def test_dirname_is_stable_across_calls():
    """Routing depends on the same title always resolving to the same dir."""
    title = "Activation Functions and Backpropagation"
    assert _name(title) == _name(title)


def test_dirname_is_filesystem_safe_and_bounded():
    got = _name(
        "Distributed Consensus Algorithms such as Paxos and Raft in Large Scale "
        "Fault Tolerant Production Systems Everywhere"
    )
    assert len(got) <= MAX_DIRNAME
    assert got == got.strip("_")
    assert all(ch.isalnum() or ch == "_" for ch in got)


def test_title_still_beats_body_and_general_is_still_the_floor():
    assert _name("") == "general"
    assert _derive_name("neural networks are layered models", None).startswith("neural")
    assert _derive_name("body text here", "Machine Learning") == "machine_learning"
