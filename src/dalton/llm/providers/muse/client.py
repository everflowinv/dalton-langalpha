"""Dalton-owned Meta Muse client builder (P0N.2, G5.2 R6-10)."""

from __future__ import annotations

import uuid
from typing import Any


def build_muse_client(llm: Any, cache_key: str | None = None):
    """Build Muse Code (Meta subscription) LLM client using ``ChatCodexOpenAI``.

    Receives the raw ``cache_key`` (fixing R6-10 where ``effective_cache_key``
    was None for ``sdk == 'muse'``), deriving stable session-affinity headers
    ``x-tbh-session-id`` and ``x-meta-ai-gateway-session-id``.
    """
    from src.llms.extension import ChatCodexOpenAI
    from src.llms.llm import STREAM_CHUNK_TIMEOUT_S, _merged_default_headers

    params: dict[str, Any] = {
        "model": llm.model,
        "api_key": llm._resolve_api_key(),
        "streaming": True,
        "stream_usage": True,
        "max_retries": 5,
        "timeout": 600.0,
        "stream_chunk_timeout": STREAM_CHUNK_TIMEOUT_S,
    }
    params.update(llm._resolve_base_url("base_url"))

    if llm.use_response_api:
        params["output_version"] = "responses/v1"

    params.update(llm.parameters)

    params["default_headers"] = _merged_default_headers(
        params,
        {
            "User-Agent": "muse-build/1.4.10 (non-interactive; macos-aarch64; build 98c762468d5746a17e31a05cc923592d4e6124d3)",
            "x-client-id": "tbh:exec",
            "x-tbh-agent-role": "main",
            "x-tbh-contributor-mode": "1" if "-contributor" in llm.model else "0",
        },
        llm.default_headers,
    )

    if cache_key:
        session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"muse-session:{cache_key}"))
        present = {k.lower() for k in params["default_headers"]}
        for header in ("x-tbh-session-id", "x-meta-ai-gateway-session-id"):
            if header not in present:
                params["default_headers"][header] = session_id

    if llm.extra_body:
        params["extra_body"] = llm.extra_body

    return ChatCodexOpenAI(**params)
