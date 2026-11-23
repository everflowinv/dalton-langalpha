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
from collections.abc import Iterable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
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
    """Parse and canonicalize an http(s) URL into ``(scheme, host, port, wire_path)``.

    Uses HTTPX's own RFC 3986 path normalization (``httpx.URL.raw_path``) so dot-segments
    are resolved identically to what HTTPX sends on the wire while duplicate slashes
    (``//``) are preserved verbatim and never silently collapsed. Rejects userinfo,
    backslashes, control characters, and percent-encoded dot/slash traversal sequences.
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

    try:
        wire_path = httpx.URL(cleaned).raw_path.split(b"?", 1)[0].decode("latin-1")
    except (httpx.InvalidURL, ValueError):
        return None

    if wire_path == "/":
        path = ""
    elif wire_path.endswith("/") and not wire_path.endswith("//"):
        path = wire_path[:-1]
    else:
        path = wire_path
    return scheme, host, port, path


def canonical_url_key(raw_url: str) -> str | None:
    """Return a canonical ``scheme://host:port/wire_path`` string key, or ``None``."""
    parsed = canonicalize_endpoint(raw_url)
    if parsed is None:
        return None
    scheme, host, port, path = parsed
    host_fmt = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_fmt}:{port}{path}"


def endpoint_matches_prefix(url: str, allowed_url: str) -> bool:
    """Return True when ``url`` matches ``allowed_url`` on scheme/host/port and HTTPX wire path."""
    candidate = canonicalize_endpoint(url)
    allowed = canonicalize_endpoint(allowed_url)
    if candidate is None or allowed is None:
        return False
    c_scheme, c_host, c_port, c_path = candidate
    a_scheme, a_host, a_port, a_path = allowed
    if (c_scheme, c_host, c_port) != (a_scheme, a_host, a_port):
        return False
    if not a_path:
        return not c_path.startswith("//")
    return c_path == a_path or (
        c_path.startswith(f"{a_path}/") and not c_path.startswith(f"{a_path}//")
    )


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
    Reads ``user_api_keys.base_url`` and recorded address classes strictly from PostgreSQL
    (``user_preferences.other_preference``), with no in-process mirror.
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

    recorded: dict[str, AddressClass] = {}
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
                    "SELECT other_preference->%s::text AS url_classes "
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
        logger.warning(
            "get_saved_team_endpoints DB lookup failed (fail-closed empty): %s",
            type(exc).__name__,
        )
        return {}

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
    """Persist the resolved address class (``'public'`` or ``'private'``) for saved team base URLs.

    PostgreSQL is the single source of truth (no in-process mirror). Raises on lookup or
    write failure so the caller's save transaction fails loudly instead of silently skipping.
    """
    if not is_dalton_local_auth_enabled() or not url_classes:
        return
    from src.dalton.credentials import get_team_credential_holder_id_async

    holder_id = await get_team_credential_holder_id_async(conn=conn)
    if not holder_id:
        return

    normalized: dict[str, AddressClass] = {}
    for raw_url, cls in url_classes.items():
        ck = canonical_url_key(raw_url)
        if ck and cls in ("public", "private"):
            normalized[ck] = cls
    if not normalized:
        return

    from src.server.database.pool import get_db_connection

    payload_json = json.dumps(normalized)
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_preferences (
                    user_preference_id, user_id, other_preference, created_at, updated_at
                )
                VALUES (
                    gen_random_uuid(),
                    %s,
                    jsonb_build_object(%s::text, %s::jsonb),
                    NOW(),
                    NOW()
                )
                ON CONFLICT (user_id) DO UPDATE
                SET other_preference = jsonb_set(
                    COALESCE(user_preferences.other_preference, '{}'::jsonb),
                    ARRAY[%s::text],
                    COALESCE(user_preferences.other_preference->%s::text, '{}'::jsonb) || %s::jsonb,
                    true
                ),
                updated_at = NOW()
                """,
                (
                    holder_id,
                    _TEAM_BASE_URL_CLASSES_PREF_KEY,
                    payload_json,
                    _TEAM_BASE_URL_CLASSES_PREF_KEY,
                    _TEAM_BASE_URL_CLASSES_PREF_KEY,
                    payload_json,
                ),
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

    Precedence:
    1. Saved team credential ``base_url`` takes priority over the deployer allowlist:
       if ``url`` matches a saved team endpoint, locks strictly to its saved address
       class (``'public'`` or ``'private'``), even if the same URL also appears in
       ``DALTON_OPERATOR_LLM_ENDPOINTS``.
    2. ``DALTON_OPERATOR_LLM_ENDPOINTS`` relaxes only URLs that have NO saved team
       record, returning ``'any'`` (allows uniform private or uniform public, still
       rejects mixed public+private and pins the resolved IP).
    3. Everything else returns ``'public'``.
    """
    if canonicalize_endpoint(url) is None:
        return "public"
    saved_cls = match_saved_team_endpoint(url, saved_team_endpoints)
    if saved_cls in ("public", "private"):
        return saved_cls
    if is_operator_allowlisted_endpoint(url):
        return "any"
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


__all__ = [
    "AddressClass",
    "IPPolicy",
    "canonical_url_key",
    "canonicalize_endpoint",
    "check_core_endpoint_refusal",
    "classify_ips",
    "endpoint_matches_prefix",
    "get_saved_team_endpoints",
    "ip_policy_for_request_url",
    "is_core_llm_egress_enforced",
    "is_operator_allowlisted_endpoint",
    "match_saved_team_endpoint",
    "record_saved_team_base_url_classes",
]
