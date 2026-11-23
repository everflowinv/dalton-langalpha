"""Unified SEC EDGAR identity resolution shared by Dalton settings and EDGAR tools.

Keep this module stdlib-only so ``src/tools/sec/edgar.py`` can import it at
module import time without triggering ``src.dalton.settings`` or ``src.config``.
"""

from __future__ import annotations

import logging
import os

DEFAULT_SEC_USER_AGENT = "DaltonResearch sec-contact@example.com"


def resolve_sec_user_agent() -> tuple[str, bool]:
    """Return ``(identity, is_configured)`` from ``SEC_USER_AGENT`` / ``EDGAR_IDENTITY``.

    Precedence: ``SEC_USER_AGENT`` > ``EDGAR_IDENTITY`` > neutral placeholder.
    """
    for key in ("SEC_USER_AGENT", "EDGAR_IDENTITY"):
        value = os.getenv(key, "").strip()
        if value:
            return value, True
    return DEFAULT_SEC_USER_AGENT, False


def ensure_edgar_identity_env(log: logging.Logger) -> str:
    """Populate ``os.environ['EDGAR_IDENTITY']`` from the unified SEC identity rules.

    If only ``SEC_USER_AGENT`` is configured, ``EDGAR_IDENTITY`` is populated with
    that value so direct ``edgar_engine`` calls use the same identity without
    falling back to an unrelated placeholder. When neither variable is set, logs
    a warning matching Dalton startup guidance and sets the neutral placeholder.
    """
    identity, configured = resolve_sec_user_agent()
    if not configured:
        log.warning(
            "Neither SEC_USER_AGENT nor EDGAR_IDENTITY is set: SEC asks every client to "
            "name itself with an organization/name and email (\"<Organization> <contact_email>\"). "
            "Using neutral placeholder %r.",
            identity,
        )
    if not os.getenv("EDGAR_IDENTITY", "").strip():
        os.environ["EDGAR_IDENTITY"] = identity
    return identity
