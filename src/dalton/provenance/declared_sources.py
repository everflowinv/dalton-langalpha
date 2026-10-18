"""Expand `_declared_sources` / `_sources` from trusted Dalton MCP servers into ProvenanceSource records."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from ptc_agent.agent.provenance.types import ProvenanceSource, SNIPPET_MAX_CHARS

_ALLOWED_SERVERS: frozenset[str] = frozenset({"researchdata", "dalton_sec"})

_ALLOWED_SOURCE_TYPES: frozenset[str] = frozenset(
    {"sec_filing", "research_doc", "market_data", "data_api"}
)

_ALLOWED_IDENTITIES: frozenset[str] = frozenset(
    {
        "filing",
        "company",
        "broker_report",
        "official_data",
        "expert",
        "news",
        "internal_note",
        "derived",
        "street_estimate",
    }
)

_MAX_DECLARED_PER_CALL = 50
_IDENT_MAX_CHARS = 256
_DOC_ID_MAX_CHARS = 200
_TITLE_MAX_CHARS = 300
_PUBLISHER_MAX_CHARS = 120
_DETAIL_MAX_CHARS = 128
_PROVIDER_MAX_CHARS = 128

_DECLARED_KEY_RE = re.compile(r'"(?:_declared_sources|_sources)"\s*:\s*(\[)')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_raw_sources_list(body: str) -> list[Any]:
    """Parse `_declared_sources` or `_sources` from JSON body, tolerating 64 KiB body truncation."""
    text = body.lstrip()
    if not text.startswith("{"):
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            raw = parsed.get("_declared_sources")
            if not isinstance(raw, list):
                raw = parsed.get("_sources")
            if isinstance(raw, list):
                return raw
            # Also check one level deeper if wrapped in {"data": ...}
            inner = parsed.get("data")
            if isinstance(inner, dict):
                raw = inner.get("_declared_sources") or inner.get("_sources")
                if isinstance(raw, list):
                    return raw
        return []
    except Exception:
        # Fallback for bodies truncated mid-JSON after the opening `_declared_sources` / `_sources` array
        match = _DECLARED_KEY_RE.search(text)
        if not match:
            return []
        start_idx = match.start(1)
        try:
            arr, _ = json.JSONDecoder().raw_decode(text, start_idx)
            return arr if isinstance(arr, list) else []
        except Exception:
            return []


def expand_declared_sources(
    server: str,
    tool: str,
    body: str | None,
    tool_call_id: str | None = None,
    *,
    timestamp: str | None = None,
) -> list[ProvenanceSource]:
    """Expand `_declared_sources` / `_sources` from a whitelisted MCP server result body.

    Never raises: returns an empty list on non-whitelisted servers, missing/empty
    body, or malformed/truncated JSON.
    """
    try:
        if not server or server not in _ALLOWED_SERVERS:
            return []
        if not body or not isinstance(body, str):
            return []

        raw_list = _extract_raw_sources_list(body)
        if not raw_list:
            return []

        ts = timestamp if isinstance(timestamp, str) and timestamp else _now_iso()
        out: list[ProvenanceSource] = []

        for item in raw_list[:_MAX_DECLARED_PER_CALL]:
            if not isinstance(item, dict):
                continue

            # Validate identity if present
            raw_identity = item.get("identity")
            if raw_identity is not None:
                if not isinstance(raw_identity, str) or raw_identity not in _ALLOWED_IDENTITIES:
                    continue

            # Validate source_type if present; default to "research_doc"
            raw_st = item.get("source_type")
            if raw_st is not None:
                if not isinstance(raw_st, str) or raw_st not in _ALLOWED_SOURCE_TYPES:
                    continue
                source_type = raw_st
            else:
                source_type = "research_doc"

            # Determine identifier (prefer identifier, then document_id, then url)
            raw_ident = item.get("identifier")
            raw_doc_id = item.get("document_id")
            raw_url = item.get("url")
            if isinstance(raw_ident, str) and raw_ident.strip():
                identifier = raw_ident.strip()[:_IDENT_MAX_CHARS]
            elif isinstance(raw_doc_id, str) and raw_doc_id.strip():
                identifier = raw_doc_id.strip()[:_DOC_ID_MAX_CHARS]
            elif isinstance(raw_url, str) and raw_url.strip():
                identifier = raw_url.strip()[:_IDENT_MAX_CHARS]
            else:
                continue

            # Title & as_of
            raw_title = item.get("title")
            title_str = str(raw_title).strip()[:_TITLE_MAX_CHARS] if raw_title else ""
            raw_as_of = item.get("as_of")
            as_of_str = str(raw_as_of).strip()[:64] if raw_as_of else ""

            if title_str and as_of_str and f"· {as_of_str}" not in title_str and raw_identity:
                final_title: str | None = f"{title_str} · {as_of_str}"[:_TITLE_MAX_CHARS]
            elif title_str:
                final_title = title_str
            elif as_of_str:
                final_title = as_of_str
            else:
                final_title = None

            # Detail (identity or explicit detail)
            raw_detail = item.get("detail") or raw_identity
            detail: str | None = (
                str(raw_detail).strip()[:_DETAIL_MAX_CHARS] if raw_detail else None
            )

            # Provider
            raw_provider = item.get("provider")
            if isinstance(raw_provider, str) and raw_provider.strip():
                provider = raw_provider.strip()[:_PROVIDER_MAX_CHARS]
            else:
                route = str(item.get("route") or server).strip()[:60]
                publisher = str(item.get("publisher") or "").strip()[:_PUBLISHER_MAX_CHARS]
                if publisher:
                    provider = f"{route}:{publisher}"[:_PROVIDER_MAX_CHARS]
                else:
                    provider = f"mcp:{server}"[:_PROVIDER_MAX_CHARS]

            # Optional snippet
            raw_snippet = item.get("snippet")
            snippet: str | None = (
                str(raw_snippet)[:SNIPPET_MAX_CHARS]
                if isinstance(raw_snippet, str) and raw_snippet
                else None
            )

            out.append(
                ProvenanceSource(
                    record_id=str(uuid.uuid4()),
                    source_type=source_type,
                    identifier=identifier,
                    timestamp=ts,
                    title=final_title,
                    detail=detail,
                    provider=provider,
                    tool_call_id=tool_call_id,
                    result_snippet=snippet,
                )
            )

        return out
    except Exception:
        return []
