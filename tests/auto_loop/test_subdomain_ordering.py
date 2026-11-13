"""Fix 4 — ordering corrections in subdomain_for (RED → GREEN).

(a) A possessive first-person pronoun ("my"/"me") must not capture a sentence
    whose SUBJECT is third-person ("he is my father" → person_stub, not operator).
    Third-person is checked before first-person in identity + personas.
(b) Term-match: a bare subdomain name ("concurrency", "performance") must win
    over a composite subdomain ("go_concurrency", "cpp_performance") that only
    matched via a shared part-word — an exact full-name match beats a part match.
"""

from __future__ import annotations

from auto_loop.mril_tools.subdomain_helpers import subdomain_for
from memory.dewey_map import SUBDOMAIN_MAP
from memory.identity.subdomains import TRAITS_Y


def _y(lobe: int, name: str) -> int:
    return next(y for (x, y), n in SUBDOMAIN_MAP.items() if x == lobe and n == name)


# ── (a) possessive must not capture a third-person subject ───────────────
def test_persona_third_person_with_possessive_my():
    assert subdomain_for("he is my father and lives in leeds", 1) == _y(1, "person_stub")


def test_persona_third_person_mentor_me():
    assert subdomain_for("they mentor me on distributed systems", 1) == _y(1, "person_stub")


def test_identity_third_person_with_possessive():
    assert subdomain_for("she is my sister and visits on weekends", 0) == TRAITS_Y


# ── (a) REGRESSION: pure first-person still routes to operator/constitution
def test_persona_first_person_operator_kept():
    assert subdomain_for("i prefer terse direct answers", 1) == _y(1, "operator")


def test_identity_first_person_constitution_kept():
    assert subdomain_for("i am noah the operator", 0) == _y(0, "constitution")


def test_identity_second_person_still_omega():
    assert subdomain_for("what is your identity", 0) == _y(0, "omega")


# ── (b) bare term beats a composite that only shares a part-word ─────────
def test_termmatch_bare_concurrency_not_go_concurrency():
    assert subdomain_for("this note records key facts about concurrency", 8) == _y(8, "concurrency")


def test_termmatch_bare_performance_not_cpp_performance():
    assert subdomain_for("a reference entry on performance and its ideas", 8) == _y(
        8, "performance"
    )


# ── (b) REGRESSION: a composite full-name still wins when actually present
def test_termmatch_go_concurrency_kept():
    assert subdomain_for("notes about go concurrency in services", 8) == _y(8, "go_concurrency")


def test_termmatch_cpp_performance_kept():
    assert subdomain_for("notes about cpp performance tuning", 8) == _y(8, "cpp_performance")


def test_termmatch_python_async_exact_kept():
    assert subdomain_for("python async", 8) == _y(8, "python_async")


def test_termmatch_quantum_physics_kept():
    assert subdomain_for("quantum physics of entanglement", 2) == _y(2, "quantum_physics")
