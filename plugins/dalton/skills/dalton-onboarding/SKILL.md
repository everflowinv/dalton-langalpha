---
name: dalton-onboarding
description: First-time user onboarding interview — collect holdings, markets, style, risk, and answer preferences.
---

# Dalton Onboarding

First-conversation interview for a new Dalton user. You are running in **Home** (`All workspaces`) as the user's **Chief of Staff**. Conduct a short, conversational interview to capture their holdings and watchlist, followed markets, investment style and risk tolerance, and answer preferences, then write the structured profile under `.agents/user/profile/` and propose a tailored first research task.

**Language**: Match the `<locale>` tag from the user message (e.g., `zh-CN` -> Simplified Chinese, `en-US` -> English). If the user replies in a different language, switch to theirs.

---

## Interview Flow

### Step 1: Read `.agents/user/profile/README.md` & Check Existing Profile

Before asking questions, read `.agents/user/profile/README.md` for the authoritative schema and vocabulary slugs, and check `.agents/user/profile/user.json`, `.agents/user/profile/portfolio.json`, `.agents/user/profile/watchlist.json`, and `.agents/user/profile/preference.json` to see if anything is already populated.

- Do **not** set `onboarding_completed: true` yet.
- Do **not** write `.agents/user/profile/preference.json` until **Step 5 (Finish)** — committing `risk_preference.risk_level` in `preference.json` automatically marks onboarding complete on the server.

### Step 2: Greeting + Holdings, Watchlist & Followed Markets

Greet the user warmly (2–3 sentences) and briefly introduce what you can do together:

- **Research Workspaces & Deep Dives** — Dedicated workspaces where an analyst agent runs fundamental, macro, credit, and quantitative research with Python and live market data.
- **Reports, Charts & Financial Models** — Valuation models, peer comps, earnings previews/reviews, and visual deliverables.
- **Portfolio & Watchlist Tracking** — Monitoring holdings, watchlists, earnings calendars, and scheduled briefings.

Then ask one combined question to get started:

> What stocks, ETFs, bonds, or other assets do you currently hold or watch (and roughly how much or at what cost, if you'd like to share), and which markets do you primarily follow (e.g., US, HK, A-shares/CN, Japan, Europe)? If you don't have specific positions in mind yet, what sectors or themes are you most interested in exploring?

Once the user answers:
- Write any current positions to `.agents/user/profile/portfolio.json`.
- Write any watched tickers or candidate ideas to `.agents/user/profile/watchlist.json`.
- Hold any market/sector preferences in context to write into `.agents/user/profile/preference.json` in Step 5.

### Step 3: Investment Style & Risk

Next, ask about their investment approach and risk tolerance in a single turn (use `AskUserQuestion` with 2 concise questions, or ask directly in 2–3 sentences):

1. **Investment style & horizon** — How do they approach investing (fundamental, macro, quantitative, technical, event-driven), what style fits best (growth, value, income, index), and what is their typical holding period (intraday, days, months, years)?
2. **Risk tolerance** — How do they think about risk (conservative / capital preservation, moderate / balanced, growth, or aggressive), and do they have any drawdown or concentration rules?

Hold their answers in context until Step 5 (do **not** write `preference.json` yet).

### Step 4: Answer Preferences

Ask one final question about how they want research and answers delivered (via `AskUserQuestion` or a brief conversational question):

- **Research depth** — Quick takeaways (`light`), matched to the question (`adaptive`), or deep-dive with full citations and tables (`thorough`)?
- **Deliverable & proactivity** — Prose-first (`text`) or charts and tables (`visual`), Markdown (`markdown`) or HTML (`html`), and whether you should proactively suggest follow-ups (`proactive`, `when_needed`, or `on_request`)?

### Step 5: Finish (Write Structured Profile & Complete Onboarding)

Only after the interview questions in Steps 2–4 are answered (or if the user explicitly asks to skip the remaining questions):

1. **Write `.agents/user/profile/preference.json`** with `risk_preference`, `investment_preference`, and `agent_preference` following the structured schema below.
2. **Write `.agents/user/profile/user.json`** with `name`, `timezone`, `locale`, and `"onboarding_completed": true` (preserving any existing `name`, `timezone`, or `locale` values).
3. **Summarize & propose a first task**:
   - Briefly confirm what you saved (2–3 sentences).
   - Propose 1–2 concrete, high-value first research tasks tailored to their holdings, watchlist, or sectors (for example: an earnings or valuation deep-dive on one of their core holdings in a dedicated workspace using `manage_workspaces(action="create")` and `delegate_to_analyst`, or a quick portfolio/watchlist briefing right here in Home).

---

## Structured Profile Schema (`.agents/user/profile/`)

Always follow `.agents/user/profile/README.md`. All four files under `.agents/user/profile/` are validated by the server on save; invalid enum values return `NOT SAVED`.

### 1. `.agents/user/profile/user.json`

```json
{
  "name": "Alice",
  "timezone": "Asia/Shanghai",
  "locale": "zh-CN",
  "onboarding_completed": true
}
```

- `name`: `string | null` — how the user wants to be addressed.
- `timezone`: `string | null` — IANA timezone name (e.g., `"America/New_York"`, `"Asia/Shanghai"`, `"Asia/Hong_Kong"`).
- `locale`: `string | null` — BCP-47 locale tag (e.g., `"en-US"`, `"zh-CN"`).
- `onboarding_completed`: `boolean` — set to `true` **only** in Step 5 after the interview finishes.

### 2. `.agents/user/profile/portfolio.json`

```json
{
  "holdings": [
    {
      "symbol": "NVDA",
      "instrument_type": "stock",
      "quantity": 100,
      "average_cost": 115.5,
      "currency": "USD",
      "exchange": "NASDAQ",
      "name": "NVIDIA Corp.",
      "account_name": null,
      "notes": "Core AI infrastructure position",
      "first_purchased_at": null
    }
  ]
}
```

- `symbol`: required ticker symbol (e.g., `"NVDA"`, `"0700.HK"`, `"600519.SS"`).
- `instrument_type`: `"stock"` | `"etf"` | `"option"` | `"crypto"` | `"bond"` | `"fund"`.
- `quantity`: `number` (use `0` if the user only shared the ticker or allocation weight and put weight details in `notes`).
- `average_cost`: `number | null` (omit or `null` if unknown).

### 3. `.agents/user/profile/watchlist.json`

```json
{
  "watchlists": [
    {
      "name": "Default",
      "description": null,
      "is_default": true,
      "items": [
        {
          "symbol": "MSFT",
          "instrument_type": "stock",
          "exchange": "NASDAQ",
          "name": "Microsoft Corp.",
          "notes": "Watching cloud margin expansion",
          "alert_settings": {}
        }
      ]
    }
  ]
}
```

- Each item requires `symbol` and `instrument_type` (`"stock"` | `"etf"` | `"index"` | `"crypto"` | `"bond"` | `"fund"` | `"commodity"` | `"forex"`).

### 4. `.agents/user/profile/preference.json`

```json
{
  "risk_preference": {
    "risk_level": "moderate",
    "risk_tolerance": "Max single-position weight 10%; uncomfortable with >15% drawdown"
  },
  "investment_preference": {
    "investor_type": "professional",
    "experience": "3_10y",
    "help_with": ["research", "ideas", "portfolio"],
    "familiar_instruments": ["stock", "etf", "bond"],
    "markets": ["us", "hk", "cn"],
    "approaches": ["fundamental", "macro"],
    "styles": ["growth", "value"],
    "horizon": "months",
    "sectors": ["technology", "semiconductors", "healthcare"],
    "analysis_focus": "Free cash flow yield, ROIC durability, and margin inflection"
  },
  "agent_preference": {
    "research_depth": "thorough",
    "deliverable": "visual",
    "proactivity": "when_needed",
    "output_format": "markdown",
    "tone": "Direct, institutional-grade, data-first"
  }
}
```

Allowed vocabulary values for structured keys (additional free-form keys like `risk_tolerance`, `analysis_focus`, `company_interest`, `tone` are preserved alongside them):

- **`risk_preference`**:
  - `risk_level`: `"conservative"` | `"moderate"` | `"growth"` | `"aggressive"`
- **`investment_preference`**:
  - `investor_type`: `"individual"` | `"professional"` | `"student"`
  - `experience`: `"under_1y"` | `"1_3y"` | `"3_10y"` | `"over_10y"`
  - `help_with` (array): `"ideas"` | `"research"` | `"portfolio"` | `"news"` | `"learning"`
  - `familiar_instruments` / `asset_classes` (array): `"stock"` | `"etf"` | `"index"` | `"option"` | `"future"` | `"bond"` | `"fund"` | `"forex"` | `"crypto"` | `"commodity"`
  - `markets` (array): `"us"` | `"hk"` | `"cn"` | `"jp"` | `"eu"`
  - `approaches` (array): `"fundamental"` | `"macro"` | `"quantitative"` | `"technical"` | `"event_driven"`
  - `styles` (array): `"growth"` | `"value"` | `"income"` | `"index"`
  - `horizon`: `"intraday"` | `"days"` | `"months"` | `"years"`
  - `sectors` (array, up to 20): lowercase slugs matching `[a-z0-9]+(?:[_-][a-z0-9]+)*` (e.g., `"technology"`, `"healthcare"`, `"financials"`, `"energy"`, `"consumer"`, `"industrials"`, `"real_estate"`, `"utilities"`, `"materials"`, `"communication"`, `"semiconductors"`, `"clean_energy"`, `"biotech"`)
- **`agent_preference`**:
  - `research_depth`: `"light"` | `"adaptive"` | `"thorough"`
  - `deliverable`: `"text"` | `"visual"`
  - `proactivity`: `"proactive"` | `"when_needed"` | `"on_request"`
  - `output_format`: `"markdown"` | `"html"`

---

## Guardrails

- **No brokerage or live trading**: Never ask about brokerage accounts, linking a broker, or live trade execution. Do not set `brokerages` or `"trading"` in `preference.json`.
- **Complete onboarding only at the end**: Never write `"onboarding_completed": true` in `user.json` or commit `preference.json` before the interview finishes in Step 5.
- **Keep it conversational**: 2–4 short turns total. Do not dump a long questionnaire in a single wall of text.
- **If the user jumps straight to a research task**: Help them with their request first (answering directly or delegating to a workspace analyst), save any profile facts they mentioned, and only mark `onboarding_completed: true` once they confirm they are done with setup or finish the remaining profile questions.
