#!/usr/bin/env python3
"""CT semiconductor sentiment helper: New York window, filter, and log.

The script does not fetch posts and does not score sentiment. It filters a
saved tool payload to the strict 24-hour window, checks the plain-text draft,
and appends one log line only after a confirmed text delivery.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
LOG_REL = "state/ct-semi-sentiment/sentiment.log"
HANDLES = (
    ("xiaomustock", "川沐"),
    ("nft_hu", "Fiona"),
    ("octopusycc", "大老师"),
    ("bitfool1", "比特傻"),
    ("blknoiz06", "Ansem"),
    ("notthreadguy", "ThreadGuy"),
    ("ShanghaoJin", "Herman Jin"),
    ("degentradingLSD", ""),
    ("qinbafrank", ""),
    ("bboczeng", "勃勃OC"),
)
HANDLE_ORDER = tuple(handle for handle, _label in HANDLES)
LABELS = dict(HANDLES)
X_QUERY = (
    "半导体、存储、HBM、CPO、semicap 设备、AI 算力与 Neocloud、相关个股、期权流和仓位动作。"
    "返回原帖 URL、发布时间和作者；图或视频里的关键信息一并返回，没有则说明没有。"
)
CHAR_LIMIT = 1700
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LINE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) \| (?P<score>10|[1-9])/10 \| (?P<note>\S(?:.*\S)?|\S)$")
_STATUS_URL_RE = re.compile(r"^https://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/\d+$")
_TWITTER_RE = re.compile(
    r"^[A-Za-z]{3} (?P<mon>[A-Za-z]{3}) (?P<day>\d{1,2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}) "
    r"(?P<sign>[+-])(?P<tzh>\d{2})(?P<tzm>\d{2}) (?P<year>\d{4})$"
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_SECTIONS = (
    "【共识主线】",
    "【情绪温度】",
    "【分歧点】",
    "【个股/仓位信号】",
    "【数据备注】",
)
_NOTE_LABELS = (
    "时间线抓取失败账号",
    "过去24小时无新推账号",
    "有新推但无半导体相关观点账号",
    "SuperGrok X Search 补充状态",
)
_MEDIA_KEYS = ("image_understanding", "video_understanding", "image_note", "video_note")
_NEEDLES = (
    "o" + "c_",
    "disc" + "ord",
    "fei" + "shu",
    "chat" + "_id",
    "/Users" + "/",
    "file:" + "//",
)
_CANONICAL = {handle.casefold(): handle for handle in HANDLE_ORDER}


def require_workspace(raw: str | None) -> Path:
    if not raw:
        raise RuntimeError("missing --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        raise RuntimeError("workspace is a symlink")
    if not path.is_dir():
        raise RuntimeError("workspace not found")
    return path.resolve()


def workspace_file(workspace: Path, relative: str) -> Path:
    """Resolve a workspace-relative file and reject links and path escape."""
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or any(part in ("", ".", "..") for part in raw.parts):
        raise ValueError("path must stay inside the workspace")
    path = workspace.joinpath(*raw.parts)
    if path.is_symlink():
        raise ValueError("refusing symlink")
    parent = path.parent.resolve()
    root = workspace.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("path must stay inside the workspace")
    return path


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("incomplete write")
        view = view[written:]


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    """Replace ``path`` via a new temp file so an existing symlink is not followed."""
    if path.is_symlink():
        raise ValueError("refusing symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
        os.replace(name, path)
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _has_recipient(text: str) -> bool:
    return any(needle in text for needle in _NEEDLES)


def _parse_as_of(as_of: str | None) -> datetime:
    if not as_of:
        return datetime.now(TZ)
    text = as_of.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("invalid as-of") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def compute_window(as_of: str | None = None) -> dict[str, Any]:
    end = _parse_as_of(as_of)
    # Local subtraction is 23 or 25 hours across a New York DST change.
    start = (end.astimezone(UTC) - timedelta(hours=24)).astimezone(TZ)
    summary_date = end.date().isoformat()
    handles = [{"handle": handle, "label": label} for handle, label in HANDLES]
    return {
        "timezone": "America/New_York",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "summary_date": summary_date,
        "report_run_id": f"ct-semi-sentiment-{summary_date}",
        "x_query": X_QUERY,
        "handles": handles,
        "x_handles": [item["handle"] for item in handles],
    }


def _valid_date(value: str) -> bool:
    if not _DATE_RE.fullmatch(value or ""):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def validate_line(line: str, summary_date: str) -> str | None:
    """Return the stored log line, or None when the line is not one temperature record."""
    if not isinstance(line, str) or not _valid_date(summary_date):
        return None
    text = line.strip()
    if "\n" in text or "\r" in text or len(text) > 500 or _has_recipient(text):
        return None
    match = _LINE_RE.fullmatch(text)
    if match is None or match.group("date") != summary_date:
        return None
    if not _valid_date(match.group("date")):
        return None
    return text


def _read_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("refusing symlink")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("log is not text") from exc


def _nonempty_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _prior_exists(text: str, summary_date: str) -> bool:
    prefix = summary_date + " "
    return any(line.strip() and not line.strip().startswith(prefix) for line in text.splitlines())


def validate_report(text: str, summary_date: str, *, require_change: bool) -> list[str]:
    """Return structural problems. An empty list means the draft can be submitted."""
    errors: list[str] = []
    if not isinstance(text, str):
        return ["正文必须是文本"]
    if not _valid_date(summary_date):
        return ["日期格式应为 YYYY-MM-DD"]
    body = text.replace("\r\n", "\n").strip()
    if _has_recipient(body):
        return ["正文含有接收方痕迹"]
    if len(body) > CHAR_LIMIT:
        errors.append(f"正文超过 {CHAR_LIMIT} 个字符，先压缩【个股/仓位信号】")
    lines = body.splitlines()
    title = f"🧭 CT半导体情绪日报（{summary_date}）"
    if not lines or lines[0].strip() != title:
        errors.append(f"首行必须是「{title}」")
    positions = [body.find(label) for label in _SECTIONS]
    if any(pos < 0 for pos in positions) or positions != sorted(positions):
        errors.append("栏目顺序要按共识主线、情绪温度、分歧点、个股/仓位信号、数据备注")
    temperature = body
    if positions[1] >= 0 and positions[2] > positions[1]:
        temperature = body[positions[1] : positions[2]]
    has_score = re.search(r"(?:10|[1-9])/10", temperature) is not None
    if not has_score and "本轮无法评分" not in temperature:
        errors.append("情绪温度要写 N/10，或写本轮无法评分")
    if require_change and has_score and not any(mark in temperature for mark in ("↑", "↓", "→")):
        errors.append("已有更早的温度记录，正文要写出 ↑、↓ 或 →")
    notes_at = body.find("【数据备注】")
    notes = body[notes_at:] if notes_at >= 0 else ""
    missing = [label for label in _NOTE_LABELS if label not in notes]
    if missing:
        errors.append("数据备注要分开列出：" + "、".join(missing))
    return errors


def _parse_twitter(text: str) -> datetime | None:
    """Parse a Twitter createdAt without using the process locale or the weekday token."""
    match = _TWITTER_RE.fullmatch(text)
    if match is None:
        return None
    month = _MONTHS.get(match.group("mon").casefold())
    if month is None:
        return None
    sign = 1 if match.group("sign") == "+" else -1
    tz = timezone(sign * timedelta(hours=int(match.group("tzh")), minutes=int(match.group("tzm"))))
    try:
        parsed = datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=tz,
        )
    except ValueError:
        return None
    return parsed.astimezone(TZ)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ)
    return _parse_twitter(text)


def _iter_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "tweets"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                return value["items"]
    return []


def _handle(item: dict[str, Any], default: str | None) -> str:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    raw = (
        item.get("author_handle")
        or item.get("author")
        or item.get("handle")
        or user.get("screenName")
        or user.get("screen_name")
        or default
        or ""
    )
    return str(raw).strip().removeprefix("@")


def _status_id(item: dict[str, Any], url: str) -> str:
    for key in ("id", "id_str", "rest_id", "tweet_id"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    match = re.search(r"/status/(\d+)", url)
    return match.group(1) if match else ""


def _canonical(handle: str) -> str | None:
    return _CANONICAL.get(handle.strip().removeprefix("@").casefold())


def _normalize(item: Any, default_handle: str | None, *, require_source_url: bool) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    handle = _canonical(_handle(item, default_handle))
    url = str(item.get("url") or "").strip()
    status_id = _status_id(item, url)
    if require_source_url and not _STATUS_URL_RE.match(url):
        return None
    if not handle or not status_id:
        return None
    if not url:
        url = f"https://x.com/{handle}/status/{status_id}"
    created = _parse_time(item.get("created_at") or item.get("createdAt") or item.get("date"))
    text = str(item.get("text") or item.get("full_text") or item.get("summary_excerpt") or "").strip()
    quoted = item.get("quoted_id") or item.get("quotedId") or item.get("quoted_status_id")
    media = {
        key: str(item[key]).strip()
        for key in _MEDIA_KEYS
        if isinstance(item.get(key), str) and str(item.get(key)).strip()
    }
    return {
        "id": status_id,
        "url": url,
        "handle": handle,
        "created_at": created.isoformat() if created is not None else "",
        "created": created,
        "text": text,
        "is_repost": bool(item.get("is_repost") or item.get("isRetweet") or item.get("is_retweet")),
        "quoted_id": str(quoted).strip() if quoted else "",
        "media": media,
    }


def _pure_repost(item: dict[str, Any]) -> bool:
    if item["text"].casefold().startswith("rt @"):
        return True
    return bool(item["is_repost"] and not item["quoted_id"])


def _in_window(created: datetime | None, start: datetime, end: datetime) -> bool:
    return created is not None and start <= created <= end


def _account_payload(raw: Any) -> tuple[bool, list[Any], bool]:
    if isinstance(raw, list):
        return True, raw, False
    if not isinstance(raw, dict):
        return False, [], False
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    has_more = bool(raw.get("has_more") or raw.get("hasMore") or data.get("has_more") or data.get("hasMore"))
    if "ok" in raw:
        return bool(raw.get("ok")), _iter_items(raw), has_more
    return True, _iter_items(raw), has_more


def _public_item(item: dict[str, Any], source: str) -> dict[str, str]:
    public = {
        "id": item["id"],
        "url": item["url"],
        "created_at": item["created_at"],
        "text": item["text"],
        "source": source,
    }
    public.update(item["media"])
    return public


def _remember(
    item: dict[str, Any],
    source: str,
    seen_ids: set[str],
    seen_urls: set[str],
    by_id: dict[str, dict[str, Any]],
    by_url: dict[str, dict[str, Any]],
) -> None:
    item["source"] = source
    seen_ids.add(item["id"])
    by_id[item["id"]] = item
    if item["url"]:
        seen_urls.add(item["url"])
        by_url[item["url"]] = item


def _find_existing(
    item: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    by_url: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    found = by_id.get(item["id"])
    if found is not None:
        return found
    if item["url"]:
        return by_url.get(item["url"])
    return None


def _merge_supplement(existing: dict[str, Any], supplement: dict[str, Any]) -> None:
    """Keep one post and fold new media notes and excerpt context into it."""
    changed = False
    for key, value in supplement["media"].items():
        current = existing["media"].get(key, "")
        if not value or value == current or value in current.split("\n"):
            continue
        existing["media"][key] = f"{current}\n{value}" if current else value
        changed = True
    extra = supplement["text"]
    if extra and extra not in existing["text"]:
        existing["text"] = f"{existing['text']}\n{extra}" if existing["text"] else extra
        changed = True
    if changed and existing.get("source") == "timeline":
        existing["source"] = "timeline+x_search"
    if supplement["url"] and not existing["url"]:
        existing["url"] = supplement["url"]


def select_posts(raw: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    start = datetime.fromisoformat(window["start_iso"])
    end = datetime.fromisoformat(window["end_iso"])
    accounts_in: dict[str, Any] = {}
    incoming = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
    for key, value in incoming.items():
        canonical = _canonical(str(key))
        if canonical is not None and canonical not in accounts_in:
            accounts_in[canonical] = value
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    accounts: dict[str, Any] = {}
    for handle in HANDLE_ORDER:
        present = handle in accounts_in
        ok, items, has_more = _account_payload(accounts_in.get(handle))
        if not present:
            ok = False
        kept: list[dict[str, Any]] = []
        if ok:
            for item in items:
                norm = _normalize(item, handle, require_source_url=False)
                if norm is None or norm["handle"] != handle:
                    continue
                if not _in_window(norm["created"], start, end) or _pure_repost(norm):
                    continue
                if norm["id"] in seen_ids or (norm["url"] and norm["url"] in seen_urls):
                    continue
                _remember(norm, "timeline", seen_ids, seen_urls, by_id, by_url)
                kept.append(norm)
        accounts[handle] = {
            "ok": ok,
            "has_more": has_more if ok else False,
            "label": LABELS[handle],
            "items": kept,
        }
    search = raw.get("x_search")
    search_ok = bool(search.get("ok")) if isinstance(search, dict) and "ok" in search else False
    supplemented = 0
    if isinstance(search, dict) and search_ok:
        for item in _iter_items(search):
            norm = _normalize(item, None, require_source_url=True)
            if norm is None or norm["handle"] not in HANDLE_ORDER:
                continue
            if not _in_window(norm["created"], start, end) or _pure_repost(norm):
                continue
            existing = _find_existing(norm, by_id, by_url)
            if existing is not None:
                _merge_supplement(existing, norm)
                seen_ids.add(norm["id"])
                by_id.setdefault(norm["id"], existing)
                if norm["url"]:
                    seen_urls.add(norm["url"])
                    by_url[norm["url"]] = existing
                continue
            _remember(norm, "x_search", seen_ids, seen_urls, by_id, by_url)
            accounts[norm["handle"]]["items"].append(norm)
            supplemented += 1
    failed: list[str] = []
    no_posts: list[str] = []
    with_posts: list[str] = []
    more: list[str] = []
    for handle in HANDLE_ORDER:
        account = accounts[handle]
        account["items"].sort(key=lambda row: (row["created"], row["id"]))
        public_items = [_public_item(row, row["source"]) for row in account["items"]]
        account["items"] = public_items
        if not account["ok"]:
            failed.append(handle)
        if public_items:
            with_posts.append(handle)
        elif account["ok"]:
            no_posts.append(handle)
        if account["has_more"]:
            more.append(handle)
    return {
        "window": window,
        "accounts": accounts,
        "x_search": {"ok": search_ok, "supplemented": supplemented},
        "failed": failed,
        "no_posts": no_posts,
        "with_posts": with_posts,
        "has_more": more,
    }


def _write_text(workspace: Path, relative: str, text: str) -> None:
    path = workspace_file(workspace, relative)
    write_bytes_exclusive(path, text.encode("utf-8"))


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("refusing symlink")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_window(args: argparse.Namespace) -> dict[str, Any]:
    window = compute_window(args.as_of)
    return {"ok": True, "window": window, "report_run_id": window["report_run_id"]}


def cmd_tail(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    relative = args.file or LOG_REL
    path = workspace_file(workspace, relative)
    if not path.exists():
        return {"ok": True, "exists": False, "lines": [], "path": relative}
    lines = _nonempty_lines(_read_text(path))
    return {"ok": True, "exists": True, "lines": lines[-3:], "path": relative}


def cmd_select(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    raw = _read_json(workspace_file(workspace, args.input))
    if not isinstance(raw, dict):
        raise TypeError("select input must be a JSON object")
    selected = select_posts(raw, compute_window(args.as_of))
    _write_text(workspace, args.output, json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
    return {
        "ok": True,
        "path": args.output,
        "failed": selected["failed"],
        "no_posts": selected["no_posts"],
        "with_posts": selected["with_posts"],
        "has_more": selected["has_more"],
        "x_search_ok": selected["x_search"]["ok"],
        "supplemented": selected["x_search"]["supplemented"],
    }


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    path = workspace_file(workspace, args.file)
    if path.is_symlink() or not path.is_file():
        raise ValueError("report file not found")
    text = path.read_text(encoding="utf-8")
    log_path = workspace_file(workspace, LOG_REL)
    require_change = False
    if log_path.exists():
        require_change = _prior_exists(_read_text(log_path), args.date)
    errors = validate_report(text, args.date, require_change=require_change)
    chars = len(text.replace("\r\n", "\n").strip())
    if errors:
        return {"ok": False, "error": "invalid_report", "errors": errors, "chars": chars}
    return {"ok": True, "path": args.file, "chars": chars, "require_change": require_change}


def _confirmed(args: argparse.Namespace) -> bool:
    confirmed = str(args.delivery_confirmed).lower() == "true"
    return confirmed and args.status == "sent" and args.content_kind == "text"


def cmd_append(args: argparse.Namespace) -> dict[str, Any]:
    if not _confirmed(args):
        return {"ok": False, "error": "not_confirmed"}
    line = validate_line(args.line, args.date)
    if line is None:
        return {"ok": False, "error": "invalid_line"}
    workspace = require_workspace(args.workspace_dir)
    relative = args.file or LOG_REL
    path = workspace_file(workspace, relative)
    existing = _read_text(path) if path.exists() else ""
    prefix = args.date + " "
    for current in existing.splitlines():
        stripped = current.strip()
        if not stripped.startswith(prefix):
            continue
        if stripped == line:
            return {"ok": True, "reused": True, "path": relative}
        return {"ok": False, "error": "log_conflict", "path": relative}
    body = existing
    if body and not body.endswith("\n"):
        body += "\n"
    body += line + "\n"
    write_bytes_exclusive(path, body.encode("utf-8"))
    return {"ok": True, "reused": False, "path": relative}


def _build_parser() -> argparse.ArgumentParser:
    # Separate action objects. parents= reuses one object, and set_defaults
    # would replace its SUPPRESS default; the subparser would then write None
    # over a --workspace-dir already parsed before the subcommand.
    parser = argparse.ArgumentParser(description="CT semiconductor sentiment helper")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    window = sub.add_parser("window", parents=[shared])
    window.add_argument("--as-of")
    window.set_defaults(func=cmd_window)

    tail = sub.add_parser("tail", parents=[shared])
    tail.add_argument("--file", default=LOG_REL)
    tail.set_defaults(func=cmd_tail)

    select = sub.add_parser("select", parents=[shared])
    select.add_argument("--input", required=True)
    select.add_argument("--output", required=True)
    select.add_argument("--as-of")
    select.set_defaults(func=cmd_select)

    check = sub.add_parser("check", parents=[shared])
    check.add_argument("--file", required=True)
    check.add_argument("--date", required=True)
    check.set_defaults(func=cmd_check)

    append = sub.add_parser("append", parents=[shared])
    append.add_argument("--delivery-confirmed", required=True)
    append.add_argument("--status", required=True)
    append.add_argument("--content-kind", required=True)
    append.add_argument("--date", required=True)
    append.add_argument("--line", required=True)
    append.add_argument("--file", default=LOG_REL)
    append.set_defaults(func=cmd_append)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
