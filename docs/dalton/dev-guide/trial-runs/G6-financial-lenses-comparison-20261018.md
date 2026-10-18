# G6.2 财务镜头脚本真实运行与基金原脚本对比记录 (2026-10-18)

- **运行日期**：2026-10-18
- **对应指南**：`P0G-review-fixes.md` §7.2 (`G6.2`)、`P0H-round2-fixes.md` §6 (`G6`)
- **真实运行产物路径**：
  - `var/dalton/lenses/baba_anchors_20261018.json`
  - `var/dalton/lenses/baba_netcash_20261018.json`
  - `var/dalton/lenses/baba_return_score_20261018.json`
  - `var/dalton/lenses/baba_earnings_quality_20261018.json`
  - `var/dalton/lenses/0700hk_anchors_20261018.json`
  - `var/dalton/lenses/0700hk_netcash_20261018.json`
  - `var/dalton/lenses/0700hk_return_score_20261018.json`
  - `var/dalton/lenses/0700hk_earnings_quality_20261018.json`
  - `var/dalton/lenses/meta_anchors_20261018.json`
  - `var/dalton/lenses/meta_netcash_20261018.json`
  - `var/dalton/lenses/meta_return_score_20261018.json`
  - `var/dalton/lenses/meta_earnings_quality_20261018.json`

---

## 1. 按 `SKILL.md` 命令真实运行汇总（`BABA`、`0700.HK`、`META`）

| 标的 | 交易币种 | 财报币种 (`--reporting-currency`) | 实时汇率 (`yfinance`) | 市值 (`market_cap_usd_mn`) | 口径 A 基础净现金 (`basic_net_cash_usd`) | 口径 B 严格净现金 (`strict_net_cash_usd`) | 隐含经营资产 (Strict) |
|---|---|---|---|---|---|---|---|
| **BABA** | `USD` (ADR 1:8) | `CNY` | `CNYUSD=X = 0.149772` | `$271,437.86M` | `$54,273.94M` (22.15%) | `$48,505.94M` (19.80%，扣受限现金 `41,200M CNY = $5,768.00M`) | `$196,494.06M` |
| **0700.HK** | `HKD` | `CNY` | `CNYUSD=X = 0.149772`, `HKDUSD=X = 0.127437` | `$479,458.89M` | `$18,522.00M` (3.86%) | `$15,932.00M` (3.32%，扣受限现金 `18,500M CNY = $2,590.00M`) | `$463,526.89M` |
| **META** | `USD` | `USD` | `1.0` | `$1,807,009.84M` | `$28,826.00M` (1.60%) | `$28,826.00M` (1.60%) | `$1,778,183.84M` |

---

## 2. 与基金原脚本（OpenClaw `company-deepdive-mx-0911`）结果对比

| 维度 | 基金原脚本 (`~/.openclaw/agents/main/agent/workshop-skills/company-deepdive-mx-0911/`) | 本仓 `dalton-financial-lenses/scripts/` (`G6.2` 修复后) | 对比结论 |
|---|---|---|---|
| **ADR / 港股财报汇率换算** | 未强制要求显式传入 `--reporting-currency`，离线时回退到写死的静态汇率表 | 非美股与 ADR 强制校验 `--reporting-currency CNY`，汇率仅来自 `--fx-rate` 或 `--allow-network` 实时 `yfinance` 获取（删除静态兜底表） | `BABA` 与 `0700.HK` 人民币报表均按实时 `CNY->USD` 准确换算，消除币种错配与过期汇率风险 |
| **受限现金扣除逻辑 (`netcash.py`)** | 传入自定义 `trapped_cash_items` 时偶发漏扣 `restricted_cash` | 始终默认扣除 `restricted_cash`（除非显式声明 `include_restricted_cash_in_free=True` 并附 `reason`），且每个扣除项均保留 `value_local` 与 `value_usd` | 数学公式完全对齐口径 A/B，且修复了自定义非自由现金覆盖受限现金的漏洞 |
| **市值字段与单位 (`market_cap_usd_mn`)** | 混用 `market_cap_usd`（百万美元 vs 美元原值） | 统一使用 `market_cap_usd_mn`（百万美元），同时保留 `value_usd` 原值并与 `references/output-schema.md` 100% 对齐 | 消除 100 万倍单位歧义 |
| **盈利质量期初/期末资产与 CCC (`earnings_quality.py`)** | 需手工预计算平均总资产；缺少分项时将缺失分项按 `0` 求和算出伪造的 `ccc_days` | 支持直接传 `--beginning-assets` 与 `--ending-assets` 自动求均值；`dso`/`dio`/`dpo` 任一分项缺失时 `ccc_days` 返回 `null` | Sloan 应计比率与 CFO/NI 计算结果与原脚本一致，缺失分项时诚实返回 `null` |
