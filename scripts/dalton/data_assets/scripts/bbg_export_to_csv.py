#!/usr/bin/env python3
"""Validate a Bloomberg export (Windows CSV or cached workbook) and install it as the local CSV.

远程 Windows 机上两个产物都可能坏，而且坏法不一样，所以两个都要过同一道闸：

- ``<DALTON_DATA_TASK_BBG_CSV_REMOTE>`` 是 refresh 脚本等 BDH 落定 30 秒后导出的。BBG 连接断掉时
  它会退化成 8 行 ``#N/A Connection``（2026-08-09 遇到过）。
- ``<DALTON_DATA_TASK_BBG_XLSX_REMOTE>`` 是同一次刷新保存的工作簿。脚本在保存前会把 BDH 公式还原成原样，Excel 随即
  重算，缓存值经常停在 ``#N/A Requesting Data...``（2026-08-09 改用工作簿后一直是这个状态，
  BBG CSV 因此卡在 08-07 三天没更新）。

判断标准只看数据本身：无 Excel 错误值，且 7 条序列各自至少 MIN_HISTORY 个有效点。通过才原子替换
本地 CSV，坏数据一律不落盘。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


SERIES = {
    "cyc_price": (0, 1),
    "cyc_rsi14": (3, 4),
    "momentum_rsi": (6, 7),
    "qqq_vol_1m": (9, 10),
    "qqq_vol_3m": (12, 13),
    "ah_price": (15, 16),
    "ah_rsi30": (18, 19),
}
MIN_HISTORY = 252
ERROR_VALUES = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?"}
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d")


def valid_date(value: Any) -> bool:
    return isinstance(value, (date, datetime))


def valid_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def coerce(value: str) -> Any:
    """把 CSV 的字符串还原成 Excel 里的类型，这样两条输入路径共用同一套校验。"""
    text = value.strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return float(text)
    except ValueError:
        return text


def read_xlsx(path: Path) -> tuple[list[tuple], str]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    return list(sheet.iter_rows(values_only=True)), sheet.title


def read_csv(path: Path) -> tuple[list[tuple], str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw = list(csv.reader(handle))
    # 前两行是 Bloomberg 的表头（ticker / 字段名），原样保留；其余按类型还原。
    rows = [tuple(raw[i]) for i in range(min(2, len(raw)))]
    rows += [tuple(coerce(cell) for cell in row) for row in raw[2:]]
    return rows, path.name


def validate(rows: list[tuple]) -> tuple[dict[str, int], dict[str, str]]:
    errors = sorted({
        str(value)
        for row in rows
        for value in row
        if isinstance(value, str) and (value.startswith("#N/A") or value in ERROR_VALUES)
    })
    if errors:
        raise ValueError(f"Export contains formula/data errors: {errors[:5]}")

    counts: dict[str, int] = {}
    last_dates: dict[str, str] = {}
    for name, (date_col, value_col) in SERIES.items():
        points = [
            row
            for row in rows[2:]
            if len(row) > value_col and valid_date(row[date_col]) and valid_number(row[value_col])
        ]
        counts[name] = len(points)
        if points:
            last_dates[name] = points[-1][date_col].date().isoformat()

    short = {name: count for name, count in counts.items() if count < MIN_HISTORY}
    if short:
        raise ValueError(f"Bloomberg history is too short: {short}; need at least {MIN_HISTORY} points each")
    return counts, last_dates


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Bloomberg export and install it as the local CSV.")
    parser.add_argument("--input", required=True, type=Path, help="Windows-side .csv export or cached .xlsx workbook")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check-only", action="store_true", help="Validate without writing the output CSV")
    args = parser.parse_args()

    try:
        reader = read_xlsx if args.input.suffix.lower() in {".xlsx", ".xlsm"} else read_csv
        rows, source = reader(args.input)
        counts, last_dates = validate(rows)

        if not args.check_only:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            staging = args.output.with_suffix(args.output.suffix + ".tmp")
            with staging.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerows([["" if value is None else value for value in row] for row in rows])
            os.replace(staging, args.output)
        print(json.dumps({
            "ok": True,
            "input": str(args.input),
            "sheet": source,
            "rows": len(rows),
            "counts": counts,
            "last_dates": last_dates,
            "output": None if args.check_only else str(args.output),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "input": str(args.input),
            "error": str(exc),
            "hint": "Check the BBG terminal is logged in on the Windows box, rerun the configured BBG refresh command, then pull again.",
        }, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
