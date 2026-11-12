#!/usr/bin/env python3
"""Weekly trend scan helper: Hong Kong window and plain-text check.

The script does not search the web and does not write the analysis. It only
names the seven-day window and checks that a draft keeps the required sections.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Hong_Kong")
QUERIES = (
    ("AI与科技产品", "AI 产品 采用 中概股 港股 A股"),
    ("政策监管", "产业政策 监管 中国 香港"),
    ("中美贸易", "中美贸易 关税 出口管制"),
    ("行业竞争与需求", "行业竞争 需求 价格 中概股 港股 A股"),
    ("利率商品与地缘", "利率 原油 地缘政治 中国 香港"),
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEADING_RE = re.compile(r"^\s*#{1,6}(?:\s|$)")
_TREND_RE = re.compile(r"^趋势 (\d+)：", re.MULTILINE)
_TREND_COLUMNS = ("承压方向", "近期股价", "受益方向", "近期股价", "主要不确定性")
_COLUMN_LABELS = ("承压方向", "受益方向", "近期股价", "主要不确定性")
_NEEDLES = (
    "o" + "c_",
    "disc" + "ord",
    "fei" + "shu",
    "chat" + "_id",
    "/Users" + "/",
    "file:" + "//",
)


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
    # UTC subtraction keeps a 7×24 hour span if the zone offset ever changes.
    start = (end.astimezone(UTC) - timedelta(days=7)).astimezone(TZ)
    report_date = end.date().isoformat()
    return {
        "timezone": "Asia/Hong_Kong",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "report_date": report_date,
        "report_run_id": f"weekly-trend-impact-scan-{report_date}",
        "queries": [{"scope": scope, "query": query} for scope, query in QUERIES],
    }


def _has_recipient(text: str) -> bool:
    return any(needle in text for needle in _NEEDLES)


def _column_order(span: str) -> list[str]:
    found: list[str] = []
    for line in span.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("趋势 "):
            continue
        for label in _COLUMN_LABELS:
            if stripped.startswith(label):
                found.append(label)
                break
    return found


def validate_text(text: str, report_date: str) -> list[str]:
    """Return structural problems. An empty list means the draft can be submitted."""
    errors: list[str] = []
    if not isinstance(text, str):
        return ["正文必须是文本"]
    if not _DATE_RE.fullmatch(report_date or ""):
        return ["日期格式应为 YYYY-MM-DD"]
    body = text.replace("\r\n", "\n").strip()
    if _has_recipient(body):
        return ["正文含有接收方痕迹"]
    lines = body.splitlines()
    title = f"📊 本周趋势影响扫描 — {report_date}"
    if not lines or lines[0].strip() != title:
        errors.append(f"首行必须是「{title}」")
    for line in lines:
        if _HEADING_RE.match(line):
            errors.append("不用 Markdown 标题")
            break
    if "事实" not in body or "传导判断" not in body:
        errors.append("用「事实」和「传导判断」分开写")
    watch_at = body.find("本周更值得跟踪的方向")
    if watch_at < 0:
        errors.append("缺少「本周更值得跟踪的方向」")
        trends_part = body
        watch = ""
    else:
        trends_part = body[:watch_at]
        watch = body[watch_at:]
    if "偏空观察" not in watch or "偏多观察" not in watch:
        errors.append("跟踪方向要同时有偏空观察和偏多观察")
    headers = list(_TREND_RE.finditer(trends_part))
    numbers = [int(item.group(1)) for item in headers]
    if numbers != list(range(1, len(numbers) + 1)) or not 3 <= len(numbers) <= 5:
        errors.append("趋势要按 1 开始连续写 3 到 5 项")
        return errors
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(trends_part)
        span = trends_part[header.start() : end]
        number = numbers[index]
        if _column_order(span) != list(_TREND_COLUMNS):
            errors.append(
                f"趋势 {number} 的栏目顺序应为承压方向、近期股价、受益方向、近期股价、主要不确定性"
            )
    return errors


def cmd_window(args: argparse.Namespace) -> dict[str, Any]:
    window = compute_window(args.as_of)
    return {"ok": True, "window": window, "report_run_id": window["report_run_id"]}


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    path = workspace_file(workspace, args.file)
    if not path.is_file():
        raise ValueError("report file not found")
    text = path.read_text(encoding="utf-8")
    errors = validate_text(text, args.date)
    if errors:
        return {
            "ok": False,
            "error": "invalid_report",
            "errors": errors,
            "chars": len(text.replace("\r\n", "\n").strip()),
        }
    return {
        "ok": True,
        "path": args.file,
        "chars": len(text.replace("\r\n", "\n").strip()),
        "trends": len(_TREND_RE.findall(text)),
    }


def _build_parser() -> argparse.ArgumentParser:
    # Separate action objects. parents= reuses one object, and set_defaults
    # would replace its SUPPRESS default; the subparser would then write None
    # over a --workspace-dir already parsed before the subcommand.
    parser = argparse.ArgumentParser(description="Weekly trend impact scan helper")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    window = sub.add_parser("window", parents=[shared])
    window.add_argument("--as-of")
    window.set_defaults(func=cmd_window)
    check = sub.add_parser("check", parents=[shared])
    check.add_argument("--file", required=True)
    check.add_argument("--date", required=True)
    check.set_defaults(func=cmd_check)
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
