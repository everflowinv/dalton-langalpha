"""Commercial local-library connectors (`sales-notes`, `tmtb`, `funda-ai`) and sales-notes ingest."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.dalton.capabilities import CapabilityId
from src.dalton.research_host.wiki_store import (
    OpenWikiStore,
    _IMAGE_FILE_EXTENSIONS,
    detect_ingest_secret,
    extract_ingest_attachment,
    raw_bytes_scan_text,
)

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import funda_ai as funda_ai_mod  # noqa: E402
import research_library as research_library_mod  # noqa: E402
import sales_notes_store as sales_notes_store_mod  # noqa: E402

KEY_SQL = research_library_mod.KEY_SQL
_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_strict_ymd(raw: str) -> str:
    cleaned = str(raw or "").strip()
    if not _YMD_RE.fullmatch(cleaned):
        raise ValueError("date must be a valid YYYY-MM-DD calendar date")
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError("date must be a valid YYYY-MM-DD calendar date") from exc
    if parsed.isoformat() != cleaned:
        raise ValueError("date must be a valid YYYY-MM-DD calendar date")
    return cleaned


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_source(server_name: str, route: str, item: dict[str, Any]) -> dict[str, Any]:
    doc_id = str(item.get("document_id") or "")
    return {
        "document_id": doc_id[:200],
        "identifier": doc_id[:200],
        "route": route,
        "source_type": "research_doc",
        "identity": "internal_note",
        "detail": "internal_note",
        "publisher": str(item.get("original_source") or route)[:120],
        "provider": f"{server_name}:{route}",
        "title": str(item.get("title") or doc_id)[:300],
        "as_of": str(item.get("date") or "")[:10],
        "url": item.get("url") if isinstance(item.get("url"), str) else None,
        "private": True,
    }


class MarketDigestPartitionStore:
    """Shared SQLite reader/writer over `skills/market-digest/vectors.db` with strict SQL partitioning.

    - `sales-notes`: strictly enforces `KEY_SQL NOT LIKE 'alphaengine:%'` (plus `chunks` digests).
    - `tmtb`: strictly enforces `KEY_SQL LIKE 'alphaengine:%'`.
    """

    def __init__(self, data_root: Path | str, *, code_root: Path | str | None = None) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.code_root = (
            Path(code_root).expanduser().resolve()
            if code_root is not None
            else _SCRIPTS_DIR.parent.resolve()
        )
        self.mail_db = self.data_root / "skills" / "market-digest" / "vectors.db"
        self.notes_dir = sales_notes_store_mod.home(self.data_root)
        self.manifest_path = sales_notes_store_mod.manifest_path(self.data_root)
        self.audit_path = self.notes_dir / "ingest_audit.jsonl"
        self._write_lock = asyncio.Lock()

    def ensure_schema(self) -> None:
        self.mail_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.mail_db, timeout=20) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS email_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    email_id TEXT,
                    message_id TEXT,
                    sender TEXT,
                    subject TEXT,
                    chunk_index INTEGER DEFAULT 0,
                    content TEXT,
                    embedding BLOB,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    period TEXT,
                    section_id TEXT,
                    section_label TEXT,
                    content TEXT,
                    source_emails TEXT,
                    embedding BLOB,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sales_ingest_receipts (
                    request_id TEXT PRIMARY KEY,
                    doc_key TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        return sales_notes_store_mod.load_manifest(self.data_root)

    def _library(self) -> research_library_mod.Library:
        return research_library_mod.Library(data_root=self.data_root, code_root=self.code_root)

    def _load_embedding_config(self) -> dict[str, Any]:
        candidates = (
            self.data_root / "config" / "config.json",
            self.data_root / "skills" / "market-digest" / "config.json",
            self.code_root / "skills" / "market-digest" / "config.json",
            self.code_root / "skills" / "market-digest" / "config.example.json",
        )
        for cfg_path in candidates:
            if cfg_path.is_file():
                try:
                    loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("embedding"), dict):
                        return loaded
                except Exception:
                    continue
        return {
            "embedding": {
                "model": os.environ.get("DALTON_EMBEDDING_MODEL", "gemini-embedding-2"),
                "dimensions": 3072,
            }
        }

    def _embed_texts(self, texts: list[str]) -> list[bytes | None]:
        """Compute float32 embedding BLOBs for sales-note chunks; degrades cleanly to None if unavailable."""
        if not texts:
            return []
        try:
            import numpy as np

            mod = self._library().module("sales-notes")
            cfg = self._load_embedding_config()
            with contextlib.redirect_stdout(io.StringIO()):
                vectors = mod.embed_texts(texts, cfg)
            if not isinstance(vectors, (list, tuple)) or len(vectors) != len(texts):
                return [None] * len(texts)
            blobs: list[bytes | None] = []
            for vec in vectors:
                arr = np.asarray(vec, dtype=np.float32)
                norm = float(np.linalg.norm(arr))
                if arr.ndim != 1 or arr.size == 0 or not np.isfinite(norm) or norm == 0.0:
                    blobs.append(None)
                else:
                    blobs.append(arr.tobytes())
            return blobs
        except (Exception, SystemExit):
            return [None] * len(texts)

    def _embed_query(self, lib: research_library_mod.Library, source: str, query: str) -> Any:
        mod = lib.module(source)
        cfg = self._load_embedding_config()
        with contextlib.redirect_stdout(io.StringIO()):
            return mod.embed_query(query, cfg)

    @staticmethod
    def _merge_keyword_and_semantic(
        semantic_results: list[dict[str, Any]],
        keyword_results: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fuse semantic and keyword document ranks via RRF (k=60) so neither arm buries the other."""
        rrf_k = 60.0
        fused: dict[str, dict[str, Any]] = {}
        sem_ranked = [
            it
            for it in semantic_results
            if isinstance(it, dict) and it.get("score") is not None
        ]
        kw_ranked = [
            it
            for it in keyword_results
            if isinstance(it, dict) and int(it.get("keyword_hits") or 0) > 0
        ]

        def _doc_key(item: dict[str, Any]) -> str:
            return str(item.get("document_id") or item.get("canonical_id") or id(item))

        for arm, items in (("semantic", sem_ranked), ("keyword", kw_ranked)):
            for rank, item in enumerate(items, start=1):
                k = _doc_key(item)
                entry = fused.get(k)
                if entry is None:
                    merged_item = dict(item)
                    merged_item["matched"] = [arm]
                    merged_item["rrf_score"] = 1.0 / (rrf_k + rank)
                    fused[k] = merged_item
                else:
                    entry["rrf_score"] = float(entry.get("rrf_score") or 0.0) + 1.0 / (rrf_k + rank)
                    matched = list(entry.get("matched") or [])
                    if arm not in matched:
                        matched.append(arm)
                    entry["matched"] = matched
                    if entry.get("score") is None and item.get("score") is not None:
                        entry["score"] = item.get("score")
                    entry["keyword_hits"] = max(
                        int(entry.get("keyword_hits") or 0),
                        int(item.get("keyword_hits") or 0),
                    )
                    if not entry.get("snippet") and item.get("snippet"):
                        entry["snippet"] = item.get("snippet")

        ranked = sorted(
            fused.values(),
            key=lambda x: (
                float(x.get("rrf_score") or 0.0),
                float(x["score"]) if x.get("score") is not None else -1.0,
                int(x.get("keyword_hits") or 0),
            ),
            reverse=True,
        )
        return ranked[:limit]

    def read(self, source: str, document: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.mail_db.is_file():
            raise ValueError("Document not found")
        res = self._library().mail(source, "read", None, str(document or "").strip(), None, None, 1, dict(params or {}))
        doc = res.get("document") if isinstance(res, dict) else None
        srv = "dalton_tmtb" if source == "tmtb" else "dalton_sales_notes"
        out = dict(res)
        if isinstance(doc, dict):
            out["_sources"] = [_make_source(srv, source, doc)]
        return out

    def query(
        self,
        source: str,
        operation: str,
        *,
        query: str | None = None,
        document: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        p = dict(params or {})
        if operation == "read":
            return self.read(source, str(document or ""), p)
        if not self.mail_db.is_file():
            return {
                "ok": True,
                "status": "empty",
                "results": [],
                "result_count": 0,
                "cursor": None,
                "has_more": False,
                "total_upstream": 0,
                "returned": 0,
                "warnings": [],
            }
        capped_limit = max(1, min(100, int(limit)))
        if source == "tmtb" and str(p.get("layer") or "raw") != "raw":
            raise ValueError("tmtb only supports raw layer")
        mail_params = dict(p)
        requested_mode = str(mail_params.setdefault("search_mode", "hybrid" if operation == "search" else "keyword")).strip().lower()
        lib = self._library()
        if operation == "search" and requested_mode != "keyword":
            lib.embed = lambda src, q: self._embed_query(lib, src, q)  # type: ignore[method-assign]
            sem_res = lib.mail(
                source,
                operation,
                query,
                document,
                start,
                end,
                capped_limit,
                mail_params,
            )
            sem_warnings = list(sem_res.get("warnings") or [])
            semantic_degraded = any("语义检索不可用" in str(w) for w in sem_warnings)
            if semantic_degraded:
                res = sem_res
            else:
                kw_params = {**mail_params, "search_mode": "keyword"}
                kw_res = lib.mail(
                    source,
                    operation,
                    query,
                    document,
                    start,
                    end,
                    capped_limit,
                    kw_params,
                )
                merged_results = self._merge_keyword_and_semantic(
                    list(sem_res.get("results") or []),
                    list(kw_res.get("results") or []),
                    limit=capped_limit,
                )
                merged_warnings = list(dict.fromkeys([*sem_warnings, *list(kw_res.get("warnings") or [])]))
                res = {
                    **sem_res,
                    "results": merged_results,
                    "warnings": merged_warnings,
                    "status": "partial" if merged_warnings else ("ok" if merged_results else "empty"),
                }
        else:
            res = lib.mail(
                source,
                operation,
                query,
                document,
                start,
                end,
                capped_limit,
                mail_params,
            )
        results = list(res.get("results") or [])
        srv = "dalton_tmtb" if source == "tmtb" else "dalton_sales_notes"
        sources_list = [_make_source(srv, source, it) for it in results[:50]]
        out: dict[str, Any] = {
            **res,
            "result_count": len(results),
            "cursor": None,
            "has_more": False,
            "total_upstream": len(results),
            "returned": len(results),
            "retrieved_at": _now_iso(),
        }
        if sources_list:
            out["_sources"] = sources_list
        return out

    def _find_db_receipt_by_request_id(self, request_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not request_id or not self.mail_db.is_file():
            return None
        try:
            with sqlite3.connect(self.mail_db, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT manifest_json, receipt_json FROM sales_ingest_receipts WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            if row and row["receipt_json"]:
                return json.loads(row["manifest_json"]), json.loads(row["receipt_json"])
        except (sqlite3.Error, ValueError):
            return None
        return None

    def _find_db_receipt_by_doc_key(self, doc_key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not doc_key or not self.mail_db.is_file():
            return None
        try:
            with sqlite3.connect(self.mail_db, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT manifest_json, receipt_json FROM sales_ingest_receipts WHERE doc_key = ?",
                    (doc_key,),
                ).fetchone()
            if row and row["receipt_json"]:
                return json.loads(row["manifest_json"]), json.loads(row["receipt_json"])
        except (sqlite3.Error, ValueError):
            return None
        return None

    def _find_audit_by_doc_key(self, doc_key: str) -> dict[str, Any] | None:
        if not doc_key or not self.audit_path.is_file():
            return None
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("doc_key") == doc_key:
                return parsed
        return None

    @staticmethod
    def _chunk_sales_note(body: str, prefix: str, *, max_chunk_chars: int = 1500) -> list[str]:
        """Paragraph-packed chunker matching `market_digest_cli.chunk_note`."""
        pieces: list[str] = []
        for para in re.split(r"[\r\n]{2,}", body):
            para = para.strip()
            while len(para) > max_chunk_chars:
                cut = para.rfind("\n", 0, max_chunk_chars)
                if cut < max_chunk_chars // 2:
                    cut = max_chunk_chars
                pieces.append(para[:cut].strip())
                para = para[cut:].strip()
            if para:
                pieces.append(para)
        chunks: list[str] = []
        current = ""
        for para in pieces:
            if current and len(current) + len(para) + 2 > max_chunk_chars:
                chunks.append(prefix + current)
                current = para
            else:
                current = (current + "\n\n" + para) if current else para
        if current:
            chunks.append(prefix + current)
        return chunks or [prefix + body]

    def _find_audit_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        if not request_id or not self.audit_path.is_file():
            return None
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("request_id") == request_id:
                return parsed
        return None

    def _append_audit_entry(self, receipt: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, ensure_ascii=False) + "\n")

    async def ingest_sales_note(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Idempotent `POST /ingest/sales-notes` writing to `state/sales-notes/` and `email_chunks`."""
        if bool(body.get("dry_run", False)):
            return await asyncio.to_thread(self._ingest_sales_note_sync, body)
        async with self._write_lock:
            return await asyncio.to_thread(self._ingest_sales_note_sync, body)

    def _ingest_sales_note_sync(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        institution = str(body.get("institution") or "").strip()
        title = str(body.get("title") or "").strip()
        if not institution:
            return 400, {"ok": False, "status": "bad_request", "error_code": "missing_institution", "error": "institution is required"}
        if not title:
            return 400, {"ok": False, "status": "bad_request", "error_code": "missing_title", "error": "title is required"}

        raw_content = body.get("content")
        raw_file = body.get("file")
        has_content = isinstance(raw_content, str) and bool(raw_content.strip())
        has_file = isinstance(raw_file, dict) and bool(raw_file.get("content_b64"))
        if has_content == has_file:
            return 400, {"ok": False, "status": "bad_request", "error": "Provide exactly one of content or file"}

        binary_bytes: bytes | None = None
        orig_binary_name: str | None = None
        orig_binary_suffix: str = ""
        raw_attachment_scan: str = ""
        if has_file and isinstance(raw_file, dict):
            try:
                file_bytes = base64.b64decode(str(raw_file["content_b64"]))
            except Exception:
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_base64", "error": "Invalid base64 payload"}
            raw_fname = Path(str(raw_file.get("filename") or "note.md")).name
            raw_attachment_scan = raw_bytes_scan_text(file_bytes, suffix=Path(raw_fname).suffix)
            try:
                body_text, is_binary, orig_suffix = extract_ingest_attachment(file_bytes, raw_fname)
            except ValueError as exc:
                err_code = (
                    "empty_pdf_text"
                    if "extractable text layer" in str(exc).lower()
                    else "unsupported_file_format"
                )
                return 400, {"ok": False, "status": "bad_request", "error_code": err_code, "error": str(exc)}
            digest_bytes = file_bytes
            if is_binary:
                binary_bytes = file_bytes
                safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(raw_fname).stem).strip("-._") or "attachment"
                orig_binary_name = f"{safe_stem}{orig_suffix}"
                orig_binary_suffix = orig_suffix
        else:
            body_text = str(raw_content or "")
            digest_bytes = re.sub(r"\s+", " ", body_text).strip().encode("utf-8")

        secret_kind = detect_ingest_secret(raw_attachment_scan, body_text, title, str(body.get("source_url") or ""))
        if secret_kind is not None:
            return 400, {
                "ok": False,
                "status": "secret_detected",
                "error_code": "secret_detected",
                "secret_kind": secret_kind,
                "error": f"Detected suspected credential/secret ({secret_kind}); remove secrets before ingesting.",
            }

        sha_hex = hashlib.sha256(digest_bytes).hexdigest()
        doc_key = sales_notes_store_mod.content_key(digest_bytes)
        request_id = str(body.get("request_id") or "").strip() or None
        dry_run = bool(body.get("dry_run", False))
        raw_date = body.get("date")
        if raw_date is not None and str(raw_date).strip():
            try:
                received_date = _parse_strict_ymd(str(raw_date))
            except ValueError as exc:
                return 400, {
                    "ok": False,
                    "status": "invalid_date",
                    "error_code": "invalid_date",
                    "error": str(exc),
                }
        else:
            received_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self.ensure_schema()
        manifest = self._load_manifest()
        if not dry_run:
            if request_id:
                db_pair = self._find_db_receipt_by_request_id(request_id)
                audit_prior = self._find_audit_by_request_id(request_id)
                if db_pair is not None:
                    manifest_entry, receipt_prior = db_pair
                    if manifest_entry.get("doc_key") not in manifest:
                        sales_notes_store_mod.append_manifest(self.data_root, manifest_entry)
                    if audit_prior is None:
                        self._append_audit_entry(receipt_prior)
                    return 200, {**receipt_prior, "ok": True, "status": "ingested", "idempotent_replay": True}
                if audit_prior is not None and audit_prior.get("status") in ("ingested", "done"):
                    return 200, {**audit_prior, "ok": True, "status": "ingested", "idempotent_replay": True}
            else:
                db_doc_pair = self._find_db_receipt_by_doc_key(doc_key)
                if db_doc_pair is not None:
                    manifest_entry, receipt_prior = db_doc_pair
                    need_manifest = manifest_entry.get("doc_key") not in manifest
                    need_audit = self._find_audit_by_doc_key(doc_key) is None
                    if need_manifest or need_audit:
                        if need_manifest:
                            sales_notes_store_mod.append_manifest(self.data_root, manifest_entry)
                        if need_audit:
                            self._append_audit_entry(receipt_prior)
                        return 200, {**receipt_prior, "ok": True, "status": "ingested", "idempotent_replay": True}

        if doc_key in manifest or self._find_db_receipt_by_doc_key(doc_key) is not None:
            return 200, {
                "ok": False,
                "status": "duplicate",
                "duplicate": True,
                "doc_key": doc_key,
                "document_id": doc_key,
                "error": f"Duplicate sales note already exists: {doc_key}",
            }

        if dry_run:
            return 200, {
                "ok": True,
                "status": "preview",
                "dry_run": True,
                "doc_key": doc_key,
                "institution": institution,
                "title": title,
                "date": received_date,
                "duplicate": False,
            }

        self.notes_dir.mkdir(parents=True, exist_ok=True)
        slug = sales_notes_store_mod.slug(title, limit=40)
        base_name = f"{received_date}-{slug}-{sha_hex[:8]}.md"
        note_rel = f"state/sales-notes/{base_name}"
        notes_root = self.notes_dir.resolve()
        note_abs = (self.data_root / note_rel).resolve()
        if not note_abs.is_relative_to(notes_root):
            return 400, {
                "ok": False,
                "status": "invalid_path",
                "error_code": "invalid_path",
                "error": "Resolved sales-note path escapes state/sales-notes directory",
            }
        # Never overwrite an existing file on collision
        counter = 1
        while note_abs.exists():
            counter += 1
            note_rel = f"state/sales-notes/{received_date}-{slug}-{sha_hex[:8]}-{counter}.md"
            note_abs = (self.data_root / note_rel).resolve()
            if not note_abs.is_relative_to(notes_root):
                return 400, {
                    "ok": False,
                    "status": "invalid_path",
                    "error_code": "invalid_path",
                    "error": "Resolved sales-note path escapes state/sales-notes directory",
                }

        orig_rel: str | None = None
        orig_abs: Path | None = None
        if binary_bytes is not None and orig_binary_name is not None:
            orig_rel = f"state/sales-notes/originals/{orig_binary_name}"
            orig_abs = (self.data_root / orig_rel).resolve()
            if not orig_abs.is_relative_to(notes_root):
                return 400, {
                    "ok": False,
                    "status": "invalid_path",
                    "error_code": "invalid_path",
                    "error": "Resolved original attachment escapes state/sales-notes directory",
                }
            if orig_abs.exists():
                b_stem = Path(orig_binary_name).stem
                b_suf = Path(orig_binary_name).suffix
                cand_bin = f"{b_stem}-{sha_hex[:8]}{b_suf}"
                cnt = 1
                while (orig_abs.parent / cand_bin).exists():
                    cnt += 1
                    cand_bin = f"{b_stem}-{sha_hex[:8]}-{cnt}{b_suf}"
                orig_binary_name = cand_bin
                orig_rel = f"state/sales-notes/originals/{orig_binary_name}"
                orig_abs = (self.data_root / orig_rel).resolve()
                if not orig_abs.is_relative_to(notes_root):
                    return 400, {
                        "ok": False,
                        "status": "invalid_path",
                        "error_code": "invalid_path",
                        "error": "Resolved original attachment escapes state/sales-notes directory",
                    }

        created_at = sales_notes_store_mod.now_iso()
        entry = {
            "doc_key": doc_key,
            "institution": institution,
            "title": title,
            "kind": body.get("kind"),
            "analyst": body.get("analyst"),
            "doc_date": body.get("doc_date"),
            "tickers": body.get("tickers") or [],
            "source_url": body.get("source_url"),
            "date": received_date,
            "path": note_rel,
            **({"original_path": orig_rel} if orig_rel else {}),
            "origin": "manual",
            "created_at": created_at,
        }
        note_prefix = f"[{institution}] {title}\n\n"
        chunks_list = self._chunk_sales_note(body_text, note_prefix, max_chunk_chars=1500)
        chunk_embeddings = self._embed_texts(chunks_list)
        has_embeddings = any(blob is not None for blob in chunk_embeddings)

        is_image_ingest = binary_bytes is not None and orig_binary_suffix in _IMAGE_FILE_EXTENSIONS
        wiki_img_store: OpenWikiStore | None = None
        image_asset_abs: Path | None = None
        if is_image_ingest and binary_bytes is not None:
            wiki_img_store = OpenWikiStore(self.data_root)
            _, _, _, _, _, image_asset_abs = wiki_img_store._prepare_image_asset(
                image_bytes=binary_bytes,
                suffix=orig_binary_suffix,
            )
            wiki_img_store.ensure_images_schema()

        file_written = False
        orig_written = False
        asset_written = False
        conn = sqlite3.connect(self.mail_db, timeout=20, isolation_level=None)
        try:
            if is_image_ingest and wiki_img_store is not None:
                conn.execute("ATTACH DATABASE ? AS img_db", (str(wiki_img_store.images_db),))
            conn.execute("BEGIN IMMEDIATE")
            row_id: int | None = None
            for chunk_idx, chunk_body in enumerate(chunks_list):
                emb_blob = chunk_embeddings[chunk_idx] if chunk_idx < len(chunk_embeddings) else None
                cur = conn.execute(
                    "INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, embedding, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (received_date, doc_key, doc_key, institution, title, chunk_idx, chunk_body, emb_blob, created_at),
                )
                if row_id is None:
                    row_id = cur.lastrowid
            image_doc_id: str | None = None
            if is_image_ingest and binary_bytes is not None and wiki_img_store is not None:
                image_doc_id = wiki_img_store.register_image_asset(
                    image_bytes=binary_bytes,
                    suffix=orig_binary_suffix,
                    source="sales-notes",
                    doc_key=doc_key,
                    title=title,
                    doc_date=received_date,
                    metadata={
                        "parent_document_id": f"mail:{row_id}",
                        "original_source": institution,
                        "caption": body_text[:1000],
                    },
                    created_at=created_at,
                    conn=conn,
                    db_alias="img_db",
                    write_asset_file=False,
                )
            receipt = {
                "ok": True,
                "status": "ingested",
                "doc_key": doc_key,
                "document_id": f"mail:{row_id}",
                **({"image_id": image_doc_id} if image_doc_id else {}),
                "filepath": note_rel,
                **({"original_filepath": orig_rel} if orig_rel else {}),
                "sha256": sha_hex,
                "chunk_count": len(chunks_list),
                "embedding_indexed": has_embeddings,
                **({"request_id": request_id} if request_id else {}),
            }
            receipt_key = request_id or f"doc:{doc_key}"
            conn.execute(
                "INSERT INTO sales_ingest_receipts (request_id, doc_key, document_id, filepath, manifest_json, receipt_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_key,
                    doc_key,
                    receipt["document_id"],
                    note_rel,
                    json.dumps(entry, ensure_ascii=False),
                    json.dumps(receipt, ensure_ascii=False),
                    created_at,
                ),
            )
            if is_image_ingest and binary_bytes is not None and wiki_img_store is not None and image_asset_abs is not None:
                asset_written = wiki_img_store._persist_image_asset_file(image_asset_abs, binary_bytes)
            if binary_bytes is not None and orig_abs is not None:
                orig_abs.parent.mkdir(parents=True, exist_ok=True)
                b_fd = os.open(str(orig_abs), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    with os.fdopen(b_fd, "wb") as bfh:
                        bfh.write(binary_bytes)
                        bfh.flush()
                        os.fsync(bfh.fileno())
                    orig_written = True
                except BaseException:
                    if orig_abs.exists():
                        try:
                            orig_abs.unlink()
                        except OSError:
                            pass
                    raise
            fd = os.open(str(note_abs), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(body_text)
                    fh.flush()
                    os.fsync(fh.fileno())
                file_written = True
            except BaseException:
                if note_abs.exists():
                    try:
                        note_abs.unlink()
                    except OSError:
                        pass
                raise
            conn.execute("COMMIT")
        except BaseException:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if file_written and note_abs.exists():
                try:
                    note_abs.unlink()
                except OSError:
                    pass
            if orig_written and orig_abs is not None and orig_abs.exists():
                try:
                    orig_abs.unlink()
                except OSError:
                    pass
            if asset_written and image_asset_abs is not None and image_asset_abs.exists():
                try:
                    image_asset_abs.unlink()
                except OSError:
                    pass
            raise
        finally:
            conn.close()

        sales_notes_store_mod.append_manifest(self.data_root, entry)
        self._append_audit_entry(receipt)
        return 200, receipt


class SalesNotesConnector:
    route_id = "sales-notes"
    server_name = "dalton_sales_notes"
    required_capabilities = (CapabilityId.SALES_NOTES,)
    require_all_capabilities = True

    def __init__(self, store: MarketDigestPartitionStore) -> None:
        self.store = store

    def list_tools(self) -> list[dict[str, Any]]:
        props = {
            "query": {"type": "string"},
            "document": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
            "params": {"type": "object"},
        }
        return [
            {"name": "search", "description": "Search sales-notes partition.", "inputSchema": {"type": "object", "properties": props, "required": ["query"]}},
            {"name": "read", "description": "Read a mail:<id> or digest:<id> document.", "inputSchema": {"type": "object", "properties": props, "required": ["document"]}},
            {"name": "list", "description": "List sales-notes documents.", "inputSchema": {"type": "object", "properties": props}},
        ]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        active_caps: frozenset[CapabilityId],
        caller_key: str = "anonymous",
    ) -> dict[str, Any]:
        _ = caller_key
        if CapabilityId.SALES_NOTES not in active_caps:
            return {"ok": False, "status": "capability_disabled", "error_code": "capability_disabled", "http_status": 403, "error": "Capability 'sales_notes' is disabled."}
        op = "read" if tool_name in ("read", "get_document") else tool_name
        try:
            return await asyncio.to_thread(
                self.store.query,
                "sales-notes",
                op,
                query=arguments.get("query") or arguments.get("ticker"),
                document=arguments.get("document"),
                start=arguments.get("start"),
                end=arguments.get("end"),
                limit=int(arguments.get("limit") or 10),
                params=arguments.get("params") or {},
            )
        except Exception as exc:
            return {"ok": False, "status": "error", "error": str(exc)}


class TmtbConnector:
    route_id = "tmtb"
    server_name = "dalton_tmtb"
    required_capabilities = (CapabilityId.TMTB,)
    require_all_capabilities = True

    def __init__(self, store: MarketDigestPartitionStore) -> None:
        self.store = store

    def list_tools(self) -> list[dict[str, Any]]:
        props = {
            "query": {"type": "string"},
            "document": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
            "params": {"type": "object"},
        }
        return [
            {"name": "search", "description": "Search TMTB partition (message_id LIKE 'alphaengine:%').", "inputSchema": {"type": "object", "properties": props, "required": ["query"]}},
            {"name": "read", "description": "Read a tmtb:<id> document.", "inputSchema": {"type": "object", "properties": props, "required": ["document"]}},
            {"name": "list", "description": "List TMTB documents.", "inputSchema": {"type": "object", "properties": props}},
        ]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        active_caps: frozenset[CapabilityId],
        caller_key: str = "anonymous",
    ) -> dict[str, Any]:
        _ = caller_key
        if CapabilityId.TMTB not in active_caps:
            return {"ok": False, "status": "capability_disabled", "error_code": "capability_disabled", "http_status": 403, "error": "Capability 'tmtb' is disabled."}
        op = "read" if tool_name in ("read", "get_document") else tool_name
        try:
            return await asyncio.to_thread(
                self.store.query,
                "tmtb",
                op,
                query=arguments.get("query") or arguments.get("ticker"),
                document=arguments.get("document"),
                start=arguments.get("start"),
                end=arguments.get("end"),
                limit=int(arguments.get("limit") or 10),
                params=arguments.get("params") or {},
            )
        except Exception as exc:
            return {"ok": False, "status": "error", "error": str(exc)}


class FundaAiConnector:
    route_id = "funda-ai"
    server_name = "dalton_funda_ai"
    required_capabilities = (CapabilityId.FUNDA_AI,)
    require_all_capabilities = True

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self._store = funda_ai_mod.Store(self.data_root)
        self.db_path = self._store.db

    def list_tools(self) -> list[dict[str, Any]]:
        props = {
            "query": {"type": "string"},
            "document": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
            "params": {"type": "object"},
        }
        return [
            {"name": "search", "description": "Search FUNDA AI research archive.", "inputSchema": {"type": "object", "properties": props, "required": ["query"]}},
            {"name": "read", "description": "Read a funda:<id> document.", "inputSchema": {"type": "object", "properties": props, "required": ["document"]}},
            {"name": "list", "description": "List FUNDA AI articles.", "inputSchema": {"type": "object", "properties": props}},
        ]

    def query(
        self,
        op: str,
        *,
        query: str | None = None,
        document: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        p = dict(params or {})
        if not self.db_path.is_file():
            if op == "read":
                raise ValueError("FUNDA AI index not found")
            return {
                "ok": True,
                "status": "empty",
                "results": [],
                "result_count": 0,
                "cursor": None,
                "has_more": False,
                "total_upstream": 0,
                "returned": 0,
                "warnings": [],
                "retrieved_at": _now_iso(),
            }
        capped = max(1, min(100, int(limit)))
        store_params = dict(p)
        store_params.setdefault("search_mode", "keyword")
        res = self._store.query(
            op,
            query=query,
            document=document,
            start=start,
            end=end,
            limit=capped,
            params=store_params,
            embed=None,
        )
        out = dict(res)
        if op == "read":
            doc = out.get("document")
            if isinstance(doc, dict):
                out["_sources"] = [_make_source("dalton_funda_ai", "funda-ai", doc)]
            return out
        results = list(out.get("results") or [])
        sources_list = [_make_source("dalton_funda_ai", "funda-ai", it) for it in results[:50]]
        out.update(
            {
                "result_count": len(results),
                "cursor": None,
                "has_more": False,
                "total_upstream": len(results),
                "returned": len(results),
                "retrieved_at": _now_iso(),
            }
        )
        if sources_list:
            out["_sources"] = sources_list
        return out

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        active_caps: frozenset[CapabilityId],
        caller_key: str = "anonymous",
    ) -> dict[str, Any]:
        _ = caller_key
        if CapabilityId.FUNDA_AI not in active_caps:
            return {"ok": False, "status": "capability_disabled", "error_code": "capability_disabled", "http_status": 403, "error": "Capability 'funda_ai' is disabled."}
        op = "read" if tool_name in ("read", "get_document") else tool_name
        try:
            return await asyncio.to_thread(
                self.query,
                op,
                query=arguments.get("query") or arguments.get("ticker"),
                document=arguments.get("document"),
                start=arguments.get("start"),
                end=arguments.get("end"),
                limit=int(arguments.get("limit") or 10),
                params=arguments.get("params") or {},
            )
        except Exception as exc:
            return {"ok": False, "status": "error", "error": str(exc)}
