---
name: portfolio-diagnosis
description: "Health check of the user's own portfolio against their investing profile: concentration, exposure by sector, region and asset class, cash, currency mix, and risk fit (volatility, beta, drawdown), ending in a few next steps. Triggers on diagnose my portfolio, portfolio review, portfolio health check, am I too concentrated, how risky is my portfolio."
---

# Portfolio Diagnosis

A one-minute read of what the user holds, measured against what they told you they want. Every finding carries a number and what that number is compared with. No lecture and no trade calls.

## 1. Inputs

- `.agents/user/profile/portfolio.json`: the holdings. When this turn already read it, use that copy.
- The `## Investment Profile` block in your system prompt: its Risk level, Investor type, Experience, Markets, Wants to trade, Analysis approaches, Investing styles, Holding horizon and Sectors lines. A line may hold an older profile's free text: read what is there. When the block has no line for what you need, an older profile may keep it in a free-text key of `.agents/user/profile/preference.json`, such as `risk_tolerance` or `holding_period`. With no risk level at all, report the risk numbers without a fit verdict.
- A brokerage in `<mcp-servers>`: take positions and cash balances from it, unless this turn already fetched them. Its quantities and market values win over the file. If they differ from `portfolio.json`, say so in one line and offer to re-import. Don't write the file here.
- No holdings: say the diagnosis needs them and ask for a few positions as ticker and quantity.

### 截图输入

持仓来自截图或图片表格时，按下面在工作区文件中保存和核对。券商已经返回的数量和市值仍以券商为准；下面禁止的是为了凑市值或合计去改读数。<!-- DALTON-HOOK(dense-image-reading): a gap is an exception list, never a rewritten reading -->

- 原始读数和计算字段分开保存。原始读数是标的、数量、价格、金额（或市值）、合计和单位；权重、希腊值和其他计算字段另存，不写回原始读数。
- 数量、价格、金额（或市值）及合计分别复读。转录文件中每个单元格或每行标“已复读一致”“尚未复核”或“无法辨认”。
- 加总或比例对不上时，只记录异常清单并写明差额。禁止反向修改数量、价格、金额（或市值）及合计去消除差额。
- 跨日数量变化必须与用户申报或交易记录对应。对不上就列入异常清单，不改写任何一天的数量。
- 作为子代理向父代理返回结果时，附上异常清单，以及数量、价格、金额（或市值）里影响结论的关键变更；直接向用户输出诊断时，在 Data 行用自然语言说明差额与无法辨认项，不展示内部核对状态标签。

## 2. Compute, in one `execute_code` call

```python
from tools.yf_price import get_multiple_stocks_history   # 1y daily bars, many symbols
from tools.yf_fundamentals import get_company_info       # sector, country, quoteType, category, currency
from tools.price_data import get_stock_data, get_asset_data  # fallback bars; crypto and forex
```

- **Prices**: one `get_multiple_stocks_history(symbols, period="1y")` holding every position, the benchmark, and any currency pairs, with crypto as `BTC-USD` and pairs as `HKDUSD=X`. It keys results by canonical symbol (`600519.SH`, `HKD-USD`), not always your spelling. Retry what lands in `errors` with `get_stock_data`, or `get_asset_data("BTCUSD", "crypto")` and `get_asset_data("HKDUSD", "forex")`. When a server is missing from `<mcp-servers>`, use the other.
- **Benchmark**: the one for the market holding the most weight. US `SPY`, Hong Kong `2800.HK`, mainland China `510300.SH`, Japan `1306.T`, Europe `EXSA.DE`.
- **Base currency**: the currency most of the value is in. Convert every other series at the pair's daily close, forward-filled, inverting the reverse pair when the direct one fails.
- **Value and weights**: quantity times the latest close, or the broker's market value. Cash counts toward the total.
- **Concentration**: the largest position's weight, the top five's combined weight, and effective N (1 / Σw² over invested positions). A broad index ETF is diversified on its own, so judge single-name risk without it.
- **Exposure**:
  - Sector: from `sector`, or for an ETF its `category`.
  - Region: from the listing. No suffix is `us`, `.HK` is `hk`, `.SH`/`.SZ` is `cn`, `.T` is `jp`, European exchanges are `eu`. Note it when a holding lists in one market and does business mainly in another.
  - Asset class: from `instrument_type`.
  - Compare each with the stated Markets, Wants to trade and Sectors: the weight outside them, and any stated one with nothing in it. Read Wants to trade against only the values `instrument_type` shares with it (`stock`, `etf`, `option`, `crypto`, `bond`, `fund`), and skip that comparison when none of those is stated.
- **Cash share**, and the **currency mix** when more than one currency is held.
- **Risk**: replay today's weights over the past year on the benchmark's trading days, forward-filling a holding across its own market's holidays. From that series, take annualized volatility (daily standard deviation × √252), beta to the benchmark, and the one-year maximum drawdown. Holdings with under 60 days of history stay out of the risk figures.

Print only the figures the findings need, never the series. Save them, with the as-of date and the gaps, to `portfolio-diagnosis/<YYYY-MM-DD>.json` in a task directory. When an earlier run is there, lead with what changed since.

## 3. Judge the fit

Rough bands, not rules:

| Risk level | Annualized volatility | Drawdown it can sit through |
|---|---|---|
| conservative | under 10% | about 10% |
| moderate | 10 to 15% | about 20% |
| growth | 15 to 22% | about 30% |
| aggressive | over 22% | 40% or more |

Read drawdown as the larger of the one-year figure and twice the volatility, since one calm year understates a bad one.

Worth a finding: a position over about 20% (a broad index ETF aside); effective N under about 8; one sector over about 40%; more than about 10% outside the stated markets or asset classes; volatility or drawdown past the band; beta far from 1 without the profile asking for it; cash over about 20%. When Investing styles includes `income`, add the weighted dividend yield.

## 4. Write it

Pitch the wording to Investor type and Experience: figures without definitions for `professional` or `over_10y`, and each term explained once for `student` or `under_1y`.

- **Headline**: one sentence, the most important thing, with its number.
- **Findings**: three to five bullets, most important first. Each has a number and its comparison: the profile, the benchmark, or a band above. One finding may say what is fine, when it reassures.
- **Data**: one line with prices as of a date, the benchmark, and the base currency. Name what was left out and why: no price, an option with no broker value, under 60 days of history. Never fill a gap with a guess.
- **Next**: two or three follow-up tasks, offered and named concretely. For example: "Stress-test NVDA, 31% of the portfolio, against a 40% drawdown." Or: "Find lower-correlation names in Hong Kong to offset the tech weight." Fit them to the stated Investing styles, Analysis approaches and Holding horizon. Offer tasks, never trades. When the user picks one, handle it as a new request.

About 200 words, readable in a minute. At most one small table.
