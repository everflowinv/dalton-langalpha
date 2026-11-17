"""解析港交所 / 巨潮「翌日披露報表」(FF305) 第二章節的股份购回报告。

用法：
  python3 buyback_parse.py URL [URL ...]       # 下载 PDF 并解析
  python3 buyback_parse.py --text-json raw.json # {key: 正文} 已取回的正文

输出 JSON 列表，每条包含：
  url 或 key, trades[{date, shares, hi, lo, amount, avg, currency}],
  total_shares, total_shares_raw, total_amount, total_amount_raw, currency, avg_price, hi, lo,
  to_cancel, to_treasury, mandate_cum_shares, mandate_cum_pct, line。
多股份类别或多币种时额外返回 groups 分组（不跨币种相加）。
解析不到、缺少依赖、数字格式不完整或逐行之和与「合共」不一致时返回 error 字段（附 discrepancy），不编造数字。

威胁模型：本模块产出落盘于扫描结果文件（scan output），渲染阶段防的是模型填写的 payload 写错、抄错或混入金额，不防有人刻意伪造脚本自己写出的扫描结果文件。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
from typing import Any

CUR_CN = {"HKD": "港元", "RMB": "元人民币", "USD": "美元"}
_DEFAULT_CLASSES = {"", "不適用", "不适用", "普通股"}
_AMENDMENT_RE = re.compile(r"(?:更正|修訂|修订|補充|补充)")
_PLACEHOLDER_STOCK_CODES = frozenset({
    "不適用",
    "不适用",
    "說明",
    "说明",
    "無",
    "无",
    "N/A",
    "NA",
    "NIL",
    "NONE",
    "-",
    "--",
    "—",
    "/",
})


def _norm_stock_code(code: Any) -> str:
    raw = str(code or "").strip().upper()
    if not raw or raw in _PLACEHOLDER_STOCK_CODES:
        return ""
    if raw.endswith((".HK", ".SZ", ".SH")):
        raw = raw[:-3]
    if raw.isdigit() and len(raw) <= 5:
        return raw.zfill(5)
    return raw


def stock_code_conflict(code: str, source: dict[str, Any]) -> tuple[str, str]:
    """Return ``(raw_stock_code, error_message)`` if parsed stock_code conflicts with ``code``."""
    comp_norm = _norm_stock_code(code)
    if not comp_norm or not isinstance(source, dict):
        return "", ""
    candidates: list[Any] = [source.get("stock_code")]
    for field in ("groups", "buybacks", "classes"):
        rows = source.get(field)
        if isinstance(rows, list):
            for grp in rows:
                if isinstance(grp, dict):
                    candidates.append(grp.get("stock_code"))
    ndr_obj = source.get("ndr")
    if isinstance(ndr_obj, dict):
        for field in ("buybacks", "classes"):
            rows = ndr_obj.get(field)
            if isinstance(rows, list):
                for grp in rows:
                    if isinstance(grp, dict):
                        candidates.append(grp.get("stock_code"))
    for raw_sc in candidates:
        sc_norm = _norm_stock_code(raw_sc)
        if sc_norm and sc_norm != comp_norm:
            raw_str = str(raw_sc).strip()
            return raw_str, f"证券代号（{raw_str}）与所属公司代码（{code}）不一致"
    return "", ""


class MissingPdfDependencyError(RuntimeError):
    """Raised when neither pdfplumber nor pypdf is installed."""


def _load_sibling(module_alias: str, filename: str) -> Any:
    existing = sys.modules.get(module_alias)
    here = Path(__file__).resolve().parent
    target = here / filename
    if existing is not None and getattr(existing, "__file__", None) == str(target):
        return existing
    spec = importlib.util.spec_from_file_location(module_alias, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"missing {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_alias] = module
    spec.loader.exec_module(module)
    return module


def _netio() -> Any:
    return _load_sibling("dalton_daily_filing_netio", "netio.py")


def _cn_ndr() -> Any:
    return _load_sibling("dalton_daily_filing_cn_ndr", "cn_ndr.py")


def extract_pdf_text(raw_bytes: bytes) -> str:
    """Extract PDF text using pdfplumber first, falling back to pypdf."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        pass
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError as exc:
        raise MissingPdfDependencyError(
            "缺少 PDF 解析依赖（需要 pdfplumber 或 pypdf）"
        ) from exc


def fetch_text(url: str) -> str:
    """Fetch a PDF over netio (preserving skill proxy/timeout) and extract text."""
    clean_url = (url or "").strip()
    if not clean_url.startswith(("https://", "http://")):
        raise ValueError("仅支持 http/https URL")
    payload = _netio().fetch_bytes(
        clean_url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    return extract_pdf_text(payload)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _norm_share_class(share_class: str | None) -> str:
    text = (share_class or "").strip()
    return "" if text in _DEFAULT_CLASSES else text


def _class_prefix(share_class: str | None, cur: str, multi_group: bool) -> str:
    if not multi_group:
        return ""
    norm = _norm_share_class(share_class)
    if norm in ("A", "H"):
        return f"{norm}股："
    if norm:
        return f"{norm}："
    return f"{CUR_CN.get(cur, cur)}："


def _build_group(
    *,
    trades: list[dict[str, Any]],
    total_shares: int,
    total_shares_raw: str,
    total_amount: float,
    total_amount_raw: str,
    currency: str,
    share_class: str | None,
    stock_code: str | None,
    to_cancel: int,
    to_cancel_raw: str | None,
    to_treasury: int,
    to_treasury_raw: str | None,
    mandate_cum_shares: int | None,
    mandate_cum_shares_raw: str | None,
    mandate_cum_pct: float | None,
    mandate_cum_pct_raw: str | None,
) -> dict[str, Any]:
    avg_price = round(total_amount / total_shares, 2) if total_shares else 0.0
    lo_trade = min(trades, key=lambda t: t["lo"])
    hi_trade = max(trades, key=lambda t: t["hi"])
    out_trades = []
    for t in trades:
        entry = {
            "date": t["date"],
            "shares": t["shares"],
            "hi": t["hi"],
            "lo": t["lo"],
            "amount": t["amount"],
            "avg": t["avg"],
            "currency": t["currency"],
        }
        if t.get("venue") is not None:
            entry["venue"] = t["venue"]
        out_trades.append(entry)
    return {
        "share_class": _norm_share_class(share_class) or None,
        "stock_code": stock_code,
        "trades": out_trades,
        "total_shares": total_shares,
        "total_shares_raw": total_shares_raw,
        "total_amount": total_amount,
        "total_amount_raw": total_amount_raw,
        "currency": currency,
        "avg_price": avg_price,
        "hi": hi_trade["hi"],
        "hi_raw": hi_trade["hi_raw"],
        "lo": lo_trade["lo"],
        "lo_raw": lo_trade["lo_raw"],
        "to_cancel": to_cancel,
        "to_cancel_raw": to_cancel_raw,
        "to_treasury": to_treasury,
        "to_treasury_raw": to_treasury_raw,
        "mandate_cum_shares": mandate_cum_shares,
        "mandate_cum_shares_raw": mandate_cum_shares_raw,
        "mandate_cum_pct": mandate_cum_pct,
        "mandate_cum_pct_raw": mandate_cum_pct_raw,
    }


def _format_group_line(group: dict[str, Any], *, multi_group: bool) -> str:
    cur = group["currency"]
    cur_label = CUR_CN.get(cur, cur)
    days = "、".join(dict.fromkeys(t["date"][5:] for t in group["trades"]))
    prefix = _class_prefix(group.get("share_class"), cur, multi_group)
    lo_str = group.get("lo_raw") or f"{group['lo']:g}"
    hi_str = group.get("hi_raw") or f"{group['hi']:g}"
    line = (
        f"{prefix}{days} 回购 {group['total_shares_raw']} 股，"
        f"金额 {group['total_amount_raw']} {cur_label}，"
        f"均价 {group['avg_price']:.2f} {cur_label}（区间 {lo_str}–{hi_str}）"
    )
    to_cancel = group.get("to_cancel") or 0
    to_treasury = group.get("to_treasury") or 0
    cancel_str = group.get("to_cancel_raw") or f"{to_cancel:,}"
    treasury_str = group.get("to_treasury_raw") or f"{to_treasury:,}"
    dest: list[str] = []
    if to_cancel and to_treasury:
        dest.append(f"{cancel_str} 股拟注销")
        dest.append(f"{treasury_str} 股持作库存股")
    elif to_cancel:
        dest.append("拟注销")
    elif to_treasury:
        dest.append(f"{treasury_str} 股持作库存股")
    if dest:
        line += "，" + "，".join(dest)
    if group.get("mandate_cum_shares"):
        cum_str = group.get("mandate_cum_shares_raw") or f"{group['mandate_cum_shares']:,}"
        line += f"；本次授权累计回购 {cum_str} 股"
        if group.get("mandate_cum_pct") is not None:
            pct_str = group.get("mandate_cum_pct_raw") or f"{group['mandate_cum_pct']:.2f}"
            line += f"（占股本 {pct_str}%）"
    return line


def _normalize_trades(
    raw_trades: list[dict[str, Any]],
    fallback_shares: int | None,
    *,
    include_venue: bool = False,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    single_no_shares = len(raw_trades) == 1 and raw_trades[0].get("shares") is None
    for row in raw_trades:
        shares = int(row["shares"]) if row.get("shares") is not None else None
        amount = float(row["amount"])
        denom = shares if shares is not None else (fallback_shares if single_no_shares else None)
        entry: dict[str, Any] = {
            "date": row["date"],
            "shares": shares,
            "hi": float(row["price_high"]),
            "hi_raw": row["price_high_raw"],
            "lo": float(row["price_low"]),
            "lo_raw": row["price_low_raw"],
            "amount": amount,
            "amount_raw": row["amount_raw"],
            "avg": round(amount / denom, 2) if denom else None,
            "currency": row["currency"],
        }
        if include_venue and row.get("venue"):
            entry["venue"] = row["venue"]
        trades.append(entry)
    return trades


def _check_disposition_consistency(
    report: dict[str, Any],
    total_shares: int,
    *,
    currency: str | None = None,
) -> dict[str, Any] | None:
    to_cancel = int(report.get("to_cancel") or 0)
    to_treasury = int(report.get("to_treasury") or 0)
    has_disposition = bool(
        report.get("to_cancel_matched")
        or report.get("to_treasury_matched")
        or report.get("has_disposition_section")
    )
    if has_disposition and (to_cancel + to_treasury != total_shares):
        disc: dict[str, Any] = {
            "field": "share_disposition",
            "total_shares": total_shares,
            "to_cancel": to_cancel,
            "to_treasury": to_treasury,
            "sum_disposition": to_cancel + to_treasury,
            "share_class": report.get("share_class"),
        }
        if currency:
            disc["currency"] = currency
        return {
            "error": f"合计股数（{total_shares:,}）与拟注销（{to_cancel:,}）及库存股（{to_treasury:,}）之和不一致",
            "discrepancy": disc,
        }
    return None


def _parse_single_currency_block(
    report: dict[str, Any],
    *,
    include_venue: bool = False,
) -> dict[str, Any]:
    raw_trades = report["trades"]
    cur = raw_trades[0]["currency"]
    share_class = report.get("share_class")
    inline_shares = [int(t["shares"]) for t in raw_trades if t.get("shares") is not None]
    rep_shares = int(report["total_shares"]) if report.get("total_shares") is not None else None
    rep_shares_raw = report.get("total_shares_raw")

    if rep_shares is None:
        return {
            "error": "未找到合共購回股份總數",
            "discrepancy": {
                "field": "total_shares",
                "sum_trades": sum(inline_shares) if len(inline_shares) == len(raw_trades) else None,
                "reported_total": None,
                "currency": cur,
                "share_class": share_class,
            },
        }

    if inline_shares and len(inline_shares) < len(raw_trades):
        return {
            "error": "部分交易行缺少股数，无法与合共股数核对",
            "discrepancy": {
                "field": "total_shares",
                "sum_trades": sum(inline_shares),
                "reported_total": rep_shares,
                "currency": cur,
                "share_class": share_class,
            },
        }
    if len(inline_shares) == len(raw_trades):
        sum_sh = sum(inline_shares)
        if sum_sh != rep_shares:
            return {
                "error": f"逐行股数合计（{sum_sh:,}）与合共股数（{rep_shares:,}）不一致",
                "discrepancy": {
                    "field": "total_shares",
                    "sum_trades": sum_sh,
                    "reported_total": rep_shares,
                    "currency": cur,
                    "share_class": share_class,
                },
            }
    tot_sh = rep_shares
    tot_sh_raw = rep_shares_raw or f"{rep_shares:,}"

    disp_err = _check_disposition_consistency(report, tot_sh, currency=cur)
    if disp_err is not None:
        return disp_err

    sum_amt = sum((_dec(t["amount_raw"]) for t in raw_trades), Decimal(0))
    amt_matches = report.get("total_amount_matches") or []
    if not amt_matches:
        return {
            "error": "未找到合共付出的價格總額",
            "discrepancy": {
                "field": "total_amount",
                "sum_trades": float(sum_amt),
                "reported_total": None,
                "currency": cur,
                "share_class": share_class,
            },
        }
    rep_cur = amt_matches[0]["currency"]
    rep_amt = _dec(amt_matches[0]["raw"])
    rep_amt_raw = amt_matches[0]["raw"]
    if rep_cur != cur:
        return {
            "error": f"交易币种（{cur}）与合共币种（{rep_cur}）不一致",
            "discrepancy": {
                "field": "currency",
                "trade_currency": cur,
                "reported_currency": rep_cur,
                "share_class": share_class,
            },
        }
    if abs(sum_amt - rep_amt) > Decimal("0.02"):
        return {
            "error": f"逐行金额合计（{sum_amt:,.2f}）与合共金额（{rep_amt:,.2f}）不一致",
            "discrepancy": {
                "field": "total_amount",
                "sum_trades": float(sum_amt),
                "reported_total": float(rep_amt),
                "currency": cur,
                "share_class": share_class,
            },
        }
    tot_amt = float(rep_amt)
    tot_amt_raw = rep_amt_raw

    trades = _normalize_trades(raw_trades, tot_sh, include_venue=include_venue)
    return _build_group(
        trades=trades,
        total_shares=tot_sh,
        total_shares_raw=tot_sh_raw,
        total_amount=tot_amt,
        total_amount_raw=tot_amt_raw,
        currency=cur,
        share_class=share_class,
        stock_code=report.get("stock_code"),
        to_cancel=int(report.get("to_cancel") or 0),
        to_cancel_raw=report.get("to_cancel_raw"),
        to_treasury=int(report.get("to_treasury") or 0),
        to_treasury_raw=report.get("to_treasury_raw"),
        mandate_cum_shares=int(report["mandate_cum_shares"]) if report.get("mandate_cum_shares") is not None else None,
        mandate_cum_shares_raw=report.get("mandate_cum_shares_raw"),
        mandate_cum_pct=float(report["mandate_cum_pct"]) if report.get("mandate_cum_pct") is not None else None,
        mandate_cum_pct_raw=report.get("mandate_cum_pct_raw"),
    )


def _parse_multi_currency_block(
    report: dict[str, Any],
    *,
    include_venue: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    raw_trades = report["trades"]
    share_class = report.get("share_class")
    rep_shares = int(report["total_shares"]) if report.get("total_shares") is not None else None
    inline_shares = [int(t["shares"]) for t in raw_trades if t.get("shares") is not None]
    if rep_shares is None:
        return {
            "error": "未找到合共購回股份總數",
            "discrepancy": {
                "field": "total_shares",
                "sum_trades": sum(inline_shares) if len(inline_shares) == len(raw_trades) else None,
                "reported_total": None,
                "share_class": share_class,
            },
        }
    if len(inline_shares) < len(raw_trades):
        return {
            "error": "存在多币种交易行且缺少逐行股数，无法核对合计",
            "discrepancy": {
                "field": "total_shares",
                "sum_trades": sum(inline_shares),
                "reported_total": rep_shares,
                "share_class": share_class,
            },
        }
    sum_sh = sum(inline_shares)
    if sum_sh != rep_shares:
        return {
            "error": f"逐行股数合计（{sum_sh:,}）与合共股数（{rep_shares:,}）不一致",
            "discrepancy": {
                "field": "total_shares",
                "sum_trades": sum_sh,
                "reported_total": rep_shares,
                "share_class": share_class,
            },
        }
    disp_err = _check_disposition_consistency(report, rep_shares)
    if disp_err is not None:
        return disp_err

    amt_by_cur = {m["currency"]: m for m in (report.get("total_amount_matches") or [])}
    currencies = list(dict.fromkeys(t["currency"] for t in raw_trades))
    if not amt_by_cur or any(cur not in amt_by_cur for cur in currencies):
        return {
            "error": "缺少合共付出的價格總額或币种不完整，请按股份类别分别核对",
            "discrepancy": {
                "field": "total_amount",
                "trade_currencies": currencies,
                "reported_currencies": list(amt_by_cur),
                "share_class": share_class,
            },
        }

    groups: list[dict[str, Any]] = []
    for cur in currencies:
        cur_trades = [t for t in raw_trades if t["currency"] == cur]
        tot_sh = sum(int(t["shares"]) for t in cur_trades)
        sum_amt = sum((_dec(t["amount_raw"]) for t in cur_trades), Decimal(0))
        rep_amt = _dec(amt_by_cur[cur]["raw"])
        if abs(sum_amt - rep_amt) > Decimal("0.02"):
            return {
                "error": f"{cur} 逐行金额合计（{sum_amt:,.2f}）与合共金额（{rep_amt:,.2f}）不一致",
                "discrepancy": {
                    "field": "total_amount",
                    "sum_trades": float(sum_amt),
                    "reported_total": float(rep_amt),
                    "currency": cur,
                    "share_class": share_class,
                },
            }
        tot_amt = float(rep_amt)
        tot_amt_raw = amt_by_cur[cur]["raw"]
        trades = _normalize_trades(cur_trades, tot_sh, include_venue=include_venue)
        groups.append(
            _build_group(
                trades=trades,
                total_shares=tot_sh,
                total_shares_raw=f"{tot_sh:,}",
                total_amount=tot_amt,
                total_amount_raw=tot_amt_raw,
                currency=cur,
                share_class=share_class,
                stock_code=report.get("stock_code"),
                to_cancel=0,
                to_cancel_raw=None,
                to_treasury=0,
                to_treasury_raw=None,
                mandate_cum_shares=None,
                mandate_cum_shares_raw=None,
                mandate_cum_pct=None,
                mandate_cum_pct_raw=None,
            )
        )
    return groups


def _extract_filing_meta(text: str, ndr: Any) -> dict[str, Any]:
    head = (text or "")[:800]
    meta: dict[str, Any] = {}
    submitted_m = re.search(r"呈交日期[：:]\s*" + ndr.DATE_RE, head)
    if submitted_m:
        submitted_date = ndr._date(submitted_m)
        if submitted_date:
            meta["submitted"] = submitted_date
    status_m = re.search(r"[狀状]\s*[態态][：:]\s*([^\n]+)", head)
    first_lines = "\n".join(line.strip() for line in (text or "").splitlines()[:3] if line.strip())
    if (status_m and _AMENDMENT_RE.search(status_m.group(1))) or _AMENDMENT_RE.search(first_lines):
        meta["is_amendment"] = True
    return meta


def _parse_validated(text: str, *, include_venue: bool = False) -> dict[str, Any]:
    """Parse and validate Section II buyback trades and totals from FF305 text via cn_ndr."""
    ndr = _cn_ndr()
    reports = ndr.parse_section_two_buybacks(text or "")
    if not reports:
        return {"error": "未解析到第二章節购回明细（可能不是购回类翌日披露）"}

    groups: list[dict[str, Any]] = []
    for report in reports:
        num_errors = report.get("number_errors") or []
        if num_errors:
            first_err = num_errors[0]
            msg = (
                f"第二章節交易行解析不完整或格式异常: {first_err['raw']}"
                if first_err["field"] == "trades"
                else f"数字格式不完整或异常: {first_err['raw']}"
            )
            return {
                "error": msg,
                "discrepancy": {
                    "field": first_err["field"],
                    "raw": first_err["raw"],
                    "share_class": report.get("share_class"),
                },
            }
        raw_trades = report.get("trades") or []
        if not raw_trades:
            return {
                "error": "有合共股数但未解析到交易行",
                "discrepancy": {
                    "field": "trades",
                    "sum_trades": 0,
                    "reported_total": report.get("total_shares"),
                    "share_class": report.get("share_class"),
                },
            }
        currencies = list(dict.fromkeys(t["currency"] for t in raw_trades))
        if len(currencies) == 1:
            parsed_block = _parse_single_currency_block(report, include_venue=include_venue)
            if "error" in parsed_block:
                return parsed_block
            groups.append(parsed_block)
        else:
            parsed_multi = _parse_multi_currency_block(report, include_venue=include_venue)
            if isinstance(parsed_multi, dict) and "error" in parsed_multi:
                return parsed_multi
            groups.extend(parsed_multi)

    if not groups:
        return {"error": "未解析到第二章節购回明细（可能不是购回类翌日披露）"}

    multi_group = len(groups) > 1
    public_groups: list[dict[str, Any]] = []
    for grp in groups:
        line = _format_group_line(grp, multi_group=multi_group)
        clean = {k: v for k, v in grp.items() if v is not None or k in ("share_class", "stock_code", "mandate_cum_shares", "mandate_cum_pct")}
        clean["line"] = line
        public_groups.append(clean)

    filing_meta = _extract_filing_meta(text or "", ndr)
    if include_venue:
        return {"groups": public_groups, **filing_meta}
    if len(public_groups) == 1:
        single = dict(public_groups[0])
        single["groups"] = public_groups
        single.update(filing_meta)
        return single

    out: dict[str, Any] = {
        "groups": public_groups,
        "trades": [t for g in public_groups for t in g["trades"]],
        "line": "；".join(g["line"] for g in public_groups),
    }
    out.update(filing_meta)
    return out


def parse(text: str) -> dict[str, Any]:
    """Parse Section II buyback trades and totals from FF305 text via cn_ndr."""
    return _parse_validated(text, include_venue=False)


def parse_for_ndr(text: str) -> dict[str, Any]:
    """Parse Section II buyback groups preserving trade venues for cn_ndr digests."""
    return _parse_validated(text, include_venue=True)


def scan_record_from_parsed(
    parsed: dict[str, Any],
    *,
    ann_id: str = "",
    url: str = "",
    code: str = "",
    date: str = "",
    title: str = "",
    is_amendment: bool = False,
) -> dict[str, Any]:
    """Normalize a buyback_parse result into a scan-output buyback_results record."""
    amend = bool(is_amendment or parsed.get("is_amendment") or _AMENDMENT_RE.search(title or ""))
    rec_id = ann_id or url
    if "error" in parsed:
        out: dict[str, Any] = {
            "id": rec_id,
            "url": url,
            "code": code,
            "date": date,
            "title": title,
            "validated": False,
            "is_buyback": True,
            "is_amendment": amend,
            "error": str(parsed["error"]),
        }
        if parsed.get("stock_code"):
            out["stock_code"] = parsed["stock_code"]
        if "discrepancy" in parsed:
            out["discrepancy"] = parsed["discrepancy"]
        return out
    raw_sc, conflict_err = stock_code_conflict(code, parsed)
    if conflict_err:
        return {
            "id": rec_id,
            "url": url,
            "code": code,
            "date": date,
            "title": title,
            "validated": False,
            "is_buyback": True,
            "is_amendment": amend,
            "stock_code": raw_sc,
            "error": conflict_err,
        }
    out = {
        "id": rec_id,
        "url": url,
        "code": code,
        "date": date,
        "title": title,
        "validated": True,
        "is_buyback": True,
        "is_amendment": amend,
        "groups": list(parsed.get("groups") or []),
        "trades": list(parsed.get("trades") or []),
    }
    for field in (
        "total_shares",
        "total_shares_raw",
        "total_amount",
        "total_amount_raw",
        "currency",
        "avg_price",
        "hi",
        "hi_raw",
        "lo",
        "lo_raw",
        "to_cancel",
        "to_cancel_raw",
        "to_treasury",
        "to_treasury_raw",
        "mandate_cum_shares",
        "mandate_cum_shares_raw",
        "mandate_cum_pct",
        "mandate_cum_pct_raw",
        "share_class",
        "stock_code",
        "submitted",
        "line",
    ):
        if field in parsed and parsed[field] is not None:
            out[field] = parsed[field]
    return out


def validate_for_scan(
    text: str,
    *,
    ann_id: str = "",
    url: str = "",
    code: str = "",
    date: str = "",
    title: str = "",
    ndr_data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run parser validation and return ``(raw_parse_result, scan_record)``."""
    ndr = _cn_ndr()
    filing_meta = _extract_filing_meta(text or "", ndr)
    amend = bool(filing_meta.get("is_amendment") or _AMENDMENT_RE.search(title or ""))
    parsed = parse(text)
    if (
        "error" not in parsed
        and ndr_data is None
        and "翌日披露報表" in (text or "")[:400]
        and ("第一章節" in (text or "") or "第一章节" in (text or ""))
    ):
        try:
            ndr.parse_ndr(text)
        except ValueError as exc:
            err_parsed = {"error": str(exc)}
            return err_parsed, scan_record_from_parsed(
                err_parsed,
                ann_id=ann_id,
                url=url,
                code=code,
                date=date,
                title=title,
                is_amendment=amend,
            )
    if "error" in parsed and "未解析到第二章節购回明细" in str(parsed["error"]) and "翌日披露報表" in (text or "")[:400]:
        ndr_err: str | None = None
        try:
            ndr_res = ndr_data if isinstance(ndr_data, dict) else ndr.parse_ndr(text)
        except ValueError as exc:
            ndr_res = None
            ndr_err = str(exc)
        except Exception:  # noqa: BLE001 - fall back to buyback parse error
            ndr_res = None
        if isinstance(ndr_res, dict) and ndr_res.get("buybacks") == []:
            classes_meta: list[dict[str, Any]] = []
            stock_codes: list[str] = []
            for cls in ndr_res.get("classes") or []:
                if not isinstance(cls, dict):
                    continue
                cls_sc = str(cls.get("stock_code") or "").strip() or None
                cls_entry: dict[str, Any] = {
                    "share_class": cls.get("share_class"),
                    "stock_code": cls_sc,
                }
                if cls.get("description"):
                    cls_entry["description"] = cls["description"]
                classes_meta.append(cls_entry)
                if cls_sc and _norm_stock_code(cls_sc):
                    stock_codes.append(cls_sc)
            raw_sc, conflict_err = stock_code_conflict(code, ndr_res)
            if conflict_err:
                rec = {
                    "id": ann_id or url,
                    "url": url,
                    "code": code,
                    "date": date,
                    "title": title,
                    "validated": False,
                    "is_buyback": False,
                    "is_amendment": amend,
                    "stock_code": raw_sc,
                    "error": conflict_err,
                }
                if classes_meta:
                    rec["classes"] = classes_meta
                return parsed, rec
            rec = {
                "id": ann_id or url,
                "url": url,
                "code": code,
                "date": date,
                "title": title,
                "validated": True,
                "is_buyback": False,
                "is_amendment": amend,
                "digest": str(ndr_res.get("digest") or ""),
            }
            if classes_meta:
                rec["classes"] = classes_meta
            if stock_codes:
                rec["stock_code"] = stock_codes[0]
            return parsed, rec
        if ndr_err:
            err_parsed = {"error": ndr_err}
            return err_parsed, scan_record_from_parsed(
                err_parsed,
                ann_id=ann_id,
                url=url,
                code=code,
                date=date,
                title=title,
                is_amendment=amend,
            )
    return parsed, scan_record_from_parsed(
        parsed,
        ann_id=ann_id,
        url=url,
        code=code,
        date=date,
        title=title,
        is_amendment=amend,
    )


def self_check() -> None:
    """Offline checks for FF305 buyback parsing, cn_ndr samples, number integrity, and cross-check."""
    ndr = _cn_ndr()
    treasury = parse(ndr.SAMPLE_TREASURY)
    if treasury.get("total_shares") != 856_980 or treasury.get("total_amount") != 794_631_549.33:
        raise AssertionError("buyback_parse SAMPLE_TREASURY totals")
    if treasury.get("to_treasury") != 856_980 or treasury.get("to_cancel") != 0:
        raise AssertionError("buyback_parse SAMPLE_TREASURY dest")
    for needle in ("856,980 股", "794,631,549.33 元人民币", "均价 927.25 元人民币", "856,980 股持作库存股"):
        if needle not in treasury.get("line", ""):
            raise AssertionError(f"buyback_parse SAMPLE_TREASURY line missing {needle}")

    cancel = parse(ndr.SAMPLE_CANCEL)
    if cancel.get("total_shares") != 665_060 or cancel.get("total_amount") != 199_987_058.39:
        raise AssertionError("buyback_parse SAMPLE_CANCEL totals")
    if cancel.get("to_cancel") != 665_060 or cancel.get("to_treasury") != 0:
        raise AssertionError("buyback_parse SAMPLE_CANCEL dest")
    for needle in ("665,060 股", "199,987,058.39 元人民币", "均价 300.71 元人民币", "拟注销"):
        if needle not in cancel.get("line", ""):
            raise AssertionError(f"buyback_parse SAMPLE_CANCEL line missing {needle}")

    wrapped = """翌日披露報表
第二章節
A. 購回報告
1). 2026年11月15日 10,001 於香港聯合交易所
進行 HKD 125.00 HKD 120.00 HKD 1,234,567.89
合共購回股份總數 10,001
合共付出的價格總額 (元) HKD 1,234,567.89
購回股份（擬註銷）數目
10,001
購回股份（擬持作庫存股份）
0
"""
    exact = parse(wrapped)
    if exact.get("total_shares") != 10_001 or exact.get("to_cancel") != 10_001:
        raise AssertionError("buyback_parse wrapped line")
    for needle in ("10,001 股", "1,234,567.89 港元", "均价 123.44 港元", "拟注销"):
        if needle not in exact.get("line", ""):
            raise AssertionError(f"buyback_parse exact precision missing {needle}: {exact.get('line')}")

    compact = """翌日披露報表
第二章節
A. 購回報告
1). 2026年11月16日 340,000於本交易所進行 HKD 144.8HKD 143.4HKD 48,972,020
合共購回股份總數 340,000 合共付出的價格總額 (元) HKD 48,972,020
購回股份（擬註銷）數目 340,000
購回股份（擬持作庫存股份）
0
數目
"""
    parsed_compact = parse(compact)
    if (
        len(parsed_compact.get("trades") or []) != 1
        or parsed_compact.get("total_shares") != 340_000
        or parsed_compact.get("total_amount") != 48_972_020.0
        or parsed_compact.get("to_cancel") != 340_000
        or parsed_compact.get("to_treasury") != 0
    ):
        raise AssertionError("buyback_parse compact no-space line")
    for needle in ("340,000 股", "48,972,020 港元", "143.4–144.8", "拟注销"):
        if needle not in parsed_compact.get("line", ""):
            raise AssertionError(f"buyback_parse compact missing {needle}: {parsed_compact.get('line')}")

    mismatch = wrapped.replace("合共購回股份總數 10,001", "合共購回股份總數 20,001")
    bad_sum = parse(mismatch)
    if "error" not in bad_sum or "discrepancy" not in bad_sum or "total_shares" in bad_sum:
        raise AssertionError("buyback_parse discrepancy check")

    bad = parse("證券變動月報表")
    if "error" not in bad:
        raise AssertionError("buyback_parse non-buyback should error")

    no_total_shares = wrapped.replace("合共購回股份總數 10,001\n", "")
    if "error" not in parse(no_total_shares):
        raise AssertionError("buyback_parse missing total_shares should error")

    no_total_amount = wrapped.replace("合共付出的價格總額 (元) HKD 1,234,567.89\n", "")
    if "error" not in parse(no_total_amount):
        raise AssertionError("buyback_parse missing total_amount should error")

    wrapped_label = ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", "合共購回股份\n總數 856,980")
    if parse(wrapped_label).get("total_shares") != 856_980:
        raise AssertionError("buyback_parse wrapped label")

    for broken_text in (
        ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", "合共購回股份總數 856,\n980"),
        ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", "合共購回股份總數 856\n980").replace(
            "購回股份（擬持作庫存股份） 856,980", "購回股份（擬持作庫存股份） 856\n980"
        ),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\n631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\u00a0631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\u202f631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\u3000631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\f631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\u2060631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\u200b631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\u200c631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\u200d631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\ufeff631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794,631,549\n.33"),
        ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", "合共購回股份總數 856,\n"),
    ):
        if "error" not in parse(broken_text):
            raise AssertionError("buyback_parse broken number must error")

    bad_disp = wrapped.replace("購回股份（擬註銷）數目\n10,001", "購回股份（擬註銷）數目\n9,000")
    if "error" not in parse(bad_disp):
        raise AssertionError("buyback_parse disposition mismatch must error")

    verbatim = parse(compact.replace("合共付出的價格總額 (元) HKD 48,972,020", "合共付出的價格總額 (元) HKD 48,972,020.01"))
    if verbatim.get("total_amount_raw") != "48,972,020.01" or "48,972,020.01 港元" not in verbatim.get("line", ""):
        raise AssertionError("buyback_parse verbatim total_amount_raw")

    _opt_raw, opt_conflict_rec = validate_for_scan(ndr.SAMPLE_OPTION, code="00700")
    if opt_conflict_rec.get("validated") is not False or opt_conflict_rec.get("stock_code") != "09992":
        raise AssertionError("buyback_parse non-buyback NDR stock_code conflict")
    _bad_raw, opt_bad_num_rec = validate_for_scan(ndr.SAMPLE_OPTION.replace("15.5", "15. 5"), code="09992")
    if opt_bad_num_rec.get("validated") is not False:
        raise AssertionError("buyback_parse non-buyback NDR broken number must fail")
    _split_raw, opt_split_rec = validate_for_scan(
        ndr.SAMPLE_OPTION.replace("1). 根據股份期權計劃行使期權", "1).\n根據股份期權計劃行使期權"),
        code="09992",
    )
    if opt_split_rec.get("validated") is not False:
        raise AssertionError("buyback_parse split Section I marker must fail")
    _sec2_split_raw, sec2_split_rec = validate_for_scan(
        ndr.SAMPLE_TREASURY.replace("1). 2026年9月23日", "1).\n2026年9月23日"),
        code="300308",
    )
    if sec2_split_rec.get("validated") is not False:
        raise AssertionError("buyback_parse split Section II marker must fail")


def _emit_error(message: str, *, as_list: bool = True) -> int:
    payload: Any = [{"error": message}] if as_list else {"error": message}
    print(json.dumps(payload, ensure_ascii=False))
    return 1


def _merge_scan_output(
    workspace_dir: str,
    scan_output: str,
    records: list[dict[str, Any]],
) -> None:
    card_io = _load_sibling("dalton_daily_filing_card_io", "card_io.py")
    ws_path = Path(workspace_dir)
    if not workspace_dir or ws_path.is_symlink() or not ws_path.is_dir():
        raise ValueError("invalid --workspace-dir")
    workspace = ws_path.resolve()
    target = card_io.workspace_file(workspace, scan_output)
    if target.exists():
        if target.is_symlink():
            raise ValueError("refusing symlink")
        doc = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("scan output must be a JSON object")
    else:
        doc = {"ok": True, "results": [], "buyback_results": {}}

    ann_by_key: dict[str, dict[str, Any]] = {}
    res_by_key: dict[str, dict[str, Any]] = {}
    for res_row in doc.get("results") or []:
        if not isinstance(res_row, dict):
            continue
        for ann in res_row.get("announcements") or []:
            if not isinstance(ann, dict):
                continue
            for field in ("id", "url", "pdf_url"):
                tok = str(ann.get(field) or "").strip()
                if tok:
                    ann_by_key[tok] = ann
                    res_by_key[tok] = res_row

    bucket = doc.setdefault("buyback_results", {})
    if not isinstance(bucket, dict):
        bucket = {}
        doc["buyback_results"] = bucket
    for rec in records:
        raw_url = str(rec.get("url") or "").strip()
        key = str(rec.get("id") or raw_url or "").strip()
        matched_ann = ann_by_key.get(key) or ann_by_key.get(raw_url)
        matched_res = res_by_key.get(key) or res_by_key.get(raw_url)
        if matched_ann is not None:
            ann_id = str(matched_ann.get("id") or "").strip()
            if ann_id:
                rec["id"] = ann_id
                key = ann_id
            if not rec.get("url") and matched_ann.get("url"):
                rec["url"] = matched_ann["url"]
            if not rec.get("code") and matched_ann.get("code"):
                rec["code"] = matched_ann["code"]
            if not rec.get("date") and matched_ann.get("date"):
                rec["date"] = matched_ann["date"]
            if not rec.get("title") and matched_ann.get("title"):
                rec["title"] = matched_ann["title"]
            if _AMENDMENT_RE.search(str(rec.get("title") or "")):
                rec["is_amendment"] = True
        rec_code = str(rec.get("code") or "").strip()
        is_conflict = False
        if rec_code:
            raw_sc, conflict_err = stock_code_conflict(rec_code, rec)
            if conflict_err:
                is_conflict = True
                rec_classes = rec.get("classes")
                rec = {
                    "id": key or str(rec.get("id") or raw_url),
                    "url": str(rec.get("url") or raw_url),
                    "code": rec_code,
                    "date": str(rec.get("date") or ""),
                    "title": str(rec.get("title") or ""),
                    "validated": False,
                    "is_buyback": bool(rec.get("is_buyback", True)),
                    "is_amendment": bool(rec.get("is_amendment")),
                    "stock_code": raw_sc,
                    "error": conflict_err,
                }
                if isinstance(rec_classes, list) and rec_classes:
                    rec["classes"] = rec_classes
                desc = f"{rec['date']} {rec['title'] or '未命名公告'}".strip()
                err_line = f"证券代号冲突，金额未核对: {desc}（{conflict_err}）"
                if matched_res is not None:
                    res_errs = matched_res.setdefault("errors", [])
                    if isinstance(res_errs, list) and err_line not in res_errs:
                        res_errs.append(err_line)
                day_errs = doc.setdefault("day_errors", [])
                day_line = f"{rec_code}: {err_line}"
                if isinstance(day_errs, list) and day_line not in day_errs:
                    day_errs.append(day_line)
        if key:
            existing = bucket.get(key) or (bucket.get(raw_url) if raw_url else None)
            if (
                not is_conflict
                and isinstance(existing, dict)
                and existing.get("validated") is True
                and rec.get("validated") is not True
            ):
                continue
            bucket[key] = rec

    encoded = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    card_io.write_bytes_exclusive(target, encoded)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    workspace_dir = ""
    scan_output = ""
    text_json_path = ""
    pos_args: list[str] = []
    idx = 0
    while idx < len(raw_args):
        tok = raw_args[idx]
        if tok == "--workspace-dir":
            if idx + 1 >= len(raw_args):
                return _emit_error("缺少 --workspace-dir 参数")
            workspace_dir = raw_args[idx + 1]
            idx += 2
        elif tok == "--scan-output":
            if idx + 1 >= len(raw_args):
                return _emit_error("缺少 --scan-output 参数")
            scan_output = raw_args[idx + 1]
            idx += 2
        elif tok == "--text-json":
            if idx + 1 >= len(raw_args):
                return _emit_error("缺少或多余的 --text-json 文件路径")
            text_json_path = raw_args[idx + 1]
            idx += 2
        else:
            pos_args.append(tok)
            idx += 1

    res: list[dict[str, Any]] = []
    scan_records: list[dict[str, Any]] = []
    if text_json_path:
        if pos_args:
            return _emit_error("缺少或多余的 --text-json 文件路径")
        try:
            raw_text = Path(text_json_path).read_text(encoding="utf-8")
            raw_map = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            return _emit_error(f"读取 --text-json 失败: {exc}")
        if not isinstance(raw_map, dict):
            return _emit_error("--text-json 文件内容必须为 JSON 对象")
        for key, item in raw_map.items():
            text = item.get("text", "") if isinstance(item, dict) else str(item or "")
            meta = item if isinstance(item, dict) else {}
            row, rec = validate_for_scan(
                text,
                ann_id=str(meta.get("id") or key),
                url=str(meta.get("url") or ""),
                code=str(meta.get("code") or ""),
                date=str(meta.get("date") or ""),
                title=str(meta.get("title") or ""),
            )
            row["key"] = key
            for meta_key in ("title", "date", "submitted", "filing_date", "status", "is_amendment"):
                if meta_key in meta and meta[meta_key] not in (None, "") and meta_key not in row:
                    row[meta_key] = meta[meta_key]
            if row.get("is_amendment"):
                rec["is_amendment"] = True
            res.append(row)
            scan_records.append(rec)
    else:
        if not pos_args:
            return _emit_error("请传入 PDF URL 或 --text-json")
        for url in pos_args:
            try:
                fetched_text = fetch_text(url)
                row, rec = validate_for_scan(fetched_text, ann_id=url, url=url)
            except MissingPdfDependencyError as exc:
                row = {"error": str(exc)}
                rec = scan_record_from_parsed(row, ann_id=url, url=url)
            except Exception as exc:  # noqa: BLE001 - network/PDF failure returns per-URL error
                row = {"error": f"取正文失败: {exc}"}
                rec = scan_record_from_parsed(row, ann_id=url, url=url)
            row["url"] = url
            res.append(row)
            scan_records.append(rec)

    if scan_output:
        try:
            _merge_scan_output(workspace_dir, scan_output, scan_records)
        except Exception as exc:  # noqa: BLE001 - report workspace/IO error cleanly
            return _emit_error(f"写入 --scan-output 失败: {exc}")

    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
