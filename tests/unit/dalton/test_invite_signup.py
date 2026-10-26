"""Unit tests for Dalton invite signup and team membership (`DALTON-HOOK(invite-signup)`).

All test doubles use `unittest.mock.create_autospec` and make no real network calls.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import create_autospec

import httpx
import psycopg
import pytest
import redis.asyncio as aioredis
from fastapi import HTTPException, Request
from starlette.datastructures import Address, Headers

import src.dalton.app.router as dalton_router
import src.dalton.auth as dalton_auth
import src.dalton.database.members as members_db
import src.dalton.invites as invites_mod
import src.dalton.settings as settings_mod
import src.dalton.sharing as dalton_sharing
from src.config import settings as app_settings
from src.dalton.app.router import InviteCheckRequest, InviteRedeemRequest
from src.server.auth import jwt_bearer
from src.server.auth.jwt_bearer import AuthInfo
from src.server.database import user as user_db
from src.utils.cache.redis_cache import RedisCacheClient

PM_USER_ID = "11111111-1111-4111-8111-111111111111"
NON_PM_USER_ID = "22222222-2222-4222-8222-222222222222"
INVITE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECRET_KEY_VALUE = "sb_secret_never_log_me_998877"


@pytest.fixture(autouse=True)
def _reset_caches_and_redis(monkeypatch: pytest.MonkeyPatch):
    settings_mod.get_dalton_config.cache_clear()
    invites_mod.invalidate_team_members_cache()
    dalton_auth._auth_settings_cache = None
    dalton_auth._verified_token_identity.set(None)
    monkeypatch.setenv("DALTON_PM_USER_IDS", PM_USER_ID)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", SECRET_KEY_VALUE)
    monkeypatch.setenv("DALTON_FRONTEND_URL", "https://dalton.example.com")
    monkeypatch.setenv("DALTON_INVITE_EMAIL_DOMAIN", "team.dalton.local")

    # Default healthy Redis counter for public endpoints unless a test overrides it.
    counts: dict[str, int] = {}
    fake_redis = create_autospec(aioredis.Redis, instance=True)

    async def _incr(name: str, amount: int = 1) -> int:
        counts[name] = counts.get(name, 0) + amount
        return counts[name]

    async def _expire(name: str, time: Any, **kw: Any) -> bool:
        return True

    fake_redis.incr.side_effect = _incr
    fake_redis.expire.side_effect = _expire
    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.enabled = True
    fake_cache.client = fake_redis
    monkeypatch.setattr(
        invites_mod,
        "get_cache_client",
        create_autospec(invites_mod.get_cache_client, return_value=fake_cache),
    )

    yield
    settings_mod.get_dalton_config.cache_clear()
    invites_mod.invalidate_team_members_cache()
    dalton_auth._auth_settings_cache = None
    dalton_auth._verified_token_identity.set(None)


def _make_tx_db() -> tuple[Any, dict[str, Any]]:
    """Build an autospecced transaction connection and state tracker for two-phase redeem tests."""
    state: dict[str, Any] = {
        "tx_entered": 0,
        "tx_committed": 0,
        "tx_rolled_back": 0,
    }
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        state["tx_entered"] += 1
        try:
            yield None
        except Exception:
            state["tx_rolled_back"] += 1
            raise
        else:
            state["tx_committed"] += 1

    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _get_db_conn(conn_arg: Any = None):
        yield conn

    get_db_mock = create_autospec(invites_mod.get_db_connection, side_effect=_get_db_conn)
    return get_db_mock, state


# ---------------------------------------------------------------------------
# 1. PM-only permissions, fragment URL `/invite#<token>`, no token in route paths,
#    revoke 404 & 409, and `can_invite` vs `is_pm` on `/credentials/status`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_create_list_revoke_fragment_url_and_no_token_in_route_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only PM can create/list/revoke; link uses `/invite#<token>`; routes never put token in path; revoke 404/409."""
    # Verify route paths never contain `{token}` and check/redeem are POST body endpoints
    route_paths = {getattr(r, "path", "") for r in dalton_router.router.routes}
    assert "/api/v1/dalton/invites/check" in route_paths
    assert "/api/v1/dalton/invites/redeem" in route_paths
    assert all("{token}" not in p for p in route_paths)

    now = datetime.now(timezone.utc)
    captured_inserts: list[dict[str, Any]] = []

    async def _fake_insert_invite(
        *,
        invite_id: str | None = None,
        token_sha256: str,
        created_by_user_id: str,
        expires_at: datetime,
        conn: Any = None,
    ) -> dict[str, Any]:
        row = {
            "invite_id": invite_id or INVITE_ID,
            "token_sha256": token_sha256,
            "created_by_user_id": created_by_user_id,
            "created_at": now,
            "expires_at": expires_at,
            "redeeming_username": None,
            "redeeming_at": None,
            "used_at": None,
            "used_by_email": None,
            "supabase_user_id": None,
            "revoked_at": None,
        }
        captured_inserts.append(row)
        return row

    monkeypatch.setattr(
        members_db,
        "insert_invite",
        create_autospec(members_db.insert_invite, side_effect=_fake_insert_invite),
    )

    # Non-PM rejected with 403 on create, list, and revoke
    for call in (
        lambda: dalton_router.create_invite_endpoint(user_id=NON_PM_USER_ID),
        lambda: dalton_router.list_invites_endpoint(user_id=NON_PM_USER_ID),
        lambda: dalton_router.revoke_invite_endpoint(
            invite_id=INVITE_ID, user_id=NON_PM_USER_ID
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await call()
        assert exc_info.value.status_code == 403

    # Empty user_id rejected with 401
    with pytest.raises(HTTPException) as exc_unauth:
        await dalton_router.create_invite_endpoint(user_id="")
    assert exc_unauth.value.status_code == 401

    # PM create succeeds: returns fragment link `/invite#<token>`, stores ONLY sha256 in DB
    created = await dalton_router.create_invite_endpoint(user_id=PM_USER_ID)
    raw_token = created["token"]
    assert len(raw_token) >= 40
    assert created["url"] == f"https://dalton.example.com/invite#{raw_token}"
    assert created["invite_url"] == created["url"]
    assert "/invite/" not in created["url"]
    assert created["status"] == "pending"

    assert len(captured_inserts) == 1
    stored_hash = captured_inserts[0]["token_sha256"]
    expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert stored_hash == expected_hash
    assert raw_token not in str(captured_inserts[0].values())
    expiry_delta = captured_inserts[0]["expires_at"] - now
    assert timedelta(days=6, hours=23) <= expiry_delta <= timedelta(days=7, minutes=1)

    # PM list returns all 4 statuses (pending / used / expired / revoked) and never leaks token or hash
    fake_list_rows = [
        {
            "invite_id": "10000000-0000-4000-8000-000000000001",
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(hours=2),
            "expires_at": now + timedelta(days=6),
            "used_at": None,
            "used_by_email": None,
            "revoked_at": None,
            "used_by_username": None,
        },
        {
            "invite_id": "10000000-0000-4000-8000-000000000002",
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(days=1),
            "expires_at": now + timedelta(days=6),
            "used_at": now - timedelta(hours=1),
            "used_by_email": "alice@team.dalton.local",
            "revoked_at": None,
            "used_by_username": "alice",
        },
        {
            "invite_id": "10000000-0000-4000-8000-000000000003",
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(days=8),
            "expires_at": now - timedelta(days=1),
            "used_at": None,
            "used_by_email": None,
            "revoked_at": None,
            "used_by_username": None,
        },
        {
            "invite_id": "10000000-0000-4000-8000-000000000004",
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(days=2),
            "expires_at": now + timedelta(days=5),
            "used_at": None,
            "used_by_email": None,
            "revoked_at": now - timedelta(hours=3),
            "used_by_username": None,
        },
    ]
    monkeypatch.setattr(
        members_db,
        "list_invites",
        create_autospec(members_db.list_invites, return_value=fake_list_rows),
    )
    listed = await dalton_router.list_invites_endpoint(user_id=PM_USER_ID)
    items = listed["invites"]
    assert [i["status"] for i in items] == ["pending", "used", "expired", "revoked"]
    assert [i["status_label"] for i in items] == ["待使用", "已使用", "已过期", "已撤销"]
    assert items[1]["used_by_username"] == "alice"
    for item in items:
        assert "token" not in item
        assert "token_sha256" not in item

    # PM revoke: 404 when invite does not exist, 200 on unused invite, 409 on already-used invite
    orig_get_invite_by_id = members_db.get_invite_by_id
    monkeypatch.setattr(
        members_db,
        "get_invite_by_id",
        create_autospec(orig_get_invite_by_id, return_value=None),
    )
    with pytest.raises(HTTPException) as exc_404:
        await dalton_router.revoke_invite_endpoint(
            invite_id=INVITE_ID, user_id=PM_USER_ID
        )
    assert exc_404.value.status_code == 404

    monkeypatch.setattr(
        members_db,
        "get_invite_by_id",
        create_autospec(
            orig_get_invite_by_id,
            return_value={
                "invite_id": INVITE_ID,
                "created_by_user_id": PM_USER_ID,
                "created_at": now,
                "expires_at": now + timedelta(days=7),
                "used_at": None,
                "used_by_email": None,
                "revoked_at": None,
            },
        ),
    )
    monkeypatch.setattr(
        members_db,
        "revoke_invite",
        create_autospec(
            members_db.revoke_invite,
            return_value={
                "invite_id": INVITE_ID,
                "created_by_user_id": PM_USER_ID,
                "created_at": now,
                "expires_at": now + timedelta(days=7),
                "used_at": None,
                "used_by_email": None,
                "revoked_at": now,
            },
        ),
    )
    revoked = await dalton_router.revoke_invite_endpoint(
        invite_id=INVITE_ID, user_id=PM_USER_ID
    )
    assert revoked["status"] == "revoked"

    monkeypatch.setattr(
        members_db,
        "get_invite_by_id",
        create_autospec(
            orig_get_invite_by_id,
            return_value={
                "invite_id": INVITE_ID,
                "created_by_user_id": PM_USER_ID,
                "created_at": now,
                "expires_at": now + timedelta(days=7),
                "used_at": now,
                "used_by_email": "alice@team.dalton.local",
                "revoked_at": None,
            },
        ),
    )
    with pytest.raises(HTTPException) as exc_used:
        await dalton_router.revoke_invite_endpoint(invite_id=INVITE_ID, user_id=PM_USER_ID)
    assert exc_used.value.status_code == 409

    # `/credentials/status` keeps main-branch `is_pm == can_disconnect` while `can_invite` checks explicit DALTON_PM_USER_IDS
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    settings_mod.get_dalton_config.cache_clear()
    cred_status_unset = await dalton_router.get_team_credentials_status_endpoint(
        user_id=NON_PM_USER_ID
    )
    assert cred_status_unset["is_pm"] is True  # main branch behavior when sharing is off
    assert cred_status_unset["can_invite"] is False  # explicit DALTON_PM_USER_IDS required

    monkeypatch.setenv("DALTON_PM_USER_IDS", PM_USER_ID)
    settings_mod.get_dalton_config.cache_clear()
    cred_status_pm = await dalton_router.get_team_credentials_status_endpoint(
        user_id=PM_USER_ID
    )
    cred_status_non_pm = await dalton_router.get_team_credentials_status_endpoint(
        user_id=NON_PM_USER_ID
    )
    assert cred_status_pm["can_invite"] is True
    assert cred_status_non_pm["can_invite"] is False


# ---------------------------------------------------------------------------
# 2. Missing SUPABASE_SECRET_KEY error on both create and redeem
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_supabase_secret_key_returns_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `SUPABASE_SECRET_KEY` is missing, both create and redeem return 503 `邀请注册未配置`."""
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    with pytest.raises(HTTPException) as exc_create:
        await dalton_router.create_invite_endpoint(user_id=PM_USER_ID)
    assert exc_create.value.status_code == 503
    assert "邀请注册未配置" in str(exc_create.value.detail)

    with pytest.raises(HTTPException) as exc_redeem:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="some-token",
                username="alice",
                password="validpassword123",
            ),
        )
    assert exc_redeem.value.status_code == 503
    assert "邀请注册未配置" in str(exc_redeem.value.detail)


# ---------------------------------------------------------------------------
# 3. Check & Redeem for valid / used / expired / revoked / not_found + hmac.compare_digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_and_redeem_states_and_constant_time_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check` returns ONLY `{valid, reason}`, verifies `hmac.compare_digest`, and `redeem` rejects non-pending invites."""
    now = datetime.now(timezone.utc)
    state_rows: dict[str, dict[str, Any] | None] = {
        "tok-valid": {
            "invite_id": INVITE_ID,
            "token_sha256": invites_mod.hash_invite_token("tok-valid"),
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(hours=1),
            "expires_at": now + timedelta(days=6),
            "redeeming_username": None,
            "redeeming_at": None,
            "used_at": None,
            "used_by_email": None,
            "supabase_user_id": None,
            "revoked_at": None,
        },
        "tok-used": {
            "invite_id": INVITE_ID,
            "token_sha256": invites_mod.hash_invite_token("tok-used"),
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(days=1),
            "expires_at": now + timedelta(days=6),
            "redeeming_username": "bob",
            "redeeming_at": now - timedelta(minutes=11),
            "used_at": now - timedelta(minutes=10),
            "used_by_email": "bob@team.dalton.local",
            "supabase_user_id": "sb-bob",
            "revoked_at": None,
        },
        "tok-expired": {
            "invite_id": INVITE_ID,
            "token_sha256": invites_mod.hash_invite_token("tok-expired"),
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(days=8),
            "expires_at": now - timedelta(minutes=5),
            "redeeming_username": None,
            "redeeming_at": None,
            "used_at": None,
            "used_by_email": None,
            "supabase_user_id": None,
            "revoked_at": None,
        },
        "tok-revoked": {
            "invite_id": INVITE_ID,
            "token_sha256": invites_mod.hash_invite_token("tok-revoked"),
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(days=1),
            "expires_at": now + timedelta(days=6),
            "redeeming_username": None,
            "redeeming_at": None,
            "used_at": None,
            "used_by_email": None,
            "supabase_user_id": None,
            "revoked_at": now - timedelta(minutes=30),
        },
        "tok-tampered-digest": {
            "invite_id": INVITE_ID,
            "token_sha256": "0" * 64,  # mismatches hash_invite_token("tok-tampered-digest")
            "created_by_user_id": PM_USER_ID,
            "created_at": now - timedelta(hours=1),
            "expires_at": now + timedelta(days=6),
            "redeeming_username": None,
            "redeeming_at": None,
            "used_at": None,
            "used_by_email": None,
            "supabase_user_id": None,
            "revoked_at": None,
        },
    }
    by_hash = {
        invites_mod.hash_invite_token(k): v for k, v in state_rows.items() if v is not None
    }

    monkeypatch.setattr(
        members_db,
        "get_invite_by_token_hash",
        create_autospec(
            members_db.get_invite_by_token_hash,
            side_effect=lambda h, *, conn=None: by_hash.get(h),
        ),
    )

    res_valid = await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-valid")
    )
    assert res_valid == {"valid": True, "reason": None}
    assert set(res_valid.keys()) == {"valid", "reason"}

    res_used = await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-used")
    )
    assert res_used == {"valid": False, "reason": "used"}
    assert set(res_used.keys()) == {"valid", "reason"}

    res_expired = await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-expired")
    )
    assert res_expired == {"valid": False, "reason": "expired"}

    res_revoked = await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-revoked")
    )
    assert res_revoked == {"valid": False, "reason": "revoked"}

    res_missing = await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-unknown")
    )
    assert res_missing == {"valid": False, "reason": "not_found"}

    res_tampered = await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-tampered-digest")
    )
    assert res_tampered == {"valid": False, "reason": "not_found"}

    get_db_mock, _ = _make_tx_db()
    monkeypatch.setattr(invites_mod, "get_db_connection", get_db_mock)
    monkeypatch.setattr(
        members_db,
        "lock_invite_by_token_hash",
        create_autospec(
            members_db.lock_invite_by_token_hash,
            side_effect=lambda h, *, conn: by_hash.get(h),
        ),
    )
    for bad_token in (
        "tok-used",
        "tok-expired",
        "tok-revoked",
        "tok-unknown",
        "tok-tampered-digest",
    ):
        with pytest.raises(HTTPException) as exc_bad:
            await dalton_router.redeem_invite_endpoint(
                body=InviteRedeemRequest(
                    token=bad_token,
                    username="alice",
                    password="validpassword123",
                ),
            )
        assert exc_bad.value.status_code == 400


# ---------------------------------------------------------------------------
# 4. Username rules, password length, username conflict, get_member_by_email
#    conflict, and DALTON_ALLOWED_EMAILS conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_username_rules_and_all_conflict_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validates username/password rules and rejects conflicts from username, `get_member_by_email`, or `DALTON_ALLOWED_EMAILS`."""
    for invalid_username in (
        "ab",  # < 3 chars
        "a" * 33,  # > 32 chars
        "Alice",  # uppercase
        "alice@team",  # contains @
        "alice bob",  # contains space
        "alice!1",  # invalid symbol
    ):
        with pytest.raises(HTTPException) as exc_u:
            await dalton_router.redeem_invite_endpoint(
                body=InviteRedeemRequest(
                    token="tok-valid",
                    username=invalid_username,
                    password="validpassword123",
                ),
            )
        assert exc_u.value.status_code == 400

    with pytest.raises(HTTPException) as exc_pw:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-valid",
                username="alice_01",
                password="short1234",
            ),
        )
    assert exc_pw.value.status_code == 400

    now = datetime.now(timezone.utc)
    invite_row = {
        "invite_id": INVITE_ID,
        "token_sha256": invites_mod.hash_invite_token("tok-valid"),
        "created_by_user_id": PM_USER_ID,
        "created_at": now - timedelta(hours=1),
        "expires_at": now + timedelta(days=6),
        "redeeming_username": None,
        "redeeming_at": None,
        "used_at": None,
        "used_by_email": None,
        "supabase_user_id": None,
        "revoked_at": None,
    }
    get_db_mock, _ = _make_tx_db()
    monkeypatch.setattr(invites_mod, "get_db_connection", get_db_mock)
    monkeypatch.setattr(
        members_db,
        "lock_invite_by_token_hash",
        create_autospec(members_db.lock_invite_by_token_hash, return_value=invite_row),
    )

    orig_by_username = members_db.get_member_by_username
    orig_by_email = members_db.get_member_by_email

    # 4a. Conflict in `get_member_by_username` -> 409
    monkeypatch.setattr(
        members_db,
        "get_member_by_username",
        create_autospec(
            orig_by_username,
            return_value={"email": "alice@team.dalton.local", "username": "alice"},
        ),
    )
    with pytest.raises(HTTPException) as exc_conflict_u:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-valid",
                username="alice",
                password="validpassword123",
            ),
        )
    assert exc_conflict_u.value.status_code == 409
    assert "用户名已被占用" in str(exc_conflict_u.value.detail)

    # 4b. `get_member_by_username` is None, but `get_member_by_email` returns existing row -> 409
    monkeypatch.setattr(
        members_db,
        "get_member_by_username",
        create_autospec(orig_by_username, return_value=None),
    )
    monkeypatch.setattr(
        members_db,
        "get_member_by_email",
        create_autospec(
            orig_by_email,
            return_value={"email": "alice@team.dalton.local", "username": None},
        ),
    )
    with pytest.raises(HTTPException) as exc_conflict_email:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-valid",
                username="alice",
                password="validpassword123",
            ),
        )
    assert exc_conflict_email.value.status_code == 409
    assert "用户名已被占用" in str(exc_conflict_email.value.detail)

    # 4c. `DALTON_ALLOWED_EMAILS` contains synthesized `{username}@{domain}` -> 409
    monkeypatch.setattr(
        members_db,
        "get_member_by_email",
        create_autospec(orig_by_email, return_value=None),
    )
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", "env_taken@team.dalton.local")
    with pytest.raises(HTTPException) as exc_conflict_env:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-valid",
                username="env_taken",
                password="validpassword123",
            ),
        )
    assert exc_conflict_env.value.status_code == 409
    assert "用户名已被占用" in str(exc_conflict_env.value.detail)


# ---------------------------------------------------------------------------
# 5. Two-phase redeem, Supabase pagination & password update on retry,
#    unclaimed existing-email rejection, timeout without leaking secret key,
#    and `mark_invite_used` returning None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_phase_redeem_retry_pagination_password_update_and_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Covers two-phase redeem, timeout (no secret logged), unclaimed existing email -> 409, bound retry with pagination & password update, and mark_invite_used -> None."""
    now = datetime.now(timezone.utc)
    invite_state: dict[str, Any] = {
        "invite_id": INVITE_ID,
        "token_sha256": invites_mod.hash_invite_token("tok-redeem"),
        "created_by_user_id": PM_USER_ID,
        "created_at": now - timedelta(hours=1),
        "expires_at": now + timedelta(days=6),
        "redeeming_username": None,
        "redeeming_at": None,
        "used_at": None,
        "used_by_email": None,
        "supabase_user_id": None,
        "revoked_at": None,
    }
    get_db_mock, tx_state = _make_tx_db()
    monkeypatch.setattr(invites_mod, "get_db_connection", get_db_mock)
    monkeypatch.setattr(
        members_db,
        "lock_invite_by_token_hash",
        create_autospec(
            members_db.lock_invite_by_token_hash,
            side_effect=lambda h, *, conn: dict(invite_state),
        ),
    )
    monkeypatch.setattr(
        members_db,
        "get_member_by_username",
        create_autospec(members_db.get_member_by_username, return_value=None),
    )
    monkeypatch.setattr(
        members_db,
        "get_member_by_email",
        create_autospec(members_db.get_member_by_email, return_value=None),
    )

    async def _fake_mark_redeeming(
        *,
        invite_id: str,
        redeeming_username: str,
        conn: Any,
    ) -> dict[str, Any] | None:
        invite_state["redeeming_username"] = redeeming_username
        invite_state["redeeming_at"] = now
        return dict(invite_state)

    async def _fake_clear_redeeming(
        *,
        invite_id: str,
        redeeming_username: str,
        conn: Any = None,
    ) -> None:
        if (
            invite_state.get("used_at") is None
            and invite_state.get("redeeming_username") == redeeming_username
        ):
            invite_state["redeeming_username"] = None
            invite_state["redeeming_at"] = None

    orig_clear_redeeming = members_db.clear_invite_redeeming
    monkeypatch.setattr(
        members_db,
        "mark_invite_redeeming",
        create_autospec(members_db.mark_invite_redeeming, side_effect=_fake_mark_redeeming),
    )
    monkeypatch.setattr(
        members_db,
        "clear_invite_redeeming",
        create_autospec(orig_clear_redeeming, side_effect=_fake_clear_redeeming),
    )

    inserted_members: list[dict[str, Any]] = []
    marked_invites: list[tuple[str, str, str | None]] = []

    async def _fake_insert_member(
        *,
        email: str,
        username: str | None,
        source: str,
        invited_by_user_id: str | None,
        conn: Any = None,
    ) -> dict[str, Any]:
        row = {
            "email": email,
            "username": username,
            "source": source,
            "invited_by_user_id": invited_by_user_id,
        }
        inserted_members.append(row)
        return row

    async def _fake_mark_used(
        *,
        invite_id: str,
        used_by_email: str,
        supabase_user_id: str | None = None,
        conn: Any,
    ) -> dict[str, Any] | None:
        marked_invites.append((invite_id, used_by_email, supabase_user_id))
        invite_state["used_at"] = now
        invite_state["used_by_email"] = used_by_email
        invite_state["supabase_user_id"] = supabase_user_id
        return dict(invite_state)

    orig_mark_used = members_db.mark_invite_used
    monkeypatch.setattr(
        members_db,
        "insert_member",
        create_autospec(members_db.insert_member, side_effect=_fake_insert_member),
    )
    monkeypatch.setattr(
        members_db,
        "mark_invite_used",
        create_autospec(orig_mark_used, side_effect=_fake_mark_used),
    )

    orig_async_client_cls = httpx.AsyncClient

    # --- Case 5a: First-time redeem where email already exists in Supabase (invite was NOT in redeeming state) -> 409 & clears redeeming_username ---
    mock_client_preexisting = create_autospec(orig_async_client_cls, instance=True)
    mock_client_preexisting.__aenter__.return_value = mock_client_preexisting
    mock_client_preexisting.__aexit__.return_value = False
    mock_client_preexisting.post.return_value = httpx.Response(
        422,
        json={"error_code": "email_exists", "msg": "User already registered"},
    )
    monkeypatch.setattr(
        invites_mod.httpx,
        "AsyncClient",
        create_autospec(orig_async_client_cls, return_value=mock_client_preexisting),
    )

    with pytest.raises(HTTPException) as exc_preexisting:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-redeem",
                username="taken_in_sb",
                password="validpassword123",
            ),
        )
    assert exc_preexisting.value.status_code == 409
    assert "用户名已被占用" in str(exc_preexisting.value.detail)
    assert invite_state["redeeming_username"] is None  # cleared so invite remains usable
    assert inserted_members == []

    # Verify POST body includes `app_metadata: {"dalton_invite_id": INVITE_ID}`
    _, post_kwargs = mock_client_preexisting.post.call_args
    assert post_kwargs["json"]["app_metadata"] == {"dalton_invite_id": INVITE_ID}

    # --- Case 5b: A & B concurrency — A committed `redeeming_username`, B enters as retry and encounters an existing user WITHOUT `dalton_invite_id` tag -> 409 & NO password update ---
    invite_state["redeeming_username"] = "concurrent_user"
    invite_state["redeeming_at"] = now
    mock_client_untagged = create_autospec(orig_async_client_cls, instance=True)
    mock_client_untagged.__aenter__.return_value = mock_client_untagged
    mock_client_untagged.__aexit__.return_value = False
    mock_client_untagged.post.return_value = httpx.Response(
        422,
        json={"error_code": "email_exists", "msg": "User already registered"},
    )
    mock_client_untagged.get.return_value = httpx.Response(
        200,
        json={
            "users": [
                {
                    "id": "sb-unrelated-user",
                    "email": "concurrent_user@team.dalton.local",
                    "app_metadata": {},
                }
            ]
        },
    )
    monkeypatch.setattr(
        invites_mod.httpx,
        "AsyncClient",
        create_autospec(orig_async_client_cls, return_value=mock_client_untagged),
    )
    with pytest.raises(HTTPException) as exc_untagged:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-redeem",
                username="concurrent_user",
                password="attacker_password_123",
            ),
        )
    assert exc_untagged.value.status_code == 409
    mock_client_untagged.put.assert_not_called()
    assert invite_state["redeeming_username"] is None

    # --- Case 5c: Existing user's `app_metadata.dalton_invite_id` belongs to ANOTHER invite -> 409 & NO password update ---
    invite_state["redeeming_username"] = "other_invite_user"
    invite_state["redeeming_at"] = now
    mock_client_other_inv = create_autospec(orig_async_client_cls, instance=True)
    mock_client_other_inv.__aenter__.return_value = mock_client_other_inv
    mock_client_other_inv.__aexit__.return_value = False
    mock_client_other_inv.post.return_value = httpx.Response(
        422,
        json={"error_code": "email_exists", "msg": "User already registered"},
    )
    mock_client_other_inv.get.return_value = httpx.Response(
        200,
        json={
            "users": [
                {
                    "id": "sb-other-invite-user",
                    "email": "other_invite_user@team.dalton.local",
                    "app_metadata": {
                        "dalton_invite_id": "99999999-9999-9999-9999-999999999999"
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        invites_mod.httpx,
        "AsyncClient",
        create_autospec(orig_async_client_cls, return_value=mock_client_other_inv),
    )
    with pytest.raises(HTTPException) as exc_other_inv:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-redeem",
                username="other_invite_user",
                password="attacker_password_123",
            ),
        )
    assert exc_other_inv.value.status_code == 409
    mock_client_other_inv.put.assert_not_called()
    assert invite_state["redeeming_username"] is None

    # --- Case 5d: Cleanup failure (`clear_invite_redeeming` raises) -> logs WARNING, and subsequent retry still refuses to modify unrelated account ---
    monkeypatch.setattr(
        invites_mod.httpx,
        "AsyncClient",
        create_autospec(orig_async_client_cls, return_value=mock_client_untagged),
    )
    monkeypatch.setattr(
        members_db,
        "clear_invite_redeeming",
        create_autospec(
            orig_clear_redeeming,
            side_effect=RuntimeError("transient db error during cleanup"),
        ),
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as exc_cleanup_fail:
            await dalton_router.redeem_invite_endpoint(
                body=InviteRedeemRequest(
                    token="tok-redeem",
                    username="concurrent_user",
                    password="attacker_password_123",
                ),
            )
    assert exc_cleanup_fail.value.status_code == 409
    assert "Failed to clear redeeming state" in caplog.text
    # Because cleanup failed, `redeeming_username` is still `"concurrent_user"`.
    assert invite_state["redeeming_username"] == "concurrent_user"

    # Restore working `clear_invite_redeeming` and retry `"concurrent_user"` (which is now in `is_retry=True` state) -> still 409 and NO password update!
    monkeypatch.setattr(
        members_db,
        "clear_invite_redeeming",
        create_autospec(orig_clear_redeeming, side_effect=_fake_clear_redeeming),
    )
    with pytest.raises(HTTPException) as exc_retry_after_cleanup_fail:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-redeem",
                username="concurrent_user",
                password="attacker_password_123",
            ),
        )
    assert exc_retry_after_cleanup_fail.value.status_code == 409
    mock_client_untagged.put.assert_not_called()
    assert invite_state["redeeming_username"] is None

    # --- Case 5e: httpx.TimeoutException -> returns 502, logs NEVER contain SUPABASE_SECRET_KEY, leaves invite in `redeeming` for same username ---
    mock_client_timeout = create_autospec(orig_async_client_cls, instance=True)
    mock_client_timeout.__aenter__.return_value = mock_client_timeout
    mock_client_timeout.__aexit__.return_value = False
    mock_client_timeout.post.side_effect = httpx.ReadTimeout(
        f"timeout talking to upstream with key {SECRET_KEY_VALUE}"
    )
    monkeypatch.setattr(
        invites_mod.httpx,
        "AsyncClient",
        create_autospec(orig_async_client_cls, return_value=mock_client_timeout),
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(HTTPException) as exc_timeout:
            await dalton_router.redeem_invite_endpoint(
                body=InviteRedeemRequest(
                    token="tok-redeem",
                    username="bob_retry",
                    password="firstpassword123",
                ),
            )
    assert exc_timeout.value.status_code == 502
    assert SECRET_KEY_VALUE not in caplog.text
    assert SECRET_KEY_VALUE not in str(exc_timeout.value.detail)
    assert invite_state["redeeming_username"] == "bob_retry"

    # --- Case 5f: Retry on the same invite with a DIFFERENT username while `redeeming_username == "bob_retry"` -> 409 ---
    with pytest.raises(HTTPException) as exc_diff_user:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-redeem",
                username="other_user",
                password="firstpassword123",
            ),
        )
    assert exc_diff_user.value.status_code == 409

    # --- Case 5g: Retry with matching `redeeming_username == "bob_retry"` AND matching `app_metadata.dalton_invite_id == INVITE_ID`, target after page 51 (on page 52) -> updates password and finalizes ---
    get_urls: list[str] = []
    put_calls: list[tuple[str, Any, Any]] = []

    async def _post_exists(url: str, *, json: Any = None, headers: Any = None, **kw: Any) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error_code": "email_exists", "msg": "User already registered"},
        )

    async def _get_paginated_past_51(url: str, *, headers: Any = None, **kw: Any) -> httpx.Response:
        get_urls.append(url)
        if "page=52&" not in url:
            # Pages 1..51 each return a full page of 100 unrelated users
            return httpx.Response(
                200,
                json={
                    "users": [
                        {"id": f"sb-other-{i}", "email": f"other{i}@team.dalton.local"}
                        for i in range(100)
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "users": [
                    {
                        "id": "sb-bob-page52",
                        "email": "bob_retry@team.dalton.local",
                        "app_metadata": {"dalton_invite_id": INVITE_ID},
                    }
                ]
            },
        )

    async def _put_password(url: str, *, json: Any = None, headers: Any = None, **kw: Any) -> httpx.Response:
        put_calls.append((url, json, headers))
        return httpx.Response(
            200,
            json={"id": "sb-bob-page52", "email": "bob_retry@team.dalton.local"},
        )

    mock_client_retry = create_autospec(orig_async_client_cls, instance=True)
    mock_client_retry.__aenter__.return_value = mock_client_retry
    mock_client_retry.__aexit__.return_value = False
    mock_client_retry.post.side_effect = _post_exists
    mock_client_retry.get.side_effect = _get_paginated_past_51
    mock_client_retry.put.side_effect = _put_password
    monkeypatch.setattr(
        invites_mod.httpx,
        "AsyncClient",
        create_autospec(orig_async_client_cls, return_value=mock_client_retry),
    )

    res_retry = await dalton_router.redeem_invite_endpoint(
        body=InviteRedeemRequest(
            token="tok-redeem",
            username="bob_retry",
            password="new_updated_password_456",
        ),
    )
    assert res_retry == {
        "ok": True,
        "username": "bob_retry",
        "email": "bob_retry@team.dalton.local",
        "supabase_user_id": "sb-bob-page52",
    }
    assert len(get_urls) == 52
    assert "page=1&per_page=100" in get_urls[0]
    assert "page=52&per_page=100" in get_urls[51]
    assert len(put_calls) == 1
    assert put_calls[0][0] == "https://example.supabase.co/auth/v1/admin/users/sb-bob-page52"
    assert put_calls[0][1] == {
        "password": "new_updated_password_456",
        "email_confirm": True,
    }
    assert marked_invites == [(INVITE_ID, "bob_retry@team.dalton.local", "sb-bob-page52")]

    # --- Case 5h: Phase 3 strictly rejects `locked_redeeming is None` (if redeeming_username was cleared concurrently) ---
    invite_state["used_at"] = None
    invite_state["used_by_email"] = None
    invite_state["redeeming_username"] = None

    async def _fake_mark_redeeming_cleared_before_phase3(
        *,
        invite_id: str,
        redeeming_username: str,
        conn: Any,
    ) -> dict[str, Any] | None:
        # Claim succeeds in Phase 1, but `invite_state["redeeming_username"]` is None when Phase 3 re-locks
        invite_state["redeeming_username"] = None
        return {**invite_state, "redeeming_username": redeeming_username}

    members_db.mark_invite_redeeming.side_effect = _fake_mark_redeeming_cleared_before_phase3
    mock_client_create_ok = create_autospec(orig_async_client_cls, instance=True)
    mock_client_create_ok.__aenter__.return_value = mock_client_create_ok
    mock_client_create_ok.__aexit__.return_value = False
    mock_client_create_ok.post.return_value = httpx.Response(
        201,
        json={"id": "sb-new-99", "email": "charlie@team.dalton.local"},
    )
    monkeypatch.setattr(
        invites_mod.httpx,
        "AsyncClient",
        create_autospec(orig_async_client_cls, return_value=mock_client_create_ok),
    )
    with pytest.raises(HTTPException) as exc_phase3_none:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-redeem",
                username="charlie",
                password="validpassword123",
            ),
        )
    assert exc_phase3_none.value.status_code == 409

    # --- Case 5i: `mark_invite_used` returning None raises HTTP 400 ---
    members_db.mark_invite_redeeming.side_effect = _fake_mark_redeeming
    monkeypatch.setattr(
        members_db,
        "mark_invite_used",
        create_autospec(orig_mark_used, return_value=None),
    )
    with pytest.raises(HTTPException) as exc_mark_none:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-redeem",
                username="charlie",
                password="validpassword123",
            ),
        )
    assert exc_mark_none.value.status_code == 400

    # --- Case 5j: Pagination safety cap logs ERROR and raises HTTP 502 when `MAX_ADMIN_USER_PAGES` is reached ---
    monkeypatch.setattr(invites_mod, "MAX_ADMIN_USER_PAGES", 3)
    mock_client_cap = create_autospec(orig_async_client_cls, instance=True)
    mock_client_cap.get.return_value = httpx.Response(
        200,
        json={
            "users": [
                {"id": f"sb-other-{i}", "email": f"other{i}@team.dalton.local"}
                for i in range(100)
            ]
        },
    )
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc_cap:
            await invites_mod._find_existing_supabase_user_by_email(
                client=mock_client_cap,
                admin_users_url="https://example.supabase.co/auth/v1/admin/users",
                headers={"apikey": SECRET_KEY_VALUE},
                email="never_found@team.dalton.local",
            )
    assert exc_cap.value.status_code == 502
    assert "exceeded safety limit" in caplog.text

    # --- Case 5k: `members_db.clear_invite_redeeming` SQL executes conditional update with `redeeming_username = %s` ---
    executed_sql: list[tuple[str, Any]] = []
    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _capture_exec(query: Any, params: Any = None, **kw: Any) -> None:
        executed_sql.append((str(query), params))

    mock_cursor.execute.side_effect = _capture_exec

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield mock_cursor

    mock_conn.cursor.side_effect = _cursor_cm
    await orig_clear_redeeming(
        invite_id=INVITE_ID,
        redeeming_username="charlie",
        conn=mock_conn,
    )
    assert len(executed_sql) == 1
    assert "redeeming_username = %s" in executed_sql[0][0]
    assert executed_sql[0][1] == (INVITE_ID, "charlie")


# ---------------------------------------------------------------------------
# 6. Positive-only member cache across workers (Worker A redeem -> Worker B
#    immediately sees new member) & fail-closed when member query fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_only_cache_across_workers_and_db_failure_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker B (independent cache) never caches negative results and immediately sees a member redeemed on Worker A; DB errors reject."""
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", "env.member@dalton.test")

    db_emails_state: set[str] = {"existing.member@team.dalton.local"}
    db_query_count = 0
    orig_list_member_emails = members_db.list_member_emails

    async def _fake_list_member_emails(*, conn: Any = None) -> set[str]:
        nonlocal db_query_count
        db_query_count += 1
        return set(db_emails_state)

    monkeypatch.setattr(
        members_db,
        "list_member_emails",
        create_autospec(orig_list_member_emails, side_effect=_fake_list_member_emails),
    )

    worker_a_cache: dict[str, float] = {}
    worker_b_cache: dict[str, float] = {}

    # Worker B checks `"new.invitee@team.dalton.local"` BEFORE Worker A redeems -> False (not cached!)
    assert (
        await invites_mod.is_team_member(
            "new.invitee@team.dalton.local", cache=worker_b_cache
        )
        is False
    )
    assert "new.invitee@team.dalton.local" not in worker_b_cache
    assert "existing.member@team.dalton.local" in worker_b_cache
    assert db_query_count == 1

    # Positive hit on Worker B is served from `worker_b_cache` without hitting DB
    assert (
        await invites_mod.is_team_member(
            "existing.member@team.dalton.local", cache=worker_b_cache
        )
        is True
    )
    assert db_query_count == 1

    # Worker A completes invite redemption for `"new.invitee@team.dalton.local"`
    db_emails_state.add("new.invitee@team.dalton.local")
    invites_mod.record_team_member_cached(
        "new.invitee@team.dalton.local", cache=worker_a_cache
    )

    # Immediately (0s elapsed), Worker B checks `"new.invitee@team.dalton.local"` -> cache miss queries DB -> True!
    assert (
        await invites_mod.is_team_member(
            "new.invitee@team.dalton.local", cache=worker_b_cache
        )
        is True
    )
    assert "new.invitee@team.dalton.local" in worker_b_cache
    assert db_query_count == 2

    # Subsequent check on Worker B for `"new.invitee@team.dalton.local"` uses positive cache
    assert (
        await invites_mod.is_team_member(
            "new.invitee@team.dalton.local", cache=worker_b_cache
        )
        is True
    )
    assert db_query_count == 2

    # Member query failure -> fail-closed (returns False / rejects with 403)
    invites_mod.invalidate_team_members_cache()
    monkeypatch.setattr(
        members_db,
        "list_member_emails",
        create_autospec(
            orig_list_member_emails,
            side_effect=RuntimeError("database connection error"),
        ),
    )
    assert await invites_mod.is_team_member("unknown@team.dalton.local") is False

    users_by_id = {
        "u-db-err": {
            "user_id": "u-db-err",
            "email": "unknown@team.dalton.local",
            "name": "Unknown",
        },
    }
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, side_effect=lambda uid: users_by_id.get(uid)),
    )
    with pytest.raises(HTTPException) as exc_fail_closed:
        await dalton_sharing.require_team_reader("u-db-err")
    assert exc_fail_closed.value.status_code == 403


# ---------------------------------------------------------------------------
# 7. Trusted proxies (`DALTON_TRUSTED_PROXIES`), `check` & `redeem` rate limit,
#    and Redis failure fail-closed (503)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trusted_proxies_ip_extraction_redeem_rate_limit_and_redis_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `DALTON_TRUSTED_PROXIES` can supply `X-Forwarded-For` (right-to-left); both check & redeem rate-limit; Redis failure -> 503."""
    monkeypatch.setenv("DALTON_INVITE_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("DALTON_TRUSTED_PROXIES", "127.0.0.1,::1,172.18.0.0/16")
    monkeypatch.setattr(
        members_db,
        "get_invite_by_token_hash",
        create_autospec(members_db.get_invite_by_token_hash, return_value=None),
    )

    seen_keys: list[str] = []
    counts: dict[str, int] = {}
    fake_redis = create_autospec(aioredis.Redis, instance=True)

    async def _incr(name: str, amount: int = 1) -> int:
        seen_keys.append(name)
        counts[name] = counts.get(name, 0) + amount
        return counts[name]

    async def _expire(name: str, time: Any, **kw: Any) -> bool:
        return True

    fake_redis.incr.side_effect = _incr
    fake_redis.expire.side_effect = _expire
    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.enabled = True
    fake_cache.client = fake_redis
    invites_mod.get_cache_client.return_value = fake_cache

    # 7a. Untrusted direct peer (198.51.100.77) spoofing X-Forwarded-For -> ignores XFF and uses 198.51.100.77
    untrusted_req = create_autospec(Request, instance=True)
    untrusted_req.client = Address("198.51.100.77", 54321)
    untrusted_req.headers = Headers({"x-forwarded-for": "1.1.1.1, 2.2.2.2"})
    await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-1"), request=untrusted_req
    )
    assert ":198.51.100.77:" in seen_keys[-1]
    assert "1.1.1.1" not in seen_keys[-1]
    assert "2.2.2.2" not in seen_keys[-1]

    # 7a-v6: IPv4-mapped IPv6 peer (`::ffff:172.18.0.5`) matches trusted CIDR `172.18.0.0/16` via `ipv4_mapped`
    v4_mapped_req = create_autospec(Request, instance=True)
    v4_mapped_req.client = Address("::ffff:172.18.0.5", 443)
    v4_mapped_req.headers = Headers(
        {"x-forwarded-for": "198.51.100.42, ::ffff:127.0.0.1"}
    )
    await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-1"), request=v4_mapped_req
    )
    assert ":198.51.100.42:" in seen_keys[-1]

    # 7b. Trusted proxy peer (172.18.0.5 in 172.18.0.0/16) -> walks XFF right-to-left and picks first non-trusted IP (203.0.113.9)
    trusted_req = create_autospec(Request, instance=True)
    trusted_req.client = Address("172.18.0.5", 443)
    trusted_req.headers = Headers(
        {"x-forwarded-for": "10.0.0.1, 203.0.113.9, 172.18.0.2"}
    )
    await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-1"), request=trusted_req
    )
    assert ":203.0.113.9:" in seen_keys[-1]
    await dalton_router.check_invite_endpoint(
        InviteCheckRequest(token="tok-1"), request=trusted_req
    )
    with pytest.raises(HTTPException) as exc_check_rl:
        await dalton_router.check_invite_endpoint(
            InviteCheckRequest(token="tok-1"), request=trusted_req
        )
    assert exc_check_rl.value.status_code == 429

    # 7c. `redeem` endpoint also enforces per-IP rate limit -> 429 on 3rd call
    for _ in range(2):
        with pytest.raises(HTTPException) as exc_redeem_bad:
            await dalton_router.redeem_invite_endpoint(
                body=InviteRedeemRequest(
                    token="", username="alice", password="validpassword123"
                ),
                request=trusted_req,
            )
        assert exc_redeem_bad.value.status_code == 400
    with pytest.raises(HTTPException) as exc_redeem_rl:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="", username="alice", password="validpassword123"
            ),
            request=trusted_req,
        )
    assert exc_redeem_rl.value.status_code == 429

    # 7d. Redis disabled or raising error -> fail-closed HTTP 503 on both check and redeem (no in-memory fallback)
    fake_cache.enabled = False
    with pytest.raises(HTTPException) as exc_redis_off_check:
        await dalton_router.check_invite_endpoint(
            InviteCheckRequest(token="tok-1"), request=trusted_req
        )
    assert exc_redis_off_check.value.status_code == 503

    fake_cache.enabled = True
    fake_redis.incr.side_effect = aioredis.ConnectionError("redis down")
    with pytest.raises(HTTPException) as exc_redis_err_redeem:
        await dalton_router.redeem_invite_endpoint(
            body=InviteRedeemRequest(
                token="tok-1", username="alice", password="validpassword123"
            ),
            request=trusted_req,
        )
    assert exc_redis_err_redeem.value.status_code == 503


# ---------------------------------------------------------------------------
# 8. Auth & Sharing integration with `is_team_member`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_dalton_token_and_list_team_workspaces_with_db_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify_dalton_token` and `list_team_workspaces` accept invited DB members even when `DALTON_ALLOWED_EMAILS` is empty."""
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", "")

    monkeypatch.setattr(
        members_db,
        "list_member_emails",
        create_autospec(
            members_db.list_member_emails,
            return_value={"invited.alice@team.dalton.local", "invited.bob@team.dalton.local"},
        ),
    )
    monkeypatch.setattr(
        dalton_auth,
        "_ensure_dalton_auth_ready",
        create_autospec(dalton_auth._ensure_dalton_auth_ready, return_value=None),
    )
    monkeypatch.setattr(
        jwt_bearer,
        "_decode_token",
        create_autospec(
            jwt_bearer._decode_token,
            return_value=AuthInfo(user_id="u-alice"),
        ),
    )
    monkeypatch.setattr(
        dalton_auth,
        "_extract_token_claims",
        create_autospec(
            dalton_auth._extract_token_claims,
            return_value={
                "sub": "u-alice",
                "email": "invited.alice@team.dalton.local",
                "is_anonymous": False,
            },
        ),
    )
    auth_info = await dalton_auth.verify_dalton_token("fake-jwt-token")
    assert auth_info.user_id == "u-alice"
    assert auth_info.email == "invited.alice@team.dalton.local"

    users_by_id = {
        "u-alice": {
            "user_id": "u-alice",
            "email": "invited.alice@team.dalton.local",
            "name": "Alice",
        },
    }
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, side_effect=lambda uid: users_by_id.get(uid)),
    )

    fake_ws_rows = [
        {
            "workspace_id": "ws-bob-1",
            "user_id": "u-bob",
            "name": "Bob Workspace",
            "description": None,
            "status": "running",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "last_activity_at": datetime.now(timezone.utc),
            "owner_email": "invited.bob@team.dalton.local",
            "owner_name": "Bob",
            "owner_avatar_url": None,
        }
    ]
    mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    mock_cur.fetchall.return_value = fake_ws_rows
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False

    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__aenter__.return_value = mock_conn
    mock_conn.__aexit__.return_value = False

    monkeypatch.setattr(
        dalton_sharing,
        "get_db_connection",
        create_autospec(dalton_sharing.get_db_connection, return_value=mock_conn),
    )

    team_list = await dalton_sharing.list_team_workspaces("u-alice")
    assert len(team_list) == 1
    assert team_list[0]["user_id"] == "u-bob"
    assert team_list[0]["workspaces"][0]["workspace_id"] == "ws-bob-1"
