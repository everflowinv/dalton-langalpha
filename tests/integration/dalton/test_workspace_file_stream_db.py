"""Real PostgreSQL integration tests for SQL inline slicing and hardened backfill.

Uses the isolated ``_test`` database configured by ``tests/integration/conftest.py``
and ``tests/integration/dalton/conftest.py`` (never connects to production or reads
``.env``).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from unittest.mock import create_autospec, patch

# Disable `.env` loading before importing any `src.*` module (covers direct
# module import as well as pytest collection before fixtures execute).
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import psycopg
import pytest
from psycopg.rows import dict_row

from src.dalton import company_identity
from src.dalton import wsfiles_backfill as bf_mod
from src.dalton.workspace_file_stream import (
    StoredStreamVersionChangedError,
    get_inline_file_byte_range,
    get_large_file_meta,
    resolve_stored_byte_range,
    stream_stored_file,
)
from src.server.database import pool as db_pool_mod
from src.server.database.blob_keys import blob_key
from src.utils import storage as storage_facade

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
    conn: psycopg.AsyncConnection, *, user_id: str | None = None
) -> tuple[str, str]:
    uid = user_id or str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO users (user_id, email, name, created_at, updated_at)
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (user_id) DO NOTHING
            """,
            (uid, f"{uid.replace(':', '_')}@test.local", "Test User"),
        )
        await cur.execute(
            """
            INSERT INTO workspaces (workspace_id, user_id, name, created_at, updated_at)
            VALUES (%s::uuid, %s, %s, NOW(), NOW())
            """,
            (ws_id, uid, f"ws-{ws_id[:8]}"),
        )
    return uid, ws_id


async def _insert_workspace_file(
    conn: psycopg.AsyncConnection,
    *,
    workspace_id: str,
    file_path: str,
    file_name: str,
    content_text: str | None = None,
    content_binary: bytes | None = None,
    blob_sha256: str | None = None,
    content_hash: str | None = None,
    file_size: int | None = None,
    is_binary: bool = False,
    mime_type: str = "application/octet-stream",
) -> str:
    fid = str(uuid.uuid4())
    if content_binary is not None:
        raw = content_binary
    elif content_text is not None:
        raw = content_text.encode("utf-8")
    else:
        raw = b""
    digest = content_hash if content_hash is not None else hashlib.sha256(raw).hexdigest()
    size = file_size if file_size is not None else len(raw)

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO workspace_files (
                workspace_file_id, workspace_id, file_path, file_name,
                file_size, content_hash, content_text, content_binary,
                blob_sha256, mime_type, is_binary, kind
            )
            VALUES (
                %s::uuid, %s::uuid, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, 'file'
            )
            """,
            (
                fid,
                workspace_id,
                file_path,
                file_name,
                size,
                digest,
                content_text,
                content_binary,
                blob_sha256,
                mime_type,
                is_binary,
            ),
        )
    return fid


@pytest.mark.asyncio
async def test_sql_inline_byte_range_and_large_file_meta_real_pg(
    disable_dotenv_loading, test_db_pool, test_db_uri, dalton_schema
):
    """Real PostgreSQL verifies offset+1 slicing for content_binary and non-ASCII content_text."""
    async with await psycopg.AsyncConnection.connect(
        test_db_uri, autocommit=True, row_factory=dict_row
    ) as conn:
        await _assert_connected_to_test_db(conn)
        uid, ws_id = await _seed_user_and_workspace(conn)

        bin_bytes = bytes(range(256)) + b"\x00\xff\xfe\x80" + b"tail-binary"
        text_str = "头部-Alpha-中文测试🔥-café-尾部结束"
        text_bytes = text_str.encode("utf-8")

        await _insert_workspace_file(
            conn,
            workspace_id=ws_id,
            file_path="data/sample.bin",
            file_name="sample.bin",
            content_binary=bin_bytes,
            is_binary=True,
            mime_type="application/octet-stream",
        )
        await _insert_workspace_file(
            conn,
            workspace_id=ws_id,
            file_path="notes/utf8.txt",
            file_name="utf8.txt",
            content_text=text_str,
            is_binary=False,
            mime_type="text/plain",
        )
        await _insert_workspace_file(
            conn,
            workspace_id=ws_id,
            file_path="blobs/ptr.bin",
            file_name="ptr.bin",
            blob_sha256="a" * 64,
            content_hash="a" * 64,
            file_size=12345,
            is_binary=True,
        )

        # Inject test_db_pool into app DB pool so default get_db_connection() hits _test DB.
        with patch.object(db_pool_mod, "get_or_create_pool", return_value=test_db_pool):
            meta_bin = await get_large_file_meta(ws_id, "data/sample.bin")
            meta_txt = await get_large_file_meta(ws_id, "notes/utf8.txt")
            meta_ptr = await get_large_file_meta(ws_id, "blobs/ptr.bin")

            assert meta_bin is not None
            assert meta_bin["inline_kind"] == "binary"
            assert "content_binary" not in meta_bin and "content_text" not in meta_bin

            assert meta_txt is not None
            assert meta_txt["inline_kind"] == "text"
            assert "content_binary" not in meta_txt and "content_text" not in meta_txt

            assert meta_ptr is not None
            assert meta_ptr["inline_kind"] is None
            assert meta_ptr["blob_sha256"] == "a" * 64

            # 1) content_binary: offset=0, mid-boundary, last tail, past EOF
            b0 = await get_inline_file_byte_range(
                ws_id, "data/sample.bin", 0, 17, inline_kind="binary"
            )
            b1 = await get_inline_file_byte_range(
                ws_id, "data/sample.bin", 17, 100, inline_kind="binary"
            )
            b2 = await get_inline_file_byte_range(
                ws_id, "data/sample.bin", 117, 500, inline_kind="binary"
            )
            b_eof = await get_inline_file_byte_range(
                ws_id, "data/sample.bin", len(bin_bytes), 32, inline_kind="binary"
            )
            assert b0 == bin_bytes[:17]
            assert b0 + b1 + b2 == bin_bytes
            assert b_eof == b""

            # 2) content_text (non-ASCII): offset=0, crossing multi-byte UTF-8 char, tail, past EOF
            # Byte 0..2 is '头' (3 bytes); cut at offset=1 to split inside a UTF-8 char!
            t0 = await resolve_stored_byte_range(ws_id, meta_txt, 0, 1, user_id=uid)
            t1 = await resolve_stored_byte_range(ws_id, meta_txt, 1, 13, user_id=uid)
            t2 = await resolve_stored_byte_range(ws_id, meta_txt, 14, 1000, user_id=uid)
            t_eof = await resolve_stored_byte_range(
                ws_id, meta_txt, len(text_bytes) + 5, 16, user_id=uid
            )
            assert t0 == text_bytes[:1]
            assert t0 + t1 + t2 == text_bytes
            assert (t0 + t1 + t2).decode("utf-8") == text_str
            assert t_eof == b""

            # 3) Version consistency: overwriting the same path mid-stream (even
            # with identical byte length) aborts the next slice with
            # StoredStreamVersionChangedError.
            v1_text = "version-one-plain-text-payload!!"
            v2_bin = b"\xff\xfe\x00\x01" + b"\xab" * (len(v1_text.encode("utf-8")) - 4)
            assert len(v2_bin) == len(v1_text.encode("utf-8"))

            await _insert_workspace_file(
                conn,
                workspace_id=ws_id,
                file_path="race/overwrite.dat",
                file_name="overwrite.dat",
                content_text=v1_text,
                is_binary=False,
                mime_type="text/plain",
            )
            meta_v1 = await get_large_file_meta(ws_id, "race/overwrite.dat")
            assert meta_v1 is not None
            assert meta_v1["inline_kind"] == "text"
            assert "workspace_file_id" in meta_v1 and "updated_at" in meta_v1

            first = await resolve_stored_byte_range(ws_id, meta_v1, 0, 8, user_id=uid)
            assert first == v1_text.encode("utf-8")[:8]

            # Overwrite same path with same-length binary bytes & new hash/updated_at.
            v2_hash = hashlib.sha256(v2_bin).hexdigest()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE workspace_files
                       SET content_text = NULL,
                           content_binary = %s,
                           content_hash = %s,
                           is_binary = TRUE,
                           updated_at = updated_at + INTERVAL '1 second'
                     WHERE workspace_id = %s::uuid AND file_path = %s
                    """,
                    (v2_bin, v2_hash, ws_id, "race/overwrite.dat"),
                )

            with pytest.raises(StoredStreamVersionChangedError):
                await resolve_stored_byte_range(ws_id, meta_v1, 8, 8, user_id=uid)

            with pytest.raises(StoredStreamVersionChangedError):
                async for _ in stream_stored_file(
                    ws_id, meta_v1, user_id=uid, first_chunk=first
                ):
                    pass


@pytest.mark.asyncio
async def test_backfill_stats_only_and_apply_real_pg(
    disable_dotenv_loading, test_db_pool, test_db_uri, dalton_schema
):
    """Real PostgreSQL + in-memory storage double runs --stats-only and --apply end-to-end."""
    async with await psycopg.AsyncConnection.connect(
        test_db_uri, autocommit=True, row_factory=dict_row
    ) as conn:
        await _assert_connected_to_test_db(conn)
        uid_normal, ws_normal = await _seed_user_and_workspace(conn)
        company_uid = company_identity.COMPANY_SYSTEM_USER_ID
        _, ws_company = await _seed_user_and_workspace(conn, user_id=company_uid)

        txt_content = "research memo 中文 2026"
        bin_content = b"\x89PNG\r\n\x1a\nfake-binary-bytes"
        co_content = "company daily automation output"

        fid_txt = await _insert_workspace_file(
            conn,
            workspace_id=ws_normal,
            file_path="memo.md",
            file_name="memo.md",
            content_text=txt_content,
            is_binary=False,
        )
        fid_bin = await _insert_workspace_file(
            conn,
            workspace_id=ws_normal,
            file_path="chart.png",
            file_name="chart.png",
            content_binary=bin_content,
            is_binary=True,
        )
        fid_lossy = await _insert_workspace_file(
            conn,
            workspace_id=ws_normal,
            file_path="lossy.txt",
            file_name="lossy.txt",
            content_text="stripped-nul-body",
            content_hash="f" * 64,  # disagrees with utf-8 sha256 -> skipped_lossy
            is_binary=False,
        )
        fid_co = await _insert_workspace_file(
            conn,
            workspace_id=ws_company,
            file_path="company.md",
            file_name="company.md",
            content_text=co_content,
            is_binary=False,
        )

        # 1) --stats-only (read-only, no object storage)
        stats = await bf_mod.collect_backfill_stats(
            conn, workspace_id=ws_normal, include_legacy_owners=True
        )
        assert stats.inline_rows == 3
        assert stats.skipped_lossy == [fid_lossy]
        assert stats.owner_errors == []

        rc_stats = await bf_mod.run_stats_only(
            conn, workspace_id=ws_company, include_legacy_owners=True
        )
        assert rc_stats == 0
        co_stats = await bf_mod.collect_backfill_stats(
            conn, workspace_id=ws_company, include_legacy_owners=True
        )
        assert co_stats.non_uuid_owners == {company_uid: "sys_dalton-company"}

        # 2) --apply with create_autospec in-memory storage facade
        mem_store: dict[str, bytes] = {}
        size_stub = create_autospec(
            storage_facade.object_size,
            spec_set=True,
            side_effect=lambda k: len(mem_store[k]) if k in mem_store else None,
        )
        put_stub = create_autospec(
            storage_facade.upload_bytes,
            spec_set=True,
            side_effect=lambda k, d, ct=None, max_size=None: (
                mem_store.__setitem__(k, d) or True
            ),
        )
        sha_stub = create_autospec(
            storage_facade.sha256_object,
            spec_set=True,
            side_effect=lambda k, **kw: (
                hashlib.sha256(mem_store[k]).hexdigest() if k in mem_store else None
            ),
        )

        with (
            patch.object(storage_facade, "object_size", size_stub),
            patch.object(storage_facade, "upload_bytes", put_stub),
            patch.object(storage_facade, "sha256_object", sha_stub),
        ):
            rc_ws = await bf_mod.run_forward(
                conn, apply=True, workspace_id=ws_normal, include_legacy_owners=True
            )
            rc_co = await bf_mod.run_forward(
                conn, apply=True, workspace_id=ws_company, include_legacy_owners=True
            )

        assert rc_ws == 0
        assert rc_co == 0

        # Verify migrated rows cleared inline columns and populated blob_sha256 + registry.
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT workspace_file_id::text AS id, content_text, content_binary, blob_sha256
                FROM workspace_files
                WHERE workspace_file_id = ANY(%s::uuid[])
                """,
                ([fid_txt, fid_bin, fid_lossy, fid_co],),
            )
            by_id = {r["id"]: r for r in await cur.fetchall()}

            txt_sha = hashlib.sha256(txt_content.encode("utf-8")).hexdigest()
            bin_sha = hashlib.sha256(bin_content).hexdigest()
            co_sha = hashlib.sha256(co_content.encode("utf-8")).hexdigest()

            assert by_id[fid_txt]["content_text"] is None
            assert by_id[fid_txt]["content_binary"] is None
            assert by_id[fid_txt]["blob_sha256"] == txt_sha

            assert by_id[fid_bin]["content_text"] is None
            assert by_id[fid_bin]["content_binary"] is None
            assert by_id[fid_bin]["blob_sha256"] == bin_sha

            assert by_id[fid_co]["content_text"] is None
            assert by_id[fid_co]["content_binary"] is None
            assert by_id[fid_co]["blob_sha256"] == co_sha

            # skipped_lossy row MUST remain untouched inline!
            assert by_id[fid_lossy]["content_text"] == "stripped-nul-body"
            assert by_id[fid_lossy]["blob_sha256"] is None

            # Verify workspace_file_blobs registry rows (company owner keeps raw DB user_id).
            await cur.execute(
                """
                SELECT user_id, sha256, byte_len
                FROM workspace_file_blobs
                WHERE (user_id = %s AND sha256 = ANY(%s))
                   OR (user_id = %s AND sha256 = %s)
                """,
                (uid_normal, [txt_sha, bin_sha], company_uid, co_sha),
            )
            reg_rows = {(r["user_id"], r["sha256"]): r["byte_len"] for r in await cur.fetchall()}
            assert reg_rows[(uid_normal, txt_sha)] == len(txt_content.encode("utf-8"))
            assert reg_rows[(uid_normal, bin_sha)] == len(bin_content)
            assert reg_rows[(company_uid, co_sha)] == len(co_content.encode("utf-8"))

        # Object storage received mapped key for company owner and normal keys for UUID owner.
        assert mem_store[blob_key(uid_normal, txt_sha)] == txt_content.encode("utf-8")
        assert mem_store[blob_key(uid_normal, bin_sha)] == bin_content
        assert mem_store[f"blobs/sys_dalton-company/{co_sha}"] == co_content.encode("utf-8")
