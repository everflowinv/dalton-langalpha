"""The new-user defaults on the creation paths.

The seeded ``auto_approve_handoffs`` belongs to a row the same call inserted
(``xmax = 0`` on the upsert's insert side); a user who already existed — the
local-dev backfill, a login race — gets the blank seed, so a backfill never
flips their hand-offs to auto-approved. A timezone that is missing or blank
lands in ``Asia/Hong_Kong`` on the INSERT only; the conflict-update refresh
keeps reading the raw value, so a login never writes the default over an
existing row.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec, patch

import psycopg
import pytest

from src.server.database import user as user_db
from src.server.database.user import (
    DEFAULT_OTHER_PREFERENCE,
    DEFAULT_USER_TIMEZONE,
)

_ROW = {"user_id": "u1", "timezone": "x"}


def _cursor(fetches: list[Any]) -> tuple[Any, list[tuple[str, tuple]]]:
    """A spec'd cursor: ``fetches`` returned by successive ``fetchone`` calls,
    every ``execute`` recorded as ``(sql, params)``."""
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.__aenter__.return_value = cursor
    cursor.fetchone.side_effect = fetches
    calls: list[tuple[str, tuple]] = []

    async def _execute(sql, params=()):
        calls.append((sql, params))

    cursor.execute.side_effect = _execute
    return cursor, calls


def _run(fn, fetches: list[Any], **kw: Any) -> list[tuple[str, tuple]]:
    cursor, calls = _cursor(fetches)
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _get_db_connection(_conn=None):
        yield conn

    async def _call():
        with patch.object(user_db, "get_db_connection", _get_db_connection):
            await fn(**kw)

    asyncio.run(_call())
    return calls


def _seed(calls: list[tuple[str, tuple]]) -> tuple[str, tuple]:
    """The user_preferences seed statement of a creation."""
    seeds = [(sql, params) for sql, params in calls if "user_preferences" in sql]
    assert len(seeds) == 1
    return seeds[0]


# --- The preference default goes only to a row this call inserted --------------


def test_a_user_inserted_by_this_login_gets_the_seeded_default():
    calls = _run(
        user_db.create_user_from_auth,
        [{**_ROW, "inserted_now": True}],
        user_id="u1",
    )
    sql, params = _seed(calls)
    # The fresh row carries the default, and a conflicting blank row from a
    # concurrent login would get it merged in (DO UPDATE under WHERE true).
    assert "ON CONFLICT (user_id) DO UPDATE" in sql
    assert params[1].obj == DEFAULT_OTHER_PREFERENCE == {"auto_approve_handoffs": True}
    assert params[2].obj == DEFAULT_OTHER_PREFERENCE
    assert params[3] is True


def test_a_user_who_already_existed_gets_the_blank_seed():
    """The upsert refreshed an existing row (xmax ≠ 0): the values slot is
    blank and the conflict update never fires, so a backfill of an older
    account neither turns its hand-offs auto-approved nor writes at all."""
    calls = _run(
        user_db.create_user_from_auth,
        [{**_ROW, "inserted_now": False}],
        user_id="u1",
    )
    sql, params = _seed(calls)
    assert "ON CONFLICT (user_id) DO UPDATE" in sql
    assert params[1].obj == {}
    assert params[3] is False


def test_the_upsert_reports_which_side_it_landed_on():
    calls = _run(
        user_db.create_user_from_auth,
        [{**_ROW, "inserted_now": True}],
        user_id="u1",
    )
    assert any("(xmax = 0) AS inserted_now" in sql for sql, _ in calls)


def test_create_user_always_seeds_the_default():
    """``create_user`` is insert-only (it refuses an existing user), so every
    row it writes is new."""
    calls = _run(
        user_db.create_user,
        [None, _ROW],
        user_id="u2",
    )
    sql, params = _seed(calls)
    assert "other_preference" in sql
    assert params[-1].obj == DEFAULT_OTHER_PREFERENCE


# --- Concurrent first logins ----------------------------------------------------


class _MiniDb:
    """The two statements a first login writes, as tables: enough state to
    replay the insert race and see whose seed landed."""

    def __init__(self) -> None:
        self.users: dict[str, bool] = {}  # user_id -> inserted by its writer
        self.prefs: dict[str, dict | None] = {}
        self.log: list[tuple] = []

    def user_upsert(self, user_id: str) -> dict:
        inserted = user_id not in self.users
        self.users[user_id] = inserted
        self.log.append(("users", user_id, inserted))
        return {**_ROW, "user_id": user_id, "inserted_now": inserted}

    def pref_seed(self, user_id: str, values: dict, merge: dict, do_merge: bool) -> None:
        self.log.append(("prefs", user_id, do_merge))
        if user_id not in self.prefs:
            self.prefs[user_id] = dict(values or {})
        elif do_merge:
            self.prefs[user_id] = {**(self.prefs[user_id] or {}), **merge}


def _login_cursor(db: _MiniDb, gate: asyncio.Event | None, reached: asyncio.Event | None):
    """A spec'd cursor against the mini DB; ``gate`` holds this login between
    its user upsert and its preference seed until the race has played out."""
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.__aenter__.return_value = cursor
    row: dict[str, Any] = {}

    async def _execute(sql, params=()):
        if "user_preferences" in sql:
            db.pref_seed(params[0], params[1].obj, params[2].obj, params[3])
        else:
            row.update(db.user_upsert(params[0]))

    async def _fetchone():
        if gate is not None:
            reached.set()
            await gate.wait()
        return dict(row)

    cursor.execute.side_effect = _execute
    cursor.fetchone.side_effect = _fetchone
    return cursor


def _login_conn(db: _MiniDb, cursor) -> Any:
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _transaction():
        db.log.append(("tx_begin",))
        yield
        db.log.append(("tx_commit",))

    conn.transaction.side_effect = _transaction
    return conn


@pytest.mark.asyncio
async def test_a_concurrent_blank_seed_still_leaves_the_default_in_place():
    """Two first logins race. The loser's upsert reports ``inserted_now``
    false and its blank preference row lands first; the winner — the request
    that actually inserted the user — must still end with the default, and
    its insert and seed must have committed as one transaction."""
    db = _MiniDb()
    a_may_seed = asyncio.Event()
    a_at_fetch = asyncio.Event()
    conns = [
        _login_conn(db, _login_cursor(db, a_may_seed, a_at_fetch)),
        _login_conn(db, _login_cursor(db, None, None)),
    ]

    @asynccontextmanager
    async def _get_db_connection(_conn=None):
        yield conns.pop(0)

    with patch.object(user_db, "get_db_connection", _get_db_connection):
        # A: inserts the user, then waits at its fetchone — its seed not run.
        first = asyncio.create_task(user_db.create_user_from_auth(user_id="u1"))
        await a_at_fetch.wait()
        # B: full login while A waits; inserts nothing, seeds a blank row.
        await user_db.create_user_from_auth(user_id="u1")
        a_may_seed.set()
        await first

    assert db.prefs["u1"] == {"auto_approve_handoffs": True}
    # Exactly one writer inserted the user, and it is the one whose seed merged.
    users_events = [e for e in db.log if e[0] == "users"]
    assert [(e[1], e[2]) for e in users_events] == [("u1", True), ("u1", False)]
    prefs_events = [e for e in db.log if e[0] == "prefs"]
    assert [(e[2]) for e in prefs_events] == [False, True]
    # A's transaction wrapped both of its statements: it opened before its
    # user insert and closed after its seed, all around B's whole login.
    assert db.log[0] == ("tx_begin",)
    assert db.log[-1] == ("tx_commit",)
    assert db.log.index(("users", "u1", True)) == 1
    assert db.log.index(("prefs", "u1", True)) == len(db.log) - 2


# --- The timezone default, on the INSERT only ----------------------------------


def _user_upsert(calls) -> tuple[str, tuple]:
    upserts = [
        (sql, params)
        for sql, params in calls
        if "INSERT INTO users" in sql and "ON CONFLICT (user_id) DO UPDATE" in sql
    ]
    assert len(upserts) == 1
    return upserts[0]


def test_a_blank_timezone_lands_in_the_default_on_the_insert():
    calls = _run(
        user_db.create_user_from_auth,
        [{**_ROW, "inserted_now": True}],
        user_id="u1",
        timezone="   ",
    )
    sql, params = _user_upsert(calls)
    # (…, timezone, default, locale, timezone-for-the-refresh): blank was
    # normalized to None, so the INSERT slot falls back and the refresh keeps
    # whatever the row holds.
    assert params[5] is None
    assert params[6] == DEFAULT_USER_TIMEZONE == "Asia/Hong_Kong"
    assert params[8] is None
    assert "COALESCE(NULLIF(%s, ''), %s)" in sql


def test_a_browser_timezone_is_kept_verbatim():
    calls = _run(
        user_db.create_user_from_auth,
        [{**_ROW, "inserted_now": True}],
        user_id="u1",
        timezone="America/New_York",
    )
    _, params = _user_upsert(calls)
    assert params[5] == "America/New_York"
    assert params[8] == "America/New_York"


def test_create_user_defaults_a_missing_timezone_in_python():
    calls = _run(user_db.create_user, [None, _ROW], user_id="u2", timezone=None)
    inserts = [(sql, params) for sql, params in calls if "INSERT INTO users" in sql]
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[4] == DEFAULT_USER_TIMEZONE


# --- The first sync answers with the seeded preferences ------------------------


@pytest.mark.asyncio
async def test_a_first_sync_returns_the_seeded_preferences(monkeypatch):
    """A brand-new user's sync response carries the preferences row, so the
    client never caches "no preferences" over the hand-off default."""
    from datetime import datetime, timezone as tz

    from src.server.app import users as users_app
    from src.server.auth.jwt_bearer import AuthInfo

    now = datetime.now(tz.utc)
    user_row = {
        "user_id": "u1",
        "email": "a@dalton.org",
        "name": None,
        "avatar_url": None,
        "timezone": None,
        "locale": None,
        "onboarding_completed": False,
        "personalization_completed": False,
        "has_api_key": False,
        "has_oauth_token": False,
        "auth_provider": "email",
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
    }
    seeded = {
        "user_preference_id": "22222222-2222-4222-8222-222222222222",
        "user_id": "u1",
        "risk_preference": None,
        "investment_preference": None,
        "agent_preference": None,
        "other_preference": dict(DEFAULT_OTHER_PREFERENCE),
        "model_preference": None,
        "created_at": now,
        "updated_at": now,
    }
    monkeypatch.setattr(
        users_app, "db_get_user", create_autospec(users_app.db_get_user, return_value=None)
    )
    monkeypatch.setattr(
        users_app,
        "find_user_by_email",
        create_autospec(users_app.find_user_by_email, return_value=None),
    )
    monkeypatch.setattr(
        users_app,
        "create_user_from_auth",
        create_autospec(users_app.create_user_from_auth, return_value=user_row),
    )
    monkeypatch.setattr(
        users_app,
        "get_user_with_preferences",
        create_autospec(
            users_app.get_user_with_preferences,
            return_value={"user": user_row, "preferences": seeded},
        ),
    )

    resp = await users_app.sync_user(
        body=users_app.AuthSyncRequest(email="a@dalton.org", name="A"),
        auth_info=AuthInfo(user_id="u1", auth_provider="email"),
    )

    assert resp.preferences is not None
    assert resp.preferences.other_preference is not None
    assert resp.preferences.other_preference.auto_approve_handoffs is True
