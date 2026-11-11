#!/usr/bin/env python3
"""Persistent AI signal memory with Gemini embeddings and SQLite FTS fallback."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIM = 768
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}")


def dalton_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_db_path() -> Path:
    return dalton_root() / "state" / "ai_finance" / "ai_signal_memory.sqlite"


def resolve_explicit_path(path: Path) -> Path:
    """Resolve one caller-supplied path and refuse host assistant state directories."""
    if not isinstance(path, Path):
        path = Path(path)
    resolved = path.expanduser()
    resolved = resolved.resolve() if resolved.is_absolute() else (Path.cwd() / resolved).resolve()
    if any(part in {".openclaw", ".feishu-daily-ai-finance"} for part in resolved.parts):
        raise ValueError("refusing a path under host assistant state")
    return resolved


def embedding_settings(model: str | None = None, dim: int | None = None) -> tuple[str, int]:
    """Active model and dimension: explicit arguments, else the process environment."""
    chosen = (model if model is not None else os.environ.get("AI_SIGNAL_EMBEDDING_MODEL") or EMBEDDING_MODEL).strip()
    if not _MODEL_NAME.fullmatch(chosen):
        raise RuntimeError("embedding model name is not usable")
    if dim is None:
        raw = os.environ.get("AI_SIGNAL_EMBEDDING_DIM", "").strip()
        if raw:
            try:
                dim = int(raw)
            except ValueError as exc:
                raise RuntimeError("AI_SIGNAL_EMBEDDING_DIM must be an integer") from exc
        else:
            dim = EMBEDDING_DIM
    if type(dim) is not int or not 1 <= dim <= 4096:
        raise RuntimeError("embedding dimension is not usable")
    return chosen, dim


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect_db(path: Path) -> sqlite3.Connection:
    path = resolve_explicit_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # Autocommit so embedding HTTP calls cannot sit inside a write transaction.
    # ingest_records opens an explicit transaction for the signal row and its FTS row.
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
          id INTEGER PRIMARY KEY,
          record_key TEXT NOT NULL UNIQUE,
          source_type TEXT NOT NULL,
          external_id TEXT NOT NULL,
          event_time TEXT,
          title TEXT,
          entity TEXT,
          industry TEXT,
          url TEXT,
          summary TEXT NOT NULL,
          evidence TEXT,
          metrics_json TEXT NOT NULL DEFAULT '[]',
          signal_types_json TEXT NOT NULL DEFAULT '[]',
          signal_direction TEXT NOT NULL DEFAULT 'unspecified',
          evidence_status TEXT,
          qa_concern TEXT,
          management_response TEXT,
          management_response_status TEXT,
          tam_mapping TEXT,
          investment_implication TEXT,
          tags_json TEXT NOT NULL DEFAULT '[]',
          content_hash TEXT NOT NULL,
          embedding BLOB,
          embedding_dim INTEGER,
          embedding_model TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_signals_source_time
          ON signals(source_type, event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_signals_external
          ON signals(source_type, external_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS signals_fts USING fts5(
          record_key UNINDEXED,
          title,
          entity,
          industry,
          summary,
          evidence,
          tam_mapping,
          investment_implication,
          tags,
          tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    existing_columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(signals)").fetchall()
    }
    migrations = {
        "signal_direction": "TEXT NOT NULL DEFAULT 'unspecified'",
        "qa_concern": "TEXT",
        "management_response": "TEXT",
        "management_response_status": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {definition}")
    return conn


def load_api_key() -> str | None:
    value = os.environ.get("GEMINI_API_KEY")
    if value is None:
        return None
    text = value.strip()
    return text or None


def normalize_vector(values: Iterable[float]) -> list[float]:
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def embed_text(
    text: str,
    task_type: str,
    *,
    model: str | None = None,
    dim: int | None = None,
    api_key: str | None = None,
) -> list[float]:
    if task_type not in {"RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"}:
        raise ValueError("task_type must be RETRIEVAL_QUERY or RETRIEVAL_DOCUMENT")
    chosen_model, chosen_dim = embedding_settings(model, dim)
    key = load_api_key() if api_key is None else api_key.strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not available")
    payload = {
        "model": f"models/{chosen_model}",
        "content": {"parts": [{"text": text[:24000]}]},
        "taskType": task_type,
        "outputDimensionality": chosen_dim,
    }
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{chosen_model}:embedContent"
    )
    req = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urlopen(req, timeout=20) as response:
        data = json.load(response)
    values = data.get("embedding", {}).get("values") or []
    if len(values) != chosen_dim:
        raise RuntimeError(f"embedding dimension mismatch: {len(values)}")
    vector = [float(value) for value in values]
    if any(not math.isfinite(value) for value in vector):
        raise RuntimeError("embedding values must be finite")
    return normalize_vector(vector)


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes, dim: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dim}f", blob)


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def record_embedding_text(record: dict[str, Any]) -> str:
    fields = [
        f"Source: {record.get('source_type', '')}",
        f"Entity: {record.get('entity', '')}",
        f"Industry: {record.get('industry', '')}",
        f"Title: {record.get('title', '')}",
        f"Direction: {record.get('signal_direction', 'unspecified')}",
        f"Signal types: {', '.join(map(str, as_list(record.get('signal_types'))))}",
        f"Summary: {record.get('summary', '')}",
        f"Evidence: {record.get('evidence', '')}",
        f"Q&A concern: {record.get('qa_concern', '')}",
        f"Management response: {record.get('management_response', '')}",
        f"Management response status: {record.get('management_response_status', '')}",
        f"Metrics: {json.dumps(as_list(record.get('metrics')), ensure_ascii=False)}",
        f"TAM mapping: {record.get('tam_mapping', '')}",
        f"Investment implication: {record.get('investment_implication', '')}",
        f"Tags: {', '.join(map(str, as_list(record.get('tags'))))}",
    ]
    return "\n".join(fields)


def read_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(resolve_explicit_path(path).read_text(encoding="utf-8"))
    records = data.get("records", []) if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError("input must be a list or an object containing records[]")
    return [record for record in records if isinstance(record, dict)]


def canonical_record(record: dict[str, Any]) -> dict[str, Any]:
    source_type = str(record.get("source_type") or "").strip()
    external_id = str(record.get("external_id") or "").strip()
    summary = str(record.get("summary") or "").strip()
    if source_type not in {"github", "earnings"}:
        raise ValueError("source_type must be github or earnings")
    if not external_id or not summary:
        raise ValueError("each record requires source_type, external_id, and summary")
    event_time = str(record.get("event_time") or now_iso())
    record_key = str(record.get("record_key") or f"{source_type}:{external_id}:{event_time[:10]}")
    signal_direction = str(record.get("signal_direction") or "unspecified").strip().lower()
    if signal_direction not in {"positive", "negative", "mixed", "unspecified"}:
        raise ValueError(
            "signal_direction must be positive, negative, mixed, or unspecified"
        )
    response_status = str(record.get("management_response_status") or "").strip().lower()
    if response_status not in {"", "direct", "partial", "evasive_or_unanswered"}:
        raise ValueError(
            "management_response_status must be direct, partial, evasive_or_unanswered, or empty"
        )
    return {
        "record_key": record_key,
        "source_type": source_type,
        "external_id": external_id,
        "event_time": event_time,
        "title": str(record.get("title") or ""),
        "entity": str(record.get("entity") or ""),
        "industry": str(record.get("industry") or ""),
        "url": str(record.get("url") or ""),
        "summary": summary,
        "evidence": str(record.get("evidence") or ""),
        "metrics": as_list(record.get("metrics")),
        "signal_types": as_list(record.get("signal_types")),
        "signal_direction": signal_direction,
        "evidence_status": str(record.get("evidence_status") or ""),
        "qa_concern": str(record.get("qa_concern") or ""),
        "management_response": str(record.get("management_response") or ""),
        "management_response_status": response_status,
        "tam_mapping": str(record.get("tam_mapping") or ""),
        "investment_implication": str(record.get("investment_implication") or ""),
        "tags": as_list(record.get("tags")),
    }


def content_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(record_embedding_text(record).encode("utf-8")).hexdigest()


def update_fts(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute("DELETE FROM signals_fts WHERE record_key = ?", (record["record_key"],))
    conn.execute(
        """INSERT INTO signals_fts(
             record_key,title,entity,industry,summary,evidence,tam_mapping,
             investment_implication,tags
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            record["record_key"],
            record["title"],
            record["entity"],
            record["industry"],
            record["summary"],
            "\n".join(
                part for part in (
                    record["evidence"],
                    f"Q&A concern: {record['qa_concern']}" if record["qa_concern"] else "",
                    (
                        f"Management response ({record['management_response_status']}): "
                        f"{record['management_response']}"
                        if record["management_response"] else ""
                    ),
                ) if part
            ),
            record["tam_mapping"],
            record["investment_implication"],
            " ".join(
                map(str, record["tags"] + record["signal_types"] + [record["signal_direction"]])
            ),
        ),
    )


def _release_read(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()


def _vector_bytes_ok(blob: object, model: object, dim: object) -> bool:
    # sqlite3 may return BLOB as bytes or memoryview; both are the stored vector.
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if not isinstance(blob, (bytes, bytearray)) or not isinstance(model, str) or type(dim) is not int:
        return False
    if dim < 1 or len(blob) != dim * struct.calcsize("<f"):
        return False
    try:
        values = unpack_vector(bytes(blob), dim)
    except struct.error:
        return False
    return all(math.isfinite(value) for value in values)


def ingest_records(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    no_embeddings: bool,
    *,
    model: str | None = None,
    dim: int | None = None,
) -> dict[str, Any]:
    active_model, active_dim = embedding_settings(model, dim)
    staged: list[tuple[dict[str, Any], str, bytes | None, str | None, int | None, str, bool]] = []
    embedding_errors: list[str] = []
    for raw in records:
        record = canonical_record(raw)
        previous = conn.execute(
            """SELECT id, content_hash, embedding, embedding_dim, embedding_model
               FROM signals WHERE record_key = ?""",
            (record["record_key"],),
        ).fetchone()
        _release_read(conn)
        digest = content_hash(record)
        same_body = previous is not None and previous["content_hash"] == digest
        embedding_blob: bytes | None = None
        stored_model: str | None = None
        stored_dim: int | None = None
        reusable = (
            same_body
            and previous["embedding_model"] == active_model
            and previous["embedding_dim"] == active_dim
            and _vector_bytes_ok(previous["embedding"], previous["embedding_model"], previous["embedding_dim"])
        )
        if reusable:
            embedding_blob = bytes(previous["embedding"])
            stored_model = active_model
            stored_dim = active_dim
        elif not no_embeddings:
            try:
                _release_read(conn)
                vector = embed_text(
                    record_embedding_text(record),
                    "RETRIEVAL_DOCUMENT",
                    model=active_model,
                    dim=active_dim,
                )
                if len(vector) != active_dim or any(not math.isfinite(float(value)) for value in vector):
                    raise RuntimeError("embedding is not a finite vector of the configured dimension")
                embedding_blob = pack_vector(vector)
                stored_model = active_model
                stored_dim = active_dim
            except (RuntimeError, HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                embedding_errors.append(f"{record['record_key']}: {type(exc).__name__}")
                # A changed body must not keep the previous vector under the new hash.
                # The same body may keep a vector only with the model and dimension that produced it.
                if same_body and _vector_bytes_ok(
                    previous["embedding"], previous["embedding_model"], previous["embedding_dim"]
                ):
                    embedding_blob = bytes(previous["embedding"])
                    stored_model = previous["embedding_model"]
                    stored_dim = previous["embedding_dim"]
        elif same_body and _vector_bytes_ok(
            previous["embedding"], previous["embedding_model"], previous["embedding_dim"]
        ):
            embedding_blob = bytes(previous["embedding"])
            stored_model = previous["embedding_model"]
            stored_dim = previous["embedding_dim"]
        staged.append((record, digest, embedding_blob, stored_model, stored_dim, now_iso(), previous is not None))

    _release_read(conn)
    inserted = 0
    updated = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for record, digest, embedding_blob, stored_model, stored_dim, stamp, existed in staged:
            conn.execute(
                """INSERT INTO signals(
                     record_key,source_type,external_id,event_time,title,entity,industry,url,
                     summary,evidence,metrics_json,signal_types_json,signal_direction,evidence_status,
                     qa_concern,management_response,management_response_status,
                     tam_mapping,investment_implication,tags_json,content_hash,embedding,
                     embedding_dim,embedding_model,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(record_key) DO UPDATE SET
                     source_type=excluded.source_type, external_id=excluded.external_id,
                     event_time=excluded.event_time, title=excluded.title, entity=excluded.entity,
                     industry=excluded.industry, url=excluded.url, summary=excluded.summary,
                     evidence=excluded.evidence, metrics_json=excluded.metrics_json,
                     signal_types_json=excluded.signal_types_json,
                     signal_direction=excluded.signal_direction,
                     evidence_status=excluded.evidence_status,
                     qa_concern=excluded.qa_concern,
                     management_response=excluded.management_response,
                     management_response_status=excluded.management_response_status,
                     tam_mapping=excluded.tam_mapping,
                     investment_implication=excluded.investment_implication,
                     tags_json=excluded.tags_json, content_hash=excluded.content_hash,
                     embedding=excluded.embedding,
                     embedding_dim=excluded.embedding_dim,
                     embedding_model=excluded.embedding_model,
                     updated_at=excluded.updated_at""",
                (
                    record["record_key"], record["source_type"], record["external_id"],
                    record["event_time"], record["title"], record["entity"], record["industry"],
                    record["url"], record["summary"], record["evidence"],
                    json.dumps(record["metrics"], ensure_ascii=False),
                    json.dumps(record["signal_types"], ensure_ascii=False), record["signal_direction"],
                    record["evidence_status"], record["qa_concern"],
                    record["management_response"], record["management_response_status"],
                    record["tam_mapping"], record["investment_implication"],
                    json.dumps(record["tags"], ensure_ascii=False), digest, embedding_blob,
                    stored_dim, stored_model,
                    stamp, stamp,
                ),
            )
            update_fts(conn, record)
            if existed:
                updated += 1
            else:
                inserted += 1
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {"inserted": inserted, "updated": updated, "embedding_errors": embedding_errors}


def fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", query.lower())
    return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:20])


def tags_from_row(row: sqlite3.Row) -> set[str]:
    try:
        return {str(tag).lower() for tag in json.loads(row["tags_json"] or "[]")}
    except json.JSONDecodeError:
        return set()


def serialize_match(row: sqlite3.Row, similarity: float | None, shared_tags: set[str]) -> dict[str, Any]:
    return {
        "record_key": row["record_key"],
        "source_type": row["source_type"],
        "external_id": row["external_id"],
        "event_time": row["event_time"],
        "title": row["title"],
        "entity": row["entity"],
        "industry": row["industry"],
        "summary": row["summary"],
        "evidence": row["evidence"],
        "evidence_status": row["evidence_status"],
        "signal_types": json.loads(row["signal_types_json"] or "[]"),
        "signal_direction": row["signal_direction"],
        "qa_concern": row["qa_concern"],
        "management_response": row["management_response"],
        "management_response_status": row["management_response_status"],
        "tam_mapping": row["tam_mapping"],
        "investment_implication": row["investment_implication"],
        "url": row["url"],
        "embedding_similarity": round(similarity, 4) if similarity is not None else None,
        "shared_tags": sorted(shared_tags),
    }


def vector_search(
    conn: sqlite3.Connection,
    query_text: str,
    limit: int,
    exclude_keys: set[str] | None = None,
    source_type: str | None = None,
    query_tags: set[str] | None = None,
    *,
    model: str | None = None,
    dim: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    exclude_keys = exclude_keys or set()
    query_tags = query_tags or set()
    history_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    if history_count == 0:
        return [], "empty_history"
    chosen_model, chosen_dim = embedding_settings(model, dim)

    try:
        _release_read(conn)
        query_vector = embed_text(query_text, "RETRIEVAL_QUERY", model=chosen_model, dim=chosen_dim)
    except (RuntimeError, HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        query_vector = []

    where = "WHERE embedding IS NOT NULL AND embedding_model = ? AND embedding_dim = ?"
    params: list[Any] = [chosen_model, chosen_dim]
    if source_type:
        where += " AND source_type = ?"
        params.append(source_type)
    rows = conn.execute(f"SELECT * FROM signals {where}", params).fetchall()
    if query_vector and rows:
        scored = []
        for row in rows:
            if row["record_key"] in exclude_keys or not _vector_bytes_ok(row["embedding"], row["embedding_model"], row["embedding_dim"]):
                continue
            score = cosine(query_vector, unpack_vector(row["embedding"], chosen_dim))
            shared = query_tags & tags_from_row(row)
            scored.append((score + min(len(shared), 3) * 0.015, score, row, shared))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [serialize_match(row, raw_score, shared) for _, raw_score, row, shared in scored[:limit]], "embedding"

    expression = fts_query(query_text)
    if not expression:
        return [], "none"
    fts_rows = conn.execute(
        """SELECT s.* FROM signals_fts f
           JOIN signals s ON s.record_key=f.record_key
           WHERE signals_fts MATCH ?
           ORDER BY bm25(signals_fts) LIMIT ?""",
        (expression, limit * 3),
    ).fetchall()
    matches = []
    for row in fts_rows:
        if row["record_key"] in exclude_keys or (source_type and row["source_type"] != source_type):
            continue
        matches.append(serialize_match(row, None, query_tags & tags_from_row(row)))
        if len(matches) >= limit:
            break
    return matches, "fts"


def compare_records(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    limit: int,
    *,
    model: str | None = None,
    dim: int | None = None,
) -> dict[str, Any]:
    history = stats(conn, model=model, dim=dim)
    comparisons = []
    modes: set[str] = set()
    current_keys = {canonical_record(record)["record_key"] for record in records}
    for raw in records:
        record = canonical_record(raw)
        tags = {str(tag).lower() for tag in record["tags"] + record["signal_types"]}
        matches, mode = vector_search(
            conn,
            record_embedding_text(record),
            limit,
            exclude_keys=current_keys,
            query_tags=tags,
            model=model,
            dim=dim,
        )
        modes.add(mode)
        comparisons.append({"current": record, "matches": matches})
    return {
        "search_modes": sorted(modes),
        "history_signals_before_ingest": history["signals"],
        "history_embedded_signals_before_ingest": history["embedded_signals"],
        "history_embedding_status_before_ingest": history["embedding_status"],
        "comparisons": comparisons,
    }


def _coverage_status(signal_count: int, embedded_count: int) -> str:
    if signal_count == 0:
        return "empty"
    if embedded_count == signal_count:
        return "complete"
    if embedded_count == 0:
        return "none"
    return "partial"


def stats(
    conn: sqlite3.Connection, *, model: str | None = None, dim: int | None = None
) -> dict[str, Any]:
    chosen_model, chosen_dim = embedding_settings(model, dim)
    signal_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    any_embedded = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    embedded_count = 0
    for row in conn.execute(
        """SELECT embedding, embedding_dim, embedding_model FROM signals
           WHERE embedding IS NOT NULL AND embedding_model = ? AND embedding_dim = ?""",
        (chosen_model, chosen_dim),
    ):
        if _vector_bytes_ok(row["embedding"], row["embedding_model"], row["embedding_dim"]):
            embedded_count += 1
    embedding_status = _coverage_status(signal_count, embedded_count)
    return {
        "signals": signal_count,
        "embedded_signals": embedded_count,
        "embedded_signals_any_model": any_embedded,
        "embedding_status": embedding_status,
        "embedding_coverage_pct": (
            round(embedded_count / signal_count * 100, 2) if signal_count else 0.0
        ),
        "configured_embedding_model": chosen_model,
        "configured_embedding_dim": chosen_dim,
        "signals_by_source": dict(
            conn.execute("SELECT source_type,COUNT(*) FROM signals GROUP BY source_type").fetchall()
        ),
        "signals_by_direction": dict(
            conn.execute(
                "SELECT signal_direction,COUNT(*) FROM signals GROUP BY signal_direction"
            ).fetchall()
        ),
        "embedding_models": dict(
            conn.execute(
                """SELECT embedding_model,COUNT(*) FROM signals
                   WHERE embedding IS NOT NULL GROUP BY embedding_model"""
            ).fetchall()
        ),
        "db_bytes": Path(conn.execute("PRAGMA database_list").fetchone()[2]).stat().st_size,
    }


def self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "test.sqlite"
        conn = connect_db(db_path)
        rows, mode = vector_search(conn, "AI customer service costs", 5)
        assert mode == "empty_history" and not rows
        sample = [{
            "source_type": "earnings",
            "external_id": "demo-1",
            "event_time": "2026-07-22T00:00:00Z",
            "entity": "Demo Co",
            "summary": "AI automation reduced customer service cost",
            "signal_types": ["cost_productivity"],
            "signal_direction": "positive",
            "tags": ["customer-service", "ai-automation"],
        }]
        result = ingest_records(conn, sample, no_embeddings=True)
        assert result["inserted"] == 1
        assert stats(conn)["signals"] == 1
        assert stats(conn)["signals_by_direction"] == {"positive": 1}
        assert stats(conn)["embedding_status"] == "none"
        rows, mode = vector_search(conn, "AI customer service costs", 5)
        assert mode == "fts" and rows
        conn.close()
    print(json.dumps({"ok": True, "tests": 6}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("stats")
    sub.add_parser("self-test")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--input", type=Path, required=True)
    ingest.add_argument("--no-embeddings", action="store_true")

    compare = sub.add_parser("compare")
    compare.add_argument("--input", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    compare.add_argument("--limit", type=int, default=8)

    search = sub.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--source-type")

    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
        return 0
    db_path = args.db or default_db_path()
    conn = connect_db(db_path)
    if args.command == "init":
        result: Any = {"ok": True, "db": str(db_path)}
    elif args.command == "stats":
        result = stats(conn)
    elif args.command == "ingest":
        result = ingest_records(conn, read_records(args.input), args.no_embeddings)
    elif args.command == "compare":
        result = compare_records(conn, read_records(args.input), max(1, args.limit))
    elif args.command == "search":
        matches, mode = vector_search(
            conn, args.query, max(1, args.limit), source_type=args.source_type
        )
        result = {"search_mode": mode, "query": args.query, "matches": matches}
    else:
        raise AssertionError(args.command)
    conn.close()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    output = getattr(args, "output", None)
    if output:
        output = resolve_explicit_path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
