"""First-wake onboarding seed: occupation -> lobe, subcategories -> subdomains.

The operator's onboarding is the system's first reward signal. The occupation
routes the operator into the coding lobe (8) or the knowledge lobe (2); each
chosen subcategory becomes a freshly-created dynamic subdomain, and creating a
subdomain registers a mastery gap (target>0, actual=0) via
``memory.subdomain_allocator``.

Those gaps raise ``learning_debt``, but deterministic pressure-field readers never
own ignition (``pressure.gather_contributors`` reads only operator ingress and
Omega-authored successors), so the gaps alone never ignite. The mind-primacy
bootstrap is OPERATOR INTENT: onboarding ENQUEUES an operator-sourced LEARN
(``_ignite_onboarding_learn``) aimed at the first focus, which ignites the first
episode through the cognition queue like any operator input. This
is how the cold mind boots pointed at YOU instead of staying idle; the mind then
sustains the agenda from there.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Tuple

log = logging.getLogger("omega.hud.operator_onboarding")

# Onboarding trajectory establish. Module-constant idiom (toggle by direct edit),
# matching the pressure-evolution flags.
#
# The Omega contract states "the operator sets [the trajectory] (onboarding, then
# directives); you decompose". Without this flag, onboarding seeds only mastery GAPS,
# so the FIRST trajectory crystallizes lazily at the first Omega window-close — meaning
# the system's first ~10 episodes run auto-driven (learning_debt) with NO mind-set
# agenda. Flipping this ON makes the operator's own onboarding choices SET the initial
# trajectory immediately, so episodes are steered from episode 1; the mind then
# decomposes/steers it from window 1 onward (north-star: mind decides -> loop ignites).
OMEGA_ONBOARDING_ESTABLISH: bool = True

# (id, label, lobe). Coding occupations route to lobe 8 (800_coding); everything
# else routes to lobe 2 (200_knowledge). Edit freely — it is just data.
OCCUPATIONS: Tuple[Tuple[str, str, int], ...] = (
    ("software_engineer", "Software Engineer", 8),
    ("data_scientist", "Data Scientist / ML Engineer", 8),
    ("devops_sre", "DevOps / SRE", 8),
    ("web_developer", "Web Developer", 8),
    ("security_engineer", "Security Engineer", 8),
    ("game_developer", "Game Developer", 8),
    ("healthcare", "Doctor / Healthcare", 2),
    ("lawyer", "Lawyer", 2),
    ("educator", "Educator", 2),
    ("writer", "Writer / Journalist", 2),
    ("designer", "Designer", 2),
    ("researcher", "Researcher / Academic", 2),
    ("business", "Business / Finance", 2),
    ("marketing", "Marketing", 2),
    ("student", "Student", 2),
    ("other", "Other", 2),
)

# Suggested subcategories per occupation id (front-end hints; free-text allowed).
OCCUPATION_SUBCATEGORY_HINTS: Dict[str, Tuple[str, ...]] = {
    "software_engineer": ("Python", "System Design", "Distributed Systems", "Algorithms"),
    "data_scientist": ("Machine Learning", "Statistics", "Deep Learning", "Data Pipelines"),
    "devops_sre": ("Kubernetes", "CI/CD", "Observability", "Cloud Infrastructure"),
    "web_developer": ("JavaScript", "React", "APIs", "Accessibility"),
    "security_engineer": ("AppSec", "Cryptography", "Threat Modeling", "Incident Response"),
    "game_developer": ("Game Engines", "Graphics", "Physics", "Gameplay Systems"),
    "healthcare": ("Cardiology", "Pharmacology", "Diagnostics", "Patient Care"),
    "lawyer": ("Contract Law", "Litigation", "Compliance", "IP Law"),
    "educator": ("Curriculum Design", "Pedagogy", "Assessment", "EdTech"),
    "writer": ("Narrative Craft", "Editing", "Research", "Interviewing"),
    "designer": ("UX Research", "Visual Design", "Typography", "Design Systems"),
    "researcher": ("Methodology", "Literature Review", "Statistics", "Grant Writing"),
    "business": ("Financial Modeling", "Strategy", "Operations", "Accounting"),
    "marketing": ("SEO", "Content Strategy", "Analytics", "Brand"),
    "student": ("Study Skills", "Note Taking", "Exam Prep"),
    "other": (),
}

_OCC_BY_ID: Dict[str, int] = {oid: lobe for oid, _label, lobe in OCCUPATIONS}
_OCC_BY_LABEL: Dict[str, int] = {label.strip().lower(): lobe for _oid, label, lobe in OCCUPATIONS}


def lobe_for_occupation(occupation: str) -> int:
    """Route an occupation to its lobe. Coding -> 8; everything else -> 2
    (the operator's 'rest through knowledge' rule, incl. free-text 'Other')."""
    raw = (occupation or "").strip()
    if not raw:
        return 2
    by_id = raw.lower().replace(" ", "_").replace("/", "_")
    if by_id in _OCC_BY_ID:
        return _OCC_BY_ID[by_id]
    if raw.lower() in _OCC_BY_LABEL:
        return _OCC_BY_LABEL[raw.lower()]
    return 2


def occupation_options() -> List[Dict[str, Any]]:
    """Front-end payload: the occupation dropdown + per-occupation subcategory hints."""
    return [
        {
            "id": oid,
            "label": label,
            "lobe": lobe,
            "domain": "coding" if lobe == 8 else "knowledge",
            "subcategory_hints": list(OCCUPATION_SUBCATEGORY_HINTS.get(oid, ())),
        }
        for oid, label, lobe in OCCUPATIONS
    ]


def seed_first_wake(
    occupation: str,
    subcategories: List[str],
    *,
    source_citation: Mapping[str, Any] | None = None,
    operator_id: str = "operator",
) -> Dict[str, Any]:
    """Enable dynamic-Y for lobes 2/8, then seed the operator's first subdomains.

    Each new subdomain registers a mastery gap (target>0, actual=0) -> raises
    learning_debt -> ignites the first LEARN. Failure-soft per subcategory: one
    bad title must not abort the others, but it is logged + surfaced.
    """
    from memory.subdomain_allocator import get_or_create_subdomain
    from memory.subdomain_flag import set_dynamic_y_lobes

    flag = set_dynamic_y_lobes([2, 8])
    occupation_lobe = lobe_for_occupation(occupation)

    seeded: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in subcategories or []:
        title = str(raw or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        # PER-TOPIC coding-vs-knowledge routing (operator model): classify EACH
        # subcategory on its OWN merits so a data-scientist's "Statistics" (knowledge)
        # and "Python" (coding) each land in the PROPER lobe — the occupation NEVER
        # forces the lobe. Only a concrete language/framework/tool -> coding lobe 8;
        # everything else (concepts, theory, domains) -> knowledge lobe 2 (the default).
        # So a Software Engineer's "Algorithms"/"System Design" are knowledge, not coding.
        lobe = _route_subcat_lobe(title)
        try:
            y, dirname = get_or_create_subdomain(
                lobe=lobe, seed_text=title, title=title, dynamic_y_lobes=flag
            )
            memory_id = _write_work_topic_leaf(
                topic=title,
                occupation=occupation,
                lobe=lobe,
                y=y,
                operator_id=operator_id,
                source_citation=source_citation,
            )
            seeded.append(
                {
                    "subcategory": title,
                    "lobe": lobe,
                    "y": y,
                    "dirname": dirname,
                    "memory_id": memory_id,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("onboarding seed failed for %r: %s", title, exc)
            failed.append({"subcategory": title, "error": str(exc)})

    result = {"occupation": occupation, "lobe": occupation_lobe, "seeded": seeded, "failed": failed}

    # The operator SETS the trajectory at onboarding (flag-gated; OFF -> byte-identical).
    # Establish the initial setpoint from the operator's own choices so episodes are
    # steered from episode 1 instead of running auto-driven until the first Omega window
    # closes. Writes via the SAME store the Omega window reads, so window-1 then sees an
    # existing setpoint and ASSESSES it (idle/alter) rather than force-establishing.
    # No-clobber (never overwrite an existing trajectory) + failure-soft (a trajectory
    # hiccup must never break gap-seeding, the critical ignition path).
    if OMEGA_ONBOARDING_ESTABLISH:
        try:
            from pressure_loop.temporal_store import read_trajectory, write_trajectory

            if not read_trajectory():  # one active trajectory; onboarding never clobbers
                traj = build_onboarding_trajectory(occupation, seeded)
                if traj is not None:
                    write_trajectory(traj)  # durable setpoint (lobe 9)
                    # Ignition reads the LAST_OMEGA_TRAJECTORY shared-state slot, not lobe
                    # 9; that slot is only hydrated from lobe 9 at boot, and onboarding runs
                    # after boot -- so publish the live signal too, or episode 1 wouldn't
                    # see the trajectory until the next boot.
                    live = _publish_onboarding_trajectory_signal(traj)
                    result["trajectory"] = traj
                    result["trajectory_live_signal"] = live
        except Exception as exc:  # noqa: BLE001
            log.warning("onboarding trajectory establish failed: %s", exc)

        # BOOTSTRAP THE FIRST LEARN independently of trajectory creation. A prior
        # trajectory must never be clobbered, but it also must not swallow this one-shot
        # onboarding request and its occupation-expansion foldback. This is explicit
        # operator intent, so it remains valid while autonomous *idle* learning is paused.
        result["learn_ignition"] = _ignite_onboarding_learn(
            seeded,
            occupation=occupation,
            source_citation=source_citation,
            operator_id=operator_id,
        )

    return result


def build_onboarding_trajectory(
    occupation: str, seeded: List[Dict[str, Any]]
) -> Dict[str, Any] | None:
    """Build the operator's initial Omega trajectory from their onboarding choices.

    The first seeded subcategory becomes the concrete ``target_domain`` (normalized to
    match the mastery-gap key the SAME subcategory created), the occupation becomes the
    broad ``target_capability``. Returns the trajectory dict, or ``None`` when there is
    no learnable subcategory to anchor on. Pure (no I/O) — the caller persists it.
    """
    from deepsleep_loop.mastery_store import normalize_domain_id
    from schema.omega_trajectory import make_trajectory

    first = next(
        (
            str((r or {}).get("subcategory") or "").strip()
            for r in (seeded or [])
            if str((r or {}).get("subcategory") or "").strip()
        ),
        "",
    )
    if not first:
        return None
    target_domain = normalize_domain_id(first)
    if not target_domain:
        return None
    target_capability = normalize_domain_id(str(occupation or "").strip()) or "general"
    traj = make_trajectory(
        cycle_id="onboarding",
        epoch_emitted=0,
        intent="explore_domain",
        target_domain=target_domain,
        target_capability=target_capability,
        rationale=f"operator onboarding: {occupation} -> first focus {first}",
    )
    return traj.to_dict()


def _publish_onboarding_trajectory_signal(traj: Dict[str, Any]) -> bool:
    """Publish the onboarding trajectory to the LAST_OMEGA_TRAJECTORY live slot so the
    FIRST episode's ignition sees it this session.

    The durable lobe-9 write is the source of truth; this is the live signal pressure
    ignition actually reads. Best-effort: with no registered runtime (tests / headless)
    the publish skips and the signal simply defers to the next boot's slot hydration.
    Returns True iff the slot was written.
    """
    try:
        from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
        from pressure_loop.runtime import try_get_default_runtime

        rt = try_get_default_runtime()
        if rt is None:
            return False
        rt.shared_state.write_canonical(LAST_OMEGA_TRAJECTORY, traj)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("onboarding trajectory live-signal publish skipped: %s", exc)
        return False


def _ignite_onboarding_learn(
    seeded: List[Dict[str, Any]],
    *,
    occupation: str,
    source_citation: Mapping[str, Any] | None = None,
    operator_id: str = "operator",
) -> bool:
    """Enqueue ONE operator-intent LEARN for the operator's first onboarded focus.

    This is the bootstrap that makes onboarding CREATE LEARNING. The seeded subdomains
    raise ``learning_debt``, but deterministic pressure-field readers never own ignition
    (``pressure.gather_contributors`` reads only operator ingress and Omega-authored
    successors), so the gaps alone never ignite: the mind-primacy bootstrap is OPERATOR
    INTENT. An operator-SOURCED contributor rides the ignition path; ``directive_intent=LEARN``
    routes Global A straight to LEARN (the directive rail, no word inference). The mind
    sustains the agenda from there. Failure-soft (a queue hiccup must never break the
    seeded gaps — they are the durable record). Returns True iff an input was enqueued.
    """
    first = next(
        (
            str((r or {}).get("subcategory") or "").strip()
            for r in (seeded or [])
            if str((r or {}).get("subcategory") or "").strip()
        ),
        "",
    )
    if not first:
        return False
    try:
        from pressure_loop.contributors import from_operator_message
        from pressure_loop.cognition_queue import enqueue_input

        categories = [
            {
                "title": str(row.get("subcategory") or "").strip(),
                "lobe": int(row.get("lobe")),
                "y": int(row.get("y")),
                "dirname": str(row.get("dirname") or ""),
                "memory_id": str(row.get("memory_id") or ""),
            }
            for row in seeded
            if str(row.get("subcategory") or "").strip()
        ]
        contributor = from_operator_message(
            {
                "message": f"Learn {first}",
                "directive_intent": "LEARN",
                "weight": 1.0,
                "origin": "onboarding_bootstrap",
                "cycle_id": f"onboarding-bootstrap-{operator_id}-{first}",
                "onboarding_context": {
                    "occupation": str(occupation or "").strip(),
                    "operator_id": str(operator_id or "operator"),
                    "categories": categories,
                    "source_citation": dict(source_citation or {}),
                },
            }
        )
        return enqueue_input(contributor, operator_activity=True)
    except Exception as exc:  # noqa: BLE001
        log.info("onboarding learn-ignition skipped: %s", exc)
        return False


def _route_subcat_lobe(title: str) -> int:
    """Per-topic coding(8)-vs-knowledge(2) routing for an onboarding/learned subcat —
    deterministic (the classifier is RETIRED). Judges the TOPIC, not the occupation: a
    concrete programming language / framework / runtime / dev-or-infra tool -> coding(8);
    everything else (concepts, theory, methods, non-software domains) -> knowledge(2),
    which is the DEFAULT. Smart-on-ingest discrimination lives in one place
    (``memory.routing.coding_vs_knowledge``) so every ingest path shares it. Failure-soft
    -> knowledge (a router hiccup must never force a topic into coding)."""
    try:
        from memory.routing.coding_vs_knowledge import classify_topic_lobe

        return classify_topic_lobe(title)
    except Exception:
        return 2


def _citation_fields(source_citation: Mapping[str, Any] | None) -> Dict[str, Any]:
    keys = (
        "source_id",
        "source_type",
        "span_start",
        "span_end",
        "span_hash",
        "source_content_hash",
        "source_session_id",
        "source_session_timestamp",
    )
    return {
        key: source_citation[key]
        for key in keys
        if source_citation and source_citation.get(key) is not None
    }


def _tag_slug(value: str) -> str:
    from deepsleep_loop.mastery_store import normalize_domain_id

    return normalize_domain_id(str(value or "").strip()) or "general"


def _write_work_topic_leaf(
    *,
    topic: str,
    occupation: str,
    lobe: int,
    y: int,
    operator_id: str,
    source_citation: Mapping[str, Any] | None,
    parent: str | None = None,
) -> str:
    """Write the canonical topic root that ties a mastery gap to the operator."""
    from memory.api import CanonicalEnvelope, intake_memory
    from memory.deterministic import stable_hash

    topic = str(topic or "").strip()
    occupation = str(occupation or "").strip() or "unspecified occupation"
    operator_id = str(operator_id or "operator").strip() or "operator"
    parent = str(parent or "").strip() or None
    occupation_tag = _tag_slug(occupation)
    topic_tag = _tag_slug(topic)
    tags = [
        "operator_work_topic",
        "onboarding",
        f"operator:{operator_id}",
        f"occupation:{occupation_tag}",
        f"work_topic:{topic_tag}",
    ]
    if parent:
        tags.append(f"parent_work_topic:{_tag_slug(parent)}")
    source_fields = _citation_fields(source_citation)
    memory_key = stable_hash(
        "operator_work_topic",
        operator_id,
        occupation,
        parent or "",
        topic,
        length=12,
    )
    sentence = (
        f"{topic} is a child work-learning topic under {parent} for the operator's "
        f"occupation, {occupation}."
        if parent
        else f"{topic} is a work-learning topic for the operator's occupation, {occupation}."
    )
    envelope = CanonicalEnvelope(
        cycle_id=f"onboarding-work-topic-{memory_key}",
        coord=[int(lobe), int(y), 0],
        type="operator_work_topic",
        payload={
            "title": f"Operator work topic: {topic}"[:120],
            "summary": sentence[:500],
            "text": sentence,
            "tags": tags,
            # Domain participates in the on-disk tier path; keep the human label
            # in enrichment/summary and use a filesystem-safe canonical key here.
            "domain": occupation_tag,
            "subtopic": topic,
            "private": False,
            "linked_persons": [operator_id],
            "enrichment": {
                # The taxonomy capture reads payload.linked_persons, while the
                # post-commit entity linker reads enrichment.linked_persons. Stamp
                # both projections so this leaf is actually connected to the
                # operator persona instead of being related by tags alone.
                "linked_persons": [operator_id],
                "operator_work_topic": {
                    "operator_id": operator_id,
                    "occupation": occupation,
                    "topic": topic,
                    "parent": parent,
                    "lobe": int(lobe),
                    "subdomain_y": int(y),
                },
            },
            **source_fields,
        },
        source="hud.operator_onboarding",
        confidence=0.95,
        provenance=[
            "hud.operator_onboarding.work_topic",
            f"operator:{operator_id}",
            f"occupation:{occupation_tag}",
            *(
                [f"source_id:{source_citation['source_id']}"]
                if source_citation and source_citation.get("source_id")
                else []
            ),
        ],
        id=f"{int(lobe)}.{int(y):02d}.operator-work-topic.{memory_key}",
        strength_index=0.8,
    )
    receipt = intake_memory(
        envelope,
        propose=False,
        bypass_guardian=True,
        skip_embed=True,
    )
    if receipt.validation_status == "rejected":
        raise RuntimeError(f"work-topic memory rejected for {topic}: {receipt.message}")
    return str(receipt.path or envelope.id or memory_key)


def seed_foldback_subtopics(
    onboarding_context: Mapping[str, Any],
    planned_rows: Any,
) -> List[Dict[str, Any]]:
    """Materialize Omega's validated onboarding plan as child mastery gaps/leaves.

    The model only proposes child labels at foldback. This host path enforces the
    original parent/lobe mapping, allocates each child beneath that parent, and writes
    operator-linked canonical topic roots. Those fresh gaps are visible to the next
    pressure episode.
    """
    if not isinstance(onboarding_context, Mapping):
        return []
    occupation = str(onboarding_context.get("occupation") or "").strip()
    operator_id = str(onboarding_context.get("operator_id") or "operator").strip()
    source_citation = (
        dict(onboarding_context.get("source_citation") or {})
        if isinstance(onboarding_context.get("source_citation"), Mapping)
        else {}
    )
    categories: Dict[str, Dict[str, Any]] = {}
    for raw in onboarding_context.get("categories") or ():
        if not isinstance(raw, Mapping):
            continue
        title = str(raw.get("title") or raw.get("subcategory") or "").strip()
        try:
            lobe = int(raw.get("lobe"))
        except (TypeError, ValueError):
            continue
        if title and lobe in (2, 8):
            categories[title.casefold()] = {"title": title, "lobe": lobe}

    from memory.subdomain_allocator import get_or_create_subdomain
    from memory.subdomain_flag import set_dynamic_y_lobes

    flag = set_dynamic_y_lobes([2, 8])
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for row in planned_rows or ():
        if not isinstance(row, Mapping):
            continue
        parent_raw = str(row.get("parent") or "").strip()
        parent = categories.get(parent_raw.casefold())
        if parent is None:
            continue
        for raw_topic in row.get("subtopics") or ():
            topic = str(raw_topic or "").strip()
            key = (parent["title"].casefold(), topic.casefold())
            if not topic or topic.casefold() == parent["title"].casefold() or key in seen:
                continue
            seen.add(key)
            y, dirname = get_or_create_subdomain(
                lobe=parent["lobe"],
                seed_text=topic,
                title=topic,
                dynamic_y_lobes=flag,
                parent=parent["title"],
            )
            memory_id = _write_work_topic_leaf(
                topic=topic,
                occupation=occupation,
                lobe=parent["lobe"],
                y=y,
                parent=parent["title"],
                operator_id=operator_id,
                source_citation=source_citation,
            )
            out.append(
                {
                    "subtopic": topic,
                    "parent": parent["title"],
                    "lobe": parent["lobe"],
                    "y": y,
                    "dirname": dirname,
                    "memory_id": memory_id,
                }
            )
    return out
