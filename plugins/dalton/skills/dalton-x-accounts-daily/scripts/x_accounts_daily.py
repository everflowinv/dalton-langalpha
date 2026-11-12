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
from datetime import datetime, timedelta
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
    return {
        "timezone": "Asia/Hong_Kong",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "summary_date": end.date().isoformat(),
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
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


def _account_payload(raw: Any) -> tuple[bool, list[Any], bool]:
    if isinstance(raw, list):
        return True, raw, False
    if not isinstance(raw, dict):
        return False, [], False
    has_more = bool(raw.get("has_more") or raw.get("hasMore"))
    if "ok" in raw:
        return bool(raw.get("ok")), _iter_items(raw), has_more
    return True, _iter_items(raw), has_more


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
        ok, items, has_more = _account_payload(accounts_in.get(handle))
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
        accounts[handle] = {
            "ok": ok,
            "has_more": has_more if ok else False,
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


def compose_markdown(selected: dict[str, Any], summaries: dict[str, str]) -> dict[str, Any]:
    window = selected.get("window") or {}
    summary_date = str(window.get("summary_date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", summary_date):
        raise RuntimeError("selected file has no summary date")
    accounts = selected.get("accounts") or {}
    search_ok = bool((selected.get("x_search") or {}).get("ok"))
    title = f"📋 推特每日总结 — {summary_date}"
    lines = [f"# {title}", ""]
    all_ok = True
    any_more = False
    for handle in HANDLES:
        account = accounts.get(handle) or {}
        ok = bool(account.get("ok"))
        all_ok = all_ok and ok
        any_more = any_more or bool(account.get("has_more"))
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
    if all_ok and search_ok and not any_more:
        notes.append(f"- {SUCCESS_NOTE}")
    else:
        for handle in HANDLES:
            account = accounts.get(handle) or {}
            if account.get("ok") and account.get("has_more"):
                notes.append(f"- @{handle} 时间线已返回，窗口内还有未取回的帖子")
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
