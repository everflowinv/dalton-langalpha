"""Unified Dalton role & permission guards with structured HTTP 403 error payloads.

All role-based 403 responses constructed here serialize via FastAPI as::

    {
      "detail": {
        "code": "external_member_forbidden" | "admin_required" | ...,
        "message": "<human-readable message>"
      }
    }

while preserving string equality (`exc.detail == "<message>"`) for direct unit-test
endpoint invocations that inspect `HTTPException.detail` without going through HTTP
JSON serialization.
"""

from __future__ import annotations

import os
from typing import Any, Callable, NoReturn

from fastapi import HTTPException

from src.dalton.access_tiers import get_user_tier as _default_get_user_tier
from src.dalton.settings import (
    is_dalton_multiuser_auth_enabled,
    is_dalton_single_user_oss_mode,
)

EXTERNAL_MEMBER_FORBIDDEN_CODE = "external_member_forbidden"
ADMIN_REQUIRED_CODE = "admin_required"
ARTIFACT_PUBLISHING_DISABLED_CODE = "artifact_publishing_disabled"
INVITES_DISABLED_IN_OSS_CODE = "invites_disabled_in_oss"

DEFAULT_EXTERNAL_MEMBER_FORBIDDEN_MESSAGE = "外部成员无权执行此操作"
DEFAULT_ADMIN_REQUIRED_MESSAGE = "仅管理员可执行此操作"
DEFAULT_INVITES_DISABLED_IN_OSS_MESSAGE = "单人模式下无需使用团队邀请"


class StructuredForbiddenDetail(dict[str, Any]):
    """Dictionary payload `{"code": ..., "message": ...}` for `HTTPException(status_code=403)`.

    Subclasses `dict` so FastAPI/Pydantic JSON serialization emits
    `{"detail": {"code": ..., "message": ...}}` and `isinstance(detail, dict)` is True.
    Also compares equal to `self["message"]` when compared against a `str` in direct
    Python unit test assertions (`assert exc.value.detail == "..."`), and renders
    `self["message"]` in `str()` containment checks.
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.get("message") == other
        return super().__eq__(other)


def forbidden_detail(code: str, message: str, **extra: Any) -> StructuredForbiddenDetail:
    """Build a structured 403 `detail` dictionary (`{"code": code, "message": message}`)."""
    payload = StructuredForbiddenDetail(code=code, message=message)
    if extra:
        payload.update(extra)
    return payload


def raise_forbidden(code: str, message: str, **extra: Any) -> NoReturn:
    """Raise `HTTPException(status_code=403, detail={"code": code, "message": message})`."""
    raise HTTPException(
        status_code=403,
        detail=forbidden_detail(code, message, **extra),
    )


def raise_external_member_forbidden(
    message: str = DEFAULT_EXTERNAL_MEMBER_FORBIDDEN_MESSAGE,
    **extra: Any,
) -> NoReturn:
    """Raise HTTP 403 with `code='external_member_forbidden'`."""
    raise_forbidden(EXTERNAL_MEMBER_FORBIDDEN_CODE, message, **extra)


def raise_admin_required(
    message: str = DEFAULT_ADMIN_REQUIRED_MESSAGE,
    **extra: Any,
) -> NoReturn:
    """Raise HTTP 403 with `code='admin_required'`."""
    raise_forbidden(ADMIN_REQUIRED_CODE, message, **extra)


def raise_invites_disabled_in_oss(
    message: str = DEFAULT_INVITES_DISABLED_IN_OSS_MESSAGE,
    **extra: Any,
) -> NoReturn:
    """Raise HTTP 403 with `code='invites_disabled_in_oss'`."""
    raise_forbidden(INVITES_DISABLED_IN_OSS_CODE, message, **extra)


def is_multiuser_guard_active(
    *,
    multiuser_enabled: bool | None = None,
    custom_tier_fn: bool = False,
) -> bool:
    """Return True when multi-user role guards should run for the current request.

    - Always False when `HOST_MODE != 'oss'` (e.g., upstream `platform` mode).
    - Always True when `is_dalton_multiuser_auth_enabled()` (`supabase` or `local`) is True,
      or when the caller explicitly passes `multiuser_enabled=True`.
    - Also True in unit tests that leave `DALTON_AUTH_MODE` unset in `os.environ` while
      explicitly monkeypatching `get_user_tier`.
    """
    from src.config import settings as app_settings

    if app_settings.HOST_MODE != "oss":
        return False
    if multiuser_enabled is not None:
        return bool(multiuser_enabled)
    if is_dalton_multiuser_auth_enabled():
        return True
    if custom_tier_fn and not os.getenv("DALTON_AUTH_MODE", "").strip():
        return True
    if is_dalton_single_user_oss_mode():
        return False
    return False


async def require_internal_member(
    user_id: str | None,
    *,
    message: str = DEFAULT_EXTERNAL_MEMBER_FORBIDDEN_MESSAGE,
    tier_fn: Callable[[str | None], Any] | None = None,
    multiuser_enabled: bool | None = None,
    conn: Any = None,
) -> None:
    """Ensure `user_id` is an internal or admin member when multi-user auth is enabled."""
    custom_fn = tier_fn is not None and tier_fn is not _default_get_user_tier
    if not is_multiuser_guard_active(
        multiuser_enabled=multiuser_enabled,
        custom_tier_fn=custom_fn,
    ):
        return
    resolve = tier_fn or _default_get_user_tier
    if conn is not None and resolve is _default_get_user_tier:
        tier = await resolve(user_id, conn=conn)
    else:
        tier = await resolve(user_id)
    if tier not in ("admin", "internal", "company"):
        raise_external_member_forbidden(message)


async def require_admin_member(
    user_id: str | None,
    *,
    message: str = DEFAULT_ADMIN_REQUIRED_MESSAGE,
    tier_fn: Callable[[str | None], Any] | None = None,
    multiuser_enabled: bool | None = None,
    conn: Any = None,
) -> None:
    """Ensure `user_id` is an admin member when multi-user auth is enabled."""
    custom_fn = tier_fn is not None and tier_fn is not _default_get_user_tier
    if not is_multiuser_guard_active(
        multiuser_enabled=multiuser_enabled,
        custom_tier_fn=custom_fn,
    ):
        return
    resolve = tier_fn or _default_get_user_tier
    if conn is not None and resolve is _default_get_user_tier:
        tier = await resolve(user_id, conn=conn)
    else:
        tier = await resolve(user_id)
    if tier != "admin":
        raise_admin_required(message)


__all__ = [
    "ADMIN_REQUIRED_CODE",
    "ARTIFACT_PUBLISHING_DISABLED_CODE",
    "DEFAULT_ADMIN_REQUIRED_MESSAGE",
    "DEFAULT_EXTERNAL_MEMBER_FORBIDDEN_MESSAGE",
    "DEFAULT_INVITES_DISABLED_IN_OSS_MESSAGE",
    "EXTERNAL_MEMBER_FORBIDDEN_CODE",
    "INVITES_DISABLED_IN_OSS_CODE",
    "StructuredForbiddenDetail",
    "forbidden_detail",
    "is_multiuser_guard_active",
    "raise_admin_required",
    "raise_external_member_forbidden",
    "raise_forbidden",
    "raise_invites_disabled_in_oss",
    "require_admin_member",
    "require_internal_member",
]
