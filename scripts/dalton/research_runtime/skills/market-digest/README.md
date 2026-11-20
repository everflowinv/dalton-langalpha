# Market Digest

Sales email digest system with Gemini embedding semantic search. Fetches investment bank sales emails (GS, Jefferies, BofA, etc.), outputs structured market summaries, and maintains a searchable vector memory of all past digests.

## Features

- **Dual-layer indexing**: Raw email chunks (fine-grained) + AI-synthesized digest summaries (coarse-grained)
- **Semantic search**: Cross-language queries (e.g., Chinese query → English emails) via Gemini embeddings
- **MBOX backfill**: Bulk-index years of historical emails with streaming parser (no OOM)
- **Cron-ready**: Schedule AM/PM digest runs with any cron system

## Prerequisites

- [gws CLI](https://github.com/nicholasgasior/gws) — Google Workspace CLI
- Gmail OAuth: `gws auth login -s gmail`
- `GEMINI_API_KEY` environment variable

## Quick Start

```bash
# 1. Copy and configure
cp config.example.json config.json
# Edit config.json: set your Gmail label ID, output channel, priority senders, etc.

# 2. Verify setup
bash run.sh self-test

# 3. Fetch new emails
bash run.sh digest --period AM

# 4. Search your memory
bash run.sh search --query "semiconductor supply chain"

# 5. Backfill historical emails
bash run.sh backfill --file export.mbox --all --batch-size 100
```

## Configuration

See `config.example.json` for all available options:

- **email_sources.domains**: Email domains to monitor
- **email_sources.priority_senders**: Names to flag as high-priority
- **gmail.label_name / label_id**: Gmail label for marking processed emails
- **output.discord_channel_id**: Discord channel for digest output (optional)
- **embedding.model**: Gemini embedding model (`gemini-embedding-2-preview`, also the cache and index label; mapped to `gemini-embedding-2` at call time since the preview name was retired)
- **sections**: Customizable digest sections

## OpenClaw Skill

This is an [OpenClaw](https://github.com/openclaw/openclaw) skill. See `SKILL.md` for agent integration details.

## License

MIT
