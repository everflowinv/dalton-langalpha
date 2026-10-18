#!/usr/bin/env python3
"""Dalton Bull/Bear 财务体检脚本（无 edgartools 依赖，基于 us-gaap XBRL concepts）。

功能：
1. 资产负债表大额/跳变科目检测：
   - 写死无维度的资产负债表 us-gaap concept 清单（剔除现金、短投、有息负债、递延收入、股东权益）
   - 触发规则：占总资产 >= 10%（大额），或同比变动 >= 30% 且当期/上期金额至少占最新总资产 2%（跳变）
   - 取不到的科目记入“底表缺”，绝不静默填 0
2. Capex 强度与现金流三年序列：
   - Capex（PP&E）、无形资产投入、经营现金流（OCF）、净利润、D&A、SBC 费用、营业收入
   - 触发提示：Capex 同比 >= 20% 或 Capex 相当于净利润的 30% 以上
   - 附同年 D&A 对比与 SBC 占收入比例（供股东回报支线参考）

数据输入方式（按优先级）：
1. `--facts-json <path>`：传入 `dalton_sec.get_company_facts` 返回的 JSON，或 SEC EDGAR companyfacts 原始 JSON
2. 沙箱 PTC 环境内自动调用 `dalton_sec.get_company_facts`（若可导入）
3. 直连 SEC EDGAR API（`data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`，使用 `SEC_USER_AGENT` 或 `EDGAR_IDENTITY`）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import sys
import urllib.request
from typing import Any

# 资产负债表总资产锚点
TOTAL_ASSETS_CONCEPTS = ["us-gaap:Assets"]

# 写死的非现金/非短投/非有息债务/非递延收入/非权益资产负债表科目清单
BS_CONCEPTS: list[tuple[str, list[str]]] = [
    ("应收账款净额 (Accounts Receivable, Net)", [
        "us-gaap:AccountsReceivableNetCurrent",
        "us-gaap:ReceivablesNetCurrent",
    ]),
    ("存货净额 (Inventory, Net)", [
        "us-gaap:InventoryNet",
    ]),
    ("预付费用及其他流动资产 (Prepaid & Other Current Assets)", [
        "us-gaap:PrepaidExpenseAndOtherAssetsCurrent",
        "us-gaap:OtherAssetsCurrent",
    ]),
    ("不动产、厂房及设备净额 (PP&E, Net)", [
        "us-gaap:PropertyPlantAndEquipmentNet",
    ]),
    ("经营租赁使用权资产 (Operating Lease ROU Assets)", [
        "us-gaap:OperatingLeaseRightOfUseAsset",
    ]),
    ("商誉 (Goodwill)", [
        "us-gaap:Goodwill",
    ]),
    ("无形资产净额 (Intangible Assets, Net)", [
        "us-gaap:IntangibleAssetsNetExcludingGoodwill",
        "us-gaap:FiniteLivedIntangibleAssetsNet",
    ]),
    ("长期投资 (Long-Term Investments)", [
        "us-gaap:LongTermInvestments",
        "us-gaap:MarketableSecuritiesNoncurrent",
    ]),
    ("递延所得税资产 (Deferred Tax Assets)", [
        "us-gaap:DeferredIncomeTaxAssetsNet",
    ]),
    ("其他非流动资产 (Other Noncurrent Assets)", [
        "us-gaap:OtherAssetsNoncurrent",
    ]),
    ("应付账款 (Accounts Payable)", [
        "us-gaap:AccountsPayableCurrent",
    ]),
    ("应计负债及应付职工薪酬 (Accrued Liabilities)", [
        "us-gaap:AccruedLiabilitiesCurrent",
        "us-gaap:EmployeeRelatedLiabilitiesCurrent",
    ]),
    ("流动经营租赁负债 (Operating Lease Liability, Current)", [
        "us-gaap:OperatingLeaseLiabilityCurrent",
    ]),
    ("非流动经营租赁负债 (Operating Lease Liability, Noncurrent)", [
        "us-gaap:OperatingLeaseLiabilityNoncurrent",
    ]),
    ("递延所得税负债 (Deferred Tax Liabilities)", [
        "us-gaap:DeferredIncomeTaxLiabilitiesNet",
    ]),
    ("其他流动负债 (Other Current Liabilities)", [
        "us-gaap:OtherLiabilitiesCurrent",
    ]),
    ("其他非流动负债 (Other Noncurrent Liabilities)", [
        "us-gaap:OtherLiabilitiesNoncurrent",
    ]),
]

# 现金流与利润表科目清单
CF_CONCEPTS: dict[str, tuple[str, list[str]]] = {
    "capex": ("Capex (PP&E)", [
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap:PaymentsToAcquireProductiveAssets",
    ]),
    "capex_intangible": ("无形资产投入", [
        "us-gaap:PaymentsToAcquireIntangibleAssets",
    ]),
    "ocf": ("经营现金流 (OCF)", [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    ]),
    "net_income": ("净利润 (Net Income)", [
        "us-gaap:NetIncomeLoss",
        "us-gaap:ProfitLoss",
    ]),
    "da": ("折旧与摊销 (D&A)", [
        "us-gaap:DepreciationDepletionAndAmortization",
        "us-gaap:DepreciationAndAmortization",
        "us-gaap:DepreciationAmortizationAndAccretionNet",
    ]),
    "revenue": ("营业收入 (Revenue)", [
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:Revenues",
        "us-gaap:SalesRevenueNet",
    ]),
    "sbc": ("股权激励费用 (SBC)", [
        "us-gaap:ShareBasedCompensation",
        "us-gaap:AllocatedShareBasedCompensationExpense",
    ]),
}


def all_requested_concepts() -> list[str]:
    out = list(TOTAL_ASSETS_CONCEPTS)
    for _, cs in BS_CONCEPTS:
        for c in cs:
            if c not in out:
                out.append(c)
    for _, (_, cs) in CF_CONCEPTS.items():
        for c in cs:
            if c not in out:
                out.append(c)
    return out


def fmt(v: float | None) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "底表缺"
    return f"{v / 1e6:,.0f}"


def sp(v: float | None) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "N/A"
    return f"{v:.1f}%"


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    if any(isinstance(x, float) and (math.isnan(x) or math.isinf(x)) for x in (a, b)):
        return None
    return a / b * 100.0


def _days_between(start_s: str | None, end_s: str | None) -> int | None:
    if not start_s or not end_s:
        return None
    try:
        d0 = dt.date.fromisoformat(start_s[:10])
        d1 = dt.date.fromisoformat(end_s[:10])
        return (d1 - d0).days
    except ValueError:
        return None


def normalize_facts_payload(
    raw: dict[str, Any],
    currency: str = "USD",
    forms: tuple[str, ...] = ("10-K", "20-F"),
) -> tuple[dict[str, dict[str, float]], list[dict[str, str]]]:
    """将 dalton_sec.get_company_facts 返回值或 SEC EDGAR companyfacts JSON 归一为：
    concept -> {period_end_date: float_value}，以及 filings 元信息列表。
    对于资产负债表（instant）取年末时点值；对于现金流/利润表取年度（350–380 天或 duration=="12m"）值。
    """
    by_concept: dict[str, dict[str, tuple[str, float]]] = {}  # concept -> {end: (filed, val)}
    meta_by_acc: dict[str, dict[str, str]] = {}

    # 形态 1：dalton_sec.get_company_facts 的 {"data": [...]} 结构
    if isinstance(raw.get("data"), list):
        for pt in raw["data"]:
            if not isinstance(pt, dict):
                continue
            concept = str(pt.get("concept") or "")
            if not concept:
                continue
            unit = str(pt.get("unit") or "")
            if unit and currency and unit.upper() != currency.upper():
                continue
            form = str(pt.get("form") or "")
            if forms and form and form not in forms:
                continue
            dur = str(pt.get("duration") or "")
            p_start = pt.get("period_start")
            p_end = str(pt.get("period_end") or "")[:10]
            if not p_end:
                continue
            days = _days_between(p_start, p_end)
            # 只保留资产负债表时点值（instant / 无 start）或全年流量值（12m / 350-380天）
            is_instant = dur == "instant" or (p_start is None and dur in ("", "instant"))
            is_annual_flow = dur == "12m" or (days is not None and 350 <= days <= 380)
            if not (is_instant or is_annual_flow):
                continue
            val = pt.get("value")
            if val is None:
                continue
            filed = str(pt.get("filed") or "")
            acc = str(pt.get("accession") or "")
            if acc and acc not in meta_by_acc:
                meta_by_acc[acc] = {"accession": acc, "form": form or "10-K", "filing_date": filed, "report_date": p_end}
            cur = by_concept.setdefault(concept, {})
            prev_filed, _ = cur.get(p_end, ("", 0.0))
            if filed >= prev_filed:
                cur[p_end] = (filed, float(val))

    # 形态 2：SEC EDGAR companyfacts 原始 JSON {"facts": {"us-gaap": {"Assets": {"units": {"USD": [...]}}}}}
    elif isinstance(raw.get("facts"), dict):
        for taxo, concepts_map in raw["facts"].items():
            if not isinstance(concepts_map, dict):
                continue
            for cname, cobj in concepts_map.items():
                full_concept = f"{taxo}:{cname}"
                units_map = (cobj or {}).get("units") or {}
                unit_points = units_map.get(currency) or units_map.get(currency.upper())
                if not unit_points:
                    continue
                for pt in unit_points:
                    form = str(pt.get("form") or "")
                    if forms and form not in forms:
                        continue
                    # 忽略带 segment/dimension 的点（标准 companyfacts 默认无维度，若有 frame 含维度则跳过）
                    if pt.get("segment"):
                        continue
                    p_start = pt.get("start")
                    p_end = str(pt.get("end") or "")[:10]
                    if not p_end:
                        continue
                    days = _days_between(p_start, p_end)
                    is_instant = p_start is None
                    is_annual_flow = days is not None and 350 <= days <= 380
                    if not (is_instant or is_annual_flow):
                        continue
                    val = pt.get("val")
                    if val is None:
                        continue
                    filed = str(pt.get("filed") or "")
                    acc = str(pt.get("accn") or "")
                    if acc and acc not in meta_by_acc:
                        meta_by_acc[acc] = {
                            "accession": acc,
                            "form": form,
                            "filing_date": filed,
                            "report_date": p_end,
                        }
                    cur = by_concept.setdefault(full_concept, {})
                    prev_filed, _ = cur.get(p_end, ("", 0.0))
                    if filed >= prev_filed:
                        cur[p_end] = (filed, float(val))

    clean_map: dict[str, dict[str, float]] = {
        c: {d: v for d, (_, v) in dmap.items()} for c, dmap in by_concept.items()
    }
    meta_list = sorted(meta_by_acc.values(), key=lambda x: x.get("filing_date", ""), reverse=True)[:3]
    return clean_map, meta_list


def fetch_sec_companyfacts(ticker: str, forms: tuple[str, ...]) -> dict[str, Any]:
    """优先尝试调用沙箱 dalton_sec，不可用时直连 SEC EDGAR companyfacts。"""
    concepts = all_requested_concepts()
    # 1. 尝试沙箱生成的 dalton_sec 模块
    for mod_name in ("dalton_sec", "mcp_servers.dalton_sec", "tools.dalton_sec"):
        try:
            mod = __import__(mod_name, fromlist=["get_company_facts"])
            fn = getattr(mod, "get_company_facts", None)
            if callable(fn):
                res = fn(ticker=ticker, concepts=concepts, forms=list(forms))
                if isinstance(res, dict) and "data" in res:
                    return res
        except Exception:
            pass

    # 2. 直连 SEC EDGAR API
    ua = (
        os.environ.get("SEC_USER_AGENT")
        or os.environ.get("EDGAR_IDENTITY")
        or "DaltonResearch <research@dalton.local>"
    )
    headers = {"User-Agent": ua, "Accept": "application/json"}
    req_t = urllib.request.Request("https://www.sec.gov/files/company_tickers.json", headers=headers)
    with urllib.request.urlopen(req_t, timeout=20) as resp:
        tickers_data = json.loads(resp.read().decode("utf-8"))
    base_ticker = ticker.split(".")[0].upper()
    cik_int = None
    for item in tickers_data.values():
        if str(item.get("ticker", "")).upper() == base_ticker:
            cik_int = int(item["cik_str"])
            break
    if cik_int is None:
        raise RuntimeError(f"SEC company_tickers.json 中找不到 {base_ticker}")
    url_facts = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_int:010d}.json"
    req_f = urllib.request.Request(url_facts, headers=headers)
    with urllib.request.urlopen(req_f, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def first_available_series(
    concept_map: dict[str, dict[str, float]],
    candidates: list[str],
) -> tuple[str | None, dict[str, float]]:
    for c in candidates:
        s = concept_map.get(c)
        if s:
            return c, s
    return None, {}


def run_fincheck(
    ticker: str,
    concept_map: dict[str, dict[str, float]],
    meta: list[dict[str, str]],
    currency: str = "USD",
    form: str = "10-K",
) -> dict[str, Any]:
    _, assets_series = first_available_series(concept_map, TOTAL_ASSETS_CONCEPTS)
    if not assets_series:
        raise RuntimeError("底表缺 us-gaap:Assets（总资产），无法进行资产负债表体检")

    bs_dates = sorted(assets_series.keys(), reverse=True)[:3]
    latest = bs_dates[0]
    prior = bs_dates[1] if len(bs_dates) > 1 else None
    third = bs_dates[2] if len(bs_dates) > 2 else None
    ta_latest = assets_series.get(latest)
    ta_prior = assets_series.get(prior) if prior else None
    if not ta_latest:
        raise RuntimeError(f"{latest} 总资产为 0 或缺失")

    items: list[dict[str, Any]] = []
    triggered: list[dict[str, Any]] = []
    missing_bs: list[str] = []

    for label, candidates in BS_CONCEPTS:
        matched_concept, series = first_available_series(concept_map, candidates)
        if not matched_concept:
            missing_bs.append(f"{label} ({candidates[0]})")
            continue
        v1 = series.get(latest)
        v0 = series.get(prior) if prior else None
        v_third = series.get(third) if third else None
        if v1 is None and v0 is None:
            missing_bs.append(f"{label} ({matched_concept})")
            continue
        share = pct(v1, ta_latest)
        yoy = pct((v1 or 0.0) - (v0 or 0.0), abs(v0)) if (v1 is not None and v0) else None
        big = share is not None and abs(share) >= 10.0
        jump = (
            yoy is not None
            and abs(yoy) >= 30.0
            and (abs(v1 or 0.0) >= 0.02 * ta_latest or abs(v0 or 0.0) >= 0.02 * ta_latest)
        )
        trig = "大额+跳变" if (big and jump) else ("大额" if big else ("跳变" if jump else ""))
        rec = {
            "label": label,
            "concept": matched_concept,
            "latest": v1,
            "prior": v0,
            "third": v_third,
            "share_pct": share,
            "yoy_pct": yoy,
            "trigger": trig,
        }
        items.append(rec)
        if trig:
            triggered.append(rec)

    cf_series: dict[str, dict[str, float]] = {}
    missing_cf: list[str] = []
    for key, (label, candidates) in CF_CONCEPTS.items():
        matched_c, s = first_available_series(concept_map, candidates)
        if not matched_c:
            missing_cf.append(f"{label} ({candidates[0]})")
        cf_series[key] = s

    years = sorted(
        {d for k in ("capex", "ocf", "net_income") for d in cf_series.get(k, {})},
        reverse=True,
    )[:3]

    md: list[str] = []
    today_s = dt.date.today().isoformat()
    md.append(f"# {ticker} 财务体检（{form} us-gaap XBRL，{today_s}）\n")
    if meta:
        md.append(
            "来源："
            + "；".join(
                f"{m.get('form', form)} {m.get('report_date', '')} accession {m.get('accession', '')}（filed {m.get('filing_date', '')}）"
                for m in meta
            )
        )
    md.append(
        f"单位：{currency} 百万。总资产 {latest}：{fmt(ta_latest)}"
        + (f"；{prior}：{fmt(ta_prior)}" if ta_prior else "")
        + "\n"
    )

    md.append("## 触发项（占总资产 >= 10%，或同比变动 >= 30% 且金额 >= 总资产 2%）\n")
    if not triggered:
        md.append("资产负债表没有触发提示项。\n")
    else:
        for t in sorted(triggered, key=lambda x: -(abs(x["share_pct"] or 0.0))):
            yoy_s = "N/A" if t["yoy_pct"] is None else f"{t['yoy_pct']:+.0f}%"
            md.append(
                f"- [{t['trigger']}] **{t['label']}**（`{t['concept']}`）：{latest} {fmt(t['latest'])}（占总资产 {sp(t['share_pct'])}），"
                f"{prior or '-'} {fmt(t['prior'])}，同比 {yoy_s}"
            )

    md.append("\n## 资产负债表主要科目（已剔除现金、短投、有息负债、递延收入、股东权益）\n")
    md.append(f"| 科目 | Concept | {latest} | {prior or '-'} | {third or '-'} | 占总资产 | 同比 |")
    md.append("|---|---|---|---|---|---|---|")
    for r in sorted(items, key=lambda x: -(abs(x["share_pct"] or 0.0))):
        if (r["share_pct"] or 0.0) < 3.0 and not r["trigger"]:
            continue
        yoy_s = "N/A" if r["yoy_pct"] is None else f"{r['yoy_pct']:+.0f}%"
        md.append(
            f"| {r['label']} | `{r['concept']}` | {fmt(r['latest'])} | {fmt(r['prior'])} | {fmt(r.get('third'))} | {sp(r['share_pct'])} | {yoy_s} |"
        )

    md.append(f"\n## Capex 与现金流（年度，{currency} 百万）\n")
    md.append(
        "| 年度 | Capex（PP&E） | 无形资产投入 | 经营现金流 | 净利润 | D&A | Capex 相当于净利润的 | Capex/经营现金流 | Capex 同比 |"
    )
    md.append("|---|---|---|---|---|---|---|---|---|")
    prev_capex: float | None = None
    capex_flags: list[str] = []
    for y in sorted(years):
        cap = cf_series.get("capex", {}).get(y)
        intang = cf_series.get("capex_intangible", {}).get(y)
        ocf = cf_series.get("ocf", {}).get(y)
        ni = cf_series.get("net_income", {}).get(y)
        da = cf_series.get("da", {}).get(y)
        r_ni = pct(cap, ni)
        r_ocf = pct(cap, ocf)
        g = pct((cap or 0.0) - (prev_capex or 0.0), abs(prev_capex)) if (prev_capex and cap is not None) else None
        md.append(
            f"| {y[:4]} | {fmt(cap)} | {fmt(intang)} | {fmt(ocf)} | {fmt(ni)} | {fmt(da)} | "
            f"{'N/A' if r_ni is None else f'{r_ni:.0f}%'} | {'N/A' if r_ocf is None else f'{r_ocf:.0f}%'} | "
            f"{'N/A' if g is None else f'{g:+.0f}%'} |"
        )
        if r_ni is not None and r_ni >= 30.0:
            capex_flags.append(f"{y[:4]} Capex 相当于净利润的 {r_ni:.0f}%（同年 D&A {fmt(da)}）")
        if g is not None and g >= 20.0:
            capex_flags.append(f"{y[:4]} Capex 同比 {g:+.0f}%")
        prev_capex = cap

    md.append("")
    md.append(
        "Capex 提示："
        + (
            "；".join(capex_flags)
            if capex_flags
            else "三年 Capex 增速与相当于净利润的比例均未触发阈值（同比 >= 20% 或相当于净利润的 30% 以上）。"
        )
    )

    sbc = cf_series.get("sbc", {})
    rev = cf_series.get("revenue", {})
    if sbc:
        sbc_parts = []
        for y, v in sorted(sbc.items())[-3:]:
            r_v = rev.get(y)
            ratio_s = f"（占收入 {pct(v, r_v):.1f}%）" if r_v else ""
            sbc_parts.append(f"{y[:4]} {fmt(v)}{ratio_s}")
        md.append("\nSBC 费用（供股东回报支线参考）：" + "；".join(sbc_parts))

    all_missing = missing_bs + missing_cf
    if all_missing:
        md.append("\n## 底表缺（XBRL 无对应无维度 us-gaap 科目，如使用扩展标签请查附注）\n")
        md.append("- 底表缺：" + "；".join(all_missing))

    return {
        "ok": True,
        "ticker": ticker,
        "latest_period": latest,
        "triggered_count": len(triggered),
        "triggered": triggered,
        "capex_flags": capex_flags,
        "years": years,
        "missing_concepts": all_missing,
        "markdown": "\n".join(md) + "\n",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Dalton Bull/Bear 财务体检脚本（基于 us-gaap XBRL concepts）")
    ap.add_argument("--ticker", required=True, help="股票代码，如 META / MSFT / AMZN")
    ap.add_argument("--form", default="10-K", help="年报表格类型，默认 10-K（外资发行人可传 20-F）")
    ap.add_argument("--currency", default="USD", help="报表货币单位（默认 USD）")
    ap.add_argument("--facts-json", default=None, help="可选：本地 dalton_sec.get_company_facts 或 SEC companyfacts JSON 文件路径")
    ap.add_argument("--out", default=None, help="输出 Markdown 文件路径（或目录路径）")
    ap.add_argument("--slug", default=None, help="当 --out 为目录时使用的文件名前缀（默认写 fincheck.md）")
    ap.add_argument("--json", action="store_true", help="打印 JSON 摘要")
    args = ap.parse_args()

    try:
        if args.facts_json:
            raw = json.loads(pathlib.Path(args.facts_json).read_text(encoding="utf-8"))
        else:
            raw = fetch_sec_companyfacts(args.ticker, forms=(args.form, "10-K", "20-F"))
        concept_map, meta = normalize_facts_payload(raw, currency=args.currency, forms=(args.form, "10-K", "20-F"))
        res = run_fincheck(args.ticker.upper(), concept_map, meta, currency=args.currency, form=args.form)
    except Exception as e:
        err = {"ok": False, "ticker": args.ticker, "error": str(e)}
        if args.json:
            print(json.dumps(err, ensure_ascii=False))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    out_file: pathlib.Path | None = None
    if args.out:
        p = pathlib.Path(args.out)
        if p.suffix.lower() == ".md":
            out_file = p
        else:
            p.mkdir(parents=True, exist_ok=True)
            fname = f"{args.slug}_fincheck.md" if args.slug else "fincheck.md"
            out_file = p / fname
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(res["markdown"], encoding="utf-8")

    payload = {
        "ok": True,
        "ticker": res["ticker"],
        "path": str(out_file) if out_file else None,
        "triggered": res["triggered_count"],
        "capex_flags": res["capex_flags"],
        "years": res["years"],
        "missing_count": len(res["missing_concepts"]),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif out_file:
        print(out_file, "triggered", res["triggered_count"], "capex_flags", res["capex_flags"])
    else:
        print(res["markdown"], end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
