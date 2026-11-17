#!/usr/bin/env python3
"""Render static HK Momentum Indices monitor HTML with 60s grant-aware polling (no CDN)."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from scripts.dalton.monitor_feeds.common import (
    SHARED_MONITOR_CSS,
    SourceStatusStore,
    atomic_write_text,
    build_grant_poller_js,
    format_source_statuses,
    load_json_safe,
    render_color_legend,
    resolve_state_dir,
)
from scripts.dalton.monitor_feeds.hk_momentum.ui_copy import UI_COPY

HKMOM_EXTRA_CSS = r"""
.chart-box{width:100%;height:420px;position:relative}
.chart-box canvas{width:100%;height:100%;display:block}
.tooltip{display:none;position:absolute;pointer-events:none;background:#050b15f0;border:1px solid #405777;
  border-radius:8px;padding:8px 11px;font-size:12px;line-height:1.5;box-shadow:0 8px 24px #0008;white-space:nowrap;z-index:20}
.controls{margin:12px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.showmore{color:var(--blue);cursor:pointer;font-size:12px;margin-top:8px;display:inline-block}
.showmore:hover{text-decoration:underline}
.partial-note{font-size:11px;color:var(--temp-warm);margin-top:4px}
"""


def render_hkmom_html(
    *,
    series: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    constituents: dict[str, Any] | None = None,
    intraday: dict[str, Any] | None = None,
) -> str:
    cp = json.loads(json.dumps(UI_COPY, ensure_ascii=False))
    meta_dict = meta or {}
    params = meta_dict.get("params") or {}
    launch = meta_dict.get("launch_date") or meta_dict.get("last_date") or "上线日"
    fmt_vals = {
        "base_date": meta_dict.get("base_date") or "—",
        "last_date": meta_dict.get("last_date") or "—",
        "top_n": str(params.get("top_n") or 50),
        "smallcap_limit": f"{float(params.get('smallcap_limit_e8') or 50):g}",
        "min_turn_wan": f"{float(params.get('min_turnover_wan') or 100):.0f}",
        "min_price": f"{float(params.get('min_price') or 0.10):.2f}",
        "launch_date": str(launch),
    }
    resolved_subline = cp["subtitle_tpl"].format(**fmt_vals)
    resolved_bullets = [b.format(**fmt_vals) for b in cp["methodology"]["bullets_tpl"]]
    cp["subtitle_tpl"] = resolved_subline
    cp["subtitle_resolved"] = resolved_subline
    cp["methodology"]["bullets_tpl"] = resolved_bullets
    cp["methodology"]["bullets_resolved"] = resolved_bullets

    payload = {
        "series": series or {},
        "meta": meta_dict,
        "constituents": constituents or {},
        "intraday": intraday or {},
        "copy": cp,
    }
    seed_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    legend_html = render_color_legend(cp["legend_dir"], cp["legend_sec"], cp["legend_note"])
    poller_js = build_grant_poller_js(
        "intraday",
        on_data_fn="onMonitorData",
        on_error_fn="onMonitorPollError",
        on_stale_fn="onMonitorStale",
        interval_ms=60_000,
        market="hk",
    )
    range_buttons = "".join(
        f'<button class="tab{" active" if r == 0 else ""}" data-r="{r}">{html.escape(lbl)}</button>'
        for r, lbl in cp["ranges"]
    )

    js = r"""
const BOOT = JSON.parse(document.getElementById('hkmom-seed').textContent || '{}');
const COPY = BOOT.copy;
const S = { range: 0, expanded: {} };
let seriesData = BOOT.series && BOOT.series.dates ? BOOT.series : null;
let meta = BOOT.meta && Object.keys(BOOT.meta).length ? BOOT.meta : null;
let cons = BOOT.constituents || {};
let live = BOOT.intraday && Object.keys(BOOT.intraday).length ? BOOT.intraday : null;
window.__DALTON_LAST_PAYLOAD__ = live;
let pollErr = '';
let clientStaleReason = '';

const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
const fmtPct = v => (v === null || v === undefined || Number.isNaN(Number(v))) ? '—' : ((v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%');
const cls = v => (v === null || v === undefined) ? 'muted' : (v > 0 ? 'up' : v < 0 ? 'down' : 'muted');

function formatSourcesStale(sources) {
  if (!sources || typeof sources !== 'object') return '';
  const parts = [];
  for (const [k, s] of Object.entries(sources)) {
    if (s && (!s.ok || s.stale)) {
      parts.push(`${s.label || k}：数据未更新（上次成功：${s.last_success_at || '无'}，原因：${s.reason || '未知'}）`);
    }
  }
  return parts.join('；');
}

function updateStatus() {
  const b = document.getElementById('status-pill');
  const banner = document.getElementById('stale-banner');
  const d = live || {};
  const label = d.market_status_label || (d.trading ? '港股交易中' : '已收盘');
  const asOf = d.as_of || (meta ? meta.last_date : '未知');
  const schedNote = d.session_note ? ` · ${d.session_note}` : '';
  if (pollErr) {
    b.className = 'flow-status stale';
    b.textContent = `数据未更新（上次成功：${d.last_success_at || asOf}）`;
    banner.className = 'stale-banner show';
    banner.textContent = COPY.poll_error_tpl.replace('{err}', pollErr).replace('{last_ok}', d.last_success_at || asOf);
    return;
  }
  if (clientStaleReason) {
    b.className = 'flow-status stale';
    b.textContent = `数据未更新（上次成功：${d.last_success_at || asOf}）`;
    banner.className = 'stale-banner show';
    banner.textContent = COPY.stale_banner_tpl
      .replace('{last_ok}', d.last_success_at || asOf)
      .replace('{reason}', clientStaleReason);
    return;
  }
  if (d.stale || (meta && meta.stale)) {
    const reasons = [];
    if (meta && meta.stale && meta.stale_reason) reasons.push(meta.stale_reason);
    if (d.stale && d.stale_reason && !reasons.includes(d.stale_reason)) reasons.push(d.stale_reason);
    const srcDetail = formatSourcesStale(d.sources || (meta && meta.sources));
    if (srcDetail && !reasons.some(r => r.includes('数据未更新'))) reasons.push(srcDetail);
    const lastOk = d.last_success_at || (meta && meta.last_success_at) || asOf;
    b.className = 'flow-status stale';
    b.textContent = `数据未更新（上次成功：${lastOk}）`;
    banner.className = 'stale-banner show';
    banner.textContent = COPY.stale_banner_tpl
      .replace('{last_ok}', lastOk)
      .replace('{reason}', reasons.join('；') || '部分报价暂未返回');
    return;
  }
  b.className = 'flow-status' + (d.trading ? ' live' : '');
  b.textContent = `${label} · 数据时间：${asOf} 北京时间${schedNote}`;
  banner.className = 'stale-banner';
  banner.textContent = '';
}

function renderCards() {
  const defs = [
    ['smallcap', COPY.cards.smallcap, '#4da3ff'],
    ['nonggt', COPY.cards.nonggt, '#f778ba'],
    ['hsi', COPY.cards.hsi, '#7d8590'],
  ];
  let htmlOut = '';
  for (const [k, name, color] of defs) {
    let lvl = '—', chg = '', stats = '', partial = '';
    if (k === 'hsi') {
      if (live && live.hsi && live.hsi.value != null) {
        lvl = Number(live.hsi.value).toLocaleString();
        const tag = (live.trading ? '今日(盘中)' : '今日收盘');
        chg = `<span class="${cls(live.hsi.pct)}">${fmtPct(live.hsi.pct)}</span> ${tag}`;
      } else if (seriesData && seriesData.hsi) {
        lvl = String(seriesData.hsi[seriesData.hsi.length - 1]);
      }
      if (seriesData && seriesData.hsi) {
        const norm = seriesData.hsi[seriesData.hsi.length - 1];
        stats = COPY.cards.hsi_norm_tpl.replace('{norm}', norm != null ? String(norm) : '—');
      }
    } else {
      const lv = live && live[k];
      if (lv && lv.level != null) {
        lvl = Number(lv.level).toLocaleString();
        const tag = lv.source === 'daily_close' ? '今日收盘' : (live.trading ? '今日(盘中实时)' : '今日收盘');
        chg = `<span class="${cls(lv.pct)}">${fmtPct(lv.pct)}</span> ${tag}`;
        if (lv.partial_quotes && lv.n_expected) {
          partial = `<div class="partial-note">${esc(COPY.cards.partial_tpl
            .replace('{quoted}', String(lv.n_quoted))
            .replace('{expected}', String(lv.n_expected))
            .replace('{missing}', String((lv.missing_codes || []).length)))}</div>`;
        }
      } else if (seriesData && seriesData[k]) {
        const a = seriesData[k];
        lvl = Number(a[a.length - 1]).toLocaleString();
      }
      if (meta && meta.perf && meta.perf[k]) {
        const p = meta.perf[k];
        stats = COPY.cards.stats_tpl
          .replace('{total}', (p.total_ret_pct >= 0 ? '+' : '') + p.total_ret_pct + '%')
          .replace('{cagr}', p.cagr_pct + '%')
          .replace('{vol}', p.vol_pct + '%')
          .replace('{mdd}', p.max_dd_pct + '%');
      }
    }
    htmlOut += `<div class="metric" style="border-top-color:${color}">`
      + `<div class="metric-title">${esc(name)}</div>`
      + `<div class="metric-value">${esc(lvl)}</div>`
      + `<div class="metric-sub">${chg}</div>`
      + `${partial}`
      + `<div class="metric-meta">${esc(stats)}</div></div>`;
  }
  document.getElementById('cards').innerHTML = htmlOut;
}

function drawChart(hoverIdx) {
  const box = document.getElementById('chart-box');
  const cv = document.getElementById('hkmom-canvas');
  if (!seriesData || !seriesData.dates || !seriesData.dates.length) return;
  const D = window.devicePixelRatio || 1, W = box.clientWidth, H = box.clientHeight;
  if (W < 40) return;
  cv.width = W * D; cv.height = H * D;
  const ctx = cv.getContext('2d');
  ctx.setTransform(D, 0, 0, D, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const nTotal = seriesData.dates.length;
  const start = S.range > 0 ? Math.max(0, nTotal - S.range) : 0;
  const dates = seriesData.dates.slice(start);
  const norm = arr => {
    const sub = arr.slice(start);
    const b = sub.find(x => x != null && x > 0) || 1000;
    return sub.map(x => x == null ? null : +(x / b * 1000).toFixed(2));
  };
  const lines = [
    { name: '① 小市值动量', data: norm(seriesData.smallcap), color: '#4da3ff', dash: [] },
    { name: '② 非港股通动量', data: norm(seriesData.nonggt), color: '#f778ba', dash: [] },
  ];
  if (seriesData.hsi) lines.push({ name: '恒生指数（对照）', data: norm(seriesData.hsi), color: '#7d8590', dash: [5, 4] });

  const vals = lines.flatMap(l => l.data.filter(v => v != null));
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const padV = Math.max((maxV - minV) * 0.08, 10);
  const lo = minV - padV, hi = maxV + padV;
  const pad = { l: 58, r: 22, t: 20, b: 30 };
  const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
  const xp = i => pad.l + (dates.length < 2 ? 0.5 : i / (dates.length - 1)) * pw;
  const yp = v => pad.t + (hi - v) / (hi - lo || 1) * ph;

  ctx.lineWidth = 1;
  ctx.font = '11px -apple-system,sans-serif';
  for (let k = 0; k <= 4; k++) {
    const y = pad.t + ph * k / 4;
    const val = hi - (hi - lo) * k / 4;
    ctx.strokeStyle = '#1f3251'; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = '#8294ae'; ctx.textAlign = 'right'; ctx.fillText(val.toFixed(0), pad.l - 8, y + 4);
  }
  ctx.textAlign = 'center'; ctx.fillStyle = '#8294ae';
  for (let i = 0; i < 5; i++) {
    const idx = Math.min(dates.length - 1, Math.round((dates.length - 1) * i / 4));
    ctx.fillText(dates[idx], xp(idx), H - 8);
  }

  lines.forEach(l => {
    ctx.save();
    ctx.setLineDash(l.dash);
    ctx.strokeStyle = l.color;
    ctx.lineWidth = 2.0;
    ctx.beginPath();
    let started = false;
    l.data.forEach((v, i) => {
      if (v == null) return;
      if (!started) { ctx.moveTo(xp(i), yp(v)); started = true; }
      else ctx.lineTo(xp(i), yp(v));
    });
    ctx.stroke();
    ctx.restore();
  });

  if (hoverIdx != null && hoverIdx >= 0 && hoverIdx < dates.length) {
    const hx = xp(hoverIdx);
    ctx.save(); ctx.strokeStyle = '#8ea5c4aa'; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(hx, pad.t); ctx.lineTo(hx, pad.t + ph); ctx.stroke(); ctx.restore();
  }
  box._chartState = { dates, lines, pad, pw, W };
}

function renderTables() {
  const rebal = (meta && meta.last_rebalance_date) || '上月末';
  document.getElementById('sub-smallcap').textContent = COPY.tables.smallcap_sub_tpl.replace('{rebal_date}', rebal);
  document.getElementById('sub-nonggt').textContent = COPY.tables.nonggt_sub_tpl.replace('{rebal_date}', rebal);
  for (const k of ['smallcap', 'nonggt']) {
    const rows = cons[k] || [];
    const lim = S.expanded[k] ? rows.length : 10;
    let h = `<thead><tr>${COPY.tables.headers.map((hd, idx) => `<th${idx <= 2 ? ' style="text-align:left"' : ''}>${esc(hd)}</th>`).join('')}</tr></thead><tbody>`;
    rows.slice(0, lim).forEach((r, i) => {
      h += `<tr><td style="text-align:left">${i + 1}</td><td style="text-align:left">${esc(r.code)}</td><td style="text-align:left">${esc(r.name)}</td>`
        + `<td>${r.score ?? '—'}</td>`
        + `<td class="${cls(r.ret12)}">${r.ret12 != null ? (r.ret12 >= 0 ? '+' : '') + r.ret12 + '%' : '—'}</td>`
        + `<td>${r.mktcap_e8 ?? '—'}</td></tr>`;
    });
    h += '</tbody>';
    document.getElementById('tbl-' + k).innerHTML = h;
  }
}

function renderMetaCopy() {
  if (!meta) return;
  const p = meta.params || {};
  document.getElementById('subline').textContent = COPY.subtitle_resolved || COPY.subtitle_tpl
    .replace('{base_date}', meta.base_date || '—')
    .replace('{last_date}', meta.last_date || '—')
    .replace('{top_n}', String(p.top_n || 50));
  const launch = meta.launch_date || meta.last_date || '上线日';
  const rawBullets = COPY.methodology.bullets_resolved || COPY.methodology.bullets_tpl;
  const bullets = rawBullets.map(b =>
    '<li>' + esc(b
      .replaceAll('{top_n}', String(p.top_n || 50))
      .replaceAll('{smallcap_limit}', String(p.smallcap_limit_e8 || 50))
      .replaceAll('{min_turn_wan}', Number(p.min_turnover_wan ?? 100).toFixed(0))
      .replaceAll('{min_price}', Number(p.min_price ?? 0.1).toFixed(2))
      .replaceAll('{launch_date}', launch)) + '</li>'
  ).join('');
  document.getElementById('method-list').innerHTML = bullets;
}

function onMonitorData(body) {
  pollErr = '';
  if (body && body.data) {
    live = body.data;
    window.__DALTON_LAST_PAYLOAD__ = live;
    updateStatus();
    renderCards();
  }
}

function onMonitorPollError(err) {
  pollErr = err || '请求失败';
  updateStatus();
}

function onMonitorStale(reason) {
  clientStaleReason = reason || '';
  updateStatus();
}
window.onMonitorStale = onMonitorStale;

const box = document.getElementById('chart-box');
const tip = document.getElementById('chart-tip');
box.addEventListener('mousemove', e => {
  const st = box._chartState;
  if (!st) return;
  const r = box.getBoundingClientRect();
  const mx = e.clientX - r.left;
  const frac = Math.max(0, Math.min(1, (mx - st.pad.l) / (st.pw || 1)));
  const idx = Math.round(frac * (st.dates.length - 1));
  drawChart(idx);
  const lines = st.lines.map(l => {
    const v = l.data[idx];
    return `<span style="color:${l.color}">●</span> ${esc(l.name)}: <b>${v != null ? v.toFixed(1) : '—'}</b>`;
  });
  tip.innerHTML = `<b>${esc(st.dates[idx])}</b><br>${lines.join('<br>')}`;
  tip.style.display = 'block';
  tip.style.left = Math.min(Math.max(mx + 14, 8), st.W - tip.offsetWidth - 8) + 'px';
  tip.style.top = '18px';
});
box.addEventListener('mouseleave', () => { tip.style.display = 'none'; drawChart(null); });

document.querySelectorAll('#ranges button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('#ranges button').forEach(x => x.classList.remove('active'));
    btn.classList.add('active');
    S.range = Number(btn.dataset.r);
    drawChart(null);
  };
});
document.querySelectorAll('.showmore').forEach(btn => {
  btn.onclick = () => {
    const k = btn.dataset.k;
    S.expanded[k] = !S.expanded[k];
    btn.textContent = S.expanded[k] ? COPY.tables.collapse : COPY.tables.show_all;
    renderTables();
  };
});

updateStatus();
renderCards();
renderMetaCopy();
renderTables();
drawChart(null);
window.addEventListener('resize', () => drawChart(null));
""" + poller_js

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(cp["title"])}</title>'
        f'<style>{SHARED_MONITOR_CSS}{HKMOM_EXTRA_CSS}</style></head><body>'
        '<header class="top">'
        '<div class="top-head">'
        f'<div><h1>{html.escape(cp["title"])}</h1>'
        f'<p id="subline">{html.escape(resolved_subline)}</p></div>'
        '<div class="top-meta"><span class="flow-status" id="status-pill">等待数据刷新…</span></div>'
        '</div>'
        f'{legend_html}'
        '<div class="stale-banner" id="stale-banner"></div>'
        '</header>'
        '<main class="wrap">'
        '<div class="cards" id="cards"></div>'
        '<section class="panel" style="margin-bottom:16px">'
        f'  <div class="panel-head"><h2>指数走势对比（归一化基点 1000）</h2>'
        f'  <div class="controls" id="ranges">{range_buttons}<span class="muted" style="font-size:11.5px">{html.escape(cp["range_note"])}</span></div></div>'
        '  <div class="chart-box" id="chart-box"><canvas id="hkmom-canvas"></canvas><div class="tooltip" id="chart-tip"></div></div>'
        '</section>'
        '<div class="grid g2" style="margin-bottom:16px">'
        '  <section class="panel">'
        f'    <div class="panel-head"><div><h3 style="color:var(--blue)">{html.escape(cp["tables"]["smallcap_title"])}</h3>'
        '      <div class="panel-sub" id="sub-smallcap"></div></div></div>'
        '    <div class="table-wrap"><table id="tbl-smallcap"></table></div>'
        f'    <span class="showmore" data-k="smallcap">{html.escape(cp["tables"]["show_all"])}</span>'
        '  </section>'
        '  <section class="panel">'
        f'    <div class="panel-head"><div><h3 style="color:#f778ba">{html.escape(cp["tables"]["nonggt_title"])}</h3>'
        '      <div class="panel-sub" id="sub-nonggt"></div></div></div>'
        '    <div class="table-wrap"><table id="tbl-nonggt"></table></div>'
        f'    <span class="showmore" data-k="nonggt">{html.escape(cp["tables"]["show_all"])}</span>'
        '  </section>'
        '</div>'
        '<section class="panel">'
        f'  <h3 style="margin-bottom:8px">{html.escape(cp["methodology"]["title"])}</h3>'
        f'  <ul id="method-list" style="margin:0;padding-left:20px;color:#c5d2e5;line-height:1.75;font-size:12px">{"".join(f"<li>{html.escape(b)}</li>" for b in resolved_bullets)}</ul>'
        '</section>'
        '</main>'
        f'<footer>{html.escape(cp["footer"])}</footer>'
        f'<script id="hkmom-seed" type="application/json">{seed_json}</script>'
        f'<script>{js}</script>'
        '</body></html>'
    )


def write_hkmom_panel(
    *,
    state_dir: str | Path | None = None,
    output_dir: str | Path,
    series: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    constituents: dict[str, Any] | None = None,
    intraday: dict[str, Any] | None = None,
    status_doc: dict[str, Any] | None = None,
) -> Path:
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    resolved_series = series if series is not None else (load_json_safe(st_dir / "index_series.json", {}) or {})
    resolved_meta = dict(meta) if meta is not None else (load_json_safe(st_dir / "index_meta.json", {}) or {})
    resolved_status = status_doc if status_doc is not None else SourceStatusStore(st_dir).load()
    if resolved_status.get("sources"):
        combined_src = dict(resolved_meta.get("sources") or {})
        combined_src.update(resolved_status["sources"])
        resolved_meta["sources"] = combined_src
        banner_src = format_source_statuses(combined_src)
        if banner_src:
            resolved_meta["stale"] = True
            if not resolved_meta.get("stale_reason"):
                resolved_meta["stale_reason"] = banner_src
            elif banner_src not in str(resolved_meta["stale_reason"]):
                resolved_meta["stale_reason"] = f"{resolved_meta['stale_reason']}；{banner_src}"
    resolved_cons = constituents if constituents is not None else (load_json_safe(st_dir / "constituents.json", {}) or {})
    resolved_intraday = intraday if intraday is not None else (load_json_safe(st_dir / "live.json", {}) or {})
    out_path = Path(output_dir) / "index.html"
    atomic_write_text(
        out_path,
        render_hkmom_html(
            series=resolved_series,
            meta=resolved_meta,
            constituents=resolved_cons,
            intraday=resolved_intraday,
        ),
    )
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render HK Momentum monitor HTML.")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    out = write_hkmom_panel(state_dir=args.state_dir, output_dir=args.output_dir)
    print(json.dumps({"ok": True, "output": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
