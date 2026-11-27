"""Real PostgreSQL integration tests for the storage-off startup reference gate (§6.1 #2).

Uses the isolated ``_test`` database configured by ``tests/integration/conftest.py``
and ``tests/integration/dalton/conftest.py`` (never connects to production or reads
``.env``).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Disable `.env` loading before importing any `src.*` module.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import psycopg
import pytest
from psycopg.rows import dict_row

from src.dalton import storage_refs
from src.dalton.profile import DaltonStartupConfigError
from src.server.database import pool as db_pool_mod

pytestmark = [pytest.mark.integration]


async def _assert_connected_to_test_db(conn: psycopg.AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT current_database()")
        row = await cur.fetchone()
    db_name = row["current_database"] if isinstance(row, dict) else row[0]
    assert isinstance(db_name, str) and db_name.endswith("_test"), (
        f"Refusing to run against non-test database: {db_name!r}"
    )


async def _seed_user_and_workspace(
    conn: psycopg.AsyncConnection,
) -> tuple[str, str]:
    uid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO users (user_id, email, name, created_at, updated_at)
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (user_id) DO NOTHING
            """,
            (uid, f"{uid}@test.local", "Gate Test User"),
        )
        await cur.execute(
            """
            INSERT INTO workspaces (workspace_id, user_id, name, created_at, updated_at)
            VALUES (%s::uuid, %s, %s, NOW(), NOW())
            """,
            (ws_id, uid, f"gate-ws-{ws_id[:8]}"),
        )
    return uid, ws_id


@pytest.mark.asyncio
async def test_all_main_bucket_probes_valid_sql_and_gate_behavior_real_pg(
    disable_dotenv_loading, test_db_pool, test_db_uri, dalton_schema
):
    """Run every probe SQL against migrated real Postgres and test gate pass/fail rows."""
    uid: str | None = None
    ws_id: str | None = None
    inline_att_id = str(uuid.uuid4())
    obj_att_id = str(uuid.uuid4())
    ptr_file_id = str(uuid.uuid4())

    async with await psycopg.AsyncConnection.connect(
        test_db_uri, autocommit=True, row_factory=dict_row
    ) as conn:
        await _assert_connected_to_test_db(conn)
        uid, ws_id = await _seed_user_and_workspace(conn)

        try:
            with (
                patch.object(db_pool_mod, "get_or_create_pool", return_value=test_db_pool),
                patch.object(storage_refs, "is_storage_enabled", return_value=False),
            ):
                # 1. Every registered probe executes with zero SQL errors and all tables exist.
                outcomes, total_ms = await storage_refs.probe_main_bucket_references()
                assert total_ms >= 0.0
                assert len(outcomes) == len(storage_refs.MAIN_BUCKET_CONSUMER_PROBES)
                for outcome in outcomes:
                    assert outcome.error is None, (
                        f"Probe {outcome.name} failed SQL execution: {outcome.error}"
                    )
                    assert outcome.skipped_missing_table is False, (
                        f"Probe {outcome.name} table missing on migrated DB: {outcome.table}"
                    )

                # Clean test tables from any prior test artifacts before checking empty-DB pass.
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM workspace_files WHERE blob_sha256 IS NOT NULL OR pack_sha256 IS NOT NULL"
                    )
                    await cur.execute("DELETE FROM workspace_file_blobs")
                    await cur.execute(
                        "DELETE FROM chat_attachments WHERE object_key IS NOT NULL"
                    )

                # 2. Clean DB passes startup gate.
                gate_outcomes = await storage_refs.enforce_storage_off_startup_gate()
                assert all(not o.has_references for o in gate_outcomes)

                # 3. Pre-upgrade inline chat_attachment (object_key IS NULL) still passes gate.
                expires = datetime.now(timezone.utc) + timedelta(hours=1)
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO chat_attachments (
                            attachment_id, user_id, name, mime_type, size, mode,
                            object_key, content, state, expires_at
                        )
                        VALUES (%s::uuid, %s, 'inline.txt', 'text/plain', 5, 'relay',
                                NULL, %s, 'ready', %s)
                        """,
                        (inline_att_id, uid, b"hello", expires),
                    )
                gate_outcomes_inline = await storage_refs.enforce_storage_off_startup_gate()
                assert all(not o.has_references for o in gate_outcomes_inline)

                # 4. Pointer row in workspace_files causes startup gate refusal.
                digest = hashlib.sha256(b"pointer-payload").hexdigest()
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO workspace_files (
                            workspace_file_id, workspace_id, file_path, file_name,
                            file_size, content_hash, blob_sha256, is_binary, kind
                        )
                        VALUES (%s::uuid, %s::uuid, 'ptr.bin', 'ptr.bin',
                                15, %s, %s, TRUE, 'file')
                        """,
                        (ptr_file_id, ws_id, digest, digest),
                    )
                with pytest.raises(DaltonStartupConfigError) as exc_ws:
                    await storage_refs.enforce_storage_off_startup_gate()
                assert storage_refs.STORAGE_OFF_GATE_ERROR_PREFIX in str(exc_ws.value)
                assert "workspace_files" in str(exc_ws.value)

                # Remove workspace_files pointer row so only chat_attachments is tested next.
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM workspace_files WHERE workspace_file_id = %s::uuid",
                        (ptr_file_id,),
                    )

                # 5. Object-backed chat_attachments row (object_key IS NOT NULL) refuses startup.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO chat_attachments (
                            attachment_id, user_id, name, mime_type, size, mode,
                            object_key, state, expires_at
                        )
                        VALUES (%s::uuid, %s, 'obj.pdf', 'application/pdf', 123, 'relay',
                                %s, 'ready', %s)
                        """,
                        (obj_att_id, uid, f"attachments/{obj_att_id}/obj.pdf", expires),
                    )
                with pytest.raises(DaltonStartupConfigError) as exc_att:
                    await storage_refs.enforce_storage_off_startup_gate()
                assert storage_refs.STORAGE_OFF_GATE_ERROR_PREFIX in str(exc_att.value)
                assert "chat_attachments" in str(exc_att.value)
        finally:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM chat_attachments WHERE attachment_id IN (%s::uuid, %s::uuid)",
                    (inline_att_id, obj_att_id),
                )
                await cur.execute(
                    "DELETE FROM workspace_files WHERE workspace_file_id = %s::uuid",
                    (ptr_file_id,),
                )
                if ws_id:
                    await cur.execute(
                        "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
                        (ws_id,),
                    )
                if uid:
                    await cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
