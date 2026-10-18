# 输出 JSON Schema（`lenses.json`）

本文件定义 `dalton-financial-lenses` 写入 `research/<YYYY-MM-DD>-lenses/lenses.json` 的完整 JSON 结构，所有脚本子块直接对应 `scripts/{anchors,netcash,return_score,earnings_quality}.py` 的真实输出字段。市值统一以百万美元 `market_cap_usd_mn` 为主键（同时提供美元原值 `value_usd` 及兼容别名 `market_cap_usd`），三把镜头及盈利质量共用顶层 `anchors` 锚点。

---

## 1. 顶层结构（Top-Level Object）

```json
{
  "ok": true,
  "ticker": "META",
  "company": "Meta Platforms, Inc.",
  "anchors": { ... },
  "data_source": { ... },
  "netcash": { ... },
  "hidden_assets": { ... },
  "shareholder_return": { ... },
  "earnings_quality": { ... },
  "implied_operating_assets": { ... }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ok` | boolean | ✅ | 计算是否成功 |
| `ticker` | string | ✅ | 股票代码（如 `"META"`、`"BABA"`、`"0700.HK"`） |
| `company` | string | ✅ | 公司全称 |
| `anchors` | object | ✅ | `scripts/anchors.py` 输出的统一市值、汇率、报告期锚点 |
| `data_source` | object | ✅ | 所用主定期报告元数据（`filing_type`、`period_end`、`filing_date`、`accession_number`、`url`） |
| `netcash` | object | ✅ | 第一把镜头：`scripts/netcash.py` 输出的基础净现金（Definition A）、严格净现金（Definition B）、营运资本、现金质量与隐含经营资产 |
| `hidden_assets` | object | ✅ | 第二把镜头：四类非核心隐含资产明细、折前/折后汇总与重要性评级 |
| `shareholder_return` | object | ✅ | 第三把镜头：7 项必算指标、回购性质、SBC 稀释、`scripts/return_score.py` 评分结果 |
| `earnings_quality` | object \| null | ❌ | `scripts/earnings_quality.py` 输出的盈利质量指标（Sloan Ratio、CFO/NI、CCC），Stage 0 必填 |
| `implied_operating_assets` | object | ✅ | 隐含经营资产（可引用 `netcash.implied_operating_assets`） |

---

## 2. `anchors`（统一锚点，由 `scripts/anchors.py` 真实产出）

```json
{
  "ticker": "0700.HK",
  "company": "Tencent Holdings Limited",
  "market": "HK",
  "period_end": "2025-09-30",
  "as_of": "2026-10-18",
  "listing_currency": "HKD",
  "financial_currency": "CNY",
  "reporting_currency": "CNY",
  "fx": {
    "pair": "CNYUSD=X",
    "from": "CNY",
    "to": "USD",
    "from_currency": "CNY",
    "to_currency": "USD",
    "rate": 0.149772,
    "as_of": "2026-10-18",
    "source": "yfinance (CNYUSD=X)"
  },
  "listing_fx": {
    "pair": "HKDUSD=X",
    "from": "HKD",
    "to": "USD",
    "from_currency": "HKD",
    "to_currency": "USD",
    "rate": 0.127427,
    "as_of": "2026-10-18",
    "source": "yfinance (HKDUSD=X)"
  },
  "market_cap": {
    "market_cap_usd_mn": 479422.23,
    "market_cap_usd": 479422.23,
    "value_mn_usd": 479422.23,
    "value_usd": 479422230000.0,
    "price_local": 417.8,
    "price_usd": 53.2392,
    "shares_mn": 9005.0604,
    "shares": 9005060400,
    "shares_unit": "auto",
    "adr_ratio": 1.0,
    "as_of": "2026-10-18",
    "source": "yfinance marketCap * listing_fx_rate"
  }
}
```

- **双汇率与单位纪律**：
  - `fx` 恒为 `financial_currency -> USD`（用于资产负债表、利润表、现金流表本币转美元）；
  - `listing_fx` 恒为 `listing_currency -> USD`（用于交易币种股价与市值转美元）；
  - 非美元币种严禁使用写死的旧汇率，必须传 `--fx-rate` 或 `--allow-network`；
  - `market_cap_usd_mn` 为百万美元（USD millions），`value_usd` 为美元原值（USD）。

---

## 3. `netcash`（净现金双口径，由 `scripts/netcash.py` 真实产出）

```json
{
  "market_cap_usd_mn": 1480000.0,
  "gross_cash_usd": 70889.0,
  "gross_cash_breakdown_usd": {
    "cash_and_equivalents_usd": 43389.0,
    "restricted_cash_usd": 500.0,
    "time_deposits_usd": 0.0,
    "short_term_investments_usd": 27000.0
  },
  "interest_bearing_debt_usd": 28826.0,
  "debt_breakdown_usd": {
    "short_term_debt_usd": 0.0,
    "current_long_term_debt_usd": 0.0,
    "long_term_debt_usd": 28826.0,
    "convertible_debt_usd": 0.0
  },
  "lease_liabilities_usd": 19500.0,
  "basic_net_cash_usd": 42063.0,
  "basic_net_cash_to_mcap_pct": 2.84,
  "quasi_debt_deductions_usd": 1200.0,
  "quasi_debt_items": [
    {
      "item": "deferred_revenue",
      "value_local": 1200.0,
      "value_usd": 1200.0,
      "amount_usd": 1200.0,
      "include": true,
      "reason": "客户预付款/递延收入，具履约义务"
    }
  ],
  "non_free_cash_deductions_usd": 500.0,
  "non_free_cash_items": [
    {
      "item": "restricted_cash",
      "value_local": 500.0,
      "value_usd": 500.0,
      "amount_usd": 500.0,
      "include": true,
      "reason": "Default strict deduction of restricted/pledged cash"
    }
  ],
  "strict_net_cash_usd": 40363.0,
  "strict_net_cash_to_mcap_pct": 2.73,
  "basic": {
    "definition": "Definition A (Basic Net Cash)",
    "cash_side_usd": 70889.0,
    "debt_side_usd": 28826.0,
    "net_cash_usd": 42063.0,
    "net_cash_to_market_cap_pct": 2.84
  },
  "strict": {
    "definition": "Definition B (Strict Net Cash)",
    "basic_net_cash_usd": 42063.0,
    "quasi_debt_deductions_usd": 1200.0,
    "non_free_cash_deductions_usd": 500.0,
    "total_deductions_usd": 1700.0,
    "net_cash_usd": 40363.0,
    "net_cash_to_market_cap_pct": 2.73,
    "large_deposit_loan_flag": false,
    "lease_liabilities_usd": 19500.0
  },
  "hidden_assets": {
    "gross_usd": 6200.0,
    "after_haircut_usd": 3720.0,
    "after_haircut_to_mcap_pct": 0.25
  },
  "implied_operating_assets": {
    "basic_usd": 1434217.0,
    "basic_to_mcap_pct": 96.91,
    "strict_usd": 1435917.0,
    "strict_to_mcap_pct": 97.02,
    "negative_ev_basic": false,
    "negative_ev_strict": false
  },
  "working_capital": {
    "working_capital_usd": 63000.0,
    "other_payable_to_current_liabilities": 0.1406,
    "other_receivable_to_current_assets": 0.0316
  },
  "large_deposit_loan_flag": false,
  "cash_quality": "moderate",
  "warnings": []
}
```

- 每个扣减项（`quasi_debt_items`、`non_free_cash_items`）均保留 `item`、`value_local`、`value_usd`（及兼容字段 `amount_usd`）、`include`、`reason`。
- 受限资金（`restricted_cash`）在严格口径（Definition B）默认自动扣减，即使同时传入了其他 `non_free_cash_items` 也不会遗漏；仅当显式声明 `include_restricted_cash_in_free=True` 并附 `restricted_cash_free_reason` 时才不扣减。

---

## 4. `hidden_assets`（隐含资产）

```json
{
  "items": [
    {
      "asset_name": "Non-marketable equity securities & associates",
      "category": "A",
      "asset_type": "associate_jv",
      "book_value_local": 6200.0,
      "book_value_usd": 6200.0,
      "below_half_pct_threshold": true,
      "valuation_basis": "账面值 < 市值 0.5% 阈值（$7,400M），按账面值及非上市联营标准折扣 40% 估算",
      "gross_estimated_value_usd": 6200.0,
      "haircut_pct": 40.0,
      "haircut_rationale": "非上市股权缺乏公开价格发现与流动性",
      "net_estimated_value_usd": 3720.0,
      "net_value_to_market_cap_pct": 0.25,
      "monetization_difficulty": "high",
      "dedup_confirmed": true,
      "note": "未计入净现金端，已与短期有价证券去重"
    }
  ],
  "summary": {
    "total_gross_usd": 6200.0,
    "total_net_usd": 3720.0,
    "total_net_to_market_cap_pct": 0.25,
    "asset_count": 1,
    "half_pct_threshold_usd": 7400.0
  },
  "assessment": "minimal"
}
```

---

## 5. `shareholder_return`（股东回报与评分，`score` 由 `scripts/return_score.py` 真实产出）

```json
{
  "metrics_7": {
    "ttm_dividend_yield_pct": 0.35,
    "ttm_buyback_yield_pct": 2.95,
    "ttm_total_return_pct": 3.30,
    "annual_total_return_pct": {
      "FY2022": 1.89,
      "FY2023": 1.35,
      "FY2024": 2.36,
      "three_year_avg_pct": 1.87
    },
    "fwd_dividend_yield_pct": 0.36,
    "fwd_buyback_yield_pct": 2.70,
    "fwd_total_return_pct": 3.06
  },
  "buyback_type_primary": "mixed",
  "true_return_share": 0.65,
  "true_return_share_basis": "share_count",
  "score": {
    "inputs": {
      "ttm_div_pct": 0.35,
      "ttm_buyback_pct": 2.95,
      "true_return_share": 0.65,
      "annual_3y_pct": [1.89, 1.35, 2.36],
      "annual_total_return_pct": [1.89, 1.35, 2.36],
      "fwd_total_pct": 3.06,
      "credibility_pct": 90,
      "credibility_reason": "管理层明确比例或金额，且当前已有执行"
    },
    "R1": 2.27,
    "R2": 1.87,
    "R3": 2.75,
    "R": 2.75,
    "driver": "R3",
    "base_score": 2.75,
    "adjustment": 0.5,
    "adjustment_raw": 0.5,
    "adjustment_clamped": false,
    "adjustments": [
      {
        "delta": 0.5,
        "reason": "季度常规股息启动并连续派发"
      }
    ],
    "final_score": 3.3,
    "max_score": 20,
    "rationale_zh": "R1(TTM 净回报率)=2.27%，R2(三年均值)=1.87%，R3(前瞻折算，可信度 90%：管理层明确比例或金额，且当前已有执行)=2.75%；取最高 R3=2.75% 对应基础分 2.75；可持续性调整 +0.5（+0.5（季度常规股息启动并连续派发））；最终得分 3.3/20。"
  }
}
```

---

## 6. `earnings_quality`（盈利质量，由 `scripts/earnings_quality.py` 真实产出）

```json
{
  "average_total_assets": 250000.0,
  "sloan": {
    "operating_accruals": -29500.0,
    "operating_sloan_ratio": -0.118,
    "operating_sloan_pct": -11.8,
    "total_accruals": -10500.0,
    "total_sloan_ratio": -0.042,
    "total_sloan_pct": -4.2,
    "flag_gt_10pct": false,
    "signal": "conservative_cash_exceeds_earnings"
  },
  "cfo_to_ni": {
    "cfo": 91300.0,
    "net_income": 62360.0,
    "cfo_to_ni_ratio": 1.4641,
    "cfo_to_ni_pct": 146.41,
    "flag_weak_conversion": false,
    "signal": "strong_cash_conversion"
  },
  "ccc": {
    "dio_days": null,
    "dso_days": 34.5,
    "dpo_days": 18.2,
    "ccc_days": null,
    "applicable": false,
    "prior_ccc_days": null,
    "yoy_change_days": null,
    "flag_working_capital_deterioration": false
  },
  "overall_quality": "strong",
  "warnings": []
}
```
- 当 `dio`、`dso`、`dpo` 中任一分项缺失且未显式传入 `0` 时，`ccc_days` 返回 `null`（`applicable: false`），绝不把缺失分项当作 `0.0` 编造部分和。
- `facts.md`（FACTS-LEDGER）对应键：`qoe_sloan`、`qoe_cfo_ni`、`qoe_ccc`。
