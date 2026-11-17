#!/usr/bin/env python3
"""Manual refresh script for Shenwan Level-3 industry stock mapping (sw3_mapping.json).

Downloads the official Shenwan 2021 stock classification XLS + L2/L3 industry tables
and writes sw3_mapping.json atomically. Run manually when Shenwan updates constituents.
"""

from __future__ import annotations

import argparse
from io import BytesIO, StringIO
import json
from pathlib import Path
import time
from typing import Any

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "sw3_mapping.json"
STOCK_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
RAW = "https://raw.githubusercontent.com/zwq2018/Data-Copilot/c32f9dfa0ea7e9cd6316f06dab38ef2f1eb30187"
L2_URL = RAW + "/SW2021_industry_L2.csv"
L3_URL = RAW + "/SW2021_industry_L3.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.swsresearch.com/swindex/pdf/SwClass2021/",
}


def download(url: str, *, verify: bool = True, session: Any = None) -> bytes:
    client = session or requests
    resp = client.get(url, headers=HEADERS, timeout=60, verify=verify)
    resp.raise_for_status()
    return resp.content


def refresh_mapping(out_path: Path | None = None, *, session: Any = None) -> dict[str, Any]:
    target = out_path or DEFAULT_OUT
    stock_bytes = download(STOCK_URL, verify=False, session=session)
    stock_history = pd.read_excel(
        BytesIO(stock_bytes),
        dtype={"股票代码": str, "行业代码": str},
    )
    stock_history["计入日期"] = pd.to_datetime(stock_history["计入日期"], errors="coerce")
    stock_history = stock_history.dropna(subset=["计入日期"]).sort_values("计入日期")
    current = stock_history.groupby("股票代码", as_index=False).tail(1)

    l2 = pd.read_csv(
        StringIO(download(L2_URL, session=session).decode("utf-8-sig")), dtype=str
    )
    l3 = pd.read_csv(
        StringIO(download(L3_URL, session=session).decode("utf-8-sig")), dtype=str
    )
    l2_names = dict(zip(l2["industry_code"], l2["industry_name"]))
    l3_names = dict(zip(l3["industry_code"], l3["industry_name"]))
    l3_parents = dict(zip(l3["industry_code"], l3["parent_code"]))

    stocks: dict[str, dict[str, str]] = {}
    industries: dict[str, dict[str, Any]] = {}
    counts = current["行业代码"].value_counts().to_dict()
    for code, name in l3_names.items():
        parent_code = l3_parents.get(code, "")
        industries[code] = {
            "name": str(name).replace("Ⅲ", ""),
            "parent": str(l2_names.get(parent_code, "")).replace("Ⅱ", ""),
            "constituents": int(counts.get(code, 0)),
        }
    for _, row in current.iterrows():
        code = str(row["行业代码"])
        industry = industries.get(code)
        if not industry:
            continue
        stock_code = str(row["股票代码"]).zfill(6)
        stocks[stock_code] = {
            "stock_code": stock_code,
            "industry_code": code,
            "industry_name": industry["name"],
            "parent_name": industry["parent"],
        }

    payload = {
        "asof": time.strftime("%Y-%m-%d"),
        "source": "申万2021三级行业（申万宏源官方个股分类）",
        "source_urls": [STOCK_URL, L2_URL, L3_URL],
        "industry_count": len(industries),
        "stock_count": len(stocks),
        "errors": [],
        "industries": industries,
        "stocks": stocks,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(target)
    return {
        "out": str(target),
        "asof": payload["asof"],
        "industry_count": len(industries),
        "stock_count": len(stocks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh sw3_mapping.json from Shenwan official XLS.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    res = refresh_mapping(args.out)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
