"""CN/HK filings probe: zero-token check for new A-share and HKEX CNINFO filings via researchdata bridge."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Callable, Optional
import urllib.request
from zoneinfo import ZoneInfo

from src.dalton.autonomy.probes import ProbeSignal

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
CNHK_PROBE_STEP_TIMEOUT_S: float = 10.0
CNHK_PROBE_TOTAL_BUDGET_S: float = 25.0
DEFAULT_INITIAL_LOOKBACK_DAYS: int = 3
CNHK_FILINGS_QUERY_LIMIT: int = 50
MAX_SEEN_CNHK_FILINGS: int = 200
_LAST_UNCONFIGURED_WARN_AT: float = 0.0

_TRAD_TO_SIMP_TABLE: dict[int, int] = str.maketrans(
    "業績預須購關連會議開報變辭資產組問詢監調減計劃終屆滿實財務佈",
    "业绩预须购关连会议开报变辞资产组问询监调减计划终届满实财务布",
)

P1_KEYWORD_RULES: dict[str, tuple[str, ...]] = {
    "earnings": (
        "年度报告",
        "半年度报告",
        "季度报告",
        "业绩快报",
        "年度业绩",
        "中期业绩",
        "季度业绩",
        "末期业绩",
        "业绩公告",
        "业绩公布",
        "全年业绩",
        "财务业绩",
        "年度業績",
        "中期業績",
        "季度業績",
        "末期業績",
        "業績公告",
    ),
    "forecast": (
        "业绩预告",
        "盈利警告",
        "盈警",
        "盈利预告",
        "正面盈利预告",
        "盈利預告",
        "正面盈利預告",
    ),
    "restructuring": (
        "重大资产重组",
        "须予披露的交易",
        "主要交易",
        "非常重大的收购",
        "关连交易",
        "須予披露的交易",
        "非常重大的收購",
        "關連交易",
    ),
    "regulatory_inquiry": (
        "问询函",
        "关注函",
        "监管函",
        "立案调查",
    ),
    "shareholder_reduction": (
        "减持计划",
        "减持股份计划",
    ),
    # Board-meeting dates announce an upcoming results release. Next-day
    # disclosure returns, appointments and generic "changes" are routine for
    # large HK issuers (daily buyback filers) and stay P2.
    "hk_corporate_actions": (
        "董事会会议召开日期",
        "董事會會議召開日期",
    ),
}

P1_KEYWORDS: tuple[str, ...] = tuple(
    kw for group in P1_KEYWORD_RULES.values() for kw in group
)

P1_NEGATION_KEYWORDS: tuple[str, ...] = (
    "不减持",
    "未减持",
    "终止",
    "不予",
    "届满",
    "未实施",
    "不存在",
)


def normalize_cnhk_text(text: str) -> str:
    """Normalize Traditional Chinese characters in CN/HK filing titles into Simplified Chinese for keyword matching."""
    return (text or "").translate(_TRAD_TO_SIMP_TABLE)


def normalize_hk_ticker(ticker: str) -> str:
    """Normalize a Hong Kong ticker (``700``, ``0700.HK``, ``00700.HK``, ``HK700``) into 5-digit zero-padded form (``00700``)."""
    t = (ticker or "").strip().upper()
    m_suf = re.fullmatch(r"(\d{1,5})\.HK", t)
    if m_suf:
        return m_suf.group(1).zfill(5)
    m_pref = re.fullmatch(r"HK(\d{1,5})", t)
    if m_pref:
        return m_pref.group(1).zfill(5)
    if re.fullmatch(r"\d{1,5}", t):
        return t.zfill(5)
    return t


def normalize_cn_ticker(ticker: str) -> str:
    """Normalize an A-share ticker into canonical ``DDDDDD.{SS|SZ|BJ}`` form."""
    t = (ticker or "").strip().upper()
    m_pref = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", t)
    if m_pref:
        pref, digits = m_pref.group(1), m_pref.group(2)
        suffix = ".SS" if pref == "SH" else (".SZ" if pref == "SZ" else ".BJ")
        return f"{digits}{suffix}"
    if t.endswith(".SH"):
        return f"{t[:-3]}.SS"
    if t.endswith((".SS", ".SZ", ".BJ")):
        return t
    if re.fullmatch(r"\d{6}", t):
        if t.startswith(("920", "4", "8")):
            return f"{t}.BJ"
        if t.startswith(("900", "5", "6", "9")):
            return f"{t}.SS"
        if t.startswith(("0", "1", "2", "3")):
            return f"{t}.SZ"
    return t


def classify_cnhk_ticker(ticker: str) -> tuple[str, Optional[str]]:
    """Classify ``ticker`` as ``('cn', code6)``, ``('hk', code5)``, or ``('other', None)``."""
    t = (ticker or "").strip().upper()
    if not t or t == "UNKNOWN":
        return "other", None
    m_pref = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", t)
    if m_pref:
        return "cn", m_pref.group(2)
    m_suf = re.fullmatch(r"(\d{6})\.(SS|SZ|SH|BJ)", t)
    if m_suf:
        return "cn", m_suf.group(1)
    if re.fullmatch(r"\d{6}", t):
        return "cn", t
    if re.fullmatch(r"(\d{1,5})\.HK", t) or re.fullmatch(r"HK(\d{1,5})", t) or re.fullmatch(r"\d{1,5}", t):
        return "hk", normalize_hk_ticker(t)
    return "other", None


def compute_cnhk_filing_hash(f_date: str, title: str, pdf_link: str) -> str:
    """Return deterministic SHA-256 hex digest for ``date|title|pdf_link``."""
    raw = f"{(f_date or '').strip()}|{(title or '').strip()}|{(pdf_link or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def classify_cnhk_priority(title: str, category: str = "") -> str:
    """Classify announcement priority as P1 or P2 based on ``P1_KEYWORD_RULES`` and ``P1_NEGATION_KEYWORDS``."""
    title_str = normalize_cnhk_text(title or "")
    if any(normalize_cnhk_text(neg) in title_str for neg in P1_NEGATION_KEYWORDS):
        return "P2"
    combined = f"{title_str} {normalize_cnhk_text(category or '')}"
    for kw in P1_KEYWORDS:
        if normalize_cnhk_text(kw) in combined:
            return "P1"
    return "P2"


def _resolve_bridge_url() -> str:
    bridge_url = os.environ.get("DALTON_BRIDGE_MCP_URL") or ""
    if not bridge_url:
        token = os.environ.get("DALTON_BRIDGE_TOKEN") or ""
        port = os.environ.get("DALTON_BRIDGE_PORT") or "8960"
        if token:
            bridge_url = f"http://127.0.0.1:{port}/mcp/{token}"
    return bridge_url


def _fetch_bridge_route_sync(
    *,
    bridge_url: str,
    ticker: str,
    start: str,
    end: str,
    limit: int = 50,
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    """Call local researchdata bridge ``finance(route='cn-hk', operation='filings')`` synchronously."""
    global _LAST_UNCONFIGURED_WARN_AT
    if not bridge_url:
        now_mono = time.monotonic()
        if _LAST_UNCONFIGURED_WARN_AT == 0.0 or (now_mono - _LAST_UNCONFIGURED_WARN_AT) >= 3600.0:
            _LAST_UNCONFIGURED_WARN_AT = now_mono
            logger.warning(
                "[DALTON_CNHK_PROBE] neither DALTON_BRIDGE_MCP_URL nor DALTON_BRIDGE_TOKEN "
                "is configured; cnhk probe degraded (ticker=%s).",
                ticker,
            )
        return {"ok": False, "error": "bridge_unconfigured"}

    payload = {
        "jsonrpc": "2.0",
        "id": f"probe-cnhk-filings-{ticker}",
        "method": "tools/call",
        "params": {
            "name": "finance",
            "arguments": {
                "route": "cn-hk",
                "operation": "filings",
                "ticker": ticker,
                "start": start,
                "end": end,
                "limit": limit,
            },
        },
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        bridge_url,
        data=data_bytes,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = json.loads(resp.read().decode("utf-8", errors="replace"))

    if not isinstance(raw, dict):
        return {"ok": False, "error": "invalid_jsonrpc_response"}
    if raw.get("error"):
        return {"ok": False, "error": str(raw.get("error"))}

    result = raw.get("result")
    if not isinstance(result, dict):
        if "ok" in raw or "rows" in raw or "filings" in raw:
            return raw
        return {"ok": False, "error": "missing_result"}
    if result.get("isError"):
        return {"ok": False, "error": "mcp_is_error"}

    content_list = result.get("content") or []
    for block in content_list:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            parsed = json.loads(block.get("text") or "{}")
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    if isinstance(result.get("structuredContent"), dict):
        return dict(result["structuredContent"])
    return {"ok": False, "error": "empty_bridge_content"}


async def _query_bridge_cnhk_filings(
    *,
    ticker: str,
    start: str,
    end: str,
    limit: int = 50,
    allow_network: bool = True,
) -> dict[str, Any]:
    """Query CNINFO filings for A-share and HK ``ticker`` via researchdata bridge in a worker thread."""
    if not allow_network:
        return {"ok": True, "rows": []}
    bridge_url = _resolve_bridge_url()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _fetch_bridge_route_sync,
                bridge_url=bridge_url,
                ticker=ticker,
                start=start,
                end=end,
                limit=limit,
            ),
            timeout=CNHK_PROBE_STEP_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning(
            "[DALTON_CNHK_PROBE] bridge query failed for ticker=%s (%s..%s): %s",
            ticker,
            start,
            end,
            exc,
        )
        return {"ok": False, "error": str(exc)}


def _extract_filings_from_response(raw_resp: Any) -> tuple[bool, list[dict[str, Any]]]:
    """Unwrap bridge response into ``(is_ok, rows)``."""
    if isinstance(raw_resp, list):
        return True, [x for x in raw_resp if isinstance(x, dict)]
    if not isinstance(raw_resp, dict):
        return False, []
    if raw_resp.get("ok") is False or raw_resp.get("isError") is True:
        return False, []
    inner = raw_resp.get("data")
    if isinstance(inner, dict):
        if inner.get("ok") is False or inner.get("isError") is True:
            return False, []
        candidates = (
            inner.get("rows")
            or inner.get("filings")
            or inner.get("items")
            or inner.get("results")
            or inner.get("data")
        )
        if isinstance(candidates, list):
            return True, [x for x in candidates if isinstance(x, dict)]
        return True, []
    candidates = (
        raw_resp.get("rows")
        or raw_resp.get("filings")
        or raw_resp.get("items")
        or raw_resp.get("results")
        or (raw_resp.get("data") if isinstance(raw_resp.get("data"), list) else None)
    )
    if isinstance(candidates, list):
        return True, [x for x in candidates if isinstance(x, dict)]
    return True, []


def _parse_iso_date(val: Any) -> Optional[date]:
    if not val:
        return None
    try:
        return date.fromisoformat(str(val).strip()[:10])
    except ValueError:
        return None


async def _fetch_window_filings_recursive(
    *,
    cn_code: str,
    norm_ticker: str,
    win_start: date,
    win_end: date,
    limit: int,
    allow_network: bool,
    truncated_dates: list[str],
    clock: Callable[[], float],
    deadline: float,
) -> tuple[bool, bool, Optional[date], list[dict[str, Any]]]:
    """Fetch filings for ``[win_start, win_end]``, recursively halving the window when ``len(rows) >= limit``.

    Returns ``(is_ok, is_complete, contiguous_completed_end, rows)``.
    Before issuing each request, checks remaining time budget against ``deadline``.
    """
    start_s = win_start.isoformat()
    end_s = win_end.isoformat()
    if clock() >= deadline:
        return True, False, None, []

    try:
        raw_resp = await _query_bridge_cnhk_filings(
            ticker=cn_code,
            start=start_s,
            end=end_s,
            limit=limit,
            allow_network=allow_network,
        )
    except Exception as exc:
        logger.warning(
            "[DALTON_CNHK_PROBE] unexpected error querying filings for %s (%s..%s): %s",
            norm_ticker,
            start_s,
            end_s,
            exc,
        )
        return False, False, None, []

    is_ok, rows = _extract_filings_from_response(raw_resp)
    if not is_ok:
        return False, False, None, []

    if limit > 0 and len(rows) >= limit:
        if win_start >= win_end:
            logger.warning(
                "[DALTON_CNHK_PROBE] single-day filings window saturated (len=%d >= limit=%d) for %s on %s; "
                "recording in cnhk_probe_truncated_dates.",
                len(rows),
                limit,
                norm_ticker,
                start_s,
            )
            if start_s not in truncated_dates:
                truncated_dates.append(start_s)
            return True, True, win_end, rows

        mid_days = (win_end - win_start).days // 2
        mid_date = win_start + timedelta(days=mid_days)
        ok_left, comp_left, end_left, left_rows = await _fetch_window_filings_recursive(
            cn_code=cn_code,
            norm_ticker=norm_ticker,
            win_start=win_start,
            win_end=mid_date,
            limit=limit,
            allow_network=allow_network,
            truncated_dates=truncated_dates,
            clock=clock,
            deadline=deadline,
        )
        if not ok_left:
            return False, False, None, []
        if not comp_left:
            # Keep the parent window's rows too: they are real filings already fetched, and the
            # seen-hash dedupe downstream drops any overlap. The watermark still stops at end_left.
            return True, False, end_left, [*rows, *left_rows]

        ok_right, comp_right, end_right, right_rows = await _fetch_window_filings_recursive(
            cn_code=cn_code,
            norm_ticker=norm_ticker,
            win_start=mid_date + timedelta(days=1),
            win_end=win_end,
            limit=limit,
            allow_network=allow_network,
            truncated_dates=truncated_dates,
            clock=clock,
            deadline=deadline,
        )
        if not ok_right:
            return False, False, None, []
        if not comp_right:
            return True, False, (end_right if end_right is not None else mid_date), [*rows, *left_rows, *right_rows]

        return True, True, win_end, [*left_rows, *right_rows]

    return True, True, win_end, rows


async def probe_cnhk_filings(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    workspace_id: Optional[str] = None,
    filings_override: Optional[Any] = None,
    allow_network: bool = True,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    limit: int = CNHK_FILINGS_QUERY_LIMIT,
    clock: Callable[[], float] = time.monotonic,
    total_budget_s: float = CNHK_PROBE_TOTAL_BUDGET_S,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Check CNINFO filings via researchdata bridge (`cn-hk` / `filings`) for A-share and HK tickers."""
    state = dict(watch_state or {})
    kind, cn_code = classify_cnhk_ticker(ticker)
    if kind not in ("cn", "hk") or not cn_code:
        return [], state

    state.pop("cnhk_probe_unsupported", None)
    norm_ticker = normalize_cn_ticker(ticker) if kind == "cn" else normalize_hk_ticker(ticker)

    if now is not None:
        et_today = now.astimezone(ET_TZ).date() if now.tzinfo is not None else now.date()
    else:
        et_today = datetime.now(ET_TZ).date()

    end_date = min(today, et_today) if today is not None else et_today
    last_check_date = _parse_iso_date(
        state.get("last_cnhk_check_date") or state.get("last_cnhk_date")
    )
    if last_check_date is not None:
        start_date = min(last_check_date, end_date)
    else:
        start_date = end_date - timedelta(days=DEFAULT_INITIAL_LOOKBACK_DAYS)

    end_str = end_date.isoformat()
    truncated_dates: list[str] = []
    is_complete = True
    completed_end_date: Optional[date] = end_date

    raw_data = filings_override if filings_override is not None else state.get("cnhk_filings_mock")
    if raw_data is not None:
        is_ok, rows = _extract_filings_from_response(raw_data)
    else:
        if not allow_network:
            return [], state
        deadline = clock() + total_budget_s
        is_ok, is_complete, completed_end_date, rows = await _fetch_window_filings_recursive(
            cn_code=cn_code,
            norm_ticker=norm_ticker,
            win_start=start_date,
            win_end=end_date,
            limit=limit,
            allow_network=allow_network,
            truncated_dates=truncated_dates,
            clock=clock,
            deadline=deadline,
        )

    if not is_ok:
        state["cnhk_probe_degraded"] = True
        return [], state

    state["cnhk_probe_degraded"] = False
    if not is_complete:
        state["cnhk_probe_partial"] = True
        logger.warning(
            "[DALTON_CNHK_PROBE] query budget (%.1fs) exhausted during window split for %s (%s..%s); "
            "contiguous_end=%s, marking cnhk_probe_partial=True.",
            total_budget_s,
            norm_ticker,
            start_date.isoformat(),
            end_str,
            completed_end_date.isoformat() if completed_end_date else None,
        )
        if completed_end_date is not None:
            state["last_cnhk_check_date"] = completed_end_date.isoformat()
    else:
        state.pop("cnhk_probe_partial", None)
        state["last_cnhk_check_date"] = end_str
    if truncated_dates:
        prev_trunc = state.get("cnhk_probe_truncated_dates")
        existing_trunc = [str(x) for x in prev_trunc if x] if isinstance(prev_trunc, list) else []
        state["cnhk_probe_truncated_dates"] = list(dict.fromkeys([*existing_trunc, *truncated_dates]))

    raw_seen = state.get("seen_cnhk_filings")
    existing_seen: list[str] = (
        [str(x) for x in raw_seen if x] if isinstance(raw_seen, list) else []
    )
    seen_set: set[str] = set(existing_seen)
    new_hashes: list[str] = []
    signals: list[ProbeSignal] = []

    for item in rows:
        f_date = str(item.get("date") or item.get("filing_date") or end_str).strip()[:10]
        title = str(item.get("title") or "").strip()
        category = str(item.get("category") or item.get("type") or "").strip()
        pdf_link = str(item.get("pdf_link") or item.get("url") or "").strip()
        if not title and not pdf_link:
            continue

        f_hash = compute_cnhk_filing_hash(f_date, title, pdf_link)
        if f_hash in seen_set:
            continue
        seen_set.add(f_hash)
        new_hashes.append(f_hash)

        prio = classify_cnhk_priority(title, category)
        payload: dict[str, Any] = {
            "kind": "filing",
            "source": "cninfo",
            "ticker": cn_code,
            "date": f_date,
            "title": title,
            "category": category,
            "pdf_link": pdf_link,
        }
        if kind == "hk":
            payload["market"] = "HK"
        signals.append(
            ProbeSignal(
                probe="cnhk",
                signal_key=f"filing:cninfo:{f_hash}",
                priority=prio,
                summary=f"{norm_ticker} 新巨潮公告（{prio}）（{f_date}）",
                payload=payload,
            )
        )

    merged_seen = list(dict.fromkeys([*new_hashes, *existing_seen]))
    state["seen_cnhk_filings"] = merged_seen[:MAX_SEEN_CNHK_FILINGS]
    return signals, state


probe_cnhk = probe_cnhk_filings
