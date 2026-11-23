"""Standalone core multi-user LLM endpoint egress policy.

Enforces address-based (never identity-based) private / loopback / link-local /
cloud-metadata blocking when running in standalone core multi-user mode
(``DALTON_AUTH_MODE=local``), while keeping:
- Everflow (``DALTON_AUTH_MODE=supabase``) and single-user ``oss`` dev mode unchanged;
- Only two address classes exempt from the private-IP block:
  1. Endpoints matching a ``base_url`` saved in the team credentials (locked at
     connect time to the address class — ``public`` or ``private`` — resolved when saved);
  2. Endpoints matching the deployer-configured ``DALTON_OPERATOR_LLM_ENDPOINTS`` allowlist
     (matched on canonical normalized scheme, host, port, and path).
- Connect-time DNS pinning (anti-DNS-rebinding) and ``follow_redirects=False`` enforced
  for ALL model endpoints in ``local`` mode, including team and allowlisted endpoints.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import posixpath
from collections.abc import Iterable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from psycopg.rows import dict_row

from src.dalton.settings import (
    get_dalton_operator_llm_endpoints,
    is_dalton_local_auth_enabled,
)
from src.server.utils.egress_guard import (
    EgressBlockedError,
    EgressResolutionError,
    is_public_address,
    resolve_public_ips,
)

logger = logging.getLogger(__name__)

AddressClass = Literal["public", "private"]
IPPolicy = Literal["public", "private", "any"]

_TEAM_BASE_URL_CLASSES_PREF_KEY = "dalton_team_base_url_classes"
# In-process mirror keyed by team holder user_id -> {canonical_url_key: "public"|"private"}.
# Used as a fallback when user_preferences cannot be updated in isolated unit tests.
_LOCAL_TEAM_URL_CLASSES: dict[str, dict[str, AddressClass]] = {}

_ENCODED_TRAVERSAL_MARKERS = ("%2e", "%2f", "%5c")


def is_core_llm_egress_enforced() -> bool:
    """Return True when Dalton core multi-user LLM egress enforcement is active.

    Everflow (``supabase``) and single-user ``oss`` dev mode return ``False``.
    """
    return is_dalton_local_auth_enabled()


def _expand_host_ip(raw_url: str) -> str:
    if "{HOST_IP}" not in raw_url:
        return raw_url
    from src.config.env import HOST_IP

    return raw_url.replace("{HOST_IP}", HOST_IP)


def canonicalize_endpoint(raw_url: str) -> tuple[str, str, int, str] | None:
    """Parse and canonicalize an http(s) URL into ``(scheme, host, port, norm_path)``.

    Resolves ``.`` and ``..`` segments and duplicate slashes so allowlist and saved
    team-endpoint comparisons always match the normalized request target that HTTP
    clients send on the wire. Rejects userinfo, backslashes, control characters, and
    percent-encoded dot/slash traversal sequences.
    """
    cleaned = _expand_host_ip(str(raw_url or "").strip())
    if not cleaned:
        return None
    if "\\" in cleaned or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
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

    raw_path = parts.path or ""
    lowered_path = raw_path.lower()
    if any(marker in lowered_path for marker in _ENCODED_TRAVERSAL_MARKERS):
        return None

    if raw_path:
        norm = posixpath.normpath("/" + raw_path.lstrip("/"))
        # posixpath.normpath("//a") preserves leading double-slash per POSIX; collapse it.
        if norm.startswith("//"):
            norm = "/" + norm.lstrip("/")
        path = "" if norm == "/" else norm.rstrip("/")
    else:
        path = ""
    return scheme, host, port, path


def canonical_url_key(raw_url: str) -> str | None:
    """Return a canonical ``scheme://host:port/norm_path`` string key, or ``None``."""
    parsed = canonicalize_endpoint(raw_url)
    if parsed is None:
        return None
    scheme, host, port, path = parsed
    host_fmt = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_fmt}:{port}{path}"


def endpoint_matches_prefix(url: str, allowed_url: str) -> bool:
    """Return True when ``url`` matches ``allowed_url`` on canonical scheme/host/port/path."""
    candidate = canonicalize_endpoint(url)
    allowed = canonicalize_endpoint(allowed_url)
    if candidate is None or allowed is None:
        return False
    c_scheme, c_host, c_port, c_path = candidate
    a_scheme, a_host, a_port, a_path = allowed
    if (c_scheme, c_host, c_port) != (a_scheme, a_host, a_port):
        return False
    if not a_path:
        return True
    return c_path == a_path or c_path.startswith(f"{a_path}/")


def is_operator_allowlisted_endpoint(url: str) -> bool:
    """Return True when ``url`` matches an entry in ``DALTON_OPERATOR_LLM_ENDPOINTS``.

    Comparison uses canonical normalized ``(scheme, host, port, path)`` so dot-segments
    like ``/v1/../admin`` cannot escape a configured ``/v1`` prefix.
    """
    if not is_dalton_local_auth_enabled():
        return False
    allowlist = get_dalton_operator_llm_endpoints()
    if not allowlist:
        return False
    return any(endpoint_matches_prefix(url, entry) for entry in allowlist)


def classify_ips(ips: Iterable[str]) -> AddressClass | None:
    """Classify a non-empty IP list as uniformly ``'public'`` or ``'private'``.

    Returns ``None`` if empty, unparseable, or mixed (contains both public and private IPs).
    """
    saw_public = False
    saw_private = False
    for raw in ips:
        try:
            addr = ipaddress.ip_address(str(raw).strip().strip("[]"))
        except ValueError:
            return None
        if is_public_address(addr):
            saw_public = True
        else:
            saw_private = True
    if saw_public and saw_private:
        return None
    if saw_public:
        return "public"
    if saw_private:
        return "private"
    return None


def _is_literal_private_host(host: str) -> bool:
    cleaned = host.lower().rstrip(".").strip("[]")
    if cleaned == "localhost" or cleaned.endswith(".localhost"):
        return True
    try:
        return not is_public_address(ipaddress.ip_address(cleaned))
    except ValueError:
        return False


def _infer_fallback_class(url: str) -> AddressClass:
    parsed = canonicalize_endpoint(url)
    if parsed is not None and _is_literal_private_host(parsed[1]):
        return "private"
    if is_operator_allowlisted_endpoint(url):
        return "private"
    return "public"


async def get_saved_team_endpoints() -> dict[str, AddressClass]:
    """Return ``{canonical_url_key: address_class}`` for all saved team credential base URLs.

    Active only in ``local`` auth mode when team credential sharing is enabled.
    Reads ``user_api_keys.base_url`` and recorded address classes from the team holder's
    ``user_preferences.other_preference`` (merged with in-process mirror).
    """
    if not is_dalton_local_auth_enabled():
        return {}
    try:
        from src.dalton.credentials import get_team_credential_holder_id_async

        holder_id = await get_team_credential_holder_id_async()
    except Exception as exc:
        logger.warning(
            "get_saved_team_endpoints holder lookup failed (fail-closed empty): %s",
            type(exc).__name__,
        )
        return {}
    if not holder_id:
        return {}

    recorded: dict[str, AddressClass] = dict(_LOCAL_TEAM_URL_CLASSES.get(holder_id, {}))
    base_urls: list[str] = []

    try:
        from src.server.database.pool import get_db_connection

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT DISTINCT base_url FROM user_api_keys "
                    "WHERE user_id = %s AND base_url IS NOT NULL AND BTRIM(base_url) <> ''",
                    (holder_id,),
                )
                rows = await cur.fetchall()
                base_urls = [
                    str(r["base_url"]).strip()
                    for r in rows
                    if r and r.get("base_url") and str(r["base_url"]).strip()
                ]
                await cur.execute(
                    "SELECT other_preference->%s AS url_classes "
                    "FROM user_preferences WHERE user_id = %s",
                    (_TEAM_BASE_URL_CLASSES_PREF_KEY, holder_id),
                )
                pref_row = await cur.fetchone()
                raw_classes = (pref_row or {}).get("url_classes")
                if isinstance(raw_classes, dict):
                    for k, v in raw_classes.items():
                        ck = canonical_url_key(str(k))
                        if ck and v in ("public", "private"):
                            recorded[ck] = v  # type: ignore[assignment]
    except Exception as exc:
        logger.debug(
            "get_saved_team_endpoints DB lookup unavailable (using in-process mirror): %s",
            type(exc).__name__,
        )

    result: dict[str, AddressClass] = {}
    for url in base_urls:
        ck = canonical_url_key(url)
        if not ck:
            continue
        result[ck] = recorded.get(ck) or _infer_fallback_class(url)
    return result


async def record_saved_team_base_url_classes(
    url_classes: Mapping[str, AddressClass],
    *,
    conn: Any = None,
) -> None:
    """Persist the resolved address class (``'public'`` or ``'private'``) for saved team base URLs."""
    if not is_dalton_local_auth_enabled() or not url_classes:
        return
    try:
        from src.dalton.credentials import get_team_credential_holder_id_async

        holder_id = await get_team_credential_holder_id_async(conn=conn)
    except Exception:
        return
    if not holder_id:
        return

    normalized: dict[str, AddressClass] = {}
    for raw_url, cls in url_classes.items():
        ck = canonical_url_key(raw_url)
        if ck and cls in ("public", "private"):
            normalized[ck] = cls
    if not normalized:
        return

    _LOCAL_TEAM_URL_CLASSES.setdefault(holder_id, {}).update(normalized)

    try:
        from src.server.database.pool import get_db_connection

        payload_json = json.dumps(normalized)
        async with get_db_connection(conn) as db:
            async with db.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE user_preferences
                    SET other_preference = jsonb_set(
                        COALESCE(other_preference, '{}'::jsonb),
                        ARRAY[%s],
                        COALESCE(other_preference->%s, '{}'::jsonb) || %s::jsonb,
                        true
                    ),
                    updated_at = NOW()
                    WHERE user_id = %s
                    """,
                    (
                        _TEAM_BASE_URL_CLASSES_PREF_KEY,
                        _TEAM_BASE_URL_CLASSES_PREF_KEY,
                        payload_json,
                        holder_id,
                    ),
                )
    except Exception as exc:
        logger.debug(
            "record_saved_team_base_url_classes DB write skipped: %s",
            type(exc).__name__,
        )


def match_saved_team_endpoint(
    url: str,
    saved_team_endpoints: Mapping[str, AddressClass] | None,
) -> AddressClass | None:
    """Return the saved address class when ``url`` matches a saved team credential ``base_url``."""
    if not saved_team_endpoints:
        return None
    best_len = -1
    best_class: AddressClass | None = None
    for saved_url, addr_class in saved_team_endpoints.items():
        if not endpoint_matches_prefix(url, saved_url):
            continue
        parsed = canonicalize_endpoint(saved_url)
        path_len = len(parsed[3]) if parsed is not None else 0
        if path_len > best_len:
            best_len = path_len
            best_class = addr_class
    return best_class


def ip_policy_for_request_url(
    url: str,
    *,
    saved_team_endpoints: Mapping[str, AddressClass] | None = None,
) -> IPPolicy:
    """Resolve the connect-time IP policy for an outgoing model request URL.

    - ``'any'``: URL matches ``DALTON_OPERATOR_LLM_ENDPOINTS`` (allows private or public,
      still rejects mixed public+private and pins the resolved IP).
    - ``'private'``: URL matches a saved team credential ``base_url`` whose saved address
      class was ``'private'`` (pins to non-global IPs only).
    - ``'public'``: everything else, including saved team URLs that resolved to public IPs
      when saved, admin key-test URLs, and admin custom-model parameter URLs.
    """
    if canonicalize_endpoint(url) is None:
        return "public"
    if is_operator_allowlisted_endpoint(url):
        return "any"
    saved_cls = match_saved_team_endpoint(url, saved_team_endpoints)
    if saved_cls == "private":
        return "private"
    return "public"


async def check_core_endpoint_refusal(
    url: str,
    *,
    require_resolution: bool = True,
    saved_team_endpoints: Mapping[str, AddressClass] | None = None,
) -> tuple[str | None, AddressClass | None]:
    """Validate ``url`` under core multi-user egress rules and return ``(refusal_reason, resolved_class)``.

    Enforces address-based policy only (never user identity):
    - Only saved team credential ``base_url``s and ``DALTON_OPERATOR_LLM_ENDPOINTS`` may
      resolve to private/non-global addresses.
    - Every other URL (including manifest/public URLs, admin probes, and admin custom-model
      parameter URLs) must resolve exclusively to public addresses.
    - Mixed public+private DNS responses are always refused.
    """
    from src.server.services.llm import egress

    parsed = canonicalize_endpoint(url)
    if parsed is None:
        return egress.NOT_HTTP, None
    scheme, host, port, _path = parsed
    policy = ip_policy_for_request_url(url, saved_team_endpoints=saved_team_endpoints)

    if policy == "public":
        if host == "localhost" or host.endswith(".localhost"):
            return egress.PRIVATE_ADDRESS, None
        try:
            import asyncio

            ips = await asyncio.wait_for(
                resolve_public_ips(
                    host,
                    port=port,
                    allow_non_global=False,
                    executor=egress._CHECK_RESOLVER,
                ),
                egress.CHECK_TIMEOUT,
            )
        except (EgressResolutionError, TimeoutError):
            return (egress.UNRESOLVED, None) if require_resolution else (None, "public")
        except EgressBlockedError:
            return egress.PRIVATE_ADDRESS, None
        return None, classify_ips(ips) or "public"

    # policy in ("private", "any"): allow non-global IPs, but still resolve and verify class
    try:
        import asyncio

        ips = await asyncio.wait_for(
            resolve_public_ips(
                host,
                port=port,
                allow_non_global=True,
                executor=egress._CHECK_RESOLVER,
            ),
            egress.CHECK_TIMEOUT,
        )
    except (EgressResolutionError, TimeoutError):
        if require_resolution:
            return egress.UNRESOLVED, None
        return None, ("private" if policy == "private" else None)
    except EgressBlockedError:
        return egress.PRIVATE_ADDRESS, None

    addr_class = classify_ips(ips)
    if addr_class is None:
        # Mixed public + private DNS response is always refused.
        return egress.PRIVATE_ADDRESS, None
    if policy == "private" and addr_class != "private":
        return egress.PRIVATE_ADDRESS, None
    return None, addr_class


def clear_local_team_url_classes() -> None:
    """Clear the in-process team URL address-class mirror (for unit test isolation)."""
    _LOCAL_TEAM_URL_CLASSES.clear()


__all__ = [
    "AddressClass",
    "IPPolicy",
    "canonical_url_key",
    "canonicalize_endpoint",
    "check_core_endpoint_refusal",
    "classify_ips",
    "clear_local_team_url_classes",
    "endpoint_matches_prefix",
    "get_saved_team_endpoints",
    "ip_policy_for_request_url",
    "is_core_llm_egress_enforced",
    "is_operator_allowlisted_endpoint",
    "match_saved_team_endpoint",
    "record_saved_team_base_url_classes",
]
