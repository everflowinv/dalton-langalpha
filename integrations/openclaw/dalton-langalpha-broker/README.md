# Dalton LangAlpha Model Broker v2 (`dalton-langalpha-broker`)

Loopback-only OpenAI-compatible HTTP broker plugin for OpenClaw (`>=2026.10.4`), mounted on the gateway HTTP server under `/dalton-broker/v1/`.

## Endpoints

- `GET /dalton-broker/v1/health` — Loopback health probe (no bearer required from loopback)
- `GET /dalton-broker/v1/models` (`?detail=1`) — Enumerates configured models from `openclaw.json` with capability & auth-readiness metadata
- `POST /dalton-broker/v1/chat/completions` — OpenAI Chat Completions compatible endpoint (`stream: false` and `stream: true` SSE) with tool calling, multi-turn `tool` results, reasoning/thinking blocks, `response_format`, and `dalton_meta` cost/usage/latency telemetry
- `POST /dalton-broker/v1/invoke` — Backward-compatible v1 Dalton native invocation endpoint
- `POST /dalton-broker/v1/x-search` — Loopback-only X (Twitter) search endpoint backed by OpenClaw's built-in `x_search` tool (`extensions/xai/x-search.js`) and runtime xAI OAuth/API-key resolution (`api.runtime.modelAuth`), authenticated with the same scoped `<stateDir>/broker.token` Bearer token

## Security Invariants

1. **Loopback-only (`127.0.0.1` / `::1`)**: Non-loopback requests and requests carrying proxy headers (`Forwarded`, `X-Forwarded-*`, `X-Real-IP`, `CF-Connecting-IP`, `True-Client-IP`, `Via`) are rejected with `403 loop_only`.
2. **Scoped Bearer Token**: Stored at `<stateDir>/broker.token` (`0600`), verified with `crypto.timingSafeEqual`.
3. **Zero Credential Exfiltration**: Provider API keys, OAuth tokens, and auth headers never leave the OpenClaw gateway process or appear in journals.
