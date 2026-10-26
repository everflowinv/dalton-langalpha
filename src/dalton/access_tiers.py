"""Three-tier user identity for Dalton permission gates.

Tier is fixed by roster only — there is no UI to change it:

- ``admin``: user_id in ``DALTON_PM_USER_IDS``.
- ``internal``: user email (from the ``users`` table) in ``DALTON_ALLOWED_EMAILS``.
- ``external``: everyone else, which includes every invite-signup member
  (``dalton_members``).

Every lookup failure resolves to ``external`` (fail closed): a raising roster
check can never widen access — an admin-check failure falls through to the
email check rather than granting admin, and a failing email check resolves to
external.
"""

from __future__ import annotations

import logging
from typing import Literal

from src.dalton.invites import is_invite_pm
from src.dalton.settings import get_dalton_allowed_emails

logger = logging.getLogger(__name__)

UserTier = Literal["admin", "internal", "external"]


async def get_user_tier(user_id: str | None) -> UserTier:
    """Resolve a user's access tier; never raises, never returns a wider tier on failure."""
    uid = str(user_id or "").strip()
    if not uid:
        return "external"
    try:
        if is_invite_pm(uid):
            return "admin"
    except Exception as exc:
        # A broken PM check must not hand out admin: fall through to the
        # email check, which itself fails closed to external.
        logger.warning(
            "get_user_tier admin check failed for user_id=%s (continuing as non-admin): %s",
            uid,
            type(exc).__name__,
        )
    try:
        from src.server.database.user import get_user

        user = await get_user(uid)
        email = str((user or {}).get("email") or "").strip().lower()
        if email and email in get_dalton_allowed_emails():
            return "internal"
    except Exception as exc:
        logger.warning(
            "get_user_tier lookup failed for user_id=%s (treated as external): %s",
            uid,
            type(exc).__name__,
        )
    return "external"
