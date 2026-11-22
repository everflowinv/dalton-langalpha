---
name: dalton-onboarding
description: First-time user onboarding interview — collect holdings, markets, style, risk, and answer preferences.
---

# Dalton Onboarding

First-conversation interview for a new Dalton user. You are running in **Home** (`All workspaces`) as the user's **Chief of Staff**. Conduct a short, conversational interview to capture their holdings and watchlist, followed markets, investment style and risk tolerance, and answer preferences, then write the structured profile under `.agents/user/profile/` and propose a tailored first research task.

**Language**: Match the language of the opening message (there is no `<locale>` tag). If the user switches to a different language, switch to theirs.

---

## Interview Flow

### Step 1: Read `.agents/user/profile/README.md` & Check Existing Profile

Before asking questions, read `.agents/user/profile/README.md` for the authoritative schema and vocabulary slugs, and check `.agents/user/profile/user.json`, `.agents/user/profile/portfolio.json`, `.agents/user/profile/watchlist.json`, and `.agents/user/profile/preference.json` to see if anything is already populated.

- Do **not** set `onboarding_completed: true` yet.
- **Write no profile file until Step 5.** Every committed save to any of the four profile files runs the server's fallback completion check (`profile_files.py` → `maybe_complete_onboarding`), and a user who already has risk-related free text or watchlist items would be marked complete the moment Step 2 wrote a portfolio row. Steps 2–4 only hold answers in the conversation; Step 5 writes everything at once.

### Step 2: Greeting + Holdings, Watchlist & Followed Markets

Greet the user warmly (2–3 sentences) and briefly introduce what you can do together:

- **Research Workspaces & Deep Dives** — Dedicated workspaces where an analyst agent runs fundamental, macro, credit, and quantitative research with Python and live market data.
- **Reports, Charts & Financial Models** — Valuation models, peer comps, earnings previews/reviews, and visual deliverables.
- **Portfolio & Watchlist Tracking** — Monitoring holdings, watchlists, earnings calendars, and scheduled briefings.

Then ask one combined question to get started:

> What stocks, ETFs, bonds, or other assets do you currently hold or watch (and roughly how much or at what cost, if you'd like to share), and which markets do you primarily follow (e.g., US, HK, A-shares/CN, Japan, Europe)? If you don't have specific positions in mind yet, what sectors or themes are you most interested in exploring?

Hold everything the user answers — positions, watched tickers, markets, sectors — in context for Step 5. **Do not write `portfolio.json` or `watchlist.json` yet** (see Step 1).

### Step 3: Investment Style & Risk

Next, ask about their investment approach and risk tolerance in a single chat message (2–3 sentences). When using `AskUserQuestion`, ask only one topic per call:

1. **Investment style & horizon** — How do they approach investing (fundamental, macro, quantitative, technical, event-driven), what style fits best (growth, value, income, index), and what is their typical holding period (intraday, days, months, years)?
2. **Risk tolerance** — How do they think about risk (conservative / capital preservation, moderate / balanced, growth, or aggressive), and do they have any drawdown or concentration rules?

Hold their answers in context for Step 5 (still no writes).

### Step 4: Answer Preferences

Ask one final question about how they want research and answers delivered (via `AskUserQuestion` or a brief conversational question):

- **Research depth** — Quick takeaways (`light`), matched to the question (`adaptive`), or deep-dive with full citations and tables (`thorough`)?
- **Output format & proactivity** — Ranked order of answer formats (`output_format`, most preferred first: `"visual"` for charts and HTML reports, `"markdown"` for summary with a Markdown file, `"text"` for text in the chat), and whether you should proactively suggest follow-ups (`proactive`, `when_needed`, or `on_request`)?

Hold their answers in context for Step 5 (still no writes).

### Step 5: Finish (Write Structured Profile & Complete Onboarding)

Only after the interview questions in Steps 2–4 are answered (or if the user explicitly asks to skip the remaining questions), **re-read the existing profile files first**, then merge only the user-confirmed changes in this one step (the server deletes any holdings, watchlists, or items omitted from a written file):

1. **`.agents/user/profile/portfolio.json`**: merge user-confirmed holdings while preserving all existing holdings; if the user provided no holdings info, skip writing this file.
2. **`.agents/user/profile/watchlist.json`**: merge user-confirmed tickers while preserving all existing watchlists and items; if the user provided no watchlist info, skip writing this file.
3. **`.agents/user/profile/preference.json`**: merge user-confirmed keys under `risk_preference`, `investment_preference`, and `agent_preference` while preserving existing values (only keys the user actually answered; skip writing if none were provided).
4. **`.agents/user/profile/user.json`**: preserve any existing `name`, `timezone`, or `locale` values and set `"onboarding_completed": true`.
5. **Summarize & propose a first task**:
   - Briefly confirm what you saved (2–3 sentences).
   - Propose 1–2 concrete, high-value first research tasks tailored to their holdings, watchlist, or sectors (for example: an earnings or valuation deep-dive on one of their core holdings in a dedicated workspace using `manage_workspaces(action="create")` and `delegate_to_analyst`, or a quick portfolio/watchlist briefing right here in Home).

---

## Structured Profile Schema (`.agents/user/profile/`)

All field schemas and allowed enum values follow `.agents/user/profile/README.md` (validated server-side on save; invalid values return `NOT SAVED`). Save only information the user explicitly provided — never invent unasked keys (`investor_type`, `experience`, `help_with`, `familiar_instruments`, `tone`, …), and condense free-text fields (`risk_tolerance`, `analysis_focus`) from the user's own words. Set `onboarding_completed: true` in `user.json` only in Step 5.

Minimal `preference.json` example (include only keys answered):

```json
{
  "risk_preference": {
    "risk_level": "moderate"
  },
  "investment_preference": {
    "markets": ["us", "hk"],
    "approaches": ["fundamental"],
    "styles": ["growth"],
    "horizon": "months"
  },
  "agent_preference": {
    "research_depth": "adaptive",
    "output_format": ["markdown", "visual", "text"],
    "proactivity": "when_needed"
  }
}
```

---

## Guardrails

- **No brokerage or live trading**: Never ask about brokerage accounts, linking a broker, or live trade execution. Do not set `brokerages` or `"trading"` in `preference.json`.
- **Write no profile file before Step 5**: Follow Step 1 — write all changed profile files together in Step 5, and set `onboarding_completed: true` only there.
- **Keep it conversational**: 2–4 short turns total. Do not dump a long questionnaire in a single wall of text.
- **If the user jumps straight to a research task**: Help them with their request first (answering directly or delegating to a workspace analyst) and write nothing. When they later confirm setup is done — or answer the remaining interview questions — run Step 5's writes in full (including `onboarding_completed: true`); facts they mentioned earlier ride along in that one write. Never write `preference.json`'s risk keys or `onboarding_completed` on the strength of a passing mention alone.
