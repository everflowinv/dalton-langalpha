"""Cross-turn inline attachment budget middleware.

Caps total decoded bytes of inline image/PDF content blocks sent in a single
model request by walking user messages from newest to oldest and replacing
earlier attachment blocks beyond the 12 MB request budget with a compact text
pointer to the workspace copy, without mutating checkpoint messages.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from src.llms.attachment_payload import is_attachment_block, is_file_block, is_text_block

NATIVE_REQUEST_MAX_BYTES = 12 * 1024 * 1024

_ATTACHED_LABEL_RE = re.compile(
    r"^\[Attached (?:PDF|image):\s*(.+?)(?:\s*\(saved to ([^)]+)\))?\]$"
)
_REMINDER_SAVED_RE = re.compile(
    r"-\s*(?:PDF|Image|File)\s+'([^']+)'[^\n]*?\bsaved to\s+([^\s)]+)"
)
_GENERIC_SAVED_RE = re.compile(r"\bsaved to\s+([^\s)]+)")


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


def _clean_path(raw_path: str | None) -> str | None:
    if not raw_path:
        return None
    cleaned = raw_path.strip().rstrip(".,;)")
    return cleaned or None


def _extract_name_and_path(
    block: dict[str, Any], idx: int, content: list[Any]
) -> tuple[str, str | None]:
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

    # Scan message text blocks for upload note paths if not already resolved.
    reminder_pairs: list[tuple[str, str]] = []
    generic_paths: list[str] = []
    for item in content:
        if not is_text_block(item):
            continue
        text = str(item.get("text") or "")
        for rm in _REMINDER_SAVED_RE.finditer(text):
            n_cand = rm.group(1).strip()
            p_cand = _clean_path(rm.group(2))
            if n_cand and p_cand:
                reminder_pairs.append((n_cand, p_cand))
        for gm in _GENERIC_SAVED_RE.finditer(text):
            p_cand = _clean_path(gm.group(1))
            if p_cand:
                generic_paths.append(p_cand)

    if not name and reminder_pairs:
        name, path = reminder_pairs[0]
    if not path and name:
        for n_cand, p_cand in reminder_pairs:
            if n_cand == name:
                path = p_cand
                break
        if not path:
            for p_cand in generic_paths:
                if posixpath.basename(p_cand) == name:
                    path = p_cand
                    break
    if not path and len(generic_paths) == 1:
        path = generic_paths[0]
    if not name:
        if path:
            name = posixpath.basename(path) or ("pdf" if is_file_block(block) else "image")
        else:
            name = "pdf" if is_file_block(block) else "image"

    return name, path


def _build_evicted_placeholder(
    block: dict[str, Any], idx: int, content: list[Any]
) -> dict[str, str]:
    name, path = _extract_name_and_path(block, idx, content)
    location = f"已保存在 {path}" if path else "已保存在工作区 work/uploads/ 下"
    return {
        "type": "text",
        "text": f"[附件 {name} 较早上传，{location}；需要时用工具读取]",
    }


def trim_request_inline_attachments(
    messages: list[Any],
    *,
    budget_bytes: int = NATIVE_REQUEST_MAX_BYTES,
) -> list[Any]:
    """Walk user messages newest-first and replace earlier inline attachments past ``budget_bytes``.

    Returns the original ``messages`` list when nothing exceeds the budget, and
    never mutates any input message or content list in place so checkpoint
    state stays untouched.
    """
    if not messages:
        return messages

    accumulated = 0
    replacements_by_msg: dict[int, list[Any]] = {}

    for msg_idx in range(len(messages) - 1, -1, -1):
        msg = messages[msg_idx]
        if not _is_user_message(msg):
            continue
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if not isinstance(content, list):
            continue

        # Evaluate attachment blocks in this message; keep within budget, evict beyond.
        evicted_indices: set[int] = set()
        for blk_idx, block in enumerate(content):
            size = _inline_block_bytes(block)
            if size <= 0:
                continue
            if accumulated + size <= budget_bytes:
                accumulated += size
            else:
                accumulated += size
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
    "trim_request_inline_attachments",
]
