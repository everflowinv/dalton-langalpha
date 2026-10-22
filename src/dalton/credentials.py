"""Team-shared model credential resolution and admin guard (`DALTON-HOOK(team-credentials)`).

When `credentials.sharing == "team"`, Dalton Supabase auth is enabled, and
`DALTON_PM_USER_IDS` is explicitly configured and non-empty, all model credential
reads, writes, caches, and OAuth refresh locks (`user_oauth_tokens`,
`user_api_keys`, `users.byok_enabled`) resolve to a single credential holder
(`DALTON_PM_USER_IDS[0]`). Only PM users (`DALTON_PM_USER_IDS`) may disconnect,
clear, or disable shared model credentials.
"""

from __future__ import annotations

from fastapi import HTTPException

from src.dalton.settings import (
    get_configured_pm_user_ids as _settings_configured_pm_user_ids,
    get_dalton_config,
    is_dalton_supabase_auth_enabled,
)

TEAM_CREDENTIAL_DISCONNECT_FORBIDDEN_DETAIL = "团队共用凭据只能由管理员断开"


def get_configured_pm_user_ids() -> list[str]:
    """Return explicitly configured PM user IDs from `DALTON_PM_USER_IDS` (empty when unset or blank)."""
    return [
        str(u).strip()
        for u in (_settings_configured_pm_user_ids() or [])
        if str(u).strip()
    ]


def get_team_credential_holder_id() -> str | None:
    """Return the single team credential holder user_id when sharing is active, else None."""
    cfg = get_dalton_config()
    if not cfg.enabled or cfg.credentials.sharing != "team":
        return None
    if not is_dalton_supabase_auth_enabled():
        return None
    pm_ids = get_configured_pm_user_ids()
    if not pm_ids:
        return None
    return pm_ids[0]


def is_team_credential_sharing_enabled() -> bool:
    """Return True when team credential sharing is active."""
    return get_team_credential_holder_id() is not None


def credential_user_id(user_id: str) -> str:
    """Resolve `user_id` to the team credential holder when sharing is enabled.

    Returns `user_id` unchanged when sharing is off, when `user_id` is empty,
    or when `user_id` is already the holder.
    """
    if not user_id:
        return user_id
    holder_id = get_team_credential_holder_id()
    if not holder_id or user_id == holder_id:
        return user_id
    return holder_id


def is_credential_admin(user_id: str) -> bool:
    """Return True if `user_id` may disconnect or delete credentials."""
    if not is_team_credential_sharing_enabled():
        return True
    if not user_id:
        return False
    return str(user_id).strip() in set(get_configured_pm_user_ids())


def require_credential_admin(user_id: str) -> None:
    """Raise HTTP 403 if team credential sharing is active and `user_id` is not a PM user."""
    if not is_credential_admin(user_id):
        raise HTTPException(
            status_code=403,
            detail=TEAM_CREDENTIAL_DISCONNECT_FORBIDDEN_DETAIL,
        )
