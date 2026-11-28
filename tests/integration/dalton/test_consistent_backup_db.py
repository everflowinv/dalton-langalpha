"""Real PostgreSQL integration tests for Batch 1 Step 3:
- GC session advisory locks + REPEATABLE READ pg_export_snapshot() + full-consumer
  ``storage_refs.enumerate_snapshot_refs()`` (including pack_sha256, inline chat_attachments,
  skill archive ``sha256:`` prefix, provenance_result_bodies, and dalton_transcription_jobs).
- Post-restore ``reconcile_restored_inflight_states()`` across ``drive_uploads``,
  ``chat_attachments``, ``dalton_chat_file_uploads``, and ``dalton_transcription_jobs``.

Runs exclusively against an isolated ``_test`` database configured via ``DALTON_TEST_PG_DSN``
or ``TEST_DB_HOST/TEST_DB_PORT/TEST_DB_NAME`` (enforced by ``tests/integration/dalton/conftest.py``).
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import psycopg
import pytest

from src.dalton import consistent_backup, storage_refs
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.storage_keys import storage_user_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_snapshot_advisory_locks_and_full_consumer_enumeration_db(
    test_db_uri: str, dalton_schema
) -> None:
    user_id = f"u-snap-{uuid4().hex[:8]}"
    ws_id = str(uuid4())
    pack_bytes = b"AABBCCDDEEFF"
    pack_sha = _sha(pack_bytes)
    blob_bytes = b"single-blob-bytes"
    blob_sha = _sha(blob_bytes)
    skill_bytes = b"PK\x03\x04skill-db-test"
    skill_sha = _sha(skill_bytes)
    chat_bytes = b"chat-upload-ready"
    chat_sha = _sha(chat_bytes)
    asr_bytes = b"asr-source-audio"
    asr_sha = _sha(asr_bytes)
    job_done_id = str(uuid4())
    job_inflight_id = str(uuid4())
    att_ready_id = str(uuid4())
    att_inline_id = str(uuid4())
    att_uploading_id = str(uuid4())

    conn = await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True)
    try:
        async with conn.cursor() as cur:
            # Seed user & workspace
            await cur.execute(
                "INSERT INTO users (user_id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{user_id}@example.test"),
            )
            await cur.execute(
                "INSERT INTO workspaces (workspace_id, user_id, name) VALUES (%s, %s, %s)",
                (ws_id, user_id, "snap-ws"),
            )
            # Seed workspace_file_blobs (blob + pack)
            await cur.execute(
                "INSERT INTO workspace_file_blobs (user_id, sha256, byte_len) VALUES (%s, %s, %s), (%s, %s, %s)",
                (user_id, blob_sha, len(blob_bytes), user_id, pack_sha, len(pack_bytes)),
            )
            # Seed workspace_files: blob row, pack member row, and inline row
            await cur.execute(
                """
                INSERT INTO workspace_files
                    (workspace_file_id, workspace_id, file_path, file_name, file_size, content_hash,
                     blob_sha256, pack_sha256, pack_offset, content_text, is_binary, kind)
                VALUES
                    (%s, %s, 'a.bin', 'a.bin', %s, %s, %s, NULL, NULL, NULL, TRUE, 'file'),
                    (%s, %s, 'b.txt', 'b.txt', 6, %s, NULL, %s, 0, NULL, FALSE, 'file'),
                    (%s, %s, 'c.txt', 'c.txt', 5, %s, NULL, NULL, NULL, 'hello', FALSE, 'file')
                """,
                (
                    str(uuid4()), ws_id, len(blob_bytes), blob_sha, blob_sha,
                    str(uuid4()), ws_id, _sha(pack_bytes[:6]), pack_sha,
                    str(uuid4()), ws_id, _sha(b"hello"),
                ),
            )
            # Seed user_skills with canonical sha256:<hex>
            await cur.execute(
                """
                INSERT INTO user_skills
                    (user_skill_id, user_id, name, content_hash, archive_key, archive_bytes)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()),
                    user_id,
                    f"sk-{uuid4().hex[:6]}",
                    f"sha256:{skill_sha}",
                    f"user-skills/{user_id}/{skill_sha}.zip",
                    len(skill_bytes),
                ),
            )
            # Seed provenance_result_bodies (mutable key)
            await cur.execute(
                """
                INSERT INTO provenance_result_bodies
                    (result_sha256, body_inline, object_key, byte_len)
                VALUES (%s, '', %s, 100)
                """,
                (f"prov-{uuid4().hex[:12]}", f"provenance/prov-{user_id}"),
            )
            # Seed chat_attachments: ready with key, pre-upgrade inline (object_key IS NULL), uploading
            await cur.execute(
                """
                INSERT INTO chat_attachments
                    (attachment_id, user_id, name, mime_type, size, mode, object_key, content, state, expires_at)
                VALUES
                    (%s, %s, 'r.pdf', 'application/pdf', 50, 'relay', %s, NULL, 'ready', NOW() + INTERVAL '1 day'),
                    (%s, %s, 'in.txt', 'text/plain', 5, 'relay', NULL, %s, 'ready', NOW() + INTERVAL '1 day'),
                    (%s, %s, 'up.bin', 'application/octet-stream', 20, 'relay', %s, NULL, 'uploading', NOW() + INTERVAL '1 day')
                """,
                (
                    att_ready_id, user_id, f"attachments/{att_ready_id}/r.pdf",
                    att_inline_id, user_id, b"hello",
                    att_uploading_id, user_id, f"attachments/{att_uploading_id}/up.bin",
                ),
            )
            # Seed dalton_chat_file_uploads (ready)
            await cur.execute(
                """
                INSERT INTO dalton_chat_file_uploads
                    (upload_id, user_id, workspace_id, filename, safe_filename, size_bytes,
                     part_size, part_count, object_key, store_upload_id, actual_sha256, state)
                VALUES (%s, %s, %s, 'd.csv', 'd.csv', %s, 1048576, 1, %s, 'su1', %s, 'ready')
                """,
                (
                    str(uuid4()),
                    user_id,
                    ws_id,
                    len(chat_bytes),
                    f"chat-uploads/{ws_id}/u1/d.csv",
                    chat_sha,
                ),
            )
            # Seed dalton_transcription_jobs (one succeeded, one transcribing)
            await cur.execute(
                """
                INSERT INTO dalton_transcription_jobs
                    (job_id, created_by_user_id, workspace_id, thread_id, source_object_key,
                     source_size_bytes, source_sha256, status, stage, usage, idempotency_key)
                VALUES
                    (%s, %s, %s, '', %s, %s, %s, 'succeeded', 'succeeded', %s::jsonb, %s),
                    (%s, %s, %s, '', %s, %s, %s, 'transcribing', 'transcribing', %s::jsonb, %s)
                """,
                (
                    job_done_id, user_id, ws_id, f"transcriptions/{ws_id}/{job_done_id}/source/a.mp3",
                    len(asr_bytes), asr_sha, json.dumps({"upload": {"completed": True}}), f"idemp-{job_done_id}",
                    job_inflight_id, user_id, ws_id, f"transcriptions/{ws_id}/{job_inflight_id}/source/b.mp3",
                    len(asr_bytes), asr_sha, json.dumps({"upload": {"completed": True}}), f"idemp-{job_inflight_id}",
                ),
            )

        # Acquire real GC session advisory locks + open real REPEATABLE READ exported snapshot
        await consistent_backup.acquire_gc_advisory_locks(conn)
        try:
            snap_id = await consistent_backup.begin_exported_snapshot(conn)
            assert snap_id
            inv = await storage_refs.enumerate_snapshot_refs(conn)
            await consistent_backup.commit_exported_snapshot(conn)
        finally:
            await consistent_backup.release_gc_advisory_locks(conn)

        assert job_done_id in inv.terminal_asr_job_ids
        assert job_done_id in inv.succeeded_asr_job_ids
        assert job_inflight_id not in inv.terminal_asr_job_ids
        att_ids = {r.row_id for r in inv.references if r.consumer == "chat_attachments"}
        assert att_ready_id in att_ids
        assert att_inline_id not in att_ids
        assert att_uploading_id not in att_ids
        assert inv.skipped_pending.get("chat_attachments", 0) >= 1

        skill_refs = [r for r in inv.references if r.consumer == "user_skills" and r.expected_sha256 == skill_sha]
        assert len(skill_refs) == 1
        assert skill_refs[0].check_mode == "strict"

        pack_refs = [r for r in inv.references if r.consumer == "workspace_files_pack" and r.expected_sha256 == pack_sha]
        assert len(pack_refs) == 1
        assert pack_refs[0].expected_size == len(pack_bytes)
        assert not [r for r in inv.references if r.consumer == "workspace_files_pack_member" and r.check_mode == "invalid"]
        assert storage_user_id(COMPANY_SYSTEM_USER_ID) == "sys_dalton-company"
    finally:
        await conn.close()


async def test_reconcile_restored_inflight_states_real_db(
    test_db_uri: str, dalton_schema
) -> None:
    user_id = f"u-rec-{uuid4().hex[:8]}"
    ws_id = str(uuid4())
    space_id = str(uuid4())
    drive_up_id = str(uuid4())
    att_up_id = str(uuid4())
    chat_match_id = str(uuid4())
    chat_miss_id = str(uuid4())
    chat_up_id = str(uuid4())
    asr_await_incomplete = str(uuid4())
    asr_await_complete = str(uuid4())
    asr_inflight = str(uuid4())
    asr_indeterminate = str(uuid4())
    asr_succeeded = str(uuid4())
    matched_sha = _sha(b"verified-assembled-chat-object")

    conn = await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO users (user_id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{user_id}@example.test"),
            )
            await cur.execute(
                "INSERT INTO drive_spaces (space_id, kind, user_id) VALUES (%s, 'personal', %s)",
                (space_id, user_id),
            )
            await cur.execute(
                """
                INSERT INTO drive_uploads
                    (upload_id, space_id, user_id, path, path_key, size, part_size, part_count,
                     actor, mode, object_key, store_upload_id, state, lease_until, lease_token, expires_at)
                VALUES (%s, %s, %s, 'f.bin', 'f.bin', 100, 100, 1, '{}'::jsonb, 'relay',
                        'uploads/u/1', 'su1', 'verifying', NOW() + INTERVAL '1 hour', %s, NOW() + INTERVAL '1 day')
                """,
                (drive_up_id, space_id, user_id, str(uuid4())),
            )
            await cur.execute(
                """
                INSERT INTO chat_attachments
                    (attachment_id, user_id, name, mime_type, size, mode, object_key, state, lease_until, expires_at)
                VALUES (%s, %s, 'u.bin', 'application/octet-stream', 10, 'relay',
                        'attachments/u/1', 'uploading', NOW() + INTERVAL '1 hour', NOW() + INTERVAL '1 day')
                """,
                (att_up_id, user_id),
            )
            await cur.execute(
                """
                INSERT INTO dalton_chat_file_uploads
                    (upload_id, user_id, workspace_id, filename, safe_filename, size_bytes,
                     part_size, part_count, object_key, store_upload_id, expected_sha256,
                     state, claim_token, lease_until, pending_claims)
                VALUES
                    (%s, %s, %s, 'm.bin', 'm.bin', 30, 1048576, 1, 'chat-uploads/ws/m/f.bin',
                     'su-m', %s, 'completing', 'tok-m', NOW() + INTERVAL '1 hour', '{"a":1}'::jsonb),
                    (%s, %s, %s, 'x.bin', 'x.bin', 40, 1048576, 1, 'chat-uploads/ws/x/f.bin',
                     'su-x', %s, 'completing', 'tok-x', NOW() + INTERVAL '1 hour', '{"b":1}'::jsonb),
                    (%s, %s, %s, 'u.bin', 'u.bin', 50, 1048576, 1, 'chat-uploads/ws/u/f.bin',
                     'su-u', NULL, 'uploading', 'tok-u', NOW() + INTERVAL '1 hour', '{"c":1}'::jsonb)
                """,
                (
                    chat_match_id, user_id, ws_id, matched_sha,
                    chat_miss_id, user_id, ws_id, "0" * 64,
                    chat_up_id, user_id, ws_id,
                ),
            )
            await cur.execute(
                """
                INSERT INTO dalton_transcription_jobs
                    (job_id, created_by_user_id, workspace_id, thread_id, source_object_key,
                     source_size_bytes, source_sha256, status, stage, usage, idempotency_key)
                VALUES
                    (%s, %s, %s, '', 'transcriptions/ws/1/source/a.mp3', 10, %s, 'awaiting_upload', 'awaiting_upload', '{}'::jsonb, %s),
                    (%s, %s, %s, '', 'transcriptions/ws/2/source/b.mp3', 10, %s, 'awaiting_upload', 'awaiting_upload', '{"upload":{"completed":true}}'::jsonb, %s),
                    (%s, %s, %s, '', 'transcriptions/ws/3/source/c.mp3', 10, %s, 'transcribing', 'transcribing', '{"upload":{"completed":true}}'::jsonb, %s),
                    (%s, %s, %s, '', 'transcriptions/ws/5/source/e.mp3', 10, %s, 'indeterminate', 'indeterminate', '{"upload":{"completed":true}}'::jsonb, %s),
                    (%s, %s, %s, '', 'transcriptions/ws/4/source/d.mp3', 10, %s, 'succeeded', 'succeeded', '{"upload":{"completed":true}}'::jsonb, %s)
                """,
                (
                    asr_await_incomplete, user_id, ws_id, "1" * 64, f"id-{asr_await_incomplete}",
                    asr_await_complete, user_id, ws_id, "2" * 64, f"id-{asr_await_complete}",
                    asr_inflight, user_id, ws_id, "3" * 64, f"id-{asr_inflight}",
                    asr_indeterminate, user_id, ws_id, "5" * 64, f"id-{asr_indeterminate}",
                    asr_succeeded, user_id, ws_id, "4" * 64, f"id-{asr_succeeded}",
                ),
            )

        summary = await consistent_backup.reconcile_restored_inflight_states(
            conn,
            inspect_chat_upload_object_fn=lambda k: (
                (30, matched_sha) if k == "chat-uploads/ws/m/f.bin" else None
            ),
        )
        assert summary.drive_uploads_aborted >= 1
        assert summary.chat_attachments_aborted >= 1
        assert summary.chat_uploads_promoted_ready >= 1
        assert summary.chat_uploads_aborted >= 2
        assert summary.transcriptions_cancelled >= 1
        assert summary.transcriptions_failed >= 2

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT state, cleaned, lease_until, lease_token FROM drive_uploads WHERE upload_id = %s",
                (drive_up_id,),
            )
            assert await cur.fetchone() == ("aborted", True, None, None)

            await cur.execute(
                "SELECT state, lease_until FROM chat_attachments WHERE attachment_id = %s",
                (att_up_id,),
            )
            assert await cur.fetchone() == ("aborted", None)

            await cur.execute(
                "SELECT state, actual_sha256, claim_token, lease_until, pending_claims "
                "FROM dalton_chat_file_uploads WHERE upload_id = %s",
                (chat_match_id,),
            )
            assert await cur.fetchone() == ("ready", matched_sha, None, None, {})

            await cur.execute(
                "SELECT state, claim_token, lease_until, pending_claims "
                "FROM dalton_chat_file_uploads WHERE upload_id = %s",
                (chat_miss_id,),
            )
            assert await cur.fetchone() == ("aborted", None, None, {})

            await cur.execute(
                "SELECT state, claim_token, lease_until, pending_claims "
                "FROM dalton_chat_file_uploads WHERE upload_id = %s",
                (chat_up_id,),
            )
            assert await cur.fetchone() == ("aborted", None, None, {})

            await cur.execute(
                "SELECT status, stage, completed_at IS NOT NULL FROM dalton_transcription_jobs WHERE job_id = %s",
                (asr_await_incomplete,),
            )
            assert await cur.fetchone() == ("cancelled", "cancelled", True)

            await cur.execute(
                "SELECT status FROM dalton_transcription_jobs WHERE job_id = %s",
                (asr_await_complete,),
            )
            assert await cur.fetchone() == ("awaiting_upload",)

            await cur.execute(
                "SELECT status, stage, error_message, completed_at IS NOT NULL "
                "FROM dalton_transcription_jobs WHERE job_id = %s",
                (asr_inflight,),
            )
            assert await cur.fetchone() == (
                "failed",
                "failed",
                consistent_backup.RESTORED_INFLIGHT_ERROR_MESSAGE,
                True,
            )

            await cur.execute(
                "SELECT status, stage, error_message, completed_at IS NOT NULL "
                "FROM dalton_transcription_jobs WHERE job_id = %s",
                (asr_indeterminate,),
            )
            assert await cur.fetchone() == (
                "failed",
                "failed",
                consistent_backup.RESTORED_INFLIGHT_ERROR_MESSAGE,
                True,
            )

            await cur.execute(
                "SELECT status FROM dalton_transcription_jobs WHERE job_id = %s",
                (asr_succeeded,),
            )
            assert await cur.fetchone() == ("succeeded",)
    finally:
        await conn.close()
