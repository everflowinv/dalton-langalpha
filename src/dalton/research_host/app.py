"""FastAPI HTTP service and lifecycle runtime for ``dalton-research-host`` (default ``:8951``)."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.dalton.capabilities import CapabilityId, effective_capabilities
from src.dalton.profile import DeploymentProfile, resolve_profile
from src.dalton.research_host import __version__
from src.dalton.research_host.auth import (
    CURRENT_CALLER_HEADERS,
    extract_bearer_or_header,
    resolve_caller_identity,
    verify_constant_time_token,
)
from src.dalton.research_host.connector_registry import (
    ConnectorRegistry,
    load_extra_connector_modules,
)
from src.dalton.research_host.federated import (
    DEFAULT_FANOUT_GRACE_S,
    FederatedResearchConnector,
)
from src.dalton.research_host.mcp_protocol import (
    jsonrpc_error,
    jsonrpc_result,
    make_initialize_result,
    mcp_tool_result,
)
from src.dalton.research_host.wiki_store import CompanyWikiConnector, OpenWikiStore

logger = logging.getLogger("dalton-research-host")

DEFAULT_RESEARCH_HOST: str = "127.0.0.1"
DEFAULT_RESEARCH_PORT: int = 8951
DEFAULT_DATA_ROOT: str = "/var/lib/dalton/wiki"
DEFAULT_WIKI_PAUSE_TTL_S: float = 300.0
MIN_WIKI_PAUSE_TTL_S: float = 0.05
MAX_WIKI_PAUSE_TTL_S: float = 3600.0


def _parse_extra_modules(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _resolve_extra_default_data_root(
    extra_modules: list[str],
    extra_python_paths: list[str],
    environ: Mapping[str, str],
) -> str:
    """Allow an optional host extension module to supply a deployment-specific default data root."""
    if not extra_modules:
        return ""
    for p in extra_python_paths:
        if p and p not in sys.path:
            sys.path.insert(0, p)
    for mod_name in extra_modules:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            logger.debug("Skipping default data root lookup for %s: %s", mod_name, exc)
            continue
        resolver = getattr(mod, "resolve_default_data_root", None)
        if callable(resolver):
            candidate = str(resolver(environ) or "").strip()
            if candidate:
                return candidate
    return ""


@dataclass
class ResearchHostConfig:
    """Runtime configuration for ``dalton-research-host``."""

    data_root: Path
    code_root: Path | None = None
    host: str = DEFAULT_RESEARCH_HOST
    port: int = DEFAULT_RESEARCH_PORT
    mcp_token: str = ""
    admin_token: str = ""
    extra_caller_admin_secrets: tuple[str, ...] = ()
    usage_file: Path | None = None
    wiki_audit_file: Path | None = None
    fanout_grace_s: float = DEFAULT_FANOUT_GRACE_S
    extra_modules: list[str] = field(default_factory=list)
    extra_python_paths: list[str] = field(default_factory=list)
    profile: DeploymentProfile | None = None
    environ: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ResearchHostConfig:
        env = os.environ if environ is None else environ
        active_profile = resolve_profile(
            app_env=env.get("APP_ENV", "") if environ is not None else None,
            dalton_profile=env.get("DALTON_PROFILE", "") if environ is not None else None,
        )
        extra_modules = _parse_extra_modules(env.get("DALTON_RESEARCH_HOST_EXTRA_MODULES"))
        extra_paths = _parse_extra_modules(env.get("DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH"))
        raw_home = str(env.get("DALTON_RESEARCH_HOME") or env.get("DALTON_WIKI_DATA_DIR") or "").strip()
        if not raw_home:
            raw_home = _resolve_extra_default_data_root(extra_modules, extra_paths, env) or DEFAULT_DATA_ROOT
        raw_code = str(env.get("DALTON_RESEARCH_CODE") or "").strip()
        host = str(env.get("DALTON_RESEARCH_HOST_BIND") or DEFAULT_RESEARCH_HOST).strip() or DEFAULT_RESEARCH_HOST
        try:
            port = int(env.get("DALTON_RESEARCH_HOST_PORT") or DEFAULT_RESEARCH_PORT)
        except ValueError:
            port = DEFAULT_RESEARCH_PORT
        mcp_token = str(env.get("DALTON_RESEARCH_HOST_TOKEN") or "").strip()
        admin_token = str(env.get("DALTON_RESEARCH_ADMIN_TOKEN") or "").strip()
        raw_audit = str(env.get("DALTON_WIKI_INGEST_AUDIT_FILE") or "").strip()
        try:
            grace_s = float(env.get("DALTON_RESEARCH_FANOUT_GRACE_S") or DEFAULT_FANOUT_GRACE_S)
        except ValueError:
            grace_s = DEFAULT_FANOUT_GRACE_S
        return cls(
            data_root=Path(raw_home).expanduser().resolve(),
            code_root=Path(raw_code).expanduser().resolve() if raw_code else None,
            host=host,
            port=port,
            mcp_token=mcp_token,
            admin_token=admin_token,
            usage_file=None,
            wiki_audit_file=Path(raw_audit).expanduser().resolve() if raw_audit else None,
            fanout_grace_s=max(0.05, grace_s),
            extra_modules=extra_modules,
            extra_python_paths=extra_paths,
            profile=active_profile,
            environ=dict(env),
        )


class ResearchHostRuntime:
    """Owns the connector registry, open-source wiki store, extra modules, and drain state."""

    def __init__(
        self,
        config: ResearchHostConfig,
        *,
        capabilities_provider: Any = None,
    ) -> None:
        if config.mcp_token and config.admin_token and config.mcp_token == config.admin_token:
            raise ValueError(
                "DALTON_RESEARCH_HOST_TOKEN and DALTON_RESEARCH_ADMIN_TOKEN must be distinct secrets"
            )
        self.config = config
        self._capabilities_provider = capabilities_provider
        self.registry = ConnectorRegistry()
        self.wiki_store = OpenWikiStore(
            config.data_root,
            audit_file=config.wiki_audit_file,
            code_root=config.code_root,
        )
        self.draining: bool = False
        self.in_flight: int = 0
        self.wiki_writes_paused: bool = False
        self.wiki_pause_owner: str | None = None
        self.wiki_pause_expires_at: float | None = None
        self.wiki_pause_lease_id: int = 0
        self._next_wiki_pause_lease_id: int = 0
        self._expired_wiki_pause_leases: dict[int, str | None] = {}
        self.wiki_writes_in_flight: int = 0
        self.loaded_extra_modules: list[str] = []

        # Register built-in open-source `company-wiki` connector & ingest handler
        wiki_conn = CompanyWikiConnector(self.wiki_store)
        self.registry.register_connector(wiki_conn)
        self.registry.register_ingest("wiki", CapabilityId.COMPANY_WIKI, self.wiki_store.ingest)

        async def _wiki_search_leg(
            query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _caller: str
        ) -> dict[str, Any]:
            return await asyncio.to_thread(
                self.wiki_store.query,
                "search",
                query=query,
                start=start,
                end=end,
                limit=limit,
                params=params,
            )

        async def _wiki_list_leg(
            query: str, start: str | None, end: str | None, limit: int, params: dict[str, Any], _caller: str
        ) -> dict[str, Any]:
            return await asyncio.to_thread(
                self.wiki_store.query,
                "list",
                query=query or None,
                start=start,
                end=end,
                limit=limit,
                params=params,
            )

        async def _wiki_read(document: str, params: dict[str, Any]) -> dict[str, Any]:
            return await asyncio.to_thread(
                self.wiki_store.query,
                "read",
                document=document,
                params=params,
            )

        self.registry.register_federated_source(
            "company-wiki",
            CapabilityId.COMPANY_WIKI,
            search_fn=_wiki_search_leg,
            list_fn=_wiki_list_leg,
            allowed_params=frozenset(
                {"company", "sector", "tag", "search_mode", "no_decay", "media", "indexed_since"}
            ),
            prefixes=("wiki",),
            top_level_block=False,
            fuse_local=True,
            provenance_identity="internal_note",
        )
        self.registry.register_prefix_reader(
            "wiki",
            CapabilityId.COMPANY_WIKI,
            _wiki_read,
            source_id="company-wiki",
        )

        # Load optional host-side connector extension modules (excluded from Core release)
        for p in config.extra_python_paths:
            if p and p not in sys.path:
                sys.path.insert(0, p)
        if config.extra_modules:
            self.loaded_extra_modules = load_extra_connector_modules(
                self.registry,
                config.extra_modules,
                config=config,
                active_caps=self.current_capabilities(),
            )

        self.federated_connector = FederatedResearchConnector(
            self.registry,
            self.wiki_store,
            fanout_grace_s=config.fanout_grace_s,
        )
        self.registry.register_connector(self.federated_connector)

    def is_wiki_writes_paused(self) -> bool:
        """Return True if Wiki writes are paused and the pause lease has not expired.

        Expired leases auto-resume writes so a crashed backup can never leave Wiki stuck in 503,
        and record the expired lease_id so renewal/resume can detect non-continuous pauses.
        """
        if not self.wiki_writes_paused:
            return False
        if self.wiki_pause_expires_at is not None and time.monotonic() >= self.wiki_pause_expires_at:
            expired_id = self.wiki_pause_lease_id
            logger.warning(
                "Wiki write-pause lease id=%s held by %r expired; auto-resuming Wiki writes",
                expired_id,
                self.wiki_pause_owner,
            )
            if expired_id > 0:
                self._expired_wiki_pause_leases[expired_id] = self.wiki_pause_owner
                if len(self._expired_wiki_pause_leases) > 64:
                    oldest = next(iter(self._expired_wiki_pause_leases))
                    self._expired_wiki_pause_leases.pop(oldest, None)
            self.wiki_writes_paused = False
            self.wiki_pause_owner = None
            self.wiki_pause_expires_at = None
            self.wiki_pause_lease_id = 0
            return False
        return True

    def wiki_pause_status(self) -> dict[str, Any]:
        paused = self.is_wiki_writes_paused()
        remaining = (
            max(0.0, round(self.wiki_pause_expires_at - time.monotonic(), 3))
            if (paused and self.wiki_pause_expires_at is not None)
            else 0.0
        )
        return {
            "ok": True,
            "paused": paused,
            "in_flight": self.wiki_writes_in_flight,
            "owner": self.wiki_pause_owner if paused else None,
            "lease_id": self.wiki_pause_lease_id if paused else None,
            "ttl_remaining_s": remaining,
        }

    def current_capabilities(self) -> frozenset[CapabilityId]:
        if callable(self._capabilities_provider):
            return frozenset(self._capabilities_provider())
        return effective_capabilities(profile=self.config.profile)

    def _caller_admin_secrets(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                s
                for s in (
                    self.config.admin_token,
                    *self.config.extra_caller_admin_secrets,
                    *self.registry.caller_admin_secrets,
                )
                if s
            )
        )

    def _connector_enabled(self, route_id: str, active_caps: frozenset[CapabilityId]) -> bool:
        conn = self.registry.connectors.get(route_id)
        if conn is None:
            return False
        if not conn.required_capabilities:
            return True
        if conn.require_all_capabilities:
            return all(c in active_caps for c in conn.required_capabilities)
        return any(c in active_caps for c in conn.required_capabilities)

    def health_payload(self) -> dict[str, Any]:
        active = self.current_capabilities()
        connectors_state = {
            rid: self._connector_enabled(rid, active)
            for rid in sorted(self.registry.connectors.keys())
        }
        probes = {name: fn() for name, fn in sorted(self.registry.health_probes.items())}
        payload: dict[str, Any] = {
            "ok": not self.draining,
            "status": "draining" if self.draining else "ok",
            "service": "dalton-research-host",
            "version": __version__,
            "profile": (self.config.profile or resolve_profile()).value,
            "draining": self.draining,
            "in_flight": self.in_flight,
            "connectors": connectors_state,
            "probes": probes,
        }
        if self.loaded_extra_modules:
            payload["extra_modules"] = list(self.loaded_extra_modules)
        for field_name, (req_cap, provider) in sorted(self.registry.health_fields.items()):
            if req_cap is not None and req_cap not in active:
                continue
            val = provider(active)
            if val is not None:
                payload[field_name] = val
        return payload

    async def shutdown(self) -> None:
        for hook in self.registry.shutdown_hooks:
            try:
                res = hook()
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                logger.warning("Shutdown hook error: %s", exc)


def create_app(
    config: ResearchHostConfig | None = None,
    *,
    runtime: ResearchHostRuntime | None = None,
    capabilities_provider: Any = None,
) -> FastAPI:
    """Create the FastAPI application for ``dalton-research-host``."""
    cfg = config or ResearchHostConfig.from_env()
    rt = runtime or ResearchHostRuntime(
        cfg,
        capabilities_provider=capabilities_provider,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await rt.shutdown()

    app = FastAPI(title="Dalton Research Host", version=__version__, lifespan=lifespan)
    app.state.runtime = rt

    def _require_mcp_token(request: Request) -> JSONResponse | None:
        presented = extract_bearer_or_header(request, "x-dalton-research-token")
        if not verify_constant_time_token(presented, rt.config.mcp_token):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "status": "unauthorized", "error": "Invalid or missing MCP token"},
            )
        return None

    def _require_admin_token(request: Request) -> JSONResponse | None:
        presented = extract_bearer_or_header(request, "x-dalton-research-admin-token")
        if not verify_constant_time_token(presented, rt.config.admin_token):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "status": "unauthorized", "error": "Invalid or missing admin token"},
            )
        return None

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(status_code=200, content=rt.health_payload())

    @app.get("/admin/drain")
    async def get_drain(request: Request) -> JSONResponse:
        err = _require_admin_token(request)
        if err is not None:
            return err
        return JSONResponse(
            status_code=200,
            content={"ok": True, "draining": rt.draining, "in_flight": rt.in_flight},
        )

    @app.post("/admin/drain")
    async def set_drain(request: Request) -> JSONResponse:
        err = _require_admin_token(request)
        if err is not None:
            return err
        body = await request.json()
        rt.draining = bool(body.get("drain", True))
        return JSONResponse(
            status_code=200,
            content={"ok": True, "draining": rt.draining, "in_flight": rt.in_flight},
        )

    @app.post("/admin/{connector_id}/acquire-lock")
    async def acquire_connector_lock(connector_id: str, request: Request) -> JSONResponse:
        err = _require_admin_token(request)
        if err is not None:
            return err
        entry = rt.registry.lock_handoff_handlers.get(connector_id)
        if entry is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "status": "not_found",
                    "error_code": "lock_handoff_not_registered",
                    "error": f"No lock handoff handler registered for connector '{connector_id}'.",
                },
            )
        req_cap, acquire_fn = entry
        if req_cap is not None and req_cap not in rt.current_capabilities():
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "status": "capability_disabled",
                    "error_code": "capability_disabled",
                    "error": f"Capability '{req_cap.value}' is disabled.",
                },
            )
        try:
            raw_body = await request.body()
            body = await request.json() if raw_body else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "status": "bad_request", "error": "JSON object required"},
            )
        try:
            if inspect.iscoroutinefunction(acquire_fn):
                result = await acquire_fn(body)
            else:
                result = await asyncio.to_thread(acquire_fn, body)
                if inspect.isawaitable(result):
                    result = await result
        except Exception as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "status": "lock_acquire_failed",
                    "error_code": "lock_acquire_failed",
                    "error": str(exc) or type(exc).__name__,
                },
            )
        if isinstance(result, tuple) and len(result) == 2:
            status_code, payload = result
            return JSONResponse(status_code=int(status_code), content=dict(payload))
        if isinstance(result, dict):
            code = int(result.pop("http_status", 200))
            return JSONResponse(status_code=code, content=result)
        return JSONResponse(status_code=200, content={"ok": True, "connector_id": connector_id})

    @app.get("/admin/wiki/pause-writes")
    async def get_wiki_pause_writes(request: Request) -> JSONResponse:
        err = _require_admin_token(request)
        if err is not None:
            return err
        return JSONResponse(status_code=200, content=rt.wiki_pause_status())

    @app.post("/admin/wiki/pause-writes")
    async def set_wiki_pause_writes(request: Request) -> JSONResponse:
        err = _require_admin_token(request)
        if err is not None:
            return err
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "status": "bad_request", "error": "JSON object required"},
            )
        want_paused = bool(body.get("paused", True))
        req_owner = str(body.get("owner") or "").strip() or None
        raw_lease_id = body.get("lease_id")
        req_lease_id: int | None = None
        if raw_lease_id is not None and str(raw_lease_id).strip() != "":
            try:
                req_lease_id = int(raw_lease_id)
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "status": "bad_request", "error": "lease_id must be an integer"},
                )
        currently_paused = rt.is_wiki_writes_paused()

        if want_paused:
            # Explicit lease renewal (`lease_id` supplied): only succeeds if that exact lease never expired
            if req_lease_id is not None:
                if (
                    not currently_paused
                    or rt.wiki_pause_lease_id != req_lease_id
                    or (rt.wiki_pause_owner is not None and req_owner != rt.wiki_pause_owner)
                ):
                    return JSONResponse(
                        status_code=409,
                        content={
                            "ok": False,
                            "status": "lease_expired",
                            "error_code": "wiki_pause_lease_expired",
                            "error": f"Wiki pause lease {req_lease_id} is no longer active (expired or superseded).",
                            "paused": currently_paused,
                            "owner": rt.wiki_pause_owner if currently_paused else None,
                            "lease_id": rt.wiki_pause_lease_id if currently_paused else None,
                            "requested_lease_id": req_lease_id,
                            "expired": True,
                            "in_flight": rt.wiki_writes_in_flight,
                        },
                    )
                raw_ttl = body.get("ttl_s", DEFAULT_WIKI_PAUSE_TTL_S)
                try:
                    ttl_s = float(raw_ttl)
                except (TypeError, ValueError):
                    return JSONResponse(
                        status_code=400,
                        content={"ok": False, "status": "bad_request", "error": "ttl_s must be a number"},
                    )
                ttl_s = max(MIN_WIKI_PAUSE_TTL_S, min(MAX_WIKI_PAUSE_TTL_S, ttl_s))
                rt.wiki_pause_expires_at = time.monotonic() + ttl_s
                resp = rt.wiki_pause_status()
                resp["ttl_s"] = ttl_s
                resp["expired"] = False
                return JSONResponse(status_code=200, content=resp)

            if currently_paused and rt.wiki_pause_owner is not None and req_owner != rt.wiki_pause_owner:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "status": "pause_conflict",
                        "error_code": "wiki_pause_held_by_other",
                        "error": f"Wiki writes are already paused by owner '{rt.wiki_pause_owner}'.",
                        "paused": True,
                        "owner": rt.wiki_pause_owner,
                        "lease_id": rt.wiki_pause_lease_id,
                        "in_flight": rt.wiki_writes_in_flight,
                    },
                )
            raw_ttl = body.get("ttl_s", DEFAULT_WIKI_PAUSE_TTL_S)
            try:
                ttl_s = float(raw_ttl)
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "status": "bad_request", "error": "ttl_s must be a number"},
                )
            ttl_s = max(MIN_WIKI_PAUSE_TTL_S, min(MAX_WIKI_PAUSE_TTL_S, ttl_s))
            if not currently_paused:
                rt._next_wiki_pause_lease_id += 1
                rt.wiki_pause_lease_id = rt._next_wiki_pause_lease_id
            rt.wiki_writes_paused = True
            rt.wiki_pause_owner = req_owner
            rt.wiki_pause_expires_at = time.monotonic() + ttl_s
            resp = rt.wiki_pause_status()
            resp["ttl_s"] = ttl_s
            resp["expired"] = False
            return JSONResponse(status_code=200, content=resp)

        # Resume requested: only the active owner may clear an owned pause lease
        if currently_paused and rt.wiki_pause_owner is not None and req_owner != rt.wiki_pause_owner:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "status": "pause_owner_mismatch",
                    "error_code": "wiki_pause_owner_mismatch",
                    "error": f"Only pause owner '{rt.wiki_pause_owner}' may resume Wiki writes.",
                    "paused": True,
                    "owner": rt.wiki_pause_owner,
                    "lease_id": rt.wiki_pause_lease_id,
                    "in_flight": rt.wiki_writes_in_flight,
                },
            )
        if req_lease_id is not None:
            if not currently_paused:
                resp = rt.wiki_pause_status()
                resp["lease_id"] = req_lease_id
                resp["expired"] = True
                resp["continuous"] = False
                return JSONResponse(status_code=200, content=resp)
            if rt.wiki_pause_lease_id != req_lease_id:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "status": "lease_mismatch",
                        "error_code": "wiki_pause_lease_expired",
                        "error": f"Active lease {rt.wiki_pause_lease_id} does not match requested lease {req_lease_id}.",
                        "paused": True,
                        "owner": rt.wiki_pause_owner,
                        "lease_id": rt.wiki_pause_lease_id,
                        "requested_lease_id": req_lease_id,
                        "expired": True,
                        "continuous": False,
                        "in_flight": rt.wiki_writes_in_flight,
                    },
                )
        cleared_lease_id = rt.wiki_pause_lease_id or req_lease_id
        rt.wiki_writes_paused = False
        rt.wiki_pause_owner = None
        rt.wiki_pause_expires_at = None
        rt.wiki_pause_lease_id = 0
        resp = rt.wiki_pause_status()
        resp["lease_id"] = cleared_lease_id
        resp["expired"] = False
        resp["continuous"] = True
        return JSONResponse(status_code=200, content=resp)

    def _list_connector_tools(connector: Any, active_caps: frozenset[CapabilityId]) -> list[dict[str, Any]]:
        try:
            sig = inspect.signature(connector.list_tools)
            if "active_caps" in sig.parameters:
                return connector.list_tools(active_caps=active_caps)
        except (TypeError, ValueError):
            pass
        return connector.list_tools()

    @app.post("/mcp/{connector_id}")
    async def mcp_endpoint(connector_id: str, request: Request) -> JSONResponse:
        auth_err = _require_mcp_token(request)
        if auth_err is not None:
            return auth_err
        # Atomic drain check + in-flight admission BEFORE any `await` so `/admin/drain`
        # never observes `in_flight == 0` while a request is mid-body read.
        if rt.draining:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "status": "draining", "error_code": "service_draining"},
            )
        rt.in_flight += 1
        try:
            active_caps = rt.current_capabilities()
            connector = rt.registry.connectors.get(connector_id)
            if connector is None:
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "status": "capability_disabled",
                        "error_code": "capability_disabled",
                        "error": f"Connector '{connector_id}' is not enabled on this research host.",
                    },
                )
            if not rt._connector_enabled(connector_id, active_caps):
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "status": "capability_disabled",
                        "error_code": "capability_disabled",
                        "error": f"Capability for connector '{connector_id}' is disabled.",
                    },
                )

            try:
                rpc = await request.json()
            except Exception:
                return JSONResponse(status_code=400, content=jsonrpc_error(None, -32700, "Parse error"))
            if not isinstance(rpc, dict):
                return JSONResponse(status_code=400, content=jsonrpc_error(None, -32600, "Invalid Request"))

            req_id = rpc.get("id")
            method = str(rpc.get("method") or "")
            params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}

            if method == "initialize":
                return JSONResponse(
                    status_code=200,
                    content=jsonrpc_result(req_id, make_initialize_result(connector.server_name, __version__)),
                )
            if method == "notifications/initialized":
                return JSONResponse(status_code=200, content=jsonrpc_result(req_id, {}))
            if method == "tools/list":
                return JSONResponse(
                    status_code=200,
                    content=jsonrpc_result(req_id, {"tools": _list_connector_tools(connector, active_caps)}),
                )
            if method == "tools/call":
                tool_name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                caller = resolve_caller_identity(request, admin_secrets=rt._caller_admin_secrets())
                fwd_headers: dict[str, str] = {}
                for hdr in (
                    "x-dalton-caller-token",
                    "x-dalton-caller-scope",
                    "x-dalton-request-timeout-ms",
                ):
                    val = request.headers.get(hdr, "").strip()
                    if val:
                        fwd_headers[hdr] = val
                ctx_tok = CURRENT_CALLER_HEADERS.set(fwd_headers)
                try:
                    payload = await connector.call_tool(
                        tool_name,
                        arguments,
                        active_caps=active_caps,
                        caller_key=caller.caller_key,
                    )
                finally:
                    CURRENT_CALLER_HEADERS.reset(ctx_tok)
                http_override = payload.pop("http_status", None) if isinstance(payload, dict) else None
                if http_override in (403, 503) or (
                    isinstance(payload, dict)
                    and payload.get("error_code") in ("capability_disabled", "lock_not_acquired")
                ):
                    code = (
                        int(http_override)
                        if isinstance(http_override, int) and http_override in (403, 503)
                        else (503 if isinstance(payload, dict) and payload.get("error_code") == "lock_not_acquired" else 403)
                    )
                    return JSONResponse(
                        status_code=code,
                        content=jsonrpc_result(req_id, mcp_tool_result(payload, is_error=True)),
                    )
                return JSONResponse(
                    status_code=200,
                    content=jsonrpc_result(req_id, mcp_tool_result(payload)),
                )
            return JSONResponse(
                status_code=200,
                content=jsonrpc_error(req_id, -32601, f"Method not found: {method}"),
            )
        finally:
            rt.in_flight = max(0, rt.in_flight - 1)

    @app.post("/ingest/{target_name}")
    async def ingest_endpoint(target_name: str, request: Request) -> JSONResponse:
        auth_err = _require_admin_token(request)
        if auth_err is not None:
            return auth_err
        if rt.draining:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "status": "draining", "error_code": "service_draining"},
            )
        is_wiki_write = target_name == "wiki"
        if is_wiki_write and rt.is_wiki_writes_paused():
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "status": "writes_paused",
                    "error_code": "wiki_writes_paused",
                    "error": "Wiki writes are paused for consistent backup; retry shortly.",
                },
            )
        rt.in_flight += 1
        if is_wiki_write:
            rt.wiki_writes_in_flight += 1
        try:
            entry = rt.registry.ingest_handlers.get(target_name)
            if entry is None:
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "status": "capability_disabled",
                        "error_code": "capability_disabled",
                        "error": f"Ingest handler '{target_name}' is not enabled on this host.",
                    },
                )
            cap_id, handler = entry
            if cap_id not in rt.current_capabilities():
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "status": "capability_disabled",
                        "error_code": "capability_disabled",
                        "error": f"Capability '{cap_id.value}' is disabled.",
                    },
                )
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(status_code=400, content={"ok": False, "status": "bad_request", "error": "Invalid JSON"})
            if not isinstance(body, dict):
                return JSONResponse(status_code=400, content={"ok": False, "status": "bad_request", "error": "JSON object required"})
            status_code, payload = await handler(body)
            return JSONResponse(status_code=status_code, content=payload)
        finally:
            if is_wiki_write:
                rt.wiki_writes_in_flight = max(0, rt.wiki_writes_in_flight - 1)
            rt.in_flight = max(0, rt.in_flight - 1)

    return app
