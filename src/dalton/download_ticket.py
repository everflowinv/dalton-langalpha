"""Short-lived (60s) HMAC download tickets for authenticated large workspace files (§1.3 #4).

When ``STORAGE_BROWSER_ENDPOINT_URL`` is unset (for example, an internal MinIO
endpoint ``http://minio:9000`` unreachable from browsers), presigned browser
links are skipped and ``/files/download-url`` issues a 60-second reusable
HMAC-SHA256 ticket (``dl_token``) for owner files larger than
``LIVE_LINK_MIN_BYTES`` (8 MiB). The browser follows a plain ``GET`` to
``/api/v1/dalton/workspace-files/download?dl_token=...`` so the file streams
directly to disk without buffering in tab memory.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, unquote_plus

from fastapi import HTTPException

from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_internal_service_token,
    get_jwt_signing_secret,
)
from src.server.database.workspace_file import get_file_locator
from src.server.services.workspace_file_containment import read_contained_sandbox_range

logger = logging.getLogger(__name__)

DOWNLOAD_TICKET_VERSION = 1
DOWNLOAD_TICKET_TYP = "wsfile_dl"
DOWNLOAD_TICKET_TTL_SECONDS = 60
DOWNLOAD_TICKET_CLOCK_SKEW_SECONDS = 5
DOWNLOAD_TICKET_HMAC_DOMAIN = b"dalton-wsfile-download-ticket-v1"
DOWNLOAD_TICKET_MIN_BYTES = 8 * 1024 * 1024  # matches download_link.LIVE_LINK_MIN_BYTES
INVALID_DOWNLOAD_TICKET_DETAIL = "Invalid or expired download ticket"


@dataclass(frozen=True)
class DownloadTicketClaims:
    """Verified claims from a ``dl_token`` query parameter."""

    user_id: str
    workspace_id: str
    path: str
    backup_ok: bool
    expires_at: int
    nonce: str


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    pad = "=" * ((4 - len(encoded) % 4) % 4)
    return base64.urlsafe_b64decode(encoded + pad)


def _derive_ticket_hmac_key() -> bytes | None:
    """Derive domain-separated HMAC-SHA256 key from existing server secrets.

    Seed priority (first non-empty):
    1. ``JWT_SIGNING_SECRET`` (``get_jwt_signing_secret()``)
    2. ``INTERNAL_SERVICE_TOKEN`` (``get_internal_service_token()``)
    3. ``DALTON_BRIDGE_ADMIN_TOKEN`` (``get_dalton_bridge_admin_token()``)

    Returns ``None`` if all three are unset/empty (no new env var required;
    download tickets are simply not issued).
    """
    seed = (
        get_jwt_signing_secret()
        or get_internal_service_token()
        or get_dalton_bridge_admin_token()
    )
    if not seed:
        return None
    return hmac.new(
        seed.encode("utf-8"), DOWNLOAD_TICKET_HMAC_DOMAIN, hashlib.sha256
    ).digest()


def has_browser_endpoint_url() -> bool:
    """Return True iff ``STORAGE_BROWSER_ENDPOINT_URL`` is configured and non-empty."""
    if "STORAGE_BROWSER_ENDPOINT_URL" in os.environ:
        return bool((os.environ.get("STORAGE_BROWSER_ENDPOINT_URL") or "").strip())
    from src.utils.storage.s3_client import StorageConfig

    return bool((getattr(StorageConfig, "BROWSER_ENDPOINT_URL", None) or "").strip())


def issue_download_ticket(
    *,
    user_id: str,
    workspace_id: str,
    path: str,
    backup_ok: bool,
    ttl_seconds: int = DOWNLOAD_TICKET_TTL_SECONDS,
    now: int | None = None,
) -> str | None:
    """Mint a reusable 60-second HMAC-SHA256 download ticket, or ``None`` if no key."""
    hmac_key = _derive_ticket_hmac_key()
    if hmac_key is None:
        return None

    uid = str(user_id or "").strip()
    ws = str(workspace_id or "").strip()
    raw_path = str(path or "")
    if not uid or not ws or not raw_path:
        return None

    issued_at = int(time.time()) if now is None else int(now)
    ttl = max(1, int(ttl_seconds))
    payload = {
        "v": DOWNLOAD_TICKET_VERSION,
        "typ": DOWNLOAD_TICKET_TYP,
        "uid": uid,
        "ws": ws,
        "path": raw_path,
        "backup_ok": bool(backup_ok),
        "exp": issued_at + ttl,
        "nonce": secrets.token_urlsafe(16),
    }
    payload_bytes = json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(hmac_key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def verify_download_ticket(
    token: str,
    *,
    now: int | None = None,
) -> DownloadTicketClaims:
    """Verify ``dl_token`` and return claims; raise uniform 401 on any failure."""
    def _reject() -> HTTPException:
        return HTTPException(status_code=401, detail=INVALID_DOWNLOAD_TICKET_DETAIL)

    cleaned = str(token or "").strip()
    if not cleaned:
        raise _reject()

    hmac_key = _derive_ticket_hmac_key()
    if hmac_key is None:
        raise _reject()

    parts = cleaned.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise _reject()

    payload_b64, sig_b64 = parts[0], parts[1]
    try:
        payload_b64_bytes = payload_b64.encode("ascii")
        supplied_sig = _b64url_decode(sig_b64)
    except Exception:
        raise _reject() from None

    expected_sig = hmac.new(hmac_key, payload_b64_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_sig, expected_sig):
        raise _reject()

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        raise _reject() from None

    if not isinstance(payload, dict):
        raise _reject()
    if payload.get("v") != DOWNLOAD_TICKET_VERSION or payload.get("typ") != DOWNLOAD_TICKET_TYP:
        raise _reject()

    uid = payload.get("uid")
    ws = payload.get("ws")
    raw_path = payload.get("path")
    backup_ok = payload.get("backup_ok")
    exp = payload.get("exp")
    nonce = payload.get("nonce")

    if (
        not isinstance(uid, str)
        or not uid.strip()
        or not isinstance(ws, str)
        or not ws.strip()
        or not isinstance(raw_path, str)
        or not raw_path
        or not isinstance(backup_ok, bool)
        or not isinstance(exp, int)
        or isinstance(exp, bool)
        or not isinstance(nonce, str)
        or not nonce.strip()
    ):
        raise _reject()

    now_ts = int(time.time()) if now is None else int(now)
    if now_ts > exp + DOWNLOAD_TICKET_CLOCK_SKEW_SECONDS:
        raise _reject()

    return DownloadTicketClaims(
        user_id=uid.strip(),
        workspace_id=ws.strip(),
        path=raw_path,
        backup_ok=backup_ok,
        expires_at=exp,
        nonce=nonce.strip(),
    )


async def maybe_attach_live_download_ticket(
    response_payload: dict[str, str | None],
    *,
    user_id: str,
    workspace_id: str,
    path: str,
    backup_ok: bool,
    sandbox: Any,
    canonical: str,
    work_dir: str,
) -> dict[str, str | None]:
    """Attach ``dl_token`` for running-workspace files ``> 8 MiB`` when ``url`` is None."""
    if response_payload.get("url") is not None:
        return response_payload

    try:
        sliced = await read_contained_sandbox_range(
            sandbox, canonical, work_dir=work_dir, offset=0, length=1
        )
    except Exception as exc:
        logger.debug(
            "Skipping download ticket for live %s in %s: could not stat size (%s)",
            path,
            workspace_id,
            exc,
        )
        return response_payload

    if sliced is None or sliced.size <= DOWNLOAD_TICKET_MIN_BYTES:
        return response_payload

    token = issue_download_ticket(
        user_id=user_id,
        workspace_id=workspace_id,
        path=path,
        backup_ok=backup_ok,
    )
    if not token:
        return response_payload
    return {**response_payload, "dl_token": token}


async def maybe_attach_backup_download_ticket(
    response_payload: dict[str, str | None],
    *,
    user_id: str,
    workspace_id: str,
    path: str,
    normalized_path: str,
    backup_ok: bool,
) -> dict[str, str | None]:
    """Attach ``dl_token`` for backup/stopped-workspace files ``> 8 MiB`` when ``url`` is None."""
    if response_payload.get("url") is not None or not normalized_path:
        return response_payload

    try:
        locator = await get_file_locator(workspace_id, normalized_path)
    except Exception as exc:
        logger.debug(
            "Skipping download ticket for backup %s in %s: locator read failed (%s)",
            path,
            workspace_id,
            exc,
        )
        return response_payload

    file_size = locator.get("file_size") if isinstance(locator, dict) else None
    if not isinstance(file_size, int) or file_size <= DOWNLOAD_TICKET_MIN_BYTES:
        return response_payload

    token = issue_download_ticket(
        user_id=user_id,
        workspace_id=workspace_id,
        path=path,
        backup_ok=backup_ok,
    )
    if not token:
        return response_payload
    return {**response_payload, "dl_token": token}


# ---------------------------------------------------------------------------
# Uvicorn access-log & OpenTelemetry query stripping for the redemption route
# ---------------------------------------------------------------------------

DOWNLOAD_TICKET_ROUTE_PATH = "/api/v1/dalton/workspace-files/download"
_URL_WITH_QUERY_RE = re.compile(r"[^\s\"'<>#]+\?[^\s\"'<>#]*")
_MULTI_SLASH_RE = re.compile(r"/+")


def _decode_percent_layers(text: str) -> str:
    decoded = text
    for _ in range(4):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    return decoded


def is_download_ticket_path(path: str) -> bool:
    """Return True iff ``path`` targets the download-ticket redemption route.

    Handles optional mount prefixes, trailing/duplicate slashes, full URLs,
    and percent-encoded path characters so URL-encoded bypasses cannot evade
    query-string stripping in access logs or OpenTelemetry spans.
    """
    if not isinstance(path, str) or not path:
        return False
    decoded = _decode_percent_layers(path)
    normalized = _MULTI_SLASH_RE.sub("/", decoded).rstrip("/")
    return (
        normalized == DOWNLOAD_TICKET_ROUTE_PATH
        or normalized.endswith(DOWNLOAD_TICKET_ROUTE_PATH)
        or f"{DOWNLOAD_TICKET_ROUTE_PATH}/" in f"{normalized}/"
    )


def _redact_encoded_dl_token_params(query: str) -> str:
    """Defense-in-depth redaction for any non-redemption URL carrying ``dl_token`` (even URL-encoded)."""
    if not query:
        return query
    parts = query.split("&")
    changed = False
    out: list[str] = []
    for part in parts:
        key, sep, _val = part.partition("=")
        decoded_key = unquote_plus(key).strip().lower()
        if sep and decoded_key == "dl_token":
            out.append(f"{key}=REDACTED")
            changed = True
        else:
            out.append(part)
    return "&".join(out) if changed else query


def redact_dl_token_in_text(text: str) -> str:
    """Drop the entire query string on the download-ticket redemption route.

    For any token ``<path>?<query>`` where ``<path>`` targets
    :data:`DOWNLOAD_TICKET_ROUTE_PATH` (including percent-encoded path or
    query-parameter names such as ``?%64l_token=...``), drops ``?<query>``
    completely. For unrelated URLs, falls back to masking any plain or
    percent-encoded ``dl_token`` parameter value.
    """
    if not isinstance(text, str) or "?" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        path_part, _sep, query_part = token.partition("?")
        if is_download_ticket_path(path_part):
            return path_part
        redacted_query = _redact_encoded_dl_token_params(query_part)
        return token if redacted_query == query_part else f"{path_part}?{redacted_query}"

    return _URL_WITH_QUERY_RE.sub(_replace, text)


class RedactDownloadTicketLogFilter(logging.Filter):
    """``logging.Filter`` for ``uvicorn.access`` that strips queries on the download-ticket route."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "?" in record.msg:
            record.msg = redact_dl_token_in_text(record.msg)
        args = record.args
        if isinstance(args, tuple) and args:
            mutated = False
            new_args = []
            for item in args:
                if isinstance(item, str) and "?" in item:
                    cleaned = redact_dl_token_in_text(item)
                    new_args.append(cleaned)
                    if cleaned != item:
                        mutated = True
                else:
                    new_args.append(item)
            if mutated:
                record.args = tuple(new_args)
        elif isinstance(args, dict) and args:
            for k, v in list(args.items()):
                if isinstance(v, str) and "?" in v:
                    args[k] = redact_dl_token_in_text(v)
        return True


def install_download_ticket_log_filter(
    target_logger: logging.Logger | None = None,
) -> RedactDownloadTicketLogFilter:
    """Attach :class:`RedactDownloadTicketLogFilter` idempotently to ``uvicorn.access``."""
    access_logger = target_logger or logging.getLogger("uvicorn.access")
    for existing in access_logger.filters:
        if isinstance(existing, RedactDownloadTicketLogFilter):
            return existing
    flt = RedactDownloadTicketLogFilter()
    access_logger.addFilter(flt)
    return flt


def build_uvicorn_log_config() -> dict[str, Any]:
    """Return Uvicorn ``LOGGING_CONFIG`` extended with ``dl_token`` redaction."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    filters = cfg.setdefault("filters", {})
    filters["dalton_redact_dl_token"] = {
        "()": "src.dalton.download_ticket.RedactDownloadTicketLogFilter",
    }
    access_logger = cfg.setdefault("loggers", {}).setdefault("uvicorn.access", {})
    access_filters = list(access_logger.get("filters") or [])
    if "dalton_redact_dl_token" not in access_filters:
        access_filters.append("dalton_redact_dl_token")
    access_logger["filters"] = access_filters
    return cfg
