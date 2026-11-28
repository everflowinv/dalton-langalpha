#!/usr/bin/env python3
"""GS Prime 'US Equities Weekly Rundown' PDF: fetch from Gmail, parse the leverage table, digitize the
vector charts, and merge the result into the sentiment-dashboard GS Prime store.

Needs PyMuPDF, so run it with the company-wiki venv:

    skills/company-wiki/venv/bin/python -I scripts/gs_pb_weekly.py run
    skills/company-wiki/venv/bin/python -I scripts/gs_pb_weekly.py process <pdf> [--subject S]
    skills/company-wiki/venv/bin/python -I scripts/gs_pb_weekly.py rebuild
    skills/company-wiki/venv/bin/python -I scripts/gs_pb_weekly.py self-test

Job notes and data layout: scripts/gs_pb_weekly.md.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

def _resolve_workspace() -> Path:
    for var in ("DALTON_DATA_TASK_WORKSPACE", "DALTON_WORKSPACE", "OPENCLAW_WORKSPACE", "WORKDIR"):
        env_ws = (os.environ.get(var) or "").strip()
        if env_ws:
            return Path(env_ws)
    return Path.cwd()


WORKSPACE = _resolve_workspace()
OUT_DIR = WORKSPACE / "research-outputs/gs-pb-weekly"
PDF_DIR = OUT_DIR / "pdfs"
ISSUE_DIR = OUT_DIR / "issues"
STATE_PATH = WORKSPACE / "state/gs-pb-weekly/state.json"
LAST_RUN_PATH = WORKSPACE / "state/gs-pb-weekly/last-run.json"
DASHBOARD_STORE = WORKSPACE / "state/sentiment-dashboard/gs_prime_positioning.json"
DAILY_CSV = OUT_DIR / "gs_pb_charts_daily_latest.csv"
WEEKLY_CSV = OUT_DIR / "gs_pb_core_weekly_latest.csv"
GMAIL_QUERY = 'subject:("US Equities Weekly Rundown") has:attachment'
ET = ZoneInfo("America/New_York")

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
NUM_RE = re.compile(r"^\(?-?\d+(?:\.\d+)?%?\)?$")
DATE_RE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)"
                     r"\s+(\d{1,2}),\s+(20\d\d)")
TABLE_COLS = ["gross", "net", "ls_ratio", "gross_beta_adj", "net_beta_adj", "ls_ratio_beta_adj"]
TABLE_HEADERS = ["Gross %", "Net %", "L/S Ratio", "Gross % (Beta Adj)", "Net % (Beta Adj)", "L/S Ratio (Beta Adj)"]
TABLE_VAL_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:pt|%)?$")
# digitized last point vs printed table value; prototype on 10-23 was within 0.05 / 0.05 / 0.001
TOLERANCE = {"gross": 1.5, "net": 1.0, "ls_ratio": 0.015}


# ---------------------------------------------------------------- chart digitizer
def is_black(c):
    return c is not None and all(v < 0.05 for v in c)


def is_white(c):
    return c is not None and all(v > 0.95 for v in c)


def parse_num(s):
    s = s.strip().replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    v = float(s.strip("()").rstrip("%"))
    return -v if neg else v


def spans_of(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                t = s["text"].strip()
                if t:
                    x0, y0, x1, y1 = s["bbox"]
                    out.append({"t": t, "x0": x0, "x1": x1, "xc": (x0 + x1) / 2, "yc": (y0 + y1) / 2})
    return out


def linfit(pairs):
    n = len(pairs)
    sx = sum(p[0] for p in pairs); sy = sum(p[1] for p in pairs)
    sxx = sum(p[0] ** 2 for p in pairs); sxy = sum(p[0] * p[1] for p in pairs)
    a = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    b = (sy - a * sx) / n
    return a, b, max(abs(a * x + b - y) for x, y in pairs)


def find_frames(drawings):
    """Chart frames are single black stroked rectangles."""
    frames = []
    for g in drawings:
        items = g["items"]
        if len(items) == 1 and items[0][0] == "re" and is_black(g.get("color")) and g.get("fill") is None:
            r = g["rect"]
            if r.width > 100 and r.height > 80:
                frames.append(r)
    return frames


def y_axes(frame, drawings, spans):
    """Calibrate left/right value axes from tick marks and their numeric labels."""
    axes = {}
    for side in ("left", "right"):
        ticks = []
        for g in drawings:
            it = g["items"]
            if len(it) != 1 or it[0][0] != "l" or not is_black(g.get("color")):
                continue
            p1, p2 = it[0][1], it[0][2]
            if abs(p1.y - p2.y) > 0.01 or abs(p1.x - p2.x) > 6:
                continue
            xs = sorted([p1.x, p2.x])
            if not (frame.y0 - 1 <= p1.y <= frame.y1 + 1):
                continue
            if side == "left" and abs(xs[1] - frame.x0) < 1.5:
                ticks.append((xs[0], p1.y))
            if side == "right" and abs(xs[0] - frame.x1) < 1.5:
                ticks.append((xs[1], p1.y))
        pairs = []
        for tx, ty in ticks:
            cands = [s for s in spans if NUM_RE.match(s["t"]) and abs(s["yc"] - ty) < 3.5]
            if side == "left":
                cands = sorted((s for s in cands if s["x1"] <= tx + 3 and tx - s["x1"] < 25), key=lambda s: tx - s["x1"])
            else:
                cands = sorted((s for s in cands if s["x0"] >= tx - 3 and s["x0"] - tx < 25), key=lambda s: s["x0"] - tx)
            if cands:
                pairs.append((ty, parse_num(cands[0]["t"])))
        if len(pairs) >= 3:
            a, b, resid = linfit(pairs)
            axes[side] = {"a": a, "b": b, "resid": resid, "n": len(pairs)}
    return axes


def x_axis(frame, drawings, spans):
    """Calibrate the date axis from tick marks labelled Jan/Jul plus the year row."""
    ticks = []
    for g in drawings:
        it = g["items"]
        if len(it) != 1 or it[0][0] != "l" or not is_black(g.get("color")):
            continue
        p1, p2 = it[0][1], it[0][2]
        if abs(p1.x - p2.x) > 0.01 or abs(p1.y - p2.y) > 6:
            continue
        if abs(min(p1.y, p2.y) - frame.y1) < 1.5 and frame.x0 - 1 <= p1.x <= frame.x1 + 1:
            ticks.append(p1.x)
    ticks = sorted(set(round(t, 2) for t in ticks))
    below = [s for s in spans if frame.y1 - 12 < s["yc"] < frame.y1 + 32 and frame.x0 - 15 < s["xc"] < frame.x1 + 15]
    months = [s for s in below if s["t"] in MONTHS and s["yc"] > frame.y1]
    years = sorted((s for s in below if re.fullmatch(r"20\d\d", s["t"])), key=lambda s: s["xc"])
    labelled = []
    for tx in ticks:
        m = [s for s in months if abs(s["xc"] - tx) < 4]
        if m:
            labelled.append([tx, MONTHS[m[0]["t"]], None])
    if len(labelled) < 2 or not years:
        return None
    for t in labelled:
        if t[1] == 1:
            right = [y for y in years if y["xc"] >= t[0] - 2]
            if right:
                t[2] = int(right[0]["t"])
    cur = None
    for t in labelled:
        if t[2] is not None:
            cur = t[2]
        elif cur is not None:
            t[2] = cur
    if labelled[0][2] is None:  # ticks before the first January
        first_year = int(years[0]["t"])
        for t in labelled:
            if t[2] is None:
                t[2] = first_year - 1 if t[1] > 1 else first_year
    return [(t[0], date(t[2], t[1], 1).toordinal()) for t in labelled]


def x_to_date(x, pts):
    if x <= pts[0][0]:
        (xa, da), (xb, db) = pts[0], pts[1]
    elif x >= pts[-1][0]:
        (xa, da), (xb, db) = pts[-2], pts[-1]
    else:
        k = next(k for k in range(len(pts) - 1) if pts[k][0] <= x <= pts[k + 1][0])
        (xa, da), (xb, db) = pts[k], pts[k + 1]
    return da + (x - xa) * (db - da) / (xb - xa)


def legend_names(frame, spans, n):
    zone = [s for s in spans if frame.y1 + 8 < s["yc"] < frame.y1 + 45
            and frame.x0 - 30 < s["xc"] < frame.x1 + 30 and len(s["t"]) > 1
            and s["t"] not in MONTHS and not re.fullmatch(r"20\d\d", s["t"]) and not NUM_RE.match(s["t"])]
    zone.sort(key=lambda s: (round(s["yc"]), s["x0"]))
    names = [s["t"] for s in zone]
    return names[:n] if len(names) >= n else names + [f"series{k + 1}" for k in range(len(names), n)]


def title_of(frame, spans):
    above = [s for s in spans if frame.y0 - 30 < s["yc"] < frame.y0 - 2 and s["x1"] > frame.x0 and s["x0"] < frame.x1
             and not NUM_RE.match(s["t"])]
    return max(above, key=lambda s: len(s["t"]))["t"] if above else None


def digitize(doc):
    """Return charts with raw points in drawing order: [{page,title,series:[{name,kind,axis,points}]}]."""
    charts = []
    for pno in range(doc.page_count):
        page = doc[pno]
        drawings = page.get_drawings()
        spans = spans_of(page)
        for frame in find_frames(drawings):
            axes = y_axes(frame, drawings, spans)
            xpts = x_axis(frame, drawings, spans)
            if not axes or not xpts:
                continue
            series, bars = {}, {}
            for gi, g in enumerate(drawings):
                r = g["rect"]
                if not (frame.x0 - 1 <= r.x0 and r.x1 <= frame.x1 + 1 and frame.y0 - 1 <= r.y0 and r.y1 <= frame.y1 + 1):
                    continue
                col = g.get("color")
                if col is not None and not is_white(col) and (g.get("width") or 0) >= 1 \
                        and all(it[0] == "l" for it in g["items"]):
                    s = series.setdefault(tuple(round(c, 3) for c in col), {"first": gi, "pts": []})
                    for it in g["items"]:
                        s["pts"].extend([(it[1].x, it[1].y), (it[2].x, it[2].y)])
                fill = g.get("fill")
                if fill is not None and not is_white(fill) and all(it[0] == "re" for it in g["items"]):
                    for it in g["items"]:
                        rr = it[1]
                        if rr.width < frame.width * 0.5:
                            b = bars.setdefault(tuple(round(c, 3) for c in fill), {"first": gi, "rects": []})
                            b["rects"].append((rr.x0, rr.y0, rr.x1, rr.y1))
            ordered = sorted(series.values(), key=lambda s: s["first"])
            ordered_bars = sorted(bars.values(), key=lambda b: b["first"])
            names = legend_names(frame, spans, len(ordered) + len(ordered_bars))
            # Word/Excel legends list bar series before line series
            bar_names, line_names = names[:len(ordered_bars)], names[len(ordered_bars):]
            title = title_of(frame, spans)
            out = []
            for s, name in zip(ordered, line_names):
                if re.fullmatch(r"series\d+", name) and title and "long/short ratio" in title.lower():
                    name = "L/S Ratio"
                side = "right" if "right" in name.lower() and "right" in axes else ("left" if "left" in axes else "right")
                ax = axes[side]
                pts, last = [], None
                for x, y in s["pts"]:  # keep drawing order: x is quantised, several days share one x
                    if last and abs(x - last[0]) < 1e-6 and abs(y - last[1]) < 1e-6:
                        continue
                    last = (x, y)
                    pts.append({"x": x, "date_frac": x_to_date(x, xpts), "value": ax["a"] * y + ax["b"]})
                out.append({"name": name, "kind": "line", "axis": side, "points": pts})
            for b, name in zip(ordered_bars, bar_names):
                ax = axes["left"] if "left" in axes else axes["right"]
                pts = []
                for x0, y0, x1, y1 in sorted(b["rects"]):
                    v0, v1 = ax["a"] * y0 + ax["b"], ax["a"] * y1 + ax["b"]
                    xm = (x0 + x1) / 2
                    pts.append({"x": xm, "date_frac": x_to_date(xm, xpts), "value": v0 if abs(v0) > abs(v1) else v1})
                out.append({"name": name, "kind": "bar", "axis": "left", "points": pts})
            charts.append({
                "page": pno + 1, "title": title,
                "axes_resid": {k: round(v["resid"], 4) for k, v in axes.items()},
                "x_ticks": [(round(x, 2), date.fromordinal(d).isoformat()) for x, d in xpts],
                "series": out,
            })
    return charts


def spread(points):
    """Points sharing one quantised x are consecutive days: spread their dates across the x bucket."""
    if len(points) < 2:
        return [(p["date_frac"], p["value"]) for p in points]
    xs = sorted(set(round(p["x"], 3) for p in points))
    gaps = [b - a for a, b in zip(xs, xs[1:]) if b - a > 1e-3]
    span_x = points[-1]["x"] - points[0]["x"]
    if not gaps or abs(span_x) < 1e-9:
        return [(p["date_frac"], p["value"]) for p in points]
    step = min(gaps)
    dpp = (points[-1]["date_frac"] - points[0]["date_frac"]) / span_x
    out, i, n = [], 0, len(points)
    while i < n:
        j = i
        while j + 1 < n and abs(points[j + 1]["x"] - points[i]["x"]) < 1e-6:
            j += 1
        m = j - i + 1
        for k in range(m):
            out.append((points[i + k]["date_frac"] + ((k + 0.5) / m - 0.5) * step * dpp, points[i + k]["value"]))
        i = j + 1
    return out


def to_daily(points):
    """Snap spread points to weekdays (Sat->Fri, Sun->Mon); the last point on a day wins."""
    byday = {}
    for df, v in spread(points):
        d = date.fromordinal(int(round(df)))
        if d.weekday() == 5:
            d -= timedelta(days=1)
        elif d.weekday() == 6:
            d += timedelta(days=1)
        byday[d] = v
    return sorted(byday.items())


# ---------------------------------------------------------------- text layer
def table_value(s):
    return float(s.rstrip("%").replace("pt", ""))


def parse_leverage_table(doc):
    """US Fundamental L/S leverage table: label line followed by six values per row."""
    for page in doc:
        lines = [ln.strip() for ln in page.get_text().splitlines() if ln.strip()]
        for i in range(len(lines) - 7):
            if lines[i] == "Leverage" and lines[i + 1:i + 7] == TABLE_HEADERS:
                rows, j = {}, i + 7
                while j + 6 < len(lines) and not TABLE_VAL_RE.match(lines[j]) \
                        and all(TABLE_VAL_RE.match(x) for x in lines[j + 1:j + 7]):
                    label = lines[j]
                    vals = dict(zip(TABLE_COLS, (table_value(x) for x in lines[j + 1:j + 7])))
                    if label in ("Current", "Current Level"):  # 10/30 期起改成 "Current Level"
                        key = "current"
                    elif label == "WoW Chg":
                        key = "wow_chg"
                    elif label.endswith("MTD Chg"):
                        key = "mtd_chg"
                    elif label.endswith("YTD Chg"):
                        key = "ytd_chg"
                    elif re.fullmatch(r"20\d\d Chg", label):
                        key = "prior_year_chg"
                    else:
                        m = re.fullmatch(r"Current (\d)-Yr Percentile", label)
                        key = f"pctile_{m.group(1)}y" if m else label
                    rows[key] = {"label": label, **vals}
                    j += 7
                return {"page": page.number + 1, "rows": rows}
    return None


def issue_date_of(doc, subject=""):
    head = doc[0].get_text()[:3000]
    m = DATE_RE.search(head) or DATE_RE.search(subject or "")
    if not m:
        raise ValueError("PDF 首页和邮件标题里都找不到周报日期")
    return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()


def risk_exposures_text(doc):
    for page in doc:
        flat = re.sub(r"\s+", " ", page.get_text())
        m = re.search(r"Risk Exposures:(.{0,600}?\(\d+(?:st|nd|rd|th) percentile[^)]*\)\.)", flat)
        if m:
            return "Risk Exposures:" + m.group(1)
    return ""


# ---------------------------------------------------------------- processing
def find_chart(charts, title_kw):
    return next((c for c in charts if title_kw.lower() in (c.get("title") or "").lower()), None)


def core_series(issue):
    """{'gross': [(iso, v)], 'net': ..., 'ls_ratio': ...} from an issue's digitized charts."""
    out = {}
    lev = find_chart(issue["charts"], "Gross vs Net Leverage")
    if lev:
        for s in lev["series"]:
            nm = s["name"].lower()
            if nm.startswith("gross"):
                out["gross"] = s["daily"]
            elif nm.startswith("net"):
                out["net"] = s["daily"]
    lsr = find_chart(issue["charts"], "Long/Short Ratio")
    if lsr and lsr["series"]:
        out["ls_ratio"] = lsr["series"][0]["daily"]
    return out


def validate(issue):
    """Business checks: table complete, core charts present, digitized last point matches the table."""
    errors = []
    table = issue.get("table") or {}
    rows = table.get("rows") or {}
    for key in ("current", "wow_chg", "pctile_1y", "pctile_3y", "pctile_5y"):
        if key not in rows:
            errors.append(f"杠杆表缺少「{key}」行")
    core = core_series(issue)
    for key in ("gross", "net", "ls_ratio"):
        if not core.get(key):
            errors.append(f"没还原出 {key} 图")
            continue
        last_d, last_v = core[key][-1]
        if (date.fromisoformat(issue["issue_date"]) - date.fromisoformat(last_d)).days > 6:
            errors.append(f"{key} 图最后一点 {last_d} 离周报日期超过 6 天")
        if "current" in rows and abs(last_v - rows["current"][key]) > TOLERANCE[key]:
            errors.append(f"{key} 图最后一点 {last_v:.3f} 和表格 {rows['current'][key]} 对不上")
    return errors


def process_pdf(pdf_path: Path, *, subject="", gmail_id="", received_at="") -> dict[str, Any]:
    import fitz

    data = pdf_path.read_bytes()
    doc = fitz.open(stream=data, filetype="pdf")
    issue_date = issue_date_of(doc, subject)
    charts = digitize(doc)
    for c in charts:
        for s in c["series"]:
            s["daily"] = [(d.isoformat(), round(v, 4)) for d, v in to_daily(s.pop("points"))]
    issue = {
        "issue_date": issue_date.isoformat(),
        "subject": subject,
        "gmail_id": gmail_id,
        "received_at": received_at,
        "sha256": hashlib.sha256(data).hexdigest(),
        "pages": doc.page_count,
        "processed_at": datetime.now(ET).isoformat(timespec="seconds"),
        "risk_exposures": risk_exposures_text(doc),
        "table": parse_leverage_table(doc),
        "charts": charts,
    }
    issue["errors"] = validate(issue)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    ISSUE_DIR.mkdir(parents=True, exist_ok=True)
    archived = PDF_DIR / f"US_Equities_Weekly_Rundown_{issue['issue_date']}.pdf"
    if archived.resolve() != pdf_path.resolve():
        archived.write_bytes(data)
    issue["pdf"] = str(archived.relative_to(WORKSPACE))
    write_json(ISSUE_DIR / f"{issue['issue_date']}.json", issue, indent=None)
    return issue


def load_issues(valid_only=True):
    issues = []
    for p in sorted(ISSUE_DIR.glob("*.json")):
        it = json.loads(p.read_text(encoding="utf-8"))
        if not valid_only or not it.get("errors"):
            issues.append(it)
    return sorted(issues, key=lambda it: it["issue_date"])


def merged_daily(issues):
    """{(chart, series): {iso: value}}; the newest issue wins, older issues only fill earlier dates."""
    merged: dict[tuple[str, str], dict[str, float]] = {}
    for it in reversed(issues):
        for c in it["charts"]:
            for s in c["series"]:
                key = (c.get("title") or f"p{c['page']}", s["name"])
                cur = merged.setdefault(key, {})
                first_newer = min(cur) if cur else None
                for d, v in s["daily"]:
                    if first_newer is None or d < first_newer:
                        cur[d] = v
    return merged


def friday_on_or_before(d: date) -> date:
    return d - timedelta(days=(d.weekday() - 4) % 7)


def weekly_rows(issues, merged):
    """Friday rows: printed table values on issue dates, digitized charts elsewhere."""
    core_keys = {"gross": ("US Fundamental L/S: Gross vs Net Leverage", "Gross Leverage (%)"),
                 "net": ("US Fundamental L/S: Gross vs Net Leverage", "Net Leverage (%, right)"),
                 "ls_ratio": ("US Fundamental L/S: Long/Short Ratio (MV)", "L/S Ratio")}
    series = {k: sorted(merged.get(v, {}).items()) for k, v in core_keys.items()}
    if not series["gross"]:
        return []
    by_issue = {it["issue_date"]: it for it in issues}
    start = friday_on_or_before(date.fromisoformat(series["gross"][0][0])) + timedelta(days=7)
    end = date.fromisoformat(issues[-1]["issue_date"])
    rows, d = [], start
    while d <= end:
        iso = d.isoformat()
        it = by_issue.get(iso)
        nxt = by_issue.get((d + timedelta(days=7)).isoformat())
        if it:
            t = it["table"]["rows"]
            row = {"week_ending": iso, "source": "pdf_table",
                   **{k: t["current"][k] for k in TABLE_COLS}}
            for hz in ("1y", "3y", "5y"):
                for k in ("gross", "net", "ls_ratio"):
                    v = t.get(f"pctile_{hz}", {}).get(k)
                    row[f"{k}_pctile_{hz}"] = int(v) if v is not None else None
        elif nxt and "wow_chg" in nxt["table"]["rows"]:
            # last week's level is exact from the next issue's WoW column (pt for leverage, % for ratios)
            cur, wow = nxt["table"]["rows"]["current"], nxt["table"]["rows"]["wow_chg"]
            row = {"week_ending": iso, "source": "pdf_table_wow"}
            for k in TABLE_COLS:
                row[k] = round(cur[k] / (1 + wow[k] / 100), 3) if "ls_ratio" in k else round(cur[k] - wow[k], 1)
        else:
            row = {"week_ending": iso, "source": "pdf_chart"}
            lo = (d - timedelta(days=7)).isoformat()
            for k, s in series.items():
                vals = [v for dd, v in s if lo < dd <= iso]
                row[k] = round(vals[-1], 3 if k == "ls_ratio" else 2) if vals else None
        rows.append(row)
        d += timedelta(days=7)
    return rows


def write_csvs(merged, weekly):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with DAILY_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["chart", "series", "date", "value"])
        for (chart, name), pts in sorted(merged.items()):
            for d, v in sorted(pts.items()):
                w.writerow([chart, name, d, round(v, 3)])
    cols = ["week_ending", "source", *TABLE_COLS] + [f"{k}_pctile_{hz}" for hz in ("1y", "3y", "5y")
                                                     for k in ("gross", "net", "ls_ratio")]
    with WEEKLY_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(weekly)


def merge_dashboard_store(issues, weekly, merged):
    """Upsert into state/sentiment-dashboard/gs_prime_positioning.json.

    - issue dates: printed table values and 1/3/5Y percentiles (authoritative, overwrite numbers)
    - other Fridays: digitized gross/net/LS ratio, only where no email reading sits within 3 days
    - Mag7 chart (if an issue carries one): daily points after the store's last Mag7 date
    """
    if not DASHBOARD_STORE.exists():
        return {"skipped": "dashboard store missing"}
    store = json.loads(DASHBOARD_STORE.read_text(encoding="utf-8"))
    rows = {r["date"]: dict(r) for r in store.get("leverage_history") or [] if r.get("date")}
    by_issue = {it["issue_date"]: it for it in issues}
    email_dates = [date.fromisoformat(d) for d, r in rows.items() if not str(r.get("source", "")).startswith("pdf")]
    added = updated = 0
    for w in weekly:
        d = w["week_ending"]
        if w["source"] == "pdf_table":
            it = by_issue[d]
            rec = rows.get(d, {"date": d})
            if rec.get("doc_id") and not str(rec["doc_id"]).startswith("gs_pdf"):
                rec.setdefault("email_doc_id", rec["doc_id"])
            rec.update({
                "date": d, "published_date": rec.get("published_date") or d,
                "doc_id": f"gs_pdf:{d}", "subject": it.get("subject") or f"US Equities Weekly Rundown - {d}",
                "source": "pdf_table",
                "gross_leverage": round(w["gross"], 1), "net_leverage": round(w["net"], 1),
                "ls_ratio": w["ls_ratio"], "gross_beta_adj": w["gross_beta_adj"],
                "net_beta_adj": w["net_beta_adj"], "ls_ratio_beta_adj": w["ls_ratio_beta_adj"],
            })
            for hz in ("1y", "3y", "5y"):
                rec[f"gross_pctile_{hz}"] = int(w[f"gross_pctile_{hz}"])
                rec[f"net_pctile_{hz}"] = int(w[f"net_pctile_{hz}"])
                rec[f"ls_pctile_{hz}"] = int(w[f"ls_ratio_pctile_{hz}"])
            updated += d in rows
            added += d not in rows
            rows[d] = rec
            continue
        if w.get("gross") is None or w.get("net") is None:
            continue
        dd = date.fromisoformat(d)
        if d not in rows and any(abs((dd - e).days) <= 3 for e in email_dates):
            continue
        cur = rows.get(d)
        if cur and cur.get("source") not in ("pdf_chart", "pdf_table_wow"):
            continue
        from_wow = w["source"] == "pdf_table_wow"
        rows[d] = {"date": d, "published_date": None,
                   "doc_id": f"gs_pdf:{(dd + timedelta(days=7)).isoformat()}" if from_wow
                   else f"gs_pdf_chart:{issues[-1]['issue_date']}",
                   "subject": "GS Prime 周报表格 WoW 倒推" if from_wow else "GS Prime 周报矢量图还原",
                   "source": w["source"],
                   "gross_leverage": round(w["gross"], 1), "net_leverage": round(w["net"], 1),
                   "ls_ratio": w.get("ls_ratio"),
                   **{f"{k}_pctile_{hz}": None for k in ("gross", "net") for hz in ("1y", "3y", "5y")}}
        added += cur is None
        updated += cur is not None
    lev = [rows[d] for d in sorted(rows)]

    mag7 = list(store.get("mag7_history") or [])
    mag7_added = 0
    mag7_keys = [k for k in merged if re.search(r"mag\s*7|magnificent", k[0], re.I)]
    net_key = next((k for k in mag7_keys if k[1].lower().startswith("net")), None)
    gross_key = next((k for k in mag7_keys if k[1].lower().startswith("gross")), None)
    if net_key and gross_key and mag7:
        last = mag7[-1]["date"]
        for d in sorted(set(merged[net_key]) & set(merged[gross_key])):
            if d > last:
                mag7.append({"date": d, "net_exp": round(merged[net_key][d], 2),
                             "gross_exp": round(merged[gross_key][d], 2), "doc_id": f"gs_pdf_chart:{d}"})
                mag7_added += 1

    store.update({
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "leverage_as_of": lev[-1]["date"] if lev else "",
        "leverage_count": len(lev),
        "leverage_history": lev,
        "mag7_as_of": mag7[-1]["date"] if mag7 else "",
        "mag7_count": len(mag7),
        "mag7_history": mag7,
    })
    write_json(DASHBOARD_STORE, store, indent=2)
    return {"leverage_added": added, "leverage_updated": updated, "leverage_count": len(lev),
            "leverage_as_of": store["leverage_as_of"], "mag7_added": mag7_added}


def rebuild():
    issues = load_issues()
    if not issues:
        return {"issues": 0}
    merged = merged_daily(issues)
    weekly = weekly_rows(issues, merged)
    write_csvs(merged, weekly)
    dash = merge_dashboard_store(issues, weekly, merged)
    return {"issues": len(issues), "series": len(merged), "weeks": len(weekly), "dashboard": dash,
            "daily_csv": str(DAILY_CSV.relative_to(WORKSPACE)), "weekly_csv": str(WEEKLY_CSV.relative_to(WORKSPACE))}


# ---------------------------------------------------------------- Gmail
def gws(*args, params):
    exe = shutil.which("gws") or str(Path.home() / ".openclaw/tools/node/bin/gws")
    env = dict(os.environ, PATH=f"{Path(exe).parent}:{os.environ.get('PATH', '')}")  # gateway jobs run with a bare PATH
    proc = subprocess.run([exe, "gmail", "users", "messages", *args, "--params", json.dumps(params)],
                          capture_output=True, text=True, timeout=180, env=env)
    out = proc.stdout
    if proc.returncode != 0 or "{" not in out:
        raise RuntimeError(f"gws {' '.join(args)} 失败（exit {proc.returncode}）：{(proc.stderr or out)[-400:]}")
    return json.loads(out[out.find("{"):])  # first stdout line is a keyring notice


def pdf_parts(part):
    name = part.get("filename") or ""
    if name.lower().endswith(".pdf") and part.get("body", {}).get("attachmentId"):
        yield name, part["body"]["attachmentId"]
    for sub in part.get("parts") or []:
        yield from pdf_parts(sub)


def fetch_new(state, days):
    """Download PDFs from Gmail messages not yet processed. Read-only on the mailbox (labels untouched)."""
    listed = gws("list", params={"userId": "me", "q": f"{GMAIL_QUERY} newer_than:{days}d", "maxResults": 50})
    fetched = []
    for m in listed.get("messages") or []:
        mid = m["id"]
        if mid in state["processed"]:
            continue
        msg = gws("get", params={"userId": "me", "id": mid, "format": "full"})
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("subject", "")
        received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, ET).isoformat(timespec="seconds")
        parts = [p for p in pdf_parts(msg["payload"]) if "rundown" in p[0].lower()] or list(pdf_parts(msg["payload"]))
        if not parts:
            state["processed"][mid] = {"skipped": "no pdf attachment", "subject": subject}
            continue
        name, att_id = parts[0]
        att = gws("attachments", "get", params={"userId": "me", "messageId": mid, "id": att_id})
        tmp = OUT_DIR / "incoming" / f"{mid}.pdf"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(base64.urlsafe_b64decode(att["data"] + "=" * (-len(att["data"]) % 4)))
        fetched.append({"gmail_id": mid, "subject": subject, "received_at": received, "path": tmp, "filename": name})
    return fetched


# ---------------------------------------------------------------- CLI
def write_json(path: Path, value, indent=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"processed": {}, "issues": {}}


def issue_summary(issue):
    cur = ((issue.get("table") or {}).get("rows") or {}).get("current") or {}
    return {"issue_date": issue["issue_date"], "gmail_id": issue.get("gmail_id", ""), "pdf": issue.get("pdf", ""),
            "gross": cur.get("gross"), "net": cur.get("net"), "ls_ratio": cur.get("ls_ratio"),
            "charts": [c.get("title") for c in issue["charts"]], "errors": issue.get("errors", [])}


def cmd_run(args):
    state = load_state()
    result = {"ok": True, "fetched": [], "errors": []}
    if not args.no_fetch:
        for f in fetch_new(state, args.days):
            try:
                issue = process_pdf(f["path"], subject=f["subject"], gmail_id=f["gmail_id"], received_at=f["received_at"])
            except Exception as exc:  # keep going; report below and fail the run
                result["errors"].append(f"{f['gmail_id']} {f['subject']}: {exc}")
                continue
            finally:
                f["path"].unlink(missing_ok=True)
            state["processed"][f["gmail_id"]] = {"issue_date": issue["issue_date"], "subject": f["subject"]}
            state["issues"][issue["issue_date"]] = issue_summary(issue)
            result["fetched"].append(issue_summary(issue))
            result["errors"].extend(f"{issue['issue_date']}: {e}" for e in issue["errors"])
    result["rebuild"] = rebuild()
    valid = [it["issue_date"] for it in load_issues()]
    expected = (args.expect and date.fromisoformat(args.expect)) or friday_on_or_before(datetime.now(ET).date())
    result["latest_issue"] = valid[-1] if valid else None
    result["expected_issue"] = expected.isoformat()
    if not valid or date.fromisoformat(valid[-1]) < expected:
        result["errors"].append(f"没收到 {expected.isoformat()} 那期周报（Gmail 查询：{GMAIL_QUERY}）。"
                                "先看工作邮箱的转发规则是否包含高盛 PB 周报发件人；"
                                "节假日周高盛可能停发")
    state["last_run"] = datetime.now(ET).isoformat(timespec="seconds")
    state["last_issue"] = result["latest_issue"]
    write_json(STATE_PATH, state)
    result["ok"] = not result["errors"]
    write_json(LAST_RUN_PATH, result)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0 if result["ok"] else 1


def cmd_process(args):
    issue = process_pdf(Path(args.pdf), subject=args.subject or "")
    state = load_state()
    state["issues"][issue["issue_date"]] = issue_summary(issue)
    write_json(STATE_PATH, state)
    out = {"issue": issue_summary(issue), "rebuild": rebuild()}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if issue["errors"] else 0


def cmd_self_test(args):
    """Parse a sample issue without touching any store; checks the 10-23 prototype values."""
    import fitz

    sample = Path(args.pdf) if args.pdf else PDF_DIR / "US_Equities_Weekly_Rundown_2026-10-23.pdf"
    if not sample.exists():
        print(json.dumps({"ok": False, "error": f"样本 PDF 不存在：{sample}"}, ensure_ascii=False))
        return 1
    doc = fitz.open(str(sample))
    charts = digitize(doc)
    for c in charts:
        for s in c["series"]:
            s["daily"] = [(d.isoformat(), round(v, 4)) for d, v in to_daily(s.pop("points"))]
    issue = {"issue_date": issue_date_of(doc).isoformat(), "table": parse_leverage_table(doc), "charts": charts}
    errors = validate(issue)
    checks = {"table_rows": len(issue["table"]["rows"]) if issue["table"] else 0, "charts": len(charts),
              "validate_errors": errors}
    if issue["issue_date"] == "2026-10-23":
        cur = issue["table"]["rows"]["current"]
        expect = {"gross": 216.1, "net": 43.4, "ls_ratio": 1.503, "gross_beta_adj": 226.3}
        checks["table_2026_10_23"] = all(cur[k] == v for k, v in expect.items())
        checks["net_pctile_5y_zero"] = issue["table"]["rows"]["pctile_5y"]["net"] == 0
        checks["first_date"] = core_series(issue)["gross"][0][0]
    ok = not errors and checks["table_rows"] >= 8 and checks["charts"] >= 3 \
        and checks.get("table_2026_10_23", True) and checks.get("net_pctile_5y_zero", True)
    print(json.dumps({"ok": ok, "sample": str(sample), **checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch new issues from Gmail, process, merge, check this week's issue arrived")
    r.add_argument("--days", type=int, default=21, help="Gmail lookback (use a large value to backfill)")
    r.add_argument("--no-fetch", action="store_true")
    r.add_argument("--expect", help="override the expected latest issue date (YYYY-MM-DD)")
    r.add_argument("--json", action="store_true")
    p = sub.add_parser("process", help="process a local PDF (manual backfill)")
    p.add_argument("pdf")
    p.add_argument("--subject")
    sub.add_parser("rebuild", help="re-merge CSVs and dashboard store from archived issues")
    t = sub.add_parser("self-test")
    t.add_argument("--pdf")
    args = ap.parse_args()
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "process":
        return cmd_process(args)
    if args.cmd == "rebuild":
        print(json.dumps(rebuild(), ensure_ascii=False, indent=2))
        return 0
    return cmd_self_test(args)


if __name__ == "__main__":
    sys.exit(main())
