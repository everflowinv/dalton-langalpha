# G6.2 财务镜头脚本真实运行与基金原脚本对比记录 (2026-10-19 统一锚点重算)

- **运行日期**：2026-10-19（修正 2026-10-18 手填锚点不一致问题，统一传入 `--anchors`）
- **对应指南**：`P0I-round3-fixes.md` §6 (`I6`)、`P0H-round2-fixes.md` §6 (`G6`)
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

## 1. 按 `SKILL.md` 统一 `--anchors` 命令真实运行汇总（`BABA`、`0700.HK`、`META`）

三组标的先由 `anchors.py` 生成锚点 JSON，再将 `--anchors <anchors.json>` 统一传给 `netcash.py`、`return_score.py`、`earnings_quality.py`（所有镜头输出均带 `"anchors_source": "anchors"` 且 `market_cap_usd_mn`、`fx_rate_to_usd`、`period_end` 完全一致）：

| 标的 | 交易币种 | 财报币种 (`--reporting-currency`) | 锁定汇率 (`fx_rate_to_usd`) | 锁定市值 (`market_cap_usd_mn`) | 口径 A 基础净现金 (`basic_net_cash_usd`) | 口径 B 严格净现金 (`strict_net_cash_usd`) | 隐含经营资产 (Strict) | 股东回报评分 (`final_score`) | 盈利质量 (`overall_quality` / Operating Sloan) |
|---|---|---|---|---|---|---|---|---|---|
| **BABA** | `USD` (ADR 1:8) | `CNY` | `CNYUSD=X = 0.149772` | `$271,437.86M` | `$58,062.27M` (21.39%) | `$51,891.66M` (19.12%，扣受限现金 `41,200M CNY = $6,170.61M`) | `$219,546.20M` (80.88%) | `6.5 / 20` (`R1=6.53%`) | `strong` (`-1.90%`, `CFO/NI=126.29%`) |
| **0700.HK** | `HKD` | `CNY` | `CNYUSD=X = 0.149772`, `HKDUSD=X = 0.127437` | `$479,458.89M` | `$10,409.15M` (2.17%) | `$9,899.93M` (2.06%，扣受限现金 `3,400M CNY = $509.22M`) | `$469,558.96M` (97.94%) | `3.7 / 20` (`R1=3.74%`) | `strong` (`-3.90%`, `CFO/NI=133.20%`) |
| **META** | `USD` | `USD` | `1.0` | `$1,807,009.84M` | `$49,109.00M` (2.72%) | `$48,989.00M` (2.71%，扣受限现金 `$120.00M`) | `$1,758,020.84M` (97.29%) | `2.3 / 20` (`R2=2.15%`) | `strong` (`-11.46%`, `CFO/NI=146.45%`) |

---

## 2. 与基金原脚本（OpenClaw `company-deepdive-mx-0911`）结果对比

| 维度 | 基金原脚本 (`~/.openclaw/agents/main/agent/workshop-skills/company-deepdive-mx-0911/`) | 本仓 `dalton-financial-lenses/scripts/` (`P0I.I6` 统一锚点后) | 对比结论 |
|---|---|---|---|
| **跨镜头市值与汇率锚点一致性 (`--anchors`)** | 各脚本分别手填市值和汇率，易出现 `anchors` 与 `netcash` 混用不同市值/整数汇率 | `netcash.py`、`return_score.py`、`earnings_quality.py` 统一通过 `--anchors <json>` 读取 `market_cap_usd_mn`、`fx.rate`、`period_end`；同时传 `--anchors` 与手填 `--market-cap-usd-mn`/`--fx-rate` 直接报错退出（仅手填时标 `anchors_source: "manual"`） | 从参数结构上杜绝手填市值/汇率与 `anchors.py` 输出不一致的问题 |
| **ADR / 港股财报汇率换算** | 未强制要求显式传入 `--reporting-currency`，离线时回退到写死的静态汇率表 | 非美股与 ADR 强制校验 `--reporting-currency CNY`，汇率仅来自 `--fx-rate` 或 `--allow-network` 实时 `yfinance` 获取（删除静态兜底表） | `BABA` 与 `0700.HK` 人民币报表均按 `anchors` 锁定的 `CNYUSD=X = 0.149772` 统一换算 |
| **受限现金扣除逻辑 (`netcash.py`)** | 传入自定义 `trapped_cash_items` 时偶发漏扣 `restricted_cash` | 始终默认扣除 `restricted_cash`（除非显式声明 `include_restricted_cash_in_free=True` 并附 `reason`），且每个扣除项均保留 `value_local` 与 `value_usd` | 数学公式完全对齐口径 A/B，且修复了自定义非自由现金覆盖受限现金的漏洞 |
| **市值字段与单位 (`market_cap_usd_mn`)** | 混用 `market_cap_usd`（百万美元 vs 美元原值） | 统一使用 `market_cap_usd_mn`（百万美元），同时保留 `value_usd` 原值并与 `references/output-schema.md` 100% 对齐 | 消除 100 万倍单位歧义 |
| **盈利质量期初/期末资产与 CCC (`earnings_quality.py`)** | 需手工预计算平均总资产；缺少分项时将缺失分项按 `0` 求和算出伪造的 `ccc_days` | 支持直接传 `--beginning-assets` 与 `--ending-assets` 自动求均值；`dso`/`dio`/`dpo` 任一分项缺失时 `ccc_days` 返回 `null` | Sloan 应计比率与 CFO/NI 计算结果与原脚本一致，缺失分项时诚实返回 `null` |
