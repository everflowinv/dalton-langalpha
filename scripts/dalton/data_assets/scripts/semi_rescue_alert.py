#!/usr/bin/env python3
"""半导体环境追踪 alert：取数 → 打分 → 出两张图 → 写飞书卡片 markdown →（可选）发送。

用法（workspace 根目录）:
    python3 scripts/semi_rescue_alert.py run --slot auto --dry-run      # 只生成，不发送
    python3 scripts/semi_rescue_alert.py run --slot auto --send --json  # 生成并发到 Semi Sentiment 群
    python3 scripts/semi_rescue_alert.py self-test

配置 config/semi_rescue_alert.json；说明 scripts/semi_rescue_alert.md。
退出码：0 成功；1 关键行情取不到或发送失败（cron 记失败并告警）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import warnings
warnings.filterwarnings("ignore")

def _resolve_workspace() -> Path:
    for var in ("DALTON_DATA_TASK_WORKSPACE", "DALTON_WORKSPACE", "OPENCLAW_WORKSPACE", "WORKDIR"):
        env_ws = (os.environ.get(var) or "").strip()
        if env_ws:
            return Path(env_ws)
    return Path.cwd()


def _read_data_task_config_file(config_path: Path) -> dict[str, str]:
    if not config_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def _resolve_sec_user_agent(cfg: dict) -> str:
    keys = ("DALTON_SEC_USER_AGENT", "DALTON_DATA_TASK_SEC_USER_AGENT", "SEC_USER_AGENT")
    for key in keys:
        env_val = (os.environ.get(key) or "").strip()
        if env_val:
            return env_val
    cfg_override = (os.environ.get("DALTON_DATA_TASKS_CONFIG") or "").strip()
    cfg_path = Path(cfg_override) if cfg_override else (_resolve_workspace() / "config" / "data_tasks.env")
    env_cfg = _read_data_task_config_file(cfg_path)
    for key in keys:
        val = env_cfg.get(key, "").strip()
        if val:
            return val
    return str(cfg.get("sec_user_agent") or "").strip()


ROOT = _resolve_workspace()
CONFIG = ROOT / "config" / "semi_rescue_alert.json"
STATE_DIR = ROOT / "state" / "semi-rescue-alert"
OUT_DIR = ROOT / "research-outputs" / "semi-rescue-alert"
PB_WEEKLY = ROOT / "research-outputs" / "gs-pb-weekly" / "gs_pb_core_weekly_latest.csv"
PB_DAILY = ROOT / "research-outputs" / "gs-pb-weekly" / "gs_pb_charts_daily_latest.csv"
SEND_CARD = ROOT / "scripts" / "feishu_send_card.py"

ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")
UA = "Mozilla/5.0"  # FRED 会拖住带自定义字样的 UA（实测超时），保持最简
WEEKDAY_CN = "一二三四五六日"
STATUS_TXT = {"G": "救场", "Y": "中性", "R": "坏信号", "NA": "缺数据"}
STATUS_EMO = {"G": "🟢", "Y": "🟡", "R": "🔴", "NA": "⚪"}
STATUS_COLOR = {"G": "#1f9d55", "Y": "#d69e2e", "R": "#d64545", "NA": "#a0aec0"}
PILLARS = ["价格", "基本面", "融资", "宏观", "仓位"]
PILLAR_DESC = {"价格": "价格与技术", "基本面": "基本面与需求", "融资": "融资与信用", "宏观": "宏观与利率", "仓位": "仓位与资金"}


# ------------------------------------------------------------------ utils
def http_get(url: str, timeout: int = 30, headers: dict | None = None) -> bytes:
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def log(msg):
    if os.environ.get("SEMI_DEBUG"):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def fmt_pct(x, d=1, sign=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+.{d}f}%" if sign else f"{x:.{d}f}%"


def fmt_num(x, d=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.{d}f}"


def md(d) -> str:
    d = d if isinstance(d, date) else datetime.fromisoformat(str(d)[:10]).date()
    return f"{d.month}/{d.day}"


def grade(value, green, red, higher_is_better: bool):
    """green/red 是阈值：higher_is_better=True 时 value>=green 为 G、<=red 为 R。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "NA"
    if higher_is_better:
        return "G" if value >= green else ("R" if value <= red else "Y")
    return "G" if value <= green else ("R" if value >= red else "Y")


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


def ind(id_, pillar, name, status, value, rescue, bad, note="", asof=None, key=False, raw=None, manual=False):
    return {"id": id_, "pillar": pillar, "name": name, "status": status, "value": value,
            "rescue": rescue, "bad": bad, "note": note, "asof": str(asof) if asof else None,
            "key": key, "raw": raw, "manual": manual}


# ------------------------------------------------------------------ data
class Data:
    def __init__(self, cfg):
        self.cfg = cfg
        self.errors: list[str] = []
        self.px = None   # Close DataFrame
        self.vol = None
        self.fred = {}

    def load_prices(self):
        import yfinance as yf
        u = self.cfg["universe"]
        tick = set(["SMH", "^SOX", "QQQ", "SPY", "RSP", "MTUM", "^VXN", "^VIX", "^TNX", "HYG", "IEF", "LITE", "SKHY"])
        tick |= set(u["sox"]) | set(u["memory"]) | set(u["funding"]) | set(u["earnings_extra"])
        tick |= {t for t, _ in u["asia_snapshot"]} | {t for t, _ in u["us_snapshot"]}
        tick |= {t for t, _ in u["premarket"]}
        tick |= set(self.cfg["brent_contracts"])
        self.hike_contract = hike_contract_ticker(self.cfg)
        if self.hike_contract:
            tick.add(self.hike_contract[0])
        tick = sorted(tick)
        last_err = None
        for attempt in range(3):
            try:
                d = yf.download(tick, period="420d", interval="1d", auto_adjust=False, progress=False,
                                threads=True, group_by="column")
                close, vol = d["Close"].copy(), d["Volume"].copy()
                if close["SMH"].dropna().empty:
                    raise RuntimeError("SMH 收盘价为空")
                missing = [t for t in tick if t not in close.columns or close[t].dropna().empty]
                for t in list(missing):  # 批量下载偶发单只失败，逐只补一次
                    try:
                        h = yf.Ticker(t).history(period="420d", interval="1d", auto_adjust=False)
                        if not h.empty:
                            h.index = h.index.tz_localize(None).normalize()
                            close[t] = h["Close"].reindex(close.index)
                            vol[t] = h["Volume"].reindex(close.index)
                            if not close[t].dropna().empty:
                                missing.remove(t)
                    except Exception:
                        pass
                self.px, self.vol = close, vol
                if missing:
                    self.errors.append("行情缺：" + ",".join(missing))
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"yfinance 批量行情失败：{last_err}")

    def s(self, t):
        if self.px is None or t not in self.px.columns:
            return None
        x = self.px[t].dropna()
        return x if len(x) else None

    def load_fred(self):
        start = (date.today() - timedelta(days=420)).isoformat()
        import pandas as pd
        import io
        for sid in ("BAMLH0A0HYM2", "DFII10", "DFF", "DGS10"):
            for attempt in range(2):
                try:
                    # FRED holds browser-like User-Agents (``Mozilla/5.0``) until they time
                    # out; a plain client string is answered at once.
                    b = http_get(
                        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}",
                        timeout=40,
                        headers={"User-Agent": "python-urllib/3"},
                    )
                    df = pd.read_csv(io.BytesIO(b))
                    df.columns = ["date", "v"]
                    df["v"] = pd.to_numeric(df["v"], errors="coerce")
                    df = df.dropna()
                    df["date"] = pd.to_datetime(df["date"])
                    self.fred[sid] = df.set_index("date")["v"]
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 1:
                        self.errors.append(f"FRED {sid} 失败：{e}")
                    time.sleep(3)


def hike_contract_ticker(cfg, today: date | None = None):
    today = today or datetime.now(ET).date()
    letters = "FGHJKMNQUVXZ"
    for d in cfg["fomc"]:
        m = date.fromisoformat(d)
        if m >= today:
            y, mo = (m.year + 1, 1) if m.month == 12 else (m.year, m.month + 1)
            return f"ZQ{letters[mo - 1]}{str(y)[2:]}.CBT", m
    return None


def front_brent(cfg, data: Data):
    today = datetime.now(ET).date()
    letters = "FGHJKMNQUVXZ"
    for t in cfg["brent_contracts"]:
        mo = letters.index(t[2]) + 1
        y = 2000 + int(t[3:5])
        expiry_guess = date(y, mo, 1) - timedelta(days=cfg.get("brent_roll_days_before_month", 35))
        if today <= expiry_guess:
            s = data.s(t)
            if s is not None:
                return t, s
    for t in cfg["brent_contracts"]:
        s = data.s(t)
        if s is not None:
            return t, s
    return None, None


# ------------------------------------------------------------------ external: earnings, TWSE, SEC
def earnings_cache(cfg, tickers, force=False):
    import yfinance as yf
    p = STATE_DIR / "earnings_cache.json"
    cache = load_json(p, {"fetched_at": {}, "events": {}})
    now = time.time()
    errs = []
    for t in tickers:
        ts = cache["fetched_at"].get(t, 0)
        if not force and now - ts < 12 * 3600:
            continue
        try:
            e = yf.Ticker(t).get_earnings_dates(limit=8)
            rows = []
            if e is not None:
                for idx, r in e.iterrows():
                    rep = r.get("Reported EPS")
                    rows.append({"ts": idx.isoformat(), "reported": None if rep is None or (isinstance(rep, float) and math.isnan(rep)) else float(rep)})
            cache["events"][t] = rows
            cache["fetched_at"][t] = now
        except Exception as ex:  # noqa: BLE001
            errs.append(f"{t}:{str(ex)[:60]}")
        time.sleep(0.25)
    write_json(p, cache)
    return cache, errs


def reaction_day(ts: datetime, trading_days):
    """盘前（<9:30 ET）公布 → 当天；盘后 → 下一交易日。返回在 trading_days 里的日期或 None。"""
    et = ts.astimezone(ET)
    d0 = et.date()
    same = et.hour < 9 or (et.hour == 9 and et.minute < 30)
    cands = [d for d in trading_days if (d >= d0 if same else d > d0)]
    return cands[0] if cands else None


def twse_fetch(days_needed=5, max_back=14):
    """返回 [(date, 外资买卖超股数 2330)]，和最近一天融资金额（亿新台币）。"""
    rows, margin = [], None
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    d = today
    tries = 0
    while len(rows) < days_needed and tries < max_back:
        tries += 1
        if d.weekday() < 5:
            ds = d.strftime("%Y%m%d")
            try:
                j = json.loads(http_get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ds}&selectType=ALLBUT0999&response=json", timeout=30))
                if j.get("stat") == "OK":
                    for r in j.get("data", []):
                        if r[0].strip() == "2330":
                            rows.append((d, int(r[4].replace(",", ""))))
                            break
                    if margin is None:
                        time.sleep(0.8)
                        m = json.loads(http_get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ds}&selectType=MS&response=json", timeout=30))
                        for tb in m.get("tables", []) or []:
                            for r in tb.get("data", []) or []:
                                if r and r[0].startswith("融資金額"):
                                    margin = (d, int(r[5].replace(",", "")) / 1e5)
            except Exception:
                pass
            time.sleep(0.8)
        d -= timedelta(days=1)
    return rows, margin


def sec_latest(cfg):
    out = []
    watch = cfg.get("sec_watch", [])
    if not watch:
        return out
    sec_ua = _resolve_sec_user_agent(cfg)
    if not sec_ua:
        raise RuntimeError(
            "未配置 DALTON_SEC_USER_AGENT（请设置环境变量或在 config/data_tasks.env 中配置）"
        )
    for w in watch:
        try:
            j = json.loads(http_get(f"https://data.sec.gov/submissions/CIK{w['cik']}.json",
                                    headers={"User-Agent": sec_ua}))
            r = j["filings"]["recent"]
            since = w.get("since", "1900-01-01")
            hits = [(f, d, a) for f, d, a in zip(r["form"], r["filingDate"], r["accessionNumber"])
                    if f in w["forms"] and d >= since]
            if hits:
                f, d, a = hits[0]
                out.append({"name": w["name"], "form": f, "date": d, "accession": a, "meaning": w["meaning"].get(f, f)})
            else:
                out.append({"name": w["name"], "form": None, "date": None, "meaning": w.get("pending", "尚未递交")})
        except Exception as e:  # noqa: BLE001
            out.append({"name": w["name"], "form": None, "date": None, "meaning": f"EDGAR 查询失败：{str(e)[:40]}"})
        time.sleep(0.3)
    return out


MAIL_DB = ROOT / "skills" / "market-digest" / "vectors.db"
CI_URL = "https://data-api.compute-index.com/public/indices"
CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"


def gpu_index_fetch(errors: list):
    """Compute Desk 公开 GPU 一年期租约指数（美元/卡时，无需 key）。失败时用缓存。返回 {alias: Series}, latest_date。"""
    import pandas as pd
    p = STATE_DIR / "gpu_index.json"
    j = None
    try:
        j = json.loads(http_get(CI_URL, timeout=40))
        if not j.get("indices"):
            raise RuntimeError("返回为空")
        write_json(p, {"fetched_at": time.time(), "data": j})
    except Exception as e:  # noqa: BLE001
        c = load_json(p, None)
        if c:
            j = c["data"]
            errors.append(f"GPU 租金指数取数失败，用 {datetime.fromtimestamp(c['fetched_at'], HKT):%m/%d %H:%M} 缓存：{str(e)[:50]}")
        else:
            raise
    out = {}
    for ix in j["indices"]:
        h = ix.get("history") or []
        if not h:
            continue
        s = pd.Series({pd.Timestamp(r["date"]): float(r["value"]) for r in h}).sort_index()
        out[ix["alias"]] = s
    return out, {ix["alias"]: ix.get("source") for ix in j["indices"]}


RAMP_URL = "https://ramp.com/data/ai-index"
# 本机外网代理；ramp.com 直连会被 fake-IP 拦。数据任务在容器里运行时，容器内的
# 127.0.0.1 是容器自己，要经 host.docker.internal 访问宿主机上的代理。
LOCAL_PROXY = os.environ.get("DALTON_LOCAL_PROXY") or (
    "http://host.docker.internal:7897" if os.path.exists("/.dockerenv") else "http://127.0.0.1:7897"
)


def _ramp_array(h: str, key: str):
    i = h.find('"' + key + '":')
    if i < 0:
        raise RuntimeError(f"Ramp 页面缺 {key}")
    j = h.find("[", i)
    depth = 0
    for k in range(j, len(h)):
        if h[k] == "[":
            depth += 1
        elif h[k] == "]":
            depth -= 1
            if depth == 0:
                return json.loads(h[j:k + 1])
    raise RuntimeError(f"Ramp {key} 解析失败")


def ramp_fetch(errors: list, max_age_h: float = 20):
    """Ramp AI Index 页面里嵌的月度序列（分厂商渗透率、企业人均 AI 支出）。月度数据，缓存 20 小时内不重抓。"""
    p = STATE_DIR / "ramp_ai_index.json"
    c = load_json(p, None)
    if c and time.time() - c.get("fetched_at", 0) < max_age_h * 3600:
        return c["data"]
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": LOCAL_PROXY, "https": LOCAL_PROXY}))
        with op.open(urllib.request.Request(RAMP_URL, headers={"User-Agent": UA}), timeout=60) as r:
            h = r.read().decode("utf-8", "ignore").replace('\\"', '"')
        d = {"vendor": _ramp_array(h, "adoptionVendor"), "spend": _ramp_array(h, "spendPerEmployee")}
        write_json(p, {"fetched_at": time.time(), "data": d})
        return d
    except Exception as e:  # noqa: BLE001
        if c:
            errors.append(f"Ramp AI Index 取数失败，用 {datetime.fromtimestamp(c['fetched_at'], HKT):%m/%d %H:%M} 缓存：{str(e)[:50]}")
            return c["data"]
        raise


# ------------------------------------------------------------------ news（推送前按追踪项检索新闻）
# 人工项和实验室 ARR 靠人维护，2026-11-06 漏了 WSJ 的 Anthropic ARR 和德州新州长发布会。
# 这里每次推送前按配置 news.topics 搜 Google News（只留一线媒体）和本地卖方邮件库，
# 新报道放进卡片；人工项上次更新早于新报道日期时在卡片里提醒复核。只读，不改任何评分。
GN_URL = "https://news.google.com/rss/search"
NEWS_SEEN = "news_seen.json"
_NEWS_STOP = set("the a an and or of to in on for with as at by from is are be its it this that after over into amid "
                 "says said say new will could may can than more up down how why what who".split())
_MAIL_WHO = {"gs.com": "高盛", "bofa.com": "美银", "jefferies.com": "Jefferies", "morganstanley.com": "大摩",
             "jpmorgan.com": "小摩", "jpmchase.com": "小摩", "citi.com": "花旗", "ubs.com": "UBS", "barclays.com": "巴克莱",
             "evercore.com": "Evercore", "bernstein.com": "伯恩斯坦", "wellsfargo.com": "富国", "mizuho": "瑞穗",
             "nomura.com": "野村", "macquarie.com": "麦格理", "cowen.com": "TD Cowen", "tdsecurities.com": "TD Cowen"}


def _src_key(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^www\.", "", s)
    return re.sub(r"\.(com|net|org|co\.uk)$", "", s).strip()


def _news_toks(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) > 2 and w not in _NEWS_STOP}


def _near_dup(a, b, th=0.6):
    A, B = _news_toks(a), _news_toks(b)
    return bool(A and B) and len(A & B) / min(len(A), len(B)) >= th


def _clean_title(t, src):
    t = re.sub(r"\s+", " ", t or "").strip()
    if src and t.endswith(" - " + src):
        t = t[: -len(src) - 3].rstrip()
    t = t.replace("[", "(").replace("]", ")").replace("*", "")
    return re.sub(r"(?<!US)\$(?=\d)", "US$", t)   # 防止成对 $ 被渲染成公式


def gn_parse(raw: bytes):
    """解析 Google News RSS。返回 [{title, link, ts(UTC), source}]。"""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    out = []
    for it in ET.fromstring(raw).findall("./channel/item"):
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:  # noqa: BLE001
            continue
        se = it.find("source")
        src = (se.text if se is not None else "") or ""
        out.append({"title": _clean_title(it.findtext("title"), src), "link": (it.findtext("link") or "").strip(),
                    "ts": ts, "source": src.strip()})
    return out


def _gn_fetch(q, days):
    u = GN_URL + "?" + urllib.parse.urlencode({"q": f"{q} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": LOCAL_PROXY, "https": LOCAL_PROXY}))
        with op.open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=20) as r:
            return gn_parse(r.read())
    except Exception:  # noqa: BLE001  代理不通时直连再试一次
        # 用新的 Request：代理尝试会把代理地址写进原来的 Request，复用它就还是连代理。
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with direct.open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=20) as r:
            return gn_parse(r.read())


def _mail_who(sender):
    s = (sender or "").strip()
    if "TMT Breakout" in s or "TMTB" in s:
        return "TMTB"
    m = re.search(r"@([\w.-]+)", s)
    if m:
        dom = m.group(1).lower()
        for k, v in _MAIL_WHO.items():
            if dom.endswith(k) or k in dom:
                return v
        parts = dom.split(".")
        return parts[-2].capitalize() if len(parts) >= 2 else dom
    return s.split("·")[0].strip()[:20] or "卖方"


def _mail_sentence_ok(s, terms, req):
    """句子要提到检索词本身、满足全部 require，且不是表格行或链接残片。"""
    if len(s) < 40 or not all(r.search(s) for r in req):
        return False
    if not any(t.lower() in s.lower() for t in terms):
        return False
    nums = re.findall(r"\d+(?:\.\d+)?%?", s)
    return len(nums) <= 6 and sum(len(n) for n in nums) / len(s) < 0.15


def _mail_hits(con, topic, since_date, skip, limit=1):
    """本地卖方邮件库（含 TMTB）里匹配本追踪项的句子，每封邮件最多一句。"""
    terms = topic.get("mail_terms") or []
    if not terms:
        return []
    req = [re.compile(r, re.I) for r in (topic.get("mail_require") or topic.get("require") or [])]
    where = " or ".join("content like ?" for _ in terms)
    rows = con.execute(f"select date, email_id, sender, subject, content from email_chunks where date >= ? and ({where}) "
                       "order by date desc, id desc limit 300", [since_date, *[f"%{t}%" for t in terms]]).fetchall()
    out, seen_e = [], set()
    for d, eid, sender, subj, c in rows:
        key = f"mail:{topic['id']}:{eid}"
        if eid in seen_e or key in skip:
            continue
        body = "\n".join(l for l in (c or "").splitlines()
                         if not re.match(r"^(From|Subject|Date|AlphaEngine doc_id):", l.strip()))
        body = re.sub(r"<https?://[^>]*>|https?://\S+", " ", body)
        body = re.sub(r"\s+", " ", body)
        for s in re.split(r"(?<=[.!?。])\s+|\s[*•]\s", body):
            s = s.strip(" *•-")
            if _mail_sentence_ok(s, terms, req):
                if len(s) > 170:   # 截取命中词附近
                    m = req[0].search(s) if req else None
                    st = max(0, (m.start() if m else 0) - 60)
                    s = ("…" if st else "") + s[st:st + 165].rstrip() + "…"
                out.append({"date": d, "who": _mail_who(sender), "subject": (subj or "")[:60], "text": _clean_title(s, ""),
                            "key": key})
                seen_e.add(eid)
                break
        if len(out) >= limit:
            break
    return out


def _news_link_state(cfg, link):
    """追踪项链接到的人工项：返回（名称, 上次更新日期 str）。"""
    kind, _, key = (link or "").partition(":")
    if kind == "manual":
        m = next((m for m in cfg.get("manual_items", []) if m["id"] == key), None)
        return (m["name"], m.get("updated")) if m else (None, None)
    if kind == "lab":
        la = (cfg.get("lab_arr") or {}).get(key)
        if la:
            upd = la.get("updated") or max(p["date"] for p in la["points"])
            return la["name"], upd
    return None, None


def _news_importance(nc, topic_id, title):
    """标题规则只决定展示先后，不判断报道真假，也不改投资评分。"""
    matches = [r for r in nc.get("importance_rules", [])
               if (not r.get("topics") or topic_id in r["topics"])
               and all(re.search(p, title, re.I) for p in r["require"])]
    level = max((r["level"] for r in matches), default=0)
    return level, [r["id"] for r in matches if r["level"] == level]


def _news_sort_key(it):
    return (-it.get("importance", 0), it.get("rank", 99), not it.get("hit", False),
            -it["ts"].timestamp(), it["title"], it.get("link", ""))


def _news_same_event(a, b):
    """比旧的宽松标题去重保守：数字或方向变化视为新进展。"""
    a, b = a.lower(), b.lower()
    def signature(s):
        return (tuple(re.findall(r"\d+(?:\.\d+)?", s)),
                bool(re.search(r"\b(?:cut\w*|fall\w*|fell|lower\w*|drop\w*|cancel\w*|delay\w*)\b", s)),
                bool(re.search(r"\b(?:rais\w*|ris\w*|rose|boost\w*|increas\w*)\b", s)),
                bool(re.search(r"\b(?:end\w*|clear\w*|resolv\w*|no evidence|no reason)\b", s)))
    return signature(a) == signature(b) and _near_dup(a, b, th=0.8)


def _news_delivery_state(it, seen, promote_min):
    matches = [v for k, v in seen.items() if k.startswith("gn:")
               and _news_same_event(it["title"], v.get("t", ""))]
    if not matches:
        return "new"
    # 旧记录没有 placement，保守按已展示处理，不把历史新闻批量重推。
    if any(v.get("placement", "main") == "main" for v in matches):
        return "skip"
    return "promote" if it.get("importance", 0) >= promote_min else "skip"


def news_fetch(cfg, now_utc, last_ts, errors: list):
    """按 news.topics 检索新闻。last_ts：上次成功推送的时间戳（秒），None 时用最短窗口。"""
    from concurrent.futures import ThreadPoolExecutor
    nc = cfg.get("news") or {}
    if not nc.get("enabled", True) or not nc.get("topics"):
        return None
    lo, hi = nc.get("lookback_min_h", 14), nc.get("lookback_max_h", 72)
    span_h = (now_utc.timestamp() - last_ts) / 3600 + 1 if last_ts else lo
    span_h = max(lo, min(hi, span_h))
    since = now_utc - timedelta(hours=span_h)
    days = max(1, math.ceil(span_h / 24))
    rank = {}
    for r, names in (nc.get("tiers") or {}).items():
        for n in names:
            rank[_src_key(n)] = int(r)
    seen = load_json(STATE_DIR / NEWS_SEEN, {})
    seen = {k: v for k, v in seen.items() if now_utc.timestamp() - v.get("ts", 0) < 7 * 86400}
    queries = sorted({q for t in nc["topics"] for q in t.get("queries", [])})
    results, failed = {}, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {q: ex.submit(_gn_fetch, q, days) for q in queries}
        for q, f in futs.items():
            try:
                results[q] = f.result(timeout=45)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{q}（{str(e)[:30]}）")
    if failed:
        errors.append(f"新闻检索 {len(failed)}/{len(queries)} 条查询失败" + ("，本期没有新闻" if len(failed) == len(queries) else ""))
    con = None
    if MAIL_DB.exists():
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{MAIL_DB}?mode=ro", uri=True, timeout=20)
        except Exception as e:  # noqa: BLE001
            errors.append(f"卖方邮件库打不开：{str(e)[:40]}")
    since_mail = since.astimezone(ET).date().isoformat()
    topics, coverage = [], []
    for t in nc["topics"]:
        req = [re.compile(r, re.I) for r in t.get("require", [])]
        boost = re.compile(t["boost"], re.I) if t.get("boost") else None
        query_scope = results if t.get("include_other_queries") else t.get("queries", [])
        raw = [it for q in query_scope for it in results.get(q, [])]
        # 重要报道若只进过折叠，仍可在最长窗口内晋升重点区，不依赖 RSS 再次返回。
        pending = []
        for v in seen.values():
            if v.get("placement") == "fold" and v.get("topic") == t["id"] and v.get("published"):
                try:
                    pending.append({"title": v["t"], "link": v["link"], "source": v["source"],
                                    "ts": datetime.fromisoformat(v["published"]), "carry": True})
                except (KeyError, ValueError):
                    continue
        cand = []
        for it in raw + pending:
            rk = rank.get(_src_key(it["source"]))
            cutoff = now_utc - timedelta(hours=hi) if it.get("carry") else since
            if rk is None or not cutoff <= it["ts"] <= now_utc or not all(r.search(it["title"]) for r in req):
                continue
            importance, reasons = _news_importance(nc, t["id"], it["title"])
            cand.append(dict(it, rank=rk, hit=bool(boost and boost.search(it["title"])),
                             importance=importance, reasons=reasons))
        cand.sort(key=_news_sort_key)
        uniq = []
        for it in cand:
            if any(_news_same_event(it["title"], u["title"]) for u in uniq):
                continue
            uniq.append(it)
        name, upd = _news_link_state(cfg, t.get("link"))
        stale = None
        if upd and uniq:
            newer = [u for u in uniq if u["ts"].astimezone(ET).date().isoformat() > str(upd)
                     and all(re.search(p, u["title"], re.I) for p in t.get("review_require", []))]
            if newer:
                stale = {"name": name, "updated": upd}
        fresh = []
        for u in uniq:
            delivery = _news_delivery_state(u, seen, nc.get("promote_min_importance", 2))
            if delivery != "skip":
                fresh.append(dict(u, fold_seen=delivery == "promote"))
        mail = []
        if con is not None:
            try:
                mail = _mail_hits(con, t, since_mail, seen, limit=t.get("mail_limit", 1))
            except Exception as e:  # noqa: BLE001
                errors.append(f"卖方邮件检索失败（{t['id']}）：{str(e)[:40]}")
        topics.append({"id": t["id"], "label": t["label"], "link_name": name, "items": fresh, "mail": mail,
                       "stale": stale, "n_window": len(uniq)})
        coverage.append({"id": t["id"], "queries": len(t["queries"]),
                         "succeeded": sum(q in results for q in t["queries"]), "eligible": len(uniq)})
    if con is not None:
        con.close()
    return {"since": since, "span_h": span_h, "topics": topics, "queries": len(queries), "failed": len(failed),
            "max_main": nc.get("max_main", 6), "max_fold_per_topic": nc.get("max_fold_per_topic", 2),
            "max_main_per_topic": nc.get("max_main_per_topic", 1), "coverage": coverage, "ranking_version": 2}


def _hkt_stamp_ts(stamp, now_hkt):
    """旧版 last.json 只有 "MM/DD HH:MM"（香港时间）；补上年份换成时间戳。"""
    if not stamp:
        return None
    try:
        d = datetime.strptime(f"{now_hkt.year}/{stamp}", "%Y/%m/%d %H:%M").replace(tzinfo=HKT)
        if d > now_hkt:
            d = d.replace(year=d.year - 1)
        return d.timestamp()
    except ValueError:
        return None


def news_pick(news):
    """全栏目按事件重要性、来源、主题匹配和时效排序；单栏上限避免占满。"""
    if not news:
        return [], []
    main, unique, counts = [], [], {}
    pairs = sorted(((t, it) for t in news["topics"] for it in t["items"]),
                   key=lambda pair: (*_news_sort_key(pair[1]), pair[0]["id"]))
    for t, it in pairs:
        if any(_news_same_event(it["title"], u["title"]) for _, u in unique):
            continue
        unique.append((t, it))
        if len(main) < news["max_main"] and counts.get(t["id"], 0) < news.get("max_main_per_topic", 1):
            main.append((t, it))
            counts[t["id"]] = counts.get(t["id"], 0) + 1
    used = {id(it) for _, it in main}
    fold = [(t, [it for topic, it in unique if topic["id"] == t["id"] and id(it) not in used
                 and not it.get("fold_seen")][:news["max_fold_per_topic"]]) for t in news["topics"]]
    return main, fold


def _news_line(t, it, link=True):
    when = it["ts"].astimezone(HKT).strftime("%m/%d %H:%M")
    name = re.sub(r"\.com$", "", it["source"])
    src = f"[{name}]({it['link']})" if link and it.get("link") else name
    return f"- **{t['label']}** {src} {when}：{it['title']}"


def news_markdown(news, main_body=True):
    L = []
    if not news:
        return L
    since_txt = news["since"].astimezone(HKT).strftime("%m/%d %H:%M")
    main, fold = news_pick(news)
    if main_body:
        L.append(f"### 新闻（{since_txt} 以来）")
        if main:
            L += [_news_line(t, it) for t, it in main]
        elif news["failed"] == news["queries"]:
            L.append("- 新闻检索失败，本期没有新闻")
        else:
            L.append("- 追踪项没有一线媒体的新报道")
        if news["failed"]:
            L.append(f"⚠️ 新闻查询失败 {news['failed']}/{news['queries']}，本期覆盖不完整")
        stale = [t["stale"] for t in news["topics"] if t.get("stale")]
        names = list(dict.fromkeys(f"{s['name']}（上次 {md(s['updated'])}）" for s in stale))
        if names:
            L.append("")
            L.append(f"⚠️ 新报道待复核（不代表评分已改变）：{'、'.join(names)}")
        L.append("")
        return L
    rows = []
    for t, items in fold:
        for it in items:
            rows.append(_news_line(t, it, link=news.get("fold_links", True)))
        for m_ in next((x["mail"] for x in news["topics"] if x["id"] == t["id"]), []):
            rows.append(f"- **{t['label']}** {m_['who']} {md(m_['date'])} 邮件：{m_['text']}")
    if rows:
        L.append("### 更多新闻和卖方邮件（折叠）")
        L.append("")
        L += rows
        L.append("")
    return L


def news_seen_update(news):
    """发送成功后记下已推送的报道和邮件，下一期不重复；只保留 7 天。"""
    if not news:
        return
    p = STATE_DIR / NEWS_SEEN
    seen = load_json(p, {})
    now = time.time()
    main, fold = news_pick(news)
    shown = [(t, it, "main") for t, it in main] + [(t, it, "fold") for t, its in fold for it in its]
    for t, it, placement in shown:
        key = f"gn:{it['link'] or it['title']}"
        if (key in seen and seen[key].get("placement", "main") == "main"
                and _news_same_event(it["title"], seen[key].get("t", ""))):
            placement = "main"
        seen[key] = {"t": it["title"], "ts": now, "placement": placement, "topic": t["id"],
                     "published": it["ts"].isoformat(), "source": it["source"], "link": it.get("link", ""),
                     "importance": it.get("importance", 0)}
    for t in news["topics"]:
        for m_ in t["mail"]:
            seen[m_["key"]] = {"t": m_["text"][:80], "ts": now}
    seen = {k: v for k, v in seen.items() if now - v.get("ts", 0) < 7 * 86400}
    write_json(p, seen)


def lab_arr_inds(cfg):
    """实验室 ARR：数据点在配置 lab_arr 里人工维护，状态由脚本按月增额定档。返回指标列表（排在反转条件最前）。"""
    rank = {"R": 0, "Y": 1, "G": 2}
    today = datetime.now(ET).date()
    out = []
    for key, lab in (cfg.get("lab_arr") or {}).items():
        if key.startswith("_"):
            continue
        pts = sorted(lab["points"], key=lambda p: p["date"])
        conf = [p for p in pts if not p.get("prelim")]
        pre = next((p for p in reversed(pts) if p.get("prelim")), None)
        if pre and conf and pre["date"] <= conf[-1]["date"]:
            pre = None
        d1, d0 = date.fromisoformat(conf[-1]["date"]), date.fromisoformat(conf[-2]["date"])
        gap_m = (d1.year - d0.year) * 12 + d1.month - d0.month
        if gap_m == 1 and d1.day >= 28 and d0.day >= 28:   # 相邻两个月末：直接相减
            add_c = conf[-1]["arr"] - conf[-2]["arr"]
        else:
            add_c = (conf[-1]["arr"] - conf[-2]["arr"]) * 30.4 / max((d1 - d0).days, 1)
        g, r = lab["green_add"], lab["red_add"]
        st_c = grade(add_c, g, r, True)
        st = st_c
        if pre and pre.get("add_est") is not None:
            st_p = grade(pre["add_est"], g, r, True)
            st = "Y" if rank[st_p] > rank[st_c] else st_p   # 初步数更好 → 待确认；更差 → 取更差
        capped = False
        if lab.get("cap") and rank[st] > rank[lab["cap"]]:
            st, capped = lab["cap"], True
        last_pt = pre or conf[-1]
        age = (today - date.fromisoformat(last_pt["date"])).days
        mon = f"{d1.month} 月"
        val = f"{mon}末 {conf[-1]['arr']:.0f} 亿（月增 {add_c:+.0f} 亿）"
        short = f"{mon} {add_c:+.0f} 亿"
        if pre:
            val += f"；{md(pre['date'])} 初步 {pre['arr']:.0f} 亿（推算月增约 {pre['add_est']:+.0f} 亿）"
            short += f"；{date.fromisoformat(pre['date']).month} 月初步约 {pre['add_est']:+.0f} 亿"
            if st == "Y" and rank[grade(pre['add_est'], g, r, True)] > rank[st_c] and not capped:
                short += "，" + lab.get("pending_txt", "待确认")
        if capped:
            short += "，" + lab.get("cap_short", "数据存疑")
        note_parts = [lab.get("basis", ""), "来源：" + "；".join(f"{md(p['date'])} {p['src']}" for p in pts[-3:])]
        if capped:
            note_parts.append("人工上限为黄：" + lab.get("cap_reason", ""))
        if age > lab.get("stale_days", 45):
            note_parts.append(f"⚠ 最新数据已 {age} 天未更新")
        note_parts.append("下一个数据点：" + lab.get("next", "—"))
        out.append(ind(f"arr_{key}", "基本面", lab["name"], st, val, lab["rescue_txt"], lab["bad_txt"],
                       "；".join(x for x in note_parts if x), last_pt["date"], key=True,
                       raw={"add_c": add_c, "pre": pre, "short": short, "cond": lab.get("cond", lab["name"]),
                            "next": lab.get("next", ""),
                            "series": [(p["date"], p["arr"], bool(p.get("prelim")), p.get("add_est")) for p in pts],
                            "green": g, "red": r, "age": age}))
    return out


def cftc_nq_fetch(errors: list, weeks: int = 170):
    """CFTC 金融期货交易者分类（TFF）：纳指 E-mini（合约代码 209742）。失败时用缓存。"""
    import pandas as pd
    p = STATE_DIR / "cftc_nq.json"
    rows = None
    try:
        q = urllib.parse.urlencode({"$where": "cftc_contract_market_code='209742'",
                                    "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": str(weeks)})
        rows = json.loads(http_get(f"{CFTC_URL}?{q}", timeout=40))
        if not rows:
            raise RuntimeError("返回为空")
        write_json(p, {"fetched_at": time.time(), "rows": rows})
    except Exception as e:  # noqa: BLE001
        c = load_json(p, None)
        if c:
            rows = c["rows"]
            errors.append(f"CFTC 取数失败，用缓存：{str(e)[:50]}")
        else:
            raise
    recs = []
    for r in rows:
        oi = float(r["open_interest_all"])
        recs.append({"date": pd.Timestamp(r["report_date_as_yyyy_mm_dd"][:10]),
                     "lev": (float(r["lev_money_positions_long"]) - float(r["lev_money_positions_short"])) / oi * 100,
                     "am": (float(r["asset_mgr_positions_long"]) - float(r["asset_mgr_positions_short"])) / oi * 100,
                     "lev_n": float(r["lev_money_positions_long"]) - float(r["lev_money_positions_short"])})
    return pd.DataFrame(recs).set_index("date").sort_index()


def gs_ai_readings(cfg, days: int = 35):
    """从本地卖方邮件库抽取高盛对 AI 仓位的定量表述（PB 净配置、分位、desk 长度打分、AI 多空比分位）。
    只读；邮件措辞变了就抽不到，届时用 config 里的 ai_positioning_readings 补。"""
    import re
    import sqlite3
    since = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    out = []
    if MAIL_DB.exists():
        con = sqlite3.connect(f"file:{MAIL_DB}?mode=ro", uri=True, timeout=20)
        try:
            rows = con.execute(
                "select date, sender, subject, content from email_chunks where date >= ? and content like '%AI%' "
                "and (content like '%percentile%' or content like '%/10%' or content like '%out 10%' or content like '%out of 10%')",
                (since,)).fetchall()
        finally:
            con.close()
        ORD = r"(\d{1,2})\s?(?:st|nd|rd|th)"
        pats = [
            ("alloc", re.compile(r"Net allocation to Global AI stocks.{0,120}?(?:stands at|is at|is|at) (\d{1,2}(?:\.\d)?)%.{0,160}?" + ORD +
                                 r"\s+percentile vs\.? the past year(?:.{0,30}?" + ORD + r"\s+percentile vs\.? the past (?:three|3) years)?", re.I)),
            ("exp1y", re.compile(r"AI[^.]{0,220}?exposure (?:down|up|back) to (?:the )?" + ORD + r"\s+percentile of the past year", re.I)),
            ("ls1y", re.compile(r"Global AI stocks[’']?\s+long/short ratio (?:in|at) the " + ORD + r"\s+percentile vs\.? the past year", re.I)),
            ("length", re.compile(r"AI (?:trade|theme)[^.]{0,90}?\b(\d{1,2})\s*(?:/|out(?: of)?\s+)10\b", re.I)),
        ]
        for d, sender, subj, c in rows:
            if "gs.com" not in (sender or "").lower() and "TMTB" not in (sender or "") and "Goldman" not in (c[:400]):
                continue
            c = re.sub(r"\s+", " ", c)
            src = "高盛" if "gs.com" in (sender or "").lower() else "TMTB 转述高盛"
            for kind, pat in pats:
                for m in pat.finditer(c):
                    g = m.groups()
                    rec = {"date": d, "kind": kind, "src": src}
                    if kind == "alloc":
                        rec.update(alloc=float(g[0]), p1y=int(g[1]), p3y=int(g[2]) if g[2] else None)
                    elif kind in ("exp1y", "ls1y"):
                        rec.update(p1y=int(g[0]))
                    else:
                        v = int(g[0])
                        if not 1 <= v <= 10:
                            continue
                        rec.update(length=v)
                    out.append(rec)
    for r in cfg.get("ai_positioning_readings", []):  # 人工补录（图片里或电话会上的读数）
        if r.get("date", "") >= since:
            out.append(dict(r, src=r.get("src", "人工补录")))
    latest = {}
    for r in sorted(out, key=lambda r: r["date"]):
        latest[r["kind"]] = r
    return latest


def ai_short_fetch(basket, errors: list):
    """AI 篮子空头股数（yfinance，来自 FINRA 半月数据）。12 小时缓存。"""
    import yfinance as yf
    p = STATE_DIR / "ai_short.json"
    c = load_json(p, {"fetched_at": 0, "rows": {}})
    if time.time() - c.get("fetched_at", 0) < 12 * 3600 and set(basket) <= set(c["rows"]):
        return c["rows"]
    rows = {}
    for t in basket:
        try:
            i = yf.Ticker(t).info
            if i.get("sharesShort") and i.get("sharesShortPriorMonth"):
                rows[t] = {"short": i["sharesShort"], "prior": i["sharesShortPriorMonth"],
                           "pct_float": i.get("shortPercentOfFloat"), "date": i.get("dateShortInterest"),
                           "prior_date": i.get("sharesShortPreviousMonthDate")}
        except Exception:
            pass
        time.sleep(0.2)
    if len(rows) >= max(5, len(basket) // 2):
        write_json(p, {"fetched_at": time.time(), "rows": rows})
        return rows
    if c["rows"]:
        errors.append("AI 篮子空头数据取数不足，用缓存")
        return c["rows"]
    return rows


# ------------------------------------------------------------------ indicators
def compute(cfg, data: Data, slot: str):
    import numpy as np
    import pandas as pd
    th, an, u = cfg["thresholds"], cfg["anchors"], cfg["universe"]
    out, extra = [], {}
    smh = data.s("SMH")
    tdays = [x.date() for x in smh.index]
    last_day = tdays[-1]
    extra["last_day"] = last_day

    def safe(fn):
        t0 = time.time()
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            data.errors.append(f"{fn.__name__}: {str(e)[:120]}")
            return None
        finally:
            log(f"{fn.__name__} {time.time()-t0:.1f}s")

    # ---------------- 价格
    def p_smh():
        c = smh.iloc[-1]
        ma200 = smh.rolling(200).mean().iloc[-1]
        floor = an["smh_floor"]
        st = "R" if c < floor else ("G" if c > ma200 else "Y")
        extra["smh"] = {"close": float(c), "ma200": float(ma200), "floor": floor, "asof": str(smh.index[-1].date()),
                        "to_floor": (c / floor - 1) * 100, "to_ma200": (c / ma200 - 1) * 100,
                        "chg1d": (c / smh.iloc[-2] - 1) * 100}
        return ind("smh_structure", "价格", "SMH 结构位", st,
                   f"{c:.1f}｜200日线 {ma200:.1f}｜{an['smh_floor_label']} {floor:.1f}",
                   "收盘站上 200 日线", f"收盘跌破 {floor:.1f}（{an['smh_floor_label']}）",
                   f"距底线 {fmt_pct((c/floor-1)*100)}，距 200 日线 {fmt_pct((c/ma200-1)*100)}", last_day, key=True)

    def p_sox():
        s = data.s("^SOX")
        ma200 = s.rolling(200).mean()
        ma50 = s.rolling(50).mean()
        n = th["sox_200dma_confirm_days"]
        above = (s.iloc[-n:] > ma200.iloc[-n:]).all()
        slope50 = (ma50.iloc[-1] / ma50.iloc[-11] - 1) * 100
        gap = (s.iloc[-1] / ma200.iloc[-1] - 1) * 100
        st = "G" if above else ("R" if (gap < 0 and slope50 < 0) else "Y")
        extra["sox"] = {"close": float(s.iloc[-1]), "ma200": float(ma200.iloc[-1]), "gap": gap, "asof": str(s.index[-1].date()),
                        "chg1d": (s.iloc[-1] / s.iloc[-2] - 1) * 100}
        return ind("sox_200dma", "价格", "SOX 对 200 日线", st, f"{s.iloc[-1]:,.0f}（{fmt_pct(gap)}）",
                   f"连续 {n} 天收在 200 日线上", "在 200 日线下且 50 日线走低",
                   f"200 日线 {ma200.iloc[-1]:,.0f}；50 日线 10 日 {fmt_pct(slope50)}", s.index[-1].date(), key=True)

    def p_rel():
        q = data.s("QQQ")
        r = (smh / q).dropna()
        ma50 = r.rolling(50).mean()
        low60 = r.iloc[-60:].min()
        chg10 = (r.iloc[-1] / r.iloc[-11] - 1) * 100
        near_low = r.iloc[-1] <= low60 * 1.005
        st = "R" if near_low else ("G" if (r.iloc[-1] > ma50.iloc[-1] and chg10 > 0) else "Y")
        extra["rel"] = r
        return ind("smh_vs_qqq", "价格", "SMH 相对 QQQ", st, f"{r.iloc[-1]:.3f}（10日 {fmt_pct(chg10)}）",
                   "站上 50 日均值且 10 日走强", "创 60 日新低",
                   f"60 日低点 {low60:.3f}，50 日均值 {ma50.iloc[-1]:.3f}", r.index[-1].date())

    def p_breadth():
        cols = [t for t in u["sox"] if data.s(t) is not None]
        px = data.px[cols].ffill()
        above = (px > px.rolling(50).mean()).sum(axis=1) / px.notna().sum(axis=1) * 100
        above = above.dropna()
        b = float(above.iloc[-1])
        extra["breadth"] = above
        st = grade(b, th["breadth_green"], th["breadth_red"], True)
        return ind("breadth", "价格", "SOX 成分股站上 50 日线", st, f"{b:.0f}%（{len(cols)} 只）",
                   f"≥ {th['breadth_green']}%", f"≤ {th['breadth_red']}%",
                   f"5 天前 {above.iloc[-6]:.0f}%", above.index[-1].date())

    def p_uplag():
        spy = data.s("SPY")
        r_s, r_m = spy.pct_change() * 100, smh.pct_change() * 100
        dfx = pd.concat([r_s, r_m], axis=1).dropna().iloc[-10:]
        lag = int(((dfx.iloc[:, 0] > 0) & (dfx.iloc[:, 1] < dfx.iloc[:, 0] - 1)).sum())
        ups = int((dfx.iloc[:, 0] > 0).sum())
        st = grade(lag, th["uplag_green"], th["uplag_red"], False)
        return ind("uplag", "价格", "大盘上涨日半导体跑输", st, f"{lag} 天 / 近 10 日（大盘涨 {ups} 天）",
                   f"≤ {th['uplag_green']} 天", f"≥ {th['uplag_red']} 天",
                   "跑输 = SPY 收涨、SMH 落后 SPY 超过 1 个点", dfx.index[-1].date())

    # ---------------- 基本面
    def f_reaction():
        tick = sorted(set(u["sox"]) | set(u["earnings_extra"]) | {"LITE"})
        cache, errs = earnings_cache(cfg, tick)
        if errs:
            data.errors.append("财报日期缺：" + ",".join(e.split(":")[0] for e in errs[:8]))
        extra["earn_cache"] = cache
        win = set(tdays[-th["reaction_window_days"]:])
        rows = []
        excl = set(u["reaction_exclude"])
        smh_r = smh.pct_change() * 100
        for t, evs in cache["events"].items():
            if t in excl:
                continue
            s = data.s(t)
            if s is None:
                continue
            sr = s.pct_change() * 100
            for e in evs:
                ts = datetime.fromisoformat(e["ts"])
                if e["reported"] is None and ts.astimezone(ET).date() > last_day:
                    continue
                rd = reaction_day(ts, tdays)
                if rd is None or rd not in win:
                    continue
                k = pd.Timestamp(rd)
                if k not in sr.index or k not in smh_r.index or math.isnan(sr.loc[k]):
                    continue
                rows.append({"ticker": t, "day": rd, "ret": float(sr.loc[k]), "ex": float(sr.loc[k] - smh_r.loc[k])})
        rows.sort(key=lambda r: r["ex"])
        extra["reactions"] = rows
        if len(rows) < th["reaction_min_events"]:
            med = float(np.median([r["ex"] for r in rows])) if rows else float("nan")
            return ind("earn_reaction", "基本面", "财报次日反应（相对 SMH）", "Y" if rows else "NA",
                       f"{len(rows)} 家，中位数 {fmt_pct(med)}" if rows else "近期无财报",
                       f"中位数 ≥ {th['reaction_green']:+.0f}%", f"中位数 ≤ {th['reaction_red']:+.0f}%",
                       f"样本少于 {th['reaction_min_events']} 家，按中性处理", last_day, key=True)
        med = float(np.median([r["ex"] for r in rows]))
        pos = sum(1 for r in rows if r["ex"] > 0)
        st = grade(med, th["reaction_green"], th["reaction_red"], True)
        worst, best = rows[0], rows[-1]
        return ind("earn_reaction", "基本面", "财报次日反应（相对 SMH）", st,
                   f"{len(rows)} 家，中位数 {fmt_pct(med)}，跑赢 {pos} 家",
                   f"中位数 ≥ {th['reaction_green']:+.0f}%", f"中位数 ≤ {th['reaction_red']:+.0f}%",
                   f"最好 {best['ticker']} {fmt_pct(best['ex'])}，最差 {worst['ticker']} {fmt_pct(worst['ex'])}（近 {th['reaction_window_days']} 个交易日）",
                   last_day, key=True, raw={"median": med})

    def f_eps():
        import yfinance as yf
        revs, rev90 = {}, {}
        for t in u["eps_basket"]:
            try:
                et = yf.Ticker(t).eps_trend
                row = et.loc["+1y"]
                if row["30daysAgo"] and not math.isnan(row["30daysAgo"]):
                    revs[t] = (row["current"] / row["30daysAgo"] - 1) * 100
                    rev90[t] = (row["current"] / row["90daysAgo"] - 1) * 100
            except Exception:
                pass
            time.sleep(0.2)
        if len(revs) < 6:
            return ind("eps_rev", "基本面", "下一财年 EPS 预测修正", "NA", "取数不足", f"30 日中位数 ≥ +{th['eps_rev_green']}%", "30 日中位数 < 0", "", None)
        med = float(np.median(list(revs.values())))
        med90 = float(np.median(list(rev90.values())))
        up = sum(1 for v in revs.values() if v > 0.2)
        dn = sum(1 for v in revs.values() if v < -0.2)
        st = "G" if med >= th["eps_rev_green"] else ("R" if med < th["eps_rev_red"] else "Y")
        lo = min(revs, key=revs.get)
        extra["eps"] = revs
        return ind("eps_rev", "基本面", "下一财年 EPS 预测修正", st,
                   f"30 日中位数 {fmt_pct(med)}（上调 {up} / 下调 {dn}）",
                   f"30 日中位数 ≥ +{th['eps_rev_green']:.0f}%", "30 日中位数转负",
                   f"90 日中位数 {fmt_pct(med90)}；下调最多 {lo} {fmt_pct(revs[lo])}；{len(revs)} 家大市值半导体", last_day)

    def f_memory():
        cols = [t for t in u["memory"] if data.s(t) is not None]
        rets = []
        for t in cols:
            s = data.s(t)
            if len(s) > 21:
                rets.append((s.iloc[-1] / s.iloc[-21] - 1) * 100)
        mem = float(np.mean(rets))
        smh20 = (smh.iloc[-1] / smh.iloc[-21] - 1) * 100
        rel = mem - smh20
        st = grade(rel, th["memory_rel_green"], th["memory_rel_red"], True)
        return ind("memory_rel", "基本面", "存储股 20 日相对 SMH", st, f"{fmt_pct(rel)}（存储 {fmt_pct(mem)}）",
                   f"≥ {th['memory_rel_green']:+.0f} 个点", f"≤ {th['memory_rel_red']:+.0f} 个点",
                   "篮子：" + "、".join(c.replace("000660.KS", "海力士") for c in cols) + "；市场对存储周期的定价", last_day)

    # ---------------- 基本面：GPU 租金
    gpu_state = {}

    def _gpu():
        if "s" not in gpu_state:
            gpu_state["s"], gpu_state["src"] = gpu_index_fetch(data.errors)
            extra["gpu"] = gpu_state["s"]
        return gpu_state["s"]

    def _chg(s, n):
        return (float(s.iloc[-1]) / float(s.iloc[-1 - n]) - 1) * 100 if len(s) > n else float("nan")

    def f_gpu_bw():
        g = _gpu()
        need = [a for a in ("CDB300", "CDB200") if a in g]
        if not need:
            raise RuntimeError("缺 B200/B300 序列")
        asof = max(g[a].index[-1] for a in need).date()
        c20 = {a: _chg(g[a], 20) for a in need}
        c5 = {a: _chg(g[a], 5) for a in need}
        avg = float(np.nanmean(list(c20.values())))
        st = grade(avg, th["gpu_bw_green"], th["gpu_bw_red"], True)
        if (datetime.now(ET).date() - asof).days > th["gpu_stale_days"]:
            st = "NA"
        lab = {"CDB300": "B300", "CDB200": "B200"}
        val = "｜".join(f"{lab[a]} {g[a].iloc[-1]:.2f}（20日 {fmt_pct(c20[a])}）" for a in need)
        hi = {a: float(g[a].max()) for a in need}
        note = ("美元/卡时，美国一年期租约；5 日 " + "，".join(f"{lab[a]} {fmt_pct(c5[a])}" for a in need) +
                "；一年高点 " + "，".join(f"{lab[a]} {hi[a]:.2f}" for a in need) + "；Compute Desk 指数")
        return ind("gpu_rent_bw", "基本面", "GPU 租金：Blackwell", st, val,
                   f"B200/B300 20 日均涨 ≥ {th['gpu_bw_green']:+.0f}%", f"20 日均跌 ≤ {th['gpu_bw_red']:+.0f}%",
                   note, asof, raw={"avg20": avg})

    def f_gpu_hopper():
        g = _gpu()
        need = [a for a in ("CDGXH100", "CDH200") if a in g]
        if not need:
            raise RuntimeError("缺 H100/H200 序列")
        asof = max(g[a].index[-1] for a in need).date()
        c20 = {a: _chg(g[a], 20) for a in need}
        c60 = {a: _chg(g[a], 60) for a in need}
        avg = float(np.nanmean(list(c20.values())))
        st = grade(avg, th["gpu_hopper_green"], th["gpu_hopper_red"], True)
        if (datetime.now(ET).date() - asof).days > th["gpu_stale_days"]:
            st = "NA"
        lab = {"CDGXH100": "H100", "CDH200": "H200"}
        val = "｜".join(f"{lab[a]} {g[a].iloc[-1]:.2f}（20日 {fmt_pct(c20[a])}）" for a in need)
        note = ("美元/卡时；60 日 " + "，".join(f"{lab[a]} {fmt_pct(c60[a])}" for a in need) +
                "；H100 用 GX 官方值，H200 为 Compute Index 计算值；Hopper 租金关系到旧卡残值和 GPU 抵押贷")
        return ind("gpu_rent_hopper", "基本面", "GPU 租金：Hopper", st, val,
                   f"H100/H200 20 日均涨 ≥ {th['gpu_hopper_green']:+.0f}%", f"20 日均跌 ≤ {th['gpu_hopper_red']:+.0f}%",
                   note, asof, raw={"avg20": avg})

    # ---------------- 融资
    def f_ramp():
        d = ramp_fetch(data.errors)
        sp = sorted([r for r in d["spend"] if isinstance(r.get("p99_winsorized_weighted_pepm"), (int, float))],
                    key=lambda r: r["date_month"])
        v = [float(r["p99_winsorized_weighted_pepm"]) for r in sp]
        if len(v) < 5:
            raise RuntimeError("Ramp 序列太短")
        base3 = float(np.mean(v[-4:-1]))
        vs3 = (v[-1] / base3 - 1) * 100
        m1 = (v[-1] / v[-2] - 1) * 100
        mon = date.fromisoformat(sp[-1]["date_month"])
        st = grade(vs3, th["ramp_green"], th["ramp_red"], True)
        if (datetime.now(ET).date() - mon).days > th["ramp_stale_days"]:
            st = "NA"
        ven = {r["vendor"]: r for r in d["vendor"] if r["date_month"] == sp[-1]["date_month"]}
        a, o = ven.get("Anthropic"), ven.get("OpenAI")
        adopt = ""
        if a and o:
            adopt = (f"企业渗透率 Anthropic {a['adoption_rate_pct']:.1f}%（月 {a['mom_change_pp']:+.2f}pp），"
                     f"OpenAI {o['adoption_rate_pct']:.1f}%（月 {o['mom_change_pp']:+.2f}pp）；")
        extra["ramp"] = {"months": [r["date_month"][:7] for r in sp], "v": v}
        return ind("ramp_spend", "基本面", "企业 AI 支出（Ramp 月度）", st,
                   f"{mon.month} 月加权人均 {v[-1]:.1f} 美元/月，比前三月均值 {fmt_pct(vs3)}（环比 {fmt_pct(m1)}）",
                   f"比前三月均值 ≥ +{th['ramp_green']:.0f}%", f"比前三月均值 ≤ {th['ramp_red']:+.0f}%",
                   adopt + "Ramp 企业卡样本偏初创和科技公司，按付款月记账，比用量晚 2–6 周；用作实验室收入的月度代理",
                   mon, raw={"vs3": vs3, "m1": m1})

    def c_hy():
        s = data.fred.get("BAMLH0A0HYM2")
        v = float(s.iloc[-1])
        st = grade(v, th["hy_green"], th["hy_red"], False)
        chg = v - float(s.iloc[-6]) if len(s) > 6 else float("nan")
        extra["hy"] = s
        return ind("hy_oas", "融资", "高收益债利差", st, f"{v:.2f}%（5 日 {chg*100:+.0f}bp）",
                   f"< {th['hy_green']:.2f}%", f"> {th['hy_red']:.2f}%",
                   "FRED 数据晚一个交易日", s.index[-1].date(), key=True, raw={"v": v})

    def c_hyg():
        r = (data.s("HYG") / data.s("IEF")).dropna()
        chg = (r.iloc[-1] / r.iloc[-21] - 1) * 100
        st = grade(chg, th["hyg_ief_green"], th["hyg_ief_red"], True)
        return ind("hyg_ief", "融资", "HYG/IEF 20 日变化", st, fmt_pct(chg, 2),
                   f"≥ {th['hyg_ief_green']:+.1f}%", f"≤ {th['hyg_ief_red']:+.1f}%",
                   "高收益债相对国债，比 FRED 利差早一天", r.index[-1].date())

    def c_funding():
        rets, parts = [], []
        for t in u["funding"]:
            s = data.s(t)
            if s is not None and len(s) > 11:
                r = (s.iloc[-1] / s.iloc[-11] - 1) * 100
                rets.append(r)
                parts.append(f"{t} {fmt_pct(r, 0)}")
        v = float(np.mean(rets))
        st = grade(v, th["funding_green"], th["funding_red"], True)
        return ind("funding_basket", "融资", "AI 融资链篮子 10 日", st, fmt_pct(v),
                   f"≥ {th['funding_green']:+.0f}%", f"≤ {th['funding_red']:+.0f}%", "；".join(parts), last_day)

    # ---------------- 宏观
    def m_10y():
        s = data.s("^TNX")
        v = float(s.iloc[-1])
        st = grade(v, th["us10y_green"], th["us10y_red"], False)
        return ind("us10y", "宏观", "10 年期美债收益率", st, f"{v:.2f}%（5 日 {(v-float(s.iloc[-6]))*100:+.0f}bp）",
                   f"< {th['us10y_green']:.2f}%", f"> {th['us10y_red']:.2f}%", "10/5 高点 5.31%", s.index[-1].date())

    def m_real():
        s = data.fred.get("DFII10")
        v = float(s.iloc[-1])
        st = grade(v, th["real10y_green"], th["real10y_red"], False)
        extra["real"] = s
        return ind("real10y", "宏观", "10 年期实际利率", st, f"{v:.2f}%",
                   f"< {th['real10y_green']:.2f}%", f"> {th['real10y_red']:.2f}%", "TIPS，FRED 晚一个交易日", s.index[-1].date())

    def m_hike():
        tk, meet = data.hike_contract
        z = data.s(tk)
        dff = data.fred.get("DFF")
        cur = float(dff.iloc[-1])
        prob = (100 - float(z.iloc[-1]) - cur) / 0.25 * 100
        extra["hike_series"] = (100 - z, dff)
        extra["hike_prob_now"] = prob
        extra["hike_meet"] = meet
        st = grade(prob, th["hike_green"], th["hike_red"], False)
        word = "加息" if prob >= 0 else "降息"
        return ind("hike_prob", "宏观", f"{md(meet)} FOMC {word}概率", st, f"约 {abs(prob):.0f}%",
                   f"加息概率 < {th['hike_green']}%", f"> {th['hike_red']}%",
                   f"由 {tk.replace('.CBT','')} 联邦基金期货隐含利率减实际联邦基金利率 {cur:.2f}% 推算", z.index[-1].date(),
                   key=True, raw={"prob": prob})

    def m_brent():
        tk, s = front_brent(cfg, data)
        v = float(s.iloc[-1])
        st = grade(v, th["brent_green"], th["brent_red"], False)
        return ind("brent", "宏观", "Brent 原油", st, f"{v:.1f} 美元（{fmt_pct((v/float(s.iloc[-2])-1)*100)}）",
                   f"< {th['brent_green']}", f"> {th['brent_red']}",
                   f"用具体合约 {tk.replace('.NYM','')}，避开连续合约换月", s.index[-1].date())

    # ---------------- 仓位
    def s_vxn():
        vxn, vix = data.s("^VXN"), data.s("^VIX")
        sp = (vxn - vix).dropna()
        v = float(sp.iloc[-1])
        st = grade(v, th["vxn_vix_green"], th["vxn_vix_red"], False)
        extra["vxnvix"] = sp
        return ind("vxn_vix", "仓位", "VXN - VIX", st, f"{v:.1f}（VXN {vxn.iloc[-1]:.1f}）",
                   f"< {th['vxn_vix_green']:.0f}", f"> {th['vxn_vix_red']:.0f}", "差值越大，压力越集中在科技股", sp.index[-1].date())

    def s_mom():
        r = (data.s("MTUM") / data.s("RSP")).dropna()
        chg = (r.iloc[-1] / r.iloc[-6] - 1) * 100
        st = grade(chg, th["mom_green"], th["mom_red"], True)
        extra["mom"] = r
        return ind("momentum", "仓位", "动量因子代理 5 日", st, fmt_pct(chg),
                   f"≥ {th['mom_green']:+.0f}%", f"≤ {th['mom_red']:+.0f}%", "MTUM 相对等权标普 RSP；动量平仓时明显走弱", r.index[-1].date())

    def s_pb():
        w = pd.read_csv(PB_WEEKLY)
        w["week_ending"] = pd.to_datetime(w["week_ending"])
        cur, prv = w.iloc[-1], w.iloc[-2]
        wow = float(cur["net"] - prv["net"])
        gwow = float(cur["gross"] - prv["gross"])
        asof = cur["week_ending"].date()
        stale = (datetime.now(ET).date() - asof).days > th["pb_stale_days"]
        if wow >= th["pb_net_wow_green"]:
            st = "G"
        elif wow <= th["pb_net_wow_red"] and gwow > 0:
            st = "R"
        else:
            st = "Y"
        if stale:
            st = "NA"
        extra["pb"] = w
        p5 = "" if pd.isna(cur.get("net_pctile_5y")) else f"，5 年 {int(cur['net_pctile_5y'])} 分位"
        g5 = "" if pd.isna(cur.get("gross_pctile_5y")) else f"，5 年 {int(cur['gross_pctile_5y'])} 分位"
        return ind("gs_pb", "仓位", "高盛 PB 净杠杆（周）", st,
                   f"净 {cur['net']:.1f}%（周 {wow:+.1f}）｜总 {cur['gross']:.1f}%（周 {gwow:+.1f}）",
                   "净杠杆周增 ≥ 1 个点（加多或回补）", "净杠杆周降且总杠杆上升（加空）",
                   f"净杠杆{p5}；总杠杆{g5}" + ("；数据过期" if stale else ""), asof, key=True, raw={"wow": wow})

    def s_tw():
        rows, margin = twse_fetch()
        if not rows:
            return ind("tw_foreign", "仓位", "外资买卖台积电（5 日）", "NA", "证交所取数失败", "", "", "", None)
        tsm = data.s("2330.TW")
        price = float(tsm.iloc[-1]) if tsm is not None else float("nan")
        buys = sum(1 for _, v in rows if v > 0)
        tot = sum(v for _, v in rows)
        val = tot * price / 1e8
        st = "G" if buys >= th["tw_buy_days_green"] else ("R" if buys == 0 else "Y")
        extra["tw"] = {"rows": [(str(d), v) for d, v in rows], "margin": margin}
        mtxt = ""
        if margin:
            mtxt = f"；台股融资 {margin[1]:,.0f} 亿新台币（{md(margin[0])}，{an['tw_margin_record_label']} {an['tw_margin_record_e8']:,}）"
        latest = rows[0]
        return ind("tw_foreign", "仓位", "外资买卖台积电（5 日）", st,
                   f"净买 {buys}/{len(rows)} 天，合计 {tot/1e7:+.2f} 万张，约 {val:+,.0f} 亿新台币",
                   f"5 天里净买 ≥ {th['tw_buy_days_green']} 天", "5 天全部净卖",
                   f"最近 {md(latest[0])} {latest[1]/1e7:+.2f} 万张{mtxt}", latest[0])

    def s_spread():
        m, l = data.s("MRVL"), data.s("LITE")
        ratio = an["mrvl_lite_ratio"]
        sp = ((m * ratio - l) / l * 100).dropna()
        sp = sp[sp.index >= pd.Timestamp(an["mrvl_lite_announce"])]
        v = float(sp.iloc[-1])
        prev = float(sp.iloc[-2]) if len(sp) > 1 else v
        vol = data.vol["MRVL"].dropna()
        base = vol[vol.index <= pd.Timestamp(an["mrvl_vol_baseline_end"])].iloc[-20:].mean()
        vr = float(vol.iloc[-1] / base)
        if v < th["spread_green"]:
            st = "G"
        elif v > th["spread_red"] and v >= prev:
            st = "R"
        else:
            st = "Y"
        extra["spread"] = sp
        return ind("mrvl_lite", "仓位", "MRVL/LITE 换股价差", st, f"{v:.1f}%（前一日 {prev:.1f}%）",
                   f"< {th['spread_green']:.0f}%，MRVL 成交回到常态", f"> {th['spread_red']:.0f}% 且继续扩大",
                   f"按 1 LITE = {ratio} MRVL；MRVL 成交是消息前均量的 {vr:.1f} 倍", sp.index[-1].date())

    # ---------------- 仓位：AI 相关
    def s_ai_pb():
        lt = gs_ai_readings(cfg, th["ai_pb_lookback_days"])
        extra["ai_readings"] = lt
        pct = [r for r in (lt.get("alloc"), lt.get("exp1y")) if r and r.get("p1y") is not None]
        if not pct and not lt.get("length"):
            return ind("ai_pb", "仓位", "高盛 PB：AI 股敞口", "NA", f"近 {th['ai_pb_lookback_days']} 天邮件里没有读数",
                       f"1 年分位 ≤ {th['ai_pct_green']}（出清）", f"1 年分位 ≥ {th['ai_pct_red']}（重新拥挤）",
                       "来源是高盛销售邮件里的定量表述；没有新读数时可在 config 的 ai_positioning_readings 补录", None)
        main = max(pct, key=lambda r: r["date"]) if pct else None
        ln = lt.get("length")
        parts = []
        if main:
            parts.append(f"1 年 {main['p1y']} 分位（{md(main['date'])}）")
        if ln:
            parts.append(f"desk 长度 {ln['length']}/10（{md(ln['date'])}）")
        notes = []
        a = lt.get("alloc")
        if a:
            notes.append(f"全球 AI 股占单股净敞口 {a['alloc']:.1f}%，1 年 {a['p1y']}" + (f" / 3 年 {a['p3y']}" if a.get("p3y") else "") + f" 分位（{md(a['date'])}）")
        ls = lt.get("ls1y")
        if ls:
            notes.append(f"AI 股多空比 1 年 {ls['p1y']} 分位（{md(ls['date'])}，{ls['src']}）")
        if main:
            st = grade(main["p1y"], th["ai_pct_green"], th["ai_pct_red"], False)
        else:
            st = grade(ln["length"], th["ai_len_green"], th["ai_len_red"], False)
        newest = max([r["date"] for r in lt.values()])
        if (datetime.now(ET).date() - date.fromisoformat(newest)).days > th["ai_pb_stale_days"]:
            notes.append("读数已超过 3 周")
            st = "Y" if st != "NA" else st
        return ind("ai_pb", "仓位", "高盛 PB：AI 股敞口", st, "｜".join(parts),
                   f"1 年分位 ≤ {th['ai_pct_green']}（出清，有反弹燃料）", f"1 年分位 ≥ {th['ai_pct_red']}（重新拥挤）",
                   "；".join(notes + ["高盛销售邮件自动抽取，低频"]), newest)

    def s_tmt_flow():
        d = pd.read_csv(PB_DAILY)
        s = d[(d["chart"].str.startswith("Prime Book US Net Trading Flow")) & (d["series"] == "TMT")].copy()
        s["date"] = pd.to_datetime(s["date"])
        s = s.drop_duplicates("date", keep="last").set_index("date")["value"].sort_index()
        if len(s) < 25:
            raise RuntimeError("TMT 资金流序列太短")
        w1 = float(s.iloc[-1] - s.iloc[-6])
        w4 = float(s.iloc[-1] - s.iloc[-21])
        asof = s.index[-1].date()
        if w1 >= th["tmt_flow_green"]:
            st = "G"
        elif w1 <= th["tmt_flow_red"] and w4 < 0:
            st = "R"
        else:
            st = "Y"
        if (datetime.now(ET).date() - asof).days > th["pb_stale_days"] + 3:
            st = "NA"
        extra["tmt_flow"] = s
        note = "单位：占年初美股总敞口的百分点，正值=净买入（含空头回补）；随周六 PB 周报更新"
        it = d[d["chart"].str.contains("US Info Tech", na=False) & (d["series"] == "series2")].copy()
        if len(it) > 20:
            it["date"] = pd.to_datetime(it["date"])
            it = it.drop_duplicates("date", keep="last").set_index("date")["value"].sort_index()
            if (pd.Timestamp(asof) - it.index[-1]).days <= 7:
                note = (f"本期附信息技术板块：年初至今净卖出相当于板块年初总敞口的 {abs(it.iloc[-1]):.1f}%，"
                        f"近 1 周 {it.iloc[-1]-it.iloc[-6]:+.1f} 个点；") + note
        return ind("pb_tmt_flow", "仓位", "高盛 PB：TMT 净买卖（周）", st,
                   f"近 1 周 {w1:+.2f}｜近 4 周 {w4:+.2f}｜年初至今 {s.iloc[-1]:+.2f}",
                   f"近 1 周 ≥ {th['tmt_flow_green']:+.1f}（净买入）", f"近 1 周 ≤ {th['tmt_flow_red']:+.1f} 且 4 周净卖",
                   note, asof)

    def s_cftc():
        df = cftc_nq_fetch(data.errors)
        extra["cftc"] = df
        lev = df["lev"]
        win = lev[lev.index >= lev.index[-1] - pd.Timedelta(days=3 * 365)]
        pct = float((win < lev.iloc[-1]).mean() * 100)
        am = df["am"]
        awin = am[am.index >= am.index[-1] - pd.Timedelta(days=3 * 365)]
        apct = float((awin < am.iloc[-1]).mean() * 100)
        wchg = float(lev.iloc[-1] - lev.iloc[-2])
        st = grade(pct, th["cftc_pct_green"], th["cftc_pct_red"], False)
        asof = lev.index[-1].date()
        if (datetime.now(ET).date() - asof).days > 12:
            st = "NA"
        return ind("cftc_nq", "仓位", "纳指期货：杠杆基金净头寸（周）", st,
                   f"净 {lev.iloc[-1]:+.1f}% 持仓量，3 年 {pct:.0f} 分位（周 {wchg:+.1f}）",
                   f"3 年分位 ≤ {th['cftc_pct_green']}（对冲空单重，回补有燃料）", f"3 年分位 ≥ {th['cftc_pct_red']}（空单已回补，期货端燃料少）",
                   f"资产管理人净多 {am.iloc[-1]:.1f}%，3 年 {apct:.0f} 分位；对冲基金减单股多头时常同步回补期货对冲，所以这项升高不代表看多；"
                   "CFTC 每周五公布、截至周二；口径是整个纳指",
                   asof, raw={"pct": pct})

    def s_ai_short():
        bk = cfg["universe"]["ai_short_basket"]
        rows = ai_short_fetch(bk, data.errors)
        if len(rows) < 5:
            raise RuntimeError("AI 篮子空头数据不足")
        chg = {t: (r["short"] / r["prior"] - 1) * 100 for t, r in rows.items()}
        med = float(np.median(list(chg.values())))
        pf = [r["pct_float"] * 100 for r in rows.values() if r.get("pct_float")]
        up = sum(1 for v in chg.values() if v > 2)
        dn = sum(1 for v in chg.values() if v < -2)
        d1 = max(r["date"] for r in rows.values() if r.get("date"))
        d0 = max(r["prior_date"] for r in rows.values() if r.get("prior_date"))
        asof = datetime.fromtimestamp(d1, ET).date()
        st = grade(med, th["ai_short_green"], th["ai_short_red"], False)
        if (datetime.now(ET).date() - asof).days > 35:
            st = "NA"
        top = max(chg, key=chg.get)
        bot = min(chg, key=chg.get)
        return ind("ai_short", "仓位", "AI 篮子空头股数（半月）", st,
                   f"中位变化 {fmt_pct(med, 0)}｜加空 {up} 只，回补 {dn} 只",
                   f"中位变化 ≤ {th['ai_short_green']:+.0f}%（回补）", f"中位变化 ≥ {th['ai_short_red']:+.0f}%（加空）",
                   f"空头占流通股中位 {np.median(pf):.1f}%；{md(datetime.fromtimestamp(d1, ET).date())} 对比 {md(datetime.fromtimestamp(d0, ET).date())}；"
                   f"加空最多 {top} {fmt_pct(chg[top], 0)}，回补最多 {bot} {fmt_pct(chg[bot], 0)}；{len(rows)} 只",
                   asof)

    try:
        out.extend(lab_arr_inds(cfg))
    except Exception as e:  # noqa: BLE001
        data.errors.append(f"实验室 ARR 配置解析失败：{str(e)[:60]}")
    for fn in (p_smh, p_sox, p_rel, p_breadth, p_uplag, f_reaction, f_eps, f_memory, f_gpu_bw, f_gpu_hopper, f_ramp,
               c_hy, c_hyg, c_funding, m_10y, m_real, m_hike, m_brent,
               s_vxn, s_mom, s_pb, s_tw, s_spread, s_ai_pb, s_tmt_flow, s_cftc, s_ai_short):
        r = safe(fn)
        if r is None:
            r = ind(fn.__name__, {"p": "价格", "f": "基本面", "c": "融资", "m": "宏观", "s": "仓位"}[fn.__name__[0]],
                    fn.__name__, "NA", "取数失败", "", "", "", None)
        out.append(r)

    for m in cfg.get("manual_items", []):
        out.append(ind(m["id"], m["pillar"], m["name"], m["status"], m["value"], m["rescue"], m["bad"],
                       m.get("note", "") + f"（{md(m['updated'])}）", m["updated"], manual=True))
    return out, extra


# ------------------------------------------------------------------ verdict / changes / calendar
ROOT_IDS = ("arr_anthropic", "arr_openai")


def conditions(inds, extra):
    """反转条件：先列根因（两家实验室 ARR），再列市场确认（价格、信用、财报反应）。
    返回 [(名称, 是否满足, 说明)]，前 n_root 条是根因，n_root 记在 extra["n_root"]。"""
    by = {i["id"]: i for i in inds}
    c = []
    for rid in ROOT_IDS:
        i = by.get(rid)
        if i:
            raw = i.get("raw") or {}
            c.append((raw.get("cond", i["name"]), None if i["status"] == "NA" else i["status"] == "G", raw.get("short", i["value"])))
    extra["n_root"] = len(c)
    sox = extra.get("sox", {})
    sox_status = by.get("sox_200dma", {}).get("status")
    c.append(("SOX 站上 200 日线", None if sox_status in (None, "NA") else sox_status == "G",
              ((f"还差 {abs(sox['gap']):.1f}%" if sox["gap"] < 0 else f"高出 {sox['gap']:.1f}%") if sox and sox.get("gap") is not None else "缺数据")))
    hy = (by.get("hy_oas", {}).get("raw") or {}).get("v")
    c.append(("高收益利差 < 3.2%", hy < 3.2 if hy is not None else None, f"现 {hy:.2f}%" if hy is not None else "缺数据"))
    med = (by.get("earn_reaction", {}).get("raw") or {}).get("median")
    c.append(("财报次日反应中位数转正", med >= 0 if med is not None else None, f"现 {fmt_pct(med)}" if med is not None else "样本不足"))
    return c


def verdict(inds, conds, n_root=None, extra=None):
    auto = [i for i in inds if not i["manual"] and i["status"] != "NA"]
    g = sum(i["status"] == "G" for i in auto)
    r = sum(i["status"] == "R" for i in auto)
    y = sum(i["status"] == "Y" for i in auto)
    met = sum(1 for _, ok, _ in conds if ok)
    n_root = n_root if n_root is not None else 0
    root_ok = n_root > 0 and all(ok for _, ok, _ in conds[:n_root])
    conf_met = sum(1 for _, ok, _ in conds[n_root:] if ok)
    score = g - r
    if root_ok and conf_met >= 2:
        v, tpl, emo = "救场信号成形", "green", "🟢"
    elif root_ok:
        v, tpl, emo = "实验室收入回暖，等市场确认", "turquoise", "🟢"
    elif score >= 3:
        v, tpl, emo = "积极信号较多，反转待确认", "turquoise", "🟢"
    elif score <= -3:
        v, tpl, emo = "环境仍承压", "red", "🔴"
    else:
        v, tpl, emo = "僵持，等催化剂", "orange", "🟠"
    state_text = v
    moves = {k: (extra or {}).get(k, {}).get("chg1d") for k in ("smh", "sox")}
    short_term = ""
    last_day = str((extra or {}).get("last_day"))
    same_day = all(str((extra or {}).get(k, {}).get("asof", last_day)) == last_day for k in moves)
    if same_day and all(isinstance(x, (int, float)) and math.isfinite(x) for x in moves.values()):
        if all(x > 0 for x in moves.values()):
            short_term = "短线反弹" if score < 0 else "短线上涨"
        elif all(x < 0 for x in moves.values()):
            short_term = "短线回落"
        else:
            short_term = "短线走势分化"
    if score <= -3 and not root_ok and short_term:
        v = f"{short_term}，{state_text}"
    return {"text": v, "state_text": state_text, "short_term": short_term, "market_moves": moves,
            "template": tpl, "emoji": emo, "g": g, "y": y, "r": r,
            "na": sum(1 for i in inds if not i["manual"] and i["status"] == "NA"),
            "score": score, "met": met, "n_cond": len(conds), "n_root": n_root,
            "root_met": sum(1 for _, ok, _ in conds[:n_root] if ok), "conf_met": conf_met,
            "conf_missing": sum(ok is None for _, ok, _ in conds[n_root:])}


def market_confirmation(vd):
    label = f"{vd.get('conf_met', 0)}/{vd['n_cond'] - (vd.get('n_root') or 0)}"
    if vd.get("conf_missing"):
        label += f"（{vd['conf_missing']} 项缺数据，待确认）"
    return label


def condition_icon(ok):
    return "⚪" if ok is None else ("✅" if ok else "⬜")


def diff_last(inds, last):
    if not last:
        return [], None
    prev = {i["id"]: i for i in last.get("indicators", [])}
    order = {"R": 0, "Y": 1, "G": 2, "NA": -1}
    flips = []
    for i in inds:
        p = prev.get(i["id"])
        if p and p["status"] != i["status"] and "NA" not in (p["status"], i["status"]):
            better = order[i["status"]] > order[p["status"]]
            flips.append({"name": i["name"], "from": p["status"], "to": i["status"], "better": better,
                          "pv": p["value"], "nv": i["value"]})
    return flips, last.get("generated_hkt")


def build_calendar(cfg, extra, days=16):
    today = datetime.now(ET).date()
    end = today + timedelta(days=days)
    items = []
    show = set(cfg["universe"]["calendar_show"])
    cache = extra.get("earn_cache", {}).get("events", {})
    for t, evs in cache.items():
        if t not in show:
            continue
        for e in evs:
            ts = datetime.fromisoformat(e["ts"]).astimezone(ET)
            if today <= ts.date() <= end and e["reported"] is None:
                when = "盘前" if ts.hour < 12 else "盘后"
                items.append((ts.date(), f"{when} {t}"))
    for ev in cfg.get("events", []):
        d = date.fromisoformat(ev["date"])
        if today <= d <= end:
            items.append((d, ev["name"]))
    byday = {}
    for d, txt in sorted(items):
        byday.setdefault(d, []).append(txt)
    lines = []
    for d, txts in byday.items():
        earn = sorted(x for x in txts if x.startswith(("盘前 ", "盘后 ")))
        other = [x for x in txts if x not in earn]
        parts = []
        for w in ("盘前", "盘后"):
            ts_ = [x.split(" ", 1)[1] for x in earn if x.startswith(w)]
            if ts_:
                parts.append(f"{w} {'、'.join(ts_)}")
        parts += other
        dd = (d - today).days
        tag = "今天" if dd == 0 else f"D-{dd}"
        lines.append(f"{md(d)} 周{WEEKDAY_CN[d.weekday()]}（{tag}）：" + "；".join(parts))
    return lines



def build_key_events(cfg, extra):
    """重点事件倒计时：不受两周窗口限制；已过的事件显示财报次日相对 SMH 的反应。"""
    today = datetime.now(ET).date()
    horizon = cfg.get("key_events_horizon_days", 100)
    rx = {}
    for r in extra.get("reactions") or []:
        rx.setdefault(r["ticker"], r)
    out = []
    for ev in sorted(cfg.get("key_events", []), key=lambda e: e["date"]):
        d = date.fromisoformat(ev["date"])
        dd = (d - today).days
        if dd < -7 or dd > horizon:
            continue
        when = ev.get("when_text") or md(d)
        if ev.get("tbd"):
            tag = "日期未定" if dd >= 0 else "已过预计窗口"
        elif dd > 0:
            tag = f"D-{dd}"
        elif dd == 0:
            tag = "今天"
        else:
            tag = "已过"
        line = {"head": f"{when}（{tag}）{ev['name']}", "rescue": ev.get("rescue", ""), "bad": ev.get("bad", ""),
                "short": ev.get("short") or ev["name"].split("（")[0], "when": when, "tag": tag, "dd": dd,
                "tbd": bool(ev.get("tbd"))}
        t = ev.get("ticker")
        if dd < 0 and t and t in rx:
            line["result"] = f"结果：{t} 次日相对 SMH {fmt_pct(rx[t]['ex'], 1)}"
        elif dd < 0:
            line["result"] = "结果：待人工更新"
        out.append(line)
    return out


def snapshots(cfg, data: Data, slot: str):
    """AM：美股收盘 + 盘后；PM：亚洲收盘 + 美股盘前。"""
    lines = []
    u = cfg["universe"]

    def chg(t):
        s = data.s(t)
        if s is None or len(s) < 2:
            return None, None
        return float(s.iloc[-1]), (float(s.iloc[-1]) / float(s.iloc[-2]) - 1) * 100

    if slot == "am":
        parts = []
        for t, name in u["us_snapshot"]:
            v, c = chg(t)
            if c is not None:
                parts.append(f"{name} {fmt_pct(c)}")
        lines.append("美股收盘：" + "｜".join(parts))
    else:
        parts = []
        for t, name in u["asia_snapshot"]:
            s = data.s(t)
            if s is None or len(s) < 2:
                continue
            last = s.index[-1].date()
            if (datetime.now(ET).date() - last).days > 4:
                continue
            tag = "" if last >= datetime.now(HKT).date() else f"（{md(last)}，今天休市）"
            parts.append(f"{name} {fmt_pct((s.iloc[-1]/s.iloc[-2]-1)*100)}{tag}")
        if parts:
            lines.append("亚洲收盘：" + "｜".join(parts))
    # 盘前/盘后：5 分钟线含盘前盘后
    try:
        import yfinance as yf
        tick = [t for t, _ in u["premarket"]]
        earn_today = []
        cache = load_json(STATE_DIR / "earnings_cache.json", {"events": {}})["events"]
        today = datetime.now(ET).date()
        for t, evs in cache.items():
            for e in evs:
                ts = datetime.fromisoformat(e["ts"]).astimezone(ET)
                if slot == "am" and ts.date() == today and ts.hour >= 16 and ts <= datetime.now(ET):
                    earn_today.append(t)
                if slot == "pm" and ts.date() == today and ts.hour < 9:
                    earn_today.append(t)
        tick = sorted(set(tick) | set(earn_today))
        d = yf.download(tick, period="2d", interval="5m", prepost=True, progress=False, group_by="column", auto_adjust=False)
        cl = d["Close"]
        names = dict(u["premarket"])
        parts = []
        for t in tick:
            if t not in cl.columns:
                continue
            s = cl[t].dropna()
            ref = data.s(t)
            if s.empty or ref is None:
                continue
            last_ts = s.index[-1].tz_convert(ET)
            if (datetime.now(ET) - last_ts).total_seconds() > 6 * 3600:
                continue
            # 参考价：最近一个已收盘的日线收盘
            base = float(ref.iloc[-1])
            if ref.index[-1].date() >= today and slot == "pm" and t != "NQ=F":
                base = float(ref.iloc[-2])
            if t.endswith("=F"):
                # 期货：拿上一个美股常规交易日 16:00（ET）前最后一根 5 分钟线当基准
                sess = data.s("SMH").index[-1].date()
                idx = s.index.tz_convert(ET)
                cut = datetime(sess.year, sess.month, sess.day, 16, 0, tzinfo=ET)
                prior = s[idx < cut]
                if prior.empty:
                    continue
                base = float(prior.iloc[-1])
            c = (float(s.iloc[-1]) / base - 1) * 100
            label = names.get(t, t)
            star = "（今天财报）" if t in earn_today else ""
            parts.append(f"{label} {fmt_pct(c)}{star}")
        if parts:
            now_et = datetime.now(ET)
            ext = "盘后" if (now_et.hour >= 16 or now_et.hour < 4) else "盘前"
            lines.append((f"{ext}：" if slot == "am" else "美股盘前：") + "｜".join(parts))
    except Exception as e:  # noqa: BLE001
        data.errors.append(f"盘前盘后快照失败：{str(e)[:60]}")
    return lines


# ------------------------------------------------------------------ charts
def setup_fonts():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, rcParams
    for p in ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"):
        if Path(p).exists():
            font_manager.fontManager.addfont(p)
            name = font_manager.FontProperties(fname=p).get_name()
            rcParams["font.family"] = name
            break
    rcParams["axes.unicode_minus"] = False


def _w(ch):
    return 0.55 if ord(ch) < 0x2E80 else 1.0


def _wrap(text, width, lines=2):
    """按显示宽度折行（中文算 1、ASCII 算 0.55），最多 lines 行，超出加省略号。"""
    text = str(text or "")
    out, cur, cw = [], "", 0.0
    for ch in text:
        if cw + _w(ch) > width:
            out.append(cur)
            cur, cw = "", 0.0
            if len(out) == lines:
                break
        cur += ch
        cw += _w(ch)
    else:
        out.append(cur)
        return "\n".join(out)
    out[-1] = out[-1][:-1] + "…"
    return "\n".join(out)


MW = 6.4          # 手机版图宽（英寸）。字号 F 在手机屏上约等于 F × 340 / (72 × MW) 磅
MDPI = 180        # 输出约 1150 px 宽
LH = 1.38         # 行高系数


def _lines(text, size, width_in, max_lines):
    """按可用宽度（英寸）和字号折行；中文字宽≈字号，ASCII≈0.55 字号。"""
    units = width_in / (size / 72.0)
    return _wrap(text, units, max_lines).split("\n")


def _lh(size):
    return size / 72.0 * LH


def _mfig(H):
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(MW, H), dpi=MDPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, MW)
    ax.set_ylim(0, H)
    ax.axis("off")
    return fig, ax


def _band(ax, y_top, h, color, title, size=19):
    from matplotlib.patches import FancyBboxPatch
    ax.add_patch(FancyBboxPatch((0.18, y_top - h), MW - 0.36, h, boxstyle="round,pad=0.01,rounding_size=0.1", fc=color, ec="none"))
    ax.text(0.38, y_top - h / 2, title, color="white", fontsize=size, weight="bold", va="center")


def draw_overview(inds, vd, conds, header, floor_txt, path):
    """图 1：结论、红黄绿计数、反转确认 5 条、五组指标灯。"""
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch, Circle
    col = {"green": "#1f9d55", "turquoise": "#1a9c8f", "orange": "#dd6b20", "red": "#c53030"}[vd["template"]]
    rowc, rowp = 0.50, 0.50
    H = 1.75 + 1.25 + (0.62 if floor_txt else 0) + 0.55 + len(conds) * rowc + 0.55 + len(PILLARS) * rowp + 0.55
    fig, ax = _mfig(H)
    y = H - 0.15
    # 结论条
    ax.add_patch(FancyBboxPatch((0.18, y - 1.55), MW - 0.36, 1.55, boxstyle="round,pad=0.01,rounding_size=0.12", fc=col, ec="none"))
    ax.text(0.38, y - 0.36, header["title"], color="white", fontsize=15, va="center")
    ax.text(0.38, y - 0.86, vd["text"], color="white", fontsize=23, weight="bold", va="center")
    ax.text(0.38, y - 1.30, header["when"], color="white", fontsize=12.5, va="center")
    y -= 1.75
    # 计数
    bw = (MW - 0.36 - 3 * 0.12) / 4
    items = [(str(vd["g"]), "救场", STATUS_COLOR["G"]), (str(vd["y"]), "中性", STATUS_COLOR["Y"]),
             (str(vd["r"]), "坏信号", STATUS_COLOR["R"]), (f"{vd['score']:+d}", "净分", "#2d3748")]
    for k, (num, lab, c) in enumerate(items):
        x = 0.18 + k * (bw + 0.12)
        ax.add_patch(FancyBboxPatch((x, y - 1.05), bw, 1.05, boxstyle="round,pad=0.01,rounding_size=0.08", fc="#f7fafc", ec="#e2e8f0", lw=0.8))
        ax.text(x + bw / 2, y - 0.43, num, color=c, fontsize=27, weight="bold", ha="center", va="center")
        ax.text(x + bw / 2, y - 0.84, lab, color="#4a5568", fontsize=13, ha="center", va="center")
    y -= 1.25
    if floor_txt:
        ax.add_patch(FancyBboxPatch((0.18, y - 0.50), MW - 0.36, 0.50, boxstyle="round,pad=0.01,rounding_size=0.06", fc="#fff5f5", ec="#feb2b2", lw=0.8))
        ax.text(0.34, y - 0.25, floor_txt, color="#9b2c2c", fontsize=13.5, va="center", weight="bold")
        y -= 0.62
    # 反转确认
    ax.text(0.22, y - 0.25, f"反转确认  {vd['met']} / {vd['n_cond']}", fontsize=17, weight="bold", color="#1a202c", va="center")
    y -= 0.55
    for name, ok, det in conds:
        cy = y - rowc / 2
        c = STATUS_COLOR["G"] if ok else "#a0aec0"
        ax.add_patch(Circle((0.40, cy), 0.11, fc=c if ok else "white", ec=c, lw=2))
        if ok:  # 画勾，避免字体缺字
            ax.plot([0.345, 0.390, 0.465], [cy + 0.005, cy - 0.045, cy + 0.06], color="white", lw=2.4,
                    solid_capstyle="round", solid_joinstyle="round")
        ax.text(0.66, cy, name, fontsize=15, va="center", color="#1a202c" if ok else "#2d3748", weight="bold" if ok else "normal")
        ax.text(MW - 0.25, cy, det, fontsize=13.5, va="center", ha="right", color="#4a5568")
        ax.plot([0.25, MW - 0.25], [y - rowc, y - rowc], color="#edf2f7", lw=0.8)
        y -= rowc
    # 五组指标灯
    y -= 0.05
    ax.text(0.22, y - 0.25, "五组指标", fontsize=17, weight="bold", color="#1a202c", va="center")
    ax.text(MW - 0.25, y - 0.25, "实心=自动　空心=人工", fontsize=11.5, color="#718096", va="center", ha="right")
    y -= 0.55
    for p in PILLARS:
        grp = [i for i in inds if i["pillar"] == p]
        grp.sort(key=lambda i: (i["manual"], {"G": 0, "Y": 1, "R": 2, "NA": 3}[i["status"]]))
        cy = y - rowp / 2
        ax.text(0.25, cy, PILLAR_DESC[p], fontsize=15, va="center", color="#1a202c", weight="bold")
        x = 2.05
        for i in grp:
            c = STATUS_COLOR[i["status"]]
            ax.add_patch(Circle((x, cy), 0.105, fc=("white" if i["manual"] else c), ec=c, lw=2.2))
            x += 0.30
        sc = sum(1 if i["status"] == "G" else (-1 if i["status"] == "R" else 0) for i in grp if not i["manual"])
        ax.text(MW - 0.25, cy, f"净分 {sc:+d}", fontsize=13.5, va="center", ha="right",
                color=STATUS_COLOR["G"] if sc > 0 else (STATUS_COLOR["R"] if sc < 0 else "#4a5568"), weight="bold")
        ax.plot([0.25, MW - 0.25], [y - rowp, y - rowp], color="#edf2f7", lw=0.8)
        y -= rowp
    ax.text(0.22, 0.25, "自动指标明细见下面三张图；人工判断项见文字部分", fontsize=11.5, color="#a0aec0", va="center")
    fig.savefig(path, dpi=MDPI, facecolor="white")
    plt.close(fig)


def draw_details(inds, pillars, title, path):
    """图 2/3：自动指标逐项卡片。每项：状态 + 名称 / 当前读数 / 救场线 / 坏信号线。"""
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    S_NAME, S_VAL, S_TH = 16, 15, 13.5
    inner = MW - 0.36 - 0.42          # 卡片内文字可用宽度
    blocks = []
    for p in pillars:
        grp = [i for i in inds if i["pillar"] == p and not i["manual"]]
        grp.sort(key=lambda i: (not i["key"], {"R": 0, "Y": 1, "G": 2, "NA": 3}[i["status"]]))
        sc = sum(1 if i["status"] == "G" else (-1 if i["status"] == "R" else 0) for i in grp)
        blocks.append(("sec", p, sc, 0.62))
        for i in grp:
            name = i["name"] + (" ★" if i["key"] else "")
            nl = _lines(name, S_NAME, inner - 1.25, 2)
            vl = _lines(i["value"], S_VAL, inner, 2)
            rl = _lines("救场  " + i["rescue"], S_TH, inner, 2)
            bl = _lines("坏信号  " + i["bad"], S_TH, inner, 2)
            h = 0.16 + len(nl) * _lh(S_NAME) + 0.04 + len(vl) * _lh(S_VAL) + 0.06 + (len(rl) + len(bl)) * _lh(S_TH) + 0.16
            blocks.append(("ind", i, (nl, vl, rl, bl), h))
    H = 0.95 + sum(b[3] + (0.12 if b[0] == "ind" else 0) for b in blocks) + 0.45
    fig, ax = _mfig(H)
    y = H - 0.12
    _band(ax, y, 0.68, "#2d3748", title, 17)
    y -= 0.95
    for b in blocks:
        if b[0] == "sec":
            _, p, sc, h = b
            ax.text(0.22, y - 0.30, PILLAR_DESC[p], fontsize=17, weight="bold", color="#1a202c", va="center")
            ax.text(MW - 0.25, y - 0.30, f"自动指标净分 {sc:+d}", fontsize=13, va="center", ha="right",
                    color=STATUS_COLOR["G"] if sc > 0 else (STATUS_COLOR["R"] if sc < 0 else "#4a5568"))
            y -= h
            continue
        _, i, (nl, vl, rl, bl), h = b
        st = i["status"]
        x0 = 0.18
        ax.add_patch(FancyBboxPatch((x0, y - h), MW - 0.36, h, boxstyle="round,pad=0.005,rounding_size=0.07", fc="#ffffff", ec="#e2e8f0", lw=1.0))
        ax.add_patch(plt.Rectangle((x0, y - h), 0.09, h, fc=STATUS_COLOR[st], ec="none"))
        # 状态胶囊
        ax.add_patch(FancyBboxPatch((MW - 0.36 - 1.05, y - 0.16 - 0.36), 1.0, 0.36, boxstyle="round,pad=0.005,rounding_size=0.1",
                                    fc=STATUS_COLOR[st], ec="none"))
        ax.text(MW - 0.36 - 0.55, y - 0.16 - 0.18, STATUS_TXT[st], fontsize=13, color="white", weight="bold", ha="center", va="center")
        tx = x0 + 0.26
        yy = y - 0.16
        for ln in nl:
            ax.text(tx, yy - _lh(S_NAME) / 2, ln, fontsize=S_NAME, weight="bold", color="#1a202c", va="center")
            yy -= _lh(S_NAME)
        yy -= 0.04
        for ln in vl:
            ax.text(tx, yy - _lh(S_VAL) / 2, ln, fontsize=S_VAL, color="#1a202c", va="center")
            yy -= _lh(S_VAL)
        yy -= 0.06
        for ln in rl:
            ax.text(tx, yy - _lh(S_TH) / 2, ln, fontsize=S_TH, color="#276749", va="center")
            yy -= _lh(S_TH)
        for ln in bl:
            ax.text(tx, yy - _lh(S_TH) / 2, ln, fontsize=S_TH, color="#9b2c2c", va="center")
            yy -= _lh(S_TH)
        y -= h + 0.12
    ax.text(0.22, 0.22, "★ 进入反转确认清单；阈值见 config/semi_rescue_alert.json", fontsize=11.5, color="#a0aec0", va="center")
    fig.savefig(path, dpi=MDPI, facecolor="white")
    plt.close(fig)


def draw_chart_groups(cfg, data: Data, extra, paths, start="2026-05-01"):
    """走势图：三张，每张 3 个面板竖排。paths 依次是 价格、利率与信用、仓位。"""
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import pandas as pd
    th, an = cfg["thresholds"], cfg["anchors"]
    t0 = pd.Timestamp(start)
    F_T, F_V, F_TK, F_LG = 15.5, 14, 12.5, 12

    def band(ax, green, red, higher_better, lo, hi):
        if higher_better:
            ax.axhspan(green, hi, color="#c6f6d5", alpha=0.55, lw=0)
            ax.axhspan(lo, red, color="#fed7d7", alpha=0.55, lw=0)
        else:
            ax.axhspan(lo, green, color="#c6f6d5", alpha=0.55, lw=0)
            ax.axhspan(red, hi, color="#fed7d7", alpha=0.55, lw=0)
        ax.axhline(green, color="#1f9d55", lw=1.1, ls="--")
        ax.axhline(red, color="#d64545", lw=1.1, ls="--")

    def fin(ax, title, last_txt, monthly=True):
        ax.set_title(title, fontsize=F_T, loc="left", weight="bold", pad=10)
        ax.set_title(last_txt, fontsize=F_V, loc="right", color="#2b6cb0", weight="bold", pad=10)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m月"))
        if monthly:
            ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.tick_params(labelsize=F_TK)
        ax.grid(alpha=0.25)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)

    def nodata(ax, title):
        ax.text(0.5, 0.5, "缺数据", ha="center", transform=ax.transAxes, fontsize=F_T)
        ax.set_title(title, fontsize=F_T, loc="left", weight="bold")

    def series(ax, title, s, green=None, red=None, hb=True, fmt="{:.2f}", color="#2b6cb0"):
        if s is None or len(s) == 0:
            return nodata(ax, title)
        s = s[s.index >= t0]
        lo, hi = float(s.min()), float(s.max())
        if green is not None:
            lo, hi = min(lo, green, red), max(hi, green, red)
        pad = (hi - lo) * 0.10 or 1
        ax.set_ylim(lo - pad, hi + pad)
        if green is not None:
            band(ax, green, red, hb, lo - pad, hi + pad)
        ax.plot(s.index, s.values, color=color, lw=2.2)
        fin(ax, title, "最新 " + fmt.format(float(s.iloc[-1])))

    def p_smh(ax):
        smh = data.s("SMH")
        s = smh[smh.index >= t0]
        ma = smh.rolling(200).mean()
        ma = ma[ma.index >= t0]
        ax.plot(s.index, s.values, color="#2b6cb0", lw=2.2, label="SMH")
        ax.plot(ma.index, ma.values, color="#805ad5", lw=1.6, label="200 日线")
        ax.axhline(an["smh_floor"], color="#d64545", ls="--", lw=1.4, label=f"底线 {an['smh_floor']:.0f}")
        lo_, hi_ = float(min(s.min(), an["smh_floor"])), float(max(s.max(), ma.max()))
        ax.set_ylim(lo_ - (hi_ - lo_) * 0.08, hi_ + (hi_ - lo_) * 0.30)
        ax.legend(fontsize=F_LG, loc="upper left", ncol=3, frameon=False, handlelength=1.4, columnspacing=1.0)
        fin(ax, "SMH 与 200 日线", f"最新 {s.iloc[-1]:.1f}")

    def p_rel(ax):
        r = extra.get("rel")
        if r is None:
            return nodata(ax, "SMH / QQQ")
        rs = r[r.index >= t0]
        m50 = r.rolling(50).mean()
        m50 = m50[m50.index >= t0]
        ax.plot(rs.index, rs.values, color="#2b6cb0", lw=2.2, label="SMH/QQQ")
        ax.plot(m50.index, m50.values, color="#a0aec0", lw=1.6, label="50 日均值")
        lo_, hi_ = float(min(rs.min(), m50.min())), float(max(rs.max(), m50.max()))
        ax.set_ylim(lo_ - (hi_ - lo_) * 0.06, hi_ + (hi_ - lo_) * 0.30)
        ax.legend(fontsize=F_LG, loc="upper left", frameon=False, ncol=2)
        fin(ax, "SMH / QQQ 相对强弱", f"最新 {rs.iloc[-1]:.3f}")

    def p_hike(ax):
        zk, meet = extra.get("hike_series"), extra.get("hike_meet")
        if zk is None:
            return nodata(ax, "联邦基金利率")
        imp, act = zk
        imp, act = imp[imp.index >= t0], act[act.index >= t0]
        ax.step(act.index, act.values, where="post", color="#718096", lw=1.8, label="实际")
        ax.plot(imp.index, imp.values, color="#c05621", lw=2.2, label=f"期货隐含（{md(meet)} 会后）")
        lo_, hi_ = float(min(imp.min(), act.min())), float(max(imp.max(), act.max()))
        ax.set_ylim(lo_ - 0.1, hi_ + (hi_ - lo_) * 0.40 + 0.05)
        ax.legend(fontsize=F_LG, loc="upper left", frameon=False, ncol=2)
        pr = extra.get("hike_prob_now")
        fin(ax, "联邦基金利率 %", f"加息概率约 {pr:.0f}%" if pr is not None else "")

    def p_pb(ax):
        pb = extra.get("pb")
        if pb is None:
            return nodata(ax, "高盛 PB 杠杆")
        p = pb[pb["week_ending"] >= pd.Timestamp("2025-06-01")]
        ax.plot(p["week_ending"], p["net"], color="#2b6cb0", lw=2.2, label="净杠杆（左轴）")
        ax2 = ax.twinx()
        ax2.plot(p["week_ending"], p["gross"], color="#dd6b20", lw=1.6, label="总杠杆（右轴）")
        ax2.tick_params(labelsize=F_TK)
        ax2.spines["top"].set_visible(False)
        for a_, col_ in ((ax, "net"), (ax2, "gross")):  # 顶部留白放图例
            lo_, hi_ = float(p[col_].min()), float(p[col_].max())
            a_.set_ylim(lo_ - (hi_ - lo_) * 0.06, hi_ + (hi_ - lo_) * 0.38)
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=F_LG, loc="upper left", frameon=False, ncol=2)
        last = p.iloc[-1]
        fin(ax, "高盛 PB 杠杆（周）", f"净 {last['net']:.1f}｜总 {last['gross']:.0f}", monthly=False)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%y/%-m"))

    def p_mom(ax):
        mo = extra.get("mom")
        if mo is not None:
            mo = mo[mo.index >= t0]
            mo = mo / mo.iloc[0] * 100
        series(ax, "动量代理 MTUM/RSP", mo, None, None, True, "{:.1f}", "#2f855a")

    def p_gpu(ax):
        g = extra.get("gpu")
        if not g:
            return nodata(ax, "GPU 一年期租金")
        cols = [("CDB300", "B300", "#2b6cb0"), ("CDB200", "B200", "#3182ce"), ("CDH200", "H200", "#dd6b20"),
                ("CDGXH100", "H100", "#c05621")]
        lo_, hi_ = 1e9, 0
        for a, lab, col in cols:
            if a not in g:
                continue
            s = g[a][g[a].index >= t0]
            ax.plot(s.index, s.values, color=col, lw=2.2 if lab.startswith("B") else 1.7,
                    ls="-" if lab.startswith("B") else "--", label=lab)
            lo_, hi_ = min(lo_, float(s.min())), max(hi_, float(s.max()))
        ax.set_ylim(max(0, lo_ - (hi_ - lo_) * 0.06), hi_ + (hi_ - lo_) * 0.32)
        ax.legend(fontsize=F_LG, loc="upper left", frameon=False, ncol=4, handlelength=1.6, columnspacing=1.0)
        b3 = g.get("CDB300")
        h1 = g.get("CDGXH100")
        fin(ax, "GPU 一年期租金 美元/卡时",
            "｜".join(x for x in [f"B300 {b3.iloc[-1]:.2f}" if b3 is not None else "", f"H100 {h1.iloc[-1]:.2f}" if h1 is not None else ""] if x))

    def p_tmt(ax):
        s = extra.get("tmt_flow")
        if s is None:
            return nodata(ax, "高盛 PB：TMT 累计净买卖")
        ax.axhline(0, color="#718096", lw=1.0)
        ax.fill_between(s.index, s.values, 0, where=s.values >= 0, color="#c6f6d5", alpha=0.8, lw=0)
        ax.fill_between(s.index, s.values, 0, where=s.values < 0, color="#fed7d7", alpha=0.8, lw=0)
        ax.plot(s.index, s.values, color="#2b6cb0", lw=2.2)
        lo_, hi_ = float(min(s.min(), 0)), float(max(s.max(), 0))
        ax.set_ylim(lo_ - (hi_ - lo_) * 0.08, hi_ + (hi_ - lo_) * 0.12)
        w1 = float(s.iloc[-1] - s.iloc[-6]) if len(s) > 6 else float("nan")
        fin(ax, "高盛 PB：TMT 年初累计净买卖", f"最新 {s.iloc[-1]:+.2f}（周 {w1:+.2f}）", monthly=False)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))

    def p_cftc(ax):
        df = extra.get("cftc")
        if df is None or df.empty:
            return nodata(ax, "纳指期货 杠杆基金净头寸")
        lev = df["lev"]
        win = lev[lev.index >= lev.index[-1] - pd.Timedelta(days=3 * 365)]
        q20, q80 = float(win.quantile(th["cftc_pct_green"] / 100)), float(win.quantile(th["cftc_pct_red"] / 100))
        s = lev[lev.index >= pd.Timestamp("2025-01-01")]
        lo_, hi_ = float(min(s.min(), q20)), float(max(s.max(), q80))
        pad = (hi_ - lo_) * 0.10
        band(ax, q20, q80, False, lo_ - pad, hi_ + pad * 3)
        ax.set_ylim(lo_ - pad, hi_ + pad * 3)
        ax.plot(s.index, s.values, color="#6b46c1", lw=2.2)
        ax.text(0.01, 0.96, f"绿线=3 年 {th['cftc_pct_green']} 分位，红线=3 年 {th['cftc_pct_red']} 分位",
                transform=ax.transAxes, fontsize=F_LG, color="#4a5568", va="top")
        fin(ax, "纳指期货 杠杆基金净头寸 % 持仓量", f"最新 {s.iloc[-1]:+.1f}%", monthly=False)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%y/%-m"))

    groups = [
        ("价格与技术", [p_smh, p_rel,
                    lambda ax: series(ax, "SOX 成分股站上 50 日线 %", extra.get("breadth"), th["breadth_green"], th["breadth_red"], True, "{:.0f}%")]),
        ("信用与利率", [lambda ax: series(ax, "高收益债利差 %", extra.get("hy"), th["hy_green"], th["hy_red"], False, "{:.2f}%", "#c05621"),
                    lambda ax: series(ax, "10 年期实际利率 %", extra.get("real"), th["real10y_green"], th["real10y_red"], False, "{:.2f}%", "#c05621"),
                    p_hike]),
        ("仓位与资金", [lambda ax: series(ax, "VXN - VIX", extra.get("vxnvix"), th["vxn_vix_green"], th["vxn_vix_red"], False, "{:.1f}", "#6b46c1"),
                    p_pb, p_mom]),
        ("GPU 租金与 AI 仓位", [p_gpu, p_tmt, p_cftc]),
    ]
    subs = {"GPU 租金与 AI 仓位": "租金 5 月起｜资金流年初起｜期货 2025 年起"}
    for k, ((gname, fns), path) in enumerate(zip(groups, paths)):
        n = len(fns)
        PH, TOP = 3.05, 1.32
        H = TOP + n * PH + 0.15
        fig, axs = plt.subplots(n, 1, figsize=(MW, H), dpi=MDPI)
        fig.subplots_adjust(left=0.13, right=0.88 if gname == "仓位与资金" else 0.96, top=1 - TOP / H, bottom=0.30 / H, hspace=0.55)
        fig.text(0.03, 1 - 0.36 / H, f"走势 {k + 1}/{len(groups)}：{gname}", fontsize=17, weight="bold", va="center")
        fig.text(0.03, 1 - 0.70 / H, subs.get(gname, "2026-05 起｜绿区=救场一侧，红区=坏信号一侧"), fontsize=12, color="#718096", va="center")
        for ax, fn in zip(axs, fns):
            try:
                fn(ax)
            except Exception as e:  # noqa: BLE001
                ax.cla()
                nodata(ax, f"出图失败：{str(e)[:30]}")
        fig.savefig(path, dpi=MDPI, facecolor="white")
        plt.close(fig)


def build_markdown(cfg, slot, inds, vd, conds, flips, last_hkt, snaps, cal, sec, extra, imgs, errors, now_hkt):
    L = []
    sm = extra.get("smh", {})
    L.append(f"**结论：{vd['emoji']} {vd['text']}**　自动指标 🟢{vd['g']} 🟡{vd['y']} 🔴{vd['r']}" + (f" ⚪{vd['na']}" if vd["na"] else "") + f"，净分 {vd['score']:+d}")
    L.append(f"**反转确认 {vd['met']}/{vd['n_cond']}**：" + "；".join(f"{condition_icon(ok)} {n}（{d}）" for n, ok, d in conds))
    if sm:
        L.append(f"**底线**：SMH {sm['close']:.1f}，距 {cfg['anchors']['smh_floor_label']} {sm['floor']:.1f} 还有 {fmt_pct(sm['to_floor'])}；收盘跌破就是高点、低点同时降低。")
    L.append("")
    L.append("### 本次变化" + (f"（对比 {last_hkt}）" if last_hkt else ""))
    L.append("")
    if last_hkt is None:
        L.append("- 首次推送，下一次起列出变色的指标。")
    elif not flips:
        L.append("- 没有指标变色。")
    else:
        for f in sorted(flips, key=lambda f: not f["better"]):
            arrow = "改善" if f["better"] else "转差"
            L.append(f"- {STATUS_EMO[f['from']]}→{STATUS_EMO[f['to']]} {f['name']}（{arrow}）：{f['pv']} → {f['nv']}")
    for s in snaps:
        L.append(f"- {s}")
    rx = extra.get("reactions") or []
    if rx:
        recent = sorted(rx, key=lambda r: r["day"], reverse=True)[:8]
        L.append("- 近期财报次日相对 SMH：" + "，".join(f"{r['ticker']} {fmt_pct(r['ex'], 0)}" for r in recent))
    L.append("")
    L.append("### 信号灯总览")
    L.append("")
    L.append(f"IMG:{imgs[0]}")
    L.append("")
    reds = [i for i in inds if i["status"] == "R" and not i["manual"]]
    greens = [i for i in inds if i["status"] == "G" and not i["manual"]]
    if greens:
        L.append("- 🟢 已亮绿：" + "、".join(i["name"] for i in greens))
    if reds:
        L.append("- 🔴 仍是坏信号：" + "、".join(i["name"] for i in reds))
    L.append("")
    L.append("### 自动指标明细")
    L.append("")
    for im in imgs[1:4]:
        L.append(f"IMG:{im}")
        L.append("")
    L.append("### 人工判断项")
    L.append("")
    for i in [i for i in inds if i["manual"]]:
        L.append(f"- {STATUS_EMO[i['status']]} **{i['name']}**：{i['value']}")
        L.append(f"  救场：{i['rescue']}｜坏信号：{i['bad']}")
        if i.get("note"):
            L.append(f"  {i['note']}")
    L.append("")
    L += news_markdown(extra.get("news"), main_body=True)
    L += news_markdown(extra.get("news"), main_body=False)
    L.append("### 重点事件倒计时")
    L.append("")
    for k in extra.get("key_events") or []:
        L.append(f"- **{k['head']}**")
        L.append(f"  救场：{k['rescue']}｜坏信号：{k['bad']}")
        if k.get("result"):
            L.append(f"  {k['result']}")
    L.append("")
    L.append("### 未来两周催化剂")
    L.append("")
    for c in cal or ["未来两周没有登记的事件"]:
        L.append(f"- {c}")
    for s_ in sec:
        if s_.get("form"):
            L.append(f"- EDGAR {s_['name']}：最新 {s_['form']}（{md(s_['date'])}）— {s_['meaning']}")
        else:
            L.append(f"- EDGAR {s_['name']}：{s_['meaning']}")
    L.append("")
    L.append("### 指标走势")
    L.append("")
    for im in imgs[4:]:
        L.append(f"IMG:{im}")
        L.append("")
    L.append("### 口径说明")
    L.append("")
    L.append(f"- 生成于 {now_hkt}（香港时间），美股数据截至 {md(extra.get('last_day'))} 收盘；{'早报看美股收盘和盘后' if slot == 'am' else '晚报看亚洲收盘和美股盘前'}。")
    L.append("- 行情 yfinance；利差、实际利率、联邦基金利率 FRED；台股外资和融资 台湾证交所；PB 杠杆和 TMT 资金流 高盛周报；文件 SEC EDGAR。")
    L.append("- GPU 租金 Compute Desk 公开指数（美国一年期租约，H100 为 GX 官方值）；AI 股敞口从高盛销售邮件自动抽取，低频；纳指期货仓位 CFTC TFF 周报；空头股数 FINRA 半月数据（经 yfinance）。")
    L.append("- 加息概率按联邦基金期货简化推算，和 CME FedWatch 会有几个点的差。人工项按事件结果手动更新，日期写在说明里。")
    if errors:
        L.append("- 数据缺口：" + "；".join(errors[:6]))
    return "\n".join(L)


# ------------------------------------------------------------------ compact layout
def draw_arr_panel(inds, path):
    """两家实验室 ARR 每月净增（亿美元）；初步数画斜线浅色。阈值线：绿=救场线，红=坏信号线。"""
    import matplotlib.pyplot as plt
    labs = [i for i in inds if i["id"] in ROOT_IDS]
    if not labs:
        return False
    fig = plt.figure(figsize=(MW, 1.0 + 2.25 * len(labs)), dpi=MDPI)
    H = 1.0 + 2.25 * len(labs)
    fig.text(0.04, 1 - 0.36 / H, "实验室 ARR 每月净增（亿美元）", fontsize=18, weight="bold", color="#1a202c", va="center")
    fig.text(0.04, 1 - 0.72 / H, "深色=确认数　斜线浅色=初步数", fontsize=12.5, color="#4a5568", va="center")
    for k, i in enumerate(labs):
        raw = i["raw"]
        ser = raw["series"]
        conf = [(d, a) for d, a, pre, _ in ser if not pre]
        bars = []
        for (d0, a0), (d1, a1) in zip(conf[:-1], conf[1:]):
            bars.append((f"{date.fromisoformat(d1).month}月", a1 - a0, False))
        pre = raw.get("pre")
        if pre and pre.get("add_est") is not None:
            m = date.fromisoformat(pre["date"]).month
            bars.append((f"{m}月初步", float(pre["add_est"]), True))
        bars = bars[-5:]
        top = 1 - (1.0 + 2.25 * k) / H
        ax = fig.add_axes([0.10, top - 1.78 / H, 0.86, 1.40 / H])
        col = STATUS_COLOR[i["status"]] if i["status"] in STATUS_COLOR else "#4a5568"
        xs = range(len(bars))
        for x, (lab, v, p) in zip(xs, bars):
            ax.bar(x, v, width=0.6, color=("#cbd5e0" if p else "#4a5568"), hatch=("///" if p else None),
                   edgecolor=("#718096" if p else "#4a5568"), lw=0.8)
            ax.text(x, v + 2.5, f"{v:+.0f}", ha="center", va="bottom", fontsize=13, color="#1a202c", weight="bold",
                    zorder=6, bbox=dict(fc="white", ec="none", pad=1.5, alpha=0.95))
        ax.axhline(raw["green"], color=STATUS_COLOR["G"], ls="--", lw=1.4, zorder=1)
        ax.axhline(raw["red"], color=STATUS_COLOR["R"], ls=(0, (2, 2)), lw=1.4, zorder=1)
        # 阈值线在右端直接标字，代替图例
        ax.text(len(bars) - 0.45, raw["green"], f"救场 {raw['green']}", color=STATUS_COLOR["G"], fontsize=11.5,
                va="center", ha="left", weight="bold", bbox=dict(fc="white", ec="none", pad=0.8))
        ax.text(len(bars) - 0.45, raw["red"], f"坏信号 {raw['red']}", color=STATUS_COLOR["R"], fontsize=11.5,
                va="center", ha="left", weight="bold", bbox=dict(fc="white", ec="none", pad=0.8))
        ax.set_xlim(-0.6, len(bars) + 0.6)
        ax.set_xticks(list(xs))
        ax.set_xticklabels([b[0] for b in bars], fontsize=12.5)
        ymax = max([b[1] for b in bars] + [raw["green"]]) * 1.25
        ax.set_ylim(0, ymax)
        ax.tick_params(axis="y", labelsize=11)
        ax.grid(axis="y", alpha=0.25)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        last = conf[-1]
        fig.text(0.04, top - 0.16 / H, i["name"], fontsize=15.5, weight="bold", color="#1a202c", va="center")
        fig.text(0.96, top - 0.16 / H, f"{STATUS_TXT[i['status']]}｜{date.fromisoformat(last[0]).month} 月末 {last[1]:.0f} 亿",
                 fontsize=13.5, color=col, weight="bold", va="center", ha="right")
    fig.savefig(path, dpi=MDPI, facecolor="white")
    plt.close(fig)
    return True


def stack_images(paths, out):
    """把同宽的几张图竖着拼成一张（主体只放一张图）。"""
    from PIL import Image
    ims = [Image.open(p).convert("RGB") for p in paths]
    w = max(im.width for im in ims)
    canvas = Image.new("RGB", (w, sum(im.height for im in ims) + 12 * (len(ims) - 1)), "white")
    y = 0
    for k, im in enumerate(ims):
        canvas.paste(im, (0, y))
        y += im.height
        if k < len(ims) - 1:
            for yy in range(y + 5, y + 7):
                for xx in range(40, w - 40):
                    canvas.putpixel((xx, yy), (226, 232, 240))
            y += 12
    canvas.save(out)


def draw_smh_hero(cfg, data: Data, path, start="2026-06-01"):
    """主体唯一一张图：SMH、200 日线、底线，手机上一屏内看完。"""
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import pandas as pd
    an = cfg["anchors"]
    smh = data.s("SMH")
    try:  # 200 日线要更长的历史；拿不到就用已有数据
        import yfinance as yf
        h = yf.Ticker("SMH").history(period="2y", interval="1d", auto_adjust=False)["Close"].dropna()
        h.index = h.index.tz_localize(None).normalize()
        if len(h) > 260 and abs(float(h.iloc[-1]) / float(smh.iloc[-1]) - 1) < 0.002:
            smh = h
    except Exception:  # noqa: BLE001
        pass
    t0 = pd.Timestamp(start)
    s = smh[smh.index >= t0]
    ma = smh.rolling(200).mean().dropna()
    ma = ma[ma.index >= t0]
    floor = an["smh_floor"]
    last = float(s.iloc[-1])
    gap = (last / floor - 1) * 100
    fig = plt.figure(figsize=(MW, 3.6), dpi=MDPI)
    ax = fig.add_axes([0.10, 0.12, 0.86, 0.66])
    ax.plot(s.index, s.values, color="#2b6cb0", lw=2.4)
    ax.plot(ma.index, ma.values, color="#805ad5", lw=1.6)
    ax.axhline(floor, color="#d64545", ls="--", lw=1.5)
    lo_, hi_ = float(min(s.min(), floor, ma.min())), float(max(s.max(), ma.max()))
    ax.set_ylim(lo_ - (hi_ - lo_) * 0.06, hi_ + (hi_ - lo_) * 0.06)
    x_end = s.index[-1]
    ax.scatter([x_end], [last], color="#2b6cb0", s=36, zorder=5)
    # 线尾标签，代替图例
    ax.text(s.index[0], floor, f" 底线 {floor:.0f}（{an['smh_floor_label']}）", color="#d64545", fontsize=12,
            va="bottom", ha="left")
    k3 = len(ma) // 3
    ax.annotate("200 日线", (ma.index[k3], float(ma.iloc[k3])), xytext=(0, -16), textcoords="offset points",
                color="#805ad5", fontsize=12, va="top", ha="center", weight="bold",
                bbox=dict(fc="white", ec="none", pad=1.0, alpha=0.9))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m月"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.tick_params(labelsize=12)
    ax.grid(alpha=0.25)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    col = "#c53030" if gap < 2 else "#2d3748"
    where = f"离底线 {abs(gap):.1f}%" if gap >= 0 else f"跌破底线 {abs(gap):.1f}%"
    fig.text(0.04, 0.93, f"SMH {last:.1f}", fontsize=19, weight="bold", color="#1a202c", va="center")
    fig.text(0.96, 0.93, where, fontsize=16, weight="bold", color=col, va="center", ha="right")
    fig.savefig(path, dpi=MDPI, facecolor="white")
    plt.close(fig)


def _trim_snapshot(line, keep=5):
    """快照行只留前 keep 项；今天出财报的排前面；休市的去掉。"""
    if "：" not in line:
        return line
    head, body = line.split("：", 1)
    items = [x for x in body.split("｜") if "休市" not in x]
    items.sort(key=lambda x: "今天财报" not in x)
    items = [x.replace("（今天财报）", "（财报）") for x in items[:keep]]
    return f"{head}：" + "，".join(items) if items else ""


def build_markdown_compact(cfg, slot, inds, vd, conds, flips, last_hkt, snaps, cal, sec, extra, imgs, errors, now_hkt):
    """主体一屏：一句话、反转条件、变化、接下来、1 张图；其余折叠。"""
    L = []
    sm = extra.get("smh") or {}
    kev = extra.get("key_events") or []
    nxt = next((k for k in kev if k["dd"] >= 0 and not k["tbd"]), None)
    # 一句话
    by = {i["id"]: i for i in inds}
    nr = vd.get("n_root") or 0
    # 第一句：实验室收入（根因）
    arr_bits = []
    for rid in ROOT_IDS:
        i = by.get(rid)
        if i:
            arr_bits.append(f"{i['name'].replace(' ARR', '')} {(i.get('raw') or {}).get('short', i['value'])}")
    if nr:
        if vd.get("root_met") == nr:
            head = "实验室收入已回暖"
        elif vd.get("root_met"):
            head = "实验室收入一家回暖，另一家待确认"
        else:
            head = "实验室收入还没确认回暖"
        unmet = [by[r] for r in ROOT_IDS if r in by and by[r]["status"] != "G"]
        nx = (unmet[0].get("raw") or {}).get("next") if unmet else ""
        line0 = head + "。" + (f"下一个数据点：{nx}。" if nx else "")
    else:
        line0 = ""
    parts = []
    if sm:
        lab = cfg["anchors"]["smh_floor_label"]
        if sm["to_floor"] >= 0:
            parts.append(f"SMH {sm['close']:.1f}，离 {lab} {sm['floor']:.1f} 还有 {abs(sm['to_floor']):.1f}%")
        else:
            parts.append(f"SMH {sm['close']:.1f}，已跌破 {lab} {sm['floor']:.1f}（{sm['to_floor']:.1f}%）")
    parts.append(f"市场确认 {market_confirmation(vd)}")
    line1 = "；".join(parts) + "。"
    if nxt:
        line1 += f"下一个关键事件：{nxt['when']} {nxt['short']}（{nxt['tag']}）。"
    # 注意：feishu_send_card 会丢掉第一个 ### 标题之前的内容，主体每段都要有标题
    L.append("### 结论")
    if vd.get("short_term"):
        moves = vd["market_moves"]
        L.append(f"{vd['short_term']}：SMH {fmt_pct(moves['smh'])}、SOX {fmt_pct(moves['sox'])}（{md(extra['last_day'])} 美股收盘）；{vd['state_text']}。")
    if line0:
        L.append(line0)
    L.append(line1)
    L.append("")
    # 反转条件：根因在前，确认在后；组内保持配置顺序
    L.append(f"### 反转条件 {vd['met']}/{vd['n_cond']}")
    if nr:
        L.append(f"**根因：实验室收入 {vd.get('root_met', 0)}/{nr}**")
    for k, (n, ok, d) in enumerate(conds):
        if nr and k == nr:
            L.append(f"**确认：市场 {market_confirmation(vd)}**")
        L.append(f"{condition_icon(ok)} {n}<font color='grey'>　{d}</font>")
    L.append("")
    # 变化：最多 4 条
    L.append("### 变化" + (f"（对比 {last_hkt}）" if last_hkt else ""))
    if last_hkt is None:
        L.append("- 首次推送，下一条起列变色的指标")
    elif not flips:
        L.append("- 没有指标变色")
    else:
        fl = sorted(flips, key=lambda f: not f["better"])
        for f in fl[:4]:
            L.append(f"- {STATUS_EMO[f['from']]}→{STATUS_EMO[f['to']]} {f['name']}：{f['nv']}")
        if len(fl) > 4:
            L.append(f"- 另有 {len(fl) - 4} 项变色，见下面全部指标")
    for s in snaps:
        t = _trim_snapshot(s)
        if t:
            L.append(f"- {t}")
    L.append("")
    # 新闻：按追踪项检索，主体最多 max_main 条
    L += news_markdown(extra.get("news"), main_body=True)
    # 接下来：两周日历前两天 + 新的 EDGAR 文件
    L.append("### 接下来")
    for c in (cal or [])[:2]:
        L.append(f"- {c}")
    today = datetime.now(ET).date()
    for s_ in sec:
        if s_.get("form") and s_.get("date") and (today - date.fromisoformat(str(s_["date"]))).days <= 2:
            L.append(f"- EDGAR 新文件：{s_['name']} {s_['form']}（{md(s_['date'])}）")
    if not cal:
        L.append("- 两周内没有登记的事件")
    if slot == "am" and imgs:
        L.append("")
        L.append(f"IMG:{imgs[0]}")
    # ---- 折叠区
    L.append("")
    L.append(f"### 全部 {sum(1 for i in inds if not i['manual'])} 项自动指标（折叠）")
    L.append("")
    L.append(f"🟢 {vd['g']}　🟡 {vd['y']}　🔴 {vd['r']}" + (f"　⚪ {vd['na']}" if vd["na"] else "") + f"　净分 {vd['score']:+d}")
    for p in PILLARS:
        grp = [i for i in inds if i["pillar"] == p and not i["manual"]]
        if not grp:
            continue
        grp.sort(key=lambda i: {"R": 0, "Y": 1, "G": 2, "NA": 3}[i["status"]])
        L.append("")
        L.append(f"**{p}**")
        for i in grp:
            L.append(f"{STATUS_EMO[i['status']]} {i['name']}：{i['value']}")
    rx = extra.get("reactions") or []
    if rx:
        recent = sorted(rx, key=lambda r: r["day"], reverse=True)[:8]
        L.append("")
        L.append("近期财报次日相对 SMH：" + "，".join(f"{r['ticker']} {fmt_pct(r['ex'], 0)}" for r in recent))
    if slot == "am" and len(imgs) > 3:
        L.append("")
        L.append("下面三张图写了每项的救场线和坏信号线：")
        for im in imgs[1:4]:
            L.append(f"IMG:{im}")
    L.append("")
    L.append("### 人工判断和重点事件（折叠）")
    L.append("")
    for i in [i for i in inds if i["manual"]]:
        L.append(f"{STATUS_EMO[i['status']]} **{i['name']}**：{i['value']}")
    L.append("")
    for k in kev:
        if k["dd"] < 0 and not k.get("result"):
            continue
        L.append(f"**{k['when']}（{k['tag']}）{k['short']}**")
        L.append(f"救场：{k['rescue']}")
        L.append(f"坏信号：{k['bad']}")
        if k.get("result"):
            L.append(k["result"])
        L.append("")
    for s_ in sec:
        if s_.get("form"):
            L.append(f"EDGAR {s_['name']}：最新 {s_['form']}（{md(s_['date'])}），{s_['meaning']}")
        else:
            L.append(f"EDGAR {s_['name']}：{s_['meaning']}")
    if len(cal or []) > 2:
        L.append("")
        L.append("两周日历：")
        for c in cal[2:]:
            L.append(f"- {c}")
    L.append("")
    L += news_markdown(extra.get("news"), main_body=False)
    if slot == "am" and len(imgs) > 4:
        L.append("")
        L.append("### 走势图（折叠）")
        L.append("")
        for im in imgs[4:]:
            L.append(f"IMG:{im}")
            L.append("")
    L.append("")
    L.append("### 说明")
    L.append("")
    L.append(f"{now_hkt} 香港时间生成，美股截至 {md(extra.get('last_day'))} 收盘。行情 yfinance，利差 FRED，台股证交所，PB 高盛周报，GPU 租金 Compute Desk，文件 SEC EDGAR，新闻 Google News（只列一线媒体）和本地卖方邮件。")
    if errors:
        L.append("数据缺口：" + "；".join(errors[:4]))
    return "\n".join(L)


# ------------------------------------------------------------------ main flow
def cmd_run(args):
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    now_hkt = datetime.now(HKT)
    slot = args.slot
    if slot == "auto":
        slot = "am" if 4 <= now_hkt.hour < 15 else "pm"
    data = Data(cfg)
    log("start")
    data.load_prices()           # 失败直接抛错 → 退出码 1
    log("prices")
    data.load_fred()
    log("fred")
    inds, extra = compute(cfg, data, slot)
    conds = conditions(inds, extra)
    vd = verdict(inds, conds, extra.get("n_root", 0), extra=extra)
    last = load_json(STATE_DIR / "last.json", None)
    flips, last_hkt = diff_last(inds, last)
    snaps = snapshots(cfg, data, slot)
    log("snapshots")
    cal = build_calendar(cfg, extra)
    extra["key_events"] = build_key_events(cfg, extra)
    sec = sec_latest(cfg)
    log("sec")
    news = None
    if not getattr(args, "no_news", False):
        try:
            last_ts = (last or {}).get("generated_ts") or _hkt_stamp_ts((last or {}).get("generated_hkt"), now_hkt)
            news = news_fetch(cfg, datetime.now(ZoneInfo("UTC")), last_ts, data.errors)
        except Exception as e:  # noqa: BLE001  新闻失败不影响推送
            data.errors.append(f"新闻检索出错：{str(e)[:60]}")
    extra["news"] = news
    log("news")

    stamp = now_hkt.strftime("%Y-%m-%d") + f"_{slot}"
    od = OUT_DIR / stamp
    od.mkdir(parents=True, exist_ok=True)
    setup_fonts()
    slot_cn = "早报" if slot == "am" else "晚报"
    header = {"title": f"{cfg['title']}{slot_cn}",
              "when": f"{now_hkt:%m/%d %H:%M} 香港｜美股截至 {md(extra['last_day'])} 收盘",
              "sub": f"{now_hkt:%Y-%m-%d %H:%M} 香港时间｜美股截至 {md(extra['last_day'])} 收盘｜自动指标 {vd['g']+vd['y']+vd['r']} 项 + 人工 {len(cfg.get('manual_items', []))} 项"}
    sm = extra.get("smh") or {}
    floor_txt = (f"SMH {sm['close']:.1f}，距底线 {sm['floor']:.1f} 还有 {fmt_pct(sm['to_floor'])}" if sm else "")
    layout = getattr(args, "layout", "") or cfg.get("layout", "compact")
    first = "1_smh.png" if layout == "compact" else "1_overview.png"
    imgs = [od / n for n in (first, "2_detail_a.png", "3_detail_b.png", "4_detail_c.png",
                             "5_chart_price.png", "6_chart_rates.png", "7_chart_positioning.png",
                             "8_chart_ai_gpu.png")]
    if layout == "compact":
        if slot == "am":   # 晚报只发文字，不画图
            arr_p, smh_p = od / "1a_arr.png", od / "1b_smh.png"
            draw_smh_hero(cfg, data, smh_p)
            if draw_arr_panel(inds, arr_p):
                stack_images([arr_p, smh_p], imgs[0])   # 主体一张图：上实验室 ARR，下 SMH
            else:
                import shutil
                shutil.copy(smh_p, imgs[0])
            draw_details(inds, ["价格", "基本面"], "明细 1/3：价格、基本面", imgs[1])
            draw_details(inds, ["融资", "宏观"], "明细 2/3：融资、宏观", imgs[2])
            draw_details(inds, ["仓位"], "明细 3/3：仓位与资金", imgs[3])
            draw_chart_groups(cfg, data, extra, imgs[4:])
    else:
        draw_overview(inds, vd, conds, header, floor_txt, imgs[0])
        draw_details(inds, ["价格", "基本面"], "明细 1/3：价格、基本面", imgs[1])
        draw_details(inds, ["融资", "宏观"], "明细 2/3：融资、宏观", imgs[2])
        draw_details(inds, ["仓位"], "明细 3/3：仓位与资金", imgs[3])
        draw_chart_groups(cfg, data, extra, imgs[4:])
    errors = list(dict.fromkeys(data.errors))
    builder = build_markdown_compact if layout == "compact" else build_markdown
    mdtext = builder(cfg, slot, inds, vd, conds, flips, last_hkt, snaps, cal, sec, extra,
                     [str(x) for x in imgs], errors, f"{now_hkt:%m/%d %H:%M}")
    if news and len(mdtext) > 18000:   # 飞书卡片约 30KB 上限；Google News 链接很长，超长时折叠区去掉链接
        news["fold_links"] = False
        mdtext = builder(cfg, slot, inds, vd, conds, flips, last_hkt, snaps, cal, sec, extra,
                         [str(x) for x in imgs], errors, f"{now_hkt:%m/%d %H:%M}")
    (od / "card.md").write_text(mdtext, encoding="utf-8")
    if news:
        selected, folded = news_pick(news)
        write_json(od / "news.json", {"since": news["since"].isoformat(), "span_h": news["span_h"],
                                      "queries": news["queries"], "failed": news["failed"],
                                      "ranking_version": news.get("ranking_version"), "coverage": news.get("coverage", []),
                                      "selection": [{"topic": t["id"], "title": it["title"], "link": it.get("link"),
                                                     "importance": it.get("importance", 0), "reasons": it.get("reasons", []),
                                                     "placement": place}
                                                    for t, it, place in ([(t, it, "main") for t, it in selected]
                                                        + [(t, it, "fold") for t, its in folded for it in its])],
                                      "topics": [dict(t, items=[dict(i, ts=i["ts"].isoformat()) for i in t["items"]])
                                                 for t in news["topics"]]})
    snap = {"generated_hkt": f"{now_hkt:%m/%d %H:%M}", "generated_ts": now_hkt.timestamp(), "slot": slot, "verdict": vd, "conditions": conds,
            "indicators": inds, "errors": errors, "last_day": str(extra["last_day"])}
    write_json(od / "snapshot.json", snap)

    result = {"status": "generated", "slot": slot, "dir": str(od), "verdict": vd["text"],
              "g": vd["g"], "y": vd["y"], "r": vd["r"], "na": vd["na"], "conditions_met": vd["met"],
              "flips": len(flips), "errors": errors}
    if args.send:
        title = f"{header['title']}｜{vd['emoji']} {vd['text']}"
        if layout == "compact":
            header["sub"] = f"{now_hkt:%m/%d %H:%M} 香港｜美股截至 {md(extra['last_day'])} 收盘"
        cmd = [sys.executable, "-I", str(SEND_CARD), "--chat-id", args.chat_id or cfg["chat_id"], "--title", title,
               "--subtitle", header["sub"], "--header-template", vd["template"], "--tag", "半导体",
               "--tag-color", "indigo", "--file", str(od / "card.md"), "--json"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        try:
            sr = json.loads(p.stdout)
        except Exception:
            sr = {"status": "error", "raw": (p.stdout + p.stderr)[-800:]}
        result["send"] = {"status": sr.get("status"), "message_id": sr.get("message_id"), "rc": p.returncode,
                          "error": sr.get("error") or (p.stderr[-400:] if p.returncode else None)}
        if p.returncode != 0 or sr.get("status") != "ok":
            result["status"] = "send_failed"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        result["status"] = "sent"
        # 只有发送成功才更新“上次”基准，保证变化对比的是群里看到的上一条
        write_json(STATE_DIR / "last.json", snap)
        news_seen_update(news)
        with open(STATE_DIR / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": snap["generated_hkt"], "slot": slot, "verdict": vd,
                                "status": {i["id"]: i["status"] for i in inds}}, ensure_ascii=False, default=str) + "\n")
    elif args.update_baseline:
        write_json(STATE_DIR / "last.json", snap)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
          f"{result['status']}：{vd['text']} 🟢{vd['g']} 🟡{vd['y']} 🔴{vd['r']}，确认 {vd['met']}/5 → {od}")
    return 0


def cmd_self_test(args):
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    checks = {}
    checks["config_keys"] = all(k in cfg for k in ("chat_id", "thresholds", "universe", "fomc", "manual_items"))
    checks["hike_contract"] = hike_contract_ticker(cfg, date(2026, 11, 3))[0] == "ZQF27.CBT"
    checks["grade_low_better"] = grade(3.1, 3.2, 3.5, False) == "G" and grade(3.6, 3.2, 3.5, False) == "R"
    checks["grade_high_better"] = grade(55, 50, 20, True) == "G" and grade(10, 50, 20, True) == "R"
    t = datetime(2026, 11, 3, 16, 0, tzinfo=ET)
    days = [date(2026, 11, 3), date(2026, 11, 4)]
    checks["reaction_amc"] = reaction_day(t, days) == date(2026, 11, 4)
    checks["reaction_bmo"] = reaction_day(datetime(2026, 11, 3, 7, 0, tzinfo=ET), days) == date(2026, 11, 3)
    checks["send_script"] = SEND_CARD.exists()
    checks["pb_csv"] = PB_WEEKLY.exists()
    checks["pb_daily_csv"] = PB_DAILY.exists()
    checks["mail_db"] = MAIL_DB.exists()
    checks["ai_threshold_keys"] = all(k in cfg["thresholds"] for k in ("gpu_bw_green", "gpu_hopper_red", "ai_pct_green", "tmt_flow_green", "cftc_pct_red", "ai_short_red"))
    nc = cfg.get("news") or {}
    checks["news_config"] = bool(nc.get("topics")) and all(t.get("id") and t.get("label") and t.get("queries")
                                                           for t in nc["topics"])
    try:
        for t in nc.get("topics", []):
            for r in t.get("require", []) + t.get("mail_require", []) + t.get("review_require", []) + ([t["boost"]] if t.get("boost") else []):
                re.compile(r)
        for rule in nc.get("importance_rules", []):
            for r in rule["require"]:
                re.compile(r)
        checks["news_regex"] = True
    except re.error:
        checks["news_regex"] = False
    checks["news_links"] = all(_news_link_state(cfg, t["link"])[0] for t in nc.get("topics", []) if t.get("link"))
    sample = (b'<rss><channel><item><title>Anthropic ARR tops $80 billion - WSJ</title><link>https://x/1</link>'
              b'<pubDate>Fri, 06 Nov 2026 04:36:00 GMT</pubDate><source url="https://www.wsj.com">WSJ</source></item>'
              b'</channel></rss>')
    it = gn_parse(sample)
    checks["news_parse"] = len(it) == 1 and it[0]["title"] == "Anthropic ARR tops US$80 billion" and it[0]["source"] == "WSJ"
    checks["news_dollar"] = _clean_title("raise US$50bn and $5B - Reuters", "Reuters") == "raise US$50bn and US$5B"
    checks["news_stamp"] = _hkt_stamp_ts("11/06 21:00", datetime(2026, 11, 7, 8, 0, tzinfo=HKT)) == \
        datetime(2026, 11, 6, 21, 0, tzinfo=HKT).timestamp()
    checks["news_dup"] = _near_dup("Anthropic IPO valuation under pressure after AI rout",
                                   "Anthropic IPO Valuation Under Pressure After Tech Rout") and \
        not _near_dup("Anthropic IPO valuation under pressure", "Texas governor pauses data center permits")
    ok = all(checks.values())
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--slot", default="auto", choices=["auto", "am", "pm"])
    r.add_argument("--send", action="store_true", help="发送到飞书（默认 Semi Sentiment 群）")
    r.add_argument("--dry-run", action="store_true", help="只生成文件（默认行为）")
    r.add_argument("--chat-id", default="", help="覆盖接收群，测试用")
    r.add_argument("--update-baseline", action="store_true", help="不发送也更新变化对比基准")
    r.add_argument("--json", action="store_true")
    r.add_argument("--layout", default="", choices=["", "compact", "full"], help="卡片版式，默认读配置 layout")
    r.add_argument("--no-news", action="store_true", help="跳过新闻检索（测试用）")
    sub.add_parser("self-test")
    a = ap.parse_args()
    try:
        rc = cmd_run(a) if a.cmd == "run" else cmd_self_test(a)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": str(e), "hint": "先跑 self-test；行情失败多半是 yfinance 或代理问题，稍后重跑",
                          "trace": traceback.format_exc()[-1500:]}, ensure_ascii=False))
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
