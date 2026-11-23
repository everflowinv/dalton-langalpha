"""Unit tests for last-admin protection, local access tiers, transactional team credentials, and CLI.

Covers:
- Item 5: Local credentials have zero in-process caching, fail closed with 503 on DB error,
  and pin holder + operator admin role inside the write transaction (``api_keys.upsert_api_key``).
- Item 7: CLI opens and closes DB pool in its own event loop, verified in standalone
  subprocesses (``python -m src.dalton.auth.cli ...``) for all three commands.

All test doubles use ``unittest.mock.create_autospec``.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException
from psycopg import AsyncConnection, AsyncCursor

from src.config import settings as app_settings
from src.dalton import access_tiers, credentials
from src.dalton import settings as settings_mod
from src.dalton.auth import bootstrap as bootstrap_mod
from src.dalton.auth import cli as auth_cli
from src.dalton.auth import roles as roles_mod
from src.dalton.auth import sessions as sessions_mod
from src.dalton.auth.passwords import hash_password, verify_password
from src.dalton.auth.roles import LastAdminProtectedError
from src.server.app import oauth as oauth_app
from src.server.database import api_keys as api_keys_db
from src.server.database import oauth_tokens as oauth_tokens_db
from src.server.database.pool import get_db_connection as _REAL_GET_DB_CONNECTION

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_1 = "11111111-1111-4111-8111-111111111111"
ADMIN_2 = "22222222-2222-4222-8222-222222222222"
MEMBER_1 = "33333333-3333-4333-8333-333333333333"
HOLDER_LOCAL = "44444444-4444-4444-8444-444444444444"
HOLDER_SUPABASE = "55555555-5555-4555-8555-555555555555"


class FakeRolesAndCliDB:
    """In-memory fake DB supporting roles, credentials, sessions, and bootstrap tables."""

    def __init__(self) -> None:
        self.roles: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.sessions: list[dict[str, Any]] = []
        self.bootstrap_state: dict[str, Any] | None = None
        self.bootstrap_token: dict[str, Any] | None = None
        self.auth_attempts: set[tuple[str, str]] = set()
        self.holder_user_id: str | None = None
        self.api_keys_written: list[tuple[str, str]] = []
        self.api_keys_deleted: list[tuple[str, str]] = []
        self.oauth_written: list[tuple[str, str]] = []
        self.oauth_deleted: list[tuple[str, str]] = []
        self.tx_depth: int = 0
        self.write_tx_depths: list[tuple[str, int]] = []
        self.cli_sql_log: list[str] = []

    def make_get_db_connection(self) -> Any:
        db = self

        @asynccontextmanager
        async def _get_db_connection(existing_conn: Any = None):
            @asynccontextmanager
            async def _cursor_cm(*args: Any, **kwargs: Any):
                cur = create_autospec(AsyncCursor, instance=True)
                cur._rows = []

                async def _exec(query: Any, params: Any = None, **_: Any) -> Any:
                    sql = " ".join(str(query).split())
                    p = tuple(params) if params is not None else ()
                    cur._rows = []

                    if "pg_advisory_xact_lock" in sql:
                        db.cli_sql_log.append("advisory_lock")
                        return cur

                    if "SELECT holder_user_id FROM dalton_credential_holder" in sql:
                        db.write_tx_depths.append(("holder_lock", db.tx_depth))
                        if db.holder_user_id is not None:
                            cur._rows = [{"holder_user_id": db.holder_user_id}]
                        return cur
                    if "SELECT base_url FROM user_api_keys" in sql:
                        db.write_tx_depths.append(("api_key_base_url_lock", db.tx_depth))
                        return cur
                    if "INSERT INTO user_api_keys" in sql:
                        db.write_tx_depths.append(("api_key_upsert", db.tx_depth))
                        db.api_keys_written.append((p[0], p[1]))
                        return cur
                    if "UPDATE user_api_keys SET base_url" in sql:
                        db.write_tx_depths.append(("api_key_base_url_update", db.tx_depth))
                        return cur
                    if "DELETE FROM user_api_keys" in sql:
                        db.write_tx_depths.append(("api_key_delete", db.tx_depth))
                        db.api_keys_deleted.append((p[0], p[1]))
                        return cur
                    if "INSERT INTO user_oauth_tokens" in sql:
                        db.write_tx_depths.append(("oauth_upsert", db.tx_depth))
                        db.oauth_written.append((p[0], p[1]))
                        return cur
                    if "DELETE FROM user_oauth_tokens" in sql:
                        db.write_tx_depths.append(("oauth_delete", db.tx_depth))
                        db.oauth_deleted.append((p[0], p[1]))
                        return cur

                    if "SELECT user_id FROM dalton_user_roles WHERE tier = 'admin'" in sql:
                        cur._rows = [
                            {"user_id": uid}
                            for uid, row in db.roles.items()
                            if row["tier"] == "admin"
                        ]
                        return cur
                    if "SELECT tier FROM dalton_user_roles WHERE user_id = %s" in sql:
                        if "FOR SHARE" in sql:
                            db.write_tx_depths.append(("role_lock", db.tx_depth))
                        row = db.roles.get(p[0])
                        if row is not None:
                            cur._rows = [{"tier": row["tier"]}]
                        return cur
                    if "INSERT INTO dalton_user_roles" in sql:
                        db.roles[p[0]] = {
                            "user_id": p[0],
                            "tier": p[1],
                            "granted_by": p[2],
                        }
                        cur._rows = [dict(db.roles[p[0]])]
                        return cur
                    if "DELETE FROM dalton_user_roles WHERE user_id = %s" in sql:
                        removed = db.roles.pop(p[0], None)
                        if removed is not None:
                            cur._rows = [{"user_id": p[0]}]
                        return cur

                    if "FROM dalton_local_credentials WHERE email = %s" in sql:
                        for row in db.credentials.values():
                            if row["email"] == p[0]:
                                cur._rows = [dict(row)]
                                break
                        return cur
                    if "FROM dalton_local_credentials WHERE user_id = %s" in sql:
                        if "FOR UPDATE" in sql:
                            db.cli_sql_log.append("cred_lock")
                        row = db.credentials.get(p[0])
                        if row is not None:
                            cur._rows = [dict(row)]
                        return cur
                    if "FROM users WHERE LOWER(email) = %s" in sql:
                        for row in db.users.values():
                            if row["email"].lower() == p[0]:
                                cur._rows = [dict(row)]
                                break
                        return cur
                    if "UPDATE dalton_local_credentials SET password_hash = %s" in sql:
                        row = db.credentials[p[3]]
                        row["password_hash"] = p[0]
                        row["password_changed_at"] = p[1]
                        row["failed_attempts"] = 0
                        row["locked_until"] = None
                        return cur
                    if "DELETE FROM dalton_auth_attempts" in sql:
                        db.cli_sql_log.append("attempts_delete")
                        db.auth_attempts.discard(("account", p[0]))
                        return cur
                    if "UPDATE dalton_local_sessions SET revoked_at = %s" in sql:
                        revoked = []
                        for s in db.sessions:
                            if s["user_id"] == p[2] and s["revoked_at"] is None:
                                s["revoked_at"] = p[0]
                                s["revoked_reason"] = p[1]
                                revoked.append({"session_id": s["session_id"]})
                        cur._rows = revoked
                        return cur

                    if "FROM dalton_bootstrap_state" in sql:
                        if db.bootstrap_state is not None:
                            cur._rows = [dict(db.bootstrap_state)]
                        return cur
                    if "INSERT INTO dalton_bootstrap_tokens" in sql:
                        from datetime import datetime, timedelta, timezone

                        now = datetime.now(timezone.utc)
                        exp = now + timedelta(hours=int(p[1]))
                        db.bootstrap_token = {
                            "singleton_id": 1,
                            "token_sha256": p[0],
                            "source": "cli",
                            "issued_at": now,
                            "expires_at": exp,
                            "failed_attempts": 0,
                            "locked_at": None,
                        }
                        cur._rows = [{"expires_at": exp}]
                        return cur

                    raise AssertionError(f"Unhandled SQL: {sql}")

                async def _fetchone() -> Any:
                    return cur._rows[0] if cur._rows else None

                async def _fetchall() -> list[Any]:
                    return list(cur._rows)

                cur.execute.side_effect = _exec
                cur.fetchone.side_effect = _fetchone
                cur.fetchall.side_effect = _fetchall
                yield cur

            @asynccontextmanager
            async def _tx_cm(*args: Any, **kwargs: Any):
                db.tx_depth += 1
                try:
                    yield None
                finally:
                    db.tx_depth -= 1

            conn = create_autospec(AsyncConnection, instance=True)
            conn.cursor.side_effect = _cursor_cm
            conn.transaction.side_effect = _tx_cm
            yield conn

        return create_autospec(_REAL_GET_DB_CONNECTION, side_effect=_get_db_connection)


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeRolesAndCliDB:
    settings_mod.get_dalton_config.cache_clear()
    db = FakeRolesAndCliDB()
    conn_factory = db.make_get_db_connection()
    monkeypatch.setattr(roles_mod, "get_db_connection", conn_factory)
    monkeypatch.setattr(sessions_mod, "get_db_connection", conn_factory)
    monkeypatch.setattr(bootstrap_mod, "get_db_connection", conn_factory)
    monkeypatch.setattr(auth_cli, "get_db_connection", conn_factory)
    monkeypatch.setattr(api_keys_db, "get_db_connection", conn_factory)
    monkeypatch.setattr(oauth_tokens_db, "get_db_connection", conn_factory)
    monkeypatch.setattr(
        oauth_tokens_db,
        "invalidate_oauth_active_cache",
        create_autospec(oauth_tokens_db.invalidate_oauth_active_cache, return_value=None),
    )
    yield db
    settings_mod.get_dalton_config.cache_clear()


class TestLastAdminProtection:
    @pytest.mark.asyncio
    async def test_sole_admin_cannot_be_demoted_or_deleted(
        self, fake_db: FakeRolesAndCliDB
    ) -> None:
        await roles_mod.set_user_role(ADMIN_1, "admin", granted_by="bootstrap")

        with pytest.raises(LastAdminProtectedError) as exc_demote:
            await roles_mod.set_user_role(ADMIN_1, "internal", granted_by=ADMIN_1)
        assert exc_demote.value.status_code == 409

        with pytest.raises(LastAdminProtectedError) as exc_delete:
            await roles_mod.delete_user_role(ADMIN_1)
        assert exc_delete.value.status_code == 409

        row = await roles_mod.set_user_role(ADMIN_1, "admin", granted_by="cli")
        assert row["tier"] == "admin"

        await roles_mod.set_user_role(ADMIN_2, "admin", granted_by=ADMIN_1)
        await roles_mod.set_user_role(ADMIN_1, "internal", granted_by=ADMIN_2)
        assert fake_db.roles[ADMIN_1]["tier"] == "internal"

        with pytest.raises(LastAdminProtectedError):
            await roles_mod.delete_user_role(ADMIN_2)


class TestLocalAccessTiersAndTransactionalCredentials:
    @pytest.mark.asyncio
    async def test_get_user_tier_reads_dalton_user_roles_in_local_mode(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeRolesAndCliDB
    ) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        fake_db.roles[ADMIN_1] = {"user_id": ADMIN_1, "tier": "admin", "granted_by": "bootstrap"}
        fake_db.roles[MEMBER_1] = {"user_id": MEMBER_1, "tier": "internal", "granted_by": ADMIN_1}

        assert await access_tiers.get_user_tier(ADMIN_1) == "admin"
        assert await access_tiers.get_user_tier(MEMBER_1) == "internal"
        assert await access_tiers.get_user_tier("unknown-user") == "external"
        assert await access_tiers.classify_owner_tier("unknown-user") == "unknown"

        failing_role = create_autospec(
            roles_mod.get_user_role, side_effect=RuntimeError("db down")
        )
        monkeypatch.setattr(roles_mod, "get_user_role", failing_role)
        assert await access_tiers.get_user_tier(ADMIN_1) == "external"
        assert await access_tiers.classify_owner_tier(ADMIN_1) == "unknown"

    def test_credentials_no_cache_and_503_on_db_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_PM_USER_IDS", HOLDER_SUPABASE)

        # Supabase mode keeps DALTON_PM_USER_IDS[0]
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        assert credentials.get_team_credential_holder_id() == HOLDER_SUPABASE
        assert credentials.credential_user_id(MEMBER_1) == HOLDER_SUPABASE
        assert credentials.is_credential_admin(HOLDER_SUPABASE) is True
        assert credentials.is_credential_admin(MEMBER_1) is False

        # Local mode reads live DB on EVERY call (no 15s cache!) and raises 503 on DB errors
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        admin_set = {HOLDER_LOCAL, ADMIN_1}
        mock_holder = create_autospec(
            credentials._fetch_local_holder_id_sync, return_value=HOLDER_LOCAL
        )
        mock_is_admin = create_autospec(
            credentials._fetch_local_is_admin_sync,
            side_effect=lambda uid: uid in admin_set,
        )
        monkeypatch.setattr(credentials, "_fetch_local_holder_id_sync", mock_holder)
        monkeypatch.setattr(credentials, "_fetch_local_is_admin_sync", mock_is_admin)

        assert credentials.get_team_credential_holder_id() == HOLDER_LOCAL
        assert credentials.is_credential_admin(ADMIN_1) is True

        # Immediate demotion takes effect on the VERY NEXT call (no stale cache window!)
        admin_set.remove(ADMIN_1)
        assert credentials.is_credential_admin(ADMIN_1) is False
        with pytest.raises(HTTPException) as exc_403:
            credentials.require_credential_write_admin(ADMIN_1)
        assert exc_403.value.status_code == 403

        # DB failure reading holder raises 503 (NEVER silently treats sharing as disabled!)
        mock_holder.side_effect = HTTPException(status_code=503, detail="db down")
        with pytest.raises(HTTPException) as exc_503:
            credentials.require_credential_write_admin(MEMBER_1)
        assert exc_503.value.status_code == 503

    @pytest.mark.asyncio
    async def test_transactional_api_key_write_pins_holder_and_checks_admin(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeRolesAndCliDB
    ) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "unit-test-byok-key-1234567890")
        fake_db.holder_user_id = HOLDER_LOCAL
        fake_db.roles[ADMIN_1] = {"user_id": ADMIN_1, "tier": "admin", "granted_by": "bootstrap"}
        fake_db.roles[MEMBER_1] = {"user_id": MEMBER_1, "tier": "internal", "granted_by": ADMIN_1}

        # Admin write & delete land on pinned HOLDER_LOCAL inside an active DB transaction (depth >= 1)
        await api_keys_db.upsert_api_key(ADMIN_1, "openai", "sk-test")
        assert fake_db.api_keys_written == [(HOLDER_LOCAL, "openai")]
        assert ("holder_lock", 1) in fake_db.write_tx_depths
        assert ("role_lock", 1) in fake_db.write_tx_depths
        assert ("api_key_upsert", 1) in fake_db.write_tx_depths

        fake_db.write_tx_depths.clear()
        await api_keys_db.delete_api_key(ADMIN_1, "openai")
        assert fake_db.api_keys_deleted == [(HOLDER_LOCAL, "openai")]
        assert fake_db.write_tx_depths == [
            ("holder_lock", 1),
            ("role_lock", 1),
            ("api_key_delete", 1),
        ]

        # Non-admin upsert & delete are refused 403 inside the write transaction (nothing mutated!)
        with pytest.raises(HTTPException) as exc_403:
            await api_keys_db.upsert_api_key(MEMBER_1, "openai", "sk-sneaky")
        assert exc_403.value.status_code == 403
        with pytest.raises(HTTPException) as exc_del_403:
            await api_keys_db.delete_api_key(MEMBER_1, "openai")
        assert exc_del_403.value.status_code == 403
        assert fake_db.api_keys_written == [(HOLDER_LOCAL, "openai")]
        assert fake_db.api_keys_deleted == [(HOLDER_LOCAL, "openai")]

        # Round 3 Item 7: OAuth upsert & delete ALSO pin holder and admin role inside transaction
        fake_db.write_tx_depths.clear()
        await oauth_tokens_db.upsert_oauth_tokens(
            ADMIN_1, "codex", "acc-tok", "ref-tok", "acct-1"
        )
        assert fake_db.oauth_written == [(HOLDER_LOCAL, "codex")]
        assert fake_db.write_tx_depths == [
            ("holder_lock", 1),
            ("role_lock", 1),
            ("oauth_upsert", 1),
        ]

        fake_db.write_tx_depths.clear()
        await oauth_tokens_db.delete_oauth_tokens(ADMIN_1, "codex")
        assert fake_db.oauth_deleted == [(HOLDER_LOCAL, "codex")]
        assert fake_db.write_tx_depths == [
            ("holder_lock", 1),
            ("role_lock", 1),
            ("oauth_delete", 1),
        ]

        with pytest.raises(HTTPException) as exc_oauth_403:
            await oauth_tokens_db.upsert_oauth_tokens(
                MEMBER_1, "codex", "acc-bad", "ref-bad", "acct-bad"
            )
        assert exc_oauth_403.value.status_code == 403
        with pytest.raises(HTTPException) as exc_oauth_del_403:
            await oauth_tokens_db.delete_oauth_tokens(MEMBER_1, "codex")
        assert exc_oauth_del_403.value.status_code == 403
        assert fake_db.oauth_written == [(HOLDER_LOCAL, "codex")]
        assert fake_db.oauth_deleted == [(HOLDER_LOCAL, "codex")]

        # DB failure inside transaction -> 503 (fail-closed)
        @asynccontextmanager
        async def _broken_conn(existing_conn: Any = None):
            @asynccontextmanager
            async def _tx(*a: Any, **kw: Any):
                yield None

            conn = create_autospec(AsyncConnection, instance=True)
            conn.transaction.side_effect = _tx
            conn.cursor.side_effect = RuntimeError("connection reset")
            yield conn

        monkeypatch.setattr(
            api_keys_db,
            "get_db_connection",
            create_autospec(_REAL_GET_DB_CONNECTION, side_effect=_broken_conn),
        )
        monkeypatch.setattr(
            oauth_tokens_db,
            "get_db_connection",
            create_autospec(_REAL_GET_DB_CONNECTION, side_effect=_broken_conn),
        )
        with pytest.raises(HTTPException) as exc_503:
            await api_keys_db.upsert_api_key(MEMBER_1, "openai", "sk-outage")
        assert exc_503.value.status_code == 503
        with pytest.raises(HTTPException) as exc_oauth_503:
            await oauth_tokens_db.delete_oauth_tokens(MEMBER_1, "codex")
        assert exc_oauth_503.value.status_code == 503

    @pytest.mark.asyncio
    async def test_oauth_save_preserves_403_when_demoted_before_write_transaction(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeRolesAndCliDB
    ) -> None:
        """Round 4 Item 4: transactional 403 inside oauth._save propagates as 403, NOT 500 save_failed."""
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "unit-test-byok-key-1234567890")
        fake_db.holder_user_id = HOLDER_LOCAL
        # Actor is demoted to non-admin by the time the DB write transaction runs
        fake_db.roles[MEMBER_1] = {"user_id": MEMBER_1, "tier": "internal", "granted_by": ADMIN_1}

        # Pre-check passes (simulating actor was admin when poll started, then demoted before commit)
        monkeypatch.setattr(
            credentials,
            "require_credential_write_admin",
            create_autospec(credentials.require_credential_write_admin, return_value=None),
        )
        monkeypatch.setattr(
            credentials,
            "credential_user_id",
            create_autospec(credentials.credential_user_id, return_value=HOLDER_LOCAL),
        )
        monkeypatch.setattr(
            oauth_tokens_db,
            "credential_user_id",
            create_autospec(oauth_tokens_db.credential_user_id, return_value=HOLDER_LOCAL),
        )

        class _ClaimCache:
            async def claim(self, key: str, raw: Any) -> bool:
                return True

        fake_cache = create_autospec(_ClaimCache, instance=True)
        fake_cache.claim.return_value = True

        from src.server.services.oauth_providers import TokenSet

        tokens = TokenSet(
            access_token="acc-tok",
            refresh_token="ref-tok",
            account_id="acct-1",
            email="u@example.com",
            plan_type="pro",
            expires_at=None,
        )
        with pytest.raises(HTTPException) as exc_save:
            await oauth_app._save(fake_cache, "pending-key", "raw-val", MEMBER_1, "codex-oauth", tokens)
        assert exc_save.value.status_code == 403
        assert fake_db.oauth_written == []


class TestAuthCliCommandsAndSubprocessPoolLifecycle:
    """Item 7: CLI opens and closes DB pool in its own event loop + standalone subprocess tests."""

    def test_in_process_cli_commands(self, fake_db: FakeRolesAndCliDB) -> None:
        fake_db.credentials[ADMIN_1] = {
            "user_id": ADMIN_1,
            "email": "admin@example.com",
            "username": "admin",
            "password_hash": hash_password("OldPassword123!"),
            "failed_attempts": 4,
            "locked_until": None,
        }
        fake_db.sessions.extend(
            [
                {"session_id": "s1", "user_id": ADMIN_1, "revoked_at": None, "revoked_reason": None},
                {"session_id": "s2", "user_id": ADMIN_1, "revoked_at": None, "revoked_reason": None},
            ]
        )
        answers = iter(["RecoveredPass789!", "RecoveredPass789!"])
        prompt_mock = create_autospec(
            lambda prompt: "", side_effect=lambda _p: next(answers)
        )
        out, err = io.StringIO(), io.StringIO()
        rc = auth_cli.main(
            ["reset-password", "--email", "Admin@Example.com"],
            prompt_fn=prompt_mock,
            out=out,
            err=err,
            manage_pool=False,
        )
        assert rc == 0, err.getvalue()
        assert "revoked 2 active session(s)" in out.getvalue()
        assert verify_password("RecoveredPass789!", fake_db.credentials[ADMIN_1]["password_hash"])
        # Round 3 Item 4: CLI reset-password follows unified lock order: advisory -> cred -> attempts
        assert fake_db.cli_sql_log[:3] == ["advisory_lock", "cred_lock", "attempts_delete"]

        out_g, err_g = io.StringIO(), io.StringIO()
        assert (
            auth_cli.main(
                ["grant-admin", "--email", "admin@example.com"],
                out=out_g,
                err=err_g,
                manage_pool=False,
            )
            == 0
        )
        assert fake_db.roles[ADMIN_1]["tier"] == "admin"

        out_r, err_r = io.StringIO(), io.StringIO()
        assert (
            auth_cli.main(["reissue-bootstrap-token"], out=out_r, err=err_r, manage_pool=False)
            == 0
        )
        assert "BOOTSTRAP_SETUP_TOKEN=" in out_r.getvalue()

        fake_db.bootstrap_state = {"singleton_id": 1, "completed_by_user_id": ADMIN_1}
        out_r2, err_r2 = io.StringIO(), io.StringIO()
        assert (
            auth_cli.main(["reissue-bootstrap-token"], out=out_r2, err=err_r2, manage_pool=False)
            == 1
        )
        assert "already completed" in err_r2.getvalue()

    def test_three_cli_commands_run_as_standalone_subprocesses_opening_and_closing_pool(
        self, tmp_path: Path
    ) -> None:
        """Spawn ``python -m src.dalton.auth.cli ...`` for all 3 commands with an autospecced pool harness."""
        harness_dir = tmp_path / "cli_harness"
        harness_dir.mkdir()
        log_file = tmp_path / "pool_events.log"

        # sitecustomize injects an autospecced AsyncConnectionPool that enforces open() before
        # get_db_connection() (mirroring real pool.py!) and logs open/close lifecycle events.
        (harness_dir / "sitecustomize.py").write_text(
            f"""
import getpass
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import create_autospec
from psycopg import AsyncConnection, AsyncCursor
from psycopg_pool import AsyncConnectionPool

LOG = Path({str(log_file)!r})
STATE = {{"opened": False, "closed": False}}

class _FakePool:
    async def open(self):
        STATE["opened"] = True
        with LOG.open("a") as f: f.write("pool_open\\n")
    async def close(self):
        STATE["closed"] = True
        with LOG.open("a") as f: f.write("pool_close\\n")

def _install():
    import sys as _sys
    import src.server.database.pool as pool_mod
    import src.dalton.auth.roles as roles_mod
    import src.dalton.auth.sessions as sessions_mod
    import src.dalton.auth.bootstrap as bootstrap_mod

    fake_pool = _FakePool()
    pool_mock = create_autospec(AsyncConnectionPool, instance=True)
    pool_mock.open.side_effect = fake_pool.open
    pool_mock.close.side_effect = fake_pool.close

    @asynccontextmanager
    async def _conn():
        if not STATE["opened"]:
            raise RuntimeError("Conversation DB pool is not open")
        @asynccontextmanager
        async def _cur(*a, **kw):
            c = create_autospec(AsyncCursor, instance=True)
            c._rows = []
            async def _exec(q, p=None, **k):
                sql = " ".join(str(q).split())
                c._rows = []
                if "FROM dalton_local_credentials WHERE email = %s" in sql or "FROM dalton_local_credentials WHERE user_id = %s" in sql:
                    c._rows = [{{"user_id": "u-cli-1", "email": "ops@example.com", "username": "ops"}}]
                elif "INSERT INTO dalton_user_roles" in sql:
                    c._rows = [{{"user_id": p[0], "tier": p[1], "granted_by": p[2]}}]
                elif "UPDATE dalton_local_sessions SET revoked_at" in sql:
                    c._rows = [{{"session_id": "s-1"}}]
                elif "INSERT INTO dalton_bootstrap_tokens" in sql:
                    c._rows = [{{"expires_at": datetime.now(timezone.utc) + timedelta(hours=24)}}]
                return c
            c.execute.side_effect = _exec
            c.fetchone.side_effect = lambda: (c._rows[0] if c._rows else None)
            c.fetchall.side_effect = lambda: list(c._rows)
            yield c
        @asynccontextmanager
        async def _tx(*a, **kw):
            yield None
        conn = create_autospec(AsyncConnection, instance=True)
        conn.cursor.side_effect = _cur
        conn.transaction.side_effect = _tx
        yield conn

    conn_fn = create_autospec(pool_mod.get_db_connection, side_effect=_conn)
    pool_fn = create_autospec(pool_mod.get_or_create_pool, return_value=pool_mock)
    main_mod = _sys.modules.get("__main__")
    if main_mod is not None:
        main_mod.get_or_create_pool = pool_fn
        main_mod.get_db_connection = conn_fn
    for mod in (pool_mod, roles_mod, sessions_mod, bootstrap_mod):
        mod.get_db_connection = conn_fn
    pool_mod.get_or_create_pool = pool_fn

import asyncio
_real_run = asyncio.run
def _hooked_run(coro, *args, **kwargs):
    _install()
    return _real_run(coro, *args, **kwargs)
asyncio.run = _hooked_run
""",
            encoding="utf-8",
        )

        env = {
            **os.environ,
            "PYTHONPATH": f"{harness_dir}{os.pathsep}{REPO_ROOT}",
        }

        for argv, stdin_data, expected_stdout in (
            (
                ["reset-password", "--email", "ops@example.com", "--password-stdin"],
                "NewSubprocessPass123!\n",
                "Password reset for ops@example.com",
            ),
            (
                ["grant-admin", "--email", "ops@example.com"],
                None,
                "Granted admin role to ops@example.com",
            ),
            (
                ["reissue-bootstrap-token"],
                None,
                "BOOTSTRAP_SETUP_TOKEN=",
            ),
        ):
            log_file.write_text("", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-m", "src.dalton.auth.cli", *argv],
                cwd=str(REPO_ROOT),
                env=env,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert proc.returncode == 0, f"{argv} failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
            assert expected_stdout in proc.stdout
            events = log_file.read_text(encoding="utf-8").splitlines()
            assert events == ["pool_open", "pool_close"], f"Unexpected pool events for {argv}: {events}"

        # Non-TTY reset-password WITHOUT --password-stdin fails immediately (does not hang)
        log_file.write_text("", encoding="utf-8")
        proc_notty = subprocess.run(
            [sys.executable, "-m", "src.dalton.auth.cli", "reset-password", "--email", "ops@example.com"],
            cwd=str(REPO_ROOT),
            env=env,
            input="",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc_notty.returncode == 1
        assert "--password-stdin" in proc_notty.stderr
        assert log_file.read_text(encoding="utf-8").splitlines() == ["pool_open", "pool_close"]
