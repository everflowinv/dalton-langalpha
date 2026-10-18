# Dalton Data Plugin (`dalton_data`)

Provides Dalton investment research MCP servers for LangAlpha:

1. **`researchdata`** (`transport: "http"`, `call_timeout_s: 300`):
   Connects to the host-side `researchdata_bridge.py` (`127.0.0.1:8960`), which wraps `~/.openclaw/workspace/scripts/researchdata_mcp.py` over Streamable HTTP JSON-RPC (`POST /mcp/<DALTON_BRIDGE_TOKEN>`).
   - Filters out OpenClaw-only `agent_tool` / `skill` handoff routes (`web-news`, `web-page`, `filings`, `last30days`, `agent-reach`, `x-timeline`, `x-search`, `xueqiu`, `employee`).
   - Appends `_declared_sources` to every `tools/call` payload so LangAlpha's Provenance middleware emits structured `sec_filing`, `research_doc`, and `data_api` source cards.

2. **`dalton_sec`** (`stdio` via `dalton_sec_mcp_server.py`):
   Self-contained SEC EDGAR XBRL facts server with deterministic period normalization (`get_company_facts` and `list_filings`), supporting calendar/fiscal quarters, annual, YTD (`H1`/`9M`), derived Q4 (`FY - 9M` for flow concepts), trailing series, and restatement selection (`as_reported` vs `latest`).
