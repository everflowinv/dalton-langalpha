"""Deployment-scoped capability registry and gating helpers (Standalone Core §3.1).

Single source of truth for which optional Everflow / host-coupled capabilities
are active in the current deployment:

- ``DeploymentProfile.EVERFLOW``: all capabilities default on, except
  ``transcription`` which preserves its existing default-off semantics
  (requires ``DALTON_TRANSCRIPTION_ENABLED`` or ``DALTON_CAP_TRANSCRIPTION``
  plus configured ``DALTON_ASR_STORAGE_*``).
- ``DeploymentProfile.CORE``: all optional capabilities default off.

Evaluation is a pure function with **no mutable module-level cache**, so
multi-worker processes (``--workers N``) and unit tests that mutate env vars
always observe a consistent, deterministic view. Capabilities are static for
a deployment; changing them requires restarting backend processes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeVar

from src.dalton.profile import DeploymentProfile, resolve_profile

logger = logging.getLogger(__name__)
_ServerT = TypeVar("_ServerT")

# Sentinel marking core-universal tools, routes, and background loops that do
# not require any optional capability.
CORE_CAPABILITY: None = None

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSY_VALUES = frozenset({"0", "false", "no", "off"})


class CapabilityId(StrEnum):
    """Optional Dalton capabilities gated at deployment time."""

    RESEARCHDATA_BRIDGE = "researchdata_bridge"  # 宿主机研究桥接总开关
    ALPHAENGINE = "alphaengine"  # 卖方研报与纪要（依赖 researchdata_bridge）
    GUIDEPOINT = "guidepoint"  # 专家访谈库（依赖 researchdata_bridge）
    COMPANY_WIKI = "company_wiki"  # 内部 Wiki 读写（依赖 researchdata_bridge）
    SALES_NOTES = "sales_notes"  # 销售笔记检索与入库（依赖 researchdata_bridge）
    TMTB = "tmtb"  # TMT 总结库（依赖 researchdata_bridge）
    FUNDA_AI = "funda_ai"  # FUNDA AI 研究库（依赖 researchdata_bridge）
    FEISHU_DOCS = "feishu_docs"  # 飞书文档/知识空间读写（依赖 researchdata_bridge）
    TRANSCRIPTION = "transcription"  # 语音转写（依赖 researchdata_bridge + 转写专用存储）
    CHANNEL_GATEWAY = "channel_gateway"  # 多渠道网关总开关
    CHANNEL_FEISHU = "channel_feishu"  # 飞书卡片与群绑定（依赖 channel_gateway）
    CHANNEL_DISCORD = "channel_discord"  # Discord 频道与私信（依赖 channel_gateway）
    CHANNEL_WEIXIN = "channel_weixin"  # 微信扫码与机器人绑定（依赖 channel_gateway）
    CONVERSATION_ARCHIVE = "conversation_archive"  # 历史团队对话归档检索
    DATA_JOBS = "data_jobs"  # 确定性定时数据采集任务与看板源
    FUND_AUTOMATIONS = "fund_automations"  # 内部定时简报模板、市场摘要与播客周报工具
    PROBLEM_REPORTS = "problem_reports"  # 内部运行问题工单上报工具（report_issue）
    NATIVE_OAUTH_SYNC = "native_oauth_sync"  # 私有订阅 OAuth 模型目录与同步面板


@dataclass(frozen=True)
class CapabilitySpec:
    """Static metadata describing one optional capability."""

    id: CapabilityId
    description: str
    depends_on: tuple[CapabilityId, ...] = ()
    everflow_default: bool = True
    core_default: bool = False
    legacy_env_vars: tuple[str, ...] = ()
    yaml_keys: tuple[str, ...] = ()

    @property
    def env_var(self) -> str:
        """Canonical explicit override env var (``DALTON_CAP_<NAME>``)."""
        return f"DALTON_CAP_{self.id.value.upper()}"


_RAW_CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        id=CapabilityId.RESEARCHDATA_BRIDGE,
        description="宿主机研究桥接总开关",
        everflow_default=True,
        core_default=False,
        legacy_env_vars=("DALTON_RESEARCHDATA_URL", "DALTON_BRIDGE_URL"),
        yaml_keys=("capabilities.researchdata_bridge",),
    ),
    CapabilitySpec(
        id=CapabilityId.ALPHAENGINE,
        description="卖方研报与纪要（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.alphaengine",),
    ),
    CapabilitySpec(
        id=CapabilityId.GUIDEPOINT,
        description="专家访谈库（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.guidepoint",),
    ),
    CapabilitySpec(
        id=CapabilityId.COMPANY_WIKI,
        description="内部 Wiki 读写（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.company_wiki",),
    ),
    CapabilitySpec(
        id=CapabilityId.SALES_NOTES,
        description="销售笔记检索与入库（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.sales_notes",),
    ),
    CapabilitySpec(
        id=CapabilityId.TMTB,
        description="TMT 总结库（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.tmtb",),
    ),
    CapabilitySpec(
        id=CapabilityId.FUNDA_AI,
        description="FUNDA AI 研究库（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.funda_ai",),
    ),
    CapabilitySpec(
        id=CapabilityId.FEISHU_DOCS,
        description="飞书文档/知识空间读写（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.feishu_docs",),
    ),
    CapabilitySpec(
        id=CapabilityId.TRANSCRIPTION,
        description="语音转写（依赖 researchdata_bridge + 转写专用存储）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=False,
        core_default=False,
        legacy_env_vars=("DALTON_TRANSCRIPTION_ENABLED",),
        yaml_keys=("capabilities.transcription",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_GATEWAY,
        description="多渠道网关总开关",
        everflow_default=True,
        core_default=False,
        legacy_env_vars=("CHANNEL_GATEWAY_URL",),
        yaml_keys=("capabilities.channel_gateway",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_FEISHU,
        description="飞书卡片与群绑定（依赖 channel_gateway）",
        depends_on=(CapabilityId.CHANNEL_GATEWAY,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.channel_feishu",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_DISCORD,
        description="Discord 频道与私信（依赖 channel_gateway）",
        depends_on=(CapabilityId.CHANNEL_GATEWAY,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.channel_discord",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_WEIXIN,
        description="微信扫码与机器人绑定（依赖 channel_gateway）",
        depends_on=(CapabilityId.CHANNEL_GATEWAY,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.channel_weixin",),
    ),
    CapabilitySpec(
        id=CapabilityId.CONVERSATION_ARCHIVE,
        description="历史团队对话归档检索",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.conversation_archive", "conversations.enabled"),
    ),
    CapabilitySpec(
        id=CapabilityId.DATA_JOBS,
        description="确定性定时数据采集任务与看板源",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.data_jobs", "data_jobs.enabled"),
    ),
    CapabilitySpec(
        id=CapabilityId.FUND_AUTOMATIONS,
        description="内部定时简报模板、市场摘要与播客周报工具",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.fund_automations",),
    ),
    CapabilitySpec(
        id=CapabilityId.PROBLEM_REPORTS,
        description="内部运行问题工单上报工具（report_issue）",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.problem_reports",),
    ),
    CapabilitySpec(
        id=CapabilityId.NATIVE_OAUTH_SYNC,
        description="私有订阅 OAuth 模型目录与同步面板",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.native_oauth_sync",),
    ),
)

CAPABILITY_SPECS: Mapping[CapabilityId, CapabilitySpec] = MappingProxyType(
    {spec.id: spec for spec in _RAW_CAPABILITY_SPECS}
)


def _parse_explicit_cap_env(name: str, raw: str | None) -> bool | None:
    """Parse ``DALTON_CAP_<NAME>``; returns ``None`` when unset or blank."""
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    if cleaned in _TRUTHY_VALUES:
        return True
    if cleaned in _FALSY_VALUES:
        return False
    logger.warning(
        "Unrecognized boolean value for %s=%r; treating capability as disabled (fail-closed).",
        name,
        raw,
    )
    return False


def _is_concrete_url_env(raw: str | None) -> bool:
    """Return True when a URL env var holds a non-empty value (not an unexpanded ``${...}``)."""
    if raw is None:
        return False
    cleaned = raw.strip()
    if not cleaned:
        return False
    if cleaned.startswith("${") and cleaned.endswith("}"):
        return False
    return True


def _resolve_transcription_storage_configured() -> bool:
    """Check transcription dedicated storage, honoring test patches on ``services.transcriptions``."""
    svc_mod = sys.modules.get("src.dalton.services.transcriptions")
    if svc_mod is not None:
        fn = getattr(svc_mod, "is_transcription_storage_configured", None)
        if callable(fn):
            return bool(fn())
    from src.dalton.transcription_storage import is_transcription_storage_configured

    return bool(is_transcription_storage_configured())


def _legacy_env_override(
    cap: CapabilityId,
    env: Mapping[str, str],
) -> bool | None:
    """Evaluate backward-compatible dedicated env switches (priority 2)."""
    if cap is CapabilityId.TRANSCRIPTION:
        raw = env.get("DALTON_TRANSCRIPTION_ENABLED")
        if raw is None:
            return None
        cleaned = raw.strip().lower()
        if not cleaned:
            return False
        return cleaned in _TRUTHY_VALUES

    if cap is CapabilityId.RESEARCHDATA_BRIDGE:
        if _is_concrete_url_env(env.get("DALTON_RESEARCHDATA_URL")) or _is_concrete_url_env(
            env.get("DALTON_BRIDGE_URL")
        ):
            return True
        return None

    if cap is CapabilityId.CHANNEL_GATEWAY:
        if _is_concrete_url_env(env.get("CHANNEL_GATEWAY_URL")):
            return True
        return None

    return None


def effective_capabilities(
    *,
    profile: DeploymentProfile | None = None,
    environ: Mapping[str, str] | None = None,
    yaml_capabilities: Mapping[str, bool] | None = None,
    conversations_enabled: bool | None = None,
    data_jobs_enabled: bool | None = None,
    dalton_enabled: bool | None = None,
    transcription_storage_configured: bool | None = None,
) -> frozenset[CapabilityId]:
    """Compute the active capability set as a pure function (no module-level mutable cache).

    Precedence (highest to lowest):
    1. Explicit ``DALTON_CAP_<CAPABILITY_ID>=true|false``
    2. Legacy dedicated env vars (``DALTON_TRANSCRIPTION_ENABLED``,
       ``DALTON_DATA_JOBS_ENABLED``, ``DALTON_RESEARCHDATA_URL`` /
       ``DALTON_BRIDGE_URL``, ``CHANNEL_GATEWAY_URL``)
    3. Profile-merged YAML config (``dalton.yaml`` / ``dalton.core.yaml``)
    4. Profile defaults (Everflow: on except ``transcription``; Core: all off)

    Dependency rules are applied after raw evaluation: any capability whose
    parent capability or runtime prerequisite (such as dedicated ASR storage
    for ``transcription``) is unsatisfied is forced off.
    """
    from src.dalton.settings import (
        get_dalton_config,
        get_profile_dalton_config,
        get_yaml_capabilities,
        is_dalton_enabled,
    )

    env = os.environ if environ is None else environ

    if dalton_enabled is None:
        if environ is not None and "DALTON_ENABLED" in env:
            dalton_on = env["DALTON_ENABLED"].strip().lower() in ("1", "true", "yes")
        else:
            dalton_on = is_dalton_enabled()
    else:
        dalton_on = bool(dalton_enabled)

    if not dalton_on:
        return frozenset()

    active_profile = (
        resolve_profile(
            app_env=env.get("APP_ENV", "") if environ is not None else None,
            dalton_profile=env.get("DALTON_PROFILE", "") if environ is not None else None,
        )
        if profile is None
        else profile
    )

    yaml_caps_map = (
        get_yaml_capabilities(active_profile)
        if yaml_capabilities is None
        else yaml_capabilities
    )

    cfg = None
    if conversations_enabled is None or data_jobs_enabled is None:
        cfg = (
            get_profile_dalton_config(active_profile)
            if profile is not None
            else get_dalton_config()
        )
    conv_yaml = bool(cfg.conversations.enabled) if conversations_enabled is None else bool(conversations_enabled)
    dj_yaml = bool(cfg.data_jobs.enabled) if data_jobs_enabled is None else bool(data_jobs_enabled)

    raw_state: dict[CapabilityId, bool] = {}
    for cap, spec in CAPABILITY_SPECS.items():
        # Priority 1: explicit DALTON_CAP_<NAME>
        explicit = _parse_explicit_cap_env(spec.env_var, env.get(spec.env_var))
        if explicit is not None:
            raw_state[cap] = explicit
            continue

        # Priority 2: legacy dedicated env switches
        legacy = _legacy_env_override(cap, env)
        if legacy is not None:
            raw_state[cap] = legacy
            continue

        # Priority 3: profile-merged YAML config
        if cap.value in yaml_caps_map:
            raw_state[cap] = bool(yaml_caps_map[cap.value])
            continue
        if cap is CapabilityId.CONVERSATION_ARCHIVE:
            raw_state[cap] = conv_yaml
            continue
        if cap is CapabilityId.DATA_JOBS:
            raw_state[cap] = dj_yaml
            continue

        # Priority 4: profile default
        raw_state[cap] = (
            spec.core_default
            if active_profile is DeploymentProfile.CORE
            else spec.everflow_default
        )

    # Dependency closure: disable any capability whose prerequisites are unsatisfied.
    changed = True
    while changed:
        changed = False
        for cap, spec in CAPABILITY_SPECS.items():
            if not raw_state[cap]:
                continue
            if any(not raw_state[dep] for dep in spec.depends_on):
                raw_state[cap] = False
                changed = True

    if raw_state[CapabilityId.TRANSCRIPTION]:
        storage_ok = (
            _resolve_transcription_storage_configured()
            if transcription_storage_configured is None
            else bool(transcription_storage_configured)
        )
        if not storage_ok:
            raw_state[CapabilityId.TRANSCRIPTION] = False

    return frozenset(cap for cap, enabled in raw_state.items() if enabled)


def has_capability(
    cap: CapabilityId | str,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff ``cap`` is enabled in the active deployment."""
    cap_id = cap if isinstance(cap, CapabilityId) else CapabilityId(str(cap).strip())
    active = effective_capabilities() if capabilities is None else capabilities
    return cap_id in active


def capabilities_fingerprint(
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Return a deterministic 16-char hex digest of the enabled capability set."""
    active = effective_capabilities() if capabilities is None else capabilities
    canonical = ",".join(sorted(cap.value for cap in active))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def get_capabilities_payload() -> dict[str, Any]:
    """Build the read-only ``GET /api/v1/dalton/capabilities`` response payload."""
    from src.dalton.settings import get_brand_name

    profile = resolve_profile()
    caps = effective_capabilities(profile=profile)
    brand_name = get_brand_name()
    return {
        "profile": profile.value,
        "capabilities": sorted(cap.value for cap in caps),
        "fingerprint": capabilities_fingerprint(caps),
        "brand_name": brand_name,
        "brand": {
            "name": brand_name,
        },
    }


def require_capability(*caps: CapabilityId) -> None:
    """Raise HTTP 404 when Dalton or any required capability is disabled."""
    from fastapi import HTTPException
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    active = effective_capabilities()
    for cap in caps:
        if cap not in active:
            raise HTTPException(
                status_code=404,
                detail=f"Capability '{cap.value}' is not enabled",
            )


def require_subscription_oauth_enabled() -> None:
    """Raise HTTP 404 when Dalton is enabled and ``native_oauth_sync`` is disabled.

    Gates upstream subscription OAuth endpoints (``/api/v1/oauth/{codex,claude,grok}/*``)
    in Core deployments where subscription sign-ins are disabled and all ``*-oauth``
    models are pruned from the catalog. When Dalton is disabled (upstream OSS),
    returns without error so upstream behavior is unchanged.
    """
    from fastapi import HTTPException
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled():
        return
    if not has_capability(CapabilityId.NATIVE_OAUTH_SYNC):
        raise HTTPException(
            status_code=404,
            detail="Subscription OAuth sign-in is disabled in this deployment",
        )


def require_capability_dep(*caps: CapabilityId) -> Callable[[], None]:
    """Return a FastAPI ``Depends(...)`` guard enforcing ``require_capability(*caps)``."""

    def _guard() -> None:
        require_capability(*caps)

    _guard.__name__ = "require_" + "_and_".join(c.value for c in caps)
    return _guard


def platform_to_channel_capability(platform: str | None) -> CapabilityId | None:
    """Map a channel platform identifier (``feishu`` / ``discord`` / ``weixin``) to its sub-capability."""
    cleaned = str(platform or "").strip().lower()
    if cleaned == "feishu":
        return CapabilityId.CHANNEL_FEISHU
    if cleaned == "discord":
        return CapabilityId.CHANNEL_DISCORD
    if cleaned in ("weixin", "wechat"):
        return CapabilityId.CHANNEL_WEIXIN
    return None


def require_channel_platform_capability(platform: str | None) -> None:
    """Enforce ``channel_gateway`` plus the platform's channel capability (raises HTTP 404 when off)."""
    cap = platform_to_channel_capability(platform)
    if cap is not None:
        require_capability(CapabilityId.CHANNEL_GATEWAY, cap)
    else:
        require_capability(CapabilityId.CHANNEL_GATEWAY)


def channel_target_capability(
    target: str,
    *,
    channel: str | None = None,
) -> CapabilityId | None:
    """Map a notification target prefix or channel name to its sub-capability."""
    lowered = str(target or "").strip().lower()
    if lowered.startswith("feishu:"):
        return CapabilityId.CHANNEL_FEISHU
    if lowered.startswith("discord:"):
        return CapabilityId.CHANNEL_DISCORD
    if lowered.startswith(("weixin:", "wechat:")):
        return CapabilityId.CHANNEL_WEIXIN
    return platform_to_channel_capability(channel)


_CHANNEL_CAPABILITY_BY_NAME: Mapping[str, CapabilityId] = MappingProxyType(
    {
        "feishu": CapabilityId.CHANNEL_FEISHU,
        "discord": CapabilityId.CHANNEL_DISCORD,
        "weixin": CapabilityId.CHANNEL_WEIXIN,
        "wechat": CapabilityId.CHANNEL_WEIXIN,
    }
)


def disabled_channel_names() -> list[str]:
    """Return sorted lowercase channel names whose capability is currently disabled."""
    caps = effective_capabilities()
    gateway_on = CapabilityId.CHANNEL_GATEWAY in caps
    return sorted(
        ch
        for ch, cap in _CHANNEL_CAPABILITY_BY_NAME.items()
        if not gateway_on or cap not in caps
    )


def is_channel_target_enabled(
    target: str,
    *,
    channel: str | None = None,
) -> bool:
    """Return True when channel gateway and (if applicable) the target's channel capability are enabled."""
    caps = effective_capabilities()
    if CapabilityId.CHANNEL_GATEWAY not in caps:
        return False
    sub = channel_target_capability(target, channel=channel)
    if sub is not None and sub not in caps:
        return False
    return True


def is_messaging_gateway_allowed() -> bool:
    """Return True when ``channel_gateway`` allows generic messaging tools and ``gateway_request()``."""
    return has_capability(CapabilityId.CHANNEL_GATEWAY)


# ---------------------------------------------------------------------------
# Built-in MCP server & data-map route capability bindings
# ---------------------------------------------------------------------------

MCP_SERVER_CAPABILITY_MAP: Mapping[str, CapabilityId | None] = MappingProxyType(
    {
        "researchdata": CapabilityId.RESEARCHDATA_BRIDGE,
        "dalton_sec": CORE_CAPABILITY,
        "price_data": CORE_CAPABILITY,
        "fundamentals": CORE_CAPABILITY,
        "macro": CORE_CAPABILITY,
        "options": CORE_CAPABILITY,
        "yf_price": CORE_CAPABILITY,
        "yf_fundamentals": CORE_CAPABILITY,
        "yf_analysis": CORE_CAPABILITY,
        "yf_market": CORE_CAPABILITY,
        "tickertick": CORE_CAPABILITY,
        "x_api": CORE_CAPABILITY,
        "scrape": CORE_CAPABILITY,
        "firecrawl": CORE_CAPABILITY,
    }
)

# Canonical bundled plugin registry: every shipped package under `plugins/`
# must be explicitly classified as CORE_CAPABILITY (None) or a CapabilityId.
BUNDLED_PLUGIN_CAPABILITY_MAP: Mapping[str, CapabilityId | None] = MappingProxyType(
    {
        "alternative-data": CORE_CAPABILITY,
        "dalton": CORE_CAPABILITY,
        "dalton-data": CapabilityId.RESEARCHDATA_BRIDGE,
        "dalton-sec": CORE_CAPABILITY,
        "firecrawl": CORE_CAPABILITY,
        "langalpha-deliverables": CORE_CAPABILITY,
        "langalpha-market-data": CORE_CAPABILITY,
        "langalpha-research": CORE_CAPABILITY,
        "langalpha-service": CORE_CAPABILITY,
        "yfinance": CORE_CAPABILITY,
    }
)


def is_mcp_server_enabled_by_capability(
    server_name: str | None,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff the named MCP server has no disabled capability requirement."""
    cleaned = str(server_name or "").strip()
    if not cleaned:
        return True
    req = MCP_SERVER_CAPABILITY_MAP.get(cleaned)
    if req is None:
        return True
    active = effective_capabilities() if capabilities is None else capabilities
    return req in active


def is_bundled_plugin_enabled_by_capability(
    bundle_name: str | None,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff the named bundled plugin has no disabled capability requirement.

    Accepts both manifest names (``dalton-data``) and directory names
    (``dalton_data``) so raw directory reads and API lookups always agree.
    """
    cleaned = str(bundle_name or "").strip()
    if not cleaned:
        return True
    req = BUNDLED_PLUGIN_CAPABILITY_MAP.get(cleaned)
    if req is None and "_" in cleaned:
        req = BUNDLED_PLUGIN_CAPABILITY_MAP.get(cleaned.replace("_", "-"))
    if req is None:
        return True
    active = effective_capabilities() if capabilities is None else capabilities
    return req in active


def filter_mcp_servers_by_capability(
    servers: Iterable[_ServerT],
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> list[_ServerT]:
    """Filter MCPServerConfig objects to those whose required capability is enabled."""
    active = effective_capabilities() if capabilities is None else capabilities
    return [
        srv
        for srv in servers
        if is_mcp_server_enabled_by_capability(getattr(srv, "name", None), capabilities=active)
    ]


def filter_mcp_server_dicts_by_capability(
    servers: Iterable[Mapping[str, Any]] | None,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> list[Mapping[str, Any]]:
    """Filter raw YAML ``mcp.servers`` entries before merge/validation."""
    if not servers:
        return []
    active = effective_capabilities() if capabilities is None else capabilities
    return [
        srv
        for srv in servers
        if not isinstance(srv, Mapping)
        or is_mcp_server_enabled_by_capability(srv.get("name"), capabilities=active)
    ]


def filter_reconnectable_builtin_mcp(failed_servers: Iterable[str] | None) -> set[str]:
    """Filter ``failed_builtin_remote`` server names to those whose capability is enabled.

    Used by ``src/server/app/setup.py`` (`DALTON-HOOK(standalone-core)`) so a
    disabled server such as ``researchdata`` never triggers the background
    ``mcp-builtin-reconnect`` loop in Standalone Core deployments. Preserves
    the original server name string so exact matching against ``MCPConfig.servers``
    stays identical to upstream for names with leading/trailing whitespace.
    """
    if not failed_servers:
        return set()
    active = effective_capabilities()
    eligible: set[str] = set()
    for name in failed_servers:
        if not isinstance(name, str) or not name:
            continue
        req = MCP_SERVER_CAPABILITY_MAP.get(name.strip())
        if req is None or req in active:
            eligible.add(name)
    return eligible


def filter_reconnect_targets(
    failed_servers: Iterable[str] | None,
    requested: Collection[str] | None = None,
) -> set[str]:
    """Select failed built-in remote servers eligible for background reconnect.

    Applies capability filtering first and, when ``requested`` is provided,
    intersects with ``requested`` so ``reconnect_builtin_remote`` only spawns
    reconnect tasks for enabled servers.
    """
    eligible = filter_reconnectable_builtin_mcp(failed_servers)
    if requested is None:
        return eligible
    allowed = {name for name in requested if isinstance(name, str) and name}
    return eligible & allowed


def start_mcp_builtin_reconnect_task(
    log: logging.Logger | None = None,
) -> asyncio.Task[None] | None:
    """Start the background ``mcp-builtin-reconnect`` task only for enabled failed servers.

    Called from ``src/server/app/setup.py`` via a single ``DALTON-HOOK(standalone-core)``
    delegation line so startup gating, filtered target hand-off, and logging live in Dalton.
    """
    from ptc_agent.core.mcp_registry import get_global_registry, reconnect_builtin_remote

    booted_registry = get_global_registry()
    if booted_registry is None:
        return None
    failed_builtin = filter_reconnectable_builtin_mcp(booted_registry.failed_builtin_remote)
    if not failed_builtin:
        return None
    task = asyncio.create_task(
        reconnect_builtin_remote(frozenset(failed_builtin)),
        name="mcp-builtin-reconnect",
    )
    (log or logger).info(
        "Reconnecting built-in MCP servers in the background: %s",
        ", ".join(sorted(failed_builtin)),
    )
    return task


BRIDGE_ROUTE_CAPABILITY_MAP: Mapping[str, CapabilityId] = MappingProxyType(
    {
        "alphaengine": CapabilityId.ALPHAENGINE,
        "transcript": CapabilityId.ALPHAENGINE,
        "guidepoint": CapabilityId.GUIDEPOINT,
        "company-wiki": CapabilityId.COMPANY_WIKI,
        "sales-notes": CapabilityId.SALES_NOTES,
        "tmtb": CapabilityId.TMTB,
        "funda-ai": CapabilityId.FUNDA_AI,
        "discord-history": CapabilityId.CHANNEL_DISCORD,
    }
)


def is_bridge_route_enabled(
    route_id: str,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff ``researchdata_bridge`` and the route's own capability are enabled."""
    active = effective_capabilities() if capabilities is None else capabilities
    if CapabilityId.RESEARCHDATA_BRIDGE not in active:
        return False
    req = BRIDGE_ROUTE_CAPABILITY_MAP.get(str(route_id or "").strip())
    if req is not None:
        return req in active
    return True


# ---------------------------------------------------------------------------
# Declarative registries for anti-leakage gate tests (§3.4 #1)
# ---------------------------------------------------------------------------

# Every background task (`asyncio.create_task`) and `.start()` background loop
# launched in `src/server/app/setup.py` must be classified here as either
# `CORE_CAPABILITY` (None) or a specific `CapabilityId`.
BACKGROUND_LOOP_REGISTRY: Mapping[str, CapabilityId | None] = MappingProxyType(
    {
        # Dalton lifespan tasks
        "dalton-startup-data-source-health": CORE_CAPABILITY,
        "dalton-transcription-reconcile": CapabilityId.TRANSCRIPTION,
        "dalton-data-task-scheduler": CapabilityId.DATA_JOBS,
        "dalton-chat-upload-cleanup": CORE_CAPABILITY,
        # Filtered by filter_reconnectable_builtin_mcp (skips disabled servers like researchdata)
        "mcp-builtin-reconnect": CORE_CAPABILITY,
        # Upstream core platform background tasks & services
        "warm-brokerage-marks": CORE_CAPABILITY,
        "heap-settle": CORE_CAPABILITY,
        "PlatformSecretSweeper": CORE_CAPABILITY,
        "HookOutboxDrainer": CORE_CAPABILITY,
        "RecoveryScanner": CORE_CAPABILITY,
        "restart_drain": CORE_CAPABILITY,
        "TurnCancelListener": CORE_CAPABILITY,
        "RedisBackgroundSingletons": CORE_CAPABILITY,
        "MarketDataFeed": CORE_CAPABILITY,
        "AutomationScheduler": CORE_CAPABILITY,
        "PriceMonitorService": CORE_CAPABILITY,
        "WakeupSweeper": CORE_CAPABILITY,
        "IdleCompactionSweeper": CORE_CAPABILITY,
        "InsightService": CORE_CAPABILITY,
        "NewsRefreshService": CORE_CAPABILITY,
        "ProvenanceGCService": CORE_CAPABILITY,
        "WorkspaceFileGCService": CORE_CAPABILITY,
    }
)

# Every route handler defined under `src/dalton/app/*.py` must be classified
# here as either `CORE_CAPABILITY` (None), a single `CapabilityId`, or a tuple
# of `CapabilityId` values (all required).
DALTON_ROUTE_CAPABILITY_REGISTRY: Mapping[
    tuple[str, str], CapabilityId | tuple[CapabilityId, ...] | None
] = MappingProxyType(
    {
        # --- router.py ---
        ("router.py", "get_capabilities_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_openclaw_health_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_browser_memory_diagnostics_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_native_models_status_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "check_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "sync_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "retire_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "restore_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "get_providers_status_endpoint"): CORE_CAPABILITY,
        ("router.py", "probe_provider_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_available_models_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_budget_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_budget_endpoint"): CORE_CAPABILITY,
        ("router.py", "upsert_task_cap_endpoint"): CORE_CAPABILITY,
        ("router.py", "delete_task_cap_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_budget_today_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_team_workspaces_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_workspace_sharing_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_workspace_sharing_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_workspace_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_workspace_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_user_sharing_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_user_sharing_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_user_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_user_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_team_credentials_status_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_knowledge_versions_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_knowledge_version_endpoint"): CORE_CAPABILITY,
        ("router.py", "diff_knowledge_versions_endpoint"): CORE_CAPABILITY,
        ("router.py", "import_knowledge_document_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_verdicts_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_publications_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_publication_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_publish_requests_endpoint"): CORE_CAPABILITY,
        ("router.py", "put_dalton_automation_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_coverage_board_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_coverage_units_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_coverage_unit_detail_endpoint"): CORE_CAPABILITY,
        ("router.py", "register_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "bind_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "unbind_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "archive_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_autonomy_report_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_workspace_tracking_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_workspace_tracking_endpoint"): CORE_CAPABILITY,
        # system-jobs endpoints are core for company/sector/home and gate ops/data internally
        ("router.py", "list_system_jobs_endpoint"): CORE_CAPABILITY,
        ("router.py", "toggle_system_job_endpoint"): CORE_CAPABILITY,
        ("router.py", "run_system_job_now_endpoint"): CORE_CAPABILITY,
        ("router.py", "rollback_system_job_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_user_coverage_endpoint"): CORE_CAPABILITY,
        ("router.py", "mutate_user_coverage_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_user_coverage_groups_endpoint"): CORE_CAPABILITY,
        ("router.py", "preview_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "create_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "delete_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "resolve_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_user_earnings_calendar_endpoint"): CORE_CAPABILITY,
        ("router.py", "put_user_earnings_calendar_mirror_endpoint"): CORE_CAPABILITY,
        ("router.py", "create_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_invites_endpoint"): CORE_CAPABILITY,
        ("router.py", "revoke_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "check_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "redeem_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_feishu_read_scopes_endpoint"): CapabilityId.FEISHU_DOCS,
        ("router.py", "update_feishu_read_scopes_endpoint"): CapabilityId.FEISHU_DOCS,
        ("router.py", "list_feishu_spaces_endpoint"): CapabilityId.FEISHU_DOCS,
        ("router.py", "dalton_notify_feishu_bot_endpoint"): CapabilityId.CHANNEL_FEISHU,
        ("router.py", "dalton_notify_feishu_groups_endpoint"): CapabilityId.CHANNEL_FEISHU,
        ("router.py", "dalton_notify_discord_channel_verify_endpoint"): CapabilityId.CHANNEL_DISCORD,
        ("router.py", "dalton_notify_test_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("router.py", "global_search_endpoint"): CORE_CAPABILITY,
        # --- channels_router.py ---
        ("channels_router.py", "internal_inbound_register_and_resolve_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_bind_code_consume_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_session_command_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_state_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_settings_put_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_deliveries_authorize_and_claim_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_deliveries_progress_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_inbound_progress_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_steers_reconcile_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_recover_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_automation_runs_register_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_automation_runs_finish_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_weixin_state_get_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "internal_weixin_state_put_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_create_bind_code_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_get_bindings_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_put_channel_settings_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_mutate_discord_channel_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_DISCORD,
        ),
        ("channels_router.py", "web_delete_binding_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_weixin_qrcode_start_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_qrcode_wait_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_qrcode_verify_code_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        # --- automation_notifications_router.py ---
        ("automation_notifications_router.py", "internal_automation_notification_plan_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        # --- data_task_notifications_router.py ---
        ("data_task_notifications_router.py", "internal_dedicated_feishu_vault_credentials_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_FEISHU,
        ),
        ("data_task_notifications_router.py", "internal_data_task_notification_plan_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.DATA_JOBS,
        ),
        ("data_task_notifications_router.py", "internal_data_task_run_artifact_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.DATA_JOBS,
        ),
        # --- transcriptions.py ---
        ("transcriptions.py", "create_or_submit_transcription_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_usage_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "start_transcription_upload_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "sign_transcription_upload_part_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "complete_transcription_upload_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "abort_transcription_upload_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_status_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_result_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_raw_result_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "retry_transcription_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "cancel_transcription_endpoint"): CapabilityId.TRANSCRIPTION,
        # --- oauth_native.py ---
        ("oauth_native.py", "muse_device_initiate"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "muse_device_poll"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "muse_status"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "muse_disconnect"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_initiate"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_poll"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_callback"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_status"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_disconnect"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_device_initiate"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_device_poll"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_status"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_disconnect"): CapabilityId.NATIVE_OAUTH_SYNC,
        # --- internal_llm_router.py / internal_model_availability_router.py / internal_usage_router.py ---
        ("internal_llm_router.py", "internal_llm_complete"): CapabilityId.DATA_JOBS,
        ("internal_model_availability_router.py", "internal_model_availability_check"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("internal_usage_router.py", "get_usage_by_model_endpoint"): CORE_CAPABILITY,
        # --- chat_uploads.py (core large-file multipart upload) ---
        ("chat_uploads.py", "create_chat_upload_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "upload_chat_part_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "complete_chat_upload_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "get_chat_upload_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "cancel_chat_upload_endpoint"): CORE_CAPABILITY,
    }
)
