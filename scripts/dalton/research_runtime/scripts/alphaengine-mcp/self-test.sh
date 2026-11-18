#!/usr/bin/env bash
set -euo pipefail

SERVICE_URL="${ALPHAENGINE_MCP_URL:-http://127.0.0.1:8950}"
WITH_API="${1:-}"

health="$(curl -fsS "$SERVICE_URL/health")"
jq -e '.ok == true and .service == "alphaengine-mcp" and .authenticated == true' \
  >/dev/null <<<"$health"

schema="$(mcporter list --http-url "$SERVICE_URL/mcp" --schema --json --allow-http)"
for tool in search_library get_document download_document list_filters auth_status get_document_info list_attachments list_recording_tasks get_recording_status get_recording_task; do
  jq -e --arg tool "$tool" '.tools | any(.name == $tool)' >/dev/null <<<"$schema"
done

categories="$(mcporter call --http-url "$SERVICE_URL/mcp" --tool list_filters \
  --args '{"filter_type":"categories"}' --output json --allow-http)"
jq -e '.categories | any(.id == "sell_side_comment")' >/dev/null <<<"$categories"

if [[ "$WITH_API" == "--with-api" ]]; then
  sources="$(mcporter call --http-url "$SERVICE_URL/mcp" --tool list_filters \
    --args '{"filter_type":"sources","query":"摩根士丹利","limit":10}' \
    --output json --allow-http)"
  jq -e '.sources | any(.institution_name == "Morgan Stanley")' >/dev/null <<<"$sources"
fi

echo "alphaengine-mcp self-test: PASS"
