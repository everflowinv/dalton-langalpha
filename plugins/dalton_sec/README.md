# Dalton SEC Plugin (`dalton-sec`)

Standalone Apache-2.0 Agent Plugins 1.0.0 package providing public SEC EDGAR XBRL facts and filing discovery for LangAlpha / Dalton (enabled in both Standalone Core and Everflow deployments).

- **`dalton_sec`** (`type: "stdio"` via `uv run python plugins/dalton_sec/dalton_sec_mcp_server.py`):
  Self-contained SEC EDGAR XBRL facts server (`SEC_USER_AGENT`) with deterministic period normalization (`get_company_facts` and `list_filings`), supporting calendar/fiscal quarters, annual, YTD (`H1`/`9M`), derived Q4 (`FY - 9M` for flow concepts), trailing series, and restatement selection (`as_reported` vs `latest`).
