"""DALTON-HOOK(colleague-dispatch): thread_access answers an external member 404 for a
thread that is not theirs, existing or not, as require_thread_owner does."""

from __future__ import annotations

from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException

import src.dalton.settings as dalton_settings
import src.dalton.sharing as dalton_sharing
from src.server.database import conversation as conversation_db
from src.server.services.shared_workspaces import access

_ORIG_TIER = dalton_sharing.get_user_tier
_ORIG_META = conversation_db.get_thread_auth_meta
_ORIG_AUTH = dalton_settings.is_dalton_supabase_auth_enabled


def _meta(owner: str, ws_owner: str) -> dict:
    return {"user_id": owner, "workspace_owner_id": ws_owner, "workspace_id": "ws-1"}


async def _status(caller: str, tier: str, meta: dict | None) -> int | None:
    with (
        patch.object(dalton_settings, "is_dalton_supabase_auth_enabled", create_autospec(_ORIG_AUTH, return_value=True)),
        patch.object(dalton_sharing, "get_user_tier", create_autospec(_ORIG_TIER, return_value=tier)),
        patch.object(conversation_db, "get_thread_auth_meta", create_autospec(_ORIG_META, return_value=meta)),
    ):
        try:
            await access.thread_access("t-1", caller)
        except HTTPException as e:
            return e.status_code
    return None


@pytest.mark.asyncio
async def test_external_member_gets_404_for_a_foreign_thread_and_a_missing_one():
    assert await _status("ext", "external", _meta("other", "other")) == 404
    assert await _status("ext", "external", None) == 404


@pytest.mark.asyncio
async def test_external_members_own_thread_in_a_colleagues_workspace_is_404():
    assert await _status("ext", "external", _meta("ext", "other")) == 404


@pytest.mark.asyncio
async def test_external_members_own_thread_in_their_own_workspace_passes():
    assert await _status("ext", "external", _meta("ext", "ext")) is None


@pytest.mark.asyncio
async def test_internal_member_keeps_the_403_for_a_foreign_thread():
    assert await _status("int", "internal", _meta("other", "other")) == 403
