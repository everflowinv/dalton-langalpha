#!/usr/bin/env python3
"""HK Momentum Index — data collector (HKEX universe, SZSE southbound list, Tencent quotes)."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
from datetime import date, datetime
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Iterable

import requests

from scripts.dalton.monitor_feeds.common import (
    HK_TZ,
    SourceStatus,
    atomic_write_json,
    atomic_write_text,
    load_json_safe,
    make_source_result,
    now_hk,
    resolve_market_trade_date,
    resolve_state_dir,
)

HKEX_XLSX_URL = (
    "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
)
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
logger = logging.getLogger(__name__)

HIST_START = "2023-06-01"
# Tencent returns the latest N bars and ignores the start date; up to ~1500 come
# back whole (above ~1600 it falls back to 640). 1200 covers HIST_START with room.
HIST_BARS = 1200


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


class GGTListResult(dict[str, str]):
    """Code-to-name mapping returned by ``fetch_ggt_list`` with validation counters."""

    def __init__(
        self,
        *args: Any,
        missing_cn_name: int = 0,
        skipped_invalid: int = 0,
        skipped_codes: Iterable[str] = (),
        skipped_reasons: dict[str, int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.missing_cn_name = int(missing_cn_name)
        self.skipped_invalid = int(skipped_invalid)
        self.skipped_codes = tuple(sorted({str(c).strip().zfill(5) for c in skipped_codes if str(c).strip()}))
        self.skipped_reasons = {str(k): int(v) for k, v in (skipped_reasons or {}).items() if int(v) > 0}


def _format_ggt_skip_note(skipped_invalid: int, skipped_reasons: dict[str, int] | None) -> str:
    if skipped_reasons:
        items = [(k, v) for k, v in skipped_reasons.items() if v > 0]
        if len(items) == 1:
            detail = items[0][0]
        else:
            detail = "、".join(f"{k} {v} 只" for k, v in items)
    else:
        detail = "记录字段无效"
    return f"跳过 {skipped_invalid} 只（{detail}）"


def fetch_ggt_list(*, session: Any = None) -> GGTListResult:
    """Fetch complete SZSE Southbound list across all pages; allow English fallback and skip invalid rows."""
    s = session or _sess()
    codes: dict[str, str] = {}
    page = 1
    expected_pages: int | None = None
    expected_records: int | None = None
    total_rows = 0
    missing_cn_name = 0
    skipped_invalid = 0
    skipped_codes: list[str] = []
    skipped_codes_set: set[str] = set()
    skipped_reasons: dict[str, int] = {}
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
            raw_code = row.get("zqdm")
            raw_cn = row.get("zqjc")
            raw_en = row.get("zqywjc")
            code = str(raw_code if raw_code is not None else "").strip()
            name_cn = str(raw_cn if raw_cn is not None else "").strip()
            name_en = str(raw_en if raw_en is not None else "").strip()
            if not code.isdigit() or len(code) > 5 or int(code) <= 0:
                skipped_invalid += 1
                skipped_reasons["代码无效"] = skipped_reasons.get("代码无效", 0) + 1
                continue
            padded = code.zfill(5)
            if not name_cn and not name_en:
                skipped_invalid += 1
                skipped_reasons["中英文名称均为空"] = skipped_reasons.get("中英文名称均为空", 0) + 1
                if padded not in skipped_codes_set:
                    skipped_codes_set.add(padded)
                    skipped_codes.append(padded)
                # Ensure ambiguous code cannot stay confirmed if an earlier row appeared valid
                codes.pop(padded, None)
                continue
            if name_cn:
                name = name_cn
            else:
                name = name_en
                missing_cn_name += 1
            if padded in codes or padded in skipped_codes_set:
                raise RuntimeError(f"港股通名单第 {page} 页出现重复代码: {padded}")
            codes[padded] = name
        if page >= expected_pages:
            break
        page += 1
        time.sleep(0.15)
    if not codes:
        raise RuntimeError(f"港股通名单全部记录无效（总 {total_rows} 条，跳过 {skipped_invalid} 条）")
    if len(codes) + skipped_invalid != total_rows:
        raise RuntimeError(
            f"港股通名单有效与跳过记录数({len(codes)}+{skipped_invalid})与总记录数({total_rows})不符"
        )
    if expected_records is not None and total_rows != expected_records:
        raise RuntimeError(f"港股通名单总记录数({total_rows})与 recordcount({expected_records})不符")
    return GGTListResult(
        codes,
        missing_cn_name=missing_cn_name,
        skipped_invalid=skipped_invalid,
        skipped_codes=skipped_codes,
        skipped_reasons=skipped_reasons,
    )


_QUOTE_DATE_RE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")


def _extract_quote_date(ts_raw: Any) -> str | None:
    if not ts_raw:
        return None
    m = _QUOTE_DATE_RE.search(str(ts_raw))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _target_hk_trade_date(
    as_of_date: date | str | None = None,
    *,
    now: datetime | None = None,
) -> str:
    cur = (now or now_hk()) if as_of_date is None else None
    return resolve_market_trade_date("hk", now=cur, as_of_date=as_of_date).isoformat()


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
    ggt_missing_cn = 0
    ggt_skipped_invalid = 0
    ggt_skipped_codes: list[str] = []
    ggt_skipped_reasons: dict[str, int] = {}
    ggt_skip_note: str | None = None
    try:
        ggt = fetch_ggt_list(session=session)
        ggt_missing_cn = int(getattr(ggt, "missing_cn_name", 0) or 0)
        ggt_skipped_invalid = int(getattr(ggt, "skipped_invalid", 0) or 0)
        ggt_skipped_codes = sorted(set(getattr(ggt, "skipped_codes", ()) or ()))
        ggt_skipped_reasons = dict(getattr(ggt, "skipped_reasons", {}) or {})
        ggt_notes: list[str] = []
        if ggt_missing_cn > 0:
            ggt_notes.append(f"缺中文简称 {ggt_missing_cn} 只")
        if ggt_skipped_invalid > 0:
            ggt_skip_note = _format_ggt_skip_note(ggt_skipped_invalid, ggt_skipped_reasons)
            ggt_notes.append(ggt_skip_note)
        source_statuses["ggt_list"] = make_source_result(
            "ggt_list",
            ok=True,
            stale=False,
            as_of=now_str,
            label="深交所港股通名单",
            note="，".join(ggt_notes) if ggt_notes else None,
            counts={
                "valid": len(ggt),
                "missing_cn_name": ggt_missing_cn,
                "skipped_invalid": ggt_skipped_invalid,
            },
        )
    except Exception as exc:
        err = f"港股通名单拉取失败: {exc}"
        errors.append(err)
        source_statuses["ggt_list"] = make_source_result(
            "ggt_list", ok=False, stale=True, reason=err, label="深交所港股通名单"
        )

    if uni is None:
        uni = pd.read_csv(uni_csv, dtype={"code": str}) if uni_csv.is_file() else pd.DataFrame(
            columns=["code", "name", "subcat", "ccy", "is_ggt", "excluded", "price", "mktcap_e8", "name_cn"]
        )
    if ggt is None:
        old_ggt = load_json_safe(ggt_json, {}) or {}
        ggt = old_ggt if isinstance(old_ggt, dict) else {}

    uni["is_ggt"] = uni["code"].isin(ggt.keys())
    uni["excluded"] = uni["code"].isin(ggt_skipped_codes)
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
        "target_trade_date": target_trade_date,
        "n_equity": n_equity,
        "n_ggt": int(uni["is_ggt"].sum()) if "is_ggt" in uni else 0,
        "ggt_missing_cn_name": ggt_missing_cn,
        "ggt_skipped_invalid": ggt_skipped_invalid,
        "ggt_skipped_codes": ggt_skipped_codes,
        "ggt_skipped_reasons": ggt_skipped_reasons,
        "ggt_skipped_note": ggt_skip_note,
        "excluded_codes": ggt_skipped_codes,
        "n_quoted": n_quoted,
        "errors": errors,
        "stale": degraded,
        "preserved_previous_universe": bool(degraded and uni_csv.is_file()),
        "source_statuses": {k: v.to_dict() for k, v in source_statuses.items()},
    }
    atomic_write_json(meta_path, meta)
    return uni, meta


class NoAdjustedHistory(RuntimeError):
    """Tencent has only unadjusted bars for the code (typically delisted)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"历史行情无前复权数据 ({code})")
        self.code = code


def fetch_hist_one(
    sess: Any,
    code: str,
    *,
    start: str = HIST_START,
    end: str | None = None,
    count: int = HIST_BARS,
) -> list[list[Any]]:
    end = end or now_hk().date().isoformat()
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?"
        f"param=hk{code},day,{start},{end},{count},qfq"
    )
    r = sess.get(url, timeout=20)
    j = r.json()
    d = j.get("data", {}).get(f"hk{code}", {})
    # Forward-adjusted bars only: falling back to the plain "day" series would
    # mix an unadjusted basis into a file that is rewritten whole.
    rows = d.get("qfqday") or []
    if not rows:
        if d.get("day"):
            raise NoAdjustedHistory(code)
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
    target_date: str | None = None,
) -> dict[str, Any]:
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    today = target_date or _target_hk_trade_date()

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
                try:
                    rows = fetch_hist_one(s, code)
                except NoAdjustedHistory:
                    if prev_dates:
                        # No adjusted series any more (delisted): the stored
                        # adjusted file stays as it is rather than mixing in
                        # unadjusted bars.
                        return True
                    raise
                new_dates = [r[0] for r in rows]
                new_date_set = set(new_dates)
                if len(new_date_set) != len(rows):
                    raise RuntimeError(f"历史响应含重复日期 ({code})")
                if prev_dates and new_dates:
                    # Tencent returns the latest HIST_BARS bars. Only a full
                    # window can roll older stored dates off; a short response
                    # owes every stored date. Either way the response must
                    # overlap what is stored. Sessions after the last stored bar
                    # are not checked per stock: a suspended stock has none, so
                    # target-date coverage is judged on the whole panel instead.
                    window_start = min(new_dates)
                    if window_start > max(prev_dates):
                        raise RuntimeError(f"历史响应与已有记录不重叠 ({code})")
                    owed = prev_dates if len(rows) < HIST_BARS else {d for d in prev_dates if d >= window_start}
                    missing_old = owed - new_date_set
                    if missing_old:
                        raise RuntimeError(
                            f"历史响应丢失原有日期 ({code}: 缺失 {len(missing_old)} 天)"
                        )
                if new_dates and min(new_dates) > HIST_START:
                    logger.warning(
                        "hk_momentum history window starts %s, after HIST_START %s (%s)",
                        min(new_dates), HIST_START, code,
                    )
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["date", "open", "close", "high", "low", "volume", "turnover_wan"])
                # Keep the panel's start at HIST_START: earlier bars would move
                # the first rebalance before the HSI history the index needs.
                w.writerows([r for r in rows if r[0] >= HIST_START])
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
        uni_path = st_dir / "universe.csv"
        if not uni_path.is_file():
            raise RuntimeError(f"缺少成分文件 {uni_path}，请先运行 universe 抓取生成成分名单")
        uni = pd.read_csv(uni_path, dtype={"code": str})
    if args.cmd in ("history", "all"):
        codes = list(uni.loc[uni["price"].notna(), "code"]) if "price" in uni else list(uni["code"])
        res = download_history(codes, state_dir=st_dir, full=args.full)
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
