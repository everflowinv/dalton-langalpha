"""Open-source Company Wiki store, image index reader, idempotent ingest, and MCP connector.

Compatible with both fresh Standalone Core installations (`--profile wiki`,
mounted at `/var/lib/dalton/wiki`) and existing Everflow `wiki/vectors.db` +
`state/research-images/index.sqlite` layouts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.dalton.capabilities import CapabilityId

_INDEXED_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T[0-9:.+-]+Z?)?$")
_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WIKI_DOC_ID_RE = re.compile(r"^wiki:(\d+)$")
_IMAGE_DOC_ID_RE = re.compile(r"^image:(\d+)$")
_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FILENAME_SEGMENT_UNSAFE_RE = re.compile(r"[^\w.-]+", re.UNICODE)
_MAX_INGEST_FILE_BYTES = 20 * 1024 * 1024
_MAX_READ_CHARS = 30_000
_TEXT_FILE_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt", ".markdown", ".rst", ".csv", ".json"})
_PDF_FILE_EXTENSIONS: frozenset[str] = frozenset({".pdf"})
_DOCX_FILE_EXTENSIONS: frozenset[str] = frozenset({".docx"})
_IMAGE_FILE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_IMAGE_SOURCE_CAPABILITY: dict[str, CapabilityId] = {
    "company-wiki": CapabilityId.COMPANY_WIKI,
    "sales-notes": CapabilityId.SALES_NOTES,
    "tmtb": CapabilityId.TMTB,
    "funda-ai": CapabilityId.FUNDA_AI,
}

# Reused credential/secret detection patterns matching legacy host ingest scanner.
_INGEST_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key_pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----",
            re.ASCII,
        ),
    ),
    (
        "api_key_prefix",
        re.compile(
            r"(?<![A-Za-z0-9_\-])(?:sk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z\-_]{30,}|"
            r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|xai-[A-Za-z0-9_\-]{16,}|tvly-[A-Za-z0-9_\-]{16,}|"
            r"fc-[A-Za-z0-9_\-]{16,}|xox[baprs]-[A-Za-z0-9\-]{16,})",
            re.ASCII,
        ),
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|"
            r"bearer[_-]?token|private[_-]?key|client[_-]?secret|dalton_bridge_(?:admin_)?token|"
            r"dalton_research_(?:host_|admin_)?token|"
            r"gemini_api_key|openai_api_key|anthropic_api_key)\b\s*[:=]\s*['\"]?[A-Za-z0-9._\-+/=]{12,}['\"]?",
            re.ASCII,
        ),
    ),
    (
        "bearer_token",
        re.compile(
            r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._\-+/=]{16,}",
            re.ASCII,
        ),
    ),
)


def detect_ingest_secret(*texts: str) -> str | None:
    """Return matched secret pattern kind if any text contains suspected credentials/PEM, else None."""
    for text in texts:
        if not text:
            continue
        for kind, pat in _INGEST_SECRET_PATTERNS:
            if pat.search(text):
                return kind
    return None


def raw_bytes_scan_text(file_bytes: bytes, *, suffix: str = "") -> str:
    """Build a full raw-byte scan buffer matching legacy host binary file inspection.

    Covers raw latin-1 and UTF-8 byte views (catching secrets embedded in PNG/JPEG
    metadata chunks, PDF `/Info` dictionaries, comments, and non-body objects) plus
    decompressed PDF metadata entries when `pypdf` is available.
    """
    parts = [
        file_bytes.decode("latin1"),
        file_bytes.decode("utf-8", errors="ignore"),
    ]
    if suffix.lower() == ".pdf":
        try:
            import io
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(file_bytes))
            meta = getattr(reader, "metadata", None)
            if meta:
                for k, v in meta.items():
                    parts.append(f"{k}={v}")
        except Exception:
            pass
    return "\n".join(parts)


def chunk_wiki_text(text: str, *, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks matching `company_wiki_cli.chunk_text`."""
    if len(text) <= chunk_size:
        return [text] if text.strip() else [""]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if end < len(text):
            last_para = chunk.rfind("\n\n")
            if last_para > chunk_size * 0.5:
                end = start + last_para + 2
                chunk = text[start:end]
            else:
                for sep in ("。", ".", "\n"):
                    last_sep = chunk.rfind(sep)
                    if last_sep > chunk_size * 0.3:
                        end = start + last_sep + 1
                        chunk = text[start:end]
                        break
        stripped = chunk.strip()
        if stripped:
            chunks.append(stripped)
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
        if start >= len(text):
            break
    return chunks or [text]


def _extract_pdf_text_pypdf(file_bytes: bytes) -> str:
    """Extract text from standard/compressed PDFs using `pypdf` (included in Core image)."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return ""
    reader = PdfReader(io.BytesIO(file_bytes))
    pages: list[str] = []
    for idx, page in enumerate(reader.pages):
        txt = str(page.extract_text() or "").strip()
        if txt:
            pages.append(f"--- Page {idx + 1} ---\n{txt}")
    return "\n\n".join(pages).strip()


def _extract_pdf_text_fallback(file_bytes: bytes) -> str:
    """Minimal pure-Python fallback for uncompressed PDF literal text strings `(text) Tj/TJ`."""
    try:
        latin = file_bytes.decode("latin-1", errors="ignore")
    except Exception:
        return ""
    # Extract literal strings inside BT...ET text blocks
    chunks: list[str] = []
    for block in re.findall(r"BT(.*?)ET", latin, flags=re.DOTALL):
        for raw_s in re.findall(r"\(([^()\\]*(?:\\.[^()\\]*)*)\)", block):
            cleaned = (
                raw_s.replace(r"\(", "(")
                .replace(r"\)", ")")
                .replace(r"\\", "\\")
                .replace(r"\n", "\n")
                .replace(r"\r", " ")
                .replace(r"\t", " ")
                .strip()
            )
            if cleaned:
                chunks.append(cleaned)
    return "\n".join(chunks).strip()


def _extract_docx_text_fallback(file_bytes: bytes) -> str:
    """Pure-Python OOXML `.docx` text extractor using stdlib `zipfile` + `ElementTree`."""
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError("Invalid DOCX archive: missing word/document.xml")
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for para in root.iter(f"{ns}p"):
        texts = [node.text for node in para.iter(f"{ns}t") if node.text]
        joined = "".join(texts).strip()
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs).strip()


def _inspect_image_header(file_bytes: bytes, suffix: str) -> tuple[str, int | None, int | None]:
    """Validate basic image signature and parse dimensions when available."""
    if file_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(file_bytes) >= 24:
        w, h = struct.unpack(">II", file_bytes[16:24])
        return "image/png", int(w), int(h)
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", None, None
    if file_bytes[:6] in (b"GIF87a", b"GIF89a") and len(file_bytes) >= 10:
        w, h = struct.unpack("<HH", file_bytes[6:10])
        return "image/gif", int(w), int(h)
    if file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return "image/webp", None, None
    raise ValueError(f"File bytes do not match a valid {suffix} image header")


def extract_ingest_attachment(
    file_bytes: bytes,
    filename: str,
    *,
    cli_mod: Any = None,
) -> tuple[str, bool, str]:
    """Extract indexable Markdown text from an uploaded attachment without UTF-8 corrupting binaries.

    Returns ``(extracted_text, is_binary, original_suffix)``.
    Reuses ``company_wiki_cli`` extractors (``extract_from_pdf``, ``extract_from_docx``,
    ``extract_from_image``) when available, with open-source fallbacks in Standalone Core.
    """
    raw_name = Path(str(filename or "note.md")).name
    suffix = Path(raw_name).suffix.lower() or ".md"

    if suffix in _TEXT_FILE_EXTENSIONS:
        if b"\x00" in file_bytes:
            raise ValueError(f"Text attachment '{raw_name}' contains NUL binary bytes")
        return file_bytes.decode("utf-8", errors="replace"), False, suffix

    if suffix in _PDF_FILE_EXTENSIONS:
        if not file_bytes.lstrip().startswith(b"%PDF"):
            raise ValueError(f"Invalid PDF file header in '{raw_name}'")
        extracted = ""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(file_bytes)
            tmp_path = Path(tf.name)
        try:
            if cli_mod is not None and hasattr(cli_mod, "extract_from_pdf"):
                try:
                    extracted = str(cli_mod.extract_from_pdf(tmp_path, allow_image_only=True) or "").strip()
                except (SystemExit, Exception):
                    extracted = ""
            if not extracted:
                try:
                    import fitz  # type: ignore

                    doc = fitz.open(str(tmp_path))
                    try:
                        pages = []
                        for idx, page in enumerate(doc):
                            txt = page.get_text().strip()
                            if txt:
                                pages.append(f"--- Page {idx + 1} ---\n{txt}")
                        extracted = "\n\n".join(pages).strip()
                    finally:
                        doc.close()
                except Exception:
                    extracted = ""
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not extracted:
            try:
                extracted = _extract_pdf_text_pypdf(file_bytes)
            except Exception:
                extracted = ""
        if not extracted:
            extracted = _extract_pdf_text_fallback(file_bytes)
        if not extracted:
            raise ValueError(
                f"No extractable text layer found in PDF '{raw_name}'. "
                "Convert scanned pages to images or provide extracted Markdown text."
            )
        return extracted, True, suffix

    if suffix in _DOCX_FILE_EXTENSIONS:
        extracted = ""
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
            tf.write(file_bytes)
            tmp_path = Path(tf.name)
        try:
            if cli_mod is not None and hasattr(cli_mod, "extract_from_docx"):
                try:
                    extracted = str(cli_mod.extract_from_docx(tmp_path, allow_image_only=True) or "").strip()
                except (SystemExit, Exception):
                    extracted = ""
            if not extracted:
                try:
                    from docx import Document  # type: ignore

                    doc = Document(str(tmp_path))
                    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                    for tbl in doc.tables:
                        rows = [" | ".join(c.text.strip() for c in r.cells) for r in tbl.rows]
                        if rows:
                            parts.append("\n".join(rows))
                    extracted = "\n\n".join(parts).strip()
                except Exception:
                    extracted = ""
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not extracted:
            extracted = _extract_docx_text_fallback(file_bytes)
        if not extracted:
            extracted = f"[DOCX attachment: {raw_name} ({len(file_bytes)} bytes; empty document)]"
        return extracted, True, suffix

    if suffix in _IMAGE_FILE_EXTENSIONS:
        mime, width, height = _inspect_image_header(file_bytes, suffix)
        extracted = ""
        if cli_mod is not None and hasattr(cli_mod, "extract_from_image") and os.environ.get("GEMINI_API_KEY"):
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(file_bytes)
                tmp_path = Path(tf.name)
            try:
                cfg = cli_mod.load_config() if hasattr(cli_mod, "load_config") else {}
                extracted = str(cli_mod.extract_from_image(tmp_path, cfg) or "").strip()
            except (SystemExit, Exception):
                extracted = ""
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        dims = f", {width}x{height}" if width and height else ""
        header = f"Image attachment: `{raw_name}` ({mime}, {len(file_bytes)} bytes{dims})"
        body = f"{header}\n\n{extracted}" if extracted else header
        return body, True, suffix

    raise ValueError(
        f"Unsupported attachment format '{suffix}'. Supported: "
        f"{', '.join(sorted(_TEXT_FILE_EXTENSIONS | _PDF_FILE_EXTENSIONS | _DOCX_FILE_EXTENSIONS | _IMAGE_FILE_EXTENSIONS))}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_slug(raw: str) -> str:
    cleaned = _SLUG_SAFE_RE.sub("-", str(raw or "").strip()).strip("-._")
    if not cleaned:
        raise ValueError("target must contain at least one alphanumeric character")
    return cleaned[:100]


def _safe_filename_segment(raw: str, *, fallback: str = "note") -> str:
    """Sanitize a filename segment such as `content_type` (supports Unicode labels like '研究笔记')."""
    cleaned = _FILENAME_SEGMENT_UNSAFE_RE.sub("-", str(raw or "").strip()).strip("-._")
    return cleaned[:60] if cleaned else fallback


def _page_text(text: str, *, offset: int = 0, max_chars: int = 10_000) -> dict[str, Any]:
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(max_chars) is not int or not 1 <= max_chars <= _MAX_READ_CHARS:
        raise ValueError(f"max_chars must be an integer between 1 and {_MAX_READ_CHARS}")
    if offset > len(text):
        raise ValueError("offset exceeds stored document length")
    end = min(len(text), offset + max_chars)
    return {
        "content": text[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "total_chars": len(text),
        "truncated": end < len(text),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _make_wiki_source(item: dict[str, Any]) -> dict[str, Any]:
    doc_id = str(item.get("document_id") or "")
    title = str(item.get("title") or doc_id)
    as_of = str(item.get("date") or "")[:10]
    url = item.get("url")
    return {
        "document_id": doc_id[:200],
        "identifier": doc_id[:200],
        "route": "company-wiki",
        "source_type": "research_doc",
        "identity": "internal_note",
        "detail": "internal_note",
        "publisher": str(item.get("original_source") or "company-wiki")[:120],
        "provider": "dalton_company_wiki:company-wiki",
        "title": title[:300],
        "as_of": as_of,
        "url": str(url) if isinstance(url, str) and url else None,
        "private": True,
    }


def _parse_strict_ymd(raw: str) -> str:
    cleaned = str(raw or "").strip()
    if not _YMD_RE.fullmatch(cleaned):
        raise ValueError("date 必须为有效 YYYY-MM-DD 日期")
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError("date 必须为有效 YYYY-MM-DD 日期") from exc
    if parsed.isoformat() != cleaned:
        raise ValueError("date 必须为有效 YYYY-MM-DD 日期")
    return cleaned


class OpenWikiStore:
    """Self-contained SQLite + Markdown store for ``company-wiki`` and research images."""

    def __init__(
        self,
        data_root: Path | str,
        *,
        audit_file: Path | str | None = None,
        code_root: Path | str | None = None,
    ) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.code_root = Path(code_root).expanduser().resolve() if code_root else None
        self.wiki_dir = self.data_root / "wiki"
        self.wiki_db = self.wiki_dir / "vectors.db"
        self.images_dir = self.data_root / "state" / "research-images"
        self.images_db = self.images_dir / "index.sqlite"
        self.audit_file = (
            Path(audit_file).expanduser().resolve()
            if audit_file is not None
            else (self.data_root / "state" / "wiki_ingest_audit.jsonl")
        )
        self._write_lock = asyncio.Lock()
        self._cli_module: Any = None
        self._cli_checked: bool = False

    def _load_company_wiki_cli(self) -> Any | None:
        if self._cli_checked:
            return self._cli_module
        self._cli_checked = True
        candidates: list[Path] = []
        if self.code_root is not None:
            candidates.append(self.code_root / "skills" / "company-wiki" / "scripts" / "company_wiki_cli.py")
        candidates.append(
            Path(__file__).resolve().parents[3]
            / "scripts"
            / "dalton"
            / "research_runtime"
            / "skills"
            / "company-wiki"
            / "scripts"
            / "company_wiki_cli.py"
        )
        for path in candidates:
            if path.is_file():
                try:
                    spec = importlib.util.spec_from_file_location("_dalton_company_wiki_cli", path)
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        if hasattr(mod, "insert_document_and_chunks"):
                            self._cli_module = mod
                            break
                except Exception:
                    continue
        return self._cli_module

    def ensure_schema(self) -> None:
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.wiki_db, timeout=20) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_type TEXT NOT NULL,
                    category_name TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'note',
                    date TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    related TEXT DEFAULT '[]',
                    content_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    embedding BLOB,
                    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS ingest_receipts (
                    request_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
                CREATE INDEX IF NOT EXISTS idx_docs_cat ON documents(category_type, category_name);
                """
            )
            try:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                        content,
                        content='chunks',
                        content_rowid='id',
                        tokenize='trigram'
                    );
                    CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
                        INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE OF content ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                        INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    """
                )
            except sqlite3.OperationalError:
                pass
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(documents)").fetchall()
            }
            if "content_sha256" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN content_sha256 TEXT")
            if "tags" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN tags TEXT DEFAULT '[]'")
            if "related" not in cols:
                conn.execute("ALTER TABLE documents ADD COLUMN related TEXT DEFAULT '[]'")
            conn.commit()

    @contextlib.contextmanager
    def _connect_readonly(self, db_path: Path):
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _safe_wiki_file(self, rel_path: str) -> Path:
        path = (self.data_root / rel_path).resolve()
        wiki_root = self.wiki_dir.resolve()
        if not path.is_relative_to(wiki_root) or path.suffix != ".md":
            raise ValueError("wiki file path is outside allowed directory")
        return path

    def _row_meta(self, row: sqlite3.Row) -> dict[str, Any]:
        path = self._safe_wiki_file(str(row["filepath"]))
        extras: dict[str, str] = {}
        if path.is_file():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(8192)
            if head.startswith("---\n"):
                parts = head.split("---", 2)
                if len(parts) >= 2:
                    for line in parts[1].splitlines():
                        m = re.match(r"^(source|source_url):\s*(.*?)\s*$", line)
                        if m:
                            extras[m.group(1)] = m.group(2).strip("\"'")
        doc_id = f"wiki:{row['id']}"
        try:
            tags = json.loads(row["tags"] or "[]")
            if not isinstance(tags, list):
                tags = []
        except Exception:
            tags = []
        return {
            "document_id": doc_id,
            "canonical_id": extras.get("source_url") or doc_id,
            "source": "company-wiki",
            "original_source": extras.get("source") or "company-wiki",
            "original_document_id": str(row["id"]),
            "title": str(row["filename"]),
            "date": str(row["date"] or ""),
            "date_basis": "wiki 文档日期，非严格历史可见时间",
            "indexed_at": str(row["created_at"] or ""),
            "url": extras.get("source_url") or None,
            "location": str(row["filepath"]),
            "company": row["category_name"] if row["category_type"] == "company" else None,
            "sector": row["category_name"] if row["category_type"] == "sector" else None,
            "tags": tags,
            "evidence_role": "source_document",
            "original_completeness": "as_stored",
            "private": True,
        }

    def _validate_params(
        self,
        operation: str,
        query: str | None,
        document: str | None,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        p = dict(params or {})
        allowed = {
            "company",
            "sector",
            "tag",
            "search_mode",
            "no_decay",
            "offset",
            "max_chars",
            "media",
            "indexed_since",
        }
        unknown = set(p) - allowed
        if unknown:
            raise ValueError(f"Unknown company-wiki params: {sorted(unknown)}")
        if p.get("company") and p.get("sector"):
            raise ValueError("company and sector cannot both be specified")
        for k in ("company", "sector", "tag"):
            if k in p and (not isinstance(p[k], str) or not p[k].strip()):
                raise ValueError(f"{k} must be a non-empty string")
        media = p.get("media", "all")
        if media not in ("all", "text", "images"):
            raise ValueError("media must be 'all', 'text', or 'images'")
        search_mode = p.get("search_mode", "hybrid")
        if search_mode not in ("hybrid", "keyword"):
            raise ValueError("search_mode must be 'hybrid' or 'keyword'")
        if "indexed_since" in p:
            raw_is = p["indexed_since"]
            if not isinstance(raw_is, str) or not _INDEXED_SINCE_RE.fullmatch(raw_is.strip()):
                raise ValueError("indexed_since must be YYYY-MM-DD or UTC ISO timestamp")
        if "no_decay" in p and type(p["no_decay"]) is not bool:
            raise ValueError("no_decay must be boolean")
        if operation not in ("search", "read", "list", "topic"):
            raise ValueError("operation must be search, read, list, or topic")
        if operation == "search" and not (query and str(query).strip()):
            raise ValueError("search requires query")
        if operation == "topic" and not (p.get("tag") or (query and str(query).strip())):
            raise ValueError("topic requires tag or query")
        if operation == "read":
            if not document or not str(document).strip():
                raise ValueError("read requires document")
            if set(p) - {"offset", "max_chars"} or query:
                raise ValueError("read only accepts document, offset, and max_chars")
        elif document or any(k in p for k in ("offset", "max_chars")):
            raise ValueError("document/offset/max_chars are only valid for read")
        return p

    def _build_where(
        self,
        *,
        company: str | None,
        sector: str | None,
        tag: str | None,
        start: str | None,
        end: str | None,
        indexed_since: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        vals: list[Any] = []
        if company:
            clauses.append("d.category_type = 'company' AND lower(d.category_name) = lower(?)")
            vals.append(company.strip())
        elif sector:
            clauses.append("d.category_type = 'sector' AND lower(d.category_name) = lower(?)")
            vals.append(sector.strip())
        if start:
            clauses.append("d.date >= ?")
            vals.append(start)
        if end:
            clauses.append("d.date <= ?")
            vals.append(end)
        if tag:
            clauses.append("lower(COALESCE(d.tags, '')) LIKE ?")
            vals.append(f"%{tag.strip().lower()}%")
        if indexed_since:
            clauses.append("d.created_at >= ?")
            vals.append(indexed_since.strip())
        return (" AND ".join(clauses) if clauses else "1=1"), vals

    def search_images(
        self,
        *,
        source: str,
        query: str | None,
        start: str | None,
        end: str | None,
        limit: int,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query `state/research-images/index.sqlite` for image assets belonging to `source`."""
        if not self.images_db.is_file():
            return {"ok": True, "status": "empty", "results": [], "warnings": []}
        clauses = ["r.source = ?"]
        vals: list[Any] = [source]
        if start:
            clauses.append("r.date >= ?")
            vals.append(start)
        if end:
            clauses.append("r.date <= ?")
            vals.append(end)
        if params.get("indexed_since"):
            clauses.append("r.created_at >= ?")
            vals.append(str(params["indexed_since"]).strip())
        where = " AND ".join(clauses)
        terms = [t.lower() for t in re.findall(r"[\w.-]+", str(query or "")) if t]
        company_f = str(params.get("company") or "").strip().lower()
        sector_f = str(params.get("sector") or "").strip().lower()
        tag_f = str(params.get("tag") or "").strip().lower()
        results: list[dict[str, Any]] = []
        with self._connect_readonly(self.images_db) as conn:
            rows = conn.execute(
                f"SELECT r.*, a.path, a.preview_path, a.width, a.height, a.mime "
                f"FROM refs r JOIN assets a ON a.sha = r.asset_sha "
                f"WHERE {where} ORDER BY r.date DESC, r.id DESC",
                vals,
            ).fetchall()
            for row in rows:
                try:
                    meta = json.loads(row["metadata"] or "{}")
                    if not isinstance(meta, dict):
                        meta = {}
                except Exception:
                    meta = {}
                if company_f and str(meta.get("company") or "").strip().lower() != company_f:
                    continue
                if sector_f and str(meta.get("sector") or "").strip().lower() != sector_f:
                    continue
                if tag_f and tag_f not in [str(t).lower() for t in (meta.get("tags") or [])]:
                    continue
                hay = f"{row['title']} {meta.get('caption', '')} {meta.get('company', '')} {meta.get('sector', '')}".lower()
                if terms and not any(t in hay for t in terms):
                    continue
                results.append(
                    {
                        "document_id": f"image:{row['id']}",
                        "canonical_id": f"image-sha256:{row['asset_sha']}",
                        "source": row["source"],
                        "media_type": "image",
                        "title": row["title"],
                        "date": row["date"],
                        "date_basis": meta.get("date_basis", "继承原文档日期，非严格历史可见时间"),
                        "parent_document_id": meta.get("parent_document_id"),
                        "original_document_id": row["doc_key"],
                        "original_source": meta.get("original_source", row["source"]),
                        "location": row["path"],
                        "asset_sha256": row["asset_sha"],
                        "width": row["width"],
                        "height": row["height"],
                        "company": meta.get("company"),
                        "sector": meta.get("sector"),
                        "tags": meta.get("tags", []),
                        "indexed_at": row["created_at"],
                        "snippet": str(meta.get("caption") or "")[:800],
                        "evidence_role": "source_image",
                        "private": True,
                    }
                )
                if len(results) >= limit:
                    break
        return {
            "ok": True,
            "status": "ok" if results else "empty",
            "results": results,
            "warnings": [],
        }

    def read_image(
        self,
        document: str,
        *,
        active_caps: frozenset[CapabilityId] | None = None,
        allowed_sources: set[str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Read `image:<id>` after verifying the owning source's capability is enabled."""
        m = _IMAGE_DOC_ID_RE.fullmatch(str(document or "").strip())
        if not m:
            return 400, {
                "ok": False,
                "status": "bad_request",
                "error": "image read requires document='image:<id>'",
            }
        if not self.images_db.is_file():
            return 404, {
                "ok": False,
                "status": "not_found",
                "error": f"Image index not found for {document}",
            }
        with self._connect_readonly(self.images_db) as conn:
            row = conn.execute(
                "SELECT r.*, a.path, a.preview_path, a.width, a.height, a.mime "
                "FROM refs r JOIN assets a ON a.sha = r.asset_sha WHERE r.id = ?",
                (int(m.group(1)),),
            ).fetchone()
        if row is None:
            return 404, {"ok": False, "status": "not_found", "error": f"Image {document} not found"}

        owner_source = str(row["source"] or "").strip()
        if allowed_sources is not None and owner_source not in allowed_sources:
            return 403, {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "error": f"Image {document} belongs to source '{owner_source}', which is not enabled or not allowed.",
            }
        if active_caps is not None:
            req_cap = _IMAGE_SOURCE_CAPABILITY.get(owner_source)
            if req_cap is not None and req_cap not in active_caps:
                return 403, {
                    "ok": False,
                    "status": "capability_disabled",
                    "error_code": "capability_disabled",
                    "error": f"Capability '{req_cap.value}' is disabled for image {document}.",
                }

        assets_root = (self.images_dir / "assets").resolve()
        orig_path = (self.data_root / str(row["path"])).resolve()
        prev_path = (self.data_root / str(row["preview_path"])).resolve()
        if not orig_path.is_relative_to(assets_root) or not orig_path.is_file():
            return 404, {"ok": False, "status": "error", "error": "Image asset file missing or invalid path"}
        if not prev_path.is_relative_to(assets_root) or not prev_path.is_file():
            return 404, {"ok": False, "status": "error", "error": "Image preview file missing or invalid path"}
        actual_sha = hashlib.sha256(orig_path.read_bytes()).hexdigest()
        if actual_sha != str(row["asset_sha"]):
            return 500, {"ok": False, "status": "error", "error": "Image asset SHA-256 mismatch"}

        try:
            meta = json.loads(row["metadata"] or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        doc = {
            "document_id": f"image:{row['id']}",
            "canonical_id": f"image-sha256:{row['asset_sha']}",
            "source": owner_source,
            "media_type": "image",
            "title": row["title"],
            "date": row["date"],
            "parent_document_id": meta.get("parent_document_id"),
            "original_document_id": row["doc_key"],
            "original_source": meta.get("original_source", owner_source),
            "location": row["path"],
            "asset_sha256": row["asset_sha"],
            "width": row["width"],
            "height": row["height"],
            "company": meta.get("company"),
            "sector": meta.get("sector"),
            "tags": meta.get("tags", []),
            "indexed_at": row["created_at"],
            "local_path": str(orig_path),
            "preview_path": str(prev_path),
            "preview_mime": "image/jpeg",
            "original_mime": row["mime"],
            "evidence_role": "source_image",
            "private": True,
            "note": "请查看返回的原图／预览再引用具体数字；embedding 不提供 OCR 或数值保证。",
        }
        return 200, {
            "_sources": [_make_wiki_source(doc)],
            "ok": True,
            "status": "ok",
            "document": doc,
            "retrieved_at": _now_iso(),
            "local_only": True,
        }

    def query(
        self,
        operation: str,
        *,
        query: str | None = None,
        document: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run `search`, `read`, `list`, or `topic` over `company-wiki`."""
        p = self._validate_params(operation, query, document, params)
        capped_limit = max(1, min(100, int(limit)))
        media = str(p.get("media", "all"))

        if operation == "read":
            if start or end:
                raise ValueError("read does not accept start/end date filters")
            doc_str = str(document or "").strip()
            if doc_str.startswith("image:"):
                if p.get("offset") or p.get("max_chars"):
                    raise ValueError("image read does not accept offset/max_chars")
                _code, img_res = self.read_image(doc_str, allowed_sources={"company-wiki"})
                return img_res
            m = _WIKI_DOC_ID_RE.fullmatch(doc_str)
            if not m:
                raise ValueError("company-wiki read requires document='wiki:<id>'")
            if not self.wiki_db.is_file():
                raise ValueError("Document not found")
            with self._connect_readonly(self.wiki_db) as conn:
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(m.group(1)),)).fetchone()
                if row is None:
                    raise ValueError("Document not found")
                meta = self._row_meta(row)
                body_text = self._safe_wiki_file(str(row["filepath"])).read_text(encoding="utf-8")
                meta.update(
                    _page_text(
                        body_text,
                        offset=int(p.get("offset", 0)),
                        max_chars=int(p.get("max_chars", 10_000)),
                    )
                )
            return {
                "_sources": [_make_wiki_source(meta)],
                "ok": True,
                "status": "ok",
                "document": meta,
                "warnings": [],
                "retrieved_at": _now_iso(),
                "local_only": True,
            }

        # Handle images-only query cleanly
        if media == "images":
            ip = dict(p)
            if operation == "topic":
                ip["tag"] = ip.get("tag") or query
            visual = self.search_images(
                source="company-wiki",
                query=query,
                start=start,
                end=end,
                limit=capped_limit,
                params=ip,
            )
            sources = [_make_wiki_source(it) for it in visual["results"][:50]]
            out: dict[str, Any] = {
                "ok": visual["ok"],
                "status": visual["status"],
                "results": visual["results"],
                "result_count": len(visual["results"]),
                "warnings": visual.get("warnings", []),
                "retrieved_at": _now_iso(),
                "local_only": True,
            }
            if sources:
                out["_sources"] = sources
            return out

        if not self.wiki_db.is_file():
            return {
                "ok": True,
                "status": "empty",
                "results": [],
                "result_count": 0,
                "warnings": [],
                "coverage": {"scope": "local_index", "documents": 0, "latest_document_date": None},
                "retrieved_at": _now_iso(),
                "local_only": True,
            }

        where, vals = self._build_where(
            company=p.get("company"),
            sector=p.get("sector"),
            tag=p.get("tag") or (query if operation == "topic" else None),
            start=start,
            end=end,
            indexed_since=p.get("indexed_since"),
        )
        warnings: list[str] = []
        results: list[dict[str, Any]] = []
        with self._connect_readonly(self.wiki_db) as conn:
            if operation == "search":
                q_str = str(query or "").strip()
                terms = [t.lower() for t in re.findall(r"[\w.-]+", q_str) if t]
                doc_rows = conn.execute(
                    f"SELECT d.* FROM documents d WHERE {where} ORDER BY d.date DESC, d.id DESC",
                    vals,
                ).fetchall()
                scored: list[tuple[int, dict[str, Any]]] = []
                for row in doc_rows:
                    try:
                        fpath = self._safe_wiki_file(str(row["filepath"]))
                        body = fpath.read_text(encoding="utf-8", errors="replace") if fpath.is_file() else ""
                    except ValueError:
                        warnings.append(f"Invalid wiki path for doc {row['id']}")
                        continue
                    hay = f"{row['filename']} {row['category_name']} {row['tags']} {body}".lower()
                    hits = sum(1 for t in terms if t in hay)
                    if hits <= 0:
                        continue
                    item = self._row_meta(row)
                    item.update(
                        snippet=body[:800],
                        score=round(hits / max(1, len(terms)), 4),
                        matched=["keyword"],
                    )
                    scored.append((hits, item))
                scored.sort(key=lambda pair: (pair[0], pair[1].get("date") or ""), reverse=True)
                results = [it for _, it in scored[:capped_limit]]
            else:
                rows = conn.execute(
                    f"SELECT d.* FROM documents d WHERE {where} ORDER BY d.date DESC, d.id DESC LIMIT ?",
                    vals + [capped_limit + 1],
                ).fetchall()
                if len(rows) > capped_limit:
                    warnings.append("列表有更多文档，请缩小筛选或提高 limit")
                for row in rows[:capped_limit]:
                    try:
                        item = self._row_meta(row)
                        item.update(snippet="", score=None, matched=[])
                        results.append(item)
                    except (OSError, ValueError):
                        warnings.append(f"索引命中文档缺失或路径无效：{row['id']}")
            count_row = conn.execute("SELECT COUNT(*), MAX(date) FROM documents").fetchone()
            total_docs = int(count_row[0] or 0) if count_row else 0
            latest_date = count_row[1] if count_row else None

        if media == "all":
            ip = dict(p)
            if operation == "topic":
                ip["tag"] = ip.get("tag") or query
            visual = self.search_images(
                source="company-wiki",
                query=query,
                start=start,
                end=end,
                limit=capped_limit,
                params=ip,
            )
            if visual.get("results"):
                merged: list[dict[str, Any]] = []
                for rank, item in enumerate(results, 1):
                    merged.append({**item, "media_rrf_score": 1.0 / (60 + rank), "retrieval_arm": "text"})
                for rank, item in enumerate(visual["results"], 1):
                    merged.append({**item, "media_rrf_score": 1.0 / (60 + rank), "retrieval_arm": "image"})
                merged.sort(key=lambda x: x["media_rrf_score"], reverse=True)
                results = merged[:capped_limit]

        status = "partial" if warnings else ("ok" if results else "empty")
        sources = [_make_wiki_source(it) for it in results[:50]]
        out = {
            "ok": True,
            "status": status,
            "results": results,
            "result_count": len(results),
            "warnings": warnings,
            "coverage": {
                "scope": "local_index",
                "documents": total_docs,
                "latest_document_date": latest_date,
            },
            "retrieved_at": _now_iso(),
            "local_only": True,
        }
        if sources:
            out["_sources"] = sources
        return out

    def _find_db_receipt_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        if not request_id or not self.wiki_db.is_file():
            return None
        try:
            with self._connect_readonly(self.wiki_db) as conn:
                row = conn.execute(
                    "SELECT receipt_json FROM ingest_receipts WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            if row and row["receipt_json"]:
                parsed = json.loads(row["receipt_json"])
                if isinstance(parsed, dict):
                    return parsed
        except (sqlite3.Error, ValueError):
            return None
        return None

    def _find_db_receipt_by_sha256(self, payload_sha: str) -> dict[str, Any] | None:
        if not payload_sha or not self.wiki_db.is_file():
            return None
        try:
            with self._connect_readonly(self.wiki_db) as conn:
                row = conn.execute(
                    "SELECT receipt_json FROM ingest_receipts WHERE sha256 = ? ORDER BY created_at DESC LIMIT 1",
                    (payload_sha,),
                ).fetchone()
            if row and row["receipt_json"]:
                parsed = json.loads(row["receipt_json"])
                if isinstance(parsed, dict):
                    return parsed
        except (sqlite3.Error, ValueError):
            return None
        return None

    def _find_audit_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        if not request_id or not self.audit_file.is_file():
            return None
        try:
            for line in self.audit_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("request_id") == request_id:
                    return entry
        except OSError:
            return None
        return None

    def _find_audit_by_sha256(self, payload_sha: str) -> dict[str, Any] | None:
        if not payload_sha or not self.audit_file.is_file():
            return None
        try:
            for line in self.audit_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("sha256") == payload_sha:
                    return entry
        except OSError:
            return None
        return None

    def _ensure_images_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                sha TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                preview_path TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                mime TEXT NOT NULL,
                size_bytes INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_sha TEXT NOT NULL,
                source TEXT NOT NULL,
                doc_key TEXT NOT NULL,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_refs_source_date ON refs(source, date);
            CREATE INDEX IF NOT EXISTS idx_refs_asset_sha ON refs(asset_sha);
            """
        )

    def register_image_asset(
        self,
        *,
        image_bytes: bytes,
        suffix: str,
        source: str,
        doc_key: str,
        title: str,
        doc_date: str,
        metadata: dict[str, Any],
        created_at: str,
    ) -> str:
        """Persist an uploaded image under `state/research-images/assets/` and index it in `index.sqlite`."""
        mime, width, height = _inspect_image_header(image_bytes, suffix)
        sha = hashlib.sha256(image_bytes).hexdigest()
        asset_rel = f"state/research-images/assets/{sha[:2]}/{sha}{suffix}"
        asset_abs = (self.data_root / asset_rel).resolve()
        assets_root = (self.images_dir / "assets").resolve()
        if not asset_abs.is_relative_to(assets_root):
            raise ValueError("Resolved image asset path escapes state/research-images/assets")
        asset_abs.parent.mkdir(parents=True, exist_ok=True)
        if not asset_abs.exists():
            tmp_asset = asset_abs.with_name(f".{asset_abs.name}.{os.getpid()}.tmp")
            with tmp_asset.open("wb") as fh:
                fh.write(image_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_asset, asset_abs)

        self.images_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.images_db, timeout=20) as img_conn:
            self._ensure_images_schema(img_conn)
            img_conn.execute(
                "INSERT OR IGNORE INTO assets (sha, path, preview_path, width, height, mime, size_bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sha, asset_rel, asset_rel, width, height, mime, len(image_bytes), created_at),
            )
            existing_ref = img_conn.execute(
                "SELECT id FROM refs WHERE asset_sha = ? AND source = ? AND doc_key = ?",
                (sha, source, doc_key),
            ).fetchone()
            if existing_ref is not None:
                ref_id = int(existing_ref[0])
            else:
                cur = img_conn.execute(
                    "INSERT INTO refs (asset_sha, source, doc_key, title, date, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sha,
                        source,
                        doc_key,
                        title,
                        doc_date,
                        json.dumps(metadata, ensure_ascii=False),
                        created_at,
                    ),
                )
                ref_id = int(cur.lastrowid)
            img_conn.commit()
        return f"image:{ref_id}"

    def _append_audit(self, entry: dict[str, Any]) -> None:
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def ingest(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle `POST /ingest/wiki` with validation, preview (`dry_run`), and idempotent writes."""
        if bool(body.get("dry_run", False)):
            return await asyncio.to_thread(self._ingest_sync, body)
        async with self._write_lock:
            return await asyncio.to_thread(self._ingest_sync, body)

    def _ingest_sync(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        target_type = str(body.get("target_type") or "").strip().lower()
        if target_type not in ("company", "sector"):
            return 400, {
                "ok": False,
                "status": "invalid_target_type",
                "error_code": "invalid_target_type",
                "error": "target_type 必须为 'company' 或 'sector'",
            }
        raw_target = body.get("target")
        if not isinstance(raw_target, str) or not raw_target.strip():
            return 400, {
                "ok": False,
                "status": "invalid_slug",
                "error_code": "empty_target",
                "error": "target 必须为非空字符串",
            }
        try:
            slug = _normalize_slug(raw_target)
        except ValueError as exc:
            return 400, {
                "ok": False,
                "status": "invalid_slug",
                "error_code": "invalid_slug",
                "error": str(exc),
            }

        content_type = str(body.get("content_type") or "").strip()
        if not content_type:
            return 400, {
                "ok": False,
                "status": "invalid_content_type",
                "error_code": "empty_content_type",
                "error": "content_type 必填",
            }
        try:
            doc_date = _parse_strict_ymd(str(body.get("date") or ""))
        except ValueError as exc:
            return 400, {
                "ok": False,
                "status": "invalid_date",
                "error_code": "invalid_date",
                "error": str(exc),
            }

        raw_content = body.get("content")
        raw_file = body.get("file")
        has_content = isinstance(raw_content, str) and bool(raw_content.strip())
        has_file = isinstance(raw_file, dict) and bool(raw_file.get("content_b64"))
        if has_content == has_file:
            return 400, {
                "ok": False,
                "status": "bad_request",
                "error_code": "content_or_file_required",
                "error": "Provide exactly one of content or file.",
            }

        cli_mod = self._load_company_wiki_cli()
        binary_bytes: bytes | None = None
        orig_binary_name: str | None = None
        orig_binary_suffix: str = ""
        raw_attachment_scan: str = ""
        if has_file and isinstance(raw_file, dict):
            try:
                file_bytes = base64.b64decode(str(raw_file["content_b64"]))
            except Exception:
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_base64",
                    "error": "file.content_b64 is not valid base64",
                }
            if len(file_bytes) == 0 or len(file_bytes) > _MAX_INGEST_FILE_BYTES:
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_file_size",
                    "error": "File is empty or exceeds 20 MB limit",
                }
            raw_fname = Path(str(raw_file.get("filename") or "note.md")).name
            raw_attachment_scan = raw_bytes_scan_text(file_bytes, suffix=Path(raw_fname).suffix)
            try:
                body_text, is_binary, orig_suffix = extract_ingest_attachment(
                    file_bytes, raw_fname, cli_mod=cli_mod
                )
            except ValueError as exc:
                err_code = (
                    "empty_pdf_text"
                    if "extractable text layer" in str(exc).lower()
                    else "unsupported_file_format"
                )
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": err_code,
                    "error": str(exc),
                }
            payload_sha = hashlib.sha256(file_bytes).hexdigest()
            safe_stem = _SLUG_SAFE_RE.sub("-", Path(raw_fname).stem).strip("-._") or "attachment"
            orig_name = f"{safe_stem}.md"
            if is_binary:
                binary_bytes = file_bytes
                orig_binary_name = f"{safe_stem}{orig_suffix}"
                orig_binary_suffix = orig_suffix
        else:
            body_text = str(raw_content or "")
            norm_body = re.sub(r"\s+", " ", body_text.replace("\r\n", "\n").replace("\r", "\n").strip())
            payload_sha = hashlib.sha256(norm_body.encode("utf-8")).hexdigest()
            orig_name = f"{doc_date}-{_safe_filename_segment(content_type)}-{payload_sha[:8]}.md"

        if not orig_name.endswith(".md"):
            orig_name = f"{orig_name}.md"

        request_id = str(body.get("request_id") or "").strip() or None
        dry_run = bool(body.get("dry_run", False))
        source = str(body.get("source") or "company-wiki").strip()
        source_url = str(body.get("source_url") or "").strip()

        # Full-byte + extracted-text + metadata credential check (reused from legacy wiki ingest)
        secret_kind = detect_ingest_secret(raw_attachment_scan, body_text, source, source_url)
        if secret_kind is not None:
            return 400, {
                "ok": False,
                "status": "secret_detected",
                "error_code": "secret_detected",
                "secret_kind": secret_kind,
                "error": f"Detected suspected credential/secret ({secret_kind}); remove secrets before ingesting into wiki.",
            }

        # 1. Atomic DB-backed idempotent replay & crash-recovery backfill (with OR without request_id)
        self.ensure_schema()
        if not dry_run:
            if request_id:
                db_prior = self._find_db_receipt_by_request_id(request_id)
                audit_prior = self._find_audit_by_request_id(request_id)
                prior = db_prior or audit_prior
                if prior is not None and prior.get("status") in ("ingested", "done"):
                    if db_prior is not None and audit_prior is None:
                        self._append_audit(db_prior)
                    return 200, {
                        **prior,
                        "ok": True,
                        "status": "ingested",
                        "idempotent_replay": True,
                        "request_id": request_id,
                    }
            else:
                db_sha_prior = self._find_db_receipt_by_sha256(payload_sha)
                if (
                    db_sha_prior is not None
                    and db_sha_prior.get("status") in ("ingested", "done")
                    and self._find_audit_by_sha256(payload_sha) is None
                ):
                    self._append_audit(db_sha_prior)
                    return 200, {
                        **db_sha_prior,
                        "ok": True,
                        "status": "ingested",
                        "idempotent_replay": True,
                    }

        # 2. Exact content fingerprint duplicate check
        with self._connect_readonly(self.wiki_db) as conn:
            dup_row = conn.execute(
                "SELECT id, filepath, filename, date FROM documents WHERE content_sha256 = ?",
                (payload_sha,),
            ).fetchone()
        if dup_row is not None:
            return 200, {
                "ok": False,
                "status": "duplicate",
                "duplicate": True,
                "duplicate_reason": "exact_fingerprint",
                "existing_doc_id": f"wiki:{dup_row['id']}",
                "existing_filepath": dup_row["filepath"],
                "sha256": payload_sha,
                "error": f"Duplicate document already exists as wiki:{dup_row['id']}",
            }

        subdir = "companies" if target_type == "company" else "sectors"
        wiki_root_resolved = self.wiki_dir.resolve()
        rel_path = f"wiki/{subdir}/{slug}/{orig_name}"
        dest_path = (self.data_root / rel_path).resolve()
        if not dest_path.is_relative_to(wiki_root_resolved):
            return 400, {"ok": False, "status": "invalid_slug", "error": "Resolved path outside wiki root"}

        # Never overwrite an existing Markdown file that has different content
        if dest_path.exists():
            stem = Path(orig_name).stem
            suffix = Path(orig_name).suffix or ".md"
            candidate_name = f"{stem}-{payload_sha[:8]}{suffix}"
            counter = 1
            while (dest_path.parent / candidate_name).exists():
                counter += 1
                candidate_name = f"{stem}-{payload_sha[:8]}-{counter}{suffix}"
            orig_name = candidate_name
            rel_path = f"wiki/{subdir}/{slug}/{orig_name}"
            dest_path = (self.data_root / rel_path).resolve()
            if not dest_path.is_relative_to(wiki_root_resolved):
                return 400, {"ok": False, "status": "invalid_slug", "error": "Resolved path outside wiki root"}

        orig_rel_path: str | None = None
        orig_dest_path: Path | None = None
        if binary_bytes is not None and orig_binary_name is not None:
            orig_rel_path = f"wiki/{subdir}/{slug}/originals/{orig_binary_name}"
            orig_dest_path = (self.data_root / orig_rel_path).resolve()
            if not orig_dest_path.is_relative_to(wiki_root_resolved):
                return 400, {"ok": False, "status": "invalid_slug", "error": "Resolved original path outside wiki root"}
            if orig_dest_path.exists():
                b_stem = Path(orig_binary_name).stem
                b_suf = Path(orig_binary_name).suffix
                cand_bin = f"{b_stem}-{payload_sha[:8]}{b_suf}"
                counter = 1
                while (orig_dest_path.parent / cand_bin).exists():
                    counter += 1
                    cand_bin = f"{b_stem}-{payload_sha[:8]}-{counter}{b_suf}"
                orig_binary_name = cand_bin
                orig_rel_path = f"wiki/{subdir}/{slug}/originals/{orig_binary_name}"
                orig_dest_path = (self.data_root / orig_rel_path).resolve()
                if not orig_dest_path.is_relative_to(wiki_root_resolved):
                    return 400, {"ok": False, "status": "invalid_slug", "error": "Resolved original path outside wiki root"}

        if dry_run:
            return 200, {
                "ok": True,
                "status": "preview",
                "dry_run": True,
                "target_type": target_type,
                "target": slug,
                "content_type": content_type,
                "date": doc_date,
                "filename": orig_name,
                "filepath": rel_path,
                **({"original_filepath": orig_rel_path} if orig_rel_path else {}),
                "sha256": payload_sha,
                "bytes": len(body_text.encode("utf-8")),
                "duplicate": False,
            }

        # 3. DB-first atomic ingest: insert into SQLite + persist files + record receipt inside ONE transaction;
        #    if SQLite rejects or commit fails, zero disk files remain.
        frontmatter_lines = [
            "---",
            f"target_type: {target_type}",
            f"target: {slug}",
            f"content_type: {content_type}",
            f"date: {doc_date}",
            f"source: {source}",
        ]
        if orig_rel_path:
            frontmatter_lines.append(f"original_file: {orig_rel_path}")
        if source_url:
            frontmatter_lines.append(f"source_url: {source_url}")
        frontmatter_lines.append("---")
        full_md = "\n".join(frontmatter_lines) + "\n\n" + body_text

        created_at = _now_iso()
        tags_json = json.dumps([content_type], ensure_ascii=False)
        related_json = json.dumps([], ensure_ascii=False)
        chunks_list = (
            cli_mod.chunk_text(body_text)
            if cli_mod is not None and hasattr(cli_mod, "chunk_text")
            else chunk_wiki_text(body_text)
        )
        if not chunks_list:
            chunks_list = [body_text]
        md_written = False
        orig_written = False
        conn = sqlite3.connect(self.wiki_db, timeout=20, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if cli_mod is not None and hasattr(cli_mod, "insert_document_and_chunks"):
                doc_num = cli_mod.insert_document_and_chunks(
                    conn,
                    target_type,
                    slug,
                    content_type,
                    doc_date,
                    orig_name,
                    rel_path,
                    chunks_list,
                    embeddings=None,
                    tags_json=tags_json,
                    related_json=related_json,
                    content_sha256=payload_sha,
                    created_at=created_at,
                    commit=False,
                )
            else:
                cur = conn.execute(
                    "INSERT INTO documents (category_type, category_name, content_type, date, filename, filepath, created_at, tags, related, content_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (target_type, slug, content_type, doc_date, orig_name, rel_path, created_at, tags_json, related_json, payload_sha),
                )
                doc_num = cur.lastrowid
                for chunk_idx, chunk_body in enumerate(chunks_list):
                    conn.execute(
                        "INSERT INTO chunks (doc_id, chunk_index, content, embedding) VALUES (?, ?, ?, NULL)",
                        (doc_num, chunk_idx, chunk_body),
                    )

            doc_id = f"wiki:{doc_num}"
            image_doc_id: str | None = None
            if binary_bytes is not None and orig_binary_suffix in _IMAGE_FILE_EXTENSIONS:
                image_doc_id = self.register_image_asset(
                    image_bytes=binary_bytes,
                    suffix=orig_binary_suffix,
                    source="company-wiki",
                    doc_key=doc_id,
                    title=orig_binary_name or orig_name,
                    doc_date=doc_date,
                    metadata={
                        "parent_document_id": doc_id,
                        "original_source": source,
                        "company": slug if target_type == "company" else None,
                        "sector": slug if target_type == "sector" else None,
                        "tags": [content_type],
                        "caption": body_text[:1000],
                    },
                    created_at=created_at,
                )

            receipt = {
                "ok": True,
                "status": "ingested",
                "doc_id": doc_id,
                **({"image_id": image_doc_id} if image_doc_id else {}),
                "filepath": rel_path,
                **({"original_filepath": orig_rel_path} if orig_rel_path else {}),
                "sha256": payload_sha,
                "chunk_count": len(chunks_list),
                "target_type": target_type,
                "target": slug,
                "content_type": content_type,
                "date": doc_date,
                "created_at": created_at,
                **({"request_id": request_id} if request_id else {}),
            }
            receipt_key = request_id or f"sha:{payload_sha}"
            conn.execute(
                "INSERT OR REPLACE INTO ingest_receipts (request_id, doc_id, filepath, sha256, receipt_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (receipt_key, doc_id, rel_path, payload_sha, json.dumps(receipt, ensure_ascii=False), created_at),
            )

            if binary_bytes is not None and orig_dest_path is not None:
                orig_dest_path.parent.mkdir(parents=True, exist_ok=True)
                b_fd = os.open(str(orig_dest_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    with os.fdopen(b_fd, "wb") as bfh:
                        bfh.write(binary_bytes)
                        bfh.flush()
                        os.fsync(bfh.fileno())
                    orig_written = True
                except BaseException:
                    if orig_dest_path.exists():
                        try:
                            orig_dest_path.unlink()
                        except OSError:
                            pass
                    raise

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(dest_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(full_md)
                    fh.flush()
                    os.fsync(fh.fileno())
                md_written = True
            except BaseException:
                if dest_path.exists():
                    try:
                        dest_path.unlink()
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
            if md_written and dest_path.exists():
                try:
                    dest_path.unlink()
                except OSError:
                    pass
            if orig_written and orig_dest_path is not None and orig_dest_path.exists():
                try:
                    orig_dest_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            conn.close()

        self._append_audit(receipt)
        return 200, receipt


class CompanyWikiConnector:
    """MCP connector mounted at ``/mcp/company-wiki`` (public, Core + Everflow)."""

    route_id = "company-wiki"
    server_name = "dalton_company_wiki"
    required_capabilities = (CapabilityId.COMPANY_WIKI,)
    require_all_capabilities = True

    def __init__(self, store: OpenWikiStore) -> None:
        self.store = store

    def list_tools(self) -> list[dict[str, Any]]:
        common_props = {
            "query": {"type": "string", "description": "Search keywords or topic tag"},
            "document": {"type": "string", "description": "Document ID ('wiki:<id>' or 'image:<id>')"},
            "start": {"type": "string", "description": "Start date YYYY-MM-DD"},
            "end": {"type": "string", "description": "End date YYYY-MM-DD"},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            "params": {
                "type": "object",
                "description": (
                    "Optional filters: company, sector, tag, search_mode ('hybrid'|'keyword'), "
                    "no_decay (bool), media ('all'|'text'|'images'), indexed_since (YYYY-MM-DD or UTC ISO), "
                    "offset, max_chars."
                ),
            },
        }
        return [
            {
                "name": "search",
                "description": "Search company-wiki documents and/or images (supports media='all'|'text'|'images', indexed_since UTC ISO).",
                "inputSchema": {
                    "type": "object",
                    "properties": common_props,
                    "required": ["query"],
                },
            },
            {
                "name": "read",
                "description": "Read a company-wiki document ('wiki:<id>') with offset/max_chars paging or an image ('image:<id>').",
                "inputSchema": {
                    "type": "object",
                    "properties": common_props,
                    "required": ["document"],
                },
            },
            {
                "name": "list",
                "description": "List company-wiki documents filtered by company, sector, tag, date range, or UTC indexed_since.",
                "inputSchema": {"type": "object", "properties": common_props},
            },
            {
                "name": "topic",
                "description": "List company-wiki documents by topic tag.",
                "inputSchema": {"type": "object", "properties": common_props},
            },
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
        if CapabilityId.COMPANY_WIKI not in active_caps:
            return {
                "ok": False,
                "status": "capability_disabled",
                "error_code": "capability_disabled",
                "error": "Capability 'company_wiki' is disabled.",
            }
        op = "read" if tool_name in ("read", "get_document", "read_document") else tool_name
        if op not in ("search", "read", "list", "topic"):
            return {"ok": False, "status": "error", "error": f"Unknown company-wiki tool: {tool_name}"}
        params = dict(arguments.get("params") or {})
        for k in ("company", "sector", "tag", "search_mode", "no_decay", "offset", "max_chars", "media", "indexed_since"):
            if k in arguments and arguments[k] is not None and k not in params:
                params[k] = arguments[k]
        query = arguments.get("query")
        ticker = arguments.get("ticker")
        if ticker and not query and op == "search":
            query = str(ticker)
        try:
            return await asyncio.to_thread(
                self.store.query,
                op,
                query=str(query) if query is not None else None,
                document=str(arguments["document"]) if arguments.get("document") is not None else None,
                start=str(arguments["start"]) if arguments.get("start") is not None else None,
                end=str(arguments["end"]) if arguments.get("end") is not None else None,
                limit=int(arguments.get("limit") or 10),
                params=params,
            )
        except Exception as exc:
            return {"ok": False, "status": "error", "error": str(exc)}
