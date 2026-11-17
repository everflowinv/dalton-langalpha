---
name: "findata-analyst"
description: "SEC 财报与历史披露、FRED/ALFRED 宏观序列、财政部 TGA/债务；附来源、日期与单位。"
user-invocable: true
disable-model-invocation: true
---

# Financial Data Analyst Skill v2

⚠️ **CRITICAL: NO HALLUCINATIONS** — Every number must come from command output. If the tool doesn't return data, say so.
⚠️ **CRITICAL: EXECUTION RULE** — Always run through `bash skills/findata-analyst/run.sh ...`. Do NOT write ad-hoc Python.

## Setup
```bash
export EDGAR_IDENTITY="Your Name <your.email@example.com>"  # Required by SEC
```
First run auto-creates venv and installs dependencies.

## Dependencies

### Required
- **edgartools** (auto-installed via `requirements.txt`)
- **SEC EDGAR access** (set `EDGAR_IDENTITY` env var)
- **curl** — FRED/ALFRED 与财政部公开下载接口使用本机 curl；无需新 API key。

### Optional (for `transcript` command)
- **roic-transcript** skill — Earnings call transcript fetching
  - Location: must be at `skills/roic-transcript/` (sibling directory)
  - Install: `git clone https://github.com/everflowinv/roic-transcript.git skills/roic-transcript`
  - Without it: all commands work **except** `transcript` and NL queries matching "earnings call" / "transcript"

---

## 0) Preflight
```bash
bash skills/findata-analyst/run.sh --json self-test --ticker AAPL
```

---

## 1) Natural Language Entry (Recommended)

For any SEC/EDGAR question, prefer the `ask` command — it auto-routes to the correct subcommand:

```bash
bash skills/findata-analyst/run.sh --json ask NVDA gross margin trend
bash skills/findata-analyst/run.sh --json ask AAPL recent 8-K filings
bash skills/findata-analyst/run.sh --json ask TSLA insider trading
bash skills/findata-analyst/run.sh --json ask "NVDA vs AVGO which has higher gross margin"
bash skills/findata-analyst/run.sh --json ask AMZN depreciation policy
bash skills/findata-analyst/run.sh --json ask AJG business model
```

### What `ask` can detect and route:
| Question Pattern | Routes To |
|---|---|
| "transcript", "earnings call", "conference call" | `transcript` |
| "8-K", "corporate event", "earnings release" | `eight-k` |
| "insider", "form 4", "executive buy/sell" | `insider` |
| "gross margin", "ROE", "ROA", "profit margin" | `ratios` |
| "compare", "vs", "versus", two tickers | `compare` |
| "income statement", "balance sheet", "cash flow" | `financials` |
| "business model", "what does X do", "Item 1" | `read-item` |
| "depreciation", "useful life", "accounting policy" | `search-text` |
| "segment", "revenue by", "breakdown", "xbrl" | `smart-facts` |
| "revenue", "earnings", "net income", "trend" | `smart-facts` |

If the question doesn't match any pattern but contains a ticker, it falls back to `smart-facts`.

---

## 2) Full Command Catalog

### NL & Utility
| Command | Use Case | Key Args |
|---------|----------|----------|
| `ask` | Natural language router (recommended entry point) | positional: question words |
| `self-test` | Verify EDGAR connectivity and setup | `--ticker AAPL` (optional) |

### Financial Data
| Command | Use Case | Key Args |
|---------|----------|----------|
| `financials` | Full financial statements (income/balance/cash) | `--ticker --form 10-K --statement all\|income\|balance\|cash --limit 1` |
| `ratios` | Auto-calculated financial ratios from latest filing | `--ticker --form 10-K --limit 1` |
| `smart-facts` | One-step: auto find best XBRL concept + fetch time series | `--ticker --keyword Revenue --form 10-K --limit 5` |
| `facts` | Fetch exact XBRL concept time series (need concept ID) | `--ticker --concept us-gaap:Revenue --form 10-K --limit 5` |
| `search-concepts` | Find available XBRL concept IDs for a ticker | `--ticker --keyword Revenue` |
| `compare` | Compare a metric across two tickers side-by-side | `--tickers NVDA,AVGO --keyword GrossProfit --form 10-K` |

### Filings & Events
| Command | Use Case | Key Args |
|---------|----------|----------|
| `eight-k` | Recent 8-K events (earnings, material events) | `--ticker --limit 3` |
| `insider` | Form 4 insider trading filings | `--ticker --limit 5` |
| `list-filings` | Browse all filings by form type | `--ticker --form 10-K --limit 10` |
| `attachments` | List/read exact filing attachments such as EX-99.1 through edgartools | `--ticker --form 6-K [--accession] [--document-type EX-99.1]` |

### Qualitative Research
| Command | Use Case | Key Args |
|---------|----------|----------|
| `transcript` | Earnings call transcript (via roic.ai) | `--ticker --latest` or `--ticker --year 2025 --quarter 4` |
| `read-item` | Read specific 10-K/10-Q section (max 15K chars) | `--ticker --form 10-K --item "Item 1"` |
| `search-text` | Keyword search within filing full text | `--ticker --keyword "useful life" --form 10-K --window 500 --max_matches 10` |

### Common Items for `read-item`
- **Item 1** — Business description / operations overview
- **Item 1A** — Risk factors
- **Item 7** — MD&A (Management's Discussion and Analysis)
- **Item 8** — Financial statements & notes

---

## 3) Output Modes

All commands support `--json` (structured JSON) and `--csv` (for rows-based output):
```bash
bash skills/findata-analyst/run.sh --json ratios --ticker AAPL
bash skills/findata-analyst/run.sh --csv list-filings --ticker AAPL --limit 5
```

### JSON Output Structure
Every response follows the same envelope:
```json
{"ok": true, "ticker": "AAPL", ...command-specific fields...}
{"ok": false, "error": "description", "hint": "actionable suggestion"}
```

### `ratios` returns these metrics (when available):
- `gross_margin` — Gross Profit / Revenue × 100
- `operating_margin` — Operating Income / Revenue × 100
- `net_margin` — Net Income / Revenue × 100
- `roa` — Net Income / Total Assets × 100
- `roe` — Net Income / Total Equity × 100
- `raw` — underlying absolute values (revenue, gross_profit, operating_income, net_income, total_assets, total_equity)

### `smart-facts` returns:
- `chosen_concept` — the best-matching XBRL concept (prefers non-deprecated, us-gaap)
- `all_concepts` — top 5 candidate concepts found
- `rows` — time series with `period_end`, `numeric_value`, `unit`, `year_mismatch`

---

## 4) Facts Interpretation Rule (CRITICAL)

When using `facts` or `smart-facts` output:
- Anchor metrics to **`period_end`** (source of truth), NOT `fiscal_year_raw`
- If `year_mismatch=True`, **disclose it** in your answer
- Pattern: "As of `period_end`, `metric` was `value` `unit`."

For backlog/RPO maturity estimates:
1. Use `search-text` to locate “remaining performance obligations” in the revenue note. Record the as-of balance, included flexible-spending or unselected-product commitments, recognition percentage, and any exclusions from that percentage's denominator.
2. When the note excludes those commitments from its recognition schedule, calculate `(reported RPO − excluded commitments) × recognition percentage`. Otherwise use the stated denominator. Preserve the forecast horizon and approximate precision, and reconcile each company's denominator before comparing expected revenue.

---

## 5) Output Formatting for Users

1. **结论/Answer (1-3 lines)** — Direct answer first
2. **Key Data (bullets)** — Only from command output, with units and periods
3. **Source (bullet)** — ticker + form type + filing date
4. **Caveats (if needed)** — year_mismatch, missing data, truncation

⚠️ Never output markdown tables on Discord/WhatsApp; use bullet lists instead.

---

## 6) Features

- **SEC preflight** — `self-test` verifies EDGAR identity, company lookup, filing index, and attachment access; it never calls unrelated workspace scripts
- **Attachment retrieval** — `attachments` lists exact metadata and extracts a selected text/HTML exhibit through edgartools without guessing archive filenames
- **Retry mechanism** — Transient SEC errors (timeout, TLS, rate-limit) auto-retry up to 2× with exponential backoff
- **Disk cache** — Concepts and facts cached 24h in `scripts/.cache/` to reduce SEC API calls
- **Error hints** — Failed calls return actionable `hint` field (e.g., "Set EDGAR_IDENTITY", "Use search-concepts first")
- **Market coverage** — Any US-listed company with SEC EDGAR filings (stocks, ETFs, REITs, BDCs, SPACs)

---

## 7) Workflow Examples

**"NVDA 和 AVGO 哪个毛利率高?"**
```bash
bash skills/findata-analyst/run.sh --json compare --tickers NVDA,AVGO --keyword GrossProfit
```

**"AJG 的商业模式是怎么样的?"**
```bash
bash skills/findata-analyst/run.sh --json read-item --ticker AJG --form 10-K --item "Item 1"
```

**"Amazon 的服务器折旧年限?"**
```bash
bash skills/findata-analyst/run.sh --json search-text --ticker AMZN --keyword "useful life" --form 10-K
```

**"Microsoft 过去三年收入趋势"**
```bash
bash skills/findata-analyst/run.sh --json smart-facts --ticker MSFT --keyword Revenue --limit 3
```

**"Apple 最新一期财务比率"**
```bash
bash skills/findata-analyst/run.sh --json ratios --ticker AAPL
```

**"Tesla 最近有什么 insider 交易?"**
```bash
bash skills/findata-analyst/run.sh --json insider --ticker TSLA --limit 5
```

**"AAPL 最新 earnings call transcript"**
```bash
bash skills/findata-analyst/run.sh --json transcript --ticker AAPL --latest
```

**"SE Q4 2025 管理层发言"**
```bash
bash skills/findata-analyst/run.sh --json transcript --ticker SE --year 2025 --quarter 4 --section prepared
```


## 9) 历史披露与官方宏观数据

统一分类入口见 `references/data-sources.md`；原有命令保留可用。

### SEC 历史披露约束

对 `financials/ratios/list-filings/eight-k/insider/search-text/read-item/attachments/search-concepts/facts/smart-facts/compare` 加 `--as-of YYYY-MM-DD`，只用该日及之前披露的 SEC 证据。自然语言 `ask` 和转写稿不支持此参数，历史查询使用显式子命令。

```bash
bash skills/findata-analyst/run.sh --json list-filings --ticker AAPL --form 10-K --as-of 2024-10-15 --limit 1
bash skills/findata-analyst/run.sh --json facts --ticker AAPL --concept us-gaap:Assets --form 10-K --as-of 2024-10-15 --limit 3
```

保留 `filing_date/accession/form_type/period_start/period_end/unit`。同一截止期的季度与年初至今数分别保留；`--form` 约束申报表单，不保证每一行都是一个完整季度或年度。按完整期间与单位去重，采用截止日内最后一次披露值。没有历史披露时返回缺口，不回退当前 Yahoo 数据。精度为披露日期，不是盘中发布时间；适合按日复盘，不代表完整历史 SEC 数据库快照。

### FRED / ALFRED

```bash
bash skills/findata-analyst/run.sh --json macro-catalog
bash skills/findata-analyst/run.sh --json macro --series DGS10 --start 2026-10-01 --end 2026-10-08
bash skills/findata-analyst/run.sh --json macro --series GDPC1 --start 2023-01-01 --end 2023-12-31 --vintage 2024-01-01
```

普通查询返回当前修订序列；`--vintage` 使用 ALFRED 当日版本，并核对返回列名中的版本日期，失败不换成当前数据。无需新增 API key。`value` 保留十进制字符串，缺失值为 null，不填零。CSV 未给单位、季调与频率元数据，回 `series_url` 核实；历史 GDP 尤其要确认基期，不能套用当前美元单位。FRED 为分发平台，引用其标明的原始发布机构。

### 美国财政部

```bash
bash skills/findata-analyst/run.sh --json treasury --dataset tga --start 2026-10-05 --end 2026-10-08
bash skills/findata-analyst/run.sh --json treasury --dataset debt --start 2026-10-05 --end 2026-10-08
```

TGA 按明确的 Closing Balance 行提取 `open_today_bal`，单位百万美元；债务单位美元。输出保留 raw_records 与 API 字段说明。日期只筛 `record_date`，不提供历史发布版本；不能据此做严格无前视回测。分页达到上限标 `partial`，缩短窗口后再查。`status=empty` 与请求失败分开，不用另一个口径的数据静默补位。
