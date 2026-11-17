"""sales notes 手动入库的正文与清单（state/sales-notes/）。

卖方邮件只在 market-digest 的 email_chunks 里存分块和向量。用户转发的研报、卖方电话会纪要，
以及从 wiki 迁过来的卖方文档，同样写进 email_chunks（message_id 以 ``manual:`` 开头），
另外在这里保存整篇正文和原件，读取时直接返回正文，不拼接带重叠的分块。

清单是 JSONL，同一个 doc_key 以最后一行为准。
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "manual:"


def home(root):
    return Path(root) / "state" / "sales-notes"


def manifest_path(root):
    return home(root) / "manifest.jsonl"


def is_manual(doc_key):
    return bool(doc_key) and doc_key.startswith(PREFIX)


def content_key(data):
    """doc_key from the note text (or original bytes for image-only notes)."""
    if isinstance(data, str):
        data = re.sub(r"\s+", " ", data).strip().encode("utf-8")
    return PREFIX + hashlib.sha256(data).hexdigest()[:16]


def slug(text, limit=40):
    text = re.sub(r"[^\w一-鿿]+", "-", (text or "").strip()).strip("-_")
    return (text[:limit].strip("-_") or "note")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest(root):
    entries = {}
    path = manifest_path(root)
    if not path.exists():
        return entries
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("doc_key"):
                entries[entry["doc_key"]] = entry
    return entries


def append_manifest(root, entry):
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def note_file(root, entry):
    """Stored full text for a manifest entry; None if missing or outside state/sales-notes."""
    rel = (entry or {}).get("path")
    if not rel:
        return None
    path = (Path(root) / rel).resolve()
    if not path.is_relative_to(home(root).resolve()) or path.suffix != ".md" or not path.is_file():
        return None
    return path
