"""Standalone PostgreSQL fixtures for self-contained Dalton egress policy SQL tests.

This directory lives outside ``tests/integration/`` and ``tests/integration/dalton/``,
so pytest never loads ``test_db_pool``, ``dalton_schema``, or ``cleanup_dalton_tables``
against the default ``TEST_DB_*`` database.

``DALTON_TEST_PG_DSN`` exemption applies strictly inside this directory: tests
connect solely to ``DALTON_TEST_PG_DSN``, create an isolated ephemeral schema,
and drop that ephemeral schema on teardown without touching ``public`` or any
other database.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql as pg_sql

MINIMAL_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE users (
        user_id              VARCHAR(255) PRIMARY KEY,
        email                VARCHAR(255),
        name                 VARCHAR(255),
        byok_enabled         BOOLEAN NOT NULL DEFAULT FALSE,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE dalton_user_roles (
        user_id              TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
        tier                 TEXT NOT NULL CHECK (tier IN ('admin', 'internal', 'external')),
        granted_by           TEXT NULL,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE dalton_credential_holder (
        singleton_id         SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
        holder_user_id       TEXT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
        updated_by           TEXT NULL,
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE user_api_keys (
        user_id              VARCHAR(255) NOT NULL
                             REFERENCES users(user_id) ON DELETE CASCADE ON UPDATE CASCADE,
        provider             VARCHAR(50) NOT NULL,
        api_key              BYTEA NOT NULL,
        base_url             TEXT,
        created_at           TIMESTAMPTZ DEFAULT NOW(),
        updated_at           TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (user_id, provider)
    )
    """,
    """
    CREATE TABLE user_preferences (
        user_preference_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id              VARCHAR(255) UNIQUE NOT NULL
                             REFERENCES users(user_id) ON DELETE CASCADE,
        risk_preference      JSONB DEFAULT '{}'::jsonb,
        investment_preference JSONB DEFAULT '{}'::jsonb,
        agent_preference     JSONB DEFAULT '{}'::jsonb,
        other_preference     JSONB DEFAULT '{}'::jsonb,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
)


def _require_dsn() -> str:
    dsn = os.environ.get("DALTON_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip(
            "DALTON_TEST_PG_DSN is not set; skipping standalone PostgreSQL egress tests"
        )
    return dsn


@pytest_asyncio.fixture
async def pg_conn():
    """Isolated ephemeral-schema connection against ``DALTON_TEST_PG_DSN``."""
    dsn = _require_dsn()
    schema_name = f"egress_pg_{uuid.uuid4().hex[:12]}"
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=False)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema_name))
            )
            await cur.execute(
                pg_sql.SQL("SET search_path TO {}").format(
                    pg_sql.Identifier(schema_name)
                )
            )
            for ddl in MINIMAL_SCHEMA_DDL:
                await cur.execute(ddl)
        await conn.commit()
        yield conn
    finally:
        try:
            await conn.rollback()
            async with conn.cursor() as cur:
                await cur.execute(
                    pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        pg_sql.Identifier(schema_name)
                    )
                )
            await conn.commit()
        finally:
            await conn.close()
