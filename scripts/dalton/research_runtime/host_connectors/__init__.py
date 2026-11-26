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

from .bridge_alphaengine_proxy import register_alphaengine_bridge_proxy
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

# Credential/runtime keys that `research-host.env` is allowed to populate when this
# Everflow commercial connector bundle is configured.
EXTRA_HOST_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_ALPHAENGINE_USAGE_FILE",
        "GUIDEPOINT_CLIENT_ID",
        "GUIDEPOINT_CLIENT_SECRET",
        "GUIDEPOINT_TOKENS_FILE",
        "DALTON_GUIDEPOINT_TOKENS_FILE",
        "DALTON_GUIDEPOINT_OWNERSHIP_FILE",
    }
)


def register(
    registry: ConnectorRegistry,
    config: Any,
    *,
    active_caps: frozenset[CapabilityId] | None = None,
) -> None:
    """Register enabled commercial connectors (`guidepoint`, `alphaengine` proxy, `sales-notes`, `tmtb`, `funda-ai`).

    Checks each connector's capability BEFORE initializing managers or acquiring exclusive locks
    so disabled connectors never register routes, never probe health, and never acquire locks.
    """
    data_root = Path(config.data_root)
    code_root = getattr(config, "code_root", None)
    cfg_env = getattr(config, "environ", None) or os.environ
    caps = (
        effective_capabilities(profile=getattr(config, "profile", None))
        if active_caps is None
        else frozenset(active_caps)
    )

    # 1. Guidepoint (ownership-aware startup: acquires lock ONLY when target_owner == research_host_8951)
    if CapabilityId.GUIDEPOINT in caps:
        gp_token_file = resolve_guidepoint_tokens_file(data_root=data_root, environ=cfg_env)
        gp_ownership_raw = str(cfg_env.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "").strip() or None
        gp_manager = GuidepointTokenManager(
            gp_token_file,
            client_id=str(cfg_env.get("GUIDEPOINT_CLIENT_ID") or os.environ.get("GUIDEPOINT_CLIENT_ID", "")),
            client_secret=str(cfg_env.get("GUIDEPOINT_CLIENT_SECRET") or os.environ.get("GUIDEPOINT_CLIENT_SECRET", "")),
            ownership_file=gp_ownership_raw,
        )
        gp_manager.initialize_from_ownership(timeout_s=2.0)
        gp_conn = GuidepointConnector(gp_manager)
        registry.register_connector(gp_conn)
        registry.register_health_probe("guidepoint", gp_manager.health)
        registry.register_lock_handoff(
            "guidepoint",
            gp_manager.acquire_lock_for_handoff,
            capability=CapabilityId.GUIDEPOINT,
        )
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

        registry.register_federated_source(
            "guidepoint",
            CapabilityId.GUIDEPOINT,
            search_fn=_gp_search_leg,
            allowed_params=frozenset({"size", "keywords", "recency_bias", "start_date", "end_date"}),
            top_level_block=True,
            fuse_local=False,
            provenance_identity="expert",
        )

    # 2. Batch 3 AlphaEngine federated leg forwarding to legacy bridge + daily usage health field
    if CapabilityId.ALPHAENGINE in caps:
        raw_usage = str(
            cfg_env.get("DALTON_ALPHAENGINE_USAGE_FILE")
            or os.environ.get("DALTON_ALPHAENGINE_USAGE_FILE", "")
        ).strip()
        usage_file = (
            Path(raw_usage).expanduser().resolve()
            if raw_usage
            else getattr(config, "usage_file", None)
        )
        register_alphaengine_bridge_proxy(
            registry,
            bridge_mcp_url=str(getattr(config, "legacy_bridge_mcp_url", "") or ""),
            bridge_token=str(getattr(config, "legacy_bridge_token", "") or ""),
            usage_file=usage_file,
        )

    # 3. Sales Notes & TMTB (SQL-partitioned shared market-digest DB)
    if CapabilityId.SALES_NOTES in caps or CapabilityId.TMTB in caps:
        md_store = MarketDigestPartitionStore(data_root, code_root=code_root)
        registry.register_federated_alias("sales_mail", ("sales-notes", "tmtb"))
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

            registry.register_federated_source(
                "sales-notes",
                CapabilityId.SALES_NOTES,
                search_fn=_sn_search,
                list_fn=_sn_list,
                allowed_params=frozenset({"sender", "layer", "search_mode", "media"}),
                prefixes=("mail", "digest"),
                top_level_block=False,
                fuse_local=True,
                provenance_identity="internal_note",
            )
            registry.register_prefix_reader("mail", CapabilityId.SALES_NOTES, _sn_read, source_id="sales-notes")
            registry.register_prefix_reader("digest", CapabilityId.SALES_NOTES, _sn_read, source_id="sales-notes")

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

            registry.register_federated_source(
                "tmtb",
                CapabilityId.TMTB,
                search_fn=_tmtb_search,
                list_fn=_tmtb_list,
                allowed_params=frozenset({"sender", "search_mode", "media"}),
                prefixes=("tmtb",),
                top_level_block=False,
                fuse_local=True,
                provenance_identity="internal_note",
            )
            registry.register_prefix_reader("tmtb", CapabilityId.TMTB, _tmtb_read, source_id="tmtb")

    # 4. FUNDA AI
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

        registry.register_federated_source(
            "funda-ai",
            CapabilityId.FUNDA_AI,
            search_fn=_funda_search,
            list_fn=_funda_list,
            allowed_params=frozenset({"sender", "search_mode", "media"}),
            prefixes=("funda",),
            top_level_block=False,
            fuse_local=True,
            provenance_identity="internal_note",
        )
        registry.register_prefix_reader("funda", CapabilityId.FUNDA_AI, _funda_read, source_id="funda-ai")
