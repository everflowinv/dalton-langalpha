"""The market-data token each computer's sandbox holds.

The server mints from the platform a short-lived token bound to the computer,
with no way to refresh it, and writes it into the sandbox, whose data client
only reads it. A turn's start and a background sweep each write a fresh one
once less than ``REFRESH_BELOW`` is left, and a stop or delete revokes every
token the computer was given, so one taken out of the sandbox stops working
within the hour, or as soon as the computer stops.

Nothing here raises: without a token the market-data tools answer that they
are not configured, which costs data, never a turn or a start.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.config.env import AUTH_SERVICE_URL, GINLIX_DATA_URL, PUBLIC_API_BASE_URL
from src.server.database import computer_data_tokens as db

logger = logging.getLogger(__name__)

# Write a fresh token once less than this is left. It has to outlast the
# longest a sandbox goes between two writes: the sweep's interval, and a
# failed write or two.
REFRESH_BELOW = timedelta(minutes=30)

_TIMEOUT_S = 5.0
_REVOKE_ATTEMPTS = 3

#: Writes the token file into a sandbox; True only once the write is confirmed.
Write = Callable[[dict[str, Any]], Awaitable[bool]]


@dataclass(frozen=True)
class Minted:
    token: str
    #: When the platform issued it, in epoch ms by the one clock its mints and
    #: revocations share, and after any revocation standing then: a later
    #: mint carries a larger one, and the sandbox keeps the larger.
    issued_at_ms: int
    expires_at: datetime


def configured() -> bool:
    return bool(
        AUTH_SERVICE_URL
        and GINLIX_DATA_URL
        and os.getenv("INTERNAL_SERVICE_TOKEN", "")
    )


def due(row: db.DataTokenRow, sandbox_id: str | None) -> bool:
    """Whether ``sandbox_id`` is owed a token: it holds none, or one with
    less than ``REFRESH_BELOW`` left."""
    if row.expires_at is None or row.held_by is None or row.held_by != sandbox_id:
        return True
    return row.expires_at - datetime.now(UTC) < REFRESH_BELOW


async def keep(
    computer_id: str, user_id: str | None, sandbox_id: str | None, write: Write
) -> None:
    """Write a fresh token into ``sandbox_id``, the computer's own sandbox,
    when it is owed one."""
    if not user_id or not sandbox_id or not configured():
        return
    try:
        row = await db.load(str(computer_id))
        if due(row, sandbox_id):
            await push(
                str(computer_id), user_id, sandbox_id, write, generation=row.generation
            )
    except Exception as e:
        logger.warning(
            "Market-data token not kept for computer %s (%s)",
            computer_id,
            type(e).__name__,
        )


async def push(
    computer_id: str,
    user_id: str,
    sandbox_id: str,
    write: Write,
    *,
    generation: int,
) -> bool:
    """Mint a token, record it over the row read at ``generation``, and write
    it into the sandbox; True once the sandbox holds it."""
    minted = await _mint(user_id, computer_id)
    if minted is None:
        return False
    saved = await db.save(
        computer_id, user_id, sandbox_id, minted.expires_at, generation
    )
    if not saved.serving or saved.generation is None:
        # The computer is leaving service, changed hands or sandboxes, or the
        # row moved since it was read (a newer token, or a revocation). The
        # token never left this process, so it lapses unrevoked: revoking
        # ends every token the computer holds, the live one included, and a
        # stop that completes revokes on its own.
        return False
    if not await write(_payload(minted)):
        return False
    await db.mark_held(computer_id, saved.generation, sandbox_id)
    return True


async def revoke(computer_id: Any) -> None:
    """End every token the computer was given, at a stop or delete, rather
    than when the last one would have run out. Best effort: never fails the
    stop."""
    if not configured():
        return
    computer_id = str(computer_id)
    try:
        users = await db.revoke(computer_id)
    except Exception as e:
        logger.warning(
            "Market-data token row not revoked for computer %s (%s)",
            computer_id,
            type(e).__name__,
        )
        return
    for user_id in users:
        await _revoke_upstream(user_id, computer_id)


def _payload(minted: Minted) -> dict[str, Any]:
    return {
        "access_token": minted.token,
        # What the sandbox orders tokens by: a write that stalled across a
        # newer one must not put the older, possibly revoked, token back.
        "iat_ms": minted.issued_at_ms,
        "expires_at": minted.expires_at.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        # Sandboxes are remote, so they reach ginlix-data at its public address.
        "ginlix_data_url": PUBLIC_API_BASE_URL or GINLIX_DATA_URL.rstrip("/"),
    }


def _url(path: str = "") -> str:
    return f"{AUTH_SERVICE_URL.rstrip('/')}/api/auth/data-tokens/computer{path}"


def _expires_at(body: dict[str, Any]) -> datetime:
    raw = body.get("expires_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in") or 0))


async def _mint(user_id: str, computer_id: str) -> Minted | None:
    from src.server.dependencies.usage_limits import get_http_client, service_headers

    try:
        client = await get_http_client()
        resp = await client.post(
            _url(),
            json={"user_id": user_id, "computer_id": computer_id},
            headers=service_headers(),
            timeout=_TIMEOUT_S,
        )
        if not resp.is_success:
            logger.warning(
                "Market-data token not minted for computer %s: HTTP %s",
                computer_id,
                resp.status_code,
            )
            return None
        body = resp.json()
        return Minted(body["access_token"], int(body["iat_ms"]), _expires_at(body))
    except Exception as e:
        logger.warning(
            "Market-data token not minted for computer %s (%s)",
            computer_id,
            type(e).__name__,
        )
        return None


async def _revoke_upstream(user_id: str, computer_id: str) -> bool:
    from src.server.dependencies.usage_limits import get_http_client, service_headers

    outcome = ""
    for attempt in range(_REVOKE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(0.5 * attempt)
        try:
            client = await get_http_client()
            resp = await client.post(
                _url("/revoke"),
                json={"user_id": user_id, "computer_id": computer_id},
                headers=service_headers(),
                timeout=_TIMEOUT_S,
            )
        except Exception as e:
            outcome = type(e).__name__
            continue
        if resp.is_success:
            return True
        outcome = f"HTTP {resp.status_code}"
        if resp.status_code < 500 and resp.status_code != 429:
            break
    logger.warning(
        "Market-data tokens not revoked for computer %s (%s); they lapse "
        "within the hour",
        computer_id,
        outcome,
    )
    return False
