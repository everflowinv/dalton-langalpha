# 输出 JSON Schema（`lenses.json`）

本文件定义 `dalton-financial-lenses` 写入 `research/<YYYY-MM-DD>-lenses/lenses.json` 的完整 JSON 结构。所有金额统一以 **USD**（或明确带 `_usd` / `_local` 后缀）表示，三把镜头及盈利质量共用顶层 `anchors` 锚点。

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
| `ticker` | string | ✅ | 股票代码（如 `"META"`） |
| `company` | string | ✅ | 公司全称 |
| `anchors` | object | ✅ | `scripts/anchors.py` 输出的统一市值、汇率、报告期锚点 |
| `data_source` | object | ✅ | 所用主定期报告元数据（`filing_type`、`period_end`、`filing_date`、`accession_number`、`url`） |
| `netcash` | object | ✅ | 第一把镜头：基础净现金（Definition A）、严格净现金（Definition B）、营运资本、现金质量 |
| `hidden_assets` | object | ✅ | 第二把镜头：四类非核心隐含资产明细、折前/折后汇总与重要性评级 |
| `shareholder_return` | object | ✅ | 第三把镜头：7 项必算指标、回购性质、SBC 稀释、`return_score.py` 评分结果 |
| `earnings_quality` | object \| null | ❌ | 盈利质量指标（Sloan Ratio、CFO/NI、CCC），Stage 0 必填 |
| `implied_operating_assets` | object | ✅ | 隐含经营资产（市值 − 净现金 − 折后隐含资产） |

---

## 2. `anchors`（统一锚点，由 `scripts/anchors.py` 产出）

```json
{
  "ticker": "META",
  "company": "Meta Platforms, Inc.",
  "market": "US",
  "exchange": "NASDAQ",
  "period_end": "2025-09-30",
  "listing_currency": "USD",
  "financial_currency": "USD",
  "fx": {
    "pair": "USDUSD",
    "from": "USD",
    "to": "USD",
    "rate": 1.0,
    "as_of": "2026-10-18",
    "source": "identity"
  },
  "market_cap": {
    "value_usd": 1480000000000.0,
    "value_mn_usd": 1480000.0,
    "price_local": 585.0,
    "price_usd": 585.0,
    "shares": 2529914530,
    "as_of": "2026-10-18",
    "source": "sec_cover+yfinance"
  }
}
```

- **港股人民币报表修正**：当 `market == "HK"`（`listing_currency == "HKD"`）而 `financial_currency == "CNY"` 时，`fx.pair` 固定为 `"CNYUSD"`（用于将报表本币 CNY 转换为 USD），同时市值换算使用 `HKDUSD` 将港元市值与股价换算为 `value_usd` 与 `price_usd`。

---

## 3. `netcash`（净现金双口径，由 `scripts/netcash.py` 产出）

```json
{
  "basic": {
    "definition": "Definition A (Basic Net Cash)",
    "cash_side_items": [
      {
        "item": "Cash and cash equivalents",
        "value_local": 43889000000,
        "value_usd": 43889000000,
        "include": true,
        "reason": "资产负债表货币资金（含受限资金）"
      },
      {
        "item": "Marketable securities (short-term debt/MMF)",
        "value_local": 27000000000,
        "value_usd": 27000000000,
        "include": true,
        "reason": "附注确认为高流动性短期国债与货币基金"
      }
    ],
    "cash_side_usd": 70889000000,
    "debt_side_items": [
      {
        "item": "Long-term debt",
        "value_local": 28826000000,
        "value_usd": 28826000000,
        "include": true,
        "reason": "长期优先票据（有息债务）"
      }
    ],
    "debt_side_usd": 28826000000,
    "net_cash_usd": 42063000000,
    "net_cash_to_market_cap_pct": 2.84
  },
  "strict": {
    "definition": "Definition B (Strict Net Cash)",
    "basic_net_cash_usd": 42063000000,
    "deduction_items": [
      {
        "item": "Restricted cash",
        "category": "non_free_cash",
        "value_local": 500000000,
        "value_usd": 500000000,
        "include": true,
        "reason": "受限资金不可自由支配，从严格口径剔除"
      },
      {
        "item": "Deferred revenue (current)",
        "category": "quasi_debt",
        "value_local": 1200000000,
        "value_usd": 1200000000,
        "include": true,
        "reason": "客户预付款/递延收入，具履约义务"
      }
    ],
    "quasi_debt_deductions_usd": 1200000000,
    "non_free_cash_deductions_usd": 500000000,
    "total_deductions_usd": 1700000000,
    "net_cash_usd": 40363000000,
    "net_cash_to_market_cap_pct": 2.73,
    "large_deposit_loan_flag": false,
    "large_deposit_loan_note": "有息债务主要为低息长期美元债，未见境内受限大额定存套利特征",
    "lease_liabilities_usd": 19500000000
  },
  "working_capital": {
    "current_assets_local": 95000000000,
    "current_liabilities_local": 32000000000,
    "current_assets_usd": 95000000000,
    "current_liabilities_usd": 32000000000,
    "working_capital_usd": 63000000000,
    "other_receivable_usd": 3000000000,
    "other_receivable_pct_of_ca": 3.16,
    "other_receivable_warning": false,
    "other_payable_usd": 4500000000,
    "other_payable_pct_of_cl": 14.06,
    "other_payable_warning": false,
    "warnings": [],
    "basis": "As of 2025-09-30 10-Q Consolidated Balance Sheets"
  },
  "cash_quality": "clean"
}
```

---

## 4. `hidden_assets`（隐含资产）

```json
{
  "items": [
    {
      "asset_name": "Non-marketable equity securities & associates",
      "category": "A",
      "asset_type": "associate_jv",
      "book_value_local": 6200000000,
      "book_value_usd": 6200000000,
      "below_half_pct_threshold": true,
      "valuation_basis": "账面值 < 市值 0.5% 阈值（$7.4B），按账面值及非上市联营标准折扣 40% 估算",
      "gross_estimated_value_usd": 6200000000,
      "haircut_pct": 40.0,
      "haircut_rationale": "非上市股权缺乏公开价格发现与流动性",
      "net_estimated_value_usd": 3720000000,
      "net_value_to_market_cap_pct": 0.25,
      "monetization_difficulty": "high",
      "dedup_confirmed": true,
      "note": "未计入净现金端，已与短期有价证券去重"
    }
  ],
  "summary": {
    "total_gross_usd": 6200000000,
    "total_net_usd": 3720000000,
    "total_net_to_market_cap_pct": 0.25,
    "asset_count": 1,
    "half_pct_threshold_usd": 7400000000
  },
  "assessment": "minimal"
}
```

---

## 5. `implied_operating_assets`（隐含经营资产）

```json
{
  "market_cap_usd": 1480000000000,
  "hidden_assets_net_usd": 3720000000,
  "basic": {
    "net_cash_usd": 42063000000,
    "implied_operating_assets_usd": 1434217000000,
    "implied_operating_assets_pct_of_mc": 96.91
  },
  "strict": {
    "net_cash_usd": 40363000000,
    "implied_operating_assets_usd": 1435917000000,
    "implied_operating_assets_pct_of_mc": 97.02
  }
}
```

---

## 6. `shareholder_return`（股东回报与评分，由 `scripts/return_score.py` 产出 `score`）

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
  "sbc_dilution": {
    "FY2022": 1.2,
    "FY2023": 1.1,
    "FY2024": 1.0,
    "basis": "employee-plan shares issued / beginning share count (%)"
  },
  "latest_buyback_execution": {
    "remaining_authorization_usd": 50000000000,
    "latest_quarter_buyback_usd": 8800000000,
    "pace_note": "3Q25 单季执行 88 亿美元"
  },
  "special_dividends": {
    "history": "FY2022-FY2024 无特别股息",
    "forward_treatment": "记 0（未连续两年发放）"
  },
  "management_policy_zh": "管理层维持季度常规现金分红（每股 $0.50），并在保证 AI 资本开支后将剩余自由现金流用于股票回购（来源：FY2024 10-K Note 14）",
  "score": {
    "inputs": {
      "ttm_div_pct": 0.35,
      "ttm_buyback_pct": 2.95,
      "true_return_share": 0.65,
      "annual_total_return_pct": [1.89, 1.35, 2.36],
      "fwd_total_pct": 3.06,
      "credibility_pct": 90,
      "credibility_reason": "明确比例或金额，且当前已有执行"
    },
    "R1": 2.27,
    "R2": 1.87,
    "R3": 2.75,
    "R": 2.75,
    "driver": "R3",
    "base_score": 2.75,
    "adjustment": 0.5,
    "adjustment_clamped": false,
    "adjustments": [
      {"delta": 0.5, "reason": "季度常规股息启动并连续派发"}
    ],
    "final_score": 3.3,
    "max_score": 20,
    "rationale_zh": "R1 = 0.35% + 2.95% × 65% = 2.27%；R2 = (1.89% + 1.35% + 2.36%) / 3 = 1.87%；R3 = 3.06% × 可信度 90%（明确比例或金额，且当前已有执行）= 2.75%；R = max = 2.75%（R3 驱动）；基础分 2.75；调整 +0.5 分（+0.5：季度常规股息启动并连续派发）；最终 3.3 / 20。"
  }
}
```

---

## 7. `earnings_quality`（盈利质量，由 `scripts/earnings_quality.py` 产出）

```json
{
  "period": "FY2025",
  "average_total_assets": 250000000000.0,
  "sloan_ratio": {
    "total_accruals_ratio": -0.042,
    "total_accruals_pct": -4.2,
    "operating_accruals_ratio": -0.118,
    "operating_accruals_pct": -11.8,
    "primary_formula": "(NI - CFO - CFI) / Avg Total Assets",
    "flag": "normal"
  },
  "cfo_to_ni": {
    "ratio": 1.46,
    "pct": 146.0,
    "flag": "healthy"
  },
  "ccc": {
    "dio_days": 0.0,
    "dso_days": 34.5,
    "dpo_days": 18.2,
    "ccc_days": 16.3,
    "applicable": true
  }
}
```
- `facts.md`（FACTS-LEDGER）对应键：`qoe_sloan`、`qoe_cfo_ni`、`qoe_ccc`。
