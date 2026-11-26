#!/usr/bin/env python3
"""
CLI and workflow state helpers for dalton-richard-coverage-weekly.

Consolidates Richard's 106-company coverage metadata (28 Core + 78 Watch),
Friday-to-Friday window computation, target building, batch source sweeps
via Dalton tools.researchdata (AlphaEngine / x-timeline / xueqiu), and
idempotent Feishu doc receipt tracking under
state/richard-coverage-weekly/runs/<end>/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def _load_richard_cov_io() -> Any:
    cached = sys.modules.get("richard_cov_io")
    if cached is not None:
        return cached
    target = Path(__file__).resolve().with_name("richard_cov_io.py")
    spec = importlib.util.spec_from_file_location("richard_cov_io", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["richard_cov_io"] = mod
    spec.loader.exec_module(mod)
    return mod


_io = _load_richard_cov_io()
workspace_file = _io.workspace_file
resolve_workspace_target = _io.resolve_workspace_target
write_bytes_exclusive = _io.write_bytes_exclusive
write_json = _io.write_json
write_target_text = _io.write_target_text
write_text = _io.write_text
_transport = _io.load_sibling("richard_cov_transport", "richard_cov_transport.py")
call_bridge_jsonrpc = _transport.call_bridge_jsonrpc
extract_nested_value = _transport.extract_nested_value
extract_route_items = _transport.extract_route_items
is_quota_or_auth_error = _transport.is_quota_or_auth_error
resolve_researchdata_transport = _transport.resolve_researchdata_transport
unwrap_bridge_response = _transport.unwrap_bridge_response
screen_movers = _io.load_sibling("richard_cov_screen_movers", "screen_movers.py")

TEMPLATE_VERSION = "dalton-richard-coverage-weekly-v1.0"
STATE_ROOT = "state/richard-coverage-weekly"
DOC_FILE = "doc.json"
FAILED_DOC_FILE = "doc.failed.json"
APPEND_PROGRESS_FILE = "doc.progress.json"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ET_TZ = ZoneInfo("America/New_York")

# Core 28 search names; must stay in sync with screen_movers.TICKERS where tier == "Core".
CORE: list[dict[str, Any]] = [
    {"name": "Google", "search_names": ["Alphabet", "Google", "谷歌", "GOOGL"]},
    {"name": "Amazon", "search_names": ["Amazon", "亚马逊", "AMZN"]},
    {"name": "DoorDash", "search_names": ["DoorDash", "DASH"]},
    {"name": "Affirm", "search_names": ["Affirm Holdings", "Affirm", "AFRM"]},
    {"name": "亚朵", "search_names": ["亚朵", "Atour", "ATAT"]},
    {"name": "Boss直聘", "search_names": ["BOSS直聘", "Kanzhun", "BZ"]},
    {"name": "Microsoft", "search_names": ["Microsoft", "微软", "MSFT"]},
    {"name": "SpaceX", "search_names": ["SpaceX", "SPCX"]},
    {"name": "Meta", "search_names": ["Meta Platforms", "Meta", "META"]},
    {"name": "Anthropic", "search_names": ["Anthropic", "Claude", "ANT"]},
    {"name": "智谱", "search_names": ["智谱", "Zhipu", "Z.ai", "2513.HK"]},
    {"name": "Uber", "search_names": ["Uber", "UBER"]},
    {"name": "Visa", "search_names": ["Visa", "V"]},
    {"name": "Mastercard", "search_names": ["Mastercard", "万事达", "MA"]},
    {"name": "Robinhood", "search_names": ["Robinhood", "HOOD"]},
    {"name": "Interactive Brokers", "search_names": ["Interactive Brokers", "盈透", "IBKR"]},
    {"name": "S&P Global", "search_names": ["S&P Global", "标普全球", "SPGI"]},
    {"name": "Moody's", "search_names": ["Moody's", "穆迪", "MCO"]},
    {"name": "Nasdaq", "search_names": ["Nasdaq Inc", "纳斯达克", "NDAQ"]},
    {"name": "腾讯", "search_names": ["腾讯", "Tencent", "0700.HK"]},
    {"name": "阿里巴巴", "search_names": ["阿里巴巴", "Alibaba", "BABA"]},
    {"name": "拼多多", "search_names": ["拼多多", "PDD Holdings", "PDD"]},
    {"name": "网易", "search_names": ["网易", "NetEase", "NTES"]},
    {"name": "美团", "search_names": ["美团", "Meituan", "3690.HK"]},
    {"name": "携程", "search_names": ["携程", "Trip.com", "TCOM"]},
    {"name": "贝壳", "search_names": ["贝壳", "KE Holdings", "BEKE"]},
    {"name": "富途", "search_names": ["富途", "Futu", "FUTU"]},
    {"name": "华住", "search_names": ["华住", "H World", "HTHT"]},
]

# AlphaEngine primary / secondary (alternate language) query per Core ticker.
AE_QUERY: dict[str, tuple[str, str | None]] = {
    "GOOGL": ("Alphabet", "谷歌"),
    "AMZN": ("Amazon", "亚马逊"),
    "DASH": ("DoorDash", None),
    "AFRM": ("Affirm", None),
    "ATAT": ("亚朵", "Atour"),
    "BZ": ("BOSS直聘", "Kanzhun"),
    "MSFT": ("Microsoft", "微软"),
    "SPCX": ("SpaceX", None),
    "META": ("Meta Platforms", "Meta"),
    "ANT": ("Anthropic", None),
    "2513.HK": ("智谱", "Zhipu"),
    "UBER": ("Uber", "优步"),
    "V": ("Visa", "维萨"),
    "MA": ("Mastercard", "万事达"),
    "HOOD": ("Robinhood", None),
    "IBKR": ("Interactive Brokers", "盈透"),
    "SPGI": ("S&P Global", "标普全球"),
    "MCO": ("Moody's", "穆迪"),
    "NDAQ": ("Nasdaq Inc", "纳斯达克"),
    "0700.HK": ("腾讯", "Tencent"),
    "BABA": ("阿里巴巴", "Alibaba"),
    "PDD": ("拼多多", "PDD Holdings"),
    "NTES": ("网易", "NetEase"),
    "3690.HK": ("美团", "Meituan"),
    "TCOM": ("携程", "Trip.com"),
    "BEKE": ("贝壳", "KE Holdings"),
    "FUTU": ("富途", "Futu"),
    "HTHT": ("华住", "H World"),
}

# Relevance keywords used when digesting results per Core ticker.
REL_KEYS: dict[str, list[str]] = {
    "GOOGL": ["Alphabet", "谷歌", "Google", "GOOGL", "Gemini", "Waymo"],
    "AMZN": ["Amazon", "亚马逊", "AWS", "AMZN"],
    "DASH": ["DoorDash", "DASH"],
    "AFRM": ["Affirm", "AFRM"],
    "ATAT": ["亚朵", "Atour"],
    "BZ": ["BOSS直聘", "Kanzhun", "看准"],
    "MSFT": ["Microsoft", "微软", "Azure", "OpenAI"],
    "SPCX": ["SpaceX", "Starlink", "星链"],
    "META": ["Meta", "Facebook"],
    "ANT": ["Anthropic", "Claude", "ANT"],
    "2513.HK": ["智谱", "Zhipu", "GLM"],
    "UBER": ["Uber", "优步"],
    "V": ["Visa", "维萨"],
    "MA": ["Mastercard", "万事达"],
    "HOOD": ["Robinhood", "HOOD"],
    "IBKR": ["Interactive Brokers", "盈透", "IBKR"],
    "SPGI": ["S&P Global", "标普全球", "SPGI"],
    "MCO": ["Moody", "穆迪"],
    "NDAQ": ["Nasdaq, Inc", "Nasdaq Inc", "纳斯达克集团", "NDAQ"],
    "0700.HK": ["腾讯", "Tencent"],
    "BABA": ["阿里", "Alibaba"],
    "PDD": ["拼多多", "PDD", "Temu"],
    "NTES": ["网易", "NetEase"],
    "3690.HK": ["美团", "Meituan"],
    "TCOM": ["携程", "Trip.com"],
    "BEKE": ["贝壳", "KE Holdings"],
    "FUTU": ["富途", "Futu", "moomoo"],
    "HTHT": ["华住", "H World"],
}

# Alternate keywords for Watch companies (internal library kw, AlphaEngine query, digest relevance).
WATCH_ALT: dict[str, list[str]] = {
    "VIPS": ["唯品会", "Vipshop"],
    "0100.HK": ["MiniMax", "Minimax", "稀宇"],
    "NFLX": ["Netflix", "奈飞"],
    "SPOT": ["Spotify"],
    "SE": ["Sea Limited", "Shopee", "Garena"],
    "RBLX": ["Roblox"],
    "RDDT": ["Reddit"],
    "RKT": ["Rocket Companies", "Rocket Mortgage"],
    "FIGR": ["Figure Technology"],
    "BX": ["Blackstone", "黑石"],
    "ICE": ["Intercontinental Exchange"],
    "HURN": ["Huron Consulting", "Huron"],
    "HLT": ["Hilton", "希尔顿"],
    "FIX": ["Comfort Systems"],
    "PHM": ["PulteGroup", "Pulte"],
    "JD": ["京东", "JD.com"],
    "0625.HK": ["希音", "SHEIN", "Shein"],
    "DIDIY": ["滴滴", "DiDi"],
    "TME": ["腾讯音乐", "Tencent Music"],
    "1860.HK": ["汇量", "Mobvista"],
    "3696.HK": ["英矽智能", "Insilico"],
    "9992.HK": ["泡泡玛特", "Pop Mart", "POP MART"],
    "6181.HK": ["老铺黄金", "Laopu"],
    "601100.SS": ["恒立液压"],
    "CPNG": ["Coupang", "酷澎"],
    "RKLB": ["Rocket Lab"],
    "2556.HK": ["迈富时", "Marketingforce"],
    "BAC": ["Bank of America", "美国银行"],
    "JPM": ["JPMorgan", "摩根大通"],
    "PGR": ["Progressive"],
    "6618.HK": ["京东健康", "JD Health"],
    "ECHO": ["EchoStar"],
    "DHI": ["D.R. Horton", "DR Horton"],
}

# X search query overrides (defaults to "$TICKER" for clean US cashtags).
X_QUERY: dict[str, str] = {
    "BZ": "Kanzhun OR BOSS直聘",
    "2513.HK": "Zhipu OR 智谱 OR 2513",
    "0700.HK": "Tencent OR $TCEHY OR 腾讯",
    "3690.HK": "Meituan OR 美团 OR $MPNGY",
    "V": "$V Visa",
    "MA": "$MA Mastercard",
    "0100.HK": "MiniMax OR 稀宇 OR 0100.HK",
    "0625.HK": "Shein OR 希音",
    "1860.HK": "Mobvista OR 汇量",
    "3696.HK": "Insilico OR 英矽智能",
    "9992.HK": '"Pop Mart" OR 泡泡玛特 OR Labubu',
    "6181.HK": "Laopu OR 老铺黄金",
    "601100.SS": "恒立液压",
    "ICE": "$ICE Intercontinental",
    "FIX": '$FIX "Comfort Systems"',
}

BridgeCaller = Callable[[str, dict[str, Any]], dict[str, Any]]


def xq_code(ticker: str) -> str:
    """Convert Yahoo Finance ticker into Xueqiu symbol format (5-digit HK, SH/SZ prefix for A-shares)."""
    if ticker.endswith(".HK"):
        return ticker[:-3].zfill(5)
    if ticker.endswith(".SS"):
        return "SH" + ticker[:-3]
    if ticker.endswith(".SZ"):
        return "SZ" + ticker[:-3]
    return ticker


def _fname(ticker: str) -> str:
    return ticker.replace("/", "_")


def run_rel(end: dt.date, *parts: str) -> str:
    """Return workspace-relative path under state/richard-coverage-weekly/runs/<end>/."""
    base = f"{STATE_ROOT}/runs/{end.isoformat()}"
    return "/".join([base, *parts]) if parts else base


def _parse_now_et(now_date: str | None) -> dt.datetime:
    if now_date is None:
        return dt.datetime.now(ET_TZ)
    raw = now_date.strip()
    if "T" in raw or " " in raw or ":" in raw:
        parsed = dt.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET_TZ)
        return parsed.astimezone(ET_TZ)
    d = dt.date.fromisoformat(raw)
    return dt.datetime.combine(d, dt.time(16, 0), tzinfo=ET_TZ)


def compute_window(
    *,
    base: str | None = None,
    end: str | None = None,
    now_date: str | None = None,
    mode: str = "auto",
    downloader: Callable[[list[str], str, str], Any] | None = None,
) -> dict[str, Any]:
    """Compute window end from actual completed SPY daily bar (16:00 America/New_York cutoff)."""
    if end:
        end_d = dt.date.fromisoformat(end)
    else:
        now_et = _parse_now_et(now_date)
        today_et = now_et.date()
        us_closed = now_et.time() >= dt.time(16, 0)
        if mode == "manual":
            if today_et.weekday() == 5:
                cutoff = today_et - dt.timedelta(days=1)
                include_cutoff = True
            elif today_et.weekday() == 6:
                cutoff = today_et - dt.timedelta(days=2)
                include_cutoff = True
            else:
                cutoff = today_et
                include_cutoff = us_closed
        else:
            days_since_fri = (today_et.weekday() - 4) % 7
            target_fri = today_et - dt.timedelta(days=days_since_fri)
            if days_since_fri == 0 and not us_closed:
                target_fri -= dt.timedelta(days=7)
            cutoff = target_fri
            include_cutoff = True
        try:
            end_d = screen_movers.latest_completed_trading_day(
                cutoff,
                include_cutoff=include_cutoff,
                downloader=downloader,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "mode": mode,
                "cutoff": cutoff.isoformat(),
                "error": f"failed to determine latest completed US trading day from SPY daily bars: {exc}",
            }
    base_d = dt.date.fromisoformat(base) if base else end_d - dt.timedelta(days=7)
    search_from = (base_d - dt.timedelta(days=1)).isoformat()
    search_to = (end_d + dt.timedelta(days=1)).isoformat()
    end_iso = end_d.isoformat()
    base_iso = base_d.isoformat()
    return {
        "ok": True,
        "mode": mode,
        "base": base_iso,
        "end": end_iso,
        "search_from": search_from,
        "search_to": search_to,
        "run_dir": run_rel(end_d),
        "report_profile": "richard-coverage-weekly",
        "report_run_id": f"richard-coverage-weekly-{end_iso}",
        # A saved report is immutable per id, so a failure notice must not take the weekly id:
        # a later successful rerun of the same week would then be refused as a content conflict.
        "failure_report_run_id": (
            f"richard-coverage-weekly-{end_iso}-failed-"
            f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M')}"
        ),
        "doc_title": f"Richard 覆盖周报 {base_iso} 至 {end_iso}（Core 逐家 + Watch 异动筛选）",
    }


def core_list(tickers: dict[str, tuple[str, str, str, str]] | None = None) -> list[dict[str, Any]]:
    src = tickers or screen_movers.TICKERS
    out: list[dict[str, Any]] = []
    for item in CORE:
        ticker, group, sec, _ = src[item["name"]]
        out.append({**item, "ticker": ticker, "group": group, "section": sec, "tier": "Core"})
    return out


def watch_list(tickers: dict[str, tuple[str, str, str, str]] | None = None) -> list[dict[str, Any]]:
    src = tickers or screen_movers.TICKERS
    return [
        {"name": name, "ticker": ticker, "group": group, "section": sec, "tier": tier}
        for name, (ticker, group, sec, tier) in src.items()
        if tier == "Watch"
    ]


def build_targets_from_screen(screen_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build search targets for Core 27 + flagged Watch + unflagged Live Position Watch."""
    if not screen_data.get("ok"):
        raise ValueError(f"screen.json ok=false: {screen_data.get('warning')}")
    core_kw = {c["name"]: c["search_names"] for c in CORE}
    targets: list[dict[str, Any]] = []
    for row in screen_data.get("rows", []):
        tk = row["ticker"]
        if row.get("tier") == "Core":
            kw = list(core_kw.get(row["name"], [row["name"], tk]))
        elif row.get("tier") == "Watch" and (row.get("flags") or row.get("section") == "Live Position"):
            kw = list(WATCH_ALT.get(tk, [row["name"]])) + ([tk] if len(tk) > 2 and "." not in tk else [])
            if tk not in WATCH_ALT and row["name"] not in kw:
                kw.insert(0, row["name"])
        else:
            continue
        targets.append(
            {
                "name": row["name"],
                "ticker": tk,
                "kw": kw,
                "tier": row["tier"],
                "section": row.get("section"),
                "flagged": bool(row.get("flags")),
                "xq_code": xq_code(tk),
                "x_query": X_QUERY.get(tk, f"${tk}"),
            }
        )
    return targets


def _doc_id_from_record(record: dict[str, Any] | None) -> str | None:
    if not isinstance(record, dict):
        return None
    nested = record.get("data") if isinstance(record.get("data"), dict) else None
    for container in (record, nested):
        if not isinstance(container, dict):
            continue
        for key in ("document_id", "token", "node_token"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def feishu_doc_url(record: dict[str, Any] | None) -> str | None:
    """Return Feishu URL/id even when ``ok`` is False (partial-create content failure)."""
    if not isinstance(record, dict):
        return None
    nested = record.get("data") if isinstance(record.get("data"), dict) else None
    for container in (record, nested):
        if not isinstance(container, dict):
            continue
        for key in ("url", "doc_url"):
            val = container.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
    doc_id = _doc_id_from_record(record)
    if doc_id:
        return f"https://feishu.cn/docx/{doc_id}"
    return None


def _read_json_state(workspace: Path, relative: str) -> tuple[str, dict[str, Any] | None]:
    path = workspace_file(workspace, relative)
    if not path.is_file() or path.is_symlink():
        return "missing", None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unreadable", None
    if not isinstance(loaded, dict):
        return "unreadable", None
    return "present", loaded


def _failed_attempts(workspace: Path, end: dt.date) -> int:
    state, data = _read_json_state(workspace, run_rel(end, FAILED_DOC_FILE))
    if state != "present" or data is None:
        return 0
    try:
        return int(data.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _compute_progress(
    *,
    total_parts: int,
    parts_meta: dict[str, Any] | None = None,
    completed_parts: list[int] | None = None,
) -> dict[str, Any]:
    total = max(1, int(total_parts))
    norm_parts: dict[str, dict[str, Any]] = {}
    if isinstance(parts_meta, dict):
        for k, v in parts_meta.items():
            if not str(k).isdigit() or not isinstance(v, dict):
                continue
            p_num = int(k)
            if 1 <= p_num <= total:
                entry = dict(v)
                if "status" not in entry:
                    entry["status"] = "completed" if entry.get("ok") is True else "unverified"
                norm_parts[str(p_num)] = entry
    for p in completed_parts or []:
        p_num = int(p)
        if 1 <= p_num <= total:
            cur = norm_parts.setdefault(str(p_num), {})
            if cur.get("status") not in ("unverified", "verified_missing"):
                cur["status"] = "completed"
                cur.setdefault("ok", True)

    done = [p for p in range(1, total + 1) if norm_parts.get(str(p), {}).get("status") == "completed"]
    unverified = [p for p in range(1, total + 1) if norm_parts.get(str(p), {}).get("status") == "unverified"]
    pending = [p for p in range(1, total + 1) if p not in done]
    return {
        "total_parts": total,
        "completed_parts": done,
        "unverified_parts": unverified,
        "pending_parts": pending,
        "body_incomplete": bool(pending),
        "progress_missing": False,
        "needs_verify": bool(unverified),
        "parts": norm_parts,
    }


def _read_progress(workspace: Path, end: dt.date, record: dict[str, Any]) -> dict[str, Any]:
    embedded = record.get("_progress")
    if isinstance(embedded, dict) and isinstance(embedded.get("total_parts"), int) and embedded["total_parts"] >= 1:
        return _compute_progress(
            total_parts=embedded["total_parts"],
            parts_meta=embedded.get("parts") if isinstance(embedded.get("parts"), dict) else None,
            completed_parts=list(embedded.get("completed_parts") or []),
        )
    state, prog = _read_json_state(workspace, run_rel(end, APPEND_PROGRESS_FILE))
    if state == "present" and prog is not None and isinstance(prog.get("total_parts"), int) and prog["total_parts"] >= 1:
        return _compute_progress(
            total_parts=prog["total_parts"],
            parts_meta=prog.get("parts") if isinstance(prog.get("parts"), dict) else None,
            completed_parts=list(prog.get("completed_parts") or []),
        )
    return {
        "total_parts": None,
        "completed_parts": [],
        "unverified_parts": [],
        "pending_parts": [],
        "body_incomplete": True,
        "progress_missing": True,
        "needs_verify": True,
        "reason": "progress_missing",
        "parts": {},
    }


def _persist_doc_progress(workspace: Path, end: dt.date, record: dict[str, Any], prog: dict[str, Any]) -> None:
    updated_doc = {**record, "_progress": prog}
    write_json(workspace, run_rel(end, DOC_FILE), updated_doc)


def doc_status_for(workspace: Path, end: dt.date) -> dict[str, Any]:
    """Check idempotent doc creation & chunked append status for one weekly run."""
    relative = run_rel(end, DOC_FILE)
    state, record = _read_json_state(workspace, relative)
    if state == "unreadable":
        return {
            "ok": False,
            "exists": True,
            "create": False,
            "reason": "doc_record_unreadable",
            "path": relative,
            "url": None,
        }
    if state == "present" and record is not None:
        url = feishu_doc_url(record)
        if url is None:
            return {
                "ok": False,
                "exists": True,
                "create": False,
                "reason": "doc_record_unreadable",
                "path": relative,
                "url": None,
            }
        prog = _read_progress(workspace, end, record)
        payload: dict[str, Any] = {
            "ok": True,
            "exists": True,
            "create": False,
            "path": relative,
            "url": url,
            "document_id": _doc_id_from_record(record),
            "body_incomplete": prog["body_incomplete"],
            "progress_missing": prog["progress_missing"],
            "needs_verify": prog["needs_verify"],
            "total_parts": prog["total_parts"],
            "completed_parts": prog["completed_parts"],
            "unverified_parts": prog["unverified_parts"],
            "pending_parts": prog["pending_parts"],
        }
        if prog.get("reason"):
            payload["reason"] = prog["reason"]
        return payload
    attempts = _failed_attempts(workspace, end)
    if attempts >= 2:
        return {
            "ok": True,
            "exists": False,
            "create": False,
            "reason": "create_failed_twice",
            "path": relative,
            "url": None,
            "attempts": attempts,
        }
    return {
        "ok": True,
        "exists": False,
        "create": True,
        "attempt_id": f"attempt-{attempts + 1}-{uuid.uuid4().hex[:8]}",
        "path": relative,
        "url": None,
        "attempts": attempts,
    }


def _failure_fingerprint(raw: bytes, parsed: dict[str, Any] | None) -> str:
    if isinstance(parsed, dict):
        basis = json.dumps(parsed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    else:
        basis = raw
    return hashlib.sha256(basis).hexdigest()


def _record_failed_attempt(
    workspace: Path,
    end: dt.date,
    raw: bytes,
    parsed: dict[str, Any] | None = None,
    *,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    relative = run_rel(end, FAILED_DOC_FILE)
    path = workspace_file(workspace, relative)
    attempts = 0
    returns: list[Any] = []
    fingerprints: list[str] = []
    attempt_ids: list[str] = []
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "ok": False,
                "stored": False,
                "create": False,
                "reason": "doc_record_unreadable",
                "path": relative,
            }
        if isinstance(current, dict):
            try:
                attempts = int(current.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            if isinstance(current.get("returns"), list):
                returns = list(current["returns"])
            if isinstance(current.get("fingerprints"), list):
                fingerprints = [str(fp) for fp in current["fingerprints"]]
            if isinstance(current.get("attempt_ids"), list):
                attempt_ids = [str(aid) for aid in current["attempt_ids"]]

    norm_attempt = attempt_id.strip() if isinstance(attempt_id, str) and attempt_id.strip() else None
    fp = _failure_fingerprint(raw, parsed)
    is_dup = (norm_attempt in attempt_ids) if norm_attempt is not None else (fp in fingerprints)
    if is_dup:
        create = attempts < 2
        dup_payload: dict[str, Any] = {
            "ok": True,
            "stored": False,
            "skipped": True,
            "duplicate": True,
            "create": create,
            "path": relative,
            "url": None,
            "attempts": attempts,
            **({"attempt_id": norm_attempt} if norm_attempt else {}),
        }
        if not create:
            dup_payload["reason"] = "create_failed_twice"
        return dup_payload

    attempts += 1
    if norm_attempt is not None:
        attempt_ids.append(norm_attempt)
    fingerprints.append(fp)
    returns.append({"attempt_id": norm_attempt, "raw": raw.decode("utf-8", "replace")})
    write_json(
        workspace,
        relative,
        {
            "attempts": attempts,
            "attempt_ids": attempt_ids,
            "fingerprints": fingerprints,
            "returns": returns,
        },
    )
    create = attempts < 2
    payload: dict[str, Any] = {
        "ok": True,
        "stored": True,
        "skipped": False,
        "duplicate": False,
        "create": create,
        "path": relative,
        "url": None,
        "attempts": attempts,
        **({"attempt_id": norm_attempt} if norm_attempt else {}),
    }
    if not create:
        payload["reason"] = "create_failed_twice"
    return payload


def save_doc_record(
    workspace: Path,
    end: dt.date,
    raw: bytes,
    *,
    total_parts: int | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Atomically persist feishu_create_doc identity AND chunk progress together inside runs/<end>/doc.json."""
    relative = run_rel(end, DOC_FILE)
    dest = workspace_file(workspace, relative)
    if dest.is_file():
        try:
            existing = json.loads(dest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        prog = _read_progress(workspace, end, existing if isinstance(existing, dict) else {})
        return {
            "ok": True,
            "stored": False,
            "skipped": True,
            "create": False,
            "path": relative,
            "url": feishu_doc_url(existing if isinstance(existing, dict) else None),
            "document_id": _doc_id_from_record(existing if isinstance(existing, dict) else None),
            "body_incomplete": prog["body_incomplete"],
            "progress_missing": prog["progress_missing"],
            "needs_verify": prog["needs_verify"],
            "total_parts": prog["total_parts"],
            "completed_parts": prog["completed_parts"],
            "unverified_parts": prog["unverified_parts"],
            "pending_parts": prog["pending_parts"],
            **({"reason": prog["reason"]} if prog.get("reason") else {}),
        }
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "stored": False, "error": "invalid json"}
    if not isinstance(record, dict):
        return {"ok": False, "stored": False, "error": "invalid json"}
    url = feishu_doc_url(record)
    if url is None:
        return _record_failed_attempt(workspace, end, raw, record, attempt_id=attempt_id)

    if total_parts is None or int(total_parts) < 1:
        return {"ok": False, "stored": False, "error": "total_parts (>= 1) is required"}

    total = int(total_parts)
    create_ok = record.get("ok") is not False and record.get("outcome") != "unknown"
    part1_status = "completed" if create_ok else "unverified"
    prog = _compute_progress(
        total_parts=total,
        parts_meta={"1": {"status": part1_status, "ok": create_ok, "error": record.get("error")}},
    )
    doc_payload: dict[str, Any] = {
        **record,
        "_progress": prog,
        **({"_attempt_id": attempt_id.strip()} if isinstance(attempt_id, str) and attempt_id.strip() else {}),
    }
    encoded = (json.dumps(doc_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    write_bytes_exclusive(dest, encoded)
    return {
        "ok": True,
        "stored": True,
        "skipped": False,
        "create": False,
        "path": relative,
        "url": url,
        "document_id": _doc_id_from_record(record),
        "body_incomplete": prog["body_incomplete"],
        "progress_missing": False,
        "needs_verify": prog["needs_verify"],
        "total_parts": prog["total_parts"],
        "completed_parts": prog["completed_parts"],
        "unverified_parts": prog["unverified_parts"],
        "pending_parts": prog["pending_parts"],
    }


def record_doc_append(
    workspace: Path,
    end: dt.date,
    part: int,
    raw: bytes,
) -> dict[str, Any]:
    """Checkpoint one feishu_append segment atomically in doc.json; uncertain or invalid payloads become unverified."""
    if part < 1:
        return {"ok": False, "stored": False, "error": "part must be >= 1"}
    state, record = _read_json_state(workspace, run_rel(end, DOC_FILE))
    if state != "present" or record is None:
        return {"ok": False, "stored": False, "error": "doc_missing_or_unreadable"}
    url = feishu_doc_url(record)
    if url is None:
        return {"ok": False, "stored": False, "error": "doc_record_unreadable"}

    prog = _read_progress(workspace, end, record)
    if prog["progress_missing"]:
        return {"ok": False, "stored": False, "error": "progress_missing; run verify-append --total-parts first"}
    total = max(int(prog["total_parts"]), part)
    parts_meta: dict[str, Any] = dict(prog.get("parts") or {})
    if part in prog["completed_parts"]:
        return {
            "ok": True,
            "stored": False,
            "skipped": True,
            "path": run_rel(end, DOC_FILE),
            "url": url,
            "document_id": _doc_id_from_record(record),
            "part": part,
            **{k: prog[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
        }

    append_rec: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            append_rec = parsed
    except (UnicodeDecodeError, json.JSONDecodeError):
        append_rec = None

    if append_rec is None:
        parts_meta[str(part)] = {
            "status": "unverified",
            "ok": False,
            "outcome": "invalid_json",
            "error": "invalid json",
        }
        summary = _compute_progress(total_parts=total, parts_meta=parts_meta)
        _persist_doc_progress(workspace, end, record, summary)
        return {
            "ok": False,
            "stored": True,
            "skipped": False,
            "unverified": True,
            "path": run_rel(end, DOC_FILE),
            "url": url,
            "document_id": _doc_id_from_record(record),
            "part": part,
            **{k: summary[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
            "error": "invalid json",
        }

    append_ok = append_rec.get("ok") is True and append_rec.get("outcome") != "unknown"
    status = "completed" if append_ok else "unverified"
    parts_meta[str(part)] = {
        "status": status,
        "ok": append_ok,
        "outcome": append_rec.get("outcome") or ("ok" if append_ok else "unknown"),
        "error": append_rec.get("error"),
    }
    summary = _compute_progress(total_parts=total, parts_meta=parts_meta)
    _persist_doc_progress(workspace, end, record, summary)
    return {
        "ok": append_ok,
        "stored": True,
        "skipped": False,
        "unverified": status == "unverified",
        "path": run_rel(end, DOC_FILE),
        "url": url,
        "document_id": _doc_id_from_record(record),
        "part": part,
        **{k: summary[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
        **({"error": append_rec.get("error") or append_rec.get("outcome")} if not append_ok else {}),
    }


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_md_ws(text: str) -> str:
    # Align markdown source with feishu_read plain text while keeping intra-token hyphens (-1.5%, dates).
    cleaned = re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", text)
    cleaned = re.sub(r"(?m)^\s*(?:[-*_]{3,}|\|[\s:|-]+\|?)\s*$", " ", cleaned)
    cleaned = re.sub(r"(?m)^\s*(?:[-*+•]|(?:\d+\.))\s+", "", cleaned)
    cleaned = re.sub(r"[*_`#>|~]+", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _strip_separators(text: str) -> str:
    return re.sub(r"(?m)^\s*---+\s*$", "", text).strip()


def _evaluate_segment_match(
    doc_text: str,
    part_text: str,
    *,
    expected_after: str | None = None,
) -> tuple[str, str | None]:
    stripped = _strip_separators(part_text)
    if not stripped:
        return "error", "part_text contains no non-separator content"
    stripped_after = _strip_separators(expected_after) if expected_after else None

    out_of_order = False
    for norm_fn in (_norm_ws, _strip_md_ws):
        doc_s = norm_fn(doc_text)
        part_s = norm_fn(stripped)
        if not part_s:
            continue
        if stripped_after:
            after_s = norm_fn(stripped_after)
            pos_after = doc_s.find(after_s) if after_s else -1
            if pos_after >= 0 and doc_s.find(part_s, pos_after + len(after_s)) >= 0:
                return "full", None
            if part_s in doc_s:
                out_of_order = True
        elif part_s in doc_s:
            return "full", None

    if out_of_order:
        return "partial", None

    for norm_fn in (_norm_ws, _strip_md_ws):
        doc_s = norm_fn(doc_text)
        part_s = norm_fn(stripped)
        if not doc_s or not part_s:
            continue
        n = len(part_s)
        probe_len = min(40, max(16, n // 4)) if n >= 24 else max(8, n // 2)
        if n < probe_len:
            continue
        probes: list[str] = [part_s[:probe_len], part_s[-probe_len:]]
        if n > probe_len * 2:
            step = max(probe_len // 2, 20)
            for start in range(probe_len // 2, n - probe_len + 1, step):
                probes.append(part_s[start : start + probe_len])
        for line in stripped.splitlines():
            line_s = norm_fn(line)
            if len(line_s) >= probe_len:
                probes.append(line_s[:probe_len])
                if len(line_s) > probe_len:
                    probes.append(line_s[-probe_len:])
        for probe in dict.fromkeys(probes):
            if len(probe) >= probe_len and probe in doc_s:
                return "partial", None

    return "none", None


def _parse_feishu_readback(
    doc_readback: dict[str, Any] | str | bytes | bytearray,
) -> tuple[str | None, str, bool | None, int | None, str | None]:
    obj: Any = doc_readback
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = json.loads(bytes(obj).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "readback_invalid_json", "", None, None, "invalid feishu_read JSON bytes"
    elif isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except json.JSONDecodeError:
            return "readback_invalid_json", "", None, None, "invalid feishu_read JSON string"
    if not isinstance(obj, dict):
        return "readback_invalid_json", "", None, None, "feishu_read payload is not a JSON object"
    if obj.get("ok") is False:
        err = str(obj.get("error") or obj.get("message") or "feishu_read_failed")
        return "readback_failed", "", None, None, err
    content = obj.get("content")
    if not isinstance(content, str):
        return "readback_missing_content", "", None, None, "feishu_read JSON missing string 'content'"

    raw_tc = obj.get("total_chars")
    total_chars = raw_tc if isinstance(raw_tc, int) and not isinstance(raw_tc, bool) else None
    raw_trunc = obj.get("truncated")
    if isinstance(raw_trunc, bool):
        truncated: bool | None = raw_trunc or (total_chars is not None and total_chars > len(content))
    elif total_chars is not None:
        truncated = total_chars > len(content)
    else:
        truncated = None
    return None, content, truncated, total_chars, None


def verify_doc_append(
    workspace: Path,
    end: dt.date,
    part: int,
    doc_readback: dict[str, Any] | str | bytes | bytearray,
    *,
    part_text: str,
    expected_after: str | None = None,
    total_parts: int | None = None,
) -> dict[str, Any]:
    """Verify via full feishu_read JSON whether an unverified segment is completely in the Feishu doc."""
    if part < 1:
        return {"ok": False, "error": "part must be >= 1", "error_code": "invalid_part", "should_append": False}
    state, record = _read_json_state(workspace, run_rel(end, DOC_FILE))
    if state != "present" or record is None:
        return {"ok": False, "error": "doc_missing_or_unreadable", "error_code": "doc_missing_or_unreadable", "should_append": False}
    url = feishu_doc_url(record)
    if url is None:
        return {"ok": False, "error": "doc_record_unreadable", "error_code": "doc_record_unreadable", "should_append": False}

    prog = _read_progress(workspace, end, record)
    resolved_total = prog["total_parts"] if prog["total_parts"] is not None else total_parts
    if resolved_total is None or int(resolved_total) < part:
        return {
            "ok": False,
            "error": "total_parts (>= part) is required when progress is missing",
            "error_code": "progress_missing",
            "should_append": False,
        }
    total = int(resolved_total)
    parts_meta: dict[str, Any] = dict(prog.get("parts") or {})

    if part in prog["completed_parts"]:
        return {
            "ok": True,
            "part": part,
            "outcome": "present",
            "present": True,
            "should_append": False,
            "indeterminate": False,
            "action": "already_completed",
            "path": run_rel(end, DOC_FILE),
            "url": url,
            "document_id": _doc_id_from_record(record),
            **{k: prog[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
        }

    if not isinstance(part_text, str) or not part_text.strip():
        return {"ok": False, "part": part, "error": "empty part_text", "error_code": "empty_part_text", "should_append": False}

    parse_err_code, doc_text, truncated, total_chars, parse_detail = _parse_feishu_readback(doc_readback)
    if parse_err_code is not None:
        parts_meta[str(part)] = {
            "status": "unverified",
            "ok": False,
            "outcome": "indeterminate",
            "error": parse_err_code,
        }
        summary = _compute_progress(total_parts=total, parts_meta=parts_meta)
        _persist_doc_progress(workspace, end, record, summary)
        return {
            "ok": False,
            "part": part,
            "outcome": "indeterminate",
            "present": None,
            "should_append": False,
            "indeterminate": True,
            "error": parse_detail or parse_err_code,
            "error_code": parse_err_code,
            "action": "stop_and_manual_check",
            "path": run_rel(end, DOC_FILE),
            "url": url,
            "document_id": _doc_id_from_record(record),
            **{k: summary[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
        }

    match_kind, match_err = _evaluate_segment_match(doc_text, part_text, expected_after=expected_after)
    if match_kind == "error":
        return {"ok": False, "part": part, "error": match_err, "error_code": "empty_part_text", "should_append": False}

    if match_kind == "full":
        parts_meta[str(part)] = {
            "status": "completed",
            "ok": True,
            "outcome": "verified_present",
            "verified_by": "feishu_read",
        }
        summary = _compute_progress(total_parts=total, parts_meta=parts_meta)
        _persist_doc_progress(workspace, end, record, summary)
        return {
            "ok": True,
            "part": part,
            "outcome": "present",
            "present": True,
            "should_append": False,
            "indeterminate": False,
            "truncated": bool(truncated),
            "total_chars": total_chars,
            "action": "marked_completed",
            "path": run_rel(end, DOC_FILE),
            "url": url,
            "document_id": _doc_id_from_record(record),
            **{k: summary[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
        }

    if match_kind == "none" and truncated is False:
        parts_meta[str(part)] = {
            "status": "verified_missing",
            "ok": False,
            "outcome": "verified_missing",
            "verified_by": "feishu_read",
        }
        summary = _compute_progress(total_parts=total, parts_meta=parts_meta)
        _persist_doc_progress(workspace, end, record, summary)
        return {
            "ok": True,
            "part": part,
            "outcome": "missing",
            "present": False,
            "should_append": True,
            "indeterminate": False,
            "truncated": False,
            "total_chars": total_chars,
            "action": "append_required",
            "path": run_rel(end, DOC_FILE),
            "url": url,
            "document_id": _doc_id_from_record(record),
            **{k: summary[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
        }

    if match_kind == "partial":
        err_code = "readback_partial_match"
    elif truncated is True:
        err_code = "readback_truncated_not_found"
    else:
        err_code = "readback_truncation_unknown"

    parts_meta[str(part)] = {
        "status": "unverified",
        "ok": False,
        "outcome": "indeterminate",
        "error": err_code,
        "verified_by": "feishu_read",
    }
    summary = _compute_progress(total_parts=total, parts_meta=parts_meta)
    _persist_doc_progress(workspace, end, record, summary)
    return {
        "ok": False,
        "part": part,
        "outcome": "indeterminate",
        "present": None,
        "should_append": False,
        "indeterminate": True,
        "partial_match": match_kind == "partial",
        "truncated": truncated,
        "total_chars": total_chars,
        "error": err_code,
        "error_code": err_code,
        "action": "stop_and_manual_check",
        "path": run_rel(end, DOC_FILE),
        "url": url,
        "document_id": _doc_id_from_record(record),
        **{k: summary[k] for k in ("total_parts", "completed_parts", "unverified_parts", "pending_parts", "body_incomplete", "needs_verify")},
    }


def _resolve_ws_path(path_arg: str, workspace_dir: str | None) -> Path:
    return resolve_workspace_target(Path(workspace_dir) if workspace_dir else None, path_arg)


def _write_path_text(path_arg: str, text: str, workspace_dir: str | None) -> None:
    write_target_text(Path(workspace_dir) if workspace_dir else None, path_arg, text)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def cmd_window(args: argparse.Namespace) -> None:
    res = compute_window(base=args.base, end=args.end, now_date=args.now, mode=args.mode)
    _emit(res)
    if not res.get("ok"):
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    sheet_path = _resolve_ws_path(args.sheet, args.workspace_dir) if args.sheet else None
    tickers, _ = screen_movers.build_universe(str(sheet_path) if sheet_path else None)
    show_core = args.core or not args.watch
    show_watch = args.watch or not args.core
    core, watch = core_list(tickers), watch_list(tickers)
    if args.json:
        payload: dict[str, Any] = {
            "ok": True,
            "meta": {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "version": TEMPLATE_VERSION},
        }
        if show_core:
            payload["core"] = core
        if show_watch:
            payload["watch"] = watch
        payload["total"] = (len(core) if show_core else 0) + (len(watch) if show_watch else 0)
        _emit(payload)
        return
    if show_core:
        print(f"=== Core ({len(core)}; searched one by one) ===")
        for item in core:
            live = "  [Live Position]" if item["section"] == "Live Position" else ""
            print(f"  {item['name']} {item['ticker']}  ({', '.join(item['search_names'])}){live}")
    if show_watch:
        print(f"\n=== Watch ({len(watch)}; screened by screen_movers.py) ===")
        by_sec: dict[str, list[str]] = {}
        for item in watch:
            by_sec.setdefault(item["section"], []).append(f"{item['name']} {item['ticker']}")
        for sec, names in by_sec.items():
            print(f"  {sec}: {', '.join(names)}")
    total = (len(core) if show_core else 0) + (len(watch) if show_watch else 0)
    print(f"\nTotal: {total} companies")


def cmd_estimate(args: argparse.Namespace) -> None:
    n_core, n_watch = len(CORE), len(watch_list(screen_movers.TICKERS))
    flagged_assumed = 15
    ae_core = n_core * 2
    payload = {
        "ok": True,
        "core_count": n_core,
        "watch_count": n_watch,
        "watch_flagged_assumed": flagged_assumed,
        "alphaengine_search_calls": {
            "core": ae_core,
            "core_max": n_core * 3,
            "watch_flagged_max": flagged_assumed,
            "note": "core: 2 queries (reports, news), +1 other-language query when rel < 3; watch: <=1 query; sequential, stop on quota error (~190/day)",
        },
        "web_search_calls": n_core + flagged_assumed,
        "community_x_timeline_calls": n_core + flagged_assumed,
        "community_xueqiu_calls": n_core + flagged_assumed,
        "get_document_calls": "keep <= 8 per run (daily full-text quota ~20)",
    }
    if args.json:
        _emit(payload)
        return
    print("Estimated calls per run:")
    print(f"  Core: {n_core} companies, searched one by one")
    print(f"  Watch: {n_watch} screened by price/volume; ~{flagged_assumed} flagged for targeted search")
    print(f"  AlphaEngine search_library: core ~{ae_core} (max {n_core * 3}) + watch <= {flagged_assumed} (sequential)")
    print(f"  web_search: ~{n_core + flagged_assumed}; x-timeline / xueqiu: ~{n_core + flagged_assumed}")
    print("  AlphaEngine get_document: keep <= 8 per run")


def cmd_self_test(_args: argparse.Namespace) -> None:
    checks: list[dict[str, Any]] = []
    tickers = screen_movers.TICKERS
    core_tagged = {name for name, val in tickers.items() if val[3] == "Core"}
    core_named = {c["name"] for c in CORE}
    in_sync = core_tagged == core_named
    checks.append(
        {
            "name": "Core list in sync with screen_movers.TICKERS",
            "ok": in_sync,
            "info": f"core {len(core_named)}, watch {len(watch_list(tickers))}, covered {len(screen_movers.covered(tickers))}",
            "error": (
                ""
                if in_sync
                else f"only in TICKERS: {sorted(core_tagged - core_named)}; only in CORE: {sorted(core_named - core_tagged)}"
            ),
        }
    )
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        import yfinance  # noqa: F401

        checks.append({"name": "market dependencies (yfinance/pandas/numpy)", "ok": True})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "market dependencies (yfinance/pandas/numpy)", "ok": False, "error": str(exc)})

    try:
        kind, _ = resolve_researchdata_transport()
        checks.append({"name": "researchdata sandbox transport", "ok": True, "info": kind})
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {
                "name": "researchdata sandbox transport",
                "ok": False,
                "error": str(exc),
                "note": "expected outside Dalton sandbox; inside agent runs tools.researchdata is injected",
            }
        )

    ok = all(c["ok"] for c in checks[:2])
    _emit({"ok": ok, "checks": checks})
    sys.exit(0 if ok else 1)


def cmd_targets(args: argparse.Namespace) -> None:
    screen_path = _resolve_ws_path(args.screen, args.workspace_dir)
    screen_data = json.loads(screen_path.read_text(encoding="utf-8"))
    try:
        targets = build_targets_from_screen(screen_data)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    _write_path_text(args.out, json.dumps(targets, ensure_ascii=False, indent=1) + "\n", args.workspace_dir)
    _emit(
        {
            "ok": True,
            "core": sum(1 for item in targets if item["tier"] == "Core"),
            "watch": sum(1 for item in targets if item["tier"] == "Watch"),
            "out": args.out,
        }
    )


def _as_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _ae_quota_hit(resp: dict[str, Any]) -> bool:
    return bool(is_quota_or_auth_error(resp))


def _ae_item_blob(item: dict[str, Any]) -> str:
    companies = item.get("companies") or []
    comp_strs: list[str] = []
    if isinstance(companies, list):
        for entry in companies:
            if isinstance(entry, dict):
                comp_strs.append(str(entry.get("name") or entry.get("ticker") or ""))
            else:
                comp_strs.append(str(entry))
    elif isinstance(companies, str):
        comp_strs.append(companies)
    return " ".join(
        [
            str(item.get("title") or ""),
            " ".join(comp_strs),
            str(item.get("snippet") or item.get("summary") or ""),
        ]
    ).lower()


def _rel_count(res: dict[str, Any], keys: list[str]) -> int:
    count = 0
    for item in extract_route_items(res):
        blob = _ae_item_blob(item)
        if any(k and k.lower() in blob for k in keys):
            count += 1
    return count


def _checked_subpath(ws_root: Path | None, path: Path) -> Path:
    return resolve_workspace_target(ws_root, str(path))


def cmd_ae(args: argparse.Namespace, *, caller: BridgeCaller = call_bridge_jsonrpc) -> None:
    ws_root = Path(args.workspace_dir) if args.workspace_dir else None
    tg = json.loads(_resolve_ws_path(args.targets, args.workspace_dir).read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    run_dir = _resolve_ws_path(args.run_dir, args.workspace_dir)
    out = _checked_subpath(ws_root, run_dir / "ae")
    out.mkdir(parents=True, exist_ok=True)
    cats_all = {"res": ["sell_side_report", "sell_side_comment", "meeting_minutes"], "news": ["news", "announcement"]}
    calls = 0
    log_path = _checked_subpath(ws_root, run_dir / "ae_calls.log")
    for item in tg:
        tk = item["ticker"]
        if item["tier"] != args.tier or (only and tk not in only):
            continue
        if args.tier == "Core":
            prim, alt = AE_QUERY.get(tk, (item["kw"][0], None))
            cats = cats_all
        else:
            prim, alt = (WATCH_ALT.get(tk) or [item["name"]])[0], None
            cats = {"res": cats_all["res"]}
        keys = REL_KEYS.get(tk) or (item["kw"] + WATCH_ALT.get(tk, []))
        for cat, cl in cats.items():
            f = _checked_subpath(ws_root, out / f"{_fname(tk)}_{cat}.json")
            if f.exists() and not args.force:
                print(f"{tk}|{cat}|skip (exists)", flush=True)
                continue
            resp = unwrap_bridge_response(
                caller(
                    "research",
                    {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": prim,
                        "start": args.date_from,
                        "end": args.date_to,
                        "limit": 20 if args.tier == "Core" else 15,
                        "params": {"document_categories": cl, "sort": "relevance"},
                    },
                )
            )
            calls += 1
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{dt.datetime.now().isoformat()} {tk} {cat} {prim} ok={resp.get('ok')}\n")
            if _ae_quota_hit(resp):
                print(f"QUOTA at {tk} {cat} after {calls} calls", flush=True)
                sys.exit(3)
            if not resp.get("ok"):
                print(f"ERR {tk} {cat} {str(resp)[:300]}", flush=True)
                continue
            write_target_text(ws_root, str(f), json.dumps(resp, ensure_ascii=False))
            items1 = extract_route_items(resp)
            up1 = extract_nested_value(resp, "total_upstream")
            ret1 = extract_nested_value(resp, "returned")
            if ret1 is None:
                ret1 = len(items1)
            n = _rel_count(resp, keys)
            line = f"{tk}|{cat}|{prim}|up={up1}|ret={ret1}|rel={n}"
            if n < 3 and alt:
                resp2 = unwrap_bridge_response(
                    caller(
                        "research",
                        {
                            "route": "alphaengine",
                            "operation": "search_library",
                            "query": alt,
                            "start": args.date_from,
                            "end": args.date_to,
                            "limit": 20,
                            "params": {"document_categories": cl, "sort": "relevance"},
                        },
                    )
                )
                calls += 1
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"{dt.datetime.now().isoformat()} {tk} {cat}_alt {alt} ok={resp2.get('ok')}\n")
                if _ae_quota_hit(resp2):
                    print(line, flush=True)
                    print(f"QUOTA at {tk} {cat} alt after {calls} calls", flush=True)
                    sys.exit(3)
                if resp2.get("ok"):
                    alt_file = _checked_subpath(ws_root, out / f"{_fname(tk)}_{cat}_alt.json")
                    write_target_text(ws_root, str(alt_file), json.dumps(resp2, ensure_ascii=False))
                    items2 = extract_route_items(resp2)
                    up2 = extract_nested_value(resp2, "total_upstream")
                    ret2 = extract_nested_value(resp2, "returned")
                    if ret2 is None:
                        ret2 = len(items2)
                    line += f" || alt {alt} up={up2} ret={ret2} rel={_rel_count(resp2, keys)}"
            print(line, flush=True)
    print(f"calls {calls}", flush=True)


def cmd_ae_digest(args: argparse.Namespace) -> None:
    ws_root = Path(args.workspace_dir) if args.workspace_dir else None
    run_dir = _resolve_ws_path(args.run_dir, args.workspace_dir)
    d_ = _checked_subpath(ws_root, run_dir / "ae")
    for tk in args.tickers.split(","):
        keys = REL_KEYS.get(tk) or (WATCH_ALT.get(tk, []) + [tk])
        print("=====", tk)
        seen: set[str] = set()
        for cat in ["res", "news"]:
            items: list[dict[str, Any]] = []
            for fname in (f"{_fname(tk)}_{cat}.json", f"{_fname(tk)}_{cat}_alt.json"):
                f = _checked_subpath(ws_root, d_ / fname)
                if not f.exists():
                    continue
                payload = json.loads(f.read_text(encoding="utf-8"))
                for x in extract_route_items(payload):
                    blob = _ae_item_blob(x)
                    doc_id = str(x.get("doc_id") or x.get("document_id") or x.get("id") or "")
                    if not doc_id or doc_id in seen:
                        continue
                    if any(k and k.lower() in blob for k in keys):
                        seen.add(doc_id)
                        items.append(x)
            primary_f = _checked_subpath(ws_root, d_ / f"{_fname(tk)}_{cat}.json")
            if not items and not primary_f.exists():
                continue
            print("--", cat, len(items))
            limit = args.n if cat == "res" else args.news
            for x in items[:limit]:
                pub = str(x.get("publish_time") or x.get("published_at") or x.get("date") or "")[:10]
                srcs = x.get("sources") or ([x["source"]] if x.get("source") else [])
                doc_id = x.get("doc_id") or x.get("document_id") or x.get("id")
                snip = str(x.get("snippet") or x.get("summary") or "")[: args.snip]
                print(f"[{pub}|{','.join(str(s) for s in srcs)}|{doc_id}] {str(x.get('title') or '')[:90]} :: {snip}")


def _x_metrics(x: dict[str, Any]) -> tuple[int, int]:
    metrics = x.get("metrics") if isinstance(x.get("metrics"), dict) else {}
    views_raw = metrics.get("views")
    if views_raw is None:
        views_raw = x.get("viewCount") if x.get("viewCount") is not None else x.get("view_count")
    likes_raw = metrics.get("likes")
    if likes_raw is None:
        likes_raw = x.get("likeCount") if x.get("likeCount") is not None else x.get("like_count")
    return _as_int(views_raw), _as_int(likes_raw)


def cmd_x_timeline(args: argparse.Namespace, *, caller: BridgeCaller = call_bridge_jsonrpc) -> None:
    ws_root = Path(args.workspace_dir) if args.workspace_dir else None
    tg = json.loads(_resolve_ws_path(args.targets, args.workspace_dir).read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    run_dir = _resolve_ws_path(args.run_dir, args.workspace_dir)
    out = _checked_subpath(ws_root, run_dir / "x")
    out.mkdir(parents=True, exist_ok=True)
    for item in tg:
        tk = item["ticker"]
        if only and tk not in only:
            continue
        if not only and item["tier"] != args.tier:
            continue
        q = X_QUERY.get(tk, f"${tk}")
        req: dict[str, Any] = {
            "route": "x-timeline",
            "operation": "search",
            "query": q,
            "since": args.since,
            "count": args.n,
        }
        if args.until:
            req["until"] = args.until
        resp = unwrap_bridge_response(caller("community", req))
        dest = _checked_subpath(ws_root, out / f"{_fname(tk)}.json")
        write_target_text(ws_root, str(dest), json.dumps(resp, ensure_ascii=False))
        items = extract_route_items(resp)
        print(f"{tk} ok={resp.get('ok')} items={len(items)} q={q}", flush=True)
        time.sleep(0.5)


def cmd_x_digest(args: argparse.Namespace) -> None:
    ws_root = Path(args.workspace_dir) if args.workspace_dir else None
    run_dir = _resolve_ws_path(args.run_dir, args.workspace_dir)
    d_ = _checked_subpath(ws_root, run_dir / "x")
    for k in args.keys.split(","):
        try:
            src_f = _checked_subpath(ws_root, d_ / f"{_fname(k)}.json")
            d = json.loads(src_f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print("==", k, "ERR", exc)
            continue
        if isinstance(d, dict) and d.get("ok") is False:
            print("==", k, "ERR ok=False", d.get("error") or d.get("status"))
            continue
        items = extract_route_items(d)
        it = [x for x in items if not x.get("is_repost") and not x.get("isRetweet")]
        it.sort(key=_x_metrics, reverse=True)
        print("=====", k, len(it))
        for x in it[: args.n]:
            created = str(x.get("createdAt") or x.get("created_at") or "")
            date_tag = created[:10]
            try:
                date_tag = dt.datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").strftime("%m-%d")
            except ValueError:
                pass
            views, likes = _x_metrics(x)
            txt = re.sub(r"\s+", " ", str(x.get("text") or ""))[: args.len]
            url = x.get("url") or f"https://x.com/i/status/{x.get('id', '')}"
            print(f"  [{date_tag}|v{views}|l{likes}] {url} :: {txt}")


def _xq_sort_key(p: dict[str, Any]) -> tuple[int, int]:
    views = p.get("view_count") if p.get("view_count") is not None else p.get("views")
    likes = p.get("like_count") if p.get("like_count") is not None else p.get("likes")
    return _as_int(views), _as_int(likes)


def cmd_xq(args: argparse.Namespace, *, caller: BridgeCaller = call_bridge_jsonrpc) -> None:
    ws_root = Path(args.workspace_dir) if args.workspace_dir else None
    tg = json.loads(_resolve_ws_path(args.targets, args.workspace_dir).read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    run_dir = _resolve_ws_path(args.run_dir, args.workspace_dir)
    out = _checked_subpath(ws_root, run_dir / "xq")
    out.mkdir(parents=True, exist_ok=True)
    for item in tg:
        tk = item["ticker"]
        if only and tk not in only:
            continue
        if not only and item["tier"] != args.tier:
            continue
        code = xq_code(tk)
        resp = unwrap_bridge_response(
            caller("community", {"route": "xueqiu", "operation": "posts", "ticker": code, "pages": args.pages})
        )
        raw_dest = _checked_subpath(ws_root, out / f"{code}.json")
        write_target_text(ws_root, str(raw_dest), json.dumps(resp, ensure_ascii=False))
        summary_dest = _checked_subpath(ws_root, out / f"{code}.summary.json")
        if resp.get("ok") is False:
            write_target_text(
                ws_root,
                str(summary_dest),
                json.dumps(
                    {
                        "ok": False,
                        "since": args.since,
                        "error": resp.get("error") or resp.get("status"),
                        "posts": [],
                    },
                    ensure_ascii=False,
                ),
            )
            print(f"ERR {tk} {code} ok=False error={resp.get('error') or resp.get('status')}", flush=True)
            time.sleep(0.5)
            continue
        posts = extract_route_items(resp)
        filtered = [
            p
            for p in posts
            if not args.since or str(p.get("created_at") or p.get("createdAt") or "")[:10] >= args.since
        ]
        filtered.sort(key=_xq_sort_key, reverse=True)
        write_target_text(
            ws_root,
            str(summary_dest),
            json.dumps(
                {
                    "ok": resp.get("ok"),
                    "since": args.since,
                    "raw_count": len(posts),
                    "in_window": len(filtered),
                    "posts": filtered,
                    "error": resp.get("error"),
                },
                ensure_ascii=False,
            ),
        )
        print(f"{tk} {code} ok={resp.get('ok')} raw={len(posts)} in_window={len(filtered)}", flush=True)
        time.sleep(0.5)


def cmd_xq_digest(args: argparse.Namespace) -> None:
    ws_root = Path(args.workspace_dir) if args.workspace_dir else None
    run_dir = _resolve_ws_path(args.run_dir, args.workspace_dir)
    d_ = _checked_subpath(ws_root, run_dir / "xq")
    for k in args.codes.split(","):
        try:
            raw_f = _checked_subpath(ws_root, d_ / f"{k}.json")
            raw = json.loads(raw_f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print("==", k, "ERR", exc)
            continue
        if isinstance(raw, dict) and raw.get("ok") is False:
            print("==", k, "ERR ok=False", raw.get("error") or raw.get("status"))
            continue
        since = getattr(args, "since", None)
        if since is None:
            summary_f = _checked_subpath(ws_root, d_ / f"{k}.summary.json")
            if summary_f.is_file():
                try:
                    summary_data = json.loads(summary_f.read_text(encoding="utf-8"))
                    if isinstance(summary_data, dict):
                        since = summary_data.get("since")
                except json.JSONDecodeError:
                    pass
        items = extract_route_items(raw)
        if since:
            items = [
                p
                for p in items
                if str(p.get("created_at") or p.get("createdAt") or "")[:10] >= since
            ]
        items.sort(key=_xq_sort_key, reverse=True)
        print("=====", k, len(items))
        for x in items[: args.n]:
            views, _ = _xq_sort_key(x)
            txt = re.sub(r"\s+", " ", (str(x.get("title") or "") + " " + str(x.get("text") or ""))).strip()[: args.len]
            print(f"  [{str(x.get('created_at') or '')[5:10]}|v{views}|{x.get('author')}] {x.get('url')} :: {txt}")


def cmd_doc_status(args: argparse.Namespace) -> None:
    ws = Path(args.workspace_dir).resolve()
    end = dt.date.fromisoformat(args.end)
    _emit(doc_status_for(ws, end))


def cmd_save_doc(args: argparse.Namespace) -> None:
    ws = Path(args.workspace_dir).resolve()
    end = dt.date.fromisoformat(args.end)
    if args.file:
        raw = _resolve_ws_path(args.file, args.workspace_dir).read_bytes()
    else:
        raw = args.json_str.encode("utf-8")
    _emit(
        save_doc_record(
            ws,
            end,
            raw,
            total_parts=args.total_parts,
            attempt_id=args.attempt_id,
        )
    )


def cmd_record_append(args: argparse.Namespace) -> None:
    ws = Path(args.workspace_dir).resolve()
    end = dt.date.fromisoformat(args.end)
    if args.file:
        raw = _resolve_ws_path(args.file, args.workspace_dir).read_bytes()
    else:
        raw = args.json_str.encode("utf-8")
    _emit(record_doc_append(ws, end, args.part, raw))


def cmd_verify_append(args: argparse.Namespace) -> None:
    ws = Path(args.workspace_dir).resolve()
    end = dt.date.fromisoformat(args.end)
    doc_readback = (
        _resolve_ws_path(args.doc_json_file, args.workspace_dir).read_bytes()
        if args.doc_json_file
        else args.doc_json_str
    )
    part_text = (
        _resolve_ws_path(args.part_file, args.workspace_dir).read_text(encoding="utf-8")
        if args.part_file
        else args.part_text
    )
    expected_after = (
        _resolve_ws_path(args.expected_after_file, args.workspace_dir).read_text(encoding="utf-8")
        if args.expected_after_file
        else args.expected_after_text
    )
    res = verify_doc_append(
        ws,
        end,
        args.part,
        doc_readback,
        part_text=part_text,
        expected_after=expected_after,
        total_parts=args.total_parts,
    )
    _emit(res)
    if not res.get("ok"):
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Richard Weekly Coverage Dalton CLI")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("window")
    p.add_argument("--base", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--now", default=None)
    p.add_argument("--mode", default="auto", choices=["auto", "manual"])
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("list")
    p.add_argument("--core", action="store_true")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--sheet", default=None)
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("estimate")
    p.add_argument("--json", action="store_true")

    sp.add_parser("self-test")

    p = sp.add_parser("targets")
    p.add_argument("--screen", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("ae")
    for flag in ("--targets", "--run-dir", "--date-from", "--date-to"):
        p.add_argument(flag, required=True)
    p.add_argument("--tier", default="Core", choices=["Core", "Watch"])
    p.add_argument("--only", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("ae-digest")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--tickers", required=True)
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--snip", type=int, default=260)
    p.add_argument("--news", type=int, default=4)
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("x-timeline")
    for flag in ("--targets", "--run-dir", "--since"):
        p.add_argument(flag, required=True)
    p.add_argument("--until", default=None)
    p.add_argument("--only", default=None)
    p.add_argument("--tier", default="Core")
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("x-digest")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--keys", required=True)
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--len", type=int, default=220)
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("xq")
    for flag in ("--targets", "--run-dir", "--since"):
        p.add_argument(flag, required=True)
    p.add_argument("--only", default=None)
    p.add_argument("--tier", default="Core")
    p.add_argument("--pages", type=int, default=2)
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("xq-digest")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--codes", required=True)
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--len", type=int, default=160)
    p.add_argument("--since", default=None)
    p.add_argument("--workspace-dir", default=None)

    p = sp.add_parser("doc-status")
    p.add_argument("--end", required=True)
    p.add_argument("--workspace-dir", required=True)

    p = sp.add_parser("save-doc")
    p.add_argument("--end", required=True)
    p.add_argument("--workspace-dir", required=True)
    p.add_argument("--total-parts", type=int, required=True)
    p.add_argument("--attempt-id", required=True)
    src_grp = p.add_mutually_exclusive_group(required=True)
    src_grp.add_argument("--file")
    src_grp.add_argument("--json-str")

    p = sp.add_parser("record-append")
    p.add_argument("--end", required=True)
    p.add_argument("--workspace-dir", required=True)
    p.add_argument("--part", type=int, required=True)
    app_grp = p.add_mutually_exclusive_group(required=True)
    app_grp.add_argument("--file")
    app_grp.add_argument("--json-str")

    p = sp.add_parser("verify-append")
    p.add_argument("--end", required=True)
    p.add_argument("--workspace-dir", required=True)
    p.add_argument("--part", type=int, required=True)
    doc_grp = p.add_mutually_exclusive_group(required=True)
    doc_grp.add_argument("--doc-json-file")
    doc_grp.add_argument("--doc-json-str")
    part_grp = p.add_mutually_exclusive_group(required=True)
    part_grp.add_argument("--part-file")
    part_grp.add_argument("--part-text")
    p.add_argument("--expected-after-file", default=None)
    p.add_argument("--expected-after-text", default=None)
    p.add_argument("--total-parts", type=int, default=None)

    args = ap.parse_args()
    handlers = {
        "window": cmd_window,
        "list": cmd_list,
        "estimate": cmd_estimate,
        "self-test": cmd_self_test,
        "targets": cmd_targets,
        "ae": cmd_ae,
        "ae-digest": cmd_ae_digest,
        "x-timeline": cmd_x_timeline,
        "x-digest": cmd_x_digest,
        "xq": cmd_xq,
        "xq-digest": cmd_xq_digest,
        "doc-status": cmd_doc_status,
        "save-doc": cmd_save_doc,
        "record-append": cmd_record_append,
        "verify-append": cmd_verify_append,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
