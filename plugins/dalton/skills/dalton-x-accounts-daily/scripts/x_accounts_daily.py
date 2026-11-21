#!/usr/bin/env python3
"""X account daily helper: window, select, compose, and card render.

The script does not fetch posts and does not invent summaries. It filters a
saved tool payload to the strict 24-hour Hong Kong window and writes the
markdown skeleton the card builder already knows how to split.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Hong_Kong")
HANDLES = ("aleabitoreddit", "jukan05", "mingchikuo", "citrini")
EMPTY_LINE = "过去 24 小时内没有新推文"
FAILURE_LINE = "抓取失败"
SUCCESS_NOTE = "四个账号均抓取成功；SuperGrok X Search 补充成功"
_LINK_RE = re.compile(r"\s*\[原推\]\([^)]*\)\s*$")
_STATUS_URL_RE = re.compile(r"^https://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/\d+$")


def _card_io() -> Any:
    path = Path(__file__).resolve().parent / "card_io.py"
    name = "card_io_dalton_x_accounts_daily"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("card helper is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def require_workspace(raw: str | None) -> Path:
    if not raw:
        raise RuntimeError("missing --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        raise RuntimeError("workspace is a symlink")
    if not path.is_dir():
        raise RuntimeError("workspace not found")
    return path.resolve()


def compute_window(as_of: str | None = None) -> dict[str, Any]:
    if as_of:
        parsed = datetime.fromisoformat(as_of)
        end = parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    else:
        end = datetime.now(TZ)
    start = end - timedelta(hours=24)
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    return {
        "timezone": "Asia/Hong_Kong",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "start_date": start_utc.date().isoformat(),
        "end_date": end_utc.date().isoformat(),
        "summary_date": end.date().isoformat(),
    }


def _parse_time(value: Any, *, default_tz: ZoneInfo | timezone = TZ) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        return parsed
    try:
        return datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def _iter_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "tweets"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("items", "tweets"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
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
    text = str(raw).strip()
    text = text.removeprefix("@")
    return text


def _status_id(item: dict[str, Any], url: str) -> str:
    for key in ("id", "id_str", "rest_id", "tweet_id"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    match = re.search(r"/status/(\d+)", url)
    return match.group(1) if match else ""


def _normalize(item: Any, default_handle: str | None, *, require_source_url: bool) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    handle = _handle(item, default_handle)
    url = str(item.get("url") or "").strip()
    status_id = _status_id(item, url)
    if not url and not require_source_url and handle and status_id:
        url = f"https://x.com/{handle}/status/{status_id}"
    if require_source_url and not _STATUS_URL_RE.match(url):
        return None
    if not handle or not status_id:
        return None
    created = _parse_time(item.get("created_at") or item.get("createdAt") or item.get("date"))
    text = str(item.get("text") or item.get("full_text") or item.get("summary_excerpt") or "").strip()
    quoted = item.get("quoted_id") or item.get("quotedId") or item.get("quoted_status_id")
    return {
        "id": status_id,
        "url": url,
        "handle": handle,
        "created_at": created.isoformat() if created is not None else "",
        "created": created,
        "text": text,
        "is_repost": bool(item.get("is_repost") or item.get("isRetweet") or item.get("is_retweet")),
        "quoted_id": str(quoted).strip() if quoted else "",
    }


def _pure_repost(item: dict[str, Any]) -> bool:
    if item["text"].casefold().startswith("rt @"):
        return True
    return bool(item["is_repost"] and not item["quoted_id"])


def _in_window(created: datetime | None, start: datetime, end: datetime) -> bool:
    return created is not None and start <= created <= end


def _account_payload(
    raw: Any,
) -> tuple[bool, list[Any], bool, bool, datetime | None, datetime | None, bool, bool]:
    if isinstance(raw, list):
        return True, raw, False, False, None, None, False, False
    if not isinstance(raw, dict):
        return False, [], False, False, None, None, False, False
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    has_more = bool(
        raw.get("has_more")
        or raw.get("hasMore")
        or data.get("has_more")
        or data.get("hasMore")
    )
    truncated = bool(raw.get("truncated") or data.get("truncated"))
    has_fetched_key = any(
        key in container
        for container in (raw, data)
        for key in ("oldest_fetched_at", "oldestFetchedAt")
    )
    has_returned_key = any(
        key in container
        for container in (raw, data)
        for key in ("oldest_returned_at", "oldestReturnedAt")
    )
    oldest_fetched = _parse_time(
        raw.get("oldest_fetched_at")
        or raw.get("oldestFetchedAt")
        or data.get("oldest_fetched_at")
        or data.get("oldestFetchedAt"),
        default_tz=timezone.utc,
    )
    oldest_returned = _parse_time(
        raw.get("oldest_returned_at")
        or raw.get("oldestReturnedAt")
        or data.get("oldest_returned_at")
        or data.get("oldestReturnedAt"),
        default_tz=timezone.utc,
    )
    ok = bool(raw.get("ok")) if "ok" in raw else True
    return (
        ok,
        _iter_items(raw),
        has_more,
        truncated,
        oldest_fetched,
        oldest_returned,
        has_fetched_key,
        has_returned_key,
    )


def _oldest_timeline_item(items: list[Any], handle: str) -> datetime | None:
    oldest_item: datetime | None = None
    for item in items:
        if isinstance(item, dict) and _folded_handle(_handle(item, handle)) == handle:
            created = _item_created(item)
            if created is not None:
                created_utc = created.astimezone(timezone.utc)
                if oldest_item is None or created_utc < oldest_item:
                    oldest_item = created_utc
    return oldest_item


def _evaluate_timeline_coverage(
    *,
    has_more: bool,
    truncated: bool,
    oldest_fetched: datetime | None,
    oldest_returned: datetime | None,
    has_fetched_key: bool,
    has_returned_key: bool,
    items: list[Any],
    handle: str,
    window_start: datetime,
) -> tuple[bool, datetime | None]:
    """Evaluate `(incomplete, display_oldest)` using bridge fields with legacy fallback."""
    start_utc = window_start.astimezone(timezone.utc)
    fetched_utc = oldest_fetched.astimezone(timezone.utc) if oldest_fetched is not None else None
    returned_utc = oldest_returned.astimezone(timezone.utc) if oldest_returned is not None else None
    if not has_fetched_key and not has_returned_key:
        oldest = _oldest_timeline_item(items, handle)
        incomplete = bool((has_more or truncated) and (oldest is None or oldest >= start_utc))
        return incomplete, oldest
    effective_returned = (
        returned_utc if has_returned_key else _oldest_timeline_item(items, handle)
    )
    if truncated:
        covered = effective_returned is not None and effective_returned < start_utc
    elif fetched_utc is None and not has_returned_key:
        fallback_oldest = _oldest_timeline_item(items, handle)
        covered = fallback_oldest is not None and fallback_oldest < start_utc
        effective_returned = effective_returned or fallback_oldest
    else:
        covered = fetched_utc is not None and fetched_utc < start_utc
    incomplete = bool((has_more or truncated) and not covered)
    display_oldest = effective_returned if effective_returned is not None else fetched_utc
    return incomplete, display_oldest


def _item_created(item: Any) -> datetime | None:
    if not isinstance(item, dict):
        return None
    return _parse_time(item.get("created_at") or item.get("createdAt") or item.get("date"))


def _folded_handle(value: str) -> str:
    return value.strip().removeprefix("@").casefold()


def _indexed_accounts(raw: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Index account payloads by lowercase handle and keep the first original spelling."""
    if not isinstance(raw, dict):
        return {}, {}
    found: dict[str, Any] = {}
    names: dict[str, str] = {}
    for key, value in raw.items():
        shown = str(key).strip().removeprefix("@")
        folded = shown.casefold()
        if folded in HANDLES and folded not in found:
            found[folded] = value
            names[folded] = shown
    return found, names


def _remember_name(names: dict[str, str], folded: str, shown: str) -> None:
    current = names.get(folded, folded)
    if current == folded and shown and shown.casefold() == folded and shown != folded:
        names[folded] = shown


def select_posts(raw: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    start = datetime.fromisoformat(window["start_iso"])
    end = datetime.fromisoformat(window["end_iso"])
    accounts_in, names = _indexed_accounts(raw.get("accounts"))
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    accounts: dict[str, Any] = {}
    for handle in HANDLES:
        names.setdefault(handle, handle)
        present = handle in accounts_in
        (
            ok,
            items,
            has_more,
            truncated,
            oldest_fetched,
            oldest_returned,
            has_fetched_key,
            has_returned_key,
        ) = _account_payload(accounts_in.get(handle))
        if not present:
            ok = False
        kept: list[dict[str, Any]] = []
        if ok:
            for item in items:
                norm = _normalize(item, handle, require_source_url=False)
                if norm is None or _folded_handle(norm["handle"]) != handle:
                    continue
                if not _in_window(norm["created"], start, end) or _pure_repost(norm):
                    continue
                if norm["id"] in seen_ids or (norm["url"] and norm["url"] in seen_urls):
                    continue
                seen_ids.add(norm["id"])
                if norm["url"]:
                    seen_urls.add(norm["url"])
                _remember_name(names, handle, norm["handle"])
                kept.append(norm)
        if ok:
            incomplete, display_oldest = _evaluate_timeline_coverage(
                has_more=has_more,
                truncated=truncated,
                oldest_fetched=oldest_fetched,
                oldest_returned=oldest_returned,
                has_fetched_key=has_fetched_key,
                has_returned_key=has_returned_key,
                items=items,
                handle=handle,
                window_start=start,
            )
        else:
            incomplete, display_oldest = False, None
        accounts[handle] = {
            "ok": ok,
            "has_more": has_more if ok else False,
            "truncated": truncated if ok else False,
            "incomplete": incomplete,
            "oldest_fetched_at": (
                oldest_fetched.astimezone(timezone.utc).isoformat()
                if (ok and oldest_fetched is not None)
                else None
            ),
            "oldest_returned_at": (
                oldest_returned.astimezone(timezone.utc).isoformat()
                if (ok and oldest_returned is not None)
                else None
            ),
            "oldest_created_at": display_oldest.astimezone(TZ).isoformat()
            if (ok and display_oldest is not None)
            else "",
            "items": kept,
        }
    search = raw.get("x_search")
    search_ok = bool(search.get("ok")) if isinstance(search, dict) and "ok" in search else False
    if isinstance(search, dict) and search_ok:
        for item in _iter_items(search):
            norm = _normalize(item, None, require_source_url=True)
            if norm is None:
                continue
            folded = _folded_handle(norm["handle"])
            if folded not in HANDLES:
                continue
            if not norm["url"] or not _in_window(norm["created"], start, end) or _pure_repost(norm):
                continue
            if norm["id"] in seen_ids or norm["url"] in seen_urls:
                continue
            seen_ids.add(norm["id"])
            seen_urls.add(norm["url"])
            _remember_name(names, folded, norm["handle"])
            accounts[folded]["items"].append(norm)
    for handle in HANDLES:
        accounts[handle]["items"].sort(key=lambda row: (row["created"], row["id"]))
        accounts[handle]["items"] = [_public_item(row) for row in accounts[handle]["items"]]
        accounts[handle]["display_handle"] = names.get(handle, handle)
    return {"window": window, "accounts": accounts, "x_search": {"ok": search_ok}}


def _public_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": item["id"],
        "url": item["url"],
        "created_at": item["created_at"],
        "text": item["text"],
    }


def _summary_map(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise TypeError("summaries must be a JSON object")
    raw = payload.get("summaries") if "summaries" in payload else payload
    if not isinstance(raw, dict):
        raise TypeError("summaries must map status id to one line")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"missing summary for {key}")
        out[str(key)] = _LINK_RE.sub("", value).strip()
    return out


def _account_coverage(
    account: dict[str, Any],
    window_start: datetime | None,
) -> tuple[bool, datetime | None]:
    if not account.get("ok"):
        return False, None
    oldest_returned = _parse_time(account.get("oldest_returned_at"), default_tz=timezone.utc)
    oldest_fetched = _parse_time(account.get("oldest_fetched_at"), default_tz=timezone.utc)
    oldest_created = _parse_time(account.get("oldest_created_at"))
    display_oldest = oldest_returned or oldest_fetched or oldest_created
    truncated = bool(account.get("truncated"))
    if "incomplete" in account:
        return bool(account.get("incomplete")), display_oldest
    # Legacy selected.json (from older select_posts) already merged x_search into `items`;
    # never inspect `items` here so supplemented posts cannot masquerade as timeline reach.
    has_more = bool(account.get("has_more") or truncated)
    if window_start is None:
        return has_more, display_oldest
    has_fetched_key = "oldest_fetched_at" in account or "oldestFetchedAt" in account
    has_returned_key = "oldest_returned_at" in account or "oldestReturnedAt" in account
    start_utc = window_start.astimezone(timezone.utc)
    if not has_fetched_key and not has_returned_key:
        covered = oldest_created is not None and oldest_created.astimezone(timezone.utc) < start_utc
        return bool(has_more and not covered), display_oldest
    if truncated:
        covered = (
            oldest_returned is not None and oldest_returned.astimezone(timezone.utc) < start_utc
        )
    else:
        covered = (
            oldest_fetched is not None and oldest_fetched.astimezone(timezone.utc) < start_utc
        )
    incomplete = bool(has_more and not covered)
    return incomplete, display_oldest


def _incomplete_note(handle: str, oldest: datetime | None) -> str:
    prefix = "窗口内可能还有未取回的帖子"
    if oldest is None:
        return f"- @{handle} {prefix}"
    earliest = oldest.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
    return f"- @{handle} {prefix}，最早只取到 {earliest}（香港时间）"


def compose_markdown(selected: dict[str, Any], summaries: dict[str, str]) -> dict[str, Any]:
    window = selected.get("window") or {}
    summary_date = str(window.get("summary_date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", summary_date):
        raise RuntimeError("selected file has no summary date")
    window_start = _parse_time(window.get("start_iso"))
    accounts = selected.get("accounts") or {}
    search_ok = bool((selected.get("x_search") or {}).get("ok"))
    title = f"📋 推特每日总结 — {summary_date}"
    lines = [f"# {title}", ""]
    all_ok = True
    any_incomplete = False
    coverage: dict[str, tuple[bool, datetime | None]] = {}
    for handle in HANDLES:
        account = accounts.get(handle) or {}
        ok = bool(account.get("ok"))
        all_ok = all_ok and ok
        incomplete, oldest = _account_coverage(account, window_start)
        coverage[handle] = (incomplete, oldest)
        any_incomplete = any_incomplete or incomplete
        shown = str(account.get("display_handle") or handle)
        lines.extend(["", f"## @{shown}", ""])
        items = account.get("items") or []
        if not ok and not items:
            lines.append(FAILURE_LINE)
            continue
        if ok and not items:
            lines.append(EMPTY_LINE)
            continue
        for index, item in enumerate(items, start=1):
            status_id = str(item.get("id") or "")
            summary = summaries.get(status_id, "")
            if not summary:
                raise RuntimeError(f"missing summary for {status_id}")
            url = str(item.get("url") or "")
            if url:
                lines.append(f"{index}. {summary} [原推]({url})")
            else:
                lines.append(f"{index}. {summary}")
    notes = [f"- 严格 24 小时窗口：{window.get('start_iso')} 至 {window.get('end_iso')}（Asia/Hong_Kong）"]
    if all_ok and search_ok and not any_incomplete:
        notes.append(f"- {SUCCESS_NOTE}")
    else:
        for handle in HANDLES:
            account = accounts.get(handle) or {}
            incomplete, oldest = coverage[handle]
            if account.get("ok") and incomplete:
                notes.append(_incomplete_note(handle, oldest))
            elif account.get("ok"):
                notes.append(f"- @{handle} 时间线抓取成功")
            else:
                notes.append(f"- @{handle} 抓取失败")
        notes.append("- 补充检索成功" if search_ok else "- 补充检索失败，日报采用四个账号的时间线")
    lines.extend(["", "## 数据备注", "", *notes, ""])
    return {"title": title, "markdown": "\n".join(lines).rstrip() + "\n"}


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("refusing to read a missing file or a symlink")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(workspace: Path, relative: str, text: str) -> None:
    """Write text only after the resolved parent is inside the workspace."""
    path = _card_io().workspace_file(workspace, relative)
    _card_io().write_bytes_exclusive(path, text.encode("utf-8"))


def cmd_window(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, "window": compute_window(args.as_of)}


def cmd_select(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    source = _card_io().workspace_file(workspace, args.input)
    raw = _read_json(source)
    if not isinstance(raw, dict):
        raise TypeError("select input must be a JSON object")
    selected = select_posts(raw, compute_window(args.as_of))
    _write_text(workspace, args.output, json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "path": args.output, "selected": selected}


def cmd_compose(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    selected = _read_json(_card_io().workspace_file(workspace, args.selected))
    summaries = _summary_map(_read_json(_card_io().workspace_file(workspace, args.summaries)))
    if not isinstance(selected, dict):
        raise TypeError("selected file must be a JSON object")
    composed = compose_markdown(selected, summaries)
    _write_text(workspace, args.output, composed["markdown"])
    return {"ok": True, "path": args.output, "title": composed["title"]}


def cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    title = args.title
    if not title:
        text = _card_io().workspace_file(workspace, args.file).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("# ") and not line.startswith("## "):
                title = line[2:].strip()
                break
    result = _card_io().render_markdown(
        workspace=workspace,
        markdown_rel=args.file,
        output_rel=args.output,
        title=title or "",
    )
    return result


def _build_parser() -> argparse.ArgumentParser:
    # Separate action objects. parents= reuses one object, and set_defaults
    # would replace its SUPPRESS default; the subparser would then write None
    # over a --workspace-dir already parsed before the subcommand.
    parser = argparse.ArgumentParser(description="X account daily helper")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    window = sub.add_parser("window", parents=[shared])
    window.add_argument("--as-of")
    window.set_defaults(func=cmd_window)
    select = sub.add_parser("select", parents=[shared])
    select.add_argument("--input", required=True)
    select.add_argument("--output", required=True)
    select.add_argument("--as-of")
    select.set_defaults(func=cmd_select)
    compose = sub.add_parser("compose", parents=[shared])
    compose.add_argument("--selected", required=True)
    compose.add_argument("--summaries", required=True)
    compose.add_argument("--output", required=True)
    compose.set_defaults(func=cmd_compose)
    render = sub.add_parser("render", parents=[shared])
    render.add_argument("--file", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--title")
    render.set_defaults(func=cmd_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
