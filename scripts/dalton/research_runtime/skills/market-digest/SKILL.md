---
name: "market-digest"
description: "卖方邮件摘要与语义搜索（GS/Jefferies/BofA）。触发词：market digest、卖方邮件、sales email、邮件摘要、搜邮件、拉邮件。"
user-invocable: true
---

# Market Digest Skill

## 统一只读入口

主会话查原邮件优先用 `researchdata.research(route="sales-notes", operation="search", query="...", start="YYYY-MM-DD", end="YYYY-MM-DD")`；TMTB 用 `route="tmtb"`，FUNDA AI 用 `route="funda-ai"`；`route="all"` 联搜 wiki、原邮件、TMTB 与 FUNDA AI。`sales-notes` 默认只查原邮件且排除 TMTB，FUNDA AI 单独存库；摘要需显式传 `params.layer="digest"` 或 `"all"`，摘要不算第二份独立证据。

新路由按文档返回，使用 `document_id` 调 `operation="read", document="..."`，正文按 `params.offset/max_chars` 分页。邮件/TMTB 读取全部已存分块，但历史导入可能截断；原始邮件完整性不能由缓存推定。详见 workspace 的 `references/data-sources.md`。

旧搜索名称已从模型目录收起，只兼容转发新版文档检索；新调用统一用下文 researchdata／data_gateway；`digest/index/backfill` 和 TMTB 同步脚本及 cron 不变。统一检索不会抓新邮件、打 Gmail 标签、回填或重新生成摘要。

图片同样从新路由检索：`params.media="images"` 只搜图，默认 `all` 搜图文，`text` 仅正文。用 `read image:<id>` 获取实际图片和原文 ID；读邮件/TMTB 正文时，`images` 会列出关联图片。引用图中数字前须读图核对，不能从 embedding 分数推导数值。旧名称调用现在同样返回 `document_id/snippet`，通过 read 取正文，不再返回旧分块 JSON。

`digest` 在打 Gmail 标签前持久保存图片任务，正文结果写好后再处理图片；TMTB 既有同步命令会保存原 HTML/PDF 并索引其中图片。图片失败留队，不抹掉正文；上游额度耗尽时暂停 TMTB 图片抓取。无需新增 cron 或改调用路径；新增耗时有界，检查 `image_ingestion`，不为图片重跑 digest。维护与覆盖边界见 [图片维护说明](../../references/research-images.md)。

## Overview

Automated pipeline for investment bank sales email processing:
1. **Fetch** — Pull unprocessed sales emails from Gmail (GS, Jefferies, BofA)
2. **Digest** — Output structured email data for AI analysis
3. **Index** — Embed digest sections into vector DB
4. **Search** — 统一入口检索原邮件；显式 layer=all 时加入历史日报摘要
5. **Backfill** — Bulk-index historical emails from MBOX export

## Architecture: Dual-Layer Indexing

| Layer | Table | Content | Granularity |
|-------|-------|---------|-------------|
| Layer 1 | `email_chunks` | Raw email full-text chunks | ~1500 chars/chunk, fine-grained |
| Layer 2 | `chunks` | Digest section summaries | Coarse-grained, AI-synthesized |

底库仍保留两层。新版默认只查原文；`sales-notes` 或联搜 `all` 传 `params.layer="all"` 时加入日报摘要。原文按文档去重；摘要带 `derived_from` 与 `independent_evidence=false`，不能充当第二份独立证据。

## Embedding Model

- **Model**: 以 `config.json` 为准；当前文本配置为 `gemini-embedding-2-preview`（3072 维，同时是缓存与索引标签）；2026-11-19 该预览名下线，调用接口时映射为正式版 `gemini-embedding-2`，向量相同。新增图片独立索引固定同型号、同维度，不与旧文本向量直接混排或静默降为 `001`。
- **SDK**: `google-genai>=1.0.0` (new SDK, not deprecated `google.generativeai`)
- **Batch embedding**: True batch calls (100 texts per API call, not 100 individual calls)
- **Rate limits**: Respects RPM/TPM with exponential backoff on 429

## Commands

### `digest --period AM|PM`
Fetch new sales emails since last scan. Returns email bodies + metadata as JSON. Auto-indexes raw emails into Layer 1.

```bash
bash skills/market-digest/run.sh digest --period AM
bash skills/market-digest/run.sh --json digest --period PM
```

#### ⚠️ 抓取会消耗邮件：每次 run 只跑一次

抓取成功后会给这批邮件打上 `📊 Sales/Research` 标签，Gmail 之后不再返回它们。因此：

- 调用方（cron agent 或人）在一次任务里**最多执行一次** `digest`。
- 正文入向量库通常耗时 30–90 秒；sales notes 与 FUNDA AI 图片队列各有 120 秒处理预算，单次网络调用可能稍超预算。**不要因为"看起来没反应"而重发**。
- 怀疑上一次没成功时，不要重跑命令，直接看 `skills/market-digest/output/digest_<date>_<period>.json` 是否已存在。

命令本身也做了兜底：同一 `date + period` 的输出文件存在时，新抓到的邮件会**并入**该文件（按 Gmail message id 去重，旧的在前），不会覆盖。上一次跑到一半死掉时，那批已被标记的邮件仍然留在文件里。

#### `digest` 返回的 status

| status | 含义 | 调用方该做什么 |
|--------|------|----------------|
| `emails_fetched` | 抓到新邮件；文件已写。`count` 是并入后的总数，`new_count` / `carried_over_count` 区分本次新抓和上次遗留 | 读 `output_file`，写摘要 |
| `reused_existing` | 本次没有新邮件，但同一时段的文件里有上次遗留的邮件 | 读 `output_file`，**补完**上次没写完的摘要 |
| `no_new_emails` | 没有新邮件，也没有遗留文件 | 真的没东西可写，输出一行状态后结束 |

`reused_existing` 和 `no_new_emails` 必须区别对待。把 `reused_existing` 当成"没邮件"会让已消耗的邮件永久丢失。

### 只读检索：统一 researchdata / data_gateway

```bash
# 只查销售原邮件
python3 scripts/data_gateway.py fetch sales-notes --query "momentum unwind rotation" --limit 10
# 四库图文联搜，显式带上历史日报摘要
python3 scripts/data_gateway.py fetch all --query "韩国存储涨价" --limit 20 --params-json '{"layer":"all","media":"all"}'
# 复现旧混搜的范围（邮件、TMTB、摘要），但返回新版文档结果
python3 scripts/data_gateway.py fetch all --query "半导体" --start 2025-01-01 --limit 20 --params-json '{"sources":["sales-notes","tmtb"],"layer":"all","media":"text"}'
# 按搜索返回的 ID 读全部已存正文；有 next_offset 就续读
python3 scripts/data_gateway.py fetch all --operation read --document 'mail:<返回ID>'
```

旧 `search` 子命令仅接住历史调用，内部使用同一新版引擎，返回文档级 JSON；没有单独的旧检索算法。关键词降级、来源失败、历史图片缺口以 `status/source_status` 为准。检索不执行抓取或入库。

### 手动入库：`add-note`

用户转发的卖方研报、点评、卖方电话会纪要、sales color 进 sales notes（分库标准见 workspace AGENTS.md「转发资料入库：先分 sales notes 还是 wiki」）。写入的是同一张 `email_chunks`，同一个 embedding 模型，所以 `route="sales-notes"` 和联搜 `all` 直接能搜到，`params.sender` 按机构名筛。

```bash
# PDF / DOCX：正文由 company-wiki 的 extract 提取（它的 venv 有 pymupdf / python-docx）
bash skills/market-digest/run.sh --json add-note --institution "Morgan Stanley" --analyst "Keith Weiss" \
  --title "ORCL｜3Q26 Results – ..." --kind 研报 --doc-date 2026-10-17 --tickers ORCL --file /path/report.pdf
# 自己整理的转写稿或截图读出的文字：--content 给正文，--file 给原件
bash skills/market-digest/run.sh --json add-note --institution "BofA Securities" --kind 卖方电话会 \
  --title "..." --content @temp/note.md --file /path/screenshot.png
# 先看分块和写入位置
bash skills/market-digest/run.sh --json add-note ... --dry-run
```

- `--date` 是收到日期，默认今天，写进库的 `date`，检索的 start/end 和 stock tracker 的时间窗都按它算；报告或会议本身的日期放 `--doc-date`，写进正文头和元数据。
- 每块正文前加 `From: 机构 · 分析师 / Subject: 标题 /【类型 | 报告日期 | 标的】` 抬头，tracker 的别名关键词检索能命中。
- `doc_key` 由正文哈希生成（纯图片按文件字节），以 `manual:` 开头；同一正文再跑一次返回 `duplicate`，不会重复写。
- 整篇正文和元数据存 `state/sales-notes/notes/YYYY/MM/`，原件存 `state/sales-notes/originals/`，清单是 `state/sales-notes/manifest.jsonl`。`read` 返回这份正文，不拼接分块。
- PDF 和图片原件会排进图片队列，和邮件图片一样用 `params.media="images"` 检索。单张图片默认只建图片向量、不识别文字；要文字检索就自己读图整理成 md 传 `--content`，或加 `--ocr`。
- 正文先落盘再算 embedding；embedding 失败时重跑同一条命令即可补索引。
- 从 wiki 迁来的 79 篇（2026-10-19）doc_key 是 `manual:wiki-<原 wiki id>`，`date` 沿用原 wiki 文档日期，正文在 `state/sales-notes/notes/wiki-migrated/`，`migrated_from` 记了原路径和标签。迁移脚本 `scripts/wiki_to_sales_notes.py` 直接复制分块和向量（两库同为 gemini-embedding-2-preview / 3072 维，已逐块比对），可用于以后再迁。

### `index --file path.md [--date YYYY-MM-DD] [--period AM|PM]`
Manually index a markdown digest file into Layer 2.

```bash
bash skills/market-digest/run.sh index --file market-memory/2026-03-09.md --period AM
```

### `backfill --file path.mbox [--all] [--dry-run] [--batch-size N] [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]`
Bulk-index historical emails from MBOX export into Layer 1.

```bash
bash skills/market-digest/run.sh --json --verbose backfill --file temp/export.mbox --all --batch-size 100
bash skills/market-digest/run.sh backfill --file temp/export.mbox --dry-run  # preview only
```

**Features:**
- True streaming MBOX parse (line-by-line, no OOM on large files)
- Message-ID dedup (safe to re-run)
- HTML fallback body extraction (text/plain → text/html with tag stripping)
- Oversized email truncation (>50K chars capped)
- Junk email filtering (no sender + no date = skip)
- `gc.collect()` every 100 messages for memory control

### `self-test`
Verify gws CLI, Gmail auth, Gemini API key, embedding connectivity, vector DB.

```bash
bash skills/market-digest/run.sh self-test
bash skills/market-digest/run.sh --json self-test
```

离线校验抓取合并逻辑（不连 Gmail、不发网络请求）：

```bash
skills/market-digest/venv/bin/python skills/market-digest/scripts/test_digest_idempotency.py
```

## 晨晚报输出格式

```markdown
# 📊 市场{晨报/晚报} — {日期}

## 🇺🇸 美股
### 大型科技与 AI
### 半导体
### 软件
### 金融
### 其他行业亮点

## 🇨🇳 中国/港股

## 🇰🇷 韩国

## 📌 宏观与地缘政治

## 🎯 会议与活动

## 📝 来源
```

每条先写发生了什么，再写影响和交易含义。没有证据时不补判断；不要用 Beat、raise、thesis、run-rate 充当中文谓语，也不要写中英双标题或空泛总结。标题、正文、归档和飞书卡片必须使用同一份校对后的 Markdown。校对只改语言和标点，不得改变既有章节、顺序、字段、抓取状态、恢复逻辑、验证规则和投递要求。

## Configuration
`config.json`: email domains, priority senders, output channel, embedding model, section definitions. Copy `config.example.json` to `config.json` and fill in your own values.

## Dependencies
- `gws` CLI (npm: `@googleworkspace/cli`)
- `GEMINI_API_KEY` environment variable
- Gmail OAuth scopes (via `gws auth login -s gmail`)

## Cron Integration (Example)
Schedule cron jobs to run digest automatically:
- **AM**: Mon-Fri 09:00 → `bash skills/market-digest/run.sh digest --period AM`
- **PM**: Mon-Fri 21:00 → `bash skills/market-digest/run.sh digest --period PM`

Cron prompt 里必须写清两件事：`digest` 一次 run 只能跑一次；无论走哪条路径，最后都要输出一行结束状态。空回复会被 runtime 记成任务失败，掩盖掉"邮件已消耗但摘要没写"的情况。

## FUNDA AI 邮件

- `digest` 同时抓取 `fundaai@substack.com` 的新邮件，先保存至独立 `state/funda-ai/index.sqlite` 和原件目录，再处理 embedding；不写入 sales notes 文本表。验证码、订阅和账单等非研究邮件不入研究索引、不进入日报。
- `mail_fetch.more_unfetched/failed_reads` 标出分页上限或读取失败；未取完不能写成没有更多邮件，后续继续抓未标签邮件。
- 输出 `emails[].source="funda-ai"`、`document_id` 和 `funda_ingestion`；`research_eligible=false` 的条目跳过。将研究邮件按主题并入既有栏目，引用末尾标「（FUNDA AI）」；来源清单列标题、日期。FUNDA AI 内容有增量就采用，不新设固定栏目，不把历史回填当成当日新稿。
- 正文和摘要文件保存成功后才打既有 Gmail 标签；embedding 失败保留待处理状态，下一次 digest 自动续跑；图片单独有界处理。日报结束状态分别说明 FUNDA AI 收到数、正文入库与图片队列状态，不把命令 exit 0 当作全部入库完成。
- 历史导入、验收与来源口径见 workspace 的 `references/funda-ai.md`。旧命令和 TMTB 路径保留。
