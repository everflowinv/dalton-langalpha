"""Standalone PostgreSQL fixtures for self-contained Dalton billing SQL tests.

This directory lives outside `tests/integration/` and `tests/integration/dalton/`,
so pytest never loads `test_db_pool`, `dalton_schema`, or `cleanup_dalton_tables`
against the default `TEST_DB_*` database.

`DALTON_TEST_PG_DSN` exemption applies strictly inside this directory: tests
connect solely to `DALTON_TEST_PG_DSN`, create an isolated ephemeral schema,
and drop that ephemeral schema on teardown without touching `public` or any
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
    CREATE OR REPLACE FUNCTION dalton_cash_usd(
        tu JSONB,
        total_credits NUMERIC,
        infra_credits NUMERIC,
        rate NUMERIC
    )
    RETURNS NUMERIC LANGUAGE sql IMMUTABLE AS $$
      SELECT CASE
        WHEN tu IS NOT NULL AND jsonb_typeof(tu->'per_call_costs') = 'array' THEN
          COALESCE((
            SELECT SUM((c->>'cost')::numeric)
            FROM jsonb_array_elements(tu->'per_call_costs') c
            WHERE COALESCE(c->>'billing_type', 'platform') <> 'oauth'
          ), 0)
          + COALESCE(infra_credits, 0) / rate
        ELSE COALESCE(total_credits, 0) / rate
      END
    $$;
    """,
    """
    CREATE TABLE conversation_responses (
        conversation_response_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        conversation_thread_id   UUID NOT NULL,
        turn_index               INTEGER NOT NULL DEFAULT 0,
        run_seq                  BIGINT NOT NULL DEFAULT 1,
        status                   VARCHAR(50) NOT NULL
                                 CHECK (status IN ('in_progress','interrupted','completed','error','cancelled')),
        interrupt_reason         VARCHAR(100),
        cancel_requested_at      TIMESTAMPTZ,
        metadata                 JSONB DEFAULT '{}'::jsonb,
        warnings                 TEXT[],
        errors                   TEXT[],
        execution_time           DOUBLE PRECISION,
        created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        in_flight_credits        NUMERIC NOT NULL DEFAULT 0,
        user_id                  VARCHAR(255),
        usage_settled_at         TIMESTAMPTZ,
        replay_facts             JSONB
    )
    """,
    """
    CREATE TABLE conversation_usages (
        conversation_usage_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        conversation_response_id UUID NOT NULL,
        user_id                  VARCHAR(255) NOT NULL,
        conversation_thread_id   UUID NOT NULL,
        workspace_id             UUID NOT NULL,
        msg_type                 VARCHAR(50) NOT NULL DEFAULT 'ptc'
                                 CHECK (msg_type IN ('flash','ptc','interrupted','task')),
        status                   VARCHAR(50) NOT NULL DEFAULT 'completed'
                                 CHECK (status IN ('in_progress','interrupted','completed','error','cancelled')),
        token_usage              JSONB,
        infrastructure_usage     JSONB DEFAULT '{}'::jsonb,
        token_credits            DECIMAL(10, 6) NOT NULL DEFAULT 0,
        infrastructure_credits   DECIMAL(10, 6) NOT NULL DEFAULT 0,
        total_credits            DECIMAL(10, 6) NOT NULL DEFAULT 0,
        is_byok                  BOOLEAN NOT NULL DEFAULT FALSE,
        created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE subagent_runs (
        task_run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        thread_id          UUID NOT NULL,
        task_id            TEXT NOT NULL,
        parent_run_id      UUID REFERENCES conversation_responses(conversation_response_id) ON DELETE CASCADE,
        predecessor_run_id UUID REFERENCES subagent_runs(task_run_id) ON DELETE SET NULL,
        cause              TEXT NOT NULL DEFAULT 'init' CHECK (cause IN ('init', 'resume', 'hitl')),
        status             TEXT NOT NULL DEFAULT 'in_progress'
                           CHECK (status IN ('in_progress','completed','interrupted','error','cancelled')),
        replay_facts       JSONB,
        started_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finalized_at       TIMESTAMPTZ,
        in_flight_credits  NUMERIC NOT NULL DEFAULT 0,
        user_id            VARCHAR(255),
        usage_settled_at   TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE dalton_budget_reservations (
        reservation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id          VARCHAR(255) NOT NULL,
        budget_day       DATE NOT NULL,
        run_ref          UUID NOT NULL UNIQUE,
        thread_id        UUID NULL,
        workspace_id     UUID NULL,
        reserved_usd     NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
        observed_usd     NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (observed_usd >= 0),
        task_cap_usd     NUMERIC(12,6) NULL,
        state            TEXT NOT NULL DEFAULT 'open'
                         CHECK (state IN ('open','released','settled','expired')),
        generation       INTEGER NOT NULL DEFAULT 1,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        renewed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at       TIMESTAMPTZ NOT NULL,
        released_at      TIMESTAMPTZ NULL,
        settled_at       TIMESTAMPTZ NULL,
        settled_usd      NUMERIC(12,6) NULL,
        settle_source    TEXT NULL CHECK (settle_source IN ('usage','conservative'))
    )
    """,
)


def _require_dsn() -> str:
    dsn = os.environ.get("DALTON_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip(
            "DALTON_TEST_PG_DSN is not set; skipping standalone PostgreSQL billing tests"
        )
    return dsn


@pytest_asyncio.fixture
async def pg_conn():
    """Isolated ephemeral-schema connection against `DALTON_TEST_PG_DSN`."""
    dsn = _require_dsn()
    schema_name = f"billing_gaps_pg_{uuid.uuid4().hex[:12]}"
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
