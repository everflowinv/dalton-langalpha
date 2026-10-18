#!/usr/bin/env python3
"""
make_price_triggers.py — 为覆盖名单中的 Core 公司批量生成涨跌价格触发自动任务文件（PS.10）

生成文件名符合 PK.15 规范：
  - dalton-<t>-move-down.json
  - dalton-<t>-move-up.json
其中 <t> 为 ticker 小写并将 '.' 替换为 '-'。

用法：
  python3 make_price_triggers.py --coverage ../Home/dalton/coverage.yaml --tier Core --out-dir .agents/user/automations
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# 导入同目录的 coverage.yaml 解析辅助函数
sys.path.insert(0, str(Path(__file__).resolve().parent))
from move_quant import PRIVATE_TICKERS, load_coverage_yaml  # noqa: E402

DEFAULT_THRESHOLD_PCT = 4.0  # 默认取自 stock_tracker 配置的 price_1d_pct: 4.0


def slug_ticker(ticker: str) -> str:
    return ticker.strip().lower().replace(".", "-")


def build_trigger_pair(
    ticker: str,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    workspace_id: str | None = None,
    delivery: list[str] | None = None,
    timezone: str = "America/New_York",
) -> dict[str, dict[str, Any]]:
    """为单只股票构造 move-down 与 move-up 两个自动任务 JSON 对象。"""
    t = ticker.upper().strip()
    slug = slug_ticker(t)
    ws_id = workspace_id or f"<{t} 工作区 id>"
    deliv = delivery if delivery is not None else ["<PM 通知渠道>"]
    thr = round(float(threshold_pct), 1)
    thr_str = f"{thr:g}" if thr == int(thr) else f"{thr:.1f}"

    down_obj = {
        "name": f"{t} 异动（跌）",
        "status": "active",
        "trigger_type": "price",
        "trigger_config": {
            "symbol": t,
            "market": "stock",
            "conditions": [
                {
                    "type": "pct_change_below",
                    "value": thr,
                    "reference": "previous_close",
                }
            ],
            "retrigger": {
                "mode": "recurring",
                "cooldown_seconds": None,
            },
        },
        "timezone": timezone,
        "instruction": (
            f"/dalton-stock-move {t} 今天 —— 价格触发：较昨收跌超 {thr_str}%。"
            f"先量化同组与板块，再找原因；若判定为板块 beta 且无个股事件，"
            f"只用 knowledge_append_verdict 写一条 NO_CHANGE，不推送。"
        ),
        "workspace_id": ws_id,
        "thread": "new",
        "delivery": deliv,
    }

    up_obj = {
        "name": f"{t} 异动（涨）",
        "status": "active",
        "trigger_type": "price",
        "trigger_config": {
            "symbol": t,
            "market": "stock",
            "conditions": [
                {
                    "type": "pct_change_above",
                    "value": thr,
                    "reference": "previous_close",
                }
            ],
            "retrigger": {
                "mode": "recurring",
                "cooldown_seconds": None,
            },
        },
        "timezone": timezone,
        "instruction": (
            f"/dalton-stock-move {t} 今天 —— 价格触发：较昨收涨超 {thr_str}%。"
            f"先量化同组与板块，再找原因；若判定为板块 beta 且无个股事件，"
            f"只用 knowledge_append_verdict 写一条 NO_CHANGE，不推送。"
        ),
        "workspace_id": ws_id,
        "thread": "new",
        "delivery": deliv,
    }

    return {
        f"dalton-{slug}-move-down.json": down_obj,
        f"dalton-{slug}-move-up.json": up_obj,
    }


def compute_vol_threshold(ticker: str, default_pct: float = DEFAULT_THRESHOLD_PCT) -> float:
    """高波动名字可按 2 倍 60 日日波动率单独设阈值（不低于默认 4.0%）。"""
    try:
        import yfinance as yf  # type: ignore
        df = yf.download(ticker, period="4mo", auto_adjust=False, progress=False)
        if df is None or len(df) < 25:
            return default_pct
        col = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
        if hasattr(col, "columns"):
            col = col.iloc[:, 0]
        rets = col.dropna().pct_change().dropna().tail(60) * 100.0
        if len(rets) > 20:
            two_sigma = round(float(rets.std(ddof=1)) * 2.0, 1)
            return max(default_pct, two_sigma)
    except Exception:
        pass
    return default_pct


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成 Core 公司价格触发自动任务文件 (PS.10)")
    parser.add_argument("--coverage", required=True, help="coverage.yaml 文件路径")
    parser.add_argument("--tier", default="Core", help="筛选层级（默认 Core）")
    parser.add_argument("--tickers", help="仅生成指定代码（逗号分隔，如 META,AMZN,GOOGL,MSFT）")
    parser.add_argument("--out-dir", required=True, help="输出目录路径")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT, help="默认单日涨跌触发阈值百分数（默认 4.0）")
    parser.add_argument("--compute-vol", action="store_true", help="对高波动股票按 2 倍 60 日日波动率动态计算阈值")
    parser.add_argument("--include-non-us", action="store_true", help="是否包含港股/A股（本阶段默认只生成美股）")
    parser.add_argument("--delivery", help="指定 PM 通知渠道（逗号分隔）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")

    args = parser.parse_args()
    companies = load_coverage_yaml(args.coverage)
    tier_target = args.tier.strip().lower()
    only_tickers = (
        {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        if args.tickers
        else None
    )
    delivery_list = [d.strip() for d in args.delivery.split(",") if d.strip()] if args.delivery else None

    # 当 coverage.yaml 不存在但传了 --tickers 时支持直接按 --tickers 生成
    if not companies and only_tickers:
        companies = [{"ticker": t, "tier": "core", "market": "US"} for t in sorted(only_tickers)]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for c in companies:
        ticker = str(c.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        if only_tickers and ticker not in only_tickers:
            continue
        c_tier = str(c.get("tier") or "").strip().lower()
        if c_tier != tier_target:
            continue
        if ticker in PRIVATE_TICKERS:
            skipped.append({"ticker": ticker, "reason": "private_or_no_market_data"})
            continue
        market = str(c.get("market") or "US").upper()
        if not args.include_non_us and (market != "US" or "." in ticker or len(ticker) > 10):
            skipped.append({"ticker": ticker, "reason": "non_us_deferred_to_p6"})
            continue
        if not re.match(r"^[A-Z0-9-]{1,10}$", ticker):
            skipped.append({"ticker": ticker, "reason": "invalid_symbol_format"})
            continue

        thr = compute_vol_threshold(ticker, args.threshold) if args.compute_vol else args.threshold
        ws_id = None
        if isinstance(c.get("workspace"), dict) and c["workspace"].get("id"):
            ws_id = str(c["workspace"]["id"])

        pair = build_trigger_pair(
            ticker=ticker,
            threshold_pct=thr,
            workspace_id=ws_id,
            delivery=delivery_list,
        )
        files_written = []
        for fname, payload in pair.items():
            fpath = out_dir / fname
            fpath.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            files_written.append(str(fpath))
        generated.append({"ticker": ticker, "threshold_pct": thr, "files": files_written})

    summary = {
        "ok": True,
        "tier": args.tier,
        "out_dir": str(out_dir),
        "generated_companies": len(generated),
        "generated_files": sum(len(g["files"]) for g in generated),
        "generated": generated,
        "skipped": skipped,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
