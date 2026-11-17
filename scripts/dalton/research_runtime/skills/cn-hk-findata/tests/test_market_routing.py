"""Market isolation regression: never turn HK 00700 into A-share 000700."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import financemcp_adapter as adapter


class MarketRoutingTests(unittest.TestCase):
    def setUp(self):
        self.names = patch.object(adapter, "_load_name_cache", return_value={
            "腾讯": "00700", "茅台": "600519",
        })
        self.names.start()
        self.addCleanup(self.names.stop)
        self.calls = []

    def fetch(self, entry, question, ticker):
        self.calls.append((entry["id"], ticker))
        kwargs = adapter._apply_resolver_map(
            entry.get("args", {}), adapter._build_resolver_map(question, ticker))
        return pd.DataFrame([{"security": ticker}]), {
            "function": entry["function"], "source": "mock", "kwargs": kwargs,
        }

    def ask(self, query):
        with patch.object(adapter, "_run_registry_entry", side_effect=self.fetch):
            return adapter.run_ask_akshare(query)

    def test_explicit_quote_uses_existing_hk_provider(self):
        with patch.object(adapter, "_fallback_quote", return_value={"ok": True}) as quote:
            result = adapter.run_ask_akshare("腾讯 00700 当前股价")
        self.assertTrue(result["ok"])
        quote.assert_called_once_with("00700", market="hk")

    def test_current_quote_word_does_not_override_financial_intent(self):
        with patch.object(adapter, "_fallback_quote") as quote:
            self.ask("腾讯当前股价与财务指标")
        quote.assert_not_called()

    def test_tencent_financial_indicator_keeps_hk_identity(self):
        result = self.ask("腾讯财务指标")
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["module"], "financial_hk_indicator")
        self.assertEqual(result["plan"]["kwargs"]["symbol"], "00700")

    def test_hk_roe_and_summary(self):
        for query in ("00700 ROE", "腾讯财务摘要", "港股00700财务指标"):
            with self.subTest(query=query):
                self.assertEqual(self.ask(query)["plan"]["module"], "financial_hk_indicator")

    def test_a_share_indicators_remain_a_share(self):
        for query, ticker in (("000700财务指标", "000700"), ("茅台财务指标", "600519")):
            with self.subTest(query=query):
                result = self.ask(query)
                self.assertEqual(result["plan"]["module"], "financial_indicator")
                self.assertEqual(result["plan"]["kwargs"]["symbol"], ticker)

    def test_combo_keeps_every_result_in_hk(self):
        result = self.ask("腾讯财务指标以及资产负债表")
        self.assertEqual(result["mode"], "combo")
        modules = {row["module"] for row in result["results"]}
        self.assertIn("financial_hk_indicator", modules)
        self.assertIn("financial_hk_balance", modules)
        self.assertTrue(all(module.startswith("financial_hk_") and ticker == "00700"
                            for module, ticker in self.calls))

    def test_us_financials_never_fall_into_generic_a_share_router(self):
        with patch.object(adapter, "_route_via_score") as generic:
            result = self.ask("AAPL 财务指标")
        self.assertFalse(result["ok"])
        self.assertEqual(result["market"], "us")
        self.assertEqual(self.calls, [])
        generic.assert_not_called()

    def test_failed_hk_interface_does_not_switch_market(self):
        attempted = []

        def fail(entry, question, ticker):
            attempted.append(entry)
            raise RuntimeError("fixture: source unavailable")

        with patch.object(adapter, "_run_registry_entry", side_effect=fail), \
                patch.object(adapter, "_route_via_score") as generic:
            result = adapter.run_ask_akshare("腾讯财务指标")
        self.assertFalse(result["ok"])
        self.assertTrue(attempted)
        self.assertTrue(all(adapter._is_market_compatible(entry, "00700") for entry in attempted))
        generic.assert_not_called()

    def test_market_is_checked_before_candidate_limit(self):
        wrong = {"id": "a", "function": "a", "keywords": ["财务指标"],
                 "args": {"symbol": "{ticker6}"}}
        right = {"id": "hk", "function": "hk", "keywords": ["财务指标"],
                 "ticker_format": "hk5"}
        with patch.object(adapter, "_load_registry", return_value=[wrong, right]):
            candidates = adapter._match_multi_intents("财务指标", max_intents=1, ticker="00700")
        self.assertEqual(candidates[0][0]["id"], "hk")

    def test_executor_rejects_incompatible_entry_before_fetch(self):
        entry = {"id": "research_report", "function": "stock_research_report_em",
                 "args": {"symbol": "{ticker6}"}}
        with patch.object(adapter, "_retry_call") as fetch:
            with self.assertRaisesRegex(ValueError, "市场不匹配"):
                adapter._run_registry_entry(entry, "腾讯研报", "00700")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
