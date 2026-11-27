"""Real PostgreSQL integration tests for warning-only startup check and strict CLI.

Uses the isolated ``_test`` database configured by ``tests/integration/conftest.py``
and ``tests/integration/dalton/conftest.py`` (never connects to production or reads
``.env``).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Disable `.env` loading before importing any `src.*` module.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import psycopg
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row

from src.dalton import storage_refs
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
            (uid, f"{uid}@test.local", "Storage Refs Test User"),
        )
        await cur.execute(
            """
            INSERT INTO workspaces (workspace_id, user_id, name, created_at, updated_at)
            VALUES (%s::uuid, %s, %s, NOW(), NOW())
            """,
            (ws_id, uid, f"refs-ws-{ws_id[:8]}"),
        )
    return uid, ws_id


@pytest.mark.asyncio
async def test_startup_warning_and_strict_check_real_pg(
    disable_dotenv_loading, test_db_pool, test_db_uri, dalton_schema, caplog
):
    """Verify startup warning-only behavior and strict full-table CLI verification on real Postgres."""
    uid: str | None = None
    ws_id: str | None = None
    inline_att_id = str(uuid.uuid4())
    obj_att_id = str(uuid.uuid4())
    ptr_file_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    resp_id = str(uuid.uuid4())
    serde = JsonPlusSerializer()

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
                # Clean leftover rows from prior runs before asserting clean baseline.
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM workspace_files WHERE blob_sha256 IS NOT NULL OR pack_sha256 IS NOT NULL"
                    )
                    await cur.execute("DELETE FROM workspace_file_blobs")
                    await cur.execute(
                        "DELETE FROM chat_attachments WHERE object_key IS NOT NULL"
                    )

                # 1. Startup advisory probes execute with zero SQL errors and all tables present.
                outcomes, total_ms = await storage_refs.probe_main_bucket_references()
                assert total_ms >= 0.0
                assert len(outcomes) == len(storage_refs.STARTUP_MAIN_BUCKET_PROBES)
                for outcome in outcomes:
                    assert outcome.error is None, (
                        f"Startup probe {outcome.name} failed SQL execution: {outcome.error}"
                    )
                    assert outcome.skipped_missing_table is False, (
                        f"Startup probe {outcome.name} table missing: {outcome.table}"
                    )

                # 2. Clean DB + pre-upgrade inline chat_attachment -> startup clean, strict CLI exit 0.
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
                startup_clean = await storage_refs.warn_main_bucket_references_on_startup()
                assert all(not o.has_references for o in startup_clean)

                progress_buf = io.StringIO()
                strict_clean = await storage_refs.run_strict_main_bucket_check(
                    conn=conn,
                    batch_size=50,
                    progress_stream=progress_buf,
                    env={"STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage"},
                )
                assert strict_clean.exit_code == storage_refs.EXIT_CLEAN
                assert strict_clean.errors == []
                assert strict_clean.confirmed_hits == []

                # 3. Pointer row in workspace_files -> startup logs WARNING only (never raises),
                #    strict CLI returns exit code 1 (stop-on-hit).
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
                caplog.clear()
                with caplog.at_level(logging.WARNING, logger="src.dalton.storage_refs"):
                    startup_warn = await storage_refs.warn_main_bucket_references_on_startup()
                ws_out = next(o for o in startup_warn if o.name == "workspace_files")
                assert ws_out.has_references is True
                assert ws_out.hit_count >= 1
                warn_text = "\n".join(
                    r.message for r in caplog.records if r.levelno == logging.WARNING
                )
                assert "public.workspace_files" in warn_text
                assert "ptr.bin" not in warn_text
                assert digest not in warn_text

                strict_ws = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50
                )
                assert strict_ws.exit_code == storage_refs.EXIT_REFERENCES_FOUND
                assert "workspace_files" in strict_ws.confirmed_hits

                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM workspace_files WHERE workspace_file_id = %s::uuid",
                        (ptr_file_id,),
                    )

                # 4. Object-backed chat_attachments row -> startup warns (no raise), strict CLI exits 1.
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
                startup_att = await storage_refs.warn_main_bucket_references_on_startup()
                assert any(o.name == "chat_attachments" and o.has_references for o in startup_att)
                strict_att = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50
                )
                assert strict_att.exit_code == storage_refs.EXIT_REFERENCES_FOUND
                assert "chat_attachments" in strict_att.confirmed_hits

                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM chat_attachments WHERE attachment_id = %s::uuid",
                        (obj_att_id,),
                    )

                # 5. Site-shaped avatar_url without recorded object key -> suspected WARNING only, exit 0.
                site_env = {
                    "STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage",
                    "STORAGE_KEY_PREFIX": "staging",
                }
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE users SET avatar_url = %s WHERE user_id = %s",
                        (f"https://app.example.com/storage/staging/avatars/{uid}.png", uid),
                    )
                caplog.clear()
                with caplog.at_level(logging.WARNING, logger="src.dalton.storage_refs"):
                    strict_avatar = await storage_refs.run_strict_main_bucket_check(
                        conn=conn, batch_size=50, env=site_env
                    )
                assert strict_avatar.exit_code == storage_refs.EXIT_CLEAN
                assert strict_avatar.confirmed_hits == []
                assert strict_avatar.suspected_warnings.get("users_avatars", 0) >= 1

                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE users SET avatar_url = NULL WHERE user_id = %s",
                        (uid,),
                    )

                # 6. Plain message text mentioning data_ref & widgets/ in checkpoint_writes + turn_slices
                #    does NOT false-positive (exit 0); real ShowWidget ToolMessage artifact exits 1.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO conversation_threads (
                            conversation_thread_id, workspace_id, thread_index,
                            current_status, created_at, updated_at
                        )
                        VALUES (%s::uuid, %s::uuid, 1, 'idle', NOW(), NOW())
                        """,
                        (thread_id, ws_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO conversation_responses (
                            conversation_response_id, conversation_thread_id,
                            turn_index, status, created_at
                        )
                        VALUES (%s::uuid, %s::uuid, 1, 'completed', NOW())
                        """,
                        (resp_id, thread_id),
                    )
                    plain_msg = HumanMessage(
                        content='Please inspect {"data_ref":{"key":"widgets/t1/fake.json"}} in code'
                    )
                    p_type, p_blob = serde.dumps_typed([plain_msg])
                    await cur.execute(
                        """
                        INSERT INTO checkpoint_writes (
                            thread_id, checkpoint_ns, checkpoint_id, task_id,
                            idx, channel, type, blob
                        )
                        VALUES (%s, '', 'cp-plain', 't-plain', 0, 'messages', %s, %s)
                        """,
                        (thread_id, p_type, p_blob),
                    )
                    plain_line = "message_chunk\t\t" + json.dumps(
                        {"content": '{"data_ref":{"key":"widgets/t1/fake.json"}}'}
                    )
                    await cur.execute(
                        """
                        INSERT INTO turn_slices (
                            conversation_response_id, conversation_thread_id,
                            turn_index, lines, slice_version
                        )
                        VALUES (%s::uuid, %s::uuid, 1, %s, 1)
                        """,
                        (resp_id, thread_id, plain_line),
                    )

                strict_plain = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50
                )
                assert strict_plain.exit_code == storage_refs.EXIT_CLEAN
                assert strict_plain.confirmed_hits == []

                # Now insert a real ShowWidget ToolMessage with data_ref.key -> exit 1!
                widget_msg = ToolMessage(
                    content="Widget rendered: chart",
                    tool_call_id="call_widget_1",
                    name="ShowWidget",
                    artifact={
                        "type": "html_widget",
                        "html": "<div>chart</div>",
                        "data_ref": {
                            "key": f"widgets/{thread_id}/abc123.json",
                            "sha256": "abc123",
                            "size": 4096,
                        },
                    },
                )
                w_type, w_blob = serde.dumps_typed([widget_msg])
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO checkpoint_writes (
                            thread_id, checkpoint_ns, checkpoint_id, task_id,
                            idx, channel, type, blob
                        )
                        VALUES (%s, '', 'cp-widget', 't-widget', 0, 'messages', %s, %s)
                        """,
                        (thread_id, w_type, w_blob),
                    )
                strict_widget = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50
                )
                assert strict_widget.exit_code == storage_refs.EXIT_REFERENCES_FOUND
                assert "widgets_checkpoint_writes" in strict_widget.confirmed_hits
        finally:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM turn_slices WHERE conversation_response_id = %s::uuid",
                    (resp_id,),
                )
                await cur.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s",
                    (thread_id,),
                )
                await cur.execute(
                    "DELETE FROM conversation_responses WHERE conversation_response_id = %s::uuid",
                    (resp_id,),
                )
                await cur.execute(
                    "DELETE FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                    (thread_id,),
                )
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
