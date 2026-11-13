"""Operator-phrased records must be admitted when the query names the operator.

Live finding 2026-07-31 (boot #1): "What do you remember about Noah." retrieved
13 persona records and admitted 4. The 4 survivors were exactly the records
containing the literal token "Noah"; the 8 dropped all say "operator" instead
("operator's birthday is ...", "operator lives in ...", "operator works as ...").

``_partition_responsive`` gates on lexical content-token overlap, and
``content_tokens("What do you remember about Noah.")`` is ``{noah, remember}``.
An "operator's ..." record shares no token with that, so ``anchored`` is False
and the record is blocked before the question-form gate is ever consulted
(measured: ``carries_form_payload('', text)`` is True for every one of them --
the form gate was never the blocker).

Omega HELD the binding the whole time: ``1.00.operator_name`` says "when a
memory refers to 'the operator' ... it means Noah Lee". It was recalled and
used as evidence, never as an admission rule. ``memory.persona_ingest`` already
resolves operator aliases on the WRITE side; this is the read-side half of that
symmetry.
"""

from __future__ import annotations

import pytest

from auto_loop.mril_tools import remember as mril


@pytest.fixture()
def operator_named(monkeypatch):
    """The canonical operator-name anchor resolves to Noah Lee."""
    monkeypatch.setattr(mril, "_resolve_operator_display_name", lambda: "Noah Lee")


def _c(text: str) -> dict:
    return {"text": text, "summary": "", "title": ""}


OPERATOR_PHRASED = [
    _c("operator's birthday is September 03, 1985."),
    _c("operator lives in DeRidder."),
    _c("operator works as software_engineer."),
    _c("operator's pronouns are He, Him."),
    _c("operator's relationship to Omega is creator."),
    _c("operator's timezone is Central."),
]


def test_operator_phrased_records_admitted_when_query_names_the_operator(operator_named):
    responsive, blocked = mril._partition_responsive(
        "What do you remember about Noah.", list(OPERATOR_PHRASED)
    )
    assert blocked == [], f"{len(blocked)} operator-phrased records still dropped"
    assert len(responsive) == len(OPERATOR_PHRASED)


def test_name_phrased_records_admitted_when_query_says_operator(operator_named):
    """The bridge runs both ways."""
    candidates = [
        _c("Noah Lee's son is Abel."),
        _c("operator's name is Noah Lee."),
    ]
    responsive, blocked = mril._partition_responsive(
        "What do you remember about the operator.", candidates
    )
    assert blocked == []
    assert len(responsive) == 2


def test_the_exact_live_query_admits_every_operator_record(operator_named):
    """Verbatim reproduction of the 4-of-13 turn."""
    candidates = [
        _c("Operator name: Noah Lee: operator's name is Noah Lee."),
        _c("Operator - Noah Lee: The operator is Noah Lee."),
        _c("Abel - son of Noah Lee: Abel is Noah Lee's son - the operator's son."),
        *OPERATOR_PHRASED,
    ]
    responsive, blocked = mril._partition_responsive(
        "What do you remember about Noah.", candidates
    )
    assert blocked == []
    assert len(responsive) == 9


def test_alias_bridge_does_not_admit_unrelated_third_parties(operator_named):
    """Widening for the operator must not turn admission into a pass-through."""
    candidates = [
        _c("Mitchell Ray is part owner of Omega AI LLC."),
        _c("Pytest is a Python testing framework."),
    ]
    responsive, _ = mril._partition_responsive(
        "What do you remember about Noah.", candidates
    )
    assert responsive == []


def test_no_operator_name_configured_changes_nothing(monkeypatch):
    """Before onboarding names the operator, behaviour is exactly as before."""
    monkeypatch.setattr(mril, "_resolve_operator_display_name", lambda: None)
    responsive, blocked = mril._partition_responsive(
        "What do you remember about Noah.", list(OPERATOR_PHRASED)
    )
    assert responsive == []
    assert len(blocked) == len(OPERATOR_PHRASED)


def test_resolver_failure_never_breaks_admission(monkeypatch):
    """Alias resolution is an enrichment; it must fail soft, not raise."""

    def _boom():
        raise RuntimeError("coord index unavailable")

    monkeypatch.setattr(mril, "_resolve_operator_display_name", _boom)
    responsive, blocked = mril._partition_responsive(
        "What do you remember about Noah.", [_c("operator lives in DeRidder.")]
    )
    assert len(responsive) + len(blocked) == 1
