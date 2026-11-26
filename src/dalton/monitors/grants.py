"""Signed path grants for sandboxed monitor iframes and 1-minute realtime data polling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from src.server.services.signed_grants import COUNT, GrantCodec

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MONITOR_GRANT_ROUTE",
    "ROOT_NONCE_RE",
    "MonitorGrant",
    "MonitorGrantError",
    "VerifiedMonitorGrant",
    "compute_public_root_proof",
    "data_prefix",
    "frame_prefix",
    "mint_monitor_grant",
    "mint_public_root_challenge",
    "verify_monitor_grant",
]

DEFAULT_TTL_SECONDS = 12 * 3600
MONITOR_GRANT_ROUTE = "/api/v1/dalton/monitors/g/"
ROOT_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_MONITOR_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_B64_USER = re.compile(r"[A-Za-z0-9_-]{1,512}")


class MonitorGrantError(Exception):
    """The presented monitor grant is invalid, expired, or malformed."""


_MONITOR_CODEC = GrantCodec(
    key_name="monitor_grant",
    version="m1",
    claim_patterns=(_MONITOR_ID, _B64_USER, COUNT),
    error=MonitorGrantError,
    label="monitor grant",
)


def _encode_user_id(user_id: str) -> str:
    return base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_user_id(encoded: str) -> str:
    pad = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode((encoded + pad).encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise MonitorGrantError("malformed user claim") from exc


@dataclass(frozen=True)
class MonitorGrant:
    token: str
    monitor_id: str
    user_id: str
    acl_revision: int
    expires_at: int


@dataclass(frozen=True)
class VerifiedMonitorGrant:
    monitor_id: str
    user_id: str
    acl_revision: int
    expires_at: int


async def mint_monitor_grant(
    monitor_id: str,
    user_id: str,
    acl_revision: int,
    *,
    ttl: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> MonitorGrant:
    """Mint a 12h grant bound to ``(monitor_id, user_id, acl_revision, exp)``."""
    encoded_user = _encode_user_id(user_id)
    token, expires_at = await _MONITOR_CODEC.mint(
        (monitor_id, encoded_user, str(acl_revision)),
        ttl=ttl,
        now=now,
    )
    return MonitorGrant(
        token=token,
        monitor_id=monitor_id,
        user_id=user_id,
        acl_revision=acl_revision,
        expires_at=expires_at,
    )


async def verify_monitor_grant(
    token: str, *, now: int | None = None
) -> VerifiedMonitorGrant:
    """Verify a monitor grant's signature, expiry, and claim format."""
    (monitor_id, encoded_user, acl_rev_str), expires_at = await _MONITOR_CODEC.verify(
        token, now=now
    )
    user_id = _decode_user_id(encoded_user)
    if not user_id:
        raise MonitorGrantError("empty user claim")
    return VerifiedMonitorGrant(
        monitor_id=monitor_id,
        user_id=user_id,
        acl_revision=int(acl_rev_str),
        expires_at=expires_at,
    )


def frame_prefix(grant: MonitorGrant | str) -> str:
    token = grant if isinstance(grant, str) else grant.token
    return f"{MONITOR_GRANT_ROUTE}{token}/"


def data_prefix(grant: MonitorGrant | str) -> str:
    return f"{frame_prefix(grant)}data"


async def compute_public_root_proof(slug: str, seq: int, nonce: str) -> str:
    """Compute a domain-separated HMAC proof binding a public monitor root entry to a one-time nonce."""
    key = await _MONITOR_CODEC._signing_key()
    message = "\x00".join(("public-monitor-root-v1", slug, str(seq), nonce))
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def mint_public_root_challenge(slug: str, seq: int) -> tuple[str, str]:
    """Mint a one-time ``(root_nonce, root_proof)`` pair for an opaque-sandbox public monitor root."""
    nonce = secrets.token_urlsafe(18)
    proof = await compute_public_root_proof(slug, seq, nonce)
    return nonce, proof
