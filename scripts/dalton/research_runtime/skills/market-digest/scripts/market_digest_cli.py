#!/usr/bin/env python3
"""
market-digest CLI — Sales email digest + semantic search over market memory.

Commands:
  digest   --period AM|PM    Fetch new sales emails, output structured digest
  search   --query "..."     Semantic search over indexed market memory
  index    --file path.md    Manually index a daily log file
  add-note --institution X --title Y (--file F | --content @F)
                             Add a forwarded sell-side note/report to sales notes
  self-test                  Verify connectivity (gws, Gemini API)

Flags:
  --json       JSON output mode
  --verbose    Verbose logging
  --help       Show help
"""

import argparse
import base64
import email.utils
import gc
import json
import mailbox
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

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


def _resolve_config_path() -> Path:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    if raw_ocw and not raw_code and not raw_home:
        return Path(raw_ocw).expanduser().resolve() / "skills" / "market-digest" / "config.json"
    runtime_cfg = DATA_ROOT / "config" / "config.json"
    if runtime_cfg.is_file():
        return runtime_cfg
    legacy_fallback = SKILL_DIR / "config.json"
    if legacy_fallback.is_file():
        return legacy_fallback
    return runtime_cfg


CODE_ROOT, DATA_ROOT = _resolve_research_roots(SKILL_DIR.parent.parent)
WORKSPACE = DATA_ROOT
CONFIG_PATH = _resolve_config_path()
CONFIG_EXAMPLE_PATH = SKILL_DIR / "config.example.json"
DB_PATH = DATA_ROOT / "skills" / "market-digest" / "vectors.db"
MARKET_MEMORY_DIR = DATA_ROOT / "market-memory"
DIGEST_OUTPUT_DIR = DATA_ROOT / "skills" / "market-digest" / "output"  # temp dir for large JSON output

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
        fail(
            "config.json not found",
            "Copy config.example.json to config.json and fill in your values:\n"
            f"  cp {CONFIG_EXAMPLE_PATH} {CONFIG_PATH}"
        )
    fail("No config file found", "Neither config.json nor config.example.json exists")


def log(msg):
    if VERBOSE:
        print(f"[market-digest] {msg}", file=sys.stderr)


def output(data):
    if JSON_MODE:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict):
            if data.get("ok"):
                for k, v in data.items():
                    if k == "ok":
                        continue
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
# Gmail helpers (via gws CLI)
# ---------------------------------------------------------------------------
def gws_run(*args):
    """Run a gws CLI command and return parsed JSON."""
    cmd = ["gws"] + list(args)
    log(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"gws error: {result.stderr.strip() or result.stdout.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def fetch_new_emails(config):
    """Fetch unprocessed sales emails from Gmail."""
    domains = config["email_sources"]["domains"]
    from_query = " OR ".join(domains)
    label_name = config["gmail"]["label_name"]
    max_results = config["gmail"]["max_results"]

    query = f"from:({from_query}) -label:{json.dumps(label_name, ensure_ascii=False)}"
    log(f"Gmail query: {query}")

    messages = []; seen = set(); token = None; pages = 0
    try:
        for _ in range(config["gmail"].get("max_pages", 10)):
            params = {"userId": "me", "q": query, "maxResults": max_results}
            if token: params["pageToken"] = token
            result = gws_run("gmail", "users", "messages", "list", "--params", json.dumps(params))
            pages += 1
            for row in result.get("messages", []):
                if row["id"] not in seen:
                    messages.append(row); seen.add(row["id"])
            token = result.get("nextPageToken")
            if not token: break
    except RuntimeError as e:
        fail(str(e), "Check gws auth status: gws auth status")
    config["_fetch_coverage"] = {"pages": pages, "listed": len(messages), "more_unfetched": bool(token), "failed_reads": 0}
    if not messages:
        return []

    emails = []
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        try:
            msg = gws_run(
                "gmail", "users", "messages", "get",
                "--params", json.dumps({"userId": "me", "id": msg_id, "format": "full"})
            )
            email_data = parse_email(msg)
            if email_data:
                email_data["id"] = msg_id
                email_data["_mime_payload"] = msg
                emails.append(email_data)
        except Exception as e:
            config["_fetch_coverage"]["failed_reads"] += 1
            log(f"Failed to fetch message {msg_id}: {e}")

    return emails


def parse_email(msg):
    """Extract from, subject, date, body from Gmail API message."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

    from_addr = headers.get("from", "")
    subject = headers.get("subject", "")
    date = headers.get("date", "")

    body = extract_body(msg.get("payload", {}))

    return {
        "from": from_addr,
        "subject": subject,
        "date": date,
        "body": body
    }


def extract_body(payload):
    """Extract plaintext body from Gmail payload (recursive). Falls back to HTML → text."""
    plain = _extract_by_mime(payload, "text/plain")
    if plain:
        return plain
    # Fallback: extract from HTML
    html = _extract_by_mime(payload, "text/html")
    if html:
        return _html_to_text(html)
    return ""


def _extract_by_mime(payload, target_mime):
    """Recursively find first part matching target MIME type."""
    mime = payload.get("mimeType", "")
    if mime == target_mime and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_by_mime(part, target_mime)
        if result:
            return result
    return ""


def _html_to_text(html_str):
    """Convert HTML to readable plain text (no external deps)."""
    import html
    # Remove style/script blocks
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_str, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert common block elements to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(p|div|tr|li|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode HTML entities
    text = html.unescape(text)
    # Clean up whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def label_emails(email_ids, label_id):
    """Add label to processed emails."""
    for eid in email_ids:
        try:
            gws_run(
                "gmail", "users", "messages", "modify",
                "--params", json.dumps({"userId": "me", "id": eid}),
                "--json", json.dumps({"addLabelIds": [label_id]})
            )
            log(f"Labeled {eid}")
        except Exception as e:
            log(f"Failed to label {eid}: {e}")


# ---------------------------------------------------------------------------
# Embedding (Gemini)
# ---------------------------------------------------------------------------
_genai_client = None


def get_genai_client():
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        fail("GEMINI_API_KEY not set", "Export GEMINI_API_KEY in your shell profile")

    from google import genai
    _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def embed_texts(texts, config):
    """Embed a list of texts using Gemini embedding API with true batch calls."""
    client = get_genai_client()
    from google.genai import types

    model_name = config["embedding"]["model"]
    log(f"Embedding {len(texts)} chunks with {model_name}")

    embeddings = []
    # Process in batches of 100 — each batch is ONE API call (not N calls)
    BATCH_SIZE = 100
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i+BATCH_SIZE]
        import time
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
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait = 2 ** attempt * 5
                    log(f"Rate limited, waiting {wait}s (attempt {attempt+1}/5)")
                    time.sleep(wait)
                else:
                    raise
        else:
            raise RuntimeError(f"embedding rate-limited 5 times for texts {i}..{i + len(batch) - 1}")

    return embeddings


def embed_query(text, config):
    """Embed a single query text."""
    client = get_genai_client()
    model_name = config["embedding"]["model"]

    result = client.models.embed_content(
        model=api_model(model_name),
        contents=text,
        config={"task_type": "RETRIEVAL_QUERY"}
    )
    return result.embeddings[0].values


# ---------------------------------------------------------------------------
# Vector DB (SQLite)
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    # Layer 2: digest summaries (coarse-grained)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            period TEXT,
            section_id TEXT,
            section_label TEXT,
            content TEXT NOT NULL,
            source_emails TEXT,
            embedding BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_date ON chunks(date)")
    # Layer 1: raw email chunks (fine-grained, full content)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            email_id TEXT,
            message_id TEXT,
            sender TEXT,
            subject TEXT,
            chunk_index INTEGER,
            content TEXT NOT NULL,
            embedding BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_chunks_date ON email_chunks(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_chunks_email_id ON email_chunks(email_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_chunks_message_id ON email_chunks(message_id)")
    # Add message_id column if upgrading from older schema
    try:
        conn.execute("ALTER TABLE email_chunks ADD COLUMN message_id TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn


def store_chunks(conn, chunks, embeddings):
    """Store chunks with their embeddings."""
    import numpy as np

    for chunk, emb in zip(chunks, embeddings):
        emb_blob = np.array(emb, dtype=np.float32).tobytes()
        conn.execute(
            """INSERT INTO chunks (date, period, section_id, section_label, content, source_emails, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                chunk["date"],
                chunk.get("period"),
                chunk.get("section_id"),
                chunk.get("section_label"),
                chunk["content"],
                json.dumps(chunk.get("sources", []), ensure_ascii=False),
                emb_blob
            )
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Email chunking (Layer 1 — raw full-text)
# ---------------------------------------------------------------------------
def chunk_email(email, date_str, max_chunk_chars=1500):
    """Split a single email body into overlapping chunks for embedding."""
    body = email.get("body", "").strip()
    if not body:
        return []

    sender = email.get("from", "")
    subject = email.get("subject", "")
    email_id = email.get("id", "")

    # Prefix each chunk with sender/subject for context
    prefix = f"From: {sender}\nSubject: {subject}\n\n"

    # Split by double newline (paragraph boundaries), handle \r\n too
    paragraphs = re.split(r"[\r\n]{2,}", body)

    chunks = []
    current_chunk = ""
    chunk_idx = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current_chunk) + len(para) + 2 > max_chunk_chars and current_chunk:
            # Save current chunk
            chunks.append({
                "date": date_str,
                "email_id": email_id,
                "sender": sender,
                "subject": subject,
                "chunk_index": chunk_idx,
                "content": prefix + current_chunk
            })
            chunk_idx += 1
            current_chunk = para
        else:
            current_chunk = current_chunk + "\n\n" + para if current_chunk else para

    # Don't forget the last chunk
    if current_chunk.strip():
        chunks.append({
            "date": date_str,
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "chunk_index": chunk_idx,
            "content": prefix + current_chunk
        })

    return chunks


def store_email_chunks(conn, chunks, embeddings):
    """Store raw email chunks with embeddings into email_chunks table."""
    import numpy as np

    for chunk, emb in zip(chunks, embeddings):
        emb_blob = np.array(emb, dtype=np.float32).tobytes()
        conn.execute(
            """INSERT INTO email_chunks (date, email_id, message_id, sender, subject, chunk_index, content, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chunk["date"],
                chunk.get("email_id"),
                chunk.get("message_id"),
                chunk.get("sender"),
                chunk.get("subject"),
                chunk.get("chunk_index", 0),
                chunk["content"],
                emb_blob
            )
        )
    conn.commit()


def index_raw_emails(emails, date_str, config):
    """Index raw email bodies into the vector DB (Layer 1)."""
    all_chunks = []
    for email in emails:
        chunks = chunk_email(email, date_str)
        all_chunks.extend(chunks)

    if not all_chunks:
        log("No email chunks to index")
        return 0

    log(f"Indexing {len(all_chunks)} raw email chunks from {len(emails)} emails")

    texts_to_embed = [c["content"] for c in all_chunks]
    try:
        embeddings = embed_texts(texts_to_embed, config)
        conn = init_db()
        store_email_chunks(conn, all_chunks, embeddings)
        conn.close()
    except Exception as e:
        print(f"WARNING: Failed to index raw emails (Layer 1): {e}", file=sys.stderr)

    return len(all_chunks)


# ---------------------------------------------------------------------------
# Digest: chunking for indexing (Layer 2 — summaries)
# ---------------------------------------------------------------------------
def chunk_digest(digest_text, date_str, period, config):
    """Split a digest into section-level chunks for embedding."""
    sections = config.get("sections", [])
    chunks = []

    # Split by ## headers
    section_pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    splits = section_pattern.split(digest_text)

    # splits = [preamble, header1, body1, header2, body2, ...]
    if len(splits) < 3:
        # No sections found — index as one chunk
        chunks.append({
            "date": date_str,
            "period": period,
            "section_id": "full",
            "section_label": "Full Digest",
            "content": digest_text.strip()[:2000],
            "sources": []
        })
        return chunks

    for i in range(1, len(splits), 2):
        header = splits[i].strip()
        body = splits[i + 1].strip() if i + 1 < len(splits) else ""

        if not body or len(body) < 20:
            continue

        # Try to match to a known section
        section_id = "other"
        section_label = header
        for sec in sections:
            if sec["label"] in header or header in sec["label"]:
                section_id = sec["id"]
                section_label = sec["label"]
                break

        # Truncate very long sections
        content = f"## {header}\n{body}"
        if len(content) > 3000:
            content = content[:3000] + "..."

        chunks.append({
            "date": date_str,
            "period": period,
            "section_id": section_id,
            "section_label": section_label,
            "content": content,
            "sources": []
        })

    return chunks


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def image_helpers():
    if str(CODE_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
    import research_images
    return research_images


def process_image_queue():
    helper = image_helpers()
    sales = helper.drain("sales-notes")
    funda = helper.drain("funda-ai")
    return {"ok": sales.get("ok", False) and funda.get("ok", False),
            "status": "ok" if all(x.get("status") == "ok" for x in (sales, funda)) else "partial",
            "sources": {"sales-notes": sales, "funda-ai": funda}}


def ingest_funda_emails(emails, config):
    """Archive before labelling; provider failures leave a retryable local backlog."""
    if str(CODE_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
    from funda_ai import Store, is_funda, MODEL, DIMS
    store = Store(DATA_ROOT)
    selected = [e for e in emails if is_funda(e)]
    for mail in selected:
        captured = store.capture(mail, received_at=mail.get("_mime_payload", {}).get("internalDate"))
        mail.update(source="funda-ai", document_id=captured["document_id"],
                    image_job=captured.get("image_job"), original_path=captured["original_path"],
                    research_eligible=not bool(captured.get("skipped")))
    if not selected and not store.db.exists():
        return {"ok": True, "status": "empty", "received": 0}
    try:
        result = store.index_pending(lambda texts: embed_texts(texts, {"embedding": {"model": MODEL, "dimensions": DIMS}}), max_docs=20)
        result["status"] = "partial" if result.get("pending_documents") or result.get("status") == "pending" else "ok"
        return {**result, "received": len(selected)}
    except (Exception, SystemExit) as exc:
        return {"ok": False, "status": "partial", "received": len(selected), "error": type(exc).__name__,
                "hint": "FUNDA AI 原件和正文已保存；embedding 待下次 digest 或 funda_ai.sh index 重试。"}


def queue_email_images(email, date_str):
    if not email.get("_mime_payload"):
        return None
    helper = image_helpers()
    from research_image_ingest import gmail_items
    if not gmail_items(email["_mime_payload"]):
        return {"status": "no_images"}
    return helper.enqueue("sales-notes", email["id"], "gmail", email["_mime_payload"], {
        "title": email["subject"], "date": date_str, "original_source": email["from"],
        "date_basis": "继承邮件入库批次日期", "gmail_message_id": email["id"]}, root=DATA_ROOT)


def cmd_digest(args, config):
    """Fetch new emails and output structured digest data."""
    period = args.period.upper()
    if period not in ("AM", "PM"):
        fail("Period must be AM or PM")

    hkt = timezone(timedelta(hours=8))
    now = datetime.now(hkt)
    date_str = now.strftime("%Y-%m-%d")

    log(f"Running digest for {date_str} {period}")

    # Recovery path: a previous run may have fetched (and labelled) emails but
    # died before writing the digest. Those emails will never be returned by
    # Gmail again, so the only copy lives in this file. Load it and merge.
    DIGEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = DIGEST_OUTPUT_DIR / f"digest_{date_str}_{period}.json"
    prior_emails = []
    prior_chunks = 0
    if out_file.exists():
        try:
            with open(out_file) as f:
                prior = json.load(f)
            prior_emails = prior.get("emails", []) or []
            prior_chunks = prior.get("raw_chunks_indexed", 0) or 0
            log(f"Found existing {out_file.name} with {len(prior_emails)} email(s); will merge")
        except (json.JSONDecodeError, OSError) as e:
            log(f"Existing {out_file.name} unreadable ({e}); treating as empty")

    # Fetch emails
    emails = fetch_new_emails(config)
    funda_ingestion = ingest_funda_emails(emails, config)

    if not emails:
        images = process_image_queue()
        if prior_emails:
            # Nothing new, but an earlier run already consumed emails for this
            # slot. Hand them back so the caller can finish the digest.
            output({
                "ok": True,
                "status": "reused_existing",
                "date": date_str,
                "period": period,
                "count": len(prior_emails),
                "priority_count": sum(1 for e in prior_emails if e.get("is_priority")),
                "raw_chunks_indexed": prior_chunks,
                "image_ingestion": images,
                "funda_ingestion": funda_ingestion,
                "mail_fetch": config.get("_fetch_coverage"),
                "output_file": str(out_file),
                "senders": [e.get("from", "") for e in prior_emails]
            })
            return
        output({"ok": True, "status": "no_new_emails", "date": date_str, "period": period, "count": 0, "image_ingestion": images, "funda_ingestion": funda_ingestion, "mail_fetch": config.get("_fetch_coverage")})
        return

    # Mark priority senders
    priority_names = [n.lower() for n in config["email_sources"]["priority_senders"]]
    for email in emails:
        from_lower = email["from"].lower()
        email["is_priority"] = any(name in from_lower for name in priority_names)
        # Durable capture precedes Gmail labelling; retries never need to unlabel mail.
        if email.get("source") != "funda-ai":
            email["source"] = "sales-notes"
            email["image_job"] = queue_email_images(email, date_str)

    # Layer 1: Index raw email full-text immediately
    raw_chunks_count = index_raw_emails([e for e in emails if e.get("source") != "funda-ai"], date_str, config)
    log(f"Indexed {raw_chunks_count} raw email chunks (Layer 1)")

    # Output email data for the cron agent to analyze
    email_summaries = []
    for e in emails:
        email_summaries.append({
            "id": e["id"],
            "from": e["from"],
            "subject": e["subject"] if e.get("research_eligible", True) else "FUNDA AI 非研究邮件（已排除）",
            "date": e["date"],
            "is_priority": e["is_priority"],
            "body_length": len(e.get("body", "")),
            "image_job": e.get("image_job"),
            "source": e.get("source", "sales-notes"),
            "document_id": e.get("document_id"),
            "original_path": e.get("original_path"),
            "research_eligible": e.get("research_eligible", True),
            "body": (e.get("body", "") if e.get("source") == "funda-ai" else e.get("body", "")[:50000]) if e.get("research_eligible", True) else ""
        })

    # Merge with anything a previous run already consumed for this slot, oldest
    # first, de-duplicating by Gmail message id. Never drop prior emails: Gmail
    # will not return them a second time.
    seen = set()
    merged_emails = []
    for e in prior_emails + email_summaries:
        eid = e.get("id")
        if eid in seen:
            continue
        seen.add(eid)
        merged_emails.append(e)
    total_chunks = prior_chunks + raw_chunks_count

    result_data = {
        "ok": True,
        "status": "emails_fetched",
        "date": date_str,
        "period": period,
        "count": len(merged_emails),
        "priority_count": sum(1 for e in merged_emails if e.get("is_priority")),
        "raw_chunks_indexed": total_chunks,
        "image_ingestion": {"status": "pending"},
        "funda_ingestion": funda_ingestion,
        "mail_fetch": config.get("_fetch_coverage"),
        "emails": merged_emails
    }

    # Write full JSON to file to avoid stdout buffer truncation (large outputs
    # can exceed the exec tool's poll buffer and lose the beginning of the data).
    image_helpers().atomic_json(out_file, result_data)

    # Durable digest + FUNDA archive are committed before the existing Gmail label.
    label_emails([e["id"] for e in emails], config["gmail"]["label_id"])

    # Preserve the digest before bounded image work; an interrupted image API
    # must not lose emails already labelled by the existing workflow.
    images = process_image_queue()
    result_data["image_ingestion"] = images
    image_helpers().atomic_json(out_file, result_data)

    # Print a compact summary to stdout (small enough to never truncate)
    output({
        "ok": True,
        "status": "emails_fetched",
        "date": date_str,
        "period": period,
        "count": len(merged_emails),
        "new_count": len(email_summaries),
        "carried_over_count": len(merged_emails) - len(email_summaries),
        "priority_count": sum(1 for e in merged_emails if e.get("is_priority")),
        "raw_chunks_indexed": total_chunks,
        "image_ingestion": images,
        "funda_ingestion": funda_ingestion,
        "mail_fetch": config.get("_fetch_coverage"),
        "output_file": str(out_file),
        "senders": [e.get("from", "") for e in merged_emails]
    })


def cmd_search(args, config):
    """Deprecated command spelling; execute the canonical document search."""
    if not args.query:
        fail("--query is required")
    limit = args.top_k if args.top_k is not None else 10
    if not 1 <= limit <= 100:
        fail("top-k 须在 1–100", "使用新版 data_gateway.py fetch all --limit N。")
    try:
        for value in (args.date_from, args.date_to):
            if value and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
                raise ValueError("日期须为 YYYY-MM-DD")
        if args.date_from and args.date_to and args.date_from > args.date_to:
            raise ValueError("起止日期倒置")
        if str(CODE_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
        from research_library import Library
        result = Library(code_root=CODE_ROOT, data_root=DATA_ROOT).run("all", "search", query=args.query,
            start=args.date_from, end=args.date_to, limit=limit,
            params={"sources": ["sales-notes", "tmtb"], "layer": "all", "media": "text"})
    except (ValueError, OSError) as exc:
        fail(str(exc), "改用新版只读入口；检查来源库与日期参数，不要为检索运行 digest。")
    result["migration"] = {"deprecated_command": "market-digest search", "engine": "research_library",
        "result_format": "document_v1", "replacement": "data_gateway.py fetch all",
        "note": "旧名称只转发新版；结果用 document_id/snippet，正文通过 read 读取。"}
    output(result)
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_index(args, config):
    """Index a digest text into the vector DB."""
    # Can be called with --file (path to .md) or --text (raw text) + --date + --period
    if args.file:
        fpath = Path(args.file)
        if not fpath.exists():
            fail(f"File not found: {fpath}")
        text = fpath.read_text(encoding="utf-8")
        # Infer date from filename (YYYY-MM-DD.md)
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", fpath.name)
        date_str = date_match.group(1) if date_match else args.date or "unknown"
        period = args.period or "FULL"
    elif args.text:
        text = args.text
        date_str = args.date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        period = args.period or "FULL"
    else:
        fail("Provide --file or --text to index")

    log(f"Indexing {len(text)} chars for {date_str} {period}")

    # Chunk
    chunks = chunk_digest(text, date_str, period, config)
    if not chunks:
        output({"ok": True, "status": "no_chunks", "message": "No indexable sections found"})
        return

    # Embed
    texts_to_embed = [c["content"] for c in chunks]
    embeddings = embed_texts(texts_to_embed, config)

    # Store
    conn = init_db()
    store_chunks(conn, chunks, embeddings)
    conn.close()

    output({
        "ok": True,
        "status": "indexed",
        "date": date_str,
        "period": period,
        "chunks_indexed": len(chunks),
        "sections": [c["section_label"] for c in chunks]
    })


NOTE_TEXT_EXTENSIONS = {".md", ".txt", ".markdown"}
NOTE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
NOTE_EXTRACT_EXTENSIONS = {".pdf", ".docx"} | NOTE_IMAGE_EXTENSIONS


def notes_store():
    if str(CODE_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
    import sales_notes_store
    return sales_notes_store


def chunk_note(body, prefix, max_chunk_chars=1500):
    """Paragraph-packed chunks like chunk_email; overlong paragraphs (PDF text) are cut."""
    pieces = []
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
    chunks, current = [], ""
    for para in pieces:
        if current and len(current) + len(para) + 2 > max_chunk_chars:
            chunks.append(prefix + current)
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(prefix + current)
    return chunks


def extract_note_file(path, ocr=False):
    """PDF/DOCX/image text via company-wiki's extractor (its venv has pymupdf/python-docx)."""
    cmd = ["bash", str(CODE_ROOT / "skills/company-wiki/run.sh"), "--json", "extract", "--file", str(path)]
    if ocr:
        cmd.append("--ocr")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        data = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        fail(f"正文提取失败: {type(e).__name__}",
             "先用 MarkItDown 或读图把正文转成 .md，再用 --content @文件 --file 原件 重试")
    if not data.get("ok"):
        fail(f"正文提取失败: {data.get('error')}", data.get("hint"))
    return data


def cmd_add_note(args, config):
    """Add a forwarded sell-side note/report to the sales-notes library (email_chunks)."""
    store = notes_store()
    for name in ("date", "doc_date"):
        value = getattr(args, name)
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            fail(f"--{name.replace('_', '-')} 需要 YYYY-MM-DD", f"收到: {value}")
    received = args.date or datetime.now().strftime("%Y-%m-%d")
    src = Path(args.file).expanduser().resolve() if args.file else None
    if src and not src.is_file():
        fail(f"文件不存在: {src}")
    if not args.content and not src:
        fail("需要 --content 或 --file", "例: --file 报告.pdf，或 --content @temp/note.md")

    image_only = False
    if args.content:
        if args.content.startswith("@"):
            cpath = Path(args.content[1:]).expanduser()
            if not cpath.is_file():
                fail(f"正文文件不存在: {cpath}")
            body = cpath.read_text(encoding="utf-8")
        else:
            body = args.content
    elif src.suffix.lower() in NOTE_TEXT_EXTENSIONS:
        body = src.read_text(encoding="utf-8")
    elif src.suffix.lower() in NOTE_EXTRACT_EXTENSIONS:
        body = extract_note_file(src, args.ocr).get("text", "")
    else:
        fail(f"不支持的文件格式: {src.suffix}", "支持 .pdf .docx .md .txt 和常见图片")
    if not body.strip():
        if src and src.suffix.lower() in NOTE_EXTRACT_EXTENSIONS:
            image_only = True
            body = (f"图片资料：{args.institution} / {args.title} / {src.name}。\n"
                    "原图单独建立图片向量；此处未预先识别图中文字。请读图后引用内容。")
        else:
            fail("正文为空")

    doc_key = store.content_key(src.read_bytes() if image_only else body)
    sender = args.institution + (f" · {args.analyst}" if args.analyst else "")
    tickers = [t.strip() for t in (args.tickers or "").split(",") if t.strip()]
    tag = " | ".join(x for x in (args.kind, args.doc_date and f"报告日期 {args.doc_date}",
                                 tickers and "标的 " + ", ".join(tickers)) if x)
    prefix = f"From: {sender}\nSubject: {args.title}\n" + (f"【{tag}】\n" if tag else "") + "\n"
    chunks = chunk_note(body, prefix)

    stem = f"{received}-{store.slug(args.institution, 20)}-{store.slug(args.title, 40)}-{doc_key[-8:]}"
    note_rel = Path("state/sales-notes/notes") / received[:4] / received[5:7] / f"{stem}.md"
    # 纯文本原件就是正文本身，不另存副本
    orig_rel = (Path("state/sales-notes/originals") / received[:4] / received[5:7] / f"{stem}{src.suffix.lower()}"
                if src and src.suffix.lower() not in NOTE_TEXT_EXTENSIONS else None)
    plan = {"doc_key": doc_key, "sender": sender, "subject": args.title, "date": received,
            "doc_date": args.doc_date, "kind": args.kind, "tickers": tickers, "chunks": len(chunks),
            "chars": len(body), "image_only": image_only, "path": str(note_rel),
            "original": str(orig_rel) if orig_rel else None}

    conn = init_db()
    found = conn.execute("SELECT MIN(id) FROM email_chunks WHERE message_id=?", (doc_key,)).fetchone()[0]
    if found is not None:
        conn.close()
        output({"ok": True, "status": "duplicate", "document_id": f"mail:{found}", **plan,
                "hint": "同一正文已在 sales notes 里，没有重复写入"})
        return
    if args.dry_run:
        conn.close()
        output({"ok": True, "status": "dry_run", **plan, "preview": chunks[0][:600] if chunks else ""})
        return

    # 正文和原件先落盘：后面 embedding 失败时重跑同一条命令即可补索引
    meta = {"doc_key": doc_key, "institution": args.institution, "analyst": args.analyst,
            "kind": args.kind, "title": args.title, "doc_date": args.doc_date, "received_date": received,
            "tickers": tickers, "source_url": args.source_url,
            "original": str(orig_rel) if orig_rel else None}
    front = "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items()
                              if v not in (None, [], "")) + "---\n"
    head = (f"# {args.title}\n\n**机构**: {sender}\n**类型**: {args.kind}\n"
            f"**报告/会议日期**: {args.doc_date or '未注明'}\n**收到日期**: {received}\n"
            + (f"**标的**: {', '.join(tickers)}\n" if tickers else "")
            + (f"**原文链接**: {args.source_url}\n" if args.source_url else "") + "\n---\n\n")
    note_path = WORKSPACE / note_rel
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(front + head + body.strip() + "\n", encoding="utf-8")
    if orig_rel:
        import shutil
        (WORKSPACE / orig_rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, WORKSPACE / orig_rel)

    try:
        embeddings = embed_texts([c for c in chunks], config)
    except Exception as e:
        conn.close()
        fail(f"embedding 失败: {type(e).__name__}: {str(e)[:200]}",
             f"正文已存 {note_rel}；重跑同一条命令即可补索引，不会重复写入")
    rows = [{"date": received, "email_id": doc_key, "message_id": doc_key, "sender": sender,
             "subject": args.title, "chunk_index": i, "content": c} for i, c in enumerate(chunks)]
    store_email_chunks(conn, rows, embeddings)
    first = conn.execute("SELECT MIN(id) FROM email_chunks WHERE message_id=?", (doc_key,)).fetchone()[0]
    conn.close()
    document_id = f"mail:{first}"

    image_result = {"ok": True, "status": "skipped", "reason": "no_original_file"}
    if orig_rel and src.suffix.lower() in ({".pdf"} | NOTE_IMAGE_EXTENSIONS):
        helper = image_helpers()
        job = helper.enqueue("sales-notes", doc_key, "file", {"path": str(WORKSPACE / orig_rel)}, {
            "title": args.title, "date": received, "parent_document_id": document_id,
            "original_source": sender, "date_basis": "手动入库收到日期"}, root=DATA_ROOT)
        drained = helper.drain("sales-notes")
        # drain 按队列顺序处理整个 sales-notes 积压，这条可能还没轮到；没轮到的下次 digest 会接着处理
        mine = next((j for j in drained.get("jobs", []) if j.get("doc_key") == doc_key), None)
        image_result = {"ok": drained.get("ok", False), "job": job.get("job"),
                        "status": (mine or {}).get("status", "queued"),
                        "queue_status": drained.get("status"), "pending_jobs": drained.get("pending_jobs")}

    store.append_manifest(WORKSPACE, {**meta, "document_id": document_id, "date": received,
                                      "sender": sender, "path": str(note_rel), "chunks": len(chunks),
                                      "embedding_model": config["embedding"]["model"], "origin": "manual",
                                      "ingested_at": store.now_iso()})
    output({"ok": True, "status": "added", "document_id": document_id, **plan,
            "embedding_model": config["embedding"]["model"], "image_ingestion": image_result,
            "read": f"researchdata.research(route=\"sales-notes\", operation=\"read\", document=\"{document_id}\")"})


def cmd_backfill(args, config):
    """Import historical emails from MBOX file into the vector DB."""
    mbox_path = Path(args.file)
    if not mbox_path.exists():
        fail(f"MBOX file not found: {mbox_path}")

    date_from = args.date_from
    date_to = args.date_to
    index_all = args.index_all
    domains = None
    if not index_all:
        domains = [d.lower() for d in config["email_sources"]["domains"]]
        if args.domains:
            domains = [d.strip().lower() for d in args.domains.split(",")]
    dry_run = args.dry_run
    batch_size = args.batch_size or 20

    log(f"Opening MBOX: {mbox_path}")
    log(f"Domain filter: {'ALL (no filter)' if index_all else domains}")
    log(f"Date range: {date_from} to {date_to}")

    # Get already-indexed message IDs for dedup
    conn = init_db()
    existing_msg_ids = set()
    rows = conn.execute("SELECT DISTINCT message_id FROM email_chunks WHERE message_id IS NOT NULL").fetchall()
    for row in rows:
        existing_msg_ids.add(row[0])
    log(f"Already indexed: {len(existing_msg_ids)} unique message IDs")

    # True streaming parse — read MBOX line by line, never load full file
    skipped_domain = 0
    skipped_date = 0
    skipped_dup = 0
    skipped_empty = 0
    total_messages = 0
    qualifying_count = 0
    total_chunks_stored = 0

    chunk_buffer = []
    dry_run_samples = []

    def process_raw_message(raw_bytes, msg_index):
        """Parse a single raw email message from bytes."""
        import email as email_mod
        try:
            return email_mod.message_from_bytes(raw_bytes)
        except Exception:
            return None

    def iter_mbox_messages(path):
        """Yield raw message bytes one at a time from an MBOX file, without loading all into memory."""
        current_lines = []
        with open(path, "rb") as f:
            for line in f:
                if line.startswith(b"From ") and current_lines:
                    yield b"".join(current_lines)
                    current_lines = []
                current_lines.append(line)
        if current_lines:
            yield b"".join(current_lines)

    for i, raw_bytes in enumerate(iter_mbox_messages(str(mbox_path))):
        msg = process_raw_message(raw_bytes, i)
        if msg is None:
            continue
        total_messages += 1
        # Free raw bytes immediately
        del raw_bytes

        # Extract sender
        from_header = msg.get("From", "")
        from_lower = from_header.lower()

        # Domain filter (skip if --all)
        if domains:
            matches_domain = any(d in from_lower for d in domains)
            if not matches_domain:
                skipped_domain += 1
                continue

        # Message-ID dedup
        msg_id = msg.get("Message-ID", "").strip()
        if msg_id and msg_id in existing_msg_ids:
            skipped_dup += 1
            continue

        # Date filter
        date_header = msg.get("Date", "")
        parsed_date = None
        if date_header:
            try:
                dt = email.utils.parsedate_to_datetime(date_header)
                parsed_date = dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        if parsed_date:
            if date_from and parsed_date < date_from:
                skipped_date += 1
                continue
            if date_to and parsed_date > date_to:
                skipped_date += 1
                continue

        # Skip emails with no sender AND no date (junk/corrupt entries)
        if not from_header.strip() and not parsed_date:
            skipped_empty += 1
            continue

        # Extract body
        body = extract_mbox_body(msg)
        if not body or len(body.strip()) < 50:
            skipped_empty += 1
            continue

        subject = msg.get("Subject", "")

        # Skip monster emails (>50K chars = ~33 chunks) to avoid index pollution
        MAX_BODY_CHARS = 50000
        if len(body) > MAX_BODY_CHARS:
            log(f"Truncating oversized email ({len(body)} chars -> {MAX_BODY_CHARS}): {str(subject)[:60]}")
            body = body[:MAX_BODY_CHARS]
        # Decode encoded subject if needed
        try:
            from email.header import decode_header as _decode_header
            decoded_parts = _decode_header(subject)
            subject = "".join(
                part.decode(enc or "utf-8") if isinstance(part, bytes) else part
                for part, enc in decoded_parts
            )
        except Exception:
            pass

        qualifying_count += 1
        email_data = {
            "from": from_header,
            "subject": subject,
            "date": date_header,
            "date_parsed": parsed_date or "unknown",
            "body": body,
            "message_id": msg_id,
            "id": msg_id or f"mbox-{i}"
        }

        if dry_run:
            if len(dry_run_samples) < 50:
                dry_run_samples.append({
                    "from": from_header,
                    "subject": subject[:100],
                    "date": parsed_date or "unknown",
                    "body_length": len(body),
                    "message_id": msg_id[:60] if msg_id else None
                })
            continue

        # Chunk this email
        date_str = email_data["date_parsed"]
        chunks = chunk_email(email_data, date_str)
        for c in chunks:
            c["message_id"] = email_data.get("message_id", "")
        chunk_buffer.extend(chunks)

        # Flush buffer when it reaches batch_size
        while len(chunk_buffer) >= batch_size:
            batch = chunk_buffer[:batch_size]
            chunk_buffer = chunk_buffer[batch_size:]
            texts = [c["content"] for c in batch]

            try:
                embeddings = embed_texts(texts, config)
                store_email_chunks(conn, batch, embeddings)
                total_chunks_stored += len(batch)
                print(f"\r[backfill] {total_chunks_stored} chunks | {qualifying_count} emails | scanning msg #{total_messages}", end="", file=sys.stderr)
            except Exception as e:
                log(f"Batch embedding failed: {e}")
                time.sleep(3)
                try:
                    embeddings = embed_texts(texts, config)
                    store_email_chunks(conn, batch, embeddings)
                    total_chunks_stored += len(batch)
                except Exception as e2:
                    log(f"Retry failed: {e2}. Skipping {len(batch)} chunks.")

        # Release memory — clear body after chunking
        email_data["body"] = ""
        del email_data
        if total_messages % 100 == 0:
            gc.collect()

    if dry_run:
        output({
            "ok": True,
            "status": "dry_run",
            "total_in_mbox": total_messages,
            "qualifying": qualifying_count,
            "skipped": {
                "domain": skipped_domain,
                "date": skipped_date,
                "duplicate": skipped_dup,
                "empty": skipped_empty
            },
            "sample": dry_run_samples
        })
        return

    # Flush remaining chunks
    if chunk_buffer:
        texts = [c["content"] for c in chunk_buffer]
        try:
            embeddings = embed_texts(texts, config)
            store_email_chunks(conn, chunk_buffer, embeddings)
            total_chunks_stored += len(chunk_buffer)
        except Exception as e:
            log(f"Final batch failed: {e}")

    print("", file=sys.stderr)
    conn.close()

    log(f"Done. {qualifying_count} emails → {total_chunks_stored} chunks")

    output({
        "ok": True,
        "status": "backfill_complete",
        "total_in_mbox": total_messages,
        "qualifying_emails": qualifying_count,
        "chunks_indexed": total_chunks_stored,
        "skipped": {
            "domain": skipped_domain,
            "date": skipped_date,
            "duplicate": skipped_dup,
            "empty": skipped_empty
        }
    })


def extract_mbox_body(msg):
    """Extract plaintext body from a mailbox.Message. Falls back to HTML → text."""
    plain = _extract_mbox_by_type(msg, "text/plain")
    if plain:
        return plain
    html = _extract_mbox_by_type(msg, "text/html")
    if html:
        return _html_to_text(html)
    return ""


def _extract_mbox_by_type(msg, target_type):
    """Extract first part matching target content type from a mailbox.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == target_type:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        return payload.decode("utf-8", errors="replace")
    else:
        if msg.get_content_type() == target_type:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    return payload.decode("utf-8", errors="replace")
    return ""


def cmd_self_test(args, config):
    """Verify all dependencies and connectivity."""
    results = {}

    # 1. Check gws CLI
    try:
        version = subprocess.run(["gws", "--version"], capture_output=True, text=True, timeout=10)
        results["gws_cli"] = {"ok": True, "version": version.stdout.strip()}
    except Exception as e:
        results["gws_cli"] = {"ok": False, "error": str(e), "hint": "Install gws: npm i -g @googleworkspace/cli"}

    # 2. Check gws auth
    try:
        auth = gws_run("auth", "status")
        has_token = auth.get("has_refresh_token", False)
        scope_count = auth.get("scope_count", 0)
        results["gws_auth"] = {"ok": has_token and scope_count > 0, "scopes": scope_count}
    except Exception as e:
        results["gws_auth"] = {"ok": False, "error": str(e), "hint": "Run: gws auth login -s gmail"}

    # 3. Check GEMINI_API_KEY
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        results["gemini_api_key"] = {"ok": True, "key_prefix": api_key[:8] + "..."}
    else:
        results["gemini_api_key"] = {"ok": False, "hint": "Export GEMINI_API_KEY"}

    # 4. Check Gemini embedding
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            r = client.models.embed_content(
                model=api_model(config["embedding"]["model"]),
                contents="test",
                config={"task_type": "RETRIEVAL_QUERY"}
            )
            dim = len(r.embeddings[0].values)
            results["gemini_embedding"] = {"ok": True, "model": config["embedding"]["model"], "dimensions": dim}
        except Exception as e:
            results["gemini_embedding"] = {"ok": False, "error": str(e)}
    else:
        results["gemini_embedding"] = {"ok": False, "hint": "Needs GEMINI_API_KEY"}

    # 5. Check SQLite
    try:
        conn = init_db()
        count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        results["vector_db"] = {"ok": True, "path": str(DB_PATH), "chunks": count}
        conn.close()
    except Exception as e:
        results["vector_db"] = {"ok": False, "error": str(e)}

    # 6. Check config
    results["config"] = {
        "ok": True,
        "domains": len(config["email_sources"]["domains"]),
        "priority_senders": len(config["email_sources"]["priority_senders"]),
        "sections": len(config.get("sections", []))
    }

    all_ok = all(v.get("ok", False) for v in results.values())
    output({"ok": all_ok, "checks": results})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global JSON_MODE, VERBOSE

    parser = argparse.ArgumentParser(
        prog="market-digest",
        description="Sales email digest + semantic search over market memory"
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--version", action="version", version="market-digest 1.0.0")

    sub = parser.add_subparsers(dest="command")

    # digest
    p_digest = sub.add_parser("digest", help="Fetch & process new sales emails")
    p_digest.add_argument("--period", required=True, choices=["AM", "PM", "am", "pm"])

    # search
    p_search = sub.add_parser("search", help="Semantic search market memory")
    p_search.add_argument("--query", "-q", required=True)
    p_search.add_argument("--top-k", type=int, default=10)
    p_search.add_argument("--date-from", help="YYYY-MM-DD")
    p_search.add_argument("--date-to", help="YYYY-MM-DD")

    # index
    p_index = sub.add_parser("index", help="Index a digest into vector DB")
    p_index.add_argument("--file", "-f", help="Path to .md file")
    p_index.add_argument("--text", "-t", help="Raw text to index")
    p_index.add_argument("--date", "-d", help="YYYY-MM-DD")
    p_index.add_argument("--period", "-p", help="AM|PM|FULL")

    # backfill
    p_backfill = sub.add_parser("backfill", help="Import historical emails from MBOX")
    p_backfill.add_argument("--file", "-f", required=True, help="Path to .mbox file")
    p_backfill.add_argument("--date-from", help="Only emails after YYYY-MM-DD")
    p_backfill.add_argument("--date-to", help="Only emails before YYYY-MM-DD")
    p_backfill.add_argument("--domains", help="Only emails from these domains (comma-separated)")
    p_backfill.add_argument("--all", action="store_true", dest="index_all", help="Index ALL emails (skip domain filter)")
    p_backfill.add_argument("--dry-run", action="store_true", help="Preview without indexing")
    p_backfill.add_argument("--batch-size", type=int, default=20, help="Embedding batch size")

    # add-note
    p_note = sub.add_parser("add-note", help="Add a forwarded sell-side note/report to sales notes")
    p_note.add_argument("--institution", required=True, help="机构，如 Morgan Stanley / 中金")
    p_note.add_argument("--title", required=True, help="标题")
    p_note.add_argument("--kind", default="研报", help="研报 / 点评 / 卖方电话会 / 销售观点 / 策略 等")
    p_note.add_argument("--analyst", help="分析师或销售姓名")
    p_note.add_argument("--doc-date", dest="doc_date", help="报告或会议日期 YYYY-MM-DD（写进正文头）")
    p_note.add_argument("--date", help="收到日期 YYYY-MM-DD，默认今天；检索的 start/end 按这个日期")
    p_note.add_argument("--tickers", help="涉及标的，逗号分隔")
    p_note.add_argument("--source-url", dest="source_url", help="原文链接")
    p_note.add_argument("--content", help="正文，或 @文件路径")
    p_note.add_argument("--file", help="原件 pdf/docx/md/txt/图片；不给 --content 时从原件提取正文")
    p_note.add_argument("--ocr", action="store_true", help="图片原件用视觉模型识别文字；默认只建图片向量")
    p_note.add_argument("--dry-run", action="store_true", help="只显示分块和写入位置")

    # self-test
    sub.add_parser("self-test", help="Verify connectivity")

    args = parser.parse_args()
    JSON_MODE = args.json
    VERBOSE = args.verbose

    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config()

    if args.command == "digest":
        cmd_digest(args, config)
    elif args.command == "search":
        cmd_search(args, config)
    elif args.command == "index":
        cmd_index(args, config)
    elif args.command == "backfill":
        cmd_backfill(args, config)
    elif args.command == "add-note":
        cmd_add_note(args, config)
    elif args.command == "self-test":
        cmd_self_test(args, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
