#!/usr/bin/env python3
"""A-share intraday monitor collector (indices, breadth, turnover, SW3 flow, ETF radar).

Fixes vs legacy local server:
- Two-market turnover uses SH Composite (sh000001) + SZ Composite (sz399106),
  matching the sentiment dashboard (instead of 500-stock sz399001).
- Uses exchange trading calendar (XSHG) rather than weekend-only checks.
- Tracks per-source failures explicitly: retains last valid values with
  ``stale=True``, ``stale_reason``, and ``last_success_at``.
- State directory is configurable (default ``var/dalton/monitor-feeds/ashare_monitor``).
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import sqlite3
import ssl
import threading
import time
from typing import Any, Callable
import urllib.parse
import urllib.request

from scripts.dalton.monitor_feeds.common import (
    ASHARE_REQUIRED_PUBLISH_SOURCES,
    CN_TZ,
    CalendarLookupError,
    SourceStatusStore,
    atomic_write_json,
    is_trading_day,
    load_json_safe,
    make_source_result,
    market_session_state,
    now_cn,
    resolve_state_dir,
)

HERE = Path(__file__).resolve().parent
DEFAULT_SW3_MAPPING = HERE / "sw3_mapping.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

DISPLAY_INDICES: tuple[tuple[str, str], ...] = (
    ("s_sh000001", "上证指数"),
    ("s_sh000852", "中证1000"),
    ("s_sh000905", "中证500"),
    ("s_sz399006", "创业板指"),
    ("s_sh000300", "沪深300"),
    ("s_sz399001", "深证成指"),
    ("s_sh000688", "科创50"),
)
TURNOVER_EXTRA_INDICES: tuple[tuple[str, str], ...] = (
    ("s_sz399106", "深证综指"),
)
ALL_FETCH_INDICES = DISPLAY_INDICES + TURNOVER_EXTRA_INDICES

ETF_GROUPS = (
    ("industry", "半导体", ("芯片", "半导体", "集成电路")),
    ("industry", "软件", ("软件", "信创", "云计算", "计算机")),
    ("industry", "人工智能", ("人工智能", "AIETF", "算力", "大数据")),
    ("industry", "传媒", ("传媒", "游戏", "动漫", "影视")),
    ("industry", "消费", ("消费", "白酒", "食品", "家电", "零售", "农业", "畜牧")),
    ("industry", "医药医疗", ("医药", "医疗", "生物", "创新药", "中药")),
    ("industry", "新能源", ("新能源", "光伏", "电池", "储能", "绿电", "风电")),
    ("industry", "军工", ("军工", "国防", "航天", "航空")),
    ("industry", "机器人装备", ("机器人", "工业母机", "高端装备")),
    ("industry", "通信电子", ("通信", "5G", "消费电子", "电子ETF")),
    ("industry", "金融地产", ("银行", "证券", "券商", "保险", "地产")),
    ("industry", "汽车", ("汽车", "智能车")),
    ("broad", "沪深300", ("沪深300",)),
    ("broad", "上证50", ("上证50",)),
    ("broad", "中证A500", ("中证A500", "A500ETF")),
    ("broad", "科创板", ("科创50", "科创100", "科创200", "科创综指", "科创板")),
    ("broad", "创业板", ("创业板", "创50", "创蓝筹", "创成长", "创科技")),
    ("broad", "中证500", ("中证500",)),
    ("broad", "中证1000", ("中证1000",)),
    ("broad", "小微盘", ("中证2000", "国证2000", "小盘", "微盘")),
    ("broad", "全市场", ("中证全指", "国证A指")),
)
ETF_DIRECTIONS = tuple((cat, direction) for cat, direction, _ in ETF_GROUPS)

ETF_PAGE_SIZE = 100
# Full ETF and stock scans are ~70 Eastmoney requests; at the original 30s/120s
# cadence (plus the old OpenClaw terminal) the host throttled this IP to 502s
# (2026-11-17). Breadth and indices still refresh every minute.
ETF_SCAN_TTL_TRADING = 120
ETF_SCAN_TTL_CLOSED = 300
ETF_REPRESENTATIVES = 3
ETF_MIN_TURNOVER_YI = 0.05
ETF_LEADER_COUNT = 12
ETF_LEADER_POOL = 36
ETF_LEADER_MIN_TURNOVER_YI = 0.30
ETF_LEADER_MIN_FLOW_YI = 0.10
RADAR_ALERT_SCORE = 60
OFFICIAL_SHARE_TTL = 30 * 60
STOCK_PAGE_SIZE = 100
# Eastmoney's clist endpoint answers 502 for a large share of pages under six
# concurrent requests (measured 2026-11-17: 9/14 pages at 6 workers, 14/14 at
# 2). Fetch with two workers, back off between attempts, and try the second
# host on the last attempt before a page counts as missing.
EASTMONEY_PAGE_WORKERS = 2
EASTMONEY_HOSTS = ("push2delay.eastmoney.com", "push2.eastmoney.com")
EASTMONEY_PAGE_ATTEMPTS = 3


def _fetch_page_with_retry(fetch, validate, page: int) -> tuple[list[dict[str, Any]], str | None]:
    last_err: str | None = None
    for attempt in range(EASTMONEY_PAGE_ATTEMPTS):
        host = EASTMONEY_HOSTS[-1] if attempt == EASTMONEY_PAGE_ATTEMPTS - 1 else EASTMONEY_HOSTS[0]
        try:
            pdiff, _, perr = validate(fetch(host), page=page)
            if not perr:
                return pdiff, None
            last_err = perr
        except Exception as exc:  # network errors become a missing page, never a silent gap
            last_err = str(exc)
        time.sleep(1.0 * (attempt + 1))
    return [], last_err


def _fetch_first_page_with_retry(fetch, validate) -> tuple[list[dict[str, Any]], int, str | None]:
    """Page 1 carries the total, so it gets the same paced retries as the rest."""
    last_err: str | None = None
    for attempt in range(EASTMONEY_PAGE_ATTEMPTS):
        host = EASTMONEY_HOSTS[-1] if attempt == EASTMONEY_PAGE_ATTEMPTS - 1 else EASTMONEY_HOSTS[0]
        try:
            pdiff, total, perr = validate(fetch(host), page=1)
            if not perr:
                return pdiff, total, None
            last_err = perr
        except Exception as exc:
            last_err = str(exc)
        time.sleep(1.0 * (attempt + 1))
    return [], 0, last_err
STOCK_SCAN_TTL_TRADING = 300
STOCK_SCAN_TTL_CLOSED = 600
HISTORY_TTL = 15 * 60

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def default_http_get(
    url: str,
    *,
    referer: str | None = None,
    decode: str = "utf-8",
    timeout: float = 15.0,
) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read()
    return raw.decode(decode, "replace")


def default_http_get_json(url: str, *, timeout: float = 15.0) -> Any:
    return json.loads(default_http_get(url, timeout=timeout))


def classify_etf(name: str) -> tuple[str, str] | None:
    n = (name or "").upper().replace(" ", "")
    if not n or "ETF" not in n:
        return None
    for category, direction, words in ETF_GROUPS:
        if any(w in n for w in words):
            return category, direction
    return None


def etf_track_key(name: str, direction: str) -> str:
    n = (name or "").upper().replace(" ", "")
    keys = {
        "沪深300": ("沪深300",),
        "上证50": ("上证50",),
        "中证A500": ("中证A500", "A500"),
        "中证500": ("中证500",),
        "中证1000": ("中证1000",),
        "小微盘": ("中证2000", "国证2000", "小盘", "微盘"),
        "创业板": ("创业板50", "创50", "创业板", "创蓝筹", "创成长", "创科技"),
        "科创板": ("科创50", "科创100", "科创200", "科创综指", "科创"),
        "半导体": ("科创芯片", "科创半导体", "半导体设备", "集成电路", "芯片", "半导体"),
    }
    for key in keys.get(direction, ()):
        if key in n:
            return {
                "中证A500": "A500",
                "中证A50": "A50",
                "创业板50": "创业板50",
                "创50": "创业板50",
            }.get(key, key)
    return direction


def _strip_jsonp(text: str) -> Any:
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("invalid JSONP")
    return json.loads(text[start + 1 : end])


class _SohuTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_td = False
        self.cell: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag == "td":
            self.in_td = True
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.in_td:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self.in_td = False
            self.row.append("".join(self.cell).strip())
        elif tag == "tr" and self.row:
            self.rows.append(self.row)


def trading_progress(now: datetime | None = None) -> tuple[int, int, float]:
    cur = (now or now_cn()).astimezone(CN_TZ)
    t = cur.hour * 60 + cur.minute
    am_s, am_e = 9 * 60 + 30, 11 * 60 + 30
    pm_s, pm_e = 13 * 60, 15 * 60
    elapsed = 0
    if t >= am_s:
        elapsed += min(t, am_e) - am_s
    if t >= pm_s:
        elapsed += min(t, pm_e) - pm_s
    elapsed = max(0, min(elapsed, 240))
    return elapsed, 240, (elapsed / 240.0 if elapsed else 0.0)


def _trading_minute_index(minute: str) -> int:
    h, m = map(int, minute.split(":"))
    raw = h * 60 + m
    if raw <= 11 * 60 + 30:
        return raw - (9 * 60 + 30)
    return 120 + raw - 13 * 60


def _minute_gap(a: str, b: str) -> int:
    try:
        return _trading_minute_index(b) - _trading_minute_index(a)
    except Exception:
        return 1


def _valid_session_minute(minute: str) -> bool:
    return "09:30" <= minute <= "11:30" or "13:00" <= minute <= "15:00"


def _sample_minute(now: datetime | None = None) -> str | None:
    cur = (now or now_cn()).astimezone(CN_TZ)
    st = market_session_state("cn", cur)
    if not st["is_trading_day"]:
        return None
    t = cur.hour * 60 + cur.minute
    if t < 9 * 60 + 30:
        return None
    if t <= 11 * 60 + 30:
        return cur.strftime("%H:%M")
    if t < 13 * 60:
        return "11:30"
    if t <= 15 * 60:
        return cur.strftime("%H:%M")
    return "15:00"


def _score_direction(row: dict[str, Any]) -> tuple[int, str]:
    flow = row["net_flow_yi"]
    turnover = row["turnover_yi"]
    share = abs(row["flow_share_pct"] or 0.0)
    flow_sign = 1 if flow > 0 else (-1 if flow < 0 else 0)
    score = min(22.0, share / 8.0 * 22.0)
    score += min(10.0, abs(flow) / max(3.0, turnover * 0.06) * 10.0)
    mshare = abs(row.get("delta_share_5m") or 0.0)
    score += min(10.0, mshare / 10.0 * 10.0)
    reps = row.get("representatives") or []
    if reps and flow_sign:
        rep_turnover = sum(r.get("turnover_yi", 0.0) for r in reps)
        aligned_turnover = sum(
            r.get("turnover_yi", 0.0) for r in reps if r["inflow_yi"] * flow_sign > 0
        )
        sync = aligned_turnover / rep_turnover if rep_turnover else 0.0
        score += max(0.0, (sync - 0.5) * 2.0) * 12.0
        leader_share = max(
            (
                r["inflow_yi"] * flow_sign / r["turnover_yi"] * 100.0
                for r in reps
                if r.get("turnover_yi", 0) > 0
            ),
            default=0.0,
        )
        score += min(4.0, max(0.0, leader_share) / 10.0 * 4.0)
    vr = [r.get("vratio") for r in reps if isinstance(r.get("vratio"), (int, float))]
    if vr:
        score += min(16.0, max(0.0, sum(vr) / len(vr) - 1.0) / 1.5 * 16.0)
    pctile = row.get("history_percentile")
    if isinstance(pctile, (int, float)) and row.get("history_days", 0) >= 3:
        tail = pctile if flow_sign >= 0 else 100.0 - pctile
        score += max(0.0, tail - 50.0) / 50.0 * 8.0
    price_move = abs(row.get("weighted_pct") or 0.0)
    score += min(10.0, price_move / 3.0 * 10.0)
    return_pctile = row.get("return_percentile")
    if isinstance(return_pctile, (int, float)) and row.get("return_history_days", 0) >= 3:
        score += max(0.0, return_pctile - 50.0) / 50.0 * 8.0
    final = round(min(100.0, score))
    if final < 40:
        signal = "正常"
    elif share >= 1.0 and flow_sign > 0:
        signal = "强异常流入" if final >= 75 else ("异常流入" if final >= 60 else "流入关注")
    elif share >= 1.0 and flow_sign < 0:
        signal = "强异常流出" if final >= 75 else ("异常流出" if final >= 60 else "流出关注")
    elif (row.get("weighted_pct") or 0.0) >= 0:
        signal = "放量上涨" if final >= 60 else "上涨关注"
    else:
        signal = "放量下跌" if final >= 60 else "下跌关注"
    return final, signal


def _pre_score_etf(row: dict[str, Any], direction_score: int = 0) -> float:
    turnover = row.get("turnover_yi", 0.0)
    flow = abs(row.get("inflow_yi", 0.0))
    share = abs(
        row.get("share")
        if isinstance(row.get("share"), (int, float))
        else (flow / turnover * 100.0 if turnover else 0.0)
    )
    price_move = abs(row.get("pct") or 0.0)
    return (
        min(35.0, share / 10.0 * 35.0)
        + min(25.0, flow / 5.0 * 25.0)
        + min(15.0, turnover / 2.0 * 15.0)
        + min(15.0, price_move / 3.0 * 15.0)
        + min(10.0, direction_score / 80.0 * 10.0)
    )


def _score_etf(row: dict[str, Any], direction_score: int = 0) -> tuple[int, str]:
    turnover = row.get("turnover_yi", 0.0)
    raw_flow = row.get("inflow_yi", 0.0)
    flow = abs(raw_flow)
    share = abs(
        row.get("share")
        if isinstance(row.get("share"), (int, float))
        else (raw_flow / turnover * 100.0 if turnover else 0.0)
    )
    score = min(20.0, share / 10.0 * 20.0)
    score += min(15.0, flow / 5.0 * 15.0)
    score += min(10.0, abs(row.get("delta_share_5m") or 0.0) / 10.0 * 10.0)
    score += min(10.0, direction_score / 80.0 * 10.0)
    vratio = row.get("vratio")
    if isinstance(vratio, (int, float)):
        score += min(20.0, max(0.0, vratio - 1.0) / 1.5 * 20.0)
    pctile = row.get("history_percentile")
    if isinstance(pctile, (int, float)) and row.get("history_days", 0) >= 3:
        tail = pctile if raw_flow >= 0 else 100.0 - pctile
        score += max(0.0, tail - 50.0) / 50.0 * 10.0
    score += min(10.0, abs(row.get("pct") or 0.0) / 3.0 * 10.0)
    return_pctile = row.get("return_percentile")
    if isinstance(return_pctile, (int, float)) and row.get("return_history_days", 0) >= 3:
        score += max(0.0, return_pctile - 50.0) / 50.0 * 5.0
    confidence = min(1.0, turnover / 1.0)
    final = round(min(100.0, score) * (0.75 + 0.25 * confidence))
    if final >= 70:
        signal = "强异常流入" if raw_flow >= 0 else "强异常流出"
    elif final >= 55:
        signal = "异常流入" if raw_flow >= 0 else "异常流出"
    elif final >= 40:
        signal = "流入关注" if raw_flow >= 0 else "流出关注"
    else:
        signal = "正常"
    return final, signal


class AShareCollector:
    """Stateful A-share intraday collector with SQLite minute series & stale tracking."""

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        mapping_path: str | Path | None = None,
        http_get: Callable[..., str] | None = None,
        http_get_json: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_dir = resolve_state_dir("ashare_monitor", state_dir)
        self.db_path = self.state_dir / "intraday.db"
        self.snapshot_cache_path = self.state_dir / "last_snapshot.json"
        if mapping_path is not None:
            self.mapping_path = Path(mapping_path)
        elif (self.state_dir / "sw3_mapping.json").is_file():
            self.mapping_path = self.state_dir / "sw3_mapping.json"
        else:
            self.mapping_path = DEFAULT_SW3_MAPPING
        self._get = http_get or default_http_get
        self._get_json = http_get_json or default_http_get_json
        self._now = now_fn or now_cn
        self._lock = threading.Lock()
        self._etf_scan_cache: dict[str, Any] = {
            "ts": 0.0,
            "rows": [],
            "total": 0,
            "error": None,
            "last_success_at": None,
        }
        self._vratio_cache: dict[str, dict[str, Any]] = {}
        self._stock_scan_cache: dict[str, Any] = {
            "ts": 0.0,
            "rows": [],
            "error": None,
            "last_success_at": None,
        }
        self._history_cache: dict[str, Any] = {
            "ts": 0.0,
            "rows": [],
            "error": None,
            "last_success_at": None,
        }
        self._official_cache: dict[str, Any] = {
            "ts": 0.0,
            "codes": set(),
            "data": {},
            "error": None,
            "last_success_at": None,
        }
        self._sw3_mapping_cache: dict[str, Any] = {"mtime": 0.0, "data": None}
        self._status_store = SourceStatusStore(self.state_dir)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=40)
        self._last_alert_key: dict[str, int] = {}
        self._last_good: dict[str, Any] = load_json_safe(self.snapshot_cache_path, {}) or {}
        self._init_db()

    def _prior_source_success(self, key: str) -> str | None:
        sources = self._status_store.load().get("sources") or {}
        ent = sources.get(key)
        return str(ent["last_success_at"]) if isinstance(ent, dict) and ent.get("last_success_at") else None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS vol (day TEXT, ts INTEGER, minute TEXT, cum_yi REAL, PRIMARY KEY(day, minute))"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS breadth (day TEXT, ts INTEGER, minute TEXT, up INTEGER, down INTEGER, flat INTEGER, PRIMARY KEY(day, minute))"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS daily_market (day TEXT PRIMARY KEY, up INTEGER, down INTEGER, flat INTEGER, amount_yi REAL, limit_up INTEGER, limit_down INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS etf_direction (day TEXT, ts INTEGER, minute TEXT, direction TEXT, flow_yi REAL, turnover_yi REAL, pct REAL, PRIMARY KEY(day, minute, direction))"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_etf_direction_lookup ON etf_direction(direction, minute, day)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS etf_flow (day TEXT, ts INTEGER, minute TEXT, code TEXT, flow_yi REAL, turnover_yi REAL, pct REAL, PRIMARY KEY(day, minute, code))"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_etf_flow_lookup ON etf_flow(code, minute, day)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS etf_units (trade_day TEXT, code TEXT, units_wan REAL, exchange TEXT, PRIMARY KEY(trade_day, code))"
                )
                conn.commit()
            finally:
                conn.close()

    def _load_sw3_mapping(self) -> dict[str, Any] | None:
        try:
            if not self.mapping_path.is_file():
                return None
            mtime = self.mapping_path.stat().st_mtime
            if self._sw3_mapping_cache["data"] is None or mtime != self._sw3_mapping_cache["mtime"]:
                self._sw3_mapping_cache = {
                    "mtime": mtime,
                    "data": json.loads(self.mapping_path.read_text(encoding="utf-8")),
                }
            return self._sw3_mapping_cache["data"]
        except Exception:
            return None

    # ---- Source fetchers --------------------------------------------------

    def fetch_indices(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]], str | None]:
        ids = ",".join(c for c, _ in ALL_FETCH_INDICES)
        try:
            txt = self._get(
                f"https://hq.sinajs.cn/list={ids}",
                referer="https://finance.sina.com.cn",
                decode="gbk",
                timeout=10,
            )
            by_code: dict[str, dict[str, float]] = {}
            for line in txt.splitlines():
                if "=" not in line:
                    continue
                code = line.split("=")[0].replace("var hq_str_", "").strip()
                payload = line.split('"')[1] if '"' in line else ""
                if not payload:
                    continue
                p = payload.split(",")
                try:
                    by_code[code] = {
                        "price": float(p[1]),
                        "change": float(p[2]),
                        "pct": float(p[3]),
                        "amount_wan": float(p[5]),
                    }
                except (ValueError, IndexError):
                    continue
            out = [
                {"name": name, **by_code[code]}
                for code, name in DISPLAY_INDICES
                if code in by_code
            ]
            if not out or "s_sh000001" not in by_code or "s_sz399106" not in by_code:
                missing = [
                    c for c, _ in ALL_FETCH_INDICES if c not in by_code
                ]
                return out, by_code, f"指数行情返回不完整（缺少: {', '.join(missing)}）"
            return out, by_code, None
        except Exception as exc:
            return [], {}, f"指数行情拉取失败: {exc}"

    def fetch_breadth(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            j = self._get_json(
                "https://push2.eastmoney.com/api/qt/ulist.np/get"
                "?fields=f104,f105,f106&secids=1.000001,0.399001,0.899050",
                timeout=10,
            )
            diff = j["data"]["diff"]
            if not diff:
                return None, "涨跌家数接口返回空"
            up = sum(int(d["f104"]) for d in diff)
            down = sum(int(d["f105"]) for d in diff)
            flat = sum(int(d["f106"]) for d in diff)
            ratio = round(up / down, 2) if down else 0.0
            return {"up": up, "down": down, "flat": flat, "ratio": ratio}, None
        except Exception as exc:
            return None, f"涨跌家数拉取失败: {exc}"

    def _fetch_etf_page(self, page: int, host: str = EASTMONEY_HOSTS[0]) -> dict[str, Any]:
        params = urllib.parse.urlencode(
            {
                "pn": page,
                "pz": ETF_PAGE_SIZE,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f6",
                "fs": "b:MK0021",
                "fields": "f2,f3,f6,f12,f13,f14,f20,f62,f184",
            }
        )
        return self._get_json(
            f"https://{host}/api/qt/clist/get?" + params,
            timeout=15,
        )

    @staticmethod
    def _validate_clist_page(payload: Any, *, page: int) -> tuple[list[dict[str, Any]], int, str | None]:
        if not isinstance(payload, dict):
            return [], 0, f"第 {page} 页响应格式无效"
        data = payload.get("data")
        if not isinstance(data, dict):
            return [], 0, f"第 {page} 页返回 data:null"
        diff = data.get("diff")
        if not isinstance(diff, list) or not diff:
            return [], 0, f"第 {page} 页返回空 diff"
        if page == 1:
            raw_total = data.get("total")
            if raw_total is None or isinstance(raw_total, bool):
                return [], 0, "第 1 页缺少 total"
            try:
                if isinstance(raw_total, float) and not raw_total.is_integer():
                    raise ValueError("non-integer total")
                total = int(raw_total)
            except (TypeError, ValueError):
                return [], 0, f"第 1 页 total 无效 ({raw_total!r})"
            if total <= 0:
                return [], 0, f"第 1 页 total={total} 无效（diff 非空）"
            return list(diff), total, None
        return list(diff), 0, None

    def fetch_etf_market(self) -> tuple[list[dict[str, Any]], int, str | None]:
        now = time.time()
        trading = market_session_state("cn", self._now())["in_trading_window"]
        base_ttl = ETF_SCAN_TTL_TRADING if trading else ETF_SCAN_TTL_CLOSED
        eff_ttl = min(base_ttl, 55.0) if self._etf_scan_cache.get("error") else base_ttl
        if self._etf_scan_cache["rows"] and now - self._etf_scan_cache["ts"] < eff_ttl:
            return (
                self._etf_scan_cache["rows"],
                self._etf_scan_cache["total"],
                self._etf_scan_cache.get("error"),
            )
        if self._etf_scan_cache.get("error") and now - self._etf_scan_cache["ts"] < eff_ttl:
            return [], 0, self._etf_scan_cache["error"]
        try:
            raw, total, first_err = _fetch_first_page_with_retry(
                lambda host: self._fetch_etf_page(1, host=host), self._validate_clist_page
            )
            if first_err:
                raise RuntimeError(first_err)
            pages = max(1, math.ceil(total / ETF_PAGE_SIZE))
            page_errors = 0
            page_err_msgs: list[str] = []
            if pages > 1:
                def _safe(p: int):
                    return _fetch_page_with_retry(
                        lambda host: self._fetch_etf_page(p, host=host),
                        self._validate_clist_page,
                        p,
                    )

                with ThreadPoolExecutor(max_workers=EASTMONEY_PAGE_WORKERS) as ex:
                    for pdiff, perr in ex.map(_safe, range(2, pages + 1)):
                        if perr:
                            page_errors += 1
                            page_err_msgs.append(perr)
                        raw.extend(pdiff)

            if page_errors > 0 or len(raw) != total:
                prev_ok = self._prior_source_success("etf_radar")
                detail = page_err_msgs[0] if page_err_msgs else f"记录数({len(raw)})与总数({total})不符"
                err = f"ETF 分页不完整 ({detail}；上次成功：{prev_ok or '无'})"
                self._etf_scan_cache["ts"] = now
                self._etf_scan_cache["error"] = err
                return self._etf_scan_cache["rows"], self._etf_scan_cache["total"], err

            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for d in raw:
                code = str(d.get("f12") or "")
                market = d.get("f13")
                name = d.get("f14") or ""
                if not code or market not in (0, 1) or code in seen:
                    continue
                seen.add(code)
                classified = classify_etf(name)
                if not classified:
                    continue
                category, direction = classified
                turnover = d.get("f6")
                flow = d.get("f62")
                price = d.get("f2")
                pct = d.get("f3")
                mcap = d.get("f20")
                rows.append(
                    {
                        "secid": f"{market}.{code}",
                        "code": code,
                        "name": name,
                        "category": category,
                        "direction": direction,
                        "track_key": etf_track_key(name, direction),
                        "price": price if isinstance(price, (int, float)) and not isinstance(price, bool) else None,
                        "pct": pct if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None,
                        "turnover_yi": round(turnover / 1e8, 3) if isinstance(turnover, (int, float)) and not isinstance(turnover, bool) else 0.0,
                        "inflow_yi": round(flow / 1e8, 3) if isinstance(flow, (int, float)) and not isinstance(flow, bool) else 0.0,
                        "share": d.get("f184") if isinstance(d.get("f184"), (int, float)) and not isinstance(d.get("f184"), bool) else None,
                        "market_cap_yi": round(mcap / 1e8, 2) if isinstance(mcap, (int, float)) and not isinstance(mcap, bool) else None,
                    }
                )
            if not rows:
                raise RuntimeError("场内 ETF 扫描返回空列表")
            self._etf_scan_cache = {
                "ts": now,
                "rows": rows,
                "total": total,
                "error": None,
            }
            return rows, total, None
        except Exception as exc:
            prev_ok = self._prior_source_success("etf_radar")
            err = f"场内 ETF 扫描失败（上次成功：{prev_ok or '无'}）: {exc}"
            self._etf_scan_cache["ts"] = now
            self._etf_scan_cache["error"] = err
            return self._etf_scan_cache["rows"], self._etf_scan_cache["total"], err

    def _etf_vratio(self, secid: str) -> tuple[float | None, str | None]:
        try:
            j = self._get_json(
                f"https://push2delay.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f50,f57",
                timeout=10,
            )
            data = j.get("data") if isinstance(j, dict) else None
            if not isinstance(data, dict):
                return None, f"量比返回缺少 data ({secid})"
            f50 = data.get("f50")
            if isinstance(f50, (int, float)) and not isinstance(f50, bool) and not math.isnan(float(f50)) and float(f50) >= 0:
                return round(float(f50) / 100.0, 2), None
            return None, f"量比缺少有效 f50 ({secid})"
        except Exception as exc:
            return None, f"量比拉取失败 ({secid}): {exc}"

    def _fetch_representative_vratios(self, reps: list[dict[str, Any]]) -> str | None:
        now = time.time()
        need: list[dict[str, Any]] = []
        seen_secids: set[str] = set()
        for r in reps:
            sid = r["secid"]
            if sid in seen_secids:
                continue
            seen_secids.add(sid)
            ent = self._vratio_cache.get(sid)
            ttl = 55.0 if (ent and ent.get("error")) else 60.0
            if not ent or now - ent["ts"] > ttl:
                need.append(r)

        if need:
            prev_ok = self._prior_source_success("etf_vratio")
            with ThreadPoolExecutor(max_workers=min(12, len(need))) as ex:
                vals = ex.map(self._etf_vratio, [r["secid"] for r in need])
                for r, (val, err) in zip(need, vals):
                    sid = r["secid"]
                    prev = self._vratio_cache.get(sid) or {}
                    if err:
                        self._vratio_cache[sid] = {
                            "ts": now,
                            "val": prev.get("val"),
                            "error": f"{err}（上次成功：{prev_ok or '无'}）",
                        }
                    else:
                        self._vratio_cache[sid] = {
                            "ts": now,
                            "val": val,
                            "error": None,
                        }

        errs: list[str] = []
        for r in reps:
            ent = self._vratio_cache.get(r["secid"]) or {}
            r["vratio"] = ent.get("val")
            if ent.get("error") and ent["error"] not in errs:
                errs.append(str(ent["error"]))
        if errs:
            return f"ETF 量比拉取异常 ({len(errs)} 只，如 {errs[0]})"
        return None

    def _fetch_stock_page(self, page: int, host: str = EASTMONEY_HOSTS[0]) -> dict[str, Any]:
        params = urllib.parse.urlencode(
            {
                "pn": page,
                "pz": STOCK_PAGE_SIZE,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
                "fields": "f3,f6,f12,f14,f62",
            }
        )
        return self._get_json(
            f"https://{host}/api/qt/clist/get?" + params,
            timeout=25,
        )

    def fetch_stock_market(self) -> tuple[list[dict[str, Any]], str | None]:
        now = time.time()
        trading = market_session_state("cn", self._now())["in_trading_window"]
        base_ttl = STOCK_SCAN_TTL_TRADING if trading else STOCK_SCAN_TTL_CLOSED
        eff_ttl = min(base_ttl, 55.0) if self._stock_scan_cache.get("error") else base_ttl
        if self._stock_scan_cache["rows"] and now - self._stock_scan_cache["ts"] < eff_ttl:
            return self._stock_scan_cache["rows"], self._stock_scan_cache.get("error")
        if self._stock_scan_cache.get("error") and now - self._stock_scan_cache["ts"] < eff_ttl:
            return [], self._stock_scan_cache["error"]
        try:
            raw, total, first_err = _fetch_first_page_with_retry(
                lambda host: self._fetch_stock_page(1, host=host), self._validate_clist_page
            )
            if first_err:
                raise RuntimeError(first_err)
            pages = max(1, math.ceil(total / STOCK_PAGE_SIZE))
            page_errors = 0
            page_err_msgs: list[str] = []
            if pages > 1:
                def _safe(p: int):
                    return _fetch_page_with_retry(
                        lambda host: self._fetch_stock_page(p, host=host),
                        self._validate_clist_page,
                        p,
                    )

                with ThreadPoolExecutor(max_workers=EASTMONEY_PAGE_WORKERS) as pool:
                    for pdiff, perr in pool.map(_safe, range(2, pages + 1)):
                        if perr:
                            page_errors += 1
                            page_err_msgs.append(perr)
                        raw.extend(pdiff)

            if page_errors > 0 or len(raw) != total:
                prev_ok = self._prior_source_success("industry")
                detail = page_err_msgs[0] if page_err_msgs else f"记录数({len(raw)})与总数({total})不符"
                err = f"个股资金分页不完整 ({detail}；上次成功：{prev_ok or '无'})"
                # Preserve last complete rows; do not cache incomplete rows
                self._stock_scan_cache["ts"] = now
                self._stock_scan_cache["error"] = err
                return self._stock_scan_cache["rows"], err

            rows: list[dict[str, Any]] = []
            for item in raw:
                code = str(item.get("f12") or "").zfill(6)
                turnover, flow, pct = item.get("f6"), item.get("f62"), item.get("f3")
                if not code.isdigit() or not isinstance(turnover, (int, float)):
                    continue
                rows.append(
                    {
                        "code": code,
                        "name": item.get("f14") or "",
                        "pct": pct if isinstance(pct, (int, float)) else 0.0,
                        "turnover_yi": turnover / 1e8,
                        "inflow_yi": flow / 1e8 if isinstance(flow, (int, float)) else 0.0,
                    }
                )
            if not rows:
                raise RuntimeError("全市场个股资金扫描返回空")
            self._stock_scan_cache = {
                "ts": now,
                "rows": rows,
                "error": None,
            }
            return rows, None
        except Exception as exc:
            prev_ok = self._prior_source_success("industry")
            err = f"个股主力资金拉取失败（上次成功：{prev_ok or '无'}）: {exc}"
            self._stock_scan_cache["ts"] = now
            self._stock_scan_cache["error"] = err
            return self._stock_scan_cache["rows"], err

    def fetch_industry_flow(self) -> tuple[dict[str, Any], str | None]:
        mapping = self._load_sw3_mapping()
        if not mapping:
            return {
                "rows": [],
                "source": "申万2021三级行业",
                "mapping_asof": None,
                "industry_count": 0,
                "coverage_pct": None,
            }, "申万三级行业映射文件不可用"
        stocks, stock_err = self.fetch_stock_market()
        if not stocks:
            return {
                "rows": [],
                "source": mapping.get("source", "申万2021三级行业"),
                "mapping_asof": mapping.get("asof"),
                "industry_count": 0,
                "coverage_pct": None,
            }, stock_err or "个股资金数据为空"

        by_stock = mapping.get("stocks") or {}
        groups: dict[str, dict[str, Any]] = {}
        covered_turnover = 0.0
        total_turnover = sum(r["turnover_yi"] for r in stocks)
        for stock in stocks:
            meta = by_stock.get(stock["code"])
            if not meta:
                continue
            covered_turnover += stock["turnover_yi"]
            code = meta["industry_code"]
            grp = groups.setdefault(
                code,
                {
                    "code": code,
                    "name": meta["industry_name"],
                    "parent": meta["parent_name"],
                    "turnover_yi": 0.0,
                    "inflow_yi": 0.0,
                    "pct_numerator": 0.0,
                    "stock_count": 0,
                },
            )
            grp["turnover_yi"] += stock["turnover_yi"]
            grp["inflow_yi"] += stock["inflow_yi"]
            grp["pct_numerator"] += stock["pct"] * stock["turnover_yi"]
            grp["stock_count"] += 1

        rows: list[dict[str, Any]] = []
        for grp in groups.values():
            turnover = grp["turnover_yi"]
            if grp["stock_count"] < 3 or turnover < 1.0:
                continue
            rows.append(
                {
                    "code": grp["code"],
                    "name": grp["name"],
                    "parent": grp["parent"],
                    "pct": round(grp["pct_numerator"] / turnover, 2) if turnover else None,
                    "inflow_yi": round(grp["inflow_yi"], 2),
                    "share": round(grp["inflow_yi"] / turnover * 100.0, 2) if turnover else None,
                    "turnover_yi": round(turnover, 1),
                    "stock_count": grp["stock_count"],
                }
            )
        rows.sort(key=lambda r: (r["inflow_yi"], r["turnover_yi"]), reverse=True)
        return {
            "rows": rows,
            "source": mapping.get("source", "申万2021三级行业"),
            "mapping_asof": mapping.get("asof"),
            "industry_count": len(rows),
            "coverage_pct": round(covered_turnover / total_turnover * 100.0, 1) if total_turnover else None,
        }, stock_err

    def _full_date(self, mmdd: str) -> str:
        month, day = map(int, mmdd.split("/"))
        today = self._now().astimezone(CN_TZ).date()
        year = today.year - (1 if month > today.month + 1 else 0)
        return date(year, month, day).isoformat()

    def fetch_daily_history(self) -> tuple[list[dict[str, Any]], str | None]:
        now = time.time()
        eff_ttl = min(HISTORY_TTL, 55.0) if self._history_cache.get("error") else HISTORY_TTL
        if self._history_cache["rows"] and now - self._history_cache["ts"] < eff_ttl:
            return self._history_cache["rows"], self._history_cache.get("error")
        if self._history_cache.get("error") and now - self._history_cache["ts"] < eff_ttl:
            return [], self._history_cache["error"]
        try:
            html_txt = self._get(
                "https://q.stock.sohu.com/cn/zdt.shtml",
                referer="https://q.stock.sohu.com/",
                timeout=15,
            )
            parser = _SohuTableParser()
            parser.feed(html_txt)
            rows: list[dict[str, Any]] = []
            for cells in parser.rows:
                if len(cells) < 14 or "/" not in cells[0]:
                    continue
                try:
                    nums = [int(float(x)) for x in cells[5:14]]
                    rows.append(
                        {
                            "day": self._full_date(cells[0]),
                            "up": nums[0] + nums[3] + nums[6],
                            "flat": nums[1] + nums[4] + nums[7],
                            "down": nums[2] + nums[5] + nums[8],
                            "amount_yi": round(float(cells[4]), 1),
                            "limit_up": int(float(cells[1])),
                            "limit_down": int(float(cells[2])),
                        }
                    )
                except (ValueError, IndexError):
                    continue
            rows.sort(key=lambda r: r["day"])
            rows = rows[-30:]
            if rows:
                self._history_cache = {
                    "ts": now,
                    "rows": rows,
                    "error": None,
                }
                self._store_daily_history(rows)
                return rows, None
            raise RuntimeError("30日历史行情解析结果为空")
        except Exception as exc:
            stored = self._load_daily_history() or self._history_cache.get("rows") or []
            prev_ok = self._prior_source_success("daily_history")
            err = f"30日历史行情拉取失败（上次成功：{prev_ok or '无'}）: {exc}"
            self._history_cache = {
                "ts": now,
                "rows": stored,
                "error": err,
            }
            return stored, err

    # ---- SQLite Persistence -----------------------------------------------

    def _store_daily_history(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO daily_market(day,up,down,flat,amount_yi,limit_up,limit_down) VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            r["day"],
                            r["up"],
                            r["down"],
                            r["flat"],
                            r["amount_yi"],
                            r["limit_up"],
                            r["limit_down"],
                        )
                        for r in rows
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def _load_daily_history(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT day,up,down,flat,amount_yi,limit_up,limit_down FROM daily_market ORDER BY day DESC LIMIT 30"
                ).fetchall()
            finally:
                conn.close()
        cols = ("day", "up", "down", "flat", "amount_yi", "limit_up", "limit_down")
        return [dict(zip(cols, r)) for r in reversed(rows)]

    def record_market_sample(
        self, two_market_yi: float | None, breadth: dict[str, Any] | None
    ) -> None:
        cur = self._now().astimezone(CN_TZ)
        minute = _sample_minute(cur)
        if not minute:
            return
        day = cur.date().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                if two_market_yi:
                    conn.execute(
                        "INSERT OR REPLACE INTO vol(day, ts, minute, cum_yi) VALUES(?,?,?,?)",
                        (day, int(cur.timestamp()), minute, two_market_yi),
                    )
                if breadth:
                    conn.execute(
                        "INSERT OR REPLACE INTO breadth(day, ts, minute, up, down, flat) VALUES(?,?,?,?,?,?)",
                        (
                            day,
                            int(cur.timestamp()),
                            minute,
                            breadth["up"],
                            breadth["down"],
                            breadth["flat"],
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    def record_direction_samples(self, direction_rows: list[dict[str, Any]]) -> None:
        cur = self._now().astimezone(CN_TZ)
        minute = _sample_minute(cur)
        if not minute or not direction_rows:
            return
        day = cur.date().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO etf_direction(day,ts,minute,direction,flow_yi,turnover_yi,pct) VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            day,
                            int(cur.timestamp()),
                            minute,
                            r["series_key"],
                            r["net_flow_yi"],
                            r["turnover_yi"],
                            r.get("weighted_pct"),
                        )
                        for r in direction_rows
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def record_etf_samples(self, etf_rows: list[dict[str, Any]]) -> None:
        cur = self._now().astimezone(CN_TZ)
        minute = _sample_minute(cur)
        rows = [r for r in etf_rows if r.get("turnover_yi", 0) >= ETF_MIN_TURNOVER_YI]
        if not minute or not rows:
            return
        day = cur.date().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO etf_flow(day,ts,minute,code,flow_yi,turnover_yi,pct) VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            day,
                            int(cur.timestamp()),
                            minute,
                            r["code"],
                            r["inflow_yi"],
                            r["turnover_yi"],
                            r.get("pct"),
                        )
                        for r in rows
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def volume_series(self) -> dict[str, list[Any]]:
        day = self._now().astimezone(CN_TZ).date().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT minute, cum_yi FROM vol WHERE day=? ORDER BY minute",
                    (day,),
                ).fetchall()
            finally:
                conn.close()
        rows = [r for r in rows if _valid_session_minute(r[0])]
        labels, cum, rate = [], [], []
        prev_cum: float | None = None
        prev_min: str | None = None
        for minute, c in rows:
            labels.append(minute)
            cum.append(round(c, 0))
            if prev_cum is not None and prev_min is not None:
                gap = _minute_gap(prev_min, minute) or 1
                rate.append(round(max(0.0, (c - prev_cum)) / gap, 1))
            else:
                rate.append(0.0)
            prev_cum, prev_min = c, minute
        return {"labels": labels, "cum": cum, "rate": rate}

    def breadth_series(self) -> dict[str, list[Any]]:
        day = self._now().astimezone(CN_TZ).date().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT minute, up, down, flat FROM breadth WHERE day=? ORDER BY minute",
                    (day,),
                ).fetchall()
            finally:
                conn.close()
        rows = [r for r in rows if _valid_session_minute(r[0])]
        return {
            "labels": [r[0] for r in rows],
            "up": [r[1] for r in rows],
            "down": [r[2] for r in rows],
            "flat": [r[3] for r in rows],
        }

    def _momentum_query(self, table: str, key_col: str, key_val: str, current_flow: float, current_turnover: float, current_pct: float | None) -> dict[str, Any]:
        cur = self._now().astimezone(CN_TZ)
        today = cur.date().isoformat()
        minute = _sample_minute(cur)
        if not minute:
            return {
                "delta_1m": None,
                "delta_5m": None,
                "delta_15m": None,
                "delta_share_5m": None,
                "accel_5m": None,
                "history_percentile": None,
                "history_days": 0,
                "return_percentile": None,
                "return_history_days": 0,
            }
        with self._lock:
            conn = self._connect()
            try:
                day_rows = conn.execute(
                    f"SELECT minute,flow_yi,turnover_yi,pct FROM {table} WHERE day=? AND {key_col}=? ORDER BY minute",
                    (today, key_val),
                ).fetchall()
                hist = conn.execute(
                    f"SELECT flow_yi,turnover_yi,pct FROM {table} WHERE {key_col}=? AND minute=? AND day<>? ORDER BY day DESC LIMIT 20",
                    (key_val, minute, today),
                ).fetchall()
            finally:
                conn.close()
        current_idx = _trading_minute_index(minute)

        def at_or_before(target: int):
            candidates = [r for r in day_rows if _trading_minute_index(r[0]) <= target]
            return candidates[-1] if candidates else None

        deltas: dict[int, float | None] = {}
        for gap in (1, 5, 15):
            prev = at_or_before(current_idx - gap)
            deltas[gap] = round(current_flow - prev[1], 3) if prev else None
        p5 = at_or_before(current_idx - 5)
        p10 = at_or_before(current_idx - 10)
        delta_turn = current_turnover - p5[2] if p5 else None
        delta_share = (
            (deltas[5] / delta_turn * 100.0)
            if deltas[5] is not None and delta_turn and delta_turn > 0
            else None
        )
        accel = (
            (deltas[5] - (p5[1] - p10[1]))
            if deltas[5] is not None and p5 and p10
            else None
        )
        current_ratio = current_flow / current_turnover * 100.0 if current_turnover else 0.0
        hist_ratios = [f / t * 100.0 for f, t, _ in hist if t]
        percentile = None
        if hist_ratios:
            percentile = max(
                0.0,
                min(100.0, 100.0 * (sum(x <= current_ratio for x in hist_ratios) - 0.5) / len(hist_ratios)),
            )
        hist_returns = [abs(p) for _, _, p in hist if isinstance(p, (int, float))]
        return_pctile = None
        if hist_returns and isinstance(current_pct, (int, float)):
            return_pctile = max(
                0.0,
                min(100.0, 100.0 * (sum(x <= abs(current_pct) for x in hist_returns) - 0.5) / len(hist_returns)),
            )
        return {
            "delta_1m": deltas[1],
            "delta_5m": deltas[5],
            "delta_15m": deltas[15],
            "delta_share_5m": round(delta_share, 2) if delta_share is not None else None,
            "accel_5m": round(accel, 3) if accel is not None else None,
            "history_percentile": round(percentile, 1) if percentile is not None else None,
            "history_days": len(hist_ratios),
            "return_percentile": round(return_pctile, 1) if return_pctile is not None else None,
            "return_history_days": len(hist_returns),
        }

    # ---- Official Exchange ETF Shares -------------------------------------

    def _sse_share_day(self, stat_date: str = "") -> list[dict[str, Any]]:
        params = {
            "jsonCallBack": "jsonpCallback",
            "isPagination": "true",
            "pageHelp.pageSize": 1000,
            "pageHelp.pageNo": 1,
            "pageHelp.beginPage": 1,
            "pageHelp.cacheSize": 1,
            "pageHelp.endPage": 1,
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": stat_date,
        }
        url = "https://query.sse.com.cn/commonQuery.do?" + urllib.parse.urlencode(params)
        text = self._get(
            url,
            referer="https://www.sse.com.cn/market/funddata/volumn/etfvolumn/",
            timeout=20,
        )
        return _strip_jsonp(text).get("result") or []

    def _fetch_sse_pairs(self, codes: list[str]) -> dict[str, tuple[Any, Any]]:
        wanted = set(codes)
        if not wanted:
            return {}
        latest_rows = self._sse_share_day("")
        if not latest_rows:
            raise RuntimeError("上交所官方份额接口返回空列表")
        latest_day = str(latest_rows[0].get("STAT_DATE") or "").strip()
        try:
            base = datetime.strptime(latest_day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise RuntimeError(f"上交所官方份额缺少有效 STAT_DATE: {latest_day!r}") from exc

        latest: dict[str, tuple[str, float]] = {}
        for r in latest_rows:
            sec = str(r.get("SEC_CODE") or "").strip()
            if sec not in wanted:
                continue
            try:
                val = float(str(r.get("TOT_VOL", "")).replace(",", ""))
                stat_d = str(r.get("STAT_DATE") or latest_day).strip()
                if not stat_d or math.isnan(val) or math.isinf(val):
                    continue
                latest[sec] = (stat_d, val)
            except (TypeError, ValueError):
                continue

        missing_latest = sorted(wanted - set(latest.keys()))
        if missing_latest:
            raise RuntimeError(
                f"上交所官方份额缺少代表 ETF 最新记录: {', '.join(missing_latest[:5])}"
            )

        previous: dict[str, tuple[str, float]] = {}
        for gap in range(1, 10):
            d = base - timedelta(days=gap)
            try:
                if not is_trading_day("cn", d):
                    continue
            except CalendarLookupError:
                if d.weekday() >= 5:
                    continue
            prev_rows = self._sse_share_day(d.isoformat())
            if not prev_rows:
                continue
            for r in prev_rows:
                sec = str(r.get("SEC_CODE") or "").strip()
                if sec not in wanted or sec in previous:
                    continue
                stat_d = str(r.get("STAT_DATE") or "").strip()
                if not stat_d or stat_d == latest[sec][0]:
                    continue
                try:
                    val = float(str(r.get("TOT_VOL", "")).replace(",", ""))
                    if math.isnan(val) or math.isinf(val):
                        continue
                    previous[sec] = (stat_d, val)
                except (TypeError, ValueError):
                    continue
            if wanted.issubset(previous.keys()):
                break

        missing_prev = sorted(wanted - set(previous.keys()))
        if missing_prev:
            raise RuntimeError(
                f"上交所官方份额缺少前一期有效记录: {', '.join(missing_prev[:5])}"
            )
        return {code: (latest[code], previous[code]) for code in wanted}

    def _fetch_szse_pair(self, code: str) -> tuple[Any, Any]:
        end = self._now().astimezone(CN_TZ).date()
        start = end - timedelta(days=14)
        params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": "scsj_fund_jjgm",
            "jjlb": "ETF",
            "txtStart": start.isoformat(),
            "txtEnd": end.isoformat(),
            "txtDm": code,
            "TABKEY": "tab1",
            "PAGENO": 1,
        }
        url = "https://www.szse.cn/api/report/ShowReport/data?" + urllib.parse.urlencode(params)
        text = self._get(
            url,
            referer="https://www.szse.cn/market/fund/volume/etf/index.html",
            timeout=20,
        )
        payload = json.loads(text)
        rows = (payload[0].get("data") if isinstance(payload, list) and payload else None) or []
        if not rows:
            raise RuntimeError(f"深交所官方份额({code})返回空记录")
        by_date: dict[str, float] = {}
        for r in rows:
            if str(r.get("fund_code")) != code:
                continue
            sdate = str(r.get("size_date") or "").strip()
            if not sdate:
                continue
            try:
                val = float(str(r.get("current_size", "")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if not (math.isnan(val) or math.isinf(val)):
                by_date[sdate] = val
        points = sorted(by_date.items(), reverse=True)
        if not points:
            raise RuntimeError(f"深交所官方份额({code})未解析到有效日期")
        if len(points) < 2:
            raise RuntimeError(f"深交所官方份额({code})缺少前一期记录")
        return (points[0], points[1])

    def _store_unit_pairs(self, pairs: dict[str, tuple[Any, Any]]) -> None:
        values = []
        for code, item in pairs.items():
            exchange = "SSE" if code.startswith(("5", "6")) else "SZSE"
            for pt in item:
                if pt:
                    values.append((pt[0], code, pt[1], exchange))
        if not values:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO etf_units(trade_day,code,units_wan,exchange) VALUES(?,?,?,?)",
                    values,
                )
                conn.commit()
            finally:
                conn.close()

    def _load_unit_pair(self, code: str) -> tuple[Any, Any]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT trade_day,units_wan FROM etf_units WHERE code=? ORDER BY trade_day DESC LIMIT 2",
                    (code,),
                ).fetchall()
            finally:
                conn.close()
        return (rows[0] if rows else None, rows[1] if len(rows) > 1 else None)

    def official_share_changes(
        self, representatives: list[dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        unique = {r["code"]: r for r in representatives}
        codes = set(unique)
        now = time.time()
        eff_ttl = min(OFFICIAL_SHARE_TTL, 55.0) if self._official_cache.get("error") else OFFICIAL_SHARE_TTL
        fresh = now - self._official_cache["ts"] < eff_ttl
        if not fresh or not codes.issubset(self._official_cache["codes"]):
            pairs: dict[str, tuple[Any, Any]] = {}
            sse_codes = [c for c in codes if c.startswith(("5", "6"))]
            szse_codes = [c for c in codes if not c.startswith(("5", "6"))]
            errs: list[str] = []
            try:
                pairs.update(self._fetch_sse_pairs(sse_codes))
            except Exception as exc:
                errs.append(f"上交所官方份额拉取失败: {exc}")
            if szse_codes:
                def _safe_szse(c: str):
                    try:
                        return self._fetch_szse_pair(c), None
                    except Exception as exc:
                        return (None, None), str(exc)

                szse_errs: list[str] = []
                with ThreadPoolExecutor(max_workers=min(10, len(szse_codes))) as ex:
                    for code, (pair, perr) in zip(szse_codes, ex.map(_safe_szse, szse_codes)):
                        if perr:
                            szse_errs.append(f"{code}:{perr}")
                        elif pair[0] and pair[1]:
                            pairs[code] = pair
                if szse_errs:
                    errs.append(f"深交所官方份额拉取失败 ({len(szse_errs)} 只，如 {szse_errs[0]})")

            complete_pairs = {
                c: p
                for c, p in pairs.items()
                if p and p[0] and p[1] and str(p[0][0]) != str(p[1][0])
            }
            missing_codes = sorted(c for c in codes if c not in complete_pairs)
            if missing_codes and not errs:
                errs.append(f"官方份额缺少完整双期数据: {', '.join(missing_codes[:5])}")

            self._store_unit_pairs(complete_pairs)
            for code in codes:
                pair = complete_pairs.get(code)
                if pair:
                    self._official_cache["data"][code] = pair
                elif code not in self._official_cache["data"]:
                    loaded = self._load_unit_pair(code)
                    if loaded and loaded[0] and loaded[1]:
                        self._official_cache["data"][code] = loaded

            prev_ok = self._prior_source_success("etf_official_shares")
            if errs:
                err_msg = "；".join(errs) + f"（上次成功：{prev_ok or '无'}）"
                self._official_cache["error"] = err_msg
                self._official_cache["ts"] = now
            else:
                self._official_cache["error"] = None
                self._official_cache["ts"] = now
                self._official_cache["codes"] = self._official_cache["codes"] | codes

        out: dict[str, dict[str, Any]] = {}
        for code, rep in unique.items():
            pair = self._official_cache["data"].get(code) or self._load_unit_pair(code)
            if not pair or not pair[0] or not pair[1]:
                continue
            latest, previous = pair
            change_wan = latest[1] - previous[1]
            price = rep.get("price")
            change_pct = change_wan / previous[1] * 100.0 if previous[1] else None
            implied_units_wan = None
            if isinstance(price, (int, float)) and price > 0 and isinstance(rep.get("market_cap_yi"), (int, float)):
                implied_units_wan = rep["market_cap_yi"] * 10000.0 / price
            unit_ratio = latest[1] / implied_units_wan if implied_units_wan else None
            quality = "ok"
            if (change_pct is not None and abs(change_pct) > 50) or (unit_ratio is not None and not 0.5 <= unit_ratio <= 2.0):
                quality = "corporate_action_suspected"
            confirmed_yi = change_wan * price / 10000.0 if isinstance(price, (int, float)) else None
            out[code] = {
                "asof": latest[0],
                "previous_day": previous[0],
                "units_change_wan": round(change_wan, 2),
                "units_change_pct": round(change_pct, 2) if change_pct is not None else None,
                "confirmed_yi": round(confirmed_yi, 2) if confirmed_yi is not None and quality == "ok" else None,
                "quality": quality,
            }
        return out, self._official_cache.get("error")

    # ---- Aggregation & Snapshot ------------------------------------------

    def build_etf_radar(self) -> tuple[dict[str, Any], str | None]:
        market_rows, scanned, etf_err = self.fetch_etf_market()
        if not market_rows:
            return {
                "scanned": 0,
                "matched": 0,
                "direction_count": len(ETF_DIRECTIONS),
                "strong_count": 0,
                "top_direction": "暂无数据",
                "top_score": 0,
                "official_asof": None,
                "industry_directions": [],
                "broad_directions": [],
                "directions": [],
                "etf_leaders": [],
            }, etf_err

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in ETF_DIRECTIONS}
        for row in market_rows:
            grouped[(row["category"], row["direction"])].append(row)

        directions: list[dict[str, Any]] = []
        all_reps: list[dict[str, Any]] = []
        for category, direction in ETF_DIRECTIONS:
            rows = grouped[(category, direction)]
            by_track: dict[str, dict[str, Any]] = {}
            for r in sorted(rows, key=lambda x: x["turnover_yi"], reverse=True):
                if r["turnover_yi"] < ETF_MIN_TURNOVER_YI:
                    continue
                by_track.setdefault(r["track_key"], r)
            reps = list(by_track.values())[:ETF_REPRESENTATIVES]
            all_reps.extend(reps)
            turnover = sum(r["turnover_yi"] for r in rows)
            net_flow = sum(r["inflow_yi"] for r in rows)
            pct_num = sum((r["pct"] or 0.0) * r["turnover_yi"] for r in rows)
            weighted_pct = pct_num / turnover if turnover else None
            directions.append(
                {
                    "category": category,
                    "direction": direction,
                    "series_key": f"{category}:{direction}",
                    "fund_count": len(rows),
                    "turnover_yi": round(turnover, 2),
                    "net_flow_yi": round(net_flow, 2),
                    "flow_share_pct": round(net_flow / turnover * 100.0, 2) if turnover else None,
                    "weighted_pct": round(weighted_pct, 2) if weighted_pct is not None else None,
                    "representatives": reps,
                }
            )

        vratio_err1 = self._fetch_representative_vratios(all_reps)
        self.record_direction_samples(directions)
        for row in directions:
            row.update(
                self._momentum_query(
                    "etf_direction",
                    "direction",
                    row["series_key"],
                    row["net_flow_yi"],
                    row["turnover_yi"],
                    row["weighted_pct"],
                )
            )
            reps = row["representatives"]
            pos = sum(1 for r in reps if r["inflow_yi"] > 0)
            neg = sum(1 for r in reps if r["inflow_yi"] < 0)
            row["sync"] = {"positive": pos, "negative": neg, "total": len(reps)}
            vrs = [r["vratio"] for r in reps if isinstance(r.get("vratio"), (int, float))]
            row["avg_vratio"] = round(sum(vrs) / len(vrs), 2) if vrs else None
            row["score"], row["signal"] = _score_direction(row)

        self.record_etf_samples(market_rows)
        direction_scores = {(r["category"], r["direction"]): r["score"] for r in directions}
        eligible = [
            r
            for r in market_rows
            if r["turnover_yi"] >= ETF_LEADER_MIN_TURNOVER_YI
            and (abs(r["inflow_yi"]) >= ETF_LEADER_MIN_FLOW_YI or abs(r.get("pct") or 0.0) >= 0.8)
        ]
        leader_pool = sorted(
            eligible,
            key=lambda r: (
                _pre_score_etf(r, direction_scores.get((r["category"], r["direction"]), 0)),
                abs(r["inflow_yi"]),
            ),
            reverse=True,
        )[:ETF_LEADER_POOL]
        vratio_err2 = self._fetch_representative_vratios(leader_pool)
        vratio_err = vratio_err1 or vratio_err2
        for item in leader_pool:
            item.update(
                self._momentum_query(
                    "etf_flow",
                    "code",
                    item["code"],
                    item["inflow_yi"],
                    item["turnover_yi"],
                    item.get("pct"),
                )
            )
            item["anomaly_score"], item["signal"] = _score_etf(
                item, direction_scores.get((item["category"], item["direction"]), 0)
            )
        leaders = sorted(
            leader_pool,
            key=lambda r: (r["anomaly_score"], abs(r["inflow_yi"]), r["turnover_yi"]),
            reverse=True,
        )[:ETF_LEADER_COUNT]

        official, official_err = (
            self.official_share_changes(all_reps + leaders) if (all_reps or leaders) else ({}, None)
        )
        official_dates: list[str] = []
        current_day = self._now().astimezone(CN_TZ).date().isoformat()
        for row in directions:
            confirmations = []
            for rep in row["representatives"]:
                info = official.get(rep["code"])
                if info:
                    rep["official"] = info
                    confirmations.append(info)
                    official_dates.append(info["asof"])
            asof = max((x["asof"] for x in confirmations), default=None)
            same_day = [
                x
                for x in confirmations
                if x["asof"] == asof and isinstance(x.get("confirmed_yi"), (int, float))
            ]
            confirmed = sum(x["confirmed_yi"] for x in same_day) if same_day else None
            row["official_asof"] = asof
            row["confirmed_yi"] = round(confirmed, 2) if confirmed is not None else None
            prefix = "本日" if asof == current_day else "上期"
            if confirmed is None:
                row["confirmation"] = "待更新"
            elif confirmed >= 1.0:
                row["confirmation"] = f"{prefix}申购"
            elif confirmed <= -1.0:
                row["confirmation"] = f"{prefix}赎回"
            else:
                row["confirmation"] = "份额中性"

        directions.sort(key=lambda r: (r["score"], abs(r["net_flow_yi"])), reverse=True)
        industry_directions = [r for r in directions if r["category"] == "industry"]
        broad_directions = [r for r in directions if r["category"] == "broad"]
        top = directions[0] if directions else None
        return {
            "scanned": scanned,
            "matched": len(market_rows),
            "direction_count": len(directions),
            "strong_count": sum(1 for r in directions if r["score"] >= RADAR_ALERT_SCORE),
            "top_direction": top["direction"] if top and top["score"] >= 45 else "暂无明显异动",
            "top_score": top["score"] if top else 0,
            "official_asof": max(official_dates, default=None),
            "industry_directions": industry_directions,
            "broad_directions": broad_directions,
            "directions": directions,
            "etf_leaders": leaders,
            "sub_errors": {
                "etf_vratio": vratio_err,
                "etf_official_shares": official_err,
            },
        }, etf_err

    def _push_alerts(self, radar: dict[str, Any], cur: datetime) -> None:
        session = market_session_state("cn", cur)
        if not session["in_trading_window"]:
            return
        stamp = cur.strftime("%m-%d %H:%M:%S")
        for r in (radar or {}).get("directions", []):
            score = r.get("score", 0)
            if score < RADAR_ALERT_SCORE:
                continue
            key = r["series_key"]
            prev = self._last_alert_key.get(key)
            if prev is not None and abs(prev - score) < 5:
                continue
            self._last_alert_key[key] = score
            flow = r.get("net_flow_yi", 0.0)
            kind = "入" if flow >= 0 else "出"
            category = "行业ETF" if r.get("category") == "industry" else "宽基ETF"
            self._alerts.appendleft(
                {
                    "ts": stamp,
                    "text": f"{category} · {r['direction']} {r['signal']} · 异常度 {score} · 主力大单净流{kind} {abs(flow):.1f} 亿元",
                    "pos": flow >= 0,
                }
            )

    def collect_snapshot(self, *, commit_status: bool = True) -> dict[str, Any]:
        """Collect all A-share intraday panels, computing unified SourceStatusStore evaluation."""
        cur = self._now().astimezone(CN_TZ)
        now_full = cur.strftime("%Y-%m-%d %H:%M:%S")
        session = market_session_state("cn", cur)
        prev = dict(self._last_good or {})

        def _call_safe(fn, fallback):
            try:
                return fn()
            except Exception as exc:
                return fallback(exc)

        with ThreadPoolExecutor(max_workers=5) as ex:
            f_idx = ex.submit(self.fetch_indices)
            f_brd = ex.submit(self.fetch_breadth)
            f_etf = ex.submit(self.build_etf_radar)
            f_ind = ex.submit(self.fetch_industry_flow)
            f_hist = ex.submit(self.fetch_daily_history)
            indices, by_code, idx_err = _call_safe(f_idx.result, lambda e: ([], {}, f"指数采集异常: {e}"))
            breadth, brd_err = _call_safe(f_brd.result, lambda e: (None, f"涨跌家数采集异常: {e}"))
            etf_radar, etf_err = _call_safe(f_etf.result, lambda e: ({}, f"ETF 雷达异常: {e}"))
            industry, ind_err = _call_safe(f_ind.result, lambda e: ({"rows": []}, f"申万行业采集异常: {e}"))
            daily_history, hist_err = _call_safe(f_hist.result, lambda e: ([], f"30日历史异常: {e}"))

        source_results = []
        required_sources = [
            "indices",
            "turnover",
            "breadth",
            "etf_radar",
            "etf_vratio",
            "etf_official_shares",
            "industry",
            "daily_history",
        ]
        if session.get("calendar_error"):
            required_sources.append("calendar")
            source_results.append(
                make_source_result(
                    "calendar",
                    ok=False,
                    stale=True,
                    reason=str(session["calendar_error"]),
                    as_of=now_full,
                    label="交易所日历",
                )
            )

        idx_fail_reason = idx_err or ("指数行情缺失" if not indices else None)
        if not indices and prev.get("indices"):
            indices = prev["indices"]
        source_results.append(
            make_source_result(
                "indices",
                ok=not bool(idx_fail_reason),
                stale=bool(idx_fail_reason),
                reason=idx_fail_reason,
                as_of=now_full,
                label="核心指数",
            )
        )

        # Unified two-market turnover: SH Composite (s_sh000001) + SZ Composite (s_sz399106).
        sh = by_code.get("s_sh000001", {})
        sz_comp = by_code.get("s_sz399106", {})
        if sh.get("amount_wan") is not None and sz_comp.get("amount_wan") is not None:
            two_market_yi = round((sh["amount_wan"] + sz_comp["amount_wan"]) / 10000.0, 0)
            turn_reason = None
        else:
            two_market_yi = prev.get("two_market_yi")
            turn_reason = idx_err or "上证综指或深证综指成交额缺失，沿用上次有效值"
        source_results.append(
            make_source_result(
                "turnover",
                ok=not bool(turn_reason),
                stale=bool(turn_reason),
                reason=turn_reason,
                as_of=now_full,
                label="两市成交额",
            )
        )

        brd_reason = brd_err or ("涨跌家数缺失" if breadth is None else None)
        if breadth is None and prev.get("breadth"):
            breadth = prev["breadth"]
        source_results.append(
            make_source_result(
                "breadth",
                ok=not bool(brd_reason),
                stale=bool(brd_reason),
                reason=brd_reason,
                as_of=now_full,
                label="三市涨跌家数",
            )
        )

        etf_reason = etf_err or ("ETF 扫描异常" if not (etf_radar or {}).get("directions") else None)
        if not (etf_radar or {}).get("directions") and prev.get("etf_radar"):
            etf_radar = prev["etf_radar"]
        source_results.append(
            make_source_result(
                "etf_radar",
                ok=not bool(etf_reason),
                stale=bool(etf_reason),
                reason=etf_reason,
                as_of=now_full,
                label="场内ETF扫描",
            )
        )

        sub_errors = etf_radar.get("sub_errors") if isinstance(etf_radar, dict) else None
        vr_reason = (sub_errors or {}).get("etf_vratio") if isinstance(sub_errors, dict) else None
        off_reason = (sub_errors or {}).get("etf_official_shares") if isinstance(sub_errors, dict) else None
        source_results.append(
            make_source_result(
                "etf_vratio",
                ok=not bool(vr_reason),
                stale=bool(vr_reason),
                reason=vr_reason,
                as_of=now_full,
                label="ETF量比",
            )
        )
        source_results.append(
            make_source_result(
                "etf_official_shares",
                ok=not bool(off_reason),
                stale=bool(off_reason),
                reason=off_reason,
                as_of=now_full,
                label="交易所官方份额",
            )
        )

        mapping_missing = ind_err == "申万三级行业映射文件不可用"
        ind_reason = ind_err or ("申万三级行业资金缺失" if not (industry or {}).get("rows") else None)
        if not mapping_missing and not (industry or {}).get("rows") and prev.get("industry"):
            industry = prev["industry"]
        source_results.append(
            make_source_result(
                "industry",
                ok=not bool(ind_reason),
                stale=bool(ind_reason),
                reason=ind_reason,
                as_of=now_full,
                label="申万三级行业资金",
            )
        )

        hist_reason = hist_err or ("30日历史行情缺失" if not daily_history else None)
        if not daily_history and prev.get("daily_history"):
            daily_history = prev["daily_history"]
        source_results.append(
            make_source_result(
                "daily_history",
                ok=not bool(hist_reason),
                stale=bool(hist_reason),
                reason=hist_reason,
                as_of=now_full,
                label="30日历史行情",
            )
        )

        # Only record minute bar into SQLite when fresh values were actually fetched.
        if not idx_err and not brd_err:
            self.record_market_sample(two_market_yi, breadth)
        if not etf_err:
            self._push_alerts(etf_radar, cur)

        elapsed, total_min, frac = trading_progress(cur)
        if two_market_yi is not None:
            projected_yi = round(two_market_yi / frac, 0) if frac > 0.02 else two_market_yi
        else:
            projected_yi = prev.get("projected_yi")

        # Single source of truth for per-source & board last_success_at: SourceStatusStore
        status_doc = (
            self._status_store.update_sources(
                source_results,
                required=required_sources,
                now_str=now_full,
            )
            if commit_status
            else self._status_store.preview_sources(
                source_results,
                required=required_sources,
                now_str=now_full,
            )
        )
        overall = status_doc["overall"]
        is_stale = bool(overall["stale"])
        publish_ok = all(
            bool((status_doc["sources"].get(k) or {}).get("ok"))
            and (not bool((status_doc["sources"].get(k) or {}).get("stale")))
            for k in ASHARE_REQUIRED_PUBLISH_SOURCES
        )

        snapshot = {
            "as_of": now_full,
            "as_of_iso": cur.isoformat(timespec="seconds"),
            "tz": "北京时间",
            "trade_date": session["trade_date"],
            "is_trading_day": session["is_trading_day"],
            "windows": session.get("windows") or [],
            "trading": session["in_trading_window"],
            "market_status": session["status"],
            "market_status_label": session["label"],
            "using_default_schedule": session.get("using_default_schedule", False),
            "session_note": session.get("session_note"),
            "stale": is_stale,
            "stale_sources": overall["stale_sources"],
            "stale_reason": overall["reason"],
            "last_success_at": overall["last_success_at"],
            "sources": status_doc["sources"],
            "overall": overall,
            "publish_ok": publish_ok,
            "_pending_source_results": [r.to_dict() for r in source_results],
            "_required_sources": required_sources,
            "turnover_basis": "上证综指（000001.SH）＋ 深证综指（399106.SZ）两市全口径",
            "indices": indices,
            "breadth": breadth,
            "breadth_series": self.breadth_series(),
            "daily_history": daily_history,
            "etf_radar": etf_radar,
            "industry": industry,
            "two_market_yi": two_market_yi,
            "projected_yi": projected_yi,
            "progress": {"elapsed": elapsed, "total": total_min, "pct": round(frac * 100)},
            "volume_series": self.volume_series(),
            "alerts": list(self._alerts)[:15],
        }
        if not is_stale or not self._last_good:
            self._last_good = snapshot
            atomic_write_json(self.snapshot_cache_path, snapshot)
        return snapshot
