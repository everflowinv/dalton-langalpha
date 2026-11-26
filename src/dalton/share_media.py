"""Controlled session media serving, multi-bucket resolution, and private ACL helpers (AU1).

Provides:
- ``resolve_active_storage_targets()``: reuses runtime storage resolution across all
  3 logical storage backends (upstream ``STORAGE_*``/``S3_*``/``AWS_*``/``OSS_*``,
  Dalton chat-upload/channel-snapshot bucket, and independent ASR transcription bucket)
  and deduplicates by normalized physical bucket.
- Private ACL helpers with safe fallback on ``BucketOwnerEnforced`` / R2 stores.
- Dual-read/delete helpers for ``private-media/`` migration continuity.
- ``dalton_oss_media_session`` Cookie minting with 3-factor trust gate and
  cross-port origin isolation in single-user ``oss`` mode.
- Multi-user JWT + thread-access media authorization.
- Owner media router (``GET /api/v1/threads/{thread_id}/media/{media_token}``) and
  public shared media router (``GET /api/v1/public/shared/{share_token}/media/{media_ref}``).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import dict_row

from src.dalton.auth.dispatch import (
    _normalize_origin_str,
    _scope_headers_map,
    is_trusted_oss_local_owner,
)
from src.dalton.settings import (
    get_dalton_auth_mode,
    get_dalton_bridge_admin_token,
    get_internal_service_token,
    is_dalton_multiuser_auth_enabled,
)
from src.utils.mime import resolve_content_type

logger = logging.getLogger(__name__)

PRIVATE_MEDIA_PREFIX = "private-media/"
OSS_MEDIA_SESSION_COOKIE = "dalton_oss_media_session"
MEDIA_TOKEN_TTL_SECONDS = 3600
_MEDIA_SANDBOX_CSP = "default-src 'none'; img-src 'self' data:; style-src 'none'; script-src 'none'; sandbox"
_ACL_UNSUPPORTED_CODES = frozenset(
    {
        "AccessControlListNotSupported",
        "NotImplemented",
        "InvalidRequest",
        "InvalidArgument",
    }
)

_optional_bearer = HTTPBearer(auto_error=False)

class NoStorePublicAPIRoute(APIRoute):
    """Ensure every public share response and HTTPException carries ``Cache-Control: no-store``."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def _no_store_handler(request: Any) -> Any:
            try:
                resp = await original(request)
            except HTTPException as exc:
                hdrs = dict(exc.headers or {})
                hdrs.setdefault("Cache-Control", "no-store")
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=exc.detail,
                    headers=hdrs,
                ) from None
            resp.headers["Cache-Control"] = "no-store"
            return resp

        return _no_store_handler


owner_media_router = APIRouter(prefix="/api/v1/threads", tags=["Thread Media"])
public_media_router = APIRouter(tags=["Public Sharing"], route_class=NoStorePublicAPIRoute)

# Process-local ephemeral fallback secret when no persistent signing secret is configured.
_EPHEMERAL_MEDIA_SECRET = secrets.token_hex(32)


# =============================================================================
# PRIVATE ACL & PRIVATE-MEDIA KEY HELPERS
# =============================================================================

def is_public_avatar_key(key: str) -> bool:
    """Return True iff ``key`` targets the public ``avatars/`` prefix."""
    cleaned = str(key or "").strip().lstrip("/").lower()
    if cleaned.startswith("avatars/"):
        return True
    raw_prefix = (os.getenv("STORAGE_KEY_PREFIX") or "").strip().strip("/").lower()
    if raw_prefix and cleaned.startswith(f"{raw_prefix}/avatars/"):
        return True
    return False


def _is_object_acl_disabled(*, endpoint_url: str | None = None) -> bool:
    for env_name in ("DALTON_STORAGE_DISABLE_OBJECT_ACL", "STORAGE_DISABLE_OBJECT_ACL"):
        raw = (os.getenv(env_name) or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
    ep = (endpoint_url or "").strip().lower()
    if "r2.cloudflarestorage.com" in ep:
        return True
    return False


def storage_private_acl_kwargs(
    key: str,
    *,
    provider: str = "s3",
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Return object-level private ACL kwargs for non-avatar keys."""
    if is_public_avatar_key(key):
        return {}
    if _is_object_acl_disabled(endpoint_url=endpoint_url):
        return {}
    if provider == "oss":
        return {"acl": "private"}
    return {"ACL": "private"}


def is_acl_not_supported_error(exc: BaseException) -> bool:
    """Return True when S3/R2/OSS refuses object ACLs (e.g. ``BucketOwnerEnforced``)."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, Mapping):
        code = str((resp.get("Error") or {}).get("Code") or "")
        msg = str((resp.get("Error") or {}).get("Message") or "").lower()
        if code in _ACL_UNSUPPORTED_CODES:
            if code in ("InvalidRequest", "InvalidArgument"):
                return "acl" in msg or "access control list" in msg
            return True
    # Alibabacloud OSS SDK OssError / ServiceError attributes
    for attr in ("code", "error_code"):
        raw_code = str(getattr(exc, attr, "") or "")
        if raw_code in _ACL_UNSUPPORTED_CODES or raw_code in ("InvalidObjectAcl", "PutObjectAclNotAllowed"):
            if raw_code in ("InvalidRequest", "InvalidArgument"):
                raw_msg = str(getattr(exc, "message", "") or exc).lower()
                return "acl" in raw_msg or "access control list" in raw_msg
            return True
    low = str(exc).lower()
    return (
        "accesscontrollistnotsupported" in low
        or "invalidobjectacl" in low
        or ("bucket does not allow acls" in low)
        or ("acl" in low and "not supported" in low)
    )


def to_private_media_key(key: str) -> str:
    """Prefix ``key`` with ``private-media/`` if not already prefixed."""
    cleaned = str(key or "").strip().lstrip("/")
    if not cleaned:
        return cleaned
    if cleaned.startswith(PRIVATE_MEDIA_PREFIX):
        return cleaned
    return f"{PRIVATE_MEDIA_PREFIX}{cleaned}"


def strip_private_media_prefix(key: str) -> str:
    """Strip leading ``private-media/`` prefix when present."""
    cleaned = str(key or "").strip().lstrip("/")
    if cleaned.startswith(PRIVATE_MEDIA_PREFIX):
        return cleaned[len(PRIVATE_MEDIA_PREFIX) :]
    return cleaned


_MIGRABLE_UPSTREAM_PREFIXES = ("response-images/", "attachments/", "widgets/")


def candidate_upstream_keys(key: str) -> tuple[str, ...]:
    """Return ordered keys to try for dual-read continuity across ``private-media/`` migration."""
    cleaned = str(key or "").strip().lstrip("/")
    if not cleaned:
        return ()
    if cleaned.startswith(PRIVATE_MEDIA_PREFIX):
        legacy = strip_private_media_prefix(cleaned)
        return (cleaned, legacy) if legacy and legacy != cleaned else (cleaned,)
    if cleaned.startswith(_MIGRABLE_UPSTREAM_PREFIXES):
        return (to_private_media_key(cleaned), cleaned)
    return (cleaned,)


def read_controlled_object_bytes(key: str, *, reader: Any = None) -> bytes | None:
    """Read upstream object bytes checking ``private-media/{key}`` first, falling back to legacy key."""
    if reader is None:
        from src.utils.storage import get_bytes as reader

    for candidate in candidate_upstream_keys(key):
        data = reader(candidate)
        if data is not None:
            return data
    return None


def delete_controlled_object(key: str) -> bool:
    """Idempotently delete both ``private-media/{key}`` and legacy key from upstream store."""
    from src.utils.storage import delete_object

    deleted_any = False
    for candidate in candidate_upstream_keys(key):
        if delete_object(candidate):
            deleted_any = True
    return deleted_any


# =============================================================================
# THREE-BACKEND PHYSICAL BUCKET DEDUPLICATION RESOLVER
# =============================================================================

@dataclass(frozen=True)
class ActiveStorageTarget:
    """Deduplicated physical storage bucket resolved from active runtime configs."""

    provider: str  # "s3" | "oss"
    bucket_name: str
    endpoint_url: str
    signing_endpoint_url: str
    region: str
    addressing_style: str
    access_key_id: str
    secret_access_key: str
    roles: tuple[str, ...]  # subset of ("upstream", "chat_upload", "transcription")
    key_prefixes: tuple[str, ...]
    sensitive_prefixes: tuple[str, ...]
    public_url_bases: tuple[str, ...]


def _norm_prefix(raw: str | None) -> str:
    cleaned = str(raw or "").strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


def _resolve_upstream_runtime_config() -> dict[str, Any] | None:
    """Resolve active upstream storage config mirroring ``src/utils/storage`` & ``s3_client.py``."""
    from src.utils.storage import _load_storage_setting

    provider = _load_storage_setting("provider", "STORAGE_PROVIDER", "none")
    if provider == "none":
        # Also allow env-only bucket detection if STORAGE_PROVIDER was set dynamically in env
        env_prov = (os.getenv("STORAGE_PROVIDER") or "").strip().lower()
        if env_prov and env_prov != "none":
            provider = env_prov
        else:
            # Check legacy S3_* / AWS_* fallback if STORAGE_PROVIDER is unset and bucket is configured
            if os.getenv("STORAGE_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME"):
                provider = "s3"
            elif os.getenv("OSS_BUCKET_NAME"):
                provider = "oss"
            else:
                return None

    key_prefix = _norm_prefix(os.getenv("STORAGE_KEY_PREFIX"))

    if provider == "oss":
        bucket = (os.getenv("OSS_BUCKET_NAME") or "").strip()
        if not bucket:
            return None
        endpoint = (os.getenv("OSS_ENDPOINT") or "").strip().rstrip("/")
        region = (os.getenv("OSS_REGION") or "").strip()
        if endpoint:
            if "://" in endpoint:
                scheme, rest = endpoint.split("://", 1)
                public_base = (
                    endpoint
                    if rest.lower().startswith(f"{bucket.lower()}.")
                    else f"{scheme}://{bucket}.{rest}"
                )
            else:
                public_base = f"https://{bucket}.{endpoint}"
        else:
            public_base = ""
        return {
            "provider": "oss",
            "bucket_name": bucket,
            "endpoint_url": endpoint,
            "signing_endpoint_url": endpoint,
            "region": region,
            "addressing_style": "virtual",
            "access_key_id": (os.getenv("OSS_ACCESS_KEY_ID") or "").strip(),
            "secret_access_key": (os.getenv("OSS_ACCESS_KEY_SECRET") or "").strip(),
            "key_prefix": key_prefix,
            "public_url_base": public_base.rstrip("/"),
        }

    # S3-compatible (STORAGE_* with legacy S3_* / AWS_* fallback, matching s3_client.py)
    bucket = (os.getenv("STORAGE_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME") or "").strip()
    if not bucket:
        return None
    region = (os.getenv("STORAGE_REGION") or os.getenv("S3_REGION") or "us-east-1").strip()
    endpoint = (os.getenv("STORAGE_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "").strip().rstrip("/")
    browser_ep = (os.getenv("STORAGE_BROWSER_ENDPOINT_URL") or "").strip().rstrip("/")
    public_base = (os.getenv("STORAGE_PUBLIC_URL_BASE") or os.getenv("S3_PUBLIC_URL_BASE") or "").strip().rstrip("/")
    style = (os.getenv("STORAGE_ADDRESSING_STYLE") or "virtual").strip().lower()
    if style not in ("virtual", "path", "auto"):
        style = "virtual"
    if not public_base:
        if endpoint:
            if style == "path":
                public_base = f"{endpoint}/{bucket}"
            elif "://" in endpoint:
                scheme, rest = endpoint.split("://", 1)
                public_base = (
                    endpoint
                    if rest.lower().startswith(f"{bucket.lower()}.")
                    else f"{scheme}://{bucket}.{rest}"
                )
            else:
                public_base = f"https://{bucket}.{endpoint}"
        else:
            public_base = f"https://{bucket}.s3.{region}.amazonaws.com"
    return {
        "provider": "s3",
        "bucket_name": bucket,
        "endpoint_url": endpoint,
        "signing_endpoint_url": browser_ep or endpoint,
        "region": region,
        "addressing_style": style,
        "access_key_id": (
            os.getenv("STORAGE_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or ""
        ).strip(),
        "secret_access_key": (
            os.getenv("STORAGE_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or ""
        ).strip(),
        "key_prefix": key_prefix,
        "public_url_base": public_base.rstrip("/"),
    }


def _dedup_bucket_key(provider: str, endpoint_url: str, bucket_name: str) -> tuple[str, str, str]:
    ep_norm = (endpoint_url or "").strip().lower().rstrip("/")
    return (provider.lower(), ep_norm, bucket_name.strip())


def resolve_active_storage_targets() -> list[ActiveStorageTarget]:
    """Resolve and deduplicate all active physical storage buckets (max 3).

    Independently inspects:
    1. Upstream primary store (``STORAGE_*`` / legacy ``S3_*``+``AWS_*`` / ``OSS_*``)
    2. Dalton chat-upload & channel-snapshot store (dedicated ``DALTON_CHAT_UPLOAD_STORAGE_*``
       or fallback ``DALTON_ASR_STORAGE_*``)
    3. Independent transcription ASR store (``DALTON_ASR_STORAGE_*``, always resolved when
       configured even if dedicated chat-upload storage is also configured)
    """
    from src.dalton import chat_upload_storage as chat_store
    from src.dalton import transcription_storage as asr_store

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _register(
        *,
        role: str,
        provider: str,
        bucket_name: str,
        endpoint_url: str,
        signing_endpoint_url: str,
        region: str,
        addressing_style: str,
        access_key_id: str,
        secret_access_key: str,
        key_prefix: str,
        sensitive_subprefixes: tuple[str, ...],
        public_url_base: str,
    ) -> None:
        if not bucket_name:
            return
        k = _dedup_bucket_key(provider, endpoint_url, bucket_name)
        pref = _norm_prefix(key_prefix)
        full_sensitive = [f"{pref}{sub}" for sub in sensitive_subprefixes]
        entry = merged.get(k)
        if entry is None:
            merged[k] = {
                "provider": provider,
                "bucket_name": bucket_name,
                "endpoint_url": endpoint_url,
                "signing_endpoint_url": signing_endpoint_url or endpoint_url,
                "region": region,
                "addressing_style": addressing_style,
                "access_key_id": access_key_id,
                "secret_access_key": secret_access_key,
                "roles": [role],
                "key_prefixes": [pref],
                "sensitive_prefixes": list(full_sensitive),
                "public_url_bases": [public_url_base] if public_url_base else [],
            }
        else:
            if role not in entry["roles"]:
                entry["roles"].append(role)
            if pref not in entry["key_prefixes"]:
                entry["key_prefixes"].append(pref)
            for sp in full_sensitive:
                if sp not in entry["sensitive_prefixes"]:
                    entry["sensitive_prefixes"].append(sp)
            if public_url_base and public_url_base not in entry["public_url_bases"]:
                entry["public_url_bases"].append(public_url_base)

    # 1. Upstream primary bucket
    up = _resolve_upstream_runtime_config()
    if up is not None:
        _register(
            role="upstream",
            provider=up["provider"],
            bucket_name=up["bucket_name"],
            endpoint_url=up["endpoint_url"],
            signing_endpoint_url=up["signing_endpoint_url"],
            region=up["region"],
            addressing_style=up["addressing_style"],
            access_key_id=up["access_key_id"],
            secret_access_key=up["secret_access_key"],
            key_prefix=up["key_prefix"],
            sensitive_subprefixes=(
                "private-media/",
                "blobs/",
                "uploads/",
                "provenance/",
                "memo/",
                "user-skills/",
                "org-library/",
                "response-images/",
                "attachments/",
                "widgets/",
            ),
            public_url_base=up["public_url_base"],
        )

    # 2. Dalton chat-upload & channel-snapshot bucket
    if chat_store.is_chat_upload_storage_configured():
        ep = chat_store._endpoint_url()
        bucket = chat_store._bucket_name()
        style = chat_store._addressing_style()
        pub_base = f"{ep}/{bucket}" if ep and style == "path" else ep
        _register(
            role="chat_upload",
            provider="s3",
            bucket_name=bucket,
            endpoint_url=ep,
            signing_endpoint_url=chat_store.signing_endpoint_url(),
            region=chat_store._region(),
            addressing_style=style,
            access_key_id=chat_store._access_key_id(),
            secret_access_key=chat_store._secret_access_key(),
            key_prefix=chat_store._key_prefix(),
            sensitive_subprefixes=(
                "private-media/",
                "channel-snapshots/",
                "chat-uploads/",
            ),
            public_url_base=pub_base.rstrip("/") if pub_base else "",
        )

    # 3. Independent transcription ASR bucket (always evaluated independently)
    if asr_store.is_transcription_storage_configured():
        ep = asr_store._endpoint_url()
        bucket = asr_store._bucket_name()
        style = asr_store._addressing_style()
        pub_base = f"{ep}/{bucket}" if ep and style == "path" else ep
        _register(
            role="transcription",
            provider="s3",
            bucket_name=bucket,
            endpoint_url=ep,
            signing_endpoint_url=asr_store.signing_endpoint_url(),
            region=asr_store._region(),
            addressing_style=style,
            access_key_id=asr_store._access_key_id(),
            secret_access_key=asr_store._secret_access_key(),
            key_prefix=asr_store._key_prefix(),
            sensitive_subprefixes=(
                "transcriptions/",
                "uploads/",
            ),
            public_url_base=pub_base.rstrip("/") if pub_base else "",
        )

    return [
        ActiveStorageTarget(
            provider=v["provider"],
            bucket_name=v["bucket_name"],
            endpoint_url=v["endpoint_url"],
            signing_endpoint_url=v["signing_endpoint_url"],
            region=v["region"],
            addressing_style=v["addressing_style"],
            access_key_id=v["access_key_id"],
            secret_access_key=v["secret_access_key"],
            roles=tuple(v["roles"]),
            key_prefixes=tuple(v["key_prefixes"]),
            sensitive_prefixes=tuple(v["sensitive_prefixes"]),
            public_url_bases=tuple(v["public_url_bases"]),
        )
        for v in merged.values()
    ]


# =============================================================================
# MEDIA SIGNING, OSS MEDIA SESSION COOKIE & TRUSTED ORIGIN GATE
# =============================================================================

def _media_hmac_key() -> bytes:
    seed = (
        (os.getenv("DALTON_MEDIA_SIGNING_SECRET") or "").strip()
        or (os.getenv("JWT_SIGNING_SECRET") or "").strip()
        or (os.getenv("DALTON_CHAT_UPLOAD_SIGNING_SECRET") or "").strip()
        or get_internal_service_token()
        or get_dalton_bridge_admin_token()
        or _EPHEMERAL_MEDIA_SECRET
    )
    return hmac.new(seed.encode("utf-8"), b"dalton-share-media-v1", hashlib.sha256).digest()


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sha256_hex(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def get_trusted_app_origins() -> frozenset[str]:
    """Build exact ``scheme://host[:port]`` set of trusted primary app origins."""
    from src.config import env
    from src.config.settings import get_allowed_origins

    origins: set[str] = set()
    for candidate in (
        getattr(env, "PUBLIC_APP_URL", ""),
        getattr(env, "SERVER_BASE_URL", ""),
        *(get_allowed_origins() or ()),
        *(os.getenv("DALTON_TRUSTED_APP_ORIGINS", "").split(",")),
    ):
        cand = str(candidate or "").strip()
        if not cand or "*" in cand:
            continue
        norm = _normalize_origin_str(cand)
        if norm:
            origins.add(norm)
    return frozenset(origins)


def _request_own_origin(request: Request) -> str | None:
    headers = _scope_headers_map(request.scope)
    host = headers.get("host", "").strip()
    scheme = str(request.scope.get("scheme") or "http").lower()
    if not host:
        return None
    return _normalize_origin_str(f"{scheme}://{host}")


def _extract_caller_origin(request: Request) -> tuple[str | None, bool]:
    """Return ``(normalized_origin, was_explicit_origin_header)``."""
    raw_origin = request.headers.get("origin")
    if raw_origin is not None and raw_origin.strip() != "":
        return _normalize_origin_str(raw_origin), True
    raw_referer = request.headers.get("referer")
    if raw_referer:
        return _normalize_origin_str(raw_referer), False
    return None, False


def can_mint_oss_media_session(request: Request) -> tuple[bool, str | None]:
    """Enforce the 3-factor Minting Trust Gate for ``dalton_oss_media_session``.

    Returns ``(allowed, bound_origin)``.
    """
    from src.server.auth import owner_requests

    if not is_trusted_oss_local_owner(request.scope):
        return False, None

    req_origin = _request_own_origin(request)
    trusted = set(get_trusted_app_origins())
    if req_origin:
        trusted.add(req_origin)

    # Desktop app carrying valid X-LangAlpha-Owner is trusted directly
    owner_secret = os.environ.get(owner_requests.OWNER_SECRET_ENV, "")
    has_valid_owner_secret = bool(owner_secret) and owner_requests.is_owner(request.scope)

    caller_origin, has_explicit_origin = _extract_caller_origin(request)
    raw_origin_hdr = request.headers.get("origin")
    if raw_origin_hdr is not None and raw_origin_hdr.strip().lower() == "null":
        return False, None

    if has_valid_owner_secret and caller_origin is None:
        return True, req_origin or ""

    if not caller_origin or caller_origin not in trusted:
        return False, None

    # Fetch Metadata check
    sfs = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sfs:
        if caller_origin == req_origin:
            if sfs != "same-origin":
                return False, None
        else:
            if sfs not in ("same-origin", "same-site") or not has_explicit_origin:
                return False, None

    return True, caller_origin


def _sign_oss_media_cookie(nonce: str) -> str:
    sig = hmac.new(_media_hmac_key(), f"oss-sid:{nonce}".encode("utf-8"), hashlib.sha256).digest()
    return f"{nonce}.{_b64u_encode(sig)}"


def _verify_oss_media_cookie(cookie_val: str | None) -> str | None:
    if not cookie_val or "." not in cookie_val:
        return None
    nonce, sig_b64 = cookie_val.split(".", 1)
    if not nonce or not sig_b64:
        return None
    expected = hmac.new(
        _media_hmac_key(), f"oss-sid:{nonce}".encode("utf-8"), hashlib.sha256
    ).digest()
    try:
        supplied = _b64u_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(supplied, expected):
        return None
    return cookie_val


def mint_oss_media_session_cookie(
    request: Request, response: Response
) -> tuple[str, str] | None:
    """Mint (or reuse) ``dalton_oss_media_session`` Cookie in single-user ``oss`` mode.

    Returns ``(sid_hash, origin_hash)`` on success, or ``None`` if the Minting Trust
    Gate refuses the request (sets zero cookies on refusal).
    """
    if is_dalton_multiuser_auth_enabled() or get_dalton_auth_mode() != "oss":
        return None

    allowed, bound_origin = can_mint_oss_media_session(request)
    if not allowed:
        return None

    existing = _verify_oss_media_cookie(request.cookies.get(OSS_MEDIA_SESSION_COOKIE))
    cookie_val = existing or _sign_oss_media_cookie(secrets.token_urlsafe(24))
    is_https = str(request.scope.get("scheme") or "http").lower() == "https"
    response.set_cookie(
        key=OSS_MEDIA_SESSION_COOKIE,
        value=cookie_val,
        path="/api/v1/threads",
        httponly=True,
        samesite="strict",
        secure=is_https,
    )
    sid_hash = _sha256_hex(cookie_val)
    origin_hash = _sha256_hex(bound_origin) if bound_origin else ""
    return sid_hash, origin_hash


def mint_owner_media_token(
    *,
    thread_id: str,
    viewer_id: str,
    backend: str,
    ref_id: str,
    sid_hash: str = "",
    origin_hash: str = "",
    ttl_seconds: int = MEDIA_TOKEN_TTL_SECONDS,
    now_ts: int | None = None,
) -> str:
    """Mint HMAC-signed owner media token bound to viewer, session cookie, origin, and object."""
    now = int(now_ts if now_ts is not None else time.time())
    payload = {
        "v": 1,
        "typ": "dalton_owner_media",
        "tid": str(thread_id),
        "vid": str(viewer_id),
        "sid": str(sid_hash or ""),
        "org": str(origin_hash or ""),
        "b": str(backend),
        "ref": str(ref_id),
        "exp": now + max(60, int(ttl_seconds)),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    p_b64 = _b64u_encode(raw)
    sig = hmac.new(_media_hmac_key(), p_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{p_b64}.{_b64u_encode(sig)}"


def verify_owner_media_token(
    token: str, *, expected_thread_id: str, now_ts: int | None = None
) -> dict[str, Any]:
    """Verify HMAC signature, expiry, and thread binding of an owner media token."""
    cleaned = str(token or "").strip()
    parts = cleaned.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(status_code=401, detail="Invalid media token")
    p_b64, s_b64 = parts
    expected = hmac.new(_media_hmac_key(), p_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64u_decode(s_b64)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid media token") from None
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid media token")
    try:
        claims = json.loads(_b64u_decode(p_b64).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid media token") from None
    if (
        not isinstance(claims, dict)
        or claims.get("v") != 1
        or claims.get("typ") != "dalton_owner_media"
        or str(claims.get("tid") or "") != str(expected_thread_id)
    ):
        raise HTTPException(status_code=401, detail="Invalid media token")
    now = int(now_ts if now_ts is not None else time.time())
    if not isinstance(claims.get("exp"), int) or now >= claims["exp"]:
        raise HTTPException(status_code=401, detail="Media token expired")
    return claims


def mint_public_media_ref(
    *,
    thread_id: str,
    backend: str,
    ref_id: str,
) -> str:
    """Mint a thread-bound opaque ``media_ref`` for ``allow_files=True`` shared media routes."""
    payload = {
        "v": 1,
        "typ": "dalton_pub_media",
        "tid": str(thread_id),
        "b": str(backend),
        "ref": str(ref_id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    p_b64 = _b64u_encode(raw)
    sig = hmac.new(_media_hmac_key(), p_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{p_b64}.{_b64u_encode(sig)}"


def verify_public_media_ref(media_ref: str, *, expected_thread_id: str) -> dict[str, Any]:
    """Verify opaque ``media_ref`` binding to ``expected_thread_id``."""
    _no_store = {"Cache-Control": "no-store"}
    cleaned = str(media_ref or "").strip()
    parts = cleaned.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(status_code=404, detail="Media not found", headers=_no_store)
    p_b64, s_b64 = parts
    expected = hmac.new(_media_hmac_key(), p_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64u_decode(s_b64)
    except Exception:
        raise HTTPException(status_code=404, detail="Media not found", headers=_no_store) from None
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=404, detail="Media not found", headers=_no_store)
    try:
        claims = json.loads(_b64u_decode(p_b64).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=404, detail="Media not found", headers=_no_store) from None
    if (
        not isinstance(claims, dict)
        or claims.get("v") != 1
        or claims.get("typ") != "dalton_pub_media"
        or str(claims.get("tid") or "") != str(expected_thread_id)
    ):
        raise HTTPException(status_code=404, detail="Media not found", headers=_no_store)
    return claims


async def require_media_viewer(
    request: Request,
    thread_id: str,
    claims: Mapping[str, Any],
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    """Enforce owner media viewer authentication across ``supabase``, ``local``, and ``oss``."""
    from src.server.utils.api import get_current_user_id

    if is_dalton_multiuser_auth_enabled():
        if credentials is None or not credentials.credentials:
            raise HTTPException(status_code=401, detail="Missing authentication")
        viewer_id = await get_current_user_id(request, credentials)
        if str(viewer_id) != str(claims.get("vid") or ""):
            raise HTTPException(status_code=403, detail="Forbidden")
        await _verify_viewer_thread_read(thread_id, viewer_id)
        return viewer_id

    # Single-user OSS mode: 3-condition cross-port origin isolation
    if not is_trusted_oss_local_owner(request.scope):
        raise HTTPException(status_code=401, detail="Unauthorized")

    cookie_val = _verify_oss_media_cookie(request.cookies.get(OSS_MEDIA_SESSION_COOKIE))
    expected_sid = str(claims.get("sid") or "")
    if not cookie_val or not expected_sid or not hmac.compare_digest(_sha256_hex(cookie_val), expected_sid):
        raise HTTPException(status_code=401, detail="Invalid media session cookie")

    req_origin = _request_own_origin(request)
    trusted = set(get_trusted_app_origins())
    if req_origin:
        trusted.add(req_origin)

    caller_origin, has_explicit_origin = _extract_caller_origin(request)
    expected_org = str(claims.get("org") or "")
    sfs = (request.headers.get("sec-fetch-site") or "").strip().lower()
    sfm = (request.headers.get("sec-fetch-mode") or "").strip().lower()

    if sfs:
        if sfs == "same-origin":
            if caller_origin is not None:
                if caller_origin not in trusted:
                    raise HTTPException(status_code=401, detail="Untrusted media origin")
                if expected_org and not hmac.compare_digest(_sha256_hex(caller_origin), expected_org):
                    raise HTTPException(status_code=401, detail="Media origin mismatch")
        elif sfs == "same-site":
            if sfm != "cors" or not has_explicit_origin or not caller_origin or caller_origin not in trusted:
                raise HTTPException(status_code=401, detail="Cross-port media requires trusted CORS origin")
            if expected_org and not hmac.compare_digest(_sha256_hex(caller_origin), expected_org):
                raise HTTPException(status_code=401, detail="Media origin mismatch")
        else:
            raise HTTPException(status_code=401, detail="Cross-site media access refused")
    else:
        # B5: client omitting Sec-Fetch-* must still present a trusted Origin or same-origin Referer
        if not caller_origin or caller_origin not in trusted:
            raise HTTPException(status_code=401, detail="Trusted Origin or Referer required")
        if expected_org and not hmac.compare_digest(_sha256_hex(caller_origin), expected_org):
            raise HTTPException(status_code=401, detail="Media origin mismatch")

    return str(claims.get("vid") or "")


async def _verify_viewer_thread_read(thread_id: str, viewer_id: str) -> None:
    from src.server.database.conversation import get_thread_by_id
    from src.server.database.shared_workspaces import is_holder
    from src.server.services.shared_workspaces.access import thread_access

    thread = await get_thread_by_id(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    owner_id = str(thread.get("user_id") or thread.get("owner_id") or "")
    if is_holder(owner_id):
        if owner_id != str(viewer_id):
            await thread_access(thread_id, viewer_id)
        return
    if is_dalton_multiuser_auth_enabled():
        from src.dalton.sharing import require_thread_access

        await require_thread_access(
            thread_id,
            viewer_id,
            channel="web",
            need="read",
            owner_id=owner_id or None,
            workspace_owner_id=thread.get("workspace_owner_id"),
            workspace_id=thread.get("workspace_id"),
        )
    elif owner_id and owner_id != str(viewer_id):
        raise HTTPException(status_code=403, detail="Forbidden")


# =============================================================================
# THREAD MEDIA BINDINGS & URL REWRITING (ALLOW_FILES=TRUE & OWNER REPLAY)
# =============================================================================

_UPSTREAM_KEY_EXTRACT_RE = re.compile(
    r"/(private-media/(?:response-images|attachments|widgets)/[^?#\s\"'<>)]+|(?:response-images|attachments|widgets)/[^?#\s\"'<>)]+)",
    re.IGNORECASE,
)


def extract_upstream_key_from_url(url: str) -> str | None:
    """Extract logical upstream object key from a stored public/CDN URL."""
    if not url:
        return None
    try:
        path = unquote(urlsplit(url).path or "")
    except Exception:
        return None
    m = _UPSTREAM_KEY_EXTRACT_RE.search(path)
    if not m:
        return None
    return m.group(1).lstrip("/")


async def _fetch_ready_chat_uploads(
    workspace_id: str, upload_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    ids = list({str(u).strip() for u in upload_ids if str(u or "").strip()})
    if not ids or not workspace_id:
        return {}
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT upload_id::text AS upload_id,
                       workspace_id::text AS workspace_id,
                       object_key,
                       safe_filename,
                       mime_type,
                       state,
                       cleaned_at
                FROM dalton_chat_file_uploads
                WHERE workspace_id = %s::uuid
                  AND upload_id = ANY(%s::uuid[])
                  AND state = 'ready'
                  AND cleaned_at IS NULL
                """,
                (str(workspace_id), ids),
            )
            rows = await cur.fetchall()
    return {str(r["upload_id"]): dict(r) for r in rows}


def _is_safe_object_key(key: str) -> bool:
    cleaned = str(key or "").strip().lstrip("/")
    if not cleaned or "\x00" in cleaned or "\\" in cleaned:
        return False
    parts = cleaned.split("/")
    return all(p and p not in (".", "..") for p in parts)


def _allowed_storage_origins() -> set[str]:
    origins: set[str] = set()
    try:
        targets = resolve_active_storage_targets()
    except Exception:
        return origins
    for t in targets:
        for raw_u in (t.endpoint_url, t.signing_endpoint_url, *t.public_url_bases):
            if not raw_u:
                continue
            cand = raw_u if "://" in raw_u else f"https://{raw_u}"
            norm = _normalize_origin_str(cand)
            if norm:
                origins.add(norm)
        if t.bucket_name and t.provider == "s3" and t.region:
            norm_aws = _normalize_origin_str(f"https://{t.bucket_name}.s3.{t.region}.amazonaws.com")
            if norm_aws:
                origins.add(norm_aws)
    return origins


def _is_url_from_configured_storage(url: str) -> bool:
    """Return True if ``url`` matches a configured storage origin (or no storage origins are configured)."""
    origins = _allowed_storage_origins()
    if not origins:
        return True
    norm = _normalize_origin_str(url)
    return bool(norm and norm in origins)


def _is_thread_prefixed_upstream_key(thread_id: str, key: str) -> bool:
    """Return True iff ``key`` is directly scoped under ``{prefix}/{thread_id}/...``."""
    if not thread_id or not _is_safe_object_key(key):
        return False
    cleaned = strip_private_media_prefix(key)
    for prefix in ("response-images/", "attachments/", "widgets/"):
        if cleaned.startswith(f"{prefix}{thread_id}/"):
            return True
    return False


def _extract_registered_query_attachment_keys_and_ids(
    queries: Iterable[Mapping[str, Any]],
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(normalized_keys, attachment_ids, upload_ids)`` from persisted query metadata."""
    keys: set[str] = set()
    att_ids: set[str] = set()
    upload_ids: set[str] = set()
    for q in queries:
        meta = q.get("metadata") if isinstance(q, Mapping) else None
        if not isinstance(meta, Mapping):
            continue
        for att in meta.get("attachments") or ():
            if not isinstance(att, Mapping):
                continue
            raw_url = att.get("url")
            if isinstance(raw_url, str) and raw_url.strip():
                k = extract_upstream_key_from_url(raw_url)
                if k and _is_safe_object_key(k):
                    keys.add(strip_private_media_prefix(k))
            raw_att_id = att.get("attachment_id") or att.get("id")
            if isinstance(raw_att_id, str) and raw_att_id.strip():
                att_ids.add(raw_att_id.strip())
            raw_uid = att.get("upload_id")
            if isinstance(raw_uid, str) and raw_uid.strip():
                upload_ids.add(raw_uid.strip())
    return keys, att_ids, upload_ids


async def _load_thread_query_metadata_rows(thread_id: str) -> list[dict[str, Any]]:
    """Load persisted ``conversation_queries`` metadata for ``thread_id``."""
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT metadata
                FROM conversation_queries
                WHERE conversation_thread_id = %s::uuid
                  AND metadata IS NOT NULL
                """,
                (str(thread_id),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def _verify_staged_attachment_owner(
    attachment_id: str,
    cleaned_key: str,
    *,
    owner_id: str | None = None,
) -> bool:
    """If a ``chat_attachments`` DB row exists for ``attachment_id``, verify key & owner match."""
    try:
        from src.server.database.chat_attachments import get_attachment

        row = await get_attachment(attachment_id)
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return True
        return False
    except Exception:
        return False

    if row is None:
        # Legacy inline/migrated attachment without a chat_attachments staging row:
        # thread query metadata membership was already verified by caller.
        return True
    row_key = strip_private_media_prefix(str(row.get("object_key") or ""))
    if row_key and row_key != cleaned_key:
        return False
    if owner_id and row.get("user_id") and str(row["user_id"]) != str(owner_id):
        return False
    return True


async def _is_attachment_bound_to_thread(
    thread_id: str,
    key: str,
    *,
    owner_id: str | None = None,
    queries: Iterable[Mapping[str, Any]] | None = None,
) -> bool:
    """Verify an ``attachments/*`` object key is bound to ``thread_id`` (fail closed)."""
    if not thread_id or not _is_safe_object_key(key):
        return False
    cleaned = strip_private_media_prefix(key)
    if not cleaned.startswith("attachments/"):
        return False
    if cleaned.startswith(f"attachments/{thread_id}/"):
        return True

    parts = cleaned.split("/")
    if len(parts) < 3:
        return False
    att_id = parts[1]

    if queries is None:
        try:
            query_rows = await _load_thread_query_metadata_rows(thread_id)
        except Exception:
            logger.debug("Attachment thread binding check failed for thread %s", thread_id, exc_info=True)
            return False
    else:
        query_rows = list(queries)

    reg_keys, reg_att_ids, _ = _extract_registered_query_attachment_keys_and_ids(query_rows)
    if cleaned not in reg_keys and att_id not in reg_att_ids:
        return False

    return await _verify_staged_attachment_owner(att_id, cleaned, owner_id=owner_id)


async def _is_chat_upload_bound_to_thread(
    thread_id: str,
    upload_id: str,
    *,
    queries: Iterable[Mapping[str, Any]] | None = None,
) -> bool:
    """Verify a Dalton ``chat_upload`` ``upload_id`` is registered on ``thread_id`` (fail closed)."""
    uid = str(upload_id or "").strip()
    if not thread_id or not uid:
        return False
    if queries is None:
        try:
            query_rows = await _load_thread_query_metadata_rows(thread_id)
        except Exception:
            logger.debug("Chat upload thread binding check failed for thread %s", thread_id, exc_info=True)
            return False
    else:
        query_rows = list(queries)

    _, _, reg_upload_ids = _extract_registered_query_attachment_keys_and_ids(query_rows)
    return uid in reg_upload_ids


async def resolve_thread_media_bindings(
    thread: Mapping[str, Any],
    queries: Iterable[Mapping[str, Any]],
    items: Iterable[Mapping[str, Any]],
    *,
    responses_by_turn: Mapping[Any, Mapping[str, Any]] | None = None,
    run_facts: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, tuple[str, str]]:
    """Collect verified URL/upload_id -> ``(backend, ref_id)`` bindings for a thread's media.

    Only signs media demonstrably registered to ``thread``:
    - Persisted ``queries[*].metadata.attachments`` URLs (verified storage origin + thread/attachment binding)
    - Persisted ``queries[*].metadata.attachments[*].upload_id`` (verified in ``dalton_chat_file_uploads``)
    - Server-captured assistant images under ``response-images/{thread_id}/...`` (from ``replay_facts.images``
      or assistant ``message_chunk`` content, verified against configured storage origins)

    Never signs arbitrary URLs pasted into user message text or foreign ``attachments/`` / ``widgets/`` keys.
    """
    thread_id = str(
        thread.get("conversation_thread_id") or thread.get("thread_id") or ""
    ).strip()
    if not thread_id:
        return {}
    owner_id = str(thread.get("user_id") or thread.get("owner_id") or "").strip() or None
    query_list = list(queries)

    bindings: dict[str, tuple[str, str]] = {}
    upload_ids: set[str] = set()

    async def _register_upstream_url(u: Any, *, allow_staged_attachment: bool) -> None:
        if not isinstance(u, str) or not u.strip():
            return
        if not _is_url_from_configured_storage(u):
            return
        key = extract_upstream_key_from_url(u)
        if not key or not _is_safe_object_key(key):
            return
        cleaned = strip_private_media_prefix(key)
        if cleaned.startswith("response-images/") or cleaned.startswith("widgets/"):
            if _is_thread_prefixed_upstream_key(thread_id, key):
                bindings[u] = ("upstream", key)
            return
        if cleaned.startswith("attachments/"):
            if cleaned.startswith(f"attachments/{thread_id}/"):
                bindings[u] = ("upstream", key)
            elif allow_staged_attachment and await _is_attachment_bound_to_thread(
                thread_id, key, owner_id=owner_id, queries=query_list
            ):
                bindings[u] = ("upstream", key)

    # 1. Server-registered query attachments (persisted in conversation_queries)
    for q in query_list:
        meta = q.get("metadata") if isinstance(q, Mapping) else None
        if isinstance(meta, Mapping):
            for att in meta.get("attachments") or ():
                if not isinstance(att, Mapping):
                    continue
                if att.get("url"):
                    await _register_upstream_url(att["url"], allow_staged_attachment=True)
                if att.get("upload_id"):
                    upload_ids.add(str(att["upload_id"]))

    # 2. Server-recorded captured sandbox images in replay_facts
    for container in (
        *(responses_by_turn.values() if isinstance(responses_by_turn, Mapping) else ()),
        *(run_facts or ()),
    ):
        if not isinstance(container, Mapping):
            continue
        facts = container.get("replay_facts") if "replay_facts" in container else container
        if not isinstance(facts, Mapping):
            continue
        imgs = facts.get("images")
        img_urls = imgs.values() if isinstance(imgs, Mapping) else (imgs if isinstance(imgs, list) else ())
        for u in img_urls:
            await _register_upstream_url(u, allow_staged_attachment=False)

    # 3. Replay items: assistant message_chunk may embed thread-scoped response-images/{thread_id}/...
    #    User messages & steering free text are NEVER scanned for arbitrary URLs.
    for item in items:
        if not isinstance(item, Mapping):
            continue
        event_type = item.get("event")
        data = item.get("data")
        if not isinstance(data, Mapping):
            continue
        if event_type == "message_chunk":
            role = data.get("role")
            if role in (None, "", "assistant"):
                content = data.get("content")
                if isinstance(content, str) and (
                    "response-images/" in content or "private-media/" in content
                ):
                    for m in re.finditer(r"https?://[^\s\"'<>)]+", content):
                        u = m.group(0)
                        k = extract_upstream_key_from_url(u)
                        if k and strip_private_media_prefix(k).startswith(
                            f"response-images/{thread_id}/"
                        ):
                            await _register_upstream_url(u, allow_staged_attachment=False)

    ws_id = str(thread.get("workspace_id") or "")
    if upload_ids and ws_id:
        try:
            ready_map = await _fetch_ready_chat_uploads(ws_id, upload_ids)
            for uid in ready_map:
                bindings[f"upload_id:{uid}"] = ("chat_upload", uid)
        except Exception:
            logger.debug("Could not resolve chat upload bindings for thread", exc_info=True)

    return bindings


def _rewrite_item_urls(
    item: dict[str, Any],
    url_map: Mapping[str, str],
    upload_id_map: Mapping[str, str],
) -> dict[str, Any]:
    if not url_map and not upload_id_map:
        return item
    data = item.get("data")
    if not isinstance(data, dict):
        return item
    copied = dict(item)
    new_data = dict(data)
    changed = False

    content = new_data.get("content")
    if isinstance(content, str) and url_map:
        new_content = content
        for old_url, new_url in url_map.items():
            if old_url in new_content:
                new_content = new_content.replace(old_url, new_url)
        if new_content != content:
            new_data["content"] = new_content
            changed = True

    meta = new_data.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("attachments"), list):
        new_atts = []
        att_changed = False
        for att in meta["attachments"]:
            if not isinstance(att, dict):
                new_atts.append(att)
                continue
            att_copy = dict(att)
            old_u = att_copy.get("url")
            if isinstance(old_u, str) and old_u in url_map:
                att_copy["url"] = url_map[old_u]
                att_changed = True
            elif not att_copy.get("url") and att_copy.get("upload_id"):
                uid = str(att_copy["upload_id"])
                if uid in upload_id_map:
                    att_copy["url"] = upload_id_map[uid]
                    att_changed = True
            new_atts.append(att_copy)
        if att_changed:
            new_data["metadata"] = {**meta, "attachments": new_atts}
            changed = True

    if changed:
        copied["data"] = new_data
        return copied
    return item


def rewrite_items_for_public_media(
    items: list[dict[str, Any]],
    *,
    thread_id: str,
    share_token: str,
    bindings: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Rewrite stored URLs and ``upload_id``-only attachments to ``/api/v1/public/shared/{token}/media/{ref}``."""
    if not bindings:
        return items
    url_map: dict[str, str] = {}
    upload_id_map: dict[str, str] = {}
    for key, (backend, ref_id) in bindings.items():
        ref = mint_public_media_ref(thread_id=thread_id, backend=backend, ref_id=ref_id)
        controlled = f"/api/v1/public/shared/{share_token}/media/{ref}"
        if key.startswith("upload_id:"):
            upload_id_map[key[len("upload_id:") :]] = controlled
        else:
            url_map[key] = controlled
    return [_rewrite_item_urls(it, url_map, upload_id_map) for it in items]


def rewrite_lines_for_owner_media(
    lines: list[Any],
    *,
    thread_id: str,
    viewer_id: str,
    bindings: Mapping[str, tuple[str, str]],
    sid_hash: str = "",
    origin_hash: str = "",
) -> list[Any]:
    """Rewrite stored media URLs in owner replay ``Line``s to ``/api/v1/threads/{id}/media/{token}``."""
    from src.server.services.history.replay.lines import Line, line_of

    if not bindings:
        return lines
    url_map: dict[str, str] = {}
    upload_id_map: dict[str, str] = {}
    for key, (backend, ref_id) in bindings.items():
        tok = mint_owner_media_token(
            thread_id=thread_id,
            viewer_id=viewer_id,
            backend=backend,
            ref_id=ref_id,
            sid_hash=sid_hash,
            origin_hash=origin_hash,
        )
        controlled = f"/api/v1/threads/{thread_id}/media/{tok}"
        if key.startswith("upload_id:"):
            upload_id_map[key[len("upload_id:") :]] = controlled
        else:
            url_map[key] = controlled

    out: list[Any] = []
    for ln in lines:
        if not isinstance(ln, Line):
            out.append(ln)
            continue
        if ln.event not in ("user_message", "message_chunk", "steering_delivered"):
            out.append(ln)
            continue
        needs = any(u in ln.data_json for u in url_map) or (
            bool(upload_id_map) and '"upload_id"' in ln.data_json
        )
        if not needs:
            out.append(ln)
            continue
        rewritten = _rewrite_item_urls(ln.item(), url_map, upload_id_map)
        out.append(line_of(rewritten))
    return out


async def prepare_owner_replay_media(
    request: Request | None,
    rows: Any,
    replay_lines: list[Any],
    *,
    thread_id: str,
    viewer_id: str,
) -> tuple[list[Any], list[str]]:
    """Mint ``dalton_oss_media_session`` cookie (when applicable) and rewrite owner media URLs.

    Thin-hook entry point for ``src/server/app/threads/messaging.py``. Returns
    ``(rewritten_lines, set_cookie_headers)``.
    """
    set_cookies: list[str] = []
    sid_hash, origin_hash = "", ""
    if request is not None:
        cookie_holder = Response()
        oss_session = mint_oss_media_session_cookie(request, cookie_holder)
        if oss_session:
            sid_hash, origin_hash = oss_session
        set_cookies = cookie_holder.headers.getlist("set-cookie")

    thread_dict = dict(rows.thread) if isinstance(getattr(rows, "thread", None), Mapping) else {}
    thread_dict.setdefault("conversation_thread_id", thread_id)
    queries = getattr(rows, "queries", None) or ()
    responses_by_turn = getattr(rows, "responses_by_turn", None)
    run_facts = getattr(rows, "run_facts", None)

    bindings = await resolve_thread_media_bindings(
        thread_dict,
        queries,
        [
            ln.item()
            for ln in replay_lines
            if ln.event in ("user_message", "message_chunk", "steering_delivered")
        ],
        responses_by_turn=responses_by_turn,
        run_facts=run_facts,
    )
    if bindings:
        replay_lines = rewrite_lines_for_owner_media(
            replay_lines,
            thread_id=thread_id,
            viewer_id=viewer_id,
            bindings=bindings,
            sid_hash=sid_hash,
            origin_hash=origin_hash,
        )
    return replay_lines, set_cookies


# =============================================================================
# STREAMING RESPONSE BUILDERS & MEDIA ENDPOINTS
# =============================================================================

async def _serve_media_payload(
    *,
    thread_id: str,
    workspace_id: str,
    thread_owner_id: str = "",
    backend: str,
    ref_id: str,
    cache_control: str,
) -> Response:
    base_headers = {
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _MEDIA_SANDBOX_CSP,
    }
    err_headers = {"Cache-Control": cache_control}

    if backend == "upstream":
        if not _is_safe_object_key(ref_id):
            raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        cleaned = strip_private_media_prefix(ref_id)
        if cleaned.startswith("response-images/"):
            if not cleaned.startswith(f"response-images/{thread_id}/"):
                raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        elif cleaned.startswith("widgets/"):
            if not cleaned.startswith(f"widgets/{thread_id}/"):
                raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        elif cleaned.startswith("attachments/"):
            bound = await _is_attachment_bound_to_thread(
                thread_id, cleaned, owner_id=thread_owner_id or None
            )
            if not bound:
                raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        else:
            raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)

        raw = await asyncio.to_thread(read_controlled_object_bytes, ref_id)
        if raw is None:
            raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        mime = resolve_content_type(cleaned)
        return Response(content=raw, media_type=mime, headers=base_headers)

    if backend == "chat_upload":
        from src.dalton import chat_upload_storage as chat_store

        bound = await _is_chat_upload_bound_to_thread(thread_id, ref_id)
        if not bound:
            raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        ready = await _fetch_ready_chat_uploads(workspace_id, [ref_id])
        row = ready.get(str(ref_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        resolved_key = await asyncio.to_thread(
            chat_store.resolve_existing_chat_upload_key, str(row["object_key"])
        )
        if not resolved_key:
            raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        mime = str(row.get("mime_type") or "") or resolve_content_type(
            str(row.get("safe_filename") or resolved_key)
        )

        def _chunks():
            yield from chat_store.iter_stored_object_chunks(resolved_key)

        return StreamingResponse(_chunks(), media_type=mime, headers=base_headers)

    raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)


@owner_media_router.get("/{thread_id}/media/{media_token}")
async def serve_owner_thread_media(
    thread_id: str,
    media_token: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> Response:
    """Serve controlled session media to an authenticated thread viewer."""
    from src.server.database.conversation import get_thread_by_id

    claims = verify_owner_media_token(media_token, expected_thread_id=thread_id)
    await require_media_viewer(request, thread_id, claims, credentials)

    thread = await get_thread_by_id(thread_id)
    if not thread:
        raise HTTPException(
            status_code=404,
            detail="Thread not found",
            headers={"Cache-Control": "private, no-store"},
        )
    return await _serve_media_payload(
        thread_id=thread_id,
        workspace_id=str(thread.get("workspace_id") or ""),
        thread_owner_id=str(thread.get("user_id") or thread.get("owner_id") or ""),
        backend=str(claims.get("b") or ""),
        ref_id=str(claims.get("ref") or ""),
        cache_control="private, no-store",
    )


@public_media_router.get("/shared/{share_token}/media/{media_ref}")
async def serve_shared_thread_media(
    share_token: str,
    media_ref: str,
) -> Response:
    """Serve controlled session media for an ``allow_files=True`` shared thread."""
    from src.dalton.share_redaction import resolve_effective_share_permissions
    from src.server.app.share_access import get_shared_thread

    try:
        thread = await get_shared_thread(share_token)
    except HTTPException as exc:
        hdrs = dict(exc.headers or {})
        hdrs.setdefault("Cache-Control", "no-store")
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=hdrs) from None

    eff = await resolve_effective_share_permissions(thread)
    if not eff.allow_files:
        raise HTTPException(
            status_code=403,
            detail="Permission 'allow_files' not granted for this shared thread",
            headers={
                "Cache-Control": "no-store",
                "Clear-Site-Data": '"cache"',
            },
        )
    thread_id = str(thread["conversation_thread_id"])
    claims = verify_public_media_ref(media_ref, expected_thread_id=thread_id)
    return await _serve_media_payload(
        thread_id=thread_id,
        workspace_id=str(thread.get("workspace_id") or ""),
        thread_owner_id=str(thread.get("user_id") or thread.get("owner_id") or ""),
        backend=str(claims.get("b") or ""),
        ref_id=str(claims.get("ref") or ""),
        cache_control="no-store",
    )
