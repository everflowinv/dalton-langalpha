#!/usr/bin/env python3
"""Screen A-share earnings filings saved by the agent.

The agent fetches each name with finance(route="cn-hk", operation="filings").
This script applies the title screen and the UTC+8 announcement date. It does
not open a network connection. The start and end dates are a closed interval:
a filing on the end date stays, and the next day is dropped.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
UNIVERSE_FILE = SKILL_DIR / "references" / "universe.json"
CN_TZ = timezone(timedelta(hours=8))
# 2026-08-14 09:30 CST = 2026-08-14 01:30 UTC.
_CN_DATE_PROBE_MS = 1786671000000

TITLE_RE = re.compile(
    r"(半年度报告|年度报告|季度报告|中期报告|业绩快报|业绩预告|业绩预增|业绩预减|"
    r"业绩预盈|业绩预亏|财务报告|经营数据)"
)
DROP_RE = re.compile(r"(说明会|摘要|问询|监事|独立董事|审计委员会|募集资金|社会责任)")


class CliExit(SystemExit):
    """Exit the CLI after a JSON error has already been printed."""


def _card_io():
    path = HERE / "card_io.py"
    name = "card_io_dalton_tob_industry_weekly_cninfo"
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


def emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def fail(msg: str, hint: str, extra: dict | None = None) -> None:
    out = {"ok": False, "error": msg, "hint": hint}
    if extra:
        out.update(extra)
    emit(out)
    raise CliExit(1)


def cn_date(ms: float) -> str:
    """Convert a cninfo millisecond timestamp to a UTC+8 calendar date."""
    moment = datetime.fromtimestamp(ms / 1000, UTC).astimezone(CN_TZ)
    return moment.strftime("%Y-%m-%d")


def code_key(ticker: str) -> str:
    base = str(ticker).split(".")[0].strip()
    if base.isdigit():
        return base.lstrip("0") or "0"
    return base.upper()


def load_universe() -> dict:
    if UNIVERSE_FILE.is_symlink() or SKILL_DIR.joinpath("references").is_symlink():
        raise ValueError("refusing symlink")
    return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))


def a_share_rows() -> list[dict]:
    raw = load_universe()
    return [row for row in raw["companies"] if row.get("market") == "A"]


_FORMAT_ERROR = (
    "a-filings.json 格式不对：顶层需要 companies 字典，"
    "键是 6 位代码，值是 filings 返回组成的列表"
)


_ROW_KEYS = ("rows", "filings", "items", "results", "data")


def _call_rows(call: dict) -> list[dict]:
    """Rows from one finance filings return.

    Same envelope keys as the cn-hk probe: rows, filings, items, results or
    data, at the top level or inside a ``data`` object.
    """
    sources = [call]
    inner = call.get("data")
    if isinstance(inner, dict):
        sources.append(inner)
    for source in sources:
        for key in _ROW_KEYS:
            found = source.get(key)
            if isinstance(found, list) and found:
                return [item for item in found if isinstance(item, dict)]
    return []


def _call_failure(call) -> str | None:
    """None when this call succeeded. Any other value is an error string."""
    if not isinstance(call, dict):
        return "返回不是对象"
    inner = call.get("data")
    if isinstance(inner, dict) and (inner.get("ok") is False or inner.get("isError") is True):
        for key in ("error", "message", "detail"):
            text = inner.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
        return "data.ok 是 false"
    if call.get("ok") is True:
        return None
    for key in ("error", "message", "detail"):
        text = call.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return "ok 不是 true"


def _blank_screen(start: str, end: str, error: str) -> dict:
    return {
        "ok": False,
        "incomplete": True,
        "error": error,
        "window": f"{start}~{end}",
        "closed_interval": True,
        "scanned": 0,
        "scanned_rows": 0,
        "errors": [],
        "error_details": {},
        "missing": [],
        "hit_companies": 0,
        "dropped_outside_window": 0,
        "unmatched": [],
        "companies": {},
    }


def _company_calls(payload, pool: list[dict]):
    """Classify the one accepted a-filings shape.

    Returns None when the top-level object has no companies dict.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("companies"), dict):
        return None
    by_code = {code_key(row["ticker"]): row for row in pool}
    state: dict[str, dict] = {}
    unmatched: list[str] = []
    for key, calls in payload["companies"].items():
        known = by_code.get(code_key(str(key)))
        if known is None:
            unmatched.append(str(key))
            continue
        ticker = known["ticker"]
        slot = state.setdefault(ticker, {"errors": [], "rows": [], "ok": False})
        if not isinstance(calls, list):
            slot["errors"] = ["值不是 filings 返回列表"]
            slot["rows"] = []
            slot["ok"] = False
            continue
        if not calls:
            continue
        failed = []
        for call in calls:
            text = _call_failure(call)
            if text is not None and text not in failed:
                failed.append(text)
        if failed or slot["errors"]:
            slot["errors"] = failed or slot["errors"]
            slot["rows"] = []
            slot["ok"] = False
            continue
        slot["ok"] = True
        for call in calls:
            slot["rows"].extend(_call_rows(call))
    return state, unmatched


def row_date(row: dict) -> str | None:
    for key in ("announcementTime", "announcement_time"):
        raw = row.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)) and raw > 10_000_000_000:
            return cn_date(raw)
        if isinstance(raw, str) and raw.isdigit() and len(raw) >= 12:
            return cn_date(int(raw))
    for key in ("date", "filing_date", "announcementDate"):
        raw = row.get(key)
        if isinstance(raw, str) and len(raw) >= 10:
            return raw[:10]
    return None


def row_title(row: dict) -> str:
    raw = row.get("announcementTitle") or row.get("title") or ""
    return re.sub(r"</?em>", "", str(raw))


def row_url(row: dict) -> str:
    url = row.get("pdf_link") or row.get("url") or ""
    if url:
        return str(url)
    adjunct = row.get("adjunctUrl") or ""
    if adjunct:
        return "http://static.cninfo.com.cn/" + str(adjunct)
    return ""


def earnings_title(title: str) -> bool:
    return bool(TITLE_RE.search(title)) and not bool(DROP_RE.search(title))


def screen_rows(payload, start: str, end: str) -> dict:
    """Keep earnings titles whose UTC+8 date is inside the closed interval.

    ``a-filings.json`` is only ``{"companies": {"600588": [<filings return>, ...]}}``.
    An empty list or a missing name is ``missing``. Any call whose ``ok`` is not
    true makes that company an error and drops its rows. Successful calls merge
    ``rows`` or ``data``, then this screen filters and dedupes hits.
    """
    pool = a_share_rows()
    by_code = {code_key(row["ticker"]): row for row in pool}
    parsed = _company_calls(payload, pool)
    if parsed is None:
        return _blank_screen(start, end, _FORMAT_ERROR)
    state, unmatched = parsed
    companies: dict[str, dict] = {}
    dropped_window = 0
    scanned_rows = 0
    seen_hits: set[tuple[str, str, str, str]] = set()
    for ticker, slot in state.items():
        if not slot["ok"]:
            continue
        known = by_code[code_key(ticker)]
        scanned_rows += len(slot["rows"])
        for row in slot["rows"]:
            stamp = row_date(row)
            title = row_title(row)
            if stamp is None or stamp < start or stamp > end:
                dropped_window += 1
                continue
            if not earnings_title(title):
                continue
            url = row_url(row)
            identity = (ticker, title, stamp, url)
            if identity in seen_hits:
                continue
            seen_hits.add(identity)
            bucket = companies.setdefault(
                ticker,
                {
                    "name": known.get("name"),
                    "subsector": known.get("subsector"),
                    "hits": [],
                },
            )
            bucket["hits"].append({"title": title, "date": stamp, "url": url})
    errors = sorted(ticker for ticker, slot in state.items() if slot["errors"])
    error_details = {
        ticker: "；".join(state[ticker]["errors"])
        for ticker in errors
    }
    succeeded = {ticker for ticker, slot in state.items() if slot["ok"]}
    missing = sorted(
        row["ticker"]
        for row in pool
        if row["ticker"] not in succeeded and row["ticker"] not in errors
    )
    incomplete = bool(errors or missing)
    return {
        "ok": not incomplete,
        "incomplete": incomplete,
        "window": f"{start}~{end}",
        "closed_interval": True,
        "scanned": len(succeeded),
        "scanned_rows": scanned_rows,
        "errors": errors,
        "error_details": error_details,
        "missing": missing,
        "hit_companies": sum(1 for item in companies.values() if item["hits"]),
        "dropped_outside_window": dropped_window,
        "unmatched": unmatched,
        "companies": companies,
    }


def offline_checks() -> list[dict]:
    """Date, closed-interval, and A-share pool checks. No network."""
    checks = []
    got = cn_date(_CN_DATE_PROBE_MS)
    checks.append({
        "check": "cn_date_utc8",
        "ok": got == "2026-08-14",
        "detail": f"{_CN_DATE_PROBE_MS} -> {got}",
        "hint": "公告日期按东八区换算",
    })
    sample = {
        "companies": {
            "600588": [
                {
                    "ok": True,
                    "rows": [
                        {
                            "ticker": "600588",
                            "title": "2026年半年度报告",
                            "announcementTime": _CN_DATE_PROBE_MS,
                            "adjunctUrl": "finalpage/demo.pdf",
                        },
                        {"ticker": "600588", "title": "2026年半年度报告摘要", "date": "2026-08-14"},
                        {"ticker": "600588", "title": "关于召开业绩说明会的通知", "date": "2026-08-14"},
                        {"ticker": "600588", "title": "2026年半年度报告", "date": "2026-08-15"},
                    ],
                }
            ]
        }
    }
    screened = screen_rows(sample, "2026-08-14", "2026-08-14")
    hits = screened["companies"].get("600588.SH", {}).get("hits", [])
    closed_ok = (
        screened["hit_companies"] == 1
        and len(hits) == 1
        and hits[0]["date"] == "2026-08-14"
    )
    checks.append({
        "check": "sedate_closed_interval",
        "ok": closed_ok,
        "detail": f"hits={len(hits)}",
        "hint": "起止日都算在窗口内，不要给结束日加一天",
    })
    try:
        count = len(a_share_rows())
        checks.append({
            "check": "universe_a_shares",
            "ok": count == 56,
            "detail": f"{count} 家",
            "hint": "references/universe.json 里 market=A 应为 56 家",
        })
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        checks.append({
            "check": "universe_a_shares",
            "ok": False,
            "detail": str(exc),
            "hint": "检查 references/universe.json",
        })
    return checks


def _bind(raw: str | None) -> Path:
    if not raw:
        fail("missing --workspace-dir", "每个命令都要带 --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        fail("workspace is a symlink", "工作区不能是符号链接")
    if not path.is_dir():
        fail("workspace not found", "先创建工作区目录")
    return path.resolve()


def _rel(workspace: Path, relative: str) -> Path:
    try:
        return _card_io().workspace_file(workspace, relative)
    except ValueError as exc:
        fail(str(exc), "路径必须留在工作区内")


def cmd_self_test(_args) -> None:
    checks = offline_checks()
    ok = all(item["ok"] for item in checks)
    payload = {"ok": ok, "checks": checks}
    if not ok:
        payload["error"] = "self-test 未全部通过"
        payload["hint"] = "; ".join(
            item["hint"] for item in checks if not item["ok"] and item.get("hint")
        )
    emit(payload)
    raise CliExit(0 if ok else 1)


def cmd_screen(args) -> None:
    workspace = _bind(args.workspace_dir)
    if not args.start or not args.end:
        fail("需要 --start 和 --end", "日期用 YYYY-MM-DD，首尾都算在窗口内")
    if args.start > args.end:
        fail("窗口起止颠倒", "start 要早于或等于 end")
    source = _rel(workspace, args.filings)
    if not source.is_file():
        fail("公告文件不存在", "先把 finance filings 的返回写进工作区")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("公告文件不是 JSON", str(exc))
    result = screen_rows(payload, args.start, args.end)
    result["filings"] = args.filings
    if args.output:
        dest = _rel(workspace, args.output)
        text = json.dumps(result, ensure_ascii=False, indent=1)
        _card_io().write_bytes_exclusive(dest, text.encode("utf-8"))
        result["written"] = args.output
    emit(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="筛 A 股业绩类公告，不直连巨潮")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    screen = sub.add_parser("screen", parents=[shared])
    screen.add_argument("--filings", required=True)
    screen.add_argument("--start", required=True)
    screen.add_argument("--end", required=True)
    screen.add_argument("--output", default="state/tob-industry-weekly/a-scan.json")
    screen.set_defaults(func=cmd_screen)

    check = sub.add_parser("self-test", parents=[shared])
    check.set_defaults(func=cmd_self_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else (code if isinstance(code, int) else 1)
    try:
        if args.cmd == "self-test":
            _bind(getattr(args, "workspace_dir", None))
        args.func(args)
    except CliExit as exc:
        code = exc.code
        return 1 if code is None else (code if isinstance(code, int) else 1)
    except Exception as exc:  # noqa: BLE001  # 命令行边界把未分类错误收成 JSON，避免栈信息直接吐出
        emit({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "检查输入参数；路径必须留在工作区内",
        })
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
