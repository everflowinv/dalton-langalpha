"""Anti-leakage registration gates and unit tests for Standalone Core capabilities (Batch 2a, §3.1–§3.4)."""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
import gc
import importlib
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import psycopg
from psycopg_pool import AsyncConnectionPool
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from langchain_core.tools import BaseTool

from ptc_agent import config as ptc_config_mod
from ptc_agent.config.core import CoreConfig, MCPConfig, MCPServerConfig
from ptc_agent.core import mcp_registry as mcp_registry_mod
from ptc_agent.core.mcp_registry import MCPRegistry
from src.config import env as app_env_mod
from src.config.core import clear_config_cache
from src.dalton import access_tiers as access_tiers_mod
from src.dalton import sharing as sharing_mod
from src.dalton.app import channels_router as channels_router_mod
from src.dalton.app import dalton_router
from src.dalton.autonomy import system_jobs as system_jobs_mod
from src.dalton.autonomy.probes import cnhk as cnhk_mod
from src.dalton.capabilities import (
    BACKGROUND_LOOP_REGISTRY,
    BUNDLED_PLUGIN_CAPABILITY_MAP,
    CAPABILITY_SPECS,
    CORE_CAPABILITY,
    DALTON_ROUTE_CAPABILITY_REGISTRY,
    MCP_SERVER_CAPABILITY_MAP,
    CapabilityId,
    capabilities_fingerprint,
    disabled_channel_names,
    effective_capabilities,
    filter_reconnect_targets,
    filter_reconnectable_builtin_mcp,
    has_capability,
    start_mcp_builtin_reconnect_task,
)
from src.dalton.data_jobs import scheduler as data_jobs_scheduler_mod
from src.dalton.data_jobs.scheduler import is_data_jobs_scheduler_enabled
from src.dalton.data_map import health as health_mod
from src.dalton.database import search as search_db_mod
from src.dalton.notify import channel_transport as transport_mod
from src.dalton.notify import dispatch as dispatch_mod
from src.dalton.profile import DeploymentProfile
from src.dalton.services import transcriptions as svc_transcriptions
from src.dalton.settings import (
    get_dalton_bridge_base_url,
    get_dalton_bridge_embed_url,
    get_dalton_channel_gateway_url,
    get_dalton_config,
    get_dalton_researchdata_url,
    notify_via_gateway_enabled,
)
from src.dalton.tools import DALTON_TOOL_REGISTRY, build_dalton_tools
from src.dalton.tools import conversation_tools as conversation_tools_mod
from src.dalton.tools import ingest_tools as ingest_tools_mod
from src.dalton.tools import wiki_tools as wiki_tools_mod
from src.dalton import transcription_storage as transcription_storage_mod
from src.server.app import setup as server_setup_mod
from src.server.auth import jwt_bearer as jwt_bearer_mod
from src.server.auth import oauth_resource as oauth_resource_mod
from src.server.database import pool as db_pool_mod
from src.server.database import user as user_db_mod
from src.server.services import brand_icons as brand_icons_mod
from src.server.services import heap_settle as heap_settle_mod
from src.server.services.report_back.handoff import retired_names as retired_names_mod
from src.server.services.runs import restart_drain as restart_drain_mod
from src.server.utils.api import get_current_user_id
from src.tools.messaging import tools as messaging_tools_mod
from src.utils.cache import redis_cache as redis_cache_mod

REPO_ROOT = Path(__file__).resolve().parents[3]
DALTON_TOOLS_DIR = REPO_ROOT / "src" / "dalton" / "tools"
DALTON_APP_DIR = REPO_ROOT / "src" / "dalton" / "app"
SETUP_PY_PATH = REPO_ROOT / "src" / "server" / "app" / "setup.py"
EVERFLOW_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "unit" / "dalton" / "snapshots" / "everflow" / "app_env_dalton" / "manifest.json"
)

# Explicit independent proprietary lists (NOT derived from DALTON_TOOL_REGISTRY /
# DALTON_ROUTE_CAPABILITY_REGISTRY, preventing circular validation).
EXPLICIT_PROPRIETARY_TOOLS: frozenset[str] = frozenset(
    {
        "feishu_read",
        "feishu_list_wiki",
        "feishu_create_doc",
        "feishu_append",
        "feishu_grant_access",
        "ingest_company_wiki",
        "ingest_sales_note",
        "ingest_research",
        "prepare_transcription",
        "conversation_search",
        "dalton_notify_settings",
        "dalton_report_delivery",
        "automation_report",
        "market_digest_inputs",
        "market_digest_output",
        "ai_finance_prepare",
        "ai_finance_read",
        "ai_finance_signals",
        "ai_finance_report",
        "podcast_weekly_read",
        "podcast_weekly_write",
        "report_issue",
        "send_message",
        "list_message_targets",
    }
)

EXPLICIT_PROPRIETARY_ROUTE_PREFIXES: tuple[str, ...] = (
    "/api/v1/dalton/channels/",
    "/api/v1/dalton/transcriptions",
    "/api/v1/oauth/",
    "/api/v1/dalton/native-models/",
    "/api/v1/dalton/feishu/",
    "/api/v1/dalton/notify/",
    "/api/v1/dalton/internal/llm/",
    "/api/v1/dalton/internal/model-availability/",
)

_CLEAR_CAP_ENV_VARS: tuple[str, ...] = (
    "APP_ENV",
    "DALTON_PROFILE",
    "DALTON_ENABLED",
    "DALTON_RESEARCHDATA_URL",
    "DALTON_BRIDGE_URL",
    "DALTON_BRIDGE_TOKEN",
    "DALTON_BRIDGE_ADMIN_TOKEN",
    "DALTON_BRIDGE_MCP_URL",
    "DALTON_BRIDGE_PORT",
    "CHANNEL_GATEWAY_URL",
    "GATEWAY_PORT",
    "INTERNAL_SERVICE_TOKEN",
    "DALTON_NOTIFY_VIA_GATEWAY",
    "DALTON_TRANSCRIPTION_ENABLED",
    "DALTON_TRANSCRIPTION_RECONCILE_ENABLED",
    "DALTON_ASR_STORAGE_ENDPOINT_URL",
    "DALTON_ASR_STORAGE_BUCKET_NAME",
    "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
    "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
    "DALTON_DATA_JOBS_ENABLED",
    "DALTON_ARTIFACT_PUBLISHING_ENABLED",
    "DALTON_COVERAGE_UNITS_ENABLED",
    "DALTON_PROBLEM_REPORTS_ENABLED",
    "BRAND_NAME",
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
    "BOCHA_API_KEY",
    "GEMINI_API_KEY",
    *(spec.env_var for spec in CAPABILITY_SPECS.values()),
)


def _clean_cap_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_env: str | None = None,
    dalton_profile: str | None = None,
) -> None:
    for var in _CLEAR_CAP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    if app_env is not None:
        monkeypatch.setenv("APP_ENV", app_env)
    if dalton_profile is not None:
        monkeypatch.setenv("DALTON_PROFILE", dalton_profile)
    clear_config_cache()
    get_dalton_config.cache_clear()


def _install_no_outbound_http_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fail immediately if any code tries to open an outbound httpx.AsyncClient."""

    def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            f"Unexpected outbound httpx.AsyncClient creation when capabilities are disabled: "
            f"args={args!r}, kwargs={kwargs!r}"
        )

    guard = create_autospec(httpx.AsyncClient, side_effect=_forbidden_client)
    monkeypatch.setattr(httpx, "AsyncClient", guard)
    return guard


def _make_dalton_test_app(user_id: str = "test-user-1") -> FastAPI:
    app = FastAPI()
    app.include_router(dalton_router)
    auth_override = create_autospec(get_current_user_id, return_value=user_id)
    app.dependency_overrides[get_current_user_id] = auth_override
    return app


def _enable_all_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable every capability (including transcription + storage) for per-cap disable tests."""
    _clean_cap_env(monkeypatch, app_env="dalton")
    monkeypatch.setenv("DALTON_CAP_TRANSCRIPTION", "true")
    monkeypatch.setattr(
        svc_transcriptions,
        "is_transcription_storage_configured",
        create_autospec(
            transcription_storage_mod.is_transcription_storage_configured,
            return_value=True,
        ),
    )


def _fill_path_params(path: str) -> str:
    """Replace FastAPI ``{param}`` placeholders with safe dummy values."""
    replacements = {
        "job_id": "00000000-0000-0000-0000-000000000001",
        "run_id": "00000000-0000-0000-0000-000000000002",
        "artifact_id": "00000000-0000-0000-0000-000000000003",
        "platform": "feishu",
    }
    return re.sub(
        r"\{([^}:]+)(?::[^}]+)?\}",
        lambda m: replacements.get(m.group(1), "test-id"),
        path,
    )


# ---------------------------------------------------------------------------
# 1. Capability Registry semantics: defaults, precedence, dependencies, hash
# ---------------------------------------------------------------------------


class TestCapabilityRegistry:
    def test_every_capability_has_spec_and_chinese_description(self) -> None:
        assert set(CAPABILITY_SPECS.keys()) == set(CapabilityId)
        for cap, spec in CAPABILITY_SPECS.items():
            assert spec.id is cap
            assert spec.description.strip()
            assert spec.env_var == f"DALTON_CAP_{cap.value.upper()}"

    @pytest.mark.parametrize(
        ("app_env", "dalton_profile"),
        [("core", None), ("", "core"), ("core", "core")],
    )
    def test_core_profile_defaults_all_capabilities_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str | None,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
        caps = effective_capabilities()
        assert caps == frozenset()
        for cap in CapabilityId:
            assert has_capability(cap) is False

    @pytest.mark.parametrize("app_env", ["dalton", ""])
    def test_everflow_profile_defaults_all_on_except_transcription(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env=app_env)
        caps = effective_capabilities()
        expected = frozenset(CapabilityId) - {CapabilityId.TRANSCRIPTION}
        assert caps == expected
        assert has_capability(CapabilityId.TRANSCRIPTION) is False
        assert has_capability(CapabilityId.PROBLEM_REPORTS) is True
        assert has_capability(CapabilityId.RESEARCHDATA_BRIDGE) is True
        assert has_capability(CapabilityId.CHANNEL_GATEWAY) is True

    def test_precedence_explicit_env_over_legacy_over_yaml_over_profile(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env="core")

        # Pure YAML override turns on data_jobs in Core
        caps_yaml = effective_capabilities(
            profile=DeploymentProfile.CORE,
            environ={},
            yaml_capabilities={"data_jobs": True},
            conversations_enabled=False,
            data_jobs_enabled=False,
            dalton_enabled=True,
            transcription_storage_configured=False,
        )
        assert CapabilityId.DATA_JOBS in caps_yaml

        # Explicit DALTON_CAP_DATA_JOBS=false overrides YAML True
        caps_explicit_off = effective_capabilities(
            profile=DeploymentProfile.CORE,
            environ={"DALTON_CAP_DATA_JOBS": "false"},
            yaml_capabilities={"data_jobs": True},
            conversations_enabled=False,
            data_jobs_enabled=True,
            dalton_enabled=True,
            transcription_storage_configured=False,
        )
        assert CapabilityId.DATA_JOBS not in caps_explicit_off

        # Explicit DALTON_CAP_DATA_JOBS=true overrides YAML False
        caps_explicit_on = effective_capabilities(
            profile=DeploymentProfile.CORE,
            environ={"DALTON_CAP_DATA_JOBS": "true"},
            yaml_capabilities={"data_jobs": False},
            conversations_enabled=False,
            data_jobs_enabled=False,
            dalton_enabled=True,
            transcription_storage_configured=False,
        )
        assert CapabilityId.DATA_JOBS in caps_explicit_on

        # Unexpanded ${DALTON_RESEARCHDATA_URL} literal must NOT turn bridge on in Core
        monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "${DALTON_RESEARCHDATA_URL}")
        assert has_capability(CapabilityId.RESEARCHDATA_BRIDGE) is False

        # Concrete URL turns bridge on in Core unless DALTON_CAP_RESEARCHDATA_BRIDGE=false
        monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://bridge.internal:8960/mcp")
        assert has_capability(CapabilityId.RESEARCHDATA_BRIDGE) is True
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "false")
        assert has_capability(CapabilityId.RESEARCHDATA_BRIDGE) is False

    def test_explicit_profile_does_not_leak_process_yaml(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 10 (codex low 1): effective_capabilities(profile=...) reads that profile's own YAML."""
        # Reference fingerprints from true process profiles
        _clean_cap_env(monkeypatch, app_env="core")
        true_core_caps = effective_capabilities()
        true_core_fp = capabilities_fingerprint(true_core_caps)

        _clean_cap_env(monkeypatch, app_env="dalton")
        true_ef_caps = effective_capabilities()
        true_ef_fp = capabilities_fingerprint(true_ef_caps)

        # In Everflow process, asking for explicit CORE must NOT inherit Everflow's conversations/data_jobs
        explicit_core = effective_capabilities(profile=DeploymentProfile.CORE)
        assert explicit_core == true_core_caps == frozenset()
        assert capabilities_fingerprint(explicit_core) == true_core_fp

        # In Core process, asking for explicit EVERFLOW must read Everflow's conversations/data_jobs
        _clean_cap_env(monkeypatch, app_env="core")
        explicit_ef = effective_capabilities(profile=DeploymentProfile.EVERFLOW)
        assert explicit_ef == true_ef_caps
        assert CapabilityId.CONVERSATION_ARCHIVE in explicit_ef
        assert CapabilityId.DATA_JOBS in explicit_ef
        assert capabilities_fingerprint(explicit_ef) == true_ef_fp

    def test_dependency_rules_force_children_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env="core")

        bridge_children = (
            CapabilityId.ALPHAENGINE,
            CapabilityId.GUIDEPOINT,
            CapabilityId.COMPANY_WIKI,
            CapabilityId.SALES_NOTES,
            CapabilityId.TMTB,
            CapabilityId.FUNDA_AI,
            CapabilityId.FEISHU_DOCS,
            CapabilityId.TRANSCRIPTION,
        )
        for child in bridge_children:
            monkeypatch.setenv(CAPABILITY_SPECS[child].env_var, "true")
        monkeypatch.setattr(
            svc_transcriptions,
            "is_transcription_storage_configured",
            create_autospec(
                transcription_storage_mod.is_transcription_storage_configured,
                return_value=True,
            ),
        )
        assert effective_capabilities() == frozenset()

        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "true")
        caps = effective_capabilities()
        assert CapabilityId.RESEARCHDATA_BRIDGE in caps
        for child in bridge_children:
            assert child in caps

        monkeypatch.setattr(
            svc_transcriptions,
            "is_transcription_storage_configured",
            create_autospec(
                transcription_storage_mod.is_transcription_storage_configured,
                return_value=False,
            ),
        )
        caps_no_storage = effective_capabilities()
        assert CapabilityId.TRANSCRIPTION not in caps_no_storage
        assert CapabilityId.ALPHAENGINE in caps_no_storage

        for ch in (
            CapabilityId.CHANNEL_FEISHU,
            CapabilityId.CHANNEL_DISCORD,
            CapabilityId.CHANNEL_WEIXIN,
        ):
            monkeypatch.setenv(CAPABILITY_SPECS[ch].env_var, "true")
        assert has_capability(CapabilityId.CHANNEL_FEISHU) is False
        assert has_capability(CapabilityId.CHANNEL_DISCORD) is False
        assert has_capability(CapabilityId.CHANNEL_WEIXIN) is False

        monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
        assert has_capability(CapabilityId.CHANNEL_FEISHU) is True
        assert has_capability(CapabilityId.CHANNEL_DISCORD) is True
        assert has_capability(CapabilityId.CHANNEL_WEIXIN) is True

    def test_fingerprint_is_stable_and_sensitive_without_mutable_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env="core")
        fp_core_1 = capabilities_fingerprint()
        fp_core_2 = capabilities_fingerprint()
        assert len(fp_core_1) == 16
        assert fp_core_1 == fp_core_2

        monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "true")
        fp_with_jobs = capabilities_fingerprint()
        assert fp_with_jobs != fp_core_1

        monkeypatch.delenv("DALTON_CAP_DATA_JOBS", raising=False)
        assert capabilities_fingerprint() == fp_core_1

        _clean_cap_env(monkeypatch, app_env="dalton")
        fp_everflow = capabilities_fingerprint()
        assert fp_everflow != fp_core_1


# ---------------------------------------------------------------------------
# 2. Anti-leakage gate #1: src/dalton/tools/ (constructed tools + AST + per-cap)
# ---------------------------------------------------------------------------


def _construct_all_exported_dalton_tools() -> dict[str, set[str]]:
    """Import every ``src/dalton/tools/*.py``, construct actual ``BaseTool`` objects, and collect their real ``.name``."""
    discovered: dict[str, set[str]] = {}
    for py_file in sorted(DALTON_TOOLS_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        mod = importlib.import_module(f"src.dalton.tools.{py_file.stem}")
        found_names: set[str] = set()

        # 1) Module-level BaseTool instances
        for obj in vars(mod).values():
            if isinstance(obj, BaseTool):
                found_names.add(obj.name)

        # 2) Exported factory functions (create_* / build_*) returning BaseTool or list[BaseTool]
        for attr_name, obj in vars(mod).items():
            if not callable(obj) or getattr(obj, "__module__", None) != mod.__name__:
                continue
            if not (attr_name.startswith("create_") or attr_name.startswith("build_")):
                continue
            if inspect.iscoroutinefunction(obj):
                continue
            sig = inspect.signature(obj)
            # Invoke across both roles when factory accepts `role`
            param_names = set(sig.parameters.keys())
            call_variants: list[dict[str, Any]] = []
            if "role" in param_names:
                for role in ("analyst", "chief_of_staff"):
                    kw: dict[str, Any] = {"role": role}
                    if "filesystem_backend" in param_names:
                        kw["filesystem_backend"] = None
                    call_variants.append(kw)
            else:
                kw = {}
                if "filesystem_backend" in param_names:
                    kw["filesystem_backend"] = None
                if "allow_home_workspace" in param_names:
                    kw["allow_home_workspace"] = False
                # Skip helpers with required positional arguments without defaults
                required_missing = [
                    p.name
                    for p in sig.parameters.values()
                    if p.default is inspect.Parameter.empty
                    and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                    and p.name not in kw
                ]
                if not required_missing:
                    call_variants.append(kw)

            for kw in call_variants:
                result = obj(**kw)
                if isinstance(result, BaseTool):
                    found_names.add(result.name)
                elif isinstance(result, (list, tuple)):
                    for item in result:
                        if isinstance(item, BaseTool):
                            found_names.add(item.name)

        if found_names:
            discovered[py_file.name] = found_names
    return discovered


def _scan_tool_decorators_ast() -> list[str]:
    """Ensure every ``@tool(...)`` / ``StructuredTool`` definition in ``src/dalton/tools/`` is accounted for."""
    missing_factories: list[str] = []
    for py_file in sorted(DALTON_TOOLS_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        has_tool_def = False
        has_factory = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith(("create_", "build_")):
                    has_factory = True
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Name) and dec.id == "tool":
                        has_tool_def = True
                    elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "tool":
                        has_tool_def = True
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "from_function":
                    has_tool_def = True
                elif isinstance(node.func, ast.Name) and node.func.id == "StructuredTool":
                    has_tool_def = True
        if has_tool_def and not has_factory:
            missing_factories.append(py_file.name)
    return missing_factories


class TestDaltonToolsRegistrationGate:
    def test_every_constructed_tool_in_src_dalton_tools_is_explicitly_registered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_all_capabilities(monkeypatch)
        assert _scan_tool_decorators_ast() == [], (
            "Found tool module(s) with @tool/StructuredTool but no create_*/build_* factory"
        )
        discovered = _construct_all_exported_dalton_tools()
        assert discovered, "Expected to construct BaseTool instances from src/dalton/tools/"

        registered_by_module: dict[str, set[str]] = {}
        for entry in DALTON_TOOL_REGISTRY:
            assert entry.capability is CORE_CAPABILITY or isinstance(entry.capability, CapabilityId), (
                f"Tool registration {entry.id!r} has invalid capability={entry.capability!r}"
            )
            mod_file = entry.module.rsplit(".", 1)[-1] + ".py"
            registered_by_module.setdefault(mod_file, set()).update(entry.tool_names)

        unregistered: list[str] = []
        for fname, names in sorted(discovered.items()):
            reg_names = registered_by_module.get(fname, set())
            for tname in sorted(names - reg_names):
                unregistered.append(f"{fname}:{tname}")

        assert not unregistered, (
            "Unregistered Dalton tool(s) constructed from src/dalton/tools/ — register each in "
            "src/dalton/tools/__init__.py::DALTON_TOOL_REGISTRY: " + ", ".join(unregistered)
        )

        stale: list[str] = []
        for fname, reg_names in sorted(registered_by_module.items()):
            actual_names = discovered.get(fname, set())
            for tname in sorted(reg_names - actual_names):
                stale.append(f"{fname}:{tname}")
        assert not stale, f"Stale tool name(s) in DALTON_TOOL_REGISTRY: {', '.join(stale)}"

    def test_no_explicit_proprietary_tool_is_marked_core(self) -> None:
        """Item 11 (grok 2): independent check that proprietary tools are never tagged CORE_CAPABILITY."""
        wrongly_core: list[str] = []
        for entry in DALTON_TOOL_REGISTRY:
            if entry.capability is CORE_CAPABILITY:
                for tname in entry.tool_names:
                    if tname in EXPLICIT_PROPRIETARY_TOOLS:
                        wrongly_core.append(f"{entry.id}:{tname}")
        assert not wrongly_core, f"Proprietary tool(s) wrongly marked CORE_CAPABILITY: {wrongly_core}"

    def test_each_capability_gated_tool_unmounts_when_its_capability_is_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 11: individually disable each registered capability and verify its tools actually vanish."""
        for entry in DALTON_TOOL_REGISTRY:
            if entry.capability is CORE_CAPABILITY:
                continue
            _enable_all_capabilities(monkeypatch)
            cap = entry.capability
            for role in sorted(entry.roles):
                mounted_on = {t.name for t in build_dalton_tools(role, None, user_tier="internal")}
                for tname in entry.tool_names:
                    assert tname in mounted_on, (
                        f"Expected {tname!r} to be mounted when {cap.value} is enabled ({role})"
                    )

            monkeypatch.setenv(CAPABILITY_SPECS[cap].env_var, "false")
            for role in sorted(entry.roles):
                mounted_off = {t.name for t in build_dalton_tools(role, None, user_tier="internal")}
                for tname in entry.tool_names:
                    assert tname not in mounted_off, (
                        f"Tool {tname!r} stayed mounted when {cap.value} was disabled ({role})"
                    )

    @pytest.mark.parametrize(
        ("app_env", "dalton_profile"),
        [("core", None), ("", "core")],
    )
    @pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
    @pytest.mark.parametrize("tier", ["admin", "internal", "external", None])
    def test_core_profile_mounts_zero_explicit_proprietary_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str | None,
        role: str,
        tier: str | None,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
        monkeypatch.setattr(app_env_mod, "CHANNEL_GATEWAY_URL", "http://127.0.0.1:8962")
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok-test")

        mounted = {t.name for t in build_dalton_tools(role, None, user_tier=tier)}
        mounted |= {t.name for t in messaging_tools_mod.build_messaging_tools(role)}  # type: ignore[arg-type]

        leaked = sorted(mounted & EXPLICIT_PROPRIETARY_TOOLS)
        assert not leaked, (
            f"Proprietary tool(s) leaked in core profile ({role}, tier={tier}): {leaked}"
        )

        # Verify core skeleton tools remain mounted
        assert "knowledge_update" in mounted
        assert "knowledge_read" in mounted
        assert "check_language" in mounted
        assert "dalton_check_done" in mounted
        assert "dalton_system_jobs" in mounted
        assert "switch_thread_model" in mounted
        assert "dalton_monitor" in mounted
        if role == "analyst":
            assert "check_draft" in mounted
            assert "publish_report" in mounted
        else:
            assert "dalton_coverage" in mounted
            assert "dalton_coverage_board" in mounted

    @pytest.mark.parametrize("app_env", ["dalton", ""])
    def test_everflow_profile_tool_list_matches_snapshot_exactly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env=app_env)
        manifest = json.loads(EVERFLOW_MANIFEST_PATH.read_text(encoding="utf-8"))
        all_known_dalton_tools = {
            tname for entry in DALTON_TOOL_REGISTRY for tname in entry.tool_names
        }

        for role, key in (
            ("analyst", "main_tools_analyst"),
            ("chief_of_staff", "main_tools_chief_of_staff"),
        ):
            expected_dalton = sorted(set(manifest[key]) & all_known_dalton_tools)
            actual_dalton = sorted(
                t.name for t in build_dalton_tools(role, None, user_tier="internal")
            )
            assert actual_dalton == expected_dalton

    def test_explicit_conversation_archive_cap_mounts_tool_and_keeps_tier_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 9 (codex medium 6): DALTON_CAP_CONVERSATION_ARCHIVE=true mounts conversation_search in Core."""
        _clean_cap_env(monkeypatch, app_env="core")
        assert "conversation_search" not in {
            t.name for t in build_dalton_tools("analyst", None, user_tier="internal")
        }

        monkeypatch.setenv("DALTON_CAP_CONVERSATION_ARCHIVE", "true")
        assert has_capability(CapabilityId.CONVERSATION_ARCHIVE) is True
        for role in ("analyst", "chief_of_staff"):
            assert "conversation_search" in {
                t.name for t in build_dalton_tools(role, None, user_tier="internal")
            }
            assert "conversation_search" in {
                t.name for t in build_dalton_tools(role, None, user_tier="admin")
            }
            # External tier check must remain enforced
            assert "conversation_search" not in {
                t.name for t in build_dalton_tools(role, None, user_tier="external")
            }


# ---------------------------------------------------------------------------
# 3. Anti-leakage gate #2: src/dalton/app/ routes + per-route runtime 404
# ---------------------------------------------------------------------------


def _scan_dalton_app_routes_ast() -> set[tuple[str, str]]:
    """AST-scan every ``src/dalton/app/*.py`` for decorator AND ``add_api_route()`` handlers."""
    route_methods = {"get", "post", "put", "patch", "delete", "api_route"}
    discovered: set[tuple[str, str]] = set()
    for py_file in sorted(DALTON_APP_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in route_methods
                    ):
                        discovered.add((py_file.name, node.name))
                        break
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_api_route"
            ):
                endpoint_arg = node.args[1] if len(node.args) >= 2 else next(
                    (kw.value for kw in node.keywords if kw.arg == "endpoint"),
                    None,
                )
                if isinstance(endpoint_arg, ast.Name):
                    discovered.add((py_file.name, endpoint_arg.id))
                else:
                    discovered.add((py_file.name, f"<add_api_route@L{node.lineno}>"))
    return discovered


def _mounted_dalton_app_routes() -> list[tuple[str, str, str, str]]:
    """Inspect real mounted ``APIRoute`` objects under ``src/dalton/app/``: ``(method, path, file, handler)``."""
    out: list[tuple[str, str, str, str]] = []
    for route in dalton_router.routes:
        if not isinstance(route, APIRoute):
            continue
        mod_name = getattr(route.endpoint, "__module__", "")
        if not mod_name.startswith("src.dalton.app."):
            continue
        fname = mod_name.rsplit(".", 1)[-1] + ".py"
        handler = route.endpoint.__name__
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue
            out.append((method, route.path, fname, handler))
    return out


class TestDaltonRoutesRegistrationGate:
    def test_every_mounted_and_ast_route_in_src_dalton_app_is_registered(self) -> None:
        ast_discovered = _scan_dalton_app_routes_ast()
        mounted = _mounted_dalton_app_routes()
        mounted_keys = {(fname, handler) for _, _, fname, handler in mounted}
        assert ast_discovered and mounted_keys

        all_discovered = ast_discovered | mounted_keys
        registered = set(DALTON_ROUTE_CAPABILITY_REGISTRY.keys())
        unregistered = sorted(all_discovered - registered)
        assert not unregistered, (
            "Unregistered Dalton API route(s) found in src/dalton/app/ — register each in "
            "src/dalton/capabilities.py::DALTON_ROUTE_CAPABILITY_REGISTRY: "
            + ", ".join(f"{f}:{fn}" for f, fn in unregistered)
        )

        stale = sorted(registered - all_discovered)
        assert not stale, (
            "Stale entry(ies) in DALTON_ROUTE_CAPABILITY_REGISTRY: "
            + ", ".join(f"{f}:{fn}" for f, fn in stale)
        )

        # Independent prefix check: no route under a proprietary prefix may be marked CORE_CAPABILITY
        wrongly_core: list[str] = []
        for method, path, fname, handler in mounted:
            if any(path.startswith(prefix) for prefix in EXPLICIT_PROPRIETARY_ROUTE_PREFIXES):
                if DALTON_ROUTE_CAPABILITY_REGISTRY[(fname, handler)] is CORE_CAPABILITY:
                    wrongly_core.append(f"{method} {path} ({fname}:{handler})")
        assert not wrongly_core, f"Proprietary route(s) wrongly marked CORE_CAPABILITY: {wrongly_core}"

    def test_every_capability_gated_route_returns_404_when_each_required_capability_is_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 6 & Item 11: test EVERY registered non-CORE route individually (no hand-written subset)."""
        _install_no_outbound_http_guard(monkeypatch)
        client = TestClient(_make_dalton_test_app())
        tested_count = 0

        for method, path, fname, handler in _mounted_dalton_app_routes():
            reg = DALTON_ROUTE_CAPABILITY_REGISTRY[(fname, handler)]
            if reg is CORE_CAPABILITY:
                continue
            required_caps = reg if isinstance(reg, tuple) else (reg,)
            concrete_path = _fill_path_params(path)

            for cap in required_caps:
                _enable_all_capabilities(monkeypatch)
                monkeypatch.setenv(CAPABILITY_SPECS[cap].env_var, "false")
                resp = client.request(method, concrete_path)
                assert resp.status_code == 404, (
                    f"{method} {concrete_path} ({fname}:{handler}) returned {resp.status_code} "
                    f"instead of 404 when {cap.value}=false: {resp.text}"
                )
                tested_count += 1

        assert tested_count >= 50, f"Expected to test all gated routes, only tested {tested_count}"

    def test_explicit_proprietary_prefixes_all_404_in_core(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env="core")
        _install_no_outbound_http_guard(monkeypatch)
        client = TestClient(_make_dalton_test_app())

        matched = [
            (method, path)
            for method, path, _, _ in _mounted_dalton_app_routes()
            if any(path.startswith(prefix) for prefix in EXPLICIT_PROPRIETARY_ROUTE_PREFIXES)
        ]
        assert matched, "Expected proprietary routes mounted on dalton_router"
        for method, path in matched:
            resp = client.request(method, _fill_path_params(path))
            assert resp.status_code == 404, f"Core {method} {path} returned {resp.status_code}: {resp.text}"

        # Also verify ops-* and data:* system-job mutations return 404 in Core
        for path in (
            "/api/v1/dalton/system-jobs/ops-db-backup/run",
            "/api/v1/dalton/system-jobs/data:us_macro_fred/run",
        ):
            resp = client.post(path)
            assert resp.status_code == 404, f"Core POST {path} returned {resp.status_code}"

    def test_capabilities_endpoint_in_core_and_everflow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = TestClient(_make_dalton_test_app())

        _clean_cap_env(monkeypatch, app_env="core")
        resp = client.get("/api/v1/dalton/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        assert body["profile"] == "core"
        assert body["capabilities"] == []
        assert body["brand_name"] == "Dalton"
        assert body["brand"] == {"name": "Dalton"}
        assert body["fingerprint"] == capabilities_fingerprint()

        monkeypatch.setenv("BRAND_NAME", "Acme Research")
        resp_brand = client.get("/api/v1/dalton/capabilities")
        assert resp_brand.status_code == 200
        assert resp_brand.json()["brand_name"] == "Acme Research"
        assert resp_brand.json()["brand"] == {"name": "Acme Research"}

        _clean_cap_env(monkeypatch, app_env="dalton")
        resp_ef = client.get("/api/v1/dalton/capabilities")
        assert resp_ef.status_code == 200
        ef_body = resp_ef.json()
        assert ef_body["profile"] == "everflow"
        assert ef_body["brand_name"] == "Everflow"
        assert "researchdata_bridge" in ef_body["capabilities"]
        assert "channel_gateway" in ef_body["capabilities"]
        assert "transcription" not in ef_body["capabilities"]

    def test_global_search_preserves_user_own_conversations_in_core(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 7 (codex high 3): GET /api/v1/dalton/search keeps include_conversations for user's own threads."""
        _clean_cap_env(monkeypatch, app_env="core")
        assert has_capability(CapabilityId.CONVERSATION_ARCHIVE) is False

        observed: list[dict[str, Any]] = []

        async def _fake_global_search(
            user_id: str,
            query: str,
            limit: int = 20,
            *,
            include_conversations: bool = True,
            workspace_id: str | None = None,
            include_content: bool = True,
            conn: Any = None,
        ) -> dict[str, Any]:
            observed.append(
                {
                    "user_id": user_id,
                    "query": query,
                    "include_conversations": include_conversations,
                }
            )
            return {
                "conversations": [{"kind": "conversation", "thread_id": "t-1", "title": "NVDA notes"}],
                "documents": [],
            }

        spy_search = create_autospec(search_db_mod.global_search, side_effect=_fake_global_search)
        monkeypatch.setattr(search_db_mod, "global_search", spy_search)

        client = TestClient(_make_dalton_test_app())
        resp = client.get("/api/v1/dalton/search?q=NVDA&include_conversations=true")
        assert resp.status_code == 200
        assert observed == [{"user_id": "test-user-1", "query": "NVDA", "include_conversations": True}]
        assert len(resp.json()["conversations"]) == 1

        resp_off = client.get("/api/v1/dalton/search?q=NVDA&include_conversations=false")
        assert resp_off.status_code == 200
        assert observed[-1]["include_conversations"] is False


# ---------------------------------------------------------------------------
# 4. Anti-leakage gate #3: src/server/app/setup.py background loops (full AST)
# ---------------------------------------------------------------------------


def _scan_setup_py_background_loops_full() -> list[tuple[int, str]]:
    """AST-scan ALL of ``src/server/app/setup.py`` (inside & outside ``lifespan``) without deduplicating."""
    tree = ast.parse(SETUP_PY_PATH.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    alias_map = {
        "scanner": "RecoveryScanner",
        "restart_drain": "restart_drain",
        "ws": "MarketDataFeed",
        "automation_scheduler": "AutomationScheduler",
        "price_monitor": "PriceMonitorService",
        "insight_service_inst": "InsightService",
        "news_refresh": "NewsRefreshService",
        "provenance_gc": "ProvenanceGCService",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "create_task":
            task_name: str | None = None
            for kw in node.keywords:
                if (
                    kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    task_name = kw.value.value
            found.append((node.lineno, task_name or f"<unnamed_task@L{node.lineno}>"))
        elif isinstance(node.func, ast.Name) and node.func.id == "start_sweeper":
            found.append((node.lineno, "IdleCompactionSweeper"))
        elif isinstance(node.func, ast.Name) and node.func.id == "start_mcp_builtin_reconnect_task":
            found.append((node.lineno, "mcp-builtin-reconnect"))
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "start":
            target = node.func.value
            if isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute):
                if isinstance(target.func.value, ast.Name):
                    found.append((node.lineno, target.func.value.id))
                else:
                    found.append((node.lineno, ast.unparse(target.func.value)))
            elif isinstance(target, ast.Call) and isinstance(target.func, ast.Name):
                if target.func.id == "_background_singleton":
                    found.append((node.lineno, "RedisBackgroundSingletons"))
                else:
                    found.append((node.lineno, target.func.id))
            elif isinstance(target, ast.Name):
                found.append((node.lineno, alias_map.get(target.id, f"{target.id}@L{node.lineno}")))
    return sorted(found)


class TestSetupBackgroundLoopsGate:
    def test_every_background_loop_in_setup_py_is_registered_and_non_duplicated(self) -> None:
        discovered_sites = _scan_setup_py_background_loops_full()
        assert discovered_sites

        seen: dict[str, list[int]] = {}
        for lineno, name in discovered_sites:
            seen.setdefault(name, []).append(lineno)

        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        assert not duplicates, f"Duplicate background loop launch sites in setup.py: {duplicates}"

        registered = set(BACKGROUND_LOOP_REGISTRY.keys())
        discovered_names = set(seen.keys())
        unregistered = sorted(discovered_names - registered)
        assert not unregistered, (
            "Unregistered background loop(s) in src/server/app/setup.py — register each in "
            "src/dalton/capabilities.py::BACKGROUND_LOOP_REGISTRY: " + ", ".join(unregistered)
        )
        stale = sorted(registered - discovered_names)
        assert not stale, f"Stale entry(ies) in BACKGROUND_LOOP_REGISTRY: {', '.join(stale)}"

        # Cross-check setup.py startup guards against capability-gated loop registrations
        setup_source = SETUP_PY_PATH.read_text(encoding="utf-8")
        assert "if is_transcription_reconciliation_enabled():" in setup_source
        assert "if is_data_jobs_scheduler_enabled():" in setup_source
        assert "start_mcp_builtin_reconnect_task(logger)" in setup_source

    def test_background_loop_gates_follow_profile_capabilities(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Core: data_jobs, transcription reconcile, and researchdata MCP reconnect all off
        _clean_cap_env(monkeypatch, app_env="core")
        monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
        monkeypatch.setattr(
            svc_transcriptions,
            "is_transcription_storage_configured",
            create_autospec(
                transcription_storage_mod.is_transcription_storage_configured,
                return_value=True,
            ),
        )
        assert is_data_jobs_scheduler_enabled() is False
        assert svc_transcriptions.is_transcription_reconciliation_enabled() is False
        assert filter_reconnectable_builtin_mcp({"researchdata"}) == set()
        assert filter_reconnectable_builtin_mcp({"researchdata", "firecrawl"}) == {"firecrawl"}

        # Everflow: disabling each loop's registered capability stops that loop
        _enable_all_capabilities(monkeypatch)
        assert svc_transcriptions.is_transcription_reconciliation_enabled() is True
        monkeypatch.setenv("DALTON_CAP_TRANSCRIPTION", "false")
        assert svc_transcriptions.is_transcription_reconciliation_enabled() is False

        _enable_all_capabilities(monkeypatch)
        assert is_data_jobs_scheduler_enabled() is True
        monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "false")
        assert is_data_jobs_scheduler_enabled() is False

    @pytest.mark.asyncio
    async def test_mcp_builtin_reconnect_only_spawns_tasks_for_enabled_servers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 1 (codex high 1, grok 3): when researchdata + firecrawl both fail in Core, only firecrawl reconnects."""
        rd_cfg = MCPServerConfig(
            name="researchdata",
            transport="http",
            url="http://127.0.0.1:8960/mcp",
            enabled=True,
        )
        fc_cfg = MCPServerConfig(
            name="firecrawl",
            transport="http",
            url="https://mcp.firecrawl.dev/v2/mcp",
            enabled=True,
        )
        core_cfg = create_autospec(CoreConfig, instance=True)
        core_cfg.mcp = MCPConfig(servers=[rd_cfg, fc_cfg])
        fake_reg = create_autospec(MCPRegistry, instance=True)
        fake_reg.config = core_cfg
        fake_reg.failed_builtin_remote = {"researchdata", "firecrawl"}

        reconnected: list[str] = []

        async def _fake_reconnect_one(cfg: MCPServerConfig) -> None:
            reconnected.append(cfg.name)

        monkeypatch.setattr(
            mcp_registry_mod,
            "get_global_registry",
            create_autospec(mcp_registry_mod.get_global_registry, return_value=fake_reg),
        )
        monkeypatch.setattr(
            mcp_registry_mod,
            "_reconnect_one",
            create_autospec(mcp_registry_mod._reconnect_one, side_effect=_fake_reconnect_one),
        )

        # Core: both failed -> task starts, ONLY firecrawl is reconnected (never researchdata)
        _clean_cap_env(monkeypatch, app_env="core")
        task = start_mcp_builtin_reconnect_task(logging.getLogger("test.reconnect"))
        assert task is not None
        assert task.get_name() == "mcp-builtin-reconnect"
        await task
        assert reconnected == ["firecrawl"]

        # Core: calling reconnect_builtin_remote() directly also skips researchdata
        reconnected.clear()
        await mcp_registry_mod.reconnect_builtin_remote()
        assert reconnected == ["firecrawl"]

        # Core: only researchdata failed -> no task created at all
        fake_reg.failed_builtin_remote = {"researchdata"}
        assert start_mcp_builtin_reconnect_task(logging.getLogger("test.reconnect")) is None

        # Everflow: both failed -> both researchdata and firecrawl reconnect
        _clean_cap_env(monkeypatch, app_env="dalton")
        fake_reg.failed_builtin_remote = {"researchdata", "firecrawl"}
        reconnected.clear()
        ef_task = start_mcp_builtin_reconnect_task(logging.getLogger("test.reconnect"))
        assert ef_task is not None
        await ef_task
        assert sorted(reconnected) == ["firecrawl", "researchdata"]

    @pytest.mark.asyncio
    async def test_mcp_builtin_reconnect_preserves_whitespace_server_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 3 Item 5 (codex new 2): custom server names with leading/trailing whitespace still reconnect."""
        ws_cfg = MCPServerConfig(
            name="custom remote ",
            transport="http",
            url="https://example.invalid/mcp",
            enabled=True,
        )
        rd_cfg = MCPServerConfig(
            name="researchdata",
            transport="http",
            url="http://127.0.0.1:8960/mcp",
            enabled=True,
        )
        core_cfg = create_autospec(CoreConfig, instance=True)
        core_cfg.mcp = MCPConfig(servers=[ws_cfg, rd_cfg])
        fake_reg = create_autospec(MCPRegistry, instance=True)
        fake_reg.config = core_cfg
        fake_reg.failed_builtin_remote = {"custom remote ", "researchdata"}

        reconnected: list[str] = []

        async def _fake_reconnect_one(cfg: MCPServerConfig) -> None:
            reconnected.append(cfg.name)

        monkeypatch.setattr(
            mcp_registry_mod,
            "get_global_registry",
            create_autospec(mcp_registry_mod.get_global_registry, return_value=fake_reg),
        )
        monkeypatch.setattr(
            mcp_registry_mod,
            "_reconnect_one",
            create_autospec(mcp_registry_mod._reconnect_one, side_effect=_fake_reconnect_one),
        )

        _clean_cap_env(monkeypatch, app_env="core")
        assert filter_reconnectable_builtin_mcp({"custom remote ", "researchdata"}) == {"custom remote "}
        assert filter_reconnect_targets({"custom remote ", "researchdata"}, frozenset({"custom remote "})) == {
            "custom remote "
        }

        task = start_mcp_builtin_reconnect_task(logging.getLogger("test.reconnect.ws"))
        assert task is not None
        await task
        assert reconnected == ["custom remote "]

    @pytest.mark.asyncio
    async def test_lifespan_actually_starts_or_skips_capability_gated_background_loops(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 3 Item 3 (codex med 6): execute real setup.lifespan and capture actual asyncio.create_task launches."""
        # A single worker skips the multi-worker writer guard this test does not set up.
        monkeypatch.delenv("LANGALPHA_WORKERS", raising=False)
        # Core installs sign in with local accounts, whose startup check needs a signing secret.
        monkeypatch.setenv("JWT_SIGNING_SECRET", "test-signing-secret-0123456789abcdef0123456789")
        monkeypatch.setenv("DALTON_TRANSCRIPTION_TICKET_SECRET", "test-ticket-secret-0123456789abcdef0123456789")
        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _pool_conn_ctx():
            yield fake_conn

        fake_pool = create_autospec(AsyncConnectionPool, instance=True)
        fake_pool._conninfo = "postgresql://u:p@127.0.0.1:5432/testdb"
        fake_pool.connection.side_effect = _pool_conn_ctx

        monkeypatch.setattr(
            db_pool_mod,
            "get_or_create_pool",
            create_autospec(db_pool_mod.get_or_create_pool, return_value=fake_pool),
        )
        monkeypatch.setattr(
            user_db_mod,
            "get_user",
            create_autospec(user_db_mod.get_user, return_value={"name": "Local User"}),
        )
        monkeypatch.setattr(
            retired_names_mod,
            "rename_retired_names",
            create_autospec(retired_names_mod.rename_retired_names, return_value=None),
        )
        monkeypatch.setattr(
            redis_cache_mod,
            "init_cache",
            create_autospec(redis_cache_mod.init_cache, return_value=None),
        )
        monkeypatch.setattr(
            redis_cache_mod,
            "close_cache",
            create_autospec(redis_cache_mod.close_cache, return_value=None),
        )
        monkeypatch.setattr(
            ptc_config_mod,
            "load_from_files",
            create_autospec(
                ptc_config_mod.load_from_files,
                side_effect=FileNotFoundError("skip full PTC init in unit test"),
            ),
        )
        monkeypatch.setattr(
            jwt_bearer_mod,
            "warm_jwks",
            create_autospec(jwt_bearer_mod.warm_jwks, return_value=None),
        )
        monkeypatch.setattr(
            oauth_resource_mod,
            "warm_oauth_resource_keys",
            create_autospec(oauth_resource_mod.warm_oauth_resource_keys, return_value=None),
        )
        monkeypatch.setattr(
            brand_icons_mod,
            "stop_refreshes",
            create_autospec(brand_icons_mod.stop_refreshes, return_value=None),
        )
        monkeypatch.setattr(
            restart_drain_mod,
            "start",
            create_autospec(restart_drain_mod.start, return_value=None),
        )
        monkeypatch.setattr(
            restart_drain_mod,
            "stop",
            create_autospec(restart_drain_mod.stop, return_value=None),
        )
        monkeypatch.setattr(
            heap_settle_mod,
            "run",
            create_autospec(heap_settle_mod.run, return_value=None),
        )
        monkeypatch.setattr(
            gc,
            "freeze",
            create_autospec(gc.freeze, return_value=None),
        )
        monkeypatch.setattr(
            svc_transcriptions,
            "is_transcription_storage_configured",
            create_autospec(
                transcription_storage_mod.is_transcription_storage_configured,
                return_value=True,
            ),
        )
        monkeypatch.setattr(
            server_setup_mod,
            "is_brokerage_marks_warm_on_startup_enabled",
            create_autospec(
                server_setup_mod.is_brokerage_marks_warm_on_startup_enabled,
                return_value=False,
            ),
        )

        fake_reg = create_autospec(MCPRegistry, instance=True)
        fake_reg.failed_builtin_remote = {"researchdata"}
        monkeypatch.setattr(
            mcp_registry_mod,
            "get_global_registry",
            create_autospec(mcp_registry_mod.get_global_registry, return_value=fake_reg),
        )

        created_tasks: list[str] = []

        def _capture_create_task(coro: Any, *, name: str | None = None, context: Any = None) -> Any:
            created_tasks.append(name or "")
            if inspect.iscoroutine(coro):
                coro.close()
            fake_task = create_autospec(asyncio.Task, instance=True)
            fake_task.get_name.return_value = name or ""
            return fake_task

        monkeypatch.setattr(
            asyncio,
            "create_task",
            create_autospec(asyncio.create_task, side_effect=_capture_create_task),
        )

        async def _fake_gather(*aws: Any, return_exceptions: bool = False) -> list[Any]:
            return []

        monkeypatch.setattr(
            asyncio,
            "gather",
            create_autospec(asyncio.gather, side_effect=_fake_gather),
        )

        async def _run_lifespan() -> list[str]:
            created_tasks.clear()
            monkeypatch.setenv("DALTON_HEALTH_ON_STARTUP", "0")
            async with server_setup_mod.lifespan(FastAPI()):
                pass
            return list(created_tasks)

        # 1) Everflow all enabled -> all 3 capability-gated background loops launch
        _enable_all_capabilities(monkeypatch)
        names_all_on = await _run_lifespan()
        assert "dalton-transcription-reconcile" in names_all_on
        assert "dalton-data-task-scheduler" in names_all_on
        assert "mcp-builtin-reconnect" in names_all_on

        # 2) Disable TRANSCRIPTION only -> transcription reconcile does NOT launch
        _enable_all_capabilities(monkeypatch)
        monkeypatch.setenv("DALTON_CAP_TRANSCRIPTION", "false")
        names_no_asr = await _run_lifespan()
        assert "dalton-transcription-reconcile" not in names_no_asr
        assert "dalton-data-task-scheduler" in names_no_asr
        assert "mcp-builtin-reconnect" in names_no_asr

        # 3) Disable DATA_JOBS only -> data task scheduler does NOT launch
        _enable_all_capabilities(monkeypatch)
        monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "false")
        names_no_dj = await _run_lifespan()
        assert "dalton-data-task-scheduler" not in names_no_dj
        assert "dalton-transcription-reconcile" in names_no_dj
        assert "mcp-builtin-reconnect" in names_no_dj

        # 4) Disable RESEARCHDATA_BRIDGE only -> researchdata-only mcp-builtin-reconnect does NOT launch
        _enable_all_capabilities(monkeypatch)
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "false")
        names_no_bridge = await _run_lifespan()
        assert "mcp-builtin-reconnect" not in names_no_bridge
        assert "dalton-transcription-reconcile" not in names_no_bridge
        assert "dalton-data-task-scheduler" in names_no_bridge

        # 5) Core profile -> none of the 3 launch
        _clean_cap_env(monkeypatch, app_env="core")
        monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
        names_core = await _run_lifespan()
        assert "dalton-transcription-reconcile" not in names_core
        assert "dalton-data-task-scheduler" not in names_core
        assert "mcp-builtin-reconnect" not in names_core


# ---------------------------------------------------------------------------
# 5. Messaging tools, individual channels, ingest, transcript health, data_jobs
# ---------------------------------------------------------------------------


class TestBackendCapabilityPruning:
    @pytest.mark.asyncio
    async def test_generic_messaging_tools_and_gateway_request_respect_channel_gateway_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 2 (codex high 2): send_message, list_message_targets, and gateway_request respect CHANNEL_GATEWAY."""
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setattr(app_env_mod, "CHANNEL_GATEWAY_URL", "http://127.0.0.1:8962")
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-token")

        # Enabled in Everflow: mounts both tools
        assert messaging_tools_mod.messaging_enabled() is True
        analyst_tools = {t.name for t in messaging_tools_mod.build_messaging_tools("analyst")}
        assert analyst_tools == {"send_message", "list_message_targets"}

        # Disable CHANNEL_GATEWAY explicitly while URL + token remain configured
        monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "false")
        http_guard = _install_no_outbound_http_guard(monkeypatch)
        assert messaging_tools_mod.messaging_enabled() is False
        assert messaging_tools_mod.build_messaging_tools("analyst") == []
        assert messaging_tools_mod.build_messaging_tools("chief_of_staff") == []

        with pytest.raises(messaging_tools_mod.GatewayError) as exc_info:
            await messaging_tools_mod.gateway_request(
                "GET",
                "/internal/targets",
                user_id="u-1",
                timeout=httpx.Timeout(5.0),
            )
        assert exc_info.value.delivered_unknown is False
        assert http_guard.call_count == 0

    @pytest.mark.asyncio
    async def test_single_channel_disabled_blocks_bind_code_claim_and_bridge_send(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 3 (codex medium 3): disabling channel_feishu alone blocks Feishu bind codes, claims, and bridge sends."""
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "false")
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "s" * 40)
        http_guard = _install_no_outbound_http_guard(monkeypatch)

        # 1) POST /channels/bind-codes rejects feishu with 404
        client = TestClient(_make_dalton_test_app())
        resp_bind = client.post("/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"})
        assert resp_bind.status_code == 404

        # 2) POST /channels/internal/bind-code/consume rejects feishu bind with 404
        resp_consume = client.post(
            "/api/v1/dalton/channels/internal/bind-code/consume",
            headers={"X-Service-Token": "s" * 40},
            json={
                "action": "bind",
                "platform": "feishu",
                "platform_user_id": "ou_1",
                "code": "DLT-AAAA-BBBB",
            },
        )
        assert resp_consume.status_code == 404

        # 3) post_bridge_notify and channel_transport refuse feishu target without HTTP call
        res_bridge = await dispatch_mod.post_bridge_notify(["feishu:user:ou_1"], "hello")
        assert res_bridge["ok"] is False
        assert res_bridge["error_code"] == "capability_disabled"

        res_gw = await transport_mod.post_automation_gateway_notify(
            "00000000-0000-0000-0000-000000000001",
            target="feishu:user:ou_1",
        )
        assert res_gw["ok"] is False
        assert res_gw["error_code"] == "capability_disabled"

        # 4) on_automation_settled strips disabled feishu target BEFORE claiming
        claimed: list[tuple[str, str]] = []

        async def _fake_claim(
            execution_id: str,
            channel: str,
            target: str,
            **kwargs: Any,
        ) -> bool:
            claimed.append((channel, target))
            return True

        async def _fake_post(
            targets: list[str],
            text: str,
            card: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return {
                "ok": True,
                "results": [{"target": t, "ok": True} for t in targets],
            }

        async def _fake_mark(*args: Any, **kwargs: Any) -> None:
            return None

        sent_only_feishu = await dispatch_mod.on_automation_settled(
            execution_id="exec-1",
            automation={
                "name": "NVDA 每日研报",
                "automation_id": "auto-1",
                "user_id": "user-1",
            },
            final_text="summary",
            resolve_enabled_fn=create_autospec(dispatch_mod.resolve_dalton_notify_enabled, return_value=True),
            resolve_overrides_fn=create_autospec(dispatch_mod.resolve_dalton_notify_overrides, return_value={}),
            resolve_targets_fn=create_autospec(
                dispatch_mod.resolve_dalton_notify_targets,
                return_value=["feishu:user:ou_1"],
            ),
            resolve_routes_fn=create_autospec(
                dispatch_mod.resolve_dalton_notify_routes,
                return_value={"automation:auto-1": ["feishu:user:ou_1"]},
            ),
            is_admin_fn=create_autospec(dispatch_mod._default_notify_admin, return_value=False),
            claim_target_fn=create_autospec(dispatch_mod.claim_target_delivery, side_effect=_fake_claim),
            mark_target_fn=create_autospec(dispatch_mod.mark_target_delivery, side_effect=_fake_mark),
            post_notify_fn=create_autospec(dispatch_mod.post_bridge_notify, side_effect=_fake_post),
            get_verdict_fn=create_autospec(dispatch_mod.get_verdict_for_execution, return_value=None),
        )
        assert sent_only_feishu is False
        assert claimed == []

        sent_mixed = await dispatch_mod.on_automation_settled(
            execution_id="exec-2",
            automation={
                "name": "NVDA 每日研报",
                "automation_id": "auto-1",
                "user_id": "user-1",
            },
            final_text="summary",
            resolve_enabled_fn=create_autospec(dispatch_mod.resolve_dalton_notify_enabled, return_value=True),
            resolve_overrides_fn=create_autospec(dispatch_mod.resolve_dalton_notify_overrides, return_value={}),
            resolve_targets_fn=create_autospec(
                dispatch_mod.resolve_dalton_notify_targets,
                return_value=["feishu:user:ou_1", "discord:user:999"],
            ),
            resolve_routes_fn=create_autospec(
                dispatch_mod.resolve_dalton_notify_routes,
                return_value={"automation:auto-1": ["feishu:user:ou_1", "discord:user:999"]},
            ),
            is_admin_fn=create_autospec(dispatch_mod._default_notify_admin, return_value=False),
            claim_target_fn=create_autospec(dispatch_mod.claim_target_delivery, side_effect=_fake_claim),
            mark_target_fn=create_autospec(dispatch_mod.mark_target_delivery, side_effect=_fake_mark),
            post_notify_fn=create_autospec(dispatch_mod.post_bridge_notify, side_effect=_fake_post),
            get_verdict_fn=create_autospec(dispatch_mod.get_verdict_for_execution, return_value=None),
        )
        assert sent_mixed is True
        assert claimed == [("discord", "discord:user:999")]
        assert http_guard.call_count == 0

    @pytest.mark.asyncio
    async def test_wiki_and_sales_notes_low_level_and_unified_ingest_respect_sub_capabilities(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 4 (codex medium 4): disabling COMPANY_WIKI or SALES_NOTES blocks both low-level helpers and ingest_research."""
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "admin-tok")
        http_guard = _install_no_outbound_http_guard(monkeypatch)

        # Bridge is ON, disable COMPANY_WIKI only
        monkeypatch.setenv("DALTON_CAP_COMPANY_WIKI", "false")
        code_w, body_w = await wiki_tools_mod.call_bridge_wiki_ingest({"target": "NVDA"})
        assert code_w == 503
        assert body_w["status"] == "capability_disabled"

        # Unified ingest_research routes expert_or_company -> call_bridge_wiki_ingest -> blocked
        monkeypatch.setattr(
            ingest_tools_mod,
            "_require_internal_ingest_caller",
            create_autospec(
                ingest_tools_mod._require_internal_ingest_caller,
                return_value=("user-1", None),
            ),
        )
        monkeypatch.setattr(
            ingest_tools_mod,
            "_ingest_workspace_identity",
            create_autospec(
                ingest_tools_mod._ingest_workspace_identity,
                return_value=("ws-1", "th-1", None),
            ),
        )
        monkeypatch.setattr(
            ingest_tools_mod,
            "get_workspace_auto_ingest",
            create_autospec(ingest_tools_mod.get_workspace_auto_ingest, return_value=False),
        )

        unified_tool = ingest_tools_mod.build_ingest_research_tool(allow_home_workspace=True)
        res_wiki_raw = await unified_tool.ainvoke(
            {
                "speaker": "expert_or_company",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "expert_call",
                "date": "2026-11-20",
                "content": "Expert call note body",
                "confirm": False,
            },
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-1"}},
        )
        res_wiki = json.loads(res_wiki_raw)
        assert res_wiki["ok"] is False
        assert res_wiki["status"] == "capability_disabled"

        # Now disable SALES_NOTES only (bridge still ON)
        monkeypatch.delenv("DALTON_CAP_COMPANY_WIKI", raising=False)
        monkeypatch.setenv("DALTON_CAP_SALES_NOTES", "false")
        code_s, body_s = await ingest_tools_mod.call_bridge_sales_notes_ingest({"title": "Note"})
        assert code_s == 503
        assert body_s["status"] == "capability_disabled"

        res_sales_raw = await unified_tool.ainvoke(
            {
                "speaker": "sell_side",
                "institution": "BrokerX",
                "title": "Morning note",
                "content": "Sell-side commentary",
                "confirm": False,
            },
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-1"}},
        )
        res_sales = json.loads(res_sales_raw)
        assert res_sales["ok"] is False
        assert res_sales["status"] == "capability_disabled"
        assert http_guard.call_count == 0

    @pytest.mark.asyncio
    async def test_legacy_data_jobs_env_only_pauses_scheduler_while_cap_disables_feature(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 8 (codex medium 1): distinguish DALTON_DATA_JOBS_ENABLED=0/'' vs DALTON_CAP_DATA_JOBS=false and keep internal usage open."""
        client = TestClient(_make_dalton_test_app())

        # Mode A: Everflow + DALTON_DATA_JOBS_ENABLED=0 or "" -> scheduler off, feature & routes stay on
        for raw_val in ("0", ""):
            _clean_cap_env(monkeypatch, app_env="dalton")
            monkeypatch.setenv("DALTON_DATA_JOBS_ENABLED", raw_val)
            assert is_data_jobs_scheduler_enabled() is False
            assert has_capability(CapabilityId.DATA_JOBS) is True

            # Internal data-task routes are NOT 404 (they proceed to auth -> 401 without service token)
            resp_llm = client.post("/api/v1/dalton/internal/llm/complete", json={"model": "x", "user_prompt": "hi"})
            assert resp_llm.status_code != 404
            resp_plan = client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                json={"run_id": "r1", "target": "feishu:user:ou_1"},
            )
            assert resp_plan.status_code != 404
            resp_usage = client.get("/api/v1/dalton/internal/usage/by-model")
            assert resp_usage.status_code != 404

        # Mode B: DALTON_CAP_DATA_JOBS=false -> scheduler off, feature off, data routes 404, usage route STILL unaffected
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "false")
        assert is_data_jobs_scheduler_enabled() is False
        assert has_capability(CapabilityId.DATA_JOBS) is False
        assert client.post("/api/v1/dalton/internal/llm/complete", json={"model": "x", "user_prompt": "hi"}).status_code == 404
        assert (
            client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                json={"run_id": "r1", "target": "feishu:user:ou_1"},
            ).status_code
            == 404
        )
        # Model usage summary is independent of data_jobs capability (401 auth check, never 404)
        assert client.get("/api/v1/dalton/internal/usage/by-model").status_code == 401

        # Also verify in Core profile that internal usage stays independent (401, not 404)
        _clean_cap_env(monkeypatch, app_env="core")
        assert client.get("/api/v1/dalton/internal/usage/by-model").status_code == 401

    def test_url_resolvers_return_empty_when_capabilities_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env="core")
        assert get_dalton_researchdata_url() == ""
        assert get_dalton_bridge_base_url() == ""
        assert get_dalton_bridge_embed_url() == ""
        assert get_dalton_channel_gateway_url() == ""
        assert notify_via_gateway_enabled() is False
        assert cnhk_mod._resolve_bridge_url() == ""
        rd_url, rd_tok, _ = health_mod.resolve_researchdata_endpoint_and_token()
        assert rd_url == ""
        assert rd_tok == ""

        monkeypatch.setenv("DALTON_NOTIFY_VIA_GATEWAY", "1")
        assert notify_via_gateway_enabled() is False

        _clean_cap_env(monkeypatch, app_env="dalton")
        assert ":8960" in get_dalton_bridge_base_url()
        assert ":8960/embed" in get_dalton_bridge_embed_url()
        assert ":8962" in get_dalton_channel_gateway_url()
        ef_url, _, _ = health_mod.resolve_researchdata_endpoint_and_token()
        assert ":8960/mcp" in ef_url

    @pytest.mark.asyncio
    async def test_health_checks_probe_only_enabled_sources_and_bind_transcript_to_alphaengine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Item 5 (codex medium 5): transcript bound to ALPHAENGINE in both catalog discovery and YAML fallback."""
        # 1) Core mode: only market_quotes and sec_filings probed
        _clean_cap_env(monkeypatch, app_env="core")
        _install_no_outbound_http_guard(monkeypatch)

        bridge_spy = create_autospec(
            health_mod.default_bridge_rpc_call,
            side_effect=AssertionError("Bridge probe must not run in core"),
        )
        web_spy = create_autospec(
            health_mod.default_web_search_probe,
            side_effect=AssertionError("WebSearch probe must not run without search key in core"),
        )
        x_spy = create_autospec(
            health_mod.default_x_search_probe,
            side_effect=AssertionError("x_search probe must not run in core"),
        )
        conv_spy = create_autospec(
            health_mod.default_conversation_probe,
            side_effect=AssertionError("conversation_search probe must not run in core"),
        )
        data_task_spy = create_autospec(
            health_mod.check_data_task_runs_health,
            side_effect=AssertionError("data_tasks health check must not run in core"),
        )
        monkeypatch.setattr(health_mod, "check_data_task_runs_health", data_task_spy)

        market_spy = create_autospec(
            health_mod.default_market_quote_probe,
            return_value={"latest_date": "2026-11-20"},
        )
        sec_spy = create_autospec(
            health_mod.default_sec_filings_probe,
            return_value={"latest_date": "2026-11-19"},
        )

        summary = await health_mod.run_all_health_checks(
            bridge_caller=bridge_spy,
            web_search_prober=web_spy,
            x_search_prober=x_spy,
            market_quote_prober=market_spy,
            sec_filings_prober=sec_spy,
            conversation_prober=conv_spy,
            persist=False,
        )
        assert {r["source_id"] for r in summary["results"]} == {"market_quotes", "sec_filings"}
        assert summary["data_task_health"] is None

        # 2) Everflow with ALPHAENGINE disabled: transcript is skipped in BOTH catalog discovery & YAML fallback
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_ALPHAENGINE", "false")
        monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "false")
        monkeypatch.setenv("DALTON_CAP_CONVERSATION_ARCHIVE", "false")

        probed_routes_catalog: list[str] = []

        async def _catalog_bridge_caller(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            timeout_s: float = 45.0,
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                return {
                    "routes": [
                        {"id": "alphaengine", "category": "research"},
                        {"id": "transcript", "category": "research"},
                        {"id": "sec", "category": "finance"},
                    ]
                }
            probed_routes_catalog.append(str(arguments.get("route") or ""))
            return {"ok": True, "date": "2026-11-20"}

        web_ok = create_autospec(health_mod.default_web_search_probe, return_value={"latest_date": "2026-11-20"})
        x_ok = create_autospec(health_mod.default_x_search_probe, return_value={"latest_date": "2026-11-20"})
        conv_ok = create_autospec(health_mod.default_conversation_probe, return_value={"latest_date": "2026-11-20"})

        await health_mod.run_all_health_checks(
            bridge_caller=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_catalog_bridge_caller),
            web_search_prober=web_ok,
            x_search_prober=x_ok,
            market_quote_prober=market_spy,
            sec_filings_prober=sec_spy,
            conversation_prober=conv_ok,
            persist=False,
        )
        assert "alphaengine" not in probed_routes_catalog
        assert "transcript" not in probed_routes_catalog
        assert "sec" in probed_routes_catalog

        # YAML fallback path (catalog() fails)
        probed_routes_yaml: list[str] = []

        async def _fallback_bridge_caller(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            timeout_s: float = 45.0,
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                raise RuntimeError("catalog unavailable")
            probed_routes_yaml.append(str(arguments.get("route") or ""))
            return {"ok": True, "date": "2026-11-20"}

        await health_mod.run_all_health_checks(
            bridge_caller=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_fallback_bridge_caller),
            web_search_prober=web_ok,
            x_search_prober=x_ok,
            market_quote_prober=market_spy,
            sec_filings_prober=sec_spy,
            conversation_prober=conv_ok,
            persist=False,
        )
        assert "alphaengine" not in probed_routes_yaml
        assert "transcript" not in probed_routes_yaml
        assert "sec" in probed_routes_yaml

    @pytest.mark.asyncio
    async def test_system_jobs_hides_ops_jobs_and_skips_bridge_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env="core")
        _install_no_outbound_http_guard(monkeypatch)

        ops = await system_jobs_mod._fetch_ops_jobs()
        assert ops == []

        status_code, payload = await system_jobs_mod._call_host_bridge("GET", "/jobs/ops")
        assert status_code == 503
        assert payload is not None and payload.get("error") == "capability_disabled"

    @pytest.mark.asyncio
    async def test_notify_transport_and_dispatch_never_connect_when_channel_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_cap_env(monkeypatch, app_env="core")
        http_guard = _install_no_outbound_http_guard(monkeypatch)

        res_bridge = await dispatch_mod.post_bridge_notify(["feishu:user:ou_1"], "hello")
        assert res_bridge["ok"] is False
        assert res_bridge["error_code"] == "capability_disabled"

        res_dt = await transport_mod.dispatch_data_task_target_via_gateway(
            run_id="run-1",
            target="feishu:user:ou_1",
            gateway_url="http://127.0.0.1:8962",
            service_token="tok",
        )
        assert res_dt["ok"] is False
        assert res_dt["error_code"] == "capability_disabled"

        res_auto = await transport_mod.post_automation_gateway_notify(
            "00000000-0000-0000-0000-000000000001",
            target="discord:user:12345",
            gateway_url="http://127.0.0.1:8962",
            service_token="tok",
        )
        assert res_auto["ok"] is False
        assert res_auto["error_code"] == "capability_disabled"

        retried = await dispatch_mod.retry_failed_notify_deliveries()
        assert retried == 0

        assert http_guard.call_count == 0

    @pytest.mark.asyncio
    async def test_data_task_first_dispatch_skips_disabled_channel_before_claim(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 3 Item 1 (codex med 2): data task initial delivery checks channel capability BEFORE claiming."""
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "false")
        http_guard = _install_no_outbound_http_guard(monkeypatch)

        fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
        fake_cur.rowcount = 1
        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _tx_ctx():
            yield

        @asynccontextmanager
        async def _cur_ctx(*args: Any, **kwargs: Any):
            yield fake_cur

        fake_conn.transaction.side_effect = _tx_ctx
        fake_conn.cursor.side_effect = _cur_ctx

        monkeypatch.setattr(
            data_jobs_scheduler_mod,
            "check_scheduler_lock",
            create_autospec(data_jobs_scheduler_mod.check_scheduler_lock, return_value=None),
        )

        claimed: list[tuple[str, str]] = []
        sent_targets: list[str] = []

        async def _fake_claim(
            execution_id: str,
            channel: str,
            target: str,
            **kwargs: Any,
        ) -> bool:
            claimed.append((channel, target))
            return True

        async def _fake_post(
            targets: list[str],
            *,
            run_id: str,
            gateway_url: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            sent_targets.extend(targets)
            return {
                "ok": True,
                "results": [{"target": t, "ok": True, "status": "sent"} for t in targets],
            }

        async def _fake_mark(*args: Any, **kwargs: Any) -> bool:
            return True

        # Case 1: only Feishu target -> skipped with 0 claims
        status_feishu_only = await data_jobs_scheduler_mod.dispatch_data_task_run_notification(
            fake_conn,
            lock_conn=fake_conn,
            run_id="00000000-0000-0000-0000-000000000101",
            job_key="us_macro_fred",
            db_outbox_payload={"text": "Macro summary"},
            evaluate_eligibility_fn=create_autospec(
                dispatch_mod.evaluate_user_notify_eligibility,
                return_value=(True, None, ["feishu:user:ou_1"]),
            ),
            claim_target_fn=create_autospec(dispatch_mod.claim_target_delivery, side_effect=_fake_claim),
            post_notify_fn=create_autospec(transport_mod.post_data_task_gateway_notify, side_effect=_fake_post),
            mark_target_fn=create_autospec(dispatch_mod.mark_target_delivery, side_effect=_fake_mark),
            fetch_deliveries_fn=create_autospec(dispatch_mod.fetch_execution_deliveries, return_value=[]),
        )
        assert status_feishu_only == "skipped"
        assert claimed == []
        assert sent_targets == []

        # Case 2: Feishu + Discord -> only Discord is claimed & dispatched
        status_mixed = await data_jobs_scheduler_mod.dispatch_data_task_run_notification(
            fake_conn,
            lock_conn=fake_conn,
            run_id="00000000-0000-0000-0000-000000000102",
            job_key="us_macro_fred",
            db_outbox_payload={"text": "Macro summary"},
            evaluate_eligibility_fn=create_autospec(
                dispatch_mod.evaluate_user_notify_eligibility,
                return_value=(True, None, ["feishu:user:ou_1", "discord:user:999"]),
            ),
            claim_target_fn=create_autospec(dispatch_mod.claim_target_delivery, side_effect=_fake_claim),
            post_notify_fn=create_autospec(transport_mod.post_data_task_gateway_notify, side_effect=_fake_post),
            mark_target_fn=create_autospec(dispatch_mod.mark_target_delivery, side_effect=_fake_mark),
            fetch_deliveries_fn=create_autospec(
                dispatch_mod.fetch_execution_deliveries,
                side_effect=[
                    [],
                    [{"channel": "discord", "target": "discord:user:999", "status": "sent"}],
                ],
            ),
        )
        assert status_mixed == "dispatched"
        assert claimed == [("discord", "discord:user:999")]
        assert sent_targets == ["discord:user:999"]
        assert http_guard.call_count == 0

    @pytest.mark.asyncio
    async def test_core_explicit_conversation_archive_capability_allows_actual_search(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 3 Item 2 (codex med 5): DALTON_CAP_CONVERSATION_ARCHIVE=true in Core executes real search."""
        monkeypatch.setattr(
            conversation_tools_mod,
            "get_user_tier",
            create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_workspace_settings",
            create_autospec(
                sharing_mod.get_workspace_settings,
                return_value={"workspace_id": "ws-1", "team_memory_enabled": True},
            ),
        )
        search_mock = create_autospec(
            conversation_tools_mod.hybrid_search_conversations,
            return_value={"ok": True, "count": 1, "cards": [{"id": "card-1", "quote": "NVDA view"}]},
        )
        monkeypatch.setattr(
            conversation_tools_mod,
            "hybrid_search_conversations",
            search_mock,
        )

        # Core + explicit DALTON_CAP_CONVERSATION_ARCHIVE=true (dalton.core.yaml has conversations.enabled=false)
        _clean_cap_env(monkeypatch, app_env="core")
        monkeypatch.setenv("DALTON_CAP_CONVERSATION_ARCHIVE", "true")
        assert get_dalton_config().conversations.enabled is False

        tools = build_dalton_tools("analyst", user_tier="internal")
        conv_tool = next(t for t in tools if t.name == "conversation_search")
        raw_on = await conv_tool.ainvoke(
            {"query": "NVDA"},
            config={"configurable": {"user_id": "u-internal", "workspace_id": "ws-1", "role": "analyst"}},
        )
        payload_on = json.loads(raw_on)
        assert payload_on["ok"] is True
        assert payload_on["count"] == 1
        assert search_mock.call_count == 1

        # Core without override: blocked without calling hybrid_search_conversations
        _clean_cap_env(monkeypatch, app_env="core")
        search_mock.reset_mock()
        raw_off = await conv_tool.ainvoke(
            {"query": "NVDA"},
            config={"configurable": {"user_id": "u-internal", "workspace_id": "ws-1", "role": "analyst"}},
        )
        payload_off = json.loads(raw_off)
        assert payload_off["ok"] is False
        assert payload_off["error"] == "team_memory_disabled"
        assert search_mock.call_count == 0

    @pytest.mark.asyncio
    async def test_retry_failed_notify_deliveries_excludes_disabled_channels_in_sql_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 3 Item 4 (codex new 1): disabled channels are filtered out in SQL before LIMIT 5."""
        executed_queries: list[tuple[str, tuple[Any, ...]]] = []

        fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)

        async def _fake_execute(sql: Any, params: Any = None) -> None:
            executed_queries.append((str(sql), tuple(params or ())))

        fake_cur.execute.side_effect = _fake_execute
        fake_cur.fetchall.return_value = []

        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _cur_ctx(*args: Any, **kwargs: Any):
            yield fake_cur

        @asynccontextmanager
        async def _conn_ctx(*args: Any, **kwargs: Any):
            yield fake_conn

        fake_conn.cursor.side_effect = _cur_ctx
        monkeypatch.setattr(
            dispatch_mod,
            "get_db_connection",
            create_autospec(dispatch_mod.get_db_connection, side_effect=_conn_ctx),
        )

        dummy_post = create_autospec(dispatch_mod.post_bridge_notify, return_value={"ok": True, "results": []})

        # 1) Everflow (all channels enabled): no channel exclusion clause added (exact original SQL)
        _clean_cap_env(monkeypatch, app_env="dalton")
        assert disabled_channel_names() == []
        executed_queries.clear()
        await dispatch_mod.retry_failed_notify_deliveries(post_notify_fn=dummy_post, limit=5)
        assert len(executed_queries) >= 1
        ef_sql, ef_params = executed_queries[0]
        assert "LOWER(COALESCE(d.channel, '')) <> ALL" not in ef_sql
        assert ef_params[-1] == 5

        # 2) Feishu disabled: SQL excludes feishu BEFORE ORDER BY ... LIMIT 5
        monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "false")
        assert disabled_channel_names() == ["feishu"]
        executed_queries.clear()
        await dispatch_mod.retry_failed_notify_deliveries(post_notify_fn=dummy_post, limit=5)
        assert len(executed_queries) >= 1
        filtered_sql, filtered_params = executed_queries[0]
        excl_idx = filtered_sql.index("LOWER(COALESCE(d.channel, '')) <> ALL(%s)")
        order_idx = filtered_sql.index("ORDER BY d.updated_at ASC")
        assert excl_idx < order_idx
        assert filtered_params[-2] == ["feishu"]
        assert filtered_params[-1] == 5

    @pytest.mark.asyncio
    async def test_weixin_qr_proxy_returns_404_when_channel_disabled_and_uses_gateway_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 3 Item 6 (grok rem 1): _proxy_to_gateway_weixin returns 404 when channel/gateway disabled."""
        orig_async_client = httpx.AsyncClient
        orig_async_request = httpx.AsyncClient.request
        http_guard = _install_no_outbound_http_guard(monkeypatch)

        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "false")
        with pytest.raises(HTTPException) as exc_wx:
            await channels_router_mod._proxy_to_gateway_weixin(
                "POST", "/internal/weixin/qrcode/start", user_id="u-1"
            )
        assert exc_wx.value.status_code == 404

        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "false")
        with pytest.raises(HTTPException) as exc_gw:
            await channels_router_mod._proxy_to_gateway_weixin(
                "POST", "/internal/weixin/qrcode/start", user_id="u-1"
            )
        assert exc_gw.value.status_code == 404
        assert http_guard.call_count == 0

        # Enabled: proxies to get_dalton_channel_gateway_url()
        monkeypatch.setattr(httpx, "AsyncClient", orig_async_client)
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("CHANNEL_GATEWAY_URL", "http://gw.internal:8962")
        seen_urls: list[str] = []

        async def _fake_request(self: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
            seen_urls.append(url)
            return httpx.Response(200, json={"session_key": "sk-1", "status": "wait"})

        monkeypatch.setattr(
            httpx.AsyncClient,
            "request",
            create_autospec(orig_async_request, side_effect=_fake_request),
        )
        res = await channels_router_mod._proxy_to_gateway_weixin(
            "POST", "/internal/weixin/qrcode/start", user_id="u-1"
        )
        assert res["session_key"] == "sk-1"
        assert seen_urls == [f"{get_dalton_channel_gateway_url()}/internal/weixin/qrcode/start"]

    @pytest.mark.asyncio
    async def test_post_bridge_notify_checks_per_target_without_dual_master_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 3 Item 7 (grok rem 2): post_bridge_notify filters targets per channel instead of requiring both master switches upfront."""
        # 1) Bridge ON + Feishu OFF + Discord ON -> sends ONLY Discord target
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "false")
        posted_bodies: list[dict[str, Any]] = []

        async def _fake_post(self: Any, url: str, *, json: Any = None, **kwargs: Any) -> httpx.Response:
            posted_bodies.append(dict(json or {}))
            return httpx.Response(
                200,
                json={"ok": True, "results": [{"target": t, "ok": True} for t in (json or {}).get("targets", [])]},
            )

        monkeypatch.setattr(
            httpx.AsyncClient,
            "post",
            create_autospec(httpx.AsyncClient.post, side_effect=_fake_post),
        )
        res_partial = await dispatch_mod.post_bridge_notify(
            ["feishu:user:ou_1", "discord:user:999"],
            "hello",
        )
        assert res_partial["ok"] is True
        assert len(posted_bodies) == 1
        assert posted_bodies[0]["targets"] == ["discord:user:999"]

        # 2) Bridge ON + Channel gateway OFF -> per-target results report capability_disabled, 0 HTTP calls
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "false")
        http_guard = _install_no_outbound_http_guard(monkeypatch)
        res_gw_off = await dispatch_mod.post_bridge_notify(["feishu:user:ou_1"], "hello")
        assert res_gw_off["ok"] is False
        assert res_gw_off["error_code"] == "capability_disabled"
        assert res_gw_off["results"] == [
            {"target": "feishu:user:ou_1", "ok": False, "error": "capability_disabled"}
        ]
        assert http_guard.call_count == 0

        # 3) Bridge OFF + Channel gateway ON -> capability_disabled, 0 HTTP calls
        _clean_cap_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "false")
        res_br_off = await dispatch_mod.post_bridge_notify(["discord:user:999"], "hello")
        assert res_br_off["ok"] is False
        assert res_br_off["error_code"] == "capability_disabled"
        assert http_guard.call_count == 0


# ---------------------------------------------------------------------------
# 10. Anti-leakage gate #4 (Batch 2b): Bundled plugins & MCP servers registry
# ---------------------------------------------------------------------------


class TestBundledPluginsAndMcpRegistrationGate:
    def test_every_shipped_bundle_and_mcp_server_is_registered_with_capability(self) -> None:
        plugins_dir = REPO_ROOT / "plugins"
        disk_bundle_names: set[str] = set()
        disk_server_names: set[str] = set()

        for bundle_dir in sorted(p for p in plugins_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            manifest = json.loads((bundle_dir / "plugin.json").read_text(encoding="utf-8"))
            bname = manifest.get("name")
            assert isinstance(bname, str) and bname, f"{bundle_dir.name}/plugin.json missing name"
            disk_bundle_names.add(bname)

            mcp_path = bundle_dir / "mcp.json"
            if mcp_path.is_file():
                mcp_doc = json.loads(mcp_path.read_text(encoding="utf-8"))
                disk_server_names.update((mcp_doc.get("mcpServers") or {}).keys())

        unregistered_bundles = sorted(disk_bundle_names - set(BUNDLED_PLUGIN_CAPABILITY_MAP.keys()))
        assert not unregistered_bundles, (
            "Unregistered bundled plugin(s) — add each to "
            "src/dalton/capabilities.py::BUNDLED_PLUGIN_CAPABILITY_MAP: "
            + ", ".join(unregistered_bundles)
        )
        stale_bundles = sorted(set(BUNDLED_PLUGIN_CAPABILITY_MAP.keys()) - disk_bundle_names)
        assert not stale_bundles, f"Stale entry(ies) in BUNDLED_PLUGIN_CAPABILITY_MAP: {stale_bundles}"

        unregistered_servers = sorted(disk_server_names - set(MCP_SERVER_CAPABILITY_MAP.keys()))
        assert not unregistered_servers, (
            "Unregistered bundled MCP server(s) — add each to "
            "src/dalton/capabilities.py::MCP_SERVER_CAPABILITY_MAP: "
            + ", ".join(unregistered_servers)
        )

        # Independent proprietary check: dalton-data & researchdata may never be marked CORE_CAPABILITY
        assert BUNDLED_PLUGIN_CAPABILITY_MAP["dalton-data"] is CapabilityId.RESEARCHDATA_BRIDGE
        assert BUNDLED_PLUGIN_CAPABILITY_MAP["dalton-sec"] is CORE_CAPABILITY
        assert MCP_SERVER_CAPABILITY_MAP["researchdata"] is CapabilityId.RESEARCHDATA_BRIDGE
        assert MCP_SERVER_CAPABILITY_MAP["dalton_sec"] is CORE_CAPABILITY

    @pytest.mark.parametrize(
        ("app_env", "dalton_profile"),
        [("core", None), ("", "core")],
    )
    def test_core_profile_hides_dalton_data_and_researchdata_across_config_and_plugins(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str | None,
    ) -> None:
        from ptc_agent.config.plugins import bundled_mcp_servers, bundles
        from src.server.services.plugins.bundled import bundled_names, component_owners

        _clean_cap_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)

        bundle_dir_names = {b.name for b in bundles()}
        assert "dalton_data" not in bundle_dir_names
        assert "dalton_sec" in bundle_dir_names

        api_names = bundled_names()
        assert "dalton-data" not in api_names
        assert "dalton-sec" in api_names

        server_names = {s.name for s in bundled_mcp_servers()}
        assert "researchdata" not in server_names
        assert "dalton_sec" in server_names

        owners = component_owners()
        assert "researchdata" not in owners.servers
        assert owners.servers.get("dalton_sec") == "dalton-sec"


@pytest.fixture(autouse=True)
def _drop_profile_config_caches_after_test():
    """These tests switch APP_ENV / DALTON_PROFILE and reload config; monkeypatch
    restores the env afterwards but not the config caches, so a later test in the
    same worker would read the Dalton or core overlay. Clear them on the way out."""
    yield
    from src.config.core import clear_config_cache as _clear_config_cache
    from src.dalton.settings import get_dalton_config as _get_dalton_config

    _clear_config_cache()
    _get_dalton_config.cache_clear()
# 6. Anti-leakage gate #4 (Batch 3 Part 1): skills, sources.yaml, prompts & README
# ---------------------------------------------------------------------------


_B3_PROPRIETARY_BLACKLIST: tuple[str, ...] = (
    "researchdata",
    "alphaengine",
    "guidepoint",
    "sales-notes",
    "tmtb",
    "funda-ai",
    "feishu",
    "weixin",
    "seed-asr",
    "dalton-13f-tracker",
    "everflow",
)


class TestBatch3BackendTextAndSkillGates:
    def test_all_dalton_skills_and_sources_yaml_explicitly_classified(self) -> None:
        from ptc_agent.agent.middleware.skills import dalton_registry as dr_mod
        from src.dalton.data_map import builder as dm_builder

        disk_skills = {
            p.name
            for p in dr_mod.DALTON_SKILLS_DIR.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        }
        assert set(dr_mod.DALTON_SKILL_CAPABILITY_MAP.keys()) == disk_skills

        spec = dm_builder.load_sources_spec()
        for sec in ("catalog_routes", "direct_tools"):
            for k, entry in (spec.get(sec) or {}).items():
                assert "capability" in entry, f"sources.yaml {sec}.{k} missing capability"
        for item in spec.get("unavailable_sources") or []:
            assert "capability" in item, f"sources.yaml unavailable_sources.{item.get('id')} missing capability"

    def test_core_profile_blocks_disabled_skills_and_emits_zero_proprietary_prompt_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ptc_agent.agent.backends.automations import render_automations_readme
        from ptc_agent.agent.middleware.skills import dalton_registry as dr_mod
        from ptc_agent.agent.middleware.skills import registry as skill_reg_mod
        from ptc_agent.agent.prompts.loader import PromptLoader, reset_loader
        from src.dalton.data_map import builder as dm_builder

        _clean_cap_env(monkeypatch, app_env="core")
        dm_builder.set_turn_data_map(None)
        reset_loader()

        # Disabled skills refused on get_skill() and excluded from sandbox sync
        disabled = dr_mod.disabled_dalton_skills()
        assert len(disabled) == 22
        outside = skill_reg_mod.skills_outside_sandbox()
        for name in disabled:
            assert skill_reg_mod.get_skill(name) is None
            assert name in outside

        # Core prompts, <data_map>, and AutomationsBackend README contain 0 blacklisted terms
        loader = PromptLoader()
        surfaces = {
            "system_analyst": loader.get_system_prompt(role="analyst", channels_enabled=True),
            "system_cos": loader.get_system_prompt(role="chief_of_staff", channels_enabled=True),
            "static_data_map": dm_builder.render_static_data_map_markdown(team_memory_enabled=True),
            "automations_readme": render_automations_readme(),
        }
        for label, text in surfaces.items():
            lowered = text.lower()
            hits = [term for term in _B3_PROPRIETARY_BLACKLIST if term in lowered]
            assert not hits, f"Core {label} leaked proprietary terms: {hits}"

    def test_core_assembled_skills_and_workflows_have_zero_proprietary_terms_and_compile(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
        from ptc_agent.agent.middleware.skills import dalton_registry as dr_mod
        from src.dalton.content_assembly import read_assembled_text

        _clean_cap_env(monkeypatch, app_env="core")
        core_caps = frozenset()

        # All 29 Core-visible Dalton skills assemble cleanly with 0 proprietary hits
        core_skills = sorted(
            name for name in dr_mod.DALTON_SKILL_DEFINITIONS
            if dr_mod.is_dalton_skill_enabled(name)
        )
        assert len(core_skills) == 29
        assert "dalton-13f-tracker" not in core_skills
        assert "dalton-bullbear-tracker" not in core_skills

        for sk in core_skills:
            sk_dir = dr_mod.DALTON_SKILLS_DIR / sk
            for fp in sorted(sk_dir.rglob("*")):
                if not fp.is_file() or fp.suffix.lower() not in (".md", ".txt", ".json", ".yaml", ".yml"):
                    continue
                assembled = read_assembled_text(fp, capabilities=core_caps)
                assert "DALTON_CAP" not in assembled, f"Unresolved marker in {fp}"
                lowered = assembled.lower()
                hits = [term for term in _B3_PROPRIETARY_BLACKLIST if term in lowered]
                assert not hits, f"Core skill {fp.relative_to(REPO_ROOT)} leaked {hits}"

        # All 6 workflows assemble cleanly, pass compile_check, and contain 0 proprietary hits
        wf_root = REPO_ROOT / "workflows"
        for wf_dir in sorted(p for p in wf_root.iterdir() if p.is_dir()):
            for fp in sorted(wf_dir.rglob("*")):
                if not fp.is_file() or fp.suffix.lower() not in (".js", ".json", ".md"):
                    continue
                assembled = read_assembled_text(fp, capabilities=core_caps)
                assert "DALTON_CAP" not in assembled, f"Unresolved marker in {fp}"
                lowered = assembled.lower()
                hits = [term for term in _B3_PROPRIETARY_BLACKLIST if term in lowered]
                assert not hits, f"Core workflow {fp.relative_to(REPO_ROOT)} leaked {hits}"
                if fp.suffix.lower() == ".js":
                    compile_check(assembled)
                elif fp.suffix.lower() == ".json":
                    json.loads(assembled)

    def test_zero_everflowinv_domain_in_code_scripts_and_core_surfaces(self) -> None:
        from ptc_agent.agent.middleware.skills import dalton_registry as dr_mod
        from src.dalton.content_assembly import read_assembled_text

        domain_re = re.compile(r"everflowinv\.com", re.IGNORECASE)
        for root in (
            REPO_ROOT / "src",
            REPO_ROOT / "web" / "src",
            REPO_ROOT / "docs-site" / "src",
        ):
            for fp in sorted(root.rglob("*")):
                if not fp.is_file() or fp.suffix.lower() not in (
                    ".py", ".ts", ".tsx", ".js", ".mjs", ".md", ".mdx", ".json", ".yaml", ".yml", ".j2", ".astro",
                ):
                    continue
                text = fp.read_text(encoding="utf-8", errors="ignore")
                assert not domain_re.search(text), (
                    f"Forbidden internal domain 'everflowinv.com' found in {fp.relative_to(REPO_ROOT)}"
                )

        # Skill scripts must never hardcode everflowinv.com
        for fp in sorted(dr_mod.DALTON_SKILLS_DIR.rglob("*.py")):
            text = fp.read_text(encoding="utf-8", errors="ignore")
            assert not domain_re.search(text), (
                f"Forbidden internal domain 'everflowinv.com' in script {fp.relative_to(REPO_ROOT)}"
            )

        # Core-assembled skill markdown must never contain everflowinv.com
        for fp in sorted(dr_mod.DALTON_SKILLS_DIR.rglob("*.md")):
            assembled = read_assembled_text(fp, capabilities=frozenset())
            assert not domain_re.search(assembled), (
                f"Forbidden internal domain 'everflowinv.com' in Core skill {fp.relative_to(REPO_ROOT)}"
            )
