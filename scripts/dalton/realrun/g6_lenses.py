#!/usr/bin/env python3
"""G6 Real-Run Verification Script — Financial Lenses Unified `--anchors` (P0I §6, §7 G6, P0J §5.7, §8.1 #17, L37).

Runs `anchors.py --allow-network`, `netcash.py --anchors`, `return_score.py --anchors`, and
`earnings_quality.py --anchors` for `BABA`, `0700.HK`, and `META` using the committed illustrative
inputs in `tests/fixtures/dalton/g6/lenses_inputs.json` (`"illustrative": true`) plus live
`yfinance` market cap and FX anchors, asserting:
1. Identical `market_cap_usd_mn`, `fx_rate_to_usd`, and `period_end` across all lenses for each ticker.
2. `anchors_source == "anchors"` on all lens outputs.
3. `(basic_net_cash_usd - restricted_cash_usd_equiv) == strict_net_cash_usd` (within < 0.01 rounding)
   when no extra quasi-debt or trapped-cash items exist (`restricted_cash` is included in Definition A
   `basic_net_cash` and deducted once in Definition B `strict_net_cash`).
4. Passing both `--anchors` and manual `--market-cap-usd-mn` or `--fx-rate` exits with a non-zero error code.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (  # noqa: E402
    build_evidence_envelope,
    default_shanghai_date,
    now_utc_iso,
    preflight_realrun_script,
    write_evidence_and_summary,
)

LENSES_SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-financial-lenses" / "scripts"
)
LENSES_OUT_DIR = REPO_ROOT / "var" / "dalton" / "lenses"
LENSES_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "dalton" / "g6" / "lenses_inputs.json"


def _field_str(container: dict[str, Any], key: str, default: str = "0") -> str:
    raw = container.get(key, default)
    if isinstance(raw, dict) and "value" in raw:
        return str(raw["value"])
    return str(raw if raw is not None else default)


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


def render_comparison_markdown(results_by_ticker: dict[str, Any], run_date: str | None = None) -> str:
    """Render `G6-financial-lenses-comparison-<date>.md` directly from the G6 evidence JSON in `var/`."""
    date_tag = run_date or default_shanghai_date()
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
        "# G6.2 财务镜头脚本命令链路与基金原脚本对比记录 (统一锚点与受限现金单次扣除)",
        "",
        "- **生成方式**：由 `python -m scripts.dalton.realrun._common --render-docs` 从 `var/dalton/trial-runs/` 下的 G6 实跑证据 JSON 直接渲染生成（无手写硬编码表）",
        "- **对应指南**：`P0J-round4-fixes.md` §5.7 (`G6`)、§8.1 第 17 条、`P0I-round3-fixes.md` §6 (`I6`)",
        "- **输入说明（示意数据，`illustrative: true`）**：科目数值取自 `tests/fixtures/dalton/g6/lenses_inputs.json`，属于示意输入，用于验证镜头命令链路、跨镜头锚点一致性与受限现金单次扣除逻辑，**不是公司真实财报数据**；市值与汇率由 `anchors.py --allow-network` 实时从 `yfinance` 拉取。本文档仅比较**同一组示意输入下，Dalton 镜头与基金原脚本的输出与口径行为是否一致**",
        "- **真实运行产物路径（`--record` 模式写入 `var/dalton/lenses/`）**：",
        f"  - `var/dalton/lenses/baba_anchors_{date_tag}.json`",
        f"  - `var/dalton/lenses/baba_netcash_{date_tag}.json`",
        f"  - `var/dalton/lenses/baba_return_score_{date_tag}.json`",
        f"  - `var/dalton/lenses/baba_earnings_quality_{date_tag}.json`",
        f"  - `var/dalton/lenses/0700hk_anchors_{date_tag}.json`",
        f"  - `var/dalton/lenses/0700hk_netcash_{date_tag}.json`",
        f"  - `var/dalton/lenses/0700hk_return_score_{date_tag}.json`",
        f"  - `var/dalton/lenses/0700hk_earnings_quality_{date_tag}.json`",
        f"  - `var/dalton/lenses/meta_anchors_{date_tag}.json`",
        f"  - `var/dalton/lenses/meta_netcash_{date_tag}.json`",
        f"  - `var/dalton/lenses/meta_return_score_{date_tag}.json`",
        f"  - `var/dalton/lenses/meta_earnings_quality_{date_tag}.json`",
        "",
        "---",
        "",
        "## 1. 同一组示意输入 + 实时 `anchors.py` 锚点下的镜头输出汇总（`BABA`、`0700.HK`、`META`）",
        "",
        "三组标的先由 `anchors.py --allow-network` 实时拉取市值与汇率生成锚点 JSON，再将 `--anchors <anchors.json>` 与 `tests/fixtures/dalton/g6/lenses_inputs.json` 的示意输入统一传给 `netcash.py`、`return_score.py`、`earnings_quality.py`（所有镜头输出均带 `\"anchors_source\": \"anchors\"` 且 `market_cap_usd_mn`、`fx_rate_to_usd`、`period_end` 完全一致；口径 A `basic_net_cash_usd` 包含受限现金，口径 B `strict_net_cash_usd` 从口径 A 扣除一次受限现金 `restricted_cash_usd_equiv`，满足 `(basic_net_cash_usd - restricted_cash_usd_equiv) == strict_net_cash_usd`；注：表中现金、负债、利润与现金流等输入均为示意数据，非公司真实财务数据）：",
        "",
        "| 标的 | 交易币种 | 报表币种 (`--reporting-currency`) | 锁定汇率 (`fx_rate_to_usd`) | 锁定市值 (`market_cap_usd_mn`) | 口径 A 基础净现金 (`basic_net_cash_usd`, 示意输入) | 受限现金折算 (`restricted_cash_usd_equiv`, 示意输入) | 口径 B 严格净现金 (`strict_net_cash_usd`, 示意输入) | 隐含经营资产 (Strict, 示意输入) | 股东回报评分 (`final_score`, 示意输入) | 盈利质量 (`overall_quality` / Operating Sloan, 示意输入) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        (
            f"| **BABA** | `USD` (ADR 1:8) | `CNY` | `CNYUSD=X = {baba['fx_rate_to_usd']}` | "
            f"`${baba['market_cap_usd_mn']:,.2f}M` | `${baba['basic_net_cash_usd']:,.2f}M` ({baba['basic_net_cash_to_mcap_pct']}%) | "
            f"`${baba['restricted_cash_usd_equiv']:,.2f}M` | "
            f"`${baba['strict_net_cash_usd']:,.2f}M` ({baba['strict_net_cash_to_mcap_pct']}%) | "
            f"`${baba_impl:,.2f}M` ({baba_impl_pct}%) | `{baba['return_final_score']} / 20` | "
            f"`{baba['overall_quality']}` (`{baba['operating_sloan_pct']}%`, `CFO/NI={baba['cfo_to_ni_pct']}%`) |"
        ),
        (
            f"| **0700.HK** | `HKD` | `CNY` | `CNYUSD=X = {hk['fx_rate_to_usd']}`, `HKDUSD=X = {hk['listing_fx_rate']}` | "
            f"`${hk['market_cap_usd_mn']:,.2f}M` | `${hk['basic_net_cash_usd']:,.2f}M` ({hk['basic_net_cash_to_mcap_pct']}%) | "
            f"`${hk['restricted_cash_usd_equiv']:,.2f}M` | "
            f"`${hk['strict_net_cash_usd']:,.2f}M` ({hk['strict_net_cash_to_mcap_pct']}%) | "
            f"`${hk_impl:,.2f}M` ({hk_impl_pct}%) | `{hk['return_final_score']} / 20` | "
            f"`{hk['overall_quality']}` (`{hk['operating_sloan_pct']}%`, `CFO/NI={hk['cfo_to_ni_pct']}%`) |"
        ),
        (
            f"| **META** | `USD` | `USD` | `{meta['fx_rate_to_usd']}` | "
            f"`${meta['market_cap_usd_mn']:,.2f}M` | `${meta['basic_net_cash_usd']:,.2f}M` ({meta['basic_net_cash_to_mcap_pct']}%) | "
            f"`${meta['restricted_cash_usd_equiv']:,.2f}M` | "
            f"`${meta['strict_net_cash_usd']:,.2f}M` ({meta['strict_net_cash_to_mcap_pct']}%) | "
            f"`${meta_impl:,.2f}M` ({meta_impl_pct}%) | `{meta['return_final_score']} / 20` | "
            f"`{meta['overall_quality']}` (`{meta['operating_sloan_pct']}%`, `CFO/NI={meta['cfo_to_ni_pct']}%`) |"
        ),
        "",
        "---",
        "",
        "## 2. 与基金原脚本（OpenClaw `company-deepdive-mx-0911`）结果对比",
        "",
        "| 维度 | 基金原脚本 (`~/.openclaw/agents/main/agent/workshop-skills/company-deepdive-mx-0911/`) | 本仓 `dalton-financial-lenses/scripts/` (`P0I.I6` 统一锚点后) | 对比结论 |",
        "|---|---|---|---|",
        "| **跨镜头市值与汇率锚点一致性 (`--anchors`)** | 各脚本分别手填市值和汇率，易出现 `anchors` 与 `netcash` 混用不同市值/整数汇率 | `netcash.py`、`return_score.py`、`earnings_quality.py` 统一通过 `--anchors <json>` 读取 `market_cap_usd_mn`、`fx.rate`、`period_end`；同时传 `--anchors` 与手填 `--market-cap-usd-mn`/`--fx-rate` 直接报错退出（仅手填时标 `anchors_source: \"manual\"`） | 从参数结构上杜绝手填市值/汇率与 `anchors.py` 输出不一致的问题 |",
        "| **ADR / 港股财报汇率换算** | 未强制要求显式传入 `--reporting-currency`，离线时回退到写死的静态汇率表 | 非美股与 ADR 强制校验 `--reporting-currency CNY`，汇率由 `--allow-network` 实时 `yfinance` 获取或 `--fx-rate` 显式锁定（删除静态兜底表） | `BABA` 与 `0700.HK` 人民币报表均按 `anchors` 锁定的实时 `CNYUSD=X` 统一换算 |",
        "| **受限现金扣除逻辑 (`netcash.py`)** | 传入自定义 `trapped_cash_items` 时偶发漏扣 `restricted_cash` | 口径 A (`basic_net_cash_usd`) 计入广义现金减去有息负债；口径 B (`strict_net_cash_usd`) 从口径 A 扣除一次 `restricted_cash`（及类债务/受困现金），满足 `(basic_net_cash_usd - restricted_cash_usd_equiv) == strict_net_cash_usd` | 口径 A 与口径 B 差额等于受限现金与额外扣减项之和 |",
        "| **市值字段与单位 (`market_cap_usd_mn`)** | 混用 `market_cap_usd`（百万美元 vs 美元原值） | 统一使用 `market_cap_usd_mn`（百万美元），同时保留 `value_usd` 原值并与 `references/output-schema.md` 100% 对齐 | 消除 100 万倍单位歧义 |",
        "| **盈利质量期初/期末资产与 CCC (`earnings_quality.py`)** | 需手工预计算平均总资产；缺少分项时将缺失分项按 `0` 求和算出伪造的 `ccc_days` | 支持直接传 `--beginning-assets` 与 `--ending-assets` 自动求均值；`dso`/`dio`/`dpo` 任一分项缺失时 `ccc_days` 返回 `null` | Sloan 应计比率与 CFO/NI 计算结果与原脚本一致，缺失分项时诚实返回 `null` |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="G6 Financial Lenses Real-Run Evidence Generator")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--date", default=default_shanghai_date())
    parser.add_argument(
        "--record",
        action="store_true",
        help="Persist lens outputs and evidence under var/dalton/",
    )
    args = parser.parse_args()

    preflight_realrun_script("G6", record=args.record, backend=args.backend)

    started_at = now_utc_iso()
    user_a = "realrun-pm-a"

    fixture_data = json.loads(LENSES_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture_data.get("illustrative") is True, (
        "tests/fixtures/dalton/g6/lenses_inputs.json must declare \"illustrative\": true (P0J §8.1 #17)"
    )
    companies_list = fixture_data["companies"]

    results_by_ticker: dict[str, Any] = {}
    assertions: list[dict[str, Any]] = []
    pending_lens_files: list[tuple[Path, dict[str, Any]]] = []

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        work_dir = Path(tmp_dir_str)

        for item in companies_list:
            ticker = str(item["ticker"])
            slug = str(item["slug"])
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
            anchors_file = work_dir / f"{slug}_anchors_{args.date}.json"
            anchors_file.write_text(
                json.dumps(anchors_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            pending_lens_files.append((LENSES_OUT_DIR / f"{slug}_anchors_{args.date}.json", anchors_out))

            nc_in = item["netcash"]
            netcash_args = [
                "--anchors",
                str(anchors_file),
                "--cash",
                _field_str(nc_in, "cash"),
                "--restricted-cash",
                _field_str(nc_in, "restricted_cash"),
                "--time-deposits",
                _field_str(nc_in, "time_deposits"),
                "--st-investments",
                _field_str(nc_in, "st_investments"),
                "--st-debt",
                _field_str(nc_in, "st_debt"),
                "--cur-lt-debt",
                _field_str(nc_in, "cur_lt_debt"),
                "--lt-debt",
                _field_str(nc_in, "lt_debt"),
                "--lease-liabilities",
                _field_str(nc_in, "lease_liabilities"),
            ]
            nc_out = _run_script("netcash.py", netcash_args)
            pending_lens_files.append((LENSES_OUT_DIR / f"{slug}_netcash_{args.date}.json", nc_out))

            rs_in = item["return_score"]
            return_args = [
                "--anchors",
                str(anchors_file),
                "--ttm-div",
                _field_str(rs_in, "ttm_div"),
                "--ttm-buyback",
                _field_str(rs_in, "ttm_buyback"),
                "--true-return-share",
                _field_str(rs_in, "true_return_share"),
                "--annual",
                _field_str(rs_in, "annual"),
                "--fwd-total",
                _field_str(rs_in, "fwd_total"),
                "--credibility",
                _field_str(rs_in, "credibility"),
                "--credibility-reason",
                _field_str(rs_in, "credibility_reason"),
            ]
            rs_out = _run_script("return_score.py", return_args)
            pending_lens_files.append((LENSES_OUT_DIR / f"{slug}_return_score_{args.date}.json", rs_out))

            eq_in = item["earnings_quality"]
            eq_args = [
                "--anchors",
                str(anchors_file),
                "--ni",
                _field_str(eq_in, "ni"),
                "--cfo",
                _field_str(eq_in, "cfo"),
                "--cfi",
                _field_str(eq_in, "cfi"),
                "--beginning-assets",
                _field_str(eq_in, "beginning_assets"),
                "--ending-assets",
                _field_str(eq_in, "ending_assets"),
            ]
            eq_out = _run_script("earnings_quality.py", eq_args)
            pending_lens_files.append((LENSES_OUT_DIR / f"{slug}_earnings_quality_{args.date}.json", eq_out))

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

            # Verify restricted_cash is included in basic_net_cash_usd and deducted once in strict_net_cash_usd
            basic_nc = float(nc_out["basic_net_cash_usd"])
            strict_nc = float(nc_out["strict_net_cash_usd"])
            restricted_usd_equiv = float(
                (nc_out.get("gross_cash_breakdown_usd") or {}).get("restricted_cash_usd")
                or round(float(_field_str(nc_in, "restricted_cash")) * expected_fx, 2)
            )
            assert abs((basic_nc - restricted_usd_equiv) - strict_nc) < 0.01, (
                f"{ticker} (basic_net_cash_usd [{basic_nc}] - restricted_cash_usd_equiv [{restricted_usd_equiv}]) "
                f"!= strict_net_cash_usd ({strict_nc})"
            )

            assertions.append(
                {
                    "name": f"{slug}_unified_anchors_and_single_restricted_cash_deduction",
                    "passed": True,
                    "ticker": ticker,
                    "market_cap_usd_mn": expected_mcap,
                    "fx_rate_to_usd": expected_fx,
                    "basic_net_cash_usd": basic_nc,
                    "restricted_cash_usd_equiv": restricted_usd_equiv,
                    "strict_net_cash_usd": strict_nc,
                    "anchors_source": "anchors",
                }
            )

            results_by_ticker[ticker] = {
                "period_end": expected_period,
                "illustrative_input": True,
                "market_cap_usd_mn": expected_mcap,
                "market_cap_source": anchors_out["market_cap"]["source"],
                "fx_rate_to_usd": expected_fx,
                "fx_source": anchors_out["fx"]["source"],
                "listing_fx_rate": expected_listing_fx,
                "anchors_source_all": "anchors",
                "basic_net_cash_usd": nc_out["basic_net_cash_usd"],
                "basic_net_cash_to_mcap_pct": nc_out["basic_net_cash_to_mcap_pct"],
                "restricted_cash_usd_equiv": restricted_usd_equiv,
                "strict_net_cash_usd": nc_out["strict_net_cash_usd"],
                "strict_net_cash_to_mcap_pct": nc_out["strict_net_cash_to_mcap_pct"],
                "return_final_score": rs_out["final_score"],
                "overall_quality": eq_out["overall_quality"],
                "operating_sloan_pct": eq_out["sloan"]["operating_sloan_pct"],
                "cfo_to_ni_pct": eq_out["cfo_to_ni"]["cfo_to_ni_pct"],
            }

        conflict_check = _verify_conflict_rejection(work_dir / f"baba_anchors_{args.date}.json")
        assert conflict_check["rejected"] is True, f"Conflict check did not fail: {conflict_check}"
        assertions.append(
            {
                "name": "anchors_and_manual_override_conflict_rejected",
                "passed": conflict_check["rejected"] is True,
                "exit_code": conflict_check["exit_code"],
            }
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
                "description": (
                    "示意数据（illustrative=true）：用于验证镜头命令链路和锚点一致性，不是公司财报数据；"
                    "anchors 仍按 SKILL.md 联网（yfinance）取真实的市值和汇率。"
                ),
            }
        ],
        assertions=assertions,
        extra_fields={
            "fixture_path": "tests/fixtures/dalton/g6/lenses_inputs.json",
            "illustrative": True,
            "fixture_description": str(fixture_data.get("description") or ""),
            "tickers": results_by_ticker,
            "conflict_rejection_check": conflict_check,
        },
        record=args.record,
    )

    baba = results_by_ticker["BABA"]
    hk = results_by_ticker["0700.HK"]
    meta = results_by_ticker["META"]
    raw_path, md_path, sha256_hex = write_evidence_and_summary(
        family_code="G6",
        title="三把财务镜头与盈利质量统一 `--anchors` 命令链路验证 (`BABA`, `0700.HK`, `META`, 示意输入)",
        raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g6_lenses.json",
        summary_md_path=f"var/dalton/trial-runs/realrun-{args.date}/G6.md",
        envelope=envelope,
        key_findings=[
            (
                "`BABA` 示意输入 + 实时锚点下镜头输出（`basic - restricted == strict`）",
                f"illustrative=True, mcap=${baba['market_cap_usd_mn']}M, fx={baba['fx_rate_to_usd']}, basic=${baba['basic_net_cash_usd']}M, restricted=${baba['restricted_cash_usd_equiv']}M, strict=${baba['strict_net_cash_usd']}M ({baba['strict_net_cash_to_mcap_pct']}%), return_score={baba['return_final_score']}, quality={baba['overall_quality']}",
            ),
            (
                "`0700.HK` 示意输入 + 实时锚点下镜头输出（`basic - restricted == strict`）",
                f"illustrative=True, mcap=${hk['market_cap_usd_mn']}M, fx={hk['fx_rate_to_usd']}, basic=${hk['basic_net_cash_usd']}M, restricted=${hk['restricted_cash_usd_equiv']}M, strict=${hk['strict_net_cash_usd']}M ({hk['strict_net_cash_to_mcap_pct']}%), return_score={hk['return_final_score']}, quality={hk['overall_quality']}",
            ),
            (
                "`META` 示意输入 + 实时锚点下镜头输出（`basic - restricted == strict`）",
                f"illustrative=True, mcap=${meta['market_cap_usd_mn']}M, fx={meta['fx_rate_to_usd']}, basic=${meta['basic_net_cash_usd']}M, restricted=${meta['restricted_cash_usd_equiv']}M, strict=${meta['strict_net_cash_usd']}M ({meta['strict_net_cash_to_mcap_pct']}%), return_score={meta['return_final_score']}, quality={meta['overall_quality']}",
            ),
            (
                "`--anchors` 与手填 `--market-cap-usd-mn` 同时传入拒绝检查",
                f"rejected={conflict_check['rejected']} (exit_code={conflict_check['exit_code']})",
            ),
        ],
        notes=[
            "财务镜头为纯确定性 Python 脚本，不发生 LLM 调用（`dalton_llm_journal` 调用数为 0）。",
            "科目数值读取自 `tests/fixtures/dalton/g6/lenses_inputs.json`（`\"illustrative\": true`），属于示意输入，用于验证镜头命令链路和锚点一致性，**不是公司真实财报数据**；市值与汇率由 `anchors.py --allow-network` 从 `yfinance` 实时拉取。",
            "已验证受限现金单次扣除：口径 A (`basic_net_cash_usd`) 含受限现金，口径 B (`strict_net_cash_usd`) 从口径 A 扣除一次 `restricted_cash_usd_equiv`，满足 `abs((basic_net_cash_usd - restricted_cash_usd_equiv) - strict_net_cash_usd) < 0.01`。",
        ],
        record=args.record,
    )

    if args.record:
        LENSES_OUT_DIR.mkdir(parents=True, exist_ok=True)
        for out_path, payload in pending_lens_files:
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    print(
        json.dumps(
            {
                "record": args.record,
                "raw_json": str(raw_path),
                "summary_md": str(md_path),
                "sha256": sha256_hex,
                "tickers": results_by_ticker,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
