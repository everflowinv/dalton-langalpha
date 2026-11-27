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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tarfile
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Exact PostgreSQL read-only SQL queries specified in modeling-v2.md §8.5
# ---------------------------------------------------------------------------

SQL_PG_LARGE_UPLOADS = """
SELECT upload_id, workspace_id, filename, size_bytes, object_key,
       actual_sha256, state, created_at, used_at, cleaned_at
FROM dalton_chat_file_uploads
WHERE workspace_id = %(workspace_id)s::text
  AND created_at <= %(before_first_edit_ts)s
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
  AND q.created_at <= %(before_first_edit_ts)s
  AND COALESCE(att->>'name', '') ILIKE '%%.xlsx'
ORDER BY q.created_at ASC, q.turn_index ASC;
"""

SQL_PG_INLINE_CANDIDATES = """
SELECT attachment_id::text AS attachment_id, user_id, name, size,
       object_key, (content IS NOT NULL) AS has_inline_content,
       state, created_at, sent_at, cleaned_at
FROM chat_attachments
WHERE user_id = ANY(%(sender_user_ids)s)
  AND name = %(att_name)s
  AND size = %(att_size)s::bigint
  AND created_at <= LEAST(%(before_first_edit_ts)s, %(turn_created_at)s + INTERVAL '5 minutes')
  AND (sent_at IS NULL OR sent_at BETWEEN %(turn_created_at)s - INTERVAL '10 minutes'
                                      AND %(turn_created_at)s + INTERVAL '10 minutes')
ORDER BY ABS(EXTRACT(EPOCH FROM (COALESCE(sent_at, created_at) - %(turn_created_at)s))) ASC;
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

    def fetch_large_uploads(self, workspace_id: str, before_ts: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                SQL_PG_LARGE_UPLOADS,
                {"workspace_id": workspace_id, "before_first_edit_ts": before_ts},
            )
            return list(cur.fetchall())

    def fetch_turn_attachments(self, workspace_id: str, before_ts: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                SQL_PG_TURN_ATTACHMENTS,
                {"workspace_id": workspace_id, "before_first_edit_ts": before_ts},
            )
            return list(cur.fetchall())

    def fetch_inline_candidates(
        self,
        sender_user_ids: list[str],
        att_name: str,
        att_size: int,
        before_ts: str,
        turn_created_at: str,
    ) -> list[dict[str, Any]]:
        if not sender_user_ids:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                SQL_PG_INLINE_CANDIDATES,
                {
                    "sender_user_ids": sender_user_ids,
                    "att_name": att_name,
                    "att_size": int(att_size),
                    "before_first_edit_ts": before_ts,
                    "turn_created_at": turn_created_at,
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


def recover_from_encrypted_tar_stream(
    enc_stream_cmd: list[str],
    backup_key_file: Path,
    *,
    expected_sha256: str,
    target_filename: str | None = None,
) -> tuple[bytes | None, str | None]:
    """Stream-decrypt an AES-256-CBC PBKDF2 tar.gz.enc backup and match ``expected_sha256``."""
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

    target_base = Path(target_filename).name.lower() if target_filename else None
    try:
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if not member.name.lower().endswith(".xlsx"):
                    continue
                if target_base and Path(member.name).name.lower() != target_base:
                    # Still check hash in case object was stored under upload_id/object_key
                    pass
                fobj = tf.extractfile(member)
                if fobj is None:
                    continue
                data = fobj.read()
                if sha256_bytes(data) == expected_sha256.lower():
                    return data, member.name
    except tarfile.TarError:
        if sha256_bytes(decrypted) == expected_sha256.lower():
            return decrypted, "raw"
    return None, None


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
) -> RecoveryResult:
    """Execute §4.1 / §8.5 read-only baseline recovery across all channels."""
    want_sha = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want_sha):
        raise ValueError(f"expected_sha256 must be a 64-char hex digest, got {expected_sha256!r}")

    checked = 0

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

    # Channel 3: Encrypted rclone backup fallback (weekly MinIO/workspace archives before cutoff)
    if rclone_remote and rclone_drive_dir and backup_key_file and backup_key_file.is_file():
        cutoff_dt = parse_iso_ts(before_first_edit_ts)
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d") if cutoff_dt else "9999-99-99"
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
                    )
                    if blob is not None:
                        res = _accept(
                            blob,
                            "rclone_encrypted_backup",
                            f"{remote_obj}!{member}",
                        )
                        if res is not None:
                            return res

    return RecoveryResult(
        found=False,
        channel="none",
        checked_candidates=checked,
        detail=f"no candidate matched expected sha256 {want_sha}",
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
