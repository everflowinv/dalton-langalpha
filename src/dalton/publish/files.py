"""Immutable publication file writer for ``published/<YYYY-MM-DD>-<slug>.{md,xlsx}`` (PK.11)."""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path
from typing import Any

from src.dalton.knowledge.paths import PUBLISHED_DIR
from src.dalton.settings import get_dalton_config

logger = logging.getLogger(__name__)


def compute_bundle_sha256(
    draft_bytes: bytes,
    attachment_blobs: list[tuple[str, str, bytes]],
) -> str:
    """Compute a deterministic SHA-256 digest over the draft and all attachments."""
    h = hashlib.sha256()
    h.update(b"draft\x00")
    h.update(draft_bytes)
    for role, path, content in sorted(attachment_blobs, key=lambda x: (x[0], x[1])):
        h.update(f"\x00att:{role}:{path}\x00".encode("utf-8"))
        h.update(content)
    return h.hexdigest()


def prepend_publication_banner(
    draft_markdown: str,
    *,
    title: str,
    kind: str,
    published_date: str,
    approver_user_id: str,
    approved_at: str,
    reviewer_model: str,
    reviewer_family: str,
    publication_id: str,
    thesis_version_no: int | None = None,
) -> str:
    """Prepend an immutable publication metadata header block to the published Markdown."""
    thesis_line = (
        f"\n> - **Thesis 正式版本**：v{thesis_version_no}"
        if thesis_version_no is not None
        else ""
    )
    banner = (
        f"<!-- dalton:publication id={publication_id} date={published_date} -->\n"
        f"> **正式发布研究成果（Official Publication）** — `{publication_id}`\n"
        f"> - **标题**：{title}（`{kind}`）\n"
        f"> - **批准人**：{approver_user_id} · {approved_at}\n"
        f"> - **独立审稿**：`{reviewer_model}`（`{reviewer_family}`）· 通过"
        f"{thesis_line}\n\n"
    )
    return banner + draft_markdown


async def _pick_non_colliding_path(
    base_rel_path: str,
    content_sha256: str,
    backend: Any,
) -> str:
    """Ensure ``published/`` files are never overwritten with different bytes (PK.11 step 7).

    If ``base_rel_path`` already exists with the exact same SHA-256, returns ``base_rel_path`` (idempotent retry).
    If it exists with different bytes, appends ``-2``, ``-3``, etc. before the file extension.
    """
    if backend is None:
        return base_rel_path

    p = Path(base_rel_path)
    stem = p.stem
    suffix = p.suffix
    parent = str(p.parent)

    candidate = base_rel_path
    idx = 2
    while True:
        existing_bytes: bytes | None = None
        if hasattr(backend, "adownload_file_bytes"):
            existing_bytes = await backend.adownload_file_bytes(candidate)
        elif hasattr(backend, "aread_text"):
            txt = await backend.aread_text(candidate)
            if txt is not None:
                existing_bytes = txt.encode("utf-8")

        if existing_bytes is None:
            return candidate

        existing_sha = hashlib.sha256(existing_bytes).hexdigest()
        if existing_sha == content_sha256:
            return candidate

        candidate = f"{parent}/{stem}-{idx}{suffix}"
        idx += 1


async def write_published_bundle(
    *,
    slug: str,
    published_date: date | str,
    published_markdown: str,
    attachments: list[dict[str, Any]] | None = None,
    backend: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Write ``published/<YYYY-MM-DD>-<slug>.{md,xlsx}`` and return ``(files_meta, file_blobs, notes)``."""
    date_str = (
        published_date[:10]
        if isinstance(published_date, str)
        else published_date.isoformat()
    )
    max_bytes = get_dalton_config().publish.max_file_bytes

    files_meta: list[dict[str, Any]] = []
    file_blobs: list[dict[str, Any]] = []
    notes: list[str] = []

    # 1. Markdown report
    md_bytes = published_markdown.encode("utf-8")
    md_sha = hashlib.sha256(md_bytes).hexdigest()
    base_md_path = f"{PUBLISHED_DIR}/{date_str}-{slug}.md"
    final_md_path = await _pick_non_colliding_path(base_md_path, md_sha, backend)
    if backend is not None:
        await backend.awrite_text(final_md_path, published_markdown)
    files_meta.append(
        {
            "path": final_md_path,
            "sha256": md_sha,
            "bytes": len(md_bytes),
            "mime": "text/markdown",
        }
    )
    file_blobs.append(
        {
            "path": final_md_path,
            "sha256": md_sha,
            "mime": "text/markdown",
            "content": md_bytes,
        }
    )

    # 2. Optional attachments (model .xlsx, appendix)
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        role = str(att.get("role") or "")
        src_path = str(att.get("path") or "")
        if not src_path or role == "facts":
            continue

        if role == "model" and backend is not None:
            raw_bytes: bytes | None = None
            if hasattr(backend, "adownload_file_bytes"):
                raw_bytes = await backend.adownload_file_bytes(src_path)
            if raw_bytes is not None:
                if len(raw_bytes) > max_bytes:
                    notes.append(
                        f"跳过模型归档 {src_path}：大小超过 max_file_bytes ({max_bytes})"
                    )
                    continue
                x_sha = hashlib.sha256(raw_bytes).hexdigest()
                base_x_path = f"{PUBLISHED_DIR}/{date_str}-{slug}.xlsx"
                final_x_path = await _pick_non_colliding_path(
                    base_x_path, x_sha, backend
                )
                if hasattr(backend, "aupload_files"):
                    await backend.aupload_files([(final_x_path, raw_bytes)])
                mime = (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                files_meta.append(
                    {
                        "path": final_x_path,
                        "sha256": x_sha,
                        "bytes": len(raw_bytes),
                        "mime": mime,
                    }
                )
                file_blobs.append(
                    {
                        "path": final_x_path,
                        "sha256": x_sha,
                        "mime": mime,
                        "content": raw_bytes,
                    }
                )

    return files_meta, file_blobs, notes

