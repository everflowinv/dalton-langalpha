"""collect.py 一致预期跨快照比较的期间对齐（复审 #10）。

同名相对键 0q/+1q/0y/+1y 在两份快照里可能指向不同真实季度（0q 从 3Q26 滚到
4Q26）：同名直减把期间滚动误报成预期上修/下修。这里锁定：
1. 快照行带上 Yahoo earningsTrend 的真实 endDate（yfinance 公开 DataFrame 丢弃
   了它，collect 从 Analysis 缓存的原始列表读回——用真实 Analysis 对象 + 真实
   trend 载荷锁定数据形状）；
2. 跨快照比较按 period_end 匹配：同一真实期间挂在别的键下也算修正；期间滚动与
   任一侧无期间信息都不算修正、不触发阈值，只在 period_alignment 里标注；
3. packet.md 把滚动与"无期间信息"披露给分析师。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECT_SCRIPT_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py"
)


def _load_collect_module():
    spec = importlib.util.spec_from_file_location("dalton_thesis_delta_collect", COLLECT_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect_mod = _load_collect_module()

# 与 config/default-thresholds.json 一致的最小阈值组（self-test 同款）。
THR = {
    "eps_fy1_vs_last_pct": 1.0,
    "eps_fy1_vs_7d_pct": 2.0,
    "rev_fy1_or_q1_vs_last_pct": 0.7,
    "target_price_vs_last_pct": 2.0,
    "down_revisions_7d_min": 5,
    "price_1d_pct": 4.0,
    "price_5d_pct": 8.0,
    "any_new_sec_filing": True,
}


def _ee(avg: float, end: str | None, ccy: str = "USD") -> dict[str, Any]:
    row: dict[str, Any] = {"avg": avg, "currency": ccy}
    if end is not None:
        row["period_end"] = end
    return row


# --------------------------------------------------------------- 快照采集


def _real_analysis_with_trend() -> Any:
    """真实 yfinance Analysis 对象，注入 Yahoo earningsTrend 的原始载荷（与
    _fetch_earnings_trend 落盘的缓存同构）：每个期间带 endDate。"""
    from yfinance.scrapers.analysis import Analysis

    def _period(per: str, end: str, eps_avg: float, rev_avg: float) -> dict[str, Any]:
        return {
            "period": per,
            "endDate": end,
            "earningsEstimate": {
                "avg": {"raw": eps_avg},
                "low": {"raw": eps_avg - 0.1},
                "high": {"raw": eps_avg + 0.1},
                "numberOfAnalysts": {"raw": 12},
                "yearAgoEps": {"raw": eps_avg - 0.2},
                "growth": {"raw": 0.08},
                "earningsCurrency": "USD",
            },
            "revenueEstimate": {
                "avg": {"raw": rev_avg},
                "low": {"raw": rev_avg * 0.95},
                "high": {"raw": rev_avg * 1.05},
                "numberOfAnalysts": {"raw": 12},
                "yearAgoRevenue": {"raw": rev_avg * 0.9},
                "growth": {"raw": 0.1},
                "revenueCurrency": "USD",
            },
            "epsTrend": {
                "current": {"raw": eps_avg},
                "7daysAgo": {"raw": eps_avg * 0.99},
                "30daysAgo": {"raw": eps_avg * 0.98},
                "epsTrendCurrency": "USD",
            },
            "epsRevisions": {
                "upLast7days": {"raw": 3},
                "downLast7Days": {"raw": 1},
                "upLast30Days": {"raw": 6},
                "downLast30Days": {"raw": 2},
                "epsRevisionsCurrency": "USD",
            },
        }

    analysis = Analysis(None, "TEST")
    analysis._earnings_trend = [
        _period("0q", "2026-12-31", 1.03, 2.07e9),
        _period("+1q", "2027-03-31", 1.11, 2.2e9),
        _period("0y", "2026-12-31", 4.3, 9.1e9),
        _period("+1y", "2027-12-31", 4.49, 10.1e9),
    ]
    return analysis


def test_snapshot_carries_period_end_from_yahoo_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_consensus_snapshot 把 Yahoo 原始 trend 的 endDate 盖进两张预期表；
    yfinance 公开 DataFrame 丢了它，所以走 Analysis 缓存的原始列表。"""
    import yfinance as yf

    analysis = _real_analysis_with_trend()
    ticker_stub = type("TickerStub", (), {})()
    ticker_stub._analysis = analysis
    # fetch_consensus_snapshot 经 getattr(t, field) 读四个表属性——用真实
    # Analysis 的属性结果，锁定 yfinance 1.7.0 的真实数据形状。
    for field in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"):
        setattr(ticker_stub, field, getattr(analysis, field))
    ticker_stub.analyst_price_targets = {
        "mean": 257.37,
        "median": 260.0,
        "low": 200.0,
        "high": 300.0,
    }
    ticker_factory = create_autospec(yf.Ticker)
    ticker_factory.side_effect = lambda _symbol: ticker_stub
    monkeypatch.setattr(yf, "Ticker", ticker_factory)
    monkeypatch.setattr(
        yf,
        "download",
        create_autospec(yf.download, side_effect=RuntimeError("no network in unit test")),
    )

    snapshot = collect_mod.fetch_consensus_snapshot("TEST", benchmark="QQQ")

    ee = snapshot["earnings_estimate"]
    assert ee["0q"]["avg"] == 1.03
    assert ee["0q"]["period_end"] == "2026-12-31"
    assert ee["+1y"]["period_end"] == "2027-12-31"
    re_ = snapshot["revenue_estimate"]
    assert re_["0q"]["period_end"] == "2026-12-31"
    assert re_["+1y"]["avg"] == 10.1e9
    # price 字段失败只记 error，不影响期间盖章。
    assert snapshot["price"].get("error")


# --------------------------------------------------------------- 跨快照比较


def test_q4_and_fy_same_end_date_do_not_cross_match() -> None:
    """codex r1 #1：Q4 与财年共用结束日（12-31）。两份四期间快照数值完全相同——
    只按结束日配对时 0y 会配到旧 0q（季度 EPS 1.03 配全年 4.3，+317% 假修正）；
    按（粒度, 结束日）配对后所有 vs_last 为 0.0，且不出现跨粒度 / 跨键配对。"""
    def snap(fetched: str) -> dict[str, Any]:
        return {
            "fetched_at": fetched,
            "earnings_estimate": {
                "0q": _ee(1.03, "2026-12-31"),
                "+1q": _ee(1.11, "2027-03-31"),
                "0y": _ee(4.3, "2026-12-31"),
                "+1y": _ee(4.49, "2027-12-31"),
            },
            "revenue_estimate": {
                "0q": _ee(2.07e9, "2026-12-31"),
                "+1q": _ee(2.2e9, "2027-03-31"),
                "0y": _ee(9.1e9, "2026-12-31"),
                "+1y": _ee(10.1e9, "2027-12-31"),
            },
        }

    cur = snap("2026-10-30T23:00:00-04:00")
    prev = snap("2026-10-29T23:00:00-04:00")
    deltas, flags = collect_mod.compute_consensus_deltas(cur, prev, THR)
    for key in (
        "eps_0q_vs_last_pct", "eps_+1q_vs_last_pct", "eps_0y_vs_last_pct", "eps_+1y_vs_last_pct",
        "rev_0q_vs_last_pct", "rev_+1q_vs_last_pct", "rev_0y_vs_last_pct", "rev_+1y_vs_last_pct",
    ):
        assert deltas[key] == 0.0, key
    pa = deltas["period_alignment"]
    for table in ("earnings_estimate", "revenue_estimate"):
        for per in ("0q", "+1q", "0y", "+1y"):
            assert pa[table][per]["compare"] == "matched", (table, per)
            assert "prev_key" not in pa[table][per], (table, per)
    assert not any("较上次运行" in f for f in flags), flags


def test_period_roll_is_not_a_revision() -> None:
    """复审 #10 的原始形态：DASH 的 0q 从 3Q26（截至 09-30）滚到 4Q26（截至
    12-31），同名直减会得到 +3.39% 的假收入上修——必须拒绝并标注滚动。"""
    prev = {
        "fetched_at": "2026-10-29T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(0.996, "2026-09-30")},
        "revenue_estimate": {"0q": _ee(2.002e9, "2026-09-30")},
    }
    cur = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.03, "2026-12-31")},
        "revenue_estimate": {"0q": _ee(2.07e9, "2026-12-31")},
    }
    deltas, flags = collect_mod.compute_consensus_deltas(cur, prev, THR)
    assert deltas["eps_0q_vs_last_pct"] is None
    assert deltas["rev_0q_vs_last_pct"] is None
    alignment = deltas["period_alignment"]["earnings_estimate"]["0q"]
    assert alignment["compare"] == "rolled"
    assert alignment["cur_period_end"] == "2026-12-31"
    assert alignment["prev_same_key_period_end"] == "2026-09-30"
    # 假上修 +3.39% 远超 rev 阈值 0.7%，但不得出现任何修正类提示。
    assert not any("较上次运行" in f for f in flags), flags


def test_same_real_period_under_different_key_is_a_revision() -> None:
    """本次滚动后的 0q（截至 12-31）就是上一份的 +1q：同一真实期间挂在别的
    相对键下，对齐后比较才是真正的预期修正。"""
    prev = {
        "fetched_at": "2026-10-29T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(0.996, "2026-09-30"), "+1q": _ee(1.0, "2026-12-31")},
        "revenue_estimate": {"0q": _ee(2.002e9, "2026-09-30"), "+1q": _ee(2.0e9, "2026-12-31")},
    }
    cur = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.03, "2026-12-31")},
        "revenue_estimate": {"0q": _ee(2.07e9, "2026-12-31")},
    }
    deltas, flags = collect_mod.compute_consensus_deltas(cur, prev, THR)
    assert deltas["eps_0q_vs_last_pct"] == 3.0  # 1.03 / 1.0 - 1
    assert deltas["rev_0q_vs_last_pct"] == 3.5  # 2.07 / 2.0 - 1
    alignment = deltas["period_alignment"]["earnings_estimate"]["0q"]
    assert alignment["compare"] == "matched"
    assert alignment["prev_key"] == "+1q"
    # 0q 不在阈值检查列表（只有 +1y/0y/+1q 出提示），但真修正不会像滚动那样
    # 被压成 None——上面的两个非 None 断言就是"匹配即计算"的锁定。


def test_old_snapshot_without_period_info_refuses_comparison() -> None:
    """旧快照（改造前的存量文件）没有 period_end：不算 revision、不猜，标注
    "旧快照无期间信息，无法比较"。"""
    prev = {
        "fetched_at": "2026-10-29T23:00:00-04:00",
        "earnings_estimate": {"0q": {"avg": 0.996, "currency": "USD"}},
        "revenue_estimate": {"0q": {"avg": 2.002e9, "currency": "USD"}},
    }
    cur = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.03, "2026-12-31")},
        "revenue_estimate": {"0q": _ee(2.07e9, "2026-12-31")},
    }
    deltas, flags = collect_mod.compute_consensus_deltas(cur, prev, THR)
    assert deltas["eps_0q_vs_last_pct"] is None
    assert deltas["rev_0q_vs_last_pct"] is None
    for table in ("earnings_estimate", "revenue_estimate"):
        assert deltas["period_alignment"][table]["0q"] == {
            "compare": "no_period_info",
            "side": "previous",
        }
    assert not any("较上次运行" in f for f in flags), flags


def test_current_snapshot_without_period_info_refuses_comparison() -> None:
    """本次快照取不到 endDate（yfinance 结构漂移）同样拒绝比较——"不要猜"
    对两侧成立，避免回到同名直减。"""
    prev = {
        "fetched_at": "2026-10-29T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(0.996, "2026-09-30")},
    }
    cur = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": {"avg": 1.03, "currency": "USD"}},
    }
    deltas, _flags = collect_mod.compute_consensus_deltas(cur, prev, THR)
    assert deltas["eps_0q_vs_last_pct"] is None
    assert deltas["period_alignment"]["earnings_estimate"]["0q"] == {
        "compare": "no_period_info",
        "side": "current",
    }


def test_matched_period_currency_mismatch_still_refused() -> None:
    """按期间对齐后，币种不一致仍拒绝计算（原有币种防护保留）。"""
    prev = {
        "fetched_at": "2026-10-29T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.0, "2026-12-31", ccy="USD")},
    }
    cur = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.03, "2026-12-31", ccy="CNY")},
    }
    deltas, _flags = collect_mod.compute_consensus_deltas(cur, prev, THR)
    assert deltas["eps_0q_vs_last_pct"] is None
    assert deltas["currency_mismatch"] is True


def test_same_fetched_at_skips_comparison_entirely() -> None:
    """周末/节假日快照未更新（fetched_at 相同）仍整体跳过，不产生对齐标注。"""
    snap = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.03, "2026-12-31")},
    }
    deltas, _flags = collect_mod.compute_consensus_deltas(snap, dict(snap), THR)
    assert deltas.get("skipped_same_fetched_at") is True
    assert "period_alignment" not in deltas


# --------------------------------------------------------------- packet 披露


def test_packet_md_discloses_period_alignment() -> None:
    cur = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.03, "2026-12-31")},
        "revenue_estimate": {"0q": _ee(2.07e9, "2026-12-31")},
    }
    prev = {
        "fetched_at": "2026-10-29T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(0.996, "2026-09-30"), "+1q": _ee(1.0, "2026-12-31")},
        "revenue_estimate": {"0q": _ee(2.002e9, "2026-09-30"), "+1q": _ee(2.0e9, "2026-12-31")},
    }
    deltas, _flags = collect_mod.compute_consensus_deltas(cur, prev, THR)
    packet = {
        "ticker": "DASH",
        "run_id": "20261030-2300",
        "since": "2026-10-29",
        "until": "2026-10-30",
        "consensus": cur,
        "deltas": deltas,
        "auto_flags": [],
        "internal": {"status": {}, "new": {}},
        "alphaengine": {"status": [], "new": {}},
        "sec": {"status": [], "new": {}},
    }
    md = collect_mod.render_packet_md(packet)
    assert "0q 与上一份快照的 +1q 是同一期间（截至 2026-12-31）" in md
    assert "较上次变化即对该期间计算" in md

    # 纯滚动（上一份任何键下都没有本次 0q 的真实期间）要有滚动披露。
    rolled_cur = {
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(1.03, "2026-12-31")},
        "revenue_estimate": {"0q": _ee(2.07e9, "2026-12-31")},
    }
    rolled_prev = {
        "fetched_at": "2026-10-29T23:00:00-04:00",
        "earnings_estimate": {"0q": _ee(0.996, "2026-09-30")},
        "revenue_estimate": {"0q": _ee(2.002e9, "2026-09-30")},
    }
    deltas_roll, _ = collect_mod.compute_consensus_deltas(rolled_cur, rolled_prev, THR)
    md_roll = collect_mod.render_packet_md({**packet, "deltas": deltas_roll})
    assert "期间滚动：earnings_estimate 0q 由截至 2026-09-30 滚动到截至 2026-12-31" in md_roll
    assert "不算预期修正" in md_roll

    # 旧快照无期间信息的披露。
    deltas_old, _ = collect_mod.compute_consensus_deltas(
        rolled_cur,
        {
            "fetched_at": "2026-10-28T23:00:00-04:00",
            "earnings_estimate": {"0q": {"avg": 0.99}},
            "revenue_estimate": {},
        },
        THR,
    )
    md_old = collect_mod.render_packet_md({**packet, "deltas": deltas_old})
    assert "旧快照无期间信息" in md_old
    assert "未计算较上次变化" in md_old
    # deltas 原文保留结构化对齐结果（代理读 packet.json 用）。
    assert "period_alignment" in json.dumps(deltas, ensure_ascii=False)
