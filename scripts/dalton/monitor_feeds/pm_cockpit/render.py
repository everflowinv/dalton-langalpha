#!/usr/bin/env python3
"""Render PM L1/L2 static monitor HTML panels (pm-market-state & cn-sector-rotation)."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from scripts.dalton.monitor_feeds.common import (
    SHARED_MONITOR_CSS,
    atomic_write_text,
    format_source_statuses,
    load_json_safe,
    render_color_legend,
    resolve_state_dir,
)
from scripts.dalton.monitor_feeds.pm_cockpit.ui_copy import UI_COPY

PM_EXTRA_CSS = r"""
.l1-summary{display:flex;align-items:center;gap:24px;margin-bottom:14px;flex-wrap:wrap}
.l1-big{font-size:36px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.l1-regime{font-size:22px;font-weight:750;line-height:1.1}
.l1-k{font-size:11px;color:var(--muted);margin-top:4px}
.bar{position:relative;height:14px;background:#1a2942;border-radius:4px;overflow:hidden;min-width:92px}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:4px}
.bar span{position:absolute;right:5px;top:-1px;font-size:10.5px;color:#edf4ff;font-weight:600}
.q-lead{color:#ff8a8a;border-color:#7a2e36;background:rgba(255,107,107,.14)}
.q-impr{color:#7fc0ff;border-color:#2b5d92;background:rgba(77,163,255,.14)}
.q-weak{color:#ffc266;border-color:#7d551b;background:rgba(255,157,60,.14)}
.q-lag{color:#6ce0b7;border-color:#20664d;background:rgba(56,211,159,.14)}
.vscore{display:inline-block;min-width:22px;text-align:center;padding:1px 6px;border-radius:5px;font-weight:700;font-size:11.5px}
.vscore.v5{background:rgba(77,163,255,.30);color:#9ccbff}
.vscore.v4{background:rgba(77,163,255,.16);color:#7fc0ff}
.vscore.v3{background:rgba(174,189,210,.14);color:#c5d2e5}
.vscore.v2{background:rgba(255,157,60,.18);color:#ffc266}
.vscore.v1{background:rgba(255,157,60,.32);color:#ffad5c}
.vscore.v0{background:none;color:var(--muted)}
.val-low{color:var(--temp-cold);font-weight:650}
.val-high{color:var(--temp-hot);font-weight:650}
.rrg-wrap svg{width:100%;height:auto;display:block}
"""

_SHARED_JS = r"""
const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h !== undefined) e.innerHTML = h; return e; };
const nz = v => v === null || v === undefined || Number.isNaN(v);
const fx = (v, d = 2, s = false) => nz(v) ? '—' : (s && v > 0 ? '+' : '') + Number(v).toFixed(d);
const cls = v => nz(v) ? 'muted' : (v > 0 ? 'up' : v < 0 ? 'down' : 'muted');
const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
const Q_STYLE = { '领先': ['q-lead', '#ff6b6b'], '改善': ['q-impr', '#4da3ff'], '转弱': ['q-weak', '#ff9d3c'], '落后': ['q-lag', '#38d39f'] };
const ALL_TAB = '全部';
let RRG_STATE = {};

function barHtml(pct, color, label) {
  const p = Math.max(0, Math.min(100, pct || 0));
  return `<div class="bar"><i style="width:${p}%;background:${color}"></i><span>${esc(label ?? fx(pct, 0))}</span></div>`;
}

function heatBg(v, max) {
  if (nz(v) || v === 0) return '';
  const a = Math.min(1, Math.abs(v) / (max || 10));
  return `background:${v > 0 ? `rgba(255,107,107,${(a * .28).toFixed(2)})` : `rgba(56,211,159,${(a * .28).toFixed(2)})`}`;
}

function renderRRGCard(rows, groups, title, sub, opts) {
  opts = opts || {};
  const key = opts.key || 'rrg';
  const copy = DATA.copy_common;
  const c = el('div', 'panel rrg-wrap');
  c.innerHTML = `<div class="panel-head"><div><h2>${esc(title)}</h2><div class="panel-sub">${esc(sub || '')}</div></div></div>`;
  if (!rows || !rows.length) {
    c.appendChild(el('div', 'empty', '暂无可用板块轮动数据'));
    return c;
  }
  const cur = (RRG_STATE[key] && groups.includes(RRG_STATE[key])) ? RRG_STATE[key] : groups[0];
  if (groups.length > 1) {
    const tabs = el('div', 'tabs');
    tabs.style.marginBottom = '10px';
    groups.forEach(g => {
      const t = el('button', 'tab' + (g === cur ? ' active' : ''), esc(g));
      t.onclick = () => { RRG_STATE[key] = g; renderAll(); };
      tabs.appendChild(t);
    });
    c.appendChild(tabs);
  }
  let R = cur === ALL_TAB ? rows.slice() : rows.filter(r => r.group === cur);
  let trimmed = 0;
  if (opts.max && R.length > opts.max) {
    const dist = r => Math.hypot(r.rsr - 100, (r.rsm - 100) * 1.4);
    trimmed = R.length - opts.max;
    R = R.slice().sort((a, b) => dist(b) - dist(a)).slice(0, opts.max);
  }

  const W = 720, H = 430, P = 46;
  const xs = R.map(r => r.rsr), ys = R.map(r => r.rsm);
  const pad = v => { const mn = Math.min(...v, 99.2), mx = Math.max(...v, 100.8); const m = (mx - mn) * .18 || .5; return [mn - m, mx + m]; };
  const [x0, x1] = pad(xs), [y0, y1] = pad(ys);
  const sx = v => P + (v - x0) / (x1 - x0) * (W - P * 2);
  const sy = v => H - P - (v - y0) / (y1 - y0) * (H - P * 2);
  const cx = sx(100), cy = sy(100);
  let svg = `<svg viewBox="0 0 ${W} ${H}">`;
  svg += `<rect x="${cx}" y="${P}" width="${W - P - cx}" height="${cy - P}" fill="rgba(255,107,107,.07)"/>`;
  svg += `<rect x="${cx}" y="${cy}" width="${W - P - cx}" height="${H - P - cy}" fill="rgba(255,157,60,.07)"/>`;
  svg += `<rect x="${P}" y="${cy}" width="${cx - P}" height="${H - P - cy}" fill="rgba(56,211,159,.07)"/>`;
  svg += `<rect x="${P}" y="${P}" width="${cx - P}" height="${cy - P}" fill="rgba(77,163,255,.07)"/>`;
  svg += `<line x1="${cx}" y1="${P}" x2="${cx}" y2="${H - P}" stroke="#395276"/>`;
  svg += `<line x1="${P}" y1="${cy}" x2="${W - P}" y2="${cy}" stroke="#395276"/>`;
  svg += `<text x="${W - P - 8}" y="${P + 16}" fill="#ff8a8a" font-size="11.5" text-anchor="end" font-weight="600">领先</text>`;
  svg += `<text x="${W - P - 8}" y="${H - P - 8}" fill="#ffc266" font-size="11.5" text-anchor="end" font-weight="600">转弱</text>`;
  svg += `<text x="${P + 8}" y="${H - P - 8}" fill="#6ce0b7" font-size="11.5" font-weight="600">落后</text>`;
  svg += `<text x="${P + 8}" y="${P + 16}" fill="#7fc0ff" font-size="11.5" font-weight="600">改善</text>`;
  svg += `<text x="${W / 2}" y="${H - 10}" fill="#91a3bd" font-size="10.5" text-anchor="middle">${esc(copy.rrg_axis_x)}</text>`;
  svg += `<text x="14" y="${H / 2}" fill="#91a3bd" font-size="10.5" text-anchor="middle" transform="rotate(-90,14,${H / 2})">${esc(copy.rrg_axis_y)}</text>`;

  R.forEach(r => {
    const col = (Q_STYLE[r.quadrant] || ['q-lead', '#aebdd2'])[1];
    if (r.tail && r.tail.length > 1) {
      const d = r.tail.map((p, i) => (i ? 'L' : 'M') + sx(p[0]).toFixed(1) + ' ' + sy(p[1]).toFixed(1)).join(' ');
      svg += `<path d="${d}" fill="none" stroke="${col}" stroke-width="1.3" opacity=".45"/>`;
    }
    svg += `<circle cx="${sx(r.rsr).toFixed(1)}" cy="${sy(r.rsm).toFixed(1)}" r="5" fill="${col}" opacity=".95">`
      + `<title>${esc(r.name)} (${esc(r.ticker)})\n象限：${esc(r.quadrant)}\n相对强弱比 ${r.rsr} / 变化速度 ${r.rsm}\n近1月超额 ${fx(r.exc['1M'], 2, true)}%</title></circle>`;
    svg += `<text x="${(sx(r.rsr) + 8).toFixed(1)}" y="${(sy(r.rsm) + 3.5).toFixed(1)}" fill="#dbe6f6" font-size="10.5">${esc(r.name)}</text>`;
  });
  svg += '</svg>';
  c.appendChild(el('div', null, svg));
  let noteHtml = '';
  if (trimmed) {
    noteHtml += `<span style="color:var(--temp-hot)">${esc(copy.rrg_trimmed_tpl.replace('{shown}', String(R.length)).replace('{trimmed}', String(trimmed)))}</span><br>`;
  }
  noteHtml += esc(copy.rrg_note);
  c.appendChild(el('div', 'note', noteHtml));
  return c;
}

function renderMigrationCard(mig, names, title, sub) {
  const copy = DATA.copy_common;
  const c = el('div', 'panel');
  c.innerHTML = `<div class="panel-head"><div><h2>${esc(title)}</h2><div class="panel-sub">${esc(sub)}</div></div></div>`;
  const dates = Object.keys(mig || {});
  if (!dates.length) {
    c.appendChild(el('div', 'empty', '暂无近 8 周排名迁移数据'));
    return c;
  }
  const last = mig[dates[dates.length - 1]];
  const order = Object.keys(last).sort((a, b) => last[a] - last[b]).slice(0, 16);
  const wrap = el('div', 'table-wrap');
  const t = el('table');
  t.innerHTML = `<thead><tr><th>板块名称</th>${dates.map(d => `<th>${esc(d.slice(5))}</th>`).join('')}<th>近8周排名变化</th></tr></thead>`;
  const tb = el('tbody');
  order.forEach(tk => {
    const seq = dates.map(d => mig[d][tk]);
    const d8 = (seq[0] !== undefined && seq[seq.length - 1] !== undefined) ? seq[0] - seq[seq.length - 1] : null;
    const tr = el('tr');
    tr.innerHTML = `<td>${esc(names[tk] || tk)}</td>` + seq.map(v => {
      if (v === undefined) return '<td class="muted">—</td>';
      const a = Math.max(0, 1 - (v - 1) / 16);
      return `<td style="background:rgba(77,163,255,${(a * .34).toFixed(2)})">${v}</td>`;
    }).join('') + `<td class="${cls(d8)}">${nz(d8) ? '—' : (d8 > 0 ? '↑' + d8 : d8 < 0 ? '↓' + (-d8) : '持平')}</td>`;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  c.appendChild(wrap);
  c.appendChild(el('div', 'note', esc(copy.migration_note)));
  return c;
}
"""


def _render_us_html(panels: dict[str, Any]) -> str:
    us_copy = UI_COPY["us"]
    common_copy = UI_COPY["common"]
    st = panels.get("us_status") or {}
    stale_text = ""
    if st.get("stale"):
        detail = format_source_statuses(panels.get("sources"), only=("us",)) or st.get("reason") or "数据源未完整返回"
        stale_text = common_copy["stale_banner_tpl"].format(
            scope="美股",
            last_ok=st.get("last_success_at") or "未知",
            reason=detail,
        )
    payload = {
        "l1": panels["l1"],
        "l2_us": panels["l2_us"],
        "copy_us": us_copy,
        "copy_common": common_copy,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    legend_html = render_color_legend(
        us_copy["legend_dir"],
        us_copy["legend_sec"],
        us_copy["legend_note"],
    )
    as_of = panels.get("us_data_as_of") or "未知"
    gen_at = panels.get("generated_at_us") or "未知"

    js = _SHARED_JS + r"""
const DATA = JSON.parse(document.getElementById('pm-data').textContent);

function renderL1(l1) {
  const cp = DATA.copy_us;
  const c = el('div', 'panel');
  const col = l1.tone === 'up' ? 'var(--dir-up)' : l1.tone === 'down' ? 'var(--dir-down)' : 'var(--temp-warm)';
  c.innerHTML = `<div class="panel-head"><div><h2>${esc(cp.l1_title)}</h2><div class="panel-sub">${esc(cp.l1_sub_tpl.replace('{n_scored}', String(l1.n_scored || 0)))}</div></div></div>`;
  const sum = el('div', 'l1-summary');
  const dispCol = (l1.dispersion >= 28) ? 'var(--temp-hot)' : (l1.dispersion >= 18) ? 'var(--temp-warm)' : 'var(--temp-cold)';
  sum.innerHTML = `
    <div><div class="l1-big" style="color:${col}">${fx(l1.score, 1)}</div><div class="l1-k">${esc(cp.l1_score_label)}</div></div>
    <div><div class="l1-regime" style="color:${col}">${esc(l1.regime)}</div><div class="l1-k">${esc(cp.l1_regime_label)}</div></div>
    <div><div style="font-size:17px;font-weight:700">${esc(l1.style_tilt || '—')}</div><div class="l1-k">${esc(cp.l1_style_label)}</div></div>
    <div style="max-width:340px"><div style="font-size:13.5px;font-weight:650;color:${dispCol}">${esc(l1.conflict || '')}</div><div class="l1-k">${esc(cp.l1_conflict_label)}（分歧度 ${fx(l1.dispersion, 1)}）</div></div>
  `;
  c.appendChild(sum);
  const wrap = el('div', 'table-wrap');
  const t = el('table');
  t.innerHTML = `<thead><tr>${cp.l1_headers.map((h, i) => `<th${i === 0 || i === cp.l1_headers.length - 1 ? ' style="text-align:left"' : ''}>${esc(h)}</th>`).join('')}</tr></thead>`;
  const tb = el('tbody');
  (l1.components || []).forEach(x => {
    const good = x.sign === 0 ? null : (x.sign > 0 ? x.pctile : 100 - x.pctile);
    const barCol = x.sign === 0 ? 'var(--purple)' : (good >= 65 ? 'var(--dir-up)' : good <= 35 ? 'var(--dir-down)' : 'var(--temp-warm)');
    const tr = el('tr');
    tr.innerHTML = `<td style="text-align:left">${esc(x.label)}${x.sign === 0 ? ' <span class="muted">（仅方向参考）</span>' : ''}</td>`
      + `<td>${esc(x.value)}</td>`
      + `<td>${barHtml(x.pctile, barCol, fx(x.pctile, 0) + '%')}</td>`
      + `<td class="${cls(x.chg_1w_pct)}">${fx(x.chg_1w_pct, 1, true)}%</td>`
      + `<td class="${cls(x.chg_1m_pct)}">${fx(x.chg_1m_pct, 1, true)}%</td>`
      + `<td style="text-align:left" class="muted">${esc(x.note)}</td>`;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  c.appendChild(wrap);
  c.appendChild(el('div', 'note', esc(cp.l1_note)));
  return c;
}

function renderMatrix(rows, bench) {
  const cp = DATA.copy_us;
  const c = el('div', 'panel');
  c.innerHTML = `<div class="panel-head"><div><h2>${esc(cp.matrix_title)}</h2><div class="panel-sub">基准：${esc(bench)}（美元计价）</div></div></div>`;
  if (!rows || !rows.length) {
    c.appendChild(el('div', 'empty', '暂无美股板块数据'));
    return c;
  }
  const wrap = el('div', 'table-wrap');
  const t = el('table');
  t.innerHTML = `<thead><tr>${cp.matrix_headers.map((h, i) => `<th${i === 0 ? ' style="text-align:left"' : ''}>${esc(h)}</th>`).join('')}</tr></thead>`;
  const tb = el('tbody');
  rows.forEach(r => {
    const qCls = (Q_STYLE[r.quadrant] || ['q-lead'])[0];
    const tr = el('tr');
    tr.innerHTML = `<td>${esc(r.name)} <span class="muted">${esc(r.ticker)}</span></td>`
      + `<td><span class="pill ${qCls}">${esc(r.quadrant)}</span></td>`
      + `<td class="${cls(r.ret_1d)}">${fx(r.ret_1d, 2, true)}%</td>`
      + ['1W', '1M', '3M', '6M'].map(k => `<td style="${heatBg(r.exc[k], k === '1W' ? 4 : k === '1M' ? 8 : 15)}" class="${cls(r.exc[k])}">${fx(r.exc[k], 2, true)}%</td>`).join('')
      + `<td class="${cls(r.from_250d_high_pct)}">${fx(r.from_250d_high_pct, 1)}%</td>`;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  c.appendChild(wrap);
  c.appendChild(el('div', 'note', esc(cp.matrix_note)));
  return c;
}

function renderAll() {
  const app = $('#app');
  app.innerHTML = '';
  app.appendChild(renderL1(DATA.l1));
  const usRows = DATA.l2_us.rows || [];
  const groups = [ALL_TAB, ...new Set(usRows.map(r => r.group))];
  const grid = el('div', 'grid g3');
  grid.appendChild(renderRRGCard(usRows, groups, DATA.copy_us.rrg_title, `基准：${DATA.l2_us.benchmark}（含 27 个美股板块 ETF）`, { key: 'us' }));
  grid.appendChild(renderMigrationCard(DATA.l2_us.migration, DATA.l2_us.names, DATA.copy_us.migration_title, DATA.copy_us.migration_sub));
  app.appendChild(grid);
  app.appendChild(renderMatrix(usRows, DATA.l2_us.benchmark));
}

renderAll();
"""

    stale_cls = "stale-banner show" if stale_text else "stale-banner"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(us_copy["title"])}</title>'
        f'<style>{SHARED_MONITOR_CSS}{PM_EXTRA_CSS}</style></head><body>'
        '<header class="top">'
        '<div class="top-head">'
        f'<div><h1>{html.escape(us_copy["title"])}</h1>'
        f'<p>{html.escape(us_copy["subtitle"])}</p></div>'
        '<div class="top-meta">'
        f'<span>数据截至 {html.escape(as_of)}（美东时间）</span>'
        f'<span>｜生成于 {html.escape(gen_at)}</span>'
        f'<a class="top-link" href="{html.escape(us_copy["cross_link_href"])}">{html.escape(us_copy["cross_link_label"])}</a>'
        '</div></div>'
        f'{legend_html}'
        f'<div class="{stale_cls}">{html.escape(stale_text)}</div>'
        '</header>'
        '<main class="wrap"><div class="stack" id="app"></div></main>'
        f'<footer>{html.escape(common_copy["footer_note"])}</footer>'
        f'<script id="pm-data" type="application/json">{data_json}</script>'
        f'<script>{js}</script>'
        '</body></html>'
    )


def _render_cn_html(panels: dict[str, Any]) -> str:
    cn_copy = UI_COPY["cn"]
    common_copy = UI_COPY["common"]
    cn = panels.get("l2_cn") or {}
    st = panels.get("cn_status") or {}
    stale_text = ""
    if st.get("stale") or not cn.get("available"):
        detail = (
            format_source_statuses(panels.get("sources"), only=("cn", "cn_valuation"))
            or st.get("reason")
            or cn.get("reason")
            or "数据源未完整返回"
        )
        stale_text = common_copy["stale_banner_tpl"].format(
            scope="A股申万行业",
            last_ok=st.get("last_success_at") or cn.get("last_date") or "未知",
            reason=detail,
        )
    ws = ((cn.get("valuation_meta") or {}).get("window_start")) or "2021-12-31"
    cn_copy_resolved = dict(cn_copy)
    cn_copy_resolved["valuation_note"] = cn_copy["valuation_note_tpl"].format(window_start=ws)
    payload = {
        "l2_cn": cn,
        "copy_cn": cn_copy_resolved,
        "copy_common": common_copy,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    legend_html = render_color_legend(
        cn_copy["legend_dir"],
        cn_copy["legend_sec"],
        cn_copy["legend_note"],
    )
    as_of = cn.get("last_date") or "未知"
    gen_at = panels.get("generated_at_cn") or "未知"

    js = _SHARED_JS + r"""
const DATA = JSON.parse(document.getElementById('pm-data').textContent);
let CN_TBL = '强势前25';

function renderL1Table(cn) {
  const cp = DATA.copy_cn;
  const c = el('div', 'panel');
  c.innerHTML = `<div class="panel-head"><div><h2>${esc(cp.l1_table_title)}</h2><div class="panel-sub">基准：${esc(cn.benchmark)} · ${(cn.rows_l1 || []).length} 个一级行业（人民币计价）</div></div></div>`;
  const wrap = el('div', 'table-wrap');
  const t = el('table');
  t.innerHTML = `<thead><tr>${cp.l1_headers.map((h, i) => `<th${i === 0 ? ' style="text-align:left"' : ''}>${esc(h)}</th>`).join('')}</tr></thead>`;
  const tb = el('tbody');
  (cn.rows_l1 || []).forEach(r => {
    const qCls = (Q_STYLE[r.quadrant] || ['q-lead'])[0];
    const vCls = nz(r.val_pctile) ? '' : r.val_pctile <= 25 ? 'val-low' : r.val_pctile >= 75 ? 'val-high' : '';
    const tr = el('tr');
    tr.innerHTML = `<td>${esc(r.name)} <span class="muted">${esc(r.ticker)}</span></td>`
      + `<td><span class="pill ${qCls}">${esc(r.quadrant)}</span></td>`
      + `<td class="${cls(r.ret_1d)}">${fx(r.ret_1d, 2, true)}%</td>`
      + ['1W', '1M', '3M', '6M'].map(k => `<td style="${heatBg(r.exc[k], k === '1W' ? 4 : k === '1M' ? 8 : 15)}" class="${cls(r.exc[k])}">${fx(r.exc[k], 2, true)}%</td>`).join('')
      + `<td class="${cls(r.from_250d_high_pct)}">${fx(r.from_250d_high_pct, 1)}%</td>`
      + `<td title="${esc(r.pe_excluded || '')}">${r.pe_excluded ? '<span class="muted">盈利失真(剔除)</span>' : fx(r.pe_ttm, 1)}</td>`
      + `<td>${fx(r.pb, 2)}</td><td>${fx(r.div_yield, 2)}%</td>`
      + `<td class="${vCls}" title="市盈率分位 ${fx(r.pe_hist_pctile, 0)}% / 市净率分位 ${fx(r.pb_hist_pctile, 0)}%">${nz(r.val_pctile) ? '—' : Math.round(r.val_pctile) + '%'}</td>`
      + `<td><span class="vscore v${r.val_score || 0}">${nz(r.val_score) ? '—' : r.val_score}</span></td>`;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  c.appendChild(wrap);
  const ws = (cn.valuation_meta && cn.valuation_meta.window_start) || '2021-12-31';
  c.appendChild(el('div', 'note', esc(cp.valuation_note || cp.valuation_note_tpl.replace('{window_start}', ws))));
  return c;
}

function renderL2Matrix(cn) {
  const cp = DATA.copy_cn;
  const c = el('div', 'panel');
  const allRows = cn.rows_l2 || [];
  c.innerHTML = `<div class="panel-head"><div><h2>${esc(cp.l2_table_title)}</h2><div class="panel-sub">相对 ${esc(cn.benchmark)} · ${allRows.length} 个二级行业</div></div></div>`;
  const views = ['强势前25', '弱势后25', '全部'];
  const tabs = el('div', 'tabs');
  tabs.style.marginBottom = '10px';
  views.forEach(v => {
    const b = el('button', 'tab' + (v === CN_TBL ? ' active' : ''), esc(v));
    b.onclick = () => { CN_TBL = v; renderAll(); };
    tabs.appendChild(b);
  });
  c.appendChild(tabs);

  let R = allRows.slice();
  if (CN_TBL === '强势前25') R = R.slice(0, 25);
  else if (CN_TBL === '弱势后25') R = R.slice(-25).reverse();

  const wrap = el('div', 'table-wrap');
  const t = el('table');
  t.innerHTML = `<thead><tr>${cp.l2_headers.map((h, i) => `<th${i <= 1 ? ' style="text-align:left"' : ''}>${esc(h)}</th>`).join('')}</tr></thead>`;
  const tb = el('tbody');
  R.forEach(r => {
    const qCls = (Q_STYLE[r.quadrant] || ['q-lead'])[0];
    const vCls = nz(r.val_pctile) ? '' : r.val_pctile <= 25 ? 'val-low' : r.val_pctile >= 75 ? 'val-high' : '';
    const tr = el('tr');
    tr.innerHTML = `<td>${esc(r.name)} <span class="muted">${esc(r.ticker)}</span></td>`
      + `<td class="muted" style="text-align:left">${esc(r.parent || '')}</td>`
      + `<td><span class="pill ${qCls}">${esc(r.quadrant)}</span></td>`
      + `<td class="${cls(r.ret_1d)}">${fx(r.ret_1d, 2, true)}%</td>`
      + ['1W', '1M', '3M', '6M'].map(k => `<td style="${heatBg(r.exc[k], k === '1W' ? 4 : k === '1M' ? 8 : 15)}" class="${cls(r.exc[k])}">${fx(r.exc[k], 2, true)}%</td>`).join('')
      + `<td class="muted">${fx(r.amt_yi, 1)}</td>`
      + `<td class="${r.amt_vs_20d >= 1.5 ? 'up' : ''}">${fx(r.amt_vs_20d, 2)}</td>`
      + `<td title="${esc(r.pe_excluded || '')}">${r.pe_excluded ? '<span class="muted">盈利失真(剔除)</span>' : fx(r.pe_ttm, 1)}</td>`
      + `<td>${fx(r.pb, 2)}</td><td>${fx(r.div_yield, 2)}%</td>`
      + `<td class="${vCls}" title="市盈率分位 ${fx(r.pe_hist_pctile, 0)}% / 市净率分位 ${fx(r.pb_hist_pctile, 0)}%">${nz(r.val_pctile) ? '—' : Math.round(r.val_pctile) + '%'}</td>`
      + `<td><span class="vscore v${r.val_score || 0}">${nz(r.val_score) ? '—' : r.val_score}</span></td>`
      + `<td class="${cls(r.from_250d_high_pct)}">${fx(r.from_250d_high_pct, 1)}%</td>`;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  c.appendChild(wrap);
  const ws = (cn.valuation_meta && cn.valuation_meta.window_start) || '2021-12-31';
  c.appendChild(el('div', 'note', esc(cp.valuation_note || cp.valuation_note_tpl.replace('{window_start}', ws))));
  return c;
}

function renderAll() {
  const app = $('#app');
  app.innerHTML = '';
  const cn = DATA.l2_cn || {};
  if (!cn.available) {
    const emptyCard = el('div', 'panel');
    emptyCard.innerHTML = `<div class="empty">${esc(cn.reason || 'A股申万行业数据暂未生成，请联系系统维护人检查数据采集任务')}</div>`;
    app.appendChild(emptyCard);
    return;
  }
  const g1 = el('div', 'grid g3');
  g1.appendChild(renderRRGCard(cn.rows_l1 || [], ['申万一级'], DATA.copy_cn.l1_rrg_title, `基准：${cn.benchmark} · ${cn.last_date} · ${(cn.rows_l1 || []).length} 个一级行业`, { key: 'cn1' }));
  g1.appendChild(renderMigrationCard(cn.migration_l1, cn.names_l1, DATA.copy_cn.l1_migration_title, '近 8 周 · 按近 1 个月超额收益排名'));
  app.appendChild(g1);
  app.appendChild(renderL1Table(cn));
  app.appendChild(renderRRGCard(cn.rows_l2 || [], [ALL_TAB, ...(cn.parents || [])], DATA.copy_cn.l2_rrg_title, `基准：${cn.benchmark} · ${cn.last_date}`, { key: 'cn2', max: cn.rrg_max_points || 42 }));
  app.appendChild(renderL2Matrix(cn));
}

renderAll();
"""

    stale_cls = "stale-banner show" if stale_text else "stale-banner"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(cn_copy["title"])}</title>'
        f'<style>{SHARED_MONITOR_CSS}{PM_EXTRA_CSS}</style></head><body>'
        '<header class="top">'
        '<div class="top-head">'
        f'<div><h1>{html.escape(cn_copy["title"])}</h1>'
        f'<p>{html.escape(cn_copy["subtitle"])}</p></div>'
        '<div class="top-meta">'
        f'<span>数据截至 {html.escape(as_of)}（北京时间）</span>'
        f'<span>｜生成于 {html.escape(gen_at)}</span>'
        f'<a class="top-link" href="{html.escape(cn_copy["cross_link_href"])}">{html.escape(cn_copy["cross_link_label"])}</a>'
        '</div></div>'
        f'{legend_html}'
        f'<div class="{stale_cls}">{html.escape(stale_text)}</div>'
        '</header>'
        '<main class="wrap"><div class="stack" id="app"></div></main>'
        f'<footer>{html.escape(common_copy["footer_note"])}</footer>'
        f'<script id="pm-data" type="application/json">{data_json}</script>'
        f'<script>{js}</script>'
        '</body></html>'
    )


def render_pm_panels(
    *,
    state_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    panels: dict[str, Any] | None = None,
) -> dict[str, Path]:
    st_dir = resolve_state_dir("pm_cockpit", state_dir)
    if panels is None:
        panels = load_json_safe(st_dir / "panels.json")
        if not panels:
            raise FileNotFoundError(f"panels.json not found in {st_dir}")
    out_root = Path(output_dir) if output_dir is not None else st_dir / "output"
    us_path = out_root / "pm-market-state" / "index.html"
    cn_path = out_root / "cn-sector-rotation" / "index.html"
    atomic_write_text(us_path, _render_us_html(panels))
    atomic_write_text(cn_path, _render_cn_html(panels))
    return {"pm-market-state": us_path, "cn-sector-rotation": cn_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="Render PM L1/L2 monitor HTML pages.")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    out = render_pm_panels(state_dir=args.state_dir, output_dir=args.output_dir)
    print(json.dumps({k: str(v) for k, v in out.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
