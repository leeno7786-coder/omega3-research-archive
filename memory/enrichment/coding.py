"""Coding enrichment (lobe 8) - detect language + concern.

Lobe 8 stores coding knowledge in the flat taxonomy from
`memory.dewey_map.SUBDOMAIN_MAP`. This decomposer extracts language and
concern signals, then resolves them through `coding_coord(...)` so the
returned coord is a schema-valid integer `[8, Y, Z]` route.

Pattern-based, deterministic, no LLM. Returns None when no language
signal is detected (graceful degradation).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from memory.dewey_map import CODING_DOMAINS, coding_coord

log = logging.getLogger("omega.memory.enrichment.coding")


# Language detection cues.
# Map of regex patterns to canonical language slug.
_LANGUAGE_CUES: Dict[str, str] = {
    # Python - language name + ecosystem libs + literal syntax tokens
    # ('def ', 'import ' / 'from ' for module imports). The literal
    # tokens give the classifier a strong code-vs-prose signal that
    # other lobes' anchors never produce.
    r"\bpython\b|\bpy\b|\.py\b|\bdjango\b|\bflask\b|\bpytorch\b|\bnumpy\b|\bpandas\b|\bdef \w+\(|\bimport \w+|\bfrom \w+ import\b|\bpip install\b|\bpytest\b|\basyncio\b": "python",
    r"\btypescript\b|\bts\b|\.ts\b|\.tsx\b|\bnext\.js\b|\bnest\.js\b|\binterface \w+|\.tsx?\b|\bvite\b|\breact\b": "typescript",
    r"\brust\b|\bcargo\b|\.rs\b|\btokio\b|\baxum\b": "rust",
    r"\bgolang\b|\bgo\b(?!\s*(?:back|forward|to))|\.go\b|\bgoroutine\b": "go",
    r"\bansi\s*c\b|\.c\b|\bmalloc\b|\bstdio\b": "c",
    r"\bc\+\+\b|\bcpp\b|\.cpp\b|\bstd::\w+|\bboost\b": "cpp",
    r"\bsql\b|\bselect\s+.+\s+from\b|\bpostgres(?:ql)?\b|\bmysql\b|\bsqlite\b|\bcte\b|\bjoin\b|\bgroup by\b": "sql",
    r"\bbash\b|\bzsh\b|\.sh\b|\bshell script\b|\bpowershell\b|\bps1\b": "shell",
    r"\bhtml\b|\bcss\b|\btailwind\b|\bflexbox\b|\b<div>\b|\bgrid template\b": "html_css",
}


# Concern detection cues.
_CONCERN_CUES: Dict[str, str] = {
    # Web - frameworks count too (django, flask, fastapi for python; react,
    # next, vite for ts; axum for rust). A language anchor + framework
    # almost always implies the web concern.
    r"\bweb\b|\bhttp\b|\brest\b|\bapi\b|\bbackend\b|\bfrontend\b|\bserver\b|\bdjango\b|\bflask\b|\bfastapi\b|\bview\b|\breact\b|\bvite\b|\bnext\.js\b|\bnest\.js\b|\baxum\b|\btailwind\b|\bcss\b|\bhtml\b": "web",
    r"\bml\b|\bmachine learning\b|\bneural\b|\btraining\b|\binference\b|\bmodel\b|\bpytorch\b|\bnumpy\b": "ml",
    # Data - pandas / dataframe / etl / sql analytics + the literal SQL
    # tokens that imply data work.
    r"\bdata\b|\betl\b|\bpipeline\b|\bdataframe\b|\banalytics\b|\bpandas\b|\bcte\b|\bselect\s+.+\s+from\b|\bgroup by\b|\bjoin\b": "data",
    r"\bcli\b|\bcommand[- ]?line\b|\bargparse\b|\bclick\b|\bcobra\b": "cli",
    r"\bdevops\b|\bdocker\b|\bkubernetes\b|\bk8s\b|\bci/?cd\b|\bdeploy\b": "devops",
    r"\btests?\b|\btesting\b|\bpytest\b|\bunittest\b|\bjest\b|\bvitest\b": "testing",
    r"\bembedded\b|\bmcu\b|\bmicrocontroller\b|\bfirmware\b|\barduino\b": "embedded",
    r"\bsecurity\b|\bauth\b|\bcrypto\b|\btls\b|\bvulnerab": "security",
    r"\bsystems\b|\bkernel\b|\bsyscall\b|\bos\b|\bmemory management\b": "systems",
    r"\bnode\b|\bnpm\b|\byarn\b|\bdeno\b|\bbun\b": "node",
    r"\badmin\b|\bdba\b|\bschema\b|\bmigration\b|\bindex\b": "admin",
}


def _detect_language(text_lo: str, hint: Optional[str]) -> Optional[str]:
    """Return the dominant language slug. Caller hint wins if set."""
    if hint:
        return str(hint)
    for pattern, lang in _LANGUAGE_CUES.items():
        if re.search(pattern, text_lo):
            return lang
    return None


def _detect_concern(text_lo: str, hint: Optional[str]) -> Optional[str]:
    """Return the dominant concern slug; default 'systems' when nothing
    matches but a language was detected (so the entry still gets a Z)."""
    if hint:
        return str(hint)
    for pattern, concern in _CONCERN_CUES.items():
        if re.search(pattern, text_lo):
            return concern
    return None


def enrich(text: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Decompose a coding write into language + concern + coord."""
    if not text or not isinstance(text, str):
        return None
    text_lo = text.lower()
    language = _detect_language(text_lo, kwargs.get("language"))
    if not language:
        log.debug("coding: no language signal in %r", text[:60])
        return None
    concern = _detect_concern(text_lo, kwargs.get("concern")) or "systems"
    domain_key = f"{language}_{concern}"
    coord = coding_coord(domain_key)
    return {
        "language": language,
        "concern": concern,
        "coord": coord,
        "domain": domain_key,
        "known": domain_key in CODING_DOMAINS,
    }
