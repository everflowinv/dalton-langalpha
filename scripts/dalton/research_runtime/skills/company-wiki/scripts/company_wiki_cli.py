#!/usr/bin/env python3
"""
company-wiki CLI — Investment knowledge base with semantic search.

Commands:
  add      --company/--sector <name> --type <type> --content <text|@file> [--date YYYY-MM-DD]
  add-file --company/--sector <name> --type <type> --file <path> [--date YYYY-MM-DD]
  extract  --file <path> [--ocr]   Extract text only (no write); used by sales-notes add-note
  search   --query "..."  [--company <name>] [--sector <name>] [--date-from/--date-to] [--top-k N] [--tag <tag>]
  list     [--company <name>] [--sector <name>]
  read     --company/--sector <name> --file <filename>
  links    --company/--sector <name>
  topic    <topic_name>
  reindex  [--tags-only]
  graph    [--company <name>] [--format json|text]
  self-test

Supported file formats for add-file:
  .md .txt      Plain text / Markdown
  .pdf          PDF (text extraction via pymupdf)
  .docx         Word documents (text extraction via python-docx)
  .jpg .png .webp .gif .jpeg   Images (OCR/extraction via Gemini vision)

Flags:
  --json       JSON output mode
  --verbose    Verbose logging
  --help       Show help
"""

import argparse
import json
import os
import re
import sqlite3
import struct
import sys
import textwrap
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure the scripts directory is in sys.path so auto_tagger can be imported
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SKILL_DIR = Path(__file__).resolve().parent.parent



# Google retired gemini-embedding-2-preview on 2026-11-19. The GA model returns the
# same vectors for the same text (cosine 1.0, checked on the sales-notes and wiki
# indexes), so stored labels and configs keep the old name and only the API call maps it.
_API_MODEL_ALIASES = {"gemini-embedding-2-preview": "gemini-embedding-2"}


def api_model(name):
    return _API_MODEL_ALIASES.get(name, name)

def _resolve_research_roots(default_code_root: Path) -> tuple[Path, Path]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    # Legacy compat: when new vars are unset and DALTON_OCW_ROOT is set, keep old single-root layout.
    if raw_ocw and not raw_code and not raw_home:
        ocw = Path(raw_ocw).expanduser().resolve()
        return ocw, ocw
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root


CODE_ROOT, DATA_ROOT = _resolve_research_roots(SKILL_DIR.parent.parent)
WORKSPACE = DATA_ROOT
CONFIG_PATH = SKILL_DIR / "config.json"
CONFIG_EXAMPLE_PATH = SKILL_DIR / "config.example.json"
WIKI_DIR = DATA_ROOT / "wiki"
COMPANIES_DIR = WIKI_DIR / "companies"
SECTORS_DIR = WIKI_DIR / "sectors"
DB_PATH = WIKI_DIR / "vectors.db"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
JSON_MODE = False
VERBOSE = False


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    if CONFIG_EXAMPLE_PATH.exists():
        # Auto-copy example config
        import shutil
        shutil.copy(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
        log("Auto-created config.json from config.example.json")
        with open(CONFIG_PATH) as f:
            return json.load(f)
    fail("No config file found", "Create config.json in skill directory")


def log(msg):
    if VERBOSE:
        print(f"[company-wiki] {msg}", file=sys.stderr)


def output(data):
    if JSON_MODE:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict):
            if data.get("ok"):
                for k, v in data.items():
                    if k == "ok":
                        continue
                    if isinstance(v, list):
                        for item in v:
                            print(f"  - {item}")
                    else:
                        print(f"{k}: {v}")
            else:
                print(f"ERROR: {data.get('error', 'unknown')}")
                if data.get("hint"):
                    print(f"Hint: {data['hint']}")
        else:
            print(data)


def fail(error, hint=None):
    d = {"ok": False, "error": error}
    if hint:
        d["hint"] = hint
    output(d)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Embedding helpers (Gemini)
# ---------------------------------------------------------------------------
_genai_client = None


def get_genai_client():
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        fail("GEMINI_API_KEY not set", "Export GEMINI_API_KEY in your environment")

    from google import genai
    _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def resolve_embedding_model(config):
    """Try preferred model first; fall back only to a model in the same vector space."""
    preferred = config["embedding"].get("model_preferred")
    default = config["embedding"]["model"]

    if preferred:
        error = "empty embedding"
        try:
            client = get_genai_client()
            # Test with a tiny embed call
            result = client.models.embed_content(
                model=api_model(preferred),
                contents=["test"],
                config={"task_type": "RETRIEVAL_DOCUMENT"}
            )
            if result.embeddings and len(result.embeddings[0].values) > 0:
                log(f"Using preferred model: {preferred} (dims={len(result.embeddings[0].values)})")
                return preferred, len(result.embeddings[0].values)
        except Exception as e:
            error = e
        if api_model(preferred) != api_model(default):
            # A different model writes and queries in an unrelated vector space
            # (gemini-embedding-001 vs the stored index: cosine ~0), so fail instead.
            fail(f"Embedding model {preferred} failed and {default} is not compatible: {error}")
        log(f"Preferred model {preferred} not available: {error}")

    # Fall back to default
    try:
        client = get_genai_client()
        result = client.models.embed_content(
            model=api_model(default),
            contents=["test"],
            config={"task_type": "RETRIEVAL_DOCUMENT"}
        )
        dims = len(result.embeddings[0].values)
        log(f"Using default model: {default} (dims={dims})")
        return default, dims
    except Exception as e:
        fail(f"Embedding model {default} failed: {e}")

def embed_texts(texts, model_name):
    """Embed a list of texts using Gemini embedding API with true batch calls."""
    from google.genai import types

    client = get_genai_client()
    log(f"Embedding {len(texts)} chunks with {model_name}")

    import numpy as np
    embeddings = []
    BATCH_SIZE = 100
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        for attempt in range(5):
            try:
                # One Content per text: gemini-embedding-2 folds a list of plain
                # strings into a single vector (seen 2026-11-19).
                result = client.models.embed_content(
                    model=api_model(model_name),
                    contents=[types.Content(parts=[types.Part(text=t)]) for t in batch],
                    config={"task_type": "RETRIEVAL_DOCUMENT"}
                )
                if len(result.embeddings) != len(batch):
                    raise RuntimeError(
                        f"embedding returned {len(result.embeddings)} vectors for {len(batch)} texts"
                    )
                for emb in result.embeddings:
                    embeddings.append(emb.values)
                break
            except Exception as e:
                err_str = str(e)
                if ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str) and attempt < 4:
                    wait = 2 ** attempt * 5
                    log(f"Rate limited, waiting {wait}s (attempt {attempt + 1}/5)")
                    time.sleep(wait)
                else:
                    raise

    return embeddings


def embed_query(text, model_name):
    """Embed a single query text."""
    client = get_genai_client()
    result = client.models.embed_content(
        model=api_model(model_name),
        contents=text,
        config={"task_type": "RETRIEVAL_QUERY"}
    )
    return result.embeddings[0].values


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------
def chunk_text(text, chunk_size=1500, overlap=200):
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        # Try to break at paragraph or sentence boundary
        if end < len(text):
            # Look for last paragraph break
            last_para = chunk.rfind("\n\n")
            if last_para > chunk_size * 0.5:
                end = start + last_para + 2
                chunk = text[start:end]
            else:
                # Look for last sentence break
                for sep in ["。", ".", "\n"]:
                    last_sep = chunk.rfind(sep)
                    if last_sep > chunk_size * 0.3:
                        end = start + last_sep + 1
                        chunk = text[start:end]
                        break
        chunks.append(chunk.strip())
        start = end - overlap
        if start >= len(text):
            break

    return [c for c in chunks if c]


def build_chunk_context(cat_type, cat_name, content_type, date_str, filename=None):
    """Metadata header prepended to each chunk before embedding/indexing.

    A chunk in the middle of a long document often never names the company,
    date, or document type; without this header neither dense embeddings nor
    the keyword index can associate it with its source document.
    """
    label = "公司" if cat_type == "company" else "行业"
    parts = [f"{cat_name} ({label})", content_type, date_str]
    if filename:
        title = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", filename)
        title = re.sub(r"\.md$", "", title)
        if title and title != content_type:
            parts.append(title)
    return "【" + " | ".join(parts) + "】\n"


def contextualize_chunks(chunks, cat_type, cat_name, content_type, date_str, filename=None):
    ctx = build_chunk_context(cat_type, cat_name, content_type, date_str, filename)
    return [ctx + c for c in chunks]


# ---------------------------------------------------------------------------
# Vector DB (SQLite)
# ---------------------------------------------------------------------------
def init_db():
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_type TEXT NOT NULL,       -- 'company' or 'sector'
            category_name TEXT NOT NULL,        -- e.g. 'futu', 'china-internet'
            content_type TEXT NOT NULL,         -- e.g. '管理层会议纪要'
            date TEXT NOT NULL,                 -- YYYY-MM-DD
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(filepath)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB,
            FOREIGN KEY (doc_id) REFERENCES documents(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_docs_category
        ON documents(category_type, category_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_docs_date
        ON documents(date)
    """)

    # --- Schema migration: add tags and related columns ---
    # ALTER TABLE ADD COLUMN is safe on existing tables; new columns default to NULL.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    if "tags" not in existing_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN tags TEXT")
        log("Migrated DB: added 'tags' column to documents")
    if "related" not in existing_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN related TEXT")
        log("Migrated DB: added 'related' column to documents")

    # --- Schema migration: FTS5 keyword index over chunks (hybrid search) ---
    # trigram tokenizer requires SQLite >= 3.34; if unavailable, search
    # degrades gracefully to dense-only.
    try:
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone() is not None
        if not fts_exists:
            conn.execute("""
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                    content, content='chunks', content_rowid='id', tokenize='trigram'
                )
            """)
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            log("Migrated DB: created chunks_fts FTS5 keyword index (trigram)")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content)
                VALUES('delete', old.id, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE OF content ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content)
                VALUES('delete', old.id, old.content);
                INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
    except sqlite3.OperationalError as e:
        log(f"FTS5 index unavailable ({e}); keyword search arm disabled")

    conn.commit()
    return conn


def store_document(conn, cat_type, cat_name, content_type, date_str, filename, filepath,
                   chunks_text, embeddings, tags_json=None, related_json=None):
    """Store a document and its chunks with embeddings.

    Args:
        tags_json: JSON string of tags array, e.g. '["跨境电商", "AI"]'
        related_json: JSON string of related slugs, e.g. '["BABA", "JD", "china-internet"]'
    """
    import numpy as np

    now = datetime.now(timezone.utc).isoformat()

    # Upsert document record
    cur = conn.execute(
        "SELECT id FROM documents WHERE filepath = ?", (filepath,)
    )
    row = cur.fetchone()
    if row:
        doc_id = row[0]
        conn.execute(
            "UPDATE documents SET date=?, content_type=?, created_at=?, tags=?, related=? WHERE id=?",
            (date_str, content_type, now, tags_json, related_json, doc_id))
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    else:
        cur = conn.execute(
            """INSERT INTO documents (category_type, category_name, content_type, date, filename, filepath, created_at, tags, related)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cat_type, cat_name, content_type, date_str, filename, filepath, now, tags_json, related_json)
        )
        doc_id = cur.lastrowid

    # Store chunks
    for i, (chunk, emb) in enumerate(zip(chunks_text, embeddings)):
        emb_blob = np.array(emb, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT INTO chunks (doc_id, chunk_index, content, embedding) VALUES (?, ?, ?, ?)",
            (doc_id, i, chunk, emb_blob)
        )

    conn.commit()
    return doc_id


def tagger_context(cat_type, cat_name, content_type, conn=None, exclude_doc_id=None):
    """Filing context for auto_tag. For a company, also count the related ids its other
    documents carry, so new tags follow the library's sector convention (auto_tag keeps
    only valid sector ids from these counts)."""
    ctx = {"category_type": cat_type, "category_name": cat_name, "content_type": content_type}
    if cat_type != "company":
        return ctx
    own = conn or init_db()
    try:
        rows = own.execute(
            "SELECT related FROM documents WHERE category_type='company' AND category_name=? AND id IS NOT ?",
            (cat_name, exclude_doc_id)).fetchall()
    finally:
        if conn is None:
            own.close()
    counts = {}
    for (related,) in rows:
        try:
            items = json.loads(related or "[]")
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        for x in set(i for i in items if isinstance(i, str)):
            counts[x] = counts.get(x, 0) + 1
    ctx["sibling_docs"] = len(rows)
    ctx["sibling_related_counts"] = counts
    return ctx


def update_document_tags(conn, doc_id, tags_json, related_json):
    """Update only the tags and related columns for a document (no re-embedding)."""
    conn.execute(
        "UPDATE documents SET tags=?, related=? WHERE id=?",
        (tags_json, related_json, doc_id)
    )
    conn.commit()


def build_doc_filters(cat_type=None, cat_name=None, date_from=None, date_to=None, tag=None):
    """Shared WHERE clauses (on documents alias 'd') for both search arms."""
    where_clauses = []
    params = []
    if cat_type:
        where_clauses.append("d.category_type = ?")
        params.append(cat_type)
    if cat_name:
        where_clauses.append("d.category_name = ?")
        params.append(cat_name)
    if date_from:
        where_clauses.append("d.date >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("d.date <= ?")
        params.append(date_to)
    if tag:
        # Match tag inside JSON array — look for the quoted string within the JSON
        where_clauses.append("d.tags LIKE ?")
        params.append(f'%"{tag}"%')
    return where_clauses, params


_ROW_FIELDS = """c.id, c.doc_id, c.content, d.category_type, d.category_name,
                 d.content_type, d.date, d.filename, d.filepath"""


def dense_search(conn, query_embedding, filters, limit=50):
    """Cosine similarity over all chunk embeddings. Returns ranked row list."""
    import numpy as np

    query_vec = np.array(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []

    where_clauses, params = filters
    where_sql = " AND ".join(["c.embedding IS NOT NULL"] + where_clauses)

    rows = conn.execute(
        f"""SELECT {_ROW_FIELDS}, c.embedding
            FROM chunks c
            JOIN documents d ON c.doc_id = d.id
            WHERE {where_sql}""",
        params
    ).fetchall()

    scored = []
    for row in rows:
        emb_vec = np.frombuffer(row[-1], dtype=np.float32)
        emb_norm = np.linalg.norm(emb_vec)
        if emb_norm == 0:
            continue
        score = float(np.dot(query_vec, emb_vec) / (query_norm * emb_norm))
        scored.append((score, row[:-1]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(row, score) for score, row in scored[:limit]]


def keyword_search(conn, query, filters, limit=50):
    """Keyword arm: FTS5 trigram for terms >= 3 chars, LIKE scan for 2-char terms.

    The trigram tokenizer cannot match terms shorter than 3 characters, and
    two-character Chinese names (宇树, 美团) are common in queries — those go
    through a LIKE scan over chunks instead (a few thousand rows, cheap).
    """
    terms = [t for t in re.split(r"[\s,;，；、。/|\"'()（）\[\]【】?？!！:：]+", query) if t]
    uniq = list(dict.fromkeys(terms))
    fts_terms = [t for t in uniq if len(t) >= 3]
    like_terms = [t for t in uniq if len(t) == 2]

    where_clauses, params = filters
    extra_sql = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""

    ranked = []
    seen_ids = set()
    if fts_terms:
        match_expr = " OR ".join('"' + t.replace('"', "") + '"' for t in fts_terms)
        try:
            rows = conn.execute(
                f"""SELECT {_ROW_FIELDS}
                    FROM chunks_fts f
                    JOIN chunks c ON c.id = f.rowid
                    JOIN documents d ON c.doc_id = d.id
                    WHERE chunks_fts MATCH ? {extra_sql}
                    ORDER BY bm25(chunks_fts)
                    LIMIT ?""",
                [match_expr] + params + [limit]
            ).fetchall()
            ranked.extend(rows)
            seen_ids.update(r[0] for r in rows)
        except sqlite3.OperationalError as e:
            log(f"FTS keyword search unavailable ({e})")

    if like_terms and len(ranked) < limit:
        like_sql = " AND ".join(["c.content LIKE ?"] * len(like_terms))
        like_params = [f"%{t}%" for t in like_terms]
        try:
            rows = conn.execute(
                f"""SELECT {_ROW_FIELDS}
                    FROM chunks c
                    JOIN documents d ON c.doc_id = d.id
                    WHERE {like_sql} {extra_sql}
                    ORDER BY d.date DESC
                    LIMIT ?""",
                like_params + params + [limit]
            ).fetchall()
            for r in rows:
                if r[0] not in seen_ids and len(ranked) < limit:
                    ranked.append(r)
                    seen_ids.add(r[0])
        except sqlite3.OperationalError as e:
            log(f"LIKE keyword search failed ({e})")

    return ranked


RRF_K = 60


def hybrid_search(conn, query, query_embedding, config, top_k=10, filters=None,
                  dense_only=False, no_decay=False):
    """Dense + keyword retrieval fused with RRF, aggregated to document level,
    with optional recency decay.

    Returns top_k *documents* (best chunk per document), so one long document
    cannot occupy multiple result slots.
    """
    filters = filters or ([], [])
    candidates = max(50, top_k * 5)

    dense_rows = dense_search(conn, query_embedding, filters, limit=candidates)
    kw_rows = [] if dense_only else keyword_search(conn, query, filters, limit=candidates)

    # RRF fusion at chunk level
    fused = {}  # chunk_id -> entry
    for arm, rows in (("dense", [r for r, _ in dense_rows]), ("keyword", kw_rows)):
        for rank, row in enumerate(rows):
            cid = row[0]
            entry = fused.setdefault(cid, {"row": row, "score": 0.0, "arms": set()})
            entry["score"] += 1.0 / (RRF_K + rank + 1)
            entry["arms"].add(arm)
    for row, score in dense_rows:
        if row[0] in fused:
            fused[row[0]]["dense_score"] = score

    # Aggregate to document level: keep the best-scoring chunk per document
    docs = {}
    for entry in fused.values():
        doc_id = entry["row"][1]
        cur = docs.get(doc_id)
        if cur is None or entry["score"] > cur["score"]:
            arms = (cur["arms"] | entry["arms"]) if cur else entry["arms"]
            entry["arms"] = arms
            docs[doc_id] = entry
        else:
            cur["arms"] |= entry["arms"]

    # Recency decay (light, exponential; investment content ages fast)
    search_cfg = config.get("search", {})
    half_life = search_cfg.get("recency_half_life_days", 540)
    floor = search_cfg.get("recency_floor", 0.5)
    today = datetime.now().date()

    results = []
    for entry in docs.values():
        cid, doc_id, content, ctype, cname, content_type, date, fname, fpath = entry["row"]
        score = entry["score"]
        if not no_decay:
            try:
                age_days = (today - datetime.strptime(date, "%Y-%m-%d").date()).days
                if age_days > 0:
                    score *= max(floor, 0.5 ** (age_days / half_life))
            except (ValueError, TypeError):
                pass
        item = {
            "score": round(score, 4),
            "matched": sorted(entry["arms"]),
            "category_type": ctype,
            "category_name": cname,
            "content_type": content_type,
            "date": date,
            "filename": fname,
            "filepath": fpath,
            "content_preview": content[:500]
        }
        if "dense_score" in entry:
            item["dense_score"] = round(entry["dense_score"], 4)
        results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------
def update_index(target_dir, cat_type, cat_name):
    """Regenerate index.md for a company or sector directory.

    Includes:
    - Document list with tags (if available)
    - Reverse references: other docs that mention this company/sector
    """
    index_path = target_dir / "index.md"
    files = sorted([f for f in target_dir.iterdir()
                    if f.is_file() and f.name != "index.md" and f.suffix == ".md"],
                   reverse=True)

    label = cat_name.replace("-", " ").title()
    lines = [f"# {label}\n\n"]
    lines.append(f"**类型**: {'公司' if cat_type == 'company' else '行业'}\n")
    lines.append(f"**文档数**: {len(files)}\n\n")
    lines.append("## 本公司文档\n\n")

    # Try to read tags from DB for each file
    doc_tags = {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        for f in files:
            # Exact path first. The LIKE fallback (case-insensitive, for dir-case
            # drift on macOS) must anchor on "/": "%NET/x" also matched "us-internet/x".
            row = conn.execute(
                "SELECT tags FROM documents WHERE filepath = ?",
                (str(f.relative_to(WORKSPACE)),)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT tags FROM documents WHERE filepath LIKE ?",
                    (f"%/{cat_name}/{f.name}",)
                ).fetchone()
            if row and row[0]:
                try:
                    doc_tags[f.name] = json.loads(row[0])
                except json.JSONDecodeError:
                    pass
    except Exception:
        conn = None

    for f in files:
        # Parse date and type from filename
        parts = f.stem.split("-", 3)
        if len(parts) >= 4:
            date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"
            doc_type = parts[3]
        else:
            date_str = "N/A"
            doc_type = f.stem
        tags = doc_tags.get(f.name)
        if tags:
            tag_str = ", ".join(tags)
            lines.append(f"- **{date_str}** — {doc_type} (`{f.name}`) [tags: {tag_str}]\n")
        else:
            lines.append(f"- **{date_str}** — {doc_type} (`{f.name}`)\n")

    # Reverse references: find other docs that mention this category_name
    if conn is not None:
        try:
            # Search for this category name in the 'related' column of OTHER companies/sectors
            pattern = f'%"{cat_name}"%'
            refs = conn.execute(
                """SELECT category_type, category_name, date, content_type, filename, related
                   FROM documents
                   WHERE related LIKE ? AND NOT (category_type = ? AND category_name = ?)
                   ORDER BY date DESC""",
                (pattern, cat_type, cat_name)
            ).fetchall()
            if refs:
                lines.append("\n## 被其他公司/行业提及\n\n")
                for ref_row in refs:
                    r_type, r_name, r_date, r_ctype, r_fname, r_related = ref_row
                    lines.append(f"- {r_name} / {r_date} {r_ctype} (`{r_fname}`)\n")
        except Exception:
            pass  # If DB doesn't have 'related' column yet, skip gracefully
        finally:
            conn.close()

    index_path.write_text("".join(lines), encoding="utf-8")
    log(f"Updated index: {index_path}")


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------
def slugify(name):
    """Convert a name to a filesystem-safe slug."""
    name = name.strip().lower()
    # Keep Chinese characters, alphanumeric, hyphens
    name = re.sub(r'[^\w\u4e00-\u9fff\-]', '-', name)
    name = re.sub(r'-+', '-', name).strip('-')
    return name


def resolve_category_name(raw_name, cat_type, config=None):
    """Resolve a category name against aliases and existing directories.

    Resolution order:
    1. Check aliases in config.json (e.g. 'affirm' -> 'AFRM')
    2. Exact match against existing directories
    3. Case-insensitive match against existing directories
    4. Fall back to slugify(raw_name)
    """
    raw_lower = raw_name.strip().lower()

    # 1. Check aliases from config
    if config and "aliases" in config:
        aliases = config["aliases"]
        # Build case-insensitive alias lookup
        alias_map = {k.lower(): v for k, v in aliases.items()}
        if raw_lower in alias_map:
            return alias_map[raw_lower]

    slug = slugify(raw_name)
    base_dir = COMPANIES_DIR if cat_type == "company" else SECTORS_DIR
    if base_dir.exists():
        existing = {d.name: d.name for d in base_dir.iterdir() if d.is_dir()}
        # 2. Exact match
        if slug in existing:
            return slug
        # 3. Case-insensitive match
        for dirname in existing:
            if dirname.lower() == slug.lower():
                return dirname
        # Also try raw input against existing
        for dirname in existing:
            if dirname.lower() == raw_lower:
                return dirname
    # 4. Fallback. slugify() lowercases, but new company dirs must be
    # uppercase slugs — uppercase pure-ASCII fallbacks so 'testco'/'TESTCO'
    # both resolve to 'TESTCO' instead of dying in validation.
    if cat_type == "company" and re.fullmatch(r"[a-z][a-z0-9-]*", slug):
        return slug.upper()
    return slug


COMPANY_SLUG_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,29}$")
SECTOR_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


def validate_category_name(cat_name, cat_type):
    """Reject new category directories that violate naming rules.

    Existing directories pass unchanged; validation only gates creation of new
    ones, so historical data keeps working while bad new names are blocked at
    the door instead of cleaned up later.
    """
    base_dir = COMPANIES_DIR if cat_type == "company" else SECTORS_DIR
    # exact-name match (APFS is case-insensitive; Path.exists() would wrongly
    # accept 'msft' when only 'MSFT' exists)
    if base_dir.exists() and cat_name in {d.name for d in base_dir.iterdir() if d.is_dir()}:
        return
    if cat_name.lower().startswith("temp"):
        fail(f"Invalid {cat_type} name '{cat_name}': temp* is reserved",
             "先确定归属的公司/行业再入库，不要建 temp 目录")
    pattern = COMPANY_SLUG_RE if cat_type == "company" else SECTOR_SLUG_RE
    if not pattern.match(cat_name):
        if cat_type == "company":
            fail(f"Invalid company slug '{cat_name}' (new company dirs must match ^[A-Z][A-Z0-9-]*$, e.g. UNITREE)",
                 "中文名/数字代码先在 config.json aliases 加映射（如 \"宇树科技\": \"UNITREE\"），再重跑本命令")
        fail(f"Invalid sector slug '{cat_name}' (new sector dirs must match ^[a-z][a-z0-9-]*$, e.g. china-robotics)",
             "新行业用小写字母加连字符；中文名先在 config.json aliases 加映射")


# ---------------------------------------------------------------------------
# File extraction helpers
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PDF_EXTENSIONS = {".pdf"}
WORD_EXTENSIONS = {".docx"}
TEXT_EXTENSIONS = {".md", ".txt", ".markdown", ".rst"}


def extract_from_image(file_path, config):
    """Extract text/information from an image using Gemini vision API."""
    import base64

    client = get_genai_client()
    vision_model = config.get("vision", {}).get("model", "gemini-2.5-flash")

    suffix = file_path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif"}
    mime_type = mime_map.get(suffix, "image/jpeg")

    with open(file_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    log(f"Extracting text from image: {file_path.name} ({mime_type})")

    from google.genai import types
    response = client.models.generate_content(
        model=vision_model,
        contents=[
            types.Content(parts=[
                types.Part(inline_data=types.Blob(mime_type=mime_type, data=base64.b64decode(image_data))),
                types.Part(text=(
                    "请完整提取这张图片中的所有文字和信息。"
                    "包括所有数据、表格、图表中的数字、标题、注释、水印文字等。"
                    "如果有表格，请用markdown表格格式输出。"
                    "如果有图表，请描述图表内容和关键数据点。"
                    "请用原文语言输出（中文内容用中文，英文内容用英文）。"
                    "只输出提取的内容，不要加额外说明。"
                ))
            ])
        ]
    )

    extracted = response.text.strip() if response.text else ""
    if not extracted:
        fail(f"Failed to extract text from image: {file_path.name}")
    log(f"Extracted {len(extracted)} chars from image")
    return extracted


def extract_from_pdf(file_path, allow_image_only=False):
    """Extract text from a PDF file using pymupdf."""
    try:
        import fitz  # pymupdf
    except ImportError:
        fail("pymupdf not installed", "Run: pip install pymupdf")

    log(f"Extracting text from PDF: {file_path.name}")
    doc = fitz.open(str(file_path))
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            pages.append(f"--- Page {i + 1} ---\n{text.strip()}")
    doc.close()

    if not pages:
        if allow_image_only:
            return ""
        # PDF might be image-based, try to note that
        fail(f"No extractable text in PDF: {file_path.name}",
             "This may be a scanned/image PDF. Try converting to images first, "
             "then use add-file with the image.")

    full_text = "\n\n".join(pages)
    log(f"Extracted {len(full_text)} chars from {len(pages)} pages")
    return full_text


def extract_from_docx(file_path, allow_image_only=False):
    """Extract text from a Word .docx file."""
    try:
        from docx import Document
    except ImportError:
        fail("python-docx not installed", "Run: pip install python-docx")

    log(f"Extracting text from DOCX: {file_path.name}")
    doc = Document(str(file_path))

    paragraphs = []
    for para in doc.paragraphs:
        if para.text.strip():
            paragraphs.append(para.text.strip())

    # Also extract tables
    for table in doc.tables:
        rows = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            rows.append(" | ".join(cells))
        if rows:
            paragraphs.append("\n".join(rows))

    if not paragraphs:
        if allow_image_only:
            return ""
        fail(f"No extractable text in DOCX: {file_path.name}")

    full_text = "\n\n".join(paragraphs)
    log(f"Extracted {len(full_text)} chars from DOCX")
    return full_text


def extract_from_file(file_path, config, allow_image_only=False):
    """Auto-detect file type and extract text content."""
    suffix = file_path.suffix.lower()

    if suffix in TEXT_EXTENSIONS:
        return file_path.read_text(encoding="utf-8")
    elif suffix in IMAGE_EXTENSIONS:
        return extract_from_image(file_path, config)
    elif suffix in PDF_EXTENSIONS:
        return extract_from_pdf(file_path, allow_image_only)
    elif suffix in WORD_EXTENSIONS:
        return extract_from_docx(file_path, allow_image_only)
    else:
        fail(f"Unsupported file format: {suffix}",
             f"Supported: {', '.join(sorted(TEXT_EXTENSIONS | IMAGE_EXTENSIONS | PDF_EXTENSIONS | WORD_EXTENSIONS))}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_add(args, config):
    """Add content to the wiki."""
    cat_type = "company" if args.company else "sector"
    cat_name = resolve_category_name(args.company or args.sector, cat_type, config)
    validate_category_name(cat_name, cat_type)
    content_type = args.type
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    # Resolve content (from file or inline)
    if args.content.startswith("@"):
        fpath = Path(args.content[1:]).expanduser()
        if not fpath.exists():
            fail(f"Content file not found: {fpath}")
        content = fpath.read_text(encoding="utf-8")
    else:
        content = args.content

    if not content.strip():
        fail("Content is empty")

    # --- Auto-tag (optional) ---
    tag_result = None
    tags_json = None
    related_json = None
    if config.get("tagger", {}).get("enabled", False):
        try:
            from auto_tagger import auto_tag, build_frontmatter, tags_to_json, related_to_json
            tag_result = auto_tag(content, config, get_client_fn=get_genai_client, verbose=VERBOSE,
                                  context=tagger_context(cat_type, cat_name, content_type))
            tags_json = tags_to_json(tag_result)
            related_json = related_to_json(tag_result)
            log(f"Auto-tagged: tags={tag_result.get('tags')}, related={tag_result.get('related_companies')}")
        except Exception as e:
            log(f"Auto-tagger failed (non-fatal): {e}")

    # Determine target directory
    base_dir = COMPANIES_DIR if cat_type == "company" else SECTORS_DIR
    target_dir = base_dir / cat_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename
    type_slug = re.sub(r"\s+", "-", content_type.strip())
    filename = f"{date_str}-{type_slug}.md"
    filepath = target_dir / filename

    # Handle duplicate filenames — append counter
    if filepath.exists():
        counter = 2
        while filepath.exists():
            filename = f"{date_str}-{type_slug}-{counter}.md"
            filepath = target_dir / filename
            counter += 1

    # Write content file (with optional YAML frontmatter)
    extras = {}
    if getattr(args, "source", None):
        extras["source"] = args.source
    if getattr(args, "source_url", None):
        extras["source_url"] = args.source_url
    parts = []
    has_tags = tag_result and any(tag_result.get(k) for k in ("tags", "related_companies", "related_sectors"))
    if has_tags or extras:
        from auto_tagger import build_frontmatter
        parts.append(build_frontmatter(tag_result or {}, extras))
    header = f"# {content_type}\n\n"
    header += f"**日期**: {date_str}\n"
    header += f"**分类**: {cat_name} ({'公司' if cat_type == 'company' else '行业'})\n\n"
    header += "---\n\n"
    parts.append(header + content)
    full_content = "".join(parts)

    filepath.write_text(full_content, encoding="utf-8")
    log(f"Wrote: {filepath}")

    # Chunk and embed
    chunk_size = config["embedding"].get("chunk_size", 1500)
    chunk_overlap = config["embedding"].get("chunk_overlap", 200)
    chunks = chunk_text(content, chunk_size, chunk_overlap)
    chunks = contextualize_chunks(chunks, cat_type, cat_name, content_type, date_str, filename)
    log(f"Created {len(chunks)} chunks")

    # Resolve embedding model
    model_name, dims = resolve_embedding_model(config)
    embeddings = embed_texts(chunks, model_name)

    # Store in DB
    conn = init_db()
    rel_path = str(filepath.relative_to(WORKSPACE))
    doc_id = store_document(conn, cat_type, cat_name, content_type, date_str,
                            filename, rel_path, chunks, embeddings,
                            tags_json=tags_json, related_json=related_json)
    conn.close()

    # Update index
    update_index(target_dir, cat_type, cat_name)

    result = {
        "ok": True,
        "action": "added",
        "doc_id": doc_id,
        "category_type": cat_type,
        "category_name": cat_name,
        "content_type": content_type,
        "date": date_str,
        "filename": filename,
        "filepath": rel_path,
        "chunks": len(chunks),
        "embedding_model": model_name,
        "embedding_dims": dims
    }
    if tag_result:
        result["tags"] = tag_result.get("tags", [])
        result["related_companies"] = tag_result.get("related_companies", [])
        result["related_sectors"] = tag_result.get("related_sectors", [])
    output(result)


def cmd_add_file(args, config):
    """Add content from a file (image/PDF/Word/text) to the wiki."""
    cat_type = "company" if args.company else "sector"
    cat_name = resolve_category_name(args.company or args.sector, cat_type, config)
    validate_category_name(cat_name, cat_type)
    content_type = args.type
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    file_path = Path(args.file).expanduser().resolve()
    if not file_path.exists():
        fail(f"File not found: {file_path}")

    # Standalone images are indexed visually by default, with OCR only on request.
    image_only = (file_path.suffix.lower() in IMAGE_EXTENSIONS and
                  getattr(args, "keep_original", True) and not getattr(args, "ocr", False))
    allow_image_only = getattr(args, 'keep_original', True) and not getattr(args, 'ocr', False)
    visual_stub = (f"图片资料：{cat_name} / {content_type} / {file_path.name}。\n"
                   "原图单独建立图片向量；此处未预先识别图中文字。请读图后引用内容。")
    content = visual_stub if image_only else extract_from_file(file_path, config, allow_image_only)
    if not content.strip() and allow_image_only and file_path.suffix.lower() in (PDF_EXTENSIONS | WORD_EXTENSIONS):
        content = visual_stub
        image_only = True
    if not content.strip():
        fail("Extracted content is empty")

    source_suffix = file_path.suffix.lower()
    source_note = f"[提取自: {file_path.name} ({source_suffix})]"

    # --- Auto-tag (optional) ---
    tag_result = None
    tags_json = None
    related_json = None
    if not image_only and config.get("tagger", {}).get("enabled", False):
        try:
            from auto_tagger import auto_tag, build_frontmatter, tags_to_json, related_to_json
            tag_result = auto_tag(content, config, get_client_fn=get_genai_client, verbose=VERBOSE,
                                  context=tagger_context(cat_type, cat_name, content_type))
            tags_json = tags_to_json(tag_result)
            related_json = related_to_json(tag_result)
            log(f"Auto-tagged: tags={tag_result.get('tags')}, related={tag_result.get('related_companies')}")
        except Exception as e:
            log(f"Auto-tagger failed (non-fatal): {e}")

    # Determine target directory
    base_dir = COMPANIES_DIR if cat_type == "company" else SECTORS_DIR
    target_dir = base_dir / cat_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename (always save as .md)
    type_slug = re.sub(r"\s+", "-", content_type.strip())
    filename = f"{date_str}-{type_slug}.md"
    filepath = target_dir / filename

    # Handle duplicate filenames
    if filepath.exists():
        counter = 2
        while filepath.exists():
            filename = f"{date_str}-{type_slug}-{counter}.md"
            filepath = target_dir / filename
            counter += 1

    # Write content file (with optional YAML frontmatter)
    extras = {}
    if getattr(args, "source", None):
        extras["source"] = args.source
    if getattr(args, "source_url", None):
        extras["source_url"] = args.source_url
    parts = []
    has_tags = tag_result and any(tag_result.get(k) for k in ("tags", "related_companies", "related_sectors"))
    if has_tags or extras:
        from auto_tagger import build_frontmatter
        parts.append(build_frontmatter(tag_result or {}, extras))
    header = f"# {content_type}\n\n"
    header += f"**日期**: {date_str}\n"
    header += f"**分类**: {cat_name} ({'公司' if cat_type == 'company' else '行业'})\n"
    header += f"**来源文件**: {file_path.name}\n\n"
    header += "---\n\n"
    parts.append(header + content)
    full_content = "".join(parts)

    filepath.write_text(full_content, encoding="utf-8")
    log(f"Wrote: {filepath}")

    # Copy original file to an 'originals' subfolder (default on; --no-keep-original opts out)
    if getattr(args, 'keep_original', True):
        originals_dir = target_dir / "originals"
        originals_dir.mkdir(exist_ok=True)
        import shutil
        original_copy = originals_dir / (filepath.stem + source_suffix)
        if not original_copy.exists():
            shutil.copy2(file_path, original_copy)
            log(f"Saved original: {original_copy}")

    # Chunk and embed
    chunk_size = config["embedding"].get("chunk_size", 1500)
    chunk_overlap = config["embedding"].get("chunk_overlap", 200)
    chunks = chunk_text(content, chunk_size, chunk_overlap)
    chunks = contextualize_chunks(chunks, cat_type, cat_name, content_type, date_str, filename)
    log(f"Created {len(chunks)} chunks")

    model_name, dims = resolve_embedding_model(config)
    embeddings = embed_texts(chunks, model_name)

    # Store in DB
    conn = init_db()
    rel_path = str(filepath.relative_to(WORKSPACE))
    doc_id = store_document(conn, cat_type, cat_name, content_type, date_str,
                            filename, rel_path, chunks, embeddings,
                            tags_json=tags_json, related_json=related_json)
    conn.close()

    # Update index
    update_index(target_dir, cat_type, cat_name)

    if str(CODE_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
    from research_images import enqueue, drain
    image_job = None
    image_result = {"ok": True, "status": "opted_out", "reason": "no_keep_original"}
    if getattr(args, 'keep_original', True):
        image_job = enqueue("company-wiki", "wiki:"+str(doc_id), "file", {"path":str(original_copy)}, {
            "title":filename,"date":date_str,"parent_document_id":"wiki:"+str(doc_id),
            "original_source":getattr(args,'source',None) or "company-wiki",
            "company":cat_name if cat_type=='company' else None,"sector":cat_name if cat_type=='sector' else None,
            "tags":tag_result.get('tags',[]) if tag_result else []}, root=DATA_ROOT)
        image_result = drain("company-wiki")
    result = {
        "ok": True,
        "action": "added_from_file",
        "doc_id": doc_id,
        "category_type": cat_type,
        "category_name": cat_name,
        "content_type": content_type,
        "date": date_str,
        "filename": filename,
        "filepath": rel_path,
        "source_file": file_path.name,
        "source_format": source_suffix,
        "image_job": image_job,
        "image_ingestion": image_result,
        "image_text_mode": "deferred" if image_only else "existing_text_or_explicit_ocr",
        "extracted_chars": len(content),
        "chunks": len(chunks),
        "embedding_model": model_name,
        "embedding_dims": dims
    }
    if tag_result:
        result["tags"] = tag_result.get("tags", [])
        result["related_companies"] = tag_result.get("related_companies", [])
        result["related_sectors"] = tag_result.get("related_sectors", [])
    output(result)


def cmd_extract(args, config):
    """Extract text from a file without writing anything (used by sales-notes add-note)."""
    file_path = Path(args.file).expanduser().resolve()
    if not file_path.exists():
        fail(f"File not found: {file_path}")
    # Same default as add-file: standalone images are not OCR'd unless asked.
    if file_path.suffix.lower() in IMAGE_EXTENSIONS and not args.ocr:
        text = ""
    else:
        text = extract_from_file(file_path, config, allow_image_only=True)
    output({"ok": True, "action": "extract", "file": str(file_path), "format": file_path.suffix.lower(),
            "text": text, "chars": len(text), "image_only": not text.strip()})


def cmd_search(args, config):
    """Semantic search across wiki."""
    conn = init_db()

    # Check if DB has any data
    row = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()
    if row[0] == 0:
        output({"ok": True, "results": [], "message": "Wiki is empty. Add documents first."})
        conn.close()
        return

    # Resolve model
    model_name, _ = resolve_embedding_model(config)

    # Embed query
    query_emb = embed_query(args.query, model_name)

    # Determine filters
    cat_type = None
    cat_name = None
    if args.company:
        cat_type = "company"
        cat_name = resolve_category_name(args.company, "company", config)
    elif args.sector:
        cat_type = "sector"
        cat_name = resolve_category_name(args.sector, "sector", config)

    tag_filter = getattr(args, 'tag', None)
    dense_only = getattr(args, 'dense_only', False)
    no_decay = getattr(args, 'no_decay', False)

    filters = build_doc_filters(cat_type=cat_type, cat_name=cat_name,
                                date_from=args.date_from, date_to=args.date_to,
                                tag=tag_filter)
    results = hybrid_search(conn, args.query, query_emb, config,
                            top_k=args.top_k or 10, filters=filters,
                            dense_only=dense_only, no_decay=no_decay)
    conn.close()

    result_data = {
        "ok": True,
        "query": args.query,
        "mode": "dense-only" if dense_only else "hybrid",
        "recency_decay": not no_decay,
        "results": results,
        "count": len(results)
    }
    if tag_filter:
        result_data["tag_filter"] = tag_filter
    output(result_data)


def cmd_list(args, config):
    """List companies, sectors, or documents within one."""
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    COMPANIES_DIR.mkdir(parents=True, exist_ok=True)
    SECTORS_DIR.mkdir(parents=True, exist_ok=True)

    if args.company:
        target_dir = COMPANIES_DIR / resolve_category_name(args.company, "company", config)
        if not target_dir.exists():
            fail(f"Company '{args.company}' not found in wiki")
        files = sorted([f.name for f in target_dir.iterdir()
                        if f.is_file() and f.name != "index.md" and f.suffix == ".md"],
                       reverse=True)
        output({"ok": True, "category": "company", "name": args.company, "documents": files})

    elif args.sector:
        target_dir = SECTORS_DIR / resolve_category_name(args.sector, "sector", config)
        if not target_dir.exists():
            fail(f"Sector '{args.sector}' not found in wiki")
        files = sorted([f.name for f in target_dir.iterdir()
                        if f.is_file() and f.name != "index.md" and f.suffix == ".md"],
                       reverse=True)
        output({"ok": True, "category": "sector", "name": args.sector, "documents": files})

    else:
        # List all
        companies = sorted([d.name for d in COMPANIES_DIR.iterdir() if d.is_dir()]) \
            if COMPANIES_DIR.exists() else []
        sectors = sorted([d.name for d in SECTORS_DIR.iterdir() if d.is_dir()]) \
            if SECTORS_DIR.exists() else []

        # Count docs per category
        company_details = []
        for c in companies:
            d = COMPANIES_DIR / c
            count = len([f for f in d.iterdir() if f.is_file() and f.name != "index.md" and f.suffix == ".md"])
            company_details.append({"name": c, "documents": count})

        sector_details = []
        for s in sectors:
            d = SECTORS_DIR / s
            count = len([f for f in d.iterdir() if f.is_file() and f.name != "index.md" and f.suffix == ".md"])
            sector_details.append({"name": s, "documents": count})

        output({
            "ok": True,
            "companies": company_details,
            "sectors": sector_details,
            "total_companies": len(companies),
            "total_sectors": len(sectors)
        })


def cmd_read(args, config):
    """Read a specific document."""
    if args.company:
        target_dir = COMPANIES_DIR / resolve_category_name(args.company, "company", config)
    elif args.sector:
        target_dir = SECTORS_DIR / resolve_category_name(args.sector, "sector", config)
    else:
        fail("Specify --company or --sector")
        return

    filepath = target_dir / args.file
    if not filepath.exists():
        fail(f"File not found: {filepath}")

    content = filepath.read_text(encoding="utf-8")
    output({
        "ok": True,
        "filename": args.file,
        "content": content
    })


def cmd_links(args, config):
    """Show bidirectional cross-references for a company or sector."""
    cat_type = "company" if args.company else "sector"
    raw_name = args.company or args.sector
    cat_name = resolve_category_name(raw_name, cat_type, config)

    conn = init_db()

    # --- Forward links: what does this entity's docs reference? ---
    rows = conn.execute(
        "SELECT id, date, content_type, filename, related FROM documents WHERE category_type=? AND category_name=? AND related IS NOT NULL",
        (cat_type, cat_name)
    ).fetchall()

    forward = {}  # target_slug -> [{"date", "content_type", "filename"}]
    for doc_id, date, ctype, fname, related_str in rows:
        try:
            related_list = json.loads(related_str)
        except (json.JSONDecodeError, TypeError):
            continue
        for ref in related_list:
            if ref == cat_name:
                continue  # skip self-references
            forward.setdefault(ref, []).append({
                "date": date,
                "content_type": ctype,
                "filename": fname
            })

    # --- Backward links: which other docs reference this entity? ---
    pattern = f'%"{cat_name}"%'
    back_rows = conn.execute(
        """SELECT category_type, category_name, date, content_type, filename
           FROM documents
           WHERE related LIKE ? AND NOT (category_type=? AND category_name=?)
           ORDER BY date DESC""",
        (pattern, cat_type, cat_name)
    ).fetchall()

    backward = {}  # source_name -> [{"date", "content_type", "filename"}]
    for r_type, r_name, r_date, r_ctype, r_fname in back_rows:
        backward.setdefault(r_name, []).append({
            "date": r_date,
            "content_type": r_ctype,
            "filename": r_fname
        })

    conn.close()

    output({
        "ok": True,
        "entity": cat_name,
        "entity_type": cat_type,
        "forward_references": forward,
        "forward_count": sum(len(v) for v in forward.values()),
        "backward_references": backward,
        "backward_count": sum(len(v) for v in backward.values())
    })


def cmd_topic(args, config):
    """Find all documents tagged with a topic, grouped by company/sector."""
    topic = args.topic
    conn = init_db()

    pattern = f'%"{topic}"%'
    rows = conn.execute(
        """SELECT category_type, category_name, date, content_type, filename, filepath, tags
           FROM documents WHERE tags LIKE ? ORDER BY category_name, date DESC""",
        (pattern,)
    ).fetchall()
    conn.close()

    # Group by category
    grouped = {}
    for ctype, cname, date, content_type, fname, fpath, tags_str in rows:
        key = f"{ctype}:{cname}"
        if key not in grouped:
            grouped[key] = {
                "category_type": ctype,
                "category_name": cname,
                "documents": []
            }
        tags = []
        if tags_str:
            try:
                tags = json.loads(tags_str)
            except json.JSONDecodeError:
                pass
        grouped[key]["documents"].append({
            "date": date,
            "content_type": content_type,
            "filename": fname,
            "filepath": fpath,
            "tags": tags
        })

    output({
        "ok": True,
        "topic": topic,
        "groups": list(grouped.values()),
        "total_documents": len(rows)
    })


def cmd_reindex(args, config):
    """Batch process documents: auto-tag and/or re-embed.

    --tags-only:  skip re-embedding, only update tags/related columns and YAML frontmatter.
    --embed-only: skip auto-tagging (no LLM calls), leave files untouched,
                  re-chunk + re-embed only. Use after chunking/context changes.
    --company/--sector: limit to one category (targeted re-embed).
    """
    tags_only = getattr(args, 'tags_only', False)
    embed_only = getattr(args, 'embed_only', False)
    if tags_only and embed_only:
        fail("--tags-only and --embed-only are mutually exclusive")

    # Import auto-tagger
    try:
        from auto_tagger import (auto_tag, build_frontmatter, strip_frontmatter,
                                 extract_source_fields, tags_to_json, related_to_json)
    except ImportError:
        fail("auto_tagger module not found", "Ensure scripts/auto_tagger.py exists")

    where_sql = ""
    where_params = []
    if getattr(args, 'company', None):
        where_sql = "WHERE category_type='company' AND category_name=?"
        where_params = [resolve_category_name(args.company, "company", config)]
    elif getattr(args, 'sector', None):
        where_sql = "WHERE category_type='sector' AND category_name=?"
        where_params = [resolve_category_name(args.sector, "sector", config)]

    conn = init_db()
    docs = conn.execute(
        f"""SELECT id, category_type, category_name, content_type, date, filename,
                   filepath, tags, related
            FROM documents {where_sql} ORDER BY id""",
        where_params
    ).fetchall()

    total = len(docs)
    if total == 0:
        output({"ok": True, "message": "No documents to reindex."})
        conn.close()
        return

    # Resolve embedding model (only if not tags-only)
    model_name = None
    if not tags_only:
        model_name, _ = resolve_embedding_model(config)

    chunk_size = config["embedding"].get("chunk_size", 1500)
    chunk_overlap = config["embedding"].get("chunk_overlap", 200)

    success = 0
    errors = 0

    for idx, (doc_id, cat_type, cat_name, content_type, date_str, filename,
              filepath, old_tags, old_related) in enumerate(docs, 1):
        full_path = WORKSPACE / filepath
        print(f"Processing {idx}/{total}: {filepath}", file=sys.stderr)

        if not full_path.exists():
            print(f"  SKIP: file not found on disk", file=sys.stderr)
            errors += 1
            continue

        try:
            raw_text = full_path.read_text(encoding="utf-8")

            # Strip existing frontmatter to get pure content
            content = strip_frontmatter(raw_text)

            if embed_only:
                # Re-chunk + re-embed with existing tags; file stays untouched
                chunks = chunk_text(content, chunk_size, chunk_overlap)
                chunks = contextualize_chunks(chunks, cat_type, cat_name,
                                              content_type, date_str, filename)
                embeddings = embed_texts(chunks, model_name)
                store_document(conn, cat_type, cat_name, content_type, date_str,
                               filename, filepath, chunks, embeddings,
                               tags_json=old_tags, related_json=old_related)
                print(f"  OK: {len(chunks)} chunks re-embedded", file=sys.stderr)
                success += 1
                continue

            # Auto-tag
            tag_result = auto_tag(content, config, get_client_fn=get_genai_client, verbose=VERBOSE,
                                  context=tagger_context(cat_type, cat_name, content_type,
                                                         conn=conn, exclude_doc_id=doc_id))
            tags_json_str = tags_to_json(tag_result)
            related_json_str = related_to_json(tag_result)

            # Update file with new YAML frontmatter (preserve source fields)
            extras = extract_source_fields(raw_text)
            new_frontmatter = ""
            if any(tag_result.get(k) for k in ("tags", "related_companies", "related_sectors")) or extras:
                new_frontmatter = build_frontmatter(tag_result, extras)
            full_path.write_text(new_frontmatter + content, encoding="utf-8")

            if tags_only:
                # Only update DB tags/related
                update_document_tags(conn, doc_id, tags_json_str, related_json_str)
            else:
                # Re-embed + update tags
                chunks = chunk_text(content, chunk_size, chunk_overlap)
                chunks = contextualize_chunks(chunks, cat_type, cat_name,
                                              content_type, date_str, filename)
                embeddings = embed_texts(chunks, model_name)
                store_document(conn, cat_type, cat_name, content_type, date_str,
                               filename, filepath, chunks, embeddings,
                               tags_json=tags_json_str, related_json=related_json_str)

            tags_preview = tag_result.get("tags", [])[:3]
            print(f"  OK: tags={tags_preview}", file=sys.stderr)
            success += 1

        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            errors += 1

    conn.close()

    # Regenerate all index files
    print(f"\nRegenerating index files...", file=sys.stderr)
    for base_dir, ct in [(COMPANIES_DIR, "company"), (SECTORS_DIR, "sector")]:
        if base_dir.exists():
            for subdir in base_dir.iterdir():
                if subdir.is_dir():
                    update_index(subdir, ct, subdir.name)

    output({
        "ok": True,
        "action": "reindex",
        "tags_only": tags_only,
        "embed_only": embed_only,
        "total": total,
        "success": success,
        "errors": errors
    })




def cmd_index_missing(args, config):
    """Incrementally index on-disk documents that are missing from the DB.

    Cheaper than a full reindex: only untracked files get tagged/embedded.
    Filename pattern YYYY-MM-DD-<type>[-N].md supplies date and content type;
    files that do not match fall back to file mtime date and stem as type.
    """
    from auto_tagger import (auto_tag, build_frontmatter, strip_frontmatter,
                             extract_source_fields, tags_to_json, related_to_json)
    fname_re = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+?)(?:-\d+)?\.md$")
    dry_run = getattr(args, "dry_run", False)

    conn = init_db()
    known = {row[0] for row in conn.execute("SELECT filepath FROM documents")}
    missing = []
    for base_dir, ct in [(COMPANIES_DIR, "company"), (SECTORS_DIR, "sector")]:
        if not base_dir.exists():
            continue
        for subdir in sorted(base_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for f in sorted(subdir.glob("*.md")):
                if f.name == "index.md":
                    continue
                if str(f.relative_to(WORKSPACE)) not in known:
                    missing.append((f, ct, subdir.name))

    if dry_run or not missing:
        output({"ok": True, "action": "index-missing", "dry_run": dry_run,
                "missing": [str(f.relative_to(WORKSPACE)) for f, _, _ in missing]})
        conn.close()
        return

    model_name, _ = resolve_embedding_model(config)
    chunk_size = config["embedding"].get("chunk_size", 1500)
    chunk_overlap = config["embedding"].get("chunk_overlap", 200)
    tagger_enabled = config.get("tagger", {}).get("enabled", False)

    success = errors = 0
    touched_dirs = set()
    for i, (f, ct, cat_name) in enumerate(missing, 1):
        rel_fp = str(f.relative_to(WORKSPACE))
        print(f"Indexing {i}/{len(missing)}: {rel_fp}", file=sys.stderr)
        m = fname_re.match(f.name)
        if m:
            date_str, ctype = m.group(1), m.group(2)
        else:
            date_str = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
            ctype = f.stem
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            content = strip_frontmatter(raw)
            extras = extract_source_fields(raw)
            tags_json = related_json = None
            if tagger_enabled:
                tr = auto_tag(content, config, get_client_fn=get_genai_client, verbose=VERBOSE,
                              context=tagger_context(ct, cat_name, ctype, conn=conn))
                tags_json = tags_to_json(tr)
                related_json = related_to_json(tr)
                if any(tr.get(k) for k in ("tags", "related_companies", "related_sectors")) or extras:
                    f.write_text(build_frontmatter(tr, extras) + content, encoding="utf-8")
            chunks = chunk_text(content, chunk_size, chunk_overlap)
            chunks = contextualize_chunks(chunks, ct, cat_name, ctype, date_str, f.name)
            embeddings = embed_texts(chunks, model_name)
            store_document(conn, ct, cat_name, ctype, date_str, f.name, rel_fp,
                           chunks, embeddings, tags_json=tags_json, related_json=related_json)
            touched_dirs.add((f.parent, ct, cat_name))
            success += 1
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            errors += 1
    conn.close()
    for d, ct, name in touched_dirs:
        update_index(d, ct, name)
    output({"ok": True, "action": "index-missing", "total": len(missing),
            "success": success, "errors": errors})


def cmd_graph(args, config):
    """Generate a cross-reference relationship graph."""
    conn = init_db()

    target_company = getattr(args, 'company', None)
    if target_company:
        target_company = resolve_category_name(target_company, "company", config)
    output_format = getattr(args, 'format', 'text') or 'text'

    # Collect all docs with related data
    rows = conn.execute(
        "SELECT category_type, category_name, related FROM documents WHERE related IS NOT NULL"
    ).fetchall()
    conn.close()

    # Build adjacency: source -> target -> count
    edges = {}  # (source, target) -> weight
    all_nodes = set()

    for cat_type, cat_name, related_str in rows:
        try:
            related_list = json.loads(related_str)
        except (json.JSONDecodeError, TypeError):
            continue
        all_nodes.add(cat_name)
        for ref in related_list:
            if ref == cat_name:
                continue
            all_nodes.add(ref)
            key = tuple(sorted([cat_name, ref]))
            edges[key] = edges.get(key, 0) + 1

    # If --company specified, filter to neighborhood
    if target_company:
        neighbors = set()
        filtered_edges = {}
        for (a, b), w in edges.items():
            if a == target_company or b == target_company:
                neighbors.add(a)
                neighbors.add(b)
                filtered_edges[(a, b)] = w
        all_nodes = neighbors
        edges = filtered_edges

    # Build output
    nodes = sorted(all_nodes)
    edges_list = [{"source": a, "target": b, "weight": w}
                  for (a, b), w in sorted(edges.items(), key=lambda x: -x[1])]

    if output_format == "json":
        output({
            "ok": True,
            "nodes": nodes,
            "edges": edges_list,
            "node_count": len(nodes),
            "edge_count": len(edges_list)
        })
    else:
        # Text format
        lines = [f"Knowledge Graph — {len(nodes)} nodes, {len(edges_list)} edges\n"]
        if target_company:
            lines.append(f"Neighborhood of: {target_company}\n")
        lines.append("\nTop connections:\n")
        for e in edges_list[:30]:
            lines.append(f"  {e['source']} <-> {e['target']}  (weight: {e['weight']})")
        print("\n".join(lines))


def cmd_self_test(args, config):
    """Run self-test."""
    results = {}

    # 1. Check GEMINI_API_KEY
    api_key = os.environ.get("GEMINI_API_KEY")
    results["gemini_api_key"] = "ok" if api_key else "MISSING"

    # 2. Test embedding
    if api_key:
        try:
            model_name, dims = resolve_embedding_model(config)
            results["embedding_model"] = model_name
            results["embedding_dims"] = dims
            results["embedding"] = "ok"
        except Exception as e:
            results["embedding"] = f"FAIL: {e}"
    else:
        results["embedding"] = "SKIP (no API key)"

    # 2b. Check file extraction deps
    for dep_name, import_name in [("pymupdf", "fitz"), ("python-docx", "docx"), ("Pillow", "PIL")]:
        try:
            __import__(import_name)
            results[dep_name] = "ok"
        except ImportError:
            results[dep_name] = "MISSING"

    # 3. Check wiki directory
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    results["wiki_dir"] = str(WIKI_DIR)
    results["wiki_dir_exists"] = "ok"

    # 4. Check DB
    try:
        conn = init_db()
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
        fts_count = None
        try:
            fts_count = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            results["fts_index"] = "ok"
        except sqlite3.OperationalError:
            results["fts_index"] = "UNAVAILABLE (dense-only search)"
        conn.close()
        results["db"] = "ok"
        results["documents"] = doc_count
        results["indexed_chunks"] = chunk_count
        if fts_count is not None:
            results["fts_chunks"] = fts_count
    except Exception as e:
        results["db"] = f"FAIL: {e}"

    # 5. Tagger backend (static check only; agy failures fall back to the Gemini API)
    tagger = config.get("tagger", {})
    if tagger.get("provider", "agy") == "agy":
        try:
            from auto_tagger import AGY_BIN, AGY_AGENT_SRC, SANDBOX_PROFILE, _check_agent_frontmatter
            missing = [str(p) for p in (AGY_BIN, AGY_AGENT_SRC, SANDBOX_PROFILE) if not p.exists()]
            if not missing:
                _check_agent_frontmatter(AGY_AGENT_SRC.read_text(encoding="utf-8"))
            results["tagger"] = (f"agy:{tagger.get('agy_model', 'gemini-4-hydrogen-low')} "
                                 + (f"MISSING {missing}, will fall back to API" if missing else "ok"))
        except Exception as e:
            results["tagger"] = f"FAIL: {e} (agy calls will fall back to the Gemini API)"
    else:
        results["tagger"] = f"gemini-api:{tagger.get('model')}"

    all_ok = all(v == "ok" for k, v in results.items()
                 if k in ["gemini_api_key", "embedding", "wiki_dir_exists", "db"])
    results["ok"] = all_ok
    output(results)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global JSON_MODE, VERBOSE

    parser = argparse.ArgumentParser(
        description="company-wiki — Investment knowledge base with semantic search"
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--version", action="version", version="company-wiki 1.1.0")

    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Add content to wiki")
    p_add.add_argument("--company", help="Company name")
    p_add.add_argument("--sector", help="Sector name")
    p_add.add_argument("--type", required=True, help="Content type (e.g. 管理层会议纪要)")
    p_add.add_argument("--date", help="Date (YYYY-MM-DD), default today")
    p_add.add_argument("--content", required=True, help="Content text or @filepath")
    p_add.add_argument("--source", help="Source name (e.g. 公众号名/券商/会议名/Flomo)")
    p_add.add_argument("--source-url", dest="source_url", help="Original URL of the content")

    # add-file
    p_addfile = sub.add_parser("add-file", help="Add content from file (image/PDF/Word/text)")
    p_addfile.add_argument("--company", help="Company name")
    p_addfile.add_argument("--sector", help="Sector name")
    p_addfile.add_argument("--type", required=True, help="Content type (e.g. 管理层会议纪要)")
    p_addfile.add_argument("--date", help="Date (YYYY-MM-DD), default today")
    p_addfile.add_argument("--file", required=True, help="Path to file (image/PDF/docx/txt/md)")
    p_addfile.add_argument("--ocr", action="store_true", help="Extract image text before indexing; default embeds the image directly")
    p_addfile.add_argument("--keep-original", dest="keep_original", action="store_true", default=True,
                           help="Save a copy of the original file in originals/ (default)")
    p_addfile.add_argument("--no-keep-original", dest="keep_original", action="store_false",
                           help="Skip the originals/ copy")
    p_addfile.add_argument("--source", help="Source name (e.g. 公众号名/券商/会议名)")
    p_addfile.add_argument("--source-url", dest="source_url", help="Original URL of the content")

    # extract
    p_extract = sub.add_parser("extract", help="Extract text from a file without indexing")
    p_extract.add_argument("--file", required=True, help="Path to file (image/PDF/docx/txt/md)")
    p_extract.add_argument("--ocr", action="store_true", help="OCR images with the vision model")

    # search
    p_search = sub.add_parser("search", help="Semantic search")
    p_search.add_argument("--query", required=True, help="Search query")
    p_search.add_argument("--company", help="Filter by company")
    p_search.add_argument("--sector", help="Filter by sector")
    p_search.add_argument("--date-from", help="Start date (YYYY-MM-DD)")
    p_search.add_argument("--date-to", help="End date (YYYY-MM-DD)")
    p_search.add_argument("--top-k", type=int, default=10, help="Max results")
    p_search.add_argument("--tag", help="Filter by tag (only search docs with this tag)")
    p_search.add_argument("--dense-only", dest="dense_only", action="store_true",
                          help="Skip keyword arm, dense vector search only")
    p_search.add_argument("--no-decay", dest="no_decay", action="store_true",
                          help="Disable recency decay on result scores")

    # list
    p_list = sub.add_parser("list", help="List contents")
    p_list.add_argument("--company", help="List documents for a company")
    p_list.add_argument("--sector", help="List documents for a sector")

    # read
    p_read = sub.add_parser("read", help="Read a document")
    p_read.add_argument("--company", help="Company name")
    p_read.add_argument("--sector", help="Sector name")
    p_read.add_argument("--file", required=True, help="Filename to read")

    # links
    p_links = sub.add_parser("links", help="Show cross-references for a company/sector")
    p_links.add_argument("--company", help="Company name")
    p_links.add_argument("--sector", help="Sector name")

    # topic
    p_topic = sub.add_parser("topic", help="Find all docs tagged with a topic")
    p_topic.add_argument("topic", help="Topic tag to search for")

    # reindex
    p_indexmissing = sub.add_parser("index-missing", help="Index on-disk docs missing from DB (incremental)")
    p_indexmissing.add_argument("--dry-run", action="store_true", help="List missing files only")

    p_reindex = sub.add_parser("reindex", help="Batch re-tag (and optionally re-embed) all documents")
    p_reindex.add_argument("--tags-only", action="store_true",
                           help="Only update tags/related, skip re-embedding")
    p_reindex.add_argument("--embed-only", dest="embed_only", action="store_true",
                           help="Re-chunk + re-embed only, no LLM tagging, files untouched")
    p_reindex.add_argument("--company", help="Limit reindex to one company")
    p_reindex.add_argument("--sector", help="Limit reindex to one sector")

    # graph
    p_graph = sub.add_parser("graph", help="Generate cross-reference relationship graph")
    p_graph.add_argument("--company", help="Show only neighborhood of this company")
    p_graph.add_argument("--format", choices=["json", "text"], default="text",
                         help="Output format (default: text)")

    # import-flomo
    p_flomo = sub.add_parser("import-flomo", help="Import Flomo HTML export into wiki")
    p_flomo.add_argument("--html", type=str, help="Path to Flomo HTML export")
    p_flomo.add_argument("--images-dir", type=str, help="Path to Flomo images directory")
    p_flomo.add_argument("--dry-run", action="store_true", help="Show plan without importing")

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Remove ghost records (DB entries without local files)")
    p_cleanup.add_argument("--dry-run", action="store_true", help="Show what would be removed")

    # self-test
    sub.add_parser("self-test", help="Verify setup")

    args = parser.parse_args()
    JSON_MODE = args.json
    VERBOSE = args.verbose

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    if args.command == "add":
        if not args.company and not args.sector:
            fail("Specify --company or --sector", "e.g. --company futu or --sector china-internet")
        if args.company and args.sector:
            fail("Specify only one of --company or --sector")
        cmd_add(args, config)
    elif args.command == "add-file":
        if not args.company and not args.sector:
            fail("Specify --company or --sector", "e.g. --company futu or --sector china-internet")
        if args.company and args.sector:
            fail("Specify only one of --company or --sector")
        cmd_add_file(args, config)
    elif args.command == "extract":
        cmd_extract(args, config)
    elif args.command == "search":
        cmd_search(args, config)
    elif args.command == "list":
        cmd_list(args, config)
    elif args.command == "read":
        cmd_read(args, config)
    elif args.command == "links":
        if not args.company and not args.sector:
            fail("Specify --company or --sector")
        cmd_links(args, config)
    elif args.command == "topic":
        cmd_topic(args, config)
    elif args.command == "reindex":
        cmd_reindex(args, config)
    elif args.command == "index-missing":
        cmd_index_missing(args, config)
    elif args.command == "graph":
        cmd_graph(args, config)
    elif args.command == "import-flomo":
        from flomo_import import run_import, DEFAULT_HTML, DEFAULT_IMAGES_DIR
        html_path = Path(args.html).expanduser() if args.html else DEFAULT_HTML
        images_dir = Path(args.images_dir).expanduser() if args.images_dir else DEFAULT_IMAGES_DIR
        if not html_path.exists():
            fail(f"HTML file not found: {html_path}")
        result = run_import(html_path, images_dir, dry_run=args.dry_run, verbose=VERBOSE)
        output({"ok": True, **result})
    elif args.command == "cleanup":
        from flomo_import import cleanup_ghosts
        cleaned = cleanup_ghosts(dry_run=args.dry_run)
        output({"ok": True, "ghosts_cleaned": cleaned, "dry_run": args.dry_run})
    elif args.command == "self-test":
        cmd_self_test(args, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
