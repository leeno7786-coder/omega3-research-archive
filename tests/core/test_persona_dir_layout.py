"""Non-operator personas live BESIDE the operator, not nested under a type.

Operator ruling 2026-07-31, from the live tree:

    memory/100_personas/
      operator/            <- domain "operator", subtopics directly beneath
      person_stub/         <- the (1,1) SUBDOMAIN NAME used as a directory tier
        abel/
          persona_stub/
        mitchell_ray/
          persona_stub/

"right here it should have abel and mitchell ... not persona stub".

Two shapes were in play: the operator got ``<lobe>/<domain>/<subtopic>/`` while
everyone else got ``<lobe>/<subdomain>/<person>/<subtopic>/``, because
``_ensure_persona_dir`` inserted ``SUBDOMAIN_MAP[(1,1)]`` ("person_stub") above
the person. The record TYPE was occupying the tier the person belongs in.

The Dewey coordinate is unaffected: ``route_persona`` still returns (1, 1,
person_id), and persona_router's own docstring is explicit that person_id is
"the SEMANTIC slug used as the 3rd-tier dir name and the ``domain`` envelope
field ... NOT a schema coord component". Only the on-disk tier moves.
"""

from __future__ import annotations

from pathlib import Path

import core.persona_router as pr


def test_person_directory_sits_directly_under_the_personas_lobe(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    path = pr._ensure_persona_dir("mitchell_ray")

    assert path == tmp_path / "memory" / "100_personas" / "mitchell_ray"
    assert path.is_dir()
    assert "person_stub" not in path.parts, f"type used as a directory tier: {path}"


def test_person_is_a_sibling_of_the_operator(tmp_path, monkeypatch):
    """The whole point: `ls 100_personas` shows operator, abel, mitchell_ray."""
    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    lobe = tmp_path / "memory" / "100_personas"
    (lobe / "operator").mkdir(parents=True, exist_ok=True)

    pr._ensure_persona_dir("abel")
    pr._ensure_persona_dir("mitchell_ray")

    assert {p.name for p in lobe.iterdir() if p.is_dir()} == {
        "operator",
        "abel",
        "mitchell_ray",
    }


def test_the_dewey_coordinate_is_unchanged(tmp_path, monkeypatch):
    """Layout moved; the coord contract did not."""
    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    assert (pr._LOBE, pr._SUBDOMAIN) == (1, 1)


def test_directory_is_reused_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    first = pr._ensure_persona_dir("mitchell_ray")
    second = pr._ensure_persona_dir("mitchell_ray")
    assert first == second


def test_person_id_cannot_collide_with_the_operator_domain(tmp_path, monkeypatch):
    """A person slugged "operator" would land on the operator's own tree.

    Promoting person_id to the tier the operator's domain occupies makes that
    collision reachable, so it must be refused rather than silently merged."""
    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    try:
        path = pr._ensure_persona_dir("operator")
    except ValueError:
        return
    assert path != tmp_path / "memory" / "100_personas" / "operator", (
        "a person named 'operator' silently took over the operator's namespace"
    )
