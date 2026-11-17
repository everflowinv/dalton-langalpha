# G6.2 财务镜头脚本命令链路实跑记录 (统一锚点与受限现金单次扣除)

- **生成方式**：由 `python -m scripts.dalton.realrun._common --render-docs` 从 `var/dalton/trial-runs/` 下的 G6 实跑证据 JSON 直接渲染生成
- **对应指南**：`P0J-round4-fixes.md` §5.7 (`G6`)、§8.1 第 17 条、`P0I-round3-fixes.md` §6 (`I6`)
- **输入说明（示意数据，`illustrative: true`）**：科目数值取自 `tests/fixtures/dalton/g6/lenses_inputs.json`，属于示意输入，用于验证镜头命令链路、跨镜头锚点一致性与受限现金单次扣除逻辑，**不是公司真实财报数据**；市值与汇率由 `anchors.py --allow-network` 实时从 `yfinance` 拉取。本文档仅记录**同一组示意输入与实时锚点下，Dalton 四个财务镜头脚本（`anchors.py`、`netcash.py`、`return_score.py`、`earnings_quality.py`）的实际命令输出与口径校验结果**
- **真实运行产物路径（`--record` 模式写入 `var/dalton/lenses/`）**：
  - `var/dalton/lenses/baba_anchors_2026-10-21.json`
  - `var/dalton/lenses/baba_netcash_2026-10-21.json`
  - `var/dalton/lenses/baba_return_score_2026-10-21.json`
  - `var/dalton/lenses/baba_earnings_quality_2026-10-21.json`
  - `var/dalton/lenses/0700hk_anchors_2026-10-21.json`
  - `var/dalton/lenses/0700hk_netcash_2026-10-21.json`
  - `var/dalton/lenses/0700hk_return_score_2026-10-21.json`
  - `var/dalton/lenses/0700hk_earnings_quality_2026-10-21.json`
  - `var/dalton/lenses/meta_anchors_2026-10-21.json`
  - `var/dalton/lenses/meta_netcash_2026-10-21.json`
  - `var/dalton/lenses/meta_return_score_2026-10-21.json`
  - `var/dalton/lenses/meta_earnings_quality_2026-10-21.json`

---

## 1. 同一组示意输入 + 实时 `anchors.py` 锚点下的镜头输出汇总（`BABA`、`0700.HK`、`META`）

三组标的先由 `anchors.py --allow-network` 实时拉取市值与汇率生成锚点 JSON，再将 `--anchors <anchors.json>` 与 `tests/fixtures/dalton/g6/lenses_inputs.json` 的示意输入统一传给 `netcash.py`、`return_score.py`、`earnings_quality.py`（所有镜头输出均带 `"anchors_source": "anchors"` 且 `market_cap_usd_mn`、`fx_rate_to_usd`、`period_end` 完全一致；口径 A `basic_net_cash_usd` 包含受限现金，口径 B `strict_net_cash_usd` 从口径 A 扣除一次受限现金 `restricted_cash_usd_equiv`，满足 `(basic_net_cash_usd - restricted_cash_usd_equiv) == strict_net_cash_usd`；注：表中现金、负债、利润与现金流等输入均为示意数据，非公司真实财务数据）：

| 标的 | 交易币种 | 报表币种 (`--reporting-currency`) | 锁定汇率 (`fx_rate_to_usd`) | 锁定市值 (`market_cap_usd_mn`) | 口径 A 基础净现金 (`basic_net_cash_usd`, 示意输入) | 受限现金折算 (`restricted_cash_usd_equiv`, 示意输入) | 口径 B 严格净现金 (`strict_net_cash_usd`, 示意输入) | 隐含经营资产 (Strict, 示意输入) | 股东回报评分 (`final_score`, 示意输入) | 盈利质量 (`overall_quality` / Operating Sloan, 示意输入) |
|---|---|---|---|---|---|---|---|---|---|---|
| **BABA** | `USD` (ADR 1:8) | `CNY` | `CNYUSD=X = 0.150053` | `$270,193.98M` | `$64,342.73M` (23.81%) | `$6,182.18M` | `$58,160.55M` (21.53%) | `$212,033.43M` (78.47%) | `5.6 / 20` | `strong` (`-1.9%`, `CFO/NI=126.29%`) |
| **0700.HK** | `HKD` | `CNY` | `CNYUSD=X = 0.150053`, `HKDUSD=X = 0.127438` | `$451,919.29M` | `$10,938.86M` (2.42%) | `$510.18M` | `$10,428.68M` (2.31%) | `$441,490.61M` (97.69%) | `3.3 / 20` | `strong` (`-3.9%`, `CFO/NI=133.2%`) |
| **META** | `USD` | `USD` | `1.0` | `$1,768,606.14M` | `$49,229.00M` (2.78%) | `$120.00M` | `$49,109.00M` (2.78%) | `$1,719,497.14M` (97.22%) | `2.2 / 20` | `strong` (`-11.46%`, `CFO/NI=146.45%`) |
