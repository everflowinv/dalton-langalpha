"""四表排行榜可视化：把表一 / 表二 / 表三 / 表四 渲染成一张自包含 HTML。

不联网、不引 CDN、不新增取数。数据全部来自 rank() / screen_embedded() / usage_board()
的返回值与 state/ai_app_db/coverage.json，页面打开即用，可离线归档。

设计约束跟 SKILL.md 一致：
- 表一、表三排名次，表四只分组不排名，表二按占比水平排。
- 每条都带口径、证据等级、来源链接，点开能回指一手件。
- 数据完整度单独一屏：算不出增速的原因、只差一条归因的公司、台账热力图，
  让"哪里还缺数据"跟榜单本身一样显眼。
"""

import json
import os
from collections import Counter, defaultdict

# ------------------------------------------------------------------ 数据健康

TRACK_LABEL = {
    "toc_internet": "ToC 互联网",
    "tob_software": "ToB 软件",
    "ai_for_science": "AI for Science",
}


def _iter_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def currency_health(rank_result, emb_result):
    """币种口径的覆盖情况。榜上排名只认美元列，所以「哪些行折不出美元」
    和「哪些行原币没记」必须跟榜单一样摆在明面上。"""
    basis = Counter()
    unconvertible = []
    for name, rows in (rank_result.get("boards") or {}).items():
        for r in rows:
            basis[r.get("usd_basis") or "unlabeled"] += 1
            if r.get("usd_basis") == "unconvertible":
                unconvertible.append({"company": r.get("company"), "ticker": r.get("ticker"),
                                      "board": name, "unit": r.get("unit")})
    rev = Counter()
    for r in emb_result.get("results", []):
        rev[r.get("revenue_orig_basis") or "unknown"] += 1
    return {
        "metric_usd_basis": dict(basis),
        "unconvertible": unconvertible,
        "revenue_orig_basis": dict(rev),
    }


def health_stats(db_root, rank_result, emb_result, usage_result):
    """算出"离一张完整的榜还差什么"，全部本地统计，不猜。"""
    companies_dir = os.path.join(db_root, "companies")

    # 指标序列深度：同一家同一指标同一 series_id 有几个期
    series_periods = defaultdict(set)
    metric_rows = 0
    if os.path.isdir(companies_dir):
        for ticker in os.listdir(companies_dir):
            path = os.path.join(companies_dir, ticker, "metrics.jsonl")
            for rec in _iter_jsonl(path):
                metric_rows += 1
                key = (rec.get("ticker") or ticker, rec.get("metric"),
                       rec.get("series_id") or "default")
                if rec.get("period"):
                    series_periods[key].add(rec["period"])
    depth = Counter(len(v) for v in series_periods.values())
    single_series = sorted(
        (k for k, v in series_periods.items() if len(v) == 1),
        key=lambda k: (k[0], k[1] or ""),
    )

    # 原话与专家层
    quote_rows = expert_rows = 0
    quote_companies = set()
    if os.path.isdir(companies_dir):
        for ticker in os.listdir(companies_dir):
            n = sum(1 for _ in _iter_jsonl(os.path.join(companies_dir, ticker, "quotes.jsonl")))
            quote_rows += n
            if n:
                quote_companies.add(ticker)
            expert_rows += sum(1 for _ in _iter_jsonl(
                os.path.join(companies_dir, ticker, "experts.jsonl")))

    # 表一 算不出增速的原因
    t1_no_growth = rank_result.get("boards", {}).get("no_growth", [])
    t1_blocked = Counter(r.get("reason") or "未标注原因" for r in t1_no_growth)
    # 「算不出」里真正还欠活的只有 unchecked 那部分：first_disclosure 是查过的终态，
    # 公司历史上就没披露过同口径，再补也补不出来。两者混在一起会高估剩余工作量。
    t1_no_growth_kind = Counter(r.get("no_growth_kind") or "unresolved"
                                for r in t1_no_growth)

    # 表二 只差一条归因原话的公司：规模、增速都过闸门，缺的就是举证
    near_miss = []
    for r in emb_result.get("results", []):
        if r.get("tier") != "watch":
            continue
        reasons = " ".join(r.get("reasons") or [])
        if "归因" not in reasons:
            continue
        if not r.get("scale_ok"):
            continue
        yoy = r.get("revenue_yoy_pct")
        if yoy is None or yoy < 20:
            continue
        near_miss.append({
            "ticker": r.get("ticker"), "company": r.get("company"),
            "track": r.get("track"), "market": r.get("market"),
            "period": r.get("period"), "revenue_yoy_pct": yoy,
            "yoy_chg_vs_year_ago_pts": r.get("yoy_chg_vs_year_ago_pts"),
            "ai_purity": r.get("ai_purity"),
            # 非收入归因的 AI 点评：公司对 AI 有实际表态/使用，但未把增长归到 AI
            "ai_commentary": r.get("ai_commentary") or [],
            "ready": (r.get("yoy_chg_vs_year_ago_pts") or -99) >= 3,
        })
    near_miss.sort(key=lambda x: -(x["yoy_chg_vs_year_ago_pts"] or -999))

    # 表二 期间新鲜度
    period_freshness = Counter(r.get("period") for r in emb_result.get("results", [])
                               if r.get("period"))

    # 表三 单点序列（占"无法判断趋势"的大头）
    usage_unknown = usage_result.get("groups", {}).get("unknown", [])
    usage_no_growth = [r for r in usage_unknown
                       if r.get("yoy_pct") is None and r.get("qoq_pct") is None]

    coverage = {}
    cov_path = os.path.join(db_root, "coverage.json")
    if os.path.exists(cov_path):
        with open(cov_path, encoding="utf-8") as fh:
            coverage = json.load(fh)

    return {
        "metric_rows": metric_rows,
        "series_total": len(series_periods),
        "series_depth": {str(k): v for k, v in sorted(depth.items())},
        "single_point_series": len(single_series),
        "single_point_examples": [
            {"ticker": t, "metric": m, "series_id": s} for t, m, s in single_series
        ],
        "quote_rows": quote_rows,
        "quote_companies": len(quote_companies),
        "expert_rows": expert_rows,
        "t1_growth_blocked": dict(t1_blocked),
        "t1_no_growth_kind": dict(t1_no_growth_kind),
        "t2_near_miss": near_miss,
        "t2_period_freshness": dict(sorted(period_freshness.items(), reverse=True)),
        "currency": currency_health(rank_result, emb_result),
        "t3_unknown": len(usage_unknown),
        "t3_single_point": len(usage_no_growth),
        "coverage": {
            "built_at": coverage.get("built_at"),
            "since": coverage.get("since"),
            "until": coverage.get("until"),
            "periods": coverage.get("periods"),
            "companies_in_scope": coverage.get("companies_in_scope"),
            "cells_total": coverage.get("cells_total"),
            "core_cells_total": coverage.get("core_cells_total"),
            "summary": coverage.get("summary"),
            "summary_by_type": coverage.get("summary_by_type"),
            "grid": coverage.get("grid"),
        } if coverage else None,
    }


# ------------------------------------------------------------------ 渲染

CSS = """
:root{
  --bg:#f5f6f8; --card:#fff; --ink:#14161a; --muted:#6b7280; --line:#e3e6ea;
  --accent:#1f6feb; --hot:#d1242f; --good:#1a7f37; --warn:#9a6700; --cool:#6639ba;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",Segoe UI,sans-serif}
.num{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
header{background:#0f1419;color:#fff;padding:18px 22px 0}
header h1{margin:0;font-size:19px;font-weight:650;letter-spacing:.3px}
header .sub{color:#9aa4b2;font-size:12.5px;margin-top:5px}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 0}
.kpi{background:#1b2129;border:1px solid #272e38;border-radius:8px;padding:8px 12px;min-width:104px}
.kpi .v{font-size:19px;font-weight:650;color:#fff}
.kpi .l{font-size:11px;color:#8b95a3;margin-top:2px}
nav{display:flex;gap:2px;margin-top:16px;overflow-x:auto}
nav button{background:transparent;border:0;border-bottom:2px solid transparent;color:#9aa4b2;
  padding:9px 14px;font-size:13.5px;cursor:pointer;white-space:nowrap;font-family:inherit}
nav button.on{color:#fff;border-bottom-color:var(--accent)}
nav button:hover{color:#fff}
.bar{position:sticky;top:0;z-index:20;background:rgba(245,246,248,.96);backdrop-filter:blur(6px);
  border-bottom:1px solid var(--line);padding:9px 22px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bar input[type=search]{width:230px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;
  font-family:inherit;font-size:13px;background:#fff}
.bar select{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:#fff;
  font-family:inherit;font-size:13px}
.bar .hint{color:var(--muted);font-size:12px;margin-left:auto}
.bar button.mini{padding:6px 10px;border:1px solid var(--line);background:#fff;border-radius:6px;
  cursor:pointer;font-size:12.5px;font-family:inherit}
main{padding:18px 22px 60px;max-width:1500px}
.pane{display:none}
.pane.on{display:block}
.note{color:var(--muted);font-size:12.5px;margin:0 0 14px;line-height:1.7}
/* overflow:clip 而不是 hidden：hidden 会造出滚动容器，把表头的 sticky 吃掉 */
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:16px;overflow:clip}
.card>h2{margin:0;padding:12px 16px;font-size:14.5px;font-weight:650;border-bottom:1px solid var(--line);
  display:flex;align-items:center;gap:8px}
.card>h2 .cnt{color:var(--muted);font-weight:400;font-size:12.5px;margin-left:4px}
.card>h2 .rule{color:var(--muted);font-weight:400;font-size:12px;margin-left:auto;text-align:right;max-width:46%}
.card>h2 .fold{cursor:pointer;color:var(--muted);font-weight:400;font-size:12px;user-select:none}
.card>h2 .csv{border:1px solid var(--line);background:#fff;border-radius:5px;padding:2px 7px;font-size:11.5px;
  cursor:pointer;color:#4b5563;font-family:inherit}
.card.fold>.body{display:none}
table{width:100%;border-collapse:collapse;font-size:13px}
th{position:sticky;top:var(--barh,51px);background:#fafbfc;text-align:left;font-weight:600;font-size:12px;color:#4b5563;
  padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}
th.no-sort{cursor:default}
th .ar{color:#c3c9d1;margin-left:3px}
td{padding:8px 10px;border-bottom:1px solid #f0f2f4;vertical-align:top}
tr.row:hover{background:#f8fafc}
tr.row{cursor:pointer}
tr.det td{background:#fbfcfd;color:#3d4550;font-size:12.5px;padding:10px 14px 14px}
tr.det .kv{margin:2px 0}
tr.det .kv b{color:#111;font-weight:600}
.q{border-left:3px solid var(--accent);padding:6px 10px;margin:6px 0;background:#fff;border-radius:0 5px 5px 0}
.q .meta{color:var(--muted);font-size:11.5px;margin-bottom:3px}
.tick{color:var(--muted);font-size:11.5px}
.pos{color:var(--good)}.neg{color:var(--hot)}
.pill{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11.5px;white-space:nowrap;border:1px solid transparent}
.pill.a{background:#e8f3ec;color:#1a7f37;border-color:#c6e5d0}
.pill.b{background:#fdf3e0;color:#9a6700;border-color:#f3e0b8}
.pill.t{background:#eef2ff;color:#3730a3;border-color:#dfe4ff}
.pill.g{background:#f2f4f7;color:#4b5563;border-color:#e3e6ea}
.pill.warn{background:#fdecec;color:#b3261e;border-color:#f7cfcf}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.gbar{height:6px;border-radius:3px;background:linear-gradient(90deg,#1f6feb,#57a6ff);display:inline-block;
  vertical-align:middle;margin-left:6px;min-width:2px}
/* 占比条：0-100 的比率走线性满槽，柱长必须等于数字本身，不做开方压缩 */
.shnum{display:inline-block;width:46px;text-align:right}
.sbar{display:inline-block;vertical-align:middle;margin-left:8px;width:90px;height:6px;
  border-radius:3px;background:#eef1f4;overflow:hidden}
.sbar>i{display:block;height:100%;border-radius:3px;background:linear-gradient(90deg,#1f6feb,#57a6ff)}
.chain{color:var(--muted);font-size:11.5px}
.empty{padding:22px 16px;color:var(--muted);text-align:center;font-size:13px}
.grp{padding:10px 16px;background:#fafbfc;border-bottom:1px solid var(--line);font-size:13px;font-weight:600}
.hm{display:grid;grid-template-columns:190px 1fr;gap:6px 10px;align-items:center;padding:12px 16px;
  max-height:520px;overflow:auto}
.hm .co{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hm .cells{display:flex;gap:5px}
.hm .per{display:flex;gap:1px}
.hm i{width:8px;height:15px;display:block;border-radius:1px}
.f{background:#1a7f37}.gp{background:#f0b429}.ms{background:#e3e6ea}
.lg{display:flex;gap:14px;padding:8px 16px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.lg span{display:flex;align-items:center;gap:5px}
.lg i{width:9px;height:9px;border-radius:2px;display:inline-block}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:1050px){.two{grid-template-columns:1fr}}
.spark{vertical-align:middle}
"""

JS = r"""
const D = window.__BOARD__;
const fmt = (v,d=1)=> v===null||v===undefined||v===''?'—':(typeof v==='number'? v.toFixed(d).replace(/\.0$/,''):v);
const pct = (v,d=1)=> v===null||v===undefined?'—':(v>=0?'+':'')+v.toFixed(d)+'%';
const pts = (v)=> v===null||v===undefined?'—':(v>=0?'+':'')+v.toFixed(1)+'pp';
const esc = s => String(s===null||s===undefined?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gradePill = g => g? `<span class="pill ${g==='A'?'a':'b'}">${g} 级</span>` : '';
/* AI 归属依据：证据等级说数字有多硬，这里说这笔钱凭什么算 AI。
   judged_by=system 的两档用告警色，因为 AI 这个标签是我们贴的，不是公司贴的。 */
const BASIS = {
  company_ai_line:    {s:'公司口径即 AI',   c:'a',    sys:false},
  company_ai_related: {s:'公司称 AI 相关',  c:'b',    sys:false},
  analyst_ai_product: {s:'本系统归入',      c:'warn', sys:true},
  analyst_related:    {s:'本系统主观挂载',  c:'warn', sys:true},
};
const basisPill = b => {
  const m = BASIS[b];
  if(!m) return '<span class="pill g">未标注</span>';
  return `<span class="pill ${m.c}">${m.sys?'⚠ ':''}${m.s}</span>`;
};
const basisLine = r => r.ai_basis
  ? `<div class="kv"><b>AI 归属依据</b>：${basisPill(r.ai_basis)} ${esc(r.ai_basis_desc||'')}</div>`
  : `<div class="kv"><b>AI 归属依据</b>：<span class="pill g">未标注</span> 这条记录入库时还没有这个字段，需回原始披露补判。</div>`;
const mkt = t => /\.(HK|SS|SZ|BJ)$/i.test(t||'') ? 'AH' : 'US';
const src = (u,d)=> u? `<a href="${esc(u)}" target="_blank" rel="noopener">一手件</a>${d?` <span class="tick">${esc(d)}</span>`:''}` : '<span class="tick">无链接</span>';

/* ---------- 过滤 ---------- */
const F = {q:'',track:'',market:'',grade:'',basis:''};
function pass(r){
  if(F.track && r.track!==F.track) return false;
  if(F.market && mkt(r.ticker)!==F.market) return false;
  if(F.grade && (r.evidence_grade||'')!==F.grade) return false;
  /* basis=__system__ 是「只看 AI 归属由我们判定的那批」，不是某一个具体档位 */
  if(F.basis){
    const b = r.ai_basis||'';
    if(F.basis==='__system__'){ if(!(BASIS[b]||{}).sys) return false; }
    else if(b!==F.basis) return false;
  }
  if(F.q){
    const hay=[r.company,r.ticker,r.metric_label,r.definition,r.tier_label,r.ai_basis_label].join(' ').toLowerCase();
    if(!hay.includes(F.q)) return false;
  }
  return true;
}

/* ---------- 排序表 ---------- */
function table(rows, cols, opt={}){
  if(!rows.length) return `<div class="empty">${opt.empty||'当前筛选下没有条目'}</div>`;
  const id='t'+Math.random().toString(36).slice(2,8);
  const head=cols.map((c,i)=>`<th class="${c.sort===false?'no-sort':''}" data-i="${i}">${c.h}${c.sort===false?'':'<span class="ar">⇅</span>'}</th>`).join('');
  const body=rows.map((r,ri)=>{
    /* 有 v() 的列按 v() 的数值排，不按单元格文本排。金额列必须走这条：
       按文本排会把 1127.9 CNY 排到 800 USD 前面，那是币种差别不是规模差别。 */
    const tds=cols.map(c=>{
      const v = c.v? c.v(r) : undefined;
      const a = (v===null||v===undefined||isNaN(v))? (c.v? ' data-v=""':'') : ` data-v="${v}"`;
      return `<td${a}>${c.f(r)}</td>`;
    }).join('');
    const det=opt.detail? `<tr class="det" data-for="${ri}" style="display:none"><td colspan="${cols.length}">${opt.detail(r)}</td></tr>`:'';
    return `<tr class="row" data-ri="${ri}">${tds}</tr>${det}`;
  }).join('');
  return `<table id="${id}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
document.addEventListener('click', e=>{
  const tr=e.target.closest('tr.row');
  if(tr && !e.target.closest('a')){
    const d=tr.nextElementSibling;
    if(d && d.classList.contains('det')) d.style.display = d.style.display==='none'?'':'none';
  }
  const th=e.target.closest('th[data-i]');
  if(th && !th.classList.contains('no-sort')) sortBy(th);
});
function sortBy(th){
  const i=+th.dataset.i, tb=th.closest('table').querySelector('tbody');
  const asc = th.dataset.dir!=='asc'; th.dataset.dir=asc?'asc':'desc';
  const pairs=[];
  [...tb.children].forEach(tr=>{ if(tr.classList.contains('row')) pairs.push([tr, tr.nextElementSibling&&tr.nextElementSibling.classList.contains('det')?tr.nextElementSibling:null]); });
  const key=tr=>{
    const td=tr.children[i];
    if(td.hasAttribute('data-v')){
      const raw=td.getAttribute('data-v');
      if(raw==='') return -Infinity;          /* 没有可比值的行沉底，不拿原币数字顶上来 */
      const n=parseFloat(raw); if(!isNaN(n)) return n;
    }
    const t=td.innerText.replace(/[,%+]/g,'').replace(/pp$/,'').trim();
    const n=parseFloat(t); return isNaN(n)? t.toLowerCase() : n;
  };
  pairs.sort((a,b)=>{const x=key(a[0]),y=key(b[0]);
    if(typeof x==='number'&&typeof y==='number') return asc?x-y:y-x;
    return asc? String(x).localeCompare(String(y),'zh'):String(y).localeCompare(String(x),'zh');});
  tb.innerHTML=''; pairs.forEach(([r,d])=>{tb.appendChild(r); if(d)tb.appendChild(d);});
}

/* ---------- 增速条：开方压缩，2203% 不把 20% 压成看不见 ---------- */
function gbar(v,max){
  if(v===null||v===undefined) return '';
  const w = Math.max(2, Math.round(Math.sqrt(Math.max(v,0))/Math.sqrt(Math.max(max,1))*90));
  return `<span class="gbar" style="width:${w}px"></span>`;
}
/* ---------- 占比条：线性满槽，26.3% 就画 90px 槽里的 26.3% ---------- */
function sbar(v){
  if(v===null||v===undefined) return '';
  const p=Math.max(0,Math.min(100,Number(v)));
  return `<span class="sbar" title="${p.toFixed(1)}% / 100%"><i style="width:${p.toFixed(1)}%"></i></span>`;
}
function spark(hist){
  if(!hist||hist.length<2) return '';
  const ys=hist.map(h=>h.yoy_pct).filter(v=>v!==null&&v!==undefined);
  if(ys.length<2) return '';
  const w=88,h=22,mn=Math.min(...ys,0),mx=Math.max(...ys);
  const rng=(mx-mn)||1;
  const pt=ys.map((v,i)=>`${(i/(ys.length-1)*w).toFixed(1)},${(h-(v-mn)/rng*h).toFixed(1)}`).join(' ');
  const zero=(h-(0-mn)/rng*h).toFixed(1);
  return `<svg class="spark" width="${w}" height="${h}"><line x1="0" y1="${zero}" x2="${w}" y2="${zero}" stroke="#e3e6ea"/>`+
    `<polyline points="${pt}" fill="none" stroke="#1f6feb" stroke-width="1.6"/>`+
    `<circle cx="${w}" cy="${(h-(ys[ys.length-1]-mn)/rng*h).toFixed(1)}" r="2.2" fill="#1f6feb"/></svg>`;
}

/* ---------- 金额口径 ----------
   一条铁规矩：凡是要比大小的金额，只能来自 value_usd_mn / arr_usd_mn。
   原币金额只出现在「原币金额」列和展开的口径行里，永远不进排序。 */
const FXBASIS = {
  native:        {s:'原始即美元',   c:'g'},
  disclosed:     {s:'入库已折算',   c:'g'},
  converted:     {s:'固定汇率折算', c:'b'},
  unconvertible: {s:'币种未识别',   c:'warn'},
  none:          {s:'非金额口径',   c:'g'},
};
function usdScale(r){
  const v = (r.value_usd_mn!==null&&r.value_usd_mn!==undefined)? r.value_usd_mn : r.arr_usd_mn;
  return (v===null||v===undefined)? null : v;
}
function origCell(r){
  if(r.currency_value_mn===null||r.currency_value_mn===undefined){
    /* 原币就是美元，没有第二个口径可给，写「—」而不是留白 */
    return r.currency==='USD'? '<span class="tick">—</span>'
      : `<span class="num">${fmt(r.value,1)}</span> <span class="tick">${esc(r.unit||'')}</span>`;
  }
  const b=FXBASIS[r.usd_basis]||{};
  return `<span class="num">${fmt(r.currency_value_mn,1)}</span> <span class="tick">mn ${esc(r.currency)}</span>`+
    (b.s?` <span class="pill ${b.c}" title="${esc(r.usd_basis_label||'')}">${b.s}</span>`:'')+
    (r.fx_rate?` <span class="tick">@${esc(r.fx_rate)}</span>`:'');
}
function origRevCell(r){
  if(r.reporting_currency==='USD') return '<span class="tick">—</span>';
  if(r.total_revenue_orig_mn===null||r.total_revenue_orig_mn===undefined)
    /* 「—」留给确认以美元列报的公司；列报币种没记的写「未记录」，
       两件事不能都画成一横杠——后者是缺口，前者不是。 */
    return '<span class="tick">未记录</span>';
  const lab = r.revenue_orig_basis==='note_rate_derived'? '按备注汇率回推'
            : (r.revenue_orig_basis==='recorded'? '入库字段':'披露原值');
  return `<span class="num">${fmt(r.total_revenue_orig_mn,1)}</span> `+
    `<span class="tick">mn ${esc(r.reporting_currency)}</span> <span class="pill g">${lab}</span>`;
}

/* ---------- 表一 ---------- */
function t1detail(r){
  const notes=(r.flags||[]).concat(r.growth_blocked||[]);
  return `<div class="kv"><b>口径</b>：${esc(r.definition)}（${esc(r.period_kind_label)}）</div>`+
    basisLine(r)+
    (r.reason?`<div class="kv"><b>分流理由</b>：${esc(r.reason)}</div>`:'')+
    (r.proxy_only_reason?`<div class="kv"><b>代理口径说明</b>：${esc(r.proxy_only_reason)}</div>`:'')+
    (notes.length?`<div class="kv"><b>注意</b>：${esc(notes.join('；'))}</div>`:'')+
    `<div class="kv"><b>来源</b>：${src(r.source_url,r.source_date)}　<b>原值</b>：<span class="num">${fmt(r.value,3)}</span> ${esc(r.unit)}`+
    (r.usd_basis_label?`　<b>美元口径</b>：${esc(r.usd_basis_label)}`:'')+`</div>`;
}
function t1cols(max){
  return [
    {h:'#', f:r=>`<span class="num">${r.rank||''}</span>`},
    {h:'公司', f:r=>`${esc(r.company)}<br><span class="tick">${esc(r.ticker)}</span>`},
    {h:'赛道', f:r=>`<span class="pill g">${esc(D.trackLabel[r.track]||r.track||'')}</span>`},
    {h:'指标', f:r=>esc(r.metric_label)},
    {h:'期间', f:r=>`<span class="num">${esc(r.period)}</span>`},
    /* 规模只用一个口径：美元。排序也只认这一列。
       原币金额单独出一列，看得见、可回溯，但不参与比大小。 */
    {h:'规模 mn USD', v:r=>usdScale(r), f:r=>{
      const usd = usdScale(r);
      const main = usd!==null
        ? `<span class="num">${fmt(usd,1)}</span> <span class="tick">mn USD</span>`
        : '<span class="pill warn">未折算</span>';
      const sh = r.revenue_share_pct!=null? `<br><span class="tick">占总收入 ${fmt(r.revenue_share_pct)}%</span>`:'';
      return main+sh;
    }},
    {h:'原币金额', sort:false, f:r=>origCell(r)},
    {h:'同比', f:r=>r.growth_pct===null||r.growth_pct===undefined?'<span class="tick">算不出</span>'
        :`<span class="num ${r.growth_pct>=0?'pos':'neg'}">${pct(r.growth_pct)}</span>${gbar(r.growth_pct,max)}`},
    {h:'档位', f:r=>`<span class="pill t">${esc(r.tier_emoji||'')}${esc(r.tier_label||'')}</span>`},
    {h:'证据', f:r=>gradePill(r.evidence_grade)+(r.value_is_bound?' <span class="pill warn">下限</span>':'')},
    {h:'AI 归属', f:r=>basisPill(r.ai_basis)},
    {h:'来源', sort:false, f:r=>src(r.source_url,r.source_date)},
  ];
}

/* ---------- 表二 ---------- */
function t2detail(r){
  const at=(r.attribution||[]).slice(0,3).map(a=>
    `<div class="q"><div class="meta">${esc(a.period)} · ${esc(a.type_label)} · ${esc(a.evidence_grade)} 级 · ${src(a.source_url,a.source_date)}</div>${esc(a.quote)}</div>`).join('');
  const px=(r.ai_proxies||[]).map(p=>`${esc(p.metric_label||p.metric)} ${esc(p.period)} <span class="num">${fmt(p.value)}</span>${esc(p.unit||'')}`).join('；');
  return (r.reasons&&r.reasons.length?`<div class="kv"><b>判定</b>：${esc(r.reasons.join('；'))}</div>`:'')+
    (r.includes_ma?`<div class="kv"><b>并表口径</b>：${esc(r.ma_note||'本期含重大并表/剥离')}；`+
      `as-reported 同比 ${fmt(r.revenue_yoy_as_reported_pct)}%，`+
      (r.organic_yoy_pct===null||r.organic_yoy_pct===undefined?'公司未给同口径同比':`同口径同比 ${fmt(r.organic_yoy_pct)}%`)+
      `</div>`:'')+
    `<div class="kv"><b>业务纯度</b>：${esc(r.ai_purity)} — ${esc(r.ai_purity_label||'')}</div>`+
    (px?`<div class="kv"><b>代理量</b>：${px}</div>`:'')+
    (r.nrr_pct?`<div class="kv"><b>净留存</b>：<span class="num">${fmt(r.nrr_pct)}%</span></div>`:'')+
    (r.history&&r.history.length?`<div class="kv"><b>同比序列</b>：${r.history.map(h=>`${h.period} ${h.yoy_pct===null||h.yoy_pct===undefined?'—':fmt(h.yoy_pct)+'%'}`).join(' → ')}</div>`:'')+
    (at?`<div class="kv" style="margin-top:6px"><b>管理层归因原话</b>${at}</div>`:'<div class="kv"><b>管理层归因原话</b>：无</div>');
}
const t2cols=[
  {h:'公司', f:r=>`${esc(r.company)}<br><span class="tick">${esc(r.ticker)}</span>`},
  {h:'赛道', f:r=>`<span class="pill g">${esc(D.trackLabel[r.track]||r.track||'')}</span>`},
  {h:'期间', f:r=>`<span class="num">${esc(r.period)}</span>`},
  {h:'总收入 mn USD', v:r=>r.total_revenue_usd_mn, f:r=>`<span class="num">${fmt(r.total_revenue_usd_mn,1)}</span> <span class="tick">mn USD</span>`},
  {h:'原币金额', sort:false, f:r=>origRevCell(r)},
  {h:'同比', v:r=>r.revenue_yoy_pct, f:r=>`<span class="num ${(r.revenue_yoy_pct||0)>=0?'pos':'neg'}">${pct(r.revenue_yoy_pct)}</span>`+
    (r.revenue_yoy_basis==='unusable'?' <span class="pill warn">含并表·不可比</span>'
     :r.revenue_yoy_basis==='organic'?' <span class="pill g">同口径</span>':'')},
  {h:'增速链', sort:false, f:r=>`<span class="chain">← 上期 ${fmt(r.revenue_yoy_prev_pct)}% ← 去年同期 ${fmt(r.revenue_yoy_year_ago_pct)}%</span> ${spark(r.history)}`},
  {h:'较上期', f:r=>`<span class="num ${(r.yoy_chg_vs_prev_pts||0)>=0?'pos':'neg'}">${pts(r.yoy_chg_vs_prev_pts)}</span>`},
  {h:'较去年同期', f:r=>`<span class="num ${(r.yoy_chg_vs_year_ago_pts||0)>=0?'pos':'neg'}">${pts(r.yoy_chg_vs_year_ago_pts)}</span>${r.yoy_chg_diverged?' <span class="pill warn">⚠单期已回落</span>':''}`},
  {h:'归因', f:r=>r.has_attribution?`<span class="pill a">有 ${(r.attribution||[]).length} 条</span>`:'<span class="pill g">无</span>'},
];

/* ---------- 表三 ---------- */
function t3detail(r){
  const notes=(r.flags||[]).concat(r.growth_blocked||[]);
  return `<div class="kv"><b>口径</b>：${esc(r.definition)}（${esc(r.period_kind_label)}）</div>`+
    `<div class="kv"><b>趋势依据</b>：${esc(r.trend_basis)}</div>`+
    (notes.length?`<div class="kv"><b>注意</b>：${esc(notes.join('；'))}</div>`:'')+
    `<div class="kv"><b>来源</b>：${src(r.source_url,r.source_date)}</div>`;
}
const t3cols=[
  {h:'公司', f:r=>`${esc(r.company)}<br><span class="tick">${esc(r.ticker)}</span>`},
  {h:'赛道', f:r=>`<span class="pill g">${esc(D.trackLabel[r.track]||r.track||'')}</span>`},
  {h:'口径指标', f:r=>esc(r.metric_label)+(r.is_embedded_proxy?' <span class="pill g">⚑代理量</span>':'')},
  {h:'期间', f:r=>`<span class="num">${esc(r.period)}</span>`},
  {h:'数值', f:r=>`<span class="num">${fmt(r.value,2)}</span> <span class="tick">${esc(r.unit)}</span>`},
  {h:'同比', f:r=>r.yoy_pct===null||r.yoy_pct===undefined?'<span class="tick">—</span>':`<span class="num ${r.yoy_pct>=0?'pos':'neg'}">${pct(r.yoy_pct)}</span>`},
  {h:'环比', f:r=>r.qoq_pct===null||r.qoq_pct===undefined?'<span class="tick">—</span>':`<span class="num ${r.qoq_pct>=0?'pos':'neg'}">${pct(r.qoq_pct)}</span>`},
  {h:'证据', f:r=>gradePill(r.evidence_grade)+(r.value_is_bound?' <span class="pill warn">下限</span>':'')},
  {h:'来源', sort:false, f:r=>src(r.source_url,r.source_date)},
];

/* ---------- 表二 ---------- */
const shcols=[
  {h:'#', f:r=>`<span class="num">${r.rank||''}</span>`},
  {h:'公司', f:r=>`${esc(r.company)}<br><span class="tick">${esc(r.ticker)}</span>`},
  {h:'占比', f:r=>`<span class="num shnum">${r.share_pct===null||r.share_pct===undefined?'—':Number(r.share_pct).toFixed(1)}%</span>${sbar(r.share_pct)}`},
  {h:'期间', f:r=>`<span class="num">${esc(r.period)}</span>`},
  {h:'档位', f:r=>`<span class="pill t">${esc(r.tier_emoji||'')}${esc(r.tier)}</span>`},
  {h:'来源口径', f:r=>`<span class="pill g">${r.origin==='field_on_metric'?'挂在 AI 收入记录上':'公司直接披露'}</span>`},
  {h:'证据', f:r=>gradePill(r.evidence_grade)},
  {h:'AI 归属', f:r=>basisPill(r.ai_basis)},
  {h:'来源', sort:false, f:r=>src(r.source_url,r.source_date)},
];
const shdetail=r=>`<div class="kv"><b>口径</b>：${esc(r.definition)}</div>`+basisLine(r)+
  `<div class="kv"><b>分档规则</b>：${esc(r.tier_rule||'')}</div><div class="kv"><b>来源</b>：${src(r.source_url,r.source_date)}</div>`;
/* ---------- 渲染 ---------- */
function card(title,count,rule,inner,folded){
  return `<div class="card${folded?' fold':''}"><h2><span class="fold" data-fold>${folded?'▸':'▾'}</span>${title}`+
    `<span class="cnt">${count}</span>${rule?`<span class="rule">${esc(rule)}</span>`:''}`+
    `<button class="csv" data-csv>CSV</button></h2><div class="body">${inner}</div></div>`;
}
/* 折叠 + 导出当前视图 */
document.addEventListener('click', e=>{
  const f=e.target.closest('[data-fold]');
  if(f){const c=f.closest('.card');c.classList.toggle('fold');f.textContent=c.classList.contains('fold')?'▸':'▾';return;}
  const b=e.target.closest('[data-csv]');
  if(b){
    const c=b.closest('.card'), t=c.querySelector('table');
    if(!t) return;
    const rows=[[...t.querySelectorAll('thead th')].map(th=>th.innerText.replace('⇅','').trim())];
    t.querySelectorAll('tr.row').forEach(tr=>rows.push([...tr.children].map(td=>td.innerText.replace(/\n+/g,' ').trim())));
    const csv='﻿'+rows.map(r=>r.map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\n');
    const a=document.createElement('a');
    a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
    a.download=(c.querySelector('h2').innerText.split('\n')[0].replace(/[^\w一-龥]+/g,'_')||'board')+'.csv';
    a.click(); URL.revokeObjectURL(a.href);
  }
});
function render(){
  const t1=D.one.boards, maxg=Math.max(...(t1.main||[]).map(r=>r.growth_pct||0),1);
  // 表一
  let h='';
  const main=(t1.main||[]).filter(pass);
  h+=card('主榜 · AI 分项变现',`${main.length} / ${(t1.main||[]).length} 条`,'A/B 级财务指标且规模过闸门',
      table(main,t1cols(maxg),{detail:t1detail}));
  [['no_growth','算不出增速','有值但缺可比对比期；「首次披露」是查过的终态，「未核」才是剩下的活'],
   ['small_base','小基数','分母太小，增速失真'],
   ['caveat','口径存疑','one_off / definition_changed 等硬标记'],
   ['proxy','代理口径','不是纯 AI 分项披露，只作参考']].forEach(([k,label,rule])=>{
    const rows=(t1[k]||[]).filter(pass);
    let sub=`${rows.length} / ${(t1[k]||[]).length} 条`;
    if(k==='no_growth'){
      const n=c=>rows.filter(r=>r.no_growth_kind===c).length;
      sub+=`　首次披露 ${n('first_disclosure')}　未核 ${n('unchecked')}`;
    }
    h+=card(label,sub,rule,table(rows,k==='no_growth'?t1cols(maxg).concat([
      {h:'对比期',f:r=>r.no_growth_kind==='first_disclosure'
        ?`<span class="pill a">首次披露</span> <span class="tick">${esc(r.compare_period||'')}</span>`
        :(r.no_growth_kind==='unchecked'?`<span class="pill g">未核 ${esc(r.compare_period||'')}</span>`:'<span class="tick">不可比</span>')},
    ]):t1cols(maxg),{detail:t1detail}));
  });
  document.getElementById('p1').innerHTML=h;

  // 表二 占比
  const sh=(D.one.share_board&&D.one.share_board.rows||[]).filter(pass);
  document.getElementById('p2').innerHTML=card('表二 AI Monetization as % of Headline',`${sh.length} 条`,
      '按占比水平排，不按增速排：比率的同比不可比',table(sh,shcols,{detail:shdetail}));

  // 表二
  const tiers=['accelerating','native_accel','high_absolute','high_plateau','eroding','basis_unclear','insufficient','watch'];
  let h2='';
  tiers.forEach(t=>{
    const rows=D.two.results.filter(r=>r.tier===t).filter(pass);
    if(!rows.length && t==='watch') return;
    const meta=D.two.tiers[t]||{};
    if(!rows.length) return;
    rows.sort((a,b)=>(b.yoy_chg_vs_year_ago_pts??-999)-(a.yoy_chg_vs_year_ago_pts??-999));
    h2+=card(`${meta.emoji||''} ${meta.label||t}`,`${rows.length} 家`,meta.rule||'',
      table(rows,t2cols,{detail:t2detail}), t==='watch');
  });
  const bg=D.two.by_growth.filter(pass);
  h2+=card('绝对增速榜',`${bg.length} 家`,'按总收入同比水平直接排名，不看有没有抬升',
      table(bg,[
        {h:'#',f:r=>`<span class="num">${r.rank}</span>`},
        {h:'公司',f:r=>`${esc(r.company)}<br><span class="tick">${esc(r.ticker)}</span>`},
        {h:'期间',f:r=>`<span class="num">${esc(r.period)}</span>`},
        {h:'总收入 mn USD',v:r=>r.total_revenue_usd_mn,f:r=>`<span class="num">${fmt(r.total_revenue_usd_mn,1)}</span> <span class="tick">mn USD</span>`},
        {h:'原币金额',sort:false,f:r=>origRevCell(r)},
        {h:'同比',f:r=>`<span class="num pos">${pct(r.revenue_yoy_pct)}</span>${gbar(r.revenue_yoy_pct,Math.max(...D.two.by_growth.map(x=>x.revenue_yoy_pct||0),1))}`},
        {h:'增速变化',f:r=>`<span class="num ${(r.yoy_chg_pts||0)>=0?'pos':'neg'}">${pts(r.yoy_chg_pts)}</span>`},
        {h:'档位',f:r=>`<span class="pill t">${esc(r.tier_label)}</span>`},
        {h:'归因',f:r=>r.has_attribution?'<span class="pill a">有</span>':'<span class="pill g">无</span>'},
      ]));
  document.getElementById('p3').innerHTML=h2;

  // 表三
  let h3='';
  [['accelerating','📈 加速'],['flat','➖ 持平'],['decelerating','📉 减速'],['unknown','❔ 无法判断趋势']].forEach(([k,label])=>{
    const rows=(D.three.groups[k]||[]).filter(pass);
    if(!rows.length) return;
    h3+=card(label,`${rows.length} / ${(D.three.groups[k]||[]).length} 条`,
      k==='unknown'?'环比与同比的增速变化都算不出，如实标注，不猜':'', table(rows,t3cols,{detail:t3detail}));
  });
  document.getElementById('p4').innerHTML=h3;

  document.getElementById('hint').textContent =
    `筛选后：表一主榜 ${main.length} · 表二 ${sh.length} · 表三 ${D.two.results.filter(pass).filter(r=>r.tier!=='watch').length} · 表三 ${['accelerating','flat','decelerating','unknown'].reduce((a,k)=>a+(D.three.groups[k]||[]).filter(pass).length,0)}`;
}

/* ---------- 数据完整度 ---------- */
function renderHealth(){
  const H=D.health, C=H.coverage;
  let h='';
  const CU=H.currency||{};
  if(CU.metric_usd_basis){
    const lab={native:'原始即美元',disclosed:'入库已折算',converted:'固定汇率折算',unconvertible:'币种未识别',none:'非金额口径',unlabeled:'未标注'};
    const mb=Object.entries(CU.metric_usd_basis).map(([k,v])=>`${lab[k]||k} <b>${v}</b> 条`).join('　');
    const rlab={note_exact:'披露原值',note_rate_derived:'按备注汇率回推',recorded:'入库字段',unknown:'原币未记录'};
    const rb=Object.entries(CU.revenue_orig_basis||{}).map(([k,v])=>`${rlab[k]||k} <b>${v}</b> 家`).join('　');
    const bad=(CU.unconvertible||[]).map(x=>`${esc(x.company)} <span class="tick">${esc(x.ticker)} ${esc(x.unit||'')}</span>`).join('、');
    h+=card('金额口径覆盖',`未折算 ${(CU.unconvertible||[]).length} 条`,
      '排名只认美元列；折不出美元的行不参与规模排序，原币未记录的只影响回溯，不影响排名',
      `<div style="padding:10px 16px"><div class="kv"><b>表一 美元口径来源</b>：${mb}</div>`+
      `<div class="kv"><b>表三 原币金额来源</b>：${rb||'—'}</div>`+
      (bad?`<div class="kv"><b>折不出美元</b>：${bad}　<span class="tick">补 --value-usd-mn，或在 CLI 的 FX_TO_USD 里加这个币种</span></div>`:'')+
      `</div>`);
  }
  const depth=Object.entries(H.series_depth).map(([k,v])=>`${k} 期 <b>${v}</b> 条`).join('　');
  h+=card('指标序列深度','','增速变化要三个可比期，单点序列一个都算不出',
    `<div style="padding:12px 16px">
      <div class="kv">序列总数 <b class="num">${H.series_total}</b>，其中单点序列 <b class="num">${H.single_point_series}</b> 条（<b>${(H.single_point_series/Math.max(H.series_total,1)*100).toFixed(0)}%</b>）</div>
      <div class="kv" style="margin-top:6px">分布：${depth}</div>
      <div class="note" style="margin-top:10px">这一项是四表最大的天花板：表一 ${H.t1_growth_blocked['yoy 无可比对比期']||0} 条算不出同比、表四 ${H.t3_single_point} 条判不出趋势，根因都在这里。补的方法是给已入库的指标补上一年前同期，而不是加新公司。</div>
    </div>`);

  const nm=H.t2_near_miss, ready=nm.filter(x=>x.ready);
  const withCommentary=nm.filter(x=>(x.ai_commentary||[]).length);
  h+=card('表三 · 只差一条收入归因原话',`${nm.length} 家（其中 ${ready.length} 家补完立刻进加速档，${withCommentary.length} 家已记录非收入归因 AI 点评）`,
    '规模与增速都过闸门，缺的是 A/B 级管理层把增长归因到 AI 的原话；已有点评算进展，不算归因',
    table(nm,[
      {h:'公司',f:r=>`${esc(r.company)}<br><span class="tick">${esc(r.ticker)}</span>`},
      {h:'赛道',f:r=>`<span class="pill g">${esc(D.trackLabel[r.track]||r.track||'')}</span>`},
      {h:'期间',f:r=>`<span class="num">${esc(r.period)}</span>`},
      {h:'总收入同比',f:r=>`<span class="num pos">${pct(r.revenue_yoy_pct)}</span>`},
      {h:'较去年同期',f:r=>`<span class="num ${(r.yoy_chg_vs_year_ago_pts||0)>=0?'pos':'neg'}">${pts(r.yoy_chg_vs_year_ago_pts)}</span>`},
      {h:'纯度',f:r=>`<span class="pill g">${esc(r.ai_purity)}</span>`},
      {h:'AI 点评',f:r=>{
          const c=r.ai_commentary||[];
          if(!c.length) return '<span class="tick">无</span>';
          return c.slice(0,2).map(a=>`<span class="pill ${a.type==='explicit_non_ai'?'warn':'a'}" title="${esc(a.quote)}">${esc(a.type_label)}</span>`).join(' ');
      }},
      {h:'状态',f:r=>r.ready?'<span class="pill a">补原话即入档</span>':'<span class="pill g">增速变化未达标</span>'},
    ]));

  const KL={first_disclosure:'查过：公司首次披露，历史无同口径（终态）',
            unchecked:'还没去查对比期（这是剩下的活）',
            unresolved:'口径本身不可比，不落对比期'};
  const K=H.t1_no_growth_kind||{};
  const kl=Object.entries(K).map(([k,v])=>`<div class="kv">${esc(KL[k]||k)}　<b class="num">${v}</b> 条</div>`).join('');
  const bl=Object.entries(H.t1_growth_blocked).map(([k,v])=>`<div class="kv">${esc(k)}　<b class="num">${v}</b> 条</div>`).join('')
    +(kl?`<div class="note" style="margin-top:10px">按能不能补再拆一层：</div>${kl}`:'');
  const fr=Object.entries(H.t2_period_freshness).map(([k,v])=>`<div class="kv"><span class="num">${esc(k)}</span>　<b class="num">${v}</b> 家</div>`).join('');
  h+=`<div class="two">
    ${card('表一 · 增速算不出的原因','','',`<div style="padding:12px 16px">${bl||'<span class="tick">无</span>'}</div>`)}
    ${card('表三 · 最新期分布','','看谁的财务还停在旧期',`<div style="padding:12px 16px">${fr}</div>`)}
  </div>`;

  if(C && C.summary){
    const s=C.summary, bt=C.summary_by_type;
    const rows=Object.entries(bt).map(([k,v])=>{
      const tot=v.filled+v.gap+v.missing;
      return `<div class="kv"><b>${esc(k)}</b>　已补 <span class="num">${v.filled}</span> · 已标缺口 <span class="num">${v.gap}</span> · 待补 <span class="num">${v.missing}</span>　<span class="tick">覆盖率 ${(v.filled/Math.max(tot,1)*100).toFixed(0)}%</span></div>`;
    }).join('');
    let hm='';
    const grid=C.grid||{}, periods=C.periods||[];
    const types=['fundamentals','metrics','quotes','experts'];
    const cls=v=>!v?'ms':(v==='filled'?'f':(String(v).startsWith('gap')?'gp':'ms'));
    Object.entries(grid).forEach(([tk,g])=>{
      const cells=periods.map(p=>{
        const cell=(g.periods||{})[p]||{};
        return `<span class="per">${types.map(t=>`<i class="${cls(cell[t])}" title="${esc(tk)} ${esc(p)} ${t}: ${esc(cell[t]||'missing')}"></i>`).join('')}</span>`;
      }).join('');
      hm+=`<div class="co" title="${esc(tk)}">${esc(g.company||tk)} <span class="tick">${esc(tk)}</span></div><div class="cells">${cells}</div>`;
    });
    h+=card('补全台账热力图',`${C.companies_in_scope} 家 × ${periods.length} 期 × 4 类 = ${C.cells_total} 格`,
      `已补 ${s.filled} · 已标缺口 ${s.gap} · 待补 ${s.missing}`,
      `<div class="lg"><span><i class="f"></i>已补</span><span><i class="gp"></i>已标缺口（确认没有，不是漏）</span><span><i class="ms"></i>待补</span>
        <span class="tick">每格四个小方块＝财务 / 指标 / 原话 / 专家；横向 ${periods.join(' · ')}</span></div>
       <div style="padding:10px 16px 0">${rows}</div><div class="hm">${hm}</div>`);
  }
  document.getElementById('p5').innerHTML=h;
}

/* ---------- 导航：带 hash，方便把某一张表直接发给人 ---------- */
function goto(pane){
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x.dataset.p===pane));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id===pane));
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  goto(b.dataset.p); history.replaceState(null,'','#'+b.dataset.p);
});
window.addEventListener('hashchange',()=>{const p=location.hash.slice(1); if(document.getElementById(p)) goto(p);});
const deb=(fn,ms=160)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)}};
document.getElementById('q').addEventListener('input',deb(e=>{F.q=e.target.value.trim().toLowerCase();render();}));
['track','market','grade','basis'].forEach(k=>document.getElementById(k).addEventListener('change',e=>{F[k]=e.target.value;render();}));
document.getElementById('reset').onclick=()=>{F.q=F.track=F.market=F.grade=F.basis='';
  document.getElementById('q').value='';['track','market','grade','basis'].forEach(k=>document.getElementById(k).value='');render();};
/* 表头吸顶要贴在筛选条下沿，条高随窗宽变，动态量一次 */
function syncBar(){
  const bar=document.querySelector('.bar');
  document.documentElement.style.setProperty('--barh', bar.offsetHeight+'px');
}
window.addEventListener('resize', syncBar); syncBar();

render(); renderHealth();
if(location.hash && document.getElementById(location.hash.slice(1))) goto(location.hash.slice(1));
"""


def render_html(payload, title="AI 应用四表排行榜"):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    meta = payload["meta"]
    k = payload["kpi"]
    kpis = "".join(
        f'<div class="kpi"><div class="v">{v}</div><div class="l">{lab}</div></div>'
        for lab, v in k
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {meta['generated_at'][:10]}</title>
<style>{CSS}</style></head>
<body>
<header>
  <h1>{title}</h1>
  <div class="sub">生成于 {meta['generated_at']}　·　覆盖池 {meta['universe']} 家　·　CLI v{meta['version']}　·　
  表一表三排名次，表四只分组不排名，表二按占比水平排　·　点任意一行看口径、注意事项与一手件链接</div>
  <div class="sub"><b>金额口径</b>：所有参与排名的金额都换成美元，只有「规模 mn USD」「总收入 mn USD」这两列能比大小；
  原币金额另出一列，看得见、能回溯，但不参与排序。折算用固定汇率（CNY 7.10、HKD 7.80），只为规模可比，不还原期间汇率。</div>
  <div class="sub">口径分两个轴：<b>证据等级</b>说这个数字有多硬，<b>AI 归属</b>说这笔钱凭什么算 AI。
  标 <span class="pill warn">⚠</span> 的两档，公司自己没把这条收入叫作 AI，归入 AI 是本系统的判断；
  用上面的「AI 归属依据」下拉可以只看这批。</div>
  <div class="kpis">{kpis}</div>
  <nav>
    <button class="on" data-p="p1">表一 AI Monetization</button>
    <button data-p="p2">表二 % of Headline</button>
    <button data-p="p3">表三 整体收入增速</button>
    <button data-p="p4">表四 Usage</button>
    <button data-p="p5">数据完整度</button>
  </nav>
</header>
<div class="bar">
  <input id="q" type="search" placeholder="搜公司 / 代码 / 口径关键词">
  <select id="track"><option value="">全部赛道</option><option value="toc_internet">ToC 互联网</option><option value="tob_software">ToB 软件</option><option value="ai_for_science">AI for Science</option></select>
  <select id="market"><option value="">全部市场</option><option value="AH">AH 股</option><option value="US">美股</option></select>
  <select id="grade"><option value="">全部证据等级</option><option value="A">仅 A 级</option><option value="B">仅 B 级</option></select>
  <select id="basis"><option value="">全部 AI 归属依据</option><option value="__system__">⚠ 仅本系统判定的</option><option value="company_ai_line">公司口径即 AI</option><option value="company_ai_related">公司称 AI 相关</option><option value="analyst_ai_product">具名 AI 产品线，本系统归入</option><option value="analyst_related">本系统主观判断相关性后挂载</option></select>
  <button class="mini" id="reset">清空筛选</button>
  <span class="hint" id="hint"></span>
</div>
<main>
  <div class="pane on" id="p1"></div>
  <div class="pane" id="p2"></div>
  <div class="pane" id="p3"></div>
  <div class="pane" id="p4"></div>
  <div class="pane" id="p5"></div>
</main>
<script type="application/json" id="board-data">{data}</script>
<script>window.__BOARD__=JSON.parse(document.getElementById('board-data').textContent);</script>
<script>{JS}</script>
</body></html>"""


def build_payload(one, two, three, health, generated_at, version, universe_count):
    t1 = one.get("boards", {})
    share = one.get("share_board", {}) or {}
    kpi = [
        ("表一主榜", len(t1.get("main", []))),
        ("表二占比", len(share.get("rows", []))),
        ("表二入档", sum(1 for r in two.get("results", []) if r.get("tier") != "watch")),
        ("表三口径", sum(len(v) for v in three.get("groups", {}).values())),
        ("入库指标条数", one.get("records", 0)),
        ("管理层原话", health.get("quote_rows", 0)),
        ("单点序列占比", f"{health['single_point_series'] / max(health['series_total'], 1) * 100:.0f}%"),
    ]
    cov = (health.get("coverage") or {}).get("summary") or {}
    if cov:
        kpi.append(("台账已补", cov.get("filled", 0)))
    return {
        "meta": {
            "generated_at": generated_at,
            "version": version,
            "universe": universe_count,
        },
        "kpi": kpi,
        "trackLabel": TRACK_LABEL,
        "one": one,
        "two": two,
        "three": three,
        "health": health,
    }
