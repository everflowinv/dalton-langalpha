---
name: "guidepoint-transcript-search"
disable-model-invocation: true
description: "Use this skill whenever the user wants expert commentary, practitioner views, or thematic intelligence on markets, companies, sectors, or technologies — inclu"
---

# Guidepoint Transcript Search

This skill guides the end-to-end workflow for searching Guidepoint's expert transcript library — from understanding what the customer wants, through calling the `search_library` tool, to presenting results in a way that's immediately useful.

The tool's own parameter descriptions handle how to call the tool correctly — query construction, keyword routing, size, recency, and date filters. This skill handles everything the tool description cannot reach: when to ask a clarifying question before searching, how to present results, how to enforce citations, how to refine when results fall short, and what to do when a request is out of scope.

The skill serves three primary user types: **investment professionals** (PE, hedge fund, equity, credit) doing diligence and idea generation, **product and strategy teams** researching adoption and competitive dynamics, and **client-facing advisors** preparing briefs and thematic research. Each user type has distinct workflow patterns — see `references/use-case-patterns.md`.

---

## Step 1 — Assess Before Calling

Most requests have enough signal to call the tool immediately. Only pause when the request is so underspecified that any search you run would be a guess.

**Call the tool immediately when** the request contains:
- Any thematic or market-level question — "what are experts saying about…", "summarize trends in…", "summarize expert views on…", "find transcript insights on…", "pull expert commentary on…"
- A named company, product, or person alongside any research angle
- A clear use case signal — "brief me on…", "find commentary for a client call on…", "what do experts think about…", "prep me for the [issuer] earnings call", "build a target list for…", "what are experts seeing in [sector]"

**Ask one clarifying question first when** the request falls into either of these patterns:

1. **Bare noun or phrase with no research angle** — the tool returns results anchored to intent, not just subject matter, so a subject alone isn't enough to run a useful search.

2. **Keyword stack without a clear angle** — multiple terms strung together that describe a topic but don't signal what the user is trying to learn or decide. These look like legitimate research questions but are actually underspecified.

Examples that warrant one question:
- "Berkshire Partners" → "What aspect of Berkshire Partners are you researching — portfolio strategy, M&A activity, a specific sector?"
- "GLP-1" → "Are you focused on clinical adoption, competitive positioning, payer access, or patient adherence?"
- "ServiceNow" → "What angle are you researching — competitive dynamics, product adoption, a specific industry vertical?"
- "GLP-1 pricing pushback" → "Are you focused on payer negotiating behavior, manufacturer response, patient affordability, or formulary access decisions?"
- "semiconductor inventory correction channel checks" → "What's the core question here — demand recovery timing, distributor destocking, or customer reorder patterns?"

The distinction: "GLP-1 pricing" has a topic and a dimension but no research question. "What are experts saying about payer pushback on GLP-1 pricing" has an angle — search immediately.

Keep it to one question. If the follow-up is still vague, make a reasonable interpretation and search — note the assumption you made at the top of your response.

### Expert-finding requests

When users ask to "find a Guidepoint expert, specialist, advisor, or industry source" on a topic — or otherwise frame the request as expert discovery rather than thematic research — treat this as a transcript search, not a refusal. The transcripts surface the experts who have spoken on that topic, and the `reference_url` for each result links to Guidepoint360, where the user can view the expert's profile.

For these requests:
- Run a `search_library` call targeting the topic the expert should specialize in
- Surface the relevant transcripts with their citations as usual
- **Make the booking pathway explicit in the response**: tell the user that to schedule a call with any of these experts, they should reach out to their Guidepoint project manager, who can facilitate scheduling. They can also view expert profiles via the citation links on Guidepoint360, though direct self-booking through that platform is generally not available — the project manager is the primary path.
- Don't claim the tool returns an expert directory directly — it returns transcripts featuring experts on the topic, which surfaces who to ask about

Example response framing:

> "Here are recent transcripts featuring experts who have spoken on cloud ERP. To schedule a call with any of them, reach out to your Guidepoint project manager — they can facilitate introductions and scheduling. You can also view expert profiles by following the citation links to Guidepoint360."

---

## Step 2 — Plan the Search

Before calling the tool, decide:

**One search or several?** For requests that span multiple distinct companies, themes, or research questions, plan multiple calls before executing any. See **Multi-Part and Multi-Step Requests** below.

**Whose perspective matters most?** Different research angles are best served by different expert perspectives. When constructing the `query` and `keywords`, think about who would have spoken about this topic:

| Research angle | Expert perspectives that matter most |
|---|---|
| Customer adoption, retention, switching | Current and former customers; channel partners |
| Competitive dynamics, win/loss, market share | Competitors; sales leaders; channel partners |
| Strategy, culture, execution, capital allocation | Former executives; senior operators |
| Demand signals, order patterns, inventory | Suppliers; distributors; resellers |
| Regulation, structural shifts, macro trends | Industry specialists; trade body veterans |
| Credit quality, sponsor behavior, deal dynamics | Lenders; advisors; restructuring practitioners |

Use this lens to craft queries that capture the right evidence base.

---

## Step 3 — Call the Tool

Use the `search_library` tool. The tool's parameter descriptions provide all guidance on how to fill `query`, `keywords`, `size`, `recency_bias`, `start_date`, and `end_date`.

Search Guidepoint's compliance-reviewed library of expert interview transcripts — firsthand intelligence from former executives, operators, and industry practitioners across hundreds of industries, curated by former investment analysts for decision-critical work.

Each transcript is a structured Q&A exchange between an inquirer and a subject-matter expert, covering investment and market diligence, product and adoption research, and advisory and thematic research. Best for on-the-ground competitive intelligence, channel checks, and practitioner perspectives not available in public filings, news, or analyst reports.

Each result returns:
  - transcript_name    Name of the source transcript
  - date              Date the expert call took place
  - inquirer          Person or organization who posed the question
  - respondent        Subject-matter expert who answered
  - question          The specific question asked in the transcript
  - answer            The expert's response
  - context           Surrounding passage from the transcript, if available
  - reference_url     Link to the full transcript on the Guidepoint360 platform
  - source_attribution  Object with two sub-fields: `.description` and `.markdown`

How to search: send a direct thematic `query`, concrete `keywords` to narrow which transcripts matter, optional `start_date` / `end_date`, optional `recency_bias`, and `size` for how many ranked excerpts to return in one response. When both `query` and `keywords` are set, keywords narrow transcripts first; the query then ranks excerpts within that set.

---

## Step 4 — Present Results

### Required Output Pattern for Thematic Summaries

For "what are experts saying about…", "summarize expert views on…", "brief me on…", "pull expert commentary on…", and similar thematic synthesis requests, structure the answer around major themes.

For each major theme, include:

1. **Takeaway:** A concise synthesis in the assistant's own words.
2. **Verbatim expert support:** At least one exact quote from the returned `answer` field that supports the takeaway.
3. **Citation:** The `source_attribution.markdown` citation exactly as returned, placed immediately after the quote that uses that source — not at the end of the paragraph.

Do this for every major theme or key conclusion. Avoid producing a fully paraphrased answer unless no quotable excerpt is available.

If the available excerpts support a theme but do not contain a strong quotable sentence, state that explicitly:

> "The available excerpts support this point, but they do not contain a strong quotable sentence."

### Surfacing Variant Perspectives

When experts disagree, or when expert views diverge from common assumptions (management guidance, sell-side consensus, public narrative), surface the divergence explicitly. Label it clearly:

- **Where experts converge:** [synthesis with quotes and citations]
- **Where experts diverge:** [synthesis with quotes and citations from each side]

This is especially valuable in investment diligence, earnings prep, and market tracking workflows. Don't average disagreeing experts into a smoothed narrative — present both views with equal weight.

### Bound Adjacent Evidence

When refinement finds only an adjacent geography, period, expert role, or end market, state the mismatch before using the excerpt. Use it only as bounded context or counterevidence; never treat it as direct validation of the target market or combine it with target-scope evidence as though the samples were comparable. Name the exact missing evidence that would validate the target claim.

### Citation Rule

Every result that has a `reference_url` must appear as a clickable Markdown link in your response. Use `source_attribution.markdown` exactly as returned.

Before responding, verify:
- Every verbatim quote has a citation immediately after it.
- Every paraphrased claim based on transcript content has a citation.
- No transcript source is discussed without source_attribution.markdown.

**Each verbatim quote must carry its own citation placed immediately after that quote** — not at the end of the paragraph, and not shared across multiple quotes. If a paragraph contains two quotes from different sources, each quote gets its own citation immediately after it. If a paragraph contains two quotes from the same source, cite after each quote; do not consolidate the citations at the end.

If a result has no `reference_url`, reference the `transcript_name` inline without a link.

Never summarize or quote a Guidepoint source without its citation.

### Table vs Narrative

**Use a Markdown table** when results contain the same type of information across multiple sources — company-by-company comparisons, rankings, metric patterns, or side-by-side competitive assessments.

**Use narrative prose** when synthesizing a theme, trend, or argument that builds across multiple expert views.

When in doubt: if the user asked "how do A, B, C compare" — table. If they asked "what are experts saying about X" — prose.

### Including Verbatim Quotes

For every user-facing synthesis based on transcript results, include at least one verbatim quote from the returned `answer` field for each major theme or key conclusion.

Do not rely only on paraphrase. Each main takeaway should include:
1. A concise synthesized interpretation in the assistant's own words
2. A short verbatim quote from the expert that supports the interpretation
3. The `source_attribution.markdown` citation immediately after the quote — not deferred to the end of the paragraph or section

Use the expert's exact wording from the `answer` field. Do not paraphrase inside quotation marks.

Prefer short quotes of 5–15 words. Keep all verbatim quotes under 20 words to comply with content licensing terms.

**Quote when:**
- The expert uses a vivid, specific, or unexpected phrase
- A number, comparison, or claim is more persuasive in their own words
- The reader benefits from hearing the expert's tone, uncertainty, or hedging directly
- The answer is a thematic expert synthesis and the quote supports a major takeaway

**Format:**
- Use Markdown blockquotes for quotes longer than ~15 words
- Use inline quotation marks for shorter phrases woven into prose
- Place the citation immediately after the quote, never before, never deferred
- Trim with ellipses if you cut the middle of a sentence, but never alter wording

**Don't:**
- Stitch together fragments from different excerpts inside one quote
- "Clean up" filler words in a way that changes meaning
- Quote without a citation
- Place a citation at the end of a paragraph that contains multiple quotes — each quote gets its own citation immediately after it
- Present a thematic transcript synthesis using paraphrase only, unless no usable quote exists
- Reproduce more than ~20 words verbatim from any single transcript, or use more than one quote per source

If the returned excerpts are too thin or the exact wording is not useful, state that explicitly:

> "The available excerpts support this point, but they do not contain a strong quotable sentence."

Never summarize or quote a Guidepoint source without its citation.

### No Investment or Advisory Recommendations

This skill synthesizes and surfaces what experts have said — it does not advise on actions.

Never recommend buying, selling, holding, or shorting a security, entering or exiting a position, or taking any business or investment action based on transcript content. If a user asks what they *should do*, redirect: summarize what experts observe, surface diverging views, and leave the decision to the user. The role of this skill is to inform judgment, not substitute for it.

This applies equally to non-investment decisions — do not advise on hiring, partnerships, product launches, or strategic moves. Surface the expert evidence; let the user draw their own conclusions.

### Completeness Transparency and Query Coaching

If results remain sparse after auto-refinement (Step 5), don't just report thin coverage and stop. Help the user search better.

**First, diagnose why the search underperformed.** Look at what came back (even if thin) and at the original request. Coverage is usually thin for one of these reasons:

| Pattern | What it looks like | What to suggest |
|---|---|---|
| **Query too narrow** | Hyper-specific phrasing, multiple stacked qualifiers ("pre-release X at Fortune 500 companies in Q3") | Drop the most specific qualifier; search the broader angle |
| **Entity too small or private** | Lower-mid-market private company, regional player, niche brand with no expert coverage | Search the sector or peer set instead; surface what experts say about the broader category |
| **Angle too new** | Recent product, emerging theme, just-announced deal | Search the underlying capability or analogous prior cases; widen `recency_bias` or remove date filters |
| **Wrong sector framing** | Query references a domain that experts don't use ("healthcare SaaS" when transcripts use "provider IT" or "RCM") | Reframe with the sector vocabulary experts actually use |
| **Missing alternate names** | Single brand name when product has aliases, abbreviations, or generic equivalents | Add alternate names as keywords (see `Alternate Name Patterns` in `references/use-case-patterns.md`) |
| **Library gap** | None of the above; the topic legitimately isn't covered | Be honest — say transcripts on this topic are sparse and suggest where the user might find better signal (filings, channel partners, primary expert calls) |

**Then write a response that does three things:**

1. **State what was tried and what came back** — briefly. "I ran two searches (the second broadening to [X]); this returned 3 results."
2. **Surface what the available results do support** — don't pad, but don't waste the excerpts either.
3. **Suggest 1–2 concrete reframings** — based on the diagnosis above. Be specific, not generic.

**Example response when the query was too narrow:**

> "I ran two searches; this returned 3 results, and coverage on this specific angle is thin. The available excerpts speak more to enterprise AI tool evaluation broadly than to pre-release MCP server frameworks specifically. Two ways to get stronger coverage:
> - Broaden to **enterprise AI tooling evaluation criteria** — more transcripts speak to how buyers assess pre-release tooling generally
> - Search the **competitive set** (Anthropic, OpenAI's Assistants API, internal frameworks) — experts often discuss MCP in comparison rather than standalone
>
> Want me to run either of these?"

**Example response when it's a genuine library gap:**

> "I ran two searches; this returned 2 results. Expert coverage on quantum computing in regional logistics is genuinely sparse — the topic is too early for substantial practitioner commentary. The available excerpts touch on adjacent themes (broader logistics tech adoption) but don't directly address your question. For this one, transcript search probably isn't the right channel; primary expert calls or industry conference content may give you stronger signal."

**Don't:**
- Suggest reframings that don't reflect what the actual results showed
- Offer generic advice ("try different keywords") — be specific about *which* keywords
- Pretend a library gap is a query problem (over-promises that better phrasing will help when it won't)
- Run a third or fourth automatic search after refinement already happened — at this point, ask the user which reframing they want before searching again

### Closing Section for Substantive Research Outputs

For multi-step research tasks (pitch prep, earnings review, market tracking, deal screens, full diligence), end the output with a brief close-out:

- **What the evidence didn't cover:** Specific gaps where the transcript base was thin or silent. Suggest a follow-up search or expert call topic.
- **What would change this view:** What new evidence (data point, expert perspective, market event) would alter the conclusion. Useful for monitoring.
- **Adjacent searches the user might run next:** One or two natural follow-ups that build on this output.

Skip this section for simple thematic lookups. Include it for anything resembling diligence, advisory prep, or ongoing monitoring.

---

## Step 5 — Refine When Needed

If the tool returns 0 results or off-target results, do not report back to the user yet. Run one refinement automatically before surfacing anything. The refinement strategy depends on what went wrong.

**Tier 1 — Precision (your initial call).** Specific keywords + targeted query.

**Tier 2 — Expansion.** If results are zero or very few:
- Drop the most specific keyword and retry with the broader entity name
- Step the query sentence up one level of abstraction
- Widen the date range if you set one

**Tier 3 — Adjacent perspective.** If results are still thin or off-target:
- Drop entity filters entirely and search via thematic keywords (synonyms, competitor names, customer/supplier perspective)
- Rewrite the query to anchor explicitly to the sector or use context
- Try the perspective-of-experts lens — if a customer-side query failed, try competitor-side or supplier-side

After refining, run the second search automatically and note it briefly in your response:

> "The initial search returned limited results; I refined the query and found these:"

Don't ask permission to refine — just do it. Stop refining after two attempts; if the third would still likely fail, surface the gap honestly.

---

## Multi-Part and Multi-Step Requests

When a request contains multiple distinct companies, products, or research angles, run separate searches and synthesize.

**Identify distinct parts before calling the tool:**
- "Compare expert views on Veeva vs Salesforce Health Cloud" → two searches, one per product
- "Summarize M&A trends and pricing dynamics in cloud ERP" → two searches, one per theme
- "What are experts saying about Ozempic adoption and payer coverage?" → one search, related themes

**How to decide: one search or two?**
- If the topics are related aspects of the same subject → one search
- If they're separate subjects that happen to appear in the same request → separate searches

### Multi-step research workflows

For workflow-type requests (pitch prep, earnings review, deal screen, market tracking), decompose into atomic research questions before calling the tool. A typical decomposition:

| Workflow complexity | Atomic questions | Total searches |
|---|---|---|
| Simple (single-angle thematic, single-name brief) | 1–2 | 1–3 |
| Medium (earnings prep, target screen, single-issuer diligence) | 3–5 | 4–8 |
| Complex (full pitch build, deep market scan, multi-issuer comparison) | 5–8 | 8–15 |

Don't pad the call count. Stop searching once each atomic question has sufficient evidence. Efficiency matters — both for response time and for the quality of the synthesis (over-fetching produces noise, not signal).

Present the combined output as a unified synthesis, not as raw blocks of results per search.

For multi-part requests, still include verbatim expert support for each major theme, comparison point, or conclusion whenever transcript excerpts provide usable wording.

---

## Out of Scope — What This Tool Cannot Do

### Requests scoped to a specific named transcript

The following request types are out of scope even if the user names a transcript that exists in the library:

- **Summarize a specific transcript** ("summarize the [transcript name] call", "give me the key points from [transcript name]") — the tool returns excerpts ranked by relevance to a query, not a full document summary. Do NOT call the tool and reassemble a summary from excerpts as a workaround.
- **Extract all takeaways from a single transcript** ("what did the expert say in [transcript name]", "pull everything from that [company] call") — same limitation; the tool is not a document reader.
- **Compare two specific transcripts head-to-head** ("how does [transcript A] compare to [transcript B]") — the tool does not support retrieval by transcript ID or side-by-side comparison of named documents.
- **Find a transcript by a specific advisor's name** ("find transcripts by John Smith", "what has [advisor name] said") — the tool searches by topic, not by respondent name. There is no name-based lookup.

For all of these, respond with a clear explanation of the limitation and redirect to what the tool can do:

> "I can search for expert views on a topic, but I'm not able to retrieve, summarize, or compare specific transcripts by name or advisor. To read a specific transcript in full, go directly to Guidepoint360 or ask your project manager to pull it. If you're researching a topic, I can search the broader transcript library for relevant expert commentary — want me to do that instead?"

### Finding upcoming events

If the user asks about upcoming expert calls, scheduled webinars, live events, or future transcript availability:
- This tool only accesses completed, published transcripts — it has no visibility into scheduled events or the pipeline of future calls.
- Redirect the user to their Guidepoint project manager for information on upcoming events or to request a call on a specific topic.

### Full transcript requests

If the user asks for a full or complete transcript:
- Do NOT attempt to return or reconstruct a full transcript.
- Call the tool to find the closest matching excerpts.
- Clearly tell the user the tool returns excerpts, not full transcripts.
- Direct them to the `reference_url` in the results to access the full transcript on Guidepoint360, or suggest they contact their project manager.
- Never fabricate or hallucinate a full transcript.

### Training data or bulk export requests

If the user asks to use transcript content for training an AI model, bulk-exporting data, or aggregating content at scale:
- Do NOT call the tool for this purpose.
- Decline clearly: Guidepoint transcript content is licensed for research and advisory use, not for AI model training or bulk data extraction.
- Offer what the tool can do instead: search for specific expert insights on a topic.
- Never suggest workarounds or methods to extract data at scale.

### Sentiment scoring or quantitative ranking requests

If the user asks for sentiment scores, numerical rankings, or statistical analysis across transcripts:
- Do NOT attempt to return scores or rankings.
- Inform the user that `search_library` returns Q&A excerpts and does not support automated sentiment analysis.
- Offer a qualitative alternative: search for expert views and synthesize perspectives as bullish, cautious, or mixed.
- Never fabricate numerical scores.

### Workflows the plugin contributes to but does not complete

For investment workflows, this skill supplies expert color from transcripts as **one input** into a larger process. It does not:

- Build pitchbooks, target lists, or comparable company tables (no document generation; expert color supplements but doesn't replace this work)
- Run financial models or update assumptions (no data feeds; expert views can inform model inputs but the modeling happens elsewhere)
- Validate valuations or multiples (experts may cite multiples in conversation, but this is qualitative context, not a methodology check)
- Reconcile general ledgers, run NAV calcs, prepare close packages, or audit financial statements (no accounting system access)
- Screen KYC entities or perform compliance review (expert color on counterparty reputation may exist in transcripts, but this is **not** a substitute for KYC procedures or compliance screening — frame any such output as background only and direct the user to formal compliance workflows)

When a request implies an end-to-end workflow the plugin can't complete, deliver the expert-color slice well and be explicit about what the user still needs to do elsewhere.

---

## Use Case Reference

For detailed examples across investment diligence, product/technology research, sales advisory, and buy-side research workflows — including sample prompts, recommended parameter combinations, and workflow templates for pitch building, meeting prep, earnings review, model input research, market tracking, and valuation context — read `references/use-case-patterns.md`.

Load it when:
- The user's request is ambiguous about which research angle to pursue
- You want to verify the right `size` or `recency_bias` for a given use case type
- The user asks for help phrasing their research question
- The user signals a buy-side workflow ("pitch builder", "earnings prep", "target list", "market tracking", "model input", "valuation context")

---

## 当前公共库参数约定（2026-10-28 实测）

- 以实时 `tools/list` 和实际校验为准；开发者网页静态示例有旧参数。`query` 必填，1–1000 字符；`keywords` 无法单独替代它。
- `size` 为 1–30 的整数。researchdata 未指定时沿用其默认 limit=3；需要更多摘录时显式传 limit 或 params.size。顶层 limit 超过30按30请求，显式 params.size 超限报错；不要传网页旧示例中的50。
- `keywords` 最多10项；不同条目 OR、一个字符串内的词 AND。它先限定访谈范围，摘录本身可能没有关键词；无筛选时省略或 null。
- `recency_bias` 是1–100的整数；一般近期偏好可从30开始。它只影响排序，严格时间窗用 UTC `start_date/end_date`。
- 返回相关 Q&A，无公开库全文读取、游标或 offset；30条不代表覆盖全部访谈。保留 `context`、`reference_url` 和 `source_attribution`，不拼接冒充全文。
- 公共活动目录已有独立 `guidepoint__search_events`；查未来公开活动应按实时 schema 调用该工具，上文“找项目经理”的旧建议仅适用于缺乏活动工具的环境。当前事件类型用显示名称（如 `Moderated Call`），没有 offset 参数；官网静态事件示例较旧。
- 本次维护范围为公共访谈库和已有活动工具；个人项目、一对一访谈、My Network 和会议全文不加入 researchdata，不作为本用户工作流的新入口。

## Local Setup (Eve workspace — appended, not part of vendor skill)

本环境的 Guidepoint MCP 接入方式（vendor skill 上文描述的 `search_library` 工具通过以下链路提供）：

- **调用入口**: 统一走 researchdata：`research(route="guidepoint", operation="search_library", query=..., limit=..., start=..., params={"keywords": [...], "recency_bias": ...})`，Codex 下工具名 `openclaw__researchdata__research`。OpenClaw 的 `guidepoint` MCP 已用 toolFilter 隐藏 `search_library`，不要找 `guidepoint__search_library`；活动报名类工具（search_events 等）仍在 `guidepoint__*`。
- **MCP server**: OpenClaw `mcp.servers.guidepoint` → `http://127.0.0.1:8943/mcp`（streamable-http），researchdata 转发到这里。
- **本地 proxy**: `scripts/guidepoint_mcp_proxy.py`，LaunchAgent `ai.openclaw.guidepoint-mcp-proxy` 常驻，自动刷新 Auth0 access_token（token 存 `config/guidepoint_mcp_tokens.json`）。代理服务将在下一批迁到 Dalton。
- **CLI fallback**（工具表里没有 researchdata 时；同一入口：`python3 scripts/data_gateway.py fetch guidepoint --operation search_library --query "..." --params-json '{"keywords":["NVDA"]}'`）。直连 proxy 的旧命令仍可用，排障时用：
  ```bash
  bash skills/guidepoint-transcript-search/run.sh self-test
  bash skills/guidepoint-transcript-search/run.sh search --query "研究问题（英文效果最好）" --size 10
  bash skills/guidepoint-transcript-search/run.sh search --query "..." --keywords NVDA "data center" --start-date 2025-01-01 --json
  ```
- **故障排查**: `self-test` 失败 → `launchctl kickstart -k gui/$UID/ai.openclaw.guidepoint-mcp-proxy`；仍失败（refresh_token 失效）→ 重跑 `scripts/guidepoint_mcp_oauth.py` 走一次浏览器 OAuth。
- **query 语言**: 转录原文是英文，query 用英文；输出综述可用中文，但 verbatim quote 保留英文原文。
