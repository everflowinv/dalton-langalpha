"""Integration test for team-shared model credentials against the `_test` database (`DALTON-HOOK(team-credentials)`)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import create_autospec

import pytest

import src.dalton.settings as settings_mod
from src.dalton.llm.role_policy import explain_model_availability
from src.server.database.oauth_tokens import (
    delete_oauth_tokens,
    get_oauth_connection,
    get_oauth_tokens,
    upsert_oauth_tokens,
)
from src.server.database.user import create_user, get_user

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ORIG_LOAD_BASE_CONFIG = settings_mod._load_base_dalton_config


async def test_member_reads_holder_oauth_token_and_role_policy_marks_available(
    patched_get_db_connection,
    cleanup_tables,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two users in `_test` DB: holder has an OAuth token, member has none; with sharing=team, member reads holder's token and `role_policy` marks `*-oauth` models available."""
    holder_id = f"holder-{uuid.uuid4().hex[:8]}"
    member_id = f"member-{uuid.uuid4().hex[:8]}"

    # Pin environment and sharing="off" before any DB writes so setup is deterministic and self-contained
    if not os.getenv("BYOK_ENCRYPTION_KEY"):
        monkeypatch.setenv("BYOK_ENCRYPTION_KEY", f"test-byok-key-{uuid.uuid4().hex}")
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_PM_USER_IDS", holder_id)
    base_off = settings_mod.DaltonConfig(
        enabled=True,
        credentials=settings_mod.DaltonCredentialsConfig(sharing="off"),
    )
    monkeypatch.setattr(
        settings_mod,
        "_load_base_dalton_config",
        create_autospec(_ORIG_LOAD_BASE_CONFIG, return_value=base_off),
    )
    settings_mod.get_dalton_config.cache_clear()

    await create_user(
        user_id=holder_id,
        email="holder@everflow.ai",
        name="Team Holder",
    )
    await create_user(
        user_id=member_id,
        email="member@everflow.ai",
        name="Team Member",
    )

    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)

    try:
        # 1. Seed OAuth token under holder_id while sharing="off": member has no OAuth token and `role_policy` marks codex-oauth models unavailable
        await upsert_oauth_tokens(
            user_id=holder_id,
            provider="codex-oauth",
            access_token="holder-live-codex-token",
            refresh_token="holder-refresh-token",
            account_id="acct-holder-1",
            email="holder@everflow.ai",
            plan_type="pro",
            expires_at=expires_at,
        )

        assert await get_oauth_tokens(member_id, "codex-oauth") is None
        off_report = await explain_model_availability(user_id=member_id)
        codex_models = [
            k
            for k, v in off_report.items()
            if v.get("provider") == "codex-oauth"
            and "forbidden_for_roles" not in v.get("reasons", [])
            and "hidden" not in v.get("reasons", [])
        ]
        assert codex_models, "Expected at least one visible codex-oauth model in catalog"
        for mkey in codex_models:
            assert off_report[mkey]["available"] is False
            assert "oauth_not_connected" in off_report[mkey]["reasons"]

        # 2. Enable sharing="team" via real env var + base config: member reads holder's token and `role_policy` marks codex-oauth models available
        monkeypatch.setattr(settings_mod, "_load_base_dalton_config", _ORIG_LOAD_BASE_CONFIG)
        settings_mod.get_dalton_config.cache_clear()

        member_tok = await get_oauth_tokens(member_id, "codex-oauth")
        assert member_tok is not None
        assert member_tok["access_token"] == "holder-live-codex-token"
        assert member_tok["account_id"] == "acct-holder-1"
        assert member_tok["email"] == "holder@everflow.ai"

        member_status = await get_oauth_connection(member_id, "codex-oauth")
        assert member_status is not None
        assert member_status["email"] == "holder@everflow.ai"

        member_user_row = await get_user(member_id)
        assert member_user_row is not None
        assert member_user_row["user_id"] == member_id
        assert member_user_row["has_oauth_token"] is True

        team_report = await explain_model_availability(user_id=member_id)
        for mkey in codex_models:
            assert "oauth_not_connected" not in team_report[mkey]["reasons"]
            assert team_report[mkey]["available"] is True
    finally:
        settings_mod.get_dalton_config.cache_clear()
        await delete_oauth_tokens(holder_id, "codex-oauth")
