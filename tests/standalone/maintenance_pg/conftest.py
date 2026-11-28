"""Standalone PostgreSQL fixtures for Dalton maintenance gate advisory-lock SQL tests.

This directory lives outside ``tests/integration/`` and ``tests/integration/dalton/``,
so pytest never loads ``test_db_pool``, ``dalton_schema``, or ``cleanup_dalton_tables``
against the default ``TEST_DB_*`` database.

Tests connect solely to ``DALTON_TEST_PG_DSN``, create an isolated ephemeral schema,
and drop that ephemeral schema on teardown without touching ``public`` or any other
database.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql as pg_sql

MINIMAL_MAINTENANCE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE conversation_threads (
        conversation_thread_id TEXT PRIMARY KEY,
        workspace_id           TEXT NOT NULL,
        current_status         TEXT NOT NULL DEFAULT 'idle',
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE conversation_responses (
        conversation_response_id TEXT PRIMARY KEY,
        conversation_thread_id   TEXT NOT NULL REFERENCES conversation_threads(conversation_thread_id) ON DELETE CASCADE,
        status                   TEXT NOT NULL,
        created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
)


def require_pg_dsn() -> str:
    dsn = os.environ.get("DALTON_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip(
            "DALTON_TEST_PG_DSN is not set; skipping standalone PostgreSQL maintenance advisory-lock tests"
        )
    return dsn


@pytest_asyncio.fixture
async def maintenance_pg_env():
    """Provision an isolated ephemeral schema on ``DALTON_TEST_PG_DSN`` and return ``(dsn, schema_name)``."""
    dsn = require_pg_dsn()
    schema_name = f"maint_pg_{uuid.uuid4().hex[:12]}"
    admin_conn = await psycopg.AsyncConnection.connect(dsn, autocommit=False)
    try:
        async with admin_conn.cursor() as cur:
            await cur.execute(
                pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema_name))
            )
            await cur.execute(
                pg_sql.SQL("SET search_path TO {}").format(pg_sql.Identifier(schema_name))
            )
            for ddl in MINIMAL_MAINTENANCE_DDL:
                await cur.execute(ddl)
        await admin_conn.commit()
        yield dsn, schema_name
    finally:
        try:
            await admin_conn.rollback()
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        pg_sql.Identifier(schema_name)
                    )
                )
            await admin_conn.commit()
        finally:
            await admin_conn.close()
