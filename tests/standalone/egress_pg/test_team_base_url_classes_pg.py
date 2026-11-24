"""Real-PostgreSQL integration tests for standalone core team base_url address class persistence.

Verifies ``record_saved_team_base_url_classes`` (both initial ``INSERT`` using
``jsonb_build_object(%s::text, %s::jsonb)`` and subsequent ``ON CONFLICT (user_id) DO UPDATE``)
and ``get_saved_team_endpoints`` against a real PostgreSQL instance.

Lives in its own standalone directory (``tests/standalone/egress_pg/``) with its own
``conftest.py``, completely isolated from ``tests/integration/conftest.py``. Skipped
automatically when ``DALTON_TEST_PG_DSN`` is unset.

Run against a throwaway PostgreSQL container:
    DALTON_TEST_PG_DSN="postgresql://postgres:postgres@127.0.0.1:5432/postgres" \\
        .venv/bin/python -m pytest -v -p no:cacheprovider -m integration \\
        tests/standalone/egress_pg/test_team_base_url_classes_pg.py
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import pytest
from psycopg.rows import dict_row

from src.dalton.llm import egress_policy
from src.server.database import pool as db_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_PG_DSN = os.environ.get("DALTON_TEST_PG_DSN", "").strip()

if not _PG_DSN:
    pytest.skip(
        "DALTON_TEST_PG_DSN is not set; skipping real-PostgreSQL egress policy integration tests",
        allow_module_level=True,
    )


async def _seed_holder(
    conn: Any,
    holder_id: str,
    *,
    api_key_base_urls: dict[str, str],
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO users (user_id, email, name, byok_enabled)
            VALUES (%s, %s, %s, TRUE)
            """,
            (holder_id, f"{holder_id}@example.com", "Holder Admin"),
        )
        await cur.execute(
            """
            INSERT INTO dalton_user_roles (user_id, tier)
            VALUES (%s, 'admin')
            """,
            (holder_id,),
        )
        await cur.execute(
            """
            INSERT INTO dalton_credential_holder (singleton_id, holder_user_id, updated_by)
            VALUES (1, %s, %s)
            """,
            (holder_id, holder_id),
        )
        for provider, base_url in api_key_base_urls.items():
            await cur.execute(
                """
                INSERT INTO user_api_keys (user_id, provider, api_key, base_url)
                VALUES (%s, %s, %s, %s)
                """,
                (holder_id, provider, b"\x00", base_url),
            )


def _bind_pool_to_conn(monkeypatch: pytest.MonkeyPatch, pg_conn: Any) -> None:
    @asynccontextmanager
    async def _use_pg_conn(conn: Any = None):
        yield conn if conn is not None else pg_conn

    monkeypatch.setattr(db_pool, "get_db_connection", _use_pg_conn)


async def _fetch_other_preference(conn: Any, user_id: str) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT other_preference FROM user_preferences WHERE user_id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
        return row["other_preference"] if row is not None else None


async def test_pg_holder_first_save_and_subsequent_updates_of_team_base_url_classes(
    pg_conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holder's first INSERT and subsequent ON CONFLICT UPDATEs all succeed in real PostgreSQL."""
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.delenv("DALTON_OPERATOR_LLM_ENDPOINTS", raising=False)
    _bind_pool_to_conn(monkeypatch, pg_conn)

    holder_id = "holder-pg-1"
    private_url = "http://team-gw.internal:8000/v1"
    public_url = "https://team-public.example.com/v1/"
    private_key = egress_policy.canonical_url_key(private_url)
    public_key = egress_policy.canonical_url_key(public_url)
    assert private_key == "http://team-gw.internal:8000/v1"
    assert public_key == "https://team-public.example.com:443/v1"

    await _seed_holder(
        pg_conn,
        holder_id,
        api_key_base_urls={"openai": private_url, "anthropic": public_url},
    )
    await pg_conn.commit()

    # 1. First save: no user_preferences row yet -> exercises INSERT + jsonb_build_object(%s::text, %s::jsonb)
    assert await _fetch_other_preference(pg_conn, holder_id) is None
    await egress_policy.record_saved_team_base_url_classes(
        {private_url: "private"},
        conn=pg_conn,
    )
    await pg_conn.commit()

    pref_after_insert = await _fetch_other_preference(pg_conn, holder_id)
    assert pref_after_insert == {
        egress_policy._TEAM_BASE_URL_CLASSES_PREF_KEY: {private_key: "private"}
    }

    # 2. Second save: row already exists -> exercises ON CONFLICT DO UPDATE (adds second URL)
    await egress_policy.record_saved_team_base_url_classes(
        {public_url: "public"},
        conn=pg_conn,
    )
    await pg_conn.commit()

    pref_after_add = await _fetch_other_preference(pg_conn, holder_id)
    assert pref_after_add == {
        egress_policy._TEAM_BASE_URL_CLASSES_PREF_KEY: {
            private_key: "private",
            public_key: "public",
        }
    }
    saved = await egress_policy.get_saved_team_endpoints()
    assert saved == {private_key: "private", public_key: "public"}

    # 3. Third save: updates an existing URL's class while preserving sibling entries
    await egress_policy.record_saved_team_base_url_classes(
        {private_url: "public"},
        conn=pg_conn,
    )
    await pg_conn.commit()

    pref_after_update = await _fetch_other_preference(pg_conn, holder_id)
    assert pref_after_update == {
        egress_policy._TEAM_BASE_URL_CLASSES_PREF_KEY: {
            private_key: "public",
            public_key: "public",
        }
    }
    saved_updated = await egress_policy.get_saved_team_endpoints()
    assert saved_updated == {private_key: "public", public_key: "public"}


async def test_pg_holder_update_preserves_unrelated_preferences_and_handles_null(
    pg_conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ON CONFLICT DO UPDATE preserves existing unrelated other_preference keys and handles NULL."""
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    _bind_pool_to_conn(monkeypatch, pg_conn)

    holder_id = "holder-pg-2"
    url = "https://gateway.example.com/v1"
    url_key = egress_policy.canonical_url_key(url)
    assert url_key == "https://gateway.example.com:443/v1"

    await _seed_holder(pg_conn, holder_id, api_key_base_urls={"openai": url})
    async with pg_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO user_preferences (user_id, other_preference)
            VALUES (%s, '{"theme": "dark"}'::jsonb)
            """,
            (holder_id,),
        )
    await pg_conn.commit()

    await egress_policy.record_saved_team_base_url_classes({url: "public"}, conn=pg_conn)
    await pg_conn.commit()

    pref = await _fetch_other_preference(pg_conn, holder_id)
    assert pref == {
        "theme": "dark",
        egress_policy._TEAM_BASE_URL_CLASSES_PREF_KEY: {url_key: "public"},
    }

    # Explicit NULL in other_preference column must also coalesce cleanly
    async with pg_conn.cursor() as cur:
        await cur.execute(
            "UPDATE user_preferences SET other_preference = NULL WHERE user_id = %s",
            (holder_id,),
        )
    await pg_conn.commit()

    await egress_policy.record_saved_team_base_url_classes({url: "private"}, conn=pg_conn)
    await pg_conn.commit()

    pref_after_null = await _fetch_other_preference(pg_conn, holder_id)
    assert pref_after_null == {
        egress_policy._TEAM_BASE_URL_CLASSES_PREF_KEY: {url_key: "private"}
    }
