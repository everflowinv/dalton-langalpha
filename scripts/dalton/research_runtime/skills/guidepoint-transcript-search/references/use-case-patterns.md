# Use Case Patterns

Four research clusters with example prompts and recommended parameter combinations.
Use this file to verify you're matching the right parameter profile to the customer's intent.

---

## 1. Investment and Market Diligence

**Use case:** Find expert transcript excerpts on market dynamics, competitive positioning, customer
behavior, and industry trends across sectors. Useful for investors, strategy teams, and researchers
evaluating a company or market — particularly for clients entering a space and looking for targeted
insights from expert calls.

**Signals in the request:** competitive dynamics, market share, demand trends, pricing pressure,
M&A activity, customer behavior, industry landscape, sector evaluation.

**Recommended defaults:** `size` 15-20, omit `recency_bias` unless user specifies "recent."

### Example A — Market space overview
**Prompt:** "Find expert commentary on the mid-market cloud accounting software space."
```
query    : "Expert commentary on the mid-market cloud accounting software space, including competitive dynamics, customer behavior, and market trends."
keywords : (omit — no named entities)
size     : 15
```

### Example B — Competitive dynamics
**Prompt:** "What are experts saying about competitive dynamics in oncology advertising and market research?"
```
query    : "Expert views on competitive dynamics, market positioning, and industry trends in oncology advertising and pharmaceutical market research."
keywords : (omit)
size     : 15
```

### Example C — Recency requested
**Prompt:** "Summarize recent transcript insights on demand trends in enterprise AI infrastructure."
```
query        : "Expert views on demand trends and buyer behavior in enterprise AI infrastructure."
keywords     : (omit)
recency_bias : 80
size         : 15
```

### Example D — Named entities + thematic angle
**Prompt:** "Find expert commentary on Thrive and Berkshire Partners' roll-up activity in the MSP space."
```
query    : "Expert commentary on private equity roll-up activity and M&A in the managed service provider space."
keywords : ["Thrive", "Berkshire Partners"]
size     : 20
```

### Example E — Price and volume dynamics
**Prompt:** "What do experts say about pricing pressure in the cloud storage market?"
```
query    : "Expert views on pricing pressure, price-volume dynamics, and margin trends in the cloud storage market."
keywords : (omit)
size     : 15
```

---

## 2. Product, Technology, and Adoption Research

**Use case:** Surface expert views on product adoption, deployment patterns, buyer concerns, and
technical shifts. Helps users understand how technologies are being evaluated in the field —
including themes like AI, MCP, cloud software, and enterprise tools.

**Signals in the request:** adoption, deployment, buyer evaluation, rollout challenges, technical
shifts, enterprise tooling, product readiness, customer concerns.

**Recommended defaults:** `size` 10-15. Use `keywords` for product/protocol names and their abbreviations.

### Example A — Protocol adoption
**Prompt:** "What are experts saying about Model Context Protocol adoption in enterprise AI?"
```
query    : "Expert views on adoption, deployment patterns, and buyer evaluation of Model Context Protocol in enterprise AI environments."
keywords : ["Model Context Protocol", "MCP"]
size     : 15
```

### Example B — Pre-release product evaluation
**Prompt:** "Find transcript excerpts on how customers evaluate pre-release MCP server products."
```
query    : "How enterprise customers evaluate and assess pre-release AI tooling and server products."
keywords : ["MCP", "Model Context Protocol"]
size     : 10
```

### Example C — Enterprise rollout challenges
**Prompt:** "Summarize expert opinions on Copilot prompt training and enterprise rollout challenges."
```
query    : "Expert views on enterprise rollout challenges, prompt training, and adoption barriers for AI assistant products."
keywords : ["Microsoft Copilot", "Copilot"]
size     : 15
```

### Example D — Platform comparison (two searches)
**Prompt:** "Compare expert views on Veeva vs Salesforce Health Cloud in life sciences."
```
Search 1:
  query    : "Expert views on Veeva's product adoption, competitive positioning, and use in life sciences."
  keywords : ["Veeva"]
  size     : 10

Search 2:
  query    : "Expert views on Salesforce Health Cloud adoption, deployment, and competitive positioning in life sciences."
  keywords : ["Salesforce Health Cloud", "Salesforce"]
  size     : 10
```
Synthesize both into a unified comparison after receiving results.

---

## 3. Sales, Client Advisory, and Thematic Research

**Use case:** Quickly gather evidence-backed expert quotes for client questions, market research
themes, and commercial opportunity mapping. Helps teams identify where Guidepoint-style insight
is most useful — including market research, pharma, and adjacent commercial topics.

**Signals in the request:** client brief, market research themes, commercial opportunity, "brief me on",
"pull commentary I can use", "what themes are coming up", thematic research, advisory preparation.

**Recommended defaults:** `size` 15-20. Use `recency_bias` 70-80 when the brief is for a current client situation.

### Example A — Pharma market research
**Prompt:** "Find expert transcript excerpts relevant to market research opportunities in pharma."
```
query    : "Expert commentary on market research trends, opportunities, and buyer behavior in the pharmaceutical sector."
keywords : (omit)
size     : 20
```

### Example B — Healthcare media themes
**Prompt:** "What themes are coming up in expert calls about healthcare media and events expansion?"
```
query    : "What themes are emerging in expert calls about healthcare media, events, and commercial expansion opportunities?"
keywords : (omit)
size     : 20
```

### Example C — Client brief, managed markets
**Prompt:** "Pull recent expert commentary I can use to brief a client on trends in managed markets."
```
query        : "Expert views on trends, payer dynamics, and access strategies in managed markets."
keywords     : (omit)
recency_bias : 75
size         : 15
```
Present results in narrative prose — this is a briefing, not a comparison.

### Example D — Oncology advertising
**Prompt:** "What are experts saying about competitive dynamics in oncology advertising and market research?"
```
query    : "Expert commentary on competitive dynamics, buyer behavior, and market trends in oncology advertising and pharmaceutical market research."
keywords : (omit)
size     : 20
```

---

## 4. Hedge Fund and Buy-Side Research Workflows

**Use case:** Multi-step research workflows for buy-side professionals — pitch building, meeting
preparation, earnings review, market tracking, model input research, valuation context, and
counterparty background. The plugin supplies expert color as **one input** into these workflows;
the user combines it with their models, data feeds, filings, and internal processes.

**Signals in the request:** "build a pitch", "target list", "comp set", "meeting prep", "client brief",
"counterparty brief", "earnings prep", "earnings review", "model inputs", "track [sector]", "monitor
[issuer]", "valuation context", "multiples in [sector]", "background on [counterparty]".

**Recommended defaults:** `size` 15-20 for thematic; use `recency_bias` 70-85 for earnings prep, market
tracking, and any "current state" research.

---

### 4.1 Pitch Builder — expert color on target companies and theses

**Prompt:** "Build expert color for a pitch on consolidation in the regional MSP market — focus on Thrive and Berkshire Partners."

Decompose into atomic questions:
1. What are experts saying about regional MSP consolidation dynamics?
2. What do experts say about Thrive's growth and acquisition strategy?
3. What do experts say about Berkshire Partners' MSP playbook?

```
Search 1 (consolidation thesis):
  query        : "Expert views on consolidation, M&A activity, and roll-up dynamics in the regional managed service provider market."
  keywords     : (omit)
  recency_bias : 75
  size         : 15

Search 2 (Thrive):
  query    : "Expert commentary on Thrive's growth strategy, acquisition pattern, and competitive positioning in the MSP space."
  keywords : ["Thrive"]
  size     : 10

Search 3 (Berkshire Partners):
  query    : "Expert views on Berkshire Partners' MSP investment thesis, portfolio construction, and value creation playbook."
  keywords : ["Berkshire Partners"]
  size     : 10
```

**Present as:** Thesis-anchored synthesis with expert quotes supporting each part of the pitch. End with what the evidence didn't cover.

---

### 4.2 Meeting Preparer — counterparty and client industry briefs

**Prompt:** "Prep me for a meeting with the head of strategy at Veeva — I want recent expert color on their competitive position and where buyers are pushing back."

```
Search 1 (positioning):
  query        : "Expert views on Veeva's competitive position, product roadmap, and customer perception in life sciences."
  keywords     : ["Veeva"]
  recency_bias : 80
  size         : 15

Search 2 (buyer pushback):
  query        : "Expert commentary on customer concerns, pricing pushback, and buyer evaluation criteria for Veeva products."
  keywords     : ["Veeva"]
  recency_bias : 80
  size         : 10
```

**Present as:** Brief structured around (1) what experts say about positioning, (2) where pushback is concentrated, (3) questions worth raising in the meeting. Use `recency_bias` because meetings are about the current moment.

---

### 4.3 Earnings Reviewer — expert commentary contextualizing results

**Prompt:** "I'm prepping for ServiceNow Q3 earnings. What are experts seeing on demand trends, deal cycles, and AI agent adoption among large enterprise buyers?"

Decompose into the atomic questions a buy-side analyst would ask before the print:
1. What are experts seeing on enterprise demand and budget posture?
2. What are experts saying about deal cycles, slippage, and pipeline conversion?
3. What's expert commentary on ServiceNow's AI agent adoption with large customers?

```
Search 1 (demand):
  query        : "Expert views on enterprise IT demand trends, budget posture, and deal pipeline in workflow automation and IT service management."
  keywords     : ["ServiceNow"]
  recency_bias : 85
  size         : 12

Search 2 (deal cycles):
  query        : "Expert commentary on enterprise deal cycles, sales execution, and pipeline conversion at ServiceNow."
  keywords     : ["ServiceNow"]
  recency_bias : 85
  size         : 10

Search 3 (AI agents):
  query    : "Expert views on AI agent adoption, customer evaluation, and use cases for ServiceNow AI products in large enterprises."
  keywords : ["ServiceNow"]
  size     : 10
```

**Present as:** Pre-print thesis check structured by the questions above. Surface where expert views diverge from sell-side consensus or recent management guidance — this is the highest-value output. End with what would change the view (monitoring triggers).

---

### 4.4 Model Input Research — expert views on assumptions and drivers

**Prompt:** "I'm refreshing my model on the regional MSP space. What are experts seeing on gross margin trajectory, technician labor costs, and customer churn?"

Use this when the user wants expert color on **inputs** into a model (drivers, assumptions, sensitivities), not the model itself.

```
Search 1 (margins):
  query    : "Expert views on gross margin trajectory, pricing power, and cost structure in the regional managed service provider market."
  keywords : (omit)
  size     : 12

Search 2 (labor):
  query    : "Expert commentary on technician labor availability, wage inflation, and staffing costs at managed service providers."
  keywords : (omit)
  size     : 10

Search 3 (churn):
  query    : "Expert views on customer retention, churn drivers, and contract renewal dynamics in regional MSPs."
  keywords : (omit)
  size     : 10
```

**Present as:** Driver-by-driver synthesis. Be explicit that experts provide qualitative context for assumptions; numerical inputs still need to come from filings, data feeds, and the user's own modeling work.

---

### 4.5 Market Researcher — sector and issuer development tracking

**Prompt:** "Track recent expert sentiment on cloud ERP — pricing dynamics, M&A, and customer migrations. What's changed in the last six months?"

```
Search 1 (pricing):
  query        : "Recent expert views on pricing pressure, discounting behavior, and margin dynamics in the cloud ERP market."
  keywords     : (omit)
  recency_bias : 85
  size         : 15

Search 2 (M&A):
  query        : "Recent expert commentary on M&A activity, consolidation, and strategic moves in the cloud ERP space."
  keywords     : (omit)
  recency_bias : 80
  size         : 12

Search 3 (migrations):
  query        : "Expert views on customer migration patterns, platform switching, and competitive displacement in cloud ERP."
  keywords     : (omit)
  recency_bias : 80
  size         : 12
```

**Present as:** Three-section update with explicit "what's changed" framing. Surface emerging themes (something appearing for the first time in recent calls) and fading themes (something that was prominent six months ago and isn't now). End with monitoring triggers — what new evidence would shift the view.

---

### 4.6 Valuation Context — expert color on multiples and comparables

**Prompt:** "What multiples and valuation framings are experts citing for vertical SaaS in healthcare?"

Use this when the user wants qualitative context on how practitioners are framing valuation — multiples, deal benchmarks, market sentiment around premiums/discounts. The plugin does **not** validate or replace formal valuation work; it surfaces how the market is talking about value.

```
query        : "Expert views on valuation multiples, deal benchmarks, and pricing of M&A transactions in vertical SaaS for healthcare providers and life sciences."
keywords     : (omit)
recency_bias : 80
size         : 15
```

**Present as:** Synthesis of how experts are framing value — what multiples they cite, what premiums/discounts they explain, where they see disconnects between public and private valuations. Be explicit that this is qualitative context, not a methodology check or a substitute for comparable company analysis.

---

### 4.7 Counterparty Background — qualitative color (NOT a KYC substitute)

**Prompt:** "Pull background expert color on TSMC — operating reputation, customer concentration dynamics, and how counterparties view their reliability."

Use this when the user wants **qualitative reputational context** that may inform how they approach a counterparty or issuer. **This is not a KYC or compliance screening tool.** Frame the output accordingly.

```
query    : "Expert views on TSMC's operating reputation, customer concentration, supply chain reliability, and how counterparties assess their execution."
keywords : ["TSMC", "Taiwan Semiconductor"]
size     : 12
```

**Present as:** Qualitative summary with explicit caveat at the top:

> "This is qualitative expert color and does not constitute KYC, compliance, or due diligence screening. Use formal compliance workflows for any decision that requires verified counterparty information."

If transcript coverage is thin or non-existent on a private or smaller entity, say so directly — don't pad. Most private and lower-mid-market entities will have little or no expert coverage.

---

## Quick Reference — One-Line Summaries

Use these when you need a fast check on which cluster a request maps to:

| Cluster | What it covers | Canonical example |
|---|---|---|
| Investment and market diligence | Market dynamics, competitive positioning, customer behavior, industry trends | "Find expert commentary on the mid-market cloud accounting software space." |
| Product and technology adoption | Product adoption, deployment patterns, buyer concerns, technical shifts | "What are experts saying about Model Context Protocol adoption in enterprise AI?" |
| Sales and thematic research | Client briefs, market research themes, commercial opportunity mapping | "Find expert transcript excerpts relevant to market research opportunities in pharma." |
| Hedge fund and buy-side workflows | Pitch building, meeting prep, earnings review, model inputs, market tracking, valuation context | "Prep me for ServiceNow Q3 earnings — what are experts seeing on demand trends?" |

---

## Alternate Name Patterns

When a request involves entities with common abbreviations or trade names, include variants as separate keyword entries:

| Entity | Keywords array |
|---|---|
| Model Context Protocol | ["Model Context Protocol", "MCP"] |
| GLP-1 drugs | ["GLP-1", "semaglutide", "Ozempic", "Wegovy"] |
| Microsoft Copilot | ["Microsoft Copilot", "Copilot", "M365 Copilot"] |
| Salesforce Health Cloud | ["Salesforce Health Cloud", "Salesforce"] |

This ensures the search catches transcripts that use any common name for the same entity.
