"""Everflow host-side commercial connector bundle (excluded from Standalone Core release).

Loaded by `dalton-research-host` when `DALTON_RESEARCH_HOST_EXTRA_MODULES=host_connectors`
with `DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH=<repo>/scripts/dalton/research_runtime`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from src.dalton.capabilities import CapabilityId, effective_capabilities
from src.dalton.research_host.connector_registry import ConnectorRegistry

from .guidepoint import (
    GuidepointConnector,
    GuidepointTokenManager,
    resolve_guidepoint_tokens_file,
)
from .library_connectors import (
    FundaAiConnector,
    MarketDigestPartitionStore,
    SalesNotesConnector,
    TmtbConnector,
)


def register(
    registry: ConnectorRegistry,
    config: Any,
    *,
    active_caps: frozenset[CapabilityId] | None = None,
) -> None:
    """Register enabled commercial connectors (`guidepoint`, `sales-notes`, `tmtb`, `funda-ai`).

    Checks each connector's capability BEFORE initializing managers or acquiring exclusive locks
    so disabled connectors never register routes, never probe health, and never acquire locks.
    """
    data_root = Path(config.data_root)
    code_root = getattr(config, "code_root", None)
    caps = (
        effective_capabilities(profile=getattr(config, "profile", None))
        if active_caps is None
        else frozenset(active_caps)
    )

    # 1. Guidepoint (single exclusive refresh owner lock acquired ONLY when capability is enabled)
    if CapabilityId.GUIDEPOINT in caps:
        gp_token_file = resolve_guidepoint_tokens_file(data_root=data_root)
        gp_manager = GuidepointTokenManager(
            gp_token_file,
            client_id=os.environ.get("GUIDEPOINT_CLIENT_ID", ""),
            client_secret=os.environ.get("GUIDEPOINT_CLIENT_SECRET", ""),
        )
        gp_manager.acquire_owner_lock(timeout_s=2.0)
        gp_conn = GuidepointConnector(gp_manager)
        registry.register_connector(gp_conn)
        registry.register_health_probe("guidepoint", gp_manager.health)
        registry.register_shutdown_hook(gp_manager.release_owner_lock)

        async def _gp_search_leg(
            query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], caller_key: str
        ) -> dict[str, Any]:
            return await gp_conn.call_tool(
                "search_library",
                {"query": query, "start": start, "end": end, "limit": min(limit, 30), "params": params},
                active_caps=frozenset({CapabilityId.GUIDEPOINT}),
                caller_key=caller_key,
            )

        registry.register_federated_source("guidepoint", CapabilityId.GUIDEPOINT, search_fn=_gp_search_leg)

    # 2. Sales Notes & TMTB (SQL-partitioned shared market-digest DB)
    if CapabilityId.SALES_NOTES in caps or CapabilityId.TMTB in caps:
        md_store = MarketDigestPartitionStore(data_root, code_root=code_root)
        if CapabilityId.SALES_NOTES in caps:
            sn_conn = SalesNotesConnector(md_store)
            registry.register_connector(sn_conn)
            registry.register_ingest("sales-notes", CapabilityId.SALES_NOTES, md_store.ingest_sales_note)

            async def _sn_search(query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _c: str) -> dict[str, Any]:
                return await asyncio.to_thread(
                    md_store.query, "sales-notes", "search", query=query, start=start, end=end, limit=limit, params=params
                )

            async def _sn_list(query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _c: str) -> dict[str, Any]:
                return await asyncio.to_thread(
                    md_store.query, "sales-notes", "list", query=query or None, start=start, end=end, limit=limit, params=params
                )

            async def _sn_read(document: str, params: dict[str, Any]) -> dict[str, Any]:
                return await asyncio.to_thread(md_store.read, "sales-notes", document, params)

            registry.register_federated_source("sales-notes", CapabilityId.SALES_NOTES, search_fn=_sn_search, list_fn=_sn_list)
            registry.register_prefix_reader("mail", CapabilityId.SALES_NOTES, _sn_read)
            registry.register_prefix_reader("digest", CapabilityId.SALES_NOTES, _sn_read)

        if CapabilityId.TMTB in caps:
            tmtb_conn = TmtbConnector(md_store)
            registry.register_connector(tmtb_conn)

            async def _tmtb_search(query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _c: str) -> dict[str, Any]:
                return await asyncio.to_thread(
                    md_store.query, "tmtb", "search", query=query, start=start, end=end, limit=limit, params=params
                )

            async def _tmtb_list(query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _c: str) -> dict[str, Any]:
                return await asyncio.to_thread(
                    md_store.query, "tmtb", "list", query=query or None, start=start, end=end, limit=limit, params=params
                )

            async def _tmtb_read(document: str, params: dict[str, Any]) -> dict[str, Any]:
                return await asyncio.to_thread(md_store.read, "tmtb", document, params)

            registry.register_federated_source("tmtb", CapabilityId.TMTB, search_fn=_tmtb_search, list_fn=_tmtb_list)
            registry.register_prefix_reader("tmtb", CapabilityId.TMTB, _tmtb_read)

    # 3. FUNDA AI
    if CapabilityId.FUNDA_AI in caps:
        funda_conn = FundaAiConnector(data_root)
        registry.register_connector(funda_conn)

        async def _funda_search(query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _c: str) -> dict[str, Any]:
            return await asyncio.to_thread(
                funda_conn.query, "search", query=query, start=start, end=end, limit=limit, params=params
            )

        async def _funda_list(query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _c: str) -> dict[str, Any]:
            return await asyncio.to_thread(
                funda_conn.query, "list", query=query or None, start=start, end=end, limit=limit, params=params
            )

        async def _funda_read(document: str, params: dict[str, Any]) -> dict[str, Any]:
            return await asyncio.to_thread(funda_conn.query, "read", document=document, params=params)

        registry.register_federated_source("funda-ai", CapabilityId.FUNDA_AI, search_fn=_funda_search, list_fn=_funda_list)
        registry.register_prefix_reader("funda", CapabilityId.FUNDA_AI, _funda_read)
