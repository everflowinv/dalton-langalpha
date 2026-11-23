"""Standalone core multi-user LLM endpoint egress policy.

Enforces private / loopback / link-local / cloud-metadata blocking on member-supplied
custom model endpoints when running in standalone core multi-user mode
(``DALTON_AUTH_MODE=local``), while keeping:
- Everflow (``DALTON_AUTH_MODE=supabase``) and single-user ``oss`` dev mode unchanged;
- Deployer-configured allowlist endpoints in ``DALTON_OPERATOR_LLM_ENDPOINTS`` allowed;
- Administrator-configured team models exempt from the member egress block.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from src.dalton.settings import (
    get_dalton_operator_llm_endpoints,
    is_dalton_local_auth_enabled,
)

logger = logging.getLogger(__name__)


def is_core_llm_egress_enforced() -> bool:
    """Return True when Dalton core multi-user LLM egress enforcement is active.

    Everflow (``supabase``) and single-user ``oss`` dev mode return ``False``.
    """
    return is_dalton_local_auth_enabled()


def _parse_endpoint(raw_url: str) -> tuple[str, str, int, str] | None:
    """Parse an http(s) URL into ``(scheme, host, port, normalized_path)`` or ``None``."""
    cleaned = str(raw_url or "").strip()
    if not cleaned:
        return None
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    if parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    # Normalize trailing slashes on path so ``/v1`` matches ``/v1`` and ``/v1/models``,
    # while never matching ``/v1evil``.
    path = (parts.path or "").rstrip("/")
    return scheme, host, port, path


def is_operator_allowlisted_endpoint(url: str) -> bool:
    """Return True when ``url`` matches an entry in ``DALTON_OPERATOR_LLM_ENDPOINTS``.

    Each comma-separated entry in ``DALTON_OPERATOR_LLM_ENDPOINTS`` may be an origin
    (``http://fake-llm:8099``) or a base URL with path prefix
    (``http://fake-llm:8099/v1``). Scheme, hostname, and effective port must match
    exactly, and any configured path must match on a path-segment boundary.
    """
    if not is_dalton_local_auth_enabled():
        return False
    allowlist = get_dalton_operator_llm_endpoints()
    if not allowlist:
        return False
    candidate = _parse_endpoint(url)
    if candidate is None:
        return False
    c_scheme, c_host, c_port, c_path = candidate

    for entry in allowlist:
        allowed = _parse_endpoint(entry)
        if allowed is None:
            continue
        a_scheme, a_host, a_port, a_path = allowed
        if (c_scheme, c_host, c_port) != (a_scheme, a_host, a_port):
            continue
        if not a_path:
            return True
        if c_path == a_path or c_path.startswith(f"{a_path}/"):
            return True
    return False


async def is_admin_llm_egress_exempt(user_id: str | None) -> bool:
    """Return True when ``user_id`` is an administrator configuring team models in ``local`` mode.

    Never exempts users in ``HOST_MODE=platform`` (upstream SaaS) or non-admin members.
    Fails closed (returns ``False``) on missing ``user_id`` or lookup errors.
    """
    if not is_dalton_local_auth_enabled():
        return False
    uid = str(user_id or "").strip()
    if not uid:
        return False
    try:
        from src.dalton.access_tiers import is_admin_user

        return await is_admin_user(uid)
    except Exception as exc:
        logger.warning(
            "is_admin_llm_egress_exempt failed for user_id=%s (fail-closed False): %s",
            uid,
            type(exc).__name__,
        )
        return False


def is_team_shared_base_url_exempt() -> bool:
    """Return True in ``local`` mode when ``base_url`` comes from admin-managed team credentials."""
    if not is_dalton_local_auth_enabled():
        return False
    try:
        from src.dalton.credentials import is_team_credential_sharing_enabled

        return is_team_credential_sharing_enabled()
    except Exception as exc:
        logger.warning(
            "is_team_shared_base_url_exempt check failed (fail-closed False): %s",
            type(exc).__name__,
        )
        return False


__all__ = [
    "is_admin_llm_egress_exempt",
    "is_core_llm_egress_enforced",
    "is_operator_allowlisted_endpoint",
    "is_team_shared_base_url_exempt",
]
