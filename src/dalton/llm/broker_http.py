"""Singleton httpx clients for OpenClaw Broker v2 with deterministic invocation IDs (P0.4 §6)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import httpx


def _compute_invocation_id(content: bytes) -> str:
    """Compute `la1:` + sha256(scope + '\\n' + request.content)[:40]."""
    scope: str | None = None
    try:
        from src.ptc_agent.agent.middleware.credit_gate import current_credit_gate

        gate = current_credit_gate.get()
        if gate is not None and getattr(gate, "run_ref", None):
            scope = str(gate.run_ref)
    except Exception:
        scope = None

    if not scope:
        scope = f"nogate:{uuid.uuid4()}"

    try:
        from src.ptc_agent.agent.middleware.background_subagent.context import (
            current_background_agent_id,
        )

        bg_id = current_background_agent_id.get()
        if bg_id:
            scope = f"{scope}:{bg_id}"
    except Exception:
        pass

    h = hashlib.sha256()
    h.update(scope.encode("utf-8"))
    h.update(b"\n")
    h.update(content or b"")
    return "la1:" + h.hexdigest()[:40]


def _inject_header_sync(request: httpx.Request) -> None:
    if request.method.upper() == "POST" and request.url.path.endswith("/chat/completions"):
        if "x-dalton-invocation-id" not in request.headers:
            request.headers["X-Dalton-Invocation-Id"] = _compute_invocation_id(request.content)


async def _inject_header_async(request: httpx.Request) -> None:
    if request.method.upper() == "POST" and request.url.path.endswith("/chat/completions"):
        if "x-dalton-invocation-id" not in request.headers:
            request.headers["X-Dalton-Invocation-Id"] = _compute_invocation_id(request.content)


@dataclass(frozen=True)
class BrokerHttpClients:
    async_client: httpx.AsyncClient
    sync_client: httpx.Client


_CLIENTS: BrokerHttpClients | None = None


def broker_http_clients() -> BrokerHttpClients:
    """Return process-wide singleton httpx clients with `X-Dalton-Invocation-Id` hook."""
    global _CLIENTS
    if _CLIENTS is None:
        timeout = httpx.Timeout(connect=10.0, read=660.0, write=30.0, pool=10.0)
        _CLIENTS = BrokerHttpClients(
            async_client=httpx.AsyncClient(
                timeout=timeout,
                event_hooks={"request": [_inject_header_async]},
            ),
            sync_client=httpx.Client(
                timeout=timeout,
                event_hooks={"request": [_inject_header_sync]},
            ),
        )
    return _CLIENTS
