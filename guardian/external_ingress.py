"""Guardian external-ingress screen — untrusted human-authored connector content.

The ratified boundary (operator 2026-07-17, AGENTS.md banner + WORKSPACE_MEMORY_LOOP_
INTEGRATION.md §7): *provider authority proves what exists externally; it does not make
human-authored content trustworthy.* Calendar titles/descriptions, task notes, email
subjects/snippets, filenames — anyone who can land text in front of the operator's
accounts authors them, so every such field passes THIS screen before it may enter a
prompt, an episode, or (phase 2) canonical projection.

Two detection layers, one verdict:
  1. **Prompt-injection heuristics** (this module) — instruction-override phrasing,
     role/fence markers, exfiltration asks. External-ingress-specific: chat ingress
     rarely needs these because the operator is talking; a calendar invite from a
     stranger is a delivery vehicle.
  2. **guardian.memory_purity** — the standing anti-contamination shield (identity
     rewrite / emotional imprint / ego merge).

Verdicts collapse to the doctrine triple: ALLOW (text passes), FLAG (text passes,
logged), QUARANTINE (text replaced by the marker — the content is preserved only at
the source artifact, never in prompts/episodes). The screen TAINTS BY DEFAULT
(round-4): every external string is screened UNLESS its field is a known provider
STRUCTURAL field AND the value matches that field's shape (token/timestamp/URL — a
"structural" field carrying free text is human-authored content wearing a metadata
name). ALLOW never means trusted: composition must still present external strings
attributed and harden()-fenced.

Fail-soft: a screen crash must never take down a scan or a receipt — an unscreenable
field quarantines (fail CLOSED on the content, fail OPEN on the system).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, Mapping, Tuple

from memory.taxonomy.normalize import canonical_json

log = logging.getLogger("omega.guardian.external_ingress")

QUARANTINE_MARKER = "[quarantined by guardian]"


class ExternalIngressScreenError(RuntimeError):
    """Guardian screening failed internally; distinct from a quarantine verdict."""

# KNOWN human-authored receipt fields — documentation + the crash-fallback drop set.
# NOT the gate (round-4: trusting field names let `payload.data` and raw `value` pass):
# the gate is default-untrusted — every string that is not a shape-valid STRUCTURAL
# field gets screened, whatever its key is called.
HUMAN_AUTHORED_FIELDS = frozenset(
    {
        "summary",
        "title",
        "subject",
        "description",
        "snippet",
        "name",
        "from",
        "notes",
        "body",
        "text",
        "content",
    }
)

# Provider STRUCTURAL fields: the ONLY strings that may skip the screen, and only
# when the VALUE matches the field's shape. Shapes admit no whitespace, so they cannot
# carry an instruction sentence; a "structural" key holding free text is screened like
# any human field.
_TOKEN_RE = re.compile(r'"?[A-Za-z0-9_@.:+/=,\-]{0,255}"?\Z')
_URL_RE = re.compile(r"https?://[^\s<>\"']{1,500}\Z")
PROVIDER_STRUCTURAL_FIELDS: Dict[str, "re.Pattern[str]"] = {
    "id": _TOKEN_RE,
    "kind": _TOKEN_RE,
    "etag": _TOKEN_RE,
    "updated": _TOKEN_RE,
    "created": _TOKEN_RE,
    "status": _TOKEN_RE,
    "start": _TOKEN_RE,
    "end": _TOKEN_RE,
    "due": _TOKEN_RE,
    "count": _TOKEN_RE,
    "thread_id": _TOKEN_RE,
    "message_id": _TOKEN_RE,
    "history_id": _TOKEN_RE,
    "draft_id": _TOKEN_RE,
    "html_link": _URL_RE,
    "web_view_link": _URL_RE,
    "link": _URL_RE,
    "self_link": _URL_RE,
}

# Injection heuristics. Regex alone is a losing game against paraphrase (adversarial
# review 2026-07-17 proved three bypasses in one probe) — so the set is BROADER than a
# literal-phrase list: instruction-nullification, role/authority assumption, exfil verbs
# against sensitive objects, epistemic-override ("record as authoritative"), and
# compliance demands. memory_purity runs behind it, and the composition layer wraps
# every allowed field in harden() fences — defense in depth, not a single regex wall.
_INJECTION_PATTERNS = [
    # instruction override / nullification
    (r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions", "inj_override"),
    (
        r"disregard\s+(?:your|the|all|any)\s+(?:instructions|rules|prompt|guidelines)",
        "inj_override",
    ),
    (r"\bnew\s+instructions?\s*:", "inj_override"),
    (
        r"\b(?:treat|consider|regard)\b.{0,60}\b(?:rules?|instructions?|guidelines?|constraints?)\b.{0,40}\b(?:obsolete|void|invalid|null|no\s+longer|outdated|suspended)\b",
        "inj_override",
    ),
    (
        r"\boverride\s+(?:your|the|all)\s+(?:rules?|settings?|instructions?|guardian|safety)\b",
        "inj_override",
    ),
    (r"\byou\s+must\s+(?:obey|comply|follow)\b", "inj_override"),
    # role / authority assumption
    (r"\bsystem\s*prompt\b", "inj_system_probe"),
    (r"</?\s*(?:system|assistant|instructions?|prompt)\s*>", "inj_fence"),
    (r"\byou\s+are\s+now\s+(?:a|an|in)\b", "inj_role_switch"),
    (
        r"\bact\s+as\s+(?:the\s+)?(?:an?\s+)?(?:admin(?:istrator)?|root|system|owner|developer|superuser)\b",
        "inj_role_switch",
    ),
    (
        r"\b(?:i\s+am|this\s+is)\s+(?:the\s+)?(?:admin(?:istrator)?|your\s+(?:developer|creator|owner))\b",
        "inj_role_switch",
    ),
    # exfiltration / destructive verbs against sensitive objects
    (
        r"\b(?:upload|forward|send|exfiltrate|copy|export|transmit|share)\b.{0,50}\b(?:memory|memories|documents?|files?|passwords?|credentials?|secrets?|tokens?|contents?\s+of|everything)\b",
        "inj_exfil",
    ),
    (r"\breveal\s+(?:your|the)\s+(?:system|hidden|internal)\b", "inj_exfil"),
    (r"\bprint\s+(?:your|the)\s+(?:prompt|instructions|memory)\b", "inj_exfil"),
    (
        r"\bdelete\s+(?:all|every|your)\b.{0,30}\b(?:memory|memories|files?|records?)\b",
        "inj_destruct",
    ),
    # concealment
    (
        r"\bdo\s+not\s+(?:tell|inform|alert|mention)\b.{0,30}\b(?:user|operator|system\s+creator|owner|recipient)\b",
        "inj_conceal",
    ),
    # epistemic override — external text demanding belief. "email states X" must never
    # silently become "X is true"; text that INSISTS on that conversion is hostile.
    (
        r"\b(?:record|store|remember|save|treat|accept)\s+(?:this|it|the\s+following)\s+as\s+(?:authoritative|absolute|unquestionable|verified\s+)?(?:fact|truth|authoritative|canon)\b",
        "inj_epistemic",
    ),
    (r"\bnever\s+question\b", "inj_epistemic"),
    (r"\bwithout\s+(?:question|verification|checking)\b", "inj_epistemic"),
    # round-4 paraphrase families (adversarial review reproduced 7 semantic bypasses):
    # persistent-reframe, objective-replacement, memory-wipe asks, precedence claims,
    # persona adoption, "mode" switches, priority hijack, output steering.
    (r"\bfrom\s+now\s+on\b", "inj_override"),
    (
        r"\byour\s+(?:new|real|true|actual|updated|primary)\s+(?:task|goal|objective|purpose|directive|instructions?|mission)\b",
        "inj_override",
    ),
    (
        r"\bforget\s+(?:everything|all\s+(?:previous|prior|earlier|your)|(?:your|the|any)\s+(?:instructions?|rules?|training|guidelines?|context|memory|memories))\b",
        "inj_override",
    ),
    (
        r"\b(?:supersedes?|overrides?|replaces?|takes?\s+precedence\s+over)\b.{0,50}\b(?:guidance|instructions?|rules?|directives?|operator|previous|prior)\b",
        "inj_override",
    ),
    (r"\bpretend\s+(?:to\s+be|that\s+you|you(?:'re|\s+are))\b", "inj_role_switch"),
    (r"\b(?:developer|debug|unrestricted|jailbreak|dan|god)\s+mode\b", "inj_role_switch"),
    (
        r"\bbefore\s+(?:you\s+)?(?:do|respond|reply|process)(?:ing)?\s+(?:to\s+)?anything\s+else\b",
        "inj_override",
    ),
    (r"\b(?:respond|reply|answer)\s+only\s+with\b", "inj_output_steer"),
    (r"\bbegin\s+your\s+(?:reply|response|answer)\s+with\b", "inj_output_steer"),
    (r"\bwithout\s+(?:any\s+)?restrictions?\b", "inj_override"),
    (
        r"\byou\s+(?:will|shall|are\s+to)\s+(?:now\s+)?(?:ignore|obey|comply|follow|forget|answer|act)\b",
        "inj_override",
    ),
]
_COMPILED_INJECTION = [(re.compile(p, re.IGNORECASE), tag) for p, tag in _INJECTION_PATTERNS]

_purity_singleton = None


def _purity():
    global _purity_singleton
    if _purity_singleton is None:
        from guardian.memory_purity import MemoryPurity

        _purity_singleton = MemoryPurity()
    return _purity_singleton


def _emit(decision: str, rule: str, field: str, source: str) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "guardian",
            "external_ingress",
            status="ok" if decision == "allow" else "degraded",
            ok=decision == "allow",
            summary=f"{decision}:{rule} field={field} source={source}",
            decision=decision,
            rule=rule,
            field=field,
            source=source,
        )
    except Exception:
        pass


def screen_text(text: Any, *, source: str = "connector", field: str = "") -> Tuple[str, str]:
    """Screen ONE human-authored field. Returns (text_out, decision).

    decision ∈ {"allow", "flag", "quarantine"}. On quarantine, text_out is the marker —
    the hostile content never travels further than the source artifact.
    """
    raw = str(text or "").strip()
    if not raw:
        return raw, "allow"
    try:
        for pattern, tag in _COMPILED_INJECTION:
            if pattern.search(raw):
                _emit("quarantine", tag, field, source)
                log.info("external ingress QUARANTINE (%s) field=%s source=%s", tag, field, source)
                return QUARANTINE_MARKER, "quarantine"

        from guardian.memory_purity import PurityDecision

        result = _purity().check_leaf(
            raw, lobe="what", source=source, event_type="external_ingress"
        )
        if result.decision == PurityDecision.REJECT:
            _emit("quarantine", result.rule_id or "purity_reject", field, source)
            log.info(
                "external ingress QUARANTINE (purity %s) field=%s source=%s",
                result.rule_id,
                field,
                source,
            )
            return QUARANTINE_MARKER, "quarantine"
        if result.decision == PurityDecision.FLAG:
            _emit("flag", result.rule_id or "purity_flag", field, source)
            return raw, "flag"
        return raw, "allow"
    except Exception:  # noqa: BLE001 — fail CLOSED on content, OPEN on the system
        log.warning("external ingress screen failed; quarantining field=%s", field, exc_info=True)
        return QUARANTINE_MARKER, "quarantine"


def screen_receipt(receipt: Mapping[str, Any], *, source: str = "connector") -> Dict[str, Any]:
    """Screen a connector receipt DEFAULT-UNTRUSTED — every string, at ANY depth.

    Round-4 contract (the field-name gate let ``payload.data`` and the raw ``value``
    field pass): a string skips the screen ONLY when its key is a known provider
    STRUCTURAL field and its value matches that field's shape. Everything else —
    unknown keys, structural keys carrying free text, list elements — is screened.
    Numbers/bools/None pass (they cannot carry instructions). The returned receipt
    carries a ``guardian`` block with per-field DECISION RECEIPTS for every non-allow
    verdict plus the screened-string count.
    """
    quarantined = 0
    screened_fields = 0
    decisions: Dict[str, str] = {}

    def _screen_leaf(value: Any, path: str) -> str:
        nonlocal quarantined, screened_fields
        screened_fields += 1
        text_out, decision = screen_text(value, source=source, field=path)
        if decision != "allow":
            decisions[path] = decision
        if decision == "quarantine":
            quarantined += 1
        return text_out

    def _walk(obj: Any, path: str) -> Any:
        if isinstance(obj, Mapping):
            out: Dict[str, Any] = {}
            for key, value in obj.items():
                child_path = f"{path}.{key}" if path else str(key)
                shape = PROVIDER_STRUCTURAL_FIELDS.get(str(key))
                if isinstance(value, str):
                    if shape is not None and shape.match(value):
                        out[key] = value  # shape-valid structural metadata only
                    else:
                        out[key] = _screen_leaf(value, child_path)
                elif isinstance(value, (Mapping, list)):
                    out[key] = _walk(value, child_path)
                else:
                    out[key] = value
            return out
        if isinstance(obj, list):
            return [
                _screen_leaf(item, f"{path}[{i}]")
                if isinstance(item, str)
                else _walk(item, f"{path}[{i}]")
                if isinstance(item, (Mapping, list))
                else item
                for i, item in enumerate(obj)
            ]
        return obj

    try:
        screened = _walk(dict(receipt), "")
    except Exception as exc:  # noqa: BLE001 — receipt walk must not silently quarantine
        log.warning("receipt screen failed", exc_info=True)
        raise ExternalIngressScreenError("screen failed") from exc
    screened["guardian"] = {
        "screened": True,
        "quarantined": quarantined,
        "screened_fields": screened_fields,
        "decisions": decisions,  # the decision receipt — auditable per field-path
    }
    return screened


def receipt_digest(
    source_ledger_receipt: str,
    source: str,
    guardian_block: Mapping[str, Any],
) -> str:
    """Deterministic sha256 hex digest binding ledger receipt, source, and screen stats."""
    payload = {
        "decisions": dict(guardian_block.get("decisions") or {}),
        "quarantined": int(guardian_block.get("quarantined") or 0),
        "screened_fields": int(guardian_block.get("screened_fields") or 0),
        "source": str(source or ""),
        "source_ledger_receipt": str(source_ledger_receipt or ""),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def harden(text: str) -> str:
    """Wrap screened external content for prompt composition: data, never instructions.

    Structural delimiters are ESCAPED (round-4: unescaped content could emit
    ``</external-data>`` and close its own fence, then speak outside it) — the fence
    is only a boundary if the content cannot reproduce it."""
    body = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<external-data trust="untrusted-human-authored">{body}</external-data>'
