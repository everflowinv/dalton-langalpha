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
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
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
    """Ensure every public share response, HTTPException, and 422 validation error carries ``Cache-Control: no-store``."""

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
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={"detail": jsonable_encoder(exc.errors())},
                    headers={"Cache-Control": "no-store"},
                )
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


def _is_r2_endpoint(endpoint_url: str | None) -> bool:
    ep = (endpoint_url or "").strip().lower()
    return "r2.cloudflarestorage.com" in ep


def _resolve_object_acl_config(*, endpoint_url: str | None = None) -> str:
    """Resolve effective object ACL policy: ``'auto'``, ``'on'``, or ``'off'``.

    Explicit ``DALTON_STORAGE_OBJECT_ACL=auto|on|off`` configures the mode
    (default ``auto``). Legacy ``DALTON_STORAGE_DISABLE_OBJECT_ACL`` /
    ``STORAGE_DISABLE_OBJECT_ACL`` remain supported and act as ``off``.
    """
    for env_name in ("DALTON_STORAGE_DISABLE_OBJECT_ACL", "STORAGE_DISABLE_OBJECT_ACL"):
        raw = (os.getenv(env_name) or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return "off"
    mode = (os.getenv("DALTON_STORAGE_OBJECT_ACL") or "auto").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return "off"
    if mode in ("on", "1", "true", "yes"):
        return "on"
    return "auto"


def _is_object_acl_disabled(*, endpoint_url: str | None = None) -> bool:
    cfg = _resolve_object_acl_config(endpoint_url=endpoint_url)
    if cfg == "off":
        return True
    if cfg == "on":
        return False
    return _is_r2_endpoint(endpoint_url)


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


_BUCKET_ACL_CACHE_LOCK = threading.Lock()
_BUCKET_ACL_SUPPORT_CACHE: dict[tuple[str, str], bool] = {}
_OWNERSHIP_FALLBACK_CODES = frozenset(
    {
        "OwnershipControlsNotFoundError",
        "NoSuchOwnershipControls",
    }
)
_OWNERSHIP_PROBE_UNSUPPORTED_CODES = frozenset(
    {
        "MethodNotAllowed",
        "NotImplemented",
    }
)


def _bucket_acl_cache_key(bucket_name: str, endpoint_url: str | None) -> tuple[str, str]:
    return (
        str(endpoint_url or "").strip().lower().rstrip("/"),
        str(bucket_name or "").strip(),
    )


def reset_bucket_acl_cache_for_test() -> None:
    """Clear cached bucket ACL support probes (test helper)."""
    with _BUCKET_ACL_CACHE_LOCK:
        _BUCKET_ACL_SUPPORT_CACHE.clear()


def mark_bucket_acl_unsupported(
    bucket_name: str, *, endpoint_url: str | None = None
) -> None:
    """Remember that ``bucket_name`` rejected object-level ACLs (e.g. ``BucketOwnerEnforced``)."""
    if not bucket_name:
        return
    with _BUCKET_ACL_CACHE_LOCK:
        _BUCKET_ACL_SUPPORT_CACHE[_bucket_acl_cache_key(bucket_name, endpoint_url)] = False


def probe_bucket_object_acl_mode(
    client: Any,
    bucket_name: str,
    *,
    endpoint_url: str | None = None,
) -> str:
    """Determine bucket object-ACL mode: ``'supported'``, ``'disabled'``, or ``'unknown'``.

    - ``DALTON_STORAGE_OBJECT_ACL=off`` (or legacy ``*_DISABLE_OBJECT_ACL``) -> ``'disabled'``
    - ``DALTON_STORAGE_OBJECT_ACL=on`` -> ``'supported'``
    - ``auto`` (default):
      - R2 endpoints or ``BucketOwnerEnforced`` -> ``'disabled'`` (cached)
      - ``BucketOwnerPreferred`` / ``ObjectWriter`` or ownership-controls-absent
        legacy bucket codes -> ``'supported'`` (cached)
      - Missing probe method, ``AccessDenied``, or other errors -> ``'unknown'``
        (NOT cached)
    """
    cfg = _resolve_object_acl_config(endpoint_url=endpoint_url)
    if cfg == "off":
        return "disabled"
    if cfg == "on":
        return "supported"
    if _is_r2_endpoint(endpoint_url):
        return "disabled"

    cache_key = _bucket_acl_cache_key(bucket_name, endpoint_url)
    with _BUCKET_ACL_CACHE_LOCK:
        cached = _BUCKET_ACL_SUPPORT_CACHE.get(cache_key)
    if cached is True:
        return "supported"
    if cached is False:
        return "disabled"

    probe = getattr(client, "get_bucket_ownership_controls", None)
    if not callable(probe) or not bucket_name:
        return "unknown"

    try:
        resp = probe(Bucket=bucket_name)
        rules = (
            (resp.get("OwnershipControls") or {}).get("Rules")
            if isinstance(resp, Mapping)
            else None
        ) or ()
        for rule in rules:
            ownership = str((rule or {}).get("ObjectOwnership") or "").strip()
            if ownership == "BucketOwnerEnforced":
                with _BUCKET_ACL_CACHE_LOCK:
                    _BUCKET_ACL_SUPPORT_CACHE[cache_key] = False
                return "disabled"
            if ownership in ("BucketOwnerPreferred", "ObjectWriter"):
                with _BUCKET_ACL_CACHE_LOCK:
                    _BUCKET_ACL_SUPPORT_CACHE[cache_key] = True
                return "supported"
        return "unknown"
    except Exception as exc:
        resp_map = getattr(exc, "response", None)
        code = (
            str((resp_map.get("Error") or {}).get("Code") or "")
            if isinstance(resp_map, Mapping)
            else ""
        )
        if code in _OWNERSHIP_PROBE_UNSUPPORTED_CODES:
            logger.debug(
                "Ownership controls probe unsupported for %s (%s); treating ACL mode as unknown",
                bucket_name,
                code,
            )
            return "unknown"
        if code == "AccessControlListNotSupported" or (
            code in ("InvalidRequest", "InvalidArgument")
            and is_acl_not_supported_error(exc)
        ):
            with _BUCKET_ACL_CACHE_LOCK:
                _BUCKET_ACL_SUPPORT_CACHE[cache_key] = False
            return "disabled"
        if code in _OWNERSHIP_FALLBACK_CODES:
            with _BUCKET_ACL_CACHE_LOCK:
                _BUCKET_ACL_SUPPORT_CACHE[cache_key] = True
            return "supported"
        logger.debug(
            "Could not determine bucket ownership controls for %s (%s); treating ACL mode as unknown",
            bucket_name,
            code or type(exc).__name__,
        )
        return "unknown"


def bucket_supports_object_acl(
    client: Any,
    bucket_name: str,
    *,
    endpoint_url: str | None = None,
) -> bool:
    """Return True iff ``bucket_name`` is confirmed (or explicitly configured) to support object ACLs."""
    return (
        probe_bucket_object_acl_mode(client, bucket_name, endpoint_url=endpoint_url)
        == "supported"
    )


def s3_put_object_with_private_acl(
    client: Any,
    *,
    bucket: str,
    key: str,
    body: Any,
    content_type: str,
    endpoint_url: str | None = None,
) -> Any:
    """Thin-hook helper for upstream ``s3_compatible`` uploads with private ACL & fallback."""
    acl_kwargs = storage_private_acl_kwargs(key, provider="s3", endpoint_url=endpoint_url)
    if (
        acl_kwargs
        and probe_bucket_object_acl_mode(client, bucket, endpoint_url=endpoint_url)
        == "disabled"
    ):
        acl_kwargs = {}
    put_args: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": body,
        "ContentType": content_type,
        **acl_kwargs,
    }
    try:
        return client.put_object(**put_args)
    except Exception as exc:
        if acl_kwargs and is_acl_not_supported_error(exc):
            mark_bucket_acl_unsupported(bucket, endpoint_url=endpoint_url)
            put_args.pop("ACL", None)
            if hasattr(body, "seek"):
                body.seek(0)
            return client.put_object(**put_args)
        raise


def s3_create_multipart_with_private_acl(
    client: Any,
    *,
    bucket: str,
    key: str,
    content_type: str,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Thin-hook helper for upstream ``s3_compatible.open_multipart_upload``."""
    acl_kwargs = storage_private_acl_kwargs(key, provider="s3", endpoint_url=endpoint_url)
    if (
        acl_kwargs
        and probe_bucket_object_acl_mode(client, bucket, endpoint_url=endpoint_url)
        == "disabled"
    ):
        acl_kwargs = {}
    params: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "ContentType": content_type,
        **acl_kwargs,
    }
    try:
        return client.create_multipart_upload(**params)
    except Exception as exc:
        if acl_kwargs and is_acl_not_supported_error(exc):
            mark_bucket_acl_unsupported(bucket, endpoint_url=endpoint_url)
            params.pop("ACL", None)
            return client.create_multipart_upload(**params)
        raise


def s3_presign_put_with_private_acl(
    client: Any,
    *,
    bucket: str,
    key: str,
    checksum_b64: str,
    content_length: int,
    content_type: str,
    expires_in: int,
    endpoint_url: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Presign S3 single PUT after probing bucket ACL mode.

    Omits ``ACL`` / ``x-amz-acl`` when mode is ``disabled`` or ``unknown`` (in
    ``auto`` mode), logging a warning on ``unknown`` without caching the probe.
    """
    acl_kwargs = storage_private_acl_kwargs(key, provider="s3", endpoint_url=endpoint_url)
    if acl_kwargs:
        acl_mode = probe_bucket_object_acl_mode(
            client, bucket, endpoint_url=endpoint_url
        )
        if acl_mode == "disabled":
            acl_kwargs = {}
        elif acl_mode == "unknown":
            acl_kwargs = {}
            logger.warning(
                "Bucket object ACL mode is unknown for bucket=%s endpoint=%s; "
                "omitting ACL from presigned PUT (set DALTON_STORAGE_OBJECT_ACL=on|off to override)",
                bucket,
                endpoint_url or "default",
            )
    params: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "ContentLength": content_length,
        "ContentType": content_type,
        "ChecksumSHA256": checksum_b64,
        **acl_kwargs,
    }
    url = client.generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=expires_in,
    )
    headers: dict[str, str] = {
        "Content-Type": content_type,
        "x-amz-checksum-sha256": checksum_b64,
    }
    if acl_kwargs.get("ACL"):
        headers["x-amz-acl"] = str(acl_kwargs["ACL"])
    return url, headers


def oss_put_object_with_private_acl(
    client: Any,
    request_cls: Any,
    *,
    bucket: str,
    full_object_key: str,
    logical_key: str,
    body: Any,
    content_type: str,
) -> Any:
    """Thin-hook helper for upstream ``oss_uploader`` with private ACL & ACL-unsupported fallback."""
    acl_kwargs = storage_private_acl_kwargs(logical_key, provider="oss")
    try:
        return client.put_object(
            request_cls(
                bucket=bucket,
                key=full_object_key,
                body=body,
                content_type=content_type,
                **acl_kwargs,
            )
        )
    except Exception as exc:
        if acl_kwargs and is_acl_not_supported_error(exc):
            if hasattr(body, "seek"):
                body.seek(0)
            return client.put_object(
                request_cls(
                    bucket=bucket,
                    key=full_object_key,
                    body=body,
                    content_type=content_type,
                )
            )
        raise


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


def _is_ip_or_local_host(host: str) -> bool:
    cleaned = (host or "").strip().lower().strip("[]")
    if not cleaned:
        return False
    if cleaned == "localhost" or cleaned.endswith(".localhost") or "." not in cleaned:
        return True
    try:
        ipaddress.ip_address(cleaned)
        return True
    except ValueError:
        return False


def format_storage_endpoint_base(
    *,
    provider: str,
    endpoint_url: str,
    bucket_name: str,
    region: str,
    addressing_style: str = "virtual",
) -> str:
    """Construct the base object URL for ``endpoint_url`` matching S3/OSS addressing rules."""
    ep = (endpoint_url or "").strip().rstrip("/")
    bucket = (bucket_name or "").strip()
    if provider == "oss":
        if not ep:
            return ""
        if "://" in ep:
            scheme, rest = ep.split("://", 1)
            return (
                ep
                if rest.lower().startswith(f"{bucket.lower()}.")
                else f"{scheme}://{bucket}.{rest}"
            )
        return f"https://{bucket}.{ep}"

    if not ep:
        return f"https://{bucket}.s3.{region or 'us-east-1'}.amazonaws.com"

    style = (addressing_style or "virtual").strip().lower()
    parsed = urlsplit(ep if "://" in ep else f"https://{ep}")
    host = parsed.hostname or ""
    if style == "path" or _is_ip_or_local_host(host):
        return f"{ep}/{bucket}"
    if "://" in ep:
        scheme, rest = ep.split("://", 1)
        return (
            ep
            if rest.lower().startswith(f"{bucket.lower()}.")
            else f"{scheme}://{bucket}.{rest}"
        )
    return f"https://{bucket}.{ep}"


def build_storage_object_urls(target: ActiveStorageTarget, key: str) -> list[str]:
    """Build all anonymous probe URLs for ``key``: origin, independent signing endpoint, and public/CDN bases."""
    urls: list[str] = []
    quoted = quote(key.lstrip("/"), safe="/")

    def _add_base(base: str) -> None:
        clean_base = (base or "").strip().rstrip("/")
        if not clean_base:
            return
        u = f"{clean_base}/{quoted}"
        if u not in urls:
            urls.append(u)

    # 1. Primary origin endpoint
    origin_base = format_storage_endpoint_base(
        provider=target.provider,
        endpoint_url=target.endpoint_url,
        bucket_name=target.bucket_name,
        region=target.region,
        addressing_style=target.addressing_style,
    )
    _add_base(origin_base)
    if target.provider == "s3" and target.endpoint_url and target.addressing_style == "auto":
        _add_base(
            format_storage_endpoint_base(
                provider="s3",
                endpoint_url=target.endpoint_url,
                bucket_name=target.bucket_name,
                region=target.region,
                addressing_style="path",
            )
        )
        _add_base(
            format_storage_endpoint_base(
                provider="s3",
                endpoint_url=target.endpoint_url,
                bucket_name=target.bucket_name,
                region=target.region,
                addressing_style="virtual",
            )
        )

    # 2. Independent signing / browser endpoint (if distinct from origin)
    if target.signing_endpoint_url and target.signing_endpoint_url.rstrip("/") != target.endpoint_url.rstrip("/"):
        signing_base = format_storage_endpoint_base(
            provider=target.provider,
            endpoint_url=target.signing_endpoint_url,
            bucket_name=target.bucket_name,
            region=target.region,
            addressing_style=target.addressing_style,
        )
        _add_base(signing_base)

    # 3. Configured public URL bases / CDN domains / runtime default public base
    for base in target.public_url_bases:
        _add_base(base)

    return urls


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
        explicit_pub = (
            os.getenv("OSS_PUBLIC_URL_BASE")
            or os.getenv("STORAGE_PUBLIC_URL_BASE")
            or ""
        ).strip().rstrip("/")
        public_bases: list[str] = []
        if explicit_pub:
            public_bases.append(explicit_pub)
        if endpoint:
            try:
                from src.utils.storage.oss_uploader import OSSConfig

                ep_host = re.sub(r"^https?://", "", endpoint, flags=re.IGNORECASE)

                class _EnvOSSConfig(OSSConfig):
                    BUCKET_NAME = bucket
                    ENDPOINT = ep_host

                runtime_oss_base = _EnvOSSConfig.get_public_url_base().strip().rstrip("/")
                if runtime_oss_base and runtime_oss_base not in public_bases:
                    public_bases.append(runtime_oss_base)
            except Exception:
                logger.debug("Could not resolve runtime OSSConfig public URL base", exc_info=True)
        default_oss_base = format_storage_endpoint_base(
            provider="oss",
            endpoint_url=endpoint,
            bucket_name=bucket,
            region=region,
            addressing_style="virtual",
        )
        if default_oss_base and default_oss_base not in public_bases:
            public_bases.append(default_oss_base)
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
            "public_url_bases": public_bases,
        }

    # S3-compatible (STORAGE_* with legacy S3_* / AWS_* fallback, matching s3_client.py)
    bucket = (os.getenv("STORAGE_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME") or "").strip()
    if not bucket:
        return None
    region = (os.getenv("STORAGE_REGION") or os.getenv("S3_REGION") or "us-east-1").strip()
    endpoint = (os.getenv("STORAGE_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "").strip().rstrip("/")
    browser_ep = (os.getenv("STORAGE_BROWSER_ENDPOINT_URL") or "").strip().rstrip("/")
    explicit_pub = (os.getenv("STORAGE_PUBLIC_URL_BASE") or os.getenv("S3_PUBLIC_URL_BASE") or "").strip().rstrip("/")
    style = (os.getenv("STORAGE_ADDRESSING_STYLE") or "virtual").strip().lower()
    if style not in ("virtual", "path", "auto"):
        style = "virtual"
    public_bases = []
    if explicit_pub:
        public_bases.append(explicit_pub)
    # Always include the runtime StorageConfig.get_public_url_base() result so custom
    # S3 endpoints without explicit PUBLIC_URL_BASE still probe the AWS default host
    # generated by runtime get_public_url().
    from src.utils.storage.s3_client import StorageConfig

    class _EnvStorageConfig(StorageConfig):
        BUCKET_NAME = bucket
        REGION = region
        PUBLIC_URL_BASE = explicit_pub or None

    runtime_s3_base = _EnvStorageConfig.get_public_url_base().strip().rstrip("/")
    if runtime_s3_base and runtime_s3_base not in public_bases:
        public_bases.append(runtime_s3_base)
    ep_base = format_storage_endpoint_base(
        provider="s3",
        endpoint_url=endpoint,
        bucket_name=bucket,
        region=region,
        addressing_style=style,
    )
    if ep_base and ep_base not in public_bases:
        public_bases.append(ep_base)
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
        "public_url_bases": public_bases,
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
        public_url_bases: Iterable[str],
    ) -> None:
        if not bucket_name:
            return
        k = _dedup_bucket_key(provider, endpoint_url, bucket_name)
        pref = _norm_prefix(key_prefix)
        full_sensitive = [f"{pref}{sub}" for sub in sensitive_subprefixes]
        clean_bases = [b.rstrip("/") for b in public_url_bases if b and b.strip()]
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
                "public_url_bases": list(dict.fromkeys(clean_bases)),
            }
        else:
            if role not in entry["roles"]:
                entry["roles"].append(role)
            if pref not in entry["key_prefixes"]:
                entry["key_prefixes"].append(pref)
            for sp in full_sensitive:
                if sp not in entry["sensitive_prefixes"]:
                    entry["sensitive_prefixes"].append(sp)
            if signing_endpoint_url and signing_endpoint_url != entry["endpoint_url"]:
                if not entry["signing_endpoint_url"] or entry["signing_endpoint_url"] == entry["endpoint_url"]:
                    entry["signing_endpoint_url"] = signing_endpoint_url
                else:
                    extra_sign = format_storage_endpoint_base(
                        provider=provider,
                        endpoint_url=signing_endpoint_url,
                        bucket_name=bucket_name,
                        region=region,
                        addressing_style=addressing_style,
                    )
                    if extra_sign and extra_sign not in entry["public_url_bases"]:
                        entry["public_url_bases"].append(extra_sign)
            for pb in clean_bases:
                if pb not in entry["public_url_bases"]:
                    entry["public_url_bases"].append(pb)

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
            public_url_bases=up["public_url_bases"],
        )

    # 2. Dalton chat-upload & channel-snapshot bucket
    if chat_store.is_chat_upload_storage_configured():
        ep = chat_store._endpoint_url()
        signing_ep = chat_store.signing_endpoint_url()
        bucket = chat_store._bucket_name()
        region = chat_store._region()
        style = chat_store._addressing_style()
        chat_bases: list[str] = []
        explicit_chat_pub = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_PUBLIC_URL_BASE") or "").strip().rstrip("/")
        if explicit_chat_pub:
            chat_bases.append(explicit_chat_pub)
        chat_ep_base = format_storage_endpoint_base(
            provider="s3",
            endpoint_url=ep,
            bucket_name=bucket,
            region=region,
            addressing_style=style,
        )
        if chat_ep_base and chat_ep_base not in chat_bases:
            chat_bases.append(chat_ep_base)
        _register(
            role="chat_upload",
            provider="s3",
            bucket_name=bucket,
            endpoint_url=ep,
            signing_endpoint_url=signing_ep,
            region=region,
            addressing_style=style,
            access_key_id=chat_store._access_key_id(),
            secret_access_key=chat_store._secret_access_key(),
            key_prefix=chat_store._key_prefix(),
            sensitive_subprefixes=(
                "private-media/",
                "channel-snapshots/",
                "chat-uploads/",
            ),
            public_url_bases=chat_bases,
        )

    # 3. Independent transcription ASR bucket (always evaluated independently)
    if asr_store.is_transcription_storage_configured():
        ep = asr_store._endpoint_url()
        signing_ep = asr_store.signing_endpoint_url()
        bucket = asr_store._bucket_name()
        region = asr_store._region()
        style = asr_store._addressing_style()
        asr_bases: list[str] = []
        explicit_asr_pub = (os.getenv("DALTON_ASR_STORAGE_PUBLIC_URL_BASE") or "").strip().rstrip("/")
        if explicit_asr_pub:
            asr_bases.append(explicit_asr_pub)
        asr_ep_base = format_storage_endpoint_base(
            provider="s3",
            endpoint_url=ep,
            bucket_name=bucket,
            region=region,
            addressing_style=style,
        )
        if asr_ep_base and asr_ep_base not in asr_bases:
            asr_bases.append(asr_ep_base)
        _register(
            role="transcription",
            provider="s3",
            bucket_name=bucket,
            endpoint_url=ep,
            signing_endpoint_url=signing_ep,
            region=region,
            addressing_style=style,
            access_key_id=asr_store._access_key_id(),
            secret_access_key=asr_store._secret_access_key(),
            key_prefix=asr_store._key_prefix(),
            sensitive_subprefixes=(
                "transcriptions/",
                "uploads/",
            ),
            public_url_bases=asr_bases,
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
    if not p_b64.isascii() or not s_b64.isascii():
        raise HTTPException(status_code=401, detail="Invalid media token")
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
    if not p_b64.isascii() or not s_b64.isascii():
        raise HTTPException(status_code=404, detail="Media not found", headers=_no_store)
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
    from src.server.utils.api import admit_caller, get_current_user_id

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

    viewer_id = str(claims.get("vid") or "")
    if viewer_id:
        await admit_caller(request, viewer_id)
    return viewer_id


async def _owner_media_caller_gate(
    thread_id: str,
    media_token: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> tuple[dict[str, Any], str]:
    """FastAPI route dependency enforcing owner media token verification and account-suspension admission."""
    claims = verify_owner_media_token(media_token, expected_thread_id=thread_id)
    viewer_id = await require_media_viewer(request, thread_id, claims, credentials)
    return claims, viewer_id


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

_UPSTREAM_ROOT_SEGMENTS = frozenset({"response-images", "attachments", "widgets"})


def extract_upstream_key_from_url(url: str) -> str | None:
    """Extract and validate logical upstream object key from a stored public/CDN URL.

    Decodes each path segment from the full URL path (preserving spaces,
    parentheses, CJK characters, and percent-encoded filenames) and rejects
    unsafe segments such as ``.``, ``..``, embedded ``/`` (``%2F``), ``\\``,
    or control characters.
    """
    if not url:
        return None
    try:
        raw_path = urlsplit(url).path or ""
    except Exception:
        return None
    stripped_path = raw_path.lstrip("/")
    if not stripped_path:
        return None

    raw_parts = stripped_path.split("/")
    decoded_parts: list[str] = []
    for seg in raw_parts:
        dec = unquote(seg)
        if not dec or "/" in dec or "\\" in dec or "\x00" in dec or "\r" in dec or "\n" in dec:
            return None
        if dec in (".", ".."):
            return None
        decoded_parts.append(dec)

    key_parts: list[str] | None = None
    n_parts = len(decoded_parts)
    for idx, seg in enumerate(decoded_parts):
        low = seg.lower()
        if (
            low == "private-media"
            and idx + 2 < n_parts
            and decoded_parts[idx + 1].lower() in _UPSTREAM_ROOT_SEGMENTS
        ):
            key_parts = decoded_parts[idx:]
            break
        if low in _UPSTREAM_ROOT_SEGMENTS and idx + 1 < n_parts:
            key_parts = decoded_parts[idx:]
            break

    if not key_parts:
        return None
    candidate = "/".join(key_parts)
    if not _is_safe_object_key(candidate):
        return None
    return candidate


async def _fetch_ready_chat_uploads(
    workspace_id: str,
    upload_ids: Iterable[str],
    *,
    allowed_user_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    ids = list({str(u).strip() for u in upload_ids if str(u or "").strip()})
    if not ids or not workspace_id:
        return {}
    users_filter: list[str] | None = None
    if allowed_user_ids is not None:
        users_filter = sorted({str(u).strip() for u in allowed_user_ids if str(u or "").strip()})
        if not users_filter:
            return {}
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if users_filter is not None:
                await cur.execute(
                    """
                    SELECT upload_id,
                           user_id,
                           workspace_id,
                           object_key,
                           safe_filename,
                           mime_type,
                           state,
                           cleaned_at
                    FROM dalton_chat_file_uploads
                    WHERE workspace_id = %s
                      AND upload_id = ANY(%s::text[])
                      AND user_id = ANY(%s::text[])
                      AND state = 'ready'
                      AND cleaned_at IS NULL
                    """,
                    (str(workspace_id), ids, users_filter),
                )
            else:
                await cur.execute(
                    """
                    SELECT upload_id,
                           user_id,
                           workspace_id,
                           object_key,
                           safe_filename,
                           mime_type,
                           state,
                           cleaned_at
                    FROM dalton_chat_file_uploads
                    WHERE workspace_id = %s
                      AND upload_id = ANY(%s::text[])
                      AND state = 'ready'
                      AND cleaned_at IS NULL
                    """,
                    (str(workspace_id), ids),
                )
            rows = await cur.fetchall()
    return {str(r["upload_id"]): dict(r) for r in rows}


def _is_safe_object_key(key: str) -> bool:
    cleaned = str(key or "").strip().lstrip("/")
    if not cleaned or "\x00" in cleaned or "\\" in cleaned or "\r" in cleaned or "\n" in cleaned:
        return False
    parts = cleaned.split("/")
    return all(p and p not in (".", "..") for p in parts)


def _allowed_storage_origins() -> set[str] | None:
    """Return all configured storage origins, or ``None`` if target resolution failed."""
    origins: set[str] = set()
    try:
        targets = resolve_active_storage_targets()
    except Exception:
        logger.debug("Storage target origin resolution failed; failing closed", exc_info=True)
        return None
    for t in targets:
        formatted_origin = format_storage_endpoint_base(
            provider=t.provider,
            endpoint_url=t.endpoint_url,
            bucket_name=t.bucket_name,
            region=t.region,
            addressing_style=t.addressing_style,
        )
        formatted_signing = (
            format_storage_endpoint_base(
                provider=t.provider,
                endpoint_url=t.signing_endpoint_url,
                bucket_name=t.bucket_name,
                region=t.region,
                addressing_style=t.addressing_style,
            )
            if t.signing_endpoint_url
            else ""
        )
        for raw_u in (
            t.endpoint_url,
            t.signing_endpoint_url,
            formatted_origin,
            formatted_signing,
            *t.public_url_bases,
        ):
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
    """Return True iff ``url`` matches a non-empty configured storage origin (fail closed)."""
    origins = _allowed_storage_origins()
    if not origins:
        return False
    norm = _normalize_origin_str(url)
    return bool(norm and norm in origins)


def _is_private_media_ref(ref: str) -> bool:
    """Return True iff ``ref`` is an internal ``private-media/...`` logical key reference."""
    if not isinstance(ref, str):
        return False
    cleaned = ref.strip()
    if not cleaned or "://" in cleaned or cleaned.startswith("/"):
        return False
    if not cleaned.lower().startswith(PRIVATE_MEDIA_PREFIX):
        return False
    key = extract_upstream_key_from_url(cleaned)
    return bool(key and _is_safe_object_key(key))


def _is_thread_prefixed_upstream_key(thread_id: str, key: str) -> bool:
    """Return True iff ``key`` is directly scoped under ``{prefix}/{thread_id}/...``."""
    if not thread_id or not _is_safe_object_key(key):
        return False
    cleaned = strip_private_media_prefix(key)
    for prefix in ("response-images/", "attachments/", "widgets/"):
        if cleaned.startswith(f"{prefix}{thread_id}/"):
            return True
    return False


@dataclass
class ThreadMediaRegistry:
    """Authoritative thread-scoped media registry shared by both issuance and serving."""

    attachment_keys: set[str] = field(default_factory=set)
    attachment_ids: set[str] = field(default_factory=set)
    attachment_urls: set[str] = field(default_factory=set)
    attachment_object_keys: set[str] = field(default_factory=set)
    attachment_owners: dict[str, set[str]] = field(default_factory=dict)
    upload_id_owners: dict[str, set[str]] = field(default_factory=dict)
    captured_image_keys: set[str] = field(default_factory=set)
    captured_image_urls: set[str] = field(default_factory=set)


def _register_attachment_mapping(
    att: Any,
    registry: ThreadMediaRegistry,
    *,
    allowed_users: set[str],
) -> None:
    if not isinstance(att, Mapping):
        return
    raw_url = att.get("url")
    if isinstance(raw_url, str) and raw_url.strip():
        clean_url = raw_url.strip()
        registry.attachment_urls.add(clean_url)
        k = extract_upstream_key_from_url(clean_url)
        if k and _is_safe_object_key(k):
            cleaned = strip_private_media_prefix(k)
            registry.attachment_keys.add(cleaned)
            if allowed_users:
                registry.attachment_owners.setdefault(cleaned, set()).update(allowed_users)
            if cleaned.startswith("attachments/"):
                parts = cleaned.split("/")
                if len(parts) >= 3 and parts[1]:
                    registry.attachment_ids.add(parts[1])
                    if allowed_users:
                        registry.attachment_owners.setdefault(parts[1], set()).update(allowed_users)
    raw_okey = att.get("object_key")
    if isinstance(raw_okey, str) and raw_okey.strip():
        clean_okey = raw_okey.strip().lstrip("/")
        if _is_safe_object_key(clean_okey):
            cleaned = strip_private_media_prefix(clean_okey)
            if cleaned.startswith("attachments/"):
                registry.attachment_object_keys.add(clean_okey)
                registry.attachment_keys.add(cleaned)
                if allowed_users:
                    registry.attachment_owners.setdefault(cleaned, set()).update(allowed_users)
                parts = cleaned.split("/")
                if len(parts) >= 3 and parts[1]:
                    registry.attachment_ids.add(parts[1])
                    if allowed_users:
                        registry.attachment_owners.setdefault(parts[1], set()).update(allowed_users)
    raw_att_id = att.get("attachment_id") or att.get("id")
    if isinstance(raw_att_id, str) and raw_att_id.strip():
        clean_aid = raw_att_id.strip()
        registry.attachment_ids.add(clean_aid)
        if allowed_users:
            registry.attachment_owners.setdefault(clean_aid, set()).update(allowed_users)
    raw_uid = att.get("upload_id")
    if isinstance(raw_uid, str) and raw_uid.strip() and allowed_users:
        registry.upload_id_owners.setdefault(raw_uid.strip(), set()).update(allowed_users)


def _register_steering_messages_into_registry(
    messages: Any,
    registry: ThreadMediaRegistry,
    *,
    default_owner_id: str | None,
) -> None:
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        msg_user = str(msg.get("user_id") or default_owner_id or "").strip()
        allowed = {msg_user} if msg_user else set()
        for att in msg.get("attachments") or ():
            _register_attachment_mapping(att, registry, allowed_users=allowed)
        meta = msg.get("metadata")
        if isinstance(meta, Mapping):
            for att in meta.get("attachments") or ():
                _register_attachment_mapping(att, registry, allowed_users=allowed)


def _extract_response_sender_user_id(row: Mapping[str, Any]) -> str | None:
    direct = str(row.get("sender_user_id") or "").strip()
    if direct:
        return direct
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    if isinstance(meta, Mapping):
        uid = str(meta.get("user_id") or "").strip()
        if uid:
            return uid
    return None


def _iter_response_rows(
    response_rows: Iterable[Mapping[str, Any]] | None,
    responses_by_turn: Mapping[Any, Any] | None,
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    if response_rows is not None:
        for r in response_rows:
            if isinstance(r, Mapping):
                out.append(r)
    if isinstance(responses_by_turn, Mapping):
        for val in responses_by_turn.values():
            if isinstance(val, Mapping):
                out.append(val)
            elif isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, Mapping):
                        out.append(item)
    return out


def _collect_thread_media_registry(
    thread_id: str,
    *,
    owner_id: str | None = None,
    queries: Iterable[Mapping[str, Any]] | None = None,
    response_rows: Iterable[Mapping[str, Any]] | None = None,
    responses_by_turn: Mapping[Any, Any] | None = None,
    run_facts: Iterable[Mapping[str, Any]] | None = None,
    items: Iterable[Mapping[str, Any]] | None = None,
) -> ThreadMediaRegistry:
    """Assemble the complete trusted object registry for ``thread_id`` across all attempts."""
    registry = ThreadMediaRegistry()
    clean_owner = str(owner_id or "").strip() or None
    all_responses = _iter_response_rows(response_rows, responses_by_turn)

    # Map each turn_index to the authenticated sender(s) stamped on conversation_responses.metadata->>'user_id'.
    turn_senders: dict[Any, set[str]] = {}
    for r in all_responses:
        sender = _extract_response_sender_user_id(r)
        t_idx = r.get("turn_index")
        if sender and t_idx is not None:
            turn_senders.setdefault(t_idx, set()).add(sender)

    for q in queries or ():
        if not isinstance(q, Mapping):
            continue
        explicit_q_user = str(q.get("user_id") or "").strip()
        if explicit_q_user:
            allowed = {explicit_q_user}
        else:
            t_idx = q.get("turn_index")
            senders = turn_senders.get(t_idx) if t_idx is not None else None
            if senders:
                allowed = set(senders)
            elif clean_owner:
                allowed = {clean_owner}
            else:
                allowed = set()
        meta = q.get("metadata")
        if isinstance(meta, Mapping):
            for att in meta.get("attachments") or ():
                _register_attachment_mapping(att, registry, allowed_users=allowed)

    for container in (*all_responses, *(run_facts or ())):
        if not isinstance(container, Mapping):
            continue
        facts = container.get("replay_facts") if "replay_facts" in container else container
        if not isinstance(facts, Mapping):
            continue
        imgs = facts.get("images")
        img_urls = imgs.values() if isinstance(imgs, Mapping) else (imgs if isinstance(imgs, list) else ())
        for u in img_urls:
            if not isinstance(u, str) or not u.strip():
                continue
            clean_u = u.strip()
            k = extract_upstream_key_from_url(clean_u)
            if k and _is_safe_object_key(k):
                cleaned = strip_private_media_prefix(k)
                if cleaned.startswith(f"response-images/{thread_id}/"):
                    registry.captured_image_keys.add(cleaned)
                    registry.captured_image_urls.add(clean_u)
        # Archived legacy steering_delivered messages (if present in replay_facts.archive)
        archive = facts.get("archive")
        if isinstance(archive, list):
            for ev in archive:
                if isinstance(ev, Mapping) and ev.get("event") == "steering_delivered":
                    ev_data = ev.get("data")
                    if isinstance(ev_data, Mapping):
                        _register_steering_messages_into_registry(
                            ev_data.get("messages"),
                            registry,
                            default_owner_id=clean_owner,
                        )

    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        if item.get("event") == "steering_delivered":
            data = item.get("data")
            if isinstance(data, Mapping):
                _register_steering_messages_into_registry(
                    data.get("messages"),
                    registry,
                    default_owner_id=clean_owner,
                )

    return registry


def _extract_registered_query_attachment_keys_and_ids(
    queries: Iterable[Mapping[str, Any]],
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(normalized_keys, attachment_ids, upload_ids)`` from persisted query metadata."""
    reg = _collect_thread_media_registry("", owner_id="__any__", queries=queries)
    return reg.attachment_keys, reg.attachment_ids, set(reg.upload_id_owners)


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


async def _load_thread_media_registry_from_db(
    thread_id: str,
    *,
    owner_id: str | None = None,
    turn_indices: Iterable[int] | None = None,
) -> ThreadMediaRegistry | None:
    """Load the authoritative thread media registry from DB for issuance and serving.

    Unions ALL attempt rows per turn (never overwriting earlier attempts), reads
    per-turn authenticated sender from ``conversation_responses.metadata->>'user_id'``,
    and filters ``turn_slices`` in SQL to rows containing ``steering_delivered``.
    Returns ``None`` on DB failure so callers fail closed.
    """
    from src.server.database.pool import get_db_connection
    from src.server.services.history.replay import lines as replay_lines

    turns: list[int] | None = None
    if turn_indices is not None:
        parsed_turns = sorted({int(t) for t in turn_indices if t is not None})
        if parsed_turns:
            turns = parsed_turns

    queries: list[dict[str, Any]] = []
    response_rows: list[dict[str, Any]] = []
    run_facts: list[dict[str, Any]] = []
    steering_items: list[dict[str, Any]] = []

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                if turns is not None:
                    await cur.execute(
                        """
                        SELECT turn_index, metadata
                        FROM conversation_queries
                        WHERE conversation_thread_id = %s::uuid
                          AND metadata IS NOT NULL
                          AND turn_index = ANY(%s::int[])
                        """,
                        (str(thread_id), turns),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT turn_index, metadata
                        FROM conversation_queries
                        WHERE conversation_thread_id = %s::uuid
                          AND metadata IS NOT NULL
                        """,
                        (str(thread_id),),
                    )
                queries = [dict(r) for r in await cur.fetchall()]

                if turns is not None:
                    await cur.execute(
                        """
                        SELECT turn_index,
                               replay_facts,
                               metadata->>'user_id' AS sender_user_id
                        FROM conversation_responses
                        WHERE conversation_thread_id = %s::uuid
                          AND (replay_facts IS NOT NULL OR metadata ? 'user_id')
                          AND turn_index = ANY(%s::int[])
                        """,
                        (str(thread_id), turns),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT turn_index,
                               replay_facts,
                               metadata->>'user_id' AS sender_user_id
                        FROM conversation_responses
                        WHERE conversation_thread_id = %s::uuid
                          AND (replay_facts IS NOT NULL OR metadata ? 'user_id')
                        """,
                        (str(thread_id),),
                    )
                response_rows = [dict(r) for r in await cur.fetchall()]

                if turns is not None:
                    await cur.execute(
                        """
                        SELECT s.replay_facts
                        FROM subagent_runs s
                        JOIN conversation_responses r
                          ON r.conversation_response_id = s.parent_run_id
                        WHERE s.thread_id = %s::uuid
                          AND s.replay_facts IS NOT NULL
                          AND r.turn_index = ANY(%s::int[])
                        """,
                        (str(thread_id), turns),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT replay_facts
                        FROM subagent_runs
                        WHERE thread_id = %s::uuid
                          AND replay_facts IS NOT NULL
                        """,
                        (str(thread_id),),
                    )
                run_facts = [dict(r) for r in await cur.fetchall()]

                if turns is not None:
                    await cur.execute(
                        """
                        SELECT ts.lines
                        FROM turn_slices ts
                        JOIN conversation_responses r
                          ON r.conversation_response_id = ts.conversation_response_id
                        WHERE ts.conversation_thread_id = %s::uuid
                          AND ts.lines IS NOT NULL
                          AND position('steering_delivered' in ts.lines) > 0
                          AND r.turn_index = ANY(%s::int[])
                        """,
                        (str(thread_id), turns),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT lines
                        FROM turn_slices
                        WHERE conversation_thread_id = %s::uuid
                          AND lines IS NOT NULL
                          AND position('steering_delivered' in lines) > 0
                        """,
                        (str(thread_id),),
                    )
                for r in await cur.fetchall():
                    raw_lines = r.get("lines")
                    if not isinstance(raw_lines, str) or "steering_delivered" not in raw_lines:
                        continue
                    for ln in replay_lines.decode(raw_lines):
                        if ln.event == "steering_delivered":
                            steering_items.append(ln.item())
    except Exception:
        logger.debug("Failed loading thread media registry for %s", thread_id, exc_info=True)
        return None

    return _collect_thread_media_registry(
        thread_id,
        owner_id=owner_id,
        queries=queries,
        response_rows=response_rows,
        run_facts=run_facts,
        items=steering_items,
    )


async def _verify_staged_attachment_owner(
    attachment_id: str,
    cleaned_key: str,
    *,
    owner_id: str | None = None,
    allowed_owner_ids: Iterable[str] | None = None,
) -> bool:
    """Verify staged ``chat_attachments`` key & owner match (fail closed when pool is closed/errors)."""
    try:
        from src.server.database.chat_attachments import get_attachment

        row = await get_attachment(attachment_id)
    except Exception:
        logger.debug("Staged attachment owner check failed for %s", attachment_id, exc_info=True)
        return False

    if row is None:
        # Legacy inline/migrated attachment without a chat_attachments staging row:
        # thread query/steering registry membership was already verified by caller.
        return True
    row_key = strip_private_media_prefix(str(row.get("object_key") or ""))
    if row_key and row_key != cleaned_key:
        return False
    allowed_set = {str(u).strip() for u in (allowed_owner_ids or ()) if str(u or "").strip()}
    if not allowed_set and owner_id:
        allowed_set = {str(owner_id).strip()}
    if allowed_set and row.get("user_id") and str(row["user_id"]).strip() not in allowed_set:
        return False
    return True


async def _is_attachment_bound_to_thread(
    thread_id: str,
    key: str,
    *,
    owner_id: str | None = None,
    allowed_owner_ids: Iterable[str] | None = None,
    queries: Iterable[Mapping[str, Any]] | None = None,
    registry: ThreadMediaRegistry | None = None,
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

    resolved_registry = registry
    if resolved_registry is not None:
        reg_keys = resolved_registry.attachment_keys
        reg_att_ids = resolved_registry.attachment_ids
    elif queries is not None:
        resolved_registry = _collect_thread_media_registry(
            thread_id, owner_id=owner_id, queries=queries
        )
        reg_keys = resolved_registry.attachment_keys
        reg_att_ids = resolved_registry.attachment_ids
    else:
        resolved_registry = await _load_thread_media_registry_from_db(
            thread_id, owner_id=owner_id
        )
        if resolved_registry is None:
            return False
        reg_keys = resolved_registry.attachment_keys
        reg_att_ids = resolved_registry.attachment_ids

    if cleaned not in reg_keys and att_id not in reg_att_ids:
        return False

    effective_allowed: set[str] = set()
    if allowed_owner_ids:
        effective_allowed.update(
            str(u).strip() for u in allowed_owner_ids if str(u or "").strip()
        )
    if not effective_allowed and resolved_registry is not None:
        effective_allowed.update(resolved_registry.attachment_owners.get(cleaned) or ())
        effective_allowed.update(resolved_registry.attachment_owners.get(att_id) or ())

    return await _verify_staged_attachment_owner(
        att_id,
        cleaned,
        owner_id=owner_id,
        allowed_owner_ids=effective_allowed or None,
    )


async def _is_chat_upload_bound_to_thread(
    thread_id: str,
    upload_id: str,
    *,
    owner_id: str | None = None,
    queries: Iterable[Mapping[str, Any]] | None = None,
    registry: ThreadMediaRegistry | None = None,
) -> set[str]:
    """Return allowed uploader ``user_id``s if ``upload_id`` is registered on ``thread_id``; empty set otherwise."""
    uid = str(upload_id or "").strip()
    if not thread_id or not uid:
        return set()
    if registry is not None:
        return set(registry.upload_id_owners.get(uid) or ())
    if queries is not None:
        reg = _collect_thread_media_registry(thread_id, owner_id=owner_id, queries=queries)
        return set(reg.upload_id_owners.get(uid) or ())
    loaded = await _load_thread_media_registry_from_db(thread_id, owner_id=owner_id)
    if loaded is None:
        return set()
    return set(loaded.upload_id_owners.get(uid) or ())


_RAW_URL_SCAN_RE = re.compile(r"https?://[^\s<>'\"`)\]]+", re.IGNORECASE)
_RAW_PRIVATE_MEDIA_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_.:/@-])private-media/(?:response-images|attachments|widgets)/[^\s<>'\"`)\]]+",
    re.IGNORECASE,
)


def _strip_configured_storage_urls_from_text(text: str) -> str:
    if not text:
        return text
    out = text
    if "http" in out.lower():
        def _repl(match: re.Match[str]) -> str:
            raw = match.group(0)
            if _is_url_from_configured_storage(raw):
                return "[media-unavailable]"
            return raw

        out = _RAW_URL_SCAN_RE.sub(_repl, out)
    if "private-media/" in out.lower():
        out = _RAW_PRIVATE_MEDIA_REF_RE.sub("[media-unavailable]", out)
    return out


def _strip_configured_storage_urls_from_attachments(atts: list[Any]) -> tuple[list[Any], bool]:
    out: list[Any] = []
    changed = False
    for att in atts:
        if not isinstance(att, dict):
            out.append(att)
            continue
        att_copy = dict(att)
        old_u = att_copy.get("url")
        if isinstance(old_u, str) and old_u.strip() and (
            _is_url_from_configured_storage(old_u) or _is_private_media_ref(old_u)
        ):
            att_copy.pop("url", None)
            changed = True
        if "object_key" in att_copy:
            att_copy.pop("object_key", None)
            changed = True
        out.append(att_copy)
    return out, changed


def _strip_raw_storage_urls_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed scrubber used when DB media registry load fails (never leaves raw object URLs)."""
    data = item.get("data")
    if not isinstance(data, dict):
        return item
    copied = dict(item)
    new_data = dict(data)
    changed = False

    content = new_data.get("content")
    if isinstance(content, str):
        cleaned = _strip_configured_storage_urls_from_text(content)
        if cleaned != content:
            new_data["content"] = cleaned
            changed = True

    meta = new_data.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("attachments"), list):
        new_atts, att_changed = _strip_configured_storage_urls_from_attachments(meta["attachments"])
        if att_changed:
            new_data["metadata"] = {**meta, "attachments": new_atts}
            changed = True

    if isinstance(new_data.get("attachments"), list):
        new_top, top_changed = _strip_configured_storage_urls_from_attachments(new_data["attachments"])
        if top_changed:
            new_data["attachments"] = new_top
            changed = True

    if isinstance(new_data.get("messages"), list):
        new_msgs: list[Any] = []
        msgs_changed = False
        for msg in new_data["messages"]:
            if not isinstance(msg, dict):
                new_msgs.append(msg)
                continue
            msg_copy = dict(msg)
            m_changed = False
            mc = msg_copy.get("content")
            if isinstance(mc, str):
                cleaned_mc = _strip_configured_storage_urls_from_text(mc)
                if cleaned_mc != mc:
                    msg_copy["content"] = cleaned_mc
                    m_changed = True
            if isinstance(msg_copy.get("attachments"), list):
                na, ac = _strip_configured_storage_urls_from_attachments(msg_copy["attachments"])
                if ac:
                    msg_copy["attachments"] = na
                    m_changed = True
            mm = msg_copy.get("metadata")
            if isinstance(mm, dict) and isinstance(mm.get("attachments"), list):
                nma, mac = _strip_configured_storage_urls_from_attachments(mm["attachments"])
                if mac:
                    msg_copy["metadata"] = {**mm, "attachments": nma}
                    m_changed = True
            if m_changed:
                msgs_changed = True
            new_msgs.append(msg_copy)
        if msgs_changed:
            new_data["messages"] = new_msgs
            changed = True

    if changed:
        copied["data"] = new_data
        return copied
    return item


def _strip_raw_storage_urls_from_lines(lines: list[Any]) -> list[Any]:
    from src.server.services.history.replay.lines import Line, line_of

    out: list[Any] = []
    for ln in lines:
        if not isinstance(ln, Line):
            out.append(ln)
            continue
        if ln.event not in ("user_message", "message_chunk", "steering_delivered"):
            out.append(ln)
            continue
        low_json = ln.data_json.lower()
        if "http" not in low_json and "private-media/" not in low_json and '"object_key"' not in ln.data_json:
            out.append(ln)
            continue
        raw_item = ln.item()
        stripped = _strip_raw_storage_urls_from_item(raw_item)
        out.append(line_of(stripped) if stripped is not raw_item else ln)
    return out


def _extract_page_turn_indices(
    queries: Iterable[Mapping[str, Any]],
    items: Iterable[Mapping[str, Any]],
) -> list[int]:
    turns: set[int] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        data = item.get("data")
        t = data.get("turn_index") if isinstance(data, Mapping) else item.get("turn_index")
        if isinstance(t, int) and not isinstance(t, bool):
            turns.add(t)
    if turns:
        return sorted(turns)
    for q in queries:
        if not isinstance(q, Mapping):
            continue
        t = q.get("turn_index")
        if isinstance(t, int) and not isinstance(t, bool):
            turns.add(t)
    return sorted(turns)


async def resolve_thread_media_bindings(
    thread: Mapping[str, Any],
    queries: Iterable[Mapping[str, Any]],
    items: Iterable[Mapping[str, Any]],
    *,
    responses_by_turn: Mapping[Any, Mapping[str, Any]] | None = None,
    run_facts: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, tuple[str, str]] | None:
    """Collect verified URL/upload_id -> ``(backend, ref_id)`` bindings for a thread's media.

    Loads the authoritative ``ThreadMediaRegistry`` from DB via the same
    ``_load_thread_media_registry_from_db`` loader used by ``_serve_media_payload``
    (scoped to the page's turns when available). Returns ``None`` when the DB
    registry fails to load so callers fail closed without emitting raw object URLs.
    """
    thread_id = str(
        thread.get("conversation_thread_id") or thread.get("thread_id") or ""
    ).strip()
    if not thread_id:
        return {}
    owner_id = str(thread.get("user_id") or thread.get("owner_id") or "").strip() or None
    query_list = list(queries)
    item_list = list(items)

    page_turns = _extract_page_turn_indices(query_list, item_list)
    registry = await _load_thread_media_registry_from_db(
        thread_id,
        owner_id=owner_id,
        turn_indices=page_turns or None,
    )
    if registry is None:
        return None

    # Merge steering_delivered items from the current page (e.g., cold-projected before turn_slices persist).
    for item in item_list:
        if isinstance(item, Mapping) and item.get("event") == "steering_delivered":
            data = item.get("data")
            if isinstance(data, Mapping):
                _register_steering_messages_into_registry(
                    data.get("messages"),
                    registry,
                    default_owner_id=owner_id,
                )

    bindings: dict[str, tuple[str, str]] = {}

    async def _register_upstream_url(
        u: Any,
        *,
        allow_staged_attachment: bool,
        require_captured_registry: bool = False,
        att_owner_ids: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(u, str) or not u.strip():
            return
        if not (_is_url_from_configured_storage(u) or _is_private_media_ref(u)):
            return
        key = extract_upstream_key_from_url(u)
        if not key or not _is_safe_object_key(key):
            return
        cleaned = strip_private_media_prefix(key)
        if cleaned.startswith("response-images/"):
            if _is_thread_prefixed_upstream_key(thread_id, key) and (
                not require_captured_registry or cleaned in registry.captured_image_keys
            ):
                bindings[u] = ("upstream", key)
            return
        if cleaned.startswith("widgets/"):
            if _is_thread_prefixed_upstream_key(thread_id, key):
                bindings[u] = ("upstream", key)
            return
        if cleaned.startswith("attachments/"):
            if cleaned.startswith(f"attachments/{thread_id}/"):
                bindings[u] = ("upstream", key)
            elif allow_staged_attachment and await _is_attachment_bound_to_thread(
                thread_id,
                key,
                owner_id=owner_id,
                allowed_owner_ids=att_owner_ids,
                registry=registry,
            ):
                bindings[u] = ("upstream", key)

    # 1. Server-registered query & steering attachments from the shared DB registry
    for att_url in registry.attachment_urls:
        k = extract_upstream_key_from_url(att_url)
        cleaned = strip_private_media_prefix(k) if k else ""
        att_id = cleaned.split("/")[1] if cleaned.startswith("attachments/") and len(cleaned.split("/")) >= 3 else ""
        allowed_owners = (
            registry.attachment_owners.get(cleaned)
            or registry.attachment_owners.get(att_id)
            or None
        )
        await _register_upstream_url(
            att_url,
            allow_staged_attachment=True,
            att_owner_ids=allowed_owners,
        )

    # 1b. Private-mode attachment object_key references (public_bucket_urls=false)
    for att_okey in registry.attachment_object_keys:
        if not _is_safe_object_key(att_okey):
            continue
        cleaned = strip_private_media_prefix(att_okey)
        if not cleaned.startswith("attachments/"):
            continue
        parts = cleaned.split("/")
        att_id = parts[1] if len(parts) >= 3 else ""
        allowed_owners = (
            registry.attachment_owners.get(cleaned)
            or registry.attachment_owners.get(att_id)
            or None
        )
        if cleaned.startswith(f"attachments/{thread_id}/"):
            bindings[f"object_key:{att_okey}"] = ("upstream", att_okey)
        elif await _is_attachment_bound_to_thread(
            thread_id,
            att_okey,
            owner_id=owner_id,
            allowed_owner_ids=allowed_owners,
            registry=registry,
        ):
            bindings[f"object_key:{att_okey}"] = ("upstream", att_okey)

    # 2. Server-recorded captured sandbox images from DB replay_facts ONLY (never free-text scan)
    for img_url in registry.captured_image_urls:
        await _register_upstream_url(
            img_url,
            allow_staged_attachment=False,
            require_captured_registry=True,
        )

    # 3. Persisted chat_upload IDs verified against workspace_id AND actual turn sender / uploader user_id
    ws_id = str(thread.get("workspace_id") or "")
    if registry.upload_id_owners and ws_id:
        all_allowed_users = {
            u for users in registry.upload_id_owners.values() for u in users if u
        }
        if all_allowed_users:
            try:
                ready_map = await _fetch_ready_chat_uploads(
                    ws_id,
                    registry.upload_id_owners.keys(),
                    allowed_user_ids=all_allowed_users,
                )
                for uid, row in ready_map.items():
                    row_user = str(row.get("user_id") or "").strip()
                    expected_users = registry.upload_id_owners.get(uid) or set()
                    if row_user and row_user in expected_users:
                        bindings[f"upload_id:{uid}"] = ("chat_upload", uid)
            except Exception:
                logger.debug("Could not resolve chat upload bindings for thread", exc_info=True)

    return bindings


def _rewrite_attachment_list(
    atts: list[Any],
    url_map: Mapping[str, str],
    upload_id_map: Mapping[str, str],
    object_key_map: Mapping[str, str] | None = None,
) -> tuple[list[Any], bool]:
    new_atts: list[Any] = []
    att_changed = False
    okey_map = object_key_map or {}
    for att in atts:
        if not isinstance(att, dict):
            new_atts.append(att)
            continue
        att_copy = dict(att)
        old_u = att_copy.get("url")
        raw_okey = att_copy.pop("object_key", None)
        if raw_okey is not None:
            att_changed = True
        if isinstance(old_u, str) and old_u in url_map:
            att_copy["url"] = url_map[old_u]
            att_changed = True
        elif not att_copy.get("url") and isinstance(raw_okey, str) and raw_okey.strip():
            clean_okey = raw_okey.strip().lstrip("/")
            if clean_okey in okey_map:
                att_copy["url"] = okey_map[clean_okey]
                att_changed = True
        elif not att_copy.get("url") and att_copy.get("upload_id"):
            uid = str(att_copy["upload_id"])
            if uid in upload_id_map:
                att_copy["url"] = upload_id_map[uid]
                att_changed = True
        new_atts.append(att_copy)
    return new_atts, att_changed


def _rewrite_item_urls(
    item: dict[str, Any],
    url_map: Mapping[str, str],
    upload_id_map: Mapping[str, str],
    object_key_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not url_map and not upload_id_map and not object_key_map:
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
        new_atts, att_changed = _rewrite_attachment_list(
            meta["attachments"], url_map, upload_id_map, object_key_map
        )
        if att_changed:
            new_data["metadata"] = {**meta, "attachments": new_atts}
            changed = True

    if isinstance(new_data.get("attachments"), list):
        new_top_atts, top_changed = _rewrite_attachment_list(
            new_data["attachments"], url_map, upload_id_map, object_key_map
        )
        if top_changed:
            new_data["attachments"] = new_top_atts
            changed = True

    # Rewrite interstitial steering_delivered messages[] using the exact same logic
    if isinstance(new_data.get("messages"), list):
        new_msgs: list[Any] = []
        msgs_changed = False
        for msg in new_data["messages"]:
            if not isinstance(msg, dict):
                new_msgs.append(msg)
                continue
            msg_copy = dict(msg)
            m_changed = False
            m_content = msg_copy.get("content")
            if isinstance(m_content, str) and url_map:
                updated_mc = m_content
                for old_url, new_url in url_map.items():
                    if old_url in updated_mc:
                        updated_mc = updated_mc.replace(old_url, new_url)
                if updated_mc != m_content:
                    msg_copy["content"] = updated_mc
                    m_changed = True
            if isinstance(msg_copy.get("attachments"), list):
                rewritten_m_atts, r_changed = _rewrite_attachment_list(
                    msg_copy["attachments"], url_map, upload_id_map, object_key_map
                )
                if r_changed:
                    msg_copy["attachments"] = rewritten_m_atts
                    m_changed = True
            m_meta = msg_copy.get("metadata")
            if isinstance(m_meta, dict) and isinstance(m_meta.get("attachments"), list):
                rewritten_mm_atts, rm_changed = _rewrite_attachment_list(
                    m_meta["attachments"], url_map, upload_id_map, object_key_map
                )
                if rm_changed:
                    msg_copy["metadata"] = {**m_meta, "attachments": rewritten_mm_atts}
                    m_changed = True
            if m_changed:
                msgs_changed = True
            new_msgs.append(msg_copy)
        if msgs_changed:
            new_data["messages"] = new_msgs
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
    bindings: Mapping[str, tuple[str, str]] | None,
) -> list[dict[str, Any]]:
    """Rewrite stored URLs, ``object_key`` refs, and ``upload_id``-only attachments to ``/api/v1/public/shared/{token}/media/{ref}``.

    Fail-closed: when ``bindings is None`` or any configured-storage URL could
    not be signed into a controlled media ref, that raw object URL is erased
    rather than left verbatim in the replay payload.
    """
    if bindings is None:
        return [_strip_raw_storage_urls_from_item(it) for it in items]
    url_map: dict[str, str] = {}
    upload_id_map: dict[str, str] = {}
    object_key_map: dict[str, str] = {}
    for key, (backend, ref_id) in bindings.items():
        ref = mint_public_media_ref(thread_id=thread_id, backend=backend, ref_id=ref_id)
        controlled = f"/api/v1/public/shared/{share_token}/media/{ref}"
        if key.startswith("upload_id:"):
            upload_id_map[key[len("upload_id:") :]] = controlled
        elif key.startswith("object_key:"):
            object_key_map[key[len("object_key:") :]] = controlled
        else:
            url_map[key] = controlled
    out: list[dict[str, Any]] = []
    for it in items:
        rewritten = (
            _rewrite_item_urls(it, url_map, upload_id_map, object_key_map)
            if (url_map or upload_id_map or object_key_map)
            else it
        )
        out.append(_strip_raw_storage_urls_from_item(rewritten))
    return out


def rewrite_lines_for_owner_media(
    lines: list[Any],
    *,
    thread_id: str,
    viewer_id: str,
    bindings: Mapping[str, tuple[str, str]] | None,
    sid_hash: str = "",
    origin_hash: str = "",
) -> list[Any]:
    """Rewrite stored media URLs and private ``object_key`` refs in owner replay ``Line``s to ``/api/v1/threads/{id}/media/{token}``.

    Fail-closed: when ``bindings is None`` or any configured-storage URL could
    not be signed into a controlled media token, that raw object URL is erased
    rather than left verbatim in the replay payload.
    """
    from src.server.services.history.replay.lines import Line, line_of

    if bindings is None:
        return _strip_raw_storage_urls_from_lines(lines)
    url_map: dict[str, str] = {}
    upload_id_map: dict[str, str] = {}
    object_key_map: dict[str, str] = {}
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
        elif key.startswith("object_key:"):
            object_key_map[key[len("object_key:") :]] = controlled
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
        needs_rewrite = (
            (url_map and any(u in ln.data_json for u in url_map))
            or (bool(upload_id_map) and '"upload_id"' in ln.data_json)
            or (bool(object_key_map) and '"object_key"' in ln.data_json)
        )
        low_json = ln.data_json.lower()
        has_raw = "http" in low_json or "private-media/" in low_json or '"object_key"' in ln.data_json
        if not needs_rewrite and not has_raw:
            out.append(ln)
            continue
        raw_item = ln.item()
        rewritten = (
            _rewrite_item_urls(raw_item, url_map, upload_id_map, object_key_map)
            if needs_rewrite
            else raw_item
        )
        cleaned = _strip_raw_storage_urls_from_item(rewritten)
        out.append(line_of(cleaned) if cleaned is not raw_item else ln)
    return out


def _rewrite_live_image_map(
    img_map: Mapping[str, Any],
    *,
    thread_id: str,
    viewer_id: str,
    sid_hash: str,
    origin_hash: str,
) -> tuple[dict[str, str], bool]:
    rewritten: dict[str, str] = {}
    changed = False
    for path, raw_val in img_map.items():
        if not isinstance(raw_val, str) or not raw_val.strip():
            continue
        val = raw_val.strip()
        if _is_private_media_ref(val) or _is_url_from_configured_storage(val):
            key = extract_upstream_key_from_url(val)
            if key and _is_thread_prefixed_upstream_key(thread_id, key):
                tok = mint_owner_media_token(
                    thread_id=thread_id,
                    viewer_id=viewer_id,
                    backend="upstream",
                    ref_id=key,
                    sid_hash=sid_hash,
                    origin_hash=origin_hash,
                )
                rewritten[str(path)] = f"/api/v1/threads/{thread_id}/media/{tok}"
                changed = True
                continue
            changed = True
            continue
        rewritten[str(path)] = val
    return rewritten, changed


def rewrite_live_run_end_sse_frame(
    frame: str,
    *,
    thread_id: str,
    viewer_id: str,
    sid_hash: str = "",
    origin_hash: str = "",
) -> str:
    """Rewrite private/storage image references in a live ``run_end`` SSE frame to AU1 owner media URLs."""
    if (
        not isinstance(frame, str)
        or "event: run_end" not in frame
        or ('"images"' not in frame and '"images_by_message"' not in frame)
    ):
        return frame
    lines = frame.splitlines(keepends=True)
    changed = False
    out_lines: list[str] = []
    for line in lines:
        if line.startswith("data: "):
            raw_json = line[len("data: ") :].strip()
            try:
                payload = json.loads(raw_json)
            except Exception:
                out_lines.append(line)
                continue
            if not isinstance(payload, dict):
                out_lines.append(line)
                continue
            if isinstance(payload.get("images"), Mapping):
                new_imgs, imgs_changed = _rewrite_live_image_map(
                    payload["images"],
                    thread_id=thread_id,
                    viewer_id=viewer_id,
                    sid_hash=sid_hash,
                    origin_hash=origin_hash,
                )
                if imgs_changed:
                    payload["images"] = new_imgs
                    changed = True
            if isinstance(payload.get("images_by_message"), Mapping):
                new_by_msg: dict[str, dict[str, str]] = {}
                by_msg_changed = False
                for mid, per_map in payload["images_by_message"].items():
                    if isinstance(per_map, Mapping):
                        new_per, p_changed = _rewrite_live_image_map(
                            per_map,
                            thread_id=thread_id,
                            viewer_id=viewer_id,
                            sid_hash=sid_hash,
                            origin_hash=origin_hash,
                        )
                        if p_changed:
                            by_msg_changed = True
                        if new_per:
                            new_by_msg[str(mid)] = new_per
                if by_msg_changed:
                    payload["images_by_message"] = new_by_msg
                    changed = True
            if changed:
                ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                out_lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}{ending}")
                continue
        out_lines.append(line)
    return "".join(out_lines) if changed else frame


def wrap_owner_live_sse_media(
    stream: Any,
    request: Request | None,
    *,
    thread_id: str,
    viewer_id: str,
) -> tuple[Any, list[str]]:
    """Wrap a live/reconnect owner SSE stream to rewrite ``run_end`` media refs to AU1 URLs."""
    set_cookies: list[str] = []
    sid_hash, origin_hash = "", ""
    if request is not None:
        cookie_holder = Response()
        oss_session = mint_oss_media_session_cookie(request, cookie_holder)
        if oss_session:
            sid_hash, origin_hash = oss_session
        set_cookies = cookie_holder.headers.getlist("set-cookie")

    async def _wrapped():
        async for frame in stream:
            yield rewrite_live_run_end_sse_frame(
                frame,
                thread_id=thread_id,
                viewer_id=viewer_id,
                sid_hash=sid_hash,
                origin_hash=origin_hash,
            )

    return _wrapped(), set_cookies


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
    rows_owner = getattr(rows, "owner_id", None)
    if rows_owner and not thread_dict.get("user_id"):
        thread_dict["user_id"] = str(rows_owner)
    queries = getattr(rows, "queries", None) or ()

    bindings = await resolve_thread_media_bindings(
        thread_dict,
        queries,
        [
            ln.item()
            for ln in replay_lines
            if ln.event in ("user_message", "message_chunk", "steering_delivered")
        ],
    )
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
            registry = await _load_thread_media_registry_from_db(
                thread_id, owner_id=thread_owner_id or None
            )
            if registry is None or cleaned not in registry.captured_image_keys:
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

        allowed_users = await _is_chat_upload_bound_to_thread(
            thread_id, ref_id, owner_id=thread_owner_id or None
        )
        if not allowed_users:
            raise HTTPException(status_code=404, detail="Media not found", headers=err_headers)
        ready = await _fetch_ready_chat_uploads(
            workspace_id, [ref_id], allowed_user_ids=allowed_users
        )
        row = ready.get(str(ref_id))
        if row is None or str(row.get("user_id") or "").strip() not in allowed_users:
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
    gate: tuple[dict[str, Any], str] = Depends(_owner_media_caller_gate),
) -> Response:
    """Serve controlled session media to an authenticated thread viewer."""
    from src.server.database.conversation import get_thread_by_id

    claims, _viewer_id = gate

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
