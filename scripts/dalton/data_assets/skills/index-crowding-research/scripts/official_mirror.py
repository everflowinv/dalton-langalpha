#!/usr/bin/env python3
"""Mirror the public Index Master crowding ranking into local artifacts.

This module intentionally keeps the vendor score separate from the locally
reconstructed six-factor score in ``index_crowding.py``.  It retrieves the
same ranking payload used by the public product and writes a searchable HTML
page plus machine-readable JSON/CSV snapshots.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import tempfile
from pathlib import Path
from openclaw_workspace import workspace_root as resolve_workspace

from typing import Any

import pandas as pd
import requests


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = resolve_workspace(__file__)
OUTPUT_DIR = WORKSPACE / "research-outputs" / "index-crowding"
STATE_DIR = SKILL_ROOT / "state" / "cache" / "official"
HISTORY_DIR = SKILL_ROOT / "state" / "cache" / "history"

RANK_URL = "https://fund.wacaijijin.com/extension/api/index/zone/queryRankList"
DETAIL_URL = "https://fund.wacaijijin.com/extension/indexZoneDetail"
STATIC_RANK_URL = (
    "https://index.wacaijijin.com/index-official-static/indexCrowdedList/"
    "{date}-%E6%8B%A5%E6%8C%A4%E5%BA%A6%E6%8E%92%E8%A1%8C.html"
)

CATEGORY_LABELS = {
    0: "行业",
    1: "主题",
    2: "风格",
    3: "宽基",
    4: "海外",
}

CROWDING_LABELS = {
    1: "高",
    2: "偏高",
    3: "中等",
    4: "较低",
    5: "低",
}


class OfficialDataError(RuntimeError):
    pass


def atomic_write_text(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        frame.to_csv(temp_name, index=False, encoding="utf-8-sig")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def probability_fields(row: dict[str, Any]) -> tuple[str | None, float | None]:
    rise = safe_float(row.get("crowdedIncreaseOdds"))
    fall = safe_float(row.get("crowdedDeclineOdds"))
    if rise is not None and rise > 0:
        return "上涨", rise
    if fall is not None and fall > 0:
        return "下跌", fall
    if rise is not None and rise < 0:
        return "下跌", abs(rise)
    return None, None


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("windCode") or "").strip()
    name = str(row.get("windName") or "").strip()
    score = safe_float(row.get("crowdedPercentile"))
    if not code or not name:
        raise OfficialDataError(f"ranking row is incomplete: code={code!r}, name={name!r}")
    crowded_level = int(row.get("crowded")) if row.get("crowded") is not None else None
    category = int(row.get("zoneCategory")) if row.get("zoneCategory") is not None else None
    direction, probability = probability_fields(row)
    return {
        "index_code": code,
        "index_name": name,
        "category_code": category,
        "category": CATEGORY_LABELS.get(category, "其他"),
        "crowding_percentile": round(score, 4) if score is not None else None,
        "crowding_level_code": crowded_level,
        "crowding_level": CROWDING_LABELS.get(crowded_level, "暂无"),
        "probability_direction": direction,
        "probability_pct": probability,
        "probability_horizon_days": (
            int(row["crowdedCalculateDays"])
            if row.get("crowdedCalculateDays") is not None
            else None
        ),
        "historical_avg_return_pct": safe_float(row.get("crowdedAvgIncrease")),
        "daily_change_pct": safe_float(row.get("sdqPctchange")),
        "trade_date": str(row.get("crowdedTradeDt") or ""),
        "detail_url": f"{DETAIL_URL}?indexCode={code}&popup=1",
    }


def fetch_official_ranking(timeout: int = 35) -> dict[str, Any]:
    params = {
        "searchContent": "",
        "firstCategory": "",
        "secondCategory": "",
        "pageNo": 1,
        "pageSize": 500,
        "sortType": 18,
        "sortOrder": 1,
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://fund.wacaijijin.com/extension/indexRank",
        "User-Agent": "Mozilla/5.0 (compatible; EveIndexCrowdingMirror/1.0; research)",
    }
    response = requests.get(RANK_URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise OfficialDataError(f"vendor API error: {payload.get('error') or payload.get('code')}")
    page = payload.get("data") or {}
    raw_rows = page.get("data") or []
    rows = [normalize_row(row) for row in raw_rows]
    rows.sort(
        key=lambda item: (
            item["crowding_percentile"] is None,
            item["crowding_percentile"] if item["crowding_percentile"] is not None else 999.0,
            item["index_code"],
        )
    )
    total_count = int(page.get("totalCount") or len(rows))
    if len(rows) != total_count:
        raise OfficialDataError(
            f"ranking payload incomplete: received {len(rows)} of {total_count} rows"
        )
    if not rows:
        raise OfficialDataError("vendor ranking returned no rows")

    trade_date = str(page.get("maxCrowdedTradeDt") or rows[0]["trade_date"])
    if len(trade_date) == 8 and trade_date.isdigit():
        trade_date_iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    else:
        trade_date_iso = trade_date
    ranked = 0
    for row in rows:
        if row["crowding_percentile"] is not None:
            ranked += 1
            row["rank_low_to_high"] = ranked
        else:
            row["rank_low_to_high"] = None
        row["trade_date"] = trade_date_iso

    return {
        "ok": True,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "Index Master public ranking API",
        "source_url": RANK_URL,
        "static_html_url": STATIC_RANK_URL.format(date=trade_date_iso),
        "trade_date": trade_date_iso,
        "index_count": len(rows),
        "methodology": "vendor_score_mirror_not_local_recalculation",
        "rows": rows,
    }


def history_path(code: str) -> Path:
    return HISTORY_DIR / f"{code.replace('.', '_')}.json"


def one_year_series(payload: dict[str, Any], trade_date: str) -> list[list[Any]]:
    """Return [date, price-index, crowding] rows for the trailing calendar year."""
    end = pd.Timestamp(trade_date)
    start = end - pd.DateOffset(years=1)
    valid: list[tuple[pd.Timestamp, float, float]] = []
    # 2026-11-13: the vendor started nesting the series as {"data": {"data": [...]}}.
    inner = payload.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("data"), list):
        payload = {**payload, "data": inner["data"]}
    for item in payload.get("data") or []:
        raw_date = str(item.get("tradeDt") or "")
        try:
            date = pd.to_datetime(raw_date, format="%Y%m%d")
        except (TypeError, ValueError):
            continue
        close = safe_float(item.get("close"))
        crowding = safe_float(item.get("value"))
        if date < start or date > end or close is None or close <= 0 or crowding is None:
            continue
        valid.append((date, close, crowding))
    valid.sort(key=lambda item: item[0])
    if len(valid) < 2:
        return []
    base = valid[0][1]
    return [
        [date.strftime("%Y-%m-%d"), round(close / base * 100.0, 3), round(crowding, 4)]
        for date, close, crowding in valid
    ]


def attach_one_year_history(snapshot: dict[str, Any]) -> dict[str, Any]:
    covered = 0
    points = 0
    for row in snapshot["rows"]:
        path = history_path(row["index_code"])
        series: list[list[Any]] = []
        if path.is_file():
            try:
                series = one_year_series(json.loads(path.read_text(encoding="utf-8")), snapshot["trade_date"])
            except (OSError, json.JSONDecodeError):
                series = []
        row["history_1y"] = series
        if series:
            covered += 1
            points += len(series)
    snapshot["history_1y"] = {
        "covered_indices": covered,
        "total_indices": snapshot["index_count"],
        "points": points,
        "price_scale": "first_valid_close_equals_100",
        "crowding_scale": "vendor_score_0_to_100",
    }
    return snapshot


def rows_frame(snapshot: dict[str, Any]) -> pd.DataFrame:
    columns = [
        "rank_low_to_high",
        "index_name",
        "index_code",
        "category",
        "crowding_percentile",
        "crowding_level",
        "probability_direction",
        "probability_pct",
        "probability_horizon_days",
        "historical_avg_return_pct",
        "daily_change_pct",
        "trade_date",
        "detail_url",
    ]
    return pd.DataFrame(snapshot["rows"])[columns]


def render_html(snapshot: dict[str, Any]) -> str:
    rows_json = json.dumps(snapshot["rows"], ensure_ascii=False).replace("</", "<\\/")
    counts = {
        label: sum(row["crowding_level"] == label for row in snapshot["rows"])
        for label in ("低", "较低", "中等", "偏高", "高", "暂无")
    }
    generated = html.escape(snapshot["generated_at"])
    trade_date = html.escape(snapshot["trade_date"])
    count = int(snapshot["index_count"])
    static_url = html.escape(snapshot["static_html_url"], quote=True)
    api_url = html.escape(snapshot["source_url"], quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>指数拥挤度全市场排名 — {trade_date}</title>
  <style>
    :root {{ --ink:#14213d; --muted:#718096; --line:#e7ecf4; --blue:#2f6fed;
      --bg:#f5f8fd; --card:#fff; --red:#d94b40; --green:#1f9d72; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--bg); font-family:-apple-system,
      BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }}
    .wrap {{ max-width:1380px; margin:0 auto; padding:32px 24px 56px; }}
    .hero {{ padding:28px 30px; border-radius:22px; color:#fff;
      background:linear-gradient(125deg,#143c91,#3977ed 62%,#7aa5ff); box-shadow:0 14px 35px #193e8928; }}
    .eyebrow {{ font-size:13px; letter-spacing:.08em; opacity:.82; }}
    h1 {{ margin:8px 0 8px; font-size:32px; line-height:1.2; }}
    .subtitle {{ margin:0; opacity:.9; line-height:1.7; }}
    .stats {{ display:grid; grid-template-columns:repeat(7,minmax(90px,1fr)); gap:12px; margin:18px 0; }}
    .stat {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 16px; }}
    .stat b {{ display:block; font-size:22px; margin-top:4px; }}
    .stat span {{ color:var(--muted); font-size:12px; }}
    .controls {{ display:grid; grid-template-columns:minmax(220px,2fr) repeat(3,minmax(130px,1fr));
      gap:10px; padding:14px; background:var(--card); border:1px solid var(--line);
      border-radius:16px 16px 0 0; }}
    input,select {{ width:100%; padding:11px 12px; border:1px solid #d9e0ec; border-radius:9px;
      background:#fff; color:var(--ink); font-size:14px; outline:none; }}
    input:focus,select:focus {{ border-color:var(--blue); box-shadow:0 0 0 3px #2f6fed16; }}
    .table-shell {{ overflow:auto; background:#fff; border:1px solid var(--line); border-top:0;
      border-radius:0 0 16px 16px; box-shadow:0 8px 25px #24477b0a; }}
    table {{ width:100%; border-collapse:collapse; min-width:1280px; }}
    th {{ position:sticky; top:0; z-index:2; background:#f9fbff; color:#65738a; text-align:left;
      font-size:12px; font-weight:600; padding:12px 14px; border-bottom:1px solid var(--line); }}
    td {{ padding:13px 14px; border-bottom:1px solid #edf1f7; vertical-align:middle; font-size:14px; }}
    tbody tr:hover {{ background:#f8fbff; }}
    .rank {{ color:#8995a8; font-variant-numeric:tabular-nums; }}
    .name a {{ color:var(--ink); font-weight:650; text-decoration:none; }}
    .name a:hover {{ color:var(--blue); }}
    .code {{ color:#9aa5b5; font-size:12px; margin-top:3px; }}
    .tag {{ display:inline-block; min-width:44px; text-align:center; padding:4px 8px; border-radius:999px;
      font-size:12px; background:#edf3ff; color:#3a68ba; }}
    .score-cell {{ min-width:190px; }}
    .score-top {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; }}
    .score {{ font-size:17px; font-weight:750; font-variant-numeric:tabular-nums; }}
    .level {{ color:var(--muted); font-size:12px; }}
    .bar {{ height:5px; background:#edf1f7; border-radius:5px; overflow:hidden; margin-top:7px; }}
    .bar i {{ display:block; height:100%; border-radius:5px; }}
    .chart-cell {{ min-width:218px; width:218px; }}
    .spark {{ width:190px; padding:6px 7px 5px; border:1px solid #e7edf6; border-radius:9px;
      background:linear-gradient(180deg,#fff,#f8fbff); cursor:zoom-in; outline:none; }}
    .spark:hover,.spark:focus {{ border-color:#a8bde6; box-shadow:0 0 0 3px #2f6fed12; }}
    .spark svg {{ display:block; width:174px; height:56px; overflow:visible; }}
    .spark .grid,.chart-zoom .grid {{ stroke:#dfe6f0; stroke-width:.7; stroke-dasharray:3 3; }}
    .spark .price-line,.chart-zoom .price-line {{ fill:none; stroke:#2f6fed; stroke-width:1.7; stroke-linejoin:round;
      stroke-linecap:round; }}
    .spark .crowd-line,.chart-zoom .crowd-line {{ fill:none; stroke:#e17835; stroke-width:1.6; stroke-linejoin:round;
      stroke-linecap:round; }}
    .spark .price-dot,.chart-zoom .price-dot {{ fill:#2f6fed; }}
    .spark .crowd-dot,.chart-zoom .crowd-dot {{ fill:#e17835; }}
    .spark-meta {{ display:flex; justify-content:space-between; gap:8px; margin-top:3px;
      font-size:10px; color:#7a879a; white-space:nowrap; }}
    .spark-empty {{ width:190px; height:76px; display:flex; align-items:center; justify-content:center;
      color:#9aa5b5; font-size:12px; border:1px dashed #dce3ee; border-radius:9px; }}
    .legend-dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:4px; }}
    .legend-price {{ background:#2f6fed; }} .legend-crowd {{ background:#e17835; }}
    .chart-zoom {{ position:fixed; z-index:1000; width:min(520px,calc(100vw - 24px)); padding:15px 17px 13px;
      background:#fff; border:1px solid #cfd9e8; border-radius:14px; box-shadow:0 20px 65px #16243f35;
      pointer-events:none; }}
    .chart-zoom[hidden] {{ display:none; }}
    .zoom-title {{ display:flex; justify-content:space-between; align-items:baseline; gap:16px;
      margin-bottom:7px; font-weight:700; }}
    .zoom-title span {{ color:var(--muted); font-size:11px; font-weight:500; }}
    .chart-zoom svg {{ width:100%; height:180px; display:block; }}
    .chart-zoom .spark-meta {{ margin-top:7px; font-size:12px; }}
    .zoom-note {{ margin-top:8px; color:#7b8798; font-size:11px; line-height:1.5; }}
    .up {{ color:var(--red); }} .down {{ color:var(--green); }} .flat {{ color:var(--muted); }}
    .prob {{ font-weight:650; }} .small {{ color:var(--muted); font-size:12px; }}
    .notes {{ margin-top:18px; padding:17px 20px; background:#fff; border:1px solid var(--line);
      border-radius:14px; color:#5d6b80; font-size:13px; line-height:1.75; }}
    .notes a {{ color:var(--blue); }}
    .empty {{ text-align:center; padding:44px; color:var(--muted); }}
    @media (max-width:900px) {{ .stats {{ grid-template-columns:repeat(3,1fr); }}
      .controls {{ grid-template-columns:1fr 1fr; }} }}
    @media (max-width:560px) {{ .wrap {{ padding:16px 10px 36px; }} .hero {{ padding:22px 20px; }}
      h1 {{ font-size:25px; }} .stats {{ grid-template-columns:repeat(2,1fr); }}
      .controls {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <div class="eyebrow">INDEX MASTER · OFFICIAL SCORE MIRROR</div>
      <h1>指数拥挤度全市场排名</h1>
      <p class="subtitle">原版公开数据镜像 · 截止 {trade_date} · 共 {count} 个指数</p>
    </section>
    <section class="stats">
      <div class="stat"><span>全部指数</span><b>{count}</b></div>
      <div class="stat"><span>低拥挤</span><b>{counts['低']}</b></div>
      <div class="stat"><span>较低</span><b>{counts['较低']}</b></div>
      <div class="stat"><span>中等</span><b>{counts['中等']}</b></div>
      <div class="stat"><span>偏高</span><b>{counts['偏高']}</b></div>
      <div class="stat"><span>高拥挤</span><b>{counts['高']}</b></div>
      <div class="stat"><span>暂无一年数据</span><b>{counts['暂无']}</b></div>
    </section>
    <section class="controls">
      <input id="search" placeholder="搜索指数名称或代码">
      <select id="category"><option value="">全部类型</option><option>行业</option><option>主题</option>
        <option>风格</option><option>宽基</option><option>海外</option><option>其他</option></select>
      <select id="level"><option value="">全部档位</option><option>低</option><option>较低</option>
        <option>中等</option><option>偏高</option><option>高</option><option>暂无</option></select>
      <select id="sort"><option value="asc">拥挤度：低到高</option><option value="desc">拥挤度：高到低</option>
        <option value="changeDesc">当日涨幅：高到低</option><option value="probDesc">历史概率：高到低</option></select>
    </section>
    <section class="table-shell">
      <table><thead><tr><th>排名</th><th>指数</th><th>类型</th><th>拥挤度</th>
        <th><span class="legend-dot legend-price"></span>价格 <span class="legend-dot legend-crowd" style="margin-left:8px"></span>拥挤度（1年）</th>
        <th>原版历史概率</th><th>回测周期</th><th>当日涨跌</th></tr></thead><tbody id="rows"></tbody></table>
      <div id="empty" class="empty" hidden>没有符合条件的指数</div>
    </section>
    <section class="notes">
      <b>口径说明：</b>本页直接镜像原版公开排名字段，不是本地六因子重算结果。每行小图使用本地缓存的过去一年原版历史：蓝线为指数价格（窗口起点=100、独立纵轴），橙线为拥挤度（0–100、独立纵轴）；两线高度不能直接比较。鼠标悬停小图可放大。原版“历史概率”按其页面说明，使用近3年中拥挤度落入当前档位的日度样本计算，样本存在大量重叠，因此不能理解为独立预测胜率。<br>
      数据生成：{generated} · <a href="{static_url}">原版每日 HTML</a> · <a href="{api_url}">原版排名 API</a>
    </section>
  </main>
  <div id="chartZoom" class="chart-zoom" hidden></div>
  <script>
    const DATA = {rows_json};
    const el = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}}[c]));
    const color = v => v == null ? '#b8c1ce' : v < 15 ? '#27a17b' : v < 40 ? '#66a687' : v < 60 ? '#d5a637' : v < 85 ? '#e47a45' : '#d94b40';
    const pct = v => v == null ? '--' : `${{Number(v).toFixed(2)}}%`;
    const pathFor = (points, key, min, max, w=174, h=56, pad=3) => {{
      const span = max-min || 1;
      return points.map((p,i) => {{
        const x = pad + i*(w-2*pad)/Math.max(1,points.length-1);
        const y = h-pad-(Number(p[key])-min)*(h-2*pad)/span;
        return `${{i?'L':'M'}}${{x.toFixed(2)}},${{y.toFixed(2)}}`;
      }}).join(' ');
    }};
    function spark(r) {{
      const points = r.history_1y || [];
      if (points.length < 2) return '<div class="spark-empty">暂无一年历史</div>';
      const prices = points.map(p=>Number(p[1])), pMin0=Math.min(...prices), pMax0=Math.max(...prices);
      const padding = Math.max((pMax0-pMin0)*0.06,0.8), pMin=pMin0-padding, pMax=pMax0+padding;
      const pricePath=pathFor(points,1,pMin,pMax), crowdPath=pathFor(points,2,0,100);
      const last=points[points.length-1], ret=Number(last[1])-100, retText=`${{ret>=0?'+':''}}${{ret.toFixed(1)}}%`;
      const priceY=56-3-(Number(last[1])-pMin)*50/(pMax-pMin||1), crowdY=56-3-Number(last[2])*0.5;
      return `<div class="spark" tabindex="0" data-name="${{esc(r.index_name)}}" data-start="${{esc(points[0][0])}}" data-end="${{esc(last[0])}}">
        <svg viewBox="0 0 174 56" role="img" aria-label="${{esc(r.index_name)}}过去一年价格与拥挤度">
          <line class="grid" x1="3" y1="28" x2="171" y2="28"></line>
          <path class="price-line" d="${{pricePath}}"></path><path class="crowd-line" d="${{crowdPath}}"></path>
          <circle class="price-dot" cx="171" cy="${{priceY.toFixed(2)}}" r="2.1"></circle>
          <circle class="crowd-dot" cx="171" cy="${{crowdY.toFixed(2)}}" r="2.1"></circle>
        </svg><div class="spark-meta"><span>价格 ${{retText}}</span><span>拥挤 ${{Number(last[2]).toFixed(1)}}</span></div></div>`;
    }}
    function render() {{
      const q = el('search').value.trim().toLowerCase(), cat = el('category').value,
        level = el('level').value, sort = el('sort').value;
      let rows = DATA.filter(r => (!q || `${{r.index_name}} ${{r.index_code}}`.toLowerCase().includes(q))
        && (!cat || r.category === cat) && (!level || r.crowding_level === level));
      rows.sort((a,b) => sort === 'desc' ? (b.crowding_percentile ?? -1)-(a.crowding_percentile ?? -1)
        : sort === 'changeDesc' ? (b.daily_change_pct ?? -999)-(a.daily_change_pct ?? -999)
        : sort === 'probDesc' ? (b.probability_pct ?? -1)-(a.probability_pct ?? -1)
        : (a.crowding_percentile ?? 999)-(b.crowding_percentile ?? 999));
      el('rows').innerHTML = rows.map((r,i) => {{
        const changeClass = r.daily_change_pct > 0 ? 'up' : r.daily_change_pct < 0 ? 'down' : 'flat';
        const probClass = r.probability_direction === '上涨' ? 'up' : r.probability_direction === '下跌' ? 'down' : 'flat';
        return `<tr><td class="rank">${{r.rank_low_to_high ?? '--'}}</td><td class="name"><a target="_blank" rel="noreferrer" href="${{esc(r.detail_url)}}">${{esc(r.index_name)}}</a><div class="code">${{esc(r.index_code)}}</div></td>
          <td><span class="tag">${{esc(r.category)}}</span></td><td class="score-cell"><div class="score-top"><span class="score">${{pct(r.crowding_percentile)}}</span><span class="level">${{esc(r.crowding_level)}}</span></div><div class="bar"><i style="width:${{Math.max(1,r.crowding_percentile)}}%;background:${{color(r.crowding_percentile)}}"></i></div></td>
          <td class="chart-cell">${{spark(r)}}</td>
          <td><span class="prob ${{probClass}}">${{r.probability_direction ? esc(r.probability_direction)+' '+pct(r.probability_pct) : '--'}}</span></td><td>${{r.probability_horizon_days ? r.probability_horizon_days+'日' : '--'}}</td>
          <td class="${{changeClass}}">${{r.daily_change_pct == null ? '--' : (r.daily_change_pct>0?'+':'')+pct(r.daily_change_pct)}}</td></tr>`;
      }}).join('');
      el('empty').hidden = rows.length > 0;
    }}
    ['search','category','level','sort'].forEach(id => el(id).addEventListener(id === 'search' ? 'input' : 'change', render));
    const zoom=el('chartZoom');
    function placeZoom(x,y) {{
      const margin=12, w=zoom.offsetWidth, h=zoom.offsetHeight;
      let left=x+18, top=y-h/2;
      if (left+w+margin>window.innerWidth) left=x-w-18;
      zoom.style.left=`${{Math.max(margin,left)}}px`;
      zoom.style.top=`${{Math.max(margin,Math.min(window.innerHeight-h-margin,top))}}px`;
    }}
    function showZoom(s,x,y) {{
      zoom.innerHTML=`<div class="zoom-title">${{esc(s.dataset.name)}}<span>${{esc(s.dataset.start)}} → ${{esc(s.dataset.end)}}</span></div>${{s.innerHTML}}<div class="zoom-note"><span class="legend-dot legend-price"></span>蓝线：价格，窗口起点=100、独立纵轴　<span class="legend-dot legend-crowd"></span>橙线：拥挤度0–100、独立纵轴</div>`;
      zoom.hidden=false; placeZoom(x,y);
    }}
    el('rows').addEventListener('pointerover',e=>{{const s=e.target.closest('.spark'); if(s&&!s.contains(e.relatedTarget)) showZoom(s,e.clientX,e.clientY);}});
    el('rows').addEventListener('pointermove',e=>{{if(!zoom.hidden) placeZoom(e.clientX,e.clientY);}});
    el('rows').addEventListener('pointerout',e=>{{const s=e.target.closest('.spark'); if(s&&!s.contains(e.relatedTarget)) zoom.hidden=true;}});
    el('rows').addEventListener('focusin',e=>{{const s=e.target.closest('.spark'); if(s) showZoom(s,window.innerWidth/2,window.innerHeight/2);}});
    el('rows').addEventListener('focusout',e=>{{if(e.target.closest('.spark')) zoom.hidden=true;}});
    render();
  </script>
</body>
</html>
"""


def write_outputs(snapshot: dict[str, Any]) -> dict[str, str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "official-all.json"
    csv_path = OUTPUT_DIR / "official-all.csv"
    html_path = OUTPUT_DIR / "official-all.html"
    atomic_write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), json_path)
    atomic_write_csv(rows_frame(snapshot), csv_path)
    atomic_write_text(render_html(snapshot), html_path)
    atomic_write_text(json.dumps(snapshot, ensure_ascii=False), STATE_DIR / "latest.json")
    return {
        "json": str(json_path),
        "csv": str(csv_path),
        "html": str(html_path),
    }


def run_self_test() -> dict[str, Any]:
    fixture = {
        "windCode": "931866.CSI",
        "windName": "中证机床",
        "zoneCategory": 0,
        "crowded": 5,
        "crowdedPercentile": 12.9333,
        "crowdedCalculateDays": 180,
        "crowdedIncreaseOdds": 97.56,
        "crowdedDeclineOdds": None,
        "crowdedAvgIncrease": 38.53,
        "sdqPctchange": 4.6983,
        "crowdedTradeDt": "20260805",
    }
    row = normalize_row(fixture)
    history = one_year_series(
        {
            "data": [
                {"tradeDt": "20250804", "value": 20, "close": 1000},
                {"tradeDt": "20260804", "value": 60, "close": 1120},
                {"tradeDt": "20260805", "value": 65, "close": 1100},
            ]
        },
        "2026-08-05",
    )
    row["rank_low_to_high"] = 1
    row["history_1y"] = history
    rendered = render_html(
        {
            "generated_at": "2026-08-05T00:00:00-04:00",
            "trade_date": "2026-08-05",
            "index_count": 1,
            "static_html_url": "https://example.com/rank.html",
            "source_url": "https://example.com/api",
            "rows": [row],
        }
    )
    checks = [
        {"name": "normalize_score", "ok": row["crowding_percentile"] == 12.9333},
        {"name": "normalize_bucket", "ok": row["crowding_level"] == "低"},
        {
            "name": "normalize_probability",
            "ok": row["probability_direction"] == "上涨" and row["probability_pct"] == 97.56,
        },
        {
            "name": "vendor_grid",
            "ok": abs(row["crowding_percentile"] * 7.5 - round(row["crowding_percentile"] * 7.5)) < 0.001,
        },
        {
            "name": "one_year_history_window",
            "ok": len(history) == 2 and history[0][1] == 100.0 and history[-1][1] == 98.214,
        },
        {
            "name": "zoom_svg_styles",
            "ok": all(
                selector in rendered
                for selector in (
                    ".spark .price-line,.chart-zoom .price-line",
                    ".spark .crowd-line,.chart-zoom .crowd-line",
                    ".spark .price-dot,.chart-zoom .price-dot",
                    ".spark .crowd-dot,.chart-zoom .crowd-dot",
                )
            ),
        },
    ]
    return {"ok": all(check["ok"] for check in checks), "checks": checks}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirror all official index crowding scores")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true", help="print run metadata as JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        result = run_self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    snapshot = attach_one_year_history(fetch_official_ranking())
    paths = write_outputs(snapshot)
    result = {
        "ok": True,
        "trade_date": snapshot["trade_date"],
        "index_count": snapshot["index_count"],
        "paths": paths,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{snapshot['trade_date']}: mirrored {snapshot['index_count']} indices")
        print(paths["html"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OfficialDataError, requests.RequestException, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": "check the public ranking endpoint or retry after the vendor updates",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)
