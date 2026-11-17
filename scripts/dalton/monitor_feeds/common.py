#!/usr/bin/env python3
"""Shared runtime helpers for Dalton monitor feeds (state paths, calendars, API push, UI shell)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
import html
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("dalton.monitor_feeds.common")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATE_BASE = REPO_ROOT / "var" / "dalton" / "monitor-feeds"

CN_TZ = ZoneInfo("Asia/Shanghai")
HK_TZ = ZoneInfo("Asia/Hong_Kong")
US_TZ = ZoneInfo("America/New_York")

MarketKind = Literal["cn", "hk"]


def now_cn() -> datetime:
    """Current Beijing time (Asia/Shanghai)."""
    return datetime.now(CN_TZ)


def now_hk() -> datetime:
    """Current Hong Kong time (Asia/Hong_Kong)."""
    return datetime.now(HK_TZ)


def now_us() -> datetime:
    """Current US Eastern time (America/New_York)."""
    return datetime.now(US_TZ)


def format_dt_cn(dt: datetime | None = None) -> str:
    target = (dt or now_cn()).astimezone(CN_TZ)
    return target.strftime("%Y-%m-%d %H:%M:%S")


def format_dt_us(dt: datetime | None = None) -> str:
    target = (dt or now_us()).astimezone(US_TZ)
    return target.strftime("%Y-%m-%d %H:%M:%S")


def resolve_state_dir(name: str, state_dir: str | Path | None = None) -> Path:
    """Resolve state directory for feed ``name`` without touching ``~/.openclaw``."""
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("Feed name must not be empty")
    if state_dir is not None and str(state_dir).strip():
        p = Path(state_dir).expanduser()
    else:
        env_base = os.environ.get("DALTON_MONITOR_FEEDS_STATE_DIR", "").strip()
        p = Path(env_base).expanduser() / clean if env_base else DEFAULT_STATE_BASE / clean
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, *, indent: int | None = None) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )
    atomic_write_text(path, text)


def load_json_safe(path: Path, default: Any = None) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Unified Per-Source Status & Single-Point last_success_at Ledger (status.json)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceStatus:
    """Result reported by one collector/validator step; never sets last_success_at directly."""

    name: str
    ok: bool
    stale: bool = False
    reason: str | None = None
    last_success_at: str | None = None
    as_of: str | None = None
    label: str | None = None
    note: str | None = None
    counts: dict[str, int] | None = None

    @property
    def is_clean(self) -> bool:
        return bool(self.ok) and (not bool(self.stale))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label or self.name,
            "ok": bool(self.ok),
            "stale": bool(self.stale),
            "reason": self.reason,
            "last_success_at": self.last_success_at,
            "as_of": self.as_of,
        }
        if self.note is not None:
            out["note"] = self.note
        if self.counts is not None:
            out["counts"] = dict(self.counts)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceStatus:
        raw_counts = data.get("counts")
        counts = (
            {str(k): int(v) for k, v in raw_counts.items()}
            if isinstance(raw_counts, dict)
            else None
        )
        return cls(
            name=str(data.get("name") or ""),
            ok=bool(data.get("ok")),
            stale=bool(data.get("stale", not bool(data.get("ok")))),
            reason=data.get("reason"),
            last_success_at=data.get("last_success_at"),
            as_of=data.get("as_of"),
            label=data.get("label"),
            note=data.get("note"),
            counts=counts,
        )


def make_source_result(
    name: str,
    *,
    ok: bool,
    stale: bool | None = None,
    reason: str | None = None,
    as_of: str | None = None,
    label: str | None = None,
    note: str | None = None,
    counts: dict[str, int] | None = None,
) -> SourceStatus:
    """Create a collector status result; callers never advance ``last_success_at`` here."""
    eff_stale = (not ok) if stale is None else bool(stale)
    return SourceStatus(
        name=name,
        ok=bool(ok) and not eff_stale,
        stale=eff_stale,
        reason=reason if eff_stale or not ok else None,
        last_success_at=None,
        as_of=as_of,
        label=label or name,
        note=note,
        counts=dict(counts) if counts is not None else None,
    )


def format_source_statuses(
    sources: dict[str, dict[str, Any]] | None,
    *,
    only: Iterable[str] | None = None,
) -> str:
    """Format failing/stale entries per source as '名称：数据未更新（上次成功：时间，原因）'."""
    if not sources:
        return ""
    keys = list(only) if only is not None else list(sources.keys())
    parts: list[str] = []
    for k in keys:
        ent = sources.get(k)
        if not isinstance(ent, dict):
            continue
        if ent.get("ok") and not ent.get("stale"):
            continue
        label = ent.get("label") or k
        last_ok = ent.get("last_success_at") or "无"
        reason = ent.get("reason") or "来源采集失败"
        parts.append(f"{label}：数据未更新（上次成功：{last_ok}，原因：{reason}）")
    return "；".join(parts)


class SourceStatusStore:
    """Persistent per-monitor source status table (``status.json``).

    Single source of truth for advancing ``last_success_at``:
    a source's ``last_success_at`` advances iff ``ok and not stale``;
    board ``overall.ok`` is true iff all required sources are clean.
    """

    def __init__(self, state_dir: str | Path, *, filename: str = "status.json") -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / filename
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            raw = load_json_safe(self.path, {})
            if not isinstance(raw, dict):
                raw = {}
            return {
                "sources": dict(raw.get("sources") or {}),
                "overall": dict(raw.get("overall") or {}),
                "updated": raw.get("updated"),
            }

    @staticmethod
    def _merge_doc(
        raw: dict[str, Any],
        items: list[SourceStatus],
        *,
        required: Iterable[str] | None,
        stamp: str,
        extra_overall: dict[str, Any] | None,
        preserved_sources: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        sources: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in (raw.get("sources") or {}).items() if isinstance(v, dict)
        }
        if preserved_sources:
            for k, v in preserved_sources.items():
                if isinstance(v, dict):
                    sources[k] = dict(v)
        prev_overall = dict(raw.get("overall") or {})

        for res in items:
            prev = dict(sources.get(res.name) or {})
            clean = res.is_clean
            if clean:
                last_ok = stamp
                reason = None
                as_of = res.as_of or stamp
            else:
                last_ok = prev.get("last_success_at") or res.last_success_at
                reason = res.reason or "数据采集或校验失败"
                as_of = prev.get("as_of") or res.as_of
            ent: dict[str, Any] = {
                "name": res.name,
                "label": res.label or prev.get("label") or res.name,
                "ok": clean,
                "stale": not clean,
                "reason": reason,
                "last_success_at": last_ok,
                "as_of": as_of,
            }
            if res.note is not None:
                ent["note"] = res.note
            if res.counts is not None:
                ent["counts"] = dict(res.counts)
            sources[res.name] = ent

        req_keys = list(required) if required is not None else list(sources.keys())
        stale_sources: list[str] = []
        for rk in req_keys:
            ent = sources.get(rk)
            if not ent or (not ent.get("ok")) or ent.get("stale"):
                stale_sources.append(rk)

        board_ok = len(stale_sources) == 0 and len(req_keys) > 0
        overall_last_ok = stamp if board_ok else prev_overall.get("last_success_at")
        reason_text = format_source_statuses(sources, only=req_keys)
        overall: dict[str, Any] = {
            "ok": board_ok,
            "stale": not board_ok,
            "stale_sources": stale_sources,
            "reason": reason_text or None,
            "last_success_at": overall_last_ok,
            "updated": stamp,
        }
        if extra_overall:
            overall.update(extra_overall)

        return {
            "updated": stamp,
            "sources": sources,
            "overall": overall,
        }

    def preview_sources(
        self,
        results: dict[str, SourceStatus] | Iterable[SourceStatus],
        *,
        required: Iterable[str] | None = None,
        now_str: str | None = None,
        extra_overall: dict[str, Any] | None = None,
        preserved_sources: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compute merged status document in memory without touching status.json."""
        items: list[SourceStatus] = (
            list(results.values()) if isinstance(results, dict) else list(results)
        )
        stamp = now_str or format_dt_cn()
        with self._lock:
            raw = load_json_safe(self.path, {})
            if not isinstance(raw, dict):
                raw = {}
            return self._merge_doc(
                raw,
                items,
                required=required,
                stamp=stamp,
                extra_overall=extra_overall,
                preserved_sources=preserved_sources,
            )

    def update_sources(
        self,
        results: dict[str, SourceStatus] | Iterable[SourceStatus],
        *,
        required: Iterable[str] | None = None,
        now_str: str | None = None,
        extra_overall: dict[str, Any] | None = None,
        preserved_sources: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        items: list[SourceStatus] = (
            list(results.values()) if isinstance(results, dict) else list(results)
        )
        stamp = now_str or format_dt_cn()
        with self._lock:
            raw = load_json_safe(self.path, {})
            if not isinstance(raw, dict):
                raw = {}
            doc = self._merge_doc(
                raw,
                items,
                required=required,
                stamp=stamp,
                extra_overall=extra_overall,
                preserved_sources=preserved_sources,
            )
            atomic_write_json(self.path, doc, indent=1)
            return doc


# Required publishable source constants per monitor dashboard (used for job exit codes):
# A run exits 0 iff page HTML rendered cleanly AND all required publishable sources succeeded
# (so partial optional-source degradation publishes the page with per-source expiry notices).
ASHARE_REQUIRED_PUBLISH_SOURCES: tuple[str, ...] = ("indices", "breadth")
HK_REQUIRED_PUBLISH_SOURCES: tuple[str, ...] = ("hsi_history", "index_compute")
HK_MONTH_END_REQUIRED_SOURCES: tuple[str, ...] = (
    "hkex_universe",
    "ggt_list",
    "hk_spot",
    "hk_history",
    "hsi_history",
    "index_compute",
    "html_render",
)
PM_REQUIRED_L1_SOURCES: tuple[str, ...] = ("us",)
PM_REQUIRED_L2_MARKET_SOURCES: tuple[str, ...] = ("us", "cn")


# ---------------------------------------------------------------------------
# Exchange Trading Calendars (XSHG / XHKG via exchange_calendars)
# ---------------------------------------------------------------------------


class CalendarLookupError(RuntimeError):
    """Raised when exchange calendar lookup fails instead of silently falling back."""


@lru_cache(maxsize=4)
def _get_exchange_calendar(cal_name: str):
    import exchange_calendars as xcals

    return xcals.get_calendar(cal_name)


def _calendar_name(market: MarketKind) -> str:
    return "XSHG" if market == "cn" else "XHKG"


def _market_tz(market: MarketKind) -> ZoneInfo:
    return CN_TZ if market == "cn" else HK_TZ


def _default_windows(market: MarketKind) -> tuple[int, int, int, int]:
    return (
        9 * 60 + 30,
        (11 * 60 + 30) if market == "cn" else 12 * 60,
        13 * 60,
        (15 * 60) if market == "cn" else (16 * 60),
    )


def _to_local_hm(ts_val: Any, tz: ZoneInfo) -> int:
    if hasattr(ts_val, "tz_convert"):
        dt = ts_val.tz_convert(tz).to_pydatetime()
    elif hasattr(ts_val, "astimezone"):
        dt = ts_val.astimezone(tz)
    else:
        raise ValueError(f"Unsupported calendar timestamp: {ts_val!r}")
    return dt.hour * 60 + dt.minute


def is_trading_day(market: MarketKind, day: date) -> bool:
    """Return True if ``day`` is an exchange trading session (raises on calendar failure)."""
    cal_name = _calendar_name(market)
    day_str = day.isoformat()
    try:
        cal = _get_exchange_calendar(cal_name)
        return bool(cal.is_session(day_str))
    except Exception as exc:
        logger.error("交易所日历 %s 查询交易日 %s 失败: %s", cal_name, day_str, exc)
        raise CalendarLookupError(f"交易所日历 {cal_name} 查询 {day_str} 失败: {exc}") from exc


def get_session_schedule(market: MarketKind, day: date) -> dict[str, Any]:
    """Read open, lunch-break, and close windows from ``exchange_calendars`` for ``day``."""
    cal_name = _calendar_name(market)
    tz = _market_tz(market)
    day_str = day.isoformat()
    if not is_trading_day(market, day):
        return {
            "market": market,
            "trade_date": day_str,
            "is_trading_day": False,
            "windows": [],
            "am_start": None,
            "am_end": None,
            "pm_start": None,
            "pm_end": None,
            "has_break": False,
            "using_default_schedule": False,
            "session_source": "calendar",
            "session_note": None,
        }

    try:
        cal = _get_exchange_calendar(cal_name)
        am_start = _to_local_hm(cal.session_open(day_str), tz)
        close_hm = _to_local_hm(cal.session_close(day_str), tz)
        has_break = bool(cal.session_has_break(day_str))
        if has_break:
            b_start_raw = cal.session_break_start(day_str)
            b_end_raw = cal.session_break_end(day_str)
            if b_start_raw is None or b_end_raw is None:
                raise ValueError("session_has_break=True 但午休时间为空")
            am_end = _to_local_hm(b_start_raw, tz)
            pm_start = _to_local_hm(b_end_raw, tz)
            windows = [[am_start, am_end], [pm_start, close_hm]]
        else:
            am_end = close_hm
            pm_start = None
            windows = [[am_start, close_hm]]
        return {
            "market": market,
            "trade_date": day_str,
            "is_trading_day": True,
            "windows": windows,
            "am_start": am_start,
            "am_end": am_end,
            "pm_start": pm_start,
            "pm_end": close_hm,
            "has_break": has_break,
            "using_default_schedule": False,
            "session_source": "calendar",
            "session_note": None,
        }
    except Exception as exc:
        am_start, am_end, pm_start, pm_end = _default_windows(market)
        logger.warning(
            "无法从 %s 日历获取 %s 详细开收盘/午休时间，使用默认交易时段: %s",
            cal_name,
            day_str,
            exc,
        )
        return {
            "market": market,
            "trade_date": day_str,
            "is_trading_day": True,
            "windows": [[am_start, am_end], [pm_start, pm_end]],
            "am_start": am_start,
            "am_end": am_end,
            "pm_start": pm_start,
            "pm_end": pm_end,
            "has_break": True,
            "using_default_schedule": True,
            "session_source": "default",
            "session_note": "交易时段为默认值",
        }


def previous_trading_day(market: MarketKind, day: date) -> date:
    """Return the latest exchange trading day strictly before ``day``."""
    cursor = day - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(market, cursor):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def last_trading_day_of_month(market: MarketKind, year: int, month: int) -> date:
    """Return the final scheduled exchange session date in ``(year, month)``."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    cursor = next_month - timedelta(days=1)
    while cursor.month == month:
        if is_trading_day(market, cursor):
            return cursor
        cursor -= timedelta(days=1)
    raise ValueError(f"No trading day found in {year:04d}-{month:02d} for {market}")


def _hm_to_dtime(hm: int) -> dtime:
    return dtime(hm // 60, hm % 60)


def market_session_state(market: MarketKind, now: datetime | None = None) -> dict[str, Any]:
    """Return session state using per-day exchange calendar open/break/close hours."""
    tz = _market_tz(market)
    cur = (now or datetime.now(tz)).astimezone(tz)
    d = cur.date()
    hm = cur.hour * 60 + cur.minute
    try:
        sched = get_session_schedule(market, d)
    except CalendarLookupError as exc:
        return {
            "market": market,
            "is_trading_day": False,
            "in_trading_window": False,
            "status": "calendar_error",
            "label": "交易日历异常",
            "trade_date": d.isoformat(),
            "now_str": cur.strftime("%Y-%m-%d %H:%M:%S"),
            "hm": hm,
            "windows": [],
            "am_start": None,
            "close_hm": None,
            "using_default_schedule": False,
            "session_source": "calendar_error",
            "session_note": str(exc),
            "calendar_error": str(exc),
        }

    if not sched["is_trading_day"]:
        return {
            "market": market,
            "is_trading_day": False,
            "in_trading_window": False,
            "status": "holiday",
            "label": "休市（非交易日）",
            "trade_date": d.isoformat(),
            "now_str": cur.strftime("%Y-%m-%d %H:%M:%S"),
            "hm": hm,
            "windows": [],
            "am_start": None,
            "close_hm": None,
            "using_default_schedule": False,
            "session_source": sched["session_source"],
            "session_note": sched["session_note"],
            "calendar_error": None,
        }

    am_start = int(sched["am_start"])
    am_end = int(sched["am_end"])
    close_hm = int(sched["pm_end"])
    has_break = bool(sched["has_break"])
    pm_start = int(sched["pm_start"]) if has_break and sched["pm_start"] is not None else None

    if hm < am_start:
        status, label, in_win = "pre_open", "盘前未开盘", False
    elif hm <= am_end:
        status, label, in_win = "trading", "交易中", True
    elif has_break and pm_start is not None and am_end < hm < pm_start:
        status, label, in_win = "lunch_break", "午间休市", False
    elif has_break and pm_start is not None and pm_start <= hm <= close_hm:
        status, label, in_win = "trading", "交易中", True
    else:
        status, label, in_win = "closed", "已收盘", False

    return {
        "market": market,
        "is_trading_day": True,
        "in_trading_window": in_win,
        "status": status,
        "label": label,
        "trade_date": d.isoformat(),
        "now_str": cur.strftime("%Y-%m-%d %H:%M:%S"),
        "hm": hm,
        "windows": sched["windows"],
        "am_start": am_start,
        "close_hm": close_hm,
        "using_default_schedule": bool(sched["using_default_schedule"]),
        "session_source": sched["session_source"],
        "session_note": sched["session_note"],
        "calendar_error": None,
    }


def resolve_market_trade_date(
    market: MarketKind,
    *,
    now: datetime | None = None,
    as_of_date: date | str | None = None,
) -> date:
    """Return latest closed or currently active trading day for ``market``.

    When ``as_of_date`` is omitted, uses ``now`` in market timezone: returns today if
    today is a trading day and current time is at or after ``am_start``; otherwise
    (pre-open before ``am_start`` or non-trading day) returns ``previous_trading_day``.
    """
    if as_of_date is not None:
        d = date.fromisoformat(as_of_date) if isinstance(as_of_date, str) else as_of_date
        return d if is_trading_day(market, d) else previous_trading_day(market, d)
    tz = _market_tz(market)
    raw_now = now or datetime.now(tz)
    cur = raw_now.replace(tzinfo=tz) if raw_now.tzinfo is None else raw_now.astimezone(tz)
    sess = market_session_state(market, cur)
    if sess.get("calendar_error"):
        raise CalendarLookupError(str(sess["calendar_error"]))
    am_start = int(sess["am_start"]) if sess.get("am_start") is not None else _default_windows(market)[0]
    if sess.get("is_trading_day") and (cur.hour * 60 + cur.minute) >= am_start:
        return cur.date()
    return previous_trading_day(market, cur.date())


def next_session_open(market: MarketKind, now: datetime | None = None) -> datetime:
    """Return the datetime of the next trading session start for ``market``."""
    tz = _market_tz(market)
    cur = (now or datetime.now(tz)).astimezone(tz)
    st = market_session_state(market, cur)
    if st.get("calendar_error"):
        return cur + timedelta(minutes=5)
    d = cur.date()
    if st["is_trading_day"]:
        sched = get_session_schedule(market, d)
        if st["status"] == "pre_open" and sched["am_start"] is not None:
            return datetime.combine(d, _hm_to_dtime(int(sched["am_start"])), tzinfo=tz)
        if st["status"] == "lunch_break" and sched["pm_start"] is not None:
            return datetime.combine(d, _hm_to_dtime(int(sched["pm_start"])), tzinfo=tz)
        if st["status"] == "trading":
            return cur
    cursor = d + timedelta(days=1)
    for _ in range(30):
        try:
            if is_trading_day(market, cursor):
                nxt_sched = get_session_schedule(market, cursor)
                open_hm = int(nxt_sched["am_start"] or _default_windows(market)[0])
                return datetime.combine(cursor, _hm_to_dtime(open_hm), tzinfo=tz)
        except CalendarLookupError:
            return cur + timedelta(minutes=5)
        cursor += timedelta(days=1)
    return datetime.combine(cursor, dtime(9, 30), tzinfo=tz)


# ---------------------------------------------------------------------------
# Internal Service API Client (reads INTERNAL_SERVICE_TOKEN strictly from env)
# ---------------------------------------------------------------------------


def push_monitor_data(
    slug: str,
    data_key: str,
    payload: Any,
    *,
    as_of: str | None = None,
    backend_url: str | None = None,
    token: str | None = None,
    timeout: float = 15.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Write realtime payload to ``PUT /api/v1/dalton/monitors/internal/{slug}/data/{key}``."""
    base = (backend_url or os.environ.get("DALTON_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")
    tok = token if token is not None else os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("INTERNAL_SERVICE_TOKEN 未配置，无法写入监控实时数据")
    url = f"{base}/api/v1/dalton/monitors/internal/{slug}/data/{data_key}"
    body: dict[str, Any] = {"data": payload}
    if as_of is not None:
        body["as_of"] = as_of
    headers = {
        "X-Service-Token": tok,
        "Content-Type": "application/json",
    }
    client = session or requests
    resp = client.put(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.text[:300]
        except Exception:
            pass
        raise RuntimeError(f"写入 {slug}/{data_key} 失败 (HTTP {resp.status_code}): {detail}")
    return resp.json() if resp.content else {"ok": True}


# ---------------------------------------------------------------------------
# Shared Dark-Navy Visual Theme, Color Legend & Monitor Grant Poller JS
# ---------------------------------------------------------------------------

SHARED_MONITOR_CSS = r"""
:root{
  --bg:#07101f;--panel:#0f1b2d;--panel2:#13243b;--line:#253a57;
  --text:#edf4ff;--muted:#91a3bd;--blue:#4da3ff;--yellow:#ffbf3c;
  --dir-up:#ff6b6b;--dir-down:#38d39f;
  --dir-up-bg:rgba(255,107,107,.08);--dir-down-bg:rgba(56,211,159,.08);
  --temp-cold:#4da3ff;--temp-cool:#7fc0ff;--temp-neutral:#aebdd2;
  --temp-warm:#ffc266;--temp-hot:#ff9d3c;--purple:#a377ff;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at 20% -10%,#17335d 0,transparent 35%),var(--bg);
  color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:13px;line-height:1.5}
.top{padding:22px 28px 12px;max-width:1600px;margin:auto}
.top-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
.top h1{margin:0;font-size:24px;font-weight:750;letter-spacing:.3px}
.top p{color:var(--muted);margin:6px 0}
.top-meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.top-link{color:var(--blue);font-size:13px;font-weight:600;text-decoration:none}
.top-link:hover{text-decoration:underline}
.color-legend{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;background:#0e1a2d;
  border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:10px 0 4px;font-size:12px;color:#c5d2e5}
.lg-item{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.lg-swatch{display:inline-block;width:10px;height:10px;border-radius:3px}
.lg-up{background:var(--dir-up)}.lg-down{background:var(--dir-down)}
.lg-cold{background:var(--temp-cold)}.lg-neutral{background:var(--temp-neutral)}.lg-hot{background:var(--temp-hot)}
.lg-pink{background:#f778ba}.lg-gray{background:#7d8590}
.lg-sep{color:var(--line)}.lg-note{color:var(--muted)}
.stale-banner{background:#342214;border:1px solid #8a5f24;color:#ffd099;border-radius:10px;
  padding:9px 13px;font-size:12.5px;margin:10px 0 0;display:none}
.stale-banner.show{display:block}
.flow-status{font-size:12px;color:var(--muted);background:#16273f;border:1px solid var(--line);
  padding:5px 12px;border-radius:999px;white-space:nowrap;font-variant-numeric:tabular-nums}
.flow-status.live{color:#8fe3c4;border-color:#2f6b57}
.flow-status.stale{color:#ffc98a;border-color:#8a5f24}
.wrap{max-width:1600px;margin:auto;padding:12px 28px 56px}
.stack{display:grid;gap:16px}
.grid{display:grid;gap:16px}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:2fr 1fr}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.metric,.panel{background:linear-gradient(145deg,rgba(19,36,59,.96),rgba(12,25,43,.96));
  border:1px solid var(--line);border-radius:14px;box-shadow:0 12px 32px rgba(0,0,0,.18)}
.metric{padding:15px 16px;border-top:3px solid var(--temp-neutral)}
.metric.hot{border-top-color:var(--temp-hot)}.metric.warm{border-top-color:var(--temp-warm)}
.metric.cold{border-top-color:var(--temp-cold)}.metric.cool{border-top-color:var(--temp-cool)}
.metric.up-bar{border-top-color:var(--dir-up)}.metric.down-bar{border-top-color:var(--dir-down)}
.metric-title{font-size:12px;color:var(--muted)}.metric-value{font-size:26px;font-weight:750;margin:6px 0;font-variant-numeric:tabular-nums}
.metric-sub{font-size:13px}.metric-meta{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.4}
.panel{padding:16px 18px;min-width:0}
.panel-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap;margin-bottom:10px}
.panel h2,.panel h3{margin:0;font-size:16px;font-weight:700}
.panel-sub{font-size:12px;color:var(--muted);margin-top:3px}
.up{color:var(--dir-up)}.down{color:var(--dir-down)}.muted{color:var(--muted)}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{border:1px solid var(--line);background:#0c1728;color:var(--muted);padding:5px 12px;border-radius:10px;
  cursor:pointer;font-size:12px;user-select:none}
.tab:hover{border-color:var(--blue);color:var(--text)}
.tab.active,.tab.on{background:var(--blue);border-color:var(--blue);color:#04101f;font-weight:700}
.table-wrap{overflow:auto;max-height:580px;border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:8px 10px;border-bottom:1px solid #1b2d47;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;background:#0b1728;position:sticky;top:0;z-index:2;font-size:11.5px}
tr:hover td{background:rgba(77,163,255,.06)}
.note{font-size:12px;color:var(--muted);margin:10px 0 0;line-height:1.6}
.note.rule{background:#101d31;border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.empty{padding:28px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid transparent}
footer{max-width:1600px;margin:auto;padding:0 28px 32px;color:var(--muted);font-size:11.5px;line-height:1.7}
@media(max-width:1050px){.g2,.g3{grid-template-columns:1fr}.top,.wrap,footer{padding-left:16px;padding-right:16px}}
"""


def render_color_legend(
    dir_items: list[tuple[str, str]],
    secondary_items: list[tuple[str, str]],
    note: str = "",
) -> str:
    """Render an explicit, single-meaning color legend header block."""
    left = "".join(
        f'<span class="lg-item"><i class="lg-swatch {html.escape(cls)}"></i>{html.escape(label)}</span>'
        for cls, label in dir_items
    )
    right = "".join(
        f'<span class="lg-item"><i class="lg-swatch {html.escape(cls)}"></i>{html.escape(label)}</span>'
        for cls, label in secondary_items
    )
    sep = '<span class="lg-sep">|</span>' if (left and right) else ""
    note_html = f'<span class="lg-note">{html.escape(note)}</span>' if note else ""
    return (
        '<div class="color-legend" aria-label="颜色图例">'
        f"{left}{sep}{right}{note_html}"
        "</div>"
    )


def _build_session_calendar_map(market: MarketKind | None, days: int = 21) -> dict[str, Any]:
    if market not in ("cn", "hk"):
        return {}
    today = datetime.now(_market_tz(market)).date()
    cal_map: dict[str, list[list[int]]] = {}
    max_date = today.isoformat()
    for offset in range(-1, days + 1):
        d = today + timedelta(days=offset)
        ds = d.isoformat()
        if ds > max_date:
            max_date = ds
        try:
            sched = get_session_schedule(market, d)
        except CalendarLookupError:
            return {}
        if sched["is_trading_day"] and sched["windows"]:
            cal_map[ds] = sched["windows"]
    return {"maxDate": max_date, "sessions": cal_map}


def build_grant_poller_js(
    data_key: str,
    *,
    on_data_fn: str = "onMonitorData",
    on_error_fn: str = "onMonitorPollError",
    on_stale_fn: str = "onMonitorStale",
    interval_ms: int = 60_000,
    fetch_timeout_ms: int = 12_000,
    stale_after_ms: int = 180_000,
    market: MarketKind | None = None,
) -> str:
    """Return JS polling ``/data/{key}`` with AbortController timeout & 3m session freshness check."""
    key_js = json.dumps(data_key)
    cal_js = json.dumps(_build_session_calendar_map(market), ensure_ascii=False)
    default_wins = (
        [[570, 690], [780, 900]]
        if market == "cn"
        else ([[570, 720], [780, 960]] if market == "hk" else [])
    )
    default_wins_js = json.dumps(default_wins)
    return f"""
(function () {{
  var DATA_KEY = {key_js};
  var POLL_MS = {int(interval_ms)};
  var FETCH_TIMEOUT_MS = {int(fetch_timeout_ms)};
  var STALE_AFTER_MS = {int(stale_after_ms)};
  var SESSION_CAL = {cal_js};
  var DEFAULT_WINS = {default_wins_js};
  var etag = null;
  var dataBase = null;
  var lastDataTsMs = NaN;

  function inferDataBaseFromPath() {{
    var m = /^(.*?\\/api\\/v1\\/dalton\\/monitors\\/(?:g|public)\\/[^\\/]+)\\/v\\//.exec(location.pathname);
    return m ? (m[1] + '/data') : null;
  }}

  function resolveBase() {{
    if (window.__DALTON_MONITOR__ && window.__DALTON_MONITOR__.dataBase) {{
      dataBase = window.__DALTON_MONITOR__.dataBase;
    }} else if (!dataBase) {{
      dataBase = inferDataBaseFromPath();
    }}
    return dataBase;
  }}

  function parseBjTsMs(raw) {{
    if (!raw) return NaN;
    var s = String(raw).trim().replace(' ', 'T');
    if (!/(Z|[+-]\\d{{2}}:?\\d{{2}})$/.test(s)) s += '+08:00';
    var t = Date.parse(s);
    return Number.isNaN(t) ? NaN : t;
  }}

  function currentBjDateHm() {{
    var bj = new Date(Date.now() + 8 * 3600 * 1000);
    var y = bj.getUTCFullYear();
    var m = String(bj.getUTCMonth() + 1).padStart(2, '0');
    var d = String(bj.getUTCDate()).padStart(2, '0');
    var hm = bj.getUTCHours() * 60 + bj.getUTCMinutes();
    return {{ dateStr: y + '-' + m + '-' + d, hm: hm, dow: bj.getUTCDay() }};
  }}

  function isNowInTradingSession(payload) {{
    var cur = currentBjDateHm();
    if (payload && payload.trade_date === cur.dateStr) {{
      if (payload.is_trading_day === false) return false;
      if (Array.isArray(payload.windows) && payload.windows.length) {{
        return payload.windows.some(function (w) {{ return cur.hm >= w[0] && cur.hm <= w[1]; }});
      }}
    }}
    if (SESSION_CAL && SESSION_CAL.maxDate && cur.dateStr <= SESSION_CAL.maxDate) {{
      var wins = (SESSION_CAL.sessions || {{}})[cur.dateStr];
      if (!wins) return false;
      return wins.some(function (w) {{ return cur.hm >= w[0] && cur.hm <= w[1]; }});
    }}
    if (cur.dow === 0 || cur.dow === 6 || !DEFAULT_WINS.length) return false;
    return DEFAULT_WINS.some(function (w) {{ return cur.hm >= w[0] && cur.hm <= w[1]; }});
  }}

  function checkDataFreshness(payload) {{
    if (payload && (payload.as_of_iso || payload.as_of)) {{
      var parsed = parseBjTsMs(payload.as_of_iso || payload.as_of);
      if (Number.isFinite(parsed)) lastDataTsMs = parsed;
    }}
    var curPayload = payload || window.__DALTON_LAST_PAYLOAD__ || null;
    if (!Number.isFinite(lastDataTsMs) && curPayload && (curPayload.as_of_iso || curPayload.as_of)) {{
      lastDataTsMs = parseBjTsMs(curPayload.as_of_iso || curPayload.as_of);
    }}
    if (!Number.isFinite(lastDataTsMs)) return false;
    if (!isNowInTradingSession(curPayload)) {{
      if (typeof window[{json.dumps(on_stale_fn)}] === 'function') window[{json.dumps(on_stale_fn)}](null);
      return false;
    }}
    if (Date.now() - lastDataTsMs > STALE_AFTER_MS) {{
      if (typeof window[{json.dumps(on_stale_fn)}] === 'function') {{
        window[{json.dumps(on_stale_fn)}]('交易时段内数据时间超过 3 分钟未更新，采集可能已中断');
      }}
      return true;
    }}
    if (typeof window[{json.dumps(on_stale_fn)}] === 'function') window[{json.dumps(on_stale_fn)}](null);
    return false;
  }}

  async function pollOnce() {{
    var base = resolveBase();
    if (!base) {{
      checkDataFreshness();
      return;
    }}
    var headers = {{}};
    if (etag) headers['If-None-Match'] = etag;
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = controller ? setTimeout(function () {{ controller.abort(); }}, FETCH_TIMEOUT_MS) : null;
    try {{
      var opts = {{ headers: headers }};
      if (controller) opts.signal = controller.signal;
      var resp = await fetch(base + '/' + encodeURIComponent(DATA_KEY), opts);
      if (resp.status === 304) {{
        checkDataFreshness();
        return;
      }}
      if (!resp.ok) {{
        {on_error_fn}('HTTP ' + resp.status);
        return;
      }}
      etag = resp.headers.get('ETag') || etag;
      var body = await resp.json();
      if (body && body.data) window.__DALTON_LAST_PAYLOAD__ = body.data;
      checkDataFreshness(body && body.data ? body.data : null);
      {on_data_fn}(body);
    }} catch (err) {{
      var msg = (err && err.name === 'AbortError')
        ? ('请求超时 (' + Math.round(FETCH_TIMEOUT_MS / 1000) + ' 秒)')
        : String((err && err.message) || err || '网络请求失败');
      {on_error_fn}(msg);
    }} finally {{
      if (timer) clearTimeout(timer);
    }}
  }}

  window.addEventListener('dalton:monitor-grant', function (ev) {{
    if (ev && ev.detail && ev.detail.dataBase) {{
      dataBase = ev.detail.dataBase;
      pollOnce();
    }}
  }});

  window.__DALTON_POLL_NOW__ = pollOnce;
  window.__DALTON_CHECK_FRESHNESS__ = checkDataFreshness;
  checkDataFreshness();
  pollOnce();
  setInterval(pollOnce, POLL_MS);
  setInterval(function () {{ checkDataFreshness(); }}, 15000);
}})();
"""
