"""Unified access control for Dalton Monitors.

Company membership is resolved strictly via ``src.dalton.access_tiers.get_user_tier``
(``admin`` or ``internal``), never via ``invites.is_team_member`` (which also admits
invited external members).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from fastapi import HTTPException

from src.dalton.access_tiers import get_user_tier
from src.dalton.company_identity import is_company_system_user
from src.dalton.monitors.models import MonitorRecord, MonitorVisibility

COMPANY_TIERS = frozenset({"admin", "internal"})


def is_company_tier(tier: str | None) -> bool:
    return tier in COMPANY_TIERS


def can_manage_monitor_fields(
    *,
    owner_user_id: str,
    visibility: MonitorVisibility,
    caller_id: str,
    caller_tier: str,
) -> bool:
    """Return True when ``caller_id`` may mutate or delete a monitor with these locked attributes."""
    if caller_id == owner_user_id:
        return True
    if is_company_system_user(owner_user_id) and caller_tier == "admin":
        return True
    # An active admin may downgrade, take over, or delete another owner's monitor
    # only while that monitor is currently public.
    if caller_tier == "admin" and visibility == "public":
        return True
    return False


async def can_view_monitor(
    monitor: MonitorRecord,
    user_id: str | None,
    *,
    member_user_ids: Collection[str] | None = None,
) -> bool:
    """Single source of truth for viewing monitor metadata, files, versions, and realtime data."""
    uid = str(user_id or "").strip()
    if not uid:
        return False

    visibility = monitor.visibility
    if visibility == "public":
        return True

    if visibility == "own":
        return uid == monitor.owner_user_id

    if visibility == "company":
        tier = await get_user_tier(uid)
        return is_company_tier(tier)

    if visibility == "members":
        if uid == monitor.owner_user_id:
            return True
        members = member_user_ids if member_user_ids is not None else monitor.member_user_ids
        if uid not in members:
            return False
        # Even if listed in dalton_monitor_members, access revokes automatically
        # when the user is no longer a company member.
        tier = await get_user_tier(uid)
        return is_company_tier(tier)

    return False


async def require_monitor_view(
    monitor: MonitorRecord,
    user_id: str | None,
    *,
    member_user_ids: Collection[str] | None = None,
) -> None:
    if not await can_view_monitor(
        monitor, user_id, member_user_ids=member_user_ids
    ):
        raise HTTPException(status_code=403, detail="无权访问此监控面板")


async def validate_publish_permissions(
    *,
    caller_id: str,
    visibility: MonitorVisibility,
    member_user_ids: Sequence[str],
) -> list[str]:
    """Validate publisher tier against requested visibility and member allowlist.

    Returns normalized deduplicated ``member_user_ids`` (empty unless ``visibility == 'members'``).
    """
    tier = await get_user_tier(caller_id)

    if tier == "external" and visibility != "own":
        raise HTTPException(
            status_code=403,
            detail="外部成员发布监控面板时只能选择“仅自己”可见",
        )

    if visibility == "public" and tier != "admin":
        raise HTTPException(
            status_code=403,
            detail="只有管理员可以将监控面板设为公开链接",
        )

    if visibility != "members":
        return []

    deduped = list(dict.fromkeys(str(m).strip() for m in member_user_ids if str(m).strip()))
    if not deduped:
        raise HTTPException(
            status_code=422,
            detail="选择“指定成员”时请至少选择一位公司成员",
        )
    for mid in deduped:
        member_tier = await get_user_tier(mid)
        if not is_company_tier(member_tier):
            raise HTTPException(
                status_code=422,
                detail=f"指定成员只能选择公司成员（无效成员: {mid}）",
            )
    return deduped
