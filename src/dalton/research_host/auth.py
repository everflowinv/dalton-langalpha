"""Three-class credential verification for ``dalton-research-host`` (§0.2 #2, §2.2 #2).

Strictly separates:
1. Sandbox read-only MCP token (``DALTON_RESEARCH_HOST_TOKEN``) — accepted ONLY on
   ``/mcp/<connector>`` endpoints.
2. Per-request caller identity token (``X-Dalton-Caller-Token`` with
   ``X-Dalton-Caller-Scope: per-request``) — HMAC-SHA256 verified using an HKDF
   subkey derived from the admin secret; invalid/missing tokens fall into the
   anonymous bucket (``verified=False``) and never elevate privileges.
3. Backend-only admin token (``DALTON_RESEARCH_ADMIN_TOKEN``) — accepted ONLY on
   ``/ingest/*`` and ``/admin/*`` endpoints; never accepts the sandbox MCP token.
"""

from __future__ import annotations

import base64
from contextvars import ContextVar
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request

_CALLER_TOKEN_HKDF_INFO = b"dalton-bridge-caller-v1"
CURRENT_CALLER_HEADERS: ContextVar[dict[str, str]] = ContextVar("dalton_caller_headers", default={})


@dataclass(frozen=True)
class CallerIdentity:
    """Verified (or anonymous) per-request caller identity from sandbox headers."""

    verified: bool
    user: str = ""
    workspace: str = ""
    thread: str = ""
    run_origin: str = ""

    @property
    def caller_key(self) -> str:
        if self.verified and self.user:
            return f"user:{self.user}"
        return "anonymous"


def extract_bearer_or_header(request: Request, header_name: str) -> str:
    """Extract token from ``Authorization: Bearer <tok>`` or a dedicated header."""
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get(header_name, "").strip()


def verify_constant_time_token(presented: str, expected: str) -> bool:
    """Fail-closed constant-time token check."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _derive_caller_hmac_key(admin_secret: str) -> bytes | None:
    secret = str(admin_secret or "").strip()
    if not secret:
        return None
    prk = hmac.new(b"\x00" * 32, secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, _CALLER_TOKEN_HKDF_INFO + b"\x01", hashlib.sha256).digest()


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def verify_caller_token(
    token: str,
    *,
    admin_secrets: tuple[str, ...],
    now: float | None = None,
) -> CallerIdentity:
    """Verify ``X-Dalton-Caller-Token`` against one or more candidate admin secrets."""
    cleaned = str(token or "").strip()
    if not cleaned or "." not in cleaned:
        return CallerIdentity(verified=False)
    parts = cleaned.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return CallerIdentity(verified=False)
    payload_b64, sig_b64 = parts
    if not payload_b64.isascii() or not sig_b64.isascii():
        return CallerIdentity(verified=False)

    try:
        payload_ascii = payload_b64.encode("ascii")
        presented_sig = _b64url_decode(sig_b64)
    except Exception:
        return CallerIdentity(verified=False)

    matched = False
    for secret in admin_secrets:
        key = _derive_caller_hmac_key(secret)
        if key is None:
            continue
        expected_sig = hmac.new(key, payload_ascii, hashlib.sha256).digest()
        if hmac.compare_digest(presented_sig, expected_sig):
            matched = True
            break
    if not matched:
        return CallerIdentity(verified=False)

    try:
        payload: Any = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return CallerIdentity(verified=False)
    if not isinstance(payload, dict):
        return CallerIdentity(verified=False)

    exp = payload.get("exp")
    now_ts = time.time() if now is None else float(now)
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or float(exp) < now_ts:
        return CallerIdentity(verified=False)

    return CallerIdentity(
        verified=True,
        user=str(payload.get("user") or "").strip(),
        workspace=str(payload.get("workspace") or "").strip(),
        thread=str(payload.get("thread") or "").strip(),
        run_origin="automation" if str(payload.get("run_origin") or "") == "automation" else "",
    )


def resolve_caller_identity(
    request: Request,
    *,
    admin_secrets: tuple[str, ...],
) -> CallerIdentity:
    """Extract and verify per-request caller identity headers (anonymous on mismatch)."""
    scope = request.headers.get("x-dalton-caller-scope", "").strip().lower()
    if scope != "per-request":
        return CallerIdentity(verified=False)
    token = request.headers.get("x-dalton-caller-token", "").strip()
    if not token:
        return CallerIdentity(verified=False)
    return verify_caller_token(token, admin_secrets=admin_secrets)
