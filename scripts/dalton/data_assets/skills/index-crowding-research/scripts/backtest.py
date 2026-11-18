#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from openclaw_workspace import workspace_root as resolve_workspace

import numpy as np
import pandas as pd


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = resolve_workspace(__file__)
HISTORY_DIR = SKILL_ROOT / "state" / "cache" / "history"
META_PATH = WORKSPACE / "research-outputs" / "index-crowding" / "official-all.json"
OUT_DIR = WORKSPACE / "research-outputs" / "index-crowding-research"
HORIZONS = (5, 20, 60, 120, 252)
CUTOFF = pd.Timestamp("2023-08-04")
MIN_CROSS_SECTION = 30
RNG_SEED = 20260805


SIGNAL_LABELS = {
    "cold_level": "低拥挤水平（反向原版分）",
    "crowding_rise_5": "拥挤度5日上升",
    "crowding_rise_20": "拥挤度20日上升",
    "price_mom_20": "价格20日动量",
    "price_mom_60": "价格60日动量",
    "cold_plus_mom20": "低拥挤+20日价格动量",
    "hot_plus_mom20": "高拥挤+20日价格动量",
    "cold_turnup": "低拥挤+拥挤度5日回升",
    "cold_recovery_full": "低拥挤+拥挤度回升+价格转强",
    "three_year_cold": "原版分数的3年低分位",
    "crowding_price_divergence": "拥挤度上升-价格动量背离",
    "trend_confirmed_crowding": "拥挤度与价格同向增强",
}

EVENT_LABELS = {
    "low": "低拥挤（≤15）",
    "high": "高拥挤（≥85）",
    "low_rising": "低拥挤且5日回升",
    "low_falling": "低拥挤且5日继续下降",
    "low_price_up": "低拥挤且20日价格为正",
    "low_recovery": "低拥挤+拥挤回升+价格为正",
    "high_rising": "高拥挤且5日继续上升",
    "high_falling": "高拥挤且5日回落",
    "high_price_down": "高拥挤且20日价格为负",
    "high_breakdown": "高拥挤+拥挤回落+价格为负",
}


def code_from_path(path: Path, codes: list[str]) -> str:
    lookup = {code.replace(".", "_"): code for code in codes}
    return lookup[path.stem]


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    meta = json.loads(META_PATH.read_text())
    meta_rows = {row["index_code"]: row for row in meta["rows"]}
    codes = list(meta_rows)
    frames = []
    coverage = []
    for path in sorted(HISTORY_DIR.glob("*.json")):
        code = code_from_path(path, codes)
        payload = json.loads(path.read_text())
        raw = payload.get("data") or []
        if isinstance(raw, dict):  # 2026-11-13: vendor nests {"data": {"data": [...]}}
            raw = raw.get("data") or []
        if not isinstance(raw, list) or not raw:
            continue
        frame = pd.DataFrame(raw)
        frame["date"] = pd.to_datetime(frame["tradeDt"], format="%Y%m%d")
        frame["score"] = pd.to_numeric(frame["value"], errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.sort_values("date").drop_duplicates("date", keep="last")
        frame["index_code"] = code
        frame["index_name"] = meta_rows[code]["index_name"]
        frame["category"] = meta_rows[code]["category"]
        frame["obs_no"] = np.arange(len(frame))
        coverage.append(
            {
                "index_code": code,
                "index_name": meta_rows[code]["index_name"],
                "category": meta_rows[code]["category"],
                "start_date": frame["date"].min().strftime("%Y-%m-%d"),
                "end_date": frame["date"].max().strftime("%Y-%m-%d"),
                "observations": len(frame),
                "score_missing": int(frame["score"].isna().sum()),
                "close_missing": int(frame["close"].isna().sum()),
                "close_nonpositive": int((frame["close"] <= 0).sum()),
                "daily_return_abs_gt_35pct": 0,
            }
        )
        frames.append(frame[["date", "index_code", "index_name", "category", "obs_no", "score", "close"]])
    panel = pd.concat(frames, ignore_index=True).sort_values(["index_code", "date"])
    panel["calendar_group"] = np.select(
        [panel["index_code"].str.endswith(".HK"), panel["index_code"].str.endswith(".GI")],
        ["HK", "GLOBAL"],
        default="A",
    )
    calendar_maps = {}
    for group, dates in panel.groupby("calendar_group")["date"]:
        calendar_maps[group] = {date: i for i, date in enumerate(sorted(dates.unique()))}
    panel["calendar_pos"] = [
        calendar_maps[group][date] for group, date in zip(panel["calendar_group"], panel["date"])
    ]
    coverage_frame = pd.DataFrame(coverage).set_index("index_code")
    for code, frame in panel.groupby("index_code", sort=False):
        positions = frame["calendar_pos"].to_numpy(int)
        expected = int(positions[-1] - positions[0] + 1)
        missing = int(expected - len(frame))
        contiguous_return = frame["close"].pct_change(fill_method=None).where(
            frame["calendar_pos"].diff() == 1
        )
        coverage_frame.loc[code, "missing_calendar_dates"] = missing
        coverage_frame.loc[code, "missing_calendar_ratio"] = missing / expected if expected else 0.0
        coverage_frame.loc[code, "daily_return_abs_gt_35pct"] = int(
            (contiguous_return.abs() > 0.35).sum()
        )
    coverage_frame = coverage_frame.reset_index().sort_values(
        ["observations", "index_code"], ascending=[False, True]
    )
    return panel, coverage_frame, meta


def add_features(panel: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for _, frame in panel.groupby("index_code", sort=False):
        frame = frame.copy().sort_values("date")
        close_by_pos = dict(zip(frame["calendar_pos"], frame["close"]))
        score_by_pos = dict(zip(frame["calendar_pos"], frame["score"]))
        date_by_pos = dict(zip(frame["calendar_pos"], frame["date"]))
        for lag in (5, 20, 60):
            lag_pos = frame["calendar_pos"] - lag
            lag_close = lag_pos.map(close_by_pos)
            lag_score = lag_pos.map(score_by_pos)
            frame[f"delta{lag}"] = frame["score"] - lag_score
            frame[f"ret{lag}"] = frame["close"] / lag_close - 1.0
        frame["score_pct3y"] = (
            frame["score"].rolling(756, min_periods=504).rank(method="average", pct=True) * 100.0
        )
        for horizon in HORIZONS:
            future_pos = frame["calendar_pos"] + horizon
            future_close = future_pos.map(close_by_pos)
            frame[f"fwd_{horizon}"] = future_close / frame["close"] - 1.0
            frame[f"future_date_{horizon}"] = future_pos.map(date_by_pos)
        parts.append(frame)
    out = pd.concat(parts, ignore_index=True)

    def cs_rank(column: str, ascending: bool = True) -> pd.Series:
        values = out[column] if ascending else -out[column]
        return values.groupby(out["date"]).rank(method="average", pct=True) - 0.5

    cold = cs_rank("score", ascending=False)
    hot = -cold
    rise5 = cs_rank("delta5")
    rise20 = cs_rank("delta20")
    mom20 = cs_rank("ret20")
    mom60 = cs_rank("ret60")
    out["cold_level"] = -out["score"]
    out["crowding_rise_5"] = out["delta5"]
    out["crowding_rise_20"] = out["delta20"]
    out["price_mom_20"] = out["ret20"]
    out["price_mom_60"] = out["ret60"]
    out["cold_plus_mom20"] = cold + mom20
    out["hot_plus_mom20"] = hot + mom20
    out["cold_turnup"] = cold + rise5
    out["cold_recovery_full"] = cold + rise5 + mom20
    out["three_year_cold"] = -out["score_pct3y"]
    out["crowding_price_divergence"] = rise20 - mom20
    out["trend_confirmed_crowding"] = rise20 + mom20

    out["low"] = out["score"] <= 15
    out["high"] = out["score"] >= 85
    out["low_rising"] = out["low"] & (out["delta5"] > 0)
    out["low_falling"] = out["low"] & (out["delta5"] < 0)
    out["low_price_up"] = out["low"] & (out["ret20"] > 0)
    out["low_recovery"] = out["low_rising"] & (out["ret20"] > 0)
    out["high_rising"] = out["high"] & (out["delta5"] > 0)
    out["high_falling"] = out["high"] & (out["delta5"] < 0)
    out["high_price_down"] = out["high"] & (out["ret20"] < 0)
    out["high_breakdown"] = out["high_falling"] & (out["ret20"] < 0)

    for horizon in HORIZONS:
        out[f"market_fwd_{horizon}"] = out.groupby("date")[f"fwd_{horizon}"].transform("median")
        out[f"excess_{horizon}"] = out[f"fwd_{horizon}"] - out[f"market_fwd_{horizon}"]
    return out


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < MIN_CROSS_SECTION:
        return math.nan
    x = x - x.mean()
    y = y - y.mean()
    denom = math.sqrt(float(np.dot(x, x) * np.dot(y, y)))
    return float(np.dot(x, y) / denom) if denom > 0 else math.nan


def daily_ic_and_spread(panel: pd.DataFrame, signal: str, horizon: int) -> pd.DataFrame:
    needed = panel[["date", signal, f"fwd_{horizon}"]].dropna()
    records = []
    for date, frame in needed.groupby("date", sort=True):
        if len(frame) < MIN_CROSS_SECTION:
            continue
        x = frame[signal].rank(method="average", pct=True).to_numpy(float)
        y = frame[f"fwd_{horizon}"].rank(method="average", pct=True).to_numpy(float)
        raw_signal = frame[signal].to_numpy(float)
        raw_return = frame[f"fwd_{horizon}"].to_numpy(float)
        q = pd.Series(raw_signal).rank(method="average", pct=True).to_numpy()
        top = raw_return[q >= 0.8]
        bottom = raw_return[q <= 0.2]
        records.append(
            {
                "date": date,
                "ic": pearson(x, y),
                "spread": float(np.mean(top) - np.mean(bottom)),
                "n": len(frame),
            }
        )
    return pd.DataFrame(records)


def newey_west_t(values: pd.Series, lag: int) -> float | None:
    x = values.dropna().to_numpy(float)
    n = len(x)
    if n < max(30, lag + 5):
        return None
    demeaned = x - x.mean()
    gamma0 = float(np.dot(demeaned, demeaned) / n)
    variance = gamma0
    effective_lag = min(lag, n - 2)
    for k in range(1, effective_lag + 1):
        gamma = float(np.dot(demeaned[k:], demeaned[:-k]) / n)
        weight = 1.0 - k / (effective_lag + 1.0)
        variance += 2.0 * weight * gamma
    se_mean = math.sqrt(max(variance, 0.0) / n)
    return float(x.mean() / se_mean) if se_mean > 0 else None


def moving_block_ci(values: pd.Series, block: int, reps: int = 500) -> tuple[float | None, float | None]:
    x = values.dropna().to_numpy(float)
    n = len(x)
    if n < 40:
        return None, None
    block = max(1, min(block, n))
    rng = np.random.default_rng(RNG_SEED + n + block)
    starts = np.arange(0, n - block + 1)
    means = np.empty(reps)
    blocks_needed = math.ceil(n / block)
    for i in range(reps):
        picks = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate([x[start : start + block] for start in picks])[:n]
        means[i] = sample.mean()
    low, high = np.quantile(means, [0.025, 0.975])
    return float(low), float(high)


def summarize_daily(series: pd.DataFrame, signal: str, horizon: int, split: str) -> dict:
    if split == "train":
        sample = series[series["date"] <= CUTOFF]
    elif split == "test":
        sample = series[series["date"] > CUTOFF]
    else:
        sample = series
    ic_low, ic_high = moving_block_ci(sample["ic"], block=max(5, min(horizon, 60)))
    spread_low, spread_high = moving_block_ci(sample["spread"], block=max(5, min(horizon, 60)))
    return {
        "signal": signal,
        "signal_label": SIGNAL_LABELS[signal],
        "horizon_days": horizon,
        "split": split,
        "start_date": sample["date"].min().strftime("%Y-%m-%d") if not sample.empty else None,
        "end_date": sample["date"].max().strftime("%Y-%m-%d") if not sample.empty else None,
        "date_count": len(sample),
        "mean_ic": float(sample["ic"].mean()) if not sample.empty else None,
        "median_ic": float(sample["ic"].median()) if not sample.empty else None,
        "ic_positive_pct": float((sample["ic"] > 0).mean() * 100.0) if not sample.empty else None,
        "ic_nw_t": newey_west_t(sample["ic"], lag=max(1, horizon - 1)),
        "ic_ci_low": ic_low,
        "ic_ci_high": ic_high,
        "quintile_spread_mean_pct": float(sample["spread"].mean() * 100.0) if not sample.empty else None,
        "quintile_spread_nw_t": newey_west_t(sample["spread"], lag=max(1, horizon - 1)),
        "spread_ci_low_pct": spread_low * 100.0 if spread_low is not None else None,
        "spread_ci_high_pct": spread_high * 100.0 if spread_high is not None else None,
    }


def continuous_results(panel: pd.DataFrame) -> pd.DataFrame:
    records = []
    for signal in SIGNAL_LABELS:
        for horizon in HORIZONS:
            daily = daily_ic_and_spread(panel, signal, horizon)
            for split in ("train", "test", "full"):
                records.append(summarize_daily(daily, signal, horizon, split))
        print(f"continuous signal complete: {signal}", flush=True)
    return pd.DataFrame(records)


def deduplicated_events(frame: pd.DataFrame, event: str, horizon: int, split: str) -> pd.DataFrame:
    mask = frame[event].fillna(False) & frame[f"fwd_{horizon}"].notna()
    if split == "train":
        mask &= frame[f"future_date_{horizon}"] <= CUTOFF
    elif split == "test":
        mask &= frame["date"] > CUTOFF
    candidates = frame.loc[
        mask,
        ["index_code", "date", "obs_no", f"fwd_{horizon}", f"excess_{horizon}"],
    ].sort_values(["index_code", "obs_no"])
    selected = []
    for _, group in candidates.groupby("index_code", sort=False):
        last = -10**9
        for row in group.itertuples(index=False):
            if row.obs_no - last >= horizon:
                selected.append(row)
                last = row.obs_no
    if not selected:
        return pd.DataFrame(columns=candidates.columns)
    return pd.DataFrame(selected, columns=candidates.columns)


def clustered_date_ci(events: pd.DataFrame, value_col: str, reps: int = 1000) -> tuple[float | None, float | None]:
    if events.empty:
        return None, None
    grouped = events.groupby("date")[value_col].agg(["sum", "count"]).dropna()
    if len(grouped) < 20:
        return None, None
    sums = grouped["sum"].to_numpy(float)
    counts = grouped["count"].to_numpy(float)
    rng = np.random.default_rng(RNG_SEED + len(grouped))
    indices = rng.integers(0, len(grouped), size=(reps, len(grouped)))
    means = sums[indices].sum(axis=1) / counts[indices].sum(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return float(low), float(high)


def event_results(panel: pd.DataFrame) -> pd.DataFrame:
    records = []
    for event in EVENT_LABELS:
        for horizon in HORIZONS:
            for split in ("train", "test", "full"):
                sample = deduplicated_events(panel, event, horizon, split)
                fwd = f"fwd_{horizon}"
                excess = f"excess_{horizon}"
                ci_low, ci_high = clustered_date_ci(sample, excess)
                records.append(
                    {
                        "event": event,
                        "event_label": EVENT_LABELS[event],
                        "horizon_days": horizon,
                        "split": split,
                        "event_count": len(sample),
                        "date_count": sample["date"].nunique() if not sample.empty else 0,
                        "index_count": sample["index_code"].nunique() if not sample.empty else 0,
                        "mean_return_pct": float(sample[fwd].mean() * 100.0) if not sample.empty else None,
                        "median_return_pct": float(sample[fwd].median() * 100.0) if not sample.empty else None,
                        "positive_return_pct": float((sample[fwd] > 0).mean() * 100.0) if not sample.empty else None,
                        "p10_return_pct": float(sample[fwd].quantile(0.10) * 100.0) if not sample.empty else None,
                        "mean_excess_return_pct": float(sample[excess].mean() * 100.0) if not sample.empty else None,
                        "excess_ci_low_pct": ci_low * 100.0 if ci_low is not None else None,
                        "excess_ci_high_pct": ci_high * 100.0 if ci_high is not None else None,
                    }
                )
        print(f"event complete: {event}", flush=True)
    return pd.DataFrame(records)


def fmt(value: float | None, digits: int = 3) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{value:.{digits}f}"


def render_report(
    panel: pd.DataFrame,
    coverage: pd.DataFrame,
    continuous: pd.DataFrame,
    events: pd.DataFrame,
) -> str:
    lines = [
        "# 指数拥挤度因子预测力研究",
        "",
        f"数据截止：{panel['date'].max():%Y-%m-%d}；训练/样本外切分：{CUTOFF:%Y-%m-%d}。",
        "",
        "## 数据覆盖与完整性",
        "",
        f"- 原版当前覆盖211个指数；其中198个有历史拥挤度和收盘价。",
        f"- 历史面板共{len(panel):,}条指数-日观测，起点{panel['date'].min():%Y-%m-%d}，最长约10年。",
        f"- 至少2,000个交易日的指数：{int((coverage['observations'] >= 2000).sum())}个；至少1,200日：{int((coverage['observations'] >= 1200).sum())}个。",
        f"- 收盘价缺失{int(coverage['close_missing'].sum())}条，非正价格{int(coverage['close_nonpositive'].sum())}条；按交易日历剔除跨缺口收益后，单日绝对涨跌超过35%的观测{int(coverage['daily_return_abs_gt_35pct'].sum())}条。",
        f"- 历史缺失交易日超过各自覆盖期1%的指数：{int((coverage['missing_calendar_ratio'] > 0.01).sum())}个；所有特征与前瞻收益均要求交易日位置连续。",
        "",
        "## 样本外连续因子排名",
        "",
        "以下数值为2023-08-07之后的日度横截面 Spearman IC；正数表示因子越高，后续相对收益越高。五分位差为每日最高20%减最低20%。",
        "",
    ]
    for horizon in HORIZONS:
        subset = continuous[(continuous["split"] == "test") & (continuous["horizon_days"] == horizon)].copy()
        subset = subset.sort_values("mean_ic", ascending=False).head(5)
        lines.extend([f"### 前瞻{horizon}日", ""])
        for row in subset.itertuples(index=False):
            lines.append(
                f"- {row.signal_label}: IC {fmt(row.mean_ic)}, NW t={fmt(row.ic_nw_t, 2)}，"
                f"五分位差 {fmt(row.quintile_spread_mean_pct, 2)}%。"
            )
        lines.append("")

    lines.extend(["## 极端状态事件（样本外、按指数去重）", ""])
    for horizon in (20, 60, 120, 252):
        subset = events[(events["split"] == "test") & (events["horizon_days"] == horizon)].copy()
        subset = subset[subset["event_count"] >= 50].sort_values("mean_excess_return_pct", ascending=False)
        best = subset.head(3)
        worst = subset.tail(3).sort_values("mean_excess_return_pct")
        lines.extend([f"### 前瞻{horizon}日：相对收益最好", ""])
        for row in best.itertuples(index=False):
            lines.append(
                f"- {row.event_label}: n={row.event_count}，平均收益 {fmt(row.mean_return_pct, 2)}%，"
                f"相对全市场中位数 {fmt(row.mean_excess_return_pct, 2)}%，胜率 {fmt(row.positive_return_pct, 1)}%。"
            )
        lines.extend(["", f"### 前瞻{horizon}日：相对收益最差", ""])
        for row in worst.itertuples(index=False):
            lines.append(
                f"- {row.event_label}: n={row.event_count}，平均收益 {fmt(row.mean_return_pct, 2)}%，"
                f"相对全市场中位数 {fmt(row.mean_excess_return_pct, 2)}%，10%下行分位 {fmt(row.p10_return_pct, 2)}%。"
            )
        lines.append("")

    lines.extend(
        [
            "## 方法与限制",
            "",
            "- 连续因子用每日横截面预测IC和五分位差；重叠前瞻窗口用 Newey-West t 值和移动区块 bootstrap。",
            "- 极端事件按单个指数至少间隔一个完整前瞻周期抽样，避免原版把相邻日期重复计数造成虚高胜率。置信区间按事件日期聚类 bootstrap。",
            "- 当前接口只返回当前覆盖指数的历史，存在指数存续和产品覆盖幸存者偏差；大量指数成分重叠，显著性仍可能偏高。",
            "- 本研究检验的是原版最终拥挤度及其衍生信号；原始六项因子没有历史明细，不能据此断言某个底层子因子更强。",
            "- 价格为原版接口收盘字段，各指数本币收益；横截面比较使用百分比收益，不混合价格点位。A股与港股分别建立交易日历，跨数据缺口的收益全部剔除。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel, coverage, meta = load_panel()
    print(f"loaded panel rows={len(panel)} indices={panel['index_code'].nunique()}", flush=True)
    panel = add_features(panel)
    print("features complete", flush=True)
    continuous = continuous_results(panel)
    events = event_results(panel)
    coverage.to_csv(OUT_DIR / "data-coverage.csv", index=False, encoding="utf-8-sig")
    continuous.to_csv(OUT_DIR / "continuous-factor-results.csv", index=False, encoding="utf-8-sig")
    events.to_csv(OUT_DIR / "event-results.csv", index=False, encoding="utf-8-sig")
    summary = {
        "ok": True,
        "source_trade_date": meta["trade_date"],
        "panel_start": panel["date"].min().strftime("%Y-%m-%d"),
        "panel_end": panel["date"].max().strftime("%Y-%m-%d"),
        "train_test_cutoff": CUTOFF.strftime("%Y-%m-%d"),
        "index_count": int(panel["index_code"].nunique()),
        "panel_rows": len(panel),
        "category_counts": dict(Counter(coverage["category"])),
        "horizons": list(HORIZONS),
        "signals": SIGNAL_LABELS,
        "events": EVENT_LABELS,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = render_report(panel, coverage, continuous, events)
    (OUT_DIR / "latest-report.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
