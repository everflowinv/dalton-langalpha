#!/usr/bin/env python3
"""HK Momentum Index — data collector (HKEX universe, SZSE southbound list, Tencent quotes)."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
from datetime import date, datetime
import io
import json
import math
import os
from pathlib import Path
import random
import re
import time
from typing import Any

import requests

from scripts.dalton.monitor_feeds.common import (
    HK_TZ,
    SourceStatus,
    atomic_write_json,
    atomic_write_text,
    is_trading_day,
    load_json_safe,
    make_source_result,
    market_session_state,
    now_hk,
    previous_trading_day,
    resolve_state_dir,
)

HKEX_XLSX_URL = (
    "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
)
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
HIST_START = "2023-06-01"


def _sess(proxy: str = "") -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(UA)
    clean_proxy = (proxy or "").strip()
    if clean_proxy:
        s.proxies = {"http": clean_proxy, "https": clean_proxy}
    return s


def resolve_hkex_proxy(explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit.strip()
    return (
        os.environ.get("HK_MOMENTUM_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()


def fetch_hkex_universe(*, state_dir: Path, proxy: str = "", session: Any = None):
    import pandas as pd

    path = state_dir / "hkex_los.xlsx"
    s = session or _sess(proxy=proxy)
    r = s.get(HKEX_XLSX_URL, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    df = pd.read_excel(path, header=2)
    df = df[df["Category"] == "Equity"].copy()
    df["code"] = df["Stock Code"].astype(int).apply(lambda x: f"{x:05d}")
    df = df[df["Stock Code"].astype(int) <= 9999]
    return df[["code", "Name of Securities", "Sub-Category", "Trading Currency"]].rename(
        columns={
            "Name of Securities": "name",
            "Sub-Category": "subcat",
            "Trading Currency": "ccy",
        }
    )


def fetch_ggt_list(*, session: Any = None) -> dict[str, str]:
    """Fetch complete SZSE Southbound list across all pages; validate pagecount, count, and uniqueness."""
    s = session or _sess()
    codes: dict[str, str] = {}
    page = 1
    expected_pages: int | None = None
    expected_records: int | None = None
    total_rows = 0
    while True:
        url = (
            "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
            f"&CATALOGID=SGT_GGTBDQD&PAGENO={page}&random={random.random()}"
        )
        j = s.get(url, timeout=15).json()
        if not isinstance(j, list) or not j or not isinstance(j[0], dict):
            raise RuntimeError(f"港股通名单第 {page} 页响应格式无效")
        block = j[0]
        meta = block.get("metadata")
        if not isinstance(meta, dict) or meta.get("pagecount") is None or isinstance(meta.get("pagecount"), bool):
            raise RuntimeError(f"港股通名单第 {page} 页缺少 pagecount 元数据")
        try:
            pc = int(meta["pagecount"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"港股通名单第 {page} 页 pagecount 无效: {meta.get('pagecount')!r}") from exc
        if pc <= 0:
            raise RuntimeError(f"港股通名单总页数无效: {meta.get('pagecount')!r}")
        if expected_pages is None:
            expected_pages = pc
        elif pc != expected_pages:
            raise RuntimeError(f"港股通名单第 {page} 页总页数不一致: {pc} != {expected_pages}")

        rc_raw = meta.get("recordcount")
        if rc_raw is None or isinstance(rc_raw, bool):
            raise RuntimeError(f"港股通名单第 {page} 页缺少 recordcount 元数据")
        try:
            rc = int(rc_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"港股通名单第 {page} 页 recordcount 无效: {rc_raw!r}") from exc
        if rc <= 0:
            raise RuntimeError(f"港股通名单总条数无效: {rc_raw!r}")
        if expected_records is None:
            expected_records = rc
        elif rc != expected_records:
            raise RuntimeError(f"港股通名单第 {page} 页总条数不一致: {rc} != {expected_records}")

        data = block.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"港股通名单第 {page} 页返回空列表")
        total_rows += len(data)
        for row in data:
            if not isinstance(row, dict):
                raise RuntimeError(f"港股通名单第 {page} 页包含无效记录: {row!r}")
            code = str(row.get("zqdm", "")).strip()
            name = str(row.get("zqjc", "")).strip()
            if not code.isdigit() or not name:
                raise RuntimeError(f"港股通名单第 {page} 页含无效代码或名称: {row!r}")
            padded = code.zfill(5)
            if padded in codes:
                raise RuntimeError(f"港股通名单第 {page} 页出现重复代码: {padded}")
            codes[padded] = name
        if page >= expected_pages:
            break
        page += 1
        time.sleep(0.15)
    if not codes or len(codes) != total_rows:
        raise RuntimeError(f"港股通名单唯一代码数({len(codes)})与总记录数({total_rows})不符")
    if expected_records is not None and len(codes) != expected_records:
        raise RuntimeError(f"港股通名单唯一代码数({len(codes)})与 recordcount({expected_records})不符")
    return codes


_QUOTE_DATE_RE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")


def _extract_quote_date(ts_raw: Any) -> str | None:
    if not ts_raw:
        return None
    m = _QUOTE_DATE_RE.search(str(ts_raw))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _target_hk_trade_date(as_of_date: date | str | None = None) -> str:
    if as_of_date is not None:
        d = date.fromisoformat(as_of_date) if isinstance(as_of_date, str) else as_of_date
        if is_trading_day("hk", d):
            return d.isoformat()
        return previous_trading_day("hk", d).isoformat()
    cur = now_hk()
    sess = market_session_state("hk", cur)
    am_start = int(sess["am_start"]) if sess.get("am_start") is not None else (9 * 60 + 30)
    if sess.get("is_trading_day") and (cur.hour * 60 + cur.minute) >= am_start:
        return cur.date().isoformat()
    return previous_trading_day("hk", cur.date()).isoformat()


def fetch_spot_batch(
    codes: list[str],
    *,
    session: Any = None,
    batch_size: int = 60,
    expected_date: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Tencent batch quotes. Returns ({code: quote_dict}, errors)."""
    s = session or _sess()
    out: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    seen_codes: set[str] = set()
    dup_codes: list[str] = []
    bad_date_codes: list[str] = []
    for i in range(0, len(codes), batch_size):
        chunk = codes[i : i + batch_size]
        q = ",".join(f"r_hk{c}" for c in chunk)
        try:
            r = s.get(f"https://qt.gtimg.cn/q={q}", timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                var, payload = line.split("=", 1)
                code = var.split("_hk")[-1].strip()
                if code in seen_codes:
                    dup_codes.append(code)
                    out.pop(code, None)
                    continue
                seen_codes.add(code)
                f = payload.strip('"').split("~")
                if len(f) < 46 or f[3] in ("", "0.000"):
                    continue
                try:
                    px = float(f[3])
                    if not math.isfinite(px) or px <= 0:
                        continue
                    prev_close = float(f[4]) if f[4] else None
                    if prev_close is not None and not math.isfinite(prev_close):
                        continue
                    turnover = float(f[37]) if f[37] else 0.0
                    mktcap = float(f[45]) if f[45] else None
                    pct = float(f[32]) if f[32] else 0.0
                    if not (math.isfinite(turnover) and math.isfinite(pct)) or (mktcap is not None and not math.isfinite(mktcap)):
                        continue
                    q_date = _extract_quote_date(f[30])
                    if expected_date is not None and q_date != expected_date:
                        bad_date_codes.append(code)
                        continue
                    out[code] = {
                        "name_cn": f[1],
                        "price": px,
                        "prev_close": prev_close,
                        "turnover": turnover,
                        "mktcap_e8": mktcap,
                        "pct": pct,
                        "ts": f[30],
                    }
                except (ValueError, IndexError):
                    continue
        except Exception as exc:
            errors.append(f"batch_{i}: {exc}")
        if i + batch_size < len(codes):
            time.sleep(0.12)
    if dup_codes:
        uniq_dups = sorted(set(dup_codes))
        errors.append(
            f"港股现货响应包含重复代码 ({len(uniq_dups)} 只，如 {', '.join(uniq_dups[:5])})"
        )
    if bad_date_codes:
        uniq_bad = sorted(set(bad_date_codes))
        errors.append(
            f"港股现货报价日期非目标交易日 {expected_date} ({len(uniq_bad)} 只，如 {', '.join(uniq_bad[:5])})"
        )
    return out, errors


def fetch_hsi_spot(*, session: Any = None) -> tuple[dict[str, Any] | None, str | None]:
    s = session or _sess()
    try:
        r = s.get("https://qt.gtimg.cn/q=r_hkHSI", timeout=10)
        r.encoding = "gbk"
        payload = r.text.split("=", 1)[-1].strip().strip('"; ')
        f = payload.split("~")
        return {
            "value": float(f[3]),
            "pct": float(f[32]),
            "ts": f[30] if len(f) > 30 else "",
        }, None
    except Exception as exc:
        return None, f"恒指实时报价失败: {exc}"


def _load_held_codes(st_dir: Path) -> set[str]:
    """Return active portfolio constituent codes from frozen_history.json / constituents.json."""
    held: set[str] = set()
    frozen = load_json_safe(st_dir / "frozen_history.json", {})
    if isinstance(frozen, dict):
        rebals = frozen.get("rebalance_snapshots")
        if isinstance(rebals, dict) and rebals:
            last_rebal = rebals.get(sorted(rebals.keys())[-1])
            if isinstance(last_rebal, dict):
                for codes in last_rebal.values():
                    if isinstance(codes, list):
                        held.update(str(c).strip().zfill(5) for c in codes if c)
    cons = load_json_safe(st_dir / "constituents.json", {})
    if isinstance(cons, dict):
        for items in cons.values():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("code"):
                        held.add(str(item["code"]).strip().zfill(5))
    return held


def build_universe(
    *,
    state_dir: str | Path | None = None,
    proxy: str | None = None,
    session: Any = None,
    as_of_date: date | str | None = None,
):
    """Build HK universe + GGT list + spot prices; never overwrites prior files on any failure."""
    import pandas as pd

    st_dir = resolve_state_dir("hk_momentum", state_dir)
    meta_path = st_dir / "universe_meta.json"
    uni_csv = st_dir / "universe.csv"
    ggt_json = st_dir / "ggt_list.json"
    errors: list[str] = []
    source_statuses: dict[str, SourceStatus] = {}
    now_str = now_hk().strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
    target_trade_date = _target_hk_trade_date(as_of_date)

    held_codes = _load_held_codes(st_dir)
    uni: pd.DataFrame | None = None
    try:
        uni = fetch_hkex_universe(
            state_dir=st_dir, proxy=resolve_hkex_proxy(proxy), session=session
        )
        if uni is None or len(uni) == 0:
            raise RuntimeError("港交所证券列表为空")
        if "code" not in uni.columns or "name" not in uni.columns:
            raise RuntimeError("港交所证券列表缺少必要列")
        code_series = uni["code"].astype(str).str.strip()
        if (code_series == "").any() or code_series.duplicated().any():
            raise RuntimeError("港交所证券列表包含空代码或重复代码")
        if "subcat" in uni.columns and not uni["subcat"].astype(str).str.contains("Main Board", na=False).any():
            raise RuntimeError("港交所证券列表缺少主板股票记录")
        if len(held_codes) >= 10 and len(set(code_series) & held_codes) < int(0.8 * len(held_codes)):
            raise RuntimeError(
                f"港交所证券列表不完整（仅命中 {len(set(code_series) & held_codes)}/{len(held_codes)} 只持有成分）"
            )
        source_statuses["hkex_universe"] = make_source_result(
            "hkex_universe", ok=True, stale=False, as_of=now_str, label="港交所证券列表"
        )
    except Exception as exc:
        uni = None
        err = f"港交所证券列表拉取失败: {exc}"
        errors.append(err)
        source_statuses["hkex_universe"] = make_source_result(
            "hkex_universe", ok=False, stale=True, reason=err, label="港交所证券列表"
        )

    ggt: dict[str, str] | None = None
    try:
        ggt = fetch_ggt_list(session=session)
        source_statuses["ggt_list"] = make_source_result(
            "ggt_list", ok=True, stale=False, as_of=now_str, label="深交所港股通名单"
        )
    except Exception as exc:
        err = f"港股通名单拉取失败: {exc}"
        errors.append(err)
        source_statuses["ggt_list"] = make_source_result(
            "ggt_list", ok=False, stale=True, reason=err, label="深交所港股通名单"
        )

    if uni is None:
        uni = pd.read_csv(uni_csv, dtype={"code": str}) if uni_csv.is_file() else pd.DataFrame(
            columns=["code", "name", "subcat", "ccy", "is_ggt", "price", "mktcap_e8", "name_cn"]
        )
    if ggt is None:
        old_ggt = load_json_safe(ggt_json, {}) or {}
        ggt = old_ggt if isinstance(old_ggt, dict) else {}

    uni["is_ggt"] = uni["code"].isin(ggt.keys())
    codes_to_spot = sorted(set(uni["code"]) | held_codes)
    spot: dict[str, dict[str, Any]] = {}
    spot_errs: list[str] = []
    if codes_to_spot:
        spot, spot_errs = fetch_spot_batch(
            codes_to_spot, session=session, expected_date=target_trade_date
        )
        if spot_errs:
            errors.extend(spot_errs[:5])
    else:
        spot_errs = ["标的列表为空，无法拉取现货报价"]
        errors.extend(spot_errs)

    wrong_date_codes = sorted(
        c for c, entry in spot.items() if _extract_quote_date(entry.get("ts")) != target_trade_date
    )
    fresh_spot = {
        c: entry
        for c, entry in spot.items()
        if _extract_quote_date(entry.get("ts")) == target_trade_date
    }
    uni["price"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("price"))
    uni["mktcap_e8"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("mktcap_e8"))
    uni["name_cn"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("name_cn"))

    n_equity = int(len(uni))
    n_quoted = int(uni["price"].notna().sum()) if "price" in uni and len(uni) else 0
    spot_reasons = list(spot_errs[:5]) if spot_errs else []
    if wrong_date_codes and not any("日期非目标交易日" in msg for msg in spot_reasons):
        date_err = (
            f"港股现货报价日期非目标交易日 {target_trade_date} "
            f"({len(wrong_date_codes)} 只，如 {', '.join(wrong_date_codes[:5])})"
        )
        errors.append(date_err)
        spot_reasons.append(date_err)
    if n_equity == 0 or n_quoted == 0 or (n_quoted * 2 < n_equity):
        cov_err = f"港股现货报价覆盖不足 ({n_quoted}/{n_equity})"
        errors.append(cov_err)
        spot_reasons.append(cov_err)

    missing_held_spot = sorted(
        c
        for c in held_codes
        if not (
            isinstance(fresh_spot.get(c, {}).get("price"), (int, float))
            and not isinstance(fresh_spot.get(c, {}).get("price"), bool)
            and math.isfinite(float(fresh_spot[c]["price"]))
            and float(fresh_spot[c]["price"]) > 0
        )
    )
    if missing_held_spot:
        sample = ", ".join(missing_held_spot[:5])
        held_err = f"已持有成分缺少现货报价 {len(missing_held_spot)} 只 (如 {sample})"
        errors.append(held_err)
        spot_reasons.append(held_err)

    if spot_reasons:
        source_statuses["hk_spot"] = make_source_result(
            "hk_spot", ok=False, stale=True, reason="；".join(spot_reasons), label="港股现货行情"
        )
    else:
        source_statuses["hk_spot"] = make_source_result(
            "hk_spot", ok=True, stale=False, as_of=now_str, label="港股现货行情"
        )

    degraded = any(not st.is_clean for st in source_statuses.values())
    if not degraded:
        uni.to_csv(uni_csv, index=False)
        atomic_write_json(ggt_json, ggt)
    elif uni_csv.is_file():
        # Preserve prior clean universe.csv & ggt_list.json for caller consistency
        uni = pd.read_csv(uni_csv, dtype={"code": str})
    meta = {
        "updated": now_hk().isoformat(timespec="seconds"),
        "n_equity": n_equity,
        "n_ggt": int(uni["is_ggt"].sum()) if "is_ggt" in uni else 0,
        "n_quoted": n_quoted,
        "errors": errors,
        "stale": degraded,
        "preserved_previous_universe": bool(degraded and uni_csv.is_file()),
        "source_statuses": {k: v.to_dict() for k, v in source_statuses.items()},
    }
    atomic_write_json(meta_path, meta)
    return uni, meta


def fetch_hist_one(
    sess: Any,
    code: str,
    *,
    start: str = HIST_START,
    end: str | None = None,
    count: int = 900,
) -> list[list[Any]]:
    end = end or now_hk().date().isoformat()
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?"
        f"param=hk{code},day,{start},{end},{count},qfq"
    )
    r = sess.get(url, timeout=20)
    j = r.json()
    d = j.get("data", {}).get(f"hk{code}", {})
    rows = d.get("qfqday") or d.get("day") or []
    if not rows:
        raise RuntimeError(f"历史行情返回空列表 ({code})")
    out: list[list[Any]] = []
    seen_dates: set[str] = set()
    bad_rows = 0
    for row in rows:
        try:
            d_str = str(row[0]).strip()
            datetime.strptime(d_str, "%Y-%m-%d")
            if d_str in seen_dates:
                bad_rows += 1
                continue
            seen_dates.add(d_str)
            o, c, h, lo, vol = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
            if (
                not math.isfinite(o)
                or not math.isfinite(c)
                or c <= 0
                or not math.isfinite(h)
                or not math.isfinite(lo)
                or not math.isfinite(vol)
            ):
                bad_rows += 1
                continue
            rec: list[Any] = [d_str, o, c, h, lo, vol]
            turn: float | None = None
            for x in row[6:]:
                if isinstance(x, str):
                    try:
                        turn = float(x)
                    except ValueError:
                        pass
            rec.append(turn if turn is not None else 0.0)
            out.append(rec)
        except (ValueError, IndexError, TypeError):
            bad_rows += 1
            continue
    if not out or bad_rows > 0:
        raise RuntimeError(
            f"历史行情记录不完整或含重复/无效日期 ({code}: total={len(rows)}, valid={len(out)}, bad={bad_rows})"
        )
    return out


def _existing_csv_dates(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    dates: set[str] = set()
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for ln in lines[1:]:
            d_str = ln.split(",", 1)[0].strip()
            datetime.strptime(d_str, "%Y-%m-%d")
            dates.add(d_str)
    except Exception:
        return set()
    return dates


def download_history(
    codes: list[str],
    *,
    state_dir: str | Path | None = None,
    full: bool = False,
    workers: int = 8,
    session: Any = None,
) -> dict[str, Any]:
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    today = now_hk().date().isoformat()

    todo: list[str] = []
    for c in codes:
        p = hist_dir / f"{c}.csv"
        if full or not p.is_file():
            todo.append(c)
        else:
            try:
                lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if len(lines) <= 1:
                    todo.append(c)
                    continue
                last = lines[-1].split(",")[0]
                if last < today:
                    todo.append(c)
            except Exception:
                todo.append(c)

    errs: list[str] = []
    done = 0

    def _work(code: str) -> bool:
        s = session or _sess()
        p = hist_dir / f"{code}.csv"
        prev_dates = _existing_csv_dates(p)
        for attempt in range(3):
            try:
                rows = fetch_hist_one(s, code)
                new_dates = [r[0] for r in rows]
                new_date_set = set(new_dates)
                if len(new_date_set) != len(rows):
                    raise RuntimeError(f"历史响应含重复日期 ({code})")
                if prev_dates:
                    if len(rows) < len(prev_dates):
                        raise RuntimeError(f"历史记录不完整 ({code}: {len(rows)} < {len(prev_dates)})")
                    missing_old = prev_dates - new_date_set
                    if missing_old:
                        raise RuntimeError(
                            f"历史响应丢失原有日期 ({code}: 缺失 {len(missing_old)} 天)"
                        )
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["date", "open", "close", "high", "low", "volume", "turnover_wan"])
                w.writerows(rows)
                atomic_write_text(p, buf.getvalue())
                return True
            except Exception:
                time.sleep(0.2 + attempt * 0.2)
        errs.append(code)
        return False

    if todo:
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for ok in ex.map(_work, todo):
                if ok:
                    done += 1

    if not errs:
        hist_st = make_source_result(
            "hk_history",
            ok=True,
            stale=False,
            as_of=now_hk().date().isoformat(),
            label="港股历史日线",
        )
    else:
        hist_st = make_source_result(
            "hk_history",
            ok=False,
            stale=True,
            reason=f"历史日线下载失败 {len(errs)}/{len(todo)} 只",
            label="港股历史日线",
        )
    progress = {
        "done": done,
        "total": len(todo),
        "errors": errs[:50],
        "n_errors": len(errs),
        "stale": bool(errs),
        "source_status": hist_st.to_dict(),
        "ts": now_hk().astimezone(HK_TZ).isoformat(timespec="seconds"),
        "finished": True,
    }
    atomic_write_json(st_dir / "hist_progress.json", progress)
    return progress


def main() -> int:
    import pandas as pd

    parser = argparse.ArgumentParser(description="Fetch HKEX universe, GGT list, and HK qfq history.")
    parser.add_argument("cmd", nargs="?", default="all", choices=("universe", "history", "all"))
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    st_dir = resolve_state_dir("hk_momentum", args.state_dir)
    if args.cmd in ("universe", "all"):
        uni, _ = build_universe(state_dir=st_dir, proxy=args.proxy)
    else:
        uni = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
    if args.cmd in ("history", "all"):
        codes = list(uni.loc[uni["price"].notna(), "code"]) if "price" in uni else list(uni["code"])
        res = download_history(codes, state_dir=st_dir, full=args.full)
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
