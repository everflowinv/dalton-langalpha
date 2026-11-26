"""Pluggable connector registry for ``dalton-research-host``.

The open-source package registers only public connectors (``company-wiki`` and
``federated``). Optional host-side connector extensions are registered externally
via ``DALTON_RESEARCH_HOST_EXTRA_MODULES`` so the Core release package stays
self-contained and free of external host-only modules.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from src.dalton.capabilities import CapabilityId

logger = logging.getLogger("dalton-research-host")


@runtime_checkable
class ResearchConnector(Protocol):
    """Contract implemented by every connector mounted on ``/mcp/<route_id>``."""

    route_id: str
    server_name: str
    required_capabilities: tuple[CapabilityId, ...]
    require_all_capabilities: bool

    def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        active_caps: frozenset[CapabilityId],
        caller_key: str = "anonymous",
    ) -> dict[str, Any]: ...


IngestHandler = Callable[[dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]
FanoutProvider = Callable[
    [str, str | None, str | None, int, dict[str, Any], str],
    Awaitable[dict[str, Any]],
]
ReadHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
LockHandoffHandler = Callable[[dict[str, Any]], Awaitable[tuple[int, dict[str, Any]] | dict[str, Any]] | tuple[int, dict[str, Any]] | dict[str, Any]]
HealthFieldProvider = Callable[[frozenset[CapabilityId]], Any]


@dataclass(frozen=True)
class FederatedSourceSpec:
    """Metadata and handlers for a federated source registered on ``/mcp/federated``."""

    source_id: str
    capability: CapabilityId
    search_fn: FanoutProvider
    list_fn: FanoutProvider | None = None
    allowed_params: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] = ()
    top_level_block: bool = False
    fuse_local: bool = True
    provenance_identity: str = "internal_note"


@dataclass
class ConnectorRegistry:
    """In-memory registry of MCP connectors, ingest handlers, lock handoffs, and federated legs."""

    connectors: dict[str, ResearchConnector] = field(default_factory=dict)
    ingest_handlers: dict[str, tuple[CapabilityId, IngestHandler]] = field(default_factory=dict)
    # Federated local/remote source specs and search/list legs keyed by source id
    federated_sources: dict[str, FederatedSourceSpec] = field(default_factory=dict)
    federated_search_legs: dict[str, tuple[CapabilityId, FanoutProvider]] = field(default_factory=dict)
    federated_list_legs: dict[str, tuple[CapabilityId, FanoutProvider]] = field(default_factory=dict)
    # Named alias groups for compatibility federated tools (e.g. `sales_mail` -> ('sales-notes', 'tmtb'))
    federated_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Prefix-dispatched read handlers keyed by prefix (e.g. 'wiki', 'mail', ...)
    prefix_read_handlers: dict[str, tuple[CapabilityId | None, ReadHandler]] = field(default_factory=dict)
    prefix_to_source: dict[str, str] = field(default_factory=dict)
    # Optional health probes and extra top-level health fields contributed by extra modules
    health_probes: dict[str, Callable[[], dict[str, Any]]] = field(default_factory=dict)
    health_fields: dict[str, tuple[CapabilityId | None, HealthFieldProvider]] = field(default_factory=dict)
    # Generic admin lock-handoff handlers for `POST /admin/{connector_id}/acquire-lock`
    lock_handoff_handlers: dict[str, tuple[CapabilityId | None, LockHandoffHandler]] = field(default_factory=dict)
    # Optional shutdown hooks (e.g., releasing exclusive file locks)
    shutdown_hooks: list[Callable[[], Awaitable[None] | None]] = field(default_factory=list)

    def register_connector(self, connector: ResearchConnector) -> None:
        self.connectors[connector.route_id] = connector

    def register_ingest(self, name: str, capability: CapabilityId, handler: IngestHandler) -> None:
        self.ingest_handlers[name] = (capability, handler)

    def register_federated_source(
        self,
        source_id: str,
        capability: CapabilityId,
        *,
        search_fn: FanoutProvider,
        list_fn: FanoutProvider | None = None,
        allowed_params: frozenset[str] | set[str] | tuple[str, ...] = (),
        prefixes: tuple[str, ...] = (),
        top_level_block: bool = False,
        fuse_local: bool | None = None,
        provenance_identity: str = "internal_note",
    ) -> None:
        eff_fuse_local = (not top_level_block) if fuse_local is None else bool(fuse_local)
        spec = FederatedSourceSpec(
            source_id=source_id,
            capability=capability,
            search_fn=search_fn,
            list_fn=list_fn,
            allowed_params=frozenset(allowed_params),
            prefixes=tuple(prefixes),
            top_level_block=bool(top_level_block),
            fuse_local=eff_fuse_local,
            provenance_identity=str(provenance_identity or "internal_note"),
        )
        self.federated_sources[source_id] = spec
        self.federated_search_legs[source_id] = (capability, search_fn)
        if list_fn is not None:
            self.federated_list_legs[source_id] = (capability, list_fn)
        for pref in spec.prefixes:
            self.prefix_to_source[pref] = source_id

    def register_federated_alias(self, alias_name: str, source_ids: tuple[str, ...]) -> None:
        self.federated_aliases[alias_name] = tuple(source_ids)

    def register_prefix_reader(
        self,
        prefix: str,
        capability: CapabilityId | None,
        handler: ReadHandler,
        *,
        source_id: str | None = None,
    ) -> None:
        self.prefix_read_handlers[prefix] = (capability, handler)
        if source_id:
            self.prefix_to_source[prefix] = source_id

    def register_health_probe(self, name: str, probe: Callable[[], dict[str, Any]]) -> None:
        self.health_probes[name] = probe

    def register_health_field(
        self,
        field_name: str,
        provider: HealthFieldProvider,
        *,
        capability: CapabilityId | None = None,
    ) -> None:
        self.health_fields[field_name] = (capability, provider)

    def register_lock_handoff(
        self,
        connector_id: str,
        acquire_fn: LockHandoffHandler,
        *,
        capability: CapabilityId | None = None,
    ) -> None:
        """Register an admin lock-acquisition callback for ``POST /admin/{connector_id}/acquire-lock``."""
        self.lock_handoff_handlers[str(connector_id).strip()] = (capability, acquire_fn)

    def register_shutdown_hook(self, hook: Callable[[], Awaitable[None] | None]) -> None:
        self.shutdown_hooks.append(hook)


def load_extra_connector_modules(
    registry: ConnectorRegistry,
    module_names: list[str],
    *,
    config: Any,
    active_caps: frozenset[CapabilityId] | None = None,
) -> list[str]:
    """Import each module in `module_names` and invoke its `register(registry, config)` hook."""
    loaded: list[str] = []
    for raw_name in module_names:
        mod_name = raw_name.strip()
        if not mod_name:
            continue
        mod = importlib.import_module(mod_name)
        register_fn = getattr(mod, "register", None)
        if not callable(register_fn):
            raise RuntimeError(
                f"Extra research-host connector module {mod_name!r} does not define callable register(registry, config)"
            )
        try:
            sig = inspect.signature(register_fn)
            accepts_caps = "active_caps" in sig.parameters
        except (TypeError, ValueError):
            accepts_caps = False
        if accepts_caps:
            register_fn(registry, config, active_caps=active_caps)
        else:
            register_fn(registry, config)
        loaded.append(mod_name)
        logger.info("Loaded research-host connector module: %s", mod_name)
    return loaded
