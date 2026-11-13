"""Vision enrichment — wraps image description text for lobe 7 (vision)."""

import logging
from typing import Dict, Optional

log = logging.getLogger("omega.memory.enrichment.vision_enrichment")


def enrich(text: str) -> Optional[Dict]:
    if not text or len(text) < 10:
        return None
    return {
        "decomposer": "vision",
        "kind": "vision",
        "description": text[:500],
    }
