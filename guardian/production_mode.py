"""
Production Mode Flag — gate for the cryptographic identity layer
=================================================================

The HMAC ledger + IdentityCharter birth/verify operations are
**sacred** — once activated, the chain is permanent and tied to the
Omega instance. This is correct for production rollout (each customer's
portable Omega gets exactly one birth) but wrong for development (we
don't want a running test suite to permanently birth a charter every
time we re-clone the repo).

The flag is OFF by default. When OFF:
    - HMACLedger.birth() is a no-op that returns False
    - HMACLedger.verify() returns (True, -1) ("deferred — never sealed")
    - IdentityCharter.birth() / verify() return "deferred"
    - GuardianNode._check_constitutional returns a passed DomainResult
      with finding "constitutional check deferred — production mode disabled"
    - write_guard / memory_purity run normally —
      content policy enforcement does NOT depend on the chain

When ON (production rollout):
    - First boot: birth() runs once, ledger written, chain sealed.
    - Every subsequent boot: verify() runs; if chain broken, system
      refuses to start.

Activation paths:
    1. Environment variable: OMEGA_PRODUCTION_MODE=1
    2. Config flag: governance.yaml -> guardian.production_mode: true
    3. Sentinel file at guardian/ledger/.activated

The first boot in production mode also writes the sentinel so subsequent
boots don't have to re-check the env var or config.
"""

from __future__ import annotations

import logging
import os

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.guardian.production_mode")

_SENTINEL_PATH = OMEGA_ROOT / "guardian" / "ledger" / ".activated"
_ENV_FLAG = "OMEGA_PRODUCTION_MODE"


def is_production_mode() -> bool:
    """Return True if production mode is active.

    Resolution order:
        1. Sentinel file (set on first activation — irrevocable per instance)
        2. Environment variable
        3. Config flag (governance.yaml)
        4. Default: False (dev mode)
    """
    # Sentinel is the strongest signal — once set, the instance is in
    # production for the rest of its life.
    if _SENTINEL_PATH.exists():
        return True

    # Environment variable
    env = os.environ.get(_ENV_FLAG, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True

    # Config flag
    try:
        from config.loader import load_governance_config

        cfg = load_governance_config().get("guardian") or {}
        if bool(cfg.get("production_mode", False)):
            return True
    except Exception as exc:
        log.debug("governance.yaml unavailable for production_mode probe: %s", exc)

    return False


def activate_sentinel() -> None:
    """Write the sentinel file marking this instance as activated.

    Called by the boot path the first time IdentityCharter.birth()
    succeeds in production mode. Once written, the file is never
    removed by any tool — including day_zero_reset (which excludes
    guardian/ledger/ explicitly).
    """
    try:
        _SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SENTINEL_PATH.write_text(
            "Omega production mode activated. "
            "Do not delete — chain integrity depends on this sentinel.\n",
            encoding="utf-8",
        )
        log.info("production sentinel activated at %s", _SENTINEL_PATH)
    except OSError as exc:
        log.error("failed to write production sentinel: %s", exc)
        # Sentinel write failure means a production birth cannot be
        # durably marked — the instance may re-probe env/config on the
        # next boot or fail to recognise it is sealed. Highest-stakes
        # setup degrade; surface it structured. Lazy import keeps
        # production_mode free of an import-time feedback dependency.
        try:
            from guardian.terminal_feedback import emit_phase

            emit_phase(
                "guardian",
                "persist",
                status="fail",
                summary=f"production sentinel write failed: {type(exc).__name__}",
                component="production_mode",
                operation="persist",
                failure_class="sentinel_write_failed",
                error_type=type(exc).__name__,
                sentinel_path=str(_SENTINEL_PATH),
                reason=str(exc)[:120],
                recovery="none",
            )
        except Exception as emit_exc:
            log.debug("production_mode: structured emit failed: %s", emit_exc)


def sentinel_exists() -> bool:
    """Diagnostic — has the sentinel been written?"""
    return _SENTINEL_PATH.exists()
