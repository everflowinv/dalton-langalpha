#!/usr/bin/env python3
"""KWEB 相对强弱看板。

每个标的画一张「相对 KWEB 的 Wilder RSI30」和「此后 3 个月涨跌」的对照图，
再按该标的自身 RSI30 五分位统计未来 3 个月的中位回报和胜率。
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from openclaw_workspace import workspace_root as resolve_workspace

from typing import Any

if __name__ == "__main__" and "--json" in sys.argv:
    warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

VERSION = "1.0.0"
SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "references" / "kweb_config.json"
DEFAULT_OUTPUT = resolve_workspace(__file__) / "research-outputs" / "kweb-dashboard" / "index.html"

# 图注只写两件事：两根轴是什么、当前读数多少。理由和取舍写进 methodology.md。
NOTE_MAX_CHARS = 100
# RSI 中性位是 50，未来回报的中性位是 0。两轴各自 auto-fit 时这两条线会错位，
# 读者得先在脑子里配准才能判断「相对强弱已经转弱、但此后确实跑输了」。把左轴 50
# 钉到右轴 0 上，中性线合成一条。
ALIGN_50_TO_ZERO: tuple[float, float] = (50.0, 0.0)
QUINTILE_LABELS = ["Q1 最冷", "Q2", "Q3", "Q4", "Q5 最热"]
COLD_RSI = 30.0
HOT_RSI = 70.0
# RSI30 作用在比值上时高度均值回复，30/70 一年也未必碰一次。所以再加一条
# 「自身历史分位」触发，5%/95% 出提示，否则整个看板常年一个 chip 都没有。
COLD_PCTL = 5.0
HOT_PCTL = 95.0
# 七张图共用一套配色：同一个含义在哪张图上都是同一个颜色，翻页时不用重新认图例。
# 两个色相拉开，避免同一张图上两条线看着一样。self-test 的 palette_* 盯着这条。
RSI_COLOR = "#4da3ff"
FWD_REL_COLOR = "#ffbf3c"


# ─────────────────────────── 基础计算 ───────────────────────────

def wilder_rsi(series: pd.Series, period: int = 30) -> pd.Series:
    """Wilder 平滑 RSI，和 sentiment-dashboard 同一口径。"""
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_down = down.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_up / avg_down.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # 只涨不跌时 avg_down=0，rs 是无穷，直接定为 100；两边都是 0（完全不动）保持 NaN，
    # 不编造一个 50 出来。
    rsi = rsi.mask((avg_down == 0.0) & (avg_up > 0.0), 100.0)
    return rsi.where(avg_down.notna())


def finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def fmt(value: float | None, digits: int = 1, suffix: str = "") -> str:
    return "--" if value is None else f"{value:.{digits}f}{suffix}"


def fmt_pct(value: float | None, digits: int = 1) -> str:
    """比例转百分数，带正负号。"""
    return "--" if value is None else f"{value * 100:+.{digits}f}%"


def iso_date(idx: Any) -> str:
    return pd.Timestamp(idx).strftime("%Y-%m-%d")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"缺少配置 {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_constituents(name: str) -> dict[str, Any]:
    """读钉住的成分快照。

    成分不在 build 时联网抓：维基页面结构会变、UA 不对直接 403。快照由
    `run.sh refresh-constituents` 显式刷新，带 as_of，页面上标出来。
    """
    path = SKILL_DIR / "references" / name
    if not path.exists():
        raise FileNotFoundError(
            f"缺少成分快照 {path}，先跑 run.sh refresh-constituents"
        )
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not snapshot.get("members"):
        raise RuntimeError(f"成分快照 {path} 里没有成分")
    return snapshot


def basket_specs(section: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把成分快照摊成一组「子行业篮子 vs 基准」的 spec。"""
    cfg = section["basket"]
    snapshot = load_constituents(cfg["constituents"])
    field = cfg.get("group_field", "sub_industry")
    labels = snapshot.get("labels", {})
    groups: dict[str, list[dict[str, Any]]] = {}
    for member in snapshot["members"]:
        groups.setdefault(member[field], []).append(member)
    order = [g for g in snapshot.get("order", []) if g in groups]
    order += [g for g in sorted(groups) if g not in order]

    # 配置里点名不出图的子行业。写在 config 而不是删快照——refresh-constituents
    # 每次从 GICS 重建快照，删过的组下次又会长回来。
    excluded = list(cfg.get("exclude_groups", []))
    unknown = [g for g in excluded if g not in groups]
    if unknown:
        # GICS 改过分类名就会走到这里。静默 no-op 比报错更糟：图会突然多出来。
        raise RuntimeError(
            f"exclude_groups 里的 {unknown} 不在成分快照里，"
            "可能是 GICS 改了子行业名称，核对后再更新 kweb_config.json"
        )
    order = [g for g in order if g not in excluded]

    bench = section["benchmark"]
    specs: list[dict[str, Any]] = []
    for key in order:
        members = groups[key]
        specs.append(
            {
                "id": re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper(),
                "name": labels.get(key, key),
                "numerator": key,
                "denominator": bench,
                "kind": "basket",
                "members": members,
                "weighting": cfg.get("weighting", "cap"),
                # 单名子行业不是一个组合，就是那一只股票。标出来，别让人当行业读。
                "single_name": len(members) == 1,
            }
        )
    return specs, snapshot


def fetch_prices(tickers: list[str], start: str) -> dict[str, pd.Series]:
    """取复权收盘价（含股息），auto_adjust=False 后显式用 Adj Close。

    不用 auto_adjust=True：那会把 Adj Close 覆盖回 Close，后面再叠股息就重复计算。
    """
    import yfinance as yf

    wanted = sorted(set(tickers))
    out: dict[str, pd.Series] = {}

    def clean(series: pd.Series) -> pd.Series:
        series = pd.to_numeric(series, errors="coerce").dropna()
        series.index = pd.to_datetime(series.index).tz_localize(None)
        return series.sort_index()

    # 批量下载：金融板块那节要 77 个代码，逐个 download 要跑好几分钟。
    # 批量拿不到的再逐个补，不让一个坏代码拖垮整批。
    for chunk in [wanted[i : i + 40] for i in range(0, len(wanted), 40)]:
        if len(chunk) == 1:
            continue
        frame = yf.download(
            chunk, start=start, auto_adjust=False, progress=False,
            threads=True, group_by="column",
        )
        if frame is None or frame.empty or not isinstance(frame.columns, pd.MultiIndex):
            continue
        level = "Adj Close" if "Adj Close" in frame.columns.get_level_values(0) else "Close"
        block = frame[level]
        for ticker in chunk:
            if ticker in block.columns:
                series = clean(block[ticker])
                if not series.empty:
                    out[ticker] = series

    for ticker in wanted:
        if ticker in out:
            continue
        frame = yf.download(
            ticker, start=start, auto_adjust=False, progress=False, threads=False
        )
        if frame is None or frame.empty:
            out[ticker] = pd.Series(dtype=float)
            continue
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        column = "Adj Close" if "Adj Close" in frame.columns else "Close"
        out[ticker] = clean(frame[column])
    return out


def build_basket(
    members: list[dict[str, Any]], prices: dict[str, pd.Series], shares: dict[str, float]
) -> tuple[pd.Series, list[str]]:
    """子行业篮子的市值加权序列。

    市值权重用当期股本 × 复权收盘价，回溯到窗口起点。这不是标普官方子行业指数：
    成分和股本都是今天这一份，历史上被剔除的公司不在里面，回购造成的股本变化也没有。
    等权更简单，但和 XLF 的市值加权口径对不上——2023-01 起全板块篮子对 XLF 的
    日收益相关，市值加权 0.984、等权 0.969，跟踪误差 3.4% vs 5.0%，所以用市值加权。
    """
    missing: list[str] = []
    frame: dict[str, pd.Series] = {}
    weights: dict[str, float] = {}
    for member in members:
        ticker = member["yf"]
        series = prices.get(ticker, pd.Series(dtype=float))
        weight = shares.get(ticker)
        if series.empty or not weight:
            missing.append(ticker)
            continue
        frame[ticker] = series
        weights[ticker] = float(weight)
    if not frame:
        return pd.Series(dtype=float), missing
    # ffill 只补成分之间的零星缺口，不外推到第一个报价之前。
    panel = pd.DataFrame(frame).ffill()
    weighted = panel.mul(pd.Series(weights), axis=1)
    return weighted.sum(axis=1, min_count=1).dropna().rename("basket"), missing


def build_ratio(
    numerator: pd.Series, denominator: pd.Series, hk_leg: bool
) -> pd.Series:
    """比值序列。

    港股腿和美股腿的交易日历不同：3690.HK 有美股假期，KWEB 有港股假期。直接 dropna
    会把两边任意一方休市的日子全丢掉，序列被打得稀碎。所以先 ffill 补齐，再只保留
    基准（美股）有报价的交易日——比值的时间轴始终以基准为准。
    """
    frame = pd.concat({"num": numerator, "den": denominator}, axis=1)
    if hk_leg:
        frame = frame.ffill().dropna()
        frame = frame.loc[frame.index.isin(denominator.index)]
    else:
        frame = frame.dropna()
    if frame.empty:
        return pd.Series(dtype=float)
    return (frame["num"] / frame["den"]).rename("ratio")


def pair_frame(
    numerator: pd.Series,
    denominator: pd.Series,
    hk_leg: bool,
    rsi_period: int,
    horizon: int,
) -> pd.DataFrame:
    """一个标的的全部中间量：比值、RSI、未来绝对/相对回报。"""
    ratio = build_ratio(numerator, denominator, hk_leg)
    if ratio.empty:
        return pd.DataFrame()
    frame = pd.concat({"num": numerator, "den": denominator}, axis=1)
    frame = frame.ffill() if hk_leg else frame
    frame = frame.reindex(ratio.index)
    out = pd.DataFrame(index=ratio.index)
    out["ratio"] = ratio
    out["rsi"] = wilder_rsi(ratio, rsi_period)
    out["fwd_abs"] = frame["num"].shift(-horizon) / frame["num"] - 1.0
    out["fwd_rel"] = ratio.shift(-horizon) / ratio - 1.0
    return out


def apply_window(frame: pd.DataFrame, start: str | None) -> pd.DataFrame:
    """截到展示/统计窗口。

    必须在 pair_frame 之后调用，不能改成只取窗口内的价格再算：RSI30 需要 30 期暖机，
    从窗口第一天开始算的话头一个半月是 NaN，图和分位统计都会缺一截。前瞻回报同理，
    它向后取值，先算再截不影响窗口内每一天的读数。
    """
    if not start:
        return frame
    return frame.loc[frame.index >= pd.Timestamp(start)]


def bucket_stats(frame: pd.DataFrame, horizon: int) -> list[dict[str, Any]]:
    """按标的自身 RSI30 五分位分桶。

    不用固定的 30/40/60/70 切法：RSI30 作用在比值上时读数挤在 40–60，固定分桶会出现
    n=2 的极端桶，看着像结论其实是两天。五分位保证每桶样本相当。
    """
    valid = frame.dropna(subset=["rsi", "fwd_abs", "fwd_rel"])
    if len(valid) < 5 * horizon:
        return []
    try:
        buckets, edges = pd.qcut(
            valid["rsi"], 5, labels=QUINTILE_LABELS, retbins=True
        )
    except ValueError:
        return []
    rows: list[dict[str, Any]] = []
    for index, label in enumerate(QUINTILE_LABELS):
        group = valid[buckets == label]
        if group.empty:
            continue
        rows.append(
            {
                "label": label,
                # 保存 qcut 的连续分界点，不保存各组样本的实际极值。离散样本的
                # 相邻极值之间会有窄缝；当前 RSI 落进窄缝时，旧代码匹配不到
                # 任何桶并错误兜底到 Q5。
                "rsi_lo": float(edges[index]),
                "rsi_hi": float(edges[index + 1]),
                "n": int(len(group)),
                "independent": len(group) / horizon,
                "abs_median": float(group["fwd_abs"].median()),
                "rel_median": float(group["fwd_rel"].median()),
                "rel_hit": float((group["fwd_rel"] > 0).mean()),
            }
        )
    return rows


def current_bucket(rows: list[dict[str, Any]], rsi_now: float | None) -> dict[str, Any] | None:
    if rsi_now is None or not rows:
        return None
    # qcut 默认是右闭区间：(lo, hi]；首尾向外延伸，当前值即使刷新后超出
    # 统计样本的历史极值，也应分别归 Q1 / Q5。
    for row in rows[:-1]:
        if rsi_now <= row["rsi_hi"]:
            return row
    return rows[-1]


def bucket_range_text(row: dict[str, Any], index: int, total: int) -> str:
    """把连续、右闭的 qcut 区间写清楚，避免整数取整造成视觉冲突。"""
    lo = float(row["rsi_lo"])
    hi = float(row["rsi_hi"])
    if index == 0:
        return f"≤{hi:.1f}"
    if index == total - 1:
        return f">{lo:.1f}"
    return f"{lo:.1f} < RSI ≤ {hi:.1f}"


def spearman(x: pd.Series, y: pd.Series) -> float | None:
    """RSI 与未来回报的秩相关。负=均值回复，正=动量。"""
    both = pd.concat({"x": x, "y": y}, axis=1).dropna()
    if len(both) < 30:
        return None
    value = both["x"].rank().corr(both["y"].rank())
    return finite(value)


# ─────────────────────────── HTML 组件 ───────────────────────────

def series_payload(
    series: pd.Series, name: str, color: str, axis: str = "left",
    width: float = 1.8, max_points: int = 1200,
) -> dict[str, Any]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if max_points > 0 and len(clean) > max_points:
        step = math.ceil(len(clean) / max_points)
        clean = clean.iloc[::step]
    return {
        "name": name,
        "color": color,
        "axis": axis,
        "width": float(width),
        "data": [[iso_date(idx), round(float(val), 5)] for idx, val in clean.items()],
    }


def hline(value: float, axis: str = "left", label: str = "", tone: str = "zero") -> dict[str, Any]:
    return {"value": float(value), "axis": axis, "label": label, "tone": tone}


def chart(
    title: str,
    series: list[dict[str, Any]],
    note: str = "",
    height: int = 340,
    hlines: list[dict[str, Any]] | None = None,
    align: tuple[float, float] | None = None,
) -> str:
    payload = {
        "title": title,
        "series": series,
        "hlines": hlines or [],
        "align": None if align is None else {"left": float(align[0]), "right": float(align[1])},
    }
    encoded = html.escape(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), quote=True)
    legend = "".join(
        f'<span><i style="background:{html.escape(s["color"])}"></i>{html.escape(s["name"])}</span>'
        for s in series
    )
    return (
        f'<article class="panel chart-panel"><div class="panel-head"><h3>{html.escape(title)}</h3>'
        f'<div class="legend">{legend}</div></div>'
        f'<div class="chart" style="height:{height}px" data-chart="{encoded}">'
        '<canvas></canvas><div class="tooltip"></div></div>'
        f'<p class="note">{html.escape(note)}</p></article>'
    )


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def chip(text: str, tone: str) -> str:
    return f'<span class="{tone}">{html.escape(text)}</span>'


def alert_row(chips: list[str]) -> str:
    if not chips:
        return ""
    return f'<div class="alerts on">{"".join(chips)}</div>'


def long_notes(doc: str, cap: int = NOTE_MAX_CHARS) -> list[str]:
    """脚注体检：超过 cap 字的图注进 warnings，防止解释文重新长回来。"""
    out: list[str] = []
    for raw in re.findall(r'<p class="note[^"]*">(.*?)</p>', doc, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if len(text) > cap:
            out.append(f"{text[:40]}…（{len(text)}字）")
    return out


# ─────────────────────────── 页面装配 ───────────────────────────

def section_specs(section: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """一节的标的清单。pairs 直接读配置，basket 从成分快照摊开。"""
    if section.get("basket"):
        return basket_specs(section)
    return list(section["pairs"]), None


def excluded_labels(
    config: dict[str, Any], snapshot: dict[str, Any] | None
) -> list[str]:
    """被 exclude_groups 挡掉的子行业，写成「中文名（成分代码）」给页面用。"""
    cfg = config.get("basket") or {}
    keys = list(cfg.get("exclude_groups", []))
    if not keys or not snapshot:
        return []
    labels = snapshot.get("labels", {})
    field = cfg.get("group_field", "sub_industry")
    out: list[str] = []
    for key in keys:
        syms = [m["symbol"] for m in snapshot["members"] if m.get(field) == key]
        name = labels.get(key, key)
        out.append(f'{name}（{"、".join(syms)}）' if syms else name)
    return out


def analyse(config: dict[str, Any]) -> dict[str, Any]:
    horizon = int(config["horizon_days"])
    rsi_period = int(config["rsi_period"])
    window_start = config.get("window_start")
    pairs, snapshot = section_specs(config)
    shares = (snapshot or {}).get("shares_outstanding", {})
    excluded_groups = excluded_labels(config, snapshot)

    tickers: list[str] = []
    for spec in pairs:
        if spec.get("kind") == "basket":
            tickers.extend(m["yf"] for m in spec["members"])
            tickers.append(spec["denominator"])
        else:
            tickers.extend([spec["numerator"], spec["denominator"]])
    prices = fetch_prices(tickers, config["history_start"])

    results: list[dict[str, Any]] = []
    warns: list[str] = []
    for spec in pairs:
        if spec.get("kind") == "basket":
            num, missing = build_basket(spec["members"], prices, shares)
            if missing:
                warns.append(f"{spec['name']}：{len(missing)} 只成分缺价或缺股本（{','.join(missing[:5])}）")
        else:
            num = prices.get(spec["numerator"], pd.Series(dtype=float))
        den = prices.get(spec["denominator"], pd.Series(dtype=float))
        if num.empty or den.empty:
            warns.append(f"{spec['name']}：{spec['numerator']} 或 {spec['denominator']} 取不到价格")
            continue
        hk_leg = spec["numerator"].endswith(".HK") or spec["denominator"].endswith(".HK")
        frame = apply_window(pair_frame(num, den, hk_leg, rsi_period, horizon), window_start)
        if frame.empty or frame["rsi"].dropna().empty:
            warns.append(f"{spec['name']}：{window_start} 起的比值序列为空")
            continue
        rsi = frame["rsi"].dropna()
        rsi_now = finite(rsi.iloc[-1])
        pctl = float((rsi <= rsi.iloc[-1]).mean() * 100.0) if rsi_now is not None else None
        rows = bucket_stats(frame, horizon)
        results.append(
            {
                "spec": spec,
                "frame": frame,
                "rsi": rsi,
                "rsi_now": rsi_now,
                "pctl": pctl,
                "buckets": rows,
                "current_bucket": current_bucket(rows, rsi_now),
                "ic_rel": spearman(frame["rsi"], frame["fwd_rel"]),
                "start": iso_date(frame.index[0]),
                "end": iso_date(frame.index[-1]),
                "n_signal": int(frame["fwd_rel"].notna().sum()),
                "extreme_cold": int((rsi <= COLD_RSI).sum()),
                "extreme_hot": int((rsi >= HOT_RSI).sum()),
            }
        )
    if not results:
        raise RuntimeError("没有任何标的算出结果")
    return {
        "id": config.get("id", "pairs"),
        "label": config.get("label", ""),
        "heading": config.get("heading", ""),
        "results": results,
        "warnings": warns,
        "horizon": horizon,
        "horizon_label": config.get("horizon_label") or f"{horizon}日",
        "rsi_period": rsi_period,
        "window_start": window_start,
        "snapshot": snapshot,
        "basket": config.get("basket"),
        "excluded_groups": excluded_groups,
    }


def pair_alerts(item: dict[str, Any]) -> list[str]:
    """冷蓝热黄，中性区不出 chip。"""
    chips: list[str] = []
    name = item["spec"]["name"]
    rsi_now, pctl = item["rsi_now"], item["pctl"]
    if rsi_now is None:
        return chips
    if rsi_now <= COLD_RSI:
        chips.append(chip(f"{name} 相对RSI30={fmt(rsi_now,1)} ≤ {COLD_RSI:.0f}", "cold"))
    elif rsi_now >= HOT_RSI:
        chips.append(chip(f"{name} 相对RSI30={fmt(rsi_now,1)} ≥ {HOT_RSI:.0f}", "hot"))
    elif pctl is not None and pctl <= COLD_PCTL:
        chips.append(chip(f"{name} 相对RSI30 自身历史{fmt(pctl,0)}分位（极冷）", "cold"))
    elif pctl is not None and pctl >= HOT_PCTL:
        chips.append(chip(f"{name} 相对RSI30 自身历史{fmt(pctl,0)}分位（极热）", "hot"))
    return chips


def pair_label(spec: dict[str, Any]) -> str:
    """图例和表头里的「分子/分母」。篮子用中文行业名，不用英文 GICS 原文。"""
    if spec.get("kind") == "basket":
        return f'{spec["name"]}/{spec["denominator"]}'
    return f'{spec["numerator"]}/{spec["denominator"]}'


def readings_panel(data: dict[str, Any]) -> str:
    """一节开头的当前读数表 + 冷热 chip。原来在总览页，总览删掉后并进各节。"""
    horizon = data["horizon"]
    rows: list[list[str]] = []
    chips: list[str] = []
    for item in data["results"]:
        spec, bucket = item["spec"], item["current_bucket"]
        sub = pair_label(spec)
        if spec.get("kind") == "basket":
            sub += f'｜{len(spec["members"])} 只' + ("（单名）" if spec.get("single_name") else "")
        rows.append(
            [
                f'<b>{html.escape(spec["name"])}</b><br><span class="sub">{html.escape(sub)}</span>',
                fmt(item["rsi_now"], 1),
                fmt(item["pctl"], 0, "%"),
                html.escape(bucket["label"]) if bucket else "--",
                fmt_pct(bucket["rel_median"]) if bucket else "--",
                fmt(bucket["rel_hit"] * 100, 0, "%") if bucket else "--",
                fmt(bucket["independent"], 1) if bucket else "--",
                fmt(item["ic_rel"], 2),
            ]
        )
        chips.extend(pair_alerts(item))
    body = table(
        ["标的", f'相对RSI{data["rsi_period"]}', "自身分位", "所在桶",
         f"该桶未来{horizon}日相对中位", "相对胜率", "独立观测", "秩相关"],
        rows,
    )
    note = (
        f"相对RSI{data['rsi_period']} 是「标的/基准」比值的 Wilder RSI；"
        f"后四列是该桶历史统计，非预测。"
    )
    return (
        '<article class="panel"><div class="panel-head"><h3>当前读数与所在分位桶</h3></div>'
        f"{alert_row(chips)}{body}"
        f'<p class="note">{html.escape(note)}</p></article>'
    )


def caveat_panel(data: dict[str, Any]) -> str:
    lines = []
    for item in data["results"]:
        spec = item["spec"]
        direction = "均值回复" if (item["ic_rel"] or 0) < -0.05 else (
            "动量" if (item["ic_rel"] or 0) > 0.05 else "无方向"
        )
        lines.append(
            f'<li><b>{html.escape(spec["name"])}</b>：{item["start"]} 起，'
            f'{item["n_signal"]} 个信号日（独立约 {item["n_signal"]/data["horizon"]:.1f} 段）；'
            f'秩相关 {fmt(item["ic_rel"],2)}，读作{direction}；'
            f'RSI≤{COLD_RSI:.0f} 出现 {item["extreme_cold"]} 天、'
            f'≥{HOT_RSI:.0f} 出现 {item["extreme_hot"]} 天。</li>'
        )
    head = [
        f'<li>图和统计都只用 {data["window_start"]} 起的数据。RSI{data["rsi_period"]} 用更早的价格暖机，'
        "所以窗口第一天就有读数，不是从中间才开始。</li>",
        f'<li>未来 {data["horizon"]} 个交易日 ≈ {data["horizon_label"]}。信号画在信号当天，'
        f'所以最后 {data["horizon"]} 天没有未来回报，右轴那条线提前收尾。</li>',
        "<li>图上只画相对涨跌；绝对涨跌的中位数留在每个标的的分位表里。</li>",
        "<li>未来回报按重叠窗口统计：相邻交易日的样本几乎完全重合，"
        "「独立观测」列已换算成不重叠的段数，读结论时以它为准，不是样本数。</li>",
        "<li>价格全部取 Adj Close（含股息），比值两腿口径一致。</li>",
    ]
    snapshot = data.get("snapshot")
    if snapshot:
        singles = [i["spec"]["name"] for i in data["results"] if i["spec"].get("single_name")]
        charted = sum(len(i["spec"].get("members", [])) for i in data["results"])
        head.append(
            f'<li><b>子行业篮子是自己拼的，不是标普官方子行业指数。</b>成分取 S&P 500 金融板块'
            f'{len(snapshot["members"])} 只、按 GICS 子行业分组，快照日期 {snapshot["as_of"]}；'
            "市值加权用的是当期股本。这份成分和股本被回溯用到窗口起点，"
            "所以带幸存者偏差，也没有回购造成的股本变化。</li>"
        )
        dropped = list(data.get("excluded_groups") or [])
        if dropped:
            head.append(
                f'<li>{len(data["results"])} 个子行业出图、覆盖 {charted} 只成分。'
                f'{"、".join(html.escape(d) for d in dropped)} 各只有 1 只成分，'
                "读的是个股不是行业，已经不出图。</li>"
            )
        head.append(
            "<li>口径核对：2023-01 起，全板块篮子对 XLF 的日收益相关 0.98、年化跟踪误差 3.4%，"
            "但累计回报 92.8% vs XLF 78.3%——差的这一截主要就是上面那条偏差，"
            "不要把篮子的绝对涨幅当成板块真实收益。</li>"
        )
        if singles:
            head.append(
                f'<li>{"、".join(html.escape(s) for s in singles)} 只有 1 只成分，'
                "画出来的是那只股票本身，不是一个行业组合。</li>"
            )
    head.append("<li>各标的的方向不一致，不能合成一条统一规则：</li>")
    return (
        '<article class="panel"><div class="panel-head"><h3>口径与限制</h3></div>'
        "<ul>" + "".join(head) + "</ul>"
        "<ul class=\"tight\">" + "".join(lines) + "</ul></article>"
    )


def pair_section(data: dict[str, Any]) -> str:
    horizon = data["horizon"]
    hlabel = data["horizon_label"]
    blocks: list[str] = [readings_panel(data)]
    for item in data["results"]:
        spec, frame = item["spec"], item["frame"]
        label = pair_label(spec)
        is_pair = spec.get("kind") == "pair"
        # 图上只留相对涨跌一条：绝对涨跌和它同轴同量纲，两条线大部分时间贴在一起，
        # 反而看不清相对那条。绝对中位保留在下面的分位表里。
        series = [
            series_payload(item["rsi"], f'{label} RSI{data["rsi_period"]}', RSI_COLOR, "left"),
            series_payload(
                frame["fwd_rel"] * 100,
                f"此后{horizon}日{'涨跌' if is_pair else '相对涨跌'}",
                FWD_REL_COLOR, "right",
            ),
        ]
        note = (
            f"左轴{label}的Wilder RSI{data['rsi_period']}，右轴此后{horizon}日相对涨跌，"
            f"左轴50与右轴0同高。{item['start']}起；当前RSI {fmt(item['rsi_now'],1)}；"
            f"末段{horizon}日未实现。"
        )
        chart_html = chart(
            f'{spec["name"]}：{label} RSI{data["rsi_period"]} + 此后{hlabel}表现',
            series,
            note,
            340,
            [
                hline(COLD_RSI, "left", f"超卖{COLD_RSI:.0f}", "cold"),
                hline(HOT_RSI, "left", f"超买{HOT_RSI:.0f}", "hot"),
                hline(0.0, "right", "中性 50/0", "zero"),
            ],
            ALIGN_50_TO_ZERO,
        )
        bucket_rows: list[list[str]] = []
        for index, row in enumerate(item["buckets"]):
            mark = " ◀ 当前" if item["current_bucket"] and row["label"] == item["current_bucket"]["label"] else ""
            bucket_rows.append(
                [
                    html.escape(row["label"] + mark),
                    bucket_range_text(row, index, len(item["buckets"])),
                    str(row["n"]),
                    fmt(row["independent"], 1),
                    fmt_pct(row["abs_median"]),
                    fmt_pct(row["rel_median"]),
                    fmt(row["rel_hit"] * 100, 0, "%"),
                ]
            )
        bucket_html = table(
            ["分位桶", "RSI区间", "样本日", "独立观测",
             f"未来{horizon}日绝对中位", f"未来{horizon}日相对中位", "相对胜率"],
            bucket_rows,
        ) if bucket_rows else '<p class="note">样本不足，不出分位统计。</p>'
        tail = ""
        if spec.get("kind") == "basket":
            names = "、".join(m["symbol"] for m in spec["members"])
            # 成分名单不是图注，用 .members 不用 .note——它是数据不是解释文，
            # 不该被 100 字上限砍掉。
            tail = f'<p class="members">成分（{len(spec["members"])} 只）：{html.escape(names)}</p>'
        elif spec.get("note"):
            tail = f'<p class="note">{html.escape(spec["note"])}</p>'
        blocks.append(
            f'<div class="stack">{chart_html}'
            f'<article class="panel"><div class="panel-head">'
            f'<h3>{html.escape(spec["name"])}：按自身RSI分位看此后{hlabel}</h3></div>'
            f'{alert_row(pair_alerts(item))}{bucket_html}{tail}'
            "</article></div>"
        )
    blocks.append(caveat_panel(data))
    return '<div class="stack big">' + "".join(blocks) + "</div>"


def render_html(
    sections: list[dict[str, str]],
    generated_at: str,
    window_start: str | None = None,
    title: str = "Richard 相对看板",
    subtitle: str = "",
) -> str:
    """页签就是配置里的 sections，没有总览页。

    总览是 08-10 之前的结构，删掉之后当前读数表和「口径与限制」并进了各节开头/结尾，
    没有丢东西。`no_overview_tab` 盯着别人把它加回来。
    """
    buttons = "".join(
        f'<button data-tab="{s["id"]}" class="tab-btn{" active" if i == 0 else ""}">'
        f'{html.escape(s["label"])}</button>'
        for i, s in enumerate(sections)
    )
    bodies = "".join(
        f'<main id="tab-{s["id"]}" class="tab-page{" active" if i == 0 else ""}">'
        f'<div class="section-title"><h1>{html.escape(s.get("heading") or s["label"])}</h1></div>'
        f'{s["body"]}</main>'
        for i, s in enumerate(sections)
    )
    css = r"""
:root{--bg:#07101f;--panel:#0f1b2d;--line:#253a57;--text:#edf4ff;--muted:#91a3bd;--blue:#4da3ff;--yellow:#ffbf3c}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -10%,#17335d 0,transparent 35%),var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}.top{padding:28px 32px 12px;max-width:1500px;margin:auto}.top h1{margin:0;font-size:28px}.top p{color:var(--muted);margin:8px 0}.tabs{position:sticky;top:0;z-index:20;display:flex;gap:8px;padding:10px 32px;background:rgba(7,16,31,.92);backdrop-filter:blur(16px);border-bottom:1px solid var(--line);overflow:auto}.tab-btn{border:1px solid var(--line);background:#0c1728;color:var(--muted);padding:9px 15px;border-radius:10px;cursor:pointer;white-space:nowrap}.tab-btn.active{background:var(--blue);border-color:var(--blue);color:#04101f;font-weight:700}.tab-page{display:none;max-width:1500px;margin:auto;padding:20px 32px 60px}.tab-page.active{display:block}.section-title h1,.section-title h2{font-size:22px;margin:0 0 16px}.section-title p{color:var(--muted);margin:0 0 16px}.portfolio-title{margin-top:44px;padding-top:24px;border-top:1px solid var(--line)}.stats{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:10px;margin:0 0 14px}.stats div{background:#0d1a2c;border:1px solid var(--line);border-radius:12px;padding:12px}.stats b,.stats span{display:block}.stats b{font-size:18px}.stats span{color:var(--muted);font-size:11px;margin-top:4px}.panel{background:linear-gradient(145deg,rgba(19,36,59,.96),rgba(12,25,43,.96));border:1px solid var(--line);border-radius:16px;box-shadow:0 12px 32px rgba(0,0,0,.18);padding:18px;min-width:0;margin-bottom:14px}.panel h3{margin:0 0 12px;font-size:17px}.panel p,.panel li{color:#c5d2e5;line-height:1.55}.panel ul{margin:0 0 4px;padding-left:20px;font-size:13px}.panel ul.tight li{margin-top:4px;color:var(--muted)}.panel-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.legend{display:flex;flex-wrap:wrap;gap:12px;color:var(--muted);font-size:12px}.legend span{white-space:nowrap}.legend i{display:inline-block;width:18px;height:3px;margin-right:5px;vertical-align:middle;border-radius:3px}.chart{position:relative;width:100%;margin-top:8px}.chart canvas{width:100%;height:100%;display:block}.tooltip{display:none;position:absolute;pointer-events:none;background:#050b15e8;border:1px solid #405777;border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.45;box-shadow:0 8px 24px #0008;white-space:nowrap}.note{font-size:12px!important;color:var(--muted)!important;margin:8px 0 0}.members{font-size:11px!important;color:#7f92ad!important;margin:10px 0 0;line-height:1.5;word-break:break-word}.alerts.on span{display:inline-block;border-radius:999px;padding:3px 10px;margin:0 6px 10px 0;font-size:12px;border:1px solid transparent}.alerts.on span.hot{background:#3a2a15;border-color:#8a5f24;color:#ffc98a}.alerts.on span.cold{background:#122a45;border-color:#2f5f8a;color:#9ccbff}.stale-banner{background:#342214;border:1px solid #8a5f24;color:#ffd099;border-radius:10px;padding:9px 13px;font-size:12.5px;margin:0 0 14px}.stack{display:grid;gap:14px}.stack.big{gap:28px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:600;background:#0b1728}td .sub{font-size:11px;color:var(--muted)}footer{max-width:1500px;margin:auto;padding:0 32px 30px;color:var(--muted);font-size:11px}@media(max-width:900px){.top,.tabs,.tab-page{padding-left:16px;padding-right:16px}.panel-head{display:block}.legend{margin-bottom:8px}.stats{grid-template-columns:repeat(2,minmax(130px,1fr))}}
"""
    js = r"""
const $=(s,r=document)=>r.querySelector(s);const $$=(s,r=document)=>[...r.querySelectorAll(s)];
$$('.tab-btn').forEach(b=>b.addEventListener('click',()=>{$$('.tab-btn').forEach(x=>x.classList.remove('active'));$$('.tab-page').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('#tab-'+b.dataset.tab).classList.add('active');setTimeout(()=>redraw($('#tab-'+b.dataset.tab)),20)}));
function niceStep(span,n){const raw=(Math.abs(span)||1)/n,p=Math.pow(10,Math.floor(Math.log10(Math.abs(raw)||1))),u=raw/p,s=u<=1?1:u<=2?2:u<=2.5?2.5:u<=5?5:10;return s*p}
function drawChart(el){const cv=$('canvas',el),ctx=cv.getContext('2d'),d=JSON.parse(el.dataset.chart),D=devicePixelRatio||1,w=el.clientWidth,h=el.clientHeight;if(w<20)return;cv.width=w*D;cv.height=h*D;ctx.setTransform(D,0,0,D,0,0);ctx.clearRect(0,0,w,h);const pad={l:58,r:58,t:14,b:28},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b;const all=d.series.flatMap(s=>s.data.map(p=>({t:+new Date(p[0]),v:+p[1],a:s.axis})));if(!all.length)return;const xmin=Math.min(...all.map(p=>p.t)),xmax=Math.max(...all.map(p=>p.t)),hasRight=d.series.some(s=>s.axis==='right'),hl=d.hlines||[];
function range(axis){const vals=all.filter(p=>p.a===axis).map(p=>p.v);hl.filter(l=>(l.axis||'left')===axis).forEach(l=>vals.push(l.value));if(!vals.length)return[0,1];const lo=Math.min(...vals),hi=Math.max(...vals),step=niceStep((hi-lo)||Math.abs(hi)||1,4);return[Math.floor(lo/step)*step,Math.ceil(hi/step)*step]}
function alignLeft(L,R,a){const rs=(R[1]-R[0]);if(!rs)return L;const p=(R[1]-a.right)/rs;if(!(p>1e-6&&p<1-1e-6))return L;const k=p/(1-p);let top=Math.max(L[1]-a.left,0),bot=Math.max(a.left-L[0],0);if(top<=0&&bot<=0)return L;bot=Math.max(bot,top/k);const step=niceStep(bot*(1+k),4);const span=Math.max(Math.ceil(bot*(1+k)/step)*step,step);const nb=span/(1+k);return[a.left-nb,a.left+k*nb]}
let L=range('left');const R=range('right');
if(d.align&&hasRight)L=alignLeft(L,R,d.align);const xp=t=>pad.l+(t-xmin)/(xmax-xmin||1)*pw,yp=(v,a)=>{const q=a==='right'?R:L;return pad.t+(q[1]-v)/(q[1]-q[0]||1)*ph};const dec=r=>{const s=Math.abs(r[1]-r[0])/4;return s<1?2:s<10?1:0},dl=dec(L),dr=dec(R);ctx.lineWidth=1;ctx.font='11px -apple-system,sans-serif';
for(let i=0;i<=4;i++){const y=pad.t+ph*i/4;ctx.strokeStyle='#1f3251';ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();const lv=L[1]-(L[1]-L[0])*i/4,rv=R[1]-(R[1]-R[0])*i/4;ctx.fillStyle='#8294ae';ctx.textAlign='right';ctx.fillText(lv.toFixed(dl),pad.l-7,y+4);if(hasRight){ctx.textAlign='left';ctx.fillText(rv.toFixed(dr),w-pad.r+7,y+4)}}
hl.forEach(l=>{const a=l.axis||'left',q=a==='right'?R:L;if(l.value<Math.min(q[0],q[1])||l.value>Math.max(q[0],q[1]))return;const y=yp(l.value,a),c=l.tone==='hot'?'#ff9d3c':l.tone==='cold'?'#4da3ff':'#93a7c2';ctx.save();ctx.strokeStyle=c+'99';ctx.setLineDash(l.tone==='zero'?[2,3]:[6,4]);ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();ctx.restore();if(l.label){ctx.font='10px -apple-system,sans-serif';ctx.fillStyle=c+'cc';ctx.textAlign='left';ctx.fillText(l.label,pad.l+5,y-4);ctx.font='11px -apple-system,sans-serif'}});
const spanDays=(xmax-xmin)/864e5;ctx.textAlign='center';ctx.fillStyle='#8294ae';for(let i=0;i<5;i++){const t=xmin+(xmax-xmin)*i/4,dt=new Date(t),y=dt.getFullYear(),m=String(dt.getMonth()+1).padStart(2,'0');ctx.fillText(spanDays>150?y+'/'+m:m+'/'+String(dt.getDate()).padStart(2,'0'),xp(t),h-8)}
d.series.forEach(s=>{const pts=s.data.map(p=>[xp(+new Date(p[0])),yp(+p[1],s.axis)]);if(!pts.length)return;ctx.strokeStyle=s.color;ctx.lineWidth=s.width||1.8;ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.stroke();const last=pts.at(-1);ctx.fillStyle=s.color;ctx.beginPath();ctx.arc(last[0],last[1],3.2,0,6.284);ctx.fill()});el._chart={d,xmin,xmax,xp,yp,pad,pw,ph,w,h,ctx};}
function hover(e){const el=e.currentTarget,c=el._chart;if(!c)return;const r=el.getBoundingClientRect(),mx=e.clientX-r.left,t=c.xmin+(mx-c.pad.l)/c.pw*(c.xmax-c.xmin);c.ctx.save();c.ctx.strokeStyle='#8ea5c4aa';c.ctx.setLineDash([4,4]);c.ctx.beginPath();c.ctx.moveTo(mx,c.pad.t);c.ctx.lineTo(mx,c.pad.t+c.ph);c.ctx.stroke();c.ctx.restore();const lines=[];c.d.series.forEach(s=>{if(!s.data.length)return;let best=s.data.reduce((a,b)=>Math.abs(+new Date(b[0])-t)<Math.abs(+new Date(a[0])-t)?b:a);lines.push(`<span style="color:${s.color}">●</span> ${s.name}: <b>${(+best[1]).toFixed(2)}</b>`)});const dt=new Date(t).toISOString().slice(0,10),tip=$('.tooltip',el);tip.innerHTML=`<b>${dt}</b><br>${lines.join('<br>')}`;tip.style.display='block';tip.style.left=Math.min(Math.max(mx+12,8),c.w-tip.offsetWidth-8)+'px';tip.style.top='20px';}
function leave(e){$('.tooltip',e.currentTarget).style.display='none';drawChart(e.currentTarget)}
function redraw(root){$$('.chart',root||document).forEach(drawChart)}
$$('.chart').forEach(el=>{drawChart(el);el.addEventListener('mousemove',e=>{drawChart(el);hover(e)});el.addEventListener('mouseleave',leave)});
addEventListener('resize',()=>redraw());
"""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{css}</style></head><body>"
        f'<header class="top"><h1>{html.escape(title)}</h1>'
        f"<p>{html.escape(subtitle)}</p></header>"
        f'<nav class="tabs">{buttons}</nav>{bodies}'
        f"<footer>生成时间 {html.escape(generated_at)}｜单文件离线 HTML｜"
        f"窗口 {html.escape(window_start or '全历史')} 至今｜"
        "价格取 Adj Close（含股息）｜重叠窗口统计，读独立观测不读样本数</footer>"
        f"<script>{js}</script></body></html>"
    )


def portfolio_section(
    state: dict[str, Any] | None, cfg: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
) -> str:
    """把虚拟组合最新状态、成交和完整评分输入放在 KWEB 页末尾。"""
    title = (
        '<section class="section-title portfolio-title"><h2>虚拟组合</h2>'
        '<p>每日最新仓位与历史累计 P&amp;L</p></section>'
    )
    if state is None:
        return (
            title + '<article class="panel"><p>尚未初始化。deploy 会创建初始目标；普通 build 不改仓位。</p></article>'
        )

    def money(value: Any) -> str:
        number = float(value or 0.0)
        return f"${number:,.0f}" if number >= 0 else f"-${abs(number):,.0f}"

    pending = state.get("pending", {})
    summary = (
        '<article class="panel"><p>港股按港股开盘成交；美股开盘用美股腿补齐，多空各50万美元。'
        '盘中净敞口允许漂移，佣金、滑点、借券费和税费暂按0。</p></article>'
        '<div class="stats">'
        f'<div><b>{money(state.get("equity_usd"))}</b><span>期末权益</span></div>'
        f'<div><b>{money(state.get("daily_mtm_pnl_usd"))}</b><span>过去一天 MTM</span></div>'
        f'<div><b>{money(state.get("cumulative_pnl_usd"))}</b><span>累计 P&amp;L</span></div>'
        f'<div><b>{money(state.get("last_us_open_gross_usd"))}</b><span>美股开盘 gross</span></div>'
        f'<div><b>{money(state.get("last_us_open_net_usd"))}</b><span>美股开盘 net</span></div>'
        f'<div><b>{html.escape(str(pending.get("signal_date", "--")))}</b><span>下一目标信号日</span></div>'
        '</div>'
    )

    history_rows = [
        [
            html.escape(str(row["signal_date"])),
            "初始化" if row.get("type") == "initialize" else "日结",
            money(row.get("daily_mtm_pnl_usd")),
            money(row.get("cumulative_pnl_usd")),
            money(row.get("equity_usd")),
        ]
        for row in reversed(history or [])
    ]
    history_html = (
        table(["日期", "记录", "当日 MTM", "累计 P&L", "期末权益"], history_rows)
        if history_rows else '<p class="note">尚无历史记录。</p>'
    )

    positions = state.get("last_positions", [])
    position_rows = [
        [
            html.escape(row.get("name", row["ticker"])), html.escape(row["ticker"]),
            html.escape(row.get("market", "")),
            "多" if float(row["shares"]) > 0 else "空",
            f'{abs(float(row["shares"])):,.4f}',
            money(row.get("market_value_usd")),
        ]
        for row in sorted(positions, key=lambda row: float(row.get("market_value_usd", 0.0)), reverse=True)
    ]
    if position_rows:
        current_html = table(["标的", "Ticker", "市场", "方向", "股数", "期末市值"], position_rows)
    else:
        current_html = '<p class="note">尚未成交；下一交易日按下方目标建仓。</p>'

    trade_rows = [
        [
            html.escape(row["date"]), html.escape(row["market"]), html.escape(row["ticker"]),
            html.escape(row["side"]), f'{abs(float(row["delta_shares"])):,.4f}',
            f'{float(row["open_price_local"]):,.4f}', money(row["target_notional_usd"]),
        ]
        for row in state.get("last_trades", [])
    ]
    trades_html = (
        table(["日期", "市场", "Ticker", "交易", "股数", "开盘价", "成交后目标"], trade_rows)
        if trade_rows else '<p class="note">初始化日没有成交。</p>'
    )

    names = {row["ticker"]: row["name"] for row in state.get("last_analysis", [])}
    target_rows = [
        [html.escape(names.get(ticker, ticker)), html.escape(ticker), "多" if value > 0 else "空", money(value)]
        for ticker, value in sorted(pending.get("target_usd", {}).items(), key=lambda item: item[1], reverse=True)
    ]
    target_html = table(["标的", "Ticker", "方向", "目标美元敞口"], target_rows) if target_rows else ""

    analysis_rows = [
        [
            html.escape(row["name"]), html.escape(row["ticker"]), fmt(row.get("rsi"), 1),
            fmt(row.get("percentile"), 1, "%"), fmt(row.get("ic_rel"), 3),
            fmt_pct(row.get("bucket_rel_median")), fmt(float(row.get("bucket_hit", 0.0)) * 100, 1, "%"),
            f'{float(row.get("score", 0.0)):+.3f}',
            {"long": "多", "short": "空", "flat": "中性"}.get(row.get("signal"), "中性"),
        ]
        for row in state.get("last_analysis", [])
    ]
    analysis_html = table(
        ["标的", "Ticker", "RSI30", "自身分位", "IC", "桶中位", "跑赢率", "综合分", "信号"],
        analysis_rows,
    ) if analysis_rows else ""

    return (
        title + summary
        + '<article class="panel"><h3>历史 P&amp;L</h3>' + history_html + '</article>'
        + '<article class="panel"><h3>更新后的仓位</h3>' + current_html + '</article>'
        + '<article class="panel"><h3>过去一天的交易</h3>' + trades_html + '</article>'
        + '<article class="panel"><h3>下一交易日目标</h3>' + target_html + '</article>'
        + '<article class="panel"><h3>完整决策审计</h3>' + analysis_html
        + '<p class="note">综合分=方向效果秩50%+桶中位秩25%+跑赢率秩25%；|分数|&lt;0.20不持仓。</p></article>'
    )


def _etf_last_success_path() -> Path:
    return resolve_workspace(__file__) / "state" / "kweb-dashboard" / "etf_last_success.json"


def _read_etf_last_success() -> str | None:
    path = _etf_last_success_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        as_of = str(data.get("as_of") or "").strip() if isinstance(data, dict) else ""
        return as_of or None
    except (OSError, ValueError, TypeError):
        return None


def _write_etf_last_success(as_of: str) -> None:
    clean = str(as_of or "").strip()
    if not clean:
        return
    path = _etf_last_success_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {"as_of": clean, "updated_at": datetime.now(timezone.utc).isoformat()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def etf_stale_banner(last_ok: str | None, reason: str) -> str:
    """Render the ETF stale/failure notice banner matching sentiment-dashboard's style."""
    as_of = str(last_ok or "").strip() or "未知"
    clean_reason = str(reason or "").strip() or "未知错误"
    text = f"⚠️ ETF 数据未更新（上次成功：{as_of}，原因：{clean_reason}），请注意时效。"
    return f'<p class="stale-banner">{html.escape(text)}</p>'


def build_etf_page(etf_cfg: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    """ETF 页单独成一个页签，放在配置里两节之后。

    取数失败只影响这一页：页签照常出，ETF 区块顶部显示“ETF 数据未更新”提示，
    不阻塞任务成功与其余面板发布；它不进 build 的 warnings，否则一只 ETF 停牌就会挡住虚拟组合推进。
    """
    page = {
        "id": etf_cfg.get("id", "etf"),
        "label": etf_cfg.get("label", "ETF"),
        "heading": etf_cfg.get("heading", "ETF 看板"),
    }
    board_mod: Any = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import kweb_etf_board as board_mod

        board_cfg = board_mod.load_etf_config()
        data = board_mod.analyse_board(board_cfg, resolve_workspace(__file__), wilder_rsi)
        etf_summary = board_mod.summary(data)
        body = board_mod.render_board(board_cfg, data)
        if etf_summary.get("ok"):
            success_as_of = (
                str(etf_summary.get("board_last") or "").strip()
                or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
            )
            _write_etf_last_success(success_as_of)
        else:
            last_ok = _read_etf_last_success()
            errors = [str(e).strip() for e in etf_summary.get("errors", []) if str(e).strip()]
            reason = "；".join(errors) if errors else "未知错误"
            body = etf_stale_banner(last_ok, reason) + body
        page["body"] = body
        return page, etf_summary
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        last_ok = _read_etf_last_success()
        if board_mod is not None and hasattr(board_mod, "error_body"):
            fallback_body = board_mod.error_body(message)
        else:
            fallback_body = (
                '<article class="panel"><h3>ETF 看板这次没生成</h3>'
                f'<p>{html.escape(message)}</p>'
                '<p class="note">其余两页不受影响；修好后重跑 build。</p></article>'
            )
        page["body"] = etf_stale_banner(last_ok, message) + fallback_body
        return page, {"ok": False, "errors": [f"ETF 看板生成失败：{message}"]}


def build(
    output: Path, config: dict[str, Any], update_portfolio: bool = False,
) -> dict[str, Any]:
    rendered: list[dict[str, str]] = []
    warns: list[str] = []
    sections_meta: list[dict[str, Any]] = []
    readings: list[dict[str, Any]] = []
    charts = 0
    windows: list[str] = []

    for section in config["sections"]:
        data = analyse(section)
        rendered.append(
            {
                "id": data["id"],
                "label": data["label"],
                "heading": data["heading"],
                "body": pair_section(data),
            }
        )
        warns.extend(f'{data["label"]}：{w}' for w in data["warnings"])
        charts += len(data["results"])
        if data.get("window_start"):
            windows.append(data["window_start"])
        sections_meta.append(
            {
                "id": data["id"],
                "label": data["label"],
                "charts": len(data["results"]),
                "horizon_days": data["horizon"],
                "rsi_period": data["rsi_period"],
                "window_start": data["window_start"],
                "constituents_as_of": (data.get("snapshot") or {}).get("as_of"),
            }
        )
        readings.extend(
            {
                "section": data["id"],
                "name": item["spec"]["name"],
                "pair": pair_label(item["spec"]),
                "members": len(item["spec"].get("members", [])) or None,
                "rsi": round(item["rsi_now"], 2) if item["rsi_now"] is not None else None,
                "percentile": round(item["pctl"], 1) if item["pctl"] is not None else None,
                "bucket": item["current_bucket"]["label"] if item["current_bucket"] else None,
                "bucket_rel_median": round(item["current_bucket"]["rel_median"], 4) if item["current_bucket"] else None,
                "bucket_hit": round(item["current_bucket"]["rel_hit"], 3) if item["current_bucket"] else None,
                "ic_rel": round(item["ic_rel"], 3) if item["ic_rel"] is not None else None,
                "start": item["start"],
                "end": item["end"],
            }
            for item in data["results"]
        )

    portfolio_result: dict[str, Any] | None = None
    portfolio_cfg = config.get("portfolio") or {}
    if portfolio_cfg.get("enabled"):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import kweb_portfolio

        if update_portfolio and not warns:
            portfolio_result = kweb_portfolio.portfolio_cycle(readings, portfolio_cfg)
        elif update_portfolio:
            portfolio_result = {
                "ok": False, "blocked": True,
                "error": "看板取数有 warnings，组合状态未推进",
            }
        state = kweb_portfolio.state_summary()
        kweb_page = next((page for page in rendered if page["id"] == "kweb"), None)
        if kweb_page is None:
            raise RuntimeError("找不到 KWEB 页，不能挂载虚拟组合")
        history = kweb_portfolio.ledger_history()
        kweb_page["body"] += portfolio_section(state, portfolio_cfg, history)

    etf_result: dict[str, Any] | None = None
    etf_cfg = config.get("etf_board") or {}
    if etf_cfg.get("enabled"):
        etf_page, etf_result = build_etf_page(etf_cfg)
        rendered.append(etf_page)

    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    window_start = min(windows) if windows else None
    doc = render_html(
        rendered, generated_at, window_start,
        config.get("site_title", "Richard 相对看板"),
        config.get("site_subtitle", ""),
    )
    overlong = long_notes(doc, int(config.get("note_max_chars", NOTE_MAX_CHARS)))
    warns += [f"图注超过{NOTE_MAX_CHARS}字：{x}" for x in overlong]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(doc, encoding="utf-8")
    return {
        "ok": True,
        "output": str(output),
        "bytes": len(doc.encode("utf-8")),
        "title": config.get("site_title"),
        "window_start": window_start,
        "sections": sections_meta,
        "pairs": charts,
        "charts": charts,
        "generated_at": generated_at,
        "warnings": warns,
        "overlong_notes": overlong,
        "readings": readings,
        "portfolio": portfolio_result,
        "etf_board": etf_result,
    }


# ─────────────────────────── self-test ───────────────────────────

def self_test() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def _raises(fn: Any) -> bool:
        try:
            fn()
        except Exception:
            return True
        return False

    cfg = load_config()
    sections = cfg["sections"]
    by_id = {s["id"]: s for s in sections}
    kweb, usfin = by_id.get("kweb", {}), by_id.get("usfin", {})
    pairs = kweb.get("pairs", [])
    ids = [p["id"] for p in pairs]

    # ── 站点与页签
    check("config_loads", isinstance(cfg, dict) and bool(sections))
    check("site_title_richard", cfg.get("site_title") == "Richard 相对看板",
          "站点名，不叫 KWEB 看板")
    check("no_overview_tab", "overview" not in by_id,
          "总览页已删，当前读数表和口径面板并进各节")
    check("section_ids", [s["id"] for s in sections] == ["kweb", "usfin"],
          "两个页签：KWEB、US Financials")
    check("section_labels", [s["label"] for s in sections] == ["KWEB", "US Financials"])
    check("all_windows_2023", all(s.get("window_start") == "2023-01-01" for s in sections),
          "两节都从 2023-01 起")
    check("all_history_warms_rsi",
          all((pd.Timestamp(s["window_start"]) - pd.Timestamp(s["history_start"])).days > 180
              for s in sections),
          "取价起点要比窗口起点早半年以上，否则窗口头部的 RSI30 是 NaN")
    check("all_rsi_period_30", all(int(s["rsi_period"]) == 30 for s in sections))

    # ── KWEB 节
    check("kweb_horizon_63", int(kweb["horizon_days"]) == 63, "3 个月按 63 个交易日")
    check("sixteen_bench_pairs", sum(1 for p in pairs if p["kind"] == "vs_bench") == 16,
          "16 个标的对 KWEB")
    check("kweb_pair_order", ids == [
        "TENCENT", "BABA", "PDD", "NTES", "MEITUAN", "JD", "BIDU", "TCOM", "BEKE",
        "ZTO", "YUMC", "JDH", "FUTU", "YMM", "BZ", "VIPS", "HTHT_ATAT"],
        "页面顺序写死：腾讯打头，网易接拼多多，百度接京东，新增七个在后段")
    check("htht_atat_pair", ids[-1] == "HTHT_ATAT"
          and next(p for p in pairs if p["id"] == "HTHT_ATAT")["denominator"] == "ATAT",
          "华住/亚朵是标的对标的，分母不是 KWEB，排最后")
    check("bench_is_kweb", all(p["denominator"] == "KWEB" for p in pairs if p["kind"] == "vs_bench"))
    check("palette_not_in_config", all("color" not in p for p in pairs),
          "配色不写进 config：同一含义在所有图上必须同色")
    check("meituan_uses_hk_line", next(p for p in pairs if p["id"] == "MEITUAN")["numerator"] == "3690.HK",
          "美团用港股主板，不用 OTC ADR MPNGY")
    check("hk_lines_use_main_board",
          {p["id"] for p in pairs if str(p["numerator"]).endswith(".HK")}
          == {"TENCENT", "MEITUAN", "JDH"},
          "腾讯/美团/京东健康走港股主板，不用 TCEHY 之类的 OTC ADR")
    check("hk_lines_flag_currency",
          all("港元" in p.get("note", "")
              for p in pairs if str(p["numerator"]).endswith(".HK")),
          "港元计价的腿要在 note 里标出来")
    check("palette_distinct", RSI_COLOR != FWD_REL_COLOR,
          "同一张图上两条线必须是两个颜色")

    # ── 虚拟组合：只用 KWEB 读数，按 conviction 分配，多空各 50 万美元
    portfolio_cfg = cfg.get("portfolio", {})
    check("portfolio_enabled", portfolio_cfg.get("enabled") is True)
    check("portfolio_constraints",
          portfolio_cfg.get("gross_usd") == 1_000_000
          and portfolio_cfg.get("net_usd_at_us_open") == 0
          and portfolio_cfg.get("single_name_cap_usd") == 200_000,
          "美股开盘 gross=100万、net=0，单名不超过20% gross")
    check("portfolio_neutral_zone", portfolio_cfg.get("neutral_threshold") == 0.2,
          "弱信号不交易，不为凑数硬塞仓位")
    check("portfolio_score_weights",
          portfolio_cfg.get("score_weights") == {
              "directional_effect": 0.5, "bucket_median": 0.25, "bucket_hit": 0.25,
          }, "综合分权重必须公开、可复算")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import kweb_portfolio as _portfolio
    bootstrap = {k: int(v) for k, v in portfolio_cfg.get("bootstrap_target_usd", {}).items()}
    check("portfolio_bootstrap_exact",
          sum(abs(v) for v in bootstrap.values()) == 1_000_000 and sum(bootstrap.values()) == 0,
          "初始仓位必须多空各50万美元")
    check("portfolio_bootstrap_cap", max(abs(v) for v in bootstrap.values()) <= portfolio_cfg.get("single_name_cap_usd", 200_000))
    check("portfolio_cross_market",
          bootstrap.get("0700.HK", 0) > 0 and bootstrap.get("6618.HK", 0) < 0,
          "允许跨市场后，腾讯和京东健康要按信号进入初始仓位")
    _portfolio.validate_target(bootstrap, portfolio_cfg)
    demo_readings = []
    for i in range(16):
        demo_readings.append({
            "section": "kweb", "name": f"N{i}", "pair": f"T{i}/KWEB", "rsi": 40 + i,
            "percentile": 20.0 + i * 4.0, "ic_rel": -0.4 + i * 0.05,
            "bucket_rel_median": -0.08 + i * 0.01, "bucket_hit": 0.25 + i * 0.03,
            "bucket": "Q3", "end": "2026-08-10",
        })
    scored_demo = _portfolio.score_readings(demo_readings, portfolio_cfg)
    check("portfolio_audit_fields",
          all(all(key in row for key in ["directional_effect", "directional_rank", "median_rank",
                                          "hit_rank", "score", "conviction", "signal"])
              for row in scored_demo),
          "日报要保留每个分项，不能只给最后仓位")
    check("portfolio_mean_reversion_direction",
          next(row for row in scored_demo if row["ticker"] == "T0")["directional_effect"] > 0,
          "负 IC + 低 RSI 分位应该是多头方向")
    # B8 的三个秩是 1/15、1/3、1/3，综合分正好 0.20；2026-09-25 满帮就是这样被浮点算成中性，多头只剩 3 只装不下 50 万。
    swap = list(range(16))
    swap[8], swap[10] = 10, 8
    boundary = _portfolio.score_readings([
        {"section": "kweb", "name": f"B{i}", "pair": f"B{i}/KWEB", "rsi": 50,
         "percentile": 51.0 + i, "ic_rel": 0.5, "bucket_rel_median": 0.01 * swap[i],
         "bucket_hit": 0.30 + 0.01 * swap[i], "bucket": "Q3", "end": "2026-09-25"}
        for i in range(16)
    ], portfolio_cfg)
    check("portfolio_threshold_boundary",
          next(row for row in boundary if row["ticker"] == "B8")["signal"] == "long",
          "综合分正好 0.20 要持仓，不能被浮点误差判成中性")
    check("portfolio_excludes_non_kweb_pair",
          all(row["ticker"] != "HTHT" for row in _portfolio.score_readings(
              demo_readings[:-1] + [{**demo_readings[-1], "pair": "HTHT/ATAT"},
                                    {**demo_readings[-1], "name": "补位", "pair": "EXTRA/KWEB"}],
              portfolio_cfg)),
          "华住/亚朵不是相对 KWEB，不进入这个组合")
    check("portfolio_state_outside_skill",
          SKILL_DIR not in _portfolio.DEFAULT_STATE_DIR.parents,
          "运行状态不能写回 skill 目录")
    demo_state = {
        "equity_usd": 1_000_000, "daily_mtm_pnl_usd": 0, "cumulative_pnl_usd": 0,
        "last_us_open_gross_usd": 1_000_000, "last_us_open_net_usd": 0,
        "positions": {}, "last_positions": [], "last_trades": [],
        "last_analysis": scored_demo,
        "pending": {"signal_date": "2026-08-10", "target_usd": bootstrap},
    }
    demo_history = [
        {"type": "initialize", "signal_date": "2026-08-10", "daily_mtm_pnl_usd": 0,
         "cumulative_pnl_usd": 0, "equity_usd": 1_000_000},
        {"type": "daily", "signal_date": "2026-08-11", "daily_mtm_pnl_usd": 1_250,
         "cumulative_pnl_usd": 1_250, "equity_usd": 1_001_250},
    ]
    portfolio_html = portfolio_section(demo_state, portfolio_cfg, demo_history)
    check("portfolio_page_records_all",
          all(text in portfolio_html for text in ["过去一天 MTM", "历史 P&amp;L", "更新后的仓位", "过去一天的交易",
                                                   "下一交易日目标", "完整决策审计"]),
          "KWEB 页末尾要同时留 P&L、交易、仓位、下一目标和决策审计")
    check("portfolio_history_is_daily",
          all(text in portfolio_html for text in ["2026-08-10", "2026-08-11", "$1,250", "$1,001,250"]),
          "页面要从 append-only 账本列出逐日 MTM、累计 P&L 和权益")

    # ── US Financials 节
    check("usfin_horizon_60", int(usfin["horizon_days"]) == 60, "未来 60 个交易日，和 KWEB 节的 63 不同")
    check("usfin_bench_xlf", usfin.get("benchmark") == "XLF")
    check("usfin_cap_weight", usfin["basket"].get("weighting") == "cap",
          "市值加权，和 XLF 口径一致；等权对 XLF 的跟踪误差从 3.4% 涨到 5.0%")
    check("usfin_groups_by_sub_industry", usfin["basket"].get("group_field") == "sub_industry",
          "按 GICS 子行业分组（区域银行/财险/寿险这一层）")

    snap = load_constituents(usfin["basket"]["constituents"])
    check("snapshot_has_as_of", bool(snap.get("as_of")), "快照要带日期，页面上要标出来")
    check("snapshot_sector_financials", snap.get("sector") == "Financials")
    check("snapshot_members", len(snap["members"]) >= 60,
          f'成分 {len(snap["members"])} 只')
    check("snapshot_shares_complete",
          all(m["yf"] in snap["shares_outstanding"] for m in snap["members"]),
          "每只成分都要有股本，否则市值加权会静默漏掉它")
    check("snapshot_labels_cover",
          all(m["sub_industry"] in snap["labels"] for m in snap["members"]),
          "每个子行业都要有中文名")
    check("snapshot_brk_yf_code",
          all(m["yf"] == "BRK-B" for m in snap["members"] if m["symbol"] == "BRK.B") or
          all(m["symbol"] != "BRK.B" for m in snap["members"]),
          "BRK.B 在 yfinance 是 BRK-B，点号要换成横杠")

    specs, _ = basket_specs(usfin)
    check("usfin_11_subindustries", len(specs) == 11, f"{len(specs)} 个子行业")
    check("usfin_excludes_single_name_groups",
          set(usfin["basket"].get("exclude_groups", [])) ==
          {"Multi-Sector Holdings", "Reinsurance"},
          "多元控股只有 BRK-B、再保险只有 EG，不出图")
    check("usfin_excluded_not_charted",
          not any(s["name"] in {"多元控股", "再保险"} for s in specs),
          "被排除的子行业不能再出现在图里")
    check("usfin_no_single_name_left",
          not any(s["single_name"] for s in specs),
          f'剩下的都是多成分子行业，最小 {min(len(s["members"]) for s in specs)} 只')
    check("usfin_exclude_unknown_group_raises",
          _raises(lambda: basket_specs(
              {**usfin, "basket": {**usfin["basket"],
                                   "exclude_groups": ["Not A GICS Group"]}})),
          "GICS 改名后要报错，不能静默把图放回来")
    check("usfin_excluded_labels_name_tickers",
          excluded_labels(usfin, snap) == ["多元控股（BRK.B）", "再保险（EG）"],
          "页面上要写清楚排掉的是哪两只")
    check("usfin_specs_are_baskets", all(s["kind"] == "basket" for s in specs))
    check("usfin_specs_vs_xlf", all(s["denominator"] == "XLF" for s in specs))
    check("usfin_has_regional_banks", any(s["name"] == "区域银行" for s in specs),
          "用户点名的三个之一")
    check("usfin_has_pc_insurance", any(s["name"] == "财产险" for s in specs))
    check("usfin_has_life_insurance", any(s["name"] == "寿险与健康险" for s in specs))
    check("usfin_excluded_single_names_flagged",
          all(s["single_name"] == (len(s["members"]) == 1) for s in specs),
          "只有 1 只成分的子行业要标出来，别让人当行业组合读")
    check("usfin_order_banks_first", specs[0]["name"] == "综合性银行",
          "按 GICS 行业组排：银行→金融服务→保险，不按字母序")
    check("usfin_no_duplicate_members",
          len({m["yf"] for s in specs for m in s["members"]}) == sum(len(s["members"]) for s in specs),
          "一只股票只能进一个子行业")

    # 市值加权：权重按股本×价格，不是等权
    bidx = pd.bdate_range("2023-01-02", periods=50)
    big = pd.Series(np.linspace(100.0, 110.0, 50), index=bidx)
    small = pd.Series(np.linspace(10.0, 5.0, 50), index=bidx)
    members = [{"yf": "BIG"}, {"yf": "SMALL"}]
    basket, missing = build_basket(members, {"BIG": big, "SMALL": small},
                                   {"BIG": 1000.0, "SMALL": 1.0})
    check("basket_cap_weighted",
          abs(float(basket.iloc[0]) - (100.0 * 1000.0 + 10.0 * 1.0)) < 1e-6,
          "篮子= Σ 股本×价格，大票权重大")
    check("basket_follows_big_name", float(basket.iloc[-1]) > float(basket.iloc[0]),
          "大票涨、小票跌时篮子跟大票走，说明不是等权")
    check("basket_missing_reported",
          build_basket(members + [{"yf": "GONE"}], {"BIG": big, "SMALL": small},
                       {"BIG": 1.0, "SMALL": 1.0})[1] == ["GONE"],
          "取不到价或缺股本的成分要报出来，不能静默丢掉")
    check("basket_no_shares_is_missing",
          build_basket([{"yf": "BIG"}], {"BIG": big}, {})[1] == ["BIG"],
          "缺股本等于缺权重，同样算缺失")
    check("basket_empty_is_empty", build_basket([], {}, {})[0].empty)

    # RSI：单调上涨序列的 Wilder RSI 必须是 100
    rising = pd.Series(np.arange(1, 121, dtype=float))
    check("rsi_all_up_is_100", abs(float(wilder_rsi(rising, 30).iloc[-1]) - 100.0) < 1e-6)
    check("rsi_warmup_nan", wilder_rsi(rising, 30).iloc[:29].isna().all(), "前 29 个点不出值")
    flat = pd.Series(np.full(120, 5.0))
    check("rsi_flat_is_nan", bool(wilder_rsi(flat, 30).isna().all()), "无波动时不编造 50")

    # 比值与前瞻回报
    idx = pd.bdate_range("2020-01-01", periods=400)
    num = pd.Series(np.linspace(10, 20, 400), index=idx)
    den = pd.Series(np.full(400, 2.0), index=idx)
    frame = pair_frame(num, den, False, 30, 63)
    check("ratio_len", len(frame) == 400)
    check("fwd_tail_nan", bool(frame["fwd_abs"].iloc[-63:].isna().all()),
          "最后 63 天没有未来回报")
    check("fwd_is_forward", bool(frame["fwd_abs"].iloc[-64:-63].notna().all()))
    expected = float(num.iloc[63] / num.iloc[0] - 1.0)
    check("fwd_abs_value", abs(float(frame["fwd_abs"].iloc[0]) - expected) < 1e-9,
          "未来回报按信号日对齐，不是滞后回报")
    check("fwd_rel_flat_den", abs(float(frame["fwd_rel"].iloc[0]) - expected) < 1e-9,
          "分母不动时相对回报=绝对回报")

    # 窗口截断：先算后截，窗口第一天的 RSI 必须已经暖机
    windowed = apply_window(frame, "2021-01-01")
    check("window_trims_head", bool(windowed.index.min() >= pd.Timestamp("2021-01-01"))
          and len(windowed) < len(frame), "窗口之前的行被截掉")
    check("window_head_rsi_warm", bool(pd.notna(windowed["rsi"].iloc[0])),
          "窗口第一天就有 RSI，不是从第 30 天才开始")
    check("window_keeps_tail_nan", bool(windowed["fwd_abs"].iloc[-1] != windowed["fwd_abs"].iloc[-1]),
          "截断不影响末段没有未来回报这件事")
    check("window_none_is_noop", len(apply_window(frame, None)) == len(frame))

    # 港股腿日历对齐
    us_idx = pd.bdate_range("2021-01-04", periods=200)
    # 港股腿：少 3 个美股交易日，多 1 个美股休市日（周六），模拟两地日历错位
    hk_idx = us_idx.drop(us_idx[5:8]).append(pd.DatetimeIndex(["2021-07-03"])).sort_values()
    hk = pd.Series(np.linspace(50, 90, len(hk_idx)), index=hk_idx)
    us = pd.Series(np.linspace(20, 30, 200), index=us_idx)
    ratio_hk = build_ratio(hk, us, True)
    check("hk_calendar_on_bench", ratio_hk.index.isin(us_idx).all(),
          "比值时间轴以基准交易日为准")
    check("hk_no_dropout", len(ratio_hk) >= 190, f"n={len(ratio_hk)}，不因单边休市打碎序列")

    # 分桶
    rng = np.random.default_rng(7)
    demo = pd.DataFrame(
        {"rsi": rng.uniform(20, 80, 1000),
         "fwd_abs": rng.normal(0, 0.1, 1000),
         "fwd_rel": rng.normal(0, 0.1, 1000)}
    )
    rows = bucket_stats(demo, 63)
    check("bucket_count_5", len(rows) == 5)
    check("bucket_balanced", max(r["n"] for r in rows) - min(r["n"] for r in rows) <= 2,
          "五分位桶样本数基本相等")
    check("bucket_independent", all(abs(r["independent"] - r["n"] / 63) < 1e-9 for r in rows),
          "独立观测=样本数/63")
    check("bucket_edges_contiguous",
          all(abs(rows[i]["rsi_hi"] - rows[i + 1]["rsi_lo"]) < 1e-9 for i in range(4)),
          "五分位必须保存连续的 qcut 分界，不能用样本极值留下空隙")
    check("bucket_needs_sample", bucket_stats(demo.head(100), 63) == [],
          "样本不足 5 段时不出分位统计")
    check("current_bucket_top", current_bucket(rows, 99.0)["label"] == QUINTILE_LABELS[-1])
    check("current_bucket_bottom", current_bucket(rows, 1.0)["label"] == QUINTILE_LABELS[0])
    boundary = rows[2]["rsi_hi"]
    check("current_bucket_right_of_boundary",
          current_bucket(rows, np.nextafter(boundary, np.inf))["label"] == QUINTILE_LABELS[3],
          "刚越过 Q3/Q4 分界的值必须进 Q4，不能兜底到 Q5")
    check("bucket_range_text_exact",
          bucket_range_text(rows[0], 0, 5).startswith("≤") and
          "< RSI ≤" in bucket_range_text(rows[2], 2, 5) and
          bucket_range_text(rows[-1], 4, 5).startswith(">"),
          "页面区间要显示连续边界和开闭方向")

    # 图表约定
    check("align_50_to_zero", ALIGN_50_TO_ZERO == (50.0, 0.0),
          "左轴 RSI50 钉在右轴 0% 上")
    demo_chart = chart("t", [series_payload(pd.Series([1.0, 2.0], index=idx[:2]), "s", "#fff")],
                       "n", 340, [hline(30, "left", "", "cold")], ALIGN_50_TO_ZERO)
    payload = json.loads(html.unescape(re.search(r'data-chart="([^"]+)"', demo_chart).group(1)))
    check("chart_carries_align", payload["align"] == {"left": 50.0, "right": 0.0})
    check("chart_carries_hlines", len(payload["hlines"]) == 1)
    check("series_carries_width", payload["series"][0]["width"] == 1.8, "线宽随 payload 走")

    # 图上只有 RSI 和相对涨跌两条线
    demo_item = {
        "spec": {"id": "D", "name": "样例", "numerator": "AAA", "denominator": "KWEB",
                 "kind": "vs_bench"},
        "frame": frame, "rsi": frame["rsi"].dropna(), "rsi_now": 55.0, "pctl": 60.0,
        "buckets": [], "current_bucket": None, "ic_rel": -0.1,
        "start": "2023-01-03", "end": "2026-08-08", "n_signal": 800,
        "extreme_cold": 3, "extreme_hot": 4,
    }
    demo_data = {"id": "demo", "label": "Demo", "heading": "Demo", "results": [demo_item],
                 "horizon": 63, "horizon_label": "3个月", "rsi_period": 30,
                 "window_start": "2023-01-01", "snapshot": None, "basket": None}
    section = pair_section(demo_data)
    check("portfolio_is_kweb_footer",
          (section + portfolio_html).index("虚拟组合") > section.index("口径与限制"),
          "虚拟组合必须接在 KWEB 原内容最下面，不另开页签")
    chart_payloads = [
        json.loads(html.unescape(m)) for m in re.findall(r'data-chart="([^"]+)"', section)
    ]
    check("chart_two_series", len(chart_payloads) == 1 and len(chart_payloads[0]["series"]) == 2,
          "每张图两条线：RSI + 此后63日相对涨跌")
    check("chart_no_abs_series",
          all("绝对" not in s["name"] for s in chart_payloads[0]["series"]),
          "图上不画绝对涨跌")
    check("chart_right_axis_is_relative",
          chart_payloads[0]["series"][1]["axis"] == "right"
          and "相对涨跌" in chart_payloads[0]["series"][1]["name"])

    # 每节自带当前读数表和口径面板（总览删掉之后这两块不能跟着丢）
    check("section_has_readings", "当前读数与所在分位桶" in section,
          "读数表并进各节开头")
    check("section_has_caveats", "口径与限制" in section, "口径面板并进各节结尾")
    check("section_readings_first",
          section.index("当前读数与所在分位桶") < section.index("口径与限制"))

    # 篮子节的口径面板必须写清楚「不是官方指数」和幸存者偏差
    basket_data = dict(demo_data)
    basket_item = dict(demo_item)
    basket_item["spec"] = {"id": "B", "name": "区域银行", "numerator": "Regional Banks",
                           "denominator": "XLF", "kind": "basket", "single_name": False,
                           "members": [{"yf": "AAA", "symbol": "AAA"}, {"yf": "BBB", "symbol": "BBB"}]}
    basket_data["results"] = [basket_item]
    basket_data["snapshot"] = {"as_of": "2026-08-10", "members": [1, 2]}
    basket_section = pair_section(basket_data)
    check("basket_caveat_not_official", "不是标普官方子行业指数" in basket_section,
          "自拼篮子必须说清楚不是官方指数")
    check("basket_caveat_survivorship", "幸存者偏差" in basket_section)
    check("basket_caveat_as_of", "2026-08-10" in basket_section, "快照日期要露在页面上")
    check("basket_lists_members", 'class="members"' in basket_section and "AAA" in basket_section,
          "成分名单要列出来")
    check("basket_members_not_a_note",
          "成分（2 只）" not in re.sub(r'<p class="members">.*?</p>', "", basket_section, flags=re.S)
          and long_notes(basket_section, 100) == [],
          "成分名单用 .members，不占 .note 的 100 字额度")
    check("basket_label_uses_zh",
          "区域银行/XLF" in basket_section and "Regional Banks/XLF" not in basket_section,
          "图例用中文行业名，不用 GICS 英文原文")

    single_data = dict(basket_data)
    single_item = dict(basket_item)
    single_item["spec"] = dict(basket_item["spec"])
    single_item["spec"]["single_name"] = True
    single_item["spec"]["members"] = [{"yf": "AAA", "symbol": "AAA"}]
    single_data["results"] = [single_item]
    check("single_name_caveat", "不是一个行业组合" in pair_section(single_data),
          "单名子行业要在口径面板里点名")

    # chip 分色
    hot = pair_alerts({"spec": {"name": "X"}, "rsi_now": 80.0, "pctl": 99.0})
    cold = pair_alerts({"spec": {"name": "X"}, "rsi_now": 20.0, "pctl": 1.0})
    mid = pair_alerts({"spec": {"name": "X"}, "rsi_now": 50.0, "pctl": 50.0})
    check("chip_hot_amber", len(hot) == 1 and 'class="hot"' in hot[0])
    check("chip_cold_blue", len(cold) == 1 and 'class="cold"' in cold[0])
    check("chip_neutral_silent", mid == [], "中性区不出 chip")
    pctl_only = pair_alerts({"spec": {"name": "X"}, "rsi_now": 62.0, "pctl": 97.0})
    check("chip_percentile_fallback", len(pctl_only) == 1 and "分位" in pctl_only[0],
          "RSI 没到 70 但自身分位极端时仍然提示")

    # 脚注长度
    check("long_notes_flags", long_notes('<p class="note">' + "字" * 130 + "</p>", 100) != [])
    check("long_notes_passes_short", long_notes('<p class="note">短</p>', 100) == [])
    check("long_notes_strips_markup", long_notes('<p class="note"><b>短</b></p>', 3) == [])

    # deploy 默认拒绝带 warnings 的发布
    check("deploy_blocks_on_warnings",
          "allow_warnings" in deploy.__code__.co_varnames,
          "build 有 warnings 时默认不推进")
    check("deploy_supports_no_upload",
          "no_upload" in deploy.__code__.co_varnames,
          "deploy 兼容 --no-upload 参数，只生成本地页面并更新虚拟组合")

    # 依赖
    try:
        import yfinance  # noqa: F401
        check("yfinance_import", True)
    except Exception as exc:  # pragma: no cover
        check("yfinance_import", False, str(exc))

    # ── ETF 页
    try:
        import kweb_etf_board as etf

        check("etf_board_imports", True)
        ecfg = etf.load_etf_config()
        listed = etf.listed_tickers(ecfg)
        excluded = etf.excluded_tickers(ecfg)
        check("etf_tab_enabled", (cfg.get("etf_board") or {}).get("enabled") is True
              and (cfg.get("etf_board") or {}).get("id") not in by_id,
              "ETF 页是配置两节之外的第三个页签，id 不能和两节重复")
        check("etf_every_ticker_accounted",
              len(listed) + len(excluded) == int(ecfg.get("universe_size", -1)),
              f"列入 {len(listed)} + 排除 {len(excluded)} 要等于导出表 {ecfg.get('universe_size')} 只")
        check("etf_no_duplicate_ticker", len(set(listed + excluded)) == len(listed + excluded),
              "一只 ETF 只能出现在一个地方")
        check("etf_rates_ref_not_listed", ecfg["rates_ref"] not in listed,
              "TLT 是相关性参照，不进表")
        check("etf_corr_refs_config",
              isinstance(ecfg.get("corr_refs"), list) and "SOXX" in ecfg["corr_refs"]
              and etf.corr_refs(ecfg) == list(dict.fromkeys(ecfg["corr_refs"])),
              "corr_refs 是另列相关性的参照 ETF，SOXX 要在里面，不能和 rates_ref 重复")
        check("etf_aum_for_all_listed", all(t in ecfg.get("aum_b", {}) for t in listed),
              "代表按 AUM 选，列入的每只都要有 AUM")
        check("etf_child_kinds",
              all(c["kind"] in etf.CHILD_KINDS for s in ecfg["sectors"] for c in s.get("children", [])),
              "行业下面只挂子行业和主题")
        check("etf_sector_has_own_row", all(s["rows"] for s in ecfg["sectors"]),
              "每个行业至少有一只行业 ETF，子行业和主题挂在它下面")

        aum = {"A": 5.0, "B": 9.0, "C": 7.0}
        check("etf_rep_max_aum", etf.pick_rep(["A", "B", "C"], aum, lambda t: True) == "B")
        check("etf_rep_skips_missing", etf.pick_rep(["A", "B", "C"], aum, lambda t: t != "B") == "C",
              "最大的那只没行情时用同类下一只")

        vol = pd.Series([100.0] * 85 + [300.0] * 5)
        check("etf_volume_ratio", abs(etf.volume_ratio(vol, 5, 90) - 300.0 / ((85 * 100 + 5 * 300) / 90)) < 1e-9)
        check("etf_volume_ratio_short_history", etf.volume_ratio(vol.iloc[:50], 5, 90) is None,
              "没传交易日历时不足 90 天不出量比；传了日历按八成覆盖判断")
        vcal = pd.bdate_range("2026-01-01", periods=90)
        vgap = pd.Series(100.0, index=vcal).drop(vcal[10:40])
        check("etf_volume_ratio_window_on_calendar", etf.volume_ratio(vgap, 5, 90, vcal) is None
              and etf.volume_ratio(pd.Series(100.0, index=vcal), 5, 90, vcal) == 1.0,
              "缺数不往窗口外补；长窗口有效天数不足八成不出量比")

        cal = pd.bdate_range("2025-01-01", periods=300)
        rng = np.random.default_rng(7)
        base = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)), index=cal)
        same = base.copy()
        gappy = base.drop(cal[150])
        r_base = etf.aligned_returns(base, cal)
        r_gap = etf.aligned_returns(gappy, cal)
        corr_same, _ = etf.window_corr(etf.aligned_returns(same, cal), r_base, 252, 200)
        corr_gap, n_gap = etf.window_corr(r_gap, r_base, 252, 200)
        check("etf_corr_identity", corr_same is not None and abs(corr_same - 1) < 1e-12)
        check("etf_corr_gap_not_shifted",
              corr_gap is not None and abs(corr_gap - 1) < 1e-12 and n_gap == 250,
              f"缺一天只少两个收益样本且不错位：corr={corr_gap} n={n_gap}")
        check("etf_corr_min_obs", etf.window_corr(r_base.iloc[:150], r_base.iloc[:150], 252, 200)[0] is None,
              "共同交易日不足 200 留空")

        def fake_fetch(tickers: list[str], start: str) -> tuple[pd.DataFrame, pd.DataFrame]:
            idx = pd.bdate_range(end="2026-11-04", periods=400)
            prices, volumes = {}, {}
            for i, t in enumerate(tickers):
                if t == "IVV":
                    continue
                walk = np.cumprod(1 + np.random.default_rng(i).normal(0, 0.01, len(idx)))
                prices[t] = pd.Series(100 * walk, index=idx)
                volumes[t] = pd.Series(1e6, index=idx)
            return pd.DataFrame(prices), pd.DataFrame(volumes)

        mini = {
            "aum_as_of": "2026-11-04", "aum_source": "自检", "universe_size": 5,
            "rates_ref": "TLT", "corr_refs": ["SOXX"],
            "cyc_ref": {"label": "周期/防御", "code": "GSPUCYDE", "csv": "__missing__.csv",
                        "date_col": "x", "value_col": "y", "stale_days": 4},
            "corr_window": 252, "corr_min_obs": 200, "vol_short": 5, "vol_long": 90,
            "spark_days": 252, "history_days": 800,
            "broad": [{"label": "美国核心", "rows": [{"label": "标普500", "members": ["VOO", "IVV"]}]}],
            "sectors": [{"label": "信息技术", "rows": [{"label": "科技", "members": ["XLK"]}],
                         "children": [{"label": "半导体", "kind": "子行业",
                                       "rows": [{"label": "半导体", "members": ["SMH"]}]}]}],
            "excluded": {"债券与现金": ["TLT"]},
            "names": {}, "aum_b": {"VOO": 1.0, "IVV": 2.0, "XLK": 1.0, "SMH": 1.0},
        }
        from datetime import date as _date

        data = etf.analyse_board(mini, Path("/nonexistent"), wilder_rsi, fetch=fake_fetch,
                                 today=_date(2026, 11, 5))
        late = etf.analyse_board(mini, Path("/nonexistent"), wilder_rsi, fetch=fake_fetch,
                                 today=_date(2026, 12, 1))
        check("etf_board_freshness", any("全表行情停在" in e for e in late["errors"])
              and not any("全表行情停在" in e for e in data["errors"]),
              "整表行情都旧了也要报，不能只和表内最新日比")
        reps = [r["rep"] for r in data["results"]]
        check("etf_offline_rep_fallback", reps == ["VOO", "XLK", "SMH"]
              and data["substituted"] == ["标普500：IVV→VOO"], f"reps={reps}")
        check("etf_missing_cyc_is_error", any("周期/防御" in e for e in data["errors"])
              and all(r["metrics"]["corr_cyc"] is None for r in data["results"]),
              "周期/防御数据缺失要报出来，相关列留空")
        check("etf_corr_ref_column", data["corr_refs"] == ["SOXX"]
              and all(r["metrics"]["corr_ref"].get("SOXX") is not None for r in data["results"]),
              "每行都要算和 SOXX 的相关")

        def fetch_no_soxx(tickers: list[str], start: str) -> tuple[pd.DataFrame, pd.DataFrame]:
            return fake_fetch([t for t in tickers if t != "SOXX"], start)

        no_ref = etf.analyse_board(mini, Path("/nonexistent"), wilder_rsi, fetch=fetch_no_soxx,
                                   today=_date(2026, 11, 5))
        check("etf_missing_ref_is_error", any("SOXX" in e for e in no_ref["errors"])
              and all(r["metrics"]["corr_ref"]["SOXX"] is None for r in no_ref["results"]),
              "SOXX 没取到行情要报出来，SOXX 相关列留空")
        body = etf.render_board(mini, data)
        import re as _re

        ncols = len(etf.headers(["SOXX"]))
        data_rows = [tr for tr in _re.findall(r"<tr>(.*?)</tr>", body) if "<td" in tr]
        empty_row = etf.etf_row({"rep": None, "intended": "X", "peers": [], "label": "x",
                                 "name": "", "metrics": None}, False, ["SOXX"])
        check("etf_render_ref_column", body.count("<th>相关：SOXX</th>") == 2
              and len(data_rows) == len(data["results"])
              and all(tr.count("<td") == ncols for tr in data_rows)
              and f'colspan="{ncols}"' in body and f'colspan="{ncols - 1}"' in empty_row,
              "每张表多一列「相关：SOXX」，分组行和缺行情行的跨列数跟着变")
        check("etf_render_no_canvas", 'class="chart"' not in body,
              "ETF 页用 SVG 小图，不占另外两页的 28 张图")
        check("etf_render_spark_per_row", body.count('<span class="spark">') == len(data["results"]))
        check("etf_render_indents_children", body.count('etf-name indent') == 1
              and 'class="subgrp"' in body, "子行业和主题缩进挂在行业下面")
        check("etf_notes_short", not long_notes(body), "图注不超过 100 字")
        summ = etf.summary(data)
        check("etf_summary_compact", "results" not in summ and summ["ok"] is False,
              "build --json 只带精简结果")
        pts, since, short = etf.spark_points(base.iloc[:100], 252)
        check("etf_short_history_flagged", short and since == "2025-01-01" and len(pts) <= 91,
              "不足 1 年的 ETF 要写明起点，不冒充 1 年涨跌")
        gap_cal = pd.bdate_range("2024-01-01", periods=400)
        gap = pd.Series(100.0, index=gap_cal).drop(gap_cal[148:248])
        _, gap_since, gap_short = etf.spark_points(gap, 252, calendar=gap_cal)
        _, _, tiny_short = etf.spark_points(pd.Series(100.0, index=gap_cal[:50]), 252, calendar=gap_cal[:50])
        _, _, full_short = etf.spark_points(pd.Series(100.0, index=gap_cal), 252, calendar=gap_cal)
        _, end_since, end_short = etf.spark_points(pd.Series(100.0, index=gap_cal[:-20]), 252, calendar=gap_cal)
        check("etf_spark_window_coverage", gap_short and gap_since == gap_cal[248].strftime("%Y-%m-%d")
              and tiny_short and end_short and end_since == gap_cal[148].strftime("%Y-%m-%d")
              and not full_short,
              "窗口开头或末端缺数、日历不足 1 年都按区间不足处理，写实际起止")
        check("etf_spark_span_text", etf.span_text("2025-11-05", "2026-10-01") == "2025-11-05 至 2026-10-01"
              and etf.span_text("2026-04-02", None) == "自 2026-04-02 起"
              and "截至 2026-10-01" in etf.sparkline([1.0, 2.0], 1.0, None, "2026-10-01"))
        check("etf_parse_aum_text", etf.parse_aum_text("1.06T") == 1060.0
              and etf.parse_aum_text("887.20B") == 887.2 and etf.parse_aum_text("") is None)
        check("etf_not_in_build_warnings",
              "etf_result" in build.__code__.co_varnames
              and "warns" not in build_etf_page.__code__.co_names,
              "ETF 页的问题不进 warnings，不能挡住虚拟组合")
    except Exception as exc:  # pragma: no cover
        check("etf_board_imports", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for c in checks if c["ok"])
    return {
        "ok": passed == len(checks),
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "failed": [c for c in checks if not c["ok"]],
    }


# ─────────────────────────── 上线 ───────────────────────────

def deploy(
    output: Path,
    allow_warnings: bool = False,
    no_upload: bool = True,
) -> dict[str, Any]:
    """先 build（含推进虚拟组合），仅在本地生成页面（no_upload 为兼容空参数）。"""
    _ = no_upload
    result = build(output, load_config(), update_portfolio=True)
    blockers = list(result.get("warnings", [])) + list(result.get("overlong_notes", []))
    if blockers and not allow_warnings:
        result["ok"] = False
        result["uploaded"] = False
        result["upload_skipped"] = True
        result["error"] = "build 有 warnings，已跳过发布；确认无误后加 --allow-warnings。"
        return result
    result["upload"] = None
    result["uploaded"] = False
    result["upload_skipped"] = True
    etf = result.get("etf_board")
    if etf is not None and not etf.get("ok"):
        errors = [str(e).strip() for e in etf.get("errors", []) if str(e).strip()]
        issue = "ETF 看板有取数问题：" + ("；".join(errors) if errors else "未知错误")
        result["etf_error"] = issue
        result["ok"] = False
        result["error"] = issue
    return result


# ─────────────────────────── CLI ───────────────────────────

def main(argv: list[str] | None = None) -> int:
    # 全局开关同时挂到子命令上，`run.sh --json build` 和 `run.sh build --json` 都能跑。
    # 只挂在顶层的话后一种写法会被 argparse 判成 unrecognized arguments。
    # default=SUPPRESS：不给 SUPPRESS 的话，子命令会用自己的 False 覆盖顶层已经解析出的
    # True，`--json build` 反而不输出 JSON。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="kweb-dashboard", description="KWEB 相对强弱看板", parents=[common]
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build", help="重算并生成 HTML", parents=[common])
    build_cmd.add_argument("--output", default=str(DEFAULT_OUTPUT))
    sub.add_parser("self-test", help="离线自检", parents=[common])
    refresh_cmd = sub.add_parser(
        "refresh-constituents", help="刷新 S&P 500 金融板块成分快照", parents=[common]
    )
    refresh_cmd.add_argument("--local", help="用本地保存的维基 HTML，不联网")
    aum_cmd = sub.add_parser(
        "refresh-etf-aum", help="用新的 Bloomberg ETF 导出表更新 AUM 快照", parents=[common]
    )
    aum_cmd.add_argument("--file", required=True, help="Bloomberg 导出的 xlsx（含 Ticker、Market Cap 列）")
    aum_cmd.add_argument("--as-of", required=True, help="导出表的数据日期 YYYY-MM-DD")
    deploy_cmd = sub.add_parser(
        "deploy",
        help="build（含更新虚拟组合）",
        parents=[common],
    )
    deploy_cmd.add_argument("--output", default=str(DEFAULT_OUTPUT))
    deploy_cmd.add_argument(
        "--allow-warnings",
        action="store_true",
        help="build 有 warnings 时仍然继续（默认拒绝，避免把缺面板的版本推进）",
    )
    deploy_cmd.add_argument(
        "--no-upload",
        action="store_true",
        help="兼容保留参数（deploy 默认仅本地生成页面）",
    )

    args = parser.parse_args(argv)
    as_json = getattr(args, "json", False)
    quiet = getattr(args, "quiet", False)
    try:
        if args.command == "self-test":
            result = self_test()
        elif args.command == "refresh-constituents":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import refresh_constituents

            local = Path(args.local).expanduser() if args.local else None
            result = refresh_constituents.refresh(local)
        elif args.command == "refresh-etf-aum":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import kweb_etf_board

            result = kweb_etf_board.refresh_aum(
                Path(args.file).expanduser(), args.as_of, resolve_workspace(__file__)
            )
        elif args.command == "deploy":
            result = deploy(
                Path(args.output).expanduser(),
                allow_warnings=getattr(args, "allow_warnings", False),
                no_upload=True,
            )
        else:
            result = build(Path(args.output).expanduser(), load_config())
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "hint": "先跑 `bash skills/kweb-dashboard/scripts/run.sh self-test --json`；"
                    "报 yfinance 相关错误时检查网络，报配置错误时看 references/kweb_config.json。",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "self-test":
        print(f"self-test {result['passed']}/{result['total']}")
        for item in result["failed"]:
            print(f"  FAIL {item['name']}: {item['detail']}")
    elif args.command == "refresh-constituents":
        print(f"{result['members']} 只成分、{result['sub_industries']} 个子行业 "
              f"（{result['as_of']}）→ {result['output']}")
    elif args.command == "refresh-etf-aum":
        print(f"AUM 快照 {result['as_of']}：更新 {result['updated']} 只 → {result['output']}")
        if result["listed_missing"]:
            print(f"  导出表里没有：{'、'.join(result['listed_missing'])}")
        if result["unclassified"]:
            print(f"  未分类的新 ETF {len(result['unclassified'])} 只：{'、'.join(result['unclassified'])}")
    elif args.command == "deploy":
        print(f"生成 {result['output']}（{result['bytes']} 字节）")
        for warn in result["warnings"]:
            print(f"  warning: {warn}")
        if result.get("etf_error"):
            print(f"  {result['etf_error']}")
        if result.get("error"):
            print(f"  {result['error']}")
    else:
        print(f"生成 {result['output']}（{result['bytes']} 字节，{result['pairs']} 个标的）")
        for warn in result["warnings"]:
            print(f"  warning: {warn}")
        etf = result.get("etf_board")
        if etf is not None:
            print(f"  ETF 看板：{etf.get('rows', 0)} 行，行情截至 {etf.get('board_last')}，"
                  f"问题 {len(etf.get('errors', []))} 条")
            for err in etf.get("errors", []):
                print(f"    {err}")
        if not quiet:
            for row in result["readings"]:
                print(f"  {row['name']:<10s} RSI30={row['rsi']} 分位={row['percentile']}% "
                      f"桶={row['bucket']} 秩相关={row['ic_rel']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
