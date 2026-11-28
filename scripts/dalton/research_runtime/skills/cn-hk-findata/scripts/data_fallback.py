#!/usr/bin/env python3
"""
data_fallback.py — Multi-source data fallback framework for cn-hk-findata.

Provides a priority-based data source chain with:
- Automatic failover when primary source fails
- Failure counting scoped per (source, capability)
- Source health tracking persisted to disk
- Unified return format regardless of source used

Sources (priority order):
  1. AkShare (primary) — 东方财富
  2. Tencent Finance API — 腾讯财经
  3. Sina Finance API — 新浪财经

Health is tracked per (source, capability) rather than per source. AkShare
reaches 东方财富 through several independent host families (push2his 行情、
datacenter 榜单、push2ex 涨停池), and one of them being unreachable says
nothing about the others. A single source-wide counter made a blocked K 线
endpoint look like "AkShare is down".
"""

import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _resolve_state_file() -> Path:
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return home_root / "state" / "cn-hk-findata" / "source_health.json"


STATE_FILE = _resolve_state_file()

# Vendors reachable through AkShare. 同花顺/雪球 joined when the 东财 行情
# hosts started refusing this machine and per-intent fallbacks were added.
KNOWN_SOURCES = ["akshare", "tencent", "sina", "ths", "xueqiu"]

# Max consecutive failures before a source is deprioritized for that capability
MAX_FAILURES_BEFORE_SKIP = 3
# How long a "sick" (source, capability) pair stays deprioritized (seconds)
SKIP_DURATION = 300  # 5 minutes

# Separator between source name and capability in the health state keys
_KEY_SEP = "::"


def _load_health() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_health(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _capability_from_context(context: str) -> Optional[str]:
    """Derive a capability label from a context string like "quote:600519"."""
    if not context:
        return None
    return context.split(":", 1)[0].strip() or None


def _health_key(source_name: str, capability: Optional[str] = None) -> str:
    return f"{source_name}{_KEY_SEP}{capability}" if capability else source_name


def _split_key(key: str) -> tuple[str, Optional[str]]:
    source, sep, capability = key.partition(_KEY_SEP)
    return source, (capability if sep else None)


def _is_key_healthy(key: str, health: dict) -> bool:
    """Check whether a (source, capability) pair should be tried first."""
    info = health.get(key, {})
    consec_failures = info.get("consecutive_failures", 0)
    if consec_failures < MAX_FAILURES_BEFORE_SKIP:
        return True
    # Check if skip duration has passed
    last_fail = info.get("last_failure_ts", 0)
    if time.time() - last_fail > SKIP_DURATION:
        return True  # Give it another chance
    return False


def _is_source_healthy(source_name: str, health: dict,
                       capability: Optional[str] = None) -> bool:
    """Healthy if the named capability is healthy; without a capability,
    healthy if any tracked capability of that source is healthy."""
    if capability:
        return _is_key_healthy(_health_key(source_name, capability), health)

    keys = [k for k in health if _split_key(k)[0] == source_name]
    if not keys:
        return True
    return any(_is_key_healthy(k, health) for k in keys)


def _record_success(source_name: str, health: dict,
                    capability: Optional[str] = None):
    key = _health_key(source_name, capability)
    prev = health.get(key, {})
    health[key] = {
        "consecutive_failures": 0,
        "last_success_ts": time.time(),
        "last_success_time": datetime.now().isoformat(),
        "total_successes": prev.get("total_successes", 0) + 1,
        "total_failures": prev.get("total_failures", 0),
    }
    _save_health(health)


def _record_failure(source_name: str, error: str, health: dict,
                    capability: Optional[str] = None):
    key = _health_key(source_name, capability)
    info = health.get(key, {})
    info["consecutive_failures"] = info.get("consecutive_failures", 0) + 1
    info["last_failure_ts"] = time.time()
    info["last_failure_time"] = datetime.now().isoformat()
    info["last_error"] = error[:200]
    info["total_failures"] = info.get("total_failures", 0) + 1
    info["total_successes"] = info.get("total_successes", 0)
    health[key] = info
    _save_health(health)


def _order_by_health(providers: list[dict], health: dict,
                     capability: Optional[str]) -> list[dict]:
    """Stable reorder: healthy providers first, sick ones last.

    Sick providers are demoted rather than skipped, so a chain where every
    source is currently sick still attempts all of them instead of falling
    through to whichever happens to be last in the list.
    """
    healthy, sick = [], []
    for provider in providers:
        target = healthy if _is_source_healthy(provider["name"], health, capability) else sick
        target.append(provider)
    return healthy + sick


def fallback_fetch(providers: list[dict], context: str = "",
                   capability: Optional[str] = None) -> dict:
    """
    Try each provider until one succeeds, healthiest first.

    Args:
        providers: list of dicts, each with:
            - "name": str — source identifier (e.g. "akshare", "tencent", "sina")
            - "fetch": callable — function that returns data (dict/DataFrame/list)
            - "transform": callable (optional) — normalize output to standard format
        context: str — description of what we're fetching (for logging)
        capability: str (optional) — health-tracking scope; defaults to the
            part of `context` before the first ":" (e.g. "quote:600519" → "quote")

    Returns:
        dict with:
            - "ok": True/False
            - "source": which source succeeded
            - "data": the fetched data
            - "fallback_used": True if the declared primary source was not used
            - "capability": health scope applied
            - "attempt_order": order the sources were tried in
            - "errors": list of {source, error} for failed attempts
    """
    health = _load_health()
    errors = []
    primary_name = providers[0]["name"] if providers else "unknown"
    if capability is None:
        capability = _capability_from_context(context)

    ordered = _order_by_health(providers, health, capability)
    attempt_order = [p["name"] for p in ordered]

    for provider in ordered:
        name = provider["name"]
        fetch_fn = provider["fetch"]
        transform_fn = provider.get("transform")

        try:
            raw_data = fetch_fn()
            if raw_data is None:
                raise ValueError(f"{name} returned None")

            # Apply transform if provided
            data = transform_fn(raw_data) if transform_fn else raw_data

            _record_success(name, health, capability)
            return {
                "ok": True,
                "source": name,
                "data": data,
                "fallback_used": (name != primary_name),
                "primary_source": primary_name,
                "capability": capability,
                "attempt_order": attempt_order,
                "errors": errors if errors else None,
            }
        except Exception as e:
            err_str = str(e)
            _record_failure(name, err_str, health, capability)
            errors.append({
                "source": name,
                "error": err_str[:300],
                "traceback": traceback.format_exc()[-500:],
            })
            continue

    return {
        "ok": False,
        "source": None,
        "data": None,
        "fallback_used": True,
        "capability": capability,
        "attempt_order": attempt_order,
        "errors": errors,
        "error": f"All {len(providers)} sources failed for: {context}",
        "hint": "Check network connectivity and API availability",
    }


# ---------------------------------------------------------------------------
# Tencent Finance API helpers
# ---------------------------------------------------------------------------
def tencent_stock_quote(ticker: str, market: str = "a") -> dict:
    """Fetch real-time quote from Tencent Finance API."""
    import requests

    # Format ticker for Tencent API
    if market == "hk":
        symbol = f"hk{ticker.zfill(5)}"
    elif market == "us":
        symbol = f"us{ticker.upper()}"
    else:
        # A-share: determine SH/SZ
        t = ticker.zfill(6)
        prefix = "sh" if t.startswith(("6", "5", "9")) else "sz"
        symbol = f"{prefix}{t}"

    url = f"https://qt.gtimg.cn/q={symbol}"
    headers = {"Referer": "https://finance.qq.com", "User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    text = resp.text.strip()

    if "v_pv_none" in text or not text:
        raise ValueError(f"Tencent API: no data for {symbol}")

    # Parse response: v_sh600519="1~贵州茅台~600519~1720.00~..."
    parts = text.split("~")
    if len(parts) < 45:
        raise ValueError(f"Tencent API: unexpected format for {symbol}")

    return {
        "name": parts[1],
        "code": parts[2],
        "price": float(parts[3]) if parts[3] else None,
        "prev_close": float(parts[4]) if parts[4] else None,
        "open": float(parts[5]) if parts[5] else None,
        "volume": float(parts[6]) if parts[6] else None,  # 手
        "buy_volume": float(parts[7]) if parts[7] else None,
        "sell_volume": float(parts[8]) if parts[8] else None,
        "high": float(parts[33]) if parts[33] else None,
        "low": float(parts[34]) if parts[34] else None,
        "change_pct": float(parts[32]) if parts[32] else None,
        "turnover": float(parts[37]) if parts[37] else None,  # 万元
        "pe": _safe_float(parts[39]),
        "pb": _safe_float(parts[46]) if len(parts) > 46 else None,
        "source": "tencent",
    }


def _safe_float(val) -> Optional[float]:
    """Safely convert to float, return None on failure."""
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


def tencent_stock_hist(ticker: str, market: str = "a", days: int = 120) -> list:
    """Fetch historical daily OHLCV from Tencent Finance."""
    import requests
    from datetime import datetime, timedelta

    if market == "hk":
        symbol = f"hk{ticker.zfill(5)}"
    elif market == "us":
        symbol = f"us{ticker.upper()}"
    else:
        t = ticker.zfill(6)
        prefix = "sh" if t.startswith(("6", "5", "9")) else "sz"
        symbol = f"{prefix}{t}"

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")

    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "param": f"{symbol},day,{start_date},{end_date},{days},qfq",
        "_var": f"kline_dayqfq{symbol}",
    }
    headers = {"Referer": "https://finance.qq.com", "User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()

    # Parse JSONP response
    text = resp.text
    json_start = text.index("{")
    data = json.loads(text[json_start:])

    klines = data.get("data", {}).get(symbol, {})
    day_data = klines.get("qfqday", klines.get("day", []))

    if not day_data:
        raise ValueError(f"Tencent kline: no data for {symbol}")

    rows = []
    for row in day_data[-days:]:
        rows.append({
            "date": row[0],
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 else None,
            "source": "tencent",
        })
    return rows


# ---------------------------------------------------------------------------
# Sina Finance API helpers
# ---------------------------------------------------------------------------
def sina_stock_quote(ticker: str, market: str = "a") -> dict:
    """Fetch real-time quote from Sina Finance API."""
    import requests

    if market == "hk":
        symbol = f"hk{ticker.zfill(5)}"
    elif market == "us":
        symbol = f"gb_{ticker.lower()}"
    else:
        t = ticker.zfill(6)
        prefix = "sh" if t.startswith(("6", "5", "9")) else "sz"
        symbol = f"{prefix}{t}"

    url = f"https://hq.sinajs.cn/list={symbol}"
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    text = resp.text.strip()

    if '=""' in text or not text:
        raise ValueError(f"Sina API: no data for {symbol}")

    # Parse: var hq_str_sh600519="贵州茅台,1720,..."
    quote_str = text.split('"')[1] if '"' in text else ""
    parts = quote_str.split(",")

    if market == "hk":
        if len(parts) < 15:
            raise ValueError(f"Sina HK API: unexpected format")
        return {
            "name": parts[1],
            "code": ticker,
            "price": float(parts[6]) if parts[6] else None,
            "prev_close": float(parts[3]) if parts[3] else None,
            "open": float(parts[2]) if parts[2] else None,
            "high": float(parts[4]) if parts[4] else None,
            "low": float(parts[5]) if parts[5] else None,
            "volume": float(parts[12]) if parts[12] else None,
            "change_pct": float(parts[8]) if parts[8] else None,
            "source": "sina",
        }
    else:
        if len(parts) < 30:
            raise ValueError(f"Sina A-share API: unexpected format")
        return {
            "name": parts[0],
            "code": ticker,
            "open": float(parts[1]) if parts[1] else None,
            "prev_close": float(parts[2]) if parts[2] else None,
            "price": float(parts[3]) if parts[3] else None,
            "high": float(parts[4]) if parts[4] else None,
            "low": float(parts[5]) if parts[5] else None,
            "volume": float(parts[8]) if parts[8] else None,
            "turnover": float(parts[9]) if parts[9] else None,
            "source": "sina",
        }


def sina_stock_hist(ticker: str, market: str = "a", days: int = 120) -> list:
    """Fetch historical daily OHLCV from Sina Finance."""
    import requests
    from datetime import datetime

    if market == "hk":
        # Sina HK historical is limited; use simplified API
        raise NotImplementedError("Sina HK historical not implemented; use AkShare/Tencent")

    t = ticker.zfill(6)
    prefix = "sh" if t.startswith(("6", "5", "9")) else "sz"
    symbol = f"{prefix}{t}"

    # Use Sina's K-line API
    url = f"https://quotes.sina.cn/cn/api/jsonp.php/var%20_data=/CN_MarketDataService.getKLineData"
    params = {
        "symbol": symbol,
        "scale": "240",  # daily
        "ma": "no",
        "datalen": str(days),
    }
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()

    text = resp.text
    json_start = text.index("[")
    json_end = text.rindex("]") + 1
    data = json.loads(text[json_start:json_end])

    if not data:
        raise ValueError(f"Sina kline: no data for {symbol}")

    rows = []
    for row in data:
        rows.append({
            "date": row.get("day", "")[:10],
            "open": float(row.get("open", 0)),
            "close": float(row.get("close", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "volume": float(row.get("volume", 0)),
            "source": "sina",
        })
    return rows


# ---------------------------------------------------------------------------
# Health status query
# ---------------------------------------------------------------------------
def _status_entry(info: dict, healthy: bool) -> dict:
    return {
        "healthy": healthy,
        "consecutive_failures": info.get("consecutive_failures", 0),
        "total_successes": info.get("total_successes", 0),
        "total_failures": info.get("total_failures", 0),
        "last_success": info.get("last_success_time"),
        "last_failure": info.get("last_failure_time"),
        "last_error": info.get("last_error"),
    }


def get_source_health() -> dict:
    """Return health status per source, broken down by capability.

    `healthy` at the source level means at least one tracked capability is
    healthy — a source is not "down" just because one endpoint family is
    unreachable. Read `capabilities` to see which specific ones are failing.
    """
    health = _load_health()
    status = {}
    observed = {_split_key(k)[0] for k in health}
    for name in KNOWN_SOURCES + sorted(observed - set(KNOWN_SOURCES)):
        capabilities = {}
        for key, info in health.items():
            source, capability = _split_key(key)
            if source != name:
                continue
            label = capability or "unscoped"
            capabilities[label] = _status_entry(info, _is_key_healthy(key, health))

        entry = _status_entry(
            health.get(name, {}),
            _is_source_healthy(name, health),
        )
        entry["total_successes"] = sum(c["total_successes"] for c in capabilities.values()) or entry["total_successes"]
        entry["total_failures"] = sum(c["total_failures"] for c in capabilities.values()) or entry["total_failures"]
        entry["capabilities"] = capabilities
        entry["degraded_capabilities"] = sorted(
            label for label, c in capabilities.items() if not c["healthy"]
        )
        status[name] = entry
    return status


def reset_source_health(source_name: str = None):
    """Reset health tracking for one source (all its capabilities) or everything."""
    if source_name:
        health = _load_health()
        for key in [k for k in health if _split_key(k)[0] == source_name]:
            health.pop(key, None)
        _save_health(health)
    else:
        _save_health({})
