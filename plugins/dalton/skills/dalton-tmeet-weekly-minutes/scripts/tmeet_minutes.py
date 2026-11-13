"""Pull Tencent Meeting weekly recordings and store the Feishu doc receipt.

The script calls the read-only ``tmeet`` research route. It does not log in,
create meetings, or grant document access.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
STATE_ROOT = "state/tmeet-weekly-minutes"
PENDING_REL = f"{STATE_ROOT}/pending.flag"
FEISHU_DOC_BASE = "https://feishu.cn"
SEARCH_LIMIT = 100
TRANSCRIPT_LIMIT = 100
TRANSCRIPT_PAGE_CAP = 50
SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE_ID_RE = re.compile(r"^[0-9]{1,32}$")
_LANG_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z0-9]{2,8})?$")
_FEISHU_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_FEISHU_URL_RE = re.compile(r"^https://(?:[A-Za-z0-9.-]+\.)?feishu\.cn/\S+$")
_PATH_LEAKS = ("/Users/", "/private/", "/home/")


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
    return _load_script("card_io.py", "card_io_dalton_tmeet_weekly_minutes")


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


def now_shanghai() -> datetime:
    """Clock for the lookback window. Tests replace this function."""
    return datetime.now(SHANGHAI)


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


def load_config() -> dict[str, Any]:
    data = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config.json")
    keyword = data.get("meeting_subject_keyword")
    lookback = data.get("lookback_days")
    lang = data.get("lang") if data.get("lang") is not None else "zh"
    if not isinstance(keyword, str) or not keyword.strip() or len(keyword.strip()) > 200:
        raise ValueError("meeting_subject_keyword")
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1 or lookback > 366:
        raise ValueError("lookback_days")
    if not isinstance(lang, str) or _LANG_RE.fullmatch(lang) is None:
        raise ValueError("lang")
    return {
        "meeting_subject_keyword": keyword.strip(),
        "lookback_days": lookback,
        "lang": lang,
    }


def write_text(workspace: Path, relative: str, text: str) -> None:
    path = workspace_file(workspace, relative)
    write_bytes_exclusive(path, text.encode("utf-8"))


def write_json(workspace: Path, relative: str, payload: Any) -> None:
    write_text(workspace, relative, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def run_dir(meeting_date: str) -> str:
    return f"{STATE_ROOT}/runs/{meeting_date}"


def _require_date(value: str | None) -> str:
    text = str(value or "").strip()
    if _DATE_RE.fullmatch(text) is None:
        raise ValueError("meeting_date")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("meeting_date") from exc
    return text


def _valid_pending_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return _require_date(value)
    except ValueError:
        return None


def _valid_pending_file_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        return _require_file_id(str(value))
    except ValueError:
        return None


def _require_file_id(value: str | None) -> str:
    text = str(value or "").strip()
    if _FILE_ID_RE.fullmatch(text) is None:
        raise ValueError("record_file_id")
    return text


def _bridge_failure(body: dict[str, Any]) -> dict[str, Any]:
    status = str(body.get("status") or "")
    if status == "not_logged_in":
        return {"ok": False, "status": status, "error": "需要在宿主机重新登录 tmeet"}
    hint = str(body.get("hint") or "").strip()
    if any(marker in hint for marker in _PATH_LEAKS):
        hint = ""
    if status == "cli_contract_changed":
        hint = hint or "宿主机 tmeet 输出不再是约定结构"
    return {"ok": False, "status": status, "error": hint or "腾讯会议查询失败"}


def _research(arguments: dict[str, Any]) -> dict[str, Any]:
    body = call_research(**arguments)
    if body.get("ok") is not True:
        return _bridge_failure(body)
    data = body.get("data")
    if not isinstance(data, dict):
        return {"ok": False, "status": "cli_contract_changed", "error": "腾讯会议查询失败"}
    return {"ok": True, "status": "ok", "data": data, "hint": body.get("hint")}


def _parse_instant(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _meeting_sort_key(record: dict[str, Any]) -> datetime:
    parsed = _parse_instant(str(record.get("meeting_start_time") or ""))
    if parsed is not None:
        return parsed
    return datetime.min.replace(tzinfo=SHANGHAI)


def meeting_date_of(record: dict[str, Any]) -> str:
    for key in ("meeting_start_time", "record_start_time"):
        parsed = _parse_instant(str(record.get(key) or ""))
        if parsed is not None:
            return parsed.date().isoformat()
        raw = str(record.get(key) or "")
        if _DATE_RE.fullmatch(raw[:10]):
            return raw[:10]
    raise ValueError("meeting date missing")


def select_meeting(records: list[dict[str, Any]], keyword: str) -> dict[str, Any] | None:
    needle = keyword.casefold()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        subject = str(record.get("subject") or "")
        if needle not in subject.casefold():
            continue
        key = (subject, str(record.get("meeting_start_time") or ""))
        groups.setdefault(key, []).append(record)
    if not groups:
        return None
    winning_key = max(groups, key=lambda item: _meeting_sort_key(groups[item][0]))
    group = groups[winning_key]
    transcripts = [item for item in group if str(item.get("file_type") or "") == "transcript"]
    return transcripts[0] if transcripts else group[0]


def _paragraph_has_text(paragraphs: list[Any]) -> bool:
    for para in paragraphs:
        if not isinstance(para, dict):
            continue
        if str(para.get("text") or "").strip():
            return True
        sentences = para.get("sentences")
        if not isinstance(sentences, list):
            continue
        for sentence in sentences:
            if isinstance(sentence, str) and sentence.strip():
                return True
            if not isinstance(sentence, dict):
                continue
            if str(sentence.get("text") or "").strip():
                return True
            words = sentence.get("words")
            if not isinstance(words, list):
                continue
            for word in words:
                if isinstance(word, str) and word.strip():
                    return True
                if isinstance(word, dict) and str(word.get("text") or "").strip():
                    return True
    return False


def _today_run_id() -> str:
    now = now_shanghai()
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    else:
        now = now.astimezone(SHANGHAI)
    return f"tmeet-weekly-minutes-{now.date().isoformat()}"


def _stamp_run_id(payload: dict[str, Any], meeting_date: str | None = None) -> dict[str, Any]:
    if payload.get("report_run_id"):
        return payload
    stamped = dict(payload)
    if meeting_date:
        stamped["report_run_id"] = f"tmeet-weekly-minutes-{meeting_date}"
    else:
        stamped["report_run_id"] = _today_run_id()
    return stamped


def _not_found() -> dict[str, Any]:
    return {
        "ok": True,
        "found": False,
        "meeting_date": None,
        "report_run_id": _today_run_id(),
        "record_file_id": None,
        "file_type": None,
        "subject": None,
        "run_dir": None,
        "minutes_empty": None,
        "transcript_empty": None,
        "todos_count": 0,
        "transcript_pages": 0,
        "pending_needed": False,
    }


def _check_pids(page: list[Any], expected: int) -> tuple[int, str | None]:
    """Require pid values to start at 0 and increase by 1 with no duplicates."""
    for para in page:
        pid_text = str(para.get("pid") if isinstance(para, dict) else "")
        if not pid_text.isdigit() or int(pid_text) != expected:
            return expected, "转写 pid 不连续或有重复"
        expected += 1
    return expected, None


def _page_transcript(record_file_id: str) -> dict[str, Any]:
    paragraphs: list[Any] = []
    pid = "0"
    pages = 0
    more = True
    expected = 0
    while more and pages < TRANSCRIPT_PAGE_CAP:
        body = _research(
            {
                "route": "tmeet",
                "operation": "record_content",
                "action": "transcript",
                "record_file_id": record_file_id,
                "pid": pid,
                "limit": TRANSCRIPT_LIMIT,
            }
        )
        if body.get("ok") is not True:
            return body
        data = body["data"]
        page = data.get("paragraphs") if isinstance(data.get("paragraphs"), list) else []
        expected, pid_error = _check_pids(page, expected)
        if pid_error:
            return {"ok": False, "status": "transcript_pid_invalid", "error": pid_error}
        paragraphs.extend(page)
        pages += 1
        more = bool(data.get("more"))
        if not more:
            break
        next_pid = data.get("next_pid")
        if not isinstance(next_pid, str) or not next_pid or next_pid == pid:
            return {"ok": False, "status": "cli_contract_changed", "error": "转写还有下一页，但没有下一页起点"}
        if next_pid != str(expected):
            return {"ok": False, "status": "transcript_pid_invalid", "error": "转写 pid 不连续或有重复"}
        pid = next_pid
    if more:
        return {"ok": False, "status": "transcript_too_long", "error": "转写分页超过 50 页，已停止"}
    return {"ok": True, "paragraphs": paragraphs, "pages": pages, "more": False}


def cmd_self_test(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    body = _research({"route": "tmeet", "operation": "auth_status"})
    if body.get("ok") is not True:
        failure = dict(body)
        failure["logged_in"] = False
        failure["report_run_id"] = _today_run_id()
        if body.get("status") == "not_logged_in":
            failure["error"] = "需要在宿主机重新登录 tmeet"
        return failure
    data = body["data"]
    if data.get("logged_in") is not True:
        return {
            "ok": False,
            "logged_in": False,
            "report_run_id": _today_run_id(),
            "error": "需要在宿主机重新登录 tmeet",
        }
    return {
        "ok": True,
        "logged_in": True,
        "refresh_token_days_left": data.get("refresh_token_days_left"),
    }


def _store_meeting(
    workspace: Path,
    config: dict[str, Any],
    *,
    record_file_id: str,
    meeting_date: str,
    chosen: dict[str, Any],
    search_data: dict[str, Any] | None,
) -> dict[str, Any]:
    minutes = _research(
        {
            "route": "tmeet",
            "operation": "record_content",
            "action": "minutes",
            "record_file_id": record_file_id,
            "lang": config["lang"],
        }
    )
    if minutes.get("ok") is not True:
        return _stamp_run_id(minutes, meeting_date)
    todos = _research(
        {
            "route": "tmeet",
            "operation": "record_content",
            "action": "todos",
            "record_file_id": record_file_id,
            "lang": config["lang"],
        }
    )
    if todos.get("ok") is not True:
        return _stamp_run_id(todos, meeting_date)
    transcript = _page_transcript(record_file_id)
    if transcript.get("ok") is not True:
        return _stamp_run_id(transcript, meeting_date)
    minute_text = minutes["data"].get("minute") if isinstance(minutes["data"].get("minute"), str) else ""
    todo_items = todos["data"].get("todos") if isinstance(todos["data"].get("todos"), list) else []
    paragraphs = transcript["paragraphs"]
    minutes_empty = not minute_text.strip()
    transcript_empty = not _paragraph_has_text(paragraphs)
    relative = run_dir(meeting_date)
    result: dict[str, Any] = {
        "ok": True,
        "found": True,
        "meeting_date": meeting_date,
        "report_run_id": f"tmeet-weekly-minutes-{meeting_date}",
        "record_file_id": record_file_id,
        "file_type": chosen.get("file_type") if isinstance(chosen.get("file_type"), str) else None,
        "subject": chosen.get("subject") if isinstance(chosen.get("subject"), str) else None,
        "run_dir": relative,
        "minutes_empty": minutes_empty,
        "transcript_empty": transcript_empty,
        "todos_count": len(todo_items),
        "transcript_pages": transcript["pages"],
        "pending_needed": minutes_empty and transcript_empty,
    }
    if isinstance(search_data, dict) and search_data.get("truncated") is True:
        result["truncated"] = True
    if isinstance(search_data, dict):
        write_json(workspace, f"{relative}/search.json", search_data)
    write_json(workspace, f"{relative}/selected.json", chosen)
    write_json(workspace, f"{relative}/minutes.json", minutes["data"])
    write_json(workspace, f"{relative}/todos.json", todos["data"])
    write_json(
        workspace,
        f"{relative}/transcript.json",
        {
            "record_file_id": record_file_id,
            "pages": transcript["pages"],
            "more": False,
            "paragraphs": paragraphs,
        },
    )
    write_json(workspace, f"{relative}/manifest.json", result)
    return result


def _fetch_from_pending(workspace: Path, config: dict[str, Any]) -> dict[str, Any]:
    info = _inspect_pending(workspace)
    if info["state"] == "missing":
        return {"ok": False, "status": "pending_missing", "error": "没有待重拉的会议标记"}
    if info["state"] != "ok":
        return _stamp_run_id(
            {"ok": False, "status": "pending_unreadable", "error": "待重拉的会议标记已损坏"},
            info.get("meeting_date") if isinstance(info.get("meeting_date"), str) else None,
        )
    meeting_date = info["meeting_date"]
    record_file_id = info["record_file_id"]
    chosen: dict[str, Any] = {"record_file_id": record_file_id}
    selected_rel = f"{run_dir(meeting_date)}/selected.json"
    state, existing = _load_json_file(workspace_file(workspace, selected_rel))
    if (
        state == "present"
        and isinstance(existing, dict)
        and str(existing.get("record_file_id") or "") == record_file_id
    ):
        chosen = existing
    return _store_meeting(
        workspace,
        config,
        record_file_id=record_file_id,
        meeting_date=meeting_date,
        chosen=chosen,
        search_data=None,
    )


def cmd_fetch(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    config = load_config()
    if args.from_pending:
        return _fetch_from_pending(workspace, config)
    now = now_shanghai()
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    else:
        now = now.astimezone(SHANGHAI)
    start = (now - timedelta(days=config["lookback_days"])).replace(microsecond=0)
    end = now.replace(microsecond=0)
    searched = _research(
        {
            "route": "tmeet",
            "operation": "record_search",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "query": config["meeting_subject_keyword"],
            "limit": SEARCH_LIMIT,
        }
    )
    if searched.get("ok") is not True:
        return _stamp_run_id(searched)
    search_data = searched["data"]
    records = search_data.get("records")
    if not isinstance(records, list):
        return _stamp_run_id(
            {"ok": False, "status": "cli_contract_changed", "error": "腾讯会议查询失败"}
        )
    chosen = select_meeting([item for item in records if isinstance(item, dict)], config["meeting_subject_keyword"])
    if chosen is None:
        result = _not_found()
        if search_data.get("truncated") is True:
            result["truncated"] = True
        return result
    try:
        record_file_id = _require_file_id(str(chosen.get("record_file_id") or ""))
        meeting_date = meeting_date_of(chosen)
    except ValueError as exc:
        return _stamp_run_id({"ok": False, "error": _public_error(exc)})
    return _store_meeting(
        workspace,
        config,
        record_file_id=record_file_id,
        meeting_date=meeting_date,
        chosen=chosen,
        search_data=search_data,
    )


def _inspect_pending(workspace: Path) -> dict[str, Any]:
    """Tell a missing flag apart from one that exists but cannot be used."""
    path = workspace_file(workspace, PENDING_REL)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return {"state": "unreadable", "meeting_date": None}
    if not path.is_file():
        return {"state": "missing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"state": "unreadable", "meeting_date": None}
    if not isinstance(data, dict):
        return {"state": "unreadable", "meeting_date": None}
    meeting_date = _valid_pending_date(data.get("meeting_date"))
    record_file_id = _valid_pending_file_id(data.get("record_file_id"))
    if meeting_date is None or record_file_id is None:
        return {"state": "unreadable", "meeting_date": meeting_date}
    return {
        "state": "ok",
        "meeting_date": meeting_date,
        "record_file_id": record_file_id,
        "written_at": data.get("written_at"),
    }


def cmd_pending(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    op = str(args.op or "").strip()
    if op == "check":
        info = _inspect_pending(workspace)
        if info["state"] == "missing":
            return {"ok": True, "exists": False, "path": PENDING_REL}
        if info["state"] != "ok":
            return {"ok": True, "exists": True, "readable": False, "path": PENDING_REL}
        return {
            "ok": True,
            "exists": True,
            "readable": True,
            "path": PENDING_REL,
            "meeting_date": info["meeting_date"],
            "record_file_id": info["record_file_id"],
            "written_at": info.get("written_at"),
        }
    if op == "delete":
        path = workspace_file(workspace, PENDING_REL)
        deleted = False
        if path.is_symlink():
            raise ValueError("refusing symlink")
        if path.is_file():
            path.unlink()
            deleted = True
        return {"ok": True, "exists": False, "deleted": deleted, "path": PENDING_REL}
    if op == "write":
        meeting_date = _require_date(args.meeting_date)
        record_file_id = _require_file_id(args.record_file_id)
        payload = {
            "meeting_date": meeting_date,
            "record_file_id": record_file_id,
            "written_at": now_shanghai().replace(microsecond=0).isoformat(),
        }
        write_json(workspace, PENDING_REL, payload)
        return {"ok": True, "exists": True, "path": PENDING_REL, **payload}
    return {"ok": False, "error": "op must be write, check, or delete"}


def feishu_doc_url(record: dict[str, Any] | None) -> str | None:
    """Prefer an existing https Feishu URL, then a wiki token, then a docx token."""
    if not isinstance(record, dict):
        return None
    for key in ("url", "doc_url", "feishu_url"):
        value = record.get(key)
        if isinstance(value, str) and _FEISHU_URL_RE.fullmatch(value):
            return value
    node = record.get("node_token")
    if isinstance(node, str) and _FEISHU_TOKEN_RE.fullmatch(node):
        return f"{FEISHU_DOC_BASE}/wiki/{node}"
    document_id = record.get("document_id")
    if isinstance(document_id, str) and _FEISHU_TOKEN_RE.fullmatch(document_id):
        return f"{FEISHU_DOC_BASE}/docx/{document_id}"
    return None


def _doc_paths(meeting_date: str) -> tuple[str, str]:
    relative = run_dir(meeting_date)
    return f"{relative}/doc.json", f"{relative}/doc.failed.json"


def _load_json_file(path: Path) -> tuple[str, dict[str, Any] | None]:
    if path.is_symlink():
        raise ValueError("refusing symlink")
    if not path.is_file():
        return "missing", None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unreadable", None
    if not isinstance(record, dict):
        return "unreadable", None
    return "present", record


def _failed_attempts(workspace: Path, failed_rel: str) -> int:
    path = workspace_file(workspace, failed_rel)
    state, record = _load_json_file(path)
    if state != "present" or record is None:
        return 0
    try:
        return int(record.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _doc_status(workspace: Path, meeting_date: str) -> dict[str, Any]:
    doc_rel, failed_rel = _doc_paths(meeting_date)
    state, record = _load_json_file(workspace_file(workspace, doc_rel))
    if state == "unreadable":
        return {
            "ok": True,
            "stored": False,
            "create": False,
            "reason": "doc_record_unreadable",
            "path": doc_rel,
            "url": None,
        }
    if state == "present" and isinstance(record, dict):
        url = feishu_doc_url(record)
        content_complete = record.get("ok") is not False
        payload = {
            "ok": True,
            "stored": False,
            "skipped": True,
            "create": False,
            "content_complete": content_complete,
            "path": doc_rel,
            "url": url,
        }
        if not content_complete:
            payload["reason"] = "content_incomplete"
            if isinstance(record.get("error"), str):
                payload["error"] = record["error"]
        return payload
    failed_state, _failed_record = _load_json_file(workspace_file(workspace, failed_rel))
    if failed_state == "unreadable":
        return {
            "ok": True,
            "stored": False,
            "create": False,
            "reason": "doc_record_unreadable",
            "path": failed_rel,
            "url": None,
        }
    attempts = _failed_attempts(workspace, failed_rel)
    payload: dict[str, Any] = {
        "ok": True,
        "stored": False,
        "create": attempts < 2,
        "path": doc_rel,
        "url": None,
        "attempts": attempts,
    }
    if attempts >= 2:
        payload["reason"] = "create_failed_twice"
    return payload


def _record_failed_attempt(workspace: Path, meeting_date: str, raw: bytes) -> dict[str, Any]:
    _doc_rel, failed_rel = _doc_paths(meeting_date)
    path = workspace_file(workspace, failed_rel)
    state, current = _load_json_file(path)
    if state == "unreadable":
        return {
            "ok": False,
            "stored": False,
            "create": False,
            "reason": "doc_record_unreadable",
            "path": failed_rel,
        }
    attempts = 0
    returns: list[Any] = []
    if state == "present" and current is not None:
        try:
            attempts = int(current.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if isinstance(current.get("returns"), list):
            returns = list(current["returns"])
    attempts += 1
    returns.append(raw.decode("utf-8", "replace"))
    write_json(workspace, failed_rel, {"attempts": attempts, "returns": returns})
    payload: dict[str, Any] = {
        "ok": True,
        "stored": True,
        "skipped": False,
        "create": attempts < 2,
        "path": failed_rel,
        "url": None,
        "attempts": attempts,
    }
    if attempts >= 2:
        payload["reason"] = "create_failed_twice"
    return payload


def save_doc_record(workspace: Path, meeting_date: str, raw: bytes) -> dict[str, Any]:
    doc_rel, _failed_rel = _doc_paths(meeting_date)
    dest = workspace_file(workspace, doc_rel)
    state, existing = _load_json_file(dest)
    if state == "unreadable":
        return {
            "ok": True,
            "stored": False,
            "create": False,
            "reason": "doc_record_unreadable",
            "path": doc_rel,
            "url": None,
        }
    if state == "present" and isinstance(existing, dict):
        url = feishu_doc_url(existing)
        content_complete = existing.get("ok") is not False
        payload = {
            "ok": True,
            "stored": False,
            "skipped": True,
            "create": False,
            "content_complete": content_complete,
            "path": doc_rel,
            "url": url,
        }
        if not content_complete:
            payload["reason"] = "content_incomplete"
            if isinstance(existing.get("error"), str):
                payload["error"] = existing["error"]
        return payload
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "stored": False, "error": "invalid json"}
    if not isinstance(record, dict):
        return {"ok": False, "stored": False, "error": "invalid json"}
    url = feishu_doc_url(record)
    if url is None:
        return _record_failed_attempt(workspace, meeting_date, raw)
    write_bytes_exclusive(dest, raw)
    content_complete = record.get("ok") is not False
    payload = {
        "ok": True,
        "stored": True,
        "skipped": False,
        "create": False,
        "content_complete": content_complete,
        "path": doc_rel,
        "url": url,
    }
    if not content_complete:
        payload["reason"] = "content_incomplete"
        if isinstance(record.get("error"), str):
            payload["error"] = record["error"]
    return payload


def cmd_save_doc(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    meeting_date = _require_date(args.meeting_date)
    if not args.file:
        return _doc_status(workspace, meeting_date)
    source = workspace_file(workspace, args.file)
    if source.is_symlink():
        raise ValueError("refusing symlink")
    return save_doc_record(workspace, meeting_date, source.read_bytes())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tencent Meeting weekly minutes helper")
    parser.add_argument("--workspace-dir", default=None)
    parser.add_argument("--json", action="store_true")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("self-test", parents=[shared])
    probe.set_defaults(func=cmd_self_test)

    fetch = sub.add_parser("fetch", parents=[shared])
    fetch.add_argument("--from-pending", action="store_true")
    fetch.set_defaults(func=cmd_fetch)

    pending = sub.add_parser("pending", parents=[shared])
    pending.add_argument("--op", required=True)
    pending.add_argument("--meeting-date")
    pending.add_argument("--record-file-id")
    pending.set_defaults(func=cmd_pending)

    save = sub.add_parser("save-doc", parents=[shared])
    save.add_argument("--meeting-date", required=True)
    save.add_argument("--file")
    save.set_defaults(func=cmd_save_doc)
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
