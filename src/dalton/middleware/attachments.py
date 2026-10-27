"""Cross-turn inline attachment budget middleware.

Caps total decoded bytes of inline image/PDF content blocks sent in a single
model request by walking messages from newest to oldest — user messages and
tool results together, since a ``Read`` image or PDF rides on its
``ToolMessage`` — and replacing earlier attachment blocks beyond the 12 MB
request budget with a compact text pointer to the workspace copy, without
mutating checkpoint messages. An evicted block is swapped inside its message,
so a tool call/result pair always survives with its ``tool_call_id`` intact.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from src.llms.attachment_payload import is_attachment_block, is_file_block, is_text_block

NATIVE_REQUEST_MAX_BYTES = 12 * 1024 * 1024

#: The Read tool refuses an inline view above this size, so a bigger file needs
#: a Python-first reading path (PIL shrink, pypdf/pdfplumber extraction).
READ_INLINE_MAX_BYTES = 5 * 1024 * 1024

_ATTACHED_LABEL_RE = re.compile(
    r"^\[(?:Attached (?:PDF|image)|Viewing (?:PDF|image)):\s*(.+?)"
    r"(?:\s*\(saved to ([^)]+)\))?\]$"
)
# The upload note (`build_file_reminder` in src/server/utils/multimodal_context.py)
# is where name→path is recorded per attachment: "The user attached a file
# (name, mime). It has been saved to path." — the pairs below are matched by
# name, never assigned by position.
_FILE_NOTE_RE = re.compile(
    r"The user attached a file \(([^,)]+), [^)]+\)\. It has been saved to (\S+)"
)
_GENERIC_SAVED_RE = re.compile(r"\bsaved to\s+([^\s)]+)")

#: How to read a file the Read tool cannot take inline — one wording per
#: language, written once for both the upload note
#: (src/server/handlers/chat/attachments.py) and the eviction pointer below so
#: the two surfaces cannot drift apart.
_OVERSIZED_READING_PATHS: dict[str, dict[str, str]] = {
    "en": {
        "image": (
            "The image is over 5 MB — the Read tool cannot open it; shrink "
            "it below 5 MB with Python (PIL) first, then view it with Read."
        ),
        "pdf": (
            "The PDF is over 5 MB — the Read tool cannot open it; extract "
            "its text with Python (pypdf / pdfplumber), or split it into "
            "< 5 MB page files / render pages to images before viewing with Read."
        ),
    },
    "zh": {
        "image": "先用 Python（PIL）缩小到 5 MB 以下再用 Read",
        "pdf": "Read 打不开超过 5 MB 的 PDF，用 Python（pypdf / pdfplumber）提取文字，或拆成小于 5 MB 的文件再读",
    },
}


def reading_path_text(kind: str, size_bytes: int | None, *, lang: str = "zh") -> str:
    """The reading path for an image/PDF of this decoded size.

    Over ``READ_INLINE_MAX_BYTES`` the wording routes around Read's size
    refusal; at or under it Read serves the file directly. The English
    small-file branch is unreachable today (the upload note only describes
    oversized stashed files) and mirrors the note's plain-file tail.
    """
    if size_bytes is not None and size_bytes <= READ_INLINE_MAX_BYTES:
        return "Use Python to process it." if lang == "en" else "用 Read 查看"
    wording = _OVERSIZED_READING_PATHS.get(lang, _OVERSIZED_READING_PATHS["zh"])
    return wording[kind]


def _decoded_size(data: str) -> int:
    payload = data.split(",", 1)[1] if "," in data else data
    return len(payload) * 3 // 4


def _inline_block_bytes(block: Any) -> int:
    """Return decoded byte size of an inline image/PDF block (0 if not inline)."""
    if not isinstance(block, dict) or not is_attachment_block(block):
        return 0
    for key in ("base64", "data", "file_data"):
        val = block.get(key)
        if isinstance(val, str) and val:
            return _decoded_size(val)
    image_url = block.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url")
        if isinstance(url, str) and url and not url.startswith(("http://", "https://")):
            return _decoded_size(url)
    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data")
        if isinstance(data, str) and data:
            return _decoded_size(data)
    nested_file = block.get("file")
    if isinstance(nested_file, dict):
        for key in ("file_data", "base64", "data"):
            val = nested_file.get(key)
            if isinstance(val, str) and val:
                return _decoded_size(val)
    return 0


def _is_user_message(msg: Any) -> bool:
    if isinstance(msg, dict):
        return msg.get("role") in ("user", "human") or msg.get("type") in ("human", "user")
    return getattr(msg, "type", None) in ("human", "user")


def _is_tool_message(msg: Any) -> bool:
    if isinstance(msg, dict):
        return msg.get("role") == "tool" or msg.get("type") == "tool"
    return getattr(msg, "type", None) == "tool"


def _clean_path(raw_path: str | None) -> str | None:
    if not raw_path:
        return None
    cleaned = raw_path.strip().rstrip(".,;)")
    return cleaned or None


def _note_pairs(content: list[Any]) -> list[tuple[str, str]]:
    """(name, path) pairs the upload notes in this message recorded explicitly."""
    pairs: list[tuple[str, str]] = []
    for item in content:
        if not is_text_block(item):
            continue
        for m in _FILE_NOTE_RE.finditer(str(item.get("text") or "")):
            name = m.group(1).strip()
            path = _clean_path(m.group(2))
            if name and path:
                pairs.append((name, path))
    return pairs


def _extract_name_and_path(
    block: dict[str, Any], idx: int, content: list[Any]
) -> tuple[str, str | None]:
    """(name, path) for an attachment block, from associations recorded with it.

    Only explicit ones count: the label injected in front of the block
    (``[Attached PDF: name (saved to path)]``), the block's own filename, or an
    upload note whose file name equals this attachment's. A path saved for a
    different attachment is never borrowed — a wrong pointer is worse than
    none, because the model would Read a path that is not the file it saw.
    """
    label_name: str | None = None
    label_path: str | None = None
    if idx > 0 and is_text_block(content[idx - 1]):
        prev_text = str(content[idx - 1].get("text") or "").strip()
        m = _ATTACHED_LABEL_RE.match(prev_text)
        if m:
            label_name = m.group(1).strip()
            label_path = _clean_path(m.group(2))

    block_name = block.get("filename") or block.get("name")
    if not block_name and isinstance(block.get("file"), dict):
        block_name = block["file"].get("filename") or block["file"].get("name")
    name = str(block_name or label_name or "").strip()
    path = label_path

    if not path and name:
        for n_cand, p_cand in _note_pairs(content):
            if n_cand == name:
                path = p_cand
                break
        if not path:
            # A saved path counts only when the file is named exactly like this
            # attachment (uploads get dedup suffixes, so a near-miss never
            # matches here).
            for item in content:
                if not is_text_block(item):
                    continue
                for gm in _GENERIC_SAVED_RE.finditer(str(item.get("text") or "")):
                    p_cand = _clean_path(gm.group(1))
                    if p_cand and posixpath.basename(p_cand) == name:
                        path = p_cand
                        break
                if path:
                    break

    if not name:
        name = "pdf" if is_file_block(block) else "image"

    return name, path


def _build_evicted_placeholder(
    block: dict[str, Any], idx: int, content: list[Any]
) -> dict[str, str]:
    name, path = _extract_name_and_path(block, idx, content)
    if path is None:
        return {
            "type": "text",
            "text": (
                f"[附件 {name} 较早上传，已从本次请求中省略，未找到它在工作区的保存位置；"
                "如需再看，请让用户重新上传]"
            ),
        }
    guidance = reading_path_text(
        "pdf" if is_file_block(block) else "image", _inline_block_bytes(block)
    )
    return {
        "type": "text",
        "text": f"[附件 {name} 较早上传，已保存在 {path}；{guidance}]",
    }


def trim_request_inline_attachments(
    messages: list[Any],
    *,
    budget_bytes: int = NATIVE_REQUEST_MAX_BYTES,
) -> list[Any]:
    """Walk messages newest-first and replace earlier inline attachments past ``budget_bytes``.

    User messages and tool results are accumulated together — a ``Read`` image
    or PDF on a ``ToolMessage`` is as much request payload as an upload, and a
    replaced block never removes its message, so tool call/result pairs keep
    their structure. Each block is judged on what budget remains by itself: an
    evicted block is not sent and spends nothing, so an older small attachment
    still rides inline when it fits. Returns the original ``messages`` list
    when nothing exceeds the budget, and never mutates any input message or
    content list in place so checkpoint state stays untouched.
    """
    if not messages:
        return messages

    accumulated = 0
    replacements_by_msg: dict[int, list[Any]] = {}

    for msg_idx in range(len(messages) - 1, -1, -1):
        msg = messages[msg_idx]
        if not (_is_user_message(msg) or _is_tool_message(msg)):
            continue
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if not isinstance(content, list):
            continue

        evicted_indices: set[int] = set()
        for blk_idx, block in enumerate(content):
            size = _inline_block_bytes(block)
            if size <= 0:
                continue
            if accumulated + size <= budget_bytes:
                accumulated += size
            else:
                evicted_indices.add(blk_idx)

        if evicted_indices:
            new_blocks = [
                _build_evicted_placeholder(block, blk_idx, content)
                if blk_idx in evicted_indices
                else block
                for blk_idx, block in enumerate(content)
            ]
            replacements_by_msg[msg_idx] = new_blocks

    if not replacements_by_msg:
        return messages

    result: list[Any] = []
    for msg_idx, msg in enumerate(messages):
        if msg_idx not in replacements_by_msg:
            result.append(msg)
            continue
        new_blocks = replacements_by_msg[msg_idx]
        if isinstance(msg, dict):
            result.append({**msg, "content": new_blocks})
        else:
            result.append(msg.model_copy(update={"content": new_blocks}))
    return result


class DaltonAttachmentBudgetMiddleware(AgentMiddleware):
    """Enforce a per-request inline attachment byte budget across turns."""

    def __init__(self, *, budget_bytes: int = NATIVE_REQUEST_MAX_BYTES) -> None:
        super().__init__()
        self.budget_bytes = budget_bytes

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        messages = trim_request_inline_attachments(
            request.messages, budget_bytes=self.budget_bytes
        )
        if messages is not request.messages:
            return handler(request.override(messages=messages))
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        messages = trim_request_inline_attachments(
            request.messages, budget_bytes=self.budget_bytes
        )
        if messages is not request.messages:
            return await handler(request.override(messages=messages))
        return await handler(request)


__all__ = [
    "DaltonAttachmentBudgetMiddleware",
    "NATIVE_REQUEST_MAX_BYTES",
    "READ_INLINE_MAX_BYTES",
    "reading_path_text",
    "trim_request_inline_attachments",
]
