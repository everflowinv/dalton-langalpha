"""Local authentication JWT access tokens, refresh tokens, CSRF Origin check, and cookie helpers.

- Access tokens: HS256 signed with ``JWT_SIGNING_SECRET`` (>= 32 bytes), 15 min TTL,
  claims: ``sub`` (user_id), ``iat``, ``exp``, ``sid`` (stable session_id), ``typ="access"``.
- Refresh tokens: high-entropy opaque URL-safe strings, stored in PostgreSQL strictly
  as SHA-256 hex digests, delivered to browsers via an ``HttpOnly; SameSite=Lax;
  Path=/api/v1/dalton/auth`` cookie (plus ``Secure`` controlled by deployment config
  ``DALTON_AUTH_COOKIE_SECURE`` or HTTPS / trusted proxy ``X-Forwarded-Proto=https``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException, Request, Response

from src.dalton.company_identity import reject_company_system_auth
from src.dalton.settings import (
    MIN_JWT_SIGNING_SECRET_BYTES,
    get_dalton_auth_cookie_secure_mode,
    get_dalton_frontend_url,
    get_dalton_trusted_proxies,
    get_dalton_trusted_proxy_hops,
    get_jwt_signing_secret,
    is_dalton_local_auth_enabled,
)

LOCAL_JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes
WS_TICKET_TTL_SECONDS = 60  # 60 seconds
WS_TICKET_HMAC_DOMAIN = b"dalton-local-ws-ticket-v1"
DEFAULT_REFRESH_SESSION_TTL_DAYS = 14
REFRESH_COOKIE_NAME = "dalton_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/dalton/auth"
REFRESH_TOKEN_ENTROPY_BYTES = 48


@dataclass(frozen=True)
class LocalAccessClaims:
    """Verified claims extracted from a local HS256 access JWT."""

    user_id: str
    session_id: str
    issued_at: int
    expires_at: int


def get_refresh_session_ttl_seconds() -> int:
    """Return configured refresh session TTL in seconds (clamped to 1..90 days)."""
    raw = os.getenv("DALTON_LOCAL_SESSION_TTL_DAYS", "").strip()
    days = DEFAULT_REFRESH_SESSION_TTL_DAYS
    if raw:
        try:
            days = max(1, min(90, int(raw)))
        except ValueError:
            days = DEFAULT_REFRESH_SESSION_TTL_DAYS
    return days * 24 * 60 * 60


def _require_signing_secret() -> str:
    secret = get_jwt_signing_secret()
    if len(secret.encode("utf-8")) < MIN_JWT_SIGNING_SECRET_BYTES:
        raise HTTPException(
            status_code=503,
            detail="Local authentication JWT_SIGNING_SECRET is not configured",
        )
    return secret


def issue_local_access_token(
    *,
    user_id: str,
    session_id: str,
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, int]:
    """Mint a short-lived HS256 access token and return ``(token, expires_in)``."""
    uid = str(user_id or "").strip()
    sid = str(session_id or "").strip()
    if not uid or not sid:
        raise ValueError("user_id and session_id are required to issue an access token")
    reject_company_system_auth(uid)

    secret = _require_signing_secret()
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + int(ttl_seconds)
    payload = {
        "sub": uid,
        "iat": issued_at,
        "exp": expires_at,
        "sid": sid,
        "typ": "access",
    }
    token = jwt.encode(payload, secret, algorithm=LOCAL_JWT_ALGORITHM)
    return token, int(ttl_seconds)


def decode_local_access_token(token: str) -> LocalAccessClaims:
    """Verify and decode a local HS256 access JWT.

    Raises:
        HTTPException(503): if ``JWT_SIGNING_SECRET`` is missing or shorter than 32 bytes.
        HTTPException(401): if token is missing, malformed, expired, wrong ``typ``,
            missing required claims, or names the company system user.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    secret = _require_signing_secret()
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[LOCAL_JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp", "sid", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("typ") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    sub = payload.get("sub")
    sid = payload.get("sid")
    iat = payload.get("iat")
    exp = payload.get("exp")
    if (
        not isinstance(sub, str)
        or not sub.strip()
        or not isinstance(sid, str)
        or not sid.strip()
    ):
        raise HTTPException(status_code=401, detail="Invalid token claims")

    user_id = sub.strip()
    reject_company_system_auth(user_id)
    return LocalAccessClaims(
        user_id=user_id,
        session_id=sid.strip(),
        issued_at=int(iat),
        expires_at=int(exp),
    )


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    pad = "=" * ((4 - len(encoded) % 4) % 4)
    return base64.urlsafe_b64decode(encoded + pad)


def _ws_ticket_hmac_key() -> bytes:
    secret = _require_signing_secret()
    return hmac.new(
        secret.encode("utf-8"), WS_TICKET_HMAC_DOMAIN, hashlib.sha256
    ).digest()


def issue_local_ws_ticket(
    *,
    user_id: str,
    session_id: str,
    ttl_seconds: int = WS_TICKET_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, int]:
    """Mint a short-lived HMAC-SHA256 WebSocket handshake ticket (default 60s TTL).

    Uses a 2-segment ``<b64url_payload>.<b64url_sig>`` format with a domain-separated
    HMAC key derived from ``JWT_SIGNING_SECRET`` so access JWTs and WS tickets can
    never be confused for one another.
    """
    uid = str(user_id or "").strip()
    sid = str(session_id or "").strip()
    if not uid or not sid:
        raise ValueError("user_id and session_id are required to issue a WS ticket")
    reject_company_system_auth(uid)

    hmac_key = _ws_ticket_hmac_key()
    ttl = max(1, min(WS_TICKET_TTL_SECONDS, int(ttl_seconds)))
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + ttl
    payload = {
        "v": 1,
        "typ": "ws_ticket",
        "sub": uid,
        "sid": sid,
        "iat": issued_at,
        "exp": expires_at,
    }
    payload_bytes = json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(hmac_key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}", ttl


def verify_local_ws_ticket(
    ticket: str,
    *,
    now: int | None = None,
) -> LocalAccessClaims:
    """Verify a short-lived HMAC-SHA256 WebSocket handshake ticket.

    Raises:
        HTTPException(503): if ``JWT_SIGNING_SECRET`` is missing or shorter than 32 bytes.
        HTTPException(401): if ticket is missing, malformed, has an invalid signature,
            expired, wrong type/version, or names the company system user.
    """
    cleaned = str(ticket or "").strip()
    if not cleaned:
        raise HTTPException(status_code=401, detail="Missing WebSocket ticket")

    hmac_key = _ws_ticket_hmac_key()
    parts = cleaned.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(status_code=401, detail="Invalid WebSocket ticket")

    payload_b64, sig_b64 = parts[0], parts[1]
    expected_sig = hmac.new(
        hmac_key, payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        supplied_sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid WebSocket ticket signature"
        ) from exc

    if not hmac.compare_digest(supplied_sig, expected_sig):
        raise HTTPException(
            status_code=401, detail="Invalid WebSocket ticket signature"
        )

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Malformed WebSocket ticket payload"
        ) from exc

    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("typ") != "ws_ticket"
    ):
        raise HTTPException(status_code=401, detail="Invalid WebSocket ticket type")

    sub = payload.get("sub")
    sid = payload.get("sid")
    iat = payload.get("iat")
    exp = payload.get("exp")
    if (
        not isinstance(sub, str)
        or not sub.strip()
        or not isinstance(sid, str)
        or not sid.strip()
        or not isinstance(iat, int)
        or not isinstance(exp, int)
    ):
        raise HTTPException(status_code=401, detail="Invalid WebSocket ticket claims")

    now_ts = int(time.time()) if now is None else int(now)
    if exp <= now_ts:
        raise HTTPException(status_code=401, detail="WebSocket ticket expired")

    user_id = sub.strip()
    reject_company_system_auth(user_id)
    return LocalAccessClaims(
        user_id=user_id,
        session_id=sid.strip(),
        issued_at=iat,
        expires_at=exp,
    )


def generate_refresh_token() -> str:
    """Generate a high-entropy opaque refresh token."""
    return secrets.token_urlsafe(REFRESH_TOKEN_ENTROPY_BYTES)


def hash_refresh_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of a raw refresh token for database storage."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def is_peer_trusted_proxy(request: Request) -> bool:
    """Return True when ``request.client.host`` falls within ``DALTON_TRUSTED_PROXIES``."""
    client = getattr(request, "client", None)
    peer_host = str(getattr(client, "host", "") or "").strip() if client else ""
    if not peer_host:
        return False
    try:
        addr = ipaddress.ip_address(peer_host)
    except ValueError:
        return False
    for cidr in get_dalton_trusted_proxies() or ():
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def is_https_request(request: Request) -> bool:
    """Determine whether the refresh cookie should carry ``Secure``.

    Precedence:
    1. Explicit deployment config ``DALTON_AUTH_COOKIE_SECURE`` (``true`` or ``false``).
    2. In ``local`` auth mode, defaults to ``True`` (secure by default); ONLY explicit
       ``DALTON_AUTH_COOKIE_SECURE=false`` (dev HTTP) clears ``Secure``.
    3. Direct HTTPS connection (``request.url.scheme == "https"``).
    4. ``X-Forwarded-Proto=https`` ONLY when the direct peer is in ``DALTON_TRUSTED_PROXIES``.
    """
    policy = get_dalton_auth_cookie_secure_mode()
    if policy == "true":
        return True
    if policy == "false":
        return False

    if is_dalton_local_auth_enabled():
        return True

    url = getattr(request, "url", None)
    scheme = str(getattr(url, "scheme", "") if url is not None else "").strip().lower()
    if scheme == "https":
        return True

    if is_peer_trusted_proxy(request):
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto:
            proto_parts = [p.strip().lower() for p in forwarded_proto.split(",") if p.strip()]
            hops = get_dalton_trusted_proxy_hops()
            if proto_parts:
                selected = proto_parts[-hops] if 0 < hops <= len(proto_parts) else proto_parts[-1]
                if selected == "https":
                    return True
    return False


def _normalize_origin(raw_url: str) -> str | None:
    cleaned = str(raw_url or "").strip()
    if not cleaned or cleaned.lower() == "null":
        return None
    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    if scheme not in ("http", "https") or not netloc:
        return None
    return f"{scheme}://{netloc}"


def _allowed_cookie_origins(request: Request) -> set[str]:
    allowed: set[str] = set()
    trusted_proxy = is_peer_trusted_proxy(request)
    scheme = "https" if is_https_request(request) else (
        str(getattr(getattr(request, "url", None), "scheme", "http") or "http").lower()
    )
    host = ""
    if trusted_proxy:
        xfh = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        if xfh:
            host = xfh
    if not host:
        host = request.headers.get("host", "").strip()
    if host:
        norm = _normalize_origin(f"{scheme}://{host}")
        if norm:
            allowed.add(norm)
        # Also accept the raw request URL origin when proxy scheme differs
        url = getattr(request, "url", None)
        if url is not None:
            raw_norm = _normalize_origin(f"{url.scheme}://{url.netloc}")
            if raw_norm:
                allowed.add(raw_norm)

    frontend = get_dalton_frontend_url()
    if frontend:
        norm = _normalize_origin(frontend)
        if norm:
            allowed.add(norm)

    for env_name in ("DALTON_AUTH_ALLOWED_ORIGINS", "CORS_ORIGINS"):
        raw = os.getenv(env_name, "")
        for item in raw.split(","):
            norm = _normalize_origin(item)
            if norm:
                allowed.add(norm)
    return allowed


def verify_cookie_csrf_origin(request: Request) -> None:
    """Verify Origin and/or Sec-Fetch-Site for cookie-authenticated state-changing POSTs.

    Policy:
    - If ``Sec-Fetch-Site`` is present and is NOT ``same-origin``, reject with HTTP 403
      (blocks ``cross-site``, ``same-site`` sibling subdomains, and ``none``).
    - Every cookie-authenticated refresh/logout request MUST carry either a valid
      allowed ``Origin`` header OR ``Sec-Fetch-Site: same-origin``; requests carrying
      neither are rejected with HTTP 403.
    - When ``Origin`` is present, it must match the allowed origins set.
    - When ``Referer`` is present without ``Origin``, its origin must also be allowed.
    """
    sec_fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if sec_fetch_site and sec_fetch_site != "same-origin":
        raise HTTPException(
            status_code=403,
            detail="Cross-site or sibling-subdomain cookie request rejected",
        )

    origin_hdr = request.headers.get("origin")
    referer_hdr = request.headers.get("referer")
    allowed = _allowed_cookie_origins(request)

    if origin_hdr is not None:
        norm_origin = _normalize_origin(origin_hdr)
        if not norm_origin or norm_origin not in allowed:
            raise HTTPException(
                status_code=403,
                detail="Request Origin is not allowed for cookie authentication",
            )
        return

    if sec_fetch_site != "same-origin":
        raise HTTPException(
            status_code=403,
            detail="Missing Origin or Sec-Fetch-Site: same-origin header for cookie authentication",
        )

    if referer_hdr is not None:
        norm_referer = _normalize_origin(referer_hdr)
        if not norm_referer or norm_referer not in allowed:
            raise HTTPException(
                status_code=403,
                detail="Request Referer origin is not allowed for cookie authentication",
            )


def set_refresh_cookie(
    response: Response,
    request: Request,
    refresh_token: str,
    *,
    max_age: int | None = None,
) -> None:
    """Attach the refresh token cookie with strict security attributes."""
    ttl = get_refresh_session_ttl_seconds() if max_age is None else int(max_age)
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=ttl,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
    )


def clear_refresh_cookie(response: Response, request: Request) -> None:
    """Clear the refresh token cookie using matching path and security attributes."""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
    )
