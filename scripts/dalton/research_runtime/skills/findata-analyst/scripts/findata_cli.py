#!/usr/bin/env python3
"""findata-analyst CLI v2.1 — SEC EDGAR data with NL routing, caching, yfinance fallback, and structured output."""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from importlib.metadata import version
from pathlib import Path
from datetime import date, datetime, timezone
from math import isfinite
from typing import Any
from point_in_time import parse_as_of, dated_filings, facts_at_date, select_fact_rows
from public_macro import register_commands

import pandas as pd

# yfinance fallback availability
try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

# ---------------------------------------------------------------------------
# Edgar setup
# ---------------------------------------------------------------------------
from edgar import Company, configure_http, set_identity

configure_http(use_system_certs=True)

if "EDGAR_IDENTITY" in os.environ and os.environ["EDGAR_IDENTITY"].strip():
    set_identity(os.environ["EDGAR_IDENTITY"].strip())
else:
    print("WARNING: EDGAR_IDENTITY not set. SEC requires a valid User-Agent.", file=sys.stderr)
    set_identity("DaltonResearch <research@dalton.local>")

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 2000)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
def _resolve_cache_dir() -> Path:
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return home_root / "state" / "findata-analyst" / "cache"


CACHE_DIR = _resolve_cache_dir()
CACHE_MAX_AGE_S = 86400  # 24h
EDGAR_VERSION = version("edgartools")


class _SerializationError(RuntimeError):
    """Raised when internal result data cannot be serialized to JSON."""


def _load_sec_issuer_map_data() -> dict[str, Any]:
    """Load the canonical SEC issuer map JSON from ``plugins/dalton_sec/`` without importing ``src``."""
    here = Path(__file__).resolve()
    candidates = (
        here.with_name("sec_issuer_map.json"),
        *(parent / "plugins" / "dalton_sec" / "sec_issuer_map.json" for parent in here.parents),
    )
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


_SEC_ISSUER_MAP_DATA: dict[str, Any] = _load_sec_issuer_map_data()
_DUAL_LISTED_SEC_ISSUER_MAP: dict[str, str] = {
    str(k).upper(): str(v).upper()
    for k, v in (_SEC_ISSUER_MAP_DATA.get("dual_listed_sec_issuer_map") or {}).items()
}
_CONFIRMED_NON_SEC_ISSUERS: frozenset[str] = frozenset(
    str(item).upper() for item in (_SEC_ISSUER_MAP_DATA.get("confirmed_non_sec_issuers") or ())
)
_NON_SEC_MARKET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(str(pat), re.IGNORECASE), str(label))
    for pat, label in (_SEC_ISSUER_MAP_DATA.get("non_sec_market_patterns") or ())
)
_SEC_A_SHARE_RE = re.compile(r"^(?:\d{6}\.(?:SH|SZ|SS|BJ)|(?:SH|SZ|BJ)\d{6})$", re.IGNORECASE)
_SEC_HK_RE = re.compile(r"^(?:HK:)?0*(\d{1,5})(?:\.HK)?$", re.IGNORECASE)


def _sec_hk_candidates(raw: str) -> tuple[str, ...]:
    m_hk = _SEC_HK_RE.fullmatch(raw)
    if not m_hk:
        return ()
    if not (
        raw.endswith(".HK")
        or raw.startswith("HK:")
        or (raw.isdigit() and len(raw) == 5 and raw.startswith("0"))
    ):
        return ()
    num = int(m_hk.group(1))
    return (f"{num:04d}.HK", f"{num}.HK", f"{num:05d}", f"HK:{num:04d}", f"HK:{num:05d}")


def _detect_non_sec_ticker(ticker: str | None) -> str | None:
    raw = str(ticker or "").strip()
    if not raw:
        return None
    for pat, label in _NON_SEC_MARKET_PATTERNS:
        if pat.fullmatch(raw):
            return label
    return None


def _resolve_sec_issuer_ticker(ticker: str | None) -> str | None:
    raw = str(ticker or "").strip().upper()
    if not raw:
        return None
    if raw in _DUAL_LISTED_SEC_ISSUER_MAP:
        return _DUAL_LISTED_SEC_ISSUER_MAP[raw]
    for cand in _sec_hk_candidates(raw):
        if cand in _DUAL_LISTED_SEC_ISSUER_MAP:
            return _DUAL_LISTED_SEC_ISSUER_MAP[cand]
    return None


def _is_confirmed_non_sec_ticker(ticker: str | None) -> bool:
    raw = str(ticker or "").strip().upper()
    if not raw:
        return False
    if _SEC_A_SHARE_RE.fullmatch(raw):
        return True
    if raw in _CONFIRMED_NON_SEC_ISSUERS:
        return True
    for cand in _sec_hk_candidates(raw):
        if cand in _CONFIRMED_NON_SEC_ISSUERS:
            return True
    if raw.startswith(("KR:", "KS:", "KQ:")):
        code = raw.split(":", 1)[1].strip()
        if f"{code}.KS" in _CONFIRMED_NON_SEC_ISSUERS or f"{code}.KQ" in _CONFIRMED_NON_SEC_ISSUERS:
            return True
    return False


def _classify_sec_ticker(ticker: str | None) -> tuple[str, str | None, str | None]:
    raw = str(ticker or "").strip()
    if not raw:
        return ("direct", raw, None)
    mapped = _resolve_sec_issuer_ticker(raw)
    market_label = _detect_non_sec_ticker(raw)
    if mapped is not None:
        return ("mapped", mapped, market_label)
    if market_label is not None:
        if _is_confirmed_non_sec_ticker(raw):
            return ("confirmed_non_sec", None, market_label)
        return ("unmapped_non_us", None, market_label)
    return ("direct", raw, None)


def _iso_date_key(key: Any) -> str:
    """Convert Timestamp / datetime / date (or any non-JSON key) to an ISO date string."""
    if isinstance(key, pd.Timestamp):
        if pd.isna(key):
            return "NaT"
        if key.hour == 0 and key.minute == 0 and key.second == 0 and key.microsecond == 0:
            return key.date().isoformat()
        return key.isoformat()
    if isinstance(key, datetime):
        if key.hour == 0 and key.minute == 0 and key.second == 0 and key.microsecond == 0:
            return key.date().isoformat()
        return key.isoformat()
    if isinstance(key, date):
        return key.isoformat()
    if isinstance(key, (str, int, float, bool)) or key is None:
        return key  # type: ignore[return-value]
    return str(key)


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively convert date/Timestamp dict keys (and NaN/NaT values) before JSON serialization."""
    if isinstance(obj, dict):
        return {_iso_date_key(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, tuple):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, pd.Timestamp):
        if pd.isna(obj):
            return None
        if obj.hour == 0 and obj.minute == 0 and obj.second == 0 and obj.microsecond == 0:
            return obj.date().isoformat()
        return obj.isoformat()
    if isinstance(obj, datetime):
        if obj.hour == 0 and obj.minute == 0 and obj.second == 0 and obj.microsecond == 0:
            return obj.date().isoformat()
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, float) and not isfinite(obj):
        return None
    try:
        if obj is pd.NA or obj is pd.NaT:
            return None
    except Exception:
        pass
    return obj


def _dataframe_to_json_dict(df: pd.DataFrame) -> dict[str, Any]:
    """Convert a financial statement DataFrame (columns/index may be Timestamp) to JSON-safe dict."""
    safe_df = df.copy()
    safe_df.columns = [_iso_date_key(c) for c in safe_df.columns]
    safe_df.index = [_iso_date_key(i) for i in safe_df.index]
    return _sanitize_for_json(safe_df.to_dict())


def _dataframe_to_json_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a financial statement DataFrame to JSON-safe list of records with ISO date column keys."""
    safe_df = df.copy()
    safe_df.columns = [_iso_date_key(c) for c in safe_df.columns]
    return _sanitize_for_json(safe_df.to_dict(orient="records"))


def _is_serialization_exception(err: BaseException) -> bool:
    if isinstance(err, _SerializationError):
        return True
    msg = str(err).lower()
    return (
        "keys must be str, int, float, bool or none" in msg
        or "is not json serializable" in msg
        or "circular reference detected" in msg
    )


def _emit(payload, as_json=False, as_csv=False):
    safe_payload = _sanitize_for_json(payload)
    try:
        if as_json:
            print(json.dumps(safe_payload, ensure_ascii=False, default=str))
        elif as_csv and isinstance(safe_payload, dict) and "rows" in safe_payload:
            rows = safe_payload["rows"]
            if rows:
                cols = [str(c) for c in rows[0].keys()]
                print(",".join(cols))
                for r in rows:
                    vals = [str(r.get(c, "")).replace('"', '""') for c in cols]
                    print(",".join(f'"{v}"' if "," in v else v for v in vals))
        else:
            print(json.dumps(safe_payload, ensure_ascii=False, default=str, indent=2))
    except (TypeError, ValueError) as exc:
        raise _SerializationError(f"财务数据内部 JSON 序列化失败: {exc}") from exc


def _error_hint(err):
    if _is_serialization_exception(err):
        return "这是服务端内部数据序列化错误，不是调用参数错误；请稍后重试或改用备用数据源。"
    msg = str(err).lower()
    if "ssl" in msg or "tls" in msg or "unexpected_eof" in msg:
        return "Network/TLS error. Check proxy and access to https://data.sec.gov."
    if "edgar_identity" in msg or "user-agent" in msg:
        return "Set EDGAR_IDENTITY: export EDGAR_IDENTITY='Name <email>'."
    if "not found" in msg and "concept" in msg:
        return "Concept not found. Use search-concepts or smart-facts to find exact concept id."
    if "rate" in msg or "429" in msg or "too many" in msg:
        return "SEC rate limit hit. Wait 10s and retry."
    return "Check ticker/form/concept arguments and retry."


def _error_json(err):
    if _is_serialization_exception(err):
        return {
            "ok": False,
            "status": "internal_error",
            "error_code": "internal_serialization_error",
            "error_category": "内部错误",
            "error": f"内部序列化失败: {err}",
            "hint": _error_hint(err),
        }
    return {
        "ok": False,
        "status": "error",
        "error_code": "cli_error",
        "error": str(err),
        "hint": _error_hint(err),
    }


# ---------------------------------------------------------------------------
# Retry mechanism
# ---------------------------------------------------------------------------
def _retry(fn, max_retries=2, delay=2.0):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_transient = any(k in err_str for k in [
                "timeout", "timed out", "connection", "reset", "429",
                "rate", "ssl", "eof", "too many",
            ])
            if not is_transient or attempt >= max_retries:
                raise
            time.sleep(delay * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _cache_key(prefix, *args):
    raw = f"edgartools-{EDGAR_VERSION}:{prefix}:{'|'.join(str(a) for a in args)}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _cache_get(key):
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < CACHE_MAX_AGE_S:
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        data["cache"] = {"hit": True, "age_seconds": round(age),
                                         "stored_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
                    return data
            except Exception:
                pass
    return None


def _cache_set(key, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if isinstance(data, dict):
            data.setdefault("retrieved_at", datetime.now(timezone.utc).isoformat())
            data["cache"] = {"hit": False}
        with open(CACHE_DIR / f"{key}.json", "w") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# NL Router
# ---------------------------------------------------------------------------
_NL_PATTERNS = [
    # Transcript / earnings call
    (r"(transcript|earnings call|conference call|电话会|业绩会|管理层讲话|earnings.*call)", "transcript"),
    # 8-K / events
    (r"(8-K|8K|recent event|corporate event|earnings release|material event)", "eight-k"),
    # Insider / Form 4
    (r"(insider|form\s*4|executive buy|executive sell|insider trad|管理层买卖|高管交易)", "insider"),
    # Ratios
    (r"(gross margin|net margin|operating margin|profit margin|roe|roa|毛利率|净利率|利润率)", "ratios"),
    # Compare
    (r"(compare|vs\.?|versus|对比|哪个.*高|哪个.*好|两家)", "compare"),
    # Financial statements
    (r"(income statement|balance sheet|cash flow|financial statement|报表|利润表|资产负债)", "financials"),
    # Business model / qualitative
    (r"(business model|商业模式|what does.*do|Item\s*1\b)", "read-item"),
    # Depreciation / specific policy search
    (r"(depreciation|useful life|accounting polic|lease term|折旧|会计政策)", "search-text"),
    # XBRL concept search
    (r"(segment|revenue by|breakdown|xbrl|concept)", "smart-facts"),
    # Facts / historical metrics
    (r"(historical|trend|过去.*年|time series|growth|增速|cagr)", "smart-facts"),
    # Default: smart-facts for metric questions
    (r"(revenue|earnings|net income|gross profit|ebitda|capex|收入|利润)", "smart-facts"),
]


def _route_nl(question: str) -> tuple[str, dict]:
    """Return (command, extra_kwargs) based on NL question."""
    q_lower = question.lower()

    # Extract ticker(s)
    tickers = re.findall(r"\b([A-Z]{1,5})\b", question)
    # Filter out common English words
    stop_words = {"THE", "AND", "FOR", "ARE", "NOT", "BUT", "HAS", "HAD", "WAS", "IS", "IT", "OR", "AN", "DO", "IF",
                  "VS", "A", "IN", "ON", "AT", "TO", "BY", "OF", "UP", "NO", "SO", "AS", "BE", "AM", "HOW", "WHY",
                  "GPM", "NPM", "ROE", "ROA", "EPS", "PE", "PB", "YOY", "CAGR", "MD", "SEC", "XBRL", "IPO",
                  "WHAT", "WHEN", "WHERE", "WHO", "WHICH", "ITEM", "FROM"}
    tickers = [t for t in tickers if t not in stop_words and len(t) >= 2]

    for pattern, cmd in _NL_PATTERNS:
        if re.search(pattern, q_lower if pattern.startswith("(") and any(c.islower() for c in pattern) else question, re.IGNORECASE):
            return cmd, {"tickers": tickers, "question": question}

    # Fallback: if we have tickers, try smart-facts
    if tickers:
        return "smart-facts", {"tickers": tickers, "question": question}

    return "unknown", {"tickers": tickers, "question": question}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# yfinance fallback helpers
# ---------------------------------------------------------------------------
def _yf_financials(ticker: str, form: str = "10-K") -> dict:
    """Fetch financials from yfinance as fallback."""
    if not HAS_YFINANCE:
        return {"ok": False, "error": "yfinance not installed", "hint": "pip install yfinance"}
    
    t = yf.Ticker(ticker)
    period = "annual" if "K" in form or "20-F" in form else "quarterly"
    
    statements = {}
    try:
        if period == "annual":
            inc = t.income_stmt
            bal = t.balance_sheet
            cf = t.cashflow
        else:
            inc = t.quarterly_income_stmt
            bal = t.quarterly_balance_sheet
            cf = t.quarterly_cashflow
        
        if inc is not None and not inc.empty:
            statements["income"] = _dataframe_to_json_dict(inc)
        if bal is not None and not bal.empty:
            statements["balance"] = _dataframe_to_json_dict(bal)
        if cf is not None and not cf.empty:
            statements["cash"] = _dataframe_to_json_dict(cf)
    except Exception as e:
        return {"ok": False, "error": f"yfinance fetch failed: {e}"}
    
    if not statements:
        return {"ok": False, "error": f"yfinance: no financial data for {ticker}"}
    
    return {
        "ok": True, "ticker": ticker, "form": form, "source": "yfinance",
        "fallback_used": True,
        "rows": [{"form": form, "filing_date": "yfinance", "statements": statements}],
    }


def _yf_ratios(ticker: str) -> dict:
    """Calculate ratios from yfinance as fallback."""
    if not HAS_YFINANCE:
        return {"ok": False, "error": "yfinance not installed"}
    
    t = yf.Ticker(ticker)
    info = t.info or {}
    
    ratios = {
        "filing_date": "yfinance-latest",
        "source": "yfinance",
    }
    
    # Extract ratios from yfinance info
    mappings = {
        "gross_margin": ("grossMargins", 100),
        "operating_margin": ("operatingMargins", 100),
        "net_margin": ("profitMargins", 100),
        "roe": ("returnOnEquity", 100),
        "roa": ("returnOnAssets", 100),
    }
    for key, (yf_key, mult) in mappings.items():
        val = info.get(yf_key)
        if val is not None:
            ratios[key] = round(val * mult, 2)
    
    raw_mappings = {
        "revenue": "totalRevenue",
        "gross_profit": "grossProfits",
        "net_income": "netIncomeToCommon",
        "total_assets": "totalAssets",
        "total_equity": "bookValue",  # per share, not total
    }
    raw = {}
    for key, yf_key in raw_mappings.items():
        raw[key] = info.get(yf_key)
    ratios["raw"] = raw
    
    return {"ok": True, "ticker": ticker, "source": "yfinance", "fallback_used": True, "ratios": [ratios]}


def _get_filings(company, args, form=None):
    filings = _retry(lambda: company.get_filings(form=form)) if form else _retry(lambda: company.get_filings())
    return dated_filings(filings, getattr(args, "as_of", None))


def _facts_frame(facts, args):
    return facts_at_date(facts.to_dataframe(include_metadata=True), getattr(args, "as_of", None))


def cmd_financials(args):
    c = _retry(lambda: Company(args.ticker))
    filings = _get_filings(c, args, args.form)
    if len(filings) == 0:
        # Fallback to yfinance if SEC has no filings
        if HAS_YFINANCE and not getattr(args, "as_of", None):
            yf_result = _yf_financials(args.ticker, args.form)
            if yf_result.get("ok"):
                return yf_result
        return {"ok": False, "error": f"No {args.form} filings found for {args.ticker}.",
                "hint": "yfinance fallback also unavailable" if not HAS_YFINANCE else "yfinance fallback also failed"}

    rows = []
    for f in filings.head(args.limit):
        try:
            obj = f.obj()
            row = {
                "form": str(f.form), "filing_date": str(f.filing_date),
                "report_date": str(getattr(f, "report_date", None)),
                "accession_no": str(getattr(f, "accession_no", None)),
                "statements": {},
            }
            if hasattr(obj, "financials") and obj.financials:
                stmts = {"income": "income_statement", "balance": "balance_sheet", "cash": "cash_flow_statement"}
                for key, attr in stmts.items():
                    if args.statement not in (key, "all"):
                        continue
                    stmt = getattr(obj.financials, attr, None)
                    if callable(stmt):
                        stmt = stmt()
                    if stmt is not None and hasattr(stmt, "to_dataframe"):
                        row["statements"][key] = _dataframe_to_json_records(stmt.to_dataframe())
                    else:
                        row["statements"][key] = None
            rows.append(row)
        except Exception as e:
            rows.append({"form": str(f.form), "filing_date": str(f.filing_date), "error": str(e)})
            continue
    
    # If all rows errored, try yfinance fallback
    if rows and all("error" in r for r in rows) and HAS_YFINANCE and not getattr(args, "as_of", None):
        yf_result = _yf_financials(args.ticker, args.form)
        if yf_result.get("ok"):
            yf_result["edgar_errors"] = [r.get("error") for r in rows]
            return yf_result
    
    return {"ok": True, "ticker": args.ticker, "form": args.form, "rows": rows, "source": "sec_edgar"}


def cmd_search_concepts(args):
    ck = _cache_key("concepts_v3", args.ticker, args.keyword, getattr(args, "as_of", None))
    cached = _cache_get(ck)
    if cached:
        return cached

    c = _retry(lambda: Company(args.ticker))
    facts = _retry(lambda: c.get_facts())
    if not facts:
        return {"ok": False, "error": f"No facts found for {args.ticker}."}

    df = _facts_frame(facts, args)
    matches = df[
        df["concept"].str.contains(args.keyword, case=False, na=False)
        | df["label"].str.contains(args.keyword, case=False, na=False)
    ]
    if matches.empty:
        return {"ok": False, "error": f"No concepts matching '{args.keyword}' for {args.ticker}."}

    unique = matches[["concept", "label"]].drop_duplicates().to_dict(orient="records")
    result = {"ok": True, "ticker": args.ticker, "keyword": args.keyword, "count": len(unique), "concepts": unique}
    _cache_set(ck, result)
    return result


def cmd_facts(args):
    ck = _cache_key("facts_v3", args.ticker, args.concept, args.form, args.limit, getattr(args, "as_of", None))
    cached = _cache_get(ck)
    if cached:
        return cached

    c = _retry(lambda: Company(args.ticker))
    facts = _retry(lambda: c.get_facts())
    if not facts:
        return {"ok": False, "error": "No facts found."}

    df = _facts_frame(facts, args)
    concept_df = df[df["concept"] == args.concept].copy()
    if concept_df.empty:
        return {"ok": False, "error": f"Concept '{args.concept}' not found. Use search-concepts first."}

    selected_rows = select_fact_rows(concept_df, args.form, args.limit, getattr(args, "as_of", None))
    if not selected_rows:
        return {"ok": True, "status": "empty", "ticker": args.ticker, "rows": [],
                "hint": "No facts match this form and filing-date cutoff."}

    result = {"ok": True, "ticker": args.ticker, "concept": args.concept, "form": args.form,
              "rows": selected_rows}
    _cache_set(ck, result)
    return result


def cmd_smart_facts(args):
    """Auto concept search + fetch: one-step from keyword to time series."""
    keyword = getattr(args, "keyword", None) or getattr(args, "question", "") or ""
    if not keyword:
        return {"ok": False, "error": "Need --keyword or a question to search concepts."}

    # Step 1: search concepts
    c = _retry(lambda: Company(args.ticker))
    facts = _retry(lambda: c.get_facts())
    if not facts:
        return {"ok": False, "error": f"No facts found for {args.ticker}."}

    df = _facts_frame(facts, args)
    if getattr(args, "as_of", None) and args.form:
        df = df[df["form_type"].isin([args.form, args.form + "/A"])]
    matches = df[
        df["concept"].str.contains(keyword, case=False, na=False)
        | df["label"].str.contains(keyword, case=False, na=False)
    ]
    if matches.empty:
        return {"ok": False, "error": f"No concepts matching '{keyword}' for {args.ticker}.",
                "hint": "Try broader keywords (e.g., 'revenue' instead of 'total revenue')."}

    unique = matches[["concept", "label"]].drop_duplicates()

    # Step 2: pick best concept (prefer us-gaap, non-deprecated, most data)
    best_concept = None
    best_score = -1
    for _, row in unique.head(15).iterrows():
        concept = row["concept"]
        label = str(row.get("label", ""))
        count = len(df[df["concept"] == concept])
        score = count
        # Prefer us-gaap concepts
        if concept.startswith("us-gaap:"):
            score += 100
        # Penalize deprecated concepts
        if "deprecated" in label.lower():
            score -= 200
        # Prefer concepts whose label closely matches the keyword
        if keyword.lower() in label.lower():
            score += 50
        if score > best_score:
            best_score = score
            best_concept = concept

    if not best_concept:
        return {"ok": False, "error": "Could not determine best concept."}

    # Step 3: fetch time series for best concept
    concept_df = df[df["concept"] == best_concept].copy()
    selected_rows = select_fact_rows(concept_df, args.form, args.limit, getattr(args, "as_of", None))

    return {
        "ok": True, "status": "ok" if selected_rows else "empty", "ticker": args.ticker, "keyword": keyword,
        "chosen_concept": best_concept,
        "all_concepts": unique.head(5).to_dict(orient="records"),
        "form": args.form,
        "rows": selected_rows,
    }


def cmd_compare(args):
    """Compare a metric across two tickers."""
    tickers = args.tickers.split(",") if hasattr(args, "tickers") and args.tickers else []
    if len(tickers) < 2:
        return {"ok": False, "error": "Need two tickers separated by comma (e.g., --tickers NVDA,AVGO)."}

    keyword = args.keyword
    results = {}
    for ticker in tickers[:2]:
        ticker = ticker.strip().upper()
        try:
            # Reuse smart-facts logic
            ns = argparse.Namespace(ticker=ticker, keyword=keyword, form=args.form, limit=args.limit, question="", as_of=getattr(args, "as_of", None))
            r = cmd_smart_facts(ns)
            results[ticker] = r
        except Exception as e:
            results[ticker] = {"ok": False, "error": str(e)}

    return {"ok": True, "mode": "compare", "keyword": keyword, "form": args.form, "results": results}


def cmd_ratios(args):
    """Calculate common financial ratios from latest filing, with yfinance fallback."""
    try:
        c = _retry(lambda: Company(args.ticker))
        filings = _get_filings(c, args, args.form)
    except Exception as e:
        # SEC failed entirely — try yfinance
        if HAS_YFINANCE and not getattr(args, "as_of", None):
            yf_result = _yf_ratios(args.ticker)
            if yf_result.get("ok"):
                yf_result["edgar_error"] = str(e)
                return yf_result
        return {"ok": False, "error": str(e), "hint": _error_hint(str(e))}
    
    if len(filings) == 0:
        # No filings — try yfinance
        if HAS_YFINANCE and not getattr(args, "as_of", None):
            yf_result = _yf_ratios(args.ticker)
            if yf_result.get("ok"):
                return yf_result
        return {"ok": False, "error": f"No {args.form} filings found for {args.ticker}."}

    ratios_list = []
    for f in filings.head(args.limit):
        try:
            obj = f.obj()
            if not hasattr(obj, "financials") or not obj.financials:
                continue

            income = getattr(obj.financials, "income_statement", None)
            if callable(income):
                income = income()
            balance = getattr(obj.financials, "balance_sheet", None)
            if callable(balance):
                balance = balance()

            def _get_val(stmt, *names):
                """Get latest period value by matching label (row-based, edgartools format)."""
                if stmt is None:
                    return None
                df_stmt = stmt.to_dataframe() if hasattr(stmt, "to_dataframe") else stmt
                if not isinstance(df_stmt, pd.DataFrame) or df_stmt.empty:
                    return None
                # Find the latest date column (format: YYYY-MM-DD)
                date_cols = [c for c in df_stmt.columns if re.match(r"\d{4}-\d{2}-\d{2}", str(c))]
                if not date_cols:
                    return None
                latest_col = sorted(date_cols, reverse=True)[0]
                # Match by label column (row-based)
                label_col = "label" if "label" in df_stmt.columns else None
                if not label_col:
                    return None
                # Filter out dimension breakdowns (keep consolidated only)
                if "dimension" in df_stmt.columns:
                    consolidated = df_stmt[df_stmt["dimension"] == False]
                    if not consolidated.empty:
                        df_stmt = consolidated
                for name in names:
                    mask = df_stmt[label_col].str.contains(name, case=False, na=False)
                    matches = df_stmt[mask]
                    # Filter out rows with "liabilities" when searching for equity
                    if "equity" in name.lower() or "shareholders" in name.lower() or "stockholders" in name.lower():
                        exclude = matches[label_col].str.contains("liabilities", case=False, na=False)
                        matches = matches[~exclude]
                    if not matches.empty:
                        val = matches.iloc[0][latest_col]
                        if pd.notna(val):
                            return float(val)
                return None

            revenue = _get_val(income, "net sales", "revenue", "total revenue", "net revenue", "sales")
            gross_profit = _get_val(income, "gross margin", "gross profit")
            operating_income = _get_val(income, "operating income", "operating profit", "income from operations")
            net_income = _get_val(income, "net income")
            total_assets = _get_val(balance, "total assets")
            total_equity = _get_val(balance, "total shareholders", "total stockholders", "total equity")

            ratios = {
                "filing_date": str(f.filing_date),
                "report_date": str(getattr(f, "report_date", None)),
            }
            if revenue and revenue != 0:
                if gross_profit is not None:
                    ratios["gross_margin"] = round(gross_profit / revenue * 100, 2)
                if operating_income is not None:
                    ratios["operating_margin"] = round(operating_income / revenue * 100, 2)
                if net_income is not None:
                    ratios["net_margin"] = round(net_income / revenue * 100, 2)
            if net_income is not None:
                if total_assets and total_assets != 0:
                    ratios["roa"] = round(net_income / total_assets * 100, 2)
                if total_equity and total_equity != 0:
                    ratios["roe"] = round(net_income / total_equity * 100, 2)

            ratios["raw"] = {"revenue": revenue, "gross_profit": gross_profit,
                             "operating_income": operating_income, "net_income": net_income,
                             "total_assets": total_assets, "total_equity": total_equity}
            ratios_list.append(ratios)
        except Exception as e:
            ratios_list.append({"filing_date": str(f.filing_date), "error": str(e)})
            continue

    return {"ok": True, "ticker": args.ticker, "form": args.form, "ratios": ratios_list}


def cmd_list_filings(args):
    ticker_kind, mapped_sec, non_sec_market = _classify_sec_ticker(args.ticker)
    effective_ticker = mapped_sec or args.ticker
    if ticker_kind == "confirmed_non_sec":
        return {
            "ok": False,
            "status": "not_sec_filer",
            "error_code": "not_sec_filer",
            "sec_filer": False,
            "ticker": args.ticker,
            "count": 0,
            "rows": [],
            "error": f"该证券不在 SEC 申报：{args.ticker} 属于{non_sec_market}，无 SEC 身份（CIK），未请求 SEC。",
            "hint": "港股与 A 股请改用 cn-hk 路由；韩股等其他非美股请查当地交易所公告或内部研究库。",
        }
    if ticker_kind == "unmapped_non_us":
        return {
            "ok": False,
            "status": "sec_issuer_mapping_not_found",
            "error_code": "sec_issuer_mapping_not_found",
            "sec_filer": None,
            "ticker": args.ticker,
            "count": 0,
            "rows": [],
            "error": f"未找到 {args.ticker} 对应的美股/ADR 代码或 SEC CIK 映射（属于{non_sec_market}）；若该公司以 ADR/20-F 在 SEC 申报，请改用其美股代码或 10 位 CIK 查询。",
            "hint": "请提供该公司对应的美股 ADR 代码或 SEC CIK，或改用当地交易所/cn-hk 路由查询。",
        }
    c = _retry(lambda: Company(effective_ticker))
    filings = _get_filings(c, args, args.form)
    if len(filings) == 0:
        return {"ok": False, "error": f"No filings found for {args.ticker}."}

    df = filings.head(args.limit).to_pandas()
    if "report_date" not in df.columns and "reportDate" in df.columns:
        df["report_date"] = df["reportDate"]
    if "accession_no" not in df.columns and "accession_number" in df.columns:
        df["accession_no"] = df["accession_number"]

    desired = ["form", "filing_date", "report_date", "accession_no"]
    available = [c for c in desired if c in df.columns]
    rows = df[available].to_dict(orient="records") if available else df.to_dict(orient="records")
    return {"ok": True, "ticker": args.ticker, "count": len(rows), "rows": rows}


def cmd_eight_k(args):
    c = _retry(lambda: Company(args.ticker))
    filings = _get_filings(c, args, "8-K")
    if len(filings) == 0:
        return {"ok": False, "error": f"No 8-K filings found for {args.ticker}."}

    rows = []
    for f in filings.head(args.limit):
        row = {"filing_date": str(f.filing_date), "accession_no": str(getattr(f, "accession_no", ""))}
        try:
            obj = f.obj()
            row["items"] = str(getattr(obj, "items", "N/A"))
            text = ""
            if hasattr(obj, "text") and callable(obj.text):
                text = obj.text()
            elif hasattr(f, "text") and callable(f.text):
                text = f.text()
            else:
                text = str(obj)
            row["content_snippet"] = text[:3000]
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)
    return {"ok": True, "ticker": args.ticker, "count": len(rows), "rows": rows}


def cmd_insider(args):
    c = _retry(lambda: Company(args.ticker))
    filings = _get_filings(c, args, "4")
    if len(filings) == 0:
        return {"ok": False, "error": f"No Form 4 filings found for {args.ticker}."}

    rows = []
    for f in filings.head(args.limit):
        row = {"filing_date": str(f.filing_date), "accession_no": str(getattr(f, "accession_no", ""))}
        try:
            row["content"] = str(f.obj())[:3000]
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)
    return {"ok": True, "ticker": args.ticker, "count": len(rows), "rows": rows}


def cmd_search_text(args):
    c = _retry(lambda: Company(args.ticker))
    filings = _get_filings(c, args, args.form)
    if len(filings) == 0:
        return {"ok": False, "error": f"No {args.form} filings found for {args.ticker}."}

    results = []
    for f in filings.head(args.limit):
        filing_result = {"filing_date": str(f.filing_date), "form": str(f.form), "matches": []}
        try:
            text = ""
            if hasattr(f, "text") and callable(f.text):
                text = f.text()
            else:
                obj = f.obj()
                if hasattr(obj, "text") and callable(obj.text):
                    text = obj.text()
                else:
                    text = str(obj)

            keyword = args.keyword.lower()
            text_lower = text.lower()
            start = 0
            match_count = 0
            while True:
                idx = text_lower.find(keyword, start)
                if idx == -1:
                    break
                match_count += 1
                if match_count > args.max_matches:
                    break
                win_start = max(0, idx - args.window)
                win_end = min(len(text), idx + len(keyword) + args.window)
                snippet = " ".join(text[win_start:win_end].split())
                filing_result["matches"].append({"index": match_count, "snippet": snippet})
                start = idx + len(keyword)
            filing_result["total_matches"] = match_count
        except Exception as e:
            filing_result["error"] = str(e)
        results.append(filing_result)
    return {"ok": True, "ticker": args.ticker, "keyword": args.keyword, "filings": results}


def cmd_read_item(args):
    c = _retry(lambda: Company(args.ticker))
    filings = _get_filings(c, args, args.form).head(1)
    if len(filings) == 0:
        return {"ok": False, "error": f"No {args.form} filings found for {args.ticker}."}

    f = filings[0]
    result = {"filing_date": str(f.filing_date), "form": str(f.form), "item": args.item}
    try:
        obj = f.obj()
        if hasattr(obj, "__getitem__"):
            try:
                text = obj[args.item]
                if not text:
                    return {"ok": False, "error": f"{args.item} is empty.", **result}
                truncated = len(text) > args.max_chars
                result["text"] = text[:args.max_chars]
                result["truncated"] = truncated
                result["total_chars"] = len(text)
                return {"ok": True, "ticker": args.ticker, **result}
            except KeyError:
                available = str(getattr(obj, "items", "Unknown"))
                return {"ok": False, "error": f"Item '{args.item}' not found. Available: {available}", **result}
        else:
            return {"ok": False, "error": "Filing object does not support item extraction.", **result}
    except Exception as e:
        return {"ok": False, "error": str(e), **result}


def cmd_transcript(args):
    """Fetch earnings call transcript via roic-transcript skill."""
    import subprocess
    skill_path = Path(__file__).resolve().parent.parent.parent / "roic-transcript" / "run.sh"
    if not skill_path.exists():
        return {"ok": False, "error": "roic-transcript skill not installed. The 'transcript' command requires it.",
                "hint": "Install: git clone https://github.com/everflowinv/roic-transcript.git skills/roic-transcript"}

    cmd_parts = ["bash", str(skill_path), "fetch", "--ticker", args.ticker, "--json", "--quiet"]

    if getattr(args, "latest", False) or (not getattr(args, "year", None)):
        cmd_parts.append("--latest")
    else:
        if getattr(args, "year", None):
            cmd_parts.extend(["--year", str(args.year)])
        if getattr(args, "quarter", None):
            cmd_parts.extend(["--quarter", str(args.quarter)])

    if getattr(args, "speaker", None):
        cmd_parts.extend(["--speaker"] + args.speaker)
    if getattr(args, "section", None):
        cmd_parts.extend(["--section", args.section])
    if getattr(args, "last", None):
        cmd_parts.extend(["--last", str(args.last)])

    try:
        result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        else:
            err_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            return {"ok": False, "error": err_msg, "hint": "Check ticker and quarter availability."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Transcript fetch timed out.", "hint": "Try again or check network."}
    except Exception as e:
        return {"ok": False, "error": str(e), "hint": _error_hint(e)}


def cmd_ask(args):
    """NL router: parse question → route to appropriate command."""
    question = args.question
    cmd, ctx = _route_nl(question)
    tickers = ctx.get("tickers", [])

    if not tickers:
        return {"ok": False, "error": "Could not identify any ticker in your question.",
                "hint": "Include a stock ticker like AAPL, NVDA, MSFT in your question."}

    ticker = tickers[0]

    if cmd == "transcript":
        # Extract year/quarter from question if present
        year_match = re.search(r"(20\d{2})", question)
        q_match = re.search(r"[Qq](\d)", question)
        ns = argparse.Namespace(
            ticker=ticker, latest=not (year_match and q_match),
            year=int(year_match.group(1)) if year_match else None,
            quarter=int(q_match.group(1)) if q_match else None,
            speaker=None, section=None, last=None,
            json=True, csv=False,
        )
        return cmd_transcript(ns)

    elif cmd == "eight-k":
        ns = argparse.Namespace(ticker=ticker, limit=3, json=True, csv=False)
        return cmd_eight_k(ns)

    elif cmd == "insider":
        ns = argparse.Namespace(ticker=ticker, limit=5, json=True, csv=False)
        return cmd_insider(ns)

    elif cmd == "ratios":
        ns = argparse.Namespace(ticker=ticker, form="10-K", limit=1, json=True, csv=False)
        return cmd_ratios(ns)

    elif cmd == "compare" and len(tickers) >= 2:
        # Extract keyword from question
        kw = _extract_metric_keyword(question)
        ns = argparse.Namespace(tickers=f"{tickers[0]},{tickers[1]}", keyword=kw, form="10-K", limit=5, json=True, csv=False)
        return cmd_compare(ns)

    elif cmd == "financials":
        ns = argparse.Namespace(ticker=ticker, form="10-K", limit=1, statement="all", json=True, csv=False)
        return cmd_financials(ns)

    elif cmd == "read-item":
        item = "Item 1"
        if re.search(r"item\s*7|md.?a|discussion", question, re.IGNORECASE):
            item = "Item 7"
        elif re.search(r"item\s*8|financial statement|notes", question, re.IGNORECASE):
            item = "Item 8"
        ns = argparse.Namespace(ticker=ticker, form="10-K", item=item, max_chars=15000, json=True, csv=False)
        return cmd_read_item(ns)

    elif cmd == "search-text":
        kw = _extract_search_keyword(question)
        ns = argparse.Namespace(ticker=ticker, keyword=kw, form="10-K", limit=1, window=500, max_matches=10, json=True, csv=False)
        return cmd_search_text(ns)

    elif cmd == "smart-facts":
        kw = _extract_metric_keyword(question)
        ns = argparse.Namespace(ticker=ticker, keyword=kw, form="10-K", limit=5, question=question, json=True, csv=False)
        return cmd_smart_facts(ns)

    else:
        return {"ok": False, "error": f"Could not determine what data to fetch for: {question}",
                "hint": "Try including specific terms like 'revenue', '8-K', 'insider', 'gross margin', 'business model'."}


def _extract_metric_keyword(question: str) -> str:
    """Extract the financial metric keyword from a question."""
    q_lower = question.lower()
    metric_map = [
        ("gross margin", "GrossProfit"), ("gross profit", "GrossProfit"),
        ("net income", "NetIncome"), ("net margin", "NetIncome"),
        ("operating income", "OperatingIncome"), ("operating margin", "OperatingIncome"),
        ("revenue", "Revenue"), ("sales", "Revenue"),
        ("ebitda", "EBITDA"), ("capex", "CapitalExpenditure"),
        ("eps", "EarningsPerShare"), ("earnings per share", "EarningsPerShare"),
        ("total assets", "Assets"), ("total debt", "Debt"),
        ("free cash flow", "FreeCashFlow"), ("cash flow", "CashFlow"),
        ("roe", "ReturnOnEquity"), ("roa", "ReturnOnAssets"),
        ("r&d", "ResearchAndDevelopment"), ("research", "ResearchAndDevelopment"),
        ("sga", "SellingGeneralAndAdministrative"),
        ("depreciation", "Depreciation"), ("amortization", "Amortization"),
        ("毛利", "GrossProfit"), ("净利润", "NetIncome"), ("收入", "Revenue"), ("营收", "Revenue"),
    ]
    for trigger, concept_kw in metric_map:
        if trigger in q_lower:
            return concept_kw
    # Fallback: use first capitalized word that looks like a metric
    return "Revenue"


def _extract_search_keyword(question: str) -> str:
    """Extract keyword for text search."""
    q_lower = question.lower()
    kw_map = [
        ("depreciation", "useful life"), ("折旧", "useful life"),
        ("lease", "lease term"), ("rent", "lease term"),
        ("accounting polic", "accounting policy"), ("会计政策", "accounting policy"),
        ("server", "server"), ("data center", "data center"),
    ]
    for trigger, kw in kw_map:
        if trigger in q_lower:
            return kw
    return "policy"


def cmd_attachments(args):
    """List or read one filing attachment through edgartools."""
    company = _retry(lambda: Company(args.ticker))
    filings = _get_filings(company, args, args.form)
    if args.accession:
        filings = filings.filter(accession_number=args.accession)
    if len(filings) == 0:
        return {"ok": False, "error": f"No matching {args.form or 'SEC'} filing found for {args.ticker}.", "hint": "Run list-filings first and copy the exact accession number."}
    filing = filings[0]
    attachments = _retry(lambda: filing.attachments)
    rows, selected = [], None
    for attachment in attachments:
        row = {
            "sequence": str(getattr(attachment, "sequence_number", "")),
            "document": str(getattr(attachment, "document", "")),
            "document_type": str(getattr(attachment, "document_type", "")),
            "description": str(getattr(attachment, "display_description", "") or ""),
            "size": getattr(attachment, "size", None),
            "url": str(getattr(attachment, "url", "")),
        }
        rows.append(row)
        if args.document and row["document"] == args.document:
            selected = attachment
        elif not args.document and args.document_type and row["document_type"].upper() == args.document_type.upper() and selected is None:
            selected = attachment
    base = {
        "ticker": args.ticker, "company": str(getattr(company, "name", "")),
        "cik": str(getattr(company, "cik", "")), "form": str(getattr(filing, "form", args.form)),
        "filing_date": str(getattr(filing, "filing_date", "")),
        "accession_no": str(getattr(filing, "accession_no", "")), "attachments": rows,
    }
    if not args.document and not args.document_type:
        return {"ok": True, **base}
    if selected is None:
        return {"ok": False, "error": "Requested attachment was not found.", "hint": "Run attachments without a selector to list exact document names and types.", **base}
    text = _retry(lambda: selected.text())
    if text is None:
        return {"ok": False, "error": f"Attachment {selected.document} is binary or not text-extractable.", "hint": "Use the returned official SEC URL for manual binary-file review.", **base}
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = str(text)
    return {
        "ok": True, **base,
        "selected": {"document": str(selected.document), "document_type": str(selected.document_type), "description": str(getattr(selected, "display_description", "") or ""), "url": str(selected.url)},
        "text": text[:args.max_chars], "total_chars": len(text), "truncated": len(text) > args.max_chars,
    }


def cmd_self_test(args):
    """Verify SEC identity, company lookup, filings, and attachment access."""
    checks = []
    identity_ok = bool(os.environ.get("EDGAR_IDENTITY", "").strip())
    checks.append({"name": "edgar_identity", "ok": identity_ok, **({} if identity_ok else {"hint": "Set EDGAR_IDENTITY='Name <email>' before calling SEC."})})
    try:
        company = _retry(lambda: Company(args.ticker), max_retries=1)
        checks.append({"name": "company_lookup", "ok": True, "ticker": args.ticker, "cik": str(getattr(company, "cik", ""))})
        filings = _retry(lambda: company.get_filings(), max_retries=1)
        filings_ok = len(filings) > 0
        checks.append({"name": "filings_index", "ok": filings_ok})
        if filings_ok:
            filing = filings[0]
            attachments = _retry(lambda: filing.attachments, max_retries=1)
            checks.append({"name": "filing_attachments", "ok": len(attachments) > 0, "accession_no": str(getattr(filing, "accession_no", "")), "attachment_count": len(attachments)})
    except Exception as e:
        checks.append({"name": "sec_connectivity", "ok": False, "error": str(e), "hint": _error_hint(e)})
    ok = all(check.get("ok") for check in checks)
    return {"ok": ok, "ticker": args.ticker, "checks": checks, **({} if ok else {"hint": "Fix failed checks, then rerun --json self-test."})}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="findata-analyst v2 — SEC EDGAR data with NL routing")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--csv", action="store_true", help="CSV output (for rows-based commands)")
    parser.add_argument("--verbose", action="store_true", help="Verbose diagnostics")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-error diagnostics")
    parser.add_argument("--version", action="version", version="findata-analyst v2.2")
    sub = parser.add_subparsers(dest="command", required=True)
    register_commands(sub)

    # ask (NL router)
    p = sub.add_parser("ask")
    p.add_argument("question", nargs="+")
    p.set_defaults(func=lambda a: cmd_ask(argparse.Namespace(question=" ".join(a.question), json=a.json, csv=a.csv)))

    # financials
    p = sub.add_parser("financials")
    p.add_argument("--ticker", required=True)
    p.add_argument("--form", default="10-K")
    p.add_argument("--limit", type=int, default=1)
    p.add_argument("--statement", default="all")
    p.set_defaults(func=cmd_financials)

    # search-concepts
    p = sub.add_parser("search-concepts")
    p.add_argument("--ticker", required=True)
    p.add_argument("--keyword", required=True)
    p.set_defaults(func=cmd_search_concepts)

    # facts
    p = sub.add_parser("facts")
    p.add_argument("--ticker", required=True)
    p.add_argument("--concept", required=True)
    p.add_argument("--form", default="10-K")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_facts)

    # smart-facts (auto concept search + fetch)
    p = sub.add_parser("smart-facts")
    p.add_argument("--ticker", required=True)
    p.add_argument("--keyword", required=True)
    p.add_argument("--form", default="10-K")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_smart_facts)

    # compare
    p = sub.add_parser("compare")
    p.add_argument("--tickers", required=True, help="Two tickers comma-separated (e.g., NVDA,AVGO)")
    p.add_argument("--keyword", required=True)
    p.add_argument("--form", default="10-K")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_compare)

    # ratios
    p = sub.add_parser("ratios")
    p.add_argument("--ticker", required=True)
    p.add_argument("--form", default="10-K")
    p.add_argument("--limit", type=int, default=1)
    p.set_defaults(func=cmd_ratios)

    # list-filings
    p = sub.add_parser("list-filings")
    p.add_argument("--ticker", required=True)
    p.add_argument("--form", default="")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_list_filings)

    # eight-k
    p = sub.add_parser("eight-k")
    p.add_argument("--ticker", required=True)
    p.add_argument("--limit", type=int, default=3)
    p.set_defaults(func=cmd_eight_k)

    # insider
    p = sub.add_parser("insider")
    p.add_argument("--ticker", required=True)
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_insider)

    # search-text
    p = sub.add_parser("search-text")
    p.add_argument("--ticker", required=True)
    p.add_argument("--keyword", required=True)
    p.add_argument("--form", default="10-K")
    p.add_argument("--limit", type=int, default=1)
    p.add_argument("--window", type=int, default=500)
    p.add_argument("--max_matches", type=int, default=10)
    p.set_defaults(func=cmd_search_text)

    # read-item
    p = sub.add_parser("read-item")
    p.add_argument("--ticker", required=True)
    p.add_argument("--form", default="10-K")
    p.add_argument("--item", required=True)
    p.add_argument("--max_chars", type=int, default=15000)
    p.set_defaults(func=cmd_read_item)

    # transcript (via roic-transcript)
    p = sub.add_parser("transcript")
    p.add_argument("--ticker", required=True)
    p.add_argument("--year", type=int)
    p.add_argument("--quarter", type=int, choices=[1, 2, 3, 4])
    p.add_argument("--latest", action="store_true", help="Fetch most recent transcript")
    p.add_argument("--last", type=int, metavar="N", help="Fetch last N transcripts")
    p.add_argument("--speaker", nargs="+", help="Filter by speaker name(s)")
    p.add_argument("--section", choices=["prepared", "qa"], help="Filter prepared remarks or Q&A")
    p.set_defaults(func=cmd_transcript)

    # attachments
    p = sub.add_parser("attachments")
    p.add_argument("--ticker", required=True)
    p.add_argument("--form", default="6-K")
    p.add_argument("--accession", help="Exact accession number from list-filings")
    selector = p.add_mutually_exclusive_group()
    selector.add_argument("--document", help="Exact attachment filename")
    selector.add_argument("--document-type", help="Exact SEC document type, e.g. EX-99.1")
    p.add_argument("--max-chars", type=int, default=120000)
    p.set_defaults(func=cmd_attachments)

    # self-test
    p = sub.add_parser("self-test")
    p.add_argument("--ticker", default="AAPL")
    p.set_defaults(func=cmd_self_test)

    for command in ("financials", "ratios", "list-filings", "eight-k", "insider", "search-text", "read-item", "attachments", "search-concepts", "facts", "smart-facts", "compare"):
        sub.choices[command].add_argument("--as-of", type=parse_as_of, help="Only SEC evidence filed on or before this date (YYYY-MM-DD); daily, not intraday")

    args = parser.parse_args()
    try:
        result = args.func(args)
        if isinstance(result, dict) and getattr(args, "as_of", None):
            result["as_of"] = args.as_of
            result["point_in_time"] = "filing_date_cutoff_daily"
            result["source"] = "sec_edgar"
            result.setdefault("retrieved_at", result.get("cache", {}).get("stored_at") or datetime.now(timezone.utc).isoformat())
            if not result.get("ok"):
                result["hint"] = "Check the ticker/form and cutoff; only filings public by --as-of are allowed. Current Yahoo data is not substituted."
        _emit(result, as_json=args.json, as_csv=args.csv)
    except Exception as e:
        failure = _error_json(e)
        if args.command in ("macro", "treasury"):
            failure.update(status="error", source="fred_alfred" if args.command == "macro" else "us_treasury_fiscaldata",
                           hint="Check series/dataset and dates, then retry the official endpoint. No alternate source or cached data was substituted.")
        _emit(failure, as_json=args.json)
        sys.exit(1)


if __name__ == "__main__":
    main()
