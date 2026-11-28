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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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


async def _seed_completed_response(
    conn: psycopg.AsyncConnection,
    resp_id: str,
    thread_id: str,
    *,
    turn_index: int = 1,
    replay_facts: dict | None = None,
) -> None:
    """Insert a response row as ``in_progress`` then transition to ``completed``.

    Honors ``trg_responses_lifecycle_guard`` (migration 018), which requires
    every ``conversation_responses`` row to be inserted as ``in_progress``
    before updating to a terminal status.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO conversation_responses (
                conversation_response_id, conversation_thread_id,
                turn_index, status, created_at
            )
            VALUES (%s::uuid, %s::uuid, %s, 'in_progress', NOW())
            """,
            (resp_id, thread_id, turn_index),
        )
        if replay_facts is not None:
            await cur.execute(
                """
                UPDATE conversation_responses
                SET status = 'completed', replay_facts = %s::jsonb
                WHERE conversation_response_id = %s::uuid
                """,
                (json.dumps(replay_facts), resp_id),
            )
        else:
            await cur.execute(
                """
                UPDATE conversation_responses
                SET status = 'completed'
                WHERE conversation_response_id = %s::uuid
                """,
                (resp_id,),
            )


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
                # App logging config may disable loggers created before it ran (dictConfig).
                logging.getLogger("src.dalton.storage_refs").disabled = False
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
                # App logging config may disable loggers created before it ran (dictConfig).
                logging.getLogger("src.dalton.storage_refs").disabled = False
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
                        VALUES (%s::uuid, %s::uuid, 1, 'completed', NOW(), NOW())
                        """,
                        (thread_id, ws_id),
                    )
                await _seed_completed_response(conn, resp_id, thread_id, turn_index=1)
                async with conn.cursor() as cur:
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
                            input_checkpoint_id, tail_checkpoint_id,
                            slice_key, slice_codec, slice, lines_key, lines
                        )
                        VALUES (%s::uuid, %s::uuid, 'cp-in', 'cp-tail', 'k1', %s, %s, 'lk1', %s)
                        """,
                        (resp_id, thread_id, p_type, p_blob, plain_line),
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


@pytest.mark.asyncio
async def test_r4_startup_union_captured_images_and_malformed_avatar_real_pg(
    disable_dotenv_loading, test_db_pool, test_db_uri, dalton_schema
):
    """Real PostgreSQL verification for round-4 & round-5 fixes:
    1. ``workspace_files`` startup UNION probe executes cleanly with valid blob-only and pack-only
       pointer rows (honoring ``chk_workspace_files_one_pointer``).
    2. ``check --strict`` detects response images stored ONLY in checkpoint ``captured_images``,
       legacy ``image_capture`` UI records, and ``replay_facts.legacy.verbatim`` (exit 1),
       while ignoring external URLs that merely contain ``response-images/`` in query strings (exit 0).
    3. Malformed ``users.avatar_url`` does not fail strict audit with exit 2.
    """
    uid: str | None = None
    ws_id: str | None = None
    f_blob_id = str(uuid.uuid4())
    f_pack_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    resp_id = str(uuid.uuid4())
    serde = JsonPlusSerializer()
    site_env = {"STORAGE_PUBLIC_URL_BASE": "https://app.example.com/storage"}

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
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM workspace_files WHERE blob_sha256 IS NOT NULL OR pack_sha256 IS NOT NULL"
                    )
                    await cur.execute("DELETE FROM workspace_file_blobs")

                # 1. Startup UNION probe on real PG with two valid mutually-exclusive pointer rows
                d1 = hashlib.sha256(b"blob-only").hexdigest()
                d2 = hashlib.sha256(b"pack-only").hexdigest()
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO workspace_files (
                            workspace_file_id, workspace_id, file_path, file_name,
                            file_size, content_hash, blob_sha256, pack_sha256, pack_offset,
                            is_binary, kind
                        )
                        VALUES
                            (%s::uuid, %s::uuid, 'a.bin', 'a.bin', 9, %s, %s, NULL, NULL, TRUE, 'file'),
                            (%s::uuid, %s::uuid, 'b.bin', 'b.bin', 9, %s, NULL, %s, 0, TRUE, 'file')
                        """,
                        (
                            f_blob_id, ws_id, d1, d1,
                            f_pack_id, ws_id, d2, d2,
                        ),
                    )

                outcomes, _ms = await storage_refs.probe_main_bucket_references(conn=conn)
                ws_out = next(o for o in outcomes if o.name == "workspace_files")
                assert ws_out.error is None, f"workspace_files UNION probe failed: {ws_out.error}"
                assert ws_out.has_references is True
                assert ws_out.hit_count == 2

                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM workspace_files WHERE workspace_file_id IN (%s::uuid, %s::uuid)",
                        (f_blob_id, f_pack_id),
                    )

                # 2. Malformed avatar_url does NOT make strict audit return exit 2
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE users SET avatar_url = %s WHERE user_id = %s",
                        ("https://[invalid/avatars/u.png", uid),
                    )
                malformed_report = await storage_refs.run_strict_main_bucket_check(
                    conn=conn,
                    batch_size=50,
                    env=site_env,
                )
                assert malformed_report.exit_code == storage_refs.EXIT_CLEAN
                assert malformed_report.errors == []

                # 3. External URL with response-images/ only in query string (checkpoint + verbatim) -> exit 0
                ext_cap_msg = AIMessage(
                    id="ai-pg-ext-query",
                    content="See ![ext](charts/ext.png)",
                    response_metadata={
                        "captured_images": {
                            "charts/ext.png": f"https://external.example.com/view?next=response-images/{thread_id}/0123456789abcdef/ext.png"
                        }
                    },
                )
                ext_type, ext_blob = serde.dumps_typed([ext_cap_msg])
                ext_verbatim_facts = {
                    "legacy": {
                        "v": 3,
                        "verbatim": [
                            {
                                "event": "message_chunk",
                                "data": {
                                    "id": "lc-ext-1",
                                    "agent": "main",
                                    "content_type": "text",
                                    "content": (
                                        f"![ext](https://external.example.com/page?img=response-images/{thread_id}/0123456789abcdef/ext.png) "
                                        f"![other-origin](https://other.example.com/storage/response-images/{thread_id}/0123456789abcdef/other.png)"
                                    ),
                                },
                            }
                        ],
                    }
                }
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO conversation_threads (
                            conversation_thread_id, workspace_id, thread_index,
                            current_status, created_at, updated_at
                        )
                        VALUES (%s::uuid, %s::uuid, 1, 'completed', NOW(), NOW())
                        """,
                        (thread_id, ws_id),
                    )
                await _seed_completed_response(
                    conn,
                    resp_id,
                    thread_id,
                    turn_index=1,
                    replay_facts=ext_verbatim_facts,
                )
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO checkpoint_blobs (
                            thread_id, checkpoint_ns, channel, version, type, blob
                        )
                        VALUES (%s, '', 'messages', 'v-ext-1', %s, %s)
                        """,
                        (thread_id, ext_type, ext_blob),
                    )
                ext_report = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50, env=site_env
                )
                assert ext_report.exit_code == storage_refs.EXIT_CLEAN
                assert ext_report.confirmed_hits == []

                # 4. Reference ONLY in replay_facts.legacy.verbatim (site origin + object path) -> exit 1
                real_verbatim_facts = {
                    "legacy": {
                        "v": 3,
                        "verbatim": [
                            {
                                "event": "message_chunk",
                                "data": {
                                    "id": "lc-hit-1",
                                    "agent": "main",
                                    "content_type": "text",
                                    "content": (
                                        f"![chart](https://app.example.com/storage/response-images/{thread_id}/0123456789abcdef/chart.png)"
                                    ),
                                },
                            }
                        ],
                    }
                }
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM checkpoint_blobs WHERE thread_id = %s",
                        (thread_id,),
                    )
                    await cur.execute(
                        """
                        UPDATE conversation_responses
                        SET replay_facts = %s::jsonb
                        WHERE conversation_response_id = %s::uuid
                        """,
                        (json.dumps(real_verbatim_facts), resp_id),
                    )
                verbatim_report = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50, env=site_env
                )
                assert verbatim_report.exit_code == storage_refs.EXIT_REFERENCES_FOUND
                assert "response_images_turns" in verbatim_report.confirmed_hits

                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE conversation_responses SET replay_facts = NULL WHERE conversation_response_id = %s::uuid",
                        (resp_id,),
                    )

                # 5. Reference ONLY in checkpoint_blobs AIMessage.response_metadata.captured_images -> exit 1
                cap_msg = AIMessage(
                    id="ai-pg-cap",
                    content="See ![plot](charts/plot.png)",
                    response_metadata={
                        "captured_images": {
                            "charts/plot.png": f"https://app.example.com/storage/response-images/{thread_id}/0123456789abcdef/plot.png"
                        }
                    },
                )
                c_type, c_blob = serde.dumps_typed([cap_msg])
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO checkpoint_blobs (
                            thread_id, checkpoint_ns, channel, version, type, blob
                        )
                        VALUES (%s, '', 'messages', 'v-cap-1', %s, %s)
                        """,
                        (thread_id, c_type, c_blob),
                    )
                cap_report = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50, env=site_env
                )
                assert cap_report.exit_code == storage_refs.EXIT_REFERENCES_FOUND
                assert "widgets_checkpoint_blobs" in cap_report.confirmed_hits

                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM checkpoint_blobs WHERE thread_id = %s",
                        (thread_id,),
                    )

                # 6. Reference ONLY in checkpoint_writes legacy image_capture UI record -> exit 1
                ui_type, ui_blob = serde.dumps_typed(
                    [
                        {
                            "id": "ui-pg-1",
                            "name": "image_capture",
                            "props": {
                                "path_to_url": {
                                    "charts/old.png": f"https://app.example.com/storage/response-images/{thread_id}/fedcba9876543210/old.png"
                                }
                            },
                        }
                    ]
                )
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO checkpoint_writes (
                            thread_id, checkpoint_ns, checkpoint_id, task_id,
                            idx, channel, type, blob
                        )
                        VALUES (%s, '', 'cp-ui-1', 't-ui-1', 0, 'ui', %s, %s)
                        """,
                        (thread_id, ui_type, ui_blob),
                    )
                ui_report = await storage_refs.run_strict_main_bucket_check(
                    conn=conn, batch_size=50, env=site_env
                )
                assert ui_report.exit_code == storage_refs.EXIT_REFERENCES_FOUND
                assert "widgets_checkpoint_writes" in ui_report.confirmed_hits
        finally:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s",
                    (thread_id,),
                )
                await cur.execute(
                    "DELETE FROM checkpoint_blobs WHERE thread_id = %s",
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
                    "DELETE FROM workspace_files WHERE workspace_file_id IN (%s::uuid, %s::uuid)",
                    (f_blob_id, f_pack_id),
                )
                if ws_id:
                    await cur.execute(
                        "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
                        (ws_id,),
                    )
                if uid:
                    await cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
