"""cn_price_fallback：港股/A 股日线坏数据识别与非破坏性处置（cn-hk 备用行情源）。

线上事故（线程 63711ab2、fd9e3baa）：yfinance 的 2525.HK 日线停在 HK$15.47、
成交量为 0，腾讯备用行情（cn-hk）是对的。第 2 轮审查定案为"不破坏历史"：
永远不删、不合成历史日线；判定以请求截止日为准（历史窗口不做任何替换）；
停滞尾只标注、历史指标只用停滞段之前的有效数据；只有截止到今天才用备用
报价单独计算"今日涨跌"（必须同时有现价和前收）；停牌与连续假期照实标注、
不切换；财报窗口落在停滞段里标不完整。美股不受影响。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARD_PATH = REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/scripts/cn_price_fallback.py"
_MOVE_QUANT_PATH = REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/scripts/move_quant.py"
_PRICE_REACTION_PATH = (
    REPO_ROOT / "plugins/dalton/skills/dalton-earnings-deep/scripts/price_reaction.py"
)
_TRIGGERS_PATH = (
    REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/scripts/make_price_triggers.py"
)

# 固定的"今天"：所有场景锚在这天，判定不随真实时钟漂移。
TODAY = date(2026, 11, 5)  # 周四


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load("cn_price_fallback_under_test", _GUARD_PATH)
mq = _load("move_quant_cn_fallback_under_test", _MOVE_QUANT_PATH)
pr = _load("price_reaction_cn_fallback_under_test", _PRICE_REACTION_PATH)
# 各脚本按路径加载自己的模块实例，monkeypatch 必须打在它们实际用的那份上
mq_guard = mq._cn_price_fallback()
pr_guard = pr._cn_price_fallback()


def _fix_today(monkeypatch: Any, *mods: Any, today: date = TODAY) -> None:
    for mod in mods:
        monkeypatch.setattr(
            mod,
            "hk_today",
            create_autospec(mod.hk_today, return_value=today),
        )


def _weekday_dates(end: date, n: int) -> list[date]:
    out: list[date] = []
    cur = end
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(out))


def _iso_dates(end: date, n: int) -> list[str]:
    return [d.isoformat() for d in _weekday_dates(end, n)]


# 备用报价（cn-hk quote 解析后的规格）。默认日期为 TODAY，各场景按需覆盖。
QUOTE = {
    "price": 40.1,
    "prev_close": 39.0,
    "open": 39.5,
    "volume": 1_140_000.0,
    "source": "tencent",
    "as_of": TODAY.isoformat(),
}


def _stagnant_series(n_flat: int = 5, n_valid: int = 5) -> tuple[list[str], list[float], list[float]]:
    """n_valid 根正常 bar 后接 n_flat 根"停在 15.47、量 0"的停滞尾，截止 TODAY。"""
    dates = _iso_dates(TODAY, n_valid + n_flat)
    closes = [38.0 + (i % 5) * 0.2 for i in range(n_valid)] + [15.47] * n_flat
    vols = [1_000_000.0] * n_valid + [0.0] * n_flat
    return dates, closes, vols


# ── 判定（形态与请求截止日） ────────────────────────────────────────────────


def test_history_window_is_judged_against_requested_end(monkeypatch: Any) -> None:
    """第 2 轮审查的核心回归：查询四个工作日前的历史窗口时，即使今天看已
    "落后"，也不与今天比、不调备用源、不做任何替换。"""
    _fix_today(monkeypatch, guard)
    dates = _iso_dates(date(2026, 10, 26), 5)  # 数据到 10-26 为止，完全正常
    closes = [14.0, 14.2, 14.6, 15.0, 15.47]
    vols = [1_000_000] * 5
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=dict(QUOTE))

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols,
        end=date(2026, 10, 26), fetch_quote=fetch_quote,
    )
    assert verdict is None
    fetch_quote.assert_not_called()


def test_symbol_classification_and_tickers() -> None:
    assert guard.is_cn_symbol("2525.HK") is True
    assert guard.is_cn_symbol("600519.SS") is True
    assert guard.is_cn_symbol("00700") is True
    assert guard.is_cn_symbol("HSAI") is False
    assert guard.is_cn_symbol("QQQ") is False
    assert guard.to_cn_hk_ticker("2525.HK") == "02525"
    assert guard.to_cn_hk_ticker("0700.HK") == "00700"
    assert guard.to_cn_hk_ticker("600519.SS") == "600519"


def test_parse_cn_hk_quote_accepts_gateway_shapes() -> None:
    payload = {
        "ok": True,
        "ticker": "02525",
        "rows": [
            {
                "source": "tencent",
                "time": "2026-11-02 16:08",
                "现价": 12.47,
                "昨收": 12.17,
                "今开": 12.07,
                "成交量": 11400000,
            }
        ],
    }
    quote = guard.parse_cn_hk_quote(payload)
    assert quote is not None
    assert quote["price"] == 12.47
    assert quote["prev_close"] == 12.17
    assert quote["volume"] == 11400000.0
    assert quote["source"] == "tencent"

    assert guard.parse_cn_hk_quote({"ok": True, "rows": [{"note": "closed"}]}) is None


# ── evaluate_cn_daily：非破坏性处置的场景 ──────────────────────────────────


def test_evaluate_stagnant_tail_annotates_and_computes_today_change(
    monkeypatch: Any,
) -> None:
    """停滞尾：只标注，历史指标截至停滞前；今日涨跌用备用报价单独算。"""
    _fix_today(monkeypatch, guard)
    dates, closes, vols = _stagnant_series()
    quote = {**QUOTE, "as_of": TODAY.isoformat()}
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=quote)

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["kind"] == "stagnant"
    assert verdict["stagnant_from"] == dates[5]
    assert verdict["valid_until"] == dates[4]
    assert verdict["today"]["price"] == 40.1
    assert verdict["today"]["prev_close"] == 39.0
    assert verdict["today"]["change_pct"] == 2.82
    assert verdict["today"]["source"] == "tencent"
    assert "起停在 15.47" in verdict["note"]
    assert f"截至 {dates[4]}" in verdict["note"]
    assert "不写回日线" in verdict["note"]
    assert "cn-hk(tencent)" in verdict["price_source"]
    fetch_quote.assert_called_once_with("02525")


def test_evaluate_behind_uses_quote_date_not_in_index(monkeypatch: Any) -> None:
    """落后分支：报价日期不在原索引里（日线停在 10-26、报价是 11-05）——
    落后由报价日期证实，今日涨跌单独算，不造 bar。"""
    _fix_today(monkeypatch, guard)
    dates = _iso_dates(date(2026, 10, 26), 5)
    closes = [14.0, 14.2, 14.6, 15.0, 15.47]
    vols = [1_000_000.0] * 5
    quote = {**QUOTE, "as_of": "2026-11-05 16:08"}
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=quote)

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["kind"] == "behind"
    assert verdict["stagnant_from"] is None
    assert verdict["valid_until"] == "2026-10-26"
    assert verdict["today"]["change_pct"] == 2.82
    assert "落后" in verdict["note"]


def test_evaluate_suspension_quote_no_trades_unchanged(monkeypatch: Any) -> None:
    """停牌：备用报价也无成交且价格不变——照实标注、不切换、不算今日涨跌。"""
    _fix_today(monkeypatch, guard)
    dates, closes, vols = _stagnant_series()
    quote = {
        "price": 15.47,
        "prev_close": 15.47,
        "open": 15.47,
        "volume": 0.0,
        "source": "tencent",
        "as_of": TODAY.isoformat(),
    }
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=quote)

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["kind"] == "suspended"
    assert verdict["today"] is None
    assert "疑似停牌" in verdict["note"]
    assert f"截至 {dates[4]}（停牌前）" in verdict["note"]
    assert "不切换" in verdict["note"]
    assert verdict["price_source"] == "yfinance(疑似停牌)"


def test_evaluate_holiday_is_not_bad_data(monkeypatch: Any) -> None:
    """四天以上连续假期：日线"落后"但备用报价时间不比最后一根日线新
    （还是 10-26 的收市数据）——不判坏数据，不做任何处置。"""
    _fix_today(monkeypatch, guard)
    dates = _iso_dates(date(2026, 10, 26), 5)
    closes = [14.0, 14.2, 14.6, 15.2, 15.47]
    vols = [1_000_000.0] * 5
    quote = {  # 假期里网关返回的是最后一 个交易日的行情
        "price": 15.47,
        "prev_close": 15.2,
        "open": 15.3,
        "volume": 1_140_000.0,
        "source": "tencent",
        "as_of": "2026-10-26 16:08",
    }
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=quote)

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is None
    fetch_quote.assert_called_once_with("02525")


def test_evaluate_missing_prev_close_marks_uncalculable(monkeypatch: Any) -> None:
    """缺前收：今日涨跌标"无法计算"，绝不用现价充当前收。"""
    _fix_today(monkeypatch, guard)
    dates, closes, vols = _stagnant_series()
    quote = {**QUOTE, "prev_close": None, "as_of": TODAY.isoformat()}
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=quote)

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["today"] == {
        "error": "无法计算今日涨跌（备用报价缺前收，不以现价充当前收）"
    }
    assert "无法计算" in verdict["note"]


def test_evaluate_backup_unavailable_annotates_honestly(monkeypatch: Any) -> None:
    _fix_today(monkeypatch, guard)
    dates, closes, vols = _stagnant_series()
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=None)

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["kind"] == "stagnant"
    assert verdict["today"] is None
    assert verdict["price_source"] == "yfinance(异常,备用源不可用)"
    assert "备用源本次不可用" in verdict["note"]


def test_evaluate_historical_stagnation_annotates_without_quote(monkeypatch: Any) -> None:
    """历史窗口里的停滞段：只标注"截至某日、不替换"，不调备用源。"""
    _fix_today(monkeypatch, guard)
    dates, closes, vols = _stagnant_series()
    end = date(2026, 10, 29)  # 请求截止在停滞段开始（10-30）之前
    fetch_quote = create_autospec(guard.call_cn_hk_quote, return_value=dict(QUOTE))

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, end=end, fetch_quote=fetch_quote
    )
    assert verdict is None  # 截止日之前的数据完全正常
    fetch_quote.assert_not_called()

    # 截止日落在停滞段中间：只标注，不调备用源、不算今日涨跌
    end_in_tail = date(2026, 11, 3)
    verdict2 = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, end=end_in_tail, fetch_quote=fetch_quote
    )
    assert verdict2 is not None
    assert verdict2["kind"] == "stagnant"
    assert verdict2["today"] is None
    assert verdict2["price_source"] is None
    assert "只标注、不替换" in verdict2["note"]
    assert f"截至 {dates[4]}" in verdict2["note"]
    fetch_quote.assert_not_called()


# ── move_quant 集成 ─────────────────────────────────────────────────


def _frames(
    dates: list[str], hk_closes: list[float], hk_vols: list[float]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    idx = pd.DatetimeIndex(dates)
    adj = pd.DataFrame(
        {
            "HSAI": [100.0 + i * 0.05 for i in range(len(dates))],
            "QQQ": [500.0 + i * 0.01 for i in range(len(dates))],
            "2498.HK": hk_closes,
        },
        index=idx,
        dtype=float,
    )
    vol = pd.DataFrame(
        {
            "HSAI": [1e6] * len(dates),
            "QQQ": [5e7] * len(dates),
            "2498.HK": hk_vols,
        },
        index=idx,
        dtype=float,
    )
    return adj, adj.copy(), vol, ["HSAI", "QQQ", "2498.HK"]


def _stagnant_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    """70 根，尾部 5 根停在 15.47、量 0，截止 TODAY。"""
    dates, closes, vols = _stagnant_series(n_flat=5, n_valid=65)
    return _frames(dates, closes, vols)


def test_move_quant_stagnant_tail_masked_not_replaced(monkeypatch: Any) -> None:
    """停滞同业：不替换帧（行还在），停滞段从计算中剔除（置 NaN），历史
    指标截至停滞前；今日涨跌作为判定里的单独字段，不写回日线。"""
    _fix_today(monkeypatch, mq_guard)
    fetch_quote = create_autospec(mq_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(mq_guard, "call_cn_hk_quote", fetch_quote)

    adj, close, vol, symbols = _stagnant_frames()
    dates = [d.strftime("%Y-%m-%d") for d in close.index]
    price_sources, notes, verdicts = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=TODAY
    )

    assert "2498.HK" in price_sources
    assert "cn-hk(tencent)" in price_sources["2498.HK"]
    assert len(notes) == 1 and "起停在 15.47" in notes[0]
    verdict = verdicts["2498.HK"]
    assert verdict["kind"] == "stagnant"
    assert verdict["valid_until"] == dates[64]
    assert verdict["today"]["change_pct"] == 2.82

    # 行不删（70 行都在），停滞段（后 5 根）从计算中剔除为 NaN
    column = close["2498.HK"]
    assert len(column) == 70
    kept = column.dropna()
    assert len(kept) == 65
    assert kept.iloc[-1] == 38.0 + (64 % 5) * 0.2
    fetch_quote.assert_called_once_with("02498")


def test_move_quant_behind_peer_frames_untouched(monkeypatch: Any) -> None:
    """落后分支（报价日不在原索引里）：帧一根不动、不造 NaN，今日涨跌
    单独列在判定里——修复第 1 轮"整列对齐成空"的破坏。"""
    _fix_today(monkeypatch, mq_guard)
    quote = {**QUOTE, "as_of": "2026-11-05 16:08"}
    fetch_quote = create_autospec(mq_guard.call_cn_hk_quote, return_value=quote)
    monkeypatch.setattr(mq_guard, "call_cn_hk_quote", fetch_quote)

    dates = _iso_dates(date(2026, 10, 26), 65)
    closes = [38.0 + (i % 5) * 0.2 for i in range(65)]
    adj, close, vol, symbols = _frames(dates, closes, [1_000_000.0] * 65)
    adj_before, close_before, vol_before = adj.copy(), close.copy(), vol.copy()

    price_sources, notes, verdicts = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=TODAY
    )
    assert adj.equals(adj_before)
    assert close.equals(close_before)
    assert vol.equals(vol_before)
    verdict = verdicts["2498.HK"]
    assert verdict["kind"] == "behind"
    assert verdict["valid_until"] == "2026-10-26"
    assert verdict["today"]["change_pct"] == 2.82
    assert "cn-hk(tencent)" in price_sources["2498.HK"]
    fetch_quote.assert_called_once_with("02498")


def test_move_quant_normal_series_never_flagged(monkeypatch: Any) -> None:
    _fix_today(monkeypatch, mq_guard)
    fetch_quote = create_autospec(mq_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(mq_guard, "call_cn_hk_quote", fetch_quote)

    dates = _iso_dates(TODAY, 70)
    closes = [38.0 + (i % 5) * 0.2 for i in range(70)]
    adj, close, vol, symbols = _frames(dates, closes, [1_000_000.0] * 70)

    price_sources, notes, verdicts = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=TODAY
    )
    assert price_sources == {}
    assert notes == []
    assert verdicts == {}
    fetch_quote.assert_not_called()


def test_move_quant_unavailable_backup_masks_and_reports_honestly(
    monkeypatch: Any,
) -> None:
    """备用源不可用：停滞段照样从计算中剔除（坏数据不因备用源缺席而变好），
    并如实标注行情不可信。"""
    _fix_today(monkeypatch, mq_guard)
    fetch_quote = create_autospec(mq_guard.call_cn_hk_quote, return_value=None)
    monkeypatch.setattr(mq_guard, "call_cn_hk_quote", fetch_quote)

    adj, close, vol, symbols = _stagnant_frames()
    price_sources, notes, verdicts = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=TODAY
    )
    assert price_sources == {"2498.HK": "yfinance(异常,备用源不可用)"}
    assert "备用源本次不可用" in notes[0]
    assert verdicts["2498.HK"]["today"] is None
    kept = close["2498.HK"].dropna()
    assert len(kept) == 65
    assert len(close) == 70


def test_move_quant_us_symbols_are_never_guarded(monkeypatch: Any) -> None:
    """美股不受影响：同样的停滞形态不触发 cn 守卫。"""
    _fix_today(monkeypatch, mq_guard)
    fetch_quote = create_autospec(mq_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(mq_guard, "call_cn_hk_quote", fetch_quote)

    dates = _iso_dates(TODAY, 70)
    closes = [38.0 + (i % 5) * 0.2 for i in range(65)] + [15.47] * 5
    adj, close, vol, _ = _frames(dates, closes, [1_000_000.0] * 65 + [0.0] * 5)
    symbols = ["HSAI", "QQQ"]  # 只看美股列

    price_sources, notes, verdicts = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=TODAY
    )
    assert price_sources == {}
    assert notes == []
    assert verdicts == {}
    fetch_quote.assert_not_called()


def test_compute_quant_result_carries_source_fields(monkeypatch: Any) -> None:
    """price_sources / price_source_notes / today_quote 进结果并进 warnings；
    基于历史的指标（多日涨跌、量比）只用停滞段之前的有效数据计算，同业缺失当前窗口不造假 0%。"""
    _fix_today(monkeypatch, mq_guard)
    fetch_quote = create_autospec(mq_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(mq_guard, "call_cn_hk_quote", fetch_quote)

    adj, close, vol, symbols = _stagnant_frames()
    dates = [d.strftime("%Y-%m-%d") for d in close.index]
    price_sources, notes, verdicts = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=TODAY
    )
    today_quote = {"ticker": "2498.HK", **verdicts["2498.HK"]["today"]}
    res = mq.compute_quant_from_frames(
        target="HSAI",
        peers=["2498.HK"],
        sector_etf="QQQ",
        adj_close_df=adj,
        close_df=close,
        volume_df=vol,
        window_str="1d",
        end_override=TODAY.isoformat(),
        peer_source="explicit",
        price_sources=price_sources,
        source_notes=notes,
        today_quote=today_quote,
    )
    assert res["price_sources"] == price_sources
    assert res["price_source_notes"] == notes
    assert res["today_quote"] == today_quote
    assert notes[0] in res["warnings"]
    # 停滞同业在最新日窗口无有效数据，不造 0% 收益混入同组中位数
    assert res["peers"] == []

    # 以港股自身为目标算 5d：多日涨跌与量比只用停滞段之前（截至 dates[64]）的有效数据
    res_hk = mq.compute_quant_from_frames(
        target="2498.HK",
        peers=["HSAI"],
        sector_etf="QQQ",
        adj_close_df=adj,
        close_df=close,
        volume_df=vol,
        window_str="5d",
        end_override=TODAY.isoformat(),
        peer_source="explicit",
        price_sources=price_sources,
        source_notes=notes,
        today_quote=today_quote,
    )
    assert res_hk["ok"] is True
    assert res_hk["end_date"] == dates[64]
    assert res_hk["volume_vs_adtv"] == 1.0
    assert res_hk["today_quote"] == today_quote


# ── price_reaction 集成 ─────────────────────────────────────────────


def _hk_bars_with_flat_tail(n_valid: int = 5, n_flat: int = 5) -> list[dict]:
    dates = _iso_dates(TODAY, n_valid + n_flat)
    return [
        {
            "date": d,
            "open": 15.0,
            "high": 15.5 if i < n_valid else 15.47,
            "low": 14.9 if i < n_valid else 15.47,
            "close": (38.0 + (i % 5) * 0.2) if i < n_valid else 15.47,
            "volume": 1_000_000 if i < n_valid else 0,
        }
        for i, d in enumerate(dates)
    ]


def _qqq_bars(n: int) -> list[dict]:
    return [
        {
            "date": d,
            "open": 500.0,
            "high": 505.0,
            "low": 498.0,
            "close": 500.0 + i,
            "volume": 1_000_000,
        }
        for i, d in enumerate(_iso_dates(TODAY, n))
    ]


def test_price_reaction_release_in_stagnant_tail_marks_window_incomplete(
    monkeypatch: Any,
) -> None:
    """财报日落在停滞段：bar 原样保留（不删、不合成），次日/一周窗口标
    不完整、不计算涨跌，52 周区间只用停滞前数据并注明截至某日。"""
    _fix_today(monkeypatch, pr_guard)
    hk_bars = _hk_bars_with_flat_tail()
    qqq_bars = _qqq_bars(10)
    fetch = create_autospec(
        pr.fetch_series_yfinance,
        side_effect=lambda sym: (hk_bars if sym == "2525.HK" else qqq_bars, {}),
    )
    monkeypatch.setattr(pr, "fetch_series_yfinance", fetch)
    fetch_quote = create_autospec(pr_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(pr_guard, "call_cn_hk_quote", fetch_quote)

    series_map, _ext, price_sources, notes, cn_flags = pr.load_series(["2525.HK", "QQQ"])

    # 日线一根不动：停滞尾还在 series_map 里，只做标注
    assert series_map["2525.HK"] == hk_bars
    assert "2498" not in str(price_sources)
    assert "2525.HK" in price_sources
    assert len(notes) == 1 and "起停在 15.47" in notes[0]
    assert cn_flags["2525.HK"]["stagnant_from"] == hk_bars[5]["date"]
    assert cn_flags["2525.HK"]["valid_until"] == hk_bars[4]["date"]
    # 美股不受影响
    assert series_map["QQQ"] == qqq_bars
    assert "QQQ" not in price_sources and "QQQ" not in cn_flags

    release = hk_bars[8]["date"]  # 落在停滞段里
    res = pr.analyze_reaction(
        ticker="2525.HK",
        release_date=release,
        timing="AMC",
        benchmarks=["QQQ"],
        sector_etf=None,
        series_map=series_map,
        ext_meta={},
        daily_flags=cn_flags,
    )
    assert res["next_day"]["complete"] is False
    assert res["next_day"]["change_pct"] is None
    assert "停滞段" in res["next_day"]["incomplete_reason"]
    assert res["one_week"]["complete"] is False
    assert res["one_week"]["change_pct"] is None
    # 停滞段的假开盘不进跳空计算
    assert res["post_market"]["reaction_open_gap_pct"] is None
    # 52 周只用停滞前数据、注明截至某日、不再标"真实 52 周"
    assert res["range_52w"]["stagnant_truncated"] is True
    assert res["range_52w"]["as_of_date"] == hk_bars[4]["date"]
    assert res["range_52w"]["window_trading_days"] == 5
    md = pr.render_markdown(res)
    assert "不完整" in md
    assert f"截至 {hk_bars[4]['date']}" in md
    assert "真实 52 周" not in md


def test_price_reaction_historical_as_of_never_calls_quote(monkeypatch: Any) -> None:
    """--as-of 历史回测：判定以 as_of 为准，不调备用源；as-of 之前数据
    正常时无任何标注。"""
    _fix_today(monkeypatch, pr_guard)
    hk_bars = _hk_bars_with_flat_tail()
    fetch = create_autospec(
        pr.fetch_series_yfinance, side_effect=lambda sym: (hk_bars, {})
    )
    monkeypatch.setattr(pr, "fetch_series_yfinance", fetch)
    fetch_quote = create_autospec(pr_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(pr_guard, "call_cn_hk_quote", fetch_quote)

    as_of = hk_bars[4]["date"]  # 停滞段开始（10-30）之前
    series_map, _ext, price_sources, notes, cn_flags = pr.load_series(
        ["2525.HK"], as_of=as_of
    )
    assert price_sources == {}
    assert notes == []
    assert cn_flags == {}
    fetch_quote.assert_not_called()
    assert series_map["2525.HK"] == hk_bars  # bar 原样

    # as-of 落在停滞段中间：只标注（不删 bar、不调备用源），窗口标不完整
    as_of_in_tail = hk_bars[7]["date"]
    series_map2, _ext2, price_sources2, notes2, cn_flags2 = pr.load_series(
        ["2525.HK"], as_of=as_of_in_tail
    )
    assert series_map2["2525.HK"] == hk_bars
    assert price_sources2 == {}
    assert len(notes2) == 1 and "只标注、不替换" in notes2[0]
    assert cn_flags2["2525.HK"]["kind"] == "stagnant"
    fetch_quote.assert_not_called()

    res = pr.analyze_reaction(
        ticker="2525.HK",
        release_date=hk_bars[5]["date"],  # 停滞段首日发布
        timing="AMC",
        benchmarks=[],
        sector_etf=None,
        series_map=series_map2,
        ext_meta={},
        as_of=as_of_in_tail,
        daily_flags=cn_flags2,
    )
    assert res["next_day"]["complete"] is False
    assert res["next_day"]["change_pct"] is None


def test_price_reaction_normal_series_unchanged(monkeypatch: Any) -> None:
    _fix_today(monkeypatch, pr_guard)
    dates = _iso_dates(TODAY, 10)
    hk_bars = [
        {
            "date": d,
            "open": 15.0,
            "high": 15.5,
            "low": 14.9,
            "close": 14.0 + i * 0.3,
            "volume": 1_000_000,
        }
        for i, d in enumerate(dates)
    ]
    fetch = create_autospec(
        pr.fetch_series_yfinance, side_effect=lambda sym: (hk_bars, {})
    )
    monkeypatch.setattr(pr, "fetch_series_yfinance", fetch)
    fetch_quote = create_autospec(pr_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(pr_guard, "call_cn_hk_quote", fetch_quote)

    series_map, _ext, price_sources, notes, cn_flags = pr.load_series(["2525.HK"])

    assert price_sources == {}
    assert notes == []
    assert cn_flags == {}
    assert series_map["2525.HK"] == hk_bars
    fetch_quote.assert_not_called()


# ── make_price_triggers 集成 ────────────────────────────────────────


def _flat_hk_download_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex(_iso_dates(TODAY, 60))
    closes = [30.0 + (i % 7) * 0.4 for i in range(55)] + [15.47] * 5
    return pd.DataFrame(
        {
            "Close": closes,
            "Adj Close": closes,
            "Volume": [1_000_000] * 55 + [0] * 5,
        },
        index=idx,
    )


def test_vol_threshold_falls_back_to_default_on_bad_hk_data(
    monkeypatch: Any,
) -> None:
    trig = _load("make_price_triggers_cn_fallback_under_test", _TRIGGERS_PATH)
    trig_guard = trig._cn_price_fallback()
    _fix_today(monkeypatch, trig_guard)
    fetch_quote = create_autospec(trig_guard.call_cn_hk_quote, return_value=dict(QUOTE))
    monkeypatch.setattr(trig_guard, "call_cn_hk_quote", fetch_quote)
    import yfinance as yf

    download = create_autospec(
        yf.download, return_value=_flat_hk_download_frame()
    )
    monkeypatch.setattr(yf, "download", download)

    thr, note = trig.compute_vol_threshold("02525.HK", 4.0)
    assert thr == 4.0
    assert note is not None
    assert "日线异常" in note
    assert "停在 15.47" in note


def test_vol_threshold_us_data_is_never_guarded(monkeypatch: Any) -> None:
    """美股不受影响：同样的帧不触发 cn 守卫（没有切换说明），走原有波动率逻辑。"""
    trig = _load("make_price_triggers_cn_fallback_us_under_test", _TRIGGERS_PATH)
    import yfinance as yf

    download = create_autospec(
        yf.download, return_value=_flat_hk_download_frame()
    )
    monkeypatch.setattr(yf, "download", download)

    thr, note = trig.compute_vol_threshold("HSAI", 4.0)
    assert note is None
    assert thr >= 4.0


# ── 第 3 轮审查回归（第 2–6 条） ─────────────────────────────────────────────


def test_post_holiday_pre_open_zero_volume_quote_without_flat_tail_is_not_suspended(
    monkeypatch: Any,
) -> None:
    """长假后第一天、开盘前：报价无成交且价格不变，但日线尾部没有 3 根零成交
    平盘——不判停牌，也不误报落后。"""
    _fix_today(monkeypatch, guard)
    dates = _iso_dates(date(2026, 10, 26), 5)
    closes = [14.0, 14.2, 14.6, 15.0, 15.47]
    vols = [1_000_000.0] * 5
    pre_open_quote = {
        "price": 15.47,
        "prev_close": 15.47,
        "open": 15.47,
        "volume": 0.0,
        "source": "tencent",
        "as_of": TODAY.isoformat(),
    }
    fetch_quote = create_autospec(
        guard.call_cn_hk_quote, return_value=pre_open_quote
    )
    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is None


def test_true_suspension_masks_tail_and_marks_earnings_t1_incomplete(
    monkeypatch: Any,
) -> None:
    """真实停牌（报价日期确认是当日交易日 + 尾部 >= 3 根零成交平盘 + 报价无成交
    且价格不变）：停牌段置为 NaN，窗口涨跌、量比、52 周只用停牌前数据，文案写
    '截至某日（停牌前）'、标签写'疑似停牌'；财报窗口落在停牌段时 T+1 complete=False、
    change_pct=None（绝不是 complete=True 的 0%）。"""
    _fix_today(monkeypatch, mq_guard, pr_guard)
    susp_quote = {
        "price": 15.47,
        "prev_close": 15.47,
        "open": 15.47,
        "volume": 0.0,
        "source": "tencent",
        "as_of": TODAY.isoformat(),
    }
    monkeypatch.setattr(
        mq_guard,
        "call_cn_hk_quote",
        create_autospec(mq_guard.call_cn_hk_quote, return_value=susp_quote),
    )
    monkeypatch.setattr(
        pr_guard,
        "call_cn_hk_quote",
        create_autospec(pr_guard.call_cn_hk_quote, return_value=susp_quote),
    )

    # 1) move_quant：停牌段被置为 NaN，计算截至停牌前
    adj, close, vol, symbols = _stagnant_frames()
    dates = [d.strftime("%Y-%m-%d") for d in close.index]
    price_sources, notes, verdicts = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=TODAY
    )
    assert price_sources["2498.HK"] == "yfinance(疑似停牌)"
    assert "疑似停牌" in notes[0]
    assert f"截至 {dates[64]}（停牌前）" in notes[0]
    assert len(close["2498.HK"].dropna()) == 65
    res_hk = mq.compute_quant_from_frames(
        target="2498.HK",
        peers=["HSAI"],
        sector_etf="QQQ",
        adj_close_df=adj,
        close_df=close,
        volume_df=vol,
        window_str="5d",
        end_override=TODAY.isoformat(),
        peer_source="explicit",
        price_sources=price_sources,
        source_notes=notes,
    )
    assert res_hk["ok"] is True
    assert res_hk["end_date"] == dates[64]
    assert res_hk["volume_vs_adtv"] == 1.0

    # 2) price_reaction：保留 stagnant_from，T+1 complete=False、change_pct=None
    hk_bars = _hk_bars_with_flat_tail()
    qqq_bars = _qqq_bars(10)
    monkeypatch.setattr(
        pr,
        "fetch_series_yfinance",
        create_autospec(
            pr.fetch_series_yfinance,
            side_effect=lambda sym: (hk_bars if sym == "2525.HK" else qqq_bars, {}),
        ),
    )
    series_map, _ext, pr_sources, pr_notes, cn_flags = pr.load_series(["2525.HK", "QQQ"])
    assert pr_sources["2525.HK"] == "yfinance(疑似停牌)"
    assert cn_flags["2525.HK"]["stagnant_from"] == hk_bars[5]["date"]
    res_pr = pr.analyze_reaction(
        ticker="2525.HK",
        release_date=hk_bars[6]["date"],
        timing="AMC",
        benchmarks=["QQQ"],
        sector_etf=None,
        series_map=series_map,
        ext_meta={},
        daily_flags=cn_flags,
    )
    assert res_pr["next_day"]["complete"] is False
    assert res_pr["next_day"]["change_pct"] is None
    assert "疑似停牌" in res_pr["next_day"]["incomplete_reason"]
    assert f"截至 {hk_bars[4]['date']}（停牌前）" in res_pr["next_day"]["incomplete_reason"]
    md = pr.render_markdown(res_pr)
    assert f"截至 {hk_bars[4]['date']}（停牌前）" in md


def test_incomplete_peer_window_excluded_in_1d_and_10d_queries(
    monkeypatch: Any,
) -> None:
    """codex 第 2 条的两个复现：同业有效数据截至 10-28，历史查询 11-04 时：
    (1) 单日窗口不能出现假 0% 或旧日量比 1.0，必须从同组中位数和超额收益剔除；
    (2) 10 日窗口不能把同业的 5 日收益（+3.14%）混进同组中位数与目标的 10 日收益相减；
    source_notes 写明被剔除的标的与有效数据截至日期。"""
    _fix_today(monkeypatch, mq_guard)
    # 70 个工作日，最后 5 个工作日（10-29..11-04）同业停滞，有效数据截至 10-28
    end_hist = date(2026, 11, 4)
    dates = _iso_dates(end_hist, 70)
    assert dates[64] == "2026-10-28"
    idx = pd.DatetimeIndex(dates)

    # 构造同行 2498.HK 在 10 日窗口的前 5 天（dates[59]..dates[64]）涨 +3.14%，后 5 天停在 15.47 量 0
    hk_closes = [35.0] * 60 + [35.2, 35.4, 35.6, 35.8, round(35.0 * 1.0314, 4)] + [15.47] * 5
    hk_vols = [1_000_000.0] * 65 + [0.0] * 5
    adj = pd.DataFrame(
        {
            "HSAI": [100.0 + i * 0.5 for i in range(70)],
            "GOOGL": [200.0 + i * 0.2 for i in range(70)],
            "QQQ": [500.0 + i * 0.1 for i in range(70)],
            "2498.HK": hk_closes,
        },
        index=idx,
        dtype=float,
    )
    vol = pd.DataFrame(
        {
            "HSAI": [1_000_000.0] * 70,
            "GOOGL": [2_000_000.0] * 70,
            "QQQ": [50_000_000.0] * 70,
            "2498.HK": hk_vols,
        },
        index=idx,
        dtype=float,
    )
    close = adj.copy()
    symbols = ["HSAI", "GOOGL", "QQQ", "2498.HK"]

    # 历史查询（end=11-04 < TODAY=11-05）：price_sources 为空
    price_sources, notes, _ = mq._annotate_cn_daily_issues(
        symbols, adj, close, vol, end=end_hist
    )
    assert price_sources == {}

    # 复现 1：查询 11-04 单日窗口（即便调用方不传 price_sources）
    notes_1d = list(notes)
    res_1d = mq.compute_quant_from_frames(
        target="HSAI",
        peers=["2498.HK"],
        sector_etf="QQQ",
        adj_close_df=adj,
        close_df=close,
        volume_df=vol,
        window_str="1d",
        end_override="2026-11-04",
        source_notes=notes_1d,
    )
    assert res_1d["peers"] == []
    assert res_1d["peers_used"] == []
    assert res_1d["peer_median"] is None
    assert res_1d["alpha_vs_peers"] is None
    assert any("2498.HK" in n and "2026-10-28" in n and "剔除" in n for n in notes_1d)

    # 复现 2：查询 10 日窗口，2498.HK 的 5 日收益（+3.14%）不得混入同组中位数
    notes_10d: list[str] = []
    res_10d = mq.compute_quant_from_frames(
        target="HSAI",
        peers=["GOOGL", "2498.HK"],
        sector_etf="QQQ",
        adj_close_df=adj,
        close_df=close,
        volume_df=vol,
        window_str="10d",
        end_override="2026-11-04",
        source_notes=notes_10d,
    )
    assert res_10d["peers_used"] == ["GOOGL"]
    assert [r["ticker"] for r in res_10d["peers"]] == ["GOOGL"]
    assert res_10d["peer_median"] == res_10d["peers"][0]["change_pct"]
    assert any("2498.HK" in n and "2026-10-28" in n and "剔除" in n for n in notes_10d)


def test_price_reaction_benchmark_in_stagnant_tail_sets_relative_pct_none(
    monkeypatch: Any,
) -> None:
    """codex 第 3 条复现：目标行情正常（上涨 +0.97%），港股基准从 103 错停到
    15.47（零成交停滞段）——该基准的 relative_pct 必须为 None 并注明原因，
    绝不能算出 +85.95 个百分点的假相对收益。"""
    _fix_today(monkeypatch, pr_guard)
    dates = _iso_dates(TODAY, 10)
    target_bars = [
        {
            "date": d,
            "open": 100.0,
            "high": 101.5,
            "low": 99.5,
            "close": 100.0 if i < 6 else 100.97,
            "volume": 1_000_000,
        }
        for i, d in enumerate(dates)
    ]
    bench_hk_bars = [
        {
            "date": d,
            "open": 103.0 if i < 6 else 15.47,
            "high": 103.5 if i < 6 else 15.47,
            "low": 102.5 if i < 6 else 15.47,
            "close": 103.0 if i < 6 else 15.47,
            "volume": 1_000_000 if i < 6 else 0,
        }
        for i, d in enumerate(dates)
    ]
    monkeypatch.setattr(
        pr,
        "fetch_series_yfinance",
        create_autospec(
            pr.fetch_series_yfinance,
            side_effect=lambda sym: (
                target_bars if sym == "HSAI" else bench_hk_bars,
                {},
            ),
        ),
    )
    monkeypatch.setattr(
        pr_guard,
        "call_cn_hk_quote",
        create_autospec(pr_guard.call_cn_hk_quote, return_value=dict(QUOTE)),
    )

    series_map, _ext, _sources, _notes, cn_flags = pr.load_series(["HSAI", "2525.HK"])
    res = pr.analyze_reaction(
        ticker="HSAI",
        release_date=dates[5],  # T+1 为 dates[6]（基准已进入 15.47 停滞段）
        timing="AMC",
        benchmarks=["2525.HK"],
        sector_etf=None,
        series_map=series_map,
        ext_meta={},
        daily_flags=cn_flags,
    )
    assert res["next_day"]["complete"] is True
    assert res["next_day"]["change_pct"] == 0.97
    rel_nd = res["next_day"]["relative"]["2525.HK"]
    assert rel_nd["benchmark_change_pct"] is None
    assert rel_nd["relative_pct"] is None
    assert "停滞段" in rel_nd["incomplete_reason"]
    rel_ow = res["one_week"]["relative"]["2525.HK"]
    assert rel_ow["benchmark_change_pct"] is None
    assert rel_ow["relative_pct"] is None
    assert "停滞段" in rel_ow["incomplete_reason"]


def test_stale_or_missing_quote_date_is_not_labeled_as_today_change(
    monkeypatch: Any,
) -> None:
    """codex 第 4 条复现：请求截止 11-05，备用报价日期为 10-30（或缺失）时，
    今日字段标'无法确认当日行情'，不给数字（绝不输出今日 +10%）。"""
    _fix_today(monkeypatch, guard)
    dates, closes, vols = _stagnant_series()
    stale_quote = {
        "price": 44.0,
        "prev_close": 40.0,
        "open": 40.5,
        "volume": 1_200_000.0,
        "source": "tencent",
        "as_of": "2026-10-30 16:08",
    }
    verdict_stale = guard.evaluate_cn_daily(
        "2525.HK",
        dates,
        closes,
        vols,
        fetch_quote=create_autospec(guard.call_cn_hk_quote, return_value=stale_quote),
    )
    assert verdict_stale is not None
    assert "无法确认当日行情" in verdict_stale["today"]["error"]
    assert "change_pct" not in verdict_stale["today"]
    assert "price" not in verdict_stale["today"]
    assert "无法确认当日行情" in verdict_stale["note"]

    missing_date_quote = {**stale_quote, "as_of": None}
    verdict_missing = guard.evaluate_cn_daily(
        "2525.HK",
        dates,
        closes,
        vols,
        fetch_quote=create_autospec(
            guard.call_cn_hk_quote, return_value=missing_date_quote
        ),
    )
    assert verdict_missing is not None
    assert "无法确认当日行情" in verdict_missing["today"]["error"]
    assert "change_pct" not in verdict_missing["today"]
    assert "price" not in verdict_missing["today"]


def test_backup_unavailable_on_lagging_clean_series_returns_unconfirmed_not_behind(
    monkeypatch: Any,
) -> None:
    """10-07 查询截至 09-30 的正常日线、备用源失败时：没有日期确认，不判'落后'，
    返回'无法确认'，日线照常使用。"""
    _fix_today(monkeypatch, guard, mq_guard, today=date(2026, 10, 7))
    dates = _iso_dates(date(2026, 9, 30), 65)
    closes = [20.0 + (i % 5) * 0.3 for i in range(65)]
    vols = [1_000_000.0] * 65
    fetch_none = create_autospec(guard.call_cn_hk_quote, return_value=None)

    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_none
    )
    assert verdict is not None
    assert verdict["kind"] == "unconfirmed"
    assert "无法确认" in verdict["note"]
    assert "落后" not in verdict["note"]
    assert "不可信" not in verdict["note"]
    assert verdict["stagnant_from"] is None
    assert verdict["valid_until"] == "2026-09-30"


def test_vol_threshold_not_lowered_on_four_day_holiday_or_suspension(
    monkeypatch: Any,
) -> None:
    """连续休市四天（或备用源不可用、或真实停牌）时，高波动港股的波动率阈值
    继续用最后一段正常数据算出，不退回默认 4.0%；停牌写'疑似停牌'。"""
    trig = _load("make_price_triggers_cn_holiday_under_test", _TRIGGERS_PATH)
    trig_guard = trig._cn_price_fallback()
    _fix_today(monkeypatch, trig_guard, today=date(2026, 10, 7))
    import yfinance as yf

    # 构造截至 09-30 的 55 根高波动正常日线（日涨跌约 ±3.5%，2σ > 6%）
    dates_holiday = _iso_dates(date(2026, 9, 30), 55)
    closes_holiday: list[float] = [30.0]
    for i in range(1, 55):
        mult = 1.035 if i % 2 == 1 else 0.965
        closes_holiday.append(round(closes_holiday[-1] * mult, 4))
    df_holiday = pd.DataFrame(
        {
            "Close": closes_holiday,
            "Adj Close": closes_holiday,
            "Volume": [1_000_000.0] * 55,
        },
        index=pd.DatetimeIndex(dates_holiday),
    )
    monkeypatch.setattr(
        yf,
        "download",
        create_autospec(yf.download, return_value=df_holiday),
    )

    # 1) 连续假期：备用报价仍是 09-30，阈值不降
    holiday_quote = {
        "price": closes_holiday[-1],
        "prev_close": closes_holiday[-2],
        "open": closes_holiday[-2],
        "volume": 1_000_000.0,
        "source": "tencent",
        "as_of": "2026-09-30 16:08",
    }
    monkeypatch.setattr(
        trig_guard,
        "call_cn_hk_quote",
        create_autospec(trig_guard.call_cn_hk_quote, return_value=holiday_quote),
    )
    thr_hol, note_hol = trig.compute_vol_threshold("02525.HK", 4.0)
    assert thr_hol > 6.0
    assert note_hol is None

    # 2) 10-07 备用源失败（无法确认是否落后）：阈值同样不降
    trig_guard.call_cn_hk_quote.return_value = None
    thr_unc, note_unc = trig.compute_vol_threshold("02525.HK", 4.0)
    assert thr_unc == thr_hol
    assert note_unc is None

    # 3) 真实停牌：尾部 4 根零成交平盘 + 当日无量平盘报价 → 用停牌前正常数据算阈值，标注"疑似停牌"
    trig_guard.hk_today.return_value = TODAY
    dates_susp = _iso_dates(TODAY, 59)
    closes_susp = closes_holiday + [closes_holiday[-1]] * 4
    df_susp = pd.DataFrame(
        {
            "Close": closes_susp,
            "Adj Close": closes_susp,
            "Volume": [1_000_000.0] * 55 + [0.0] * 4,
        },
        index=pd.DatetimeIndex(dates_susp),
    )
    yf.download.return_value = df_susp
    susp_quote = {
        "price": closes_holiday[-1],
        "prev_close": closes_holiday[-1],
        "open": closes_holiday[-1],
        "volume": 0.0,
        "source": "tencent",
        "as_of": TODAY.isoformat(),
    }
    trig_guard.call_cn_hk_quote.return_value = susp_quote
    thr_susp, note_susp = trig.compute_vol_threshold("02525.HK", 4.0)
    assert thr_susp == thr_hol
    assert note_susp is not None
    assert "疑似停牌" in note_susp


# ── 第 4 轮审查回归（第 3 条） ─────────────────────────────────────────────


def test_quote_with_missing_volume_is_not_judged_suspended(
    monkeypatch: Any,
) -> None:
    """codex 第 3 条复现：日线尾部四根停在 15.47、量为零；当日报价现价与前收
    均为 40，但成交量缺失（None）——不能算'确认无成交'，不判停牌，继续走停滞判定。"""
    _fix_today(monkeypatch, guard)
    dates, closes, vols = _stagnant_series(n_flat=4, n_valid=5)
    quote_missing_vol = {
        "price": 40.0,
        "prev_close": 40.0,
        "open": 40.0,
        "volume": None,
        "source": "tencent",
        "as_of": TODAY.isoformat(),
    }
    fetch_quote = create_autospec(
        guard.call_cn_hk_quote, return_value=quote_missing_vol
    )
    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["kind"] == "stagnant"
    assert verdict["today"] is not None
    assert verdict["today"]["price"] == 40.0
    assert verdict["today"]["prev_close"] == 40.0
    assert verdict["today"]["change_pct"] == 0.0


def test_weekend_query_with_friday_quote_does_not_output_today_change(
    monkeypatch: Any,
) -> None:
    """codex 第 4 条复现：香港日期为周六 2026-11-07，存在停滞尾，报价日期为周五
    2026-11-06、现价 44、前收 40——周末不回退到最近工作日，今日字段写'无法确认
    当日行情'，不给数字（绝不输出今日 +10%）。"""
    saturday = date(2026, 11, 7)
    _fix_today(monkeypatch, guard, today=saturday)
    dates = _iso_dates(date(2026, 11, 6), 9)
    closes = [38.0, 38.2, 38.4, 38.6, 38.8] + [15.47] * 4
    vols = [1_000_000.0] * 5 + [0.0] * 4
    friday_quote = {
        "price": 44.0,
        "prev_close": 40.0,
        "open": 40.5,
        "volume": 1_200_000.0,
        "source": "tencent",
        "as_of": "2026-11-06 16:08",
    }
    fetch_quote = create_autospec(
        guard.call_cn_hk_quote, return_value=friday_quote
    )
    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates, closes, vols, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["kind"] == "stagnant"
    assert "无法确认当日行情" in verdict["today"]["error"]
    assert "change_pct" not in verdict["today"]
    assert "price" not in verdict["today"]
    assert "无法确认当日行情" in verdict["note"]


def test_weekend_suspension_judged_against_friday_session_and_preserves_vol_threshold(
    monkeypatch: Any,
) -> None:
    """Round 6 item 3 (grok 中): 周六（2026-11-07）查询、日线尾部零成交平盘、周五
    （2026-11-06）报价无成交且价格不变——停牌三条件的交易日核对回到周五，判为
    'suspended'（today 为 None），make_price_triggers 用停牌前数据算波动率阈值并写
    '疑似停牌'。"""
    saturday = date(2026, 11, 7)
    trig = _load("make_price_triggers_cn_weekend_susp_under_test", _TRIGGERS_PATH)
    trig_guard = trig._cn_price_fallback()
    _fix_today(monkeypatch, guard, trig_guard, today=saturday)
    import yfinance as yf

    closes_normal: list[float] = [30.0]
    for i in range(1, 55):
        mult = 1.035 if i % 2 == 1 else 0.965
        closes_normal.append(round(closes_normal[-1] * mult, 4))

    dates_all = _iso_dates(date(2026, 11, 6), 59)
    closes_all = closes_normal + [closes_normal[-1]] * 4
    vols_all = [1_000_000.0] * 55 + [0.0] * 4

    friday_susp_quote = {
        "price": closes_normal[-1],
        "prev_close": closes_normal[-1],
        "open": closes_normal[-1],
        "volume": 0.0,
        "source": "tencent",
        "as_of": "2026-11-06 16:08",
    }
    fetch_quote = create_autospec(
        guard.call_cn_hk_quote, return_value=friday_susp_quote
    )
    verdict = guard.evaluate_cn_daily(
        "2525.HK", dates_all, closes_all, vols_all, fetch_quote=fetch_quote
    )
    assert verdict is not None
    assert verdict["kind"] == "suspended"
    assert verdict["today"] is None
    assert "疑似停牌" in verdict["note"]

    df_susp = pd.DataFrame(
        {
            "Close": closes_all,
            "Adj Close": closes_all,
            "Volume": vols_all,
        },
        index=pd.DatetimeIndex(dates_all),
    )
    monkeypatch.setattr(
        yf, "download", create_autospec(yf.download, return_value=df_susp)
    )
    monkeypatch.setattr(
        trig_guard,
        "call_cn_hk_quote",
        create_autospec(
            trig_guard.call_cn_hk_quote, return_value=friday_susp_quote
        ),
    )
    thr, note = trig.compute_vol_threshold("02525.HK", 4.0)
    assert thr > 6.0
    assert note is not None
    assert "疑似停牌" in note



