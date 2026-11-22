# Dalton Data Plugin (`dalton_data`)

Provides Dalton investment research MCP servers for LangAlpha (configured in `plugins/dalton_data/mcp.json` and `plugins/dalton_data/plugin.json`, with Dalton runtime overrides in `agent_config.dalton.yaml`):

1. **`researchdata`** (`type: "streamable-http"`, `agent_config.dalton.yaml` `call_timeout_s: 625`):
   Connects to the host-side `scripts/dalton/researchdata_bridge.py` (`127.0.0.1:8960`, URL configured via `${DALTON_RESEARCHDATA_URL}`) over Streamable HTTP JSON-RPC (`POST /mcp`, authenticated via `Authorization: Bearer ${DALTON_BRIDGE_TOKEN}` or `X-Dalton-Bridge-Token`; legacy `POST /mcp/<token>` kept only for backwards compatibility).
   - Exposes routed research (`research`: `alphaengine`, `guidepoint`, `transcript`, `company-wiki`, `sales-notes`, `tmtb`, `funda-ai`, `all`), finance (`finance`: `sec`, `estimates`, `cn-hk`), macro (`macro`: `fred`, `treasury`, `market-regime`), community (`community`: `xueqiu`, `employee`, `last30days`, `x-timeline`), and `catalog` tools, plus AlphaEngine team SQLite WAL caching (v4 keys), singleflight coalescing, dual-quota accounting, and same-query/repeated-company soft hints (see [alphaengine-cache.md](../../docs/dalton/features/alphaengine-cache.md)).
   - Appends `_declared_sources` to `tools/call` payloads so Dalton Provenance middleware emits structured `sec_filing`, `research_doc`, and `data_api` source cards.

2. **`dalton_sec`** (`type: "stdio"` via `uv run python plugins/dalton_data/dalton_sec_mcp_server.py`):
   Self-contained SEC EDGAR XBRL facts server (`SEC_USER_AGENT`) with deterministic period normalization (`get_company_facts` and `list_filings`), supporting calendar/fiscal quarters, annual, YTD (`H1`/`9M`), derived Q4 (`FY - 9M` for flow concepts), trailing series, and restatement selection (`as_reported` vs `latest`).
