---
name: "company-wiki"
description: "本地投研知识库：按公司/行业入库研报、纪要、公告并混合检索、打标签、交叉引用。"
user-invocable: true
---

# Company Wiki Skill

## 统一只读入口

主会话查资料优先用 `researchdata.research(route="company-wiki", operation="search", query="...", params={"company":"NVDA"})`；支持 `company/sector/tag`、`start/end` 和 `limit`。搜索返回 `document_id="wiki:<id>"`，用 `operation="read", document="wiki:<id>"` 读取，正文按 `params.offset/max_chars` 分页；`list/topic` 也可从统一入口调用。

跨本地 wiki、卖方原邮件和 TMTB 查询用 `route="all"`。参数与来源口径见 workspace 的 `references/data-sources.md`。统一入口只读已有索引，不入库、不重建索引；下文原 CLI、目录命名、入库规则和 cron 调用继续有效。

图片也走同一入口：`params.media="images"` 只搜图，默认 `all` 搜图文；`operation="read", document="image:<id>"` 返回预览和原件路径、原文 ID。正文 read 的 `images` 列出已关联图片。图向量固定使用 `gemini-embedding-2-preview`，与旧文本索引分开保存。原 CLI `search` 仍只搜原文本索引。

## Overview

A local investment knowledge base that stores and retrieves research content:
1. **Add** — Save content under a company or industry sector, auto-classify, auto-tag, and embed
2. **Search** — Hybrid search (dense semantic + FTS5 keyword, RRF fusion, document-level results, recency decay), with optional tag filtering
3. **List** — Browse companies, sectors, and their documents
4. **Read** — Read a specific document
5. **Links** — Show bidirectional cross-references for a company/sector
6. **Topic** — Find all documents tagged with a topic, grouped by company/sector
7. **Reindex** — Batch re-tag all documents (with optional re-embedding)
8. **Graph** — Generate a cross-reference relationship graph
9. **Index-missing** — Incrementally index on-disk documents missing from the DB
10. **Extract** — `extract --file <path> [--ocr]` 只提取正文、不写库，供 sales notes 的 `add-note` 复用 PDF/DOCX/读图提取

## Storage Structure

```
wiki/
├── companies/
│   ├── FUTU/
│   │   ├── index.md                         # Auto-generated TOC + reverse refs
│   │   ├── 2026-03-13-管理层会议纪要.md
│   │   └── 2026-03-15-财报摘要.md
│   └── BABA/
│       ├── index.md
│       └── 2026-03-14-新闻更新.md
├── sectors/
│   ├── china-internet/
│   │   ├── index.md
│   │   └── 2026-03-13-行业趋势.md
│   ├── us-semiconductor/
│   │   ├── index.md
│   │   └── 2026-03-14-供应链更新.md
│   └── ...
└── vectors.db                               # SQLite + embeddings + tags + related
```

### Document Format
Files may include YAML frontmatter with auto-generated tags and normalized source fields:
```yaml
---
tags: [跨境电商, TEMU, 竞争格局]
related_companies: [SHEIN, BABA, JD]
related_sectors: [china-consumer, china-internet]
source: 晚点LatePost
source_url: https://mp.weixin.qq.com/s/xxxx
---
# Content Type

**日期**: 2026-03-15
...
```

`source`（来源名：公众号/券商/会议/Flomo 等）和 `source_url`（原文链接）在入库时通过 `--source` / `--source-url` 写入，可以为空。`reindex` / `index-missing` 重建 frontmatter 时会保留这两个字段，不会丢失。

### Company Folder Naming Convention (MANDATORY)

公司目录名一律用**公司名简写**（大写拉丁 slug 或惯用英文名），**禁止用纯数字 ticker**。
A股/港股公司尤其注意：不要用 `6600`、`300115`、`688017` 这类数字代码建目录。

- ✅ `ONEROBOTICS`（卧安机器人 6600.HK，官方英文名 OneRobotics）、`EVERWIN`（长盈精密 300115）、`SUNWAY`（信维通信 300136）、`LEADERDRIVE`（绿的谐波 688017）、`POPMART`、`MIXUE`
- ❌ `6600/`、`300115/`、`0700/`、`9988/`

原因：数字目录不可读、语义搜索/交叉引用时与其他数字（金额、年份）混淆、auto-tagger 归一化会失效。
新建 A股/港股公司目录前，先在 `config.json` 的 `aliases` 里加映射（中文名、数字代码 → slug），再用 slug 建目录。美股公司直接用 ticker（本身就是字母简写）。

**CLI 强制校验**：`add` / `add-file` 在创建新目录前会校验目录名——公司 slug 必须匹配 `^[A-Z][A-Z0-9-]*$`（如 `UNITREE`），行业 slug 必须匹配 `^[a-z][a-z0-9-]*$`（如 `china-robotics`），`temp*` 开头的名称保留不可用。不满足时直接报错，并提示先在 `config.json` 的 `aliases` 里加映射再重试。已存在的目录不受校验影响。新公司名若是纯 ASCII 小写输入（如 `testco`），解析时自动转大写 `TESTCO` 后再校验，不会因大小写被误拒。

### File Naming Convention
`{YYYY-MM-DD}-{content_type}.md`

content type 中的空格会自动替换为 `-`（`Earnings Update` → `Earnings-Update`），避免文件名带空格。

**卖方观点不进 wiki**（2026-10-19 起）：券商研报和点评、卖方分析师电话会、卖方 sales color、券商写的会议 takeaways 报告一律用 `skills/market-digest/run.sh add-note` 进 sales notes；wiki 只收专家会、公司会和其他资料。分库标准见 workspace AGENTS.md「转发资料入库：先分 sales notes 还是 wiki」。当天已把 wiki 里 79 篇卖方文档（原「券商研报」「卖方交流」「卖方纪要」等类型，以及标错类型的券商报告）原样迁过去，向量没有重算；以后再发现入错库的，用 `python3 scripts/wiki_to_sales_notes.py plan --ids ... --out temp/.../plan.json` 生成清单、核对机构和标题后 `apply --plan`。

Content types include but are not limited to:
- 管理层会议纪要 — Group Meeting with IR/CFO/CEO, 1x1 Meeting（券商主持、公司发言的也算）
- 专家访谈 — Expert calls (TB Call, CIC Call, 高临访谈, 内容社区, 券商组织的专家会)
- 季度研究笔记 — Quarterly earnings analysis (e.g. "BABA 25Q1")
- Earnings Update — Post-earnings update notes（团队自己写的）
- ~~券商研报~~ — 已停用，改进 sales notes
- NDR纪要 — Non-Deal Roadshow takeaways（公司发言的纪要；券商写的 NDR 报告进 sales notes）
- Fireside Chat — CFO/CEO fireside chat notes
- 买方交流 — Buy-side call takeaways
- 研究笔记 — General research notes
- 新闻更新 / News Update
- 行业趋势 / Industry Trends
- Flomo笔记 — Short-form notes from flomo
- 微信文章 — WeChat article clippings
- IPO Prospectus — S-1/Prospectus filings
- 投资备忘 / Investment Memo

### Predefined Sectors
- `us-semiconductor` — US Semiconductor
- `china-consumer` — China Consumer
- `china-internet` — China Internet
- `us-software` — US Software
- `us-financials` — US Financials
- `us-megtech` — US Mega Tech & AI
- `macro` — Macro & Geopolitics

Custom sectors can be created on the fly.

## Embedding Model

- **Model**: `gemini-embedding-001` (fallback; tries `gemini-embedding-2-preview` first)
- **SDK**: `google-genai>=1.0.0`
- **Batch embedding**: 100 texts per API call
- **Vector DB**: SQLite with numpy cosine similarity (same as market-digest)

## Search Architecture (Hybrid)

`search` 是双路召回 + RRF 融合，结果按**文档**去重聚合：

1. **Dense arm** — Gemini embedding 余弦相似度（原有路径）。
2. **Keyword arm** — SQLite FTS5 `trigram` 索引（`chunks_fts` 表，触发器与 `chunks` 自动同步）。查询按空白/标点切词：≥3 字符的词走 FTS（bm25 排序）；2 字符的中文短词（如「宇树」「美团」）trigram 无法索引，回退到 chunks 表 LIKE 扫描。精确 ticker、产品型号、数字口径这类专有名词由这一路兜住。
3. **RRF 融合**（k=60），chunk 级融合后按 doc 聚合——每篇文档只取最高分 chunk 占一个结果位，长文档不会挤占多个 top-k 名额。
4. **Recency decay** — 指数衰减，`config.json` 的 `search.recency_half_life_days`（默认 540 天）与 `search.recency_floor`（默认 0.5，衰减下限）。`--no-decay` 关闭。
5. **Chunk context header** — 入库/重嵌时每个 chunk 前拼元数据头 `【公司/行业 | content type | 日期 | 标题】`，保证孤立段落也能按归属召回（dense 与 FTS 双路都受益）。

首次运行任何命令会自动迁移：建 `chunks_fts` 表并 rebuild（秒级）。SQLite < 3.34 无 trigram 时自动降级为 dense-only，不报错。存量库的 chunk 元数据头需要跑一次 `reindex --embed-only` 才生效。

结果字段：`matched`（`dense`/`keyword`，命中的召回路）、`score`（融合+衰减后）、`dense_score`（原始余弦，若该 chunk 在 dense 路召回）。

## Auto-Tagging

When `tagger.enabled` is `true` in config.json, new documents are automatically tagged:
- **Model**: Gemini 4 Hydrogen Low through the agy CLI (`provider: "agy"`, `agy_model: "gemini-4-hydrogen-low"`), billed to the Antigravity subscription quota. Any agy failure falls back to the Gemini API (`model`, default `google/gemini-4-hydrogen`) and logs `[auto-tagger] agy ... failed` to stderr. `provider: "gemini-api"` skips agy.
- **agy safety**: document text is untrusted. agy runs as the tool-less agent `wikitagger` (source `scripts/agy_wikitagger_agent.md`, checked and re-synced to `~/.gemini/config/agents/` before each call) inside `scripts/tagger_sandbox.sb`, with a scrubbed env and a fresh empty cwd per call under `~/.local/state/wiki-tagger/agy/cwd/`. The sandbox lets only agy and its bundled helpers execute (no shell, curl, python), connect only to the local proxy plus the DNS and syslog sockets, read only `~/.gemini` and this call's cwd in home, and write only agy's runtime data, this call's cwd and temp dirs. The agent file may use only the keys name, description, tools, excludeDefaultComponents, mainAgent, subagent. The reply is discarded (→ API fallback) if the agent was not applied, any tool step or unknown output line appeared, agy exited non-zero, or files appeared in the cwd; such a cwd is moved to `~/.local/state/wiki-tagger/agy/quarantine/`. Do not add `--dangerously-skip-permissions`, `--json-schema`, or tools to that agent.
- **Tags**: 3-8 Chinese topic tags (English only for proper nouns, tickers, standard abbreviations); the prompt carries no example tags and bans generic labels such as 行业分析
- **Context**: the filing company/sector and content type are passed to the model; for a company, so are the sectors its other documents carry, so new tags follow the library convention
- **Related**: Cross-references to ALL companies and sectors substantively discussed (including the primary subject). Sector ids not in `config.json` `sectors` (or the filing sector) are mapped through sector aliases or dropped
- **Tests**: `venv/bin/python scripts/test_auto_tagger.py` (no network)
- **Input**: Up to 30000 chars (configurable via `max_input_chars`). For documents exceeding the limit, uses smart truncation (60% head + 20% middle + 20% tail) to ensure all sections are sampled
- Tags are stored in YAML frontmatter in the file and in the SQLite DB
- Rate limited (default 30 RPM) with exponential backoff on 429s

## Commands

### `add --company <name> --type <content_type> [--date YYYY-MM-DD] --content <text_or_file> [--source <name>] [--source-url <url>]`
Save content to the wiki under a company. Auto-generates filename, auto-tags, updates index, embeds for search. `--source`（来源名：公众号/券商/会议名等）和 `--source-url`（原文链接）写入 frontmatter；微信文章入库时应带上原链接。

```bash
bash skills/company-wiki/run.sh add --company "futu" --type "管理层会议纪要" --content @/path/to/notes.md
bash skills/company-wiki/run.sh add --company "futu" --type "管理层会议纪要" --content "会议要点：1. ..."
bash skills/company-wiki/run.sh add --company "HOOD" --type "微信文章" --source "明亮公司" --source-url "https://mp.weixin.qq.com/s/xxx" --content @temp/article.md
```

### `add --sector <name> --type <content_type> [--date YYYY-MM-DD] --content <text_or_file>`
Save content under an industry sector instead of a specific company.

```bash
bash skills/company-wiki/run.sh add --sector "china-internet" --type "行业趋势" --content "..."
```

### `add-file --company/--sector <name> --type <content_type> --file <path> [--date YYYY-MM-DD] [--ocr] [--no-keep-original] [--source <name>] [--source-url <url>]`
Add content from a file. Auto-detects format and extracts text. 原件默认复制到同目录的 `originals/` 子文件夹，命名与正文一致（`YYYY-MM-DD-<type_slug>.<ext>`）；只有传 `--no-keep-original` 才不留原件。`--keep-original` 仍然接受，等同默认行为。

Supported formats:
- **.md / .txt** — Plain text, direct import
- **.pdf** — 提取正文，另提取内嵌位图；有较多矢量绘图的页面生成预览入图片索引。扫描件可先保存图片索引，无需预先 OCR。
- **.docx** — 提取正文和表格，另索引内嵌 PNG/JPEG/GIF/WebP 图片。
- **.jpg / .png / .webp / .gif** — 默认保留原图、直接做图片 embedding；Markdown 保存来源说明，不预先识别全文。需要入库时提取文字，显式加 `--ocr`。

新增图片任务保存在 `state/research-images/queue/`，每次入库做有界处理，失败留队；`image_ingestion` 标明结果。`--no-keep-original` 同时退出图片归档，图片走原来的文字提取流程。旧文本、OCR 结果和原件不迁移；历史图片未全量回填，`import-flomo` 的旧 OCR 流程保留。纯 `add --content` 不自动下载正文里的图片链接。

引用图中数字前必须 read/view 原图；需要文字检索补充时按需执行 `bash scripts/research_images.sh describe --document image:<id>`，结果缓存，并仍须核对精确数字。维护、覆盖边界和重试见 [图片维护说明](../../references/research-images.md)。

```bash
bash skills/company-wiki/run.sh add-file --company "futu" --type "管理层会议纪要" --file ~/Downloads/futu_meeting.pdf
bash skills/company-wiki/run.sh add-file --sector "china-internet" --type "行业数据" --file ~/Desktop/screenshot.png
bash skills/company-wiki/run.sh add-file --company "alibaba" --type "研报摘要" --file ~/docs/baba_report.docx
```

### `search --query "..." [--company <name>] [--sector <name>] [--date-from/--date-to] [--top-k N] [--tag <tag>] [--dense-only] [--no-decay]`
Hybrid search（语义 + 关键词，见 Search Architecture）across all wiki content. Optionally filter by company, sector, date range, or tag. `--top-k` 现在按**文档**计数。含精确专有名词的查询（ticker、型号、数字口径）建议在 query 里用空格把关键词分开，让 keyword 路精确命中。`--company/--sector` 过滤支持 alias 和大小写不敏感解析（`--company unitree` 等价 `UNITREE`）。

- `--dense-only` — 只走语义路（调试/对比用）
- `--no-decay` — 关闭时效衰减（查历史专题时使用）

```bash
bash skills/company-wiki/run.sh search --query "富途用户增长策略"
bash skills/company-wiki/run.sh search --query "宇树 出货量 IPO"
bash skills/company-wiki/run.sh search --query "跨境电商竞争" --tag "跨境电商" --no-decay
bash skills/company-wiki/run.sh --json search --query "管理层对AI的看法" --top-k 20 --tag "AI变现"
```

### `list [--company <name>] [--sector <name>]`
List all companies/sectors, or list documents under a specific one.

```bash
bash skills/company-wiki/run.sh list
bash skills/company-wiki/run.sh list --company futu
bash skills/company-wiki/run.sh list --sector china-internet
```

### `read --company <name> --file <filename>` / `read --sector <name> --file <filename>`
Read a specific document.

```bash
bash skills/company-wiki/run.sh read --company futu --file "2026-03-13-管理层会议纪要.md"
```

### `links --company <name>` / `links --sector <name>`
Show bidirectional cross-references:
- **Forward**: what other companies/sectors does this entity's documents mention?
- **Backward**: which other companies' documents mention this entity?

```bash
bash skills/company-wiki/run.sh --json links --company PDD
bash skills/company-wiki/run.sh links --sector china-internet
```

### `topic <topic_name>`
Find all documents tagged with a given topic, grouped by company/sector, sorted by date.

```bash
bash skills/company-wiki/run.sh topic "跨境电商"
bash skills/company-wiki/run.sh --json topic "AI变现"
bash skills/company-wiki/run.sh topic "竞争格局"
```

### `reindex [--tags-only | --embed-only] [--company <name> | --sector <name>]`
Batch process existing documents:
- Reads each document file, strips existing frontmatter
- Default mode: auto-tag + re-embed; rebuilds frontmatter (**preserves** `source`/`source_url`)
- `--tags-only` skips re-embedding (much faster, only updates tags/related)
- `--embed-only` skips auto-tagging entirely（零 LLM 调用）：只重新 chunk + 拼元数据头 + 重嵌，**不改动文件**。chunking/context 逻辑变更后用这个刷新全库
- `--company` / `--sector` limits the run to one category (targeted re-embed)
- On error for a single doc, logs and continues (doesn't abort)

```bash
bash skills/company-wiki/run.sh reindex --tags-only              # re-tag only
bash skills/company-wiki/run.sh reindex --embed-only              # re-embed all, no LLM tagging
bash skills/company-wiki/run.sh reindex --embed-only --company UNITREE
bash skills/company-wiki/run.sh reindex                           # full re-embed + re-tag
```

### `index-missing [--dry-run]`
Incrementally index on-disk documents that exist under `wiki/` but are missing from the vector DB (e.g. after manual file moves or partial imports). Much cheaper than a full `reindex`: only untracked files are tagged and embedded.

- Filename pattern `YYYY-MM-DD-<type>[-N].md` supplies date and content type; non-matching files fall back to file mtime and stem
- Auto-tags (when tagger enabled), embeds, writes YAML frontmatter, updates the affected `index.md` files
- `--dry-run` lists missing files without indexing

```bash
bash skills/company-wiki/run.sh index-missing --dry-run   # list untracked files
bash skills/company-wiki/run.sh index-missing              # index them
bash skills/company-wiki/run.sh --json index-missing
```

### `graph [--company <name>] [--format json|text]`
Generate a cross-reference relationship graph:
- Nodes: companies and sectors
- Edges: weighted by number of cross-references
- `--company PDD` shows only PDD's neighborhood
- `--format json` outputs structured nodes + edges for visualization

```bash
bash skills/company-wiki/run.sh graph
bash skills/company-wiki/run.sh graph --company PDD
bash skills/company-wiki/run.sh --json graph --format json
bash skills/company-wiki/run.sh graph --company BABA --format json
```

### `import-flomo [--html PATH] [--images-dir PATH] [--dry-run]`
Import a Flomo HTML export into the wiki. Parses all memos, maps hierarchical tags (e.g. `#China/电商/PDD`) to companies/sectors, merges same-date memos for the same target, deduplicates against existing wiki docs, and imports with auto-tagging + embedding.

- Automatically cleans ghost records (DB entries without local files) before importing
- Images in memos are OCR'd via Gemini Vision and appended to content
- Idempotent: re-running won't create duplicates (trigram similarity check)
- `--dry-run` shows the full import plan without writing anything

```bash
bash skills/company-wiki/run.sh import-flomo --dry-run                      # preview what would be imported
bash skills/company-wiki/run.sh import-flomo                                # full import
bash skills/company-wiki/run.sh import-flomo --html ~/Downloads/export.html  # custom HTML path
bash skills/company-wiki/run.sh --json import-flomo                          # JSON summary output
```

### `cleanup [--dry-run]`
Remove ghost records from the SQLite DB — entries where the referenced local file no longer exists on disk. Useful after manual file deletions or reorganization.

```bash
bash skills/company-wiki/run.sh cleanup --dry-run   # preview what would be removed
bash skills/company-wiki/run.sh cleanup              # actually remove ghost records
bash skills/company-wiki/run.sh --json cleanup       # JSON output
```

### `self-test`
Verify Gemini API key, embedding connectivity, vector DB, and FTS5 keyword index availability (`fts_index` / `fts_chunks` fields).

```bash
bash skills/company-wiki/run.sh self-test
bash skills/company-wiki/run.sh --json self-test
```

## Agent Integration

When user says "加到wiki" or similar:
1. Extract company name (or determine if it's sector-level content)
2. Determine content type from context
3. Call `add` with appropriate parameters
4. Confirm what was saved and where (including auto-generated tags if available)

When user says "搜wiki" or asks about previously saved content:
1. Call `search` with the query
2. Use `--tag` for topic-focused searches when appropriate
3. Present results with source attribution

When user asks "PDD 和哪些公司有关联?" or cross-reference questions:
1. Call `links --company PDD` to show bidirectional references
2. Use `graph --company PDD --format json` for visualization data

When user asks about a topic across companies:
1. Call `topic "跨境电商"` to find all tagged documents
2. Present grouped results by company/sector

## Configuration

`config.json` in skill directory. Key sections:

```json
{
  "tagger": {
    "provider": "agy",
    "agy_model": "gemini-4-hydrogen-low",
    "model": "google/gemini-4-hydrogen",
    "enabled": true,
    "rate_limit_rpm": 30,
    "max_input_chars": 30000,
    "agy_timeout_sec": 180,
    "proxy_port": 7897
  },
  "embedding": {
    "model": "gemini-embedding-001",
    "model_preferred": "gemini-embedding-2-preview",
    "dimensions": 3072,
    "chunk_size": 1500,
    "chunk_overlap": 200
  },
  "aliases": { ... },
  "sectors": [ ... ]
}
```

## Dependencies
- `google-genai>=1.0.0` — Embedding + vision + auto-tagging API
- `numpy>=1.24.0` — Cosine similarity
- `pymupdf>=1.24.0` — PDF text extraction
- `python-docx>=1.0.0` — Word document extraction
- `Pillow>=10.0.0` — Image handling
- `GEMINI_API_KEY` environment variable
