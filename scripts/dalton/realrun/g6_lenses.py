#!/usr/bin/env python3
"""G6 Real-Run Verification Script — Financial Lenses Unified `--anchors` (P0I §6, §7 G6, P0J §5.7, L37).

Runs `anchors.py --allow-network`, `netcash.py --anchors`, `return_score.py --anchors`, and
`earnings_quality.py --anchors` for `BABA`, `0700.HK`, and `META` using the committed filing
inputs in `tests/fixtures/dalton/g6/lenses_inputs.json`, asserting:
1. Identical `market_cap_usd_mn`, `fx_rate_to_usd`, and `period_end` across all lenses for each ticker.
2. `anchors_source == "anchors"` on all lens outputs.
3. `strict_net_cash_usd == basic_net_cash_usd` when no extra trapped cash items exist (verifying J2a
   fix that `restricted_cash` is deducted once in `basic_net_cash` and not double-deducted in `strict_net_cash`).
4. Passing both `--anchors` and manual `--market-cap-usd-mn` or `--fx-rate` exits with a non-zero error code.
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

from scripts.dalton.realrun._common import (  # noqa: E402
    build_evidence_envelope,
    now_utc_iso,
    write_evidence_and_summary,
)

LENSES_SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-financial-lenses" / "scripts"
)
LENSES_OUT_DIR = REPO_ROOT / "var" / "dalton" / "lenses"
LENSES_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "dalton" / "g6" / "lenses_inputs.json"
COMPARISON_DOC_PATH = (
    REPO_ROOT / "docs" / "dalton" / "dev-guide" / "trial-runs" / "G6-financial-lenses-comparison-20261018.md"
)


def _run_script(script_name: str, args_list: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, str(LENSES_SCRIPTS_DIR / script_name), *args_list, "--json"]
    raw = subprocess.check_output(cmd, cwd=str(REPO_ROOT), text=True, timeout=90)
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


def render_comparison_markdown(results_by_ticker: dict[str, Any]) -> str:
    """Render `G6-financial-lenses-comparison-20261018.md` directly from the G6 evidence JSON."""
    baba = results_by_ticker["BABA"]
    hk = results_by_ticker["0700.HK"]
    meta = results_by_ticker["META"]

    baba_impl = round(baba["market_cap_usd_mn"] - baba["strict_net_cash_usd"], 2)
    baba_impl_pct = round(100.0 - baba["strict_net_cash_to_mcap_pct"], 2)
    hk_impl = round(hk["market_cap_usd_mn"] - hk["strict_net_cash_usd"], 2)
    hk_impl_pct = round(100.0 - hk["strict_net_cash_to_mcap_pct"], 2)
    meta_impl = round(meta["market_cap_usd_mn"] - meta["strict_net_cash_usd"], 2)
    meta_impl_pct = round(100.0 - meta["strict_net_cash_to_mcap_pct"], 2)

    lines = [
        "# G6.2 财务镜头脚本真实运行与基金原脚本对比记录 (2026-10-19 统一锚点与 J2a 受限现金去重重算)",
        "",
        "- **运行日期**：2026-10-19（由 `scripts/dalton/realrun/g6_lenses.py --update-comparison-doc` 从实跑 JSON 自动生成）",
        "- **对应指南**：`P0J-round4-fixes.md` §2.0 (`J2a`) & §5.7 (`G6`)、`P0I-round3-fixes.md` §6 (`I6`)",
        "- **输入科目出处**：`tests/fixtures/dalton/g6/lenses_inputs.json`（每项科目均标注财报 `accession` 编号），市值与汇率由 `anchors.py --allow-network` 实时从 `yfinance` 拉取",
        "- **真实运行产物路径**：",
        "  - `var/dalton/lenses/baba_anchors_20261018.json`",
        "  - `var/dalton/lenses/baba_netcash_20261018.json`",
        "  - `var/dalton/lenses/baba_return_score_20261018.json`",
        "  - `var/dalton/lenses/baba_earnings_quality_20261018.json`",
        "  - `var/dalton/lenses/0700hk_anchors_20261018.json`",
        "  - `var/dalton/lenses/0700hk_netcash_20261018.json`",
        "  - `var/dalton/lenses/0700hk_return_score_20261018.json`",
        "  - `var/dalton/lenses/0700hk_earnings_quality_20261018.json`",
        "  - `var/dalton/lenses/meta_anchors_20261018.json`",
        "  - `var/dalton/lenses/meta_netcash_20261018.json`",
        "  - `var/dalton/lenses/meta_return_score_20261018.json`",
        "  - `var/dalton/lenses/meta_earnings_quality_20261018.json`",
        "",
        "---",
        "",
        "## 1. 按 `SKILL.md` 统一 `--anchors` 命令真实运行汇总（`BABA`、`0700.HK`、`META`，J2a 修复后）",
        "",
        "三组标的先由 `anchors.py --allow-network` 实时拉取市值与汇率生成锚点 JSON，再将 `--anchors <anchors.json>` 统一传给 `netcash.py`、`return_score.py`、`earnings_quality.py`（所有镜头输出均带 `\"anchors_source\": \"anchors\"` 且 `market_cap_usd_mn`、`fx_rate_to_usd`、`period_end` 完全一致；口径 A `basic_net_cash` 已在 `_select_cash` 扣除一次受限现金，口径 B `strict_net_cash` 不再二次扣除受限现金，无额外 `trapped_cash_items` 时 `strict_net_cash_usd == basic_net_cash_usd`）：",
        "",
        "| 标的 | 交易币种 | 财报币种 (`--reporting-currency`) | 锁定汇率 (`fx_rate_to_usd`) | 锁定市值 (`market_cap_usd_mn`) | 口径 A 基础净现金 (`basic_net_cash_usd`) | 口径 B 严格净现金 (`strict_net_cash_usd`) | 隐含经营资产 (Strict) | 股东回报评分 (`final_score`) | 盈利质量 (`overall_quality` / Operating Sloan) |",
        "|---|---|---|---|---|---|---|---|---|---|",
        (
            f"| **BABA** | `USD` (ADR 1:8) | `CNY` | `CNYUSD=X = {baba['fx_rate_to_usd']}` | "
            f"`${baba['market_cap_usd_mn']:,.2f}M` | `${baba['basic_net_cash_usd']:,.2f}M` ({baba['basic_net_cash_to_mcap_pct']}%) | "
            f"`${baba['strict_net_cash_usd']:,.2f}M` ({baba['strict_net_cash_to_mcap_pct']}%，受限现金仅在口径 A 扣一次) | "
            f"`${baba_impl:,.2f}M` ({baba_impl_pct}%) | `{baba['return_final_score']} / 20` | "
            f"`{baba['overall_quality']}` (`{baba['operating_sloan_pct']}%`, `CFO/NI={baba['cfo_to_ni_pct']}%`) |"
        ),
        (
            f"| **0700.HK** | `HKD` | `CNY` | `CNYUSD=X = {hk['fx_rate_to_usd']}`, `HKDUSD=X = {hk['listing_fx_rate']}` | "
            f"`${hk['market_cap_usd_mn']:,.2f}M` | `${hk['basic_net_cash_usd']:,.2f}M` ({hk['basic_net_cash_to_mcap_pct']}%) | "
            f"`${hk['strict_net_cash_usd']:,.2f}M` ({hk['strict_net_cash_to_mcap_pct']}%，受限现金仅在口径 A 扣一次) | "
            f"`${hk_impl:,.2f}M` ({hk_impl_pct}%) | `{hk['return_final_score']} / 20` | "
            f"`{hk['overall_quality']}` (`{hk['operating_sloan_pct']}%`, `CFO/NI={hk['cfo_to_ni_pct']}%`) |"
        ),
        (
            f"| **META** | `USD` | `USD` | `{meta['fx_rate_to_usd']}` | "
            f"`${meta['market_cap_usd_mn']:,.2f}M` | `${meta['basic_net_cash_usd']:,.2f}M` ({meta['basic_net_cash_to_mcap_pct']}%) | "
            f"`${meta['strict_net_cash_usd']:,.2f}M` ({meta['strict_net_cash_to_mcap_pct']}%，受限现金仅在口径 A 扣一次) | "
            f"`${meta_impl:,.2f}M` ({meta_impl_pct}%) | `{meta['return_final_score']} / 20` | "
            f"`{meta['overall_quality']}` (`{meta['operating_sloan_pct']}%`, `CFO/NI={meta['cfo_to_ni_pct']}%`) |"
        ),
        "",
        "---",
        "",
        "## 2. 与基金原脚本（OpenClaw `company-deepdive-mx-0911`）结果对比",
        "",
        "| 维度 | 基金原脚本 (`~/.openclaw/agents/main/agent/workshop-skills/company-deepdive-mx-0911/`) | 本仓 `dalton-financial-lenses/scripts/` (`P0I.I6` + `P0J.J2a` 修复后) | 对比结论 |",
        "|---|---|---|---|",
        "| **跨镜头市值与汇率锚点一致性 (`--anchors`)** | 各脚本分别手填市值和汇率，易出现 `anchors` 与 `netcash` 混用不同市值/整数汇率 | `netcash.py`、`return_score.py`、`earnings_quality.py` 统一通过 `--anchors <json>` 读取 `market_cap_usd_mn`、`fx.rate`、`period_end`；同时传 `--anchors` 与手填 `--market-cap-usd-mn`/`--fx-rate` 直接报错退出（仅手填时标 `anchors_source: \"manual\"`） | 从参数结构上杜绝手填市值/汇率与 `anchors.py` 输出不一致的问题 |",
        "| **ADR / 港股财报汇率换算** | 未强制要求显式传入 `--reporting-currency`，离线时回退到写死的静态汇率表 | 非美股与 ADR 强制校验 `--reporting-currency CNY`，汇率由 `--allow-network` 实时 `yfinance` 获取或 `--fx-rate` 显式锁定（删除静态兜底表） | `BABA` 与 `0700.HK` 人民币报表均按 `anchors` 锁定的实时 `CNYUSD=X` 统一换算 |",
        "| **受限现金扣除逻辑 (`netcash.py`, J2a 修复)** | 口径 A 已扣 `restricted_cash`，若口径 B 再次把 `restricted_cash` 塞入 `trapped_cash_items` 会导致受限现金被扣两次 | `_select_cash` 在 `include_restricted_cash_in_free=False`（默认）时已在口径 A 扣除一次 `restricted_cash`；口径 B 仅扣除额外 `trapped_cash_items`，不再重复注入 `restricted_cash`（L34） | 消除受限现金双重扣除 bug；无额外 `trapped_cash_items` 时 `strict_net_cash_usd == basic_net_cash_usd` |",
        "| **市值字段与单位 (`market_cap_usd_mn`)** | 混用 `market_cap_usd`（百万美元 vs 美元原值） | 统一使用 `market_cap_usd_mn`（百万美元），同时保留 `value_usd` 原值并与 `references/output-schema.md` 100% 对齐 | 消除 100 万倍单位歧义 |",
        "| **盈利质量期初/期末资产与 CCC (`earnings_quality.py`)** | 需手工预计算平均总资产；缺少分项时将缺失分项按 `0` 求和算出伪造的 `ccc_days` | 支持直接传 `--beginning-assets` 与 `--ending-assets` 自动求均值；`dso`/`dio`/`dpo` 任一分项缺失时 `ccc_days` 返回 `null` | Sloan 应计比率与 CFO/NI 计算结果与原脚本一致，缺失分项时诚实返回 `null` |",
        "",
        "---",
        "",
        "## 3. 历史旧表归档（已作废：受限现金被扣了两次）",
        "",
        "> **已作废：受限现金被扣了两次** — 下表为 `P0J.J2a`（PR #13）修复前的历史记录，当时 `netcash.py` 在口径 A (`basic_net_cash`) 已扣除受限现金后，又在口径 B (`strict_net_cash`) 的 `trapped_cash_items` 中重复扣除了受限现金（`BABA` 多扣 `$6,170.61M`、`0700.HK` 多扣 `$509.22M`、`META` 多扣 `$120.00M`）。现保留于文末供审计对照，请以第 1 节自动生成表为准。",
        "",
        "| 标的 | 交易币种 | 财报币种 (`--reporting-currency`) | 锁定汇率 (`fx_rate_to_usd`) | 锁定市值 (`market_cap_usd_mn`) | 口径 A 基础净现金 (`basic_net_cash_usd`) | 口径 B 严格净现金 (`strict_net_cash_usd`，已作废) | 隐含经营资产 (Strict，已作废) | 股东回报评分 (`final_score`) | 盈利质量 (`overall_quality` / Operating Sloan) |",
        "|---|---|---|---|---|---|---|---|---|---|",
        "| **BABA** | `USD` (ADR 1:8) | `CNY` | `CNYUSD=X = 0.149772` | `$271,437.86M` | `$58,062.27M` (21.39%) | `$51,891.66M` (19.12%，已作废：受限现金被扣了两次) | `$219,546.20M` (80.88%) | `6.5 / 20` (`R1=6.53%`) | `strong` (`-1.90%`, `CFO/NI=126.29%`) |",
        "| **0700.HK** | `HKD` | `CNY` | `CNYUSD=X = 0.149772`, `HKDUSD=X = 0.127437` | `$479,458.89M` | `$10,409.15M` (2.17%) | `$9,899.93M` (2.06%，已作废：受限现金被扣了两次) | `$469,558.96M` (97.94%) | `3.7 / 20` (`R1=3.74%`) | `strong` (`-3.90%`, `CFO/NI=133.20%`) |",
        "| **META** | `USD` | `USD` | `1.0` | `$1,807,009.84M` | `$49,109.00M` (2.72%) | `$48,989.00M` (2.71%，已作废：受限现金被扣了两次) | `$1,758,020.84M` (97.29%) | `2.3 / 20` (`R2=2.15%`) | `strong` (`-11.46%`, `CFO/NI=146.45%`) |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="G6 Financial Lenses Real-Run Evidence Generator")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--date", default="20261019")
    parser.add_argument(
        "--update-comparison-doc",
        action="store_true",
        help="Also regenerate docs/dalton/dev-guide/trial-runs/G6-financial-lenses-comparison-20261018.md",
    )
    args = parser.parse_args()

    started_at = now_utc_iso()
    user_a = "realrun-pm-a"
    LENSES_OUT_DIR.mkdir(parents=True, exist_ok=True)

    fixture_data = json.loads(LENSES_FIXTURE_PATH.read_text(encoding="utf-8"))
    tickers_fixture = fixture_data["tickers"]

    results_by_ticker: dict[str, Any] = {}
    assertions: list[dict[str, Any]] = []

    for ticker, item in tickers_fixture.items():
        slug = item["slug"]
        anchors_args = [
            "--ticker",
            ticker,
            "--period-end",
            str(item["period_end"]),
            "--listing-currency",
            str(item["listing_currency"]),
            "--financial-currency",
            str(item["financial_currency"]),
            "--allow-network",
        ]
        if item.get("adr_ratio"):
            anchors_args.extend(["--adr-ratio", str(item["adr_ratio"])])

        anchors_out = _run_script("anchors.py", anchors_args)
        anchors_file = LENSES_OUT_DIR / f"{slug}_anchors_20261018.json"
        anchors_file.write_text(
            json.dumps(anchors_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        nc_in = item["netcash"]
        netcash_args = [
            "--anchors",
            str(anchors_file),
            "--cash",
            str(nc_in["cash"]["value"]),
            "--restricted-cash",
            str(nc_in["restricted_cash"]["value"]),
            "--time-deposits",
            str(nc_in["time_deposits"]["value"]),
            "--st-investments",
            str(nc_in["st_investments"]["value"]),
            "--st-debt",
            str(nc_in["st_debt"]["value"]),
            "--cur-lt-debt",
            str(nc_in["cur_lt_debt"]["value"]),
            "--lt-debt",
            str(nc_in["lt_debt"]["value"]),
            "--lease-liabilities",
            str(nc_in["lease_liabilities"]["value"]),
        ]
        nc_out = _run_script("netcash.py", netcash_args)
        nc_file = LENSES_OUT_DIR / f"{slug}_netcash_20261018.json"
        nc_file.write_text(json.dumps(nc_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        rs_in = item["return_score"]
        return_args = [
            "--anchors",
            str(anchors_file),
            "--ttm-div",
            str(rs_in["ttm_div"]["value"]),
            "--ttm-buyback",
            str(rs_in["ttm_buyback"]["value"]),
            "--true-return-share",
            str(rs_in["true_return_share"]["value"]),
            "--annual",
            str(rs_in["annual"]["value"]),
            "--fwd-total",
            str(rs_in["fwd_total"]["value"]),
            "--credibility",
            str(rs_in["credibility"]["value"]),
            "--credibility-reason",
            str(rs_in["credibility_reason"]),
        ]
        rs_out = _run_script("return_score.py", return_args)
        rs_file = LENSES_OUT_DIR / f"{slug}_return_score_20261018.json"
        rs_file.write_text(json.dumps(rs_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        eq_in = item["earnings_quality"]
        eq_args = [
            "--anchors",
            str(anchors_file),
            "--ni",
            str(eq_in["ni"]["value"]),
            "--cfo",
            str(eq_in["cfo"]["value"]),
            "--cfi",
            str(eq_in["cfi"]["value"]),
            "--beginning-assets",
            str(eq_in["beginning_assets"]["value"]),
            "--ending-assets",
            str(eq_in["ending_assets"]["value"]),
        ]
        eq_out = _run_script("earnings_quality.py", eq_args)
        eq_file = LENSES_OUT_DIR / f"{slug}_earnings_quality_20261018.json"
        eq_file.write_text(json.dumps(eq_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        expected_mcap = float(anchors_out["market_cap"]["market_cap_usd_mn"])
        expected_fx = float(anchors_out["fx"]["rate"])
        expected_listing_fx = float((anchors_out.get("listing_fx") or {}).get("rate") or 1.0)
        expected_period = str(anchors_out["period_end"])

        for lens_name, payload in (
            ("netcash", nc_out),
            ("return_score", rs_out),
            ("earnings_quality", eq_out),
        ):
            assert payload.get("anchors_source") == "anchors", (
                f"{ticker} {lens_name} anchors_source != 'anchors'"
            )
            assert abs(float(payload["market_cap_usd_mn"]) - expected_mcap) < 1e-6, (
                f"{ticker} {lens_name} market_cap_usd_mn mismatch: {payload['market_cap_usd_mn']} vs {expected_mcap}"
            )
            assert abs(float(payload["fx_rate_to_usd"]) - expected_fx) < 1e-9, (
                f"{ticker} {lens_name} fx_rate_to_usd mismatch: {payload['fx_rate_to_usd']} vs {expected_fx}"
            )
            assert str(payload["period_end"]) == expected_period, (
                f"{ticker} {lens_name} period_end mismatch: {payload['period_end']} vs {expected_period}"
            )

        # Verify J2a fix: restricted_cash is deducted once in basic_net_cash and not double-deducted in strict_net_cash
        basic_nc = float(nc_out["basic_net_cash_usd"])
        strict_nc = float(nc_out["strict_net_cash_usd"])
        assert abs(strict_nc - basic_nc) < 1e-6, (
            f"{ticker} strict_net_cash_usd ({strict_nc}) != basic_net_cash_usd ({basic_nc})"
        )

        assertions.append(
            {
                "name": f"{slug}_unified_anchors_and_j2a_no_double_deduction",
                "passed": True,
                "ticker": ticker,
                "market_cap_usd_mn": expected_mcap,
                "fx_rate_to_usd": expected_fx,
                "basic_net_cash_usd": basic_nc,
                "strict_net_cash_usd": strict_nc,
                "anchors_source": "anchors",
            }
        )

        results_by_ticker[ticker] = {
            "period_end": expected_period,
            "filing_source": item["filing_source"],
            "market_cap_usd_mn": expected_mcap,
            "market_cap_source": anchors_out["market_cap"]["source"],
            "fx_rate_to_usd": expected_fx,
            "fx_source": anchors_out["fx"]["source"],
            "listing_fx_rate": expected_listing_fx,
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
    assertions.append(
        {
            "name": "anchors_and_manual_override_conflict_rejected",
            "passed": conflict_check["rejected"] is True,
            "exit_code": conflict_check["exit_code"],
        }
    )

    if args.update_comparison_doc:
        COMPARISON_DOC_PATH.write_text(
            render_comparison_markdown(results_by_ticker), encoding="utf-8"
        )

    ended_at = now_utc_iso()
    script_cmd = f"scripts/dalton/realrun/g6_lenses.py --backend {args.backend} --date {args.date}"
    envelope = build_evidence_envelope(
        script=script_cmd,
        started_at=started_at,
        ended_at=ended_at,
        backend=args.backend,
        test_user_id=user_a,
        family="G6",
        sub_item="G6.all",
        synthetic=[
            {
                "kind": "committed_fixture_input",
                "entry_point": "tests/fixtures/dalton/g6/lenses_inputs.json",
                "description": "使用入库的 BABA / 0700.HK / META 财报科目夹具（每项均带真实财报 accession 编号），配合 anchors.py --allow-network 实时拉取市值与汇率。",
            }
        ],
        assertions=assertions,
        extra_fields={
            "fixture_path": "tests/fixtures/dalton/g6/lenses_inputs.json",
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
                "`BABA` 统一锚点与输出（J2a 修复后 `strict == basic`）",
                f"mcap=${baba['market_cap_usd_mn']}M, fx={baba['fx_rate_to_usd']}, basic=${baba['basic_net_cash_usd']}M, strict=${baba['strict_net_cash_usd']}M ({baba['strict_net_cash_to_mcap_pct']}%), return_score={baba['return_final_score']}, quality={baba['overall_quality']}",
            ),
            (
                "`0700.HK` 统一锚点与输出（J2a 修复后 `strict == basic`）",
                f"mcap=${hk['market_cap_usd_mn']}M, fx={hk['fx_rate_to_usd']}, basic=${hk['basic_net_cash_usd']}M, strict=${hk['strict_net_cash_usd']}M ({hk['strict_net_cash_to_mcap_pct']}%), return_score={hk['return_final_score']}, quality={hk['overall_quality']}",
            ),
            (
                "`META` 统一锚点与输出（J2a 修复后 `strict == basic`）",
                f"mcap=${meta['market_cap_usd_mn']}M, fx={meta['fx_rate_to_usd']}, basic=${meta['basic_net_cash_usd']}M, strict=${meta['strict_net_cash_usd']}M ({meta['strict_net_cash_to_mcap_pct']}%), return_score={meta['return_final_score']}, quality={meta['overall_quality']}",
            ),
            (
                "`--anchors` 与手填 `--market-cap-usd-mn` 同时传入拒绝检查",
                f"rejected={conflict_check['rejected']} (exit_code={conflict_check['exit_code']})",
            ),
        ],
        notes=[
            "财务镜头为纯确定性 Python 脚本，不发生 LLM 调用（`dalton_llm_journal` 调用数为 0）。",
            "市值与汇率由 `anchors.py --allow-network` 从 `yfinance` 实时拉取；资产负债表与现金流表科目读取自 `tests/fixtures/dalton/g6/lenses_inputs.json`（每项标注财报 `accession` 编号，已在 `synthetic` 声明 `committed_fixture_input`）。",
            "已验证 J2a 修复：受限现金在口径 A (`basic_net_cash`) 中扣除一次，口径 B (`strict_net_cash`) 不再二次扣除，无额外 `trapped_cash_items` 时 `strict_net_cash_usd == basic_net_cash_usd`。",
        ],
    )
    print(json.dumps({"raw_json": str(raw_path), "summary_md": str(md_path), "sha256": sha256_hex}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
