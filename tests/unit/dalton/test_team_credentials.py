"""Unit tests for team-shared model credentials (`DALTON-HOOK(team-credentials)`).

All test doubles use `unittest.mock.create_autospec` to preserve function and method signatures.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import create_autospec

import pytest
import redis.asyncio as aioredis
import redis.asyncio as redis_asyncio
from fastapi import HTTPException
from psycopg import AsyncConnection, AsyncCursor

import src.dalton.app.oauth_native as oauth_native_app
import src.dalton.credentials as cred_mod
import src.dalton.database.llm_journal as journal_db
import src.dalton.llm.complete as complete_mod
import src.dalton.llm.providers.antigravity.oauth as agy_oauth_mod
import src.dalton.llm.providers.muse.oauth as muse_oauth_mod
import src.dalton.settings as settings_mod
import src.server.app.api_keys as api_keys_app
import src.server.app.oauth as oauth_app
import src.server.database.api_keys as api_keys_db
import src.server.database.oauth_tokens as oauth_tokens_db
import src.server.database.user as user_db
import src.server.services.claude_oauth as claude_oauth_mod
import src.server.services.codex_oauth as codex_oauth_mod
import src.server.services.onboarding as onboarding_svc
from src.server.app.api_keys import UpdateApiKeysRequest
from src.utils.cache.redis_cache import RedisCacheClient

HOLDER_ID = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
SECOND_PM_ID = "22222222-2222-4222-8222-222222222222"
MEMBER_ID = "33333333-3333-4333-8333-333333333333"

_ORIG_LOAD_BASE_CONFIG = settings_mod._load_base_dalton_config
_ORIG_GET_DB_CONNECTION = oauth_tokens_db.get_db_connection


@pytest.fixture(autouse=True)
def _clear_dalton_config_cache():
    settings_mod.get_dalton_config.cache_clear()
    yield
    settings_mod.get_dalton_config.cache_clear()


def _enable_team_sharing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sharing: str = "team",
    auth_mode: str = "supabase",
    pm_ids: str | None = f"{HOLDER_ID},{SECOND_PM_ID}",
) -> None:
    """Configure environment and DaltonConfig via real env parsing for team credential sharing tests."""
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)
    if pm_ids is None:
        monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("DALTON_PM_USER_IDS", pm_ids)
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-byok-encryption-key-for-unit-tests")
    settings_mod.get_dalton_config.cache_clear()

    if sharing != "team":
        base_cfg = settings_mod.DaltonConfig(
            enabled=True,
            credentials=settings_mod.DaltonCredentialsConfig(sharing=sharing),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(
            settings_mod,
            "_load_base_dalton_config",
            create_autospec(_ORIG_LOAD_BASE_CONFIG, return_value=base_cfg),
        )
    else:
        monkeypatch.setattr(settings_mod, "_load_base_dalton_config", _ORIG_LOAD_BASE_CONFIG)
        settings_mod.get_dalton_config.cache_clear()


def _make_autospec_db(
    *,
    fetchone_row: dict[str, Any] | None = None,
    fetchall_rows: list[dict[str, Any]] | None = None,
    rowcount: int = 1,
) -> tuple[Any, Any, list[tuple[str, Any]]]:
    """Build an autospecced `(get_db_connection, cursor, executed_queries)` triple."""
    executed: list[tuple[str, Any]] = []
    cur = create_autospec(AsyncCursor, instance=True)

    async def _execute(
        query: Any,
        params: Any = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Any:
        executed.append((str(query), params))
        return cur

    cur.execute.side_effect = _execute
    cur.fetchone.return_value = fetchone_row
    cur.fetchall.return_value = fetchall_rows if fetchall_rows is not None else []
    cur.rowcount = rowcount

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        yield None

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _get_db_conn(conn_arg: Any = None):
        yield conn

    autospecced_get_db_conn = create_autospec(
        _ORIG_GET_DB_CONNECTION,
        side_effect=_get_db_conn,
    )
    return autospecced_get_db_conn, cur, executed


# ---------------------------------------------------------------------------
# 1. credential_user_id: 3 cases (off [including unset/empty DALTON_PM_USER_IDS], holder, member)
# ---------------------------------------------------------------------------


def test_credential_user_id_three_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """`credential_user_id` covers (1) off (YAML unquoted `sharing: off`, `sharing: "off"`, auth=oss, DALTON_PM_USER_IDS unset or empty), (2) holder self, and (3) non-holder team member."""
    # Case 1a: real YAML file load with unquoted `sharing: off` (YAML 1.1 parses `off` as bool False) and quoted `sharing: "off"`
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_PM_USER_IDS", f"{HOLDER_ID},{SECOND_PM_ID}")
    monkeypatch.setattr(settings_mod, "_load_base_dalton_config", _ORIG_LOAD_BASE_CONFIG)

    yaml_unquoted_off = tmp_path / "dalton_unquoted_off.yaml"
    yaml_unquoted_off.write_text("enabled: true\ncredentials:\n  sharing: off\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "DALTON_YAML_PATH", yaml_unquoted_off)
    settings_mod.get_dalton_config.cache_clear()
    cfg_unquoted = settings_mod.get_dalton_config()
    assert cfg_unquoted.credentials.sharing == "off"
    assert cred_mod.is_team_credential_sharing_enabled() is False
    assert cred_mod.credential_user_id(MEMBER_ID) == MEMBER_ID
    assert cred_mod.credential_user_id(HOLDER_ID) == HOLDER_ID

    yaml_quoted_off = tmp_path / "dalton_quoted_off.yaml"
    yaml_quoted_off.write_text('enabled: true\ncredentials:\n  sharing: "off"\n', encoding="utf-8")
    monkeypatch.setattr(settings_mod, "DALTON_YAML_PATH", yaml_quoted_off)
    settings_mod.get_dalton_config.cache_clear()
    cfg_quoted = settings_mod.get_dalton_config()
    assert cfg_quoted.credentials.sharing == "off"
    assert cred_mod.is_team_credential_sharing_enabled() is False

    yaml_bool_true = tmp_path / "dalton_bool_true.yaml"
    yaml_bool_true.write_text("enabled: true\ncredentials:\n  sharing: true\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "DALTON_YAML_PATH", yaml_bool_true)
    settings_mod.get_dalton_config.cache_clear()
    assert settings_mod.get_dalton_config().credentials.sharing == "team"
    assert cred_mod.is_team_credential_sharing_enabled() is True

    # Case 1b: Dalton Supabase auth disabled (DALTON_AUTH_MODE=oss)
    _enable_team_sharing(monkeypatch, sharing="team", auth_mode="oss")
    assert cred_mod.is_team_credential_sharing_enabled() is False
    assert cred_mod.credential_user_id(MEMBER_ID) == MEMBER_ID

    # Case 1c: DALTON_PM_USER_IDS unset -> real env parsing falls back to default pm_user_ids but configured_pm_user_ids is [] -> off
    _enable_team_sharing(monkeypatch, sharing="team", auth_mode="supabase", pm_ids=None)
    assert settings_mod.get_configured_pm_user_ids() == []
    assert cred_mod.is_team_credential_sharing_enabled() is False
    assert cred_mod.credential_user_id(MEMBER_ID) == MEMBER_ID

    # Case 1d: DALTON_PM_USER_IDS="" or whitespace commas -> configured_pm_user_ids is [] -> off
    _enable_team_sharing(monkeypatch, sharing="team", auth_mode="supabase", pm_ids="")
    assert settings_mod.get_configured_pm_user_ids() == []
    assert cred_mod.is_team_credential_sharing_enabled() is False
    assert cred_mod.credential_user_id(MEMBER_ID) == MEMBER_ID

    _enable_team_sharing(monkeypatch, sharing="team", auth_mode="supabase", pm_ids=" ,  ")
    assert settings_mod.get_configured_pm_user_ids() == []
    assert cred_mod.is_team_credential_sharing_enabled() is False
    assert cred_mod.credential_user_id(MEMBER_ID) == MEMBER_ID

    # Enable team sharing via real env var
    _enable_team_sharing(monkeypatch, sharing="team", auth_mode="supabase")
    assert settings_mod.get_configured_pm_user_ids() == [HOLDER_ID, SECOND_PM_ID]
    assert cred_mod.is_team_credential_sharing_enabled() is True
    assert cred_mod.get_team_credential_holder_id() == HOLDER_ID

    # Case 2: holder himself -> returns holder ID unchanged
    assert cred_mod.credential_user_id(HOLDER_ID) == HOLDER_ID

    # Case 3: other team member (and second PM) -> returns holder ID
    assert cred_mod.credential_user_id(MEMBER_ID) == HOLDER_ID
    assert cred_mod.credential_user_id(SECOND_PM_ID) == HOLDER_ID


# ---------------------------------------------------------------------------
# 2. OAuth connection & get_oauth_tokens return holder's tokens for team member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_connection_and_get_tokens_return_holder_for_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Member calling `get_oauth_connection` and `get_oauth_tokens` reads the holder's row and cache key."""
    _enable_team_sharing(monkeypatch)
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    fake_row = {
        "provider": "codex-oauth",
        "access_token": "holder-access-tok",
        "refresh_token": "holder-refresh-tok",
        "account_id": "acct-holder",
        "email": "owner@everflow.ai",
        "plan_type": "pro",
        "expires_at": expires,
        "created_at": expires,
        "updated_at": expires,
    }
    get_db_conn, _, executed = _make_autospec_db(fetchone_row=fake_row)
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    fake_redis_client = create_autospec(aioredis.Redis, instance=True)

    async def _redis_get(name: Any) -> Any:
        return None

    async def _redis_set(name: Any, value: Any, ex: Any = None, **kwargs: Any) -> Any:
        return True

    async def _redis_delete(*names: Any) -> Any:
        return 1

    fake_redis_client.get.side_effect = _redis_get
    fake_redis_client.set.side_effect = _redis_set
    fake_redis_client.delete.side_effect = _redis_delete

    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.enabled = True
    fake_cache.client = fake_redis_client
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    from src.server.services.oauth_health import connection_state

    status = await oauth_tokens_db.get_oauth_connection(MEMBER_ID, "codex-oauth")
    assert connection_state(status) == "connected"
    assert status["email"] == "owner@everflow.ai"
    assert executed[0][1] == (HOLDER_ID, "codex-oauth")

    tokens = await oauth_tokens_db.get_oauth_tokens(MEMBER_ID, "codex-oauth")
    assert tokens is not None
    assert tokens["access_token"] == "holder-access-tok"
    assert executed[1][1][-2:] == (HOLDER_ID, "codex-oauth")

    has_tok = await oauth_tokens_db.has_any_oauth_token(MEMBER_ID)
    assert has_tok is True
    fake_redis_client.get.assert_called_once_with(f"oauth_active:v2:{HOLDER_ID}")
    fake_redis_client.set.assert_called_once_with(
        f"oauth_active:v2:{HOLDER_ID}", b"1", ex=86400
    )

    await oauth_tokens_db.invalidate_oauth_active_cache(MEMBER_ID)
    fake_redis_client.delete.assert_called_once_with(
        f"oauth_active:v2:{HOLDER_ID}", f"oauth_active:{HOLDER_ID}"
    )


# ---------------------------------------------------------------------------
# 3. OAuth refresh lock key uses holder ID, while provider events record actor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_refresh_lock_key_uses_holder_id_and_events_record_actor(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Concurrent refreshes lock on `oauth:refresh:{holder_id}:{provider}`, while `dalton_provider_events` and DB upsert logs record the actual caller (`MEMBER_ID`) as actor and `HOLDER_ID` as credential."""
    import logging

    _enable_team_sharing(monkeypatch)
    caplog.set_level(logging.DEBUG)
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    refreshed_at = datetime.now(timezone.utc) + timedelta(hours=1)

    expired_row = {
        "provider": "codex-oauth",
        "access_token": "stale-token",
        "refresh_token": "refresh-token-1",
        "account_id": "acct-1",
        "email": "owner@everflow.ai",
        "plan_type": "pro",
        "expires_at": expired_at,
    }
    fresh_row = {
        **expired_row,
        "access_token": "fresh-holder-token",
        "expires_at": refreshed_at,
    }

    lock_keys: list[str] = []

    fake_cache = create_autospec(RedisCacheClient(), instance=True)

    async def _acquire_lock_wait(key: str, token: str, ttl_ms: int) -> bool | None:
        lock_keys.append(key)
        return False

    fake_cache.acquire_lock.side_effect = _acquire_lock_wait
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    import src.server.services.oauth_health as oauth_health_mod
    import src.server.services.oauth_refresh_wait as oauth_refresh_wait_mod

    def _patch_token_reads(first: dict[str, Any], second: dict[str, Any]) -> None:
        # The unified protocol holds its own bindings of get_oauth_tokens
        # (oauth_health for the first read, oauth_refresh_wait for the
        # contended wait), so both are patched.
        real_get_tokens = oauth_tokens_db.get_oauth_tokens
        monkeypatch.setattr(
            oauth_health_mod,
            "get_oauth_tokens",
            create_autospec(real_get_tokens, return_value=first),
        )
        monkeypatch.setattr(
            oauth_refresh_wait_mod,
            "get_oauth_tokens",
            create_autospec(real_get_tokens, return_value=second),
        )

    # 3a. Codex OAuth via the unified refresh protocol (lock contended ->
    # waits for the other holder of the HOLDER-keyed lock; the awaited read
    # maps the member onto the holder's row inside get_oauth_tokens)
    _patch_token_reads(expired_row, fresh_row)
    tok_res = await oauth_health_mod.valid_token(MEMBER_ID, "codex-oauth")
    assert tok_res is not None
    assert tok_res.access_token == "fresh-holder-token"
    assert lock_keys[-1] == f"oauth:refresh:{HOLDER_ID}:codex-oauth"

    # 3b. Claude OAuth (lock contended -> same HOLDER-keyed lock)
    _patch_token_reads(expired_row, fresh_row)
    claude_tok = await oauth_health_mod.valid_token(MEMBER_ID, "claude-oauth")
    assert claude_tok is not None
    assert claude_tok.access_token == "fresh-holder-token"
    assert lock_keys[-1] == f"oauth:refresh:{HOLDER_ID}:claude-oauth"

    # 3c. Antigravity serves through the unified protocol too (its own
    #     refresher is gone): member's turn refreshes under the HOLDER-keyed
    #     lock, and the conditional save writes the HOLDER's row.
    async def _acquire_lock_win(key: str, token: str, ttl_ms: int) -> bool | None:
        lock_keys.append(key)
        return True

    async def _release_lock(key: str, token: str) -> None:
        return None

    fake_cache.acquire_lock.side_effect = _acquire_lock_win
    fake_cache.release_lock.side_effect = _release_lock

    get_db_conn_refresh, _, executed_refresh = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn_refresh)

    from dataclasses import replace as _dc_replace

    import src.server.services.oauth_providers as oauth_providers_mod
    from src.server.services.oauth_protocol import TokenSet

    real_get_tokens_3c = oauth_tokens_db.get_oauth_tokens
    monkeypatch.setattr(
        oauth_health_mod,
        "get_oauth_tokens",
        create_autospec(real_get_tokens_3c, return_value=expired_row),
    )
    fake_agy_refresh = create_autospec(
        agy_oauth_mod.refresh_tokens,
        return_value=TokenSet(
            access_token="agy-new-tok",
            refresh_token="agy-new-ref",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    monkeypatch.setattr(
        oauth_health_mod,
        "SUBSCRIPTIONS",
        {
            **oauth_providers_mod.SUBSCRIPTIONS,
            "antigravity-oauth": _dc_replace(
                oauth_providers_mod.SUBSCRIPTIONS["antigravity-oauth"],
                refresh=fake_agy_refresh,
            ),
        },
    )

    caplog.clear()
    agy_tok = await oauth_health_mod.valid_token(MEMBER_ID, "antigravity-oauth")
    assert agy_tok is not None
    assert agy_tok.access_token == "agy-new-tok"
    assert lock_keys[-1] == f"oauth:refresh:{HOLDER_ID}:antigravity-oauth"
    fake_agy_refresh.assert_awaited_once_with(expired_row["refresh_token"])
    # The conditional replace writes the HOLDER's row (mapping inside).
    replace_sql, replace_params = executed_refresh[-1]
    assert "UPDATE user_oauth_tokens" in replace_sql
    assert replace_params[-4] == HOLDER_ID
    assert replace_params[-3] == "antigravity-oauth"

    # 3d. Muse OAuth minted key storage & retrieval: DB write targets HOLDER_ID, DB log & event record MEMBER_ID as actor
    muse_events: list[tuple[str, str]] = []

    async def _muse_record_event(user_id: str, kind: str, detail: dict[str, Any] | None = None) -> None:
        muse_events.append((user_id, kind))

    minted_holder_row = {
        **fresh_row,
        "access_token": "muse-minted-api-key",
        "refresh_token": "",
        "account_id": muse_oauth_mod.MUSE_API_KEY_ACCOUNT_ID,
        "plan_type": "Pro",
    }
    monkeypatch.setattr(
        muse_oauth_mod,
        "get_oauth_tokens",
        create_autospec(muse_oauth_mod.get_oauth_tokens, return_value=minted_holder_row),
    )
    monkeypatch.setattr(
        muse_oauth_mod,
        "invalidate_oauth_active_cache",
        create_autospec(muse_oauth_mod.invalidate_oauth_active_cache, return_value=None),
    )
    monkeypatch.setattr(
        muse_oauth_mod,
        "_record_provider_event_safe",
        create_autospec(muse_oauth_mod._record_provider_event_safe, side_effect=_muse_record_event),
    )

    caplog.clear()
    await muse_oauth_mod.save_minted_credentials(
        MEMBER_ID,
        {
            "api_key": "muse-minted-api-key",
            "base_url": "https://api.meta.ai/v1",
            "subs_tier_name": "Pro",
            "user_email": "pm@dalton.test",
            "is_subs_active": True,
        },
    )
    muse_tok = await muse_oauth_mod.get_valid_token(MEMBER_ID)
    assert muse_tok is not None
    assert muse_tok["access_token"] == "muse-minted-api-key"
    assert executed_refresh[-1][1][0] == HOLDER_ID
    assert f"upsert user_id={MEMBER_ID} credential_user_id={HOLDER_ID} provider=muse-oauth" in caplog.text
    assert muse_events == [(MEMBER_ID, "refresh_ok")]

    # 3e. Claude OAuth when lock is acquired: the unified protocol refreshes on
    # the holder's stored refresh token and saves over the holder's row.
    from src.server.services.oauth_protocol import TokenSet

    import src.server.services.oauth_providers as oauth_providers_mod
    from dataclasses import replace as _dc_replace

    fake_claude_refresh = create_autospec(
        claude_oauth_mod.refresh_tokens,
        return_value=TokenSet(
            access_token="claude-new-tok",
            refresh_token="claude-new-ref",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    # The registry holds the refresh callable by reference, so the patched
    # entry replaces the whole subscription record oauth_health reads.
    monkeypatch.setattr(
        oauth_health_mod,
        "SUBSCRIPTIONS",
        {
            **oauth_providers_mod.SUBSCRIPTIONS,
            "claude-oauth": _dc_replace(
                oauth_providers_mod.SUBSCRIPTIONS["claude-oauth"],
                refresh=fake_claude_refresh,
            ),
        },
    )
    monkeypatch.setattr(
        oauth_tokens_db,
        "get_db_connection",
        get_db_conn_refresh,
    )
    _real_get_tokens_3e = oauth_tokens_db.get_oauth_tokens
    monkeypatch.setattr(
        oauth_tokens_db,
        "get_oauth_tokens",
        create_autospec(_real_get_tokens_3e, return_value=expired_row),
    )
    monkeypatch.setattr(
        oauth_health_mod,
        "get_oauth_tokens",
        create_autospec(_real_get_tokens_3e, return_value=expired_row),
    )
    caplog.clear()
    claude_refreshed = await oauth_health_mod.valid_token(MEMBER_ID, "claude-oauth")
    assert claude_refreshed is not None
    assert claude_refreshed.access_token == "claude-new-tok"
    assert lock_keys[-1] == f"oauth:refresh:{HOLDER_ID}:claude-oauth"
    # replace_refreshed_tokens writes the HOLDER's row (mapping inside).
    replace_sql, replace_params = executed_refresh[-1]
    assert "UPDATE user_oauth_tokens" in replace_sql
    assert replace_params[-4] == HOLDER_ID
    assert replace_params[-3] == "claude-oauth"


# ---------------------------------------------------------------------------
# 3f. A refresh failure a non-holder triggers flags the HOLDER's row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_triggered_refresh_failure_flags_holder_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a member's turn triggers a refresh that fails for good, the
    needs-reauth flag lands on the credential holder's row, not the member's."""
    import src.server.services.oauth_health as oauth_health_mod

    _enable_team_sharing(monkeypatch)
    expires = datetime.now(timezone.utc) - timedelta(minutes=5)
    expired_row = {
        "access_token": "holder-access-tok",
        "refresh_token": "holder-refresh-tok",
        "account_id": "acct-holder",
        "email": "owner@everflow.ai",
        "plan_type": "pro",
        "expires_at": expires,
    }

    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.acquire_lock.side_effect = None
    fake_cache.acquire_lock.return_value = True
    fake_cache.release_lock.return_value = None
    fake_cache.enabled = True
    fake_cache.client = create_autospec(aioredis.Redis, instance=True)
    fake_cache.client.delete.return_value = 1
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    monkeypatch.setattr(
        oauth_health_mod,
        "get_oauth_tokens",
        create_autospec(oauth_health_mod.get_oauth_tokens, return_value=expired_row),
    )

    import httpx

    refused = httpx.Response(
        401, request=httpx.Request("POST", "https://token.example"), json={"error": "invalid_grant"}
    )
    import src.server.services.oauth_providers as oauth_providers_mod
    from dataclasses import replace as _dc_replace

    fake_codex_refresh = create_autospec(
        codex_oauth_mod.refresh_tokens,
        side_effect=httpx.HTTPStatusError("401", request=refused.request, response=refused),
    )
    monkeypatch.setattr(
        oauth_health_mod,
        "SUBSCRIPTIONS",
        {
            **oauth_providers_mod.SUBSCRIPTIONS,
            "codex-oauth": _dc_replace(
                oauth_providers_mod.SUBSCRIPTIONS["codex-oauth"],
                refresh=fake_codex_refresh,
            ),
        },
    )

    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    with pytest.raises(oauth_health_mod.OAuthReauthRequired):
        await oauth_health_mod.valid_token(MEMBER_ID, "codex-oauth")

    flag_sql, flag_params = executed[-1]
    assert "needs_reauth_at" in flag_sql
    assert flag_params[1] == HOLDER_ID
    assert flag_params[2] == "codex-oauth"


# ---------------------------------------------------------------------------
# 4. Member writes land on holder & user status queries check holder only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_writes_land_on_holder_and_user_status_checks_holder_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When a non-holder member connects OAuth or saves an API key, writes target `HOLDER_ID`, logs record both actor and `credential_user_id`, and user status queries check `HOLDER_ID` only."""
    import logging

    _enable_team_sharing(monkeypatch)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    caplog.set_level(logging.DEBUG)

    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    # 4a. OAuth token upsert & delete -> writes HOLDER_ID and logs both actor and credential_user_id
    get_db_conn_oauth, _, executed_oauth = _make_autospec_db(
        fetchone_row={"user_id": HOLDER_ID, "provider": "antigravity-oauth"}
    )
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn_oauth)
    await oauth_tokens_db.upsert_oauth_tokens(
        user_id=MEMBER_ID,
        provider="antigravity-oauth",
        access_token="member-connected-tok",
        refresh_token="member-refresh-tok",
        account_id="proj-1",
        email="member@everflow.ai",
        plan_type="standard-tier",
        expires_at=expires,
    )
    assert executed_oauth[0][1][0] == HOLDER_ID
    assert f"upsert user_id={MEMBER_ID} credential_user_id={HOLDER_ID}" in caplog.text

    await oauth_tokens_db.delete_oauth_tokens(SECOND_PM_ID, "antigravity-oauth")
    assert executed_oauth[1][1] == (HOLDER_ID, "antigravity-oauth")
    assert f"deleted user_id={SECOND_PM_ID} credential_user_id={HOLDER_ID}" in caplog.text

    # 4b. BYOK API key upsert, set_byok_enabled, update_base_url, delete_api_key -> writes HOLDER_ID and logs both actor and credential_user_id
    get_db_conn_key, _, executed_key = _make_autospec_db(
        fetchone_row={"byok_enabled": True}
    )
    monkeypatch.setattr(api_keys_db, "get_db_connection", get_db_conn_key)
    await api_keys_db.upsert_api_key(
        user_id=MEMBER_ID,
        provider="openai",
        api_key="sk-team",
        base_url="https://api.openai.com/v1",
    )
    assert executed_key[0][1][0] == HOLDER_ID
    assert f"upsert_key user_id={MEMBER_ID} credential_user_id={HOLDER_ID}" in caplog.text

    await api_keys_db.set_byok_enabled(MEMBER_ID, True)
    assert executed_key[1][1] == (True, HOLDER_ID)
    assert f"set_byok_enabled user_id={MEMBER_ID} credential_user_id={HOLDER_ID}" in caplog.text

    await api_keys_db.update_base_url(SECOND_PM_ID, "openai", "https://pm-proxy.example/v1")
    assert executed_key[2][1] == ("https://pm-proxy.example/v1", HOLDER_ID, "openai")
    assert f"update_base_url user_id={SECOND_PM_ID} credential_user_id={HOLDER_ID}" in caplog.text

    await api_keys_db.delete_api_key(SECOND_PM_ID, "openai")
    assert executed_key[3][1] == (HOLDER_ID, "openai")
    assert f"delete_key user_id={SECOND_PM_ID} credential_user_id={HOLDER_ID}" in caplog.text

    # 4c. `user_db.get_user`, `update_user`, `create_user_from_auth` check holder ONLY for has_api_key / has_oauth_token
    now_dt = datetime.now(timezone.utc)
    fake_user_row = {
        "user_id": MEMBER_ID,
        "email": "member@everflow.ai",
        "name": "Member",
        "avatar_url": None,
        "timezone": "Asia/Shanghai",
        "locale": "zh-CN",
        "onboarding_completed": True,
        "personalization_completed": True,
        "auth_provider": "email",
        "created_at": now_dt,
        "updated_at": now_dt,
        "last_login_at": now_dt,
        "has_api_key": True,
        "has_oauth_token": True,
    }
    get_db_conn_user, _, executed_user = _make_autospec_db(fetchone_row=fake_user_row)
    monkeypatch.setattr(user_db, "get_db_connection", get_db_conn_user)

    got_user = await user_db.get_user(MEMBER_ID)
    assert got_user is not None
    assert got_user["user_id"] == MEMBER_ID
    assert "IN (users.user_id" not in executed_user[0][0]
    assert executed_user[0][1] == (HOLDER_ID, HOLDER_ID, MEMBER_ID)

    updated_user = await user_db.update_user(MEMBER_ID, name="Member Updated")
    assert updated_user is not None
    assert executed_user[1][1] == ("Member Updated", MEMBER_ID, HOLDER_ID, HOLDER_ID)

    created_auth = await user_db.create_user_from_auth(MEMBER_ID, email="member@everflow.ai")
    assert created_auth["user_id"] == MEMBER_ID
    assert executed_user[2][1][-2:] == (HOLDER_ID, HOLDER_ID)
    assert executed_user[2][1][0] == MEMBER_ID


# ---------------------------------------------------------------------------
# 5. Non-PM delete/disconnect/empty-key/disable-BYOK returns 403; PM succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_pm_delete_and_disconnect_forbidden_while_pm_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-PM members get HTTP 403 (`团队共用凭据只能由管理员断开`) on OAuth disconnect, API key delete/clear, and `byok_enabled=False`; PMs are allowed."""
    _enable_team_sharing(monkeypatch)

    monkeypatch.setattr(
        api_keys_app,
        "_get_custom_providers",
        create_autospec(api_keys_app._get_custom_providers, return_value=[]),
    )

    # Non-PM member is rejected before touching DB/services on:
    # - OAuth disconnect (Codex, Claude, Muse, Antigravity)
    # - DELETE /api-keys/{provider}
    # - PUT /api-keys with None, empty string "", whitespace "   ", or None + base_url
    # - PUT /api-keys with byok_enabled=False
    for call in [
        lambda: oauth_app.oauth_disconnect(key="codex", user_id=MEMBER_ID),
        lambda: oauth_app.oauth_disconnect(key="claude", user_id=MEMBER_ID),
        lambda: oauth_app.oauth_disconnect(key="grok", user_id=MEMBER_ID),
        lambda: oauth_native_app.muse_disconnect(user_id=MEMBER_ID),
        lambda: oauth_native_app.antigravity_disconnect(user_id=MEMBER_ID),
        lambda: oauth_native_app.xai_disconnect(user_id=MEMBER_ID),
        lambda: api_keys_app.remove_api_key(provider="openai", user_id=MEMBER_ID),
        lambda: api_keys_app.update_api_keys(
            body=UpdateApiKeysRequest(api_keys={"openai": None}),
            user_id=MEMBER_ID,
        ),
        lambda: api_keys_app.update_api_keys(
            body=UpdateApiKeysRequest(api_keys={"openai": ""}),
            user_id=MEMBER_ID,
        ),
        lambda: api_keys_app.update_api_keys(
            body=UpdateApiKeysRequest(api_keys={"openai": "   "}),
            user_id=MEMBER_ID,
        ),
        lambda: api_keys_app.update_api_keys(
            body=UpdateApiKeysRequest(
                api_keys={"openai": None},
                base_urls={"openai": "https://api.openai.com/v1"},
            ),
            user_id=MEMBER_ID,
        ),
        lambda: api_keys_app.update_api_keys(
            body=UpdateApiKeysRequest(byok_enabled=False),
            user_id=MEMBER_ID,
        ),
        lambda: api_keys_app.update_api_keys(
            body=UpdateApiKeysRequest(base_urls={"openai": "https://evil.example/v1"}),
            user_id=MEMBER_ID,
        ),
        lambda: api_keys_app.update_api_keys(
            body=UpdateApiKeysRequest(base_urls={"openai": None}),
            user_id=MEMBER_ID,
        ),
    ]:
        with pytest.raises(HTTPException) as exc_info:
            await call()
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "团队共用凭据只能由管理员断开"

    # Non-PM member CAN still add/update non-empty API keys (including with base_urls for the same provider) and enable BYOK (`byok_enabled=True`),
    # while mixed requests (e.g. `byok_enabled=True` + clearing `base_urls` or clearing another key) are rejected with 403 BEFORE any DB or cache write!
    fake_set_byok_enabled = create_autospec(api_keys_app.set_byok_enabled, return_value=True)
    fake_upsert_api_key = create_autospec(api_keys_app.upsert_api_key, return_value=None)
    fake_update_base_url = create_autospec(api_keys_db.update_base_url, return_value=None)
    fake_invalidate_byok_cache = create_autospec(api_keys_app.invalidate_byok_cache, return_value=None)
    monkeypatch.setattr(
        api_keys_app,
        "set_byok_enabled",
        fake_set_byok_enabled,
    )
    monkeypatch.setattr(
        api_keys_app,
        "upsert_api_key",
        fake_upsert_api_key,
    )
    monkeypatch.setattr(
        api_keys_db,
        "update_base_url",
        fake_update_base_url,
    )
    monkeypatch.setattr(
        api_keys_app,
        "invalidate_byok_cache",
        fake_invalidate_byok_cache,
    )
    monkeypatch.setattr(
        onboarding_svc,
        "maybe_complete_personalization",
        create_autospec(
            onboarding_svc.maybe_complete_personalization,
            return_value=None,
        ),
    )
    monkeypatch.setattr(
        api_keys_app,
        "get_user_api_keys",
        create_autospec(
            api_keys_app.get_user_api_keys,
            return_value={"byok_enabled": True, "keys": {}, "base_urls": {}},
        ),
    )

    for mixed_body in [
        UpdateApiKeysRequest(byok_enabled=True, base_urls={"openai": None}),
        UpdateApiKeysRequest(
            byok_enabled=True,
            api_keys={"openai": "sk-good"},
            base_urls={"anthropic": "https://evil.example/v1"},
        ),
        UpdateApiKeysRequest(
            byok_enabled=True,
            api_keys={"openai": "sk-good", "anthropic": None},
        ),
    ]:
        with pytest.raises(HTTPException) as exc_info:
            await api_keys_app.update_api_keys(body=mixed_body, user_id=MEMBER_ID)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "团队共用凭据只能由管理员断开"
        fake_set_byok_enabled.assert_not_awaited()
        fake_upsert_api_key.assert_not_awaited()
        fake_update_base_url.assert_not_awaited()
        fake_invalidate_byok_cache.assert_not_awaited()

    res_add = await api_keys_app.update_api_keys(
        body=UpdateApiKeysRequest(
            byok_enabled=True,
            api_keys={"openai": "sk-member-new"},
            base_urls={"openai": "https://member-proxy.example/v1"},
        ),
        user_id=MEMBER_ID,
    )
    assert res_add["byok_enabled"] is True
    fake_set_byok_enabled.assert_awaited_once_with(MEMBER_ID, True)
    fake_upsert_api_key.assert_awaited_once_with(
        MEMBER_ID, "openai", "sk-member-new", base_url="https://member-proxy.example/v1"
    )
    fake_update_base_url.assert_not_awaited()
    fake_invalidate_byok_cache.assert_awaited_once_with(MEMBER_ID)

    # Both HOLDER_ID and SECOND_PM_ID pass `require_credential_admin` and can update base_urls alone / disable BYOK / delete keys / disconnect OAuth
    assert cred_mod.is_credential_admin(HOLDER_ID) is True
    assert cred_mod.is_credential_admin(SECOND_PM_ID) is True
    assert cred_mod.is_credential_admin(MEMBER_ID) is False

    res_pm_base_url = await api_keys_app.update_api_keys(
        body=UpdateApiKeysRequest(base_urls={"openai": "https://pm-gateway.example/v1"}),
        user_id=SECOND_PM_ID,
    )
    assert res_pm_base_url is not None
    fake_update_base_url.assert_awaited_once_with(
        SECOND_PM_ID, "openai", "https://pm-gateway.example/v1"
    )

    monkeypatch.setattr(
        api_keys_app,
        "delete_api_key",
        create_autospec(api_keys_app.delete_api_key, return_value=None),
    )
    res_pm_disable = await api_keys_app.update_api_keys(
        body=UpdateApiKeysRequest(byok_enabled=False, api_keys={"openai": None}),
        user_id=SECOND_PM_ID,
    )
    assert res_pm_disable is not None

    monkeypatch.setattr(
        oauth_app,
        "delete_oauth_tokens",
        create_autospec(oauth_app.delete_oauth_tokens, return_value=None),
    )

    @asynccontextmanager
    async def _null_connection_change(user_id: str, provider: str):
        yield None

    monkeypatch.setattr(
        oauth_app,
        "connection_change",
        _null_connection_change,
    )
    monkeypatch.setattr(
        oauth_app,
        "connection_changed",
        create_autospec(oauth_app.connection_changed, return_value=None),
    )
    pm_disconnect = await oauth_app.oauth_disconnect(key="codex", user_id=SECOND_PM_ID)
    assert pm_disconnect == {"success": True}


# ---------------------------------------------------------------------------
# 6. Journal & billing still record the actual caller (`user_id`), with
#    `credential_user_id` recorded in `usage` JSONB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_records_caller_user_id_and_stamps_credential_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dalton_llm_journal.user_id` stays the actual caller (`MEMBER_ID`), while `usage.credential_user_id` records `HOLDER_ID`."""
    _enable_team_sharing(monkeypatch)

    # 6a. `claim_journal_invocation` SQL parameters keep `user_id = MEMBER_ID` and stamp `credential_user_id = HOLDER_ID` in `usage`
    get_db_conn, _, executed = _make_autospec_db(
        fetchone_row={
            "invocation_id": "inv-member-1",
            "user_id": MEMBER_ID,
            "usage": {"credential_user_id": HOLDER_ID},
            "state": "pending",
            "attempt": 1,
        }
    )
    monkeypatch.setattr(journal_db, "get_db_connection", get_db_conn)
    action, row = await journal_db.claim_journal_invocation(
        invocation_id="inv-member-1",
        user_id=MEMBER_ID,
        scope_ref="turn:1",
        purpose="unit.test",
        model_key="codex-mini-oauth",
        provider="codex-oauth",
        billing_type="oauth",
        request_hash="hash-1",
    )
    assert action == "claimed"
    assert row["user_id"] == MEMBER_ID
    insert_params = executed[0][1]
    assert insert_params[0] == "inv-member-1"
    assert insert_params[1] == MEMBER_ID  # caller user_id, NOT holder!
    assert f'"credential_user_id": "{HOLDER_ID}"' in insert_params[8]

    # 6b. `dalton_complete` passes caller `user_id = MEMBER_ID` to `claim_journal_invocation` and stamps `credential_user_id = HOLDER_ID` into `complete_journal_done` usage
    from src.server.services.llm.service import LLMService

    fake_llm_service = create_autospec(LLMService, instance=True)
    fake_llm_service.complete.return_value = (
        "ok-reply",
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )

    fake_inspect = create_autospec(
        complete_mod.inspect_journal_state,
        return_value=("missing", None),
    )
    fake_claim = create_autospec(
        complete_mod.claim_journal_invocation,
        return_value=("claimed", {"attempt": 1, "user_id": MEMBER_ID}),
    )
    fake_done = create_autospec(
        complete_mod.complete_journal_done,
        return_value={"invocation_id": "inv-2", "state": "done"},
    )
    monkeypatch.setattr(complete_mod, "inspect_journal_state", fake_inspect)
    monkeypatch.setattr(complete_mod, "claim_journal_invocation", fake_claim)
    monkeypatch.setattr(complete_mod, "complete_journal_done", fake_done)
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, return_value=None),
    )

    result = await complete_mod.dalton_complete(
        llm_service=fake_llm_service,
        user_id=MEMBER_ID,
        system_prompt="sys",
        user_prompt="hello",
        scope_ref="turn:member-1",
        purpose="unit.test",
        request_model="gemini-3-flash-oauth",
    )
    assert result.ok is True
    fake_claim.assert_awaited_once()
    assert fake_claim.call_args.kwargs["user_id"] == MEMBER_ID
    fake_done.assert_awaited_once()
    done_usage = fake_done.call_args.kwargs["usage"]
    assert done_usage["credential_user_id"] == HOLDER_ID
    assert done_usage["total_tokens"] == 15

    # 6c. When sharing is off, `claim_journal_invocation`, `complete_journal_done`, and `dalton_complete` match upstream (no `credential_user_id`, full column replacement `usage = %s::jsonb`)
    _enable_team_sharing(monkeypatch, sharing="off")
    get_db_conn_off, _, executed_off = _make_autospec_db(
        fetchone_row={
            "invocation_id": "inv-off-1",
            "user_id": MEMBER_ID,
            "usage": {},
            "state": "pending",
            "attempt": 1,
        }
    )
    monkeypatch.setattr(journal_db, "get_db_connection", get_db_conn_off)
    await journal_db.claim_journal_invocation(
        invocation_id="inv-off-1",
        user_id=MEMBER_ID,
        scope_ref="turn:off",
        purpose="unit.test",
        model_key="codex-mini-oauth",
        provider="codex-oauth",
        billing_type="oauth",
        request_hash="hash-off",
    )
    assert len(executed_off[0][1]) == 8  # no initial_usage_json parameter
    assert "usage" not in executed_off[0][0]

    await journal_db.complete_journal_done(
        "inv-off-1",
        response={"text": "ok"},
        usage={"total_tokens": 15},
    )
    assert "usage = %s::jsonb" in executed_off[1][0]
    assert "||" not in executed_off[1][0]

    fake_done.reset_mock()
    res_off = await complete_mod.dalton_complete(
        llm_service=fake_llm_service,
        user_id=MEMBER_ID,
        system_prompt="sys",
        user_prompt="hello",
        scope_ref="turn:off",
        purpose="unit.test",
        request_model="gemini-3-flash-oauth",
    )
    assert res_off.ok is True
    assert "credential_user_id" not in fake_done.call_args.kwargs["usage"]


# ---------------------------------------------------------------------------
# 7. A disconnect cancels every member's pending sign-in, and a late approval
#    cannot revive the connection
# ---------------------------------------------------------------------------


class _FakeRedisStore:
    """A tiny Redis: get/set/delete plus the compare-and-delete ``eval`` the
    cache's claim runs, over one dict."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[str(key)] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if str(k) in self.store:
                del self.store[str(k)]
                removed += 1
        return removed

    async def ttl(self, key: str) -> int:
        return 60 if str(key) in self.store else -2

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        key = str(keys_and_args[0])
        value = str(keys_and_args[1])
        if "del" in script:
            if self.store.get(key) == value:
                del self.store[key]
                return 1
            return 0
        return 1


def _fake_cache_with_store(store: _FakeRedisStore):
    cache = create_autospec(RedisCacheClient(), instance=True)
    cache.enabled = True
    fake_redis = create_autospec(redis_asyncio.Redis, instance=True)
    fake_redis.get.side_effect = store.get
    fake_redis.set.side_effect = store.set
    fake_redis.delete.side_effect = store.delete
    fake_redis.ttl.side_effect = store.ttl
    fake_redis.eval.side_effect = store.eval
    cache.client = fake_redis

    async def _claim(key: str, value: str) -> bool:
        cur = store.store.get(key)
        return cur == value and bool(await store.eval("del", 1, key, value))

    async def _swap(key: str, old_value: str, new_value: str) -> bool:
        if store.store.get(key) == old_value:
            store.store[key] = new_value
            return True
        return False

    cache.claim.side_effect = _claim
    cache.swap.side_effect = _swap
    return cache


@pytest.mark.asyncio
async def test_member_signin_cancelled_by_admin_disconnect_does_not_revive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Member A initiates Muse and xAI sign-ins; admin B disconnects the shared
    connection; A's approvals arriving after that cannot save — the holder-keyed
    pending flows are gone, so both polls refuse instead of reviving."""
    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )
    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    fake_minted = {"api_key": "late-muse-key", "base_url": "https://api.meta.ai/v1", "plan_type": "Pro"}
    monkeypatch.setattr(
        oauth_native_app,
        "muse_request_device_code",
        create_autospec(oauth_native_app.muse_request_device_code, return_value={
            "device_code": "dc-muse", "user_code": "UC-MUSE", "verification_url": "https://auth.meta.com/device", "interval": 5, "expires_in": 600,
        }),
    )
    monkeypatch.setattr(
        oauth_native_app,
        "muse_poll_device_authorization",
        create_autospec(oauth_native_app.muse_poll_device_authorization, return_value=fake_minted),
    )
    fake_muse_save = create_autospec(muse_oauth_mod.save_minted_credentials)
    monkeypatch.setattr(oauth_native_app, "muse_save_minted_credentials", fake_muse_save)

    # Member initiates; the pending flow lives under the HOLDER's key.
    await oauth_native_app.muse_device_initiate(user_id=MEMBER_ID)
    assert f"oauth:muse:device:{HOLDER_ID}" in store.store
    assert f"oauth:muse:device:{MEMBER_ID}" not in store.store

    # Admin disconnects the shared connection.
    await oauth_native_app.muse_disconnect(user_id=SECOND_PM_ID)
    delete_params = [p for sql, p in executed if "DELETE FROM user_oauth_tokens" in str(sql)]
    assert any(p == (HOLDER_ID, "muse-oauth") for p in delete_params)
    assert f"oauth:muse:device:{HOLDER_ID}" not in store.store

    # The member's approval lands: the flow is gone, the save is refused.
    with pytest.raises(HTTPException) as late:
        await oauth_native_app.muse_device_poll(user_id=MEMBER_ID)
    assert late.value.status_code == 400
    fake_muse_save.assert_not_awaited()

    # Same race on the xAI line.
    monkeypatch.setattr(
        oauth_native_app,
        "xai_request_device_code",
        create_autospec(oauth_native_app.xai_request_device_code, return_value={
            "device_code": "dc-xai", "user_code": "UC-XAI", "verification_url": "https://auth.x.ai/device", "interval": 5, "expires_in": 600,
        }),
    )
    monkeypatch.setattr(
        oauth_native_app,
        "xai_poll_device_authorization",
        create_autospec(oauth_native_app.xai_poll_device_authorization, return_value={
            "access_token": "late-xai-tok", "refresh_token": "late-xai-ref", "account_id": "xai-sub", "expires_in": 3600,
        }),
    )
    import src.dalton.llm.providers.xai_grok.oauth as xai_oauth_mod

    fake_xai_save = create_autospec(xai_oauth_mod.save_xai_tokens)
    monkeypatch.setattr(oauth_native_app, "save_xai_tokens", fake_xai_save)

    # The same person signs in from another tab (SECOND_PM_ID is a PM: a
    # disconnector), then disconnects mid-sign-in; the late approval below is
    # their own and still cannot revive the connection.
    await oauth_native_app.xai_device_initiate(user_id=SECOND_PM_ID)
    assert f"oauth:xai:device:{HOLDER_ID}" in store.store
    await oauth_native_app.xai_disconnect(user_id=SECOND_PM_ID)
    assert any(
        p == (HOLDER_ID, "xai-oauth") for sql, p in executed if "DELETE FROM user_oauth_tokens" in str(sql)
    )
    with pytest.raises(HTTPException) as late_xai:
        await oauth_native_app.xai_device_poll(user_id=SECOND_PM_ID)
    assert late_xai.value.status_code == 400
    fake_xai_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_antigravity_signin_cancelled_by_disconnect_does_not_revive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Antigravity's save claims its flow key inside the connection lock: a
    disconnect that landed while Google was answering leaves the key gone and
    the save aborts."""
    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )
    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    # A pending flow exists for the holder.
    late_flow_raw = json.dumps(
        {"verifier": "v", "state": "s", "port": 51121, "redirect_uri": "http://localhost:51121/callback"}
    )
    store.store[f"oauth:antigravity:{HOLDER_ID}"] = late_flow_raw

    # The admin disconnects before the callback lands.
    await oauth_native_app.antigravity_disconnect(user_id=SECOND_PM_ID)
    assert f"oauth:antigravity:{HOLDER_ID}" not in store.store
    assert f"oauth:antigravity:done:{HOLDER_ID}" not in store.store

    fake_upsert = create_autospec(agy_oauth_mod.upsert_oauth_tokens, return_value=None)
    monkeypatch.setattr(agy_oauth_mod, "upsert_oauth_tokens", fake_upsert)
    # The late callback carries the flow it acted on; the disconnect cleared it.
    with pytest.raises(ValueError):
        await agy_oauth_mod._finalize_token_save(
            MEMBER_ID,
            {"access_token": "late-agy", "refresh_token": "late-ref", "id_token": "", "expires_in": 3600},
            expected_flow_raw=late_flow_raw,
        )
    fake_upsert.assert_not_awaited()

    # And the reverse order: a save that already claimed the flow is not
    # clobbered by the disconnect — the disconnect deletes the row after.
    fake_upsert.reset_mock()
    live_flow_raw = json.dumps({"verifier": "v2"})
    store.store[f"oauth:antigravity:{HOLDER_ID}"] = live_flow_raw
    await agy_oauth_mod._finalize_token_save(
        MEMBER_ID,
        {"access_token": "ok-agy", "refresh_token": "ok-ref", "id_token": "", "expires_in": 3600},
        expected_flow_raw=live_flow_raw,
    )
    fake_upsert.assert_awaited_once()
    assert fake_upsert.call_args.kwargs["provider"] == "antigravity-oauth"
    assert f"oauth:antigravity:done:{HOLDER_ID}" in store.store
    await oauth_native_app.antigravity_disconnect(user_id=SECOND_PM_ID)
    delete_params = [p for sql, p in executed if "DELETE FROM user_oauth_tokens" in str(sql)]
    assert any(p == (HOLDER_ID, "antigravity-oauth") for p in delete_params)


@pytest.mark.asyncio
async def test_upstream_pending_flows_keyed_by_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The built-in device and Claude flows keep their pending state under the
    credential holder's key, so an admin's disconnect clears a member's pending
    sign-in and its late approval reads as no pending flow."""
    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )

    from src.server.app import oauth as oauth_app_mod
    from src.server.services import codex_oauth

    codex_flow = codex_oauth.DEVICE_FLOW
    # Member initiates: state lands under the HOLDER's key.
    assert oauth_app_mod._pending_flow_key("codex", MEMBER_ID) == f"{codex_flow.redis_key}:{HOLDER_ID}"
    store.store[f"{codex_flow.redis_key}:{HOLDER_ID}"] = json.dumps({"device_code": "dc", "expires_at": "", "interval": 5, "next_poll_at": 0, "flow_id": "f1"})

    # Admin disconnect clears it.
    flow_key = oauth_app_mod._pending_flow_key("codex", SECOND_PM_ID)
    await store.delete(flow_key)
    assert oauth_app_mod._pending_flow_key("codex", MEMBER_ID) not in store.store

    # The member's late poll reads no pending flow, before ever asking the provider.
    fake_poll = create_autospec(codex_flow.poll)
    result = await oauth_app_mod._poll_once(cache, codex_flow, MEMBER_ID)
    assert result["status"] == "expired"
    assert result["code"] == "no_pending_flow"
    fake_poll.assert_not_awaited()

    # Claude's flow key is holder-keyed too.
    assert oauth_app_mod._claude_key(MEMBER_ID) == f"oauth:claude:{HOLDER_ID}"


# ---------------------------------------------------------------------------
# 8. In-flight races: the upstream answer arrives AFTER a disconnect (or a
#    disconnect + newer sign-in) that landed while it was being waited on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_approval_in_flight_when_admin_disconnects_does_not_revive(
    monkeypatch: pytest.MonkeyPatch,
):
    """The real low-level poll runs to the point of returning credentials (the
    mint call is what's held), while an admin disconnect lands mid-flight: the
    minted key is never saved, and a newer sign-in's pending flow survives."""
    import gzip
    import httpx

    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )
    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    mint_release: asyncio.Future[httpx.Response] = asyncio.get_event_loop().create_future()
    mint_body = gzip.compress(
        json.dumps({"api_key": "late-minted-key", "base_url": "https://api.meta.ai/v1", "subs_tier_name": "Pro"}).encode()
    )

    async def _fake_post(self, url: str, *, data=None, json=None, headers=None, **kwargs):
        if url == muse_oauth_mod.MUSE_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "transient-tok", "token_type": "Bearer"})
        if url == muse_oauth_mod.MUSE_MINT_KEY_URL:
            return await mint_release
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        create_autospec(httpx.AsyncClient.post, side_effect=_fake_post),
    )
    fake_save = create_autospec(muse_oauth_mod.save_minted_credentials)
    monkeypatch.setattr(oauth_native_app, "muse_save_minted_credentials", fake_save)

    device_codes = iter(["dc-old", "dc-new"])

    async def _device_code():
        code = next(device_codes)
        return {"device_code": code, "user_code": f"UC-{code}", "verification_url": "https://auth.meta.com/device", "interval": 5, "expires_in": 600}

    monkeypatch.setattr(
        oauth_native_app,
        "muse_request_device_code",
        create_autospec(oauth_native_app.muse_request_device_code, side_effect=_device_code),
    )
    await oauth_native_app.muse_device_initiate(user_id=MEMBER_ID)
    old_raw = store.store[f"oauth:muse:device:{HOLDER_ID}"]

    poll_task = asyncio.ensure_future(oauth_native_app.muse_device_poll(user_id=MEMBER_ID))
    await asyncio.sleep(0)  # let the poll reach the held mint call

    # Mid-flight: the admin disconnects, then a newer sign-in starts.
    await oauth_native_app.muse_disconnect(user_id=SECOND_PM_ID)
    await oauth_native_app.muse_device_initiate(user_id=MEMBER_ID)
    new_raw = store.store[f"oauth:muse:device:{HOLDER_ID}"]
    assert new_raw != old_raw

    # The approval lands.
    mint_release.set_result(
        httpx.Response(200, headers={"content-encoding": "gzip"}, content=mint_body)
    )
    with pytest.raises(HTTPException) as late:
        await poll_task
    assert late.value.status_code == 400
    # The minted key was never stored, and the newer sign-in's flow survives.
    fake_save.assert_not_awaited()
    assert store.store[f"oauth:muse:device:{HOLDER_ID}"] == new_raw
    assert not any(
        "INSERT INTO user_oauth_tokens" in str(sql) for sql, _ in executed
    )


@pytest.mark.asyncio
async def test_xai_slow_down_in_flight_does_not_resurrect_cleared_flow(
    monkeypatch: pytest.MonkeyPatch,
):
    """A slow_down answer held past a disconnect (or a newer sign-in) cannot
    write the old pending payload back: the swap is conditional on the raw
    value this poll read, so the cleared key stays cleared and a newer flow
    stays intact."""
    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )
    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    device_codes = iter(["dc-old", "dc-new"])

    async def _device_code(user_id=None):
        code = next(device_codes)
        return {"device_code": code, "user_code": f"UC-{code}", "verification_url": "https://auth.x.ai/device", "interval": 5, "expires_in": 600}

    monkeypatch.setattr(
        oauth_native_app,
        "xai_request_device_code",
        create_autospec(oauth_native_app.xai_request_device_code, side_effect=_device_code),
    )
    await oauth_native_app.xai_device_initiate(user_id=MEMBER_ID)
    old_raw = store.store[f"oauth:xai:device:{HOLDER_ID}"]

    poll_release: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
    real_poll = oauth_native_app.xai_poll_device_authorization

    async def _held_poll(*args, **kwargs):
        return await poll_release

    monkeypatch.setattr(
        oauth_native_app,
        "xai_poll_device_authorization",
        create_autospec(real_poll, side_effect=_held_poll),
    )

    poll_task = asyncio.ensure_future(oauth_native_app.xai_device_poll(user_id=MEMBER_ID))
    await asyncio.sleep(0)

    # Mid-flight: disconnect clears the flow; a newer sign-in starts.
    await oauth_native_app.xai_disconnect(user_id=SECOND_PM_ID)
    await oauth_native_app.xai_device_initiate(user_id=MEMBER_ID)
    new_raw = store.store[f"oauth:xai:device:{HOLDER_ID}"]
    assert new_raw != old_raw

    # The slow_down answer arrives: the old poll must not touch the new flow.
    poll_release.set_result({"pending": True, "interval": 9})
    with pytest.raises(HTTPException) as late:
        await poll_task
    assert late.value.status_code == 400
    assert "cancelled or replaced" in str(late.value.detail)
    assert store.store[f"oauth:xai:device:{HOLDER_ID}"] == new_raw

    # And with no newer sign-in, the cleared key stays cleared.
    await store.delete(f"oauth:xai:device:{HOLDER_ID}")
    store.store[f"oauth:xai:device:{HOLDER_ID}"] = old_raw
    poll2_release: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

    async def _held_poll2(*args, **kwargs):
        return await poll2_release

    monkeypatch.setattr(
        oauth_native_app,
        "xai_poll_device_authorization",
        create_autospec(real_poll, side_effect=_held_poll2),
    )
    poll2 = asyncio.ensure_future(oauth_native_app.xai_device_poll(user_id=MEMBER_ID))
    await asyncio.sleep(0)
    await store.delete(f"oauth:xai:device:{HOLDER_ID}")
    poll2_release.set_result({"pending": True, "interval": 9})
    with pytest.raises(HTTPException) as late2:
        await poll2
    assert late2.value.status_code == 400
    assert f"oauth:xai:device:{HOLDER_ID}" not in store.store


@pytest.mark.asyncio
async def test_xai_approval_in_flight_when_admin_disconnects_does_not_revive(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )
    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    import src.dalton.llm.providers.xai_grok.oauth as xai_oauth_mod

    fake_xai_save = create_autospec(xai_oauth_mod.save_xai_tokens)
    monkeypatch.setattr(oauth_native_app, "save_xai_tokens", fake_xai_save)

    async def _device_code(user_id=None):
        return {"device_code": "dc-old", "user_code": "UC-OLD", "verification_url": "https://auth.x.ai/device", "interval": 5, "expires_in": 600}

    monkeypatch.setattr(
        oauth_native_app,
        "xai_request_device_code",
        create_autospec(oauth_native_app.xai_request_device_code, side_effect=_device_code),
    )
    await oauth_native_app.xai_device_initiate(user_id=MEMBER_ID)
    poll_release: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
    real_poll = oauth_native_app.xai_poll_device_authorization

    async def _held_poll(*args, **kwargs):
        return await poll_release

    monkeypatch.setattr(
        oauth_native_app,
        "xai_poll_device_authorization",
        create_autospec(real_poll, side_effect=_held_poll),
    )

    poll_task = asyncio.ensure_future(oauth_native_app.xai_device_poll(user_id=MEMBER_ID))
    await asyncio.sleep(0)
    await oauth_native_app.xai_disconnect(user_id=SECOND_PM_ID)

    poll_release.set_result({"access_token": "late-tok", "refresh_token": "late-ref", "expires_in": 3600})
    with pytest.raises(HTTPException) as late:
        await poll_task
    assert late.value.status_code == 400
    fake_xai_save.assert_not_awaited()
    assert not any("INSERT INTO user_oauth_tokens" in str(sql) for sql, _ in executed)


@pytest.mark.asyncio
async def test_antigravity_old_callback_after_disconnect_and_new_login(
    monkeypatch: pytest.MonkeyPatch,
):
    """A paste callback reads its flow, spends the authorization, and only then
    finds a disconnect plus a newer login landed: the old authorization refuses
    to save, the newer login's flow survives, and the stored row stays gone."""
    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )
    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    fake_upsert = create_autospec(agy_oauth_mod.upsert_oauth_tokens, return_value=None)
    monkeypatch.setattr(agy_oauth_mod, "upsert_oauth_tokens", fake_upsert)

    flow1 = json.dumps({"verifier": "v1", "state": "s1", "port": 51121, "redirect_uri": "http://localhost:51121/callback"})
    store.store[f"oauth:antigravity:{HOLDER_ID}"] = flow1

    exchange_release: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

    async def _held_exchange(code, verifier, redirect_uri):
        return await exchange_release

    monkeypatch.setattr(
        agy_oauth_mod,
        "exchange_code",
        create_autospec(agy_oauth_mod.exchange_code, side_effect=_held_exchange),
    )

    paste_task = asyncio.ensure_future(
        agy_oauth_mod.submit_callback_url(
            MEMBER_ID,
            "http://localhost:51121/callback?code=old-code&state=s1",
        )
    )
    await asyncio.sleep(0)  # the paste has read flow1 and holds in the exchange

    # Mid-flight: disconnect, then a newer login writes its own flow.
    await oauth_native_app.antigravity_disconnect(user_id=SECOND_PM_ID)
    flow2 = json.dumps({"verifier": "v2", "state": "s2", "port": 51122, "redirect_uri": "http://localhost:51122/callback"})
    store.store[f"oauth:antigravity:{HOLDER_ID}"] = flow2

    exchange_release.set_result({"access_token": "old-agy-tok", "refresh_token": "old-ref", "id_token": "", "expires_in": 3600})
    with pytest.raises(ValueError):
        await paste_task
    fake_upsert.assert_not_awaited()
    # The newer login's flow is untouched; no done marker for the old one.
    assert store.store[f"oauth:antigravity:{HOLDER_ID}"] == flow2
    assert f"oauth:antigravity:done:{HOLDER_ID}" not in store.store
    assert not any("INSERT INTO user_oauth_tokens" in str(sql) for sql, _ in executed)


@pytest.mark.asyncio
async def test_antigravity_save_without_cache_refuses(monkeypatch: pytest.MonkeyPatch):
    """No pending-flow store means no verifiable login: the save refuses with a
    clear error instead of writing unconditionally."""
    _enable_team_sharing(monkeypatch)
    disabled = create_autospec(RedisCacheClient(), instance=True)
    disabled.enabled = False
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: disabled, return_value=disabled),
    )
    fake_upsert = create_autospec(agy_oauth_mod.upsert_oauth_tokens, return_value=None)
    monkeypatch.setattr(agy_oauth_mod, "upsert_oauth_tokens", fake_upsert)

    with pytest.raises(ValueError) as refused:
        await agy_oauth_mod._finalize_token_save(
            MEMBER_ID,
            {"access_token": "t", "refresh_token": "r", "id_token": "", "expires_in": 3600},
            expected_flow_raw="{}",
        )
    assert "cannot be verified" in str(refused.value)
    fake_upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_antigravity_stale_done_marker_cannot_complete_newer_login(
    monkeypatch: pytest.MonkeyPatch,
):
    """The completion marker rides the save's connection-lock section: an old
    callback that saved its tokens cannot mark completion AFTER a disconnect
    and a newer login landed — the newer login's poll stays pending instead of
    being reported complete."""
    _enable_team_sharing(monkeypatch)
    store = _FakeRedisStore()
    cache = _fake_cache_with_store(store)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: cache, return_value=cache),
    )
    get_db_conn, _, executed = _make_autospec_db()
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", get_db_conn)

    flow1 = json.dumps({"verifier": "v1", "state": "s1", "port": 51121, "redirect_uri": "http://localhost:51121/callback"})
    store.store[f"oauth:antigravity:{HOLDER_ID}"] = flow1

    # Hold the save mid-critical-section (the token write), Future-gated.
    upsert_release: asyncio.Future[None] = asyncio.get_event_loop().create_future()

    async def _held_upsert(**kwargs):
        await upsert_release

    fake_upsert = create_autospec(agy_oauth_mod.upsert_oauth_tokens, side_effect=_held_upsert)
    monkeypatch.setattr(agy_oauth_mod, "upsert_oauth_tokens", fake_upsert)

    save_task = asyncio.ensure_future(
        agy_oauth_mod._finalize_token_save(
            MEMBER_ID,
            {"access_token": "old-agy-tok", "refresh_token": "old-ref", "id_token": "", "expires_in": 3600},
            expected_flow_raw=flow1,
        )
    )
    await asyncio.sleep(0)  # the save holds inside its critical section

    # "Saved tokens and released the lock": the save finishes here.
    upsert_release.set_result(None)
    await save_task
    fake_upsert.assert_awaited_once()
    # The marker landed inside the same section — before anything later ran.
    # The marker names the flow it completes (DALTON-HOOK(team-credentials)).
    assert store.store[f"oauth:antigravity:done:{HOLDER_ID}"] == agy_oauth_mod._flow_id(flow1)

    # Disconnect + a newer login land after the save.
    await oauth_native_app.antigravity_disconnect(user_id=SECOND_PM_ID)
    flow2 = json.dumps({"verifier": "v2", "state": "s2", "port": 51122, "redirect_uri": "http://localhost:51122/callback"})
    store.store[f"oauth:antigravity:{HOLDER_ID}"] = flow2

    # The old callback has nothing left to write (its marker moved inside the
    # lock), so the newer login's poll reads no completion and stays pending.
    resp = await oauth_native_app.antigravity_poll(user_id=MEMBER_ID)
    assert resp == {"pending": True}
    assert f"oauth:antigravity:done:{HOLDER_ID}" not in store.store
    assert store.store[f"oauth:antigravity:{HOLDER_ID}"] == flow2
    # And the connection row the old save wrote is gone with the disconnect.
    delete_params = [p for sql, p in executed if "DELETE FROM user_oauth_tokens" in str(sql)]
    assert any(p == (HOLDER_ID, "antigravity-oauth") for p in delete_params)
