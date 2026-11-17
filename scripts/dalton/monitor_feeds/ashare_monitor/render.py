#!/usr/bin/env python3
"""Render static A-share intraday monitor HTML with 60s grant-aware live polling."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from scripts.dalton.monitor_feeds.ashare_monitor.ui_copy import UI_COPY
from scripts.dalton.monitor_feeds.common import (
    SHARED_MONITOR_CSS,
    atomic_write_text,
    build_grant_poller_js,
    render_color_legend,
)

ASHARE_EXTRA_CSS = r"""
.idxrow{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;margin-bottom:14px}
.idx{background:linear-gradient(145deg,rgba(19,36,59,.96),rgba(12,25,43,.96));
  border:1px solid var(--line);border-radius:12px;padding:11px 12px;text-align:center}
.idx .nm{color:var(--muted);font-size:12px;margin-bottom:4px}
.idx .px{font-size:20px;font-weight:750;font-variant-numeric:tabular-nums}
.idx .ch{font-size:12.5px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
.idx .amt{color:var(--muted);font-size:11px;margin-top:3px}
.metrics{display:flex;gap:24px;align-items:flex-end;flex-wrap:wrap;min-height:52px;margin:8px 0}
.metric-inline .v{font-size:24px;font-weight:750;line-height:1;font-variant-numeric:tabular-nums}
.metric-inline .k{color:var(--muted);font-size:11.5px;margin-top:5px}
.chart{width:100%;height:248px;display:block}
.monitor-row{display:grid;grid-template-columns:minmax(270px,.75fr) minmax(0,2.25fr);gap:14px}
.alerts-panel #alerts{max-height:500px;overflow:auto}
.alert{border-radius:9px;padding:8px 10px;margin-bottom:8px;font-size:12px;display:flex;gap:8px;border:1px solid transparent}
.alert.pos{background:rgba(255,107,107,.10);border-color:rgba(255,107,107,.25)}
.alert.neg{background:rgba(56,211,159,.10);border-color:rgba(56,211,159,.25)}
.alert .t{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.etf-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:10px 0 12px}
.sumcard{background:#101d31;border:1px solid var(--line);border-radius:10px;padding:9px 11px}
.sumcard .k{color:var(--muted);font-size:11px}.sumcard .v{font-size:17px;font-weight:750;margin-top:4px;font-variant-numeric:tabular-nums}
.etf-split{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.etf-block{border:1px solid var(--line);border-radius:11px;padding:10px 12px;min-width:0;background:#0b1627}
.etf-block-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.etf-block-head h4{margin:0;font-size:13.5px}
.hint{font-size:11px;color:var(--muted);font-weight:400}
.anomaly{display:inline-flex;min-width:32px;justify-content:center;padding:2px 7px;border-radius:8px;background:#1a2b45;font-weight:750}
.anomaly.hot{background:rgba(255,107,107,.22);color:#ff9aa0}
.anomaly.warm{background:rgba(255,157,60,.22);color:#ffd099}
.tag{padding:2px 8px;border-radius:999px;font-size:11px;font-weight:650;white-space:nowrap}
.tag.ok{background:#16273f;color:var(--muted)}
.tag.buy{background:rgba(255,107,107,.18);color:#ff9aa0}
.tag.sell{background:rgba(56,211,159,.16);color:#6ce0b7}
.tag.watch{background:rgba(255,157,60,.18);color:#ffd099}
.dir{font-weight:700}.topfund{font-size:10.5px;color:var(--muted)}
.industry-parent{font-size:10.5px;color:var(--muted)}
.table-controls{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:11.5px;flex-wrap:wrap}
.table-controls select{border:1px solid var(--line);border-radius:7px;background:#0c1728;color:var(--text);font-size:11.5px;padding:3px 7px}
th.sortable{cursor:pointer;user-select:none}
th.sortable::after{content:" ↕";opacity:.35;font-size:10px}
th.sortable.sorted{color:var(--blue)}
th.sortable.sorted.desc::after{content:" ▼";opacity:1}
th.sortable.sorted.asc::after{content:" ▲";opacity:1}
@media(max-width:1100px){.idxrow{grid-template-columns:repeat(4,1fr)}.monitor-row,.etf-split{grid-template-columns:1fr}.etf-summary{grid-template-columns:repeat(2,1fr)}}
@media(max-width:640px){.idxrow{grid-template-columns:repeat(2,1fr)}.etf-summary{grid-template-columns:1fr}}
"""


def render_ashare_html(initial_snapshot: dict[str, Any] | None = None) -> str:
    cp = UI_COPY
    seed = initial_snapshot or {}
    seed_json = json.dumps(seed, ensure_ascii=False).replace("</", "<\\/")
    copy_json = json.dumps(cp, ensure_ascii=False).replace("</", "<\\/")
    legend_html = render_color_legend(cp["legend_dir"], cp["legend_sec"], cp["legend_note"])
    poller_js = build_grant_poller_js(
        "snapshot",
        on_data_fn="onMonitorData",
        on_error_fn="onMonitorPollError",
        on_stale_fn="onMonitorStale",
        interval_ms=60_000,
        market="cn",
    )

    ind_headers = "".join(
        f'<th data-key="{k}" data-type="{tp}">{html.escape(label)}</th>'
        for (k, tp), label in zip(
            [
                ("name", "str"),
                ("pct", "num"),
                ("inflow_yi", "num"),
                ("share", "num"),
                ("turnover_yi", "num"),
                ("stock_count", "num"),
            ],
            cp["industry"]["headers"],
        )
    )
    etf_headers = "".join(
        f'<th data-key="{k}" data-type="{tp}">{html.escape(label)}</th>'
        for (k, tp), label in zip(
            [
                ("direction", "str"),
                ("score", "num"),
                ("weighted_pct", "num"),
                ("net_flow_yi", "num"),
                ("avg_vratio", "num"),
                ("confirmed_yi", "num"),
                ("signal", "str"),
            ],
            cp["etf"]["headers"],
        )
    )

    page_js = r"""
const INITIAL = JSON.parse(document.getElementById('ashare-seed').textContent || '{}');
const COPY = JSON.parse(document.getElementById('ashare-copy').textContent || '{}');
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? '—' : Number(v).toFixed(d);
const cls = v => (v === null || v === undefined) ? 'muted' : (v > 0 ? 'up' : v < 0 ? 'down' : 'muted');
const sign = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(d);
const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));

let last = Object.keys(INITIAL).length ? INITIAL : null;
window.__DALTON_LAST_PAYLOAD__ = last;
let pollErr = '';
let clientStaleReason = '';
const view = { breadth: 'day', volume: 'day' };
const tableState = {
  ind: { rows: [], key: 'inflow_yi', dir: -1, limit: 15 },
  'industry-etf': { rows: [], key: 'score', dir: -1, limit: 7 },
  'broad-etf': { rows: [], key: 'score', dir: -1, limit: 7 }
};

function sortedRows(state) {
  const { key, dir } = state;
  return state.rows.slice().sort((a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === 'string' || typeof bv === 'string') {
      return String(av ?? '').localeCompare(String(bv ?? ''), 'zh-Hans-CN') * dir;
    }
    const an = Number(av), bn = Number(bv);
    const aa = Number.isFinite(an) ? an : (dir < 0 ? -Infinity : Infinity);
    const bb = Number.isFinite(bn) ? bn : (dir < 0 ? -Infinity : Infinity);
    return (aa === bb ? 0 : (aa < bb ? -1 : 1)) * dir;
  }).slice(0, state.limit);
}

function paintSortHeader(id) {
  const state = tableState[id];
  document.querySelectorAll(`#${id} thead th[data-key]`).forEach(th => {
    const active = th.dataset.key === state.key;
    th.classList.toggle('sortable', true);
    th.classList.toggle('sorted', active);
    th.classList.toggle('desc', active && state.dir < 0);
    th.classList.toggle('asc', active && state.dir > 0);
  });
}

function bindSortable(id, limitId, paint) {
  const state = tableState[id];
  document.querySelectorAll(`#${id} thead th[data-key]`).forEach(th => {
    th.classList.add('sortable');
    th.onclick = () => {
      if (state.key === th.dataset.key) state.dir *= -1;
      else { state.key = th.dataset.key; state.dir = th.dataset.type === 'str' ? 1 : -1; }
      paintSortHeader(id);
      paint();
    };
  });
  const sel = document.getElementById(limitId);
  sel.onchange = () => { state.limit = Number(sel.value); paint(); };
  paintSortHeader(id);
}

function setupCanvas(id) {
  const c = document.getElementById(id), d = window.devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
  if (W < 20) return null;
  c.width = W * d; c.height = H * d;
  const g = c.getContext('2d');
  g.setTransform(d, 0, 0, d, 0, 0);
  g.clearRect(0, 0, W, H);
  return { g, W, H };
}

function drawEmpty(g, W, H, text) {
  g.fillStyle = '#91a3bd';
  g.font = '12.5px -apple-system,"PingFang SC",sans-serif';
  g.textAlign = 'center';
  g.fillText(text, W / 2, H / 2);
  g.textAlign = 'left';
}

function drawGrid(g, W, H, pad, maxL, maxR) {
  g.strokeStyle = '#1f3251';
  g.fillStyle = '#8294ae';
  g.font = '10.5px -apple-system,sans-serif';
  g.lineWidth = 1;
  for (let k = 0; k <= 4; k++) {
    const y = pad.t + (H - pad.t - pad.b) * k / 4;
    g.beginPath(); g.moveTo(pad.l, y); g.lineTo(W - pad.r, y); g.stroke();
    g.textAlign = 'right';
    g.fillText(Math.round(maxL * (4 - k) / 4), pad.l - 6, y + 3.5);
    if (maxR !== undefined && maxR !== null) {
      g.textAlign = 'left';
      g.fillText((maxR * (4 - k) / 4).toFixed(maxR < 10 ? 1 : 0), W - pad.r + 6, y + 3.5);
    }
  }
  g.textAlign = 'left';
}

function drawXLabels(g, arr, xFn, H) {
  g.fillStyle = '#8294ae';
  g.font = '10.5px -apple-system,sans-serif';
  g.textAlign = 'center';
  const step = Math.max(1, Math.ceil(arr.length / 6));
  arr.forEach((v, i) => {
    if (i % step === 0 || i === arr.length - 1) {
      const label = v.length > 5 ? v.slice(5) : v;
      g.fillText(label, xFn(i), H - 6);
    }
  });
  g.textAlign = 'left';
}

function renderIndices(arr) {
  document.getElementById('idxrow').innerHTML = (arr || []).map(x =>
    `<div class="idx"><div class="nm">${esc(x.name)}</div>`
    + `<div class="px ${cls(x.pct)}">${fmt(x.price, 2)}</div>`
    + `<div class="ch ${cls(x.pct)}">${sign(x.pct)}%</div>`
    + `<div class="amt">${fmt(x.amount_wan / 10000, 0)} 亿元</div></div>`
  ).join('');
}

function drawBreadth() {
  if (!last) return;
  const ctx = setupCanvas('bchart');
  if (!ctx) return;
  const { g, W, H } = ctx, p = { l: 48, r: 16, t: 24, b: 24 };
  if (view.breadth === 'day') {
    const s = last.breadth_series || {}, n = (s.labels || []).length;
    if (!n) { drawEmpty(g, W, H, COPY.breadth.day_empty); return; }
    const max = Math.max(1, ...(s.up || [1]), ...(s.down || [1])) * 1.08;
    const x = i => p.l + (W - p.l - p.r) * (n < 2 ? 0.5 : i / (n - 1));
    const y = v => H - p.b - (H - p.t - p.b) * v / max;
    drawGrid(g, W, H, p, max);
    [[s.up, '#ff6b6b'], [s.down, '#38d39f']].forEach(([arr, col]) => {
      g.strokeStyle = col; g.lineWidth = 2; g.beginPath();
      arr.forEach((v, i) => i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v)));
      g.stroke();
      if (n === 1) { g.fillStyle = col; g.beginPath(); g.arc(x(0), y(arr[0]), 3.5, 0, Math.PI * 2); g.fill(); }
    });
    drawXLabels(g, s.labels, x, H);
    g.fillStyle = '#ff6b6b'; g.fillText('● 上涨家数', p.l, 13);
    g.fillStyle = '#38d39f'; g.fillText('● 下跌家数', p.l + 80, 13);
  } else {
    const a = last.daily_history || [], n = a.length;
    if (!n) { drawEmpty(g, W, H, COPY.breadth.hist_empty); return; }
    const max = Math.max(1, ...a.map(x => Math.max(x.up, x.down))) * 1.12;
    const base = (p.t + H - p.b) / 2, x = i => p.l + (W - p.l - p.r) * (i + 0.5) / n;
    const bw = Math.max(3, (W - p.l - p.r) / n * 0.62), scale = (H - p.t - p.b) / 2 / max;
    g.strokeStyle = '#2c4265'; g.beginPath(); g.moveTo(p.l, base); g.lineTo(W - p.r, base); g.stroke();
    a.forEach((r, i) => {
      g.fillStyle = 'rgba(255,107,107,.80)'; g.fillRect(x(i) - bw / 2, base - r.up * scale, bw, r.up * scale);
      g.fillStyle = 'rgba(56,211,159,.80)'; g.fillRect(x(i) - bw / 2, base, bw, r.down * scale);
    });
    drawXLabels(g, a.map(r => r.day), x, H);
    g.fillStyle = '#ff6b6b'; g.fillText('■ 上涨（零轴上）', p.l, 13);
    g.fillStyle = '#38d39f'; g.fillText('■ 下跌（零轴下）', p.l + 110, 13);
  }
  const bn = (last.breadth_series?.labels || []).length;
  document.getElementById('bfoot').textContent = view.breadth === 'day'
    ? (bn < 2 ? COPY.breadth.day_foot_single : COPY.breadth.day_foot)
    : COPY.breadth.hist_foot;
}

function drawVolume() {
  if (!last) return;
  const ctx = setupCanvas('vchart');
  if (!ctx) return;
  const { g, W, H } = ctx, p = { l: 54, r: 52, t: 24, b: 24 };
  if (view.volume === 'day') {
    const s = last.volume_series || {}, n = (s.labels || []).length;
    if (!n) { drawEmpty(g, W, H, COPY.volume.day_empty); return; }
    const mc = Math.max(1, ...(s.cum || [1])) * 1.06;
    const mr = Math.max(1, ...(s.rate || [1])) * 1.15;
    const x = i => p.l + (W - p.l - p.r) * (n < 2 ? 0.5 : i / (n - 1));
    const yc = v => H - p.b - (H - p.t - p.b) * v / mc;
    const yr = v => H - p.b - (H - p.t - p.b) * v / mr;
    drawGrid(g, W, H, p, mc, mr);
    const bw = Math.max(1.5, (W - p.l - p.r) / n * 0.68);
    g.fillStyle = 'rgba(255,191,60,.48)';
    s.rate.forEach((v, i) => g.fillRect(x(i) - bw / 2, yr(v), bw, H - p.b - yr(v)));
    g.strokeStyle = '#4da3ff'; g.lineWidth = 2; g.beginPath();
    s.cum.forEach((v, i) => i ? g.lineTo(x(i), yc(v)) : g.moveTo(x(i), yc(v)));
    g.stroke();
    drawXLabels(g, s.labels, x, H);
    g.fillStyle = '#4da3ff'; g.fillText('● 累计成交额（左轴，亿元）', p.l, 13);
    g.fillStyle = '#ffbf3c'; g.fillText('■ 每分钟速率（右轴，亿元/分）', p.l + 170, 13);
  } else {
    const a = last.daily_history || [], n = a.length;
    if (!n) { drawEmpty(g, W, H, COPY.volume.hist_empty); return; }
    const vals = a.map(x => x.amount_yi);
    const ma = vals.map((_, i) => {
      const q = vals.slice(Math.max(0, i - 4), i + 1);
      return q.reduce((s, y) => s + y, 0) / q.length;
    });
    const max = Math.max(...vals, ...ma) * 1.12;
    const x = i => p.l + (W - p.l - p.r) * (i + 0.5) / n;
    const y = v => H - p.b - (H - p.t - p.b) * v / max;
    const bw = Math.max(3, (W - p.l - p.r) / n * 0.65);
    drawGrid(g, W, H, p, max);
    a.forEach((r, i) => {
      g.fillStyle = 'rgba(77,163,255,.48)';
      g.fillRect(x(i) - bw / 2, y(r.amount_yi), bw, H - p.b - y(r.amount_yi));
    });
    g.strokeStyle = '#ffbf3c'; g.lineWidth = 2; g.beginPath();
    ma.forEach((v, i) => i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v)));
    g.stroke();
    drawXLabels(g, a.map(r => r.day), x, H);
    g.fillStyle = '#4da3ff'; g.fillText('■ 日成交额（亿元）', p.l, 13);
    g.fillStyle = '#ffbf3c'; g.fillText('● 5日均值', p.l + 125, 13);
  }
  document.getElementById('vfoot').textContent = view.volume === 'day' ? COPY.volume.day_foot : COPY.volume.hist_foot;
}

function paintIndustry() {
  const a = sortedRows(tableState.ind);
  document.querySelector('#ind tbody').innerHTML = a.length
    ? a.map(r =>
        `<tr><td><div class="industry-name">${esc(r.name)}</div><div class="industry-parent">${esc(r.parent || '')} · ${esc(r.code || '')}</div></td>`
        + `<td class="${cls(r.pct)}">${sign(r.pct)}%</td>`
        + `<td class="${cls(r.inflow_yi)}">${sign(r.inflow_yi, 2)}</td>`
        + `<td class="${cls(r.share)}">${sign(r.share, 2)}%</td>`
        + `<td>${fmt(r.turnover_yi, 1)}</td>`
        + `<td class="muted">${r.stock_count || '—'} 只</td></tr>`
      ).join('')
    : `<tr><td colspan="6" class="muted">${esc(COPY.industry.empty || '行业映射不可用时本区暂停显示')}</td></tr>`;
}

function renderIndustry(d) {
  tableState.ind.rows = d?.rows || [];
  const meta = d
    ? COPY.industry.meta_tpl
        .replace('{source}', d.source || '申万2021三级行业')
        .replace('{asof}', d.mapping_asof || '未知')
        .replace('{count}', String(d.industry_count ?? tableState.ind.rows.length))
        .replace('{cov}', fmt(d.coverage_pct, 1))
    : '';
  document.getElementById('ind-meta').textContent = meta;
  paintIndustry();
}

function renderAlerts(a) {
  document.getElementById('alerts').innerHTML = (a || []).length
    ? a.map(x => `<div class="alert ${x.pos ? 'pos' : 'neg'}"><span class="t">${esc(x.ts)}</span><span>${esc(x.text)}</span></div>`).join('')
    : `<div class="empty">${esc(COPY.alerts.empty)}</div>`;
}

function paintETFTable(id) {
  const shown = sortedRows(tableState[id]);
  document.querySelector(`#${id} tbody`).innerHTML = shown.length
    ? shown.map(x => {
        const sc = x.score || 0, rep = (x.representatives || [])[0] || {};
        const scoreCls = sc >= 60 ? 'hot' : (sc >= 40 ? 'warm' : '');
        const tagCls = sc >= 60 ? (x.net_flow_yi >= 0 ? 'buy' : 'sell') : (sc >= 40 ? 'watch' : 'ok');
        const confVal = x.confirmed_yi;
        const confTxt = (confVal === null || confVal === undefined)
          ? '<span class="muted">待更新</span>'
          : `<span class="${cls(confVal)}">${esc(x.confirmation || '')} ${sign(confVal, 1)}</span>`;
        return `<tr><td><div class="dir">${esc(x.direction)}</div><div class="topfund">${rep.code ? `${esc(rep.code)} ${esc(rep.name)}` : '暂无代表ETF'}</div></td>`
          + `<td><span class="anomaly ${scoreCls}" title="资金占比历史分位 ${fmt(x.history_percentile, 0)} · 涨跌历史分位 ${fmt(x.return_percentile, 0)}">${sc}</span></td>`
          + `<td class="${cls(x.weighted_pct)}">${sign(x.weighted_pct)}%</td>`
          + `<td class="${cls(x.net_flow_yi)}">${sign(x.net_flow_yi, 1)}</td>`
          + `<td>${fmt(x.avg_vratio, 2)}</td>`
          + `<td>${confTxt}</td>`
          + `<td><span class="tag ${tagCls}">${esc(x.signal || '正常')}</span></td></tr>`;
      }).join('')
    : '<tr><td colspan="7" class="muted">暂无匹配 ETF</td></tr>';
}

function renderETFTable(id, rows) {
  tableState[id].rows = (rows || []).filter(x => x.fund_count > 0);
  paintETFTable(id);
}

function renderETF(radar) {
  const r = radar || {}, ge = id => document.getElementById(id);
  ge('g_scan').textContent = (r.scanned ?? '—') + (r.scanned ? ' 只' : '');
  ge('g_top').textContent = r.top_direction ? `${r.top_direction}（${r.top_score || 0} 分）` : '—';
  ge('g_top').className = 'v ' + ((r.top_score || 0) >= 60 ? 'up' : '');
  ge('g_strong').textContent = `${r.strong_count ?? 0} / ${r.direction_count ?? 0}`;
  ge('g_strong').className = 'v ' + (r.strong_count ? 'up' : 'muted');
  ge('g_official').textContent = r.official_asof || '待更新';
  renderETFTable('industry-etf', r.industry_directions);
  renderETFTable('broad-etf', r.broad_directions);
}

function updateStatusBanner() {
  const sb = document.getElementById('status-pill');
  const banner = document.getElementById('stale-banner');
  const d = last || {};
  const label = d.market_status_label || (d.trading ? '交易中' : '已收盘');
  const asOf = d.as_of || '未知';
  const schedNote = d.session_note ? ` · ${d.session_note}` : '';
  if (pollErr) {
    sb.className = 'flow-status stale';
    sb.textContent = `数据未更新（上次成功：${d.last_success_at || asOf}）`;
    banner.className = 'stale-banner show';
    banner.textContent = COPY.poll_error_tpl.replace('{err}', pollErr).replace('{last_ok}', d.last_success_at || asOf);
    return;
  }
  if (clientStaleReason) {
    sb.className = 'flow-status stale';
    sb.textContent = `数据未更新（上次成功：${d.last_success_at || asOf}）`;
    banner.className = 'stale-banner show';
    banner.textContent = COPY.stale_banner_tpl
      .replace('{last_ok}', d.last_success_at || asOf)
      .replace('{reason}', clientStaleReason);
    return;
  }
  if (d.stale) {
    sb.className = 'flow-status stale';
    sb.textContent = `数据未更新（上次成功：${d.last_success_at || asOf}）`;
    banner.className = 'stale-banner show';
    let detailReason = d.stale_reason || '';
    if (!detailReason && d.sources) {
      const parts = [];
      for (const [k, s] of Object.entries(d.sources)) {
        if (s && (!s.ok || s.stale)) {
          parts.push(`${s.label || k}：数据未更新（上次成功：${s.last_success_at || '无'}，原因：${s.reason || '未知'}）`);
        }
      }
      detailReason = parts.join('；');
    }
    banner.textContent = COPY.stale_banner_tpl
      .replace('{last_ok}', d.last_success_at || asOf)
      .replace('{reason}', detailReason || '部分来源暂未返回');
    return;
  }
  sb.className = 'flow-status' + (d.trading ? ' live' : '');
  sb.textContent = `${label} · 数据时间：${asOf} 北京时间${schedNote}`;
  banner.className = 'stale-banner';
  banner.textContent = '';
}

function render(d) {
  if (!d) return;
  last = d;
  updateStatusBanner();
  renderIndices(d.indices);
  const b = d.breadth || {};
  ['up', 'down', 'flat'].forEach(k => document.getElementById('s_' + k).textContent = b[k] ?? '—');
  document.getElementById('s_ratio').textContent = fmt(b.ratio, 2);
  document.getElementById('v_done').textContent = fmt(d.two_market_yi, 0);
  document.getElementById('v_proj').textContent = fmt(d.projected_yi, 0);
  const prog = d.progress || {};
  document.getElementById('v_progress').textContent = (prog.pct ?? 0) + '%';
  document.getElementById('v_progress_lbl').textContent = COPY.volume.progress_label_tpl.replace('{elapsed}', String(prog.elapsed ?? 0));
  renderIndustry(d.industry);
  renderAlerts(d.alerts);
  renderETF(d.etf_radar);
  drawBreadth();
  drawVolume();
}

function onMonitorData(body) {
  pollErr = '';
  if (body && body.data) {
    window.__DALTON_LAST_PAYLOAD__ = body.data;
    render(body.data);
  }
}

function onMonitorPollError(err) {
  pollErr = err || '请求失败';
  updateStatusBanner();
}

function onMonitorStale(reason) {
  clientStaleReason = reason || '';
  updateStatusBanner();
}
window.onMonitorStale = onMonitorStale;

document.querySelectorAll('.tab[data-group]').forEach(btn => {
  btn.onclick = () => {
    const grp = btn.dataset.group;
    view[grp] = btn.dataset.tab;
    document.querySelectorAll(`.tab[data-group="${grp}"]`).forEach(x => x.classList.toggle('active', x === btn));
    grp === 'breadth' ? drawBreadth() : drawVolume();
  };
});
bindSortable('ind', 'ind-limit', paintIndustry);
bindSortable('industry-etf', 'industry-etf-limit', () => paintETFTable('industry-etf'));
bindSortable('broad-etf', 'broad-etf-limit', () => paintETFTable('broad-etf'));
if (last) render(last);
window.addEventListener('resize', () => { drawBreadth(); drawVolume(); });
""" + poller_js

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(cp["title"])}</title>'
        f'<style>{SHARED_MONITOR_CSS}{ASHARE_EXTRA_CSS}</style></head><body>'
        '<header class="top">'
        '<div class="top-head">'
        f'<div><h1>{html.escape(cp["title"])}</h1>'
        f'<p>{html.escape(cp["subtitle"])}</p></div>'
        '<div class="top-meta">'
        '<span class="flow-status" id="status-pill">等待数据刷新…</span>'
        '</div></div>'
        f'{legend_html}'
        '<div class="stale-banner" id="stale-banner"></div>'
        '</header>'
        '<main class="wrap">'
        '<div class="idxrow" id="idxrow"></div>'
        '<div class="grid g2" style="margin-bottom:16px">'
        '  <section class="panel">'
        f'    <div class="panel-head"><h3>{html.escape(cp["breadth"]["title"])}</h3>'
        '      <div class="tabs"><button class="tab active" data-group="breadth" data-tab="day">当日趋势</button>'
        '      <button class="tab" data-group="breadth" data-tab="30d">过去30日</button></div></div>'
        '    <div class="metrics">'
        '      <div class="metric-inline"><div class="v up" id="s_up">—</div><div class="k">上涨家数</div></div>'
        '      <div class="metric-inline"><div class="v down" id="s_down">—</div><div class="k">下跌家数</div></div>'
        '      <div class="metric-inline"><div class="v" id="s_flat">—</div><div class="k">平盘</div></div>'
        '      <div class="metric-inline"><div class="v" id="s_ratio">—</div><div class="k">涨跌比</div></div>'
        '    </div>'
        '    <canvas class="chart" id="bchart"></canvas>'
        f'    <div class="note" id="bfoot">{html.escape(cp["breadth"]["day_foot"])}</div>'
        '  </section>'
        '  <section class="panel">'
        f'    <div class="panel-head"><h3>{html.escape(cp["volume"]["title"])}</h3>'
        '      <div class="tabs"><button class="tab active" data-group="volume" data-tab="day">当日分时</button>'
        '      <button class="tab" data-group="volume" data-tab="30d">过去30日</button></div></div>'
        '    <div class="metrics">'
        f'      <div class="metric-inline"><div class="v" id="v_done">—</div><div class="k">{html.escape(cp["volume"]["done_label"])}</div></div>'
        f'      <div class="metric-inline"><div class="v" id="v_proj">—</div><div class="k">{html.escape(cp["volume"]["proj_label"])}</div></div>'
        '      <div class="metric-inline"><div class="v" id="v_progress">—</div><div class="k" id="v_progress_lbl">交易时段进度</div></div>'
        '    </div>'
        '    <canvas class="chart" id="vchart"></canvas>'
        f'    <div class="note" id="vfoot">{html.escape(cp["volume"]["day_foot"])}</div>'
        '  </section>'
        '</div>'
        '<section class="panel" style="margin-bottom:16px">'
        f'  <div class="panel-head"><div><h3>{html.escape(cp["industry"]["title"])}</h3>'
        f'    <div class="panel-sub">{html.escape(cp["industry"]["intro"])}</div></div>'
        '    <div class="table-controls"><span id="ind-meta">加载中…</span><label>显示 '
        '      <select id="ind-limit"><option value="15">15条</option><option value="30">30条</option>'
        '      <option value="50">50条</option><option value="9999">全部</option></select></label></div></div>'
        f'  <div class="table-wrap"><table id="ind"><thead><tr>{ind_headers}</tr></thead><tbody></tbody></table></div>'
        '</section>'
        '<div class="monitor-row">'
        '  <section class="panel alerts-panel">'
        f'    <div class="panel-head"><div><h3>{html.escape(cp["alerts"]["title"])}</h3>'
        f'      <div class="panel-sub">{html.escape(cp["alerts"]["intro"])}</div></div></div>'
        '    <div id="alerts"></div>'
        '  </section>'
        '  <section class="panel">'
        f'    <div class="panel-head"><div><h3>{html.escape(cp["etf"]["title"])}</h3>'
        f'      <div class="panel-sub">{html.escape(cp["etf"]["intro"])}</div></div></div>'
        '    <div class="etf-summary">'
        f'      <div class="sumcard"><div class="k">{html.escape(cp["etf"]["card_scan"])}</div><div class="v" id="g_scan">—</div></div>'
        f'      <div class="sumcard"><div class="k">{html.escape(cp["etf"]["card_top"])}</div><div class="v" id="g_top">—</div></div>'
        f'      <div class="sumcard"><div class="k">{html.escape(cp["etf"]["card_strong"])}</div><div class="v" id="g_strong">—</div></div>'
        f'      <div class="sumcard"><div class="k">{html.escape(cp["etf"]["card_official"])}</div><div class="v" id="g_official">—</div></div>'
        '    </div>'
        '    <div class="etf-split">'
        '      <div class="etf-block">'
        f'        <div class="etf-block-head"><h4>{html.escape(cp["etf"]["ind_title"])} <span class="hint">{html.escape(cp["etf"]["ind_hint"])}</span></h4>'
        '          <div class="table-controls"><select id="industry-etf-limit"><option value="7">7条</option><option value="9999">全部</option></select></div></div>'
        f'        <div class="table-wrap"><table id="industry-etf"><thead><tr>{etf_headers}</tr></thead><tbody></tbody></table></div>'
        '      </div>'
        '      <div class="etf-block">'
        f'        <div class="etf-block-head"><h4>{html.escape(cp["etf"]["broad_title"])} <span class="hint">{html.escape(cp["etf"]["broad_hint"])}</span></h4>'
        '          <div class="table-controls"><select id="broad-etf-limit"><option value="7">7条</option><option value="9999">全部</option></select></div></div>'
        f'        <div class="table-wrap"><table id="broad-etf"><thead><tr>{etf_headers}</tr></thead><tbody></tbody></table></div>'
        '      </div>'
        '    </div>'
        f'    <div class="note">{html.escape(cp["etf"]["note"])}</div>'
        '  </section>'
        '</div>'
        '</main>'
        f'<footer>{html.escape(cp["footer"])}</footer>'
        f'<script id="ashare-seed" type="application/json">{seed_json}</script>'
        f'<script id="ashare-copy" type="application/json">{copy_json}</script>'
        f'<script>{page_js}</script>'
        '</body></html>'
    )


def write_ashare_panel(
    *,
    output_dir: str | Path,
    snapshot: dict[str, Any] | None = None,
) -> Path:
    out_path = Path(output_dir) / "index.html"
    atomic_write_text(out_path, render_ashare_html(snapshot))
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render static A-share monitor HTML.")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    out = write_ashare_panel(output_dir=args.output_dir)
    print(json.dumps({"ok": True, "output": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
