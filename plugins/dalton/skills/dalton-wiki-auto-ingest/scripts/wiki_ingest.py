"""Fetch Discord wiki-channel messages and record ingest progress.

The script calls the read-only ``discord-history`` research route. It does not
store the bot token, add reactions, or import a previous OpenClaw state file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
STATE_ROOT = "state/wiki-auto-ingest"
PAGE_LIMIT = 100
PAGE_CAP = 100
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 60.0
_CONTINUE_NOTE = "本轮未读完，下次继续"
_OVERSIZE_NOTE = "文档超过单次读取上限，需要人工处理"
_EXPIRED_HTTP = frozenset({403, 404, 410})
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
_PATH_LEAKS = ("/Users/", "/private/", "/home/")
_MARK_STATUSES = {"ingested", "skipped", "pending"}


def _snowflake_ok(value: str) -> bool:
    if not value or len(value) > 32:
        return False
    return value.isdigit()


def _load_script(filename: str, module_name: str) -> Any:
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{filename} is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _card_io() -> Any:
    return _load_script("card_io.py", "card_io_dalton_wiki_auto_ingest")


def workspace_file(workspace: Path, relative: str) -> Path:
    return _card_io().workspace_file(workspace, relative)


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    _card_io().write_bytes_exclusive(path, payload)


def require_workspace(value: str | None) -> Path:
    if not value:
        raise ValueError("missing --workspace-dir")
    path = Path(value)
    if path.is_symlink():
        raise ValueError("path must stay inside the workspace")
    if not path.is_dir():
        raise ValueError("workspace not found")
    return path


def _public_error(exc: BaseException) -> str:
    text = str(exc)
    if any(marker in text for marker in _PATH_LEAKS):
        return exc.__class__.__name__
    return text


def now_hk() -> datetime:
    """Clock for last_run and the report date. Tests replace this function."""
    return datetime.now(HONG_KONG)


def call_research(**arguments: Any) -> dict[str, Any]:
    """Call ``research`` from the sandbox researchdata tool and return an object."""
    from tools.researchdata import research

    result = research(**arguments)
    if isinstance(result, str):
        parsed = json.loads(result)
        if not isinstance(parsed, dict):
            raise TypeError("research returned a non-object")
        return parsed
    if not isinstance(result, dict):
        raise TypeError("research returned a non-object")
    return result


def _urlopen(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


def report_run_id(channel_id: str, now: datetime | None = None) -> str:
    current = now or now_hk()
    return f"wiki-auto-ingest-{channel_id}-{current.date().isoformat()}"


def _string_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def _blank_state(channel_id: str) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "last_processed_message_id": "",
        "pending_messages": [],
        "batch_ids": [],
        "run_marks": {},
        "fetch_errors": [],
        "continue_after": "",
        "sender_policy": None,
        "last_run": None,
        "last_result": {
            "scanned": 0,
            "ingested": 0,
            "skipped": 0,
            "pending": 0,
            "errors": [],
            "outputs": [],
        },
    }


def _state_rel(channel_id: str) -> str:
    return f"{STATE_ROOT}/{channel_id}.json"


def _message_rel(channel_id: str, message_id: str) -> str:
    return f"{STATE_ROOT}/{channel_id}/messages/{message_id}.json"


def _require_channel(value: str | None) -> str:
    text = str(value or "").strip()
    if not _snowflake_ok(text):
        raise ValueError("channel_id 需要是数字雪花 ID")
    return text


def _load_state(workspace: Path, channel_id: str) -> tuple[dict[str, Any] | None, str | None]:
    rel = _state_rel(channel_id)
    path = workspace_file(workspace, rel)
    if not path.exists():
        return None, None
    if path.is_symlink():
        raise ValueError("refusing symlink")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("state is not an object")
    stored = _string_id(data.get("channel_id"))
    if stored and stored != channel_id:
        raise ValueError("channel_id mismatch")
    data["channel_id"] = channel_id
    data["last_processed_message_id"] = _string_id(data.get("last_processed_message_id"))
    pending = data.get("pending_messages")
    data["pending_messages"] = pending if isinstance(pending, list) else []
    batch = data.get("batch_ids")
    data["batch_ids"] = [_string_id(item) for item in batch] if isinstance(batch, list) else []
    marks = data.get("run_marks")
    data["run_marks"] = marks if isinstance(marks, dict) else {}
    errors = data.get("fetch_errors")
    data["fetch_errors"] = [str(item) for item in errors] if isinstance(errors, list) else []
    if not isinstance(data.get("last_result"), dict):
        data["last_result"] = _blank_state(channel_id)["last_result"]
    return data, None


def write_json(workspace: Path, relative: str, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    path = workspace_file(workspace, relative)
    write_bytes_exclusive(path, text.encode("utf-8"))


def _save_state(workspace: Path, channel_id: str, state: dict[str, Any]) -> None:
    state["channel_id"] = channel_id
    write_json(workspace, _state_rel(channel_id), state)


def _id_num(value: Any) -> int:
    text = _string_id(value)
    if _snowflake_ok(text):
        return int(text)
    return -1


def _advance_cursor(state: dict[str, Any]) -> None:
    current = _string_id(state.get("last_processed_message_id"))
    best = _id_num(current)
    best_text = current
    marks = state.get("run_marks") if isinstance(state.get("run_marks"), dict) else {}
    batch = state.get("batch_ids") if isinstance(state.get("batch_ids"), list) else []
    for raw in batch:
        text = _string_id(raw)
        if not _snowflake_ok(text) or text not in marks:
            break
        number = int(text)
        if number > best:
            best = number
            best_text = text
    state["last_processed_message_id"] = best_text


def _recompute(state: dict[str, Any]) -> None:
    marks = state.get("run_marks") if isinstance(state.get("run_marks"), dict) else {}
    pending = [item for item in (state.get("pending_messages") or []) if isinstance(item, dict)]
    state["pending_messages"] = pending
    fetch_errors = [str(item) for item in (state.get("fetch_errors") or []) if str(item).strip()]
    state["fetch_errors"] = fetch_errors
    errors = list(fetch_errors)
    for item in pending:
        err = str(item.get("error") or "").strip()
        if err and err not in errors:
            errors.append(err)
    outputs: list[str] = []
    ingested = 0
    skipped = 0
    for mark in marks.values():
        if not isinstance(mark, dict):
            continue
        status = mark.get("status")
        if status == "ingested":
            ingested += 1
        elif status == "skipped":
            skipped += 1
        if status == "ingested":
            for out in mark.get("outputs") or []:
                text = str(out).strip()
                if text and text not in outputs:
                    outputs.append(text)
    previous = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
    scanned = previous.get("scanned")
    if isinstance(scanned, bool) or not isinstance(scanned, int):
        scanned = 0
    state["last_result"] = {
        "scanned": scanned,
        "ingested": ingested,
        "skipped": skipped,
        "pending": len(pending),
        "errors": errors,
        "outputs": outputs,
    }


def _continue_after(state: dict[str, Any]) -> str:
    text = _string_id(state.get("continue_after"))
    return text if _snowflake_ok(text) else ""


def _is_oversize_pending(item: dict[str, Any]) -> bool:
    if item.get("oversize_doc") is True:
        return True
    return _OVERSIZE_NOTE in str(item.get("error") or "")


def _oversize_docs(state: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for item in state.get("pending_messages") or []:
        if not isinstance(item, dict) or not _is_oversize_pending(item):
            continue
        title = str(item.get("doc_title") or "").strip() or "未记录标题"
        found.append({"id": _string_id(item.get("id")), "doc_title": title})
    return found


def _quiet(state: dict[str, Any]) -> bool:
    result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
    pending = state.get("pending_messages") or []
    errors = result.get("errors") or []
    ingested = result.get("ingested") or 0
    return ingested == 0 and not pending and not errors and not _continue_after(state)


def _summary_text(state: dict[str, Any]) -> str:
    if _quiet(state):
        return "这一轮没有新的入库，也没有待处理失败。"
    result = state.get("last_result") or {}
    lines = [
        "扫描 {scanned}，入库 {ingested}，跳过 {skipped}，待处理 {pending}。".format(
            scanned=result.get("scanned") or 0,
            ingested=result.get("ingested") or 0,
            skipped=result.get("skipped") or 0,
            pending=result.get("pending") or 0,
        )
    ]
    outputs = result.get("outputs") or []
    if outputs:
        lines.append("产出：" + "、".join(str(item) for item in outputs))
    for item in state.get("pending_messages") or []:
        if not isinstance(item, dict):
            continue
        if _is_oversize_pending(item):
            title = str(item.get("doc_title") or "").strip() or "未记录标题"
            lines.append(f"消息 {item.get('id')}《{title}》：{_OVERSIZE_NOTE}")
            continue
        lines.append(f"待处理 {item.get('id')}：{item.get('error') or '待补齐'}")
    fetch_errors = [str(item) for item in (state.get("fetch_errors") or [])]
    for err in fetch_errors:
        lines.append(f"读取：{err}")
    cursor = _continue_after(state)
    if cursor and not any(_CONTINUE_NOTE in item for item in fetch_errors):
        lines.append(f"{_CONTINUE_NOTE}（continue_after {cursor}）")
    return "\n".join(lines)


def _clean_attachment(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    size = item.get("size")
    return {
        "id": _string_id(item.get("id")),
        "filename": item.get("filename") if isinstance(item.get("filename"), str) else "",
        "content_type": item.get("content_type") if isinstance(item.get("content_type"), str) else "",
        "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0,
        "url": item.get("url") if isinstance(item.get("url"), str) else "",
    }


def _clean_embeds(raw: Any) -> list[dict[str, str]]:
    embeds: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return embeds
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url") if isinstance(item.get("url"), str) else ""
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        if not url and not title:
            continue
        entry: dict[str, str] = {}
        if url:
            entry["url"] = url
        if title:
            entry["title"] = title
        embeds.append(entry)
    return embeds


def _clean_message(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    message_id = _string_id(raw.get("id"))
    if not _snowflake_ok(message_id):
        return None
    attachments = []
    for item in raw.get("attachments") or []:
        cleaned = _clean_attachment(item)
        if cleaned is not None:
            attachments.append(cleaned)
    content = raw.get("content")
    return {
        "id": message_id,
        "author_id": _string_id(raw.get("author_id")),
        "author_is_bot": raw.get("author_is_bot") is True,
        "timestamp": raw.get("timestamp") if isinstance(raw.get("timestamp"), str) else "",
        "content": content if isinstance(content, str) else "",
        "attachments": attachments,
        "embeds": _clean_embeds(raw.get("embeds")),
    }


def _snapshot(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": message["id"],
        "author_id": message.get("author_id") or "",
        "author_is_bot": message.get("author_is_bot") is True,
        "timestamp": message.get("timestamp") or "",
        "content": message.get("content") or "",
        "attachments": list(message.get("attachments") or []),
        "embeds": list(message.get("embeds") or []),
    }


def _bridge_error(body: Any) -> str:
    if not isinstance(body, dict):
        return "读取失败"
    hint = str(body.get("hint") or body.get("error") or body.get("status") or "读取失败").strip()
    if any(marker in hint for marker in _PATH_LEAKS):
        return "读取失败"
    return hint[:500] or "读取失败"


def _skip_reason(message: dict[str, Any], sender_id: str | None) -> str | None:
    if message.get("author_is_bot") is True:
        return "bot"
    if sender_id is None:
        return None
    if message.get("author_id") != sender_id:
        return "sender_filter"
    return None


def _pending_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in state.get("pending_messages") or []:
        if not isinstance(item, dict):
            continue
        message_id = _string_id(item.get("id"))
        if _snowflake_ok(message_id):
            item["id"] = message_id
            found[message_id] = item
    return found


def cmd_state(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    channel_id = _require_channel(args.channel_id)
    state, _err = _load_state(workspace, channel_id)
    if state is None:
        blank = _blank_state(channel_id)
        blank["ok"] = True
        blank["exists"] = False
        return blank
    state["ok"] = True
    state["exists"] = True
    return state


def cmd_fetch(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    channel_id = _require_channel(args.channel_id)
    sender_raw = getattr(args, "sender_id", None)
    all_non_bot = bool(getattr(args, "all_non_bot", False))
    if bool(sender_raw) == all_non_bot:
        return {
            "ok": False,
            "status": "bad_sender_policy",
            "error": "发送者策略需要 --sender-id 或 --all-non-bot 里的一个",
        }
    sender_id: str | None = None
    if sender_raw:
        sender_id = str(sender_raw).strip()
        if not _snowflake_ok(sender_id):
            return {"ok": False, "status": "bad_sender", "error": "sender-id 需要是数字雪花 ID"}
    state, _err = _load_state(workspace, channel_id)
    if state is None:
        state = _blank_state(channel_id)
    cursor = _string_id(state.get("last_processed_message_id"))
    if not cursor:
        return {
            "ok": False,
            "status": "missing_cursor",
            "error": "缺少 last_processed_message_id，先导入游标再拉取",
        }
    if not _snowflake_ok(cursor):
        return {"ok": False, "status": "bad_cursor", "error": "游标不是数字雪花 ID"}

    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    fetch_errors: list[str] = []
    truncated = False
    after = cursor
    pages = 0
    while pages < PAGE_CAP:
        pages += 1
        try:
            body = call_research(
                route="discord-history",
                operation="list_messages",
                channel_id=channel_id,
                after=after,
                limit=PAGE_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 - one failed page stays on the run envelope
            fetch_errors.append(_public_error(exc) or "读取失败")
            break
        if not isinstance(body, dict) or body.get("ok") is not True:
            fetch_errors.append(_bridge_error(body))
            break
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        raw_messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        page_ids: list[str] = []
        for raw in raw_messages:
            cleaned = _clean_message(raw)
            if cleaned is None or cleaned["id"] in seen:
                continue
            seen.add(cleaned["id"])
            page_ids.append(cleaned["id"])
            collected.append(cleaned)
        if not page_ids:
            break
        after = max(page_ids, key=int)
        has_more = data.get("has_more") is True
        if not has_more:
            break
        if pages >= PAGE_CAP:
            truncated = True
            break
    continue_after = after if truncated else ""
    if truncated:
        fetch_errors.append(f"{_CONTINUE_NOTE}（continue_after {continue_after}）")

    collected.sort(key=lambda item: int(item["id"]))
    pending_by_id = _pending_map(state)
    skip_marks: dict[str, dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    for message in collected:
        reason = _skip_reason(message, sender_id)
        if reason is None:
            kept.append(message)
            continue
        skip_marks[message["id"]] = {
            "status": "skipped",
            "outputs": [],
            "reason": reason,
            "error": "",
        }
        pending_by_id.pop(message["id"], None)

    new_messages = [item for item in kept if item["id"] not in pending_by_id]
    pending_items = sorted(pending_by_id.values(), key=lambda item: _id_num(item.get("id")))
    queue: list[dict[str, Any]] = []
    for item in pending_items:
        fresh = next((message for message in kept if message["id"] == item["id"]), None)
        merged = _snapshot(fresh or _clean_message(item) or {"id": item["id"]})
        merged["queue"] = "pending"
        merged["status"] = "pending"
        merged["source"] = str(item.get("source") or "ingest")
        merged["error"] = str(item.get("error") or "")
        merged["outputs"] = [str(out) for out in (item.get("outputs") or [])]
        merged["doc_title"] = str(item.get("doc_title") or "")
        merged["oversize_doc"] = (
            item.get("oversize_doc") is True or _OVERSIZE_NOTE in merged["error"]
        )
        queue.append(merged)
    for message in new_messages:
        item = _snapshot(message)
        item["queue"] = "new"
        queue.append(item)

    for message in collected:
        write_json(workspace, _message_rel(channel_id, message["id"]), _snapshot(message))
    for item in queue:
        if item["id"] not in {message["id"] for message in collected}:
            write_json(workspace, _message_rel(channel_id, item["id"]), _snapshot(item))

    state["sender_policy"] = (
        {"mode": "all_non_bot"} if sender_id is None else {"mode": "sender", "sender_id": sender_id}
    )
    state["pending_messages"] = [
        {
            "id": item["id"],
            "status": "pending",
            "source": item.get("source") or "ingest",
            "error": item.get("error") or "",
            "outputs": list(item.get("outputs") or []),
            "doc_title": str(item.get("doc_title") or ""),
            "oversize_doc": item.get("oversize_doc") is True,
            "author_id": item.get("author_id") or "",
            "author_is_bot": item.get("author_is_bot") is True,
            "timestamp": item.get("timestamp") or "",
            "content": item.get("content") or "",
            "attachments": list(item.get("attachments") or []),
            "embeds": list(item.get("embeds") or []),
        }
        for item in queue
        if item.get("queue") == "pending"
    ]
    state["batch_ids"] = [message["id"] for message in collected]
    state["run_marks"] = skip_marks
    state["fetch_errors"] = fetch_errors
    state["continue_after"] = continue_after if _snowflake_ok(continue_after) else ""
    state["last_run"] = now_hk().isoformat()
    state["last_result"] = {
        "scanned": len(queue),
        "ingested": 0,
        "skipped": 0,
        "pending": 0,
        "errors": [],
        "outputs": [],
    }
    _recompute(state)
    _advance_cursor(state)
    _save_state(workspace, channel_id, state)
    return {
        "ok": True,
        "channel_id": channel_id,
        "read_ok": not fetch_errors,
        "truncated": truncated,
        "continue_after": state["continue_after"],
        "last_processed_message_id": state["last_processed_message_id"],
        "report_run_id": report_run_id(channel_id),
        "messages": queue,
        "pending_count": len(pending_items),
        "new_count": len(new_messages),
        "scanned": len(queue),
        "errors": fetch_errors,
        "last_result": state["last_result"],
    }


def _safe_filename(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    text = name.strip()
    if not text or len(text) > 180:
        return None
    if text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        return None
    if Path(text).name != text:
        return None
    return text


def _allowed_cdn_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return host in _CDN_HOSTS


class AttachmentTooLarge(Exception):
    pass


class AttachmentGone(Exception):
    """CDN link returned 403, 404, or 410. The body is not read."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"HTTP {code}")


def _response_status(resp: Any) -> int | None:
    for name in ("status", "status_code"):
        value = getattr(resp, name, None)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value
    return None


def fetch_attachment(url: str) -> bytes:
    if not _allowed_cdn_url(url):
        raise ValueError("附件地址不在 Discord CDN")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "DaltonWikiIngest/1.0"},
        method="GET",
    )
    try:
        opened = _urlopen(req, DOWNLOAD_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        code = exc.code if isinstance(exc.code, int) and not isinstance(exc.code, bool) else None
        if code in _EXPIRED_HTTP:
            raise AttachmentGone(code) from None
        raise
    with opened as resp:
        status = _response_status(resp)
        if status in _EXPIRED_HTTP:
            raise AttachmentGone(status)
        final = resp.geturl() if hasattr(resp, "geturl") else url
        if not _allowed_cdn_url(str(final)):
            raise ValueError("附件地址不在 Discord CDN")
        raw_length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
        if raw_length not in (None, ""):
            try:
                if int(raw_length) > MAX_ATTACHMENT_BYTES:
                    raise AttachmentTooLarge()
            except AttachmentTooLarge:
                raise
            except (TypeError, ValueError):
                pass
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ATTACHMENT_BYTES:
                raise AttachmentTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)


def _stored_attachment(workspace: Path, file_rel: str, size: Any) -> str | None:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    try:
        dest = workspace_file(workspace, file_rel)
    except ValueError:
        return None
    if dest.is_symlink() or not dest.is_file():
        return None
    try:
        actual = dest.stat().st_size
    except OSError:
        return None
    if actual != size:
        return None
    return file_rel


def _refetch_message(channel_id: str, message_id: str) -> tuple[dict[str, Any] | None, str]:
    """Load the same message once. A mismatch returns a reason and no message."""
    try:
        body = call_research(
            route="discord-history",
            operation="list_messages",
            channel_id=channel_id,
            after=str(int(message_id) - 1),
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001 - one refresh failure stays on this message
        return None, _public_error(exc) or "重新获取消息失败"
    if not isinstance(body, dict) or body.get("ok") is not True:
        return None, "附件链接失效，重新获取消息没有对上同一条"
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    raw_messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    for raw in raw_messages:
        cleaned = _clean_message(raw)
        if cleaned is not None and cleaned["id"] == message_id:
            return cleaned, ""
    return None, "附件链接失效，重新获取消息没有对上同一条"


def _replace_attachment_urls(snapshot: dict[str, Any], fresh: dict[str, Any]) -> None:
    # 替换是原位进行的，附件顺序与数量不变，download 循环靠这一点保持下标对齐。
    fresh_urls: dict[str, str] = {}
    for item in fresh.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        attachment_id = _string_id(item.get("id"))
        url = item.get("url") if isinstance(item.get("url"), str) else ""
        if attachment_id and url:
            fresh_urls[attachment_id] = url
    updated: list[Any] = []
    for item in snapshot.get("attachments") or []:
        if not isinstance(item, dict):
            updated.append(item)
            continue
        url = fresh_urls.get(_string_id(item.get("id")))
        if not url:
            updated.append(item)
            continue
        copied = dict(item)
        copied["url"] = url
        updated.append(copied)
    snapshot["attachments"] = updated


def _attachment_url(snapshot: dict[str, Any], attachment_id: str) -> str:
    for item in snapshot.get("attachments") or []:
        if isinstance(item, dict) and _string_id(item.get("id")) == attachment_id:
            url = item.get("url")
            return url if isinstance(url, str) else ""
    return ""


def _save_download(workspace: Path, file_rel: str, payload: bytes) -> None:
    dest = workspace_file(workspace, file_rel)
    write_bytes_exclusive(dest, payload)


def cmd_download(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    channel_id = _require_channel(args.channel_id)
    message_id = _string_id(getattr(args, "message_id", None))
    if not _snowflake_ok(message_id):
        return {"ok": False, "status": "bad_message", "error": "message_id 需要是数字雪花 ID"}
    rel = _message_rel(channel_id, message_id)
    path = workspace_file(workspace, rel)
    if not path.is_file() or path.is_symlink():
        return {"ok": False, "status": "missing_snapshot", "error": "没有这条消息的快照"}
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        return {"ok": False, "status": "missing_snapshot", "error": "没有这条消息的快照"}
    raw_attachments = snapshot.get("attachments") if isinstance(snapshot.get("attachments"), list) else []
    attachments = list(raw_attachments)
    files: list[str] = []
    errors: list[dict[str, str]] = []
    used: set[str] = set()
    refreshed = False
    refresh_failure = ""
    index = 0
    while index < len(attachments):
        item = attachments[index]
        index += 1
        cleaned = _clean_attachment(item)
        if cleaned is None:
            continue
        attachment_id = cleaned.get("id") or ""
        filename = _safe_filename(cleaned.get("filename"))
        if filename is None:
            errors.append({"id": attachment_id, "error": "文件名不安全"})
            continue
        if filename in used:
            prefix = _safe_filename(attachment_id) or "dup"
            candidate = f"{prefix}-{filename}"
            filename = candidate if _safe_filename(candidate) else f"{prefix}-{len(used)}.bin"
        used.add(filename)
        file_rel = f"{STATE_ROOT}/{channel_id}/files/{message_id}/{filename}"
        reused = _stored_attachment(workspace, file_rel, cleaned.get("size"))
        if reused:
            files.append(reused)
            continue
        url = str(cleaned.get("url") or "")
        if not _allowed_cdn_url(url):
            errors.append({"id": attachment_id, "error": "附件地址不在 Discord CDN"})
            continue
        try:
            payload = fetch_attachment(url)
        except AttachmentGone as gone:
            if refreshed and refresh_failure:
                errors.append({"id": attachment_id, "error": refresh_failure})
                continue
            if refreshed:
                errors.append({"id": attachment_id, "error": f"重新获取后仍是 HTTP {gone.code}"})
                continue
            refreshed = True
            fresh, reason = _refetch_message(channel_id, message_id)
            if fresh is None:
                refresh_failure = reason or "附件链接失效，重新获取消息没有对上同一条"
                errors.append({"id": attachment_id, "error": refresh_failure})
                continue
            _replace_attachment_urls(snapshot, fresh)
            write_json(workspace, rel, snapshot)
            attachments = list(snapshot.get("attachments") or [])
            new_url = _attachment_url(snapshot, attachment_id)
            if not _allowed_cdn_url(new_url):
                errors.append({"id": attachment_id, "error": "附件链接失效，重新获取的消息里没有同一附件"})
                continue
            try:
                payload = fetch_attachment(new_url)
            except AttachmentGone as again:
                errors.append({"id": attachment_id, "error": f"重新获取后仍是 HTTP {again.code}"})
                continue
            except AttachmentTooLarge:
                errors.append({"id": attachment_id, "error": "附件超过 50MB"})
                continue
            except Exception as exc:  # noqa: BLE001 - one attachment failure stays on this message
                errors.append({"id": attachment_id, "error": _public_error(exc) or "读取失败"})
                continue
        except AttachmentTooLarge:
            errors.append({"id": attachment_id, "error": "附件超过 50MB"})
            continue
        except Exception as exc:  # noqa: BLE001 - one attachment failure stays on this message
            errors.append({"id": attachment_id, "error": _public_error(exc) or "读取失败"})
            continue
        _save_download(workspace, file_rel, payload)
        files.append(file_rel)
    return {
        "ok": not errors,
        "message_id": message_id,
        "files": files,
        "errors": errors,
    }


def _clip(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if any(marker in text for marker in _PATH_LEAKS):
        return ""
    return text.strip()[:500]


def cmd_mark(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    channel_id = _require_channel(args.channel_id)
    message_id = _string_id(getattr(args, "message_id", None))
    status = str(getattr(args, "status", "") or "").strip()
    if not _snowflake_ok(message_id):
        return {"ok": False, "status": "bad_message", "error": "message_id 需要是数字雪花 ID"}
    if status not in _MARK_STATUSES:
        return {"ok": False, "status": "bad_status", "error": "status 需要是 ingested、skipped 或 pending"}
    state, _err = _load_state(workspace, channel_id)
    if state is None:
        return {"ok": False, "status": "missing_state", "error": "还没有这个频道的状态"}
    outputs = [_clip(item) for item in (getattr(args, "output", None) or [])]
    outputs = [item for item in outputs if item]
    doc_title = _clip(getattr(args, "doc_title", ""))
    mark = {
        "status": status,
        "outputs": outputs,
        "reason": _clip(getattr(args, "reason", "")),
        "error": _clip(getattr(args, "error", "")),
    }
    if doc_title:
        mark["doc_title"] = doc_title
    if _OVERSIZE_NOTE in mark["error"]:
        mark["oversize_doc"] = True
    marks = state.get("run_marks") if isinstance(state.get("run_marks"), dict) else {}
    marks[message_id] = mark
    state["run_marks"] = marks
    pending = []
    for item in state.get("pending_messages") or []:
        if isinstance(item, dict) and _string_id(item.get("id")) != message_id:
            pending.append(item)
    if status == "pending":
        snapshot = _read_snapshot(workspace, channel_id, message_id) or {"id": message_id}
        pending.append(
            {
                "id": message_id,
                "status": "pending",
                "source": "ingest",
                "error": mark["error"],
                "outputs": outputs,
                "reason": mark["reason"],
                "doc_title": doc_title,
                "oversize_doc": mark.get("oversize_doc") is True,
                "author_id": snapshot.get("author_id") or "",
                "author_is_bot": snapshot.get("author_is_bot") is True,
                "timestamp": snapshot.get("timestamp") or "",
                "content": snapshot.get("content") or "",
                "attachments": list(snapshot.get("attachments") or []),
                "embeds": list(snapshot.get("embeds") or []),
            }
        )
    pending.sort(key=lambda item: _id_num(item.get("id")))
    state["pending_messages"] = pending
    state["last_run"] = now_hk().isoformat()
    _advance_cursor(state)
    _recompute(state)
    _save_state(workspace, channel_id, state)
    return {
        "ok": True,
        "message_id": message_id,
        "status": status,
        "last_processed_message_id": state["last_processed_message_id"],
        "last_result": state["last_result"],
    }


def _read_snapshot(workspace: Path, channel_id: str, message_id: str) -> dict[str, Any] | None:
    path = workspace_file(workspace, _message_rel(channel_id, message_id))
    if not path.is_file() or path.is_symlink():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def cmd_summary(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    channel_id = _require_channel(args.channel_id)
    state, _err = _load_state(workspace, channel_id)
    now = now_hk()
    run_id = report_run_id(channel_id, now)
    if state is None or not _snowflake_ok(_string_id(state.get("last_processed_message_id"))):
        result = {
            "scanned": 0,
            "ingested": 0,
            "skipped": 0,
            "pending": 0,
            "errors": ["missing_cursor"],
            "outputs": [],
        }
        return {
            "ok": True,
            "quiet": False,
            "status": "missing_cursor",
            "exists": state is not None,
            "text": "还没有导入游标，这一轮没有读取频道。",
            "report_run_id": run_id,
            "continue_after": "",
            "oversize_docs": [],
            "last_result": result,
        }
    state["last_run"] = now.isoformat()
    _recompute(state)
    _save_state(workspace, channel_id, state)
    return {
        "ok": True,
        "quiet": _quiet(state),
        "text": _summary_text(state),
        "report_run_id": run_id,
        "continue_after": _continue_after(state),
        "oversize_docs": _oversize_docs(state),
        "last_processed_message_id": state["last_processed_message_id"],
        "last_result": state["last_result"],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discord wiki auto-ingest helper")
    parser.add_argument("--workspace-dir", default=None)
    parser.add_argument("--json", action="store_true")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    shared.add_argument("--channel-id", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    state = sub.add_parser("state", parents=[shared])
    state.set_defaults(func=cmd_state)

    fetch = sub.add_parser("fetch", parents=[shared])
    group = fetch.add_mutually_exclusive_group(required=True)
    group.add_argument("--sender-id")
    group.add_argument("--all-non-bot", action="store_true")
    fetch.set_defaults(func=cmd_fetch)

    download = sub.add_parser("download", parents=[shared])
    download.add_argument("--message-id", required=True)
    download.set_defaults(func=cmd_download)

    mark = sub.add_parser("mark", parents=[shared])
    mark.add_argument("--message-id", required=True)
    mark.add_argument("--status", required=True)
    mark.add_argument("--reason", default="")
    mark.add_argument("--error", default="")
    mark.add_argument("--doc-title", default="")
    mark.add_argument("--output", action="append")
    mark.set_defaults(func=cmd_mark)

    summary = sub.add_parser("summary", parents=[shared])
    summary.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit:
        return 2
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        result = {"ok": False, "error": _public_error(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
