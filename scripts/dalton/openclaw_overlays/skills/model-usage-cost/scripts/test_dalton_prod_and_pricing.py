"""Tests for Dalton prices.json, -oauth matching, dalton-prod, agy_enum_names, and state dir."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import create_autospec, patch

import model_usage_cost_cli as cli
import usage_sources as s


class TestDaltonProdAndPricing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_effective_from_date_selects_argon_price_by_record_timestamp(self):
        prices = cli.load_prices()
        ts_nov30 = dt.datetime(2026, 11, 30, 23, 59, tzinfo=s.UTC).timestamp()
        ts_dec01 = dt.datetime(2026, 12, 1, 0, 0, tzinfo=s.UTC).timestamp()

        v = (1_000_000, 0, 0, 0)
        c_nov = cli.cost_of(prices, "google", "gemini-4-argon", v, {"unit": "model-invocation"}, ts=ts_nov30)
        c_dec = cli.cost_of(prices, "google", "gemini-4-argon", v, {"unit": "model-invocation"}, ts=ts_dec01)
        self.assertAlmostEqual(c_nov, 2.0)
        self.assertAlmostEqual(c_dec, 4.0)

        out_v = (0, 0, 0, 1_000_000)
        self.assertAlmostEqual(
            cli.cost_of(prices, "antigravity-cli-gateway", "gemini-4-carbon", out_v, {"unit": "model-response"}, ts=ts_nov30),
            10.0,
        )
        self.assertAlmostEqual(
            cli.cost_of(prices, "antigravity-cli-gateway", "gemini-4-carbon", out_v, {"unit": "model-response"}, ts=ts_dec01),
            20.0,
        )

    def test_oauth_and_thinking_suffix_matching(self):
        prices = cli.load_prices()
        ts_nov30 = dt.datetime(2026, 11, 25, 12, 0, tzinfo=s.UTC).timestamp()

        self.assertEqual(
            cli.model_price_candidates("gemini-4-argon-high-oauth"),
            ["gemini-4-argon-high-oauth", "gemini-4-argon-high", "gemini-4-argon"],
        )
        pr = cli.price_for(
            prices,
            "google",
            "gemini-4-argon-high-oauth",
            ts=ts_nov30,
            strip_oauth=True,
        )
        self.assertIsNotNone(pr)
        self.assertAlmostEqual(pr["input"], 2.0)

        pr_med = cli.price_for(
            prices,
            "claude-cli-gateway",
            "claude-opus-5-5-medium-oauth",
            ts=ts_nov30,
            strip_oauth=True,
        )
        self.assertIsNotNone(pr_med)
        self.assertAlmostEqual(pr_med["input"], 4.0)

        cost = cli.cost_of(
            prices,
            "google",
            "gemini-4-argon-low-oauth",
            (1_000_000, 0, 0, 0),
            {"unit": "model-invocation", "strip_oauth": True},
            ts=ts_nov30,
        )
        self.assertAlmostEqual(cost, 2.0)

    def test_no_public_price_excluded_from_total_cost_with_reference_only(self):
        prices = cli.load_prices()
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=s.UTC).timestamp()
        e = s.Evidence(now - 86400, now)
        e.put("muse", "r1", now - 100, "muse-cli-gateway", "muse-wonder-full-1.0", "muse-cli(手动)", (500_000, 0, 500_000, 100_000), unit="model-response")
        e.put("openclaw", "r2", now - 100, "openai", "gpt-6-luna", "openclaw", (1_000_000, 0, 0, 0), unit="model-response")

        w = cli.window_result(list(e.records.values()), now, 1, prices)
        by_m = {r["model"]: r for r in w["rows"]}
        muse_row = by_m["muse-wonder-full-1.0"]
        self.assertIsNone(muse_row["cost_usd"])
        self.assertTrue(muse_row["no_public_price"])
        self.assertEqual(muse_row["unpriced_tokens"], 1_100_000)
        self.assertEqual(muse_row["reference_model"], "muse-wonder-1.0")
        self.assertIsNotNone(muse_row["reference_cost_usd"])
        self.assertGreater(muse_row["reference_cost_usd"], 0.0)
        # Reference cost must NEVER enter priced_cost_usd
        luna_cost = by_m["gpt-6-luna"]["cost_usd"]
        self.assertIsNotNone(luna_cost)
        self.assertAlmostEqual(w["totals"]["priced_cost_usd"], luna_cost)
        self.assertEqual(w["totals"]["unpriced_tokens"], 1_100_000)

    def test_grok_min_prompt_tokens_tier_is_loaded(self):
        prices = cli.load_prices()
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=s.UTC).timestamp()
        low = cli.cost_of(prices, "xai", "grok-4.7-build", (100_000, 0, 0, 0), {"unit": "model-response"}, ts=now)
        high = cli.cost_of(prices, "xai", "grok-4.7-build", (250_000, 0, 0, 0), {"unit": "model-response"}, ts=now)
        self.assertAlmostEqual(low, 0.4)
        self.assertAlmostEqual(high, 1.5)

    def test_dalton_prod_failure_reports_error_not_zero(self):
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=s.UTC).timestamp()
        e = s.Evidence(now - 7 * 86400, now)
        err = urllib.error.URLError("connection refused")
        mock_fetch = create_autospec(s.fetch_dalton_prod_by_model, side_effect=err)
        with patch.dict(os.environ, {"DALTON_BACKEND_URL": "http://127.0.0.1:8000", "INTERNAL_SERVICE_TOKEN": "tok-secret"}), \
             patch.object(s, "fetch_dalton_prod_by_model", mock_fetch):
            ok = s.scan_dalton_prod(e, window_days=7.0, window_since=now - 7 * 86400, window_until=now)
        self.assertFalse(ok)
        self.assertIsNotNone(e.dalton_prod)
        self.assertFalse(e.dalton_prod["ok"])
        self.assertIn("connection refused", e.dalton_prod["error"])
        self.assertNotIn("tok-secret", e.dalton_prod["error"])
        self.assertEqual(e.records, {})

    def test_dalton_prod_records_do_not_dedup_against_local_gateway(self):
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=s.UTC).timestamp()
        e = s.Evidence(now - 7 * 86400, now)
        # Add a local gateway observation with identical model/token count
        e.put("gateway", "gw1", now - 3600, "claude-cli-gateway", "claude-opus-5-5", "openclaw", (1000, 0, 500, 200), request="r1", start=now - 3605)
        mock_fetch = create_autospec(
            s.fetch_dalton_prod_by_model,
            return_value={
                "models": [
                    {
                        "model": "claude-opus-5-5-oauth",
                        "calls": 3,
                        "fresh_input": 1000,
                        "cache_read": 500,
                        "cache_write": 0,
                        "output": 200,
                        "total_tokens": 1700,
                        "runs": 2,
                    }
                ],
                "totals": {"calls": 3, "runs": 2, "fresh_input": 1000, "cache_read": 500, "cache_write": 0, "output": 200, "total_tokens": 1700},
            },
        )
        with patch.dict(os.environ, {"DALTON_BACKEND_URL": "http://127.0.0.1:8000", "INTERNAL_SERVICE_TOKEN": "tok"}), \
             patch.object(s, "fetch_dalton_prod_by_model", mock_fetch):
            ok = s.scan_dalton_prod(e, window_days=7.0, window_since=now - 7 * 86400, window_until=now)
        self.assertTrue(ok)
        records, held = s.reconcile(e)
        self.assertFalse(held)
        self.assertEqual(len(records), 2)
        self.assertEqual(s.summed(records), (2000, 0, 1000, 400))

    def test_agy_enum_names_file_resolves_and_prices_record(self):
        enum_file = self.tmp / "agy_enum_names.json"
        enum_file.write_text(json.dumps({"1417": "gemini-4-argon"}), encoding="utf-8")
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=s.UTC).timestamp()
        e = s.Evidence(now - 86400, now)
        e.put("agy", "resp1", now - 10, "antigravity-cli-gateway", "agy-enum-1417", "agy-cli(含网关)", (1_000_000, 0, 0, 0), conversation="c1", enum=1417, unit="model-response")

        with patch.dict(os.environ, {"DALTON_AGY_ENUM_NAMES_FILE": str(enum_file)}):
            records, held = s.reconcile(e)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["model"], "gemini-4-argon")
        self.assertNotIn("agy_model_unresolved", e.issues)
        prices = cli.load_prices()
        w = cli.window_result(records, now, 1, prices)
        self.assertAlmostEqual(w["totals"]["priced_cost_usd"], 2.0)

    def test_bundled_enum_1417_and_d_variant_merge_into_one_argon_row(self):
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=s.UTC).timestamp()
        e = s.Evidence(now - 86400, now)
        e.put("agy", "r1", now - 30, "antigravity-cli-gateway", "gemini-4-argon", "agy-cli(含网关)", (1_000_000, 0, 0, 0), conversation="c1", enum=1417, unit="model-response")
        e.put("agy", "r2", now - 20, "antigravity-cli-gateway", "agy-enum-1417", "agy-cli(含网关)", (1_000_000, 0, 0, 0), conversation="c2", enum=1417, unit="model-response")
        e.put("agy", "r3", now - 10, "antigravity-cli-gateway", "gemini-4-argon-d", "agy-cli(含网关)", (1_000_000, 0, 0, 0), conversation="c3", enum=1417, unit="model-response")
        e.put("gateway", "gw1", now - 5, "antigravity-cli-gateway", "gemini-4-argon-d", "agy-cli(含网关)", (1_000_000, 0, 0, 0), unit="model-request")

        records, held = s.reconcile(e)
        self.assertEqual([r["model"] for r in records], ["gemini-4-argon"] * 4)
        self.assertNotIn("agy_model_unresolved", e.issues)
        prices = cli.load_prices()
        w = cli.window_result(records, now, 1, prices, by_model=True)
        self.assertEqual(len(w["rows"]), 1)
        self.assertEqual(w["rows"][0]["model"], "gemini-4-argon")
        self.assertEqual(w["rows"][0]["tokens"], 4_000_000)
        self.assertAlmostEqual(w["rows"][0]["cost_usd"], 8.0)
        self.assertEqual(w["totals"]["unpriced_tokens"], 0)

    def test_no_openclaw_json_required_and_state_dir_configurable(self):
        fake_home = self.tmp / "empty-home"
        fake_home.mkdir()
        custom_state = self.tmp / "custom-state"
        with patch.dict(
            os.environ,
            {
                "HOME": str(fake_home),
                "DALTON_USAGE_STATE_DIR": str(custom_state),
                "DALTON_BACKEND_URL": "",
                "INTERNAL_SERVICE_TOKEN": "",
            },
        ):
            self.assertEqual(cli.default_state_dir(), custom_state)
            self.assertEqual(Path(cli.default_ledger_path()), custom_state / "evidence.sqlite")
            out_buf = io.StringIO()
            with patch("sys.argv", ["model_usage_cost_cli.py", "--json", "summary", "--days", "1", "--no-ledger"]), \
                 patch("sys.stdout", out_buf):
                rc = cli.main()
            self.assertEqual(rc, 0)
            payload = json.loads(out_buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["dalton_prod"]["ok"])

    def test_dalton_prod_daily_buckets_split_across_price_effective_date(self):
        # Weekly cron at Saturday 12:00 HKT = 04:00 UTC crosses 2026-12-01.
        since_ts = dt.datetime(2026, 11, 28, 4, 0, tzinfo=s.UTC).timestamp()
        until_ts = dt.datetime(2026, 12, 5, 4, 0, tzinfo=s.UTC).timestamp()
        e = s.Evidence(since_ts, until_ts)
        mock_fetch = create_autospec(
            s.fetch_dalton_prod_by_model,
            return_value={
                "bucket": "day",
                "timezone": "UTC",
                "models": [
                    {
                        "date": "2026-11-30",
                        "model": "gemini-4-argon-high-oauth",
                        "calls": 10,
                        "fresh_input": 1_000_000,
                        "cache_read": 0,
                        "cache_write": 0,
                        "output": 0,
                        "total_tokens": 1_000_000,
                        "runs": 5,
                    },
                    {
                        "date": "2026-12-01",
                        "model": "gemini-4-argon-high-oauth",
                        "calls": 12,
                        "fresh_input": 1_000_000,
                        "cache_read": 0,
                        "cache_write": 0,
                        "output": 0,
                        "total_tokens": 1_000_000,
                        "runs": 6,
                    },
                ],
                "totals": {"calls": 22, "runs": 10, "fresh_input": 2_000_000, "cache_read": 0, "cache_write": 0, "output": 0, "total_tokens": 2_000_000},
            },
        )
        with patch.dict(os.environ, {"DALTON_BACKEND_URL": "http://127.0.0.1:8000", "INTERNAL_SERVICE_TOKEN": "tok"}), \
             patch.object(s, "fetch_dalton_prod_by_model", mock_fetch):
            ok = s.scan_dalton_prod(e, window_days=7.0, window_since=since_ts, window_until=until_ts)
        self.assertTrue(ok)
        self.assertEqual(mock_fetch.call_args.kwargs["bucket"], "day")
        records, held = s.reconcile(e)
        self.assertFalse(held)
        self.assertEqual(len(records), 2)
        prices = cli.load_prices()
        w = cli.window_result(records, until_ts, 7, prices, by_model=True)
        # 1M on 2026-11-30 ($2) + 1M on 2026-12-01 ($4) = $6
        self.assertAlmostEqual(w["totals"]["priced_cost_usd"], 6.0)
        self.assertEqual(w["totals"]["records"], 2)
        self.assertEqual(w["totals"]["calls"], 22)
        self.assertEqual(len(w["rows"]), 1)
        self.assertAlmostEqual(w["rows"][0]["cost_usd"], 6.0)

    def test_by_model_keeps_local_and_dalton_prod_rows_separate(self):
        now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=s.UTC).timestamp()
        e = s.Evidence(now - 7 * 86400, now)
        e.put("claude", "local1", now - 60, "anthropic", "claude-opus-5-5", "openclaw", (100, 0, 0, 0))
        e.put("dalton-prod", "dp1", now - 30, "anthropic", "claude-opus-5-5", s.DALTON_PROD_CHANNEL, (1000, 0, 0, 0), calls=15, runs=4, dalton_prod=True, strip_oauth=True)
        records, _ = s.reconcile(e)
        prices = cli.load_prices()
        w = cli.window_result(records, now, 7, prices, by_model=True)
        self.assertEqual(len(w["rows"]), 2)
        by_origin = {r["dalton_prod"]: r for r in w["rows"]}
        self.assertEqual(by_origin[False]["tokens"], 100)
        self.assertEqual(by_origin[False]["source"], "openclaw")
        self.assertEqual(by_origin[True]["tokens"], 1000)
        self.assertEqual(by_origin[True]["source"], s.DALTON_PROD_CHANNEL)
        self.assertEqual(w["totals"]["tokens"], 1100)
        self.assertEqual(w["totals"]["calls"], 16)


if __name__ == "__main__":
    unittest.main()
