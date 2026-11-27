#!/usr/bin/env python3
"""cn-hk-findata backend adapter — Registry-based architecture (v2).

v2.1: Added multi-source fallback (AkShare → Tencent → Sina).
"""
import argparse
import datetime as dt
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

# Import fallback framework
try:
    from data_fallback import (
        fallback_fetch, tencent_stock_quote, tencent_stock_hist,
        sina_stock_quote, sina_stock_hist, get_source_health,
        reset_source_health,
    )
    HAS_FALLBACK = True
except ImportError:
    HAS_FALLBACK = False

# Suppress tqdm progress bars that pollute JSON output on stderr/stdout
os.environ["TQDM_DISABLE"] = "1"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REGISTRY_PATH = SCRIPT_DIR / "intent_registry.yaml"
NAME_CACHE_PATH = SCRIPT_DIR / "stock_name_cache.json"
NAME_CACHE_MAX_AGE_S = 86400  # 24h

_INTENT_REGISTRY: list[dict] | None = None
_NAME_CACHE: dict | None = None
_RESOLVE_CACHE: dict = {}


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def error_hint(msg: str) -> str:
    m = (msg or "").lower()
    if "akshare" in m and ("import" in m or "no module" in m):
        return "缺少 akshare 依赖。请重跑 run.sh 让 venv 自动安装 requirements。"
    if "ticker" in m or "symbol" in m:
        return "请在问题里给出股票代码（如 00700/600519/AAPL）或名称。"
    if "no candidate" in m:
        return "问题意图过于宽泛；请补充市场、标的、时间范围或指标关键词。"
    return "检查参数、依赖与网络后重试。"


# Errors that mean the host is refusing this client, not that the request was
# unlucky. Retrying these is actively harmful: repeated hits on 东财's blocked
# 行情 hosts on 2026-08-21 widened the block from /kline to /fflow within one
# session. Fail fast and let the caller move to the next source instead.
_BLOCKED_ERROR_MARKERS = (
    "remotedisconnected", "connection aborted", "connection reset",
    "connection refused", "bad gateway", "502", "503", "forbidden", "403",
    "proxyerror", "sslerror", "eof occurred",
)


def _is_blocked_error(err_str: str) -> bool:
    return any(k in err_str for k in _BLOCKED_ERROR_MARKERS)


def _retry_call(func, kwargs, max_retries: int = 1, delay: float = 1.5):
    """Call an AkShare function, retrying only genuinely transient errors."""
    import time
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return func(**kwargs) if kwargs else func()
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            if _is_blocked_error(err_str):
                raise
            is_transient = any(k in err_str for k in [
                "timeout", "timed out", "maxretry", "urlopen", "temporarily",
            ])
            if not is_transient or attempt >= max_retries:
                raise
            time.sleep(delay * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------------------
# Multi-source fallback wrappers
# ---------------------------------------------------------------------------
def _detect_market(ticker: str) -> str:
    """Detect market from ticker format."""
    t = str(ticker).strip()
    if t.isdigit():
        if len(t) == 5:
            return "hk"
        return "a"
    return "us"


def _fallback_quote(ticker: str, market: str = None) -> dict:
    """Fetch real-time quote with AkShare → Tencent → Sina fallback."""
    import akshare as ak
    if market is None:
        market = _detect_market(ticker)

    providers = []

    # Provider 1: AkShare (primary)
    def akshare_quote():
        if market == "hk":
            df = ak.stock_hk_spot_em()
            row = df[df["代码"] == str(ticker).zfill(5)]
            if row.empty:
                raise ValueError(f"AkShare: HK ticker {ticker} not found in spot")
            r = row.iloc[0]
            return {
                "name": r.get("名称", ""), "code": str(ticker),
                "price": float(r.get("最新价", 0)), "change_pct": float(r.get("涨跌幅", 0)),
                "volume": float(r.get("成交量", 0)), "turnover": float(r.get("成交额", 0)),
                "open": float(r.get("今开", 0)), "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)), "prev_close": float(r.get("昨收", 0)),
                "source": "akshare",
            }
        else:
            df = ak.stock_zh_a_spot_em()
            t = str(ticker).zfill(6)
            row = df[df["代码"] == t]
            if row.empty:
                raise ValueError(f"AkShare: A-share {t} not found")
            r = row.iloc[0]
            return {
                "name": r.get("名称", ""), "code": t,
                "price": float(r.get("最新价", 0)), "change_pct": float(r.get("涨跌幅", 0)),
                "volume": float(r.get("成交量", 0)), "turnover": float(r.get("成交额", 0)),
                "open": float(r.get("今开", 0)), "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)), "prev_close": float(r.get("昨收", 0)),
                "pe": float(r.get("市盈率-动态", 0)), "pb": float(r.get("市净率", 0)),
                "source": "akshare",
            }

    providers.append({"name": "akshare", "fetch": akshare_quote})

    if HAS_FALLBACK:
        providers.append({
            "name": "tencent",
            "fetch": lambda: tencent_stock_quote(ticker, market),
        })
        providers.append({
            "name": "sina",
            "fetch": lambda: sina_stock_quote(ticker, market),
        })

    if HAS_FALLBACK:
        return fallback_fetch(providers, context=f"quote:{ticker}")
    else:
        return {"ok": True, "source": "akshare", "data": akshare_quote(), "fallback_used": False}


def _fallback_hist(ticker: str, days: int = 120, market: str = None) -> dict:
    """Fetch historical OHLCV with AkShare → Tencent → Sina fallback."""
    import akshare as ak
    import pandas as pd
    if market is None:
        market = _detect_market(ticker)

    providers = []

    def akshare_hist():
        end = dt.date.today()
        start = end - dt.timedelta(days=days * 2)
        if market == "hk":
            df = ak.stock_hk_hist(symbol=str(ticker).zfill(5), period="daily",
                                   start_date=start.strftime("%Y%m%d"),
                                   end_date=end.strftime("%Y%m%d"), adjust="")
        else:
            df = ak.stock_zh_a_hist(symbol=str(ticker).zfill(6), period="daily",
                                     start_date=start.strftime("%Y%m%d"),
                                     end_date=end.strftime("%Y%m%d"), adjust="qfq")
        if df is None or df.empty:
            raise ValueError(f"AkShare hist: no data for {ticker}")
        # Standardize columns
        col_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                    "最低": "low", "成交量": "volume", "成交额": "turnover"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        rows = df.tail(days).to_dict(orient="records")
        for r in rows:
            r["source"] = "akshare"
            if "date" in r:
                r["date"] = str(r["date"])[:10]
        return rows

    providers.append({"name": "akshare", "fetch": akshare_hist})

    if HAS_FALLBACK:
        providers.append({
            "name": "tencent",
            "fetch": lambda: tencent_stock_hist(ticker, market, days),
        })
        if market == "a":  # Sina hist only works for A-share
            providers.append({
                "name": "sina",
                "fetch": lambda: sina_stock_hist(ticker, market, days),
            })

    if HAS_FALLBACK:
        return fallback_fetch(providers, context=f"hist:{ticker}:{days}d")
    else:
        return {"ok": True, "source": "akshare", "data": akshare_hist(), "fallback_used": False}


# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------
def _load_registry() -> list[dict]:
    global _INTENT_REGISTRY
    if _INTENT_REGISTRY is not None:
        return _INTENT_REGISTRY
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        _INTENT_REGISTRY = yaml.safe_load(f) or []
    return _INTENT_REGISTRY


# ---------------------------------------------------------------------------
# Company name → code cache
# ---------------------------------------------------------------------------
_HARDCODED_NAMES = {
    "腾讯": "00700", "腾讯控股": "00700",
    "茅台": "600519", "贵州茅台": "600519",
    "宁德时代": "300750", "苹果": "AAPL", "特斯拉": "TSLA",
    "中际旭创": "300308", "天孚通信": "300394",
}


def _load_name_cache() -> dict:
    global _NAME_CACHE
    if _NAME_CACHE is not None:
        return _NAME_CACHE

    # Load from pre-built disk cache (shipped with skill, ~5500 entries)
    if NAME_CACHE_PATH.exists():
        try:
            with open(NAME_CACHE_PATH, "r", encoding="utf-8") as f:
                _NAME_CACHE = json.load(f)
            # merge hardcoded to ensure they're always present
            for k, v in _HARDCODED_NAMES.items():
                _NAME_CACHE.setdefault(k, v)
            return _NAME_CACHE
        except Exception:
            pass

    # fallback: hardcoded only (cache file missing)
    _NAME_CACHE = dict(_HARDCODED_NAMES)
    return _NAME_CACHE


def _resolve_symbol_via_akshare(name: str, market_hint: str = ""):
    key = f"{market_hint}:{name}"
    if key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[key]
    import akshare as ak

    def _lookup(fetch, name_cols, code_col_candidates, pad):
        df = fetch()
        if df is None or df.empty:
            return None
        for col in name_cols:
            if col not in df.columns:
                continue
            hit = df[df[col].astype(str).str.contains(name, regex=False, na=False)]
            if hit.empty:
                continue
            for code_col in code_col_candidates:
                if code_col in hit.columns:
                    code = re.sub(r"^(sh|sz|bj|hk)", "", str(hit.iloc[0][code_col]).strip(), flags=re.I)
                    return code.zfill(pad) if pad else code
        return None

    # 东财 spot lists live on the push2 hosts, which are the ones refusing this
    # machine; 新浪/腾讯 carry the same name→code mapping.
    sources = []
    if market_hint in {"hk", ""}:
        sources += [
            (lambda: ak.stock_hk_spot_em(), ["名称", "name", "NAME"], ["代码", "symbol"], 5),
            (lambda: ak.stock_hk_spot(), ["中文名称", "名称", "name"], ["代码", "symbol"], 5),
        ]
    if market_hint in {"cn", ""}:
        sources += [
            (lambda: ak.stock_zh_a_spot_em(), ["名称", "name", "NAME"], ["代码", "symbol"], 0),
            (lambda: ak.stock_zh_a_spot_tx(), ["name", "名称"], ["code", "代码"], 0),
        ]

    for fetch, name_cols, code_cols, pad in sources:
        try:
            code = _lookup(fetch, name_cols, code_cols, pad)
        except Exception:
            continue
        if code:
            _RESOLVE_CACHE[key] = code
            return code
    return None


# ---------------------------------------------------------------------------
# Ticker / date extraction
# ---------------------------------------------------------------------------
def _extract_ticker_from_text(q: str):
    q = q.strip()
    # name cache lookup
    cache = _load_name_cache()
    # sort by length desc to match longest name first
    for name in sorted(cache.keys(), key=len, reverse=True):
        if name in q:
            return cache[name]

    m_hk = re.search(r"(?<!\d)(\d{5})(?!\d)", q)
    if m_hk:
        return m_hk.group(1)
    m_cn = re.search(r"(?<!\d)(\d{6})(?!\d)", q)
    if m_cn:
        return m_cn.group(1)
    m_us = re.search(r"\b([A-Z]{1,5})\b", q)
    if m_us:
        return m_us.group(1)

    # fallback: resolve common names via live spot
    for n in ["腾讯", "阿里", "茅台", "宁德", "比亚迪", "美团", "小米", "京东", "百度", "网易"]:
        if n in q:
            code = _resolve_symbol_via_akshare(n, market_hint="hk" if "港" in q else "")
            if code:
                return code
    return None


def _extract_two_tickers(question: str):
    nums = re.findall(r"(?<!\d)(\d{5,6})(?!\d)", question)
    if len(nums) >= 2:
        return nums[0], nums[1]
    parts = re.split(r"和|与|vs|VS|对比|比较", question)
    candidates = []
    for p in parts:
        t = _extract_ticker_from_text(p.strip())
        if t:
            candidates.append(t)
    if len(candidates) >= 2:
        return candidates[0], candidates[1]
    return None, None


def _extract_dates(q: str):
    out = re.findall(r"(20\d{2}[-/]?\d{2}[-/]?\d{2})", q)
    norm = [d.replace("/", "-") for d in out]
    norm = [d if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in norm]
    today = dt.date.today()
    if "近一年" in q or "最近一年" in q:
        norm = [(today - dt.timedelta(days=365)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")]
    elif "近三年" in q or "最近三年" in q:
        norm = [(today - dt.timedelta(days=365 * 3)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")]
    elif "近五年" in q or "最近五年" in q:
        norm = [(today - dt.timedelta(days=365 * 5)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")]
    return norm


def _extract_period_days(question: str, default_days: int = 30) -> int:
    m = re.search(r"(近|过去|最近)\s*(\d{1,4})\s*天", question)
    if m:
        return max(1, min(3650, int(m.group(2))))
    if "近一年" in question or "最近一年" in question:
        return 365
    if "近三年" in question or "最近三年" in question:
        return 365 * 3
    return default_days


def _to_yyyymmdd(d: str) -> str:
    return d.replace("-", "")


def _get_latest_quarter() -> str:
    today = dt.date.today()
    y = today.year
    m = today.month
    # map to latest completed quarter
    if m <= 3:
        return f"{y - 1}0930"
    elif m <= 6:
        return f"{y}0331"
    elif m <= 9:
        return f"{y}0630"
    else:
        return f"{y}0930"


# ---------------------------------------------------------------------------
# Placeholder resolvers
# ---------------------------------------------------------------------------
def _build_resolver_map(question: str, ticker: str | None) -> dict:
    today = dt.date.today()
    dates = _extract_dates(question)
    period_days = _extract_period_days(question, 30)
    start = today - dt.timedelta(days=period_days)

    ticker_str = str(ticker) if ticker else ""
    ticker6 = ticker_str.zfill(6) if ticker_str.isdigit() else ticker_str
    ticker5 = ticker_str.zfill(5) if ticker_str.isdigit() else ticker_str

    # detect hsgt market
    hsgt_market = "沪股通"
    if "深股通" in question:
        hsgt_market = "深股通"
    elif "港股通" in question:
        hsgt_market = "港股通"

    # detect market sh/sz
    market_sh_sz = "sh"
    if ticker_str.startswith("0") or ticker_str.startswith("3"):
        market_sh_sz = "sz"

    # detect board name — the quoted form first, then "XX概念/XX行业板块".
    # The previous pattern only listed straight ASCII quotes, so a question
    # written with 中文引号 silently fell through to the default board.
    board_name = ""
    m = re.search(r'[“”"「」『』‘’\'](.{1,14}?)[“”"「」『』‘’\']', question)
    if m and m.group(1).strip():
        board_name = m.group(1).strip()
    else:
        m2 = re.search(r'([一-龥A-Za-z0-9]{2,10}?)(?:概念板块|行业板块|概念|板块|行业)', question)
        if m2 and m2.group(1).strip():
            board_name = m2.group(1).strip()
    board_name_explicit = bool(board_name)
    if not board_name:
        board_name = "CPO概念"

    # detect annual/quarterly
    indicator = "年度"
    if "季度" in question or "季报" in question:
        indicator = "报告期"

    # SH/SZ prefix for A-share financial interfaces
    if ticker_str.isdigit() and len(ticker_str) == 6:
        if ticker_str.startswith("6"):
            ticker_cn_prefix = f"SH{ticker_str}"
        else:
            ticker_cn_prefix = f"SZ{ticker_str}"
    else:
        ticker_cn_prefix = ticker6

    # sh/sz lowercase prefix for stock_gdfx_free_top_10_em etc
    if ticker_str.isdigit() and len(ticker_str) == 6:
        if ticker_str.startswith("6"):
            ticker_sh_sz_prefix = f"sh{ticker_str}"
        else:
            ticker_sh_sz_prefix = f"sz{ticker_str}"
    else:
        ticker_sh_sz_prefix = ticker6

    # Index codes do not follow the stock prefix rule: 000xxx/95xxxx are SH,
    # 399xxx are SZ.
    if ticker_str.isdigit() and len(ticker_str) == 6:
        index_sh_sz_prefix = f"sz{ticker_str}" if ticker_str.startswith("39") else f"sh{ticker_str}"
    else:
        index_sh_sz_prefix = ticker_str

    hk_index_symbol = "HSI"
    for kw, sym in (("恒生科技", "HSTECH"), ("国企指数", "HSCEI"), ("中国企业", "HSCEI"),
                    ("H股指数", "HSCEI"), ("红筹", "HSCCI"), ("恒生指数", "HSI"), ("恒指", "HSI")):
        if kw in question:
            hk_index_symbol = sym
            break

    start_date = _to_yyyymmdd(dates[0]) if dates else start.strftime("%Y%m%d")
    end_date = _to_yyyymmdd(dates[1]) if len(dates) >= 2 else today.strftime("%Y%m%d")

    return {
        "{ticker}": ticker_str,
        "{ticker6}": ticker6,
        "{ticker5}": ticker5,
        "{ticker_cn_prefix}": ticker_cn_prefix,
        "{ticker_sh_sz_prefix}": ticker_sh_sz_prefix,
        "{index_sh_sz_prefix}": index_sh_sz_prefix,
        "{hk_index_symbol}": hk_index_symbol,
        "{start_date}": start_date,
        "{end_date}": end_date,
        "{start_date_dash}": _to_dashed(start_date),
        "{end_date_dash}": _to_dashed(end_date),
        "{today}": today.strftime("%Y%m%d"),
        "{market_sh_sz}": market_sh_sz,
        "{hsgt_market}": hsgt_market,
        "{board_name}": board_name,
        # Resolved lazily — the THS board lists cost a network call each, and
        # most questions never touch a 同花顺 fallback.
        "{board_name_ths_concept}": _LazyThsBoard(board_name, "concept", board_name_explicit),
        "{board_name_ths_industry}": _LazyThsBoard(board_name, "industry", board_name_explicit),
        "{indicator_annual_quarterly}": indicator,
        "{latest_quarter}": _get_latest_quarter(),
    }


def _resolve_placeholders(args_template: dict, question: str, ticker: str | None) -> dict:
    return _apply_resolver_map(args_template, _build_resolver_map(question, ticker))


def _apply_resolver_map(args_template: dict, resolver_map: dict) -> dict:
    resolved = {}
    for k, v in args_template.items():
        if isinstance(v, str) and v in resolver_map:
            val = resolver_map[v]
            resolved[k] = str(val) if isinstance(val, _LazyThsBoard) else val
        else:
            resolved[k] = v
    return resolved


def _to_dashed(yyyymmdd: str) -> str:
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


# ---------------------------------------------------------------------------
# 同花顺 board-name resolution
#
# EastMoney and 同花顺 do not share a board vocabulary ("半导体及元件" vs
# "半导体"), and the THS interfaces raise KeyError on an unknown name. Board
# names reach us from a regex over the user's question, so they must be mapped
# onto the live THS list before a THS fallback can be attempted.
# ---------------------------------------------------------------------------
_THS_BOARD_CACHE: dict = {}


def _ths_board_names(kind: str) -> list[str]:
    if kind in _THS_BOARD_CACHE:
        return _THS_BOARD_CACHE[kind]
    import akshare as ak
    fn = ak.stock_board_concept_name_ths if kind == "concept" else ak.stock_board_industry_name_ths
    df = fn()
    names = [str(n) for n in df["name"].tolist()] if df is not None and "name" in df.columns else []
    _THS_BOARD_CACHE[kind] = names
    return names


def _resolve_ths_board(name: str, kind: str, explicit: bool = True) -> str:
    """Map a board name onto the closest live 同花顺 board name.

    Deliberately strict: a loose match here returns a real DataFrame for the
    wrong sector, which is worse than an error. A question that never named a
    board is refused outright rather than matched against the default.
    """
    import difflib
    label = "概念" if kind == "concept" else "行业"
    raw = str(name).strip()
    if not raw:
        raise RuntimeError(f"问题里没有指出{label}板块名称")
    if not explicit:
        raise RuntimeError(f"问题里没有指出{label}板块名称，无法回退到同花顺{label}指数")
    names = _ths_board_names(kind)
    if not names:
        raise RuntimeError(f"同花顺{label}板块列表为空")
    # 东财 names are frequently a suffixed form of the THS name
    stripped = re.sub(r"(概念|板块|指数|行业)$", "", raw)
    for cand in (raw, stripped):
        if cand and cand in names:
            return cand
    contained = [n for n in names if n and len(n) >= 2 and (n in raw or raw in n)]
    if contained:
        return max(contained, key=len)
    for cand in (raw, stripped):
        if not cand:
            continue
        close = difflib.get_close_matches(cand, names, n=1, cutoff=0.75)
        if close:
            return close[0]
    raise RuntimeError(f"同花顺{label}板块列表里没有与「{raw}」对应的板块")


class _LazyThsBoard:
    """Placeholder value that resolves to a THS board name on first use."""

    __slots__ = ("_raw", "_kind", "_explicit", "_resolved")

    def __init__(self, raw: str, kind: str, explicit: bool = True):
        self._raw = raw
        self._kind = kind
        self._explicit = explicit
        self._resolved = None

    def __str__(self):
        if self._resolved is None:
            self._resolved = _resolve_ths_board(self._raw, self._kind, self._explicit)
        return self._resolved


# ---------------------------------------------------------------------------
# Two-phase intent matching (keyword + semantic fuzzy)
# ---------------------------------------------------------------------------

# Synonym normalization table: colloquial → canonical terms
_SYNONYM_MAP = {
    # 资金相关
    "钱流": "资金流", "资金面": "资金流", "主力在干嘛": "主力资金", "大资金": "主力资金",
    "谁在抄底": "资金流", "抄底": "资金流", "吸筹": "资金流", "出货": "资金流",
    "大单净量": "大单", "北水": "北向资金", "南下资金": "港股通",
    "外资": "北向资金", "外资在买": "北向资金",
    # 业绩相关
    "赚钱吗": "净利润", "亏钱吗": "净利润", "盈利": "净利润", "营业额": "营业收入",
    "报表": "利润表", "财报": "利润表", "财务状况": "利润表",
    "亏损": "净利润", "利润增长": "净利润", "收入增长": "营业收入",
    "扭亏": "业绩预告", "预喜": "业绩预告", "业绩暴雷": "业绩预告",
    "雷了": "业绩预告", "超预期": "业绩预告",
    # 估值
    "贵不贵": "估值", "便宜吗": "估值", "高估": "估值", "低估": "估值",
    "值不值": "估值", "估值高吗": "PE", "市盈率多少": "PE",
    "性价比": "估值", "泡沫": "估值",
    # 股东
    "谁持有": "十大股东", "持有人": "股东", "大户": "股东", "持股": "股东",
    "筹码": "股东户数", "散户割了吗": "股东户数", "机构跑了吗": "机构持仓",
    "社保持仓": "基金持仓", "公募": "基金持仓", "基金重仓了吗": "基金重仓",
    # 行情
    "涨了多少": "行情", "跌了多少": "行情", "走势怎么样": "走势", "表现如何": "行情",
    "怎么走的": "走势", "股价": "行情", "收盘价": "行情", "开盘价": "行情",
    "最高价": "行情", "最低价": "行情", "成交量": "行情", "换手率": "行情",
    "K线": "日K", "k线": "日k", "技术面": "走势",
    # 热度
    "最近火的": "热门股", "大家买什么": "热门股", "热搜股": "热门股",
    "讨论最多": "热门股", "关注度": "热门股",
    # 分红
    "派了多少": "分红", "有没有分红": "分红", "股息": "分红", "分红率": "分红",
    "股息率": "分红", "除权": "分红", "除息": "分红",
    # 涨跌停
    "封板了": "涨停", "炸板": "炸板池", "跌停了": "跌停",
    "几个板": "连板", "天地板": "异动", "地天板": "异动",
    # 回购
    "回购了吗": "回购", "回购计划": "回购", "注销": "回购",
    # 龙虎榜
    "游资": "龙虎榜", "席位": "龙虎榜", "谁在买卖": "龙虎榜",
    # 两融
    "融资": "两融", "融券": "两融", "杠杆资金": "两融", "做空": "融券",
    # 大宗交易
    "大宗": "大宗交易", "折价成交": "大宗交易", "溢价成交": "大宗交易",
    # 解禁
    "解禁了吗": "解禁", "限售股": "解禁", "什么时候解禁": "解禁",
    # 增减持
    "高管减持": "增减持", "大股东减持": "增减持", "董事增持": "增减持",
    # 质押
    "爆仓": "股权质押", "质押多少": "股权质押",
    # 研报
    "券商怎么看": "研报", "目标价多少": "研报", "给的评级": "研报",
    # 调研
    "谁来调研了": "机构调研", "调研了吗": "机构调研",
    # IPO
    "新股中签": "打新", "打新了吗": "打新", "中签率": "打新",
    # 个股信息
    "干嘛的": "主营业务", "做什么的": "主营业务", "哪个行业": "主营业务",
    # 新闻
    "有什么新闻": "个股新闻", "最近消息": "个股新闻", "利好利空": "个股新闻",
    # 板块
    "什么概念": "概念板块", "属于什么板块": "行业板块", "哪个板块": "行业板块",
}


def _normalize_question(question: str) -> str:
    """Apply synonym normalization to boost intent matching."""
    normalized = question
    for colloquial, canonical in _SYNONYM_MAP.items():
        if colloquial in normalized:
            normalized = normalized + " " + canonical
    return normalized


def _fuzzy_substring_score(keyword: str, text: str) -> float:
    """Return 0.0-1.0 score for fuzzy substring match."""
    if keyword in text:
        return 1.0
    # character overlap ratio for CJK
    if len(keyword) >= 2:
        chars = set(keyword)
        overlap = sum(1 for c in chars if c in text)
        ratio = overlap / len(chars)
        if ratio >= 0.7:
            return ratio * 0.5  # partial credit
    return 0.0


def _score_intent(entry: dict, question: str) -> int:
    """Phase 1: keyword exact match + Phase 2: fuzzy/synonym scoring."""
    q_normalized = _normalize_question(question)
    q_lower = q_normalized.lower()

    score = 0
    hits = 0
    for kw in entry.get("keywords", []):
        kw_str = str(kw)
        kw_lower = kw_str.lower()
        # Phase 1: exact keyword match
        if kw_lower in q_lower:
            score += 3
            hits += 1
            if kw_str in q_normalized:
                score += 1  # CJK exact bonus
            # Long keyword bonus (more specific = more trustworthy)
            if len(kw_str) >= 5:
                score += 2
            elif len(kw_str) >= 3:
                score += 1
        else:
            # Phase 2: fuzzy substring match
            fuzzy = _fuzzy_substring_score(kw_str, q_normalized)
            if fuzzy > 0:
                score += int(fuzzy * 2)  # max +1 for fuzzy

    # Multi-keyword bonus: hitting 2+ keywords = stronger confidence
    if hits >= 2:
        score += hits

    # Bonus: description match (semantic bridge)
    desc = entry.get("description", "")
    if desc:
        desc_chars = set(desc)
        q_chars = set(q_normalized)
        overlap = len(desc_chars & q_chars)
        if overlap >= 3:
            score += 1

    return score


def _match_intent(question: str) -> tuple[dict | None, int]:
    """Return best matching registry entry and its score."""
    registry = _load_registry()
    best = None
    best_score = 0
    for entry in registry:
        s = _score_intent(entry, question)
        if s > best_score:
            best_score = s
            best = entry
    return best, best_score


def _match_multi_intents(question: str, min_score: int = 3, max_intents: int = 4,
                         ticker: str | None = None) -> list[tuple[dict, int]]:
    """Return multiple distinct matching intents for combo queries."""
    registry = _load_registry()
    scored = []
    for entry in registry:
        if not _is_market_compatible(entry, ticker):
            continue
        s = _score_intent(entry, question)
        if s >= min_score:
            scored.append((entry, s))
    # sort by score desc, deduplicate by function
    scored.sort(key=lambda x: x[1], reverse=True)
    seen_funcs = set()
    unique = []
    for entry, score in scored:
        if entry["function"] not in seen_funcs:
            seen_funcs.add(entry["function"])
            unique.append((entry, score))
    return unique[:max_intents]


# ---------------------------------------------------------------------------
# DataFrame helpers
# ---------------------------------------------------------------------------
def _df_to_records(df, limit=50):
    if df is None:
        return []
    if hasattr(df, "head") and hasattr(df, "to_dict"):
        return df.head(limit).to_dict(orient="records")
    return []


def _rows_to_csv(columns, rows, max_rows=200):
    if not columns or not rows:
        return ""
    lines = [",".join([str(c) for c in columns])]
    for r in rows[:max_rows]:
        vals = []
        for c in columns:
            v = r.get(c, "")
            s = str(v).replace('"', '""')
            if "," in s or '"' in s:
                s = f'"{s}"'
            vals.append(s)
        lines.append(",".join(vals))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry entry execution with per-intent source fallback
#
# Registry entries may declare `fallbacks:` — alternative AkShare interfaces
# backed by a different vendor (腾讯/新浪/同花顺/雪球). They exist because the
# 东方财富 行情 hosts (push2his / push2 / N.push2) refuse this machine's exit
# IP, while 东财 datacenter/push2ex hosts and every other vendor answer fine.
# Each fallback carries its own args and an optional `post:` block, because the
# alternatives return different column names, different date formats and — for
# the 新浪 daily interfaces — the entire history rather than a date window.
# ---------------------------------------------------------------------------
def _apply_post(df, post: dict, resolver_map: dict):
    """Normalize a fallback source's frame toward the primary's shape."""
    if df is None or not hasattr(df, "columns") or post is None:
        return df
    import pandas as pd

    out = df

    for col, want in (post.get("filter_eq") or {}).items():
        want = str(resolver_map.get(want, want))
        if col in out.columns and want:
            series = out[col].astype(str).str.strip()
            hit = out[(series == want) | (series.str.zfill(len(want)) == want)]
            out = hit

    date_col = post.get("date_column")
    if date_col and date_col in out.columns and len(out) > 0:
        norm = out[date_col].astype(str).str.slice(0, 10).str.replace("/", "-", regex=False)
        mask = pd.Series(True, index=out.index)
        start = _to_dashed(resolver_map.get("{start_date}", ""))
        end = _to_dashed(resolver_map.get("{end_date}", ""))
        if start:
            mask &= norm >= start
        if end:
            mask &= norm <= end
        # An empty window usually means the date format did not parse the way
        # we assumed; keep the raw frame and let `tail` cut it instead of
        # reporting "no data" for a source that actually answered.
        if mask.any():
            out = out[mask]

    # Vendor snapshot interfaces return the whole universe where the 东财 one
    # was already scoped, so surface the row the question asked about instead
    # of letting the row limit cut it off.
    for col, want in (post.get("sort_first") or {}).items():
        want = str(resolver_map.get(want, want))
        if col in out.columns and want and len(out) > 0:
            match = out[col].astype(str).str.strip() == want
            if match.any():
                out = pd.concat([out[match], out[~match]])

    rename = post.get("rename") or {}
    if rename:
        out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})

    if post.get("tail"):
        out = out.tail(int(post["tail"]))
    if post.get("head"):
        out = out.head(int(post["head"]))

    return out


def _entry_source_specs(entry: dict) -> list[dict]:
    specs = [{
        "function": entry["function"],
        "source": entry.get("source", "akshare"),
        "args": entry.get("args", {}),
        "post": entry.get("post", {}),
        "note": entry.get("note", ""),
    }]
    for fb in entry.get("fallbacks") or []:
        specs.append({
            "function": fb["function"],
            "source": fb.get("source", "fallback"),
            "args": fb.get("args", {}),
            "post": fb.get("post", {}),
            "note": fb.get("note", ""),
        })
    return specs


def _is_market_compatible(entry: dict, ticker: str | None) -> bool:
    """Check the security's market before zero-padding any vendor argument."""
    if not ticker:
        return True
    market = _detect_market(ticker)
    declared = entry.get("markets")
    if declared:
        return market in declared
    fmt = entry.get("ticker_format", "")
    args = str(entry.get("args", {}))
    if fmt in ("cn6", "cn_prefix") or any(x in args for x in ("{ticker6}", "{ticker_cn_prefix}", "{ticker_sh_sz_prefix}")):
        return market == "a"
    if fmt == "hk5" or "{ticker5}" in args:
        return market == "hk"
    if entry.get("id", "").startswith("us_"):
        return market == "us"
    return True


def _run_registry_entry(entry: dict, question: str, ticker: str | None):
    """Execute one registry entry, walking its fallback chain.

    Returns (df, meta). Raises if every declared source fails.
    """
    if not _is_market_compatible(entry, ticker):
        raise ValueError(f"市场不匹配：{ticker} 不适用 {entry['id']}")
    import akshare as ak

    resolver_map = _build_resolver_map(question, ticker)
    specs = _entry_source_specs(entry)

    def make_fetch(spec):
        def fetch():
            func = getattr(ak, spec["function"], None)
            if func is None:
                raise RuntimeError(f"AkShare has no interface {spec['function']}")
            kwargs = _apply_resolver_map(spec.get("args", {}), resolver_map)
            sig = inspect.signature(func)
            cleaned = {}
            for k, v in kwargs.items():
                if k in sig.parameters:
                    if v == "" and sig.parameters[k].default is not inspect._empty:
                        continue
                    cleaned[k] = v
            spec["_kwargs"] = cleaned
            df = _retry_call(func, cleaned)
            df = _apply_post(df, spec.get("post", {}), resolver_map)
            if df is None or (hasattr(df, "empty") and df.empty):
                raise RuntimeError(f"{spec['function']} returned no rows")
            return df
        return fetch

    if not HAS_FALLBACK or len(specs) == 1:
        spec = specs[0]
        df = make_fetch(spec)()
        return df, {"function": spec["function"], "source": spec["source"],
                    "kwargs": spec.get("_kwargs", {}), "fallback_used": False}

    providers = [{"name": s["source"], "fetch": make_fetch(s), "_spec": s} for s in specs]
    result = fallback_fetch(providers, context=f"intent:{entry['id']}",
                            capability=f"intent:{entry['id']}")
    if not result.get("ok"):
        errs = "; ".join(f"{e['source']}: {e['error'][:120]}" for e in (result.get("errors") or []))
        raise RuntimeError(f"{entry['id']} 全部数据源失败 — {errs}")

    used = next(s for s in specs if s["source"] == result["source"])
    return result["data"], {
        "function": used["function"],
        "source": result["source"],
        "kwargs": used.get("_kwargs", {}),
        "fallback_used": result.get("fallback_used", False),
        "attempt_order": result.get("attempt_order"),
        "source_errors": [{"source": e["source"], "error": e["error"][:200]}
                          for e in (result.get("errors") or [])],
        "note": used.get("note", ""),
    }


# ---------------------------------------------------------------------------
# Specialized handlers (compare, xq hot, etc.)
# ---------------------------------------------------------------------------
def _compare_two_stocks_60d(question: str):
    import akshare as ak
    a, b = _extract_two_tickers(question)
    if not a or not b:
        raise RuntimeError("need two tickers for compare")
    end = dt.date.today()
    start = end - dt.timedelta(days=90)

    def fetch_hist(code):
        if str(code).isdigit() and len(str(code)) == 5:
            return ak.stock_hk_hist(symbol=str(code), period="daily", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="")
        return ak.stock_zh_a_hist(symbol=str(code).zfill(6), period="daily", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="qfq")

    dfa, dfb = fetch_hist(a), fetch_hist(b)
    if dfa is None or dfa.empty or dfb is None or dfb.empty:
        raise RuntimeError("hist data empty for one or both tickers")

    def norm(df):
        close_col = "收盘" if "收盘" in df.columns else ("close" if "close" in df.columns else None)
        amt_col = "成交额" if "成交额" in df.columns else ("amount" if "amount" in df.columns else None)
        out = df.tail(60).copy()
        out["_close"] = out[close_col].astype(float)
        out["_amt"] = out[amt_col].astype(float) if amt_col else None
        return out

    a60, b60 = norm(dfa), norm(dfb)

    def metrics(df):
        ret = (df["_close"].iloc[-1] / df["_close"].iloc[0] - 1.0) if len(df) > 1 else 0.0
        vol = df["_close"].pct_change().std() * (252 ** 0.5)
        avg_amt = float(df["_amt"].dropna().mean()) if "_amt" in df.columns and df["_amt"] is not None else None
        return ret, vol, avg_amt

    ra, vola, amta = metrics(a60)
    rb, volb, amtb = metrics(b60)
    return {
        "ok": True, "mode": "ask", "backend": "akshare", "question": question,
        "summary": f"过去60个交易日：{a}收益{ra:.2%}，{b}收益{rb:.2%}，差异{ra - rb:.2%}。",
        "comparison": {
            "ticker_a": a, "ticker_b": b,
            "return_60d": {a: ra, b: rb, "diff": ra - rb},
            "volatility_ann": {a: vola, b: volb, "diff": vola - volb},
            "avg_amount": {a: amta, b: amtb},
        },
        "data": {"a_rows": a60.tail(20).to_dict(orient="records"), "b_rows": b60.tail(20).to_dict(orient="records")},
    }


def _xq_hot_rank_5d(question: str):
    import akshare as ak
    follow = ak.stock_hot_follow_xq(symbol="最热门")
    tweet = ak.stock_hot_tweet_xq(symbol="最热门")
    deal = ak.stock_hot_deal_xq(symbol="最热门")

    def to_scores(df, w):
        if df is None or df.empty:
            return {}
        code_col = next((c for c in ["代码", "股票代码"] if c in df.columns), df.columns[0])
        name_col = next((c for c in ["股票简称", "名称"] if c in df.columns), df.columns[1] if len(df.columns) > 1 else df.columns[0])
        out = {}
        for i, r in df.head(100).reset_index(drop=True).iterrows():
            c = str(r.get(code_col))
            out[c] = {"code": c, "name": str(r.get(name_col)), "score": out.get(c, {}).get("score", 0) + (100 - i) * w}
        return out

    s = {}
    for part in [to_scores(follow, 1.0), to_scores(tweet, 1.0), to_scores(deal, 1.0)]:
        for k, v in part.items():
            if k not in s:
                s[k] = v
            else:
                s[k]["score"] += v["score"]
    top = sorted(s.values(), key=lambda x: x["score"], reverse=True)[:50]
    return {"ok": True, "mode": "ask", "backend": "akshare", "question": question,
            "summary": "雪球热股综合榜（关注+讨论+交易快照聚合）。", "data": {"rows": top}}


# ---------------------------------------------------------------------------
# Generic fallback router (score-based over all stock_* functions)
# ---------------------------------------------------------------------------
KW_FUNC_HINTS = {
    "财务": ["stock_financial", "financial_indicator", "report"],
    "利润表": ["stock_financial_hk_report_em"],
    "资产负债表": ["stock_financial_hk_report_em"],
    "现金流": ["stock_financial_hk_report_em"],
    "港股": ["_hk_", "_hk"], "美股": ["_us_", "_us"],
    "分红": ["dividend", "bonus"], "龙虎榜": ["lhb", "dragon"],
    "资金流": ["fund_flow", "money"], "行情": ["hist", "spot", "minute", "daily"],
    "指标": ["indicator"], "估值": ["indicator", "valuation", "pe", "pb"],
}


def _score_func(func_name: str, question: str):
    s = 0
    low_f = func_name.lower()
    for k, hints in KW_FUNC_HINTS.items():
        if k in question:
            for h in hints:
                if h in low_f:
                    s += 3
    if "hk" in low_f and ("港股" in question or re.search(r"(?<!\d)\d{5}(?!\d)", question)):
        s += 4
    if "hist" in low_f and any(x in question for x in ["历史", "走势", "区间", "近"]):
        s += 2
    if "indicator" in low_f and any(x in question for x in ["指标", "估值", "roe", "pe", "pb"]):
        s += 2
    if "financial" in low_f and any(x in question for x in ["财务", "利润", "税", "报表"]):
        s += 3
    return s


def _infer_args_generic(func, question: str, fname: str):
    sig = inspect.signature(func)
    ticker = _extract_ticker_from_text(question)
    dates = _extract_dates(question)
    kwargs, missing = {}, []
    for name, p in sig.parameters.items():
        required = p.default is inspect._empty
        lname = name.lower()
        if lname in {"stock", "symbol", "code", "ts_code"}:
            if ticker:
                kwargs[name] = ticker
            elif required:
                missing.append(name)
        elif lname in {"start_date", "begin_date", "from_date"}:
            if dates:
                kwargs[name] = _to_yyyymmdd(dates[0])
            elif required:
                missing.append(name)
        elif lname in {"end_date", "to_date"}:
            if len(dates) >= 2:
                kwargs[name] = _to_yyyymmdd(dates[1])
            elif required:
                missing.append(name)
    return kwargs, missing


def _route_via_score(question: str, max_rows: int = 50):
    import akshare as ak
    funcs = [n for n in dir(ak) if n.startswith("stock_") and callable(getattr(ak, n))]
    bad_keys = ["_ths", "_tx", "_sina"]
    funcs = [f for f in funcs if not any(k in f for k in bad_keys)]

    scored = sorted([(f, _score_func(f, question)) for f in funcs], key=lambda x: x[1], reverse=True)
    scored = [x for x in scored if x[1] > 0]
    if not scored:
        raise RuntimeError("No candidate AkShare stock function matched the question")

    attempts, hits = [], []
    for fname, score in scored[:12]:
        f = getattr(ak, fname)
        kwargs, missing = _infer_args_generic(f, question, fname)
        if missing:
            attempts.append({"function": fname, "score": score, "status": "skip", "reason": f"missing: {missing}"})
            continue
        try:
            result = _retry_call(f, kwargs)
            records = _df_to_records(result, limit=max_rows)
            cols = list(result.columns) if hasattr(result, "columns") else []
            if not records:
                attempts.append({"function": fname, "score": score, "status": "empty"})
                continue
            hits.append({"function": fname, "score": score, "kwargs": kwargs, "columns": cols, "rows": records})
            attempts.append({"function": fname, "score": score, "status": "ok", "rows": len(records)})
            if len(hits) >= 3:
                break
        except Exception as e:
            attempts.append({"function": fname, "score": score, "status": "error", "error": str(e)[:240]})

    if not hits:
        return {
            "ok": False, "mode": "ask", "backend": "akshare", "question": question,
            "error": "All candidate functions failed or returned empty",
            "hint": "请补充：市场（A/H/US）、标的代码、日期区间、指标关键词。",
            "attempts": attempts,
        }

    primary = hits[0]
    return {
        "ok": True, "mode": "ask", "backend": "akshare", "question": question,
        "plan": {"chosen_function": primary["function"], "kwargs": primary["kwargs"], "score": primary["score"],
                 "multi_hits": [h["function"] for h in hits]},
        "summary": f"Fallback路由命中 {len(hits)} 个接口，主接口 {primary['function']}",
        "data": {"columns": primary["columns"], "rows": primary["rows"],
                 "csv": _rows_to_csv(primary["columns"], primary["rows"])},
        "extra_hits": [{"function": h["function"], "columns": h["columns"], "rows": h["rows"]} for h in hits[1:]],
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Main ask entry point
# ---------------------------------------------------------------------------
def run_ask_akshare(question: str, max_rows: int = 50):
    import akshare as ak

    # 1) Specialized handlers
    if ("60" in question) and any(k in question for k in ["差异", "比较", "对比"]):
        try:
            return _compare_two_stocks_60d(question)
        except Exception as e:
            return {"ok": False, "mode": "ask", "backend": "akshare", "question": question, "error": str(e), "hint": error_hint(str(e))}

    if "雪球" in question and any(k in question for k in ["热股", "排行榜", "榜"]):
        try:
            return _xq_hot_rank_5d(question)
        except Exception as e:
            return {"ok": False, "mode": "ask", "backend": "akshare", "question": question, "error": str(e), "hint": error_hint(str(e))}

    # 2) Multi-intent combo query detection
    ticker = _extract_ticker_from_text(question)
    combo_signals = any(s in question for s in ["+", "以及", "还有", "同时", "加上", "并且"])
    if ticker and _detect_market(ticker) in ("a", "hk") and not combo_signals and \
            any(s in question for s in ("当前股价", "实时行情", "即时报价", "最新股价")) and \
            not any(s in question for s in ("历史", "比较", "对比", "走势", "财务", "资金", "估值", "市盈率")):
        return _fallback_quote(ticker, market=_detect_market(ticker))
    multi_intents = _match_multi_intents(question, ticker=ticker) if combo_signals else []
    if len(multi_intents) >= 2:
        results = []
        for entry, score in multi_intents:
            if not _is_market_compatible(entry, ticker):
                continue
            if entry.get("needs_ticker") and not ticker:
                continue
            try:
                df, meta = _run_registry_entry(entry, question, ticker)
                rows = _df_to_records(df, limit=max_rows)
                cols = list(df.columns) if hasattr(df, "columns") else []
                results.append({
                    "module": entry["id"],
                    "function": meta["function"],
                    "source": meta["source"],
                    "fallback_used": meta.get("fallback_used", False),
                    "description": entry.get("description", meta["function"]),
                    "score": score,
                    "kwargs": meta.get("kwargs", {}),
                    "columns": cols,
                    "rows": rows,
                })
            except Exception:
                continue
        if len(results) >= 2:
            summaries = [f"[{r['module']}] {r['description']} ({len(r['rows'])}行)" for r in results]
            return {
                "ok": True, "mode": "combo", "backend": "akshare", "question": question,
                "summary": f"组合查询命中 {len(results)} 个意图：" + "；".join(summaries),
                "results": results,
            }

    # 3) Registry-based intent matching (try top candidates, market-aware)
    candidates = _match_multi_intents(question, min_score=3, max_intents=6, ticker=ticker)

    last_error = None
    failed_intents = []
    for entry, score in candidates:
        if not _is_market_compatible(entry, ticker):
            continue
        if entry.get("needs_ticker") and not ticker:
            continue
        try:
            df, meta = _run_registry_entry(entry, question, ticker)
            rows = _df_to_records(df, limit=max_rows)
            cols = list(df.columns) if hasattr(df, "columns") else []

            plan = {"chosen_function": meta["function"], "kwargs": meta.get("kwargs", {}),
                    "module": entry["id"], "score": score, "source": meta["source"]}
            summary = f"[{entry['id']}] {entry.get('description', meta['function'])} — 返回 {len(rows)} 行"
            if meta.get("fallback_used"):
                plan["primary_function"] = entry["function"]
                plan["fallback_used"] = True
                plan["attempt_order"] = meta.get("attempt_order")
                plan["source_errors"] = meta.get("source_errors")
                summary += f"（东财接口 {entry['function']} 不可用，改用 {meta['source']} 的 {meta['function']}）"
            if meta.get("note"):
                summary += f"｜{meta['note']}"

            return {
                "ok": True, "mode": "ask", "backend": "akshare", "question": question,
                "plan": plan,
                "summary": summary,
                "data": {"columns": cols, "rows": rows, "csv": _rows_to_csv(cols, rows)},
            }
        except Exception as e:
            last_error = str(e)
            failed_intents.append({"module": entry["id"], "function": entry["function"],
                                   "error": str(e)[:240]})
            continue  # try next candidate

    if ticker and _detect_market(ticker) in ("hk", "us"):
        return {"ok": False, "mode": "ask", "backend": "akshare", "question": question,
                "ticker": ticker, "market": _detect_market(ticker),
                "error": "目标市场的已登记接口没有返回所需数据；未改查其他市场的同号股票。",
                "failed_intents": failed_intents,
                "hint": "明确所需指标，或按统一数据目录选择该市场的官方公告/财务接口。"}

    # 4) Generic fallback router.
    # It picks a function by name similarity, so it can answer a 板块 question
    # with 分红 data. Carry the matched intents' failures into the payload so a
    # degraded answer is never mistaken for the intended one.
    try:
        result = _route_via_score(question, max_rows)
        if failed_intents:
            result["degraded"] = True
            result["failed_intents"] = failed_intents
            result["summary"] = (f"⚠️ 命中的 intent 全部取数失败（{failed_intents[0]['module']}: "
                                 f"{failed_intents[0]['error'][:80]}），以下为通用路由的近似结果，"
                                 f"未必回答了原问题｜" + str(result.get("summary", "")))
        return result
    except Exception as e:
        return {"ok": False, "mode": "ask", "backend": "akshare", "question": question,
                "error": str(e), "failed_intents": failed_intents or None,
                "hint": error_hint(str(e))}


# ---------------------------------------------------------------------------
# Legacy / financials
# ---------------------------------------------------------------------------
def run_legacy(python_bin: str, legacy_script: str, argv_for_legacy, timeout: float | None = None):
    cmd = [python_bin, legacy_script] + argv_for_legacy
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "legacy script failed").strip())
    out = p.stdout.strip()
    if not out:
        raise RuntimeError("legacy script returned empty output")
    try:
        return json.loads(out)
    except Exception:
        raise RuntimeError(f"legacy script returned non-json: {out[:300]}")


def _resolve_legacy_timeout(args) -> float | None:
    deadline_at = getattr(args, "deadline_at", None)
    if deadline_at is not None and math.isfinite(float(deadline_at)) and float(deadline_at) > 0:
        rem_s = max(0.0, float(deadline_at) - time.time())
        return rem_s + 3.5
    deadline_s = getattr(args, "deadline_s", None)
    if deadline_s is not None and math.isfinite(float(deadline_s)) and float(deadline_s) > 0:
        return float(deadline_s) + 3.5
    return None


def build_financials_from_akshare(args):
    import akshare as ak

    def _latest(df, item):
        if df is None or df.empty:
            return None, None
        data = df[df["STD_ITEM_NAME"].astype(str) == item]
        if data.empty:
            return None, None
        latest = data["REPORT_DATE"].max()
        row = data[data["REPORT_DATE"] == latest].iloc[0]
        return (float(row["AMOUNT"]) if row.get("AMOUNT") is not None else None), str(latest)[:10]

    ticker = str(args.ticker).strip().zfill(5)
    df = ak.stock_financial_hk_report_em(stock=ticker, symbol="利润表", indicator="年度")
    if df is None or df.empty:
        raise RuntimeError(f"No HK income data for {ticker}")

    revenue, d1 = _latest(df, "营业额")
    gross, d2 = _latest(df, "毛利")
    op, d3 = _latest(df, "经营溢利")
    pretax, d4 = _latest(df, "除税前溢利")
    tax, d5 = _latest(df, "税项")
    net, d6 = _latest(df, "股东应占溢利")
    if net is None:
        net, d6 = _latest(df, "除税后溢利")
    report_date = next((d for d in [d1, d2, d3, d4, d5, d6] if d), dt.date.today().strftime("%Y-%m-%d"))
    etr = abs(tax) / abs(pretax) if pretax and tax and pretax != 0 else None

    return {
        "ok": True, "mode": "financials", "backend": "akshare",
        "announcement": {"ticker": ticker, "date": report_date, "title": "AkShare港股利润表(年度)", "source": "akshare"},
        "metrics": {"revenue": revenue, "net_profit": net, "gross_profit": gross, "operating_profit": op,
                     "assets": None, "liabilities": None, "operating_cashflow": None},
        "derived": {"pretax_profit": pretax, "tax_item": tax, "effective_tax_rate": etr},
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def run_self_test(python_bin: str, legacy_script: str):
    checks = []
    try:
        legacy = run_legacy(python_bin, legacy_script, ["--self-test", "--json", "--ticker", "00700"])
        checks.append({"name": "legacy.runtime", "ok": bool(legacy.get("ok"))})
    except Exception as e:
        checks.append({"name": "legacy.runtime", "ok": False, "error": str(e), "hint": error_hint(str(e))})

    try:
        import akshare as ak
        checks.append({"name": "akshare.import", "ok": True})
    except Exception as e:
        checks.append({"name": "akshare.import", "ok": False, "error": str(e), "hint": error_hint(str(e))})

    try:
        registry = _load_registry()
        checks.append({"name": "registry.load", "ok": len(registry) > 0, "count": len(registry)})
    except Exception as e:
        checks.append({"name": "registry.load", "ok": False, "error": str(e)})

    try:
        cache = _load_name_cache()
        checks.append({"name": "name_cache", "ok": len(cache) >= 5, "count": len(cache)})
    except Exception as e:
        checks.append({"name": "name_cache", "ok": False, "error": str(e)})

    return {"ok": all(c.get("ok") for c in checks), "checks": checks}


def list_capabilities_akshare():
    import akshare as ak
    out = []
    for n in sorted([x for x in dir(ak) if x.startswith("stock_") and callable(getattr(ak, x))]):
        f = getattr(ak, n)
        try:
            sig = str(inspect.signature(f))
        except Exception:
            sig = "()"
        doc = (inspect.getdoc(f) or "").split("\n")[0]
        out.append({"function": n, "signature": sig, "doc": doc})
    return {"ok": True, "backend": "akshare", "count": len(out), "capabilities": out}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="cn-hk-findata backend adapter v2 (registry)")
    parser.add_argument("--backend", choices=["legacy", "akshare", "financemcp"], default="legacy")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ticker", type=str)
    parser.add_argument("--ask", type=str, help="Natural language question")
    parser.add_argument("--max-rows", type=int, default=50)
    parser.add_argument("--list-capabilities", action="store_true")
    parser.add_argument("--source-health", action="store_true", help="Show data source health status")
    parser.add_argument("--reset-source", type=str, default=None, metavar="NAME",
                        help="Reset health tracking for a source (or 'all')")
    parser.add_argument("--fallback-quote", type=str, default=None, metavar="TICKER",
                        help="Fetch quote with multi-source fallback")
    parser.add_argument("--fallback-hist", type=str, default=None, metavar="TICKER",
                        help="Fetch history with multi-source fallback")
    parser.add_argument("--market", type=str, default=None, choices=["a", "hk", "us"],
                        help="Market hint for fallback commands")
    parser.add_argument("--days", type=int, default=120, help="History lookback days")
    # passthrough flags for legacy compat
    for flag in ["--limit", "--max-pages", "--start-page", "--date-from", "--date-to", "--max-chars", "--window", "--max-matches",
                 "--facts-limit", "--report-index", "--read-report-index", "--extract-financials-index"]:
        parser.add_argument(flag, type=int if "index" in flag or "limit" in flag or "page" in flag or "chars" in flag or "window" in flag or "matches" in flag else str, default=None)
    parser.add_argument("--deadline-at", type=float, default=None)
    parser.add_argument("--deadline-s", type=float, default=None)
    for flag in ["--search-text", "--facts-metric", "--report-keyword", "--read-report-keyword",
                 "--extract-financials-keyword", "--ocr-fallback"]:
        parser.add_argument(flag, type=str, default=None)
    for flag in ["--list-filings", "--financials", "--extract-financials-latest"]:
        parser.add_argument(flag, action="store_true")

    args = parser.parse_args()
    legacy_script = str(SKILL_DIR / "fetch_announcements.py")
    python_bin = sys.executable

    try:
        if args.self_test:
            result = run_self_test(python_bin, legacy_script)
            # Add fallback module check
            result["checks"].append({
                "name": "fallback_module",
                "ok": HAS_FALLBACK,
                "hint": "data_fallback.py not found" if not HAS_FALLBACK else None,
            })
            result["ok"] = all(c.get("ok") for c in result["checks"])
            emit(result)
            return
        if args.source_health:
            if HAS_FALLBACK:
                emit({"ok": True, "sources": get_source_health()})
            else:
                emit({"ok": False, "error": "Fallback module not available"})
            return
        if args.reset_source is not None:
            if HAS_FALLBACK:
                src = None if args.reset_source == "all" else args.reset_source
                reset_source_health(src)
                emit({"ok": True, "message": f"Reset health for: {args.reset_source}"})
            else:
                emit({"ok": False, "error": "Fallback module not available"})
            return
        if args.fallback_quote:
            result = _fallback_quote(args.fallback_quote, market=args.market)
            emit(result)
            return
        if args.fallback_hist:
            result = _fallback_hist(args.fallback_hist, days=args.days, market=args.market)
            emit(result)
            return
        if args.backend == "legacy":
            argv = [a for a in sys.argv[1:] if a not in ("--backend", "legacy", "akshare", "financemcp")]
            payload = run_legacy(python_bin, legacy_script, argv, timeout=_resolve_legacy_timeout(args))
            if isinstance(payload, dict):
                payload.setdefault("backend", "legacy")
            emit(payload)
            return
        if args.list_capabilities:
            emit(list_capabilities_akshare())
            return
        if args.ask:
            if args.backend != "akshare":
                raise RuntimeError("--ask only supports backend=akshare")
            emit(run_ask_akshare(args.ask, max_rows=args.max_rows))
            return
        if not args.ticker:
            raise RuntimeError("--ticker is required")
        if args.financials or args.extract_financials_latest:
            if args.backend == "akshare":
                emit(build_financials_from_akshare(args))
                return
        # fallback
        argv = [a for a in sys.argv[1:] if a not in ("--backend", "legacy", "akshare", "financemcp")]
        payload = run_legacy(python_bin, legacy_script, argv, timeout=_resolve_legacy_timeout(args))
        if isinstance(payload, dict):
            payload["backend"] = "legacy-fallback"
        emit(payload)
    except Exception as e:
        emit({"ok": False, "error": str(e), "hint": error_hint(str(e))})


if __name__ == "__main__":
    main()
