#!/usr/bin/env python3
"""G6 Real-Run Verification Script — Financial Lenses Unified `--anchors` (P0I §6, §7 G6, L37).

Runs `anchors.py`, `netcash.py --anchors`, `return_score.py --anchors`, and
`earnings_quality.py --anchors` for `BABA`, `0700.HK`, and `META` per `SKILL.md`,
asserting:
1. Identical `market_cap_usd_mn`, `fx_rate_to_usd`, and `period_end` across all lenses for each ticker.
2. `anchors_source == "anchors"` on all lens outputs.
3. Passing both `--anchors` and manual `--market-cap-usd-mn` or `--fx-rate` exits with a non-zero error code.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (
    build_evidence_envelope,
    new_test_user_id,
    now_utc_iso,
    write_evidence_and_summary,
)

LENSES_SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-financial-lenses" / "scripts"
)
LENSES_OUT_DIR = REPO_ROOT / "var" / "dalton" / "lenses"


def _run_script(script_name: str, args_list: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, str(LENSES_SCRIPTS_DIR / script_name), *args_list, "--json"]
    raw = subprocess.check_output(cmd, cwd=str(REPO_ROOT), text=True, timeout=60)
    return json.loads(raw)


def _verify_conflict_rejection(anchors_path: Path) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(LENSES_SCRIPTS_DIR / "netcash.py"),
        "--anchors",
        str(anchors_path),
        "--market-cap-usd-mn",
        "245000",
        "--cash",
        "1000",
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30)
    return {
        "command": " ".join(cmd[1:]),
        "exit_code": proc.returncode,
        "rejected": proc.returncode != 0,
        "stderr_excerpt": proc.stderr.strip()[-300:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="G6 Financial Lenses Real-Run Evidence Generator")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--date", default="20261019")
    args = parser.parse_args()

    started_at = now_utc_iso()
    test_user_id = new_test_user_id("g6")
    LENSES_OUT_DIR.mkdir(parents=True, exist_ok=True)

    specs: list[dict[str, Any]] = [
        {
            "slug": "baba",
            "ticker": "BABA",
            "anchors_args": [
                "--ticker", "BABA",
                "--period-end", "2026-06-30",
                "--financial-currency", "CNY",
                "--fx-rate", "0.149772",
                "--market-cap-usd-mn", "271437.86",
                "--adr-ratio", "8",
            ],
            "netcash_args": [
                "--cash", "195400",
                "--restricted-cash", "41200",
                "--time-deposits", "178600",
                "--st-investments", "182300",
                "--st-debt", "18500",
                "--cur-lt-debt", "14200",
                "--lt-debt", "136000",
                "--lease-liabilities", "19800",
            ],
            "return_args": [
                "--ttm-div", "1.02",
                "--ttm-buyback", "5.51",
                "--true-return-share", "0.82",
                "--annual", "4.10,5.80,6.53",
                "--fwd-total", "6.20",
                "--credibility", "90",
                "--credibility-reason", "持续执行大规模注销式回购并派发年度股息",
            ],
            "eq_args": [
                "--ni", "142800",
                "--cfo", "180340",
                "--cfi", "-62400",
                "--beginning-assets", "1765000",
                "--ending-assets", "2180000",
            ],
        },
        {
            "slug": "0700hk",
            "ticker": "0700.HK",
            "anchors_args": [
                "--ticker", "0700.HK",
                "--period-end", "2026-06-30",
                "--listing-currency", "HKD",
                "--financial-currency", "CNY",
                "--fx-rate", "0.149772",
                "--listing-fx-rate", "0.127437",
                "--market-cap-usd-mn", "479458.89",
            ],
            "netcash_args": [
                "--cash", "162800",
                "--restricted-cash", "3400",
                "--time-deposits", "198500",
                "--st-investments", "47200",
                "--st-debt", "42100",
                "--cur-lt-debt", "26800",
                "--lt-debt", "270100",
                "--lease-liabilities", "21400",
            ],
            "return_args": [
                "--ttm-div", "0.88",
                "--ttm-buyback", "2.86",
                "--true-return-share", "0.85",
                "--annual", "2.40,3.50,3.74",
                "--fwd-total", "3.60",
                "--credibility", "90",
                "--credibility-reason", "千亿港元回购计划按季稳定兑现并注销股份",
            ],
            "eq_args": [
                "--ni", "196400",
                "--cfo", "261600",
                "--cfi", "-94200",
                "--beginning-assets", "1580000",
                "--ending-assets", "1760000",
            ],
        },
        {
            "slug": "meta",
            "ticker": "META",
            "anchors_args": [
                "--ticker", "META",
                "--period-end", "2026-06-30",
                "--financial-currency", "USD",
                "--fx-rate", "1.0",
                "--market-cap-usd-mn", "1807009.84",
            ],
            "netcash_args": [
                "--cash", "45300",
                "--restricted-cash", "120",
                "--st-investments", "32689",
                "--lt-debt", "28880",
                "--lease-liabilities", "19450",
            ],
            "return_args": [
                "--ttm-div", "0.31",
                "--ttm-buyback", "1.84",
                "--true-return-share", "0.68",
                "--annual", "1.95,2.40,2.15",
                "--fwd-total", "2.10",
                "--credibility", "90",
                "--credibility-reason", "强劲自由现金流支撑季度分红与持续回购",
            ],
            "eq_args": [
                "--ni", "64200",
                "--cfo", "94020",
                "--cfi", "-48500",
                "--beginning-assets", "245000",
                "--ending-assets", "275400",
            ],
        },
    ]

    results_by_ticker: dict[str, Any] = {}
    for item in specs:
        slug = item["slug"]
        ticker = item["ticker"]
        anchors_out = _run_script("anchors.py", item["anchors_args"])
        anchors_file = LENSES_OUT_DIR / f"{slug}_anchors_20261018.json"
        anchors_file.write_text(json.dumps(anchors_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        nc_out = _run_script("netcash.py", ["--anchors", str(anchors_file), *item["netcash_args"]])
        nc_file = LENSES_OUT_DIR / f"{slug}_netcash_20261018.json"
        nc_file.write_text(json.dumps(nc_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        rs_out = _run_script("return_score.py", ["--anchors", str(anchors_file), *item["return_args"]])
        rs_file = LENSES_OUT_DIR / f"{slug}_return_score_20261018.json"
        rs_file.write_text(json.dumps(rs_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        eq_out = _run_script("earnings_quality.py", ["--anchors", str(anchors_file), *item["eq_args"]])
        eq_file = LENSES_OUT_DIR / f"{slug}_earnings_quality_20261018.json"
        eq_file.write_text(json.dumps(eq_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        expected_mcap = float(anchors_out["market_cap"]["market_cap_usd_mn"])
        expected_fx = float(anchors_out["fx"]["rate"])
        expected_period = str(anchors_out["period_end"])

        for lens_name, payload in (
            ("netcash", nc_out),
            ("return_score", rs_out),
            ("earnings_quality", eq_out),
        ):
            assert payload.get("anchors_source") == "anchors", f"{ticker} {lens_name} anchors_source != 'anchors'"
            assert abs(float(payload["market_cap_usd_mn"]) - expected_mcap) < 1e-6, (
                f"{ticker} {lens_name} market_cap_usd_mn mismatch: {payload['market_cap_usd_mn']} vs {expected_mcap}"
            )
            assert abs(float(payload["fx_rate_to_usd"]) - expected_fx) < 1e-9, (
                f"{ticker} {lens_name} fx_rate_to_usd mismatch: {payload['fx_rate_to_usd']} vs {expected_fx}"
            )
            assert str(payload["period_end"]) == expected_period, (
                f"{ticker} {lens_name} period_end mismatch: {payload['period_end']} vs {expected_period}"
            )

        results_by_ticker[ticker] = {
            "period_end": expected_period,
            "market_cap_usd_mn": expected_mcap,
            "fx_rate_to_usd": expected_fx,
            "anchors_source_all": "anchors",
            "basic_net_cash_usd": nc_out["basic_net_cash_usd"],
            "basic_net_cash_to_mcap_pct": nc_out["basic_net_cash_to_mcap_pct"],
            "strict_net_cash_usd": nc_out["strict_net_cash_usd"],
            "strict_net_cash_to_mcap_pct": nc_out["strict_net_cash_to_mcap_pct"],
            "return_final_score": rs_out["final_score"],
            "overall_quality": eq_out["overall_quality"],
            "operating_sloan_pct": eq_out["sloan"]["operating_sloan_pct"],
            "cfo_to_ni_pct": eq_out["cfo_to_ni"]["cfo_to_ni_pct"],
        }

    conflict_check = _verify_conflict_rejection(LENSES_OUT_DIR / "baba_anchors_20261018.json")
    assert conflict_check["rejected"] is True, f"Conflict check did not fail: {conflict_check}"

    ended_at = now_utc_iso()
    script_cmd = f"scripts/dalton/realrun/g6_lenses.py --backend {args.backend} --date {args.date}"
    envelope = build_evidence_envelope(
        script=script_cmd,
        started_at=started_at,
        ended_at=ended_at,
        backend=args.backend,
        test_user_id=test_user_id,
        synthetic=[],
        extra_fields={
            "tickers": results_by_ticker,
            "conflict_rejection_check": conflict_check,
        },
    )

    baba = results_by_ticker["BABA"]
    hk = results_by_ticker["0700.HK"]
    meta = results_by_ticker["META"]
    raw_path, md_path, sha256_hex = write_evidence_and_summary(
        family_code="G6",
        title="三把财务镜头与盈利质量统一 `--anchors` 真实运行验证 (`BABA`, `0700.HK`, `META`)",
        raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g6_lenses.json",
        summary_md_path=f"docs/dalton/dev-guide/trial-runs/realrun-{args.date}/G6.md",
        envelope=envelope,
        key_findings=[
            (
                "`BABA` 统一锚点与输出",
                f"mcap=${baba['market_cap_usd_mn']}M, fx={baba['fx_rate_to_usd']}, strict_net_cash=${baba['strict_net_cash_usd']}M ({baba['strict_net_cash_to_mcap_pct']}%), return_score={baba['return_final_score']}, quality={baba['overall_quality']}",
            ),
            (
                "`0700.HK` 统一锚点与输出",
                f"mcap=${hk['market_cap_usd_mn']}M, fx={hk['fx_rate_to_usd']}, strict_net_cash=${hk['strict_net_cash_usd']}M ({hk['strict_net_cash_to_mcap_pct']}%), return_score={hk['return_final_score']}, quality={hk['overall_quality']}",
            ),
            (
                "`META` 统一锚点与输出",
                f"mcap=${meta['market_cap_usd_mn']}M, fx={meta['fx_rate_to_usd']}, strict_net_cash=${meta['strict_net_cash_usd']}M ({meta['strict_net_cash_to_mcap_pct']}%), return_score={meta['return_final_score']}, quality={meta['overall_quality']}",
            ),
            (
                "`--anchors` 与手填 `--market-cap-usd-mn` 同时传入拒绝检查",
                f"rejected={conflict_check['rejected']} (exit_code={conflict_check['exit_code']})",
            ),
        ],
        notes=[
            "财务镜头为纯确定性 Python 脚本，不发生 LLM 调用（`dalton_llm_journal` 调用数为 0）。",
            "三组标的的 `netcash.py`、`return_score.py`、`earnings_quality.py` 均通过 `--anchors` 读取同一份 `anchors.py` 输出 JSON，断言 `market_cap_usd_mn`、`fx_rate_to_usd`、`period_end` 完全相同且 `anchors_source == 'anchors'`。",
        ],
    )
    print(json.dumps({"raw_json": str(raw_path), "summary_md": str(md_path), "sha256": sha256_hex}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
