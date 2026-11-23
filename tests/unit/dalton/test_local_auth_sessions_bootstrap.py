"""Unit tests for local auth passwords, bootstrap, stable-sid rotating sessions, concurrency,
rate limiting, Origin CSRF checks, Secure cookie policy, and immediate revocation.

Uses an in-memory transactional PostgreSQL test double wired via ``create_autospec``
on ``get_db_connection`` and ``AsyncConnection`` / ``AsyncCursor`` (including per-key
``asyncio.Lock`` objects simulating ``pg_advisory_xact_lock`` and controllable DB clock).
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from psycopg import AsyncConnection, AsyncCursor
from starlette.datastructures import Headers

from src.config import settings as app_settings
from src.dalton.auth import bootstrap as bootstrap_mod
from src.dalton.auth import passwords as passwords_mod
from src.dalton.auth import roles as roles_mod
from src.dalton.auth import sessions as sessions_mod
from src.dalton.auth.local_router import register_local_auth_routers
from src.dalton.auth.passwords import (
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)
from src.dalton.auth.tokens import REFRESH_COOKIE_NAME, decode_local_access_token
from src.server.database.pool import get_db_connection as _REAL_GET_DB_CONNECTION
from src.server.utils import api as api_utils
from src.server.utils.api import get_current_user_id

TEST_JWT_SECRET = "test-local-jwt-signing-secret-at-least-32-bytes-long!"
BOOTSTRAP_TOKEN = "setup-bootstrap-secret-token-0123456789abcdef"


class FakeLocalAuthDB:
    """In-memory transactional fake of d047 tables + keyed advisory locks + controllable DB clock."""

    def __init__(self, *, lock_delay_s: float = 0.0, db_clock: datetime | None = None) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.credential_holder: dict[str, Any] | None = None
        self.bootstrap_state: dict[str, Any] | None = None
        self.bootstrap_token: dict[str, Any] | None = None
        self.sessions: dict[str, dict[str, Any]] = {}
        self.refresh_history: dict[str, dict[str, Any]] = {}
        self.auth_attempts: dict[tuple[str, str], dict[str, Any]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.lock_delay_s = lock_delay_s
        self.advisory_lock_calls: list[int] = []
        self.clock_calls: list[tuple[str, bool]] = []  # (sql, advisory_lock_held)
        self.tx_lock_orders: list[list[str]] = []
        self.db_clock = db_clock

    def now(self) -> datetime:
        return self.db_clock or datetime.now(timezone.utc)

    def _lock_for(self, key: int) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "users": self.users,
                "credentials": self.credentials,
                "roles": self.roles,
                "credential_holder": self.credential_holder,
                "bootstrap_state": self.bootstrap_state,
                "bootstrap_token": self.bootstrap_token,
                "sessions": self.sessions,
                "refresh_history": self.refresh_history,
                "auth_attempts": self.auth_attempts,
            }
        )

    def _restore(self, snap: dict[str, Any]) -> None:
        self.users = snap["users"]
        self.credentials = snap["credentials"]
        self.roles = snap["roles"]
        self.credential_holder = snap["credential_holder"]
        self.bootstrap_state = snap["bootstrap_state"]
        self.bootstrap_token = snap["bootstrap_token"]
        self.sessions = snap["sessions"]
        self.refresh_history = snap["refresh_history"]
        self.auth_attempts = snap["auth_attempts"]

    def make_get_db_connection(self) -> Any:
        db = self

        @asynccontextmanager
        async def _get_db_connection():
            held_locks: list[int] = []
            tx_ops: list[str] = []

            async def _execute_query(
                cur: Any, query: Any, params: Any = None, **_: Any
            ) -> Any:
                sql = " ".join(str(query).split())
                p = tuple(params) if params is not None else ()
                cur._rows = []

                if "pg_advisory_xact_lock" in sql:
                    lock_key = int(p[0])
                    if lock_key not in held_locks:
                        await db._lock_for(lock_key).acquire()
                        held_locks.append(lock_key)
                        tx_ops.append("advisory_lock")
                    db.advisory_lock_calls.append(lock_key)
                    if db.lock_delay_s > 0:
                        await asyncio.sleep(db.lock_delay_s)
                    cur._rows = [{"pg_advisory_xact_lock": None}]
                    return cur

                if sql in ("SELECT NOW() AS db_now", "SELECT clock_timestamp() AS db_now"):
                    db.clock_calls.append((sql, bool(held_locks)))
                    cur._rows = [{"db_now": db.now()}]
                    return cur

                # dalton_bootstrap_state
                if "FROM dalton_bootstrap_state" in sql:
                    if db.bootstrap_state is not None:
                        cur._rows = [dict(db.bootstrap_state)]
                    return cur
                if "INSERT INTO dalton_bootstrap_state" in sql:
                    if db.bootstrap_state is not None:
                        raise RuntimeError("duplicate key dalton_bootstrap_state")
                    db.bootstrap_state = {
                        "singleton_id": 1,
                        "completed_at": p[0],
                        "completed_by_user_id": p[1],
                        "done": 1,
                    }
                    return cur

                # dalton_bootstrap_tokens
                if "FROM dalton_bootstrap_tokens" in sql and sql.startswith("SELECT"):
                    if db.bootstrap_token is not None:
                        cur._rows = [dict(db.bootstrap_token)]
                    return cur
                if "INSERT INTO dalton_bootstrap_tokens" in sql:
                    if "DO NOTHING" in sql and db.bootstrap_token is not None:
                        return cur
                    source = "cli" if "'cli'" in sql else "env"
                    if "INTERVAL '1 hour'" in sql:
                        ts = db.now()
                        exp = ts + timedelta(hours=int(p[1]))
                        token_hash = p[0]
                    else:
                        token_hash, ts, exp = p[0], p[1], p[2]
                    db.bootstrap_token = {
                        "singleton_id": 1,
                        "token_sha256": token_hash,
                        "source": source,
                        "issued_at": ts,
                        "expires_at": exp,
                        "failed_attempts": 0,
                        "locked_at": None,
                        "updated_at": ts,
                    }
                    cur._rows = [dict(db.bootstrap_token)]
                    return cur
                if "UPDATE dalton_bootstrap_tokens" in sql:
                    assert db.bootstrap_token is not None
                    db.bootstrap_token["failed_attempts"] = p[0]
                    if db.bootstrap_token["locked_at"] is None and p[1] is not None:
                        db.bootstrap_token["locked_at"] = p[1]
                    db.bootstrap_token["updated_at"] = p[2]
                    return cur
                if "DELETE FROM dalton_bootstrap_tokens" in sql:
                    db.bootstrap_token = None
                    return cur

                # users & user_preferences
                if "INSERT INTO users" in sql:
                    db.users[p[0]] = {
                        "user_id": p[0],
                        "email": p[1],
                        "name": p[2],
                        "timezone": p[3],
                        "last_login_at": p[6],
                    }
                    return cur
                if "INSERT INTO user_preferences" in sql:
                    return cur
                if "UPDATE users SET last_login_at" in sql:
                    if p[2] in db.users:
                        db.users[p[2]]["last_login_at"] = p[0]
                    return cur

                # dalton_local_credentials
                if "INSERT INTO dalton_local_credentials" in sql:
                    db.credentials[p[0]] = {
                        "user_id": p[0],
                        "email": p[1],
                        "username": p[2],
                        "password_hash": p[3],
                        "password_changed_at": p[4],
                        "failed_attempts": 0,
                        "locked_until": None,
                    }
                    return cur
                if "FROM dalton_local_credentials" in sql and "WHERE email = %s OR username = %s" in sql:
                    for row in db.credentials.values():
                        if row["email"] == p[0] or (row["username"] and row["username"] == p[1]):
                            cur._rows = [dict(row)]
                            break
                    return cur
                if "FROM dalton_local_credentials" in sql and "WHERE user_id = %s" in sql:
                    if "FOR UPDATE" in sql:
                        tx_ops.append("cred_lock")
                    row = db.credentials.get(p[0])
                    if row is not None:
                        cur._rows = [dict(row)]
                    return cur
                if "UPDATE dalton_local_credentials SET failed_attempts = %s" in sql:
                    row = db.credentials.get(p[3])
                    if row is not None:
                        row["failed_attempts"] = p[0]
                        row["locked_until"] = p[1]
                    return cur
                if "UPDATE dalton_local_credentials SET failed_attempts = 0" in sql:
                    row = db.credentials.get(p[1])
                    if row is not None:
                        row["failed_attempts"] = 0
                        row["locked_until"] = None
                    return cur
                if "UPDATE dalton_local_credentials SET password_hash = %s" in sql:
                    row = db.credentials.get(p[3])
                    if row is not None:
                        row["password_hash"] = p[0]
                        row["password_changed_at"] = p[1]
                        row["failed_attempts"] = 0
                        row["locked_until"] = None
                    return cur

                # dalton_user_roles
                if "INSERT INTO dalton_user_roles" in sql:
                    if "'admin', 'bootstrap'" in sql:
                        db.roles[p[0]] = {"user_id": p[0], "tier": "admin", "granted_by": "bootstrap"}
                    else:
                        db.roles[p[0]] = {"user_id": p[0], "tier": p[1], "granted_by": p[2]}
                    cur._rows = [dict(db.roles[p[0]])]
                    return cur
                if "SELECT tier FROM dalton_user_roles WHERE user_id = %s" in sql:
                    row = db.roles.get(p[0])
                    if row is not None:
                        cur._rows = [{"tier": row["tier"]}]
                    return cur

                # dalton_credential_holder
                if "INSERT INTO dalton_credential_holder" in sql:
                    db.credential_holder = {
                        "singleton_id": 1,
                        "holder_user_id": p[0],
                        "updated_by": p[1],
                        "updated_at": p[2],
                    }
                    cur._rows = [dict(db.credential_holder)]
                    return cur

                # dalton_local_sessions + join with credentials
                if "INSERT INTO dalton_local_sessions" in sql:
                    sid = str(uuid4())
                    db.sessions[sid] = {
                        "session_id": sid,
                        "user_id": p[0],
                        "refresh_token_hash": p[1],
                        "password_changed_at": p[2],
                        "created_at": p[3],
                        "last_used_at": p[4],
                        "expires_at": p[5],
                        "client_ip": p[6],
                        "user_agent": p[7],
                        "revoked_at": None,
                        "revoked_reason": None,
                    }
                    cur._rows = [{"session_id": sid}]
                    return cur
                if "FROM dalton_local_sessions s JOIN dalton_local_credentials c" in sql and "WHERE s.session_id = %s AND s.user_id = %s" in sql:
                    s = db.sessions.get(p[0])
                    if s is not None and s["user_id"] == p[1]:
                        c = db.credentials.get(p[1])
                        if c is not None:
                            merged = dict(s)
                            merged["current_password_changed_at"] = c["password_changed_at"]
                            merged["db_now"] = db.now()
                            cur._rows = [merged]
                    return cur
                if "FROM dalton_local_sessions s JOIN dalton_local_credentials c" in sql and "WHERE s.refresh_token_hash = %s" in sql:
                    for s in db.sessions.values():
                        if s["refresh_token_hash"] == p[0]:
                            c = db.credentials.get(s["user_id"])
                            if c is not None:
                                merged = dict(s)
                                merged["current_password_changed_at"] = c["password_changed_at"]
                                cur._rows = [merged]
                            break
                    return cur
                if "SELECT session_id FROM dalton_local_sessions WHERE refresh_token_hash = %s" in sql:
                    for s in db.sessions.values():
                        if s["refresh_token_hash"] == p[0]:
                            cur._rows = [{"session_id": s["session_id"]}]
                            break
                    return cur
                if "SELECT session_id, user_id FROM dalton_local_sessions WHERE refresh_token_hash = %s" in sql:
                    for s in db.sessions.values():
                        if s["refresh_token_hash"] == p[0]:
                            cur._rows = [{"session_id": s["session_id"], "user_id": s["user_id"]}]
                            break
                    return cur
                if "SELECT user_id FROM dalton_local_sessions WHERE refresh_token_hash = %s" in sql:
                    for s in db.sessions.values():
                        if s["refresh_token_hash"] == p[0]:
                            cur._rows = [{"user_id": s["user_id"]}]
                            break
                    return cur
                if "UPDATE dalton_local_sessions SET refresh_token_hash = %s" in sql:
                    s = db.sessions[p[5]]
                    s["refresh_token_hash"] = p[0]
                    s["last_used_at"] = p[1]
                    s["expires_at"] = p[2]
                    if p[3] is not None:
                        s["client_ip"] = p[3]
                    if p[4] is not None:
                        s["user_agent"] = p[4]
                    return cur
                if "UPDATE dalton_local_sessions SET revoked_at = %s, revoked_reason = 'reuse_detected'" in sql:
                    for s in db.sessions.values():
                        if s["user_id"] == p[1] and s["revoked_at"] is None:
                            s["revoked_at"] = p[0]
                            s["revoked_reason"] = "reuse_detected"
                    return cur
                if "UPDATE dalton_local_sessions SET revoked_at = %s, revoked_reason = 'password_changed'" in sql:
                    s = db.sessions.get(p[1])
                    if s is not None and s["revoked_at"] is None:
                        s["revoked_at"] = p[0]
                        s["revoked_reason"] = "password_changed"
                    return cur
                if "UPDATE dalton_local_sessions SET revoked_at = %s, revoked_reason = 'expired'" in sql:
                    s = db.sessions.get(p[1])
                    if s is not None:
                        s["revoked_at"] = p[0]
                        s["revoked_reason"] = "expired"
                    return cur
                if "UPDATE dalton_local_sessions SET revoked_at = %s, revoked_reason = %s WHERE session_id = %s AND user_id = %s" in sql:
                    s = db.sessions.get(p[2])
                    if s is not None and s["user_id"] == p[3] and s["revoked_at"] is None:
                        s["revoked_at"] = p[0]
                        s["revoked_reason"] = p[1]
                        cur._rows = [{"session_id": s["session_id"]}]
                    return cur
                if "UPDATE dalton_local_sessions SET revoked_at = %s, revoked_reason = %s WHERE refresh_token_hash = %s" in sql:
                    for s in db.sessions.values():
                        if s["refresh_token_hash"] == p[2] and s["revoked_at"] is None:
                            s["revoked_at"] = p[0]
                            s["revoked_reason"] = p[1]
                            cur._rows = [{"session_id": s["session_id"]}]
                            break
                    return cur
                if "UPDATE dalton_local_sessions SET revoked_at = %s, revoked_reason = %s WHERE user_id = %s" in sql:
                    revoked = []
                    for s in db.sessions.values():
                        if s["user_id"] == p[2] and s["revoked_at"] is None:
                            s["revoked_at"] = p[0]
                            s["revoked_reason"] = p[1]
                            revoked.append({"session_id": s["session_id"]})
                    cur._rows = revoked
                    return cur

                # dalton_local_refresh_tokens
                if "FROM dalton_local_refresh_tokens h JOIN dalton_local_sessions s" in sql:
                    h = db.refresh_history.get(p[0])
                    if h is not None:
                        s = db.sessions.get(h["session_id"])
                        c = db.credentials.get(h["user_id"])
                        if s is not None and c is not None:
                            merged = dict(h)
                            merged["session_revoked_at"] = s["revoked_at"]
                            merged["session_expires_at"] = s["expires_at"]
                            merged["session_password_changed_at"] = s["password_changed_at"]
                            merged["current_password_changed_at"] = c["password_changed_at"]
                            cur._rows = [merged]
                    return cur
                if "SELECT session_id, user_id, expires_at FROM dalton_local_refresh_tokens WHERE token_hash = %s" in sql:
                    h = db.refresh_history.get(p[0])
                    if h is not None:
                        cur._rows = [
                            {
                                "session_id": h["session_id"],
                                "user_id": h["user_id"],
                                "expires_at": h["expires_at"],
                            }
                        ]
                    return cur
                if "SELECT session_id, user_id FROM dalton_local_refresh_tokens WHERE token_hash = %s" in sql:
                    h = db.refresh_history.get(p[0])
                    if h is not None:
                        cur._rows = [{"session_id": h["session_id"], "user_id": h["user_id"]}]
                    return cur
                if "SELECT user_id FROM dalton_local_refresh_tokens WHERE token_hash = %s" in sql:
                    h = db.refresh_history.get(p[0])
                    if h is not None:
                        cur._rows = [{"user_id": h["user_id"]}]
                    return cur
                if "INSERT INTO dalton_local_refresh_tokens" in sql:
                    if p[0] not in db.refresh_history:
                        db.refresh_history[p[0]] = {
                            "token_hash": p[0],
                            "session_id": p[1],
                            "user_id": p[2],
                            "rotated_at": p[3],
                            "expires_at": p[4],
                            "reuse_detected_at": None,
                        }
                    return cur
                if "UPDATE dalton_local_refresh_tokens SET reuse_detected_at = %s" in sql:
                    h = db.refresh_history.get(p[1])
                    if h is not None and h["reuse_detected_at"] is None:
                        if len(p) >= 3 and not (h["expires_at"] > p[2]):
                            return cur
                        h["reuse_detected_at"] = p[0]
                        cur._rows = [{"session_id": h["session_id"], "user_id": h["user_id"]}]
                    return cur

                # dalton_auth_attempts
                if "INSERT INTO dalton_auth_attempts" in sql and "DO NOTHING" in sql:
                    key = (p[0], p[1])
                    if key not in db.auth_attempts:
                        db.auth_attempts[key] = {
                            "dimension": p[0],
                            "key_hash": p[1],
                            "failed_attempts": 0,
                            "window_started_at": p[2],
                            "last_failed_at": None,
                            "locked_until": None,
                            "updated_at": p[3],
                        }
                    return cur
                if "FROM dalton_auth_attempts WHERE dimension = %s AND key_hash = %s" in sql:
                    if p[0] == "account":
                        tx_ops.append("attempts_account_lock")
                    else:
                        tx_ops.append("attempts_ip_lock")
                    entry = db.auth_attempts.get((p[0], p[1]))
                    if entry is not None:
                        cur._rows = [dict(entry)]
                    return cur
                if sql.startswith("SELECT") and "FROM dalton_auth_attempts WHERE dimension = 'account' AND key_hash = %s" in sql:
                    tx_ops.append("attempts_account_lock")
                    entry = db.auth_attempts.get(("account", p[0]))
                    if entry is not None:
                        cur._rows = [dict(entry)]
                    return cur
                if sql.startswith("SELECT") and "FROM dalton_auth_attempts WHERE dimension = 'ip' AND key_hash = %s" in sql:
                    tx_ops.append("attempts_ip_lock")
                    entry = db.auth_attempts.get(("ip", p[0]))
                    if entry is not None:
                        cur._rows = [dict(entry)]
                    return cur
                if "UPDATE dalton_auth_attempts SET failed_attempts = %s, window_started_at = %s" in sql:
                    key = (p[5], p[6])
                    db.auth_attempts[key] = {
                        "dimension": p[5],
                        "key_hash": p[6],
                        "failed_attempts": p[0],
                        "window_started_at": p[1],
                        "last_failed_at": p[2],
                        "locked_until": p[3],
                        "updated_at": p[4],
                    }
                    return cur
                if "UPDATE dalton_auth_attempts SET failed_attempts = %s, locked_until = %s" in sql:
                    dim = "account" if "dimension = 'account'" in sql else "ip"
                    key = (dim, p[3])
                    if key in db.auth_attempts:
                        db.auth_attempts[key]["failed_attempts"] = p[0]
                        db.auth_attempts[key]["locked_until"] = p[1]
                        db.auth_attempts[key]["updated_at"] = p[2]
                    return cur
                if "DELETE FROM dalton_auth_attempts WHERE dimension = 'account'" in sql:
                    tx_ops.append("attempts_account_lock")
                    db.auth_attempts.pop(("account", p[0]), None)
                    return cur

                raise AssertionError(f"Unhandled SQL in FakeLocalAuthDB: {sql}")

            @asynccontextmanager
            async def _cursor_cm(*args: Any, **kwargs: Any):
                cur = create_autospec(AsyncCursor, instance=True)
                cur._rows = []

                async def _exec(q: Any, params: Any = None, **kw: Any) -> Any:
                    return await _execute_query(cur, q, params, **kw)

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
                snap = db._snapshot()
                tx_ops.clear()
                try:
                    yield None
                except BaseException:
                    db._restore(snap)
                    raise
                finally:
                    if tx_ops:
                        db.tx_lock_orders.append(list(tx_ops))
                    while held_locks:
                        lk = held_locks.pop()
                        db._lock_for(lk).release()

            conn = create_autospec(AsyncConnection, instance=True)
            conn.cursor.side_effect = _cursor_cm
            conn.transaction.side_effect = _tx_cm
            yield conn

        return create_autospec(_REAL_GET_DB_CONNECTION, side_effect=_get_db_connection)


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeLocalAuthDB:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("BOOTSTRAP_SETUP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.delenv("DALTON_AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("DALTON_TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.delenv("DALTON_TRUSTED_PROXIES", raising=False)

    db = FakeLocalAuthDB()
    conn_factory = db.make_get_db_connection()
    monkeypatch.setattr(bootstrap_mod, "get_db_connection", conn_factory)
    monkeypatch.setattr(sessions_mod, "get_db_connection", conn_factory)
    monkeypatch.setattr(roles_mod, "get_db_connection", conn_factory)
    return db


def _seed_bootstrap_token(db: FakeLocalAuthDB, token: str = BOOTSTRAP_TOKEN) -> None:
    """Seed ``dalton_bootstrap_tokens`` as the startup hook does at server boot."""
    ts = db.now()
    db.bootstrap_token = {
        "singleton_id": 1,
        "token_sha256": bootstrap_mod._sha256_hex(token),
        "source": "env",
        "issued_at": ts,
        "expires_at": ts + timedelta(hours=24),
        "failed_attempts": 0,
        "locked_at": None,
        "updated_at": ts,
    }


def _build_local_auth_client() -> TestClient:
    app = FastAPI()
    register_local_auth_routers(app)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


class TestScryptOffLoopExecution:
    """Item 11: versioned scrypt + bounded off-loop ThreadPoolExecutor."""

    def test_versioned_scrypt_hash_and_constant_time_verify(self) -> None:
        h1 = hash_password("StrongPass123!")
        h2 = hash_password("StrongPass123!")
        assert h1 != h2
        assert h1.startswith("scrypt$v1$n=32768,r=8,p=1$")
        assert verify_password("StrongPass123!", h1) is True
        assert verify_password("WrongPass123!", h1) is False

    @pytest.mark.asyncio
    async def test_async_scrypt_does_not_block_event_loop(self) -> None:
        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            for _ in range(3):
                await asyncio.sleep(0.005)
                ticks += 1

        hashed, _ = await asyncio.gather(
            hash_password_async("AsyncPass12345!"),
            _ticker(),
        )
        assert ticks == 3
        assert await verify_password_async("AsyncPass12345!", hashed) is True

    @pytest.mark.asyncio
    async def test_scrypt_queue_saturation_returns_429_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 3 Item 3: saturated scrypt wait queue rejects immediately with HTTP 429."""
        monkeypatch.setenv("DALTON_SCRYPT_MAX_WORKERS", "1")
        monkeypatch.setenv("DALTON_SCRYPT_MAX_QUEUE", "0")
        monkeypatch.setattr(passwords_mod, "_scrypt_inflight", 1)

        with pytest.raises(HTTPException) as exc:
            await hash_password_async("OverflowPass123!")
        assert exc.value.status_code == 429
        assert exc.value.headers == {"Retry-After": "1"}


class TestFirstAdminBootstrap:
    """Items 4 & 8: startup-seeded 24h TTL, post-lock DB clock expiry, 5-attempt lockout, concurrency."""

    @pytest.mark.asyncio
    async def test_startup_seeds_24h_ttl_and_post_lock_db_clock_enforces_expiry(
        self, fake_db: FakeLocalAuthDB
    ) -> None:
        t0 = datetime(2026, 11, 23, 0, 0, 0, tzinfo=timezone.utc)
        fake_db.db_clock = t0
        assert await bootstrap_mod.seed_bootstrap_token_from_env_if_needed() is True
        assert fake_db.bootstrap_token is not None
        assert fake_db.bootstrap_token["expires_at"] == t0 + timedelta(hours=24)

        # Restart seeding does NOT reset expires_at
        fake_db.db_clock = t0 + timedelta(hours=10)
        assert await bootstrap_mod.seed_bootstrap_token_from_env_if_needed() is False
        assert fake_db.bootstrap_token["expires_at"] == t0 + timedelta(hours=24)

        # Advance DB clock past 24h -> bootstrap rejected with 410 using post-lock clock_timestamp()
        fake_db.db_clock = t0 + timedelta(hours=24, seconds=1)
        fake_db.clock_calls.clear()
        with pytest.raises(HTTPException) as exc_exp:
            await bootstrap_mod.bootstrap_first_admin(
                bootstrap_token=BOOTSTRAP_TOKEN,
                email="admin@example.com",
                password="AdminPassword123!",
            )
        assert exc_exp.value.status_code == 410
        assert ("SELECT clock_timestamp() AS db_now", True) in fake_db.clock_calls

    @pytest.mark.asyncio
    async def test_bootstrap_checks_gates_before_scrypt_and_never_lazily_seeds_token(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeLocalAuthDB
    ) -> None:
        """Round 3 Item 3: no scrypt on unseeded/wrong/locked/expired/completed bootstrap."""
        spy_hash = create_autospec(
            bootstrap_mod.hash_password_async, return_value=hash_password("AdminPassword123!")
        )
        monkeypatch.setattr(bootstrap_mod, "hash_password_async", spy_hash)

        # 1. Missing token row in DB -> 503 (never lazily seeds a new 24h window from env!) and 0 scrypt
        with pytest.raises(HTTPException) as exc_unseeded:
            await bootstrap_mod.bootstrap_first_admin(
                bootstrap_token=BOOTSTRAP_TOKEN,
                email="admin@example.com",
                password="AdminPassword123!",
            )
        assert exc_unseeded.value.status_code == 503
        assert fake_db.bootstrap_token is None
        spy_hash.assert_not_awaited()

        # 2. Seed token, test wrong token -> 401 and 0 scrypt
        _seed_bootstrap_token(fake_db)
        with pytest.raises(HTTPException) as exc_wrong:
            await bootstrap_mod.bootstrap_first_admin(
                bootstrap_token="wrong-token",
                email="admin@example.com",
                password="AdminPassword123!",
            )
        assert exc_wrong.value.status_code == 401
        spy_hash.assert_not_awaited()

        # 3. Expired token -> 410 and 0 scrypt
        fake_db.bootstrap_token["expires_at"] = fake_db.now() - timedelta(seconds=5)  # type: ignore[index]
        with pytest.raises(HTTPException) as exc_exp:
            await bootstrap_mod.bootstrap_first_admin(
                bootstrap_token=BOOTSTRAP_TOKEN,
                email="admin@example.com",
                password="AdminPassword123!",
            )
        assert exc_exp.value.status_code == 410
        spy_hash.assert_not_awaited()

        # 4. Locked token -> 423 and 0 scrypt
        fake_db.bootstrap_token["expires_at"] = fake_db.now() + timedelta(hours=12)  # type: ignore[index]
        fake_db.bootstrap_token["locked_at"] = fake_db.now()  # type: ignore[index]
        with pytest.raises(HTTPException) as exc_locked:
            await bootstrap_mod.bootstrap_first_admin(
                bootstrap_token=BOOTSTRAP_TOKEN,
                email="admin@example.com",
                password="AdminPassword123!",
            )
        assert exc_locked.value.status_code == 423
        spy_hash.assert_not_awaited()

        # 5. Completed bootstrap -> 409 and 0 scrypt
        fake_db.bootstrap_state = {"singleton_id": 1, "completed_by_user_id": "u-done"}
        with pytest.raises(HTTPException) as exc_done:
            await bootstrap_mod.bootstrap_first_admin(
                bootstrap_token=BOOTSTRAP_TOKEN,
                email="admin@example.com",
                password="AdminPassword123!",
            )
        assert exc_done.value.status_code == 409
        spy_hash.assert_not_awaited()

    def test_bootstrap_status_success_and_permanent_409(
        self, fake_db: FakeLocalAuthDB
    ) -> None:
        _seed_bootstrap_token(fake_db)
        client = _build_local_auth_client()
        assert client.get("/api/v1/dalton/bootstrap/status").json() == {"needs_bootstrap": True}

        r_boot = client.post(
            "/api/v1/dalton/bootstrap/admin",
            json={
                "bootstrap_token": BOOTSTRAP_TOKEN,
                "email": "Admin@Example.com",
                "username": "founder",
                "password": "AdminPassword123!",
                "name": "Founder Admin",
            },
        )
        assert r_boot.status_code == 201
        uid = r_boot.json()["user"]["user_id"]
        assert decode_local_access_token(r_boot.json()["access_token"]).user_id == uid
        assert uid in fake_db.users and uid in fake_db.credentials
        assert fake_db.roles[uid]["tier"] == "admin"
        assert fake_db.credential_holder is not None
        assert fake_db.credential_holder["holder_user_id"] == uid
        assert fake_db.bootstrap_state is not None
        assert fake_db.bootstrap_token is None

        assert client.get("/api/v1/dalton/bootstrap/status").json() == {"needs_bootstrap": False}
        assert (
            client.post(
                "/api/v1/dalton/bootstrap/admin",
                json={
                    "bootstrap_token": BOOTSTRAP_TOKEN,
                    "email": "second@example.com",
                    "password": "SecondPassword123!",
                },
            ).status_code
            == 409
        )

    def test_bootstrap_locks_after_5_failed_attempts(self, fake_db: FakeLocalAuthDB) -> None:
        _seed_bootstrap_token(fake_db)
        client = _build_local_auth_client()
        for attempt in range(1, 6):
            resp = client.post(
                "/api/v1/dalton/bootstrap/admin",
                json={
                    "bootstrap_token": "wrong-token",
                    "email": "admin@example.com",
                    "password": "AdminPassword123!",
                },
            )
            assert resp.status_code == (401 if attempt < 5 else 423)

        assert (
            client.post(
                "/api/v1/dalton/bootstrap/admin",
                json={
                    "bootstrap_token": BOOTSTRAP_TOKEN,
                    "email": "admin@example.com",
                    "password": "AdminPassword123!",
                },
            ).status_code
            == 423
        )

    @pytest.mark.asyncio
    async def test_bootstrap_concurrent_requests_only_one_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        monkeypatch.setenv("BOOTSTRAP_SETUP_TOKEN", BOOTSTRAP_TOKEN)

        db = FakeLocalAuthDB(lock_delay_s=0.05)
        _seed_bootstrap_token(db)
        conn_factory = db.make_get_db_connection()
        monkeypatch.setattr(bootstrap_mod, "get_db_connection", conn_factory)
        monkeypatch.setattr(sessions_mod, "get_db_connection", conn_factory)

        results = await asyncio.gather(
            bootstrap_mod.bootstrap_first_admin(
                bootstrap_token=BOOTSTRAP_TOKEN,
                email="first@example.com",
                password="FirstPassword123!",
            ),
            bootstrap_mod.bootstrap_first_admin(
                bootstrap_token=BOOTSTRAP_TOKEN,
                email="second@example.com",
                password="SecondPassword123!",
            ),
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, bootstrap_mod.BootstrapAdminResult)]
        conflicts = [
            r for r in results if isinstance(r, Exception) and getattr(r, "status_code", None) == 409
        ]
        assert len(successes) == 1
        assert len(conflicts) == 1


class TestSessionsRevocationOriginAndRateLimits:
    """Items 3, 4, 10, 12, 13."""

    def _bootstrap_admin(
        self, client: TestClient, db: FakeLocalAuthDB | None = None
    ) -> dict[str, Any]:
        if db is not None and db.bootstrap_token is None and db.bootstrap_state is None:
            _seed_bootstrap_token(db)
        resp = client.post(
            "/api/v1/dalton/bootstrap/admin",
            json={
                "bootstrap_token": BOOTSTRAP_TOKEN,
                "email": "admin@example.com",
                "username": "adminuser",
                "password": "InitialPassword123!",
            },
        )
        assert resp.status_code == 201
        return resp.json()

    @pytest.mark.asyncio
    async def test_stable_sid_concurrent_refresh_grace_and_bounded_replay_clears_cookie(
        self, fake_db: FakeLocalAuthDB
    ) -> None:
        t0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
        fake_db.db_clock = t0
        client = _build_local_auth_client()
        self._bootstrap_admin(client, fake_db)

        r_login = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"identifier": "admin@example.com", "password": "InitialPassword123!"},
        )
        r_other_device = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"identifier": "admin@example.com", "password": "InitialPassword123!"},
        )
        assert r_login.status_code == 200 and r_other_device.status_code == 200
        tok1 = r_login.json()["access_token"]
        other_tok = r_other_device.json()["access_token"]
        sid1 = decode_local_access_token(tok1).session_id
        ref1 = r_login.cookies[REFRESH_COOKIE_NAME]

        # Refresh keeps sid STABLE while rotating refresh token and reads post-lock clock_timestamp()
        fake_db.clock_calls.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_ref = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Origin": "http://testserver"},
        )
        assert r_ref.status_code == 200
        assert ("SELECT clock_timestamp() AS db_now", True) in fake_db.clock_calls
        tok2 = r_ref.json()["access_token"]
        assert decode_local_access_token(tok2).session_id == sid1
        ref2 = r_ref.cookies[REFRESH_COOKIE_NAME]
        assert ref2 != ref1

        # Round 3 Item 1: Concurrent refresh reusing ref1 within 10s grace window ->
        # returns retryable 409 WITHOUT clearing cookie and WITHOUT revoking ANY session!
        fake_db.db_clock = t0 + timedelta(seconds=3)
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_concurrent = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Origin": "http://testserver"},
        )
        assert r_concurrent.status_code == 409
        assert "Max-Age=0" not in r_concurrent.headers.get("set-cookie", "")

        # Access tokens on BOTH this session (tok2) and the other device (other_tok) remain active!
        req = create_autospec(Request, instance=True)
        req.__len__.return_value = 1
        req.headers = Headers({})
        req.scope = {"state": {}}
        for active_tok in (tok2, other_tok):
            assert (
                await get_current_user_id(
                    request=req,
                    credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=active_tok),
                )
                == r_login.json()["user"]["user_id"]
            )

        # Replay of old rotated token ref1 AFTER grace window (>10s) ->
        # 401 WITH Set-Cookie clearing header AND revokes ALL active sessions
        fake_db.db_clock = t0 + timedelta(seconds=15)
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_replay1 = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Origin": "http://testserver"},
        )
        assert r_replay1.status_code == 401
        assert f"{REFRESH_COOKIE_NAME}=" in r_replay1.headers.get("set-cookie", "")
        assert "Max-Age=0" in r_replay1.headers.get("set-cookie", "")

        # Existing unexpired access tokens (tok2 and other_tok) are IMMEDIATELY rejected (401)
        for revoked_tok in (tok2, other_tok):
            with pytest.raises(HTTPException) as exc_imm:
                await get_current_user_id(
                    request=req,
                    credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=revoked_tok),
                )
            assert exc_imm.value.status_code == 401

        # Item 12: User logs in again with a fresh session; replaying ref1 a SECOND time
        # does NOT revoke the new session (once-only replay handling!)
        r_fresh = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"identifier": "admin@example.com", "password": "InitialPassword123!"},
        )
        assert r_fresh.status_code == 200
        fresh_tok = r_fresh.json()["access_token"]

        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_replay2 = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Origin": "http://testserver"},
        )
        assert r_replay2.status_code == 401
        assert (
            await get_current_user_id(
                request=req,
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=fresh_tok),
            )
            == r_login.json()["user"]["user_id"]
        )

    @pytest.mark.asyncio
    async def test_concurrent_refresh_tasks_never_revoke_account_sessions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 3 Item 1: two concurrent refresh tasks -> one succeeds, one gets retryable 409, other sessions stay alive."""
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        t0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
        db = FakeLocalAuthDB(lock_delay_s=0.02, db_clock=t0)
        _seed_bootstrap_token(db)
        conn_factory = db.make_get_db_connection()
        monkeypatch.setattr(bootstrap_mod, "get_db_connection", conn_factory)
        monkeypatch.setattr(sessions_mod, "get_db_connection", conn_factory)
        monkeypatch.setattr(roles_mod, "get_db_connection", conn_factory)

        boot = await bootstrap_mod.bootstrap_first_admin(
            bootstrap_token=BOOTSTRAP_TOKEN,
            email="admin@example.com",
            password="InitialPassword123!",
        )
        other = await sessions_mod.authenticate_local_login(
            identifier="admin@example.com",
            password="InitialPassword123!",
            client_ip="10.0.0.9",
            now=t0,
        )

        res = await asyncio.gather(
            sessions_mod.refresh_local_session(raw_refresh_token=boot.refresh_token),
            sessions_mod.refresh_local_session(raw_refresh_token=boot.refresh_token),
            return_exceptions=True,
        )
        succeeded = [r for r in res if isinstance(r, sessions_mod.LocalRefreshResult)]
        retryable = [
            r for r in res if isinstance(r, HTTPException) and r.status_code == 409
        ]
        assert len(succeeded) == 1
        assert len(retryable) == 1
        # Neither the rotated session nor the other device's session was revoked!
        assert db.sessions[boot.session_id]["revoked_at"] is None
        assert db.sessions[other.session_id]["revoked_at"] is None

    @pytest.mark.asyncio
    async def test_logout_revokes_by_sid_even_after_concurrent_refresh_and_errors_on_unknown(
        self, fake_db: FakeLocalAuthDB
    ) -> None:
        """Round 3 Item 2: logout resolves stable sid across rotation and returns 401 when revocation fails."""
        client = _build_local_auth_client()
        self._bootstrap_admin(client, fake_db)

        r1 = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"identifier": "admin@example.com", "password": "InitialPassword123!"},
        )
        r2 = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"identifier": "admin@example.com", "password": "InitialPassword123!"},
        )
        tok1, ref1 = r1.json()["access_token"], r1.cookies[REFRESH_COOKIE_NAME]
        tok2 = r2.json()["access_token"]

        # Simulate concurrent refresh rotating ref1 -> ref1_new BEFORE logout carrying old ref1 lands:
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_rot = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Origin": "http://testserver"},
        )
        assert r_rot.status_code == 200
        tok1_rotated = r_rot.json()["access_token"]

        req = create_autospec(Request, instance=True)
        req.__len__.return_value = 1
        req.headers = Headers({})
        req.scope = {"state": {}}

        # Logout presenting the PRE-ROTATION cookie ref1 still resolves sid and revokes the session!
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_logout = client.post(
            "/api/v1/dalton/auth/local/logout",
            headers={"Origin": "http://testserver"},
        )
        assert r_logout.status_code == 200

        # Both old and rotated access tokens for session 1 are IMMEDIATELY rejected, while session 2 stays valid!
        for t in (tok1, tok1_rotated):
            with pytest.raises(HTTPException) as exc_out:
                await get_current_user_id(
                    request=req,
                    credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=t),
                )
            assert exc_out.value.status_code == 401
        assert (
            await get_current_user_id(
                request=req,
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok2),
            )
            == r1.json()["user"]["user_id"]
        )

        # Second logout with already-revoked (or unknown) token returns 401 error (NOT 200!) and clears cookie
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_logout_again = client.post(
            "/api/v1/dalton/auth/local/logout",
            headers={"Origin": "http://testserver"},
        )
        assert r_logout_again.status_code == 401
        assert "Max-Age=0" in r_logout_again.headers.get("set-cookie", "")

        # Change password -> tok2 immediately invalid
        r_chg = client.post(
            "/api/v1/dalton/auth/local/change-password",
            json={
                "current_password": "InitialPassword123!",
                "new_password": "RotatedPassword456!",
            },
            headers={"Authorization": f"Bearer {tok2}"},
        )
        assert r_chg.status_code == 200
        with pytest.raises(HTTPException) as exc_chg:
            await get_current_user_id(
                request=req,
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok2),
            )
        assert exc_chg.value.status_code == 401

    def test_origin_csrf_guard_and_cookie_secure_policy(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeLocalAuthDB
    ) -> None:
        client = _build_local_auth_client()
        self._bootstrap_admin(client, fake_db)

        # Round 3 Item 8: local mode defaults Cookie to Secure=True even on HTTP!
        r_login = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"email": "admin@example.com", "password": "InitialPassword123!"},
        )
        ref = r_login.cookies[REFRESH_COOKIE_NAME]
        assert "; Secure" in r_login.headers["set-cookie"]

        # ONLY explicit dev config DALTON_AUTH_COOKIE_SECURE=false removes Secure
        monkeypatch.setenv("DALTON_AUTH_COOKIE_SECURE", "false")
        r_dev_http = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"email": "admin@example.com", "password": "InitialPassword123!"},
        )
        assert "; Secure" not in r_dev_http.headers["set-cookie"]
        monkeypatch.delenv("DALTON_AUTH_COOKIE_SECURE", raising=False)

        # Round 3 Item 8: Requests missing BOTH Origin and Sec-Fetch-Site: same-origin are rejected 403,
        # as well as cross-origin, sibling-subdomain, null Origin, and Sec-Fetch-Site != same-origin.
        client.cookies.set(REFRESH_COOKIE_NAME, ref)
        for bad_headers in (
            {},
            {"Sec-Fetch-Site": "none"},
            {"Referer": "http://testserver/login"},
            {"Origin": "https://evil.example.com"},
            {"Origin": "http://sub.testserver"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "same-site"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Referer": "https://attacker.example.com/form"},
        ):
            assert (
                client.post("/api/v1/dalton/auth/local/refresh", headers=bad_headers).status_code
                == 403
            )
            assert (
                client.post("/api/v1/dalton/auth/local/logout", headers=bad_headers).status_code
                == 403
            )

        # Valid Sec-Fetch-Site: same-origin (without Origin) is accepted
        r_same_origin = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert r_same_origin.status_code == 200

    @pytest.mark.asyncio
    async def test_concurrent_login_and_change_password_share_account_lock(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeLocalAuthDB
    ) -> None:
        client = _build_local_auth_client()
        boot = self._bootstrap_admin(client, fake_db)
        uid = boot["user"]["user_id"]

        # Simulate change-password completing while login is mid-scrypt:
        # login's post-scrypt account-locked recheck MUST reject with 401 instead of creating a session!
        orig_verify = sessions_mod.verify_password_async
        called = False

        async def _verify_while_pw_changes(pw: str, h: str, *, slot_held: bool = False) -> bool:
            nonlocal called
            ok = await orig_verify(pw, h, slot_held=slot_held)
            if not called:
                called = True
                await sessions_mod.change_local_password(
                    user_id=uid,
                    current_password="InitialPassword123!",
                    new_password="BrandNewPassword999!",
                )
            return ok

        monkeypatch.setattr(
            sessions_mod,
            "verify_password_async",
            create_autospec(orig_verify, side_effect=_verify_while_pw_changes),
        )

        with pytest.raises(HTTPException) as exc:
            await sessions_mod.authenticate_local_login(
                identifier="admin@example.com",
                password="InitialPassword123!",
                client_ip="10.0.0.5",
            )
        assert exc.value.status_code == 401
        # Zero active sessions remain for uid after change-password!
        active = [s for s in fake_db.sessions.values() if s["user_id"] == uid and s["revoked_at"] is None]
        assert active == []

    @pytest.mark.asyncio
    async def test_unified_lock_order_under_concurrent_login_and_password_change(
        self, fake_db: FakeLocalAuthDB
    ) -> None:
        """Round 3 Item 4: every account transaction strictly locks advisory -> cred -> attempts (zero inversions)."""
        client = _build_local_auth_client()
        boot = self._bootstrap_admin(client, fake_db)
        uid = boot["user"]["user_id"]
        fake_db.tx_lock_orders.clear()

        # Run concurrent logins + password change on the same account
        await asyncio.gather(
            sessions_mod.authenticate_local_login(
                identifier="admin@example.com",
                password="WrongPassword1!",
                client_ip="10.0.0.10",
            ),
            sessions_mod.authenticate_local_login(
                identifier="admin@example.com",
                password="InitialPassword123!",
                client_ip="10.0.0.11",
            ),
            sessions_mod.change_local_password(
                user_id=uid,
                current_password="InitialPassword123!",
                new_password="UnifiedOrderPass999!",
                client_ip="10.0.0.12",
            ),
            return_exceptions=True,
        )

        # Verify NO transaction ever acquired attempts_account_lock before cred_lock or advisory_lock,
        # and NO transaction mixed account advisory lock with IP attempt lock!
        rank = {"advisory_lock": 1, "cred_lock": 2, "attempts_account_lock": 3}
        account_txs = [tx for tx in fake_db.tx_lock_orders if "advisory_lock" in tx]
        assert len(account_txs) >= 3
        for tx in account_txs:
            assert "attempts_ip_lock" not in tx, f"IP lock mixed into account tx: {tx}"
            ranks = [rank[op] for op in tx if op in rank]
            assert ranks == sorted(ranks), f"Lock order inversion detected: {tx}"
            if "attempts_account_lock" in tx:
                assert "advisory_lock" in tx and "cred_lock" in tx, f"Missing outer locks: {tx}"

    @pytest.mark.asyncio
    async def test_atomic_rate_limit_ip_first_unknown_user_ip_only_and_alias_merge(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeLocalAuthDB
    ) -> None:
        client = _build_local_auth_client()
        boot = self._bootstrap_admin(client, fake_db)
        uid = boot["user"]["user_id"]

        # Round 3 Item 5a: Unknown usernames increment ONLY IP dimension and NEVER create 'account' rows
        for idx in range(3):
            with pytest.raises(HTTPException) as e_unk:
                await sessions_mod.authenticate_local_login(
                    identifier=f"nobody{idx}@example.com",
                    password="WrongPassword1!",
                    client_ip="10.1.1.1",
                )
            assert e_unk.value.status_code == 401
        account_rows = [k for k in fake_db.auth_attempts if k[0] == "account"]
        assert account_rows == []
        assert ("ip", sessions_mod.hash_attempt_key("10.1.1.1")) in fake_db.auth_attempts

        # Round 3 Item 5b: When IP is already locked, login returns 429 WITHOUT incrementing account counter!
        locked_ip = "10.2.2.2"
        locked_ip_hash = sessions_mod.hash_attempt_key(locked_ip)
        now = fake_db.now()
        fake_db.auth_attempts[("ip", locked_ip_hash)] = {
            "dimension": "ip",
            "key_hash": locked_ip_hash,
            "failed_attempts": 15,
            "window_started_at": now,
            "last_failed_at": now,
            "locked_until": now + timedelta(seconds=60),
            "updated_at": now,
        }
        with pytest.raises(HTTPException) as e_ip_locked:
            await sessions_mod.authenticate_local_login(
                identifier="admin@example.com",
                password="WrongPassword1!",
                client_ip=locked_ip,
                now=now,
            )
        assert e_ip_locked.value.status_code == 429
        assert [k for k in fake_db.auth_attempts if k[0] == "account"] == []
        assert fake_db.credentials[uid]["failed_attempts"] == 0

        # 3 failed attempts by email + 2 failed attempts by username -> merged into ONE uid bucket -> locked!
        for _ in range(3):
            with pytest.raises(HTTPException) as e1:
                await sessions_mod.authenticate_local_login(
                    identifier="admin@example.com", password="WrongPassword1!", client_ip="10.0.0.1"
                )
            assert e1.value.status_code == 401
        for _ in range(2):
            with pytest.raises(HTTPException) as e2:
                await sessions_mod.authenticate_local_login(
                    identifier="adminuser", password="WrongPassword2!", client_ip="10.0.0.2"
                )
            assert e2.value.status_code == 401

        # 6th attempt via EITHER email or username returns 429 BEFORE scrypt!
        spy_verify = create_autospec(sessions_mod.verify_password_async, return_value=False)
        monkeypatch.setattr(sessions_mod, "verify_password_async", spy_verify)
        with pytest.raises(HTTPException) as e_locked:
            await sessions_mod.authenticate_local_login(
                identifier="admin@example.com", password="InitialPassword123!", client_ip="10.0.0.3"
            )
        assert e_locked.value.status_code == 429
        spy_verify.assert_not_awaited()

        # Grok #6: change-password is also rate-limited (5 wrong guesses -> 429)
        fake_db.auth_attempts.clear()
        spy_verify.reset_mock()
        for _ in range(5):
            with pytest.raises(HTTPException) as e_cp:
                await sessions_mod.change_local_password(
                    user_id=uid,
                    current_password="WrongCurrent123!",
                    new_password="NewPassword7890!",
                )
            assert e_cp.value.status_code == 401
        with pytest.raises(HTTPException) as e_cp_429:
            await sessions_mod.change_local_password(
                user_id=uid,
                current_password="InitialPassword123!",
                new_password="NewPassword7890!",
            )
        assert e_cp_429.value.status_code == 429

    def test_extract_client_ip_uses_trusted_proxy_hops_and_falls_back_on_short_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = create_autospec(Request, instance=True)
        req.client = type("Client", (), {"host": "127.0.0.1"})()
        req.headers = Headers({"X-Forwarded-For": "1.2.3.4, 203.0.113.9"})

        # Default hops=0 -> ignores XFF completely, returns direct peer
        monkeypatch.delenv("DALTON_TRUSTED_PROXY_HOPS", raising=False)
        assert sessions_mod.extract_client_ip(req) == "127.0.0.1"

        # hops=1 behind trusted 127.0.0.1 -> takes 203.0.113.9 (ignores client-spoofed leftmost 1.2.3.4)
        monkeypatch.setenv("DALTON_TRUSTED_PROXY_HOPS", "1")
        monkeypatch.setenv("DALTON_TRUSTED_PROXIES", "127.0.0.1")
        assert sessions_mod.extract_client_ip(req) == "203.0.113.9"

        # Round 3 Item 5: hops > len(xff_parts) returns direct peer_host, NEVER client-supplied chain[0]!
        monkeypatch.setenv("DALTON_TRUSTED_PROXY_HOPS", "3")
        assert sessions_mod.extract_client_ip(req) == "127.0.0.1"

        # Untrusted peer ignores XFF even when hops=1
        monkeypatch.setenv("DALTON_TRUSTED_PROXY_HOPS", "1")
        req.client = type("Client", (), {"host": "198.51.100.7"})()
        assert sessions_mod.extract_client_ip(req) == "198.51.100.7"

    @pytest.mark.asyncio
    async def test_scrypt_queue_full_never_records_failed_attempts_or_locks_account(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeLocalAuthDB
    ) -> None:
        """Round 5 Item 1: scrypt slot is reserved BEFORE rate-limit registration — zero failures recorded & never re-arms expired lockouts."""
        client = _build_local_auth_client()
        boot = self._bootstrap_admin(client, fake_db)
        uid = boot["user"]["user_id"]
        acct_key = ("account", sessions_mod.account_attempt_key_hash(user_id=uid))
        ip_key = ("ip", sessions_mod.hash_attempt_key("10.9.9.1"))

        # Case A: Fresh account when scrypt queue is full -> 429 with 0 IP/account failure counts
        monkeypatch.setenv("DALTON_SCRYPT_MAX_WORKERS", "1")
        monkeypatch.setenv("DALTON_SCRYPT_MAX_QUEUE", "0")
        monkeypatch.setattr(passwords_mod, "_scrypt_inflight", 1)
        for _ in range(6):
            with pytest.raises(HTTPException) as exc_pre:
                await sessions_mod.authenticate_local_login(
                    identifier="admin@example.com",
                    password="InitialPassword123!",
                    client_ip="10.9.9.1",
                )
            assert exc_pre.value.status_code == 429
        assert acct_key not in fake_db.auth_attempts
        assert ip_key not in fake_db.auth_attempts
        assert fake_db.credentials[uid]["failed_attempts"] == 0

        # Same for change-password when queue is full -> 429 with 0 DB writes
        with pytest.raises(HTTPException) as exc_cp_full:
            await sessions_mod.change_local_password(
                user_id=uid,
                current_password="InitialPassword123!",
                new_password="QueuedNewPassword123!",
                client_ip="10.9.9.1",
            )
        assert exc_cp_full.value.status_code == 429
        assert acct_key not in fake_db.auth_attempts
        assert ip_key not in fake_db.auth_attempts
        monkeypatch.setattr(passwords_mod, "_scrypt_inflight", 0)

        # Case B (grok Med 1): Account ALREADY has 5 failed attempts in-window and its 30s lock
        # JUST EXPIRED. While another concurrent login occupies the sole scrypt slot, a valid
        # login gets 429 WITHOUT incrementing attempts and WITHOUT re-arming locked_until!
        t0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
        expired_lock = t0 - timedelta(seconds=1)
        fake_db.auth_attempts[acct_key] = {
            "dimension": "account",
            "key_hash": acct_key[1],
            "failed_attempts": 5,
            "window_started_at": t0 - timedelta(minutes=2),
            "last_failed_at": t0 - timedelta(seconds=31),
            "locked_until": expired_lock,
            "updated_at": t0 - timedelta(seconds=31),
        }
        fake_db.credentials[uid]["failed_attempts"] = 5
        fake_db.credentials[uid]["locked_until"] = expired_lock

        slot_entered = asyncio.Event()
        release_slot = asyncio.Event()
        orig_verify = sessions_mod.verify_password_async

        async def _slow_first_verify(pw: str, h: str, *, slot_held: bool = False) -> bool:
            slot_entered.set()
            await release_slot.wait()
            return await orig_verify(pw, h, slot_held=slot_held)

        monkeypatch.setattr(
            sessions_mod,
            "verify_password_async",
            create_autospec(orig_verify, side_effect=_slow_first_verify),
        )

        # Start slow login on an unknown user to legitimately occupy the sole scrypt slot
        occupier = asyncio.create_task(
            sessions_mod.authenticate_local_login(
                identifier="other-user@example.com",
                password="SomePassword123!",
                client_ip="10.9.9.9",
                now=t0,
            )
        )
        await asyncio.wait_for(slot_entered.wait(), timeout=2.0)

        # While the sole slot is held, valid admin login hits 429 BEFORE touching DB!
        with pytest.raises(HTTPException) as exc_busy:
            await sessions_mod.authenticate_local_login(
                identifier="admin@example.com",
                password="InitialPassword123!",
                client_ip="10.9.9.1",
                now=t0,
            )
        assert exc_busy.value.status_code == 429
        # Count stayed at 5 and expired lock was NOT re-armed!
        assert fake_db.auth_attempts[acct_key]["failed_attempts"] == 5
        assert fake_db.auth_attempts[acct_key]["locked_until"] == expired_lock
        assert fake_db.credentials[uid]["failed_attempts"] == 5
        assert fake_db.credentials[uid]["locked_until"] == expired_lock

        # Release the slow occupier -> slot frees up -> admin logs in with valid password immediately!
        release_slot.set()
        with pytest.raises(HTTPException) as exc_occ:
            await occupier
        assert exc_occ.value.status_code == 401

        monkeypatch.setattr(sessions_mod, "verify_password_async", orig_verify)
        res = await sessions_mod.authenticate_local_login(
            identifier="admin@example.com",
            password="InitialPassword123!",
            client_ip="10.9.9.1",
            now=t0,
        )
        assert res.user_id == uid
        assert acct_key not in fake_db.auth_attempts
        assert fake_db.credentials[uid]["failed_attempts"] == 0
        assert fake_db.credentials[uid]["locked_until"] is None

    @pytest.mark.asyncio
    async def test_post_commit_ip_decrement_failure_preserves_login_and_change_password_success(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: FakeLocalAuthDB
    ) -> None:
        """Round 4 Item 2: failure in post-commit IP decrement logs warning and preserves 200 + tokens/cookie clear."""
        client = _build_local_auth_client()
        self._bootstrap_admin(client, fake_db)

        real_conn_factory = sessions_mod.get_db_connection
        fail_on_ip_select = False

        @asynccontextmanager
        async def _flaky_ip_release_conn(*args: Any, **kwargs: Any):
            async with real_conn_factory(*args, **kwargs) as conn:
                orig_cursor = conn.cursor

                @asynccontextmanager
                async def _wrapped_cursor(*c_args: Any, **c_kwargs: Any):
                    async with orig_cursor(*c_args, **c_kwargs) as cur:
                        orig_exec = cur.execute

                        async def _exec(q: Any, p: Any = None, **kw: Any):
                            if fail_on_ip_select and "FROM dalton_auth_attempts" in str(q) and "dimension = 'ip'" in str(q):
                                raise RuntimeError("transient pool disconnect during IP cleanup")
                            return await orig_exec(q, p, **kw)

                        cur.execute = _exec
                        yield cur

                conn.cursor = _wrapped_cursor
                yield conn

        monkeypatch.setattr(
            sessions_mod,
            "get_db_connection",
            create_autospec(_REAL_GET_DB_CONNECTION, side_effect=_flaky_ip_release_conn),
        )

        # 1. Login commits session; post-commit IP decrement fails -> STILL returns 200 + access token + cookie!
        fail_on_ip_select = True
        r_login = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"email": "admin@example.com", "password": "InitialPassword123!"},
        )
        assert r_login.status_code == 200
        tok = r_login.json()["access_token"]
        assert tok
        assert REFRESH_COOKIE_NAME in r_login.cookies

        # 2. Change-password commits password + revokes sessions; post-commit IP decrement fails ->
        #    STILL returns 200 + clears refresh cookie!
        r_chg = client.post(
            "/api/v1/dalton/auth/local/change-password",
            json={
                "current_password": "InitialPassword123!",
                "new_password": "CommittedPass999!",
            },
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r_chg.status_code == 200
        assert r_chg.json()["status"] == "password_changed"
        assert "Max-Age=0" in r_chg.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_expired_historical_refresh_token_logout_succeeds_without_revoking_active_session(
        self, fake_db: FakeLocalAuthDB
    ) -> None:
        """Round 4 Item 3: expired historical refresh token does NOT revoke renewed session on logout (returns 200)."""
        t0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
        fake_db.db_clock = t0
        client = _build_local_auth_client()
        self._bootstrap_admin(client, fake_db)

        r_login = client.post(
            "/api/v1/dalton/auth/local/login",
            json={"identifier": "admin@example.com", "password": "InitialPassword123!"},
        )
        assert r_login.status_code == 200
        ref1 = r_login.cookies[REFRESH_COOKIE_NAME]
        sid = decode_local_access_token(r_login.json()["access_token"]).session_id

        # Rotate ref1 -> ref2 at day 10 so session's new expires_at is day 24, while ref1's history expires_at is day 14.
        fake_db.db_clock = t0 + timedelta(days=10)
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_ref = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Origin": "http://testserver"},
        )
        assert r_ref.status_code == 200
        tok2 = r_ref.json()["access_token"]
        assert decode_local_access_token(tok2).session_id == sid

        # Advance clock to day 15: ref1's original expires_at (day 14) is EXPIRED, while session sid (day 24) is ACTIVE.
        fake_db.db_clock = t0 + timedelta(days=15)
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_logout = client.post(
            "/api/v1/dalton/auth/local/logout",
            headers={"Origin": "http://testserver"},
        )
        # Logout returns 200 and clears cookie, WITHOUT revoking the renewed active session!
        assert r_logout.status_code == 200
        assert r_logout.json() == {"status": "logged_out"}
        assert "Max-Age=0" in r_logout.headers.get("set-cookie", "")
        assert fake_db.sessions[sid]["revoked_at"] is None

        # Active access token tok2 continues to work!
        req = create_autospec(Request, instance=True)
        req.__len__.return_value = 1
        req.headers = Headers({})
        req.scope = {"state": {}}
        assert (
            await get_current_user_id(
                request=req,
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok2),
            )
            == r_login.json()["user"]["user_id"]
        )

        # Round 5 Item 3: Expiry-boundary concurrent refresh — old token rotated 1s before
        # its original expires_at, concurrent tab arrives 2s later (within 10s grace window)
        # while the renewed session is active -> returns retryable 409 WITHOUT clearing Cookie!
        fake_db.db_clock = t0 + timedelta(days=10, seconds=2)
        h1 = fake_db.refresh_history[sessions_mod.hash_refresh_token(ref1)]
        h1["expires_at"] = t0 + timedelta(days=10, seconds=1)
        client.cookies.set(REFRESH_COOKIE_NAME, ref1)
        r_ref_boundary = client.post(
            "/api/v1/dalton/auth/local/refresh",
            headers={"Origin": "http://testserver"},
        )
        assert r_ref_boundary.status_code == 409
        assert "Max-Age=0" not in r_ref_boundary.headers.get("set-cookie", "")
        assert fake_db.sessions[sid]["revoked_at"] is None
