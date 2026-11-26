#!/usr/bin/env python3
"""Read-only Excel workbook readability check (`model_readable.py`).

Checks that a target `.xlsx` workbook exists, can be opened by `openpyxl` in
read-only mode, has at least one sheet, and allows reading each data
worksheet's dimension and first row (chart sheets are skipped). Worksheet XML is
stream-parsed to its end within a 40-second budget, so truncated sheets are
caught while very large models still finish inside the completion-check limit. Never modifies
the file and never judges financial content.

Usage:
    python3 .agents/skills/dalton-model/scripts/model_readable.py <workbook.xlsx>

Exit codes:
    0   Workbook is readable.
    1   Missing argument, missing file, non-ZIP file, corrupt XML, or unreadable
        worksheet (reason printed to stderr).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

import openpyxl


_PARSE_BUDGET_S = 40.0


def check_workbook_readable(workbook_path: str | Path) -> tuple[bool, str]:
    """Return ``(ok, message)`` for read-only structural readability of *workbook_path*."""
    raw = str(workbook_path).strip()
    if not raw:
        return False, "empty workbook path"

    path = Path(raw)
    if not path.is_file():
        return False, f"file not found: {path}"

    # 1. Container check: a real .xlsx is a ZIP holding xl/workbook.xml.
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "xl/workbook.xml" not in zf.namelist():
                return False, "missing xl/workbook.xml"
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"invalid zip/xlsx container: {exc}"

    # 2. Open with openpyxl in read_only mode, read each data worksheet's dimension
    #    and first row (chart sheets have no cells and are skipped), and collect the
    #    package path of every data worksheet from the workbook's own relationships.
    wb = None
    sheet_parts: list[str] = []
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        sheetnames = list(wb.sheetnames)
        if not sheetnames:
            return False, "workbook contains no worksheets"
        for ws in wb.worksheets:
            try:
                _ = ws.calculate_dimension()
            except ValueError:
                # Empty read-only sheets may lack a dimension; fall back to bounds.
                _ = (ws.max_row, ws.max_column)
            _ = next(ws.iter_rows(max_row=1, values_only=True), None)
            part = getattr(ws, "_worksheet_path", None)
            if isinstance(part, str) and part:
                sheet_parts.append(part.lstrip("/"))
    except Exception as exc:
        kind = "corrupt XML" if type(exc).__name__ == "ParseError" else "unreadable workbook"
        return False, f"{kind} ({type(exc).__name__}): {exc}"
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass

    # 3. Stream-parse each data worksheet to its end so a truncated or damaged
    #    sheet is caught. Parsing stops once the time budget is spent, keeping the
    #    whole check inside the 60-second completion-check limit; whatever was read
    #    by then must be well formed.
    deadline = time.monotonic() + _PARSE_BUDGET_S
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for member in sheet_parts:
                if time.monotonic() >= deadline:
                    break
                try:
                    with zf.open(member, "r") as stream:
                        for _event, elem in ET.iterparse(stream, events=("end",)):
                            elem.clear()
                            if time.monotonic() >= deadline:
                                break
                except ET.ParseError as exc:
                    return False, f"corrupt XML in {member}: {exc}"
                except KeyError:
                    return False, f"missing worksheet part: {member}"
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"invalid zip/xlsx container: {exc}"

    return True, f"readable ({len(sheetnames)} sheets)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="model_readable.py",
        description="Read-only readability check for Excel (.xlsx) workbooks.",
    )
    parser.add_argument("workbook", nargs="?", default="", help="Path to .xlsx workbook (read-only)")
    args = parser.parse_args(argv)
    if not args.workbook:
        print("ERROR: missing workbook path", file=sys.stderr)
        return 1

    ok, message = check_workbook_readable(args.workbook)
    if not ok:
        print(f"ERROR: {args.workbook}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
