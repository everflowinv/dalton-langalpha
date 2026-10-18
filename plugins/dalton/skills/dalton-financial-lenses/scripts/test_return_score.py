#!/usr/bin/env python3
"""Regression unit tests for return_score.py (PS.2)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import return_score as rs  # noqa: E402


class TestReturnScore(unittest.TestCase):
    def test_ps2_reference_sample(self) -> None:
        """Exact PS.2 line 463 regression test:
        ttm_div=4.73, ttm_buyback=6.70, true_return_share=0.78,
        annual=[11.24, 11.88, 15.05], fwd_total=14.61, credibility=90,
        adjust=[(+1, 'FCFF覆盖率1.8x'), (-1, '回购主要对冲SBC')]
        -> R3 ~ 13.15, driver R3, final_score 13.1
        """
        res = rs.compute_return_score(
            ttm_div=4.73,
            ttm_buyback=6.70,
            true_return_share=0.78,
            annual=[11.24, 11.88, 15.05],
            fwd_total=14.61,
            credibility=90,
            adjustments=[(1.0, "FCFF覆盖率1.8x"), (-1.0, "回购主要对冲SBC")],
        )
        self.assertAlmostEqual(res["R1"], 9.96, places=2)
        self.assertAlmostEqual(res["R2"], 12.72, places=2)
        self.assertAlmostEqual(res["R3"], 13.15, places=2)
        self.assertEqual(res["driver"], "R3")
        self.assertAlmostEqual(res["base_score"], 13.15, places=2)
        self.assertEqual(res["adjustment"], 0.0)
        self.assertEqual(res["final_score"], 13.1)

    def test_decimal_adjustment_rationale_formatting(self) -> None:
        """Ensure +0.5 adjustment is formatted as +0.5 (not +0) in rationale_zh."""
        res = rs.compute_return_score(
            ttm_div=3.5,
            ttm_buyback=2.0,
            true_return_share=0.80,
            annual=[4.0, 4.5, 5.0],
            fwd_total=5.0,
            credibility=75,
            adjustments=[(0.5, "半年度分红提升")],
        )
        self.assertIn("+0.5（半年度分红提升）", res["rationale_zh"])
        self.assertIn("可持续性调整 +0.5", res["rationale_zh"])

    def test_adjustment_clamping_and_cap_at_20(self) -> None:
        res = rs.compute_return_score(
            ttm_div=15.0,
            ttm_buyback=6.0,
            true_return_share=1.0,
            annual=[10.0, 12.0, 14.0],
            fwd_total=18.0,
            credibility=100,
            adjustments=[(1.5, "净现金充裕"), (1.5, "大额特别分红")],
        )
        self.assertEqual(res["driver"], "R1")
        self.assertEqual(res["base_score"], 20.0)
        self.assertEqual(res["adjustment"], 2.0)
        self.assertTrue(res["adjustment_clamped"])
        self.assertEqual(res["final_score"], 20.0)


if __name__ == "__main__":
    unittest.main()
