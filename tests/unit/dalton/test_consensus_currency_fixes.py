"""一致预期币种修正（researchdata 1.5）的本地计算链锁定测试。

覆盖两个绕过上游修正的脚本：
1. `dalton-bullbear/scripts/implied_model.py`：仅 EPS 与收入币种都确认为 USD 的期间计算
   美元隐含模型；零值与缺失分开；收入单位按字段契约换算（原始值为报表币种元）；
   上游 `warnings` / `field_status` 原样透传。
2. `dalton-weekly-review/scripts/attribution.py`：EPS 提取保留币种；同币种直算 P/E，
   不同币种经汇率换算并注明汇率与日期，拿不到汇率写“币种不一致，未计算”不硬算；
   跨期两端 EPS 币种不一致时不做跨期归因；yfinance 回退路径同样带币种。
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any
from unittest.mock import call, create_autospec, patch

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[3]
IMPLIED_MODEL_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear" / "scripts" / "implied_model.py"
)
ATTRIBUTION_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-weekly-review" / "scripts" / "attribution.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


implied = _load_module("dalton_implied_model", IMPLIED_MODEL_PATH)
attribution = _load_module("dalton_attribution", ATTRIBUTION_PATH)


def _fx_provider_spec(from_cur: str, to_cur: str, on_date: str) -> dict[str, Any] | None:
    """仅作 create_autospec 的签名来源，不会被真正调用。"""
    raise AssertionError("spec only")


def _estimates_snapshot(
    eps_cur: str | None,
    rev_cur: str | None,
    eps0: float | None = 25.5,
    eps1: float | None = 28.0,
    rev0_raw: float | None = 205_000_000_000.0,
    rev1_raw: float | None = 230_000_000_000.0,
    rev0_bn: float | None = None,
) -> dict[str, Any]:
    """构造 researchdata.finance(route="estimates") 快照的核心预期字段。"""
    return {
        "ticker": "TEST",
        "earnings_estimate": {
            "0y": ({"avg": eps0, "currency": eps_cur} if eps_cur is not None else {"avg": eps0}) if eps0 is not None else {},
            "+1y": {"avg": eps1, "currency": eps_cur} if eps_cur is not None else {"avg": eps1},
        },
        "revenue_estimate": {
            "0y": (
                ({"avg": rev0_raw, "avg_bn": rev0_bn, "currency": rev_cur} if rev_cur is not None
                 else {"avg": rev0_raw, "avg_bn": rev0_bn})
                if rev0_bn is not None
                else ({"avg": rev0_raw, "currency": rev_cur} if rev_cur is not None else {"avg": rev0_raw})
            ) if rev0_raw is not None or rev0_bn is not None else {},
            "+1y": {"avg": rev1_raw, "currency": rev_cur} if rev_cur is not None else {"avg": rev1_raw},
        },
    }


class TestImpliedModelCurrency:
    def test_usd_eps_and_revenue_runs_dollar_model(self):
        cons = _estimates_snapshot(eps_cur="USD", rev_cur="USD")
        out = implied.implied_model(cons, shares=2.55, tax=0.15)

        assert out["0y"]["currency"] == "USD"
        assert out["0y"]["consensus_revenue_bn"] == 205.0
        assert out["0y"]["consensus_eps"] == 25.5
        # implied_pretax = eps * shares / (1 - tax) = 25.5 * 2.55 / 0.85 = 76.5
        assert out["0y"]["implied_pretax_bn"] == 76.5
        assert out["+1y"]["implied_pretax_bn"] == round(28.0 * 2.55 / 0.85, 2)
        assert out["warnings"] == []

    def test_cny_period_skips_dollar_model_and_keeps_native_values(self):
        # 0y 为 CNY（BABA 形态）：不出美元数字、写明原因、保留原币种数值；+1y 为 USD 照常计算
        cons = _estimates_snapshot(eps_cur="CNY", rev_cur="CNY", rev0_raw=900_000_000_000.0, eps0=60.0)
        cons["earnings_estimate"]["+1y"] = {"avg": 28.0, "currency": "USD"}
        cons["revenue_estimate"]["+1y"] = {"avg": 230_000_000_000.0, "currency": "USD"}

        out = implied.implied_model(cons, shares=2.55, tax=0.15)

        assert "implied_pretax_bn" not in out["0y"]
        assert out["0y"]["consensus_revenue_bn"] == 900.0
        assert out["0y"]["consensus_eps"] == 60.0
        assert out["0y"]["currency"] == {"eps": "CNY", "revenue": "CNY"}
        assert "币种为 CNY/CNY，未做美元隐含测算" in out["0y"]["note"]
        assert any("0y" in w and "未做美元隐含测算" in w for w in out["warnings"])
        assert out["+1y"]["currency"] == "USD"
        assert out["+1y"]["implied_pretax_bn"] == round(28.0 * 2.55 / 0.85, 2)

    def test_missing_currency_treated_as_unknown_and_skipped(self):
        cons = _estimates_snapshot(eps_cur=None, rev_cur=None)

        out = implied.implied_model(cons, shares=2.55, tax=0.15)

        for per in ("0y", "+1y"):
            assert "implied_pretax_bn" not in out[per]
            assert out[per]["currency"] == {"eps": "未知", "revenue": "未知"}
            assert "未知" in out[per]["note"]
        assert any("未知" in w for w in out["warnings"])

    def test_zero_eps_computed_not_treated_as_missing(self):
        cons = _estimates_snapshot(eps_cur="USD", rev_cur="USD", eps0=0.0)

        out = implied.implied_model(cons, shares=2.55, tax=0.15)

        assert out["0y"]["consensus_eps"] == 0.0
        assert out["0y"]["implied_pretax_bn"] == 0.0
        # 隐含总费用 = 收入 − 税前 + 其他收入 = 205 − 0 + 0
        assert out["0y"]["implied_total_expenses_bn"] == 205.0

    def test_none_eps_skips_period(self):
        cons = _estimates_snapshot(eps_cur="USD", rev_cur="USD", eps0=None)

        out = implied.implied_model(cons, shares=2.55, tax=0.15)

        assert "0y" not in out
        assert "+1y" in out

    def test_revenue_bn_field_used_directly_without_magnitude_guess(self):
        # 上游已给 avg_bn=900.0（十亿）时直接用，不再除 1e9，也不按数值大小猜单位
        cons = _estimates_snapshot(eps_cur="CNY", rev_cur="CNY", rev0_raw=None, rev0_bn=900.0, eps0=60.0)

        out = implied.implied_model(cons, shares=2.55, tax=0.15)

        assert out["0y"]["consensus_revenue_bn"] == 900.0

    def test_upstream_field_status_and_warnings_passed_through(self):
        cons = _estimates_snapshot(eps_cur="USD", rev_cur="USD")
        cons["field_status"] = {
            "earnings_estimate": "ok", "revenue_estimate": "ok", "eps_trend": "empty",
            "eps_revisions": "error", "price_targets": "ok", "recommendations": "empty",
            "earnings_dates": "ok", "price": "ok",
        }
        cons["implied_model"] = {"warnings": ["0y EPS/收入币种为 ['CNY', 'CNY']，未确认同为 USD，跳过美元隐含模型"]}

        out = implied.implied_model(cons, shares=2.55, tax=0.15)

        assert out["field_status"]["eps_revisions"] == "error"
        assert out["upstream_warnings"] == ["0y EPS/收入币种为 ['CNY', 'CNY']，未确认同为 USD，跳过美元隐含模型"]

    def test_render_md_labels_currency_and_skipped_periods(self):
        cons = _estimates_snapshot(eps_cur="USD", rev_cur="USD")
        out = implied.implied_model(cons, shares=2.55, tax=0.15)
        md = implied.render_md(out, ticker="TEST")
        # 各期数字按“十亿 <币种>”标注；假设行的“其他收入”只进美元模型，须带专用限定
        assert "一致收入 205.0 十亿USD" in md
        assert "隐含总费用空间 **128.5 十亿USD**" in md
        assert "费用每多 100 亿USD" in md
        assert "十亿美元（美元模型专用）" in md

        cons_cny = _estimates_snapshot(eps_cur="CNY", rev_cur="CNY", eps0=60.0, rev0_raw=900_000_000_000.0)
        out_cny = implied.implied_model(cons_cny, shares=2.55, tax=0.15)
        md_cny = implied.render_md(out_cny, ticker="TEST")
        assert "未做美元隐含测算" in md_cny
        assert "900.0 十亿CNY" in md_cny
        assert "模型缺口" in md_cny


class TestAttributionCurrency:
    def _sample_kwargs(self, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "ticker": "META",
            "p_base": 100.0,
            "p_end": 110.0,
            "eps_0y_base": 10.0,
            "eps_1y_base": 10.0,
            "eps_0y_end": 10.9,
            "eps_1y_end": 10.9,
            "base_date": "2026-10-01",
            "end_date": "2026-10-08",
            "eps_currencies_base": "USD",
            "eps_currencies_end": "USD",
            "price_currency": "USD",
        }
        kwargs.update(overrides)
        return kwargs

    def test_usd_price_usd_eps_computes_pe_directly(self):
        row = attribution.decompose_price_move(**self._sample_kwargs())

        ntm_base, _ = attribution.compute_ntm_eps(10.0, 10.0, "2026-10-01")
        ntm_end, _ = attribution.compute_ntm_eps(10.9, 10.9, "2026-10-08")
        assert row["status"] == "ok"
        assert row["price_currency"] == "USD"
        assert row["price_currency_inferred"] is False
        assert row["pe_base"] == round(100.0 / ntm_base, 2)
        assert row["pe_end"] == round(110.0 / ntm_end, 2)
        assert row["fx"] is None

    def test_cny_eps_with_fx_converted_and_rate_noted(self):
        fx_rec = {"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot CNY/USD"}
        provider = create_autospec(_fx_provider_spec, return_value=fx_rec)

        row = attribution.decompose_price_move(
            **self._sample_kwargs(
                ticker="BABA",
                eps_currencies_base="CNY",
                eps_currencies_end="CNY",
                fx_rate_provider=provider,
            )
        )

        # 两端各按自己日期查一次汇率
        assert provider.call_count == 2
        provider.assert_any_call("CNY", "USD", "2026-10-01")
        provider.assert_any_call("CNY", "USD", "2026-10-08")
        assert row["status"] == "ok"
        ntm_base, _ = attribution.compute_ntm_eps(10.0, 10.0, "2026-10-01")
        ntm_end, _ = attribution.compute_ntm_eps(10.9, 10.9, "2026-10-08")
        assert row["pe_base"] == round(100.0 / (ntm_base * 0.14), 2)
        assert row["pe_end"] == round(110.0 / (ntm_end * 0.14), 2)
        assert row["fx"] == {"base": fx_rec, "end": fx_rec}
        assert "0.14" in row["multiple_explanation"]
        assert "汇率变动计入盈利贡献" in row["multiple_explanation"]

    def test_per_end_fx_rates_with_exchange_move_in_earnings(self):
        # 两端 EPS 均 10 CNY、价格均 100 USD、起点汇率 0.14（CNY→USD 方向）、终点 0.15：
        # 起点 P/E 71.43、终点 66.67；价格贡献 0，盈利贡献（含汇率变动）与倍数贡献相互抵消
        provider = create_autospec(
            _fx_provider_spec,
            side_effect=[
                {"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot CNY/USD"},
                {"rate": 0.15, "as_of": "2026-10-08", "source": "snapshot CNY/USD"},
            ],
        )

        row = attribution.decompose_price_move(
            ticker="BABA",
            p_base=100.0,
            p_end=100.0,
            eps_0y_base=10.0,
            eps_1y_base=10.0,
            eps_0y_end=10.0,
            eps_1y_end=10.0,
            base_date="2026-10-01",
            end_date="2026-10-08",
            eps_currencies_base="CNY",
            eps_currencies_end="CNY",
            price_currency="USD",
            fx_rate_provider=provider,
        )

        assert provider.call_args_list == [
            call("CNY", "USD", "2026-10-01"),
            call("CNY", "USD", "2026-10-08"),
        ]
        assert row["status"] == "ok"
        assert row["pe_base"] == 71.43
        assert row["pe_end"] == 66.67
        assert row["log_return_pp"] == 0.0
        assert row["fx"]["base"]["rate"] == 0.14 and row["fx"]["end"]["rate"] == 0.15
        assert row["eps_contrib_pp"] + row["pe_contrib_pp"] == 0.0
        assert abs(row["eps_contrib_pp"] - round(math.log(0.15 / 0.14) * 100.0, 2)) < 0.01
        assert "0.14" in row["multiple_explanation"] and "0.15" in row["multiple_explanation"]
        assert "汇率变动计入盈利贡献" in row["multiple_explanation"]

    def test_cny_eps_without_fx_not_computed_and_explained(self):
        provider = create_autospec(_fx_provider_spec, return_value=None)

        row = attribution.decompose_price_move(
            **self._sample_kwargs(
                ticker="BABA",
                eps_currencies_base="CNY",
                eps_currencies_end="CNY",
                fx_rate_provider=provider,
            )
        )

        assert provider.call_count == 2
        provider.assert_any_call("CNY", "USD", "2026-10-01")
        provider.assert_any_call("CNY", "USD", "2026-10-08")
        assert row["status"] == "currency_mismatch"
        assert row["pe_base"] is None and row["pe_end"] is None
        assert row["pe_contrib_pp"] is None
        # EPS 修正本身币种一致（CNY/CNY），原币种口径的贡献仍可给出
        assert row["eps_contrib_pp"] is not None
        assert "币种不一致" in row["multiple_explanation"]
        assert "缺起点与终点汇率" in row["multiple_explanation"]
        assert "未计算倍数" in row["multiple_explanation"]

    def test_fx_missing_one_side_reports_which(self):
        provider = create_autospec(
            _fx_provider_spec,
            side_effect=[{"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot CNY/USD"}, None],
        )

        row = attribution.decompose_price_move(
            **self._sample_kwargs(
                ticker="BABA",
                eps_currencies_base="CNY",
                eps_currencies_end="CNY",
                fx_rate_provider=provider,
            )
        )

        assert row["status"] == "currency_mismatch"
        assert row["pe_base"] is None and row["pe_end"] is None
        assert "缺终点汇率" in row["multiple_explanation"]
        assert "未计算倍数" in row["multiple_explanation"]
        assert row["eps_contrib_pp"] is not None

    def test_partially_unknown_currency_not_borrowed(self):
        # (None, "USD")：0y 与 +1y 都有值，0y 币种未知 -> 不能借用 +1y 的 USD
        row = attribution.decompose_price_move(**self._sample_kwargs(eps_currencies_end=(None, "USD")))
        assert row["status"] == "currency_mismatch"
        assert row["pe_base"] is None and row["pe_end"] is None
        assert "EPS 币种未知" in row["multiple_explanation"]

        # 起点币种全部缺失、终点为 USD -> 同样不借用
        row2 = attribution.decompose_price_move(**self._sample_kwargs(eps_currencies_base=None))
        assert row2["status"] == "currency_mismatch"
        assert row2["pe_base"] is None and row2["pe_end"] is None
        assert "EPS 币种未知" in row2["multiple_explanation"]

    def test_unknown_eps_currency_not_computed(self):
        row = attribution.decompose_price_move(**self._sample_kwargs(eps_currencies_base=None, eps_currencies_end=None))

        assert row["status"] == "currency_mismatch"
        assert row["pe_end"] is None
        assert "未知" in row["multiple_explanation"]

    def test_cross_period_currency_mismatch_refuses_attribution(self):
        row = attribution.decompose_price_move(
            **self._sample_kwargs(eps_currencies_base="CNY", eps_currencies_end="USD")
        )

        assert row["status"] == "currency_mismatch"
        assert row["pe_base"] is None and row["pe_end"] is None
        assert row["eps_contrib_pp"] is None and row["pe_contrib_pp"] is None
        assert "跨期 EPS 币种不一致" in row["multiple_explanation"]
        assert "起点 CNY、终点 USD" in row["multiple_explanation"]

    def test_within_end_period_currency_conflict_refused(self):
        row = attribution.decompose_price_move(
            **self._sample_kwargs(eps_currencies_end=("CNY", "USD"))
        )

        assert row["status"] == "currency_mismatch"
        assert "期间币种不一致" in row["multiple_explanation"]

    def test_inferred_price_currency_marked(self):
        # 未提供价格币种：按交易所推断（0700.HK→HKD），输出标注“推断”
        row = attribution.decompose_price_move(**self._sample_kwargs(ticker="0700.HK", price_currency=None))

        assert row["price_currency"] == "HKD"
        assert row["price_currency_inferred"] is True

    def test_infer_price_currency_by_exchange(self):
        assert attribution.infer_price_currency("0700.HK") == "HKD"
        assert attribution.infer_price_currency("600519.SS") == "CNY"
        assert attribution.infer_price_currency("000001.SZ") == "CNY"
        assert attribution.infer_price_currency("BABA") == "USD"

    def test_markdown_table_renders_currency_column_and_mismatch(self):
        ok_row = attribution.decompose_price_move(**self._sample_kwargs())
        provider = create_autospec(_fx_provider_spec, return_value=None)
        bad_row = attribution.decompose_price_move(
            **self._sample_kwargs(
                ticker="BABA",
                eps_currencies_base="CNY",
                eps_currencies_end="CNY",
                fx_rate_provider=provider,
            )
        )

        table = attribution.render_markdown_table([ok_row, bad_row])

        assert "币种（价格/EPS）" in table
        assert "USD/USD" in table
        assert "币种不一致，未计算" in table
        assert "USD/CNY" in table


class TestAttributionExtractionAndFallback:
    def test_extract_eps_from_snapshot_carries_currency(self):
        snap = {
            "earnings_estimate": {
                "0y": {"avg": 60.0, "currency": "CNY"},
                "+1y": {"avg": 66.0, "currency": "CNY"},
            },
        }
        e0, e1, cur0, cur1 = attribution.extract_eps_from_snapshot(snap)
        assert (e0, e1, cur0, cur1) == (60.0, 66.0, "CNY", "CNY")

    def test_extract_eps_falls_back_to_eps_trend_with_currency(self):
        snap = {
            "eps_trend": {
                "0y": {"current": 8.5, "7daysAgo": 8.4, "currency": "CNY"},
                "+1y": {"current": 9.3, "7daysAgo": 9.2, "currency": "CNY"},
            },
        }
        e0, e1, cur0, cur1 = attribution.extract_eps_from_snapshot(snap)
        assert (e0, e1, cur0, cur1) == (8.5, 9.3, "CNY", "CNY")

        b0, b1, bcur0, bcur1 = attribution.extract_7d_ago_eps(snap)
        assert (b0, b1, bcur0, bcur1) == (8.4, 9.2, "CNY", "CNY")

    def test_yfinance_fallback_path_carries_currency(self):
        # 回退路径（直接读 yfinance eps_trend）同样要带上币种
        et_df = pd.DataFrame(
            {
                "7daysAgo": [59.0, 64.0],
                "current": [60.0, 66.0],
                "currency": ["CNY", "CNY"],
            },
            index=["0y", "+1y"],
        )
        ticker_mock = create_autospec(yf.Ticker, instance=True)
        ticker_mock.eps_trend = et_df

        with patch("yfinance.Ticker", autospec=True) as ticker_cls:
            ticker_cls.return_value = ticker_mock
            win = attribution.load_snapshots_for_window("BABA", None, "2026-10-01", "2026-10-08")

        assert win["source"] == "近似（7daysAgo）"
        assert (win["e0_base"], win["e1_base"]) == (59.0, 64.0)
        assert (win["e0_end"], win["e1_end"]) == (60.0, 66.0)
        assert (win["cur0_base"], win["cur1_base"], win["cur0_end"], win["cur1_end"]) == ("CNY", "CNY", "CNY", "CNY")

    def test_workspace_snapshots_carry_currencies_and_fx(self, tmp_path: Path):
        base_snap = {
            "fetched_at": "2026-10-01T09:00:00",
            "earnings_estimate": {"0y": {"avg": 60.0, "currency": "CNY"}, "+1y": {"avg": 66.0, "currency": "CNY"}},
        }
        end_snap = {
            "fetched_at": "2026-10-08T09:00:00",
            "earnings_estimate": {"0y": {"avg": 61.8, "currency": "CNY"}, "+1y": {"avg": 67.8, "currency": "CNY"}},
            "fx": {"CNY/USD": {"rate": 0.15, "as_of": "2026-10-08"}},
        }
        cdir = tmp_path / "data" / "consensus"
        cdir.mkdir(parents=True)
        (cdir / "2026-10-01_0900.json").write_text(json.dumps(base_snap), encoding="utf-8")
        (cdir / "2026-10-08_0900.json").write_text(json.dumps(end_snap), encoding="utf-8")

        win = attribution.load_snapshots_for_window("BABA", tmp_path, "2026-10-01", "2026-10-08")

        assert win["source"] == "snapshot"
        assert win["e0_base"] == 60.0 and win["e0_end"] == 61.8
        assert win["cur0_base"] == "CNY" and win["cur0_end"] == "CNY"
        # fx 合并为规范化记录列表；无日期的裸汇率不保留
        assert win["fx"] == [{"pair": "CNY/USD", "rate": 0.15, "as_of": "2026-10-08"}]

    def test_merged_snapshot_fx_keeps_both_dates_for_both_ends(self, tmp_path: Path):
        # 两份快照各带自己日期的 CNY/USD 汇率：合并不互相覆盖，行情不可用时倍数照常算且用到两个汇率
        base_snap = {
            "fetched_at": "2026-10-01T09:00:00",
            "earnings_estimate": {"0y": {"avg": 10.0, "currency": "CNY"}, "+1y": {"avg": 10.0, "currency": "CNY"}},
            "fx": {"CNY/USD": {"rate": 0.14, "as_of": "2026-10-01"}},
        }
        end_snap = {
            "fetched_at": "2026-10-08T09:00:00",
            "earnings_estimate": {"0y": {"avg": 10.0, "currency": "CNY"}, "+1y": {"avg": 10.0, "currency": "CNY"}},
            "fx": {"CNY/USD": {"rate": 0.15, "as_of": "2026-10-08"}},
        }
        cdir = tmp_path / "data" / "consensus"
        cdir.mkdir(parents=True)
        (cdir / "2026-10-01_0900.json").write_text(json.dumps(base_snap), encoding="utf-8")
        (cdir / "2026-10-08_0900.json").write_text(json.dumps(end_snap), encoding="utf-8")

        win = attribution.load_snapshots_for_window("BABA", tmp_path, "2026-10-01", "2026-10-08")
        assert win["source"] == "snapshot"
        assert win["fx"] == [
            {"pair": "CNY/USD", "rate": 0.14, "as_of": "2026-10-01"},
            {"pair": "CNY/USD", "rate": 0.15, "as_of": "2026-10-08"},
        ]

        with patch.object(attribution, "fx_from_quote", autospec=True, return_value=None):
            row = attribution.decompose_price_move(
                ticker="BABA",
                p_base=100.0,
                p_end=100.0,
                eps_0y_base=win["e0_base"],
                eps_1y_base=win["e1_base"],
                eps_0y_end=win["e0_end"],
                eps_1y_end=win["e1_end"],
                base_date="2026-10-01",
                end_date="2026-10-08",
                base_eps_source=win["source"],
                eps_currencies_base=(win["cur0_base"], win["cur1_base"]),
                eps_currencies_end=(win["cur0_end"], win["cur1_end"]),
                price_currency="USD",
                fx_rate_provider=lambda f, to, on, snap_fx=win["fx"]: attribution.resolve_fx_rate(
                    f, to, on, snapshot_fx=snap_fx
                ),
            )

        assert row["status"] == "ok"
        assert row["fx"]["base"] == {"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot"}
        assert row["fx"]["end"] == {"rate": 0.15, "as_of": "2026-10-08", "source": "snapshot"}
        assert row["pe_base"] == 71.43
        assert row["pe_end"] == 66.67
        assert "0.14" in row["multiple_explanation"] and "0.15" in row["multiple_explanation"]

    def test_fx_from_snapshot_requires_dated_records(self):
        # 无日期的汇率不可靠，不用于换算（跳过它，留给行情按日查）
        assert attribution.fx_from_snapshot({"USD/CNY": 7.14}, "CNY", "USD", "2026-10-01") is None
        assert attribution.fx_from_snapshot({"pair": "CNY/USD", "rate": 0.14}, "CNY", "USD", "2026-10-01") is None

        # 带日期：取 on_date 当日（或之前最近一条）；晚于 on_date 的不用
        rec = attribution.fx_from_snapshot(
            {"CNY/USD": {"rate": 0.14, "as_of": "2026-10-01"}}, "CNY", "USD", "2026-10-05"
        )
        assert rec == {"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot"}
        assert attribution.fx_from_snapshot(
            {"CNY/USD": {"rate": 0.14, "as_of": "2026-10-08"}}, "CNY", "USD", "2026-10-01"
        ) is None

        # 按日期多条：两端各取各自日期（或之前最近）的汇率
        series = {"CNY/USD": {"2026-10-01": 0.14, "2026-10-08": 0.15}}
        assert attribution.fx_from_snapshot(series, "CNY", "USD", "2026-10-08")["rate"] == 0.15
        assert attribution.fx_from_snapshot(series, "CNY", "USD", "2026-10-05")["rate"] == 0.14

        # 反 pair 自动取倒数（pair 记法 A/B 表示 1 A = rate B）
        inv = attribution.fx_from_snapshot({"USD/CNY": {"2026-10-01": 7.14}}, "CNY", "USD", "2026-10-01")
        assert inv is not None and abs(inv["rate"] - 1.0 / 7.14) < 1e-12

        assert attribution.fx_from_snapshot({"EUR/USD": {"2026-10-01": 1.08}}, "CNY", "USD", "2026-10-01") is None

    def test_fx_snapshot_invalid_and_timestamp_dates(self):
        # 非法日期（2026-02-30）视为无日期：跳过它，继续查可用记录/行情
        assert attribution.fx_from_snapshot(
            {"CNY/USD": {"rate": 0.14, "as_of": "2026-02-30"}}, "CNY", "USD", "2026-10-01"
        ) is None
        mixed = {"CNY/USD": {"2026-02-30": 0.13, "2026-10-01": 0.14}}
        hit = attribution.fx_from_snapshot(mixed, "CNY", "USD", "2026-10-08")
        assert hit == {"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot"}

        # 带时间的 ISO 时间戳按其日期部分比较：目标日 2026-10-01 可用
        ts = attribution.fx_from_snapshot(
            {"CNY/USD": {"rate": 0.14, "as_of": "2026-10-01T09:00:00"}}, "CNY", "USD", "2026-10-01"
        )
        assert ts == {"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot"}
        # 时间戳的日期部分晚于目标日时不可用
        assert attribution.fx_from_snapshot(
            {"CNY/USD": {"rate": 0.15, "as_of": "2026-10-08T09:00:00"}}, "CNY", "USD", "2026-10-01"
        ) is None

    def test_fx_from_quote_uses_dated_history_close(self):
        hist = pd.DataFrame(
            {"Close": [0.139, 0.140, 0.141]},
            index=pd.to_datetime(["2026-09-30", "2026-10-01", "2026-10-02"]),
        )
        ticker_mock = create_autospec(yf.Ticker, instance=True)
        ticker_mock.history.return_value = hist

        with patch("yfinance.Ticker", autospec=True) as ticker_cls:
            ticker_cls.return_value = ticker_mock
            hit = attribution.fx_from_quote("CNY", "USD", "2026-10-01")

        # 取 on_date 当日（或之前最近一个交易日）那根 K 线，并记录其实际日期
        assert hit == {"rate": 0.14, "as_of": "2026-10-01", "source": "yfinance CNYUSD=X"}

    def test_fx_from_quote_empty_history_does_not_fall_back_to_last_price(self):
        ticker_mock = create_autospec(yf.Ticker, instance=True)
        ticker_mock.history.return_value = pd.DataFrame()
        ticker_mock.fast_info = {"last_price": 0.14}  # 有最新价但没有可靠日期，不能冒充按日汇率

        with patch("yfinance.Ticker", autospec=True) as ticker_cls:
            ticker_cls.return_value = ticker_mock
            assert attribution.fx_from_quote("CNY", "USD", "2026-10-01") is None

    def test_resolve_fx_rate_prefers_dated_snapshot_then_quote_by_date(self):
        with patch.object(attribution, "fx_from_quote", autospec=True) as quote:
            hit = attribution.resolve_fx_rate(
                "CNY", "USD", "2026-10-01", snapshot_fx={"CNY/USD": {"2026-10-01": 0.14}}
            )
        assert hit == {"rate": 0.14, "as_of": "2026-10-01", "source": "snapshot"}
        quote.assert_not_called()

        # 快照汇率无日期不可用 -> 转行情按各自日期查
        with patch.object(
            attribution,
            "fx_from_quote",
            autospec=True,
            return_value={"rate": 0.14, "as_of": "2026-10-01", "source": "yfinance CNYUSD=X"},
        ) as quote2:
            hit2 = attribution.resolve_fx_rate("CNY", "USD", "2026-10-01", snapshot_fx={"USD/CNY": 7.14})
        quote2.assert_called_once_with("CNY", "USD", "2026-10-01")
        assert hit2["source"] == "yfinance CNYUSD=X"

    def test_undated_snapshot_and_no_quote_fx_yields_no_multiple(self):
        with patch.object(attribution, "fx_from_quote", autospec=True, return_value=None):

            def provider(from_cur: str, to_cur: str, on_date: str) -> dict[str, Any] | None:
                return attribution.resolve_fx_rate(from_cur, to_cur, on_date, snapshot_fx={"USD/CNY": 7.14})

            row = attribution.decompose_price_move(
                ticker="BABA",
                p_base=100.0,
                p_end=110.0,
                eps_0y_base=10.0,
                eps_1y_base=10.0,
                eps_0y_end=10.9,
                eps_1y_end=10.9,
                base_date="2026-10-01",
                end_date="2026-10-08",
                eps_currencies_base="CNY",
                eps_currencies_end="CNY",
                price_currency="USD",
                fx_rate_provider=provider,
            )

        assert row["status"] == "currency_mismatch"
        assert row["pe_base"] is None and row["pe_end"] is None
        assert "汇率" in row["multiple_explanation"]


class TestSingleCliHint:
    def test_single_cli_without_eps_currency_prints_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "attribution.py", "single", "--ticker", "TEST",
                "--p-base", "100", "--p-end", "110",
                "--eps0-base", "10", "--eps1-base", "10",
                "--eps0-end", "10.9", "--eps1-end", "10.9",
            ],
        )

        rc = attribution.main()

        out = capsys.readouterr().out
        assert rc == 0
        assert "未提供 EPS 币种" in out
        assert "--eps-currency USD" in out
        assert "未计算 P/E" in out
