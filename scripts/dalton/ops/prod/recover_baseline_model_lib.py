#!/usr/bin/env python3
"""Read-only baseline workbook recovery helper for Everflow prod (modeling v2 §4.1 & §8.5).

Strictly read-only: queries Postgres (or a SQLite test double) without mutating
any database row, reads candidate bytes from MinIO / workspace uploads / inline
`chat_attachments.content`, verifies `SHA-256` against `records[0].input_sha256`
(and `actual_sha256` for large uploads), and supports streaming decryption from
encrypted `rclone` backups when DB/object-store channels miss.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tarfile
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Exact PostgreSQL read-only SQL queries specified in modeling-v2.md §8.5
# (with explicit ::timestamptz / ::text[] casts so PostgreSQL never mis-infers
#  untyped text parameters in INTERVAL arithmetic!)
# ---------------------------------------------------------------------------

SQL_PG_LARGE_UPLOADS = """
SELECT upload_id, workspace_id, filename, size_bytes, object_key,
       actual_sha256, state, created_at, used_at, cleaned_at
FROM dalton_chat_file_uploads
WHERE workspace_id = %(workspace_id)s::text
  AND created_at <= %(before_first_edit_ts)s::timestamptz
  AND filename ILIKE '%%.xlsx'
ORDER BY created_at ASC;
"""

SQL_PG_TURN_ATTACHMENTS = """
SELECT q.conversation_thread_id, q.turn_index, q.created_at AS turn_created_at,
       r.user_id AS turn_sender_id, t.created_by AS thread_created_by,
       w.user_id AS workspace_owner_id, att AS attachment_entry
FROM conversation_queries q
JOIN conversation_threads t ON t.conversation_thread_id = q.conversation_thread_id
JOIN workspaces w ON w.workspace_id = t.workspace_id
LEFT JOIN LATERAL (
    SELECT cr.user_id
    FROM conversation_responses cr
    WHERE cr.conversation_thread_id = q.conversation_thread_id
      AND cr.turn_index = q.turn_index
      AND cr.user_id IS NOT NULL
    ORDER BY cr.attempt_no ASC
    LIMIT 1
) r ON TRUE
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(q.metadata->'attachments', '[]'::jsonb)) AS att
WHERE t.workspace_id = %(workspace_id)s::uuid
  AND q.created_at <= %(before_first_edit_ts)s::timestamptz
  AND COALESCE(att->>'name', '') ILIKE '%%.xlsx'
ORDER BY q.created_at ASC, q.turn_index ASC;
"""

SQL_PG_INLINE_CANDIDATES = """
SELECT attachment_id::text AS attachment_id, user_id, name, size,
       object_key, (content IS NOT NULL) AS has_inline_content,
       state, created_at, sent_at, cleaned_at
FROM chat_attachments
WHERE user_id = ANY(%(sender_user_ids)s::text[])
  AND name = %(att_name)s
  AND size = %(att_size)s::bigint
  AND created_at <= LEAST(
        %(before_first_edit_ts)s::timestamptz,
        %(turn_created_at)s::timestamptz + INTERVAL '5 minutes'
      )
  AND (sent_at IS NULL OR sent_at BETWEEN %(turn_created_at)s::timestamptz - INTERVAL '10 minutes'
                                      AND %(turn_created_at)s::timestamptz + INTERVAL '10 minutes')
ORDER BY ABS(EXTRACT(EPOCH FROM (COALESCE(sent_at, created_at) - %(turn_created_at)s::timestamptz))) ASC;
"""

SQL_PG_INLINE_CONTENT = """
SELECT content
FROM chat_attachments
WHERE attachment_id = %(attachment_id)s::uuid;
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_iso_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def extract_lineage_anchor(
    *,
    workspace_dir: Path | None = None,
    lineage_file: Path | None = None,
) -> dict[str, Any]:
    """Read earliest ``research/*/model_lineage.json`` to obtain baseline sha256 & cutoff."""
    candidates: list[Path] = []
    if lineage_file is not None:
        candidates = [lineage_file]
    elif workspace_dir is not None:
        research_dir = workspace_dir / "research"
        if research_dir.is_dir():
            candidates = sorted(research_dir.glob("*/model_lineage.json"))

    earliest: tuple[str, Path, dict[str, Any]] | None = None
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records = payload.get("records") or []
        if not records or not isinstance(records[0], dict):
            continue
        rec0 = records[0]
        rec_ts = str(rec0.get("recorded_at") or "")
        sort_key = rec_ts or f"9999-{path.as_posix()}"
        if earliest is None or sort_key < earliest[0]:
            earliest = (sort_key, path, rec0)

    if earliest is None:
        return {
            "lineage_file": None,
            "before_first_edit_ts": None,
            "input_sha256": None,
            "input_file": None,
        }
    _, path, rec0 = earliest
    return {
        "lineage_file": str(path),
        "before_first_edit_ts": rec0.get("recorded_at"),
        "input_sha256": (str(rec0.get("input_sha256") or "").strip().lower() or None),
        "input_file": rec0.get("input_file"),
    }


def extract_object_key_from_url(url: str) -> str | None:
    """Extract ``attachments/<id>/<name>`` or similar object key from a stored attachment URL."""
    raw = str(url or "").strip()
    if not raw:
        return None
    m = re.search(r"(attachments/[^?#]+)", raw)
    if m:
        return m.group(1).lstrip("/")
    if not raw.startswith(("http://", "https://")) and "/" in raw:
        return raw.lstrip("/")
    return None


@dataclass
class RecoveryResult:
    found: bool
    channel: str
    output_path: str | None = None
    sha256: str | None = None
    size_bytes: int = 0
    source_ref: str | None = None
    checked_candidates: int = 0
    detail: str = ""
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "channel": self.channel,
            "output_path": self.output_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "source_ref": self.source_ref,
            "checked_candidates": self.checked_candidates,
            "detail": self.detail,
            "diagnostics": list(self.diagnostics),
        }


class ReadOnlyDBAdapter:
    """Abstract read-only DB query interface (implemented for Postgres & SQLite test doubles)."""

    def fetch_large_uploads(self, workspace_id: str, before_ts: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_turn_attachments(self, workspace_id: str, before_ts: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_inline_candidates(
        self,
        sender_user_ids: list[str],
        att_name: str,
        att_size: int,
        before_ts: str,
        turn_created_at: str,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_inline_content(self, attachment_id: str) -> bytes | None:
        raise NotImplementedError


class PostgresReadOnlyAdapter(ReadOnlyDBAdapter):
    """Read-only PostgreSQL adapter executing the exact §8.5 SQL queries."""

    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;")

    def close(self) -> None:
        self._conn.close()

    def fetch_large_uploads(self, workspace_id: str, before_ts: str | datetime) -> list[dict[str, Any]]:
        before_dt = parse_iso_ts(before_ts)
        with self._conn.cursor() as cur:
            cur.execute(
                SQL_PG_LARGE_UPLOADS,
                {"workspace_id": workspace_id, "before_first_edit_ts": before_dt},
            )
            return list(cur.fetchall())

    def fetch_turn_attachments(self, workspace_id: str, before_ts: str | datetime) -> list[dict[str, Any]]:
        before_dt = parse_iso_ts(before_ts)
        with self._conn.cursor() as cur:
            cur.execute(
                SQL_PG_TURN_ATTACHMENTS,
                {"workspace_id": workspace_id, "before_first_edit_ts": before_dt},
            )
            return list(cur.fetchall())

    def fetch_inline_candidates(
        self,
        sender_user_ids: list[str],
        att_name: str,
        att_size: int,
        before_ts: str | datetime,
        turn_created_at: str | datetime,
    ) -> list[dict[str, Any]]:
        if not sender_user_ids:
            return []
        before_dt = parse_iso_ts(before_ts)
        turn_dt = parse_iso_ts(turn_created_at)
        with self._conn.cursor() as cur:
            cur.execute(
                SQL_PG_INLINE_CANDIDATES,
                {
                    "sender_user_ids": list(sender_user_ids),
                    "att_name": att_name,
                    "att_size": int(att_size),
                    "before_first_edit_ts": before_dt,
                    "turn_created_at": turn_dt,
                },
            )
            return list(cur.fetchall())

    def fetch_inline_content(self, attachment_id: str) -> bytes | None:
        with self._conn.cursor() as cur:
            cur.execute(SQL_PG_INLINE_CONTENT, {"attachment_id": attachment_id})
            row = cur.fetchone()
            if not row or row.get("content") is None:
                return None
            return bytes(row["content"])


class SQLiteReadOnlyAdapter(ReadOnlyDBAdapter):
    """Read-only SQLite adapter mirroring the §8.5 schema for offline unit tests."""

    def __init__(self, db_path: str | Path) -> None:
        uri = f"file:{Path(db_path).resolve()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def fetch_large_uploads(self, workspace_id: str, before_ts: str) -> list[dict[str, Any]]:
        if not self._table_exists("dalton_chat_file_uploads"):
            return []
        cutoff = parse_iso_ts(before_ts)
        rows = self._conn.execute(
            """
            SELECT upload_id, workspace_id, filename, size_bytes, object_key,
                   actual_sha256, state, created_at, used_at, cleaned_at
            FROM dalton_chat_file_uploads
            WHERE workspace_id = ?
            ORDER BY created_at ASC
            """,
            (workspace_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if not str(d.get("filename") or "").lower().endswith(".xlsx"):
                continue
            dt = parse_iso_ts(d.get("created_at"))
            if cutoff and dt and dt > cutoff:
                continue
            out.append(d)
        return out

    def fetch_turn_attachments(self, workspace_id: str, before_ts: str) -> list[dict[str, Any]]:
        for tbl in ("conversation_queries", "conversation_threads", "workspaces"):
            if not self._table_exists(tbl):
                return []
        cutoff = parse_iso_ts(before_ts)
        has_resp = self._table_exists("conversation_responses")
        rows = self._conn.execute(
            """
            SELECT q.conversation_thread_id, q.turn_index, q.created_at AS turn_created_at,
                   q.metadata AS metadata_json,
                   t.created_by AS thread_created_by,
                   w.user_id AS workspace_owner_id
            FROM conversation_queries q
            JOIN conversation_threads t ON t.conversation_thread_id = q.conversation_thread_id
            JOIN workspaces w ON w.workspace_id = t.workspace_id
            WHERE t.workspace_id = ?
            ORDER BY q.created_at ASC, q.turn_index ASC
            """,
            (workspace_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            dt = parse_iso_ts(d.get("turn_created_at"))
            if cutoff and dt and dt > cutoff:
                continue
            turn_sender_id = None
            if has_resp:
                resp = self._conn.execute(
                    """
                    SELECT user_id FROM conversation_responses
                    WHERE conversation_thread_id = ? AND turn_index = ? AND user_id IS NOT NULL
                    ORDER BY attempt_no ASC LIMIT 1
                    """,
                    (d["conversation_thread_id"], d["turn_index"]),
                ).fetchone()
                if resp:
                    turn_sender_id = resp["user_id"]
            meta = d.get("metadata_json")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            attachments = (meta or {}).get("attachments") if isinstance(meta, dict) else []
            for att in attachments or []:
                if not isinstance(att, dict):
                    continue
                if not str(att.get("name") or "").lower().endswith(".xlsx"):
                    continue
                out.append(
                    {
                        "conversation_thread_id": d["conversation_thread_id"],
                        "turn_index": d["turn_index"],
                        "turn_created_at": d["turn_created_at"],
                        "turn_sender_id": turn_sender_id,
                        "thread_created_by": d["thread_created_by"],
                        "workspace_owner_id": d["workspace_owner_id"],
                        "attachment_entry": att,
                    }
                )
        return out

    def fetch_inline_candidates(
        self,
        sender_user_ids: list[str],
        att_name: str,
        att_size: int,
        before_ts: str,
        turn_created_at: str,
    ) -> list[dict[str, Any]]:
        if not sender_user_ids or not self._table_exists("chat_attachments"):
            return []
        placeholders = ",".join("?" for _ in sender_user_ids)
        rows = self._conn.execute(
            f"""
            SELECT attachment_id, user_id, name, size, object_key,
                   (content IS NOT NULL) AS has_inline_content,
                   state, created_at, sent_at, cleaned_at
            FROM chat_attachments
            WHERE user_id IN ({placeholders})
              AND name = ?
              AND size = ?
            """,
            [*sender_user_ids, att_name, int(att_size)],
        ).fetchall()
        cutoff_dt = parse_iso_ts(before_ts)
        turn_dt = parse_iso_ts(turn_created_at)
        matched: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            d = dict(r)
            c_dt = parse_iso_ts(d.get("created_at"))
            s_dt = parse_iso_ts(d.get("sent_at"))
            max_created = (
                min(cutoff_dt, turn_dt + timedelta(minutes=5))
                if (cutoff_dt and turn_dt)
                else (cutoff_dt or (turn_dt + timedelta(minutes=5) if turn_dt else None))
            )
            if max_created and c_dt and c_dt > max_created:
                continue
            if turn_dt and s_dt is not None:
                if not (turn_dt - timedelta(minutes=10) <= s_dt <= turn_dt + timedelta(minutes=10)):
                    continue
            ref_dt = s_dt or c_dt
            dist = abs((ref_dt - turn_dt).total_seconds()) if (ref_dt and turn_dt) else 0.0
            matched.append((dist, d))
        matched.sort(key=lambda item: item[0])
        return [d for _, d in matched]

    def fetch_inline_content(self, attachment_id: str) -> bytes | None:
        if not self._table_exists("chat_attachments"):
            return None
        row = self._conn.execute(
            "SELECT content FROM chat_attachments WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        if not row or row["content"] is None:
            return None
        return bytes(row["content"])


def build_object_reader(
    *,
    object_store_dir: Path | None = None,
    minio_mc_alias: str | None = None,
    minio_bucket: str | None = None,
) -> Callable[[str], bytes | None]:
    """Return a read-only object-key byte reader (local mirror dir or ``mc cat``)."""

    def _read(object_key: str) -> bytes | None:
        key = str(object_key or "").lstrip("/")
        if not key:
            return None
        if object_store_dir is not None:
            for candidate in (
                object_store_dir / key,
                (object_store_dir / minio_bucket / key) if minio_bucket else object_store_dir / key,
            ):
                if candidate.is_file():
                    return candidate.read_bytes()
        if minio_mc_alias and minio_bucket:
            proc = subprocess.run(
                ["mc", "cat", f"{minio_mc_alias}/{minio_bucket}/{key}"],
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        return None

    return _read


def _decode_msgpack_one(buf: bytes, pos: int) -> tuple[Any, int]:
    """Decode one msgpack object from ``buf`` starting at ``pos`` (stdlib-only for MinIO xl.meta)."""
    if pos >= len(buf):
        raise ValueError("unexpected EOF in msgpack")
    b = buf[pos]
    pos += 1
    if b <= 0x7F:
        return b, pos
    if b >= 0xE0:
        return b - 256, pos
    if 0x80 <= b <= 0x8F:
        n = b & 0x0F
        out: dict[Any, Any] = {}
        for _ in range(n):
            k, pos = _decode_msgpack_one(buf, pos)
            v, pos = _decode_msgpack_one(buf, pos)
            out[k] = v
        return out, pos
    if 0x90 <= b <= 0x9F:
        n = b & 0x0F
        arr = []
        for _ in range(n):
            v, pos = _decode_msgpack_one(buf, pos)
            arr.append(v)
        return arr, pos
    if 0xA0 <= b <= 0xBF:
        n = b & 0x1F
        return buf[pos : pos + n].decode("utf-8", errors="replace"), pos + n
    if b == 0xC0:
        return None, pos
    if b == 0xC2:
        return False, pos
    if b == 0xC3:
        return True, pos
    if b == 0xC4:
        n = buf[pos]
        return buf[pos + 1 : pos + 1 + n], pos + 1 + n
    if b == 0xC5:
        n = struct.unpack_from(">H", buf, pos)[0]
        return buf[pos + 2 : pos + 2 + n], pos + 2 + n
    if b == 0xC6:
        n = struct.unpack_from(">I", buf, pos)[0]
        return buf[pos + 4 : pos + 4 + n], pos + 4 + n
    if b in (0xC7, 0xC8, 0xC9):
        if b == 0xC7:
            n = buf[pos]
            pos += 1
        elif b == 0xC8:
            n = struct.unpack_from(">H", buf, pos)[0]
            pos += 2
        else:
            n = struct.unpack_from(">I", buf, pos)[0]
            pos += 4
        _ext_type = buf[pos]
        return buf[pos + 1 : pos + 1 + n], pos + 1 + n
    if b == 0xCA:
        return struct.unpack_from(">f", buf, pos)[0], pos + 4
    if b == 0xCB:
        return struct.unpack_from(">d", buf, pos)[0], pos + 8
    if b == 0xCC:
        return buf[pos], pos + 1
    if b == 0xCD:
        return struct.unpack_from(">H", buf, pos)[0], pos + 2
    if b == 0xCE:
        return struct.unpack_from(">I", buf, pos)[0], pos + 4
    if b == 0xCF:
        return struct.unpack_from(">Q", buf, pos)[0], pos + 8
    if b == 0xD0:
        return struct.unpack_from(">b", buf, pos)[0], pos + 1
    if b == 0xD1:
        return struct.unpack_from(">h", buf, pos)[0], pos + 2
    if b == 0xD2:
        return struct.unpack_from(">i", buf, pos)[0], pos + 4
    if b == 0xD3:
        return struct.unpack_from(">q", buf, pos)[0], pos + 8
    if b in (0xD4, 0xD5, 0xD6, 0xD7, 0xD8):
        sizes = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}
        n = sizes[b]
        return buf[pos + 1 : pos + 1 + n], pos + 1 + n
    if b == 0xD9:
        n = buf[pos]
        return buf[pos + 1 : pos + 1 + n].decode("utf-8", errors="replace"), pos + 1 + n
    if b == 0xDA:
        n = struct.unpack_from(">H", buf, pos)[0]
        return buf[pos + 2 : pos + 2 + n].decode("utf-8", errors="replace"), pos + 2 + n
    if b == 0xDB:
        n = struct.unpack_from(">I", buf, pos)[0]
        return buf[pos + 4 : pos + 4 + n].decode("utf-8", errors="replace"), pos + 4 + n
    if b == 0xDC:
        n = struct.unpack_from(">H", buf, pos)[0]
        pos += 2
        arr = []
        for _ in range(n):
            v, pos = _decode_msgpack_one(buf, pos)
            arr.append(v)
        return arr, pos
    if b == 0xDD:
        n = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        arr = []
        for _ in range(n):
            v, pos = _decode_msgpack_one(buf, pos)
            arr.append(v)
        return arr, pos
    if b == 0xDE:
        n = struct.unpack_from(">H", buf, pos)[0]
        pos += 2
        out = {}
        for _ in range(n):
            k, pos = _decode_msgpack_one(buf, pos)
            v, pos = _decode_msgpack_one(buf, pos)
            out[k] = v
        return out, pos
    if b == 0xDF:
        n = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        out = {}
        for _ in range(n):
            k, pos = _decode_msgpack_one(buf, pos)
            v, pos = _decode_msgpack_one(buf, pos)
            out[k] = v
        return out, pos
    raise ValueError(f"unsupported msgpack prefix byte 0x{b:02x}")


def _decode_msgpack_all(buf: bytes, start_pos: int = 0) -> tuple[list[Any], int]:
    """Decode sequential msgpack values starting at ``start_pos``; returns ``(values, end_pos)``."""
    vals: list[Any] = []
    pos = start_pos
    while pos < len(buf):
        try:
            obj, new_pos = _decode_msgpack_one(buf, pos)
        except Exception:
            break
        if new_pos <= pos:
            break
        vals.append(obj)
        pos = new_pos
    return vals, pos


def _parse_xl_meta(xl_meta_bytes: bytes) -> tuple[list[bytes], list[int], list[int]]:
    """Parse a MinIO XL2 ``xl.meta`` file recursively (supporting v1.1/v1.2/v1.3 nested msgpack ``bin``).

    Returns ``(inline_blobs, block_sizes, hash_lens)``.
    """
    blobs: list[bytes] = []
    block_sizes: list[int] = []
    hash_lens: list[int] = []
    if len(xl_meta_bytes) < 8 or xl_meta_bytes[:4] != b"XL2 ":
        return blobs, block_sizes, hash_lens

    seen_blobs: set[bytes] = set()
    # MinIO BitrotAlgorithm enum: 1=HighwayHash256(32), 2=HighwayHash256S(32), 3=BLAKE2b512(64), 4=SHA256(32)
    _csum_hash_len = {1: 32, 2: 32, 3: 64, 4: 32}

    def _collect(obj: Any, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(obj, (bytes, bytearray)):
            raw = bytes(obj)
            if len(raw) >= 32 and raw not in seen_blobs:
                seen_blobs.add(raw)
                blobs.append(raw)
            # In MinIO XL2 v1.3, outer metadata & V2Obj & inlineData are nested msgpack inside bin fields
            if depth < 8 and len(raw) >= 2:
                nested, end_pos = _decode_msgpack_all(raw, 0)
                if nested and end_pos == len(raw) and any(isinstance(x, (dict, list)) for x in nested):
                    for item in nested:
                        _collect(item, depth + 1)
        elif isinstance(obj, dict):
            bsize = obj.get("EcBSize") or obj.get("BlockSize")
            if isinstance(bsize, int) and 1024 <= bsize <= 104857600 and bsize not in block_sizes:
                block_sizes.append(bsize)
            csum = obj.get("CSumAlgo") or obj.get("Algo")
            if isinstance(csum, int) and csum in _csum_hash_len:
                hl = _csum_hash_len[csum]
                if hl not in hash_lens:
                    hash_lens.append(hl)
            for v in obj.values():
                _collect(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                _collect(v, depth + 1)

    # Offset 8: skip 4-byte 'XL2 ' magic + 2-byte major + 2-byte minor
    top_vals, _ = _decode_msgpack_all(xl_meta_bytes, 8)
    for tv in top_vals:
        _collect(tv, 0)
    # Also handle optional 4-byte LE length prefix between header and msgpack in XL2 v1.3 variants
    if len(xl_meta_bytes) > 12:
        alt_vals, _ = _decode_msgpack_all(xl_meta_bytes, 12)
        if alt_vals and not top_vals:
            for av in alt_vals:
                _collect(av, 0)
    return blobs, block_sizes, hash_lens


def _extract_xl_meta_inline_blobs(xl_meta_bytes: bytes) -> list[bytes]:
    """Extract inline binary object payloads stored inside a MinIO XL2 ``xl.meta`` file."""
    blobs, _, _ = _parse_xl_meta(xl_meta_bytes)
    return blobs


def _strip_minio_bitrot_headers(raw: bytes, block_size: int = 1048576, hash_len: int = 32) -> bytes | None:
    """Strip per-block bitrot headers (32B HighwayHash256/SHA256 or 64B BLAKE2b512) from a MinIO single-drive blob."""
    if len(raw) <= hash_len or block_size <= 0:
        return None
    out = bytearray()
    step = hash_len + block_size
    pos = 0
    while pos < len(raw):
        if len(raw) - pos <= hash_len:
            return None
        chunk_end = min(pos + step, len(raw))
        out.extend(raw[pos + hash_len : chunk_end])
        pos = chunk_end
    return bytes(out)


def _iter_minio_inline_candidates(
    blob: bytes,
    block_sizes: list[int] | None = None,
    hash_lens: list[int] | None = None,
) -> list[bytes]:
    """Yield raw and bitrot-stripped candidates for a MinIO ``xl.meta`` inline blob.

    Single-drive MinIO writes inline objects through the same streaming bitrot writer as ``part.*``
    (default 32-byte HighwayHash256 header per block; single-block inline objects are ``[32B hash][payload]``).
    """
    candidates: list[bytes] = [blob]
    seen_lens: set[tuple[int, int]] = set()
    eff_hlens = list(dict.fromkeys([*(hash_lens or []), 32, 64]))
    eff_bsizes = list(dict.fromkeys([*(block_sizes or []), 1048576, 10485760]))
    for hlen in eff_hlens:
        if len(blob) <= hlen:
            continue
        # Single-block inline object (object size <= EcBSize): strip leading hash_len bytes once
        candidates.append(blob[hlen:])
        for bsize in eff_bsizes:
            if len(blob) > hlen + bsize and (hlen, bsize) not in seen_lens:
                seen_lens.add((hlen, bsize))
                stripped = _strip_minio_bitrot_headers(blob, block_size=bsize, hash_len=hlen)
                if stripped is not None:
                    candidates.append(stripped)
    return candidates


def _iter_minio_part_candidates(
    part_blobs: list[bytes],
    block_sizes: list[int] | None = None,
    hash_lens: list[int] | None = None,
) -> list[bytes]:
    """Yield candidate payload byte sequences from ordered MinIO ``part.1..part.N`` files."""
    if not part_blobs:
        return []
    raw_concat = b"".join(part_blobs)
    candidates: list[bytes] = [raw_concat]
    eff_hlens = list(dict.fromkeys([*(hash_lens or []), 32, 64]))
    eff_bsizes = list(dict.fromkeys([*(block_sizes or []), 1048576, 10485760, 524288, 262144, 131072, 65536]))
    for hlen in eff_hlens:
        for bsize in eff_bsizes:
            stripped_parts: list[bytes] = []
            ok = True
            for pb in part_blobs:
                sp = _strip_minio_bitrot_headers(pb, block_size=bsize, hash_len=hlen)
                if sp is None:
                    ok = False
                    break
                stripped_parts.append(sp)
            if ok:
                candidates.append(b"".join(stripped_parts))
    return candidates


def _recover_from_minio_tar_bytes(
    tar_bytes: bytes,
    *,
    expected_sha256: str,
    target_filename: str | None = None,
    diagnostics: list[str] | None = None,
) -> tuple[bytes | None, str | None]:
    """Inspect a decrypted tar archive supporting BOTH plain ``.xlsx`` members AND MinIO volume layout
    (``xl.meta`` inline XL2 objects with bitrot stripping and ``<dir>/part.1..part.N`` single-drive parts)."""
    want_sha = expected_sha256.lower()
    part_re = re.compile(r"^part\.(\d+)$")
    part_groups: dict[str, list[tuple[int, str, bytes]]] = defaultdict(list)
    xl_meta_members: list[tuple[str, bytes]] = []
    has_minio_layout = False

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            mname = member.name.lstrip("./")
            base = Path(mname).name
            fobj = tf.extractfile(member)
            if fobj is None:
                continue

            # 1. Plain .xlsx file (e.g. sandbox-*.tar.gz.enc workspace backups)
            if base.lower().endswith(".xlsx"):
                data = fobj.read()
                if sha256_bytes(data) == want_sha:
                    return data, mname
                continue

            # 2. MinIO volume metadata (`xl.meta`) — may hold inline XL2 object bytes + EcBSize hints
            if base == "xl.meta":
                has_minio_layout = True
                xl_meta_members.append((mname, fobj.read()))
                continue

            # 3. MinIO data parts (`part.1`, `part.2`, ...)
            pm = part_re.match(base)
            if pm:
                has_minio_layout = True
                parent = str(Path(mname).parent)
                part_groups[parent].append((int(pm.group(1)), mname, fobj.read()))

    # Inspect MinIO xl.meta inline blobs (raw + bitrot-stripped) & collect per-object EcBSize/CSumAlgo hints
    target_base = Path(target_filename).name.lower() if target_filename else None

    def _prio(path_str: str) -> tuple[int, str]:
        low = path_str.lower()
        if target_base and target_base in low:
            return (0, path_str)
        if ".xlsx" in low:
            return (1, path_str)
        return (2, path_str)

    meta_hints_by_dir: dict[str, tuple[list[int], list[int]]] = {}
    for mname, raw_meta in sorted(xl_meta_members, key=lambda kv: _prio(kv[0])):
        blobs, bsizes, hlens = _parse_xl_meta(raw_meta)
        meta_hints_by_dir[str(Path(mname).parent)] = (bsizes, hlens)
        for blob in blobs:
            for cand in _iter_minio_inline_candidates(blob, block_sizes=bsizes, hash_lens=hlens):
                if sha256_bytes(cand) == want_sha:
                    return cand, f"minio-xl-inline:{mname}"

    # Inspect MinIO part.1..part.N groups (raw and bitrot-stripped using sibling xl.meta block sizes first)
    for parent, entries in sorted(part_groups.items(), key=lambda kv: _prio(kv[0])):
        entries.sort(key=lambda item: item[0])
        blobs = [b for _, _, b in entries]
        p_path = Path(parent)
        hints = meta_hints_by_dir.get(parent) or meta_hints_by_dir.get(str(p_path.parent)) or ([], [])
        for cand in _iter_minio_part_candidates(blobs, block_sizes=hints[0], hash_lens=hints[1]):
            if sha256_bytes(cand) == want_sha:
                return cand, f"minio-part:{parent}"

    if has_minio_layout and diagnostics is not None:
        diagnostics.append(
            "MinIO volume backup inspected (xl.meta inline + part.* raw/bitrot); "
            "no object matched expected sha256 (multi-disk erasure/encrypted MinIO volumes require isolated MinIO server replay)"
        )
    return None, None


def recover_from_encrypted_tar_stream(
    enc_stream_cmd: list[str],
    backup_key_file: Path,
    *,
    expected_sha256: str,
    target_filename: str | None = None,
    diagnostics: list[str] | None = None,
) -> tuple[bytes | None, str | None]:
    """Stream-decrypt an AES-256-CBC PBKDF2 tar.gz.enc backup (plain workspace or MinIO volume) and match ``expected_sha256``."""
    p1 = subprocess.Popen(enc_stream_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            f"file:{backup_key_file}",
        ],
        stdin=p1.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p1.stdout is not None:
        p1.stdout.close()
    decrypted, _ = p2.communicate()
    p1.wait()
    if p1.returncode != 0 or p2.returncode != 0 or not decrypted:
        return None, None

    try:
        return _recover_from_minio_tar_bytes(
            decrypted,
            expected_sha256=expected_sha256,
            target_filename=target_filename,
            diagnostics=diagnostics,
        )
    except tarfile.TarError:
        if sha256_bytes(decrypted) == expected_sha256.lower():
            return decrypted, "raw"
    return None, None


def _run_pg_restore_custom_dump(
    payload: bytes,
    *,
    pg_restore_bin: str = "pg_restore",
    pg_restore_compose_service: str | None = "postgres",
    pg_restore_compose_cwd: Path | None = None,
    pg_restore_docker_image: str | None = None,
    docker_bin: str = "docker",
    diagnostics: list[str] | None = None,
) -> bytes | None:
    """Convert a PostgreSQL custom-format dump (``PGDMP``) ``chat_attachments`` table to COPY SQL via compatible ``pg_restore``.

    Strictly read-only (``-f -`` streams SQL to stdout without connecting to or mutating any database).
    Fallback order aligned with ``scripts/dalton/ops/prod/backup.sh``:
      1. Host ``pg_restore`` binary
      2. Running compose Postgres container: ``docker compose exec -T <service> pg_restore -f - --data-only -t chat_attachments``
      3. Optional ``docker run --rm -i <image> pg_restore ...``
    """
    resolved = pg_restore_bin if Path(pg_restore_bin).is_file() else shutil.which(pg_restore_bin)
    err_reasons: list[str] = []

    if resolved:
        pr = subprocess.run(
            [resolved, "-f", "-", "--data-only", "-t", "chat_attachments"],
            input=payload,
            capture_output=True,
            check=False,
        )
        if pr.returncode == 0 and pr.stdout:
            return pr.stdout
        err_msg = (pr.stderr or b"").decode("utf-8", errors="ignore").strip()
        err_reasons.append(f"local {resolved} exited {pr.returncode}: {err_msg[:200]}")
    else:
        err_reasons.append(f"{pg_restore_bin!r} not found on PATH")

    resolved_docker = docker_bin if Path(docker_bin).is_file() else shutil.which(docker_bin)

    # Container fallback aligned with scripts/dalton/ops/prod/backup.sh (`docker compose exec -T postgres ...`)
    if pg_restore_compose_service and resolved_docker:
        pr_comp = subprocess.run(
            [
                resolved_docker,
                "compose",
                "exec",
                "-T",
                pg_restore_compose_service,
                "pg_restore",
                "-f",
                "-",
                "--data-only",
                "-t",
                "chat_attachments",
            ],
            input=payload,
            cwd=str(pg_restore_compose_cwd) if pg_restore_compose_cwd else None,
            capture_output=True,
            check=False,
        )
        if pr_comp.returncode == 0 and pr_comp.stdout:
            return pr_comp.stdout
        err_msg_c = (pr_comp.stderr or b"").decode("utf-8", errors="ignore").strip()
        err_reasons.append(
            f"docker compose exec {pg_restore_compose_service} pg_restore exited {pr_comp.returncode}: {err_msg_c[:200]}"
        )
    elif pg_restore_compose_service:
        err_reasons.append(f"{docker_bin!r} not found for compose service {pg_restore_compose_service!r}")

    # Optional ephemeral Docker image fallback when host lacks pg_restore and compose container isn't running
    if pg_restore_docker_image and resolved_docker:
        pr_dk = subprocess.run(
            [
                resolved_docker,
                "run",
                "--rm",
                "-i",
                pg_restore_docker_image,
                "pg_restore",
                "-f",
                "-",
                "--data-only",
                "-t",
                "chat_attachments",
            ],
            input=payload,
            capture_output=True,
            check=False,
        )
        if pr_dk.returncode == 0 and pr_dk.stdout:
            return pr_dk.stdout
        err_msg_dk = (pr_dk.stderr or b"").decode("utf-8", errors="ignore").strip()
        err_reasons.append(f"docker {pg_restore_docker_image} pg_restore exited {pr_dk.returncode}: {err_msg_dk[:200]}")

    if diagnostics is not None:
        diagnostics.append(
            "PGDMP custom-format daily Postgres backup could not be read without compatible pg_restore ("
            + "; ".join(err_reasons)
            + ")"
        )
    return None


def _scan_pg_sql_for_attachment(
    sql_bytes: bytes,
    expected_sha256: str,
) -> tuple[bytes | None, str | None]:
    """Scan PostgreSQL dump SQL text (COPY or INSERT into chat_attachments) for matching inline BYTEA."""
    want_sha = expected_sha256.lower()
    text = sql_bytes.decode("utf-8", errors="ignore")
    copy_re = re.compile(
        r"^\s*COPY\s+(?:[A-Za-z0-9_\"']+\.)?\"?chat_attachments\"?\s*\(([^)]+)\)\s+FROM\s+stdin;",
        re.IGNORECASE,
    )
    in_copy = False
    content_col_idx: int | None = None
    id_col_idx: int | None = None

    for line in text.splitlines():
        if not in_copy:
            m = copy_re.match(line)
            if m:
                cols = [c.strip().strip('"').lower() for c in m.group(1).split(",")]
                if "content" in cols:
                    in_copy = True
                    content_col_idx = cols.index("content")
                    id_col_idx = cols.index("attachment_id") if "attachment_id" in cols else None
                continue
            # Also handle plain INSERT INTO chat_attachments ... '\\x<hex>'
            if "chat_attachments" in line.lower() and "\\x" in line:
                for hm in re.finditer(r"'(?:\\x|\\\\x)([0-9a-fA-F]{16,})'", line):
                    hex_str = hm.group(1)
                    if len(hex_str) % 2 != 0:
                        continue
                    try:
                        blob = bytes.fromhex(hex_str)
                    except ValueError:
                        continue
                    if sha256_bytes(blob) == want_sha:
                        return blob, "chat_attachments:insert"
        else:
            if line == "\\.":
                in_copy = False
                content_col_idx = None
                id_col_idx = None
                continue
            if content_col_idx is None:
                continue
            fields = line.split("\t")
            if content_col_idx >= len(fields):
                continue
            raw_val = fields[content_col_idx]
            if raw_val == "\\N" or not raw_val:
                continue
            # PostgreSQL COPY escapes backslash as \\x<hex>
            hex_str: str | None = None
            if raw_val.startswith("\\\\x"):
                hex_str = raw_val[3:]
            elif raw_val.startswith("\\x"):
                hex_str = raw_val[2:]
            if hex_str and len(hex_str) % 2 == 0:
                try:
                    blob = bytes.fromhex(hex_str)
                except ValueError:
                    continue
                if sha256_bytes(blob) == want_sha:
                    att_id = fields[id_col_idx] if (id_col_idx is not None and id_col_idx < len(fields)) else "row"
                    return blob, f"chat_attachments:{att_id}"
    return None, None


def recover_from_encrypted_db_backup_stream(
    enc_stream_cmd: list[str],
    backup_key_file: Path,
    *,
    expected_sha256: str,
    pg_restore_bin: str = "pg_restore",
    pg_restore_compose_service: str | None = "postgres",
    pg_restore_compose_cwd: Path | None = None,
    pg_restore_docker_image: str | None = None,
    docker_bin: str = "docker",
    diagnostics: list[str] | None = None,
) -> tuple[bytes | None, str | None]:
    """Stream-decrypt a daily Postgres offsite backup (``db/<date>/*.enc``) and scan ``chat_attachments``.

    Handles ``pg_dump -Fc`` (``PGDMP``) via compatible ``pg_restore`` (host binary, compose postgres
    container, or optional Docker image fallback) and NEVER silently scans binary ``PGDMP`` as SQL.
    """
    p1 = subprocess.Popen(enc_stream_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            f"file:{backup_key_file}",
        ],
        stdin=p1.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p1.stdout is not None:
        p1.stdout.close()
    decrypted, _ = p2.communicate()
    p1.wait()
    if p1.returncode != 0 or p2.returncode != 0 or not decrypted:
        return None, None

    # Decompress gzip if needed (.sql.gz.enc)
    payload = decrypted
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except Exception:
            pass

    # Handle PostgreSQL custom-format dump (PGDMP) via compatible pg_restore; NEVER scan binary PGDMP as SQL
    if payload[:5] == b"PGDMP":
        restored_sql = _run_pg_restore_custom_dump(
            payload,
            pg_restore_bin=pg_restore_bin,
            pg_restore_compose_service=pg_restore_compose_service,
            pg_restore_compose_cwd=pg_restore_compose_cwd,
            pg_restore_docker_image=pg_restore_docker_image,
            docker_bin=docker_bin,
            diagnostics=diagnostics,
        )
        if restored_sql is None:
            return None, None
        return _scan_pg_sql_for_attachment(restored_sql, expected_sha256)

    return _scan_pg_sql_for_attachment(payload, expected_sha256)


def recover_baseline(
    *,
    workspace_id: str,
    output_path: Path,
    before_first_edit_ts: str,
    expected_sha256: str,
    expected_filename: str | None = None,
    db: ReadOnlyDBAdapter | None = None,
    object_reader: Callable[[str], bytes | None] | None = None,
    workspace_dir: Path | None = None,
    rclone_remote: str | None = None,
    rclone_drive_dir: str | None = None,
    rclone_bin: str = "rclone",
    backup_key_file: Path | None = None,
    pg_restore_bin: str = "pg_restore",
    pg_restore_compose_service: str | None = "postgres",
    pg_restore_compose_cwd: Path | None = None,
    pg_restore_docker_image: str | None = None,
    docker_bin: str = "docker",
) -> RecoveryResult:
    """Execute §4.1 / §8.5 read-only baseline recovery across all channels."""
    want_sha = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want_sha):
        raise ValueError(f"expected_sha256 must be a 64-char hex digest, got {expected_sha256!r}")

    checked = 0
    diagnostics: list[str] = []

    def _accept(data: bytes, channel: str, source_ref: str) -> RecoveryResult | None:
        nonlocal checked
        checked += 1
        actual = sha256_bytes(data)
        if actual == want_sha:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = output_path.with_name(output_path.name + ".recover.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, output_path)
            return RecoveryResult(
                found=True,
                channel=channel,
                output_path=str(output_path),
                sha256=actual,
                size_bytes=len(data),
                source_ref=source_ref,
                checked_candidates=checked,
                detail="verified against records[0].input_sha256",
                diagnostics=diagnostics,
            )
        return None

    def _check_upload_dir(filename_hint: str | None = None) -> RecoveryResult | None:
        if workspace_dir is None:
            return None
        up_dir = workspace_dir / "work" / "uploads"
        if not up_dir.is_dir():
            return None
        files = sorted(up_dir.glob("*.xlsx"))
        if filename_hint:
            base = Path(filename_hint).name
            prioritized = [p for p in files if p.name == base]
            files = prioritized + [p for p in files if p.name != base]
        for fp in files:
            res = _accept(fp.read_bytes(), "workspace_uploads", str(fp))
            if res is not None:
                return res
        return None

    # Channel 1: Large-file uploads (`dalton_chat_file_uploads`)
    if db is not None:
        for row in db.fetch_large_uploads(workspace_id, before_first_edit_ts):
            actual_col = str(row.get("actual_sha256") or "").strip().lower()
            if actual_col and actual_col != want_sha:
                checked += 1
                continue
            obj_key = str(row.get("object_key") or "")
            if object_reader and obj_key:
                blob = object_reader(obj_key)
                if blob is not None:
                    if actual_col and sha256_bytes(blob) != actual_col:
                        checked += 1
                        continue
                    res = _accept(blob, "large_file_upload", f"minio:{obj_key}")
                    if res is not None:
                        return res
        res_up = _check_upload_dir(expected_filename)
        if res_up is not None:
            return res_up

        # Channel 2: Turn attachment history (2a) + inline small attachments (2b)
        for turn_row in db.fetch_turn_attachments(workspace_id, before_first_edit_ts):
            att = turn_row.get("attachment_entry") or {}
            if isinstance(att, str):
                att = json.loads(att)
            att_name = str(att.get("name") or "")
            att_url = str(att.get("url") or "").strip()
            att_id = str(att.get("attachment_id") or "").strip()
            att_size = att.get("size")

            if att_url or att_id:
                key = extract_object_key_from_url(att_url)
                if not key and att_id and att_name:
                    key = f"attachments/{att_id}/{att_name}"
                if object_reader and key:
                    blob = object_reader(key)
                    if blob is not None:
                        res = _accept(blob, "small_attachment_url", f"minio:{key}")
                        if res is not None:
                            return res
                res_up2 = _check_upload_dir(att_name)
                if res_up2 is not None:
                    return res_up2
                continue

            # 2b: Inline small attachment without url/attachment_id
            turn_sender = turn_row.get("turn_sender_id")
            if turn_sender:
                sender_ids = [str(turn_sender)]
            else:
                sender_ids = []
                for fallback_id in (
                    turn_row.get("thread_created_by"),
                    turn_row.get("workspace_owner_id"),
                ):
                    if fallback_id and str(fallback_id) not in sender_ids:
                        sender_ids.append(str(fallback_id))
            if not sender_ids or not att_name or att_size is None:
                continue

            candidates = db.fetch_inline_candidates(
                sender_user_ids=sender_ids,
                att_name=att_name,
                att_size=int(att_size),
                before_ts=before_first_edit_ts,
                turn_created_at=str(turn_row.get("turn_created_at") or before_first_edit_ts),
            )
            for cand in candidates:
                cand_key = str(cand.get("object_key") or "").strip()
                if object_reader and cand_key:
                    blob = object_reader(cand_key)
                    if blob is not None:
                        res = _accept(
                            blob,
                            "inline_chat_attachment_object",
                            f"chat_attachments:{cand['attachment_id']}:{cand_key}",
                        )
                        if res is not None:
                            return res
                if cand.get("has_inline_content"):
                    blob = db.fetch_inline_content(str(cand["attachment_id"]))
                    if blob is not None:
                        res = _accept(
                            blob,
                            "inline_chat_attachment_content",
                            f"chat_attachments:{cand['attachment_id']}:content",
                        )
                        if res is not None:
                            return res

    # Channel 3: Encrypted rclone backup fallback (weekly/ MinIO archives + db/ daily Postgres dumps before cutoff)
    if rclone_remote and rclone_drive_dir and backup_key_file and backup_key_file.is_file():
        cutoff_dt = parse_iso_ts(before_first_edit_ts)
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d") if cutoff_dt else "9999-99-99"

        # 3a. Weekly MinIO / workspace tarballs (weekly/<YYYY-MM-DD>/*.enc)
        weekly_base = f"{rclone_remote}:{rclone_drive_dir}/weekly"
        ls_proc = subprocess.run(
            [rclone_bin, "lsf", "--dirs-only", f"{weekly_base}/"],
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_proc.returncode == 0:
            day_dirs = sorted(
                (d.strip().rstrip("/") for d in ls_proc.stdout.splitlines() if d.strip()),
                reverse=True,
            )
            for day in day_dirs:
                if day > cutoff_date:
                    continue
                obj_proc = subprocess.run(
                    [rclone_bin, "lsf", "--files-only", f"{weekly_base}/{day}/"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if obj_proc.returncode != 0:
                    continue
                for obj_name in sorted(obj_proc.stdout.splitlines()):
                    obj_name = obj_name.strip()
                    if not obj_name.endswith(".enc"):
                        continue
                    remote_obj = f"{weekly_base}/{day}/{obj_name}"
                    blob, member = recover_from_encrypted_tar_stream(
                        [rclone_bin, "cat", remote_obj],
                        backup_key_file,
                        expected_sha256=want_sha,
                        target_filename=expected_filename,
                        diagnostics=diagnostics,
                    )
                    if blob is not None:
                        res = _accept(
                            blob,
                            "rclone_encrypted_backup",
                            f"{remote_obj}!{member}",
                        )
                        if res is not None:
                            return res

        # 3b. Daily PostgreSQL offsite dumps (db/<YYYY-MM-DD>/*.enc) containing inline chat_attachments
        db_base = f"{rclone_remote}:{rclone_drive_dir}/db"
        ls_db = subprocess.run(
            [rclone_bin, "lsf", "--dirs-only", f"{db_base}/"],
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_db.returncode == 0:
            day_dirs = sorted(
                (d.strip().rstrip("/") for d in ls_db.stdout.splitlines() if d.strip()),
                reverse=True,
            )
            for day in day_dirs:
                if day > cutoff_date:
                    continue
                obj_proc = subprocess.run(
                    [rclone_bin, "lsf", "--files-only", f"{db_base}/{day}/"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if obj_proc.returncode != 0:
                    continue
                for obj_name in sorted(obj_proc.stdout.splitlines()):
                    obj_name = obj_name.strip()
                    if not obj_name.endswith(".enc"):
                        continue
                    remote_obj = f"{db_base}/{day}/{obj_name}"
                    blob, member = recover_from_encrypted_db_backup_stream(
                        [rclone_bin, "cat", remote_obj],
                        backup_key_file,
                        expected_sha256=want_sha,
                        pg_restore_bin=pg_restore_bin,
                        pg_restore_compose_service=pg_restore_compose_service,
                        pg_restore_compose_cwd=pg_restore_compose_cwd,
                        pg_restore_docker_image=pg_restore_docker_image,
                        docker_bin=docker_bin,
                        diagnostics=diagnostics,
                    )
                    if blob is not None:
                        res = _accept(
                            blob,
                            "rclone_encrypted_db_backup",
                            f"{remote_obj}!{member}",
                        )
                        if res is not None:
                            return res

    detail_msg = f"no candidate matched expected sha256 {want_sha}"
    if diagnostics:
        detail_msg += "; diagnostics: " + " | ".join(diagnostics)
    return RecoveryResult(
        found=False,
        channel="none",
        checked_candidates=checked,
        detail=detail_msg,
        diagnostics=diagnostics,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Everflow baseline model recovery (modeling v2 §4.1 & §8.5)."
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workspace-dir", default=None)
    parser.add_argument("--lineage-file", default=None)
    parser.add_argument("--before-ts", default=None)
    parser.add_argument("--expected-sha256", default=None)
    parser.add_argument("--expected-filename", default=None)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--sqlite-db", default=None)
    parser.add_argument("--object-store-dir", default=None)
    parser.add_argument("--minio-mc-alias", default=None)
    parser.add_argument("--minio-bucket", default=None)
    parser.add_argument("--rclone-remote", default=None)
    parser.add_argument("--rclone-drive-dir", default=None)
    parser.add_argument("--rclone-bin", default="rclone")
    parser.add_argument("--backup-key-file", default=None)
    parser.add_argument("--pg-restore-bin", default="pg_restore")
    parser.add_argument("--pg-restore-compose-service", default="postgres")
    parser.add_argument("--pg-restore-compose-cwd", default=None)
    parser.add_argument("--pg-restore-docker-image", default=None)
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--manifest-out", default=None)
    args = parser.parse_args(argv)

    ws_dir = Path(args.workspace_dir).expanduser().resolve() if args.workspace_dir else None
    lineage_p = Path(args.lineage_file).expanduser().resolve() if args.lineage_file else None
    anchor = extract_lineage_anchor(workspace_dir=ws_dir, lineage_file=lineage_p)

    before_ts = args.before_ts or anchor.get("before_first_edit_ts")
    expected_sha = args.expected_sha256 or anchor.get("input_sha256")
    expected_fn = args.expected_filename or anchor.get("input_file")
    if not before_ts or not expected_sha:
        print(
            json.dumps(
                {
                    "found": False,
                    "channel": "none",
                    "detail": "missing before_first_edit_ts or expected_sha256 (provide --lineage-file or explicit flags)",
                },
                ensure_ascii=False,
            )
        )
        return 1

    db: ReadOnlyDBAdapter | None = None
    try:
        if args.sqlite_db:
            db = SQLiteReadOnlyAdapter(args.sqlite_db)
        elif args.db_url:
            db = PostgresReadOnlyAdapter(args.db_url)

        obj_reader = build_object_reader(
            object_store_dir=Path(args.object_store_dir).expanduser().resolve()
            if args.object_store_dir
            else None,
            minio_mc_alias=args.minio_mc_alias,
            minio_bucket=args.minio_bucket,
        )
        result = recover_baseline(
            workspace_id=args.workspace_id,
            output_path=Path(args.output).expanduser().resolve(),
            before_first_edit_ts=str(before_ts),
            expected_sha256=str(expected_sha),
            expected_filename=str(expected_fn) if expected_fn else None,
            db=db,
            object_reader=obj_reader,
            workspace_dir=ws_dir,
            rclone_remote=args.rclone_remote,
            rclone_drive_dir=args.rclone_drive_dir,
            rclone_bin=args.rclone_bin,
            backup_key_file=Path(args.backup_key_file).expanduser().resolve()
            if args.backup_key_file
            else None,
            pg_restore_bin=args.pg_restore_bin,
            pg_restore_compose_service=(args.pg_restore_compose_service or None),
            pg_restore_compose_cwd=Path(args.pg_restore_compose_cwd).expanduser().resolve()
            if args.pg_restore_compose_cwd
            else None,
            pg_restore_docker_image=args.pg_restore_docker_image,
            docker_bin=args.docker_bin,
        )
    finally:
        if db is not None and hasattr(db, "close"):
            db.close()

    out_dict = result.to_dict()
    out_dict["lineage_anchor"] = anchor
    if args.manifest_out:
        mp = Path(args.manifest_out).expanduser().resolve()
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(out_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(out_dict, indent=2, ensure_ascii=False))
    return 0 if result.found else 1


if __name__ == "__main__":
    sys.exit(main())
